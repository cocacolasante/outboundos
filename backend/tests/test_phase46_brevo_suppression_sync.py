"""Phase 46 — bounce/block/unsubscribe → suppression list + removed from
current campaigns + blocked from future.

Covers the shared suppression core, the real-time event path (blocked/bounce),
the Brevo blocklist reason mapping + bulk apply, and the on-demand endpoint.
"""
from __future__ import annotations

from datetime import time, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.models import (
    Campaign,
    CampaignStatus,
    EmailEventType,
    Lead,
    LeadSequenceState,
    LeadSequenceStatus,
    SendStatus,
    Sequence,
    SequenceNode,
    SequenceNodeKind,
    Suppression,
    SuppressionReason,
)
from app.services import brevo_blocklist, suppression
from app.services.brevo_events import process_event
from sqlalchemy import select

pytestmark = pytest.mark.asyncio


async def _campaign(db_session) -> Campaign:
    c = Campaign(
        name="supp-test", goal="g", tone="t",
        sender_name="A", sender_email="a@example.com",
        schedule_days=[0, 1, 2, 3, 4, 5, 6],
        schedule_time_start=time(0, 0), schedule_time_end=time(23, 59),
        schedule_timezone="UTC", status=CampaignStatus.RUNNING,
    )
    db_session.add(c)
    await db_session.flush()
    return c


async def _lead_with_active_state(db_session, campaign, email, *, send_status=SendStatus.PENDING):
    lead = Lead(campaign_id=campaign.id, email=email, first_name="L", send_status=send_status)
    db_session.add(lead)
    await db_session.flush()
    seq = await db_session.scalar(select(Sequence).where(Sequence.campaign_id == campaign.id))
    if seq is None:
        seq = Sequence(campaign_id=campaign.id, is_published=True)
        db_session.add(seq)
        await db_session.flush()
    node = SequenceNode(
        sequence_id=seq.id, kind=SequenceNodeKind.EMAIL,
        config={"subject_template": "s", "body_template": "b"}, is_entry=True,
    )
    db_session.add(node)
    await db_session.flush()
    db_session.add(LeadSequenceState(
        sequence_id=seq.id, lead_id=lead.id, current_node_id=node.id,
        status=LeadSequenceStatus.ACTIVE,
    ))
    await db_session.flush()
    return lead


# --------------------------------------------------------------------------
# Shared suppression core
# --------------------------------------------------------------------------


async def test_suppress_email_lists_halts_and_removes(db_session):
    campaign = await _campaign(db_session)
    lead = await _lead_with_active_state(db_session, campaign, "Bounce@X.com")
    await db_session.commit()

    res = await suppression.suppress_email(db_session, "bounce@x.com", SuppressionReason.HARD_BOUNCE)
    await db_session.commit()

    assert res.suppressed and not res.already_suppressed
    assert res.leads_halted == 1
    assert res.leads_marked_suppressed == 1
    assert campaign.id in res.campaigns_affected

    # On the ignore list (canonical-lowercase).
    sup = await db_session.scalar(select(Suppression).where(Suppression.email == "bounce@x.com"))
    assert sup is not None and sup.reason is SuppressionReason.HARD_BOUNCE
    # Sequence halted (removed from current campaign).
    state = await db_session.scalar(select(LeadSequenceState).where(LeadSequenceState.lead_id == lead.id))
    assert state.status is LeadSequenceStatus.HALTED
    # Pulled from the send queue.
    await db_session.refresh(lead)
    assert lead.send_status is SendStatus.SUPPRESSED


async def test_suppress_email_idempotent(db_session):
    campaign = await _campaign(db_session)
    await _lead_with_active_state(db_session, campaign, "dup@x.com")
    await db_session.commit()

    await suppression.suppress_email(db_session, "dup@x.com", SuppressionReason.UNSUBSCRIBED)
    await db_session.commit()
    res2 = await suppression.suppress_email(db_session, "dup@x.com", SuppressionReason.UNSUBSCRIBED)
    await db_session.commit()

    assert res2.already_suppressed is True
    rows = (await db_session.execute(select(Suppression).where(Suppression.email == "dup@x.com"))).scalars().all()
    assert len(rows) == 1  # no duplicate suppression row


async def test_suppress_email_leaves_sent_leads_untouched(db_session):
    campaign = await _campaign(db_session)
    lead = await _lead_with_active_state(db_session, campaign, "sent@x.com", send_status=SendStatus.SENT)
    await db_session.commit()

    await suppression.suppress_email(db_session, "sent@x.com", SuppressionReason.SPAM)
    await db_session.commit()
    await db_session.refresh(lead)
    assert lead.send_status is SendStatus.SENT  # already-sent is history; not rewritten


# --------------------------------------------------------------------------
# Real-time event path
# --------------------------------------------------------------------------


