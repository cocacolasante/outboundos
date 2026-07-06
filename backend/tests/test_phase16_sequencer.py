"""Integration tests for the sequence scheduler (advance_sequences) +
send_email_step.

These directly drive ``_advance_sequences_async`` rather than going through
Celery, so we don't depend on a worker process. Brevo is monkeypatched.
"""
from datetime import datetime, time, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import (
    Campaign,
    CampaignStatus,
    EmailEvent,
    EmailEventType,
    Lead,
    LeadSequenceState,
    LeadSequenceStatus,
    LeadStepExecution,
    LeadStepResult,
    Sequence,
    SequenceEdge,
    SequenceNode,
    SequenceNodeKind,
    SendStatus,
)
from app.services.sequence_service import (
    enroll_leads,
    ensure_default_sequence,
    reenroll_for_new_nodes,
    replace_graph,
)
from app.workers import sequencer


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _make_campaign(db_session) -> Campaign:
    c = Campaign(
        name="seq-test",
        goal="Book a call",
        tone="Direct",
        sender_name="A",
        sender_email="a@example.com",
        schedule_days=[0, 1, 2, 3, 4, 5, 6],  # always-on
        schedule_time_start=time(0, 0),
        schedule_time_end=time(23, 59),
        schedule_timezone="UTC",
        status=CampaignStatus.RUNNING,
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    return c


async def _make_lead(db_session, campaign, email="lead@example.com") -> Lead:
    l = Lead(
        campaign_id=campaign.id,
        email=email,
        first_name="L",
        composed_subject="hi",
        composed_body="body",
        send_status=SendStatus.PENDING,
    )
    db_session.add(l)
    await db_session.commit()
    await db_session.refresh(l)
    return l


# --------------------------------------------------------------------------
# Default-sequence-only campaign: entry node advances when lead already sent
# --------------------------------------------------------------------------


async def test_entry_node_advances_after_legacy_send_completes(db_session):
    campaign = await _make_campaign(db_session)
    await ensure_default_sequence(db_session, campaign)
    lead = await _make_lead(db_session, campaign)
    await enroll_leads(db_session, campaign.id, [lead.id])
    await db_session.commit()

    # Simulate the legacy compose→send path having finished.
    lead.send_status = SendStatus.SENT
    await db_session.commit()

    # Tick the scheduler — should advance past the entry node and complete.
    counts = await sequencer._advance_sequences_async()
    assert counts["advanced_entry_done"] >= 1

    await db_session.refresh(lead)
    state = await db_session.scalar(
        select(LeadSequenceState).where(LeadSequenceState.lead_id == lead.id)
    )
    assert state.status == LeadSequenceStatus.COMPLETED
    assert state.current_node_id is None


async def test_entry_node_waits_when_legacy_send_pending(db_session):
    campaign = await _make_campaign(db_session)
    await ensure_default_sequence(db_session, campaign)
    lead = await _make_lead(db_session, campaign)
    await enroll_leads(db_session, campaign.id, [lead.id])
    await db_session.commit()
    # lead.send_status is still PENDING — scheduler should NOT advance.

    counts = await sequencer._advance_sequences_async()
    assert counts["advanced_entry_done"] == 0

    state = await db_session.scalar(
        select(LeadSequenceState).where(LeadSequenceState.lead_id == lead.id)
    )
    assert state.status == LeadSequenceStatus.ACTIVE
    # next_run_at pushed 5 minutes out (so we don't busy-spin)
    assert state.next_run_at > _now() + timedelta(minutes=4)


# --------------------------------------------------------------------------
# Wait node: scheduler advances when duration elapses
# --------------------------------------------------------------------------


async def _build_three_node_sequence(db_session, campaign) -> tuple[Sequence, SequenceNode, SequenceNode, SequenceNode]:
    """email entry --always--> wait --not replied--> followup-email --> end."""
    seq = Sequence(campaign_id=campaign.id, is_published=True)
    db_session.add(seq)
    await db_session.flush()

    entry = SequenceNode(
        sequence_id=seq.id, kind=SequenceNodeKind.EMAIL,
        config={"use_campaign_compose": True}, is_entry=True,
    )
    wait_node = SequenceNode(
        sequence_id=seq.id, kind=SequenceNodeKind.WAIT,
        config={"duration_minutes": 60}, is_entry=False,
    )
    followup = SequenceNode(
        sequence_id=seq.id, kind=SequenceNodeKind.EMAIL,
        config={"subject_template": "Hi {{first_name}}", "body_template": "Body for {{first_name}}"},
        is_entry=False,
    )
    db_session.add_all([entry, wait_node, followup])
    await db_session.flush()

    db_session.add_all([
        SequenceEdge(sequence_id=seq.id, from_node_id=entry.id, to_node_id=wait_node.id, condition={"op": "always"}),
        SequenceEdge(
            sequence_id=seq.id, from_node_id=wait_node.id, to_node_id=followup.id,
            condition={"op": "not", "child": {"op": "replied"}},
        ),
        SequenceEdge(sequence_id=seq.id, from_node_id=followup.id, to_node_id=None, condition={"op": "always"}),
    ])
    await db_session.commit()
    return seq, entry, wait_node, followup


async def test_wait_node_advances_when_due(db_session, monkeypatch):
    campaign = await _make_campaign(db_session)
    _, entry, wait_node, followup = await _build_three_node_sequence(db_session, campaign)
    lead = await _make_lead(db_session, campaign)
    lead.send_status = SendStatus.SENT  # entry already done via legacy path
    await db_session.commit()

    # Park the lead on the wait node with a next_run_at in the past.
    state = LeadSequenceState(
        lead_id=lead.id, sequence_id=wait_node.sequence_id,
        current_node_id=wait_node.id,
        status=LeadSequenceStatus.ACTIVE,
        next_run_at=_now() - timedelta(minutes=1),
        entered_current_at=_now() - timedelta(minutes=61),
    )
    db_session.add(state)
    await db_session.commit()

    # Capture dispatched email-step tasks so the scheduler doesn't enqueue
    # against a real Celery broker during the test.
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        sequencer.send_email_step, "delay",
        lambda lead_id, node_id: sent.append((lead_id, node_id)),
    )

    counts = await sequencer._advance_sequences_async()
    assert counts["advanced_wait"] == 1

    await db_session.refresh(state)
    # After the wait, we're now on the followup node; next_run_at is "now"
    # so the next tick dispatches it.
    assert state.current_node_id == followup.id
    assert state.status == LeadSequenceStatus.ACTIVE

    # Another tick should dispatch the followup email step.
    counts2 = await sequencer._advance_sequences_async()
    assert counts2["dispatched_email"] == 1
    assert sent == [(str(lead.id), str(followup.id))]


