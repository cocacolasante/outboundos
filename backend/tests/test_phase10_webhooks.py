"""Phase 10: Brevo webhook + one-click unsubscribe."""
import uuid
from datetime import time

from sqlalchemy import select

from app.models import (
    Campaign,
    EmailEvent,
    EmailEventType,
    Lead,
    SendStatus,
    Suppression,
    SuppressionReason,
)


async def _make_campaign_and_lead(db_session, brevo_id: str = "msg-xyz") -> tuple[Campaign, Lead]:
    c = Campaign(
        name="P10",
        goal="g", tone="t",
        sender_name="s", sender_email="s@x.com",
        sample_count=1,
        schedule_days=[0, 1, 2, 3, 4],
        schedule_time_start=time(9, 0),
        schedule_time_end=time(17, 0),
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    l = Lead(
        campaign_id=c.id, email="lead@x.com",
        brevo_message_id=brevo_id,
    )
    db_session.add(l)
    await db_session.commit()
    await db_session.refresh(l)
    return c, l


# ---------- Brevo event processing (driven by the poller, not a webhook) ----------
#
# The inbound webhook was removed in favour of brevo_events_poller pulling
# events from GET /v3/smtp/statistics/events.  These tests exercise the
# shared `process_event` handler directly so the suppression / event-row
# semantics are guaranteed regardless of whether events arrived via webhook
# (historical) or poll (current).


from app.services.brevo_events import process_event


async def test_processor_records_delivered_event(db_session):
    _, lead = await _make_campaign_and_lead(db_session)
    recorded = await process_event(db_session, {
        "event": "delivered",
        "messageId": lead.brevo_message_id,
        "email": "lead@x.com",
    })
    await db_session.commit()
    assert recorded is True
    events = (await db_session.execute(select(EmailEvent))).scalars().all()
    assert len(events) == 1
    assert events[0].event_type == EmailEventType.DELIVERED
    assert events[0].lead_id == lead.id


async def test_processor_records_opened_and_clicked(db_session):
    _, lead = await _make_campaign_and_lead(db_session)
    await process_event(db_session, {"event": "opened", "messageId": lead.brevo_message_id})
    await process_event(db_session, {"event": "click",  "messageId": lead.brevo_message_id})
    await db_session.commit()
    types = [e.event_type for e in (await db_session.execute(select(EmailEvent))).scalars().all()]
    assert EmailEventType.OPENED in types
    assert EmailEventType.CLICKED in types


async def test_hard_bounce_adds_suppression(db_session):
    _, lead = await _make_campaign_and_lead(db_session)
    await process_event(db_session, {
        "event": "hard_bounce", "messageId": lead.brevo_message_id,
    })
    await db_session.commit()
    sup = await db_session.scalar(select(Suppression).where(Suppression.email == "lead@x.com"))
    assert sup is not None
    assert sup.reason == SuppressionReason.HARD_BOUNCE


async def test_spam_complaint_adds_suppression(db_session):
    _, lead = await _make_campaign_and_lead(db_session)
    await process_event(db_session, {
        "event": "spam", "messageId": lead.brevo_message_id,
    })
    await db_session.commit()
    sup = await db_session.scalar(select(Suppression).where(Suppression.email == "lead@x.com"))
    assert sup is not None
    assert sup.reason == SuppressionReason.SPAM


async def test_unsubscribe_event_adds_suppression(db_session):
    _, lead = await _make_campaign_and_lead(db_session)
    await process_event(db_session, {
        "event": "unsubscribed", "messageId": lead.brevo_message_id,
    })
    await db_session.commit()
    sup = await db_session.scalar(select(Suppression).where(Suppression.email == "lead@x.com"))
    assert sup is not None
    assert sup.reason == SuppressionReason.UNSUBSCRIBED


async def test_soft_bounce_suppresses_at_default_threshold(db_session, monkeypatch):
    """Soft bounces now suppress at SOFT_BOUNCE_SUPPRESS_THRESHOLD (default 1)
    to protect sender reputation — and still record the event row."""
    monkeypatch.setattr("app.services.brevo_events.settings.SOFT_BOUNCE_SUPPRESS_THRESHOLD", 1)
    _, lead = await _make_campaign_and_lead(db_session)
    await process_event(db_session, {
        "event": "softBounce", "messageId": lead.brevo_message_id,
    })
    await db_session.commit()
    sup = await db_session.scalar(select(Suppression).where(Suppression.email == "lead@x.com"))
    assert sup is not None and sup.reason is SuppressionReason.SOFT_BOUNCE
    events = (await db_session.execute(select(EmailEvent))).scalars().all()
    assert any(e.event_type == EmailEventType.SOFT_BOUNCE for e in events)


async def test_processor_ignores_unknown_event_type(db_session):
    _, lead = await _make_campaign_and_lead(db_session)
    recorded = await process_event(db_session, {
        "event": "something_new", "messageId": lead.brevo_message_id,
    })
    await db_session.commit()
    assert recorded is False
    events = (await db_session.execute(select(EmailEvent))).scalars().all()
    assert events == []


async def test_processor_ignores_unknown_message_id(db_session):
    await _make_campaign_and_lead(db_session)
    recorded = await process_event(db_session, {
        "event": "opened", "messageId": "not-in-db",
    })
    await db_session.commit()
    assert recorded is False
    events = (await db_session.execute(select(EmailEvent))).scalars().all()
    assert events == []


async def test_processor_strips_angle_brackets_from_message_id(db_session):
    _, lead = await _make_campaign_and_lead(db_session, brevo_id="abc-123")
    await process_event(db_session, {
        "event": "opened", "messageId": "<abc-123>",
    })
    await db_session.commit()
    events = (await db_session.execute(select(EmailEvent))).scalars().all()
    assert len(events) == 1


async def test_processor_accepts_camelcase_and_dashed_message_id(db_session):
    """Brevo's API uses ``messageId`` while the older webhook payload used
    ``message-id``.  process_event must accept either."""
    _, lead = await _make_campaign_and_lead(db_session, brevo_id="abc-456")
    await process_event(db_session, {"event": "delivered", "message-id": "abc-456"})
    await db_session.commit()
    events = (await db_session.execute(select(EmailEvent))).scalars().all()
    assert len(events) == 1


async def test_duplicate_hard_bounce_only_adds_one_suppression(db_session):
    """Polling re-queries the same day window every tick, so process_event
    must be idempotent for terminal events."""
    _, lead = await _make_campaign_and_lead(db_session)
    await process_event(db_session, {"event": "hard_bounce", "messageId": lead.brevo_message_id})
    await process_event(db_session, {"event": "hard_bounce", "messageId": lead.brevo_message_id})
    await db_session.commit()
    sups = (await db_session.execute(select(Suppression).where(Suppression.email == "lead@x.com"))).scalars().all()
    assert len(sups) == 1
    bounces = [e for e in (await db_session.execute(select(EmailEvent))).scalars().all()
               if e.event_type == EmailEventType.HARD_BOUNCE]
    assert len(bounces) == 1


async def test_brevo_webhook_route_requires_auth(client):
    """The inbound Brevo webhook was re-added for real-time events (the poller
    stays on as the backstop).  Without the shared-secret header it rejects
    with 401 — never 404, never processed on a forged payload."""
    resp = await client.post("/webhooks/brevo", json=[])
    assert resp.status_code == 401


async def test_brevo_webhook_rejects_wrong_and_missing_secret(client, monkeypatch):
    monkeypatch.setattr("app.routers.webhooks.settings.BREVO_WEBHOOK_SECRET", "shh")
    bad = await client.post(
        "/webhooks/brevo", json={"event": "delivered"}, headers={"X-Brevo-Auth": "nope"},
    )
    assert bad.status_code == 401
    none = await client.post("/webhooks/brevo", json={"event": "delivered"})
    assert none.status_code == 401


async def test_brevo_webhook_suppresses_on_bounce(client, db_session, monkeypatch):
    """A real-time hard-bounce event funnels through process_event → suppression
    (added to the ignore list + the lead pulled from the send queue)."""
    monkeypatch.setattr("app.routers.webhooks.settings.BREVO_WEBHOOK_SECRET", "shh")
    _, lead = await _make_campaign_and_lead(db_session, brevo_id="wh-msg-1")

    resp = await client.post(
        "/webhooks/brevo",
        json={"event": "hard_bounce", "email": lead.email, "message-id": "wh-msg-1", "id": 999},
        headers={"X-Brevo-Auth": "shh"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["recorded"] == 1

    sup = await db_session.scalar(select(Suppression).where(Suppression.email == lead.email))
    assert sup is not None and sup.reason is SuppressionReason.HARD_BOUNCE
    await db_session.refresh(lead)
    assert lead.send_status is SendStatus.SUPPRESSED


async def test_brevo_webhook_dedups_retries(client, db_session, monkeypatch):
    """Brevo retries at-least-once; the second identical delivery short-circuits
    via the webhook_events event-id guard."""
    monkeypatch.setattr("app.routers.webhooks.settings.BREVO_WEBHOOK_SECRET", "shh")
    _, lead = await _make_campaign_and_lead(db_session, brevo_id="dd-1")

    ev = {"event": "unsubscribed", "email": lead.email, "message-id": "dd-1", "id": 555}
    r1 = await client.post("/webhooks/brevo", json=ev, headers={"X-Brevo-Auth": "shh"})
    r2 = await client.post("/webhooks/brevo", json=ev, headers={"X-Brevo-Auth": "shh"})

    assert r1.json()["recorded"] == 1
    assert r2.json().get("duplicate") is True


# ---------- Unsubscribe link (HMAC tokenised, POST-confirm) ----------


def _unsub_token(lead_id) -> str:
    from app.routers.webhooks import _unsubscribe_token
    return _unsubscribe_token(lead_id)


async def test_unsubscribe_get_renders_confirm_page_no_side_effect(client, db_session):
    """A GET with a valid token shows the confirm page but does NOT
    suppress.  This is the behaviour that defends against email-scanner
    link-prefetch — a scanner that GETs every link won't unsubscribe
    anyone."""
    _, lead = await _make_campaign_and_lead(db_session)
    tok = _unsub_token(lead.id)
    resp = await client.get(f"/unsubscribe/{lead.id}?t={tok}")
    assert resp.status_code == 200
    assert "Unsubscribe from these emails?" in resp.text
    assert "lead@x.com" in resp.text
    assert "Confirm unsubscribe" in resp.text

    sup = await db_session.scalar(select(Suppression).where(Suppression.email == "lead@x.com"))
    assert sup is None
    events = (await db_session.execute(select(EmailEvent).where(EmailEvent.lead_id == lead.id))).scalars().all()
    assert events == []


async def test_unsubscribe_post_applies_suppression(client, db_session):
    _, lead = await _make_campaign_and_lead(db_session)
    tok = _unsub_token(lead.id)
    resp = await client.post(f"/unsubscribe/{lead.id}?t={tok}")
    assert resp.status_code == 200
    assert "unsubscribed" in resp.text.lower()

    sup = await db_session.scalar(select(Suppression).where(Suppression.email == "lead@x.com"))
    assert sup is not None
    assert sup.reason == SuppressionReason.UNSUBSCRIBED
    events = (await db_session.execute(select(EmailEvent).where(EmailEvent.lead_id == lead.id))).scalars().all()
    assert any(e.event_type == EmailEventType.UNSUBSCRIBED for e in events)


async def test_unsubscribe_rejects_missing_token(client, db_session):
    _, lead = await _make_campaign_and_lead(db_session)
    resp = await client.get(f"/unsubscribe/{lead.id}")
    assert resp.status_code == 404
    resp2 = await client.post(f"/unsubscribe/{lead.id}")
    assert resp2.status_code == 404


async def test_unsubscribe_rejects_wrong_token(client, db_session):
    _, lead = await _make_campaign_and_lead(db_session)
    resp = await client.get(f"/unsubscribe/{lead.id}?t=deadbeef")
    assert resp.status_code == 404
    resp2 = await client.post(f"/unsubscribe/{lead.id}?t=deadbeef")
    assert resp2.status_code == 404


async def test_unsubscribe_token_for_one_lead_does_not_work_for_another(client, db_session):
    """Defence-in-depth: knowing one lead's unsubscribe URL doesn't let
    you forge another's.  This is the key reason for HMAC over plain UUID."""
    _, lead_a = await _make_campaign_and_lead(db_session)
    lead_b = Lead(campaign_id=lead_a.campaign_id, email="other@x.com")
    db_session.add(lead_b)
    await db_session.commit()
    await db_session.refresh(lead_b)
    a_token = _unsub_token(lead_a.id)
    resp = await client.post(f"/unsubscribe/{lead_b.id}?t={a_token}")
    assert resp.status_code == 404


async def test_unsubscribe_unknown_lead_returns_404(client):
    fake = uuid.uuid4()
    tok = _unsub_token(fake)  # signature is valid but lead doesn't exist
    resp = await client.get(f"/unsubscribe/{fake}?t={tok}")
    assert resp.status_code == 404


async def test_unsubscribe_post_idempotent_when_already_suppressed(client, db_session):
    _, lead = await _make_campaign_and_lead(db_session)
    db_session.add(Suppression(email="lead@x.com", reason=SuppressionReason.MANUAL))
    await db_session.commit()
    tok = _unsub_token(lead.id)
    resp = await client.post(f"/unsubscribe/{lead.id}?t={tok}")
    assert resp.status_code == 200
    sups = (await db_session.execute(select(Suppression).where(Suppression.email == "lead@x.com"))).scalars().all()
    assert len(sups) == 1
    assert sups[0].reason == SuppressionReason.MANUAL  # first reason preserved


async def test_make_unsubscribe_url_helper(db_session):
    from app.routers.webhooks import make_unsubscribe_url
    _, lead = await _make_campaign_and_lead(db_session)
    url = make_unsubscribe_url(lead.id)
    assert f"/unsubscribe/{lead.id}" in url
    assert "?t=" in url
    tok = url.split("?t=")[1]
    assert tok == _unsub_token(lead.id)
