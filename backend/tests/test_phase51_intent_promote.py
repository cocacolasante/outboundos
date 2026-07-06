"""Phase 4 — trigger bridge to outreach (draft only, hard autonomy boundary).

Proves a promotable org produces an approval-pending DRAFT with the why-now
populated, the signal is marked promoted, the owner is notified — and NOTHING
sends or converts: the campaign stays DRAFT, the lead stays PENDING.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models import (
    Campaign, CampaignStatus, ComposeStatus, IcpIntentProfile,
    IntentSignalSource, IntentSignalStatus, IntentSignalType, Lead,
    Notification, Org, OrgIntentScore, OrgSizeBand, SendStatus, Signal,
)
from app.services.intent import promote

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 6, 22, tzinfo=timezone.utc)


async def _setup(db_session, *, tier=1, score="84", stype=IntentSignalType.NEW_RFP,
                 ein="11-1111111", name="Helpful Nonprofit"):
    org = Org(name=name, ein=ein.replace("-", ""), state="WI",
              ntee_code="T31", size_band=OrgSizeBand.SMALL)
    db_session.add(org)
    await db_session.flush()
    sig = Signal(
        org_id=org.id, signal_type=stype, source=IntentSignalSource.GRANTS_GOV,
        score=Decimal("70"), event_date=NOW - timedelta(days=2),
        evidence_url="https://www.grants.gov/search-results-detail/999001",
        summary='New RFP "Capacity Building" from HHS — matches your cause area',
        dedupe_key=f"grantsgov:rfp:999001:{org.id}", status=IntentSignalStatus.SCORED,
    )
    db_session.add(sig)
    await db_session.flush()
    row = OrgIntentScore(
        org_id=org.id, tier=tier, intent_score=Decimal(score),
        top_signal_id=sig.id, fit_multiplier=Decimal("1.2"),
    )
    db_session.add(row)
    await db_session.flush()
    profile = IcpIntentProfile(name="GrantMind", promotion_threshold=Decimal("60"))
    db_session.add(profile)
    await db_session.flush()
    return org, sig, row, profile


async def test_promote_creates_approval_pending_draft_and_marks_promoted(db_session):
    org, sig, row, profile = await _setup(db_session)
    await db_session.commit()

    res = await promote.promote_org(db_session, org, row, profile, now=NOW)
    await db_session.commit()
    assert res["status"] == "promoted"

    lead = await db_session.get(Lead, res["lead_id"])
    camp = await db_session.get(Campaign, res["campaign_id"])

    # The why-now is populated from the EVIDENCED signal (no invented facts).
    assert sig.summary in lead.composed_body
    assert sig.evidence_url in lead.composed_body
    assert org.name in lead.composed_subject
    assert lead.compose_status is ComposeStatus.DONE

    # HARD BOUNDARY: nothing sent / no RUNNING campaign (awaiting approval).
    assert camp.status is CampaignStatus.PREVIEWING
    assert lead.send_status is SendStatus.PENDING

    await db_session.refresh(sig)
    assert sig.status is IntentSignalStatus.PROMOTED

    # Owner notified.
    note = await db_session.scalar(
        select(Notification).where(Notification.dedup_key == f"intent:promote:{sig.id}"))
    assert note is not None and org.name in note.title


async def test_not_promotable_tier3_and_below_threshold(db_session):
    # Tier 3 is never promotable even with a huge score.
    org, sig, row, profile = await _setup(db_session, tier=3, score="999", ein="22-2222222")
    await db_session.commit()
    assert (await promote.promote_org(db_session, org, row, profile, now=NOW))["status"] == "not_eligible"

    # Tier 2 but below the threshold.
    org2, sig2, row2, profile2 = await _setup(db_session, tier=2, score="40", ein="33-3333333")
    await db_session.commit()
    assert (await promote.promote_org(db_session, org2, row2, profile2, now=NOW))["status"] == "not_eligible"

    assert await db_session.scalar(select(func.count()).select_from(Lead)) == 0


async def test_promote_is_idempotent(db_session):
    org, sig, row, profile = await _setup(db_session)
    await db_session.commit()
    assert (await promote.promote_org(db_session, org, row, profile, now=NOW))["status"] == "promoted"
    await db_session.commit()
    # Second attempt: an open draft already exists → no duplicate.
    assert (await promote.promote_org(db_session, org, row, profile, now=NOW))["status"] == "already_promoted"
    await db_session.commit()
    assert await db_session.scalar(select(func.count()).select_from(Lead)) == 1


async def test_promote_eligible_reuses_one_draft_campaign(db_session):
    a, _, _, profile = await _setup(db_session, ein="44-4444444", name="Org A")
    b, _, _, _ = await _setup(db_session, ein="55-5555555", name="Org B")
    # Second org's profile row is redundant; keep only the first active profile.
    await db_session.commit()

    counts = await promote.promote_eligible(db_session, now=NOW)
    assert counts["promoted"] == 2

    leads = (await db_session.execute(select(Lead))).scalars().all()
    assert len({l.campaign_id for l in leads}) == 1   # one shared DRAFT campaign
    camps = (await db_session.execute(select(Campaign))).scalars().all()
    assert len(camps) == 1 and camps[0].status is CampaignStatus.PREVIEWING
    # No sends anywhere.
    assert all(l.send_status is SendStatus.PENDING for l in leads)