async def test_skip_on_reply_halts_followup(db_session, monkeypatch):
    campaign = await _make_campaign(db_session)
    _, entry, wait_node, followup = await _build_three_node_sequence(db_session, campaign)
    lead = await _make_lead(db_session, campaign)
    lead.send_status = SendStatus.SENT
    await db_session.commit()

    # Record a REPLIED event for this lead — the wait→followup edge has a
    # NOT replied condition, which now fails. Since there's no other edge,
    # the lead halts.
    db_session.add(EmailEvent(
        lead_id=lead.id, campaign_id=campaign.id,
        event_type=EmailEventType.REPLIED,
    ))
    state = LeadSequenceState(
        lead_id=lead.id, sequence_id=wait_node.sequence_id,
        current_node_id=wait_node.id, status=LeadSequenceStatus.ACTIVE,
        next_run_at=_now() - timedelta(minutes=1),
        entered_current_at=_now() - timedelta(minutes=61),
    )
    db_session.add(state)
    await db_session.commit()

    monkeypatch.setattr(sequencer.send_email_step, "delay", lambda *a, **k: None)
    await sequencer._advance_sequences_async()

    await db_session.refresh(state)
    assert state.status == LeadSequenceStatus.HALTED
    assert "no outgoing edge matched" in (state.halt_reason or "")


