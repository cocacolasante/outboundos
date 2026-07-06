"""Phase 36 — /agent router (settings, notifications, actions, replies)."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import (
    AgentAction,
    AgentActionStatus,
    AgentActionType,
    CrmActivity,
    CrmActivityDirection,
    CrmActivityType,
    Lead,
    Notification,
    NotificationKind,
    Opportunity,
)

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------


async def test_get_settings_bootstraps_and_returns_defaults(client):
    resp = await client.get("/agent/settings")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["auto_log_replies"] is True
    assert body["auto_draft_replies"] is False
    assert body["min_confidence_to_act"] == 0.6
    assert body["agent_enabled"] is True
    assert body["owner_email_configured"] is False  # empty in tests


async def test_patch_settings_updates_toggles_and_threshold(client):
    resp = await client.patch("/agent/settings", json={
        "auto_draft_replies": True,
        "notify_on_any_reply": True,
        "min_confidence_to_act": 0.75,
        "quiet_hours_start_utc": 22,
        "quiet_hours_end_utc": 6,
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["auto_draft_replies"] is True
    assert body["notify_on_any_reply"] is True
    assert body["min_confidence_to_act"] == 0.75
    assert body["quiet_hours_start_utc"] == 22
    assert body["quiet_hours_end_utc"] == 6

    # clear_quiet_hours resets both.
    resp = await client.patch("/agent/settings", json={"clear_quiet_hours": True})
    assert resp.json()["quiet_hours_start_utc"] is None


async def test_patch_settings_rejects_half_quiet_window(client):
    resp = await client.patch("/agent/settings", json={"quiet_hours_start_utc": 22})
    assert resp.status_code == 422
    assert "together" in resp.json()["detail"]


# --------------------------------------------------------------------------
# notifications
# --------------------------------------------------------------------------


async def _seed_notifications(db_session, n=3) -> list[Notification]:
    lead = Lead(campaign_id=None, email="n@x.com", first_name="Jane", last_name="Doe")
    db_session.add(lead)
    await db_session.flush()
    rows = []
    for i in range(n):
        row = Notification(
            kind=NotificationKind.POSITIVE_REPLY,
            title=f"Positive reply {i}",
            dedup_key=f"positive_reply:router-{i}",
            lead_id=lead.id,
        )
        db_session.add(row)
        rows.append(row)
    await db_session.commit()
    return rows


async def test_list_notifications_with_lead_summary_and_unread_count(client, db_session):
    await _seed_notifications(db_session, 3)
    resp = await client.get("/agent/notifications")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert body["unread"] == 3
    assert body["items"][0]["lead_email"] == "n@x.com"
    assert body["items"][0]["lead_name"] == "Jane Doe"


async def test_mark_read_and_read_all(client, db_session):
    rows = await _seed_notifications(db_session, 3)

    resp = await client.post(f"/agent/notifications/{rows[0].id}/read")
    assert resp.status_code == 200
    assert resp.json()["read_at"] is not None

    resp = await client.get("/agent/notifications?unread=true")
    assert resp.json()["total"] == 2

    resp = await client.post("/agent/notifications/read-all")
    assert resp.json()["updated"] == 2
    resp = await client.get("/agent/notifications?unread=true")
    assert resp.json()["total"] == 0


async def test_mark_read_404_on_unknown(client):
    resp = await client.post(f"/agent/notifications/{uuid.uuid4()}/read")
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# actions audit
# --------------------------------------------------------------------------


async def test_list_actions_paginates_desc(client, db_session):
    for i in range(3):
        db_session.add(AgentAction(
            action_type=AgentActionType.CLASSIFY_REPLY,
            status=AgentActionStatus.SUCCESS,
            summary=f"action {i}",
            created_at=datetime.now(timezone.utc) + timedelta(seconds=i),
        ))
    await db_session.commit()

    resp = await client.get("/agent/actions?page_size=2")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2
    assert body["items"][0]["summary"] == "action 2"  # newest first


# --------------------------------------------------------------------------
# replies triage feed
# --------------------------------------------------------------------------


async def _seed_reply(db_session, *, sentiment="positive", converted=False) -> tuple[Lead, CrmActivity]:
    opp_id = None
    if converted:
        opp = Opportunity(name="Deal", email="r@x.com")
        db_session.add(opp)
        await db_session.flush()
        opp_id = opp.id
    lead = Lead(
        campaign_id=None, email=f"r{uuid.uuid4().hex[:5]}@x.com",
        first_name="Reply", company="Acme",
        converted_opportunity_id=opp_id,
    )
    db_session.add(lead)
    await db_session.flush()
    act = CrmActivity(
        lead_id=lead.id,
        opportunity_id=opp_id,
        activity_type=CrmActivityType.EMAIL,
        direction=CrmActivityDirection.INBOUND,
        subject="Re: outreach",
        body="Very interested, send pricing please.",
        sentiment=sentiment,
        is_agent_generated=True,
    )
    db_session.add(act)
    await db_session.commit()
    return lead, act


async def test_replies_feed_returns_enriched_items(client, db_session):
    lead, act = await _seed_reply(db_session)
    resp = await client.get("/agent/replies")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["sentiment"] == "positive"
    assert item["lead_email"] == lead.email
    assert item["lead_company"] == "Acme"
    assert item["convert_eligible"] is True
    assert item["converted"] is False
    assert "interested" in item["body_preview"]


async def test_replies_feed_converted_lead_not_eligible(client, db_session):
    await _seed_reply(db_session, converted=True)
    resp = await client.get("/agent/replies")
    item = resp.json()["items"][0]
    assert item["converted"] is True
    assert item["convert_eligible"] is False


async def test_replies_feed_sentiment_filter(client, db_session):
    await _seed_reply(db_session, sentiment="positive")
    await _seed_reply(db_session, sentiment="negative")
    resp = await client.get("/agent/replies?sentiment=negative")
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["sentiment"] == "negative"


async def test_replies_feed_surfaces_draft_from_audit_row(client, db_session):
    lead, act = await _seed_reply(db_session)
    db_session.add(AgentAction(
        action_type=AgentActionType.DRAFT_REPLY,
        status=AgentActionStatus.SUCCESS,
        summary="drafted",
        lead_id=lead.id,
        activity_id=act.id,
        detail={"draft_body": "Hi — happy to share pricing. How's Tuesday?"},
    ))
    await db_session.commit()

    resp = await client.get("/agent/replies")
    item = resp.json()["items"][0]
    assert item["draft_body"].startswith("Hi — happy to share pricing")


async def test_replies_feed_excludes_manual_activities(client, db_session):
    """Manually-logged emails (is_agent_generated=False) stay out of
    the triage feed — it's an inbox of what the AGENT processed."""
    lead = Lead(campaign_id=None, email="manual@x.com")
    db_session.add(lead)
    await db_session.flush()
    db_session.add(CrmActivity(
        lead_id=lead.id,
        activity_type=CrmActivityType.EMAIL,
        direction=CrmActivityDirection.INBOUND,
        subject="manually logged",
        is_agent_generated=False,
    ))
    await db_session.commit()

    resp = await client.get("/agent/replies")
    assert resp.json()["total"] == 0
