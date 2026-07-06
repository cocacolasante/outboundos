"""Phase 44: the email_reply (reply-in-thread) sequence node — publish
validation.  Worker behaviour is covered in test_phase16_sequencer.py and
the threading headers in test_phase9_brevo.py."""
from __future__ import annotations


async def _make_campaign(client) -> str:
    resp = await client.post("/campaigns/", json={
        "name": "x", "goal": "g", "tone": "Direct",
        "sender_name": "A", "sender_email": "a@example.com",
        "research_mode": "fast",
        "schedule_days": [0, 1, 2, 3, 4],
        "schedule_time_start": "09:00:00",
        "schedule_time_end": "17:00:00",
        "schedule_timezone": "UTC",
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _publish(client, cid, reply_node):
    payload = {
        "nodes": [
            {"client_id": "e", "kind": "email", "is_entry": True, "config": {}},
            {"client_id": "w", "kind": "wait", "is_entry": False,
             "config": {"duration_minutes": 2880}},
            reply_node,
        ],
        "edges": [
            {"from_client_id": "e", "to_client_id": "w", "condition": {"op": "always"}},
            {"from_client_id": "w", "to_client_id": "r",
             "condition": {"op": "not", "child": {"op": "replied"}}},
        ],
    }
    await client.put(f"/campaigns/{cid}/sequence", json=payload)
    return await client.post(f"/campaigns/{cid}/sequence/publish")


async def test_publish_accepts_ai_reply(client):
    cid = await _make_campaign(client)
    pub = await _publish(client, cid, {
        "client_id": "r", "kind": "email_reply", "is_entry": False,
        "config": {"ai_compose": True, "ai_prompt": "nudge about the trial"},
    })
    body = pub.json()
    assert body["ok"] is True, body


async def test_publish_accepts_manual_reply(client):
    cid = await _make_campaign(client)
    pub = await _publish(client, cid, {
        "client_id": "r", "kind": "email_reply", "is_entry": False,
        "config": {"body_template": "Just bumping this, {{first_name}}."},
    })
    assert pub.json()["ok"] is True


async def test_publish_rejects_reply_without_body_or_ai(client):
    cid = await _make_campaign(client)
    pub = await _publish(client, cid, {
        "client_id": "r", "kind": "email_reply", "is_entry": False, "config": {},
    })
    body = pub.json()
    assert body["ok"] is False
    assert any("body_template" in e or "ai_compose" in e for e in body["errors"])


async def test_publish_rejects_reply_as_entry(client):
    cid = await _make_campaign(client)
    payload = {
        "nodes": [
            {"client_id": "r", "kind": "email_reply", "is_entry": True,
             "config": {"ai_compose": True}},
        ],
        "edges": [],
    }
    await client.put(f"/campaigns/{cid}/sequence", json=payload)
    pub = await client.post(f"/campaigns/{cid}/sequence/publish")
    body = pub.json()
    assert body["ok"] is False
    assert any("entry node" in e for e in body["errors"])


# --------------------------------------------------------------------------
# Reply preview endpoint
# --------------------------------------------------------------------------

import uuid  # noqa: E402

from app.models import Lead  # noqa: E402


async def _make_lead(db_session, cid, **kw):
    lead = Lead(
        campaign_id=uuid.UUID(cid),
        email=kw.pop("email", "alice@acme.com"),
        first_name=kw.pop("first_name", "Alice"),
        composed_subject=kw.pop("composed_subject", "Quick idea for Acme"),
        composed_body=kw.pop("composed_body", "Hi Alice, here's my pitch."),
        **kw,
    )
    db_session.add(lead)
    await db_session.commit()
    await db_session.refresh(lead)
    return lead


async def test_reply_preview_manual_template_is_exact(client, db_session):
    cid = await _make_campaign(client)
    await _publish(client, cid, {
        "client_id": "r", "kind": "email_reply", "is_entry": False,
        "config": {"body_template": "Just bumping this, {{first_name}}."},
    })
    lead = await _make_lead(db_session, cid)

    resp = await client.post(f"/campaigns/{cid}/leads/{lead.id}/reply-preview")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["subject"] == "Re: Quick idea for Acme"
    assert body["body"] == "Just bumping this, Alice."
    assert body["ai_compose"] is False
    assert body["regenerated_at_send"] is False
    assert body["has_original_email"] is True
    assert len(body["available_nodes"]) == 1


async def test_reply_preview_ai_uses_generate_followup(client, db_session, monkeypatch):
    async def fake_reply(**kw):
        # Should receive the lead's existing research/identity, NOT trigger new research.
        assert kw["idea"] == "nudge about the trial"
        assert kw["first_name"] == "Alice"
        return "Hi Alice — just circling back on the trial."

    monkeypatch.setattr("app.workers.compose.generate_followup_reply", fake_reply)

    cid = await _make_campaign(client)
    await _publish(client, cid, {
        "client_id": "r", "kind": "email_reply", "is_entry": False,
        "config": {"ai_compose": True, "ai_prompt": "nudge about the trial"},
    })
    lead = await _make_lead(db_session, cid)

    resp = await client.post(f"/campaigns/{cid}/leads/{lead.id}/reply-preview")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "circling back on the trial" in body["body"]
    assert body["ai_compose"] is True
    assert body["regenerated_at_send"] is True
    assert body["ai_prompt"] == "nudge about the trial"
    assert body["subject"] == "Re: Quick idea for Acme"


async def test_reply_preview_flags_missing_original(client, db_session):
    cid = await _make_campaign(client)
    await _publish(client, cid, {
        "client_id": "r", "kind": "email_reply", "is_entry": False,
        "config": {"body_template": "Bump, {{first_name}}."},
    })
    # Lead with no composed email yet (first email not sent).
    lead = await _make_lead(db_session, cid, composed_subject=None, composed_body=None)

    resp = await client.post(f"/campaigns/{cid}/leads/{lead.id}/reply-preview")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["has_original_email"] is False
    assert body["subject"] == "Re:"


async def test_reply_preview_404_when_no_reply_node(client, db_session):
    cid = await _make_campaign(client)
    # Default sequence is a single email node — no reply step.
    lead = await _make_lead(db_session, cid)
    resp = await client.post(f"/campaigns/{cid}/leads/{lead.id}/reply-preview")
    assert resp.status_code == 404
    assert "reply step" in resp.json()["detail"]


async def test_reply_preview_404_for_wrong_lead(client, db_session):
    cid = await _make_campaign(client)
    await _publish(client, cid, {
        "client_id": "r", "kind": "email_reply", "is_entry": False,
        "config": {"body_template": "Bump."},
    })
    resp = await client.post(f"/campaigns/{cid}/leads/{uuid.uuid4()}/reply-preview")
    assert resp.status_code == 404