# --------------------------------------------------------------------------
# send_email_step writes an execution row + advances cursor
# --------------------------------------------------------------------------


async def test_send_email_step_records_execution_and_advances(db_session, monkeypatch):
    campaign = await _make_campaign(db_session)
    _, entry, wait_node, followup = await _build_three_node_sequence(db_session, campaign)
    lead = await _make_lead(db_session, campaign)
    lead.send_status = SendStatus.SENT
    await db_session.commit()

    # Park lead on the followup node, ready to fire.
    state = LeadSequenceState(
        lead_id=lead.id, sequence_id=followup.sequence_id,
        current_node_id=followup.id, status=LeadSequenceStatus.ACTIVE,
        next_run_at=_now(), entered_current_at=_now(),
    )
    db_session.add(state)
    await db_session.commit()

    # Stub brevo so we don't make a real API call.
    async def fake_send(**kwargs):
        return "fake-message-id-123"
    monkeypatch.setattr(sequencer.brevo, "send_email", fake_send)

    result = await sequencer._send_email_step_async(str(lead.id), str(followup.id))
    assert result["status"] == "sent"
    assert result["message_id"] == "fake-message-id-123"

    await sequencer._record_execution_and_advance(lead.id, followup.id, result)

    exec_rows = (await db_session.execute(
        select(LeadStepExecution).where(LeadStepExecution.lead_id == lead.id)
    )).scalars().all()
    assert len(exec_rows) == 1
    assert exec_rows[0].result == LeadStepResult.SENT
    assert exec_rows[0].external_id == "fake-message-id-123"

    await db_session.refresh(state)
    # Followup terminates the branch (edge to_node_id=None).
    assert state.status == LeadSequenceStatus.COMPLETED
    assert state.current_node_id is None


async def test_sent_email_reanchors_clock_to_prevent_cascade(db_session):
    """A delayed/re-queued send (stale entered_current_at) must NOT immediately
    fire the next email: recording the SENT re-anchors entered_current_at to the
    send time, so the outgoing days_since_entered_node gate counts from now and
    the lead parks instead of cascading into the next reply seconds later."""
    campaign = await _make_campaign(db_session)
    seq = await ensure_default_sequence(db_session, campaign)
    m = await replace_graph(db_session, seq,
        nodes=[
            _n("e", "email", entry=True),
            _n("r1", "email_reply", ai_compose=True),
            _n("r2", "email_reply", ai_compose=True),
        ],
        edges=[
            _e("e", "r1"),
            _e("r1", "r2", condition={"op": "days_since_entered_node", "gte": 3}),
        ],
    )
    await db_session.commit()
    r1 = m["r1"]

    lead = await _make_lead(db_session, campaign)
    lead.send_status = SendStatus.SENT
    lead.brevo_message_id = "<orig@x>"
    # Parked on reply #1 with a STALE clock (arrived 5 days ago; reply #1 was
    # delayed and only sends now).
    state = LeadSequenceState(
        lead_id=lead.id, sequence_id=seq.id, current_node_id=r1,
        status=LeadSequenceStatus.ACTIVE, next_run_at=_now(),
        entered_current_at=_now() - timedelta(days=5),
    )
    db_session.add(state)
    await db_session.commit()

    await sequencer._record_execution_and_advance(
        lead.id, r1, {"status": "sent", "message_id": "<reply1@x>"})

    await db_session.refresh(state)
    # Did NOT cascade into reply #2 — parked on reply #1 with a fresh clock.
    assert state.current_node_id == r1
    assert state.status == LeadSequenceStatus.ACTIVE
    assert (_now() - state.entered_current_at) < timedelta(minutes=2)


