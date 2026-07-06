"""Tests for M5: soft-delete + per-node analytics endpoint."""
import uuid
from datetime import datetime, time, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import (
    Campaign,
    CampaignStatus,
    Lead,
    LeadSequenceState,
    LeadSequenceStatus,
    LeadStepExecution,
    LeadStepResult,
    SendStatus,
    Sequence,
    SequenceEdge,
    SequenceNode,
    SequenceNodeKind,
)
from app.services.sequence_service import (
    ensure_default_sequence,
    enroll_leads,
    replace_graph,
)
from app.workers import sequencer


@pytest.fixture(autouse=True)
def _reset_li_redis_singleton(monkeypatch):
    monkeypatch.setattr(sequencer, "_LI_REDIS_CLIENT", None)


async def _make_campaign(db_session) -> Campaign:
    c = Campaign(
        name="m5-test", goal="g", tone="Direct",
        sender_name="A", sender_email="a@example.com",
        schedule_days=list(range(7)),
        schedule_time_start=time(0, 0), schedule_time_end=time(23, 59),
        schedule_timezone="UTC", status=CampaignStatus.RUNNING,
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    return c


# --------------------------------------------------------------------------
# Soft-delete
# --------------------------------------------------------------------------


async def test_replace_graph_soft_deletes_existing_nodes(db_session):
    campaign = await _make_campaign(db_session)
    seq = await ensure_default_sequence(db_session, campaign)
    await db_session.commit()

    # Capture the original entry node id.
    original_entry = await db_session.scalar(
        select(SequenceNode).where(
            SequenceNode.sequence_id == seq.id, SequenceNode.is_entry.is_(True),
        )
    )

    await replace_graph(db_session, seq, [
        {"client_id": "e", "kind": "email", "is_entry": True, "config": {}, "position_x": 0, "position_y": 0},
    ], [])
    await db_session.commit()

    # Original entry node still exists in the table, just soft-deleted.
    await db_session.refresh(original_entry)
    assert original_entry.deleted_at is not None

    # A new live entry node was created.
    live_entries = (await db_session.execute(
        select(SequenceNode).where(
            SequenceNode.sequence_id == seq.id,
            SequenceNode.is_entry.is_(True),
            SequenceNode.deleted_at.is_(None),
        )
    )).scalars().all()
    assert len(live_entries) == 1
    assert live_entries[0].id != original_entry.id


async def test_soft_deleted_nodes_preserve_step_executions(db_session):
    """Step-execution rows pointing at retired nodes survive the rewrite."""
    campaign = await _make_campaign(db_session)
    seq = await ensure_default_sequence(db_session, campaign)
    await db_session.commit()
    entry = await db_session.scalar(
        select(SequenceNode).where(
            SequenceNode.sequence_id == seq.id, SequenceNode.is_entry.is_(True),
        )
    )
    lead = Lead(
        campaign_id=campaign.id, email="x@x.com",
        send_status=SendStatus.PENDING,
    )
    db_session.add(lead)
    await db_session.commit()

    db_session.add(LeadStepExecution(
        lead_id=lead.id, node_id=entry.id,
        result=LeadStepResult.SENT, external_id="msg-1",
    ))
    await db_session.commit()

    # Rewrite the graph.
    await replace_graph(db_session, seq, [
        {"client_id": "new", "kind": "email", "is_entry": True, "config": {}, "position_x": 0, "position_y": 0},
    ], [])
    await db_session.commit()

    # Execution row still exists (FK didn't cascade-delete it).
    rows = (await db_session.execute(
        select(LeadStepExecution).where(LeadStepExecution.lead_id == lead.id)
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].node_id == entry.id  # points at the now-retired node


async def test_scheduler_halts_lead_on_deleted_node(db_session):
    campaign = await _make_campaign(db_session)
    seq = await ensure_default_sequence(db_session, campaign)
    entry = await db_session.scalar(
        select(SequenceNode).where(
            SequenceNode.sequence_id == seq.id, SequenceNode.is_entry.is_(True),
        )
    )
    lead = Lead(
        campaign_id=campaign.id, email="x@x.com",
        send_status=SendStatus.SENT,
    )
    db_session.add(lead)
    await db_session.commit()
    state = LeadSequenceState(
        lead_id=lead.id, sequence_id=seq.id,
        current_node_id=entry.id, status=LeadSequenceStatus.ACTIVE,
        next_run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        entered_current_at=datetime.now(timezone.utc),
    )
    db_session.add(state)
    await db_session.commit()

    # Retire the entry node.
    entry.deleted_at = datetime.now(timezone.utc)
    await db_session.commit()

    await sequencer._advance_sequences_async()

    await db_session.refresh(state)
    assert state.status == LeadSequenceStatus.HALTED
    assert "deleted" in (state.halt_reason or "")


# --------------------------------------------------------------------------
# Analytics endpoint
# --------------------------------------------------------------------------


def _payload(**overrides):
    base = {
        "name": "x", "goal": "g", "tone": "Direct",
        "sender_name": "A", "sender_email": "a@example.com",
        "research_mode": "fast",
        "schedule_days": [0, 1, 2, 3, 4],
        "schedule_time_start": "09:00:00",
        "schedule_time_end": "17:00:00",
        "schedule_timezone": "UTC",
    }
    base.update(overrides)
    return base


async def test_analytics_empty_campaign_returns_zeros(client):
    r = await client.post("/campaigns/", json=_payload())
    cid = r.json()["id"]
    a = await client.get(f"/campaigns/{cid}/sequence/analytics")
    assert a.status_code == 200
    body = a.json()
    assert body["total_leads"] == 0
    assert body["active"] == 0
    assert len(body["per_node"]) == 1  # the auto-created entry node
    assert body["per_node"][0]["attempted"] == 0


async def test_analytics_counts_step_executions(client, db_session):
    """A SECONDARY (non-entry) node aggregates its lead_step_executions —
    this is the path that surfaces follow-up emails / replies / LinkedIn
    steps.  (The entry email node is special-cased; see the next test.)"""
    r = await client.post("/campaigns/", json=_payload())
    cid = uuid.UUID(r.json()["id"])

    seq = (await db_session.execute(
        select(Sequence).where(Sequence.campaign_id == cid)
    )).scalar_one()
    # A follow-up email node downstream of the auto-created entry node.
    followup = SequenceNode(
        sequence_id=seq.id, kind=SequenceNodeKind.EMAIL, is_entry=False,
        config={"title": "Follow-up", "subject_template": "s", "body_template": "b"},
    )
    db_session.add(followup)
    await db_session.commit()
    await db_session.refresh(followup)

    leads = []
    for i in range(3):
        l = Lead(campaign_id=cid, email=f"l{i}@x.com", send_status=SendStatus.SENT)
        db_session.add(l)
        leads.append(l)
    await db_session.commit()
    for l in leads:
        await db_session.refresh(l)

    db_session.add_all([
        LeadStepExecution(lead_id=leads[0].id, node_id=followup.id, result=LeadStepResult.SENT),
        LeadStepExecution(lead_id=leads[1].id, node_id=followup.id, result=LeadStepResult.SENT),
        LeadStepExecution(lead_id=leads[2].id, node_id=followup.id, result=LeadStepResult.SKIPPED),
    ])
    await db_session.commit()

    a = await client.get(f"/campaigns/{cid}/sequence/analytics")
    body = a.json()
    assert len(body["per_node"]) == 2
    node = next(n for n in body["per_node"] if n["node_id"] == str(followup.id))
    assert node["kind"] == "email"
    assert node["title"] == "Follow-up"
    assert node["is_entry"] is False
    assert node["attempted"] == 3
    assert node["sent"] == 2
    assert node["skipped"] == 1
    assert node["failed"] == 0


async def test_analytics_here_already_sent_split(client, db_session):
    """Of the leads sitting on a node, here_already_sent counts those that
    already have a SENT execution for it (will skip-and-advance, no resend)."""
    r = await client.post("/campaigns/", json=_payload())
    cid = uuid.UUID(r.json()["id"])
    seq = (await db_session.execute(
        select(Sequence).where(Sequence.campaign_id == cid)
    )).scalar_one()
    node = SequenceNode(
        sequence_id=seq.id, kind=SequenceNodeKind.EMAIL_REPLY, is_entry=False,
        config={"title": "Reply"},
    )
    db_session.add(node)
    await db_session.commit()
    await db_session.refresh(node)

    leads = []
    for i in range(3):
        l = Lead(campaign_id=cid, email=f"r{i}@x.com", send_status=SendStatus.SENT)
        db_session.add(l)
        leads.append(l)
    await db_session.commit()
    for l in leads:
        await db_session.refresh(l)

    # All 3 are ACTIVE on the node; 2 already sent it (re-queued after a
    # republish), 1 is awaiting its first send.
    for l in leads:
        db_session.add(LeadSequenceState(
            lead_id=l.id, sequence_id=seq.id, current_node_id=node.id,
            status=LeadSequenceStatus.ACTIVE,
        ))
    db_session.add_all([
        LeadStepExecution(lead_id=leads[0].id, node_id=node.id, result=LeadStepResult.SENT),
        LeadStepExecution(lead_id=leads[1].id, node_id=node.id, result=LeadStepResult.SENT),
    ])
    await db_session.commit()

    a = await client.get(f"/campaigns/{cid}/sequence/analytics")
    n = next(x for x in a.json()["per_node"] if x["node_id"] == str(node.id))
    assert n["currently_here"] == 3
    assert n["here_already_sent"] == 2   # will skip, no resend
    # → 1 awaiting first send (currently_here - here_already_sent)


async def test_analytics_entry_email_uses_send_status(client, db_session):
    """The entry EMAIL node reflects the legacy first-email outcome
    (lead.send_status), since the compose -> send_lead path never writes
    lead_step_executions for it."""
    r = await client.post("/campaigns/", json=_payload())
    cid = uuid.UUID(r.json()["id"])

    for i in range(4):
        db_session.add(Lead(
            campaign_id=cid, email=f"l{i}@x.com",
            send_status=SendStatus.SENT if i < 3 else SendStatus.FAILED,
        ))
    await db_session.commit()

    a = await client.get(f"/campaigns/{cid}/sequence/analytics")
    body = a.json()
    assert len(body["per_node"]) == 1
    entry = body["per_node"][0]
    assert entry["is_entry"] is True
    assert entry["sent"] == 3
    assert entry["failed"] == 1
    assert entry["attempted"] == 4


async def test_analytics_counts_active_leads(client, db_session):
    r = await client.post("/campaigns/", json=_payload())
    cid = uuid.UUID(r.json()["id"])
    campaign = await db_session.get(Campaign, cid)
    await enroll_leads(db_session, cid, [])  # no-op (no leads yet)

    # Add two leads, enroll them on the entry node.
    leads = []
    for i in range(2):
        l = Lead(campaign_id=cid, email=f"a{i}@x.com")
        db_session.add(l)
        leads.append(l)
    await db_session.commit()
    for l in leads:
        await db_session.refresh(l)
    await enroll_leads(db_session, cid, [l.id for l in leads])
    await db_session.commit()

    a = await client.get(f"/campaigns/{cid}/sequence/analytics")
    body = a.json()
    assert body["total_leads"] == 2
    assert body["active"] == 2
    assert body["per_node"][0]["currently_here"] == 2


async def test_analytics_excludes_soft_deleted_nodes(client, db_session):
    r = await client.post("/campaigns/", json=_payload())
    cid = uuid.UUID(r.json()["id"])
    seq = (await db_session.execute(
        select(Sequence).where(Sequence.campaign_id == cid)
    )).scalar_one()

    # Rewrite the graph — old entry node becomes soft-deleted.
    await replace_graph(db_session, seq, [
        {"client_id": "new", "kind": "email", "is_entry": True, "config": {}, "position_x": 0, "position_y": 0},
    ], [])
    await db_session.commit()

    a = await client.get(f"/campaigns/{cid}/sequence/analytics")
    body = a.json()
    # Only the live (new) entry node shows up — not the retired one.
    assert len(body["per_node"]) == 1


# --------------------------------------------------------------------------
# Additional analytics edge cases
# --------------------------------------------------------------------------


async def test_sequence_analytics_empty_campaign_all_zeros(db_session, client):
    """Fresh campaign with no leads should return zeroed status counts."""
    campaign = await _make_campaign(db_session)
    r = await client.get(f"/campaigns/{campaign.id}/sequence/analytics")
    assert r.status_code == 200
    body = r.json()
    # No leads enrolled yet → all counters are 0.
    assert body.get("active", 0) == 0
    assert body.get("halted", 0) == 0
    assert body.get("completed", 0) == 0
    assert body.get("total_leads", 0) == 0


async def test_sequence_analytics_per_node_counts_correct(db_session, client):
    """After running a lead through a node, counts appear in analytics."""
    campaign = await _make_campaign(db_session)
    seq = await ensure_default_sequence(db_session, campaign)
    await db_session.commit()

    # Enroll one lead and record a step execution.
    lead = Lead(
        campaign_id=campaign.id,
        email="test@example.com",
        send_status=SendStatus.SENT,
    )
    db_session.add(lead)
    await db_session.commit()
    await db_session.refresh(lead)

    await enroll_leads(db_session, campaign.id, [lead.id])
    await db_session.commit()

    # Find the entry node.
    entry_node = await db_session.scalar(
        select(SequenceNode).where(
            SequenceNode.sequence_id == seq.id,
            SequenceNode.is_entry.is_(True),
            SequenceNode.deleted_at.is_(None),
        )
    )
    assert entry_node is not None

    exec_row = LeadStepExecution(
        lead_id=lead.id,
        node_id=entry_node.id,
        result=LeadStepResult.SENT,
    )
    db_session.add(exec_row)
    await db_session.commit()

    r = await client.get(f"/campaigns/{campaign.id}/sequence/analytics")
    body = r.json()
    node_stats = {s["node_id"]: s for s in body["per_node"]}
    stats = node_stats.get(str(entry_node.id))
    assert stats is not None
    assert stats["sent"] >= 1


async def test_sequence_analytics_halted_lead_counted(db_session, client):
    """A halted lead_sequence_state contributes to the 'halted' count."""
    campaign = await _make_campaign(db_session)
    await ensure_default_sequence(db_session, campaign)
    await db_session.commit()

    lead = Lead(
        campaign_id=campaign.id,
        email="halted@example.com",
        send_status=SendStatus.PENDING,
    )
    db_session.add(lead)
    await db_session.commit()
    await db_session.refresh(lead)

    await enroll_leads(db_session, campaign.id, [lead.id])
    await db_session.commit()

    # Manually set the state to halted.
    state = await db_session.scalar(
        select(LeadSequenceState).where(LeadSequenceState.lead_id == lead.id)
    )
    assert state is not None
    state.status = LeadSequenceStatus.HALTED
    state.halt_reason = "test halt"
    await db_session.commit()

    r = await client.get(f"/campaigns/{campaign.id}/sequence/analytics")
    body = r.json()
    assert body.get("halted", 0) >= 1
