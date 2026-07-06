"""Phase 3 — intent scoring, decay, tiering (``recompute_intent``).

Covers the pure scoring helpers (tier map, per-type half-life, org-fit
multiplier, promotability) and the end-to-end ``recompute_org_intent`` /
``recompute_all_intent`` over real ``signals`` rows: decay math, lifecycle
flips (NEW→SCORED, over-age→EXPIRED), tier = most-urgent live signal, ICP
weight + size-band overrides, and the Tier-3 never-promotable rule.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import (
    IcpIntentProfile, IntentSignalSource, IntentSignalStatus, IntentSignalType,
    Org, OrgIntentScore, OrgSizeBand, Signal,
)
from app.services.intent import scoring

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 6, 22, tzinfo=timezone.utc)


# ---- pure helpers ---------------------------------------------------------

async def test_tier_for_signal_maps_each_type():
    t = scoring.tier_for_signal
    assert t(IntentSignalType.NEW_RFP) == 1
    assert t(IntentSignalType.DEV_ROLE_POSTED) == 1
    assert t(IntentSignalType.LAPSED_FUNDER) == 1
    assert t(IntentSignalType.REV_DROP) == 2
    assert t(IntentSignalType.NEW_PROGRAM) == 2
    assert t(IntentSignalType.PEER_FUNDED) == 2
    assert t(IntentSignalType.NEW_501C3) == 3
    assert t(IntentSignalType.CAUSE_MATCH) == 3


async def test_fit_multiplier_sweet_spot_and_override():
    f = scoring.fit_multiplier_for
    assert f(None, None) == 1.0
    assert f(OrgSizeBand.SMALL, None) > 1.0   # sweet spot boosted
    assert f(OrgSizeBand.MID, None) > 1.0
    assert f(OrgSizeBand.MAJOR, None) < 1.0   # large dev shop penalized
    # Profile override wins.
    prof = IcpIntentProfile(name="x", size_band_weights={"major": 2.5})
    assert f(OrgSizeBand.MAJOR, prof) == 2.5


async def test_per_type_half_life_keeps_990_signals_warm():
    # rev_drop must have a much longer half-life than a fresh RFP, else a
    # months-stale 990 signal would decay to nothing on arrival.
    rfp_hl, _ = scoring._half_life_and_max_age(IntentSignalType.NEW_RFP, None)
    rev_hl, rev_max = scoring._half_life_and_max_age(IntentSignalType.REV_DROP, None)
    assert rev_hl > rfp_hl * 5
    assert rev_max > 365


async def test_is_promotable_never_tier3():
    prof = IcpIntentProfile(name="x", promotion_threshold=Decimal("50"))
    t3 = OrgIntentScore(tier=3, intent_score=Decimal("999"))
    assert scoring.is_promotable(t3, prof) is False
    t2_hot = OrgIntentScore(tier=2, intent_score=Decimal("60"))
    assert scoring.is_promotable(t2_hot, prof) is True
    t2_cold = OrgIntentScore(tier=2, intent_score=Decimal("40"))
    assert scoring.is_promotable(t2_cold, prof) is False


# ---- recompute integration ------------------------------------------------

async def _org(db_session, band=OrgSizeBand.SMALL, ein="11-1111111") -> Org:
    org = Org(name="Helpful Nonprofit", ein=ein.replace("-", ""), size_band=band)
    db_session.add(org)
    await db_session.flush()
    return org


async def _signal(db_session, org, *, stype, score, age_days, status=IntentSignalStatus.NEW):
    sig = Signal(
        org_id=org.id, signal_type=stype, source=IntentSignalSource.PROPUBLICA,
        score=Decimal(str(score)), event_date=NOW - timedelta(days=age_days),
        evidence_url="https://example.org/x", summary="why now",
        dedupe_key=f"k:{org.id}:{stype.value}:{age_days}", status=status,
    )
    db_session.add(sig)
    await db_session.flush()
    return sig


async def test_recompute_decays_and_scores_single_signal(db_session):
    org = await _org(db_session)
    # A rev_drop exactly one half-life old → contributes ~half its score.
    hl, _ = scoring._half_life_and_max_age(IntentSignalType.REV_DROP, None)
    sig = await _signal(db_session, org, stype=IntentSignalType.REV_DROP,
                        score=80, age_days=int(hl))

    row = await scoring.recompute_org_intent(db_session, org, now=NOW)
    await db_session.commit()

    fit = scoring.fit_multiplier_for(OrgSizeBand.SMALL, None)  # 1.2
    expected = 80 * 0.5 * fit
    assert float(row.intent_score) == pytest.approx(expected, rel=0.02)
    assert row.tier == 2
    assert row.top_signal_id == sig.id
    assert float(row.fit_multiplier) == pytest.approx(fit)

    await db_session.refresh(sig)
    assert sig.status is IntentSignalStatus.SCORED   # NEW → SCORED


async def test_recompute_expires_over_age_signal(db_session):
    org = await _org(db_session, ein="22-2222222")
    _, max_age = scoring._half_life_and_max_age(IntentSignalType.NEW_RFP, None)
    sig = await _signal(db_session, org, stype=IntentSignalType.NEW_RFP,
                        score=90, age_days=max_age + 30)

    row = await scoring.recompute_org_intent(db_session, org, now=NOW)
    await db_session.commit()

    assert float(row.intent_score) == 0.0
    assert row.tier == 3 and row.top_signal_id is None
    await db_session.refresh(sig)
    assert sig.status is IntentSignalStatus.EXPIRED


async def test_recompute_tier_is_most_urgent_live_signal(db_session):
    org = await _org(db_session, ein="33-3333333")
    # A fresh Tier-1 RFP + an older Tier-2 rev_drop → org tier 1.
    await _signal(db_session, org, stype=IntentSignalType.REV_DROP, score=80, age_days=200)
    rfp = await _signal(db_session, org, stype=IntentSignalType.NEW_RFP, score=70, age_days=2)

    row = await scoring.recompute_org_intent(db_session, org, now=NOW)
    await db_session.commit()

    assert row.tier == 1
    assert row.top_signal_id == rfp.id   # fresh RFP is the strongest contributor
    assert float(row.intent_score) > 0


async def test_recompute_excludes_suppressed(db_session):
    org = await _org(db_session, ein="44-4444444")
    await _signal(db_session, org, stype=IntentSignalType.REV_DROP, score=80,
                  age_days=1, status=IntentSignalStatus.SUPPRESSED)

    row = await scoring.recompute_org_intent(db_session, org, now=NOW)
    await db_session.commit()
    assert float(row.intent_score) == 0.0 and row.tier == 3


async def test_profile_weights_override_score_and_fit(db_session):
    org = await _org(db_session, band=OrgSizeBand.MAJOR, ein="55-5555555")
    await _signal(db_session, org, stype=IntentSignalType.REV_DROP, score=80, age_days=0)
    profile = IcpIntentProfile(
        name="GrantMind", signal_weights={"rev_drop": 2.0},
        size_band_weights={"major": 1.0},
    )
    db_session.add(profile)
    await db_session.flush()

    row = await scoring.recompute_org_intent(db_session, org, profile=profile, now=NOW)
    await db_session.commit()
    # age 0 → no decay; 80 * weight 2.0 * fit 1.0 = 160.
    assert float(row.intent_score) == pytest.approx(160.0, rel=0.001)
    assert float(row.fit_multiplier) == 1.0


async def test_recompute_all_counts_and_persists(db_session):
    a = await _org(db_session, ein="66-6666666")
    b = await _org(db_session, ein="77-7777777")
    await _signal(db_session, a, stype=IntentSignalType.NEW_RFP, score=90, age_days=1)
    await _signal(db_session, b, stype=IntentSignalType.REV_DROP, score=80, age_days=10)
    await db_session.commit()

    counts = await scoring.recompute_all_intent(db_session, now=NOW)
    assert counts["orgs"] == 2
    assert counts["tier1"] == 1 and counts["tier2"] == 1

    rows = (await db_session.execute(select(OrgIntentScore))).scalars().all()
    assert len(rows) == 2
