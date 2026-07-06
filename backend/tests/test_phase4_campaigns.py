"""Phase 4: Campaign CRUD, status transitions, stats, paginated leads."""
import uuid
from datetime import time
from unittest.mock import patch

import pytest

from app.models import (
    Campaign,
    CampaignStatus,
    ComposeStatus,
    ConnectedAccount,
    EmailEvent,
    EmailEventType,
    Lead,
    SendStatus,
)


def _campaign_payload(**overrides) -> dict:
    base = {
        "name": "Q2 outreach",
        "goal": "Book a 30-minute discovery call",
        "tone": "Professional",
        "sender_name": "Anthony",
        "sender_email": "anthony@example.com",
        "research_mode": "fast",
        "sample_count": 5,
        "schedule_days": [0, 1, 2, 3, 4],
        "schedule_time_start": "09:00:00",
        "schedule_time_end": "17:00:00",
        "schedule_timezone": "America/New_York",
        "max_per_hour": 50,
        "max_per_day": 400,
        "min_delay_seconds": 60,
    }
    base.update(overrides)
    return base


async def _make_account(db_session, email="me@gmail.com") -> ConnectedAccount:
    acc = ConnectedAccount(
        label="Work Gmail",
        email_address=email,
        imap_host="imap.gmail.com",
        username=email,
        password_encrypted="ciphertext",
    )
    db_session.add(acc)
    await db_session.commit()
    await db_session.refresh(acc)
    return acc


# ---------- Create + validation ----------


async def test_create_campaign_minimum_fields(client):
    resp = await client.post("/campaigns/", json=_campaign_payload())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "draft"
    assert body["connected_account_configured"] is False
    assert body["connected_account"] is None
    assert body["lead_counts"]["total"] == 0
    assert body["stats"]["sent_count"] == 0
    assert body["stats"]["reply_rate"] is None
    assert body["stats"]["reply_tracking_note"] == "reply tracking not configured"


