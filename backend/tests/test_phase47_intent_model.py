"""Phase 1 — Signals & Intent Engine v2 data model.

Round-trips the four new tables and proves the DB-level idempotency the
collectors will rely on: unique ``signals.dedupe_key`` and the per-tenant
EIN dedup on ``orgs``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import (
    IcpIntentProfile,
    IntentSignalSource,
    IntentSignalStatus,
    IntentSignalType,
    Org,
    OrgIntentScore,
    OrgSizeBand,
    Signal,
)

pytestmark = pytest.mark.asyncio


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _make_org(db_session, *, ein="12-3456789", name="Acme Nonprofit") -> Org:
    org = Org(name=name, ein=ein, ntee_code="B82", state="PA",
              annual_revenue=Decimal("750000.00"), size_band=OrgSizeBand.SMALL)
    db_session.add(org)
    await db_session.flush()
    return org


async def test_signal_round_trips_with_all_contract_fields(db_session):
    org = await _make_org(db_session)
    sig = Signal(
        org_id=org.id,
        signal_type=IntentSignalType.REV_DROP,
        source=IntentSignalSource.PROPUBLICA,
        score=Decimal("60"),
        event_date=_now(),
        evidence_url="https://projects.propublica.org/nonprofits/organizations/123456789",
        summary="Grant & contribution revenue fell 32% on the latest 990.",
        raw_payload={"prior": 1100000, "latest": 750000},
        dedupe_key="propublica:rev_drop:123456789:2024",
    )
    db_session.add(sig)
    await db_session.commit()

    loaded = await db_session.scalar(select(Signal).where(Signal.org_id == org.id))
    assert loaded.status is IntentSignalStatus.NEW   # server default
    assert loaded.signal_type is IntentSignalType.REV_DROP
    assert loaded.evidence_url.startswith("https://")
    assert loaded.detected_at is not None            # server default


async def test_dedupe_key_is_unique(db_session):
    org = await _make_org(db_session)

    def _sig(key):
        return Signal(
            org_id=org.id, signal_type=IntentSignalType.NEW_RFP,
            source=IntentSignalSource.GRANTS_GOV, score=Decimal("80"),
            event_date=_now(), evidence_url="https://grants.gov/x",
            summary="New federal RFP matching the org's cause.", dedupe_key=key,
        )

    db_session.add(_sig("grantsgov:rfp:OPP-1:org-1"))
    await db_session.commit()

    db_session.add(_sig("grantsgov:rfp:OPP-1:org-1"))  # same key → reject
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_orgs_deduped_by_ein_within_tenant(db_session):
    await _make_org(db_session, ein="99-9999999", name="First")
    await db_session.commit()

    # Same (tenant=NULL, ein) → partial-unique violation.
    db_session.add(Org(name="Dup", ein="99-9999999"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()

    # Orgs with NO ein are NOT constrained (partial index on ein IS NOT NULL).
    db_session.add_all([Org(name="No EIN A"), Org(name="No EIN B")])
    await db_session.commit()


async def test_intent_score_and_icp_profile_round_trip(db_session):
    org = await _make_org(db_session)
    sig = Signal(
        org_id=org.id, signal_type=IntentSignalType.DEV_ROLE_POSTED,
        source=IntentSignalSource.JOBS, score=Decimal("100"), event_date=_now(),
        evidence_url="https://example.org/careers", summary="Posted a Development Director role.",
        dedupe_key="jobs:dev_role:org-1:role-1",
    )
    db_session.add(sig)
    await db_session.flush()

    db_session.add(OrgIntentScore(
        org_id=org.id, intent_score=Decimal("142.5"), tier=1,
        top_signal_id=sig.id, fit_multiplier=Decimal("1.25"),
    ))
    db_session.add(IcpIntentProfile(
        name="GrantMind nonprofits",
        cause_codes=["A", "B", "P"], geographies=["PA", "NJ"],
        size_band_weights={"small": 1.25, "mid": 1.1, "major": 0.6},
        signal_weights={"dev_role_posted": 1.5, "lapsed_funder": 1.4},
        half_life_days=Decimal("30"), max_signal_age_days=180,
        promotion_threshold=Decimal("120"),
    ))
    await db_session.commit()

    score = await db_session.scalar(select(OrgIntentScore).where(OrgIntentScore.org_id == org.id))
    assert score.tier == 1 and score.top_signal_id == sig.id
    profile = await db_session.scalar(select(IcpIntentProfile))
    assert profile.is_active is True            # server default
    assert profile.signal_weights["dev_role_posted"] == 1.5