async def test_followup_email_renders_inserted_html_links(db_session, monkeypatch):
    """The sequencer send path goes through the shared render_email_with_signature,
    so an HTML link in a follow-up body renders as a real anchor in the HTML
    body (not escaped) — consistent with the first-email/one-off paths."""
    campaign = await _make_campaign(db_session)
    _, entry, wait_node, followup = await _build_three_node_sequence(db_session, campaign)
    followup.config = {
        "subject_template": "Hi {{first_name}}",
        "body_template": 'Click <a href="https://grantmind.pro">here</a>',
    }
    lead = await _make_lead(db_session, campaign)
    lead.send_status = SendStatus.SENT
    await db_session.commit()
    state = LeadSequenceState(
        lead_id=lead.id, sequence_id=followup.sequence_id,
        current_node_id=followup.id, status=LeadSequenceStatus.ACTIVE,
        next_run_at=_now(), entered_current_at=_now(),
    )
    db_session.add(state)
    await db_session.commit()

    captured: dict = {}
    async def fake_send(**kwargs):
        captured.update(kwargs)
        return "mid-1"
    monkeypatch.setattr(sequencer.brevo, "send_email", fake_send)

    result = await sequencer._send_email_step_async(str(lead.id), str(followup.id))
    assert result["status"] == "sent"
    assert '<a href="https://grantmind.pro"' in captured["html_body"]
    assert "&lt;a" not in captured["html_body"]  # NOT escaped


async def test_send_email_step_never_double_sends(db_session, monkeypatch):
    """Two dispatches of the same (lead, node) BEFORE the SENT row is recorded
    — the redelivery / concurrent-dispatch / task-retry race — must deliver the
    email exactly ONCE.  The atomic Redis claim blocks the second send even
    though the lifetime DB guard can't yet see a SENT row."""
    campaign = await _make_campaign(db_session)
    campaign.min_delay_seconds = 0  # isolate the claim from the min-delay gate
    _, entry, wait_node, followup = await _build_three_node_sequence(db_session, campaign)
    lead = await _make_lead(db_session, campaign)
    lead.send_status = SendStatus.SENT
    await db_session.commit()

    state = LeadSequenceState(
        lead_id=lead.id, sequence_id=followup.sequence_id,
        current_node_id=followup.id, status=LeadSequenceStatus.ACTIVE,
        next_run_at=_now(), entered_current_at=_now(),
    )
    db_session.add(state)
    await db_session.commit()

    calls = {"n": 0}
    async def fake_send(**kwargs):
        calls["n"] += 1
        return f"mid-{calls['n']}"
    monkeypatch.setattr(sequencer.brevo, "send_email", fake_send)

    # Deliberately do NOT record the execution row between the two calls —
    # that is exactly the window the claim has to defend.
    r1 = await sequencer._send_email_step_async(str(lead.id), str(followup.id))
    r2 = await sequencer._send_email_step_async(str(lead.id), str(followup.id))

    assert r1["status"] == "sent"
    assert r2["status"] == "skipped"
    assert "duplicate" in (r2.get("error") or "")
    assert calls["n"] == 1  # the email left exactly once


# --------------------------------------------------------------------------
# Follow-up email step honours pre-send gates (paused / scheduled / rate-limited)
# --------------------------------------------------------------------------


async def test_followup_step_defers_when_campaign_paused(db_session, monkeypatch):
    """A paused campaign must NOT fire follow-up emails.  The step returns
    `deferred`, the cursor stays on the followup node, and next_run_at is
    pushed out by 5 minutes (default re-check interval for paused)."""
    campaign = await _make_campaign(db_session)
    campaign.status = CampaignStatus.PAUSED
    _, entry, wait_node, followup = await _build_three_node_sequence(db_session, campaign)
    lead = await _make_lead(db_session, campaign)
    lead.send_status = SendStatus.SENT
    await db_session.commit()

    state = LeadSequenceState(
        lead_id=lead.id, sequence_id=followup.sequence_id,
        current_node_id=followup.id, status=LeadSequenceStatus.ACTIVE,
        next_run_at=_now(), entered_current_at=_now(),
    )
    db_session.add(state)
    await db_session.commit()

    called = {"n": 0}
    async def explode(**kwargs):
        called["n"] += 1
        raise AssertionError("Brevo should NOT be called when paused")
    monkeypatch.setattr(sequencer.brevo, "send_email", explode)

    result = await sequencer._send_email_step_async(str(lead.id), str(followup.id))
    assert result["status"] == "deferred"
    assert result["reason"] == "paused"
    assert called["n"] == 0

    await sequencer._record_execution_and_advance(lead.id, followup.id, result)
    await db_session.refresh(state)
    assert state.current_node_id == followup.id  # parked, not advanced
    assert state.status == LeadSequenceStatus.ACTIVE
    assert state.next_run_at is not None and state.next_run_at > _now()

    # A deferral is "retry later", not an attempt — it must NOT write a
    # lead_step_executions row (otherwise a parked lead re-checking every few
    # minutes looks like repeated "skipped" steps in the Activity tab and
    # bloats the table without bound).
    exec_rows = (await db_session.execute(
        select(LeadStepExecution).where(LeadStepExecution.lead_id == lead.id)
    )).scalars().all()
    assert exec_rows == []