async def test_process_event_blocked_suppresses_and_halts(db_session):
    campaign = await _campaign(db_session)
    lead = await _lead_with_active_state(db_session, campaign, "blk@x.com")
    lead.brevo_message_id = "msg-blk-1"
    await db_session.commit()

    recorded = await process_event(db_session, {"event": "blocked", "messageId": "msg-blk-1"})
    await db_session.commit()

    assert recorded is True
    sup = await db_session.scalar(select(Suppression).where(Suppression.email == "blk@x.com"))
    assert sup is not None and sup.reason is SuppressionReason.BLOCKED
    state = await db_session.scalar(select(LeadSequenceState).where(LeadSequenceState.lead_id == lead.id))
    assert state.status is LeadSequenceStatus.HALTED
    await db_session.refresh(lead)
    assert lead.send_status is SendStatus.SUPPRESSED


async def test_soft_bounce_suppresses_at_threshold_one(db_session, monkeypatch):
    """Default threshold = 1: a single soft bounce suppresses the address
    (protecting sender reputation) and removes it from the campaign."""
    monkeypatch.setattr("app.services.brevo_events.settings.SOFT_BOUNCE_SUPPRESS_THRESHOLD", 1)
    campaign = await _campaign(db_session)
    lead = await _lead_with_active_state(db_session, campaign, "soft@x.com")
    lead.brevo_message_id = "soft-1"
    await db_session.commit()

    recorded = await process_event(db_session, {"event": "soft_bounce", "message-id": "soft-1"})
    await db_session.commit()

    assert recorded is True
    sup = await db_session.scalar(select(Suppression).where(Suppression.email == "soft@x.com"))
    assert sup is not None and sup.reason is SuppressionReason.SOFT_BOUNCE
    state = await db_session.scalar(select(LeadSequenceState).where(LeadSequenceState.lead_id == lead.id))
    assert state.status is LeadSequenceStatus.HALTED
    await db_session.refresh(lead)
    assert lead.send_status is SendStatus.SUPPRESSED


async def test_soft_bounce_threshold_zero_does_not_suppress(db_session, monkeypatch):
    """Threshold 0 keeps the old behaviour: soft bounces are recorded but never
    suppress (Brevo still escalates persistent ones to hard bounces/blocks)."""
    monkeypatch.setattr("app.services.brevo_events.settings.SOFT_BOUNCE_SUPPRESS_THRESHOLD", 0)
    campaign = await _campaign(db_session)
    lead = await _lead_with_active_state(db_session, campaign, "soft0@x.com")
    lead.brevo_message_id = "soft0-1"
    await db_session.commit()

    recorded = await process_event(db_session, {"event": "soft_bounce", "message-id": "soft0-1"})
    await db_session.commit()

    assert recorded is True  # the event row is still recorded
    sup = await db_session.scalar(select(Suppression).where(Suppression.email == "soft0@x.com"))
    assert sup is None  # but the address is NOT suppressed


# --------------------------------------------------------------------------
# Brevo blocklist sync
# --------------------------------------------------------------------------


async def test_map_reason():
    m = brevo_blocklist._map_reason
    assert m("hardBounce") is SuppressionReason.HARD_BOUNCE
    assert m("unsubscribedViaApi") is SuppressionReason.UNSUBSCRIBED
    assert m("contactFlaggedAsSpam") is SuppressionReason.SPAM
    assert m("adminBlocked") is SuppressionReason.BLOCKED
    assert m(None) is SuppressionReason.BLOCKED  # unknown → blocked


async def test_apply_blocked_contacts_suppresses_each(db_session):
    campaign = await _campaign(db_session)
    await _lead_with_active_state(db_session, campaign, "a@x.com")
    await _lead_with_active_state(db_session, campaign, "b@x.com")
    await db_session.commit()

    contacts = [
        {"email": "a@x.com", "reason": {"code": "hardBounce"}},
        {"email": "b@x.com", "reason": {"code": "unsubscribedViaApi"}},
        {"reason": {"code": "hardBounce"}},  # no email → skipped
    ]
    res = await brevo_blocklist.apply_blocked_contacts(db_session, contacts)

    assert res.fetched == 3
    assert res.newly_suppressed == 2
    assert res.leads_halted == 2
    a = await db_session.scalar(select(Suppression).where(Suppression.email == "a@x.com"))
    b = await db_session.scalar(select(Suppression).where(Suppression.email == "b@x.com"))
    assert a.reason is SuppressionReason.HARD_BOUNCE
    assert b.reason is SuppressionReason.UNSUBSCRIBED


async def test_sync_blocklist_endpoint(client, db_session, monkeypatch):
    monkeypatch.setattr("app.routers.settings.settings.BREVO_API_KEY", "test-key")
    campaign = await _campaign(db_session)
    await _lead_with_active_state(db_session, campaign, "endpoint@x.com")
    await db_session.commit()

    fake = AsyncMock(return_value=[{"email": "endpoint@x.com", "reason": {"code": "hardBounce"}}])
    with patch("app.services.brevo_blocklist.brevo.fetch_blocked_contacts", new=fake):
        resp = await client.post("/settings/brevo/sync-blocklist")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["fetched"] == 1
    assert body["newly_suppressed"] == 1
    assert body["leads_halted"] == 1
    sup = await db_session.scalar(select(Suppression).where(Suppression.email == "endpoint@x.com"))
    assert sup is not None and sup.reason is SuppressionReason.HARD_BOUNCE