async def test_create_with_connected_account_links_and_enables_reply_tracking(client, db_session):
    acc = await _make_account(db_session)
    resp = await client.post(
        "/campaigns/", json=_campaign_payload(connected_account_id=str(acc.id))
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["connected_account_configured"] is True
    assert body["connected_account"]["label"] == "Work Gmail"
    assert body["connected_account"]["email_address"] == "me@gmail.com"
    assert body["stats"]["reply_tracking_note"] is None
    assert body["stats"]["reply_rate"] is None  # no sends yet, so still None


async def test_create_rejects_bad_day_values(client):
    resp = await client.post("/campaigns/", json=_campaign_payload(schedule_days=[0, 7]))
    assert resp.status_code == 422
    assert "0-6" in resp.text


async def test_create_rejects_inverted_time_window(client):
    resp = await client.post(
        "/campaigns/",
        json=_campaign_payload(schedule_time_start="17:00:00", schedule_time_end="09:00:00"),
    )
    assert resp.status_code == 422


async def test_create_rejects_sample_count_zero(client):
    resp = await client.post("/campaigns/", json=_campaign_payload(sample_count=0))
    assert resp.status_code == 422


async def test_create_rejects_unknown_connected_account(client):
    resp = await client.post(
        "/campaigns/",
        json=_campaign_payload(connected_account_id=str(uuid.uuid4())),
    )
    assert resp.status_code == 422


# ---------- Read ----------


async def test_list_returns_campaigns_with_summary(client):
    await client.post("/campaigns/", json=_campaign_payload(name="A"))
    await client.post("/campaigns/", json=_campaign_payload(name="B"))

    resp = await client.get("/campaigns/")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    for c in body:
        assert "lead_counts" in c
        assert "stats" in c
        assert "status" in c


async def test_get_single_includes_stats(client):
    created = (await client.post("/campaigns/", json=_campaign_payload())).json()
    resp = await client.get(f"/campaigns/{created['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == created["id"]
    assert body["lead_counts"]["total"] == 0


async def test_get_not_found(client):
    resp = await client.get(f"/campaigns/{uuid.uuid4()}")
    assert resp.status_code == 404


# ---------- Update gating ----------


async def test_patch_updates_fields_in_draft(client):
    created = (await client.post("/campaigns/", json=_campaign_payload())).json()
    resp = await client.patch(
        f"/campaigns/{created['id']}", json={"name": "Renamed", "tone": "Friendly"}
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed"
    assert resp.json()["tone"] == "Friendly"


@pytest.mark.parametrize("blocked_status", ["running", "paused", "complete", "approved"])
async def test_patch_rejected_outside_draft_or_previewing(
    client, db_session, blocked_status
):
    created = (await client.post("/campaigns/", json=_campaign_payload())).json()
    # Bump the status via direct DB write (no API for that yet).
    c = await db_session.get(Campaign, uuid.UUID(created["id"]))
    c.status = CampaignStatus(blocked_status)
    await db_session.commit()

    resp = await client.patch(f"/campaigns/{created['id']}", json={"name": "X"})
    assert resp.status_code == 409


@pytest.mark.parametrize("status_val", ["running", "paused", "complete"])
async def test_patch_signature_allowed_in_any_status(client, db_session, status_val):
    """The signature bypasses the draft/previewing content guard — it's
    editable even on a paused/running campaign (only affects emails on Apply)."""
    created = (await client.post("/campaigns/", json=_campaign_payload())).json()
    c = await db_session.get(Campaign, uuid.UUID(created["id"]))
    c.status = CampaignStatus(status_val)
    await db_session.commit()

    resp = await client.patch(
        f"/campaigns/{created['id']}", json={"signature": "Anthony\n555-1234"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["signature"] == "Anthony\n555-1234"

    # Other content fields (tone) are still gated to draft/previewing.
    blocked = await client.patch(f"/campaigns/{created['id']}", json={"tone": "new"})
    assert blocked.status_code == 409


@pytest.mark.parametrize("status_val", ["running", "paused"])
async def test_patch_schedule_window_allowed_on_live_campaigns(client, db_session, status_val):
    """The 4 schedule fields and the 3 throughput fields are editable on a
    running/paused campaign — they take effect on the next send_lead gate
    check.  Content fields (goal/tone/sender_*) are still gated."""
    created = (await client.post("/campaigns/", json=_campaign_payload())).json()
    c = await db_session.get(Campaign, uuid.UUID(created["id"]))
    c.status = CampaignStatus(status_val)
    await db_session.commit()

    # Schedule window change.
    with patch("app.workers.send.send_lead.apply_async"), \
         patch("app.workers.send.send_lead.delay"):
        resp = await client.patch(
            f"/campaigns/{created['id']}",
            json={
                "schedule_days": [0, 1, 2, 3, 4, 5],
                "schedule_time_start": "08:00:00",
                "schedule_time_end": "20:00:00",
                "schedule_timezone": "America/Los_Angeles",
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["schedule_days"] == [0, 1, 2, 3, 4, 5]
    assert body["schedule_time_start"] == "08:00:00"
    assert body["schedule_time_end"] == "20:00:00"
    assert body["schedule_timezone"] == "America/Los_Angeles"

    # Throughput change is also exempt.
    with patch("app.workers.send.send_lead.apply_async"), \
         patch("app.workers.send.send_lead.delay"):
        resp = await client.patch(
            f"/campaigns/{created['id']}",
            json={"min_delay_seconds": 180, "max_per_hour": 30, "max_per_day": 200},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["min_delay_seconds"] == 180
    assert body["max_per_hour"] == 30
    assert body["max_per_day"] == 200

    # But content fields like tone are still 409 (goal is now editable).
    blocked = await client.patch(
        f"/campaigns/{created['id']}", json={"tone": "new tone"}
    )
    assert blocked.status_code == 409


async def test_patch_schedule_change_re_enqueues_waiting_leads_staggered(
    client, db_session,
):
    """When the schedule window shifts on a running campaign, every composed
    PENDING+SCHEDULED lead should re-fire so they pick up the new window.
    SENT/FAILED/composing leads are not touched.  Sends are eta-spaced by
    min_delay (same shape as resume_campaign + _kick_off_full_campaign)."""
    created = (await client.post(
        "/campaigns/", json=_campaign_payload(min_delay_seconds=120)
    )).json()
    cid = uuid.UUID(created["id"])

    c = await db_session.get(Campaign, cid)
    c.status = CampaignStatus.RUNNING
    await db_session.commit()

    # 1 PENDING-composed + 2 SCHEDULED + 1 SENT + 1 PENDING-but-not-composed.
    for i, (s_status, c_status) in enumerate([
        (SendStatus.PENDING, ComposeStatus.DONE),
        (SendStatus.SCHEDULED, ComposeStatus.DONE),
        (SendStatus.SCHEDULED, ComposeStatus.DONE),
        (SendStatus.SENT, ComposeStatus.DONE),
        (SendStatus.PENDING, ComposeStatus.RUNNING),  # compose not done yet
    ]):
        db_session.add(Lead(
            campaign_id=cid, email=f"l{i}@x.com",
            send_status=s_status, compose_status=c_status,
            composed_subject="Hi" if c_status == ComposeStatus.DONE else None,
            composed_body="Body" if c_status == ComposeStatus.DONE else None,
        ))
    await db_session.commit()

    with patch("app.workers.send.send_lead.apply_async") as enqueue, \
         patch("app.workers.send.send_lead.delay") as delay_mock:
        resp = await client.patch(
            f"/campaigns/{cid}",
            json={"schedule_time_start": "08:00:00", "schedule_time_end": "22:00:00"},
        )

    assert resp.status_code == 200, resp.text
    delay_mock.assert_not_called()
    # Only the 3 composed PENDING+SCHEDULED leads (1 + 2) re-enqueue.
    assert enqueue.call_count == 3, [c.kwargs for c in enqueue.call_args_list]

    etas = sorted(call.kwargs["eta"] for call in enqueue.call_args_list)
    gaps = [(etas[i + 1] - etas[i]).total_seconds() for i in range(len(etas) - 1)]
    assert all(g == 120 for g in gaps), gaps


async def test_patch_throughput_only_does_not_re_enqueue(client, db_session):
    """Editing only throughput knobs (min_delay/hour/day) takes effect on the
    next gate claim naturally — no re-enqueue needed.  This is the cheap-
    edit path."""
    created = (await client.post("/campaigns/", json=_campaign_payload())).json()
    cid = uuid.UUID(created["id"])
    c = await db_session.get(Campaign, cid)
    c.status = CampaignStatus.RUNNING
    await db_session.commit()

    db_session.add(Lead(
        campaign_id=cid, email="ready@x.com",
        send_status=SendStatus.SCHEDULED, compose_status=ComposeStatus.DONE,
        composed_subject="Hi", composed_body="Body",
    ))
    await db_session.commit()

    with patch("app.workers.send.send_lead.apply_async") as enqueue, \
         patch("app.workers.send.send_lead.delay") as delay_mock:
        resp = await client.patch(
            f"/campaigns/{cid}", json={"min_delay_seconds": 300},
        )

    assert resp.status_code == 200
    enqueue.assert_not_called()
    delay_mock.assert_not_called()


async def test_patch_validates_time_order_when_both_provided(client):
    created = (await client.post("/campaigns/", json=_campaign_payload())).json()
    resp = await client.patch(
        f"/campaigns/{created['id']}",
        json={"schedule_time_start": "18:00:00", "schedule_time_end": "08:00:00"},
    )
    assert resp.status_code == 422


# ---------- Delete + cascade ----------


async def test_delete_lead_removes_lead_and_cascades_child_rows(client, db_session):
    """DELETE /campaigns/{id}/leads/{lid} drops the lead row and every
    cascading child (sequence state, step executions, email events).
    Future sequencer dispatches for this lead get short-circuited by the
    existing ``lead is None`` guard in the workers."""
    from app.models import LeadSequenceState, LeadStepExecution, LeadStepResult
    from sqlalchemy import select

    created = (await client.post("/campaigns/", json=_campaign_payload())).json()
    cid = uuid.UUID(created["id"])

    lead = Lead(campaign_id=cid, email="bye@y.com")
    other = Lead(campaign_id=cid, email="stay@y.com")
    db_session.add_all([lead, other])
    await db_session.flush()
    db_session.add_all([
        EmailEvent(lead_id=lead.id, campaign_id=cid, event_type=EmailEventType.DELIVERED),
        EmailEvent(lead_id=other.id, campaign_id=cid, event_type=EmailEventType.DELIVERED),
    ])
    await db_session.commit()
    lead_id, other_id = lead.id, other.id

    resp = await client.delete(f"/campaigns/{created['id']}/leads/{lead_id}")
    assert resp.status_code == 204

    # Target lead + its events: gone.
    assert (await db_session.scalar(select(Lead).where(Lead.id == lead_id))) is None
    assert (await db_session.scalar(
        select(EmailEvent).where(EmailEvent.lead_id == lead_id)
    )) is None
    # Sibling lead is untouched.
    assert (await db_session.scalar(select(Lead).where(Lead.id == other_id))) is not None


async def test_delete_lead_404_when_lead_belongs_to_different_campaign(client, db_session):
    """A request to delete a lead through a campaign URL that doesn't
    own it must 404 — defends against cross-campaign lead-id guessing."""
    c1 = (await client.post("/campaigns/", json=_campaign_payload(name="C1"))).json()
    c2 = (await client.post("/campaigns/", json=_campaign_payload(name="C2"))).json()

    lead = Lead(campaign_id=uuid.UUID(c1["id"]), email="lead@c1.com")
    db_session.add(lead)
    await db_session.commit()

    # Try to delete c1's lead through c2's URL.
    resp = await client.delete(f"/campaigns/{c2['id']}/leads/{lead.id}")
    assert resp.status_code == 404

    # Lead is still there.
    from sqlalchemy import select
    assert (await db_session.scalar(select(Lead).where(Lead.id == lead.id))) is not None


async def test_delete_lead_404_when_lead_missing(client):
    created = (await client.post("/campaigns/", json=_campaign_payload())).json()
    resp = await client.delete(f"/campaigns/{created['id']}/leads/{uuid.uuid4()}")
    assert resp.status_code == 404


# ---------- per-lead email edit + bulk signature ----------


async def test_edit_lead_email_updates_composed_fields(client, db_session):
    created = (await client.post("/campaigns/", json=_campaign_payload())).json()
    cid = uuid.UUID(created["id"])
    lead = Lead(
        campaign_id=cid, email="e@x.com",
        compose_status=ComposeStatus.DONE,
        composed_subject="Old subject", composed_body="Old body",
        send_status=SendStatus.PENDING,
    )
    db_session.add(lead)
    await db_session.commit()

    resp = await client.patch(
        f"/campaigns/{created['id']}/leads/{lead.id}",
        json={"composed_subject": "New subject", "composed_body": "New body"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["composed_subject"] == "New subject"
    assert body["composed_body"] == "New body"


async def test_edit_lead_email_blocked_when_already_sent(client, db_session):
    created = (await client.post("/campaigns/", json=_campaign_payload())).json()
    cid = uuid.UUID(created["id"])
    lead = Lead(
        campaign_id=cid, email="s@x.com",
        compose_status=ComposeStatus.DONE, composed_body="Body",
        send_status=SendStatus.SENT,
    )
    db_session.add(lead)
    await db_session.commit()

    resp = await client.patch(
        f"/campaigns/{created['id']}/leads/{lead.id}", json={"composed_body": "x"},
    )
    assert resp.status_code == 409


async def test_apply_signature_swaps_signoff_on_unsent_composed(client, db_session):
    from sqlalchemy import select
    sig = "Anthony Colasante\n555-1234\nacme.com\ncal.com/anthony"
    created = (await client.post(
        "/campaigns/", json=_campaign_payload(signature=sig)
    )).json()
    cid = uuid.UUID(created["id"])
    pending = Lead(
        campaign_id=cid, email="p@x.com", compose_status=ComposeStatus.DONE,
        composed_body="Hi,\n\nBody.\n\nBest,\nAnthony", send_status=SendStatus.PENDING,
    )
    sent = Lead(
        campaign_id=cid, email="sent@x.com", compose_status=ComposeStatus.DONE,
        composed_body="Hi,\n\nBody.\n\nBest,\nAnthony", send_status=SendStatus.SENT,
    )
    db_session.add_all([pending, sent])
    await db_session.commit()
    pending_id, sent_body = pending.id, sent.composed_body

    resp = await client.post(f"/campaigns/{created['id']}/apply-signature")
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 1  # only the pending one

    refreshed = await db_session.scalar(select(Lead).where(Lead.id == pending_id))
    await db_session.refresh(refreshed)
    assert refreshed.composed_body.endswith(sig)
    assert "Best,\nAnthony" not in refreshed.composed_body
    # Sent email untouched.
    sent_refreshed = await db_session.scalar(select(Lead).where(Lead.id == sent.id))
    await db_session.refresh(sent_refreshed)
    assert sent_refreshed.composed_body == sent_body


async def test_patch_lead_notes_works_even_when_sent(client, db_session):
    """Notes are CRM-lite and editable on any lead, including ones whose email
    has already been sent (only the composed copy is locked at that point)."""
    created = (await client.post("/campaigns/", json=_campaign_payload())).json()
    cid = uuid.UUID(created["id"])
    lead = Lead(
        campaign_id=cid, email="n@x.com", compose_status=ComposeStatus.DONE,
        composed_body="Hi", send_status=SendStatus.SENT,
    )
    db_session.add(lead)
    await db_session.commit()

    resp = await client.patch(
        f"/campaigns/{created['id']}/leads/{lead.id}",
        json={"notes": "Met at Lattice summit; warm intro from Sara"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["notes"] == "Met at Lattice summit; warm intro from Sara"

    # Composed-body edits on a sent lead still 409.
    blocked = await client.patch(
        f"/campaigns/{created['id']}/leads/{lead.id}",
        json={"composed_body": "x"},
    )
    assert blocked.status_code == 409


async def test_global_leads_endpoint_paginates_filters_and_includes_campaign(
    client, db_session
):
    """GET /leads returns leads from every campaign with campaign_name + the
    notes preview / has_notes flag, supports campaign + search filters."""
    c1 = (await client.post("/campaigns/", json=_campaign_payload(name="C1"))).json()
    c2 = (await client.post("/campaigns/", json=_campaign_payload(name="C2"))).json()
    db_session.add_all([
        Lead(campaign_id=uuid.UUID(c1["id"]), email="a@x.com",
             first_name="Alice", notes="Met at Lattice"),
        Lead(campaign_id=uuid.UUID(c1["id"]), email="b@x.com", first_name="Bob"),
        Lead(campaign_id=uuid.UUID(c2["id"]), email="c@x.com", first_name="Cara"),
    ])
    await db_session.commit()

    # All campaigns: 3 leads, campaign_name + has_notes populated.
    resp = await client.get("/leads")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    names = {item["campaign_name"] for item in body["items"]}
    assert names == {"C1", "C2"}
    alice = next(i for i in body["items"] if i["email"] == "a@x.com")
    assert alice["has_notes"] is True
    assert "Lattice" in alice["notes"]

    # Filter by campaign_id.
    resp = await client.get(f"/leads?campaign_id={c1['id']}")
    assert resp.json()["total"] == 2

    # Search by name.
    resp = await client.get("/leads?search=cara")
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["email"] == "c@x.com"

    # has_notes filter.
    resp = await client.get("/leads?has_notes=true")
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["email"] == "a@x.com"


async def test_get_lead_detail_returns_404_for_unknown(client):
    import uuid as _uuid
    resp = await client.get(f"/leads/{_uuid.uuid4()}")
    assert resp.status_code == 404


async def test_get_lead_detail_returns_rich_fields(client, db_session):
    """``GET /leads/{id}`` surfaces LinkedIn URL + research summary +
    contact fields the global list endpoint trims out."""
    c = (await client.post("/campaigns/", json=_campaign_payload(name="C"))).json()
    cid = uuid.UUID(c["id"])
    lead = Lead(
        campaign_id=cid, email="jane@acme.io",
        first_name="Jane", last_name="Doe",
        company="Acme", job_title="CFO", company_website="acme.io",
        linkedin_url="https://www.linkedin.com/in/jane-doe/",
        phone="+1-555-0100",
        research_data={
            "industry": "SaaS",
            "size_hint": "growth",
            "company_description": "B2B platform for ops teams.",
            "person_news": ["raised Series B (Apr 2026)"],
            "company_news": ["launched Acme Pro (Mar 2026)"],
            "from_cache": False,
            "quality": "rich",
        },
    )
    db_session.add(lead)
    await db_session.commit()

    resp = await client.get(f"/leads/{lead.id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Identity + contact fields.
    assert body["email"] == "jane@acme.io"
    assert body["linkedin_url"] == "https://www.linkedin.com/in/jane-doe/"
    assert body["phone"] == "+1-555-0100"
    assert body["job_title"] == "CFO"
    assert body["company_website"] == "acme.io"
    assert body["campaign_name"] == "C"
    # Research summary distilled out of the JSONB blob.
    sig = body["research_summary"]
    assert sig["industry"] == "SaaS"
    assert sig["size_hint"] == "growth"
    assert "raised Series B" in sig["person_news"][0]
    assert "Acme Pro" in sig["company_news"][0]
    # Empty history when no executions / events exist.
    assert body["history"] == []
    assert body["history_counts"] == {}


async def test_get_lead_detail_merges_executions_and_events_in_time_order(client, db_session):
    """Activity timeline must interleave LeadStepExecution rows
    (active outreach actions we took) with EmailEvent rows (passive
    engagement signals), sorted newest-first across both sources."""
    from datetime import datetime, timedelta, timezone
    from app.models import (
        LeadStepExecution, LeadStepResult, Sequence, SequenceNode, SequenceNodeKind,
    )

    c = (await client.post("/campaigns/", json=_campaign_payload(name="X"))).json()
    cid = uuid.UUID(c["id"])
    lead = Lead(campaign_id=cid, email="ll@x.com", first_name="L")
    db_session.add(lead)
    await db_session.flush()

    # Campaign creation auto-creates a default Sequence row (unique on
    # campaign_id); reuse it rather than constructing a duplicate.
    from sqlalchemy import select as _select
    seq = (await db_session.execute(
        _select(Sequence).where(Sequence.campaign_id == cid)
    )).scalar_one()
    email_node = SequenceNode(
        sequence_id=seq.id, kind=SequenceNodeKind.EMAIL,
        config={"subject_template": "s", "body_template": "b"},
        is_entry=True,
    )
    connect_node = SequenceNode(
        sequence_id=seq.id, kind=SequenceNodeKind.LINKEDIN_CONNECT,
        config={}, is_entry=False,
    )
    db_session.add_all([email_node, connect_node])
    await db_session.flush()

    now = datetime.now(timezone.utc)
    # Three executions across two nodes, plus two engagement events.
    db_session.add_all([
        LeadStepExecution(
            lead_id=lead.id, node_id=email_node.id,
            attempted_at=now - timedelta(hours=6),
            result=LeadStepResult.SENT, external_id="brevo-msg-1",
        ),
        LeadStepExecution(
            lead_id=lead.id, node_id=connect_node.id,
            attempted_at=now - timedelta(hours=2),
            result=LeadStepResult.SENT, external_id="invitation-9",
        ),
        LeadStepExecution(
            lead_id=lead.id, node_id=email_node.id,
            attempted_at=now - timedelta(hours=1),
            result=LeadStepResult.FAILED, error="Brevo 422",
        ),
        EmailEvent(
            lead_id=lead.id, campaign_id=cid,
            event_type=EmailEventType.DELIVERED,
            occurred_at=now - timedelta(hours=5),
        ),
        EmailEvent(
            lead_id=lead.id, campaign_id=cid,
            event_type=EmailEventType.OPENED,
            occurred_at=now - timedelta(hours=4),
        ),
    ])
    await db_session.commit()

    resp = await client.get(f"/leads/{lead.id}")
    assert resp.status_code == 200
    body = resp.json()

    history = body["history"]
    assert len(history) == 5

    # Sorted newest-first, regardless of source table.
    timestamps = [h["at"] for h in history]
    assert timestamps == sorted(timestamps, reverse=True)

    # Each entry carries the kind + action label + icon.
    kinds = [h["kind"] for h in history]
    assert kinds.count("execution") == 3
    assert kinds.count("event") == 2

    # Sample some specific entries.
    actions = [h["action"] for h in history]
    assert "Sent email" in actions
    assert "Sent LinkedIn connection request" in actions
    assert "Email delivered" in actions
    assert "Email opened" in actions
    # The failed send surfaces with the failed suffix + the error detail.
    failed_email = next(h for h in history if h["action"].startswith("Sent email (failed)"))
    assert failed_email["status"] == "fail"
    assert failed_email["detail"] == "Brevo 422"
    # And external_id passes through where present (Brevo msg id /
    # LinkedIn invitation id).
    sent_email = next(
        h for h in history
        if h["action"] == "Sent email" and h["external_id"] == "brevo-msg-1"
    )
    assert sent_email is not None

    # history_counts roll-up matches what we recorded — and only counts
    # SUCCESSFUL sends per node kind, plus every email-event type.
    counts = body["history_counts"]
    assert counts["email"] == 1                # 2 attempts, 1 SENT, 1 FAILED
    assert counts["linkedin_connect"] == 1
    assert counts["delivered"] == 1
    assert counts["opened"] == 1


async def test_get_lead_detail_skips_research_summary_when_blob_empty(client, db_session):
    """No research_data → research_summary is empty dict, not 500."""
    c = (await client.post("/campaigns/", json=_campaign_payload(name="Y"))).json()
    cid = uuid.UUID(c["id"])
    lead = Lead(campaign_id=cid, email="bare@x.com", research_data=None)
    db_session.add(lead)
    await db_session.commit()

    resp = await client.get(f"/leads/{lead.id}")
    assert resp.status_code == 200
    assert resp.json()["research_summary"] == {}


async def test_ignore_lead_suppresses_email_and_halts_states(client, db_session):
    """``POST /leads/{id}/ignore`` writes a Suppression row AND flips
    every active LeadSequenceState for any lead with the same email to
    HALTED with a clear reason.  Effect: future campaigns skip the
    email (send pipeline + sequencer both check suppression) and the
    current campaign stops progressing them."""
    from app.models import (
        LeadSequenceState, LeadSequenceStatus, Sequence,
        SequenceNode, SequenceNodeKind, Suppression,
    )
    from sqlalchemy import select as _select

    c = (await client.post("/campaigns/", json=_campaign_payload(name="C1"))).json()
    cid = uuid.UUID(c["id"])
    lead = Lead(campaign_id=cid, email="target@example.com", first_name="T")
    db_session.add(lead)
    await db_session.flush()

    # Reuse the auto-created sequence + add a node so the state row has
    # something to point at.
    seq = (await db_session.execute(
        _select(Sequence).where(Sequence.campaign_id == cid)
    )).scalar_one()
    node = SequenceNode(
        sequence_id=seq.id, kind=SequenceNodeKind.EMAIL,
        config={"subject_template": "s", "body_template": "b"},
        is_entry=True,
    )
    db_session.add(node)
    await db_session.flush()
    db_session.add(LeadSequenceState(
        sequence_id=seq.id, lead_id=lead.id, current_node_id=node.id,
        status=LeadSequenceStatus.ACTIVE,
    ))
    await db_session.commit()

    resp = await client.post(f"/leads/{lead.id}/ignore")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["suppressed"] is True
    assert body["already_suppressed"] is False
    assert body["leads_halted"] == 1
    assert str(cid) in body["campaigns_affected"]

    # Suppression row present, keyed on lowered email.
    s = await db_session.scalar(
        _select(Suppression).where(Suppression.email == "target@example.com")
    )
    assert s is not None
    assert s.reason.value == "manual"

    # State row is now HALTED with the ignored-by-user reason.
    state = await db_session.scalar(
        _select(LeadSequenceState).where(LeadSequenceState.lead_id == lead.id)
    )
    assert state.status == LeadSequenceStatus.HALTED
    assert state.halt_reason and "ignored" in state.halt_reason.lower()


async def test_ignore_lead_halts_states_across_multiple_campaigns(client, db_session):
    """When the same email appears in TWO campaigns, ignoring once
    halts both sequence state rows + reports both campaigns back."""
    from app.models import (
        LeadSequenceState, LeadSequenceStatus, Sequence,
        SequenceNode, SequenceNodeKind,
    )
    from sqlalchemy import select as _select

    c1 = (await client.post("/campaigns/", json=_campaign_payload(name="C1"))).json()
    c2 = (await client.post("/campaigns/", json=_campaign_payload(name="C2"))).json()
    c1id, c2id = uuid.UUID(c1["id"]), uuid.UUID(c2["id"])

    lead1 = Lead(campaign_id=c1id, email="dup@x.com", first_name="A")
    lead2 = Lead(campaign_id=c2id, email="dup@x.com", first_name="A")
    db_session.add_all([lead1, lead2])
    await db_session.flush()

    for cid, lead in ((c1id, lead1), (c2id, lead2)):
        seq = (await db_session.execute(
            _select(Sequence).where(Sequence.campaign_id == cid)
        )).scalar_one()
        node = SequenceNode(
            sequence_id=seq.id, kind=SequenceNodeKind.EMAIL,
            config={"subject_template": "s", "body_template": "b"},
            is_entry=True,
        )
        db_session.add(node)
        await db_session.flush()
        db_session.add(LeadSequenceState(
            sequence_id=seq.id, lead_id=lead.id, current_node_id=node.id,
            status=LeadSequenceStatus.ACTIVE,
        ))
    await db_session.commit()

    # Ignore via lead1's id — should still halt lead2's state too.
    resp = await client.post(f"/leads/{lead1.id}/ignore")
    body = resp.json()
    assert body["leads_halted"] == 2
    assert set(body["campaigns_affected"]) == {str(c1id), str(c2id)}

    # Both states halted.
    states = (await db_session.execute(
        _select(LeadSequenceState)
        .where(LeadSequenceState.lead_id.in_([lead1.id, lead2.id]))
    )).scalars().all()
    assert all(s.status == LeadSequenceStatus.HALTED for s in states)


async def test_ignore_lead_is_idempotent_on_repeat(client, db_session):
    """Re-ignoring an already-suppressed lead returns
    ``already_suppressed: true`` and doesn't error.  Defensive — repeat
    clicks from a confused user shouldn't 500."""
    from app.models import Suppression, SuppressionReason
    from sqlalchemy import select as _select

    c = (await client.post("/campaigns/", json=_campaign_payload(name="X"))).json()
    cid = uuid.UUID(c["id"])
    lead = Lead(campaign_id=cid, email="repeat@x.com")
    db_session.add(lead)
    db_session.add(Suppression(email="repeat@x.com", reason=SuppressionReason.MANUAL))
    await db_session.commit()

    resp = await client.post(f"/leads/{lead.id}/ignore")
    assert resp.status_code == 200
    body = resp.json()
    assert body["already_suppressed"] is True
    # Still only one suppression row.
    rows = (await db_session.execute(
        _select(Suppression).where(Suppression.email == "repeat@x.com")
    )).scalars().all()
    assert len(rows) == 1


async def test_ignore_lead_404_for_unknown(client):
    import uuid as _uuid
    resp = await client.post(f"/leads/{_uuid.uuid4()}/ignore")
    assert resp.status_code == 404


async def test_lead_detail_surfaces_is_suppressed_flag(client, db_session):
    """``GET /leads/{id}`` returns ``is_suppressed=true`` +
    ``suppression_reason`` so the UI can render the badge + swap the
    Ignore button for Un-ignore."""
    from app.models import Suppression, SuppressionReason

    c = (await client.post("/campaigns/", json=_campaign_payload(name="S"))).json()
    cid = uuid.UUID(c["id"])
    lead = Lead(campaign_id=cid, email="susp@x.com")
    db_session.add(lead)
    db_session.add(Suppression(email="susp@x.com", reason=SuppressionReason.MANUAL))
    await db_session.commit()

    resp = await client.get(f"/leads/{lead.id}")
    body = resp.json()
    assert body["is_suppressed"] is True
    assert body["suppression_reason"] == "manual"


async def test_unignore_lead_removes_suppression(client, db_session):
    """``DELETE /leads/{id}/ignore`` removes the suppression row.  Does
    NOT reactivate halted state rows — that's intentional (prevents
    accidental mass-resends)."""
    from app.models import (
        LeadSequenceState, LeadSequenceStatus, Sequence,
        SequenceNode, SequenceNodeKind, Suppression, SuppressionReason,
    )
    from sqlalchemy import select as _select

    c = (await client.post("/campaigns/", json=_campaign_payload(name="U"))).json()
    cid = uuid.UUID(c["id"])
    lead = Lead(campaign_id=cid, email="un@x.com")
    db_session.add(lead)
    db_session.add(Suppression(email="un@x.com", reason=SuppressionReason.MANUAL))
    await db_session.flush()
    # Seed a halted state row so we can verify it STAYS halted after unignore.
    seq = (await db_session.execute(
        _select(Sequence).where(Sequence.campaign_id == cid)
    )).scalar_one()
    node = SequenceNode(
        sequence_id=seq.id, kind=SequenceNodeKind.EMAIL,
        config={"subject_template": "s", "body_template": "b"},
        is_entry=True,
    )
    db_session.add(node)
    await db_session.flush()
    db_session.add(LeadSequenceState(
        sequence_id=seq.id, lead_id=lead.id, current_node_id=node.id,
        status=LeadSequenceStatus.HALTED,
        halt_reason="ignored by user (manual suppression)",
    ))
    await db_session.commit()

    resp = await client.delete(f"/leads/{lead.id}/ignore")
    assert resp.status_code == 204

    # Suppression row gone.
    rows = (await db_session.execute(
        _select(Suppression).where(Suppression.email == "un@x.com")
    )).scalars().all()
    assert rows == []

    # State row STAYS HALTED — user has to re-enroll explicitly.
    state = await db_session.scalar(
        _select(LeadSequenceState).where(LeadSequenceState.lead_id == lead.id)
    )
    assert state.status == LeadSequenceStatus.HALTED


async def test_re_enroll_halted_skips_suppressed_leads(client, db_session):
    """Re-enroll-halted must NOT resurrect leads whose email is on the
    suppression list — re-activating them would resume LinkedIn
    outreach (the sequencer fires LinkedIn steps; only email steps
    re-check the gate).  Regression for the ignored-lead-resurrection
    finding."""
    from app.models import (
        LeadSequenceState, LeadSequenceStatus, Sequence,
        SequenceNode, SequenceNodeKind, Suppression, SuppressionReason,
    )
    from sqlalchemy import select as _select

    c = (await client.post("/campaigns/", json=_campaign_payload(name="RE"))).json()
    cid = uuid.UUID(c["id"])

    ignored = Lead(campaign_id=cid, email="ignored@x.com")
    normal = Lead(campaign_id=cid, email="normal@x.com")
    db_session.add_all([ignored, normal])
    db_session.add(Suppression(email="ignored@x.com", reason=SuppressionReason.MANUAL))
    await db_session.flush()

    seq = (await db_session.execute(
        _select(Sequence).where(Sequence.campaign_id == cid)
    )).scalar_one()
    # The auto-created default sequence already has an entry node — use
    # it (adding a second is_entry node makes the endpoint's
    # scalar_one_or_none blow up with MultipleResultsFound).
    node = (await db_session.execute(
        _select(SequenceNode).where(
            SequenceNode.sequence_id == seq.id,
            SequenceNode.is_entry.is_(True),
        )
    )).scalar_one()
    for lead in (ignored, normal):
        db_session.add(LeadSequenceState(
            sequence_id=seq.id, lead_id=lead.id, current_node_id=node.id,
            status=LeadSequenceStatus.HALTED, halt_reason="whatever",
        ))
    await db_session.commit()

    resp = await client.post(f"/campaigns/{cid}/re-enroll-halted")
    assert resp.status_code == 200
    # Only the non-suppressed lead is re-enrolled; the suppressed one is
    # reported as skipped so the UI can explain a 0/low re-enrolled result.
    assert resp.json()["re_enrolled"] == 1
    assert resp.json()["skipped_suppressed"] == 1

    states = {
        s.lead_id: s for s in (await db_session.execute(
            _select(LeadSequenceState)
            .where(LeadSequenceState.lead_id.in_([ignored.id, normal.id]))
        )).scalars().all()
    }
    assert states[normal.id].status == LeadSequenceStatus.ACTIVE
    assert states[ignored.id].status == LeadSequenceStatus.HALTED


async def test_activity_splits_suppressed_out_of_halted(client, db_session):
    """A halted-but-suppressed lead is counted as sequence_suppressed (not
    sequence_halted) and kept out of the re-enroll halted_leads list — so the
    UI doesn't prompt to re-enroll leads that can never be re-enrolled."""
    from app.models import (
        LeadSequenceState, LeadSequenceStatus, Sequence,
        SequenceNode, Suppression, SuppressionReason,
    )
    from sqlalchemy import select as _select

    c = (await client.post("/campaigns/", json=_campaign_payload(name="ACT"))).json()
    cid = uuid.UUID(c["id"])
    supp = Lead(campaign_id=cid, email="supp@x.com")
    reenrollable = Lead(campaign_id=cid, email="reenroll@x.com")
    db_session.add_all([supp, reenrollable])
    db_session.add(Suppression(email="supp@x.com", reason=SuppressionReason.HARD_BOUNCE))
    await db_session.flush()
    seq = (await db_session.execute(_select(Sequence).where(Sequence.campaign_id == cid))).scalar_one()
    node = (await db_session.execute(_select(SequenceNode).where(
        SequenceNode.sequence_id == seq.id, SequenceNode.is_entry.is_(True),
    ))).scalar_one()
    for lead in (supp, reenrollable):
        db_session.add(LeadSequenceState(
            sequence_id=seq.id, lead_id=lead.id, current_node_id=node.id,
            status=LeadSequenceStatus.HALTED, halt_reason="x",
        ))
    await db_session.commit()

    body = (await client.get(f"/campaigns/{cid}/activity")).json()
    assert body["sequence_halted"] == 1       # only the re-enrollable one
    assert body["sequence_suppressed"] == 1    # the bounced one, counted apart
    emails = {h["email"] for h in body["halted_leads"]}
    assert emails == {"reenroll@x.com"}        # suppressed lead not in the panel


async def test_apply_signature_400_when_campaign_has_no_signature(client):
    created = (await client.post("/campaigns/", json=_campaign_payload())).json()
    resp = await client.post(f"/campaigns/{created['id']}/apply-signature")
    assert resp.status_code == 400


# ---------- signature inheritance from the connected account ----------


async def _make_account_with_sig(db_session, sig, email="sig@gmail.com"):
    acc = await _make_account(db_session, email=email)
    acc.signature = sig
    await db_session.commit()
    await db_session.refresh(acc)
    return acc


async def test_campaign_response_exposes_account_signature(client, db_session):
    acc = await _make_account_with_sig(db_session, "Acct Sig\nacme.com")
    created = (await client.post(
        "/campaigns/", json=_campaign_payload(connected_account_id=str(acc.id)),
    )).json()
    resp = await client.get(f"/campaigns/{created['id']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["signature"] is None                  # no per-campaign override
    assert body["account_signature"] == "Acct Sig\nacme.com"


async def test_apply_signature_uses_account_when_campaign_has_none(client, db_session):
    from sqlalchemy import select
    acc = await _make_account_with_sig(db_session, "Inherited Sig\nacme.com")
    created = (await client.post(
        "/campaigns/", json=_campaign_payload(connected_account_id=str(acc.id)),
    )).json()
    cid = uuid.UUID(created["id"])
    lead = Lead(
        campaign_id=cid, email="p@x.com", compose_status=ComposeStatus.DONE,
        composed_body="Hi,\n\nBody.\n\nBest,\nAnthony", send_status=SendStatus.PENDING,
    )
    db_session.add(lead)
    await db_session.commit()
    lead_id = lead.id

    resp = await client.post(f"/campaigns/{created['id']}/apply-signature")
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 1

    refreshed = await db_session.scalar(select(Lead).where(Lead.id == lead_id))
    await db_session.refresh(refreshed)
    assert refreshed.composed_body.endswith("Inherited Sig\nacme.com")


async def test_campaign_override_signature_wins_over_account(client, db_session):
    acc = await _make_account_with_sig(db_session, "Account Sig")
    created = (await client.post(
        "/campaigns/",
        json=_campaign_payload(connected_account_id=str(acc.id), signature="Override Sig"),
    )).json()
    resp = await client.get(f"/campaigns/{created['id']}")
    body = resp.json()
    assert body["signature"] == "Override Sig"
    assert body["account_signature"] == "Account Sig"


# ---------- goal edit rewrites unsent emails (no new research) ----------


async def _running_campaign_with_leads(client, db_session):
    created = (await client.post("/campaigns/", json=_campaign_payload())).json()
    cid = uuid.UUID(created["id"])
    c = await db_session.get(Campaign, cid)
    c.status = CampaignStatus.RUNNING
    pending = Lead(
        campaign_id=cid, email="p@x.com", compose_status=ComposeStatus.DONE,
        composed_body="Old body", send_status=SendStatus.PENDING,
    )
    scheduled = Lead(
        campaign_id=cid, email="sch@x.com", compose_status=ComposeStatus.DONE,
        composed_body="Old body", send_status=SendStatus.SCHEDULED,
    )
    sent = Lead(
        campaign_id=cid, email="sent@x.com", compose_status=ComposeStatus.DONE,
        composed_body="Old body", send_status=SendStatus.SENT,
    )
    db_session.add_all([pending, scheduled, sent])
    await db_session.commit()
    return created, {"pending": pending.id, "scheduled": scheduled.id, "sent": sent.id}


async def test_goal_edit_on_running_recomposes_only_unsent(client, db_session, monkeypatch):
    from unittest.mock import MagicMock
    created, ids = await _running_campaign_with_leads(client, db_session)
    delay = MagicMock()
    monkeypatch.setattr("app.workers.compose.compose_lead.delay", delay)

    resp = await client.patch(f"/campaigns/{created['id']}", json={"goal": "Brand new goal"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["goal"] == "Brand new goal"

    dispatched = {call.args[0] for call in delay.call_args_list}
    assert str(ids["pending"]) in dispatched
    assert str(ids["scheduled"]) in dispatched
    assert str(ids["sent"]) not in dispatched          # sent emails left alone
    assert len(dispatched) == 2


async def test_goal_edit_unchanged_does_not_recompose(client, db_session, monkeypatch):
    from unittest.mock import MagicMock
    created, _ = await _running_campaign_with_leads(client, db_session)
    delay = MagicMock()
    monkeypatch.setattr("app.workers.compose.compose_lead.delay", delay)

    # Same goal value the campaign already has → no rewrite.
    same_goal = (await client.get(f"/campaigns/{created['id']}")).json()["goal"]
    resp = await client.patch(f"/campaigns/{created['id']}", json={"goal": same_goal})
    assert resp.status_code == 200, resp.text
    delay.assert_not_called()


async def test_goal_edit_on_draft_does_not_recompose(client, db_session, monkeypatch):
    from unittest.mock import MagicMock
    created = (await client.post("/campaigns/", json=_campaign_payload())).json()
    delay = MagicMock()
    monkeypatch.setattr("app.workers.compose.compose_lead.delay", delay)

    resp = await client.patch(f"/campaigns/{created['id']}", json={"goal": "Draft goal"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["goal"] == "Draft goal"
    delay.assert_not_called()                          # nothing composed yet


async def test_goal_edit_on_complete_409(client, db_session):
    created = (await client.post("/campaigns/", json=_campaign_payload())).json()
    c = await db_session.get(Campaign, uuid.UUID(created["id"]))
    c.status = CampaignStatus.COMPLETE
    await db_session.commit()
    resp = await client.patch(f"/campaigns/{created['id']}", json={"goal": "x"})
    assert resp.status_code == 409


async def test_goal_edit_stamps_goal_updated_at_on_running(client, db_session, monkeypatch):
    """A real goal change on a non-draft campaign stamps ``goal_updated_at``
    so the progress endpoint can count rewrite progress.  Without this the
    UI can't tell which leads are still on the old goal."""
    from unittest.mock import MagicMock
    created, _ = await _running_campaign_with_leads(client, db_session)
    monkeypatch.setattr("app.workers.compose.compose_lead.delay", MagicMock())

    before = await db_session.get(Campaign, uuid.UUID(created["id"]))
    assert before.goal_updated_at is None

    resp = await client.patch(f"/campaigns/{created['id']}", json={"goal": "Brand new goal"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["goal_updated_at"] is not None

    after = await db_session.get(Campaign, uuid.UUID(created["id"]))
    await db_session.refresh(after)
    assert after.goal_updated_at is not None


async def test_goal_edit_unchanged_does_not_stamp_goal_updated_at(client, db_session, monkeypatch):
    from unittest.mock import MagicMock
    created, _ = await _running_campaign_with_leads(client, db_session)
    monkeypatch.setattr("app.workers.compose.compose_lead.delay", MagicMock())

    same_goal = (await client.get(f"/campaigns/{created['id']}")).json()["goal"]
    resp = await client.patch(f"/campaigns/{created['id']}", json={"goal": same_goal})
    assert resp.status_code == 200, resp.text
    assert resp.json()["goal_updated_at"] is None


async def test_goal_edit_on_draft_does_not_stamp_goal_updated_at(client, db_session, monkeypatch):
    """Draft campaigns have nothing to rewrite — don't stamp, keep the
    rewrite card hidden on first launch."""
    from unittest.mock import MagicMock
    created = (await client.post("/campaigns/", json=_campaign_payload())).json()
    monkeypatch.setattr("app.workers.compose.compose_lead.delay", MagicMock())

    resp = await client.patch(f"/campaigns/{created['id']}", json={"goal": "Draft goal"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["goal_updated_at"] is None


async def test_resolve_campaign_signature_fallback_chain(client, db_session):
    from app.models import Campaign
    from app.services.signature import resolve_campaign_signature

    acc = await _make_account_with_sig(db_session, "Account Sig", email="r@x.com")
    created = (await client.post(
        "/campaigns/", json=_campaign_payload(connected_account_id=str(acc.id)),
    )).json()
    camp = await db_session.get(Campaign, uuid.UUID(created["id"]))

    # Override set → wins.
    camp.signature = "Override"
    assert await resolve_campaign_signature(db_session, camp) == "Override"
    # No override → account signature.
    camp.signature = None
    assert await resolve_campaign_signature(db_session, camp) == "Account Sig"
    # No account → None.
    camp.connected_account_id = None
    assert await resolve_campaign_signature(db_session, camp) is None


async def test_delete_campaign_cascades_to_leads_and_events(client, db_session):
    created = (await client.post("/campaigns/", json=_campaign_payload())).json()
    cid = uuid.UUID(created["id"])

    lead = Lead(campaign_id=cid, email="x@y.com")
    db_session.add(lead)
    await db_session.flush()
    ev = EmailEvent(lead_id=lead.id, campaign_id=cid, event_type=EmailEventType.OPENED)
    db_session.add(ev)
    await db_session.commit()
    lead_id, ev_id = lead.id, ev.id

    resp = await client.delete(f"/campaigns/{created['id']}")
    assert resp.status_code == 204

    from sqlalchemy import select

    assert (await db_session.scalar(select(Lead).where(Lead.id == lead_id))) is None
    assert (await db_session.scalar(select(EmailEvent).where(EmailEvent.id == ev_id))) is None


# ---------- Pause / resume ----------


async def test_pause_only_works_when_running(client, db_session):
    created = (await client.post("/campaigns/", json=_campaign_payload())).json()
    # draft → pause should 409
    resp = await client.post(f"/campaigns/{created['id']}/pause")
    assert resp.status_code == 409

    c = await db_session.get(Campaign, uuid.UUID(created["id"]))
    c.status = CampaignStatus.RUNNING
    await db_session.commit()

    resp = await client.post(f"/campaigns/{created['id']}/pause")
    assert resp.status_code == 200
    assert resp.json()["status"] == "paused"


async def test_resume_only_works_when_paused(client, db_session):
    created = (await client.post("/campaigns/", json=_campaign_payload())).json()
    # draft → resume should 409
    resp = await client.post(f"/campaigns/{created['id']}/resume")
    assert resp.status_code == 409

    c = await db_session.get(Campaign, uuid.UUID(created["id"]))
    c.status = CampaignStatus.PAUSED
    await db_session.commit()

    # Campaign has no leads → resume should still succeed and not enqueue.
    with patch("app.workers.send.send_lead.apply_async"), \
         patch("app.workers.send.send_lead.delay"):
        resp = await client.post(f"/campaigns/{created['id']}/resume")
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"


async def test_resume_transitions_running_without_self_dispatch(client, db_session):
    """Resume flips the campaign to RUNNING but does NOT re-dispatch sends
    itself — the beat-driven pace_first_emails pacer re-feeds every composed
    PENDING/SCHEDULED first-email lead at the rate the caps allow.  The old
    eta-staggered re-enqueue here piled far-future-eta tasks the broker
    redelivered into a storm."""
    created = (await client.post(
        "/campaigns/", json=_campaign_payload(min_delay_seconds=240)
    )).json()
    cid = uuid.UUID(created["id"])

    # Set the campaign to PAUSED so resume is legal.
    c = await db_session.get(Campaign, cid)
    c.status = CampaignStatus.PAUSED
    await db_session.commit()

    for i, status_ in enumerate([
        SendStatus.PENDING, SendStatus.PENDING,
        SendStatus.SCHEDULED, SendStatus.SCHEDULED,
        SendStatus.SENT, SendStatus.FAILED,
    ]):
        db_session.add(Lead(
            campaign_id=cid, email=f"l{i}@x.com", send_status=status_,
            compose_status=ComposeStatus.DONE,
            composed_subject="Hi", composed_body="Body",
        ))
    await db_session.commit()

    with patch("app.workers.send.send_lead.apply_async") as enqueue, \
         patch("app.workers.send.send_lead.delay") as delay_mock:
        resp = await client.post(f"/campaigns/{cid}/resume")

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "running"
    # Resume no longer dispatches — the pacer does.
    enqueue.assert_not_called()
    delay_mock.assert_not_called()


async def test_resume_with_no_pending_leads_is_a_noop_dispatch(client, db_session):
    """A resume on a paused campaign with nothing to send shouldn't enqueue
    anything — and must NOT 500."""
    created = (await client.post("/campaigns/", json=_campaign_payload())).json()
    cid = uuid.UUID(created["id"])
    c = await db_session.get(Campaign, cid)
    c.status = CampaignStatus.PAUSED
    await db_session.commit()

    # One SENT lead — nothing else to dispatch.
    db_session.add(Lead(
        campaign_id=cid, email="done@x.com",
        send_status=SendStatus.SENT, compose_status=ComposeStatus.DONE,
    ))
    await db_session.commit()

    with patch("app.workers.send.send_lead.apply_async") as enqueue, \
         patch("app.workers.send.send_lead.delay") as delay_mock:
        resp = await client.post(f"/campaigns/{cid}/resume")

    assert resp.status_code == 200
    enqueue.assert_not_called()
    delay_mock.assert_not_called()


# ---------- Stats wiring ----------


async def test_stats_aggregate_lead_counts_and_event_rates(client, db_session):
    created = (await client.post("/campaigns/", json=_campaign_payload())).json()
    cid = uuid.UUID(created["id"])

    # 10 leads: 5 sent, 3 pending, 2 failed
    leads_sent = []
    for i in range(10):
        if i < 5:
            s = SendStatus.SENT
        elif i < 8:
            s = SendStatus.PENDING
        else:
            s = SendStatus.FAILED
        l = Lead(campaign_id=cid, email=f"l{i}@x.com", send_status=s)
        db_session.add(l)
        if s == SendStatus.SENT:
            leads_sent.append(l)
    await db_session.flush()

    # 3 opens, 2 clicks, 1 hard bounce (counted distinct per lead)
    for l in leads_sent[:3]:
        db_session.add(EmailEvent(lead_id=l.id, campaign_id=cid, event_type=EmailEventType.OPENED))
        # Duplicate open should still count as 1 lead
        db_session.add(EmailEvent(lead_id=l.id, campaign_id=cid, event_type=EmailEventType.OPENED))
    for l in leads_sent[:2]:
        db_session.add(EmailEvent(lead_id=l.id, campaign_id=cid, event_type=EmailEventType.CLICKED))
    db_session.add(
        EmailEvent(lead_id=leads_sent[4].id, campaign_id=cid, event_type=EmailEventType.HARD_BOUNCE)
    )
    await db_session.commit()

    body = (await client.get(f"/campaigns/{created['id']}")).json()
    counts = body["lead_counts"]
    stats = body["stats"]

    assert counts == {"total": 10, "pending": 3, "scheduled": 0, "sent": 5, "failed": 2}
    assert stats["sent_count"] == 5
    assert stats["opened"] == 3
    assert stats["clicked"] == 2
    assert stats["bounced"] == 1
    assert stats["open_rate"] == 0.6
    assert stats["click_rate"] == 0.4
    assert stats["bounce_rate"] == 0.2
    # No connected account → reply tracking disabled
    assert stats["reply_rate"] is None
    assert stats["reply_tracking_note"] == "reply tracking not configured"
    # Click tracking on by default → real rate + flag true.
    assert stats["click_tracking_enabled"] is True


async def test_click_rate_not_tracked_when_click_tracking_disabled(client, db_session, monkeypatch):
    """When click tracking is turned off in Brevo (the setting mirrors it),
    the stats report click_rate as None (UI shows 'not tracked') instead of a
    misleading 0%, even though a stray CLICKED event exists."""
    from app.config import settings as _settings
    monkeypatch.setattr(_settings, "EMAIL_CLICK_TRACKING_ENABLED", False)

    created = (await client.post("/campaigns/", json=_campaign_payload())).json()
    cid = uuid.UUID(created["id"])
    lead = Lead(campaign_id=cid, email="ct@x.com", send_status=SendStatus.SENT)
    db_session.add(lead)
    await db_session.flush()
    db_session.add(EmailEvent(lead_id=lead.id, campaign_id=cid, event_type=EmailEventType.CLICKED))
    await db_session.commit()

    stats = (await client.get(f"/campaigns/{created['id']}")).json()["stats"]
    assert stats["click_tracking_enabled"] is False
    assert stats["click_rate"] is None
    assert stats["clicked"] == 0
    # Open rate is unaffected (open tracking is independent).
    assert stats["open_rate"] is not None or stats["opened"] == 0


async def test_reply_rate_computed_when_connected_account_present(client, db_session):
    acc = await _make_account(db_session)
    created = (
        await client.post("/campaigns/", json=_campaign_payload(connected_account_id=str(acc.id)))
    ).json()
    cid = uuid.UUID(created["id"])

    leads = []
    for i in range(4):
        l = Lead(campaign_id=cid, email=f"l{i}@x.com", send_status=SendStatus.SENT)
        db_session.add(l)
        leads.append(l)
    await db_session.flush()
    db_session.add(EmailEvent(lead_id=leads[0].id, campaign_id=cid, event_type=EmailEventType.REPLIED))
    await db_session.commit()

    body = (await client.get(f"/campaigns/{created['id']}")).json()
    assert body["stats"]["reply_rate"] == 0.25
    assert body["stats"]["replied"] == 1
    assert body["stats"]["reply_tracking_note"] is None


# ---------- Paginated leads ----------


async def test_leads_pagination_and_filter_and_search(client, db_session):
    created = (await client.post("/campaigns/", json=_campaign_payload())).json()
    cid = uuid.UUID(created["id"])

    # 12 leads with mixed statuses + names
    for i in range(12):
        send_st = SendStatus.SENT if i % 2 == 0 else SendStatus.PENDING
        db_session.add(Lead(
            campaign_id=cid,
            email=f"lead{i:02d}@example.com",
            first_name=f"First{i}",
            last_name="Smith" if i < 6 else "Jones",
            send_status=send_st,
        ))
    await db_session.commit()

    # Page 1, default page_size=50 — returns all 12
    r = (await client.get(f"/campaigns/{created['id']}/leads")).json()
    assert r["total"] == 12
    assert r["page"] == 1
    assert len(r["items"]) == 12
    assert r["total_pages"] == 1

    # Page 1 with page_size=5
    r = (await client.get(f"/campaigns/{created['id']}/leads?page=1&page_size=5")).json()
    assert r["total"] == 12
    assert len(r["items"]) == 5
    assert r["total_pages"] == 3

    # Filter by send_status=sent → 6 results
    r = (await client.get(f"/campaigns/{created['id']}/leads?send_status=sent")).json()
    assert r["total"] == 6
    assert all(item["send_status"] == "sent" for item in r["items"])

    # Search by last name
    r = (await client.get(f"/campaigns/{created['id']}/leads?search=Smith")).json()
    assert r["total"] == 6
    assert all(item["last_name"] == "Smith" for item in r["items"])

    # Search by email substring
    r = (await client.get(f"/campaigns/{created['id']}/leads?search=lead01")).json()
    assert r["total"] == 1


async def test_leads_endpoint_404_for_unknown_campaign(client):
    resp = await client.get(f"/campaigns/{uuid.uuid4()}/leads")
    assert resp.status_code == 404


# ---------- per-lead sequence stage in the leads list ----------

from app.models import LeadSequenceState, LeadSequenceStatus  # noqa: E402
from app.services.sequence_service import ensure_default_sequence, enroll_leads  # noqa: E402


async def test_campaign_leads_show_active_sequence_stage(client, db_session):
    created = (await client.post("/campaigns/", json=_campaign_payload())).json()
    cid = uuid.UUID(created["id"])
    campaign = await db_session.get(Campaign, cid)
    await ensure_default_sequence(db_session, campaign)   # entry email node
    lead = Lead(campaign_id=cid, email="stage@x.com",
                compose_status=ComposeStatus.DONE, send_status=SendStatus.SENT)
    db_session.add(lead)
    await db_session.flush()
    await enroll_leads(db_session, cid, [lead.id])
    await db_session.commit()

    resp = await client.get(f"/campaigns/{created['id']}/leads")
    assert resp.status_code == 200, resp.text
    item = next(i for i in resp.json()["items"] if i["email"] == "stage@x.com")
    assert item["sequence_status"] == "active"
    assert item["sequence_stage"] == "Email"          # entry email, no title


async def test_campaign_leads_stage_completed_and_not_enrolled(client, db_session):
    created = (await client.post("/campaigns/", json=_campaign_payload())).json()
    cid = uuid.UUID(created["id"])
    campaign = await db_session.get(Campaign, cid)
    seq = await ensure_default_sequence(db_session, campaign)

    done = Lead(campaign_id=cid, email="done@x.com", send_status=SendStatus.SENT)
    bare = Lead(campaign_id=cid, email="bare@x.com")
    db_session.add_all([done, bare])
    await db_session.flush()
    db_session.add(LeadSequenceState(
        lead_id=done.id, sequence_id=seq.id, current_node_id=None,
        status=LeadSequenceStatus.COMPLETED,
    ))
    await db_session.commit()

    items = {i["email"]: i for i in (await client.get(f"/campaigns/{created['id']}/leads")).json()["items"]}
    assert items["done@x.com"]["sequence_status"] == "completed"
    assert items["done@x.com"]["sequence_stage"] == "Completed"
    # No state row → not enrolled.
    assert items["bare@x.com"]["sequence_status"] is None
    assert items["bare@x.com"]["sequence_stage"] == "Not enrolled"