async def test_followup_step_defers_outside_schedule_window(db_session, monkeypatch):
    """Out-of-window step returns deferred with retry_at; cursor pinned and
    rescheduled to the window open time."""
    from datetime import time as _time
    campaign = await _make_campaign(db_session)
    # Only allow Monday (weekday 0); force-close the window for "now" by
    # giving a narrow time slot the test almost certainly isn't in.
    campaign.schedule_days = [0]
    campaign.schedule_time_start = _time(2, 0)
    campaign.schedule_time_end = _time(2, 30)
    _, entry, wait_node, followup = await _build_three_node_sequence(db_session, campaign)
    lead = await _make_lead(db_session, campaign)
    lead.send_status = SendStatus.SENT
    await db_session.commit()

    state = LeadSequenceState(
        lead_id=lead.id, sequence_id=followup.sequence_id,
        current_node_id=followup.id, status=LeadSequenceStatus.ACTIVE,
        next_run_at=_now(), entered_current_at=_now(),
    )
    db_session.add(state)
    await db_session.commit()

    async def explode(**kwargs):
        raise AssertionError("Brevo should NOT be called outside the window")
    monkeypatch.setattr(sequencer.brevo, "send_email", explode)

    result = await sequencer._send_email_step_async(str(lead.id), str(followup.id))
    assert result["status"] == "deferred"
    assert result["reason"] == "scheduled"
    assert "retry_at" in result

    await sequencer._record_execution_and_advance(lead.id, followup.id, result)
    await db_session.refresh(state)
    assert state.current_node_id == followup.id
    # Should be in the future (the next open window).
    assert state.next_run_at is not None and state.next_run_at > _now()


async def test_followup_step_halts_on_suppression(db_session, monkeypatch):
    """Suppression is permanent — the lead's WHOLE sequence is moot, so
    the state HALTS outright with a clear reason (it used to advance
    node-by-node, writing one skip row per remaining step)."""
    from app.models import Suppression, SuppressionReason
    campaign = await _make_campaign(db_session)
    _, entry, wait_node, followup = await _build_three_node_sequence(db_session, campaign)
    lead = await _make_lead(db_session, campaign)
    lead.send_status = SendStatus.SENT
    db_session.add(Suppression(email=lead.email, reason=SuppressionReason.UNSUBSCRIBED))
    await db_session.commit()

    state = LeadSequenceState(
        lead_id=lead.id, sequence_id=followup.sequence_id,
        current_node_id=followup.id, status=LeadSequenceStatus.ACTIVE,
        next_run_at=_now(), entered_current_at=_now(),
    )
    db_session.add(state)
    await db_session.commit()

    async def explode(**kwargs):
        raise AssertionError("Brevo should NOT be called for suppressed lead")
    monkeypatch.setattr(sequencer.brevo, "send_email", explode)

    result = await sequencer._send_email_step_async(str(lead.id), str(followup.id))
    assert result["status"] == "suppressed"

    await sequencer._record_execution_and_advance(lead.id, followup.id, result)
    await db_session.refresh(state)
    # Suppression halts the sequence outright with a clear reason.
    assert state.status == LeadSequenceStatus.HALTED
    assert "suppression" in (state.halt_reason or "").lower()
    assert state.next_run_at is None


