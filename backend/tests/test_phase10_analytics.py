"""Phase 10: analytics endpoint."""
import uuid
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import select

from app.models import (
    Campaign,
    ConnectedAccount,
    EmailEvent,
    EmailEventType,
    Lead,
    SendStatus,
)
from app.services import encryption


async def _make_campaign(db_session, *, connected_account_id=None) -> Campaign:
    c = Campaign(
        name="P10",
        goal="g", tone="t",
        sender_name="s", sender_email="s@x.com",
        sample_count=1,
        connected_account_id=connected_account_id,
        schedule_days=[0, 1, 2, 3, 4],
        schedule_time_start=time(9, 0),
        schedule_time_end=time(17, 0),
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    return c


async def _make_lead(
    db_session, campaign, *, email,
    send_status=SendStatus.SENT,
    composed_subject="S",
    research_data=None,
) -> Lead:
    l = Lead(
        campaign_id=campaign.id, email=email,
        send_status=send_status,
        composed_subject=composed_subject,
        research_data=research_data,
    )
    db_session.add(l)
    await db_session.commit()
    await db_session.refresh(l)
    return l


def _ev(lead, campaign, event_type, occurred_at=None) -> EmailEvent:
    e = EmailEvent(
        lead_id=lead.id, campaign_id=campaign.id, event_type=event_type,
    )
    if occurred_at is not None:
        e.occurred_at = occurred_at
    return e


# --------------------------------------------------------------------------
# Empty campaign
# --------------------------------------------------------------------------


async def test_analytics_empty_campaign(client, db_session):
    campaign = await _make_campaign(db_session)
    resp = await client.get(f"/campaigns/{campaign.id}/analytics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["overview"]["total_leads"] == 0
    assert body["overview"]["sent"] == 0
    assert body["rates"]["open_rate"] is None
    assert body["sender_reputation_score"] is None
    assert body["timeline"] == []
    assert body["research_quality_breakdown"] == []
    assert body["best_subject_lines"] == []
    assert body["reply_tracking_enabled"] is False


async def test_analytics_404_for_unknown_campaign(client):
    resp = await client.get(f"/campaigns/{uuid.uuid4()}/analytics")
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# Overview + rates
# --------------------------------------------------------------------------


async def test_overview_and_rates(client, db_session):
    campaign = await _make_campaign(db_session)
    # 10 sent leads
    leads = []
    for i in range(10):
        leads.append(await _make_lead(
            db_session, campaign, email=f"l{i}@x.com",
            research_data={"quality": "rich"} if i < 4 else {"quality": "low"},
        ))
    # Events: 8 delivered, 5 opened (distinct), 2 clicked, 1 hard_bounce, 1 spam, 1 unsubscribed
    events: list[EmailEvent] = []
    for lead in leads[:8]:
        events.append(_ev(lead, campaign, EmailEventType.DELIVERED))
    for lead in leads[:5]:
        events.append(_ev(lead, campaign, EmailEventType.OPENED))
        # duplicate open events to ensure DISTINCT counting
        events.append(_ev(lead, campaign, EmailEventType.OPENED))
    for lead in leads[:2]:
        events.append(_ev(lead, campaign, EmailEventType.CLICKED))
    events.append(_ev(leads[8], campaign, EmailEventType.HARD_BOUNCE))
    events.append(_ev(leads[9], campaign, EmailEventType.SPAM))
    events.append(_ev(leads[0], campaign, EmailEventType.UNSUBSCRIBED))
    db_session.add_all(events)
    await db_session.commit()

    body = (await client.get(f"/campaigns/{campaign.id}/analytics")).json()

    ov = body["overview"]
    assert ov["total_leads"] == 10
    assert ov["sent"] == 10
    assert ov["delivered"] == 8
    assert ov["opened"] == 5
    assert ov["clicked"] == 2
    assert ov["bounced"] == 1  # hard_bounce; no soft
    assert ov["spam_complaints"] == 1
    assert ov["unsubscribed"] == 1

    rates = body["rates"]
    assert rates["delivery_rate"] == 0.8
    assert rates["open_rate"] == 0.5
    assert rates["click_rate"] == 0.2
    assert rates["bounce_rate"] == 0.1
    assert rates["spam_rate"] == 0.1


# --------------------------------------------------------------------------
# Reply tracking gating
# --------------------------------------------------------------------------


async def test_reply_tracking_disabled_when_no_account(client, db_session):
    campaign = await _make_campaign(db_session)
    body = (await client.get(f"/campaigns/{campaign.id}/analytics")).json()
    assert body["reply_tracking_enabled"] is False
    assert body["rates"]["reply_rate"] is None


async def test_reply_tracking_enabled_with_account(client, db_session):
    acc = ConnectedAccount(
        label="X", email_address="x@y.com",
        imap_host="imap.y.com", username="x@y.com",
        password_encrypted=encryption.encrypt("p"),
    )
    db_session.add(acc)
    await db_session.commit()
    await db_session.refresh(acc)

    campaign = await _make_campaign(db_session, connected_account_id=acc.id)
    lead = await _make_lead(db_session, campaign, email="l@x.com")
    db_session.add(_ev(lead, campaign, EmailEventType.REPLIED))
    await db_session.commit()

    body = (await client.get(f"/campaigns/{campaign.id}/analytics")).json()
    assert body["reply_tracking_enabled"] is True
    assert body["overview"]["replied"] == 1
    assert body["rates"]["reply_rate"] == 1.0


# --------------------------------------------------------------------------
# Timeline
# --------------------------------------------------------------------------


async def test_timeline_groups_events_by_day(client, db_session):
    campaign = await _make_campaign(db_session)
    # Backdate the campaign so historical events fall within its window.
    campaign.created_at = datetime.now(timezone.utc) - timedelta(days=10)
    await db_session.commit()

    lead = await _make_lead(db_session, campaign, email="l@x.com")
    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)

    e1 = _ev(lead, campaign, EmailEventType.OPENED, occurred_at=now)
    e2 = _ev(lead, campaign, EmailEventType.OPENED, occurred_at=now)
    e3 = _ev(lead, campaign, EmailEventType.CLICKED, occurred_at=yesterday)
    db_session.add_all([e1, e2, e3])
    await db_session.commit()

    body = (await client.get(f"/campaigns/{campaign.id}/analytics")).json()
    tl = body["timeline"]
    assert len(tl) >= 2
    by_date = {pt["date"]: pt for pt in tl}
    assert by_date[now.date().isoformat()]["opens"] == 2
    assert by_date[yesterday.date().isoformat()]["clicks"] == 1


# --------------------------------------------------------------------------
# Quality breakdown
# --------------------------------------------------------------------------


async def test_quality_breakdown_per_tier_open_rate(client, db_session):
    campaign = await _make_campaign(db_session)
    rich_leads = [await _make_lead(
        db_session, campaign, email=f"r{i}@x.com",
        research_data={"quality": "rich"},
    ) for i in range(4)]
    low_leads = [await _make_lead(
        db_session, campaign, email=f"l{i}@x.com",
        research_data={"quality": "low"},
    ) for i in range(2)]

    # 3 of 4 rich leads opened; 0 of 2 low leads opened
    for lead in rich_leads[:3]:
        db_session.add(_ev(lead, campaign, EmailEventType.OPENED))
    await db_session.commit()

    body = (await client.get(f"/campaigns/{campaign.id}/analytics")).json()
    breakdown = {item["quality"]: item for item in body["research_quality_breakdown"]}
    assert breakdown["rich"]["count"] == 4
    assert breakdown["rich"]["open_rate"] == 0.75
    assert breakdown["low"]["count"] == 2
    assert breakdown["low"]["open_rate"] == 0.0


# --------------------------------------------------------------------------
# Best subjects
# --------------------------------------------------------------------------


async def test_best_subjects_requires_minimum_sends(client, db_session):
    campaign = await _make_campaign(db_session)
    # Subject "Popular" — 6 sends, 4 opens
    popular_leads = [await _make_lead(
        db_session, campaign, email=f"p{i}@x.com",
        composed_subject="Popular",
    ) for i in range(6)]
    for lead in popular_leads[:4]:
        db_session.add(_ev(lead, campaign, EmailEventType.OPENED))

    # Subject "Untested" — 3 sends, 3 opens (100% but below threshold)
    untested_leads = [await _make_lead(
        db_session, campaign, email=f"u{i}@x.com",
        composed_subject="Untested",
    ) for i in range(3)]
    for lead in untested_leads:
        db_session.add(_ev(lead, campaign, EmailEventType.OPENED))
    await db_session.commit()

    body = (await client.get(f"/campaigns/{campaign.id}/analytics")).json()
    subjects = {s["subject"]: s for s in body["best_subject_lines"]}
    assert "Popular" in subjects
    assert subjects["Popular"]["sent"] == 6
    assert subjects["Popular"]["open_rate"] == round(4 / 6, 4)
    # "Untested" has only 3 sends — filtered out
    assert "Untested" not in subjects


# --------------------------------------------------------------------------
# Sender reputation
# --------------------------------------------------------------------------


async def test_reputation_score_perfect_when_all_delivered(client, db_session):
    campaign = await _make_campaign(db_session)
    leads = [await _make_lead(db_session, campaign, email=f"l{i}@x.com") for i in range(10)]
    for lead in leads:
        db_session.add(_ev(lead, campaign, EmailEventType.DELIVERED))
    await db_session.commit()

    body = (await client.get(f"/campaigns/{campaign.id}/analytics")).json()
    # delivery_rate=1, spam_rate=0, bounce_rate=0 → 1*50 + 1*30 + 1*20 = 100
    assert body["sender_reputation_score"] == 100


async def test_reputation_score_drops_with_bounces_and_spam(client, db_session):
    campaign = await _make_campaign(db_session)
    leads = [await _make_lead(db_session, campaign, email=f"l{i}@x.com") for i in range(10)]
    # 7 delivered, 2 hard_bounce, 1 spam
    for lead in leads[:7]:
        db_session.add(_ev(lead, campaign, EmailEventType.DELIVERED))
    for lead in leads[7:9]:
        db_session.add(_ev(lead, campaign, EmailEventType.HARD_BOUNCE))
    db_session.add(_ev(leads[9], campaign, EmailEventType.SPAM))
    await db_session.commit()

    body = (await client.get(f"/campaigns/{campaign.id}/analytics")).json()
    # delivery=0.7, spam=0.1, bounce=0.2
    # score = 0.7*50 + 0.9*30 + 0.8*20 = 35 + 27 + 16 = 78
    assert body["sender_reputation_score"] == 78
