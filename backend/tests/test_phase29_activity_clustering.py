"""Tests for the Activity tab's recent_sequence_steps clustering.

Background: a LinkedIn step can produce a chain of skip-and-retry
``lead_step_executions`` rows for the same (lead, node) pair before
eventually succeeding.  The raw audit log keeps every row, but the
``/campaigns/{id}/activity`` endpoint dedupes consecutive same-(lead,
node) rows into a single clustered event with an ``attempt_count`` so
the UI doesn't show 10 rows that all say "Connect skipped daily cap
reached".  Tests here lock that contract.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from sqlalchemy import select

from app.models import (
    Campaign,
    Lead,
    LeadStepExecution,
    LeadStepResult,
    Sequence,
    SequenceEdge,
    SequenceNode,
    SequenceNodeKind,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _make_campaign_with_node(db_session, node_kind: SequenceNodeKind) -> tuple[Campaign, SequenceNode]:
    c = Campaign(
        name="P29", goal="g", tone="t", sender_name="s", sender_email="s@x.com",
        sample_count=1, schedule_days=[0, 1, 2, 3, 4],
        schedule_time_start=time(9, 0), schedule_time_end=time(17, 0),
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    seq = Sequence(campaign_id=c.id, is_published=True)
    db_session.add(seq)
    await db_session.flush()
    entry = SequenceNode(
        sequence_id=seq.id, kind=SequenceNodeKind.EMAIL,
        config={"use_campaign_compose": True}, is_entry=True,
    )
    node = SequenceNode(
        sequence_id=seq.id, kind=node_kind, config={}, is_entry=False,
    )
    db_session.add_all([entry, node])
    await db_session.flush()
    db_session.add(
        SequenceEdge(sequence_id=seq.id, from_node_id=entry.id,
                     to_node_id=node.id, condition={"op": "always"}),
    )
    await db_session.commit()
    await db_session.refresh(node)
    return c, node


async def _make_lead(db_session, campaign: Campaign, email: str) -> Lead:
    lead = Lead(campaign_id=campaign.id, email=email, first_name="A", last_name="B")
    db_session.add(lead)
    await db_session.commit()
    await db_session.refresh(lead)
    return lead


async def _add_exec(db_session, lead: Lead, node: SequenceNode,
                    result: LeadStepResult, attempted_at: datetime,
                    error: str | None = None) -> LeadStepExecution:
    ex = LeadStepExecution(
        lead_id=lead.id, node_id=node.id,
        result=result, attempted_at=attempted_at, error=error,
    )
    db_session.add(ex)
    await db_session.commit()
    return ex


async def test_multiple_attempts_collapse_to_one_row(client, db_session):
    """Five skipped retries on the same (lead, node) → one row with
    attempt_count=5.  The row's result/error reflect the latest attempt,
    earliest_attempted_at is set."""
    c, node = await _make_campaign_with_node(db_session, SequenceNodeKind.LINKEDIN_CONNECT)
    lead = await _make_lead(db_session, c, "lead@x.com")
    base = _now()
    for i in range(5):
        await _add_exec(
            db_session, lead, node, LeadStepResult.SKIPPED,
            attempted_at=base - timedelta(minutes=5 - i),  # i=0 oldest, i=4 newest
            error=f"daily connect cap reached (40)",
        )

    resp = await client.get(f"/campaigns/{c.id}/activity")
    assert resp.status_code == 200
    body = resp.json()
    steps = body["recent_sequence_steps"]
    assert len(steps) == 1
    s = steps[0]
    assert s["attempt_count"] == 5
    assert s["result"] == "skipped"
    assert "daily connect" in (s["error"] or "")
    # Latest attempt's timestamp
    latest = datetime.fromisoformat(s["attempted_at"])
    earliest = datetime.fromisoformat(s["earliest_attempted_at"])
    assert latest > earliest
    assert (latest - earliest) >= timedelta(minutes=4)


async def test_single_attempt_has_no_earliest(client, db_session):
    """attempt_count=1 → earliest_attempted_at omitted (null in JSON).
    Lets the UI know not to show the cluster badge."""
    c, node = await _make_campaign_with_node(db_session, SequenceNodeKind.LINKEDIN_VIEW_PROFILE)
    lead = await _make_lead(db_session, c, "solo@x.com")
    await _add_exec(db_session, lead, node, LeadStepResult.SENT, attempted_at=_now())

    resp = await client.get(f"/campaigns/{c.id}/activity")
    body = resp.json()
    steps = body["recent_sequence_steps"]
    assert len(steps) == 1
    assert steps[0]["attempt_count"] == 1
    assert steps[0]["earliest_attempted_at"] is None


async def test_clusters_split_by_lead(client, db_session):
    """Two leads each with multiple attempts on the same node → two
    clusters, not one."""
    c, node = await _make_campaign_with_node(db_session, SequenceNodeKind.LINKEDIN_CONNECT)
    a = await _make_lead(db_session, c, "a@x.com")
    b = await _make_lead(db_session, c, "b@x.com")
    base = _now()
    for lead in (a, b):
        for i in range(3):
            await _add_exec(
                db_session, lead, node, LeadStepResult.SKIPPED,
                attempted_at=base - timedelta(minutes=i),
                error="daily connect cap reached",
            )

    resp = await client.get(f"/campaigns/{c.id}/activity")
    steps = resp.json()["recent_sequence_steps"]
    assert len(steps) == 2
    by_email = {s["email"]: s for s in steps}
    assert by_email["a@x.com"]["attempt_count"] == 3
    assert by_email["b@x.com"]["attempt_count"] == 3


async def test_clusters_split_by_node(client, db_session):
    """Same lead, different node kinds → two clusters."""
    c, view_node = await _make_campaign_with_node(db_session, SequenceNodeKind.LINKEDIN_VIEW_PROFILE)
    # Add a second node to the same sequence
    seq = (await db_session.execute(select(Sequence).where(Sequence.campaign_id == c.id))).scalar_one()
    connect_node = SequenceNode(
        sequence_id=seq.id, kind=SequenceNodeKind.LINKEDIN_CONNECT, config={}, is_entry=False,
    )
    db_session.add(connect_node)
    await db_session.commit()
    await db_session.refresh(connect_node)

    lead = await _make_lead(db_session, c, "lead@x.com")
    base = _now()
    # 2 attempts on view, 3 on connect
    for i in range(2):
        await _add_exec(db_session, lead, view_node, LeadStepResult.SENT,
                        attempted_at=base - timedelta(minutes=10 + i))
    for i in range(3):
        await _add_exec(db_session, lead, connect_node, LeadStepResult.SKIPPED,
                        attempted_at=base - timedelta(minutes=i),
                        error="daily connect cap reached")

    resp = await client.get(f"/campaigns/{c.id}/activity")
    steps = resp.json()["recent_sequence_steps"]
    assert len(steps) == 2
    by_kind = {s["node_kind"]: s for s in steps}
    assert by_kind["linkedin_view_profile"]["attempt_count"] == 2
    assert by_kind["linkedin_connect"]["attempt_count"] == 3


async def test_clustering_preserves_chronological_order(client, db_session):
    """The cluster list is ordered by latest attempt, descending — most
    recently active cluster first."""
    c, node = await _make_campaign_with_node(db_session, SequenceNodeKind.LINKEDIN_CONNECT)
    older = await _make_lead(db_session, c, "older@x.com")
    newer = await _make_lead(db_session, c, "newer@x.com")
    base = _now()
    # older's latest attempt is 10 min ago
    for i in range(2):
        await _add_exec(db_session, older, node, LeadStepResult.SKIPPED,
                        attempted_at=base - timedelta(minutes=10 + i),
                        error="rate")
    # newer's latest attempt is 1 min ago
    for i in range(2):
        await _add_exec(db_session, newer, node, LeadStepResult.SKIPPED,
                        attempted_at=base - timedelta(minutes=1 + i),
                        error="rate")

    resp = await client.get(f"/campaigns/{c.id}/activity")
    steps = resp.json()["recent_sequence_steps"]
    assert steps[0]["email"] == "newer@x.com"  # most-recent cluster first
    assert steps[1]["email"] == "older@x.com"


async def test_cluster_window_dedupes_across_100_row_pull(client, db_session):
    """The backend pulls up to 100 raw rows and clusters down to 30
    displayed.  A single lead+node with 100 retry rows must collapse to
    one row in the response."""
    c, node = await _make_campaign_with_node(db_session, SequenceNodeKind.LINKEDIN_CONNECT)
    lead = await _make_lead(db_session, c, "many@x.com")
    base = _now()
    for i in range(100):
        await _add_exec(db_session, lead, node, LeadStepResult.SKIPPED,
                        attempted_at=base - timedelta(seconds=i * 30),
                        error="rate")

    resp = await client.get(f"/campaigns/{c.id}/activity")
    steps = resp.json()["recent_sequence_steps"]
    assert len(steps) == 1
    assert steps[0]["attempt_count"] == 100