# --------------------------------------------------------------------------
# Reply-in-thread node (email_reply)
# --------------------------------------------------------------------------


async def _make_reply_node(db_session, campaign, **cfg):
    seq = Sequence(campaign_id=campaign.id, is_published=True)
    db_session.add(seq)
    await db_session.flush()
    node = SequenceNode(
        sequence_id=seq.id, kind=SequenceNodeKind.EMAIL_REPLY,
        config=cfg, is_entry=False,
    )
    db_session.add(node)
    await db_session.commit()
    await db_session.refresh(node)
    return node


async def _reply_lead(db_session, campaign, *, message_id="<orig@mail>", subject="Quick question"):
    lead = await _make_lead(db_session, campaign)
    lead.send_status = SendStatus.SENT
    lead.brevo_message_id = message_id
    lead.composed_subject = subject
    lead.composed_body = "Original pitch body."
    await db_session.commit()
    await db_session.refresh(lead)
    return lead


async def test_email_reply_manual_threads_to_original(db_session, monkeypatch):
    campaign = await _make_campaign(db_session)
    node = await _make_reply_node(db_session, campaign, body_template="Just following up, {{first_name}}.")
    lead = await _reply_lead(db_session, campaign)

    captured = {}
    async def fake_send(**kwargs):
        captured.update(kwargs)
        return "<reply@mail>"
    monkeypatch.setattr(sequencer.brevo, "send_email", fake_send)

    result = await sequencer._send_email_step_async(str(lead.id), str(node.id))
    assert result["status"] == "sent"
    assert captured["subject"] == "Re: Quick question"
    assert captured["in_reply_to"] == "<orig@mail>"
    assert "Just following up, L." in captured["html_body"]


async def test_email_reply_does_not_double_prefix_re(db_session, monkeypatch):
    campaign = await _make_campaign(db_session)
    node = await _make_reply_node(db_session, campaign, body_template="Ping")
    lead = await _reply_lead(db_session, campaign, subject="Re: Already a reply")

    captured = {}
    async def fake_send(**kwargs):
        captured.update(kwargs)
        return "<r@mail>"
    monkeypatch.setattr(sequencer.brevo, "send_email", fake_send)

    await sequencer._send_email_step_async(str(lead.id), str(node.id))
    assert captured["subject"] == "Re: Already a reply"


async def test_email_reply_ai_uses_composer_and_threads(db_session, monkeypatch):
    campaign = await _make_campaign(db_session)
    node = await _make_reply_node(
        db_session, campaign, ai_compose=True, ai_prompt="mention the Q3 deadline",
    )
    lead = await _reply_lead(db_session, campaign)

    seen = {}
    async def fake_reply(**kwargs):
        seen.update(kwargs)
        return "AI-written nudge."
    monkeypatch.setattr(sequencer, "generate_followup_reply", fake_reply)

    captured = {}
    async def fake_send(**kwargs):
        captured.update(kwargs)
        return "<reply@mail>"
    monkeypatch.setattr(sequencer.brevo, "send_email", fake_send)

    result = await sequencer._send_email_step_async(str(lead.id), str(node.id))
    assert result["status"] == "sent"
    assert seen["idea"] == "mention the Q3 deadline"
    assert seen["original_subject"] == "Quick question"
    assert "AI-written nudge." in captured["html_body"]
    assert captured["in_reply_to"] == "<orig@mail>"


async def test_email_reply_skips_when_no_prior_email(db_session, monkeypatch):
    campaign = await _make_campaign(db_session)
    node = await _make_reply_node(db_session, campaign, body_template="Ping")
    lead = await _make_lead(db_session, campaign)
    lead.brevo_message_id = None          # no original email was sent
    await db_session.commit()

    async def explode(**kwargs):
        raise AssertionError("must not send a reply with no prior email")
    monkeypatch.setattr(sequencer.brevo, "send_email", explode)

    result = await sequencer._send_email_step_async(str(lead.id), str(node.id))
    assert result["status"] == "skipped"
    assert "no previous email" in result["error"]


async def test_email_reply_manual_without_body_is_misconfigured(db_session, monkeypatch):
    campaign = await _make_campaign(db_session)
    node = await _make_reply_node(db_session, campaign)   # no body_template, no ai_compose
    lead = await _reply_lead(db_session, campaign)

    async def explode(**kwargs):
        raise AssertionError("must not send a misconfigured reply")
    monkeypatch.setattr(sequencer.brevo, "send_email", explode)

    result = await sequencer._send_email_step_async(str(lead.id), str(node.id))
    assert result["status"] == "misconfigured"


# --------------------------------------------------------------------------
# replace_graph preserves node ids; publish re-queues finished leads
# --------------------------------------------------------------------------


def _n(client_id, kind, *, entry=False, **cfg):
    return {"client_id": client_id, "kind": kind, "is_entry": entry, "config": cfg}


def _e(a, b, condition=None):
    return {"from_client_id": a, "to_client_id": b, "condition": condition or {"op": "always"}}


async def test_replace_graph_preserves_unchanged_node_ids(db_session):
    campaign = await _make_campaign(db_session)
    seq = await ensure_default_sequence(db_session, campaign)

    m1 = await replace_graph(db_session, seq,
        nodes=[
            _n("e", "email", entry=True),
            _n("f", "email", subject_template="s", body_template="b"),
        ],
        edges=[_e("e", "f")],
    )
    await db_session.commit()
    entry_id, follow_id = m1["e"], m1["f"]

    # Re-save using the REAL ids as client_ids + add a new node.
    m2 = await replace_graph(db_session, seq,
        nodes=[
            _n(str(entry_id), "email", entry=True),
            _n(str(follow_id), "email", subject_template="s", body_template="b"),
            _n("new", "email", subject_template="s2", body_template="b2"),
        ],
        edges=[_e(str(entry_id), str(follow_id)), _e(str(follow_id), "new")],
    )
    await db_session.commit()

    assert m2[str(entry_id)] == entry_id      # preserved
    assert m2[str(follow_id)] == follow_id    # preserved
    assert m2["new"] not in (entry_id, follow_id)   # genuinely new id

    # Old nodes are still live (not soft-deleted).
    for nid in (entry_id, follow_id):
        node = await db_session.get(SequenceNode, nid)
        assert node.deleted_at is None


async def test_replace_graph_soft_deletes_removed_node(db_session):
    campaign = await _make_campaign(db_session)
    seq = await ensure_default_sequence(db_session, campaign)
    m1 = await replace_graph(db_session, seq,
        nodes=[_n("e", "email", entry=True), _n("w", "wait", duration_minutes=60)],
        edges=[_e("e", "w")],
    )
    await db_session.commit()
    wait_id = m1["w"]

    # Re-save without the wait node → it gets soft-deleted, entry preserved.
    m2 = await replace_graph(db_session, seq,
        nodes=[_n(str(m1["e"]), "email", entry=True)], edges=[],
    )
    await db_session.commit()
    assert m2[str(m1["e"])] == m1["e"]
    gone = await db_session.get(SequenceNode, wait_id)
    assert gone.deleted_at is not None


async def test_reenroll_for_new_nodes_requeues_completed_lead(db_session):
    campaign = await _make_campaign(db_session)
    seq = await ensure_default_sequence(db_session, campaign)
    m = await replace_graph(db_session, seq,
        nodes=[
            _n("e", "email", entry=True),
            _n("f", "email", subject_template="s", body_template="b"),
        ],
        edges=[_e("e", "f")],
    )
    await db_session.commit()
    follow_id = m["f"]

    # A lead that COMPLETED the sequence (executed the followup).
    lead = await _make_lead(db_session, campaign)
    lead.send_status = SendStatus.SENT
    state = LeadSequenceState(
        lead_id=lead.id, sequence_id=seq.id, current_node_id=None,
        status=LeadSequenceStatus.COMPLETED, entered_current_at=_now(),
    )
    db_session.add(state)
    db_session.add(LeadStepExecution(
        lead_id=lead.id, node_id=follow_id, result=LeadStepResult.SENT,
        attempted_at=_now(),
    ))
    await db_session.commit()

    # Add a new node after the followup (ids preserved).
    m2 = await replace_graph(db_session, seq,
        nodes=[
            _n(str(m["e"]), "email", entry=True),
            _n(str(follow_id), "email", subject_template="s", body_template="b"),
            _n("new", "email", subject_template="s2", body_template="b2"),
        ],
        edges=[_e(str(m["e"]), str(follow_id)), _e(str(follow_id), "new")],
    )
    await db_session.commit()

    requeued = await reenroll_for_new_nodes(db_session, seq)
    await db_session.commit()
    assert requeued == 1

    await db_session.refresh(state)
    assert state.status == LeadSequenceStatus.ACTIVE
    # Reset to its last still-live executed node (the followup), from which
    # the sequencer skips-and-advances into the new node.
    assert state.current_node_id == follow_id
    assert state.next_run_at is not None


async def test_reenroll_skips_lead_that_did_every_node(db_session):
    campaign = await _make_campaign(db_session)
    seq = await ensure_default_sequence(db_session, campaign)
    m = await replace_graph(db_session, seq,
        nodes=[
            _n("e", "email", entry=True),
            _n("f", "email", subject_template="s", body_template="b"),
        ],
        edges=[_e("e", "f")],
    )
    await db_session.commit()
    follow_id = m["f"]

    lead = await _make_lead(db_session, campaign)
    lead.send_status = SendStatus.SENT
    state = LeadSequenceState(
        lead_id=lead.id, sequence_id=seq.id, current_node_id=None,
        status=LeadSequenceStatus.COMPLETED, entered_current_at=_now(),
    )
    db_session.add(state)
    db_session.add(LeadStepExecution(
        lead_id=lead.id, node_id=follow_id, result=LeadStepResult.SENT,
        attempted_at=_now(),
    ))
    await db_session.commit()

    # Re-publish the SAME graph (no new action node) → nothing to re-run.
    requeued = await reenroll_for_new_nodes(db_session, seq)
    assert requeued == 0
    await db_session.refresh(state)
    assert state.status == LeadSequenceStatus.COMPLETED


async def test_reenroll_preserves_entered_current_at_clock(db_session):
    """Re-queuing must NOT reset entered_current_at to now — otherwise a
    time-based edge wait (e.g. "reply 2 days after the first email")
    restarts on every publish and the lead never crosses the gate.  The
    entry-email lead (no live execution) keeps its original entry time."""
    campaign = await _make_campaign(db_session)
    seq = await ensure_default_sequence(db_session, campaign)
    m = await replace_graph(db_session, seq,
        nodes=[_n("e", "email", entry=True)],
        edges=[],
    )
    await db_session.commit()

    # A lead that finished the (1-node) sequence days ago — entry email sent,
    # no sequencer execution rows (legacy first-email path writes none).
    long_ago = _now() - timedelta(days=5)
    lead = await _make_lead(db_session, campaign)
    lead.send_status = SendStatus.SENT
    state = LeadSequenceState(
        lead_id=lead.id, sequence_id=seq.id, current_node_id=None,
        status=LeadSequenceStatus.COMPLETED, entered_current_at=long_ago,
    )
    db_session.add(state)
    await db_session.commit()

    # Now add a reply node after the entry email and re-publish.
    await replace_graph(db_session, seq,
        nodes=[
            _n(str(m["e"]), "email", entry=True),
            _n("r", "email_reply", ai_compose=True),
        ],
        edges=[_e(str(m["e"]), "r", condition={"op": "days_since_entered_node", "gte": 2})],
    )
    await db_session.commit()

    requeued = await reenroll_for_new_nodes(db_session, seq)
    await db_session.commit()
    assert requeued == 1

    await db_session.refresh(state)
    assert state.status == LeadSequenceStatus.ACTIVE
    # The clock is preserved at the original entry time (5 days ago), NOT
    # reset to now — so the 2-day reply gate is already satisfied.
    assert abs((state.entered_current_at - long_ago).total_seconds()) < 5
    assert (_now() - state.entered_current_at) > timedelta(days=2)
