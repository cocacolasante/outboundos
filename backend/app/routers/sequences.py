from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import (
    Campaign,
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
from app.schemas.sequence import (
    NodeAnalytics,
    PublishResponse,
    SequenceAnalyticsResponse,
    SequenceResponse,
    SequenceUpdate,
    ValidateResponse,
)
from app.services.sequence_service import (
    ensure_default_sequence,
    reenroll_for_new_nodes,
    replace_graph,
    validate_graph,
)

router = APIRouter(tags=["sequences"])


async def _get_campaign_or_404(db: AsyncSession, campaign_id: uuid.UUID) -> Campaign:
    c = await db.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return c


async def _load_response(db: AsyncSession, sequence: Sequence) -> SequenceResponse:
    nodes = (await db.execute(
        select(SequenceNode)
        .where(
            SequenceNode.sequence_id == sequence.id,
            SequenceNode.deleted_at.is_(None),
        )
        .order_by(SequenceNode.created_at.asc())
    )).scalars().all()
    edges = (await db.execute(
        select(SequenceEdge)
        .where(SequenceEdge.sequence_id == sequence.id)
        .order_by(SequenceEdge.priority.asc(), SequenceEdge.created_at.asc())
    )).scalars().all()
    return SequenceResponse(
        id=sequence.id,
        campaign_id=sequence.campaign_id,
        is_published=sequence.is_published,
        nodes=[
            {
                "id": n.id,
                "kind": n.kind,
                "config": n.config or {},
                "position_x": n.position_x,
                "position_y": n.position_y,
                "is_entry": n.is_entry,
            }
            for n in nodes
        ],
        edges=[
            {
                "id": e.id,
                "from_node_id": e.from_node_id,
                "to_node_id": e.to_node_id,
                "condition": e.condition or {"op": "always"},
                "priority": e.priority,
            }
            for e in edges
        ],
        created_at=sequence.created_at,
        updated_at=sequence.updated_at,
    )


@router.get("/campaigns/{campaign_id}/sequence", response_model=SequenceResponse)
async def get_sequence(
    campaign_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> SequenceResponse:
    campaign = await _get_campaign_or_404(db, campaign_id)
    seq = await ensure_default_sequence(db, campaign)
    await db.commit()
    await db.refresh(seq)
    return await _load_response(db, seq)


@router.put("/campaigns/{campaign_id}/sequence", response_model=SequenceResponse)
async def update_sequence(
    campaign_id: uuid.UUID,
    payload: SequenceUpdate,
    db: AsyncSession = Depends(get_db),
) -> SequenceResponse:
    campaign = await _get_campaign_or_404(db, campaign_id)
    seq = await ensure_default_sequence(db, campaign)

    nodes_dicts = [n.model_dump() for n in payload.nodes]
    edges_dicts = [e.model_dump() for e in payload.edges]

    # Structural validation always runs (catches bad client_id refs, bad
    # condition shapes). Publish-only validation (M1 kind restriction,
    # reachability) runs in /publish so the user can save drafts.
    await replace_graph(db, seq, nodes_dicts, edges_dicts)
    await db.commit()
    await db.refresh(seq)
    return await _load_response(db, seq)


@router.post("/campaigns/{campaign_id}/sequence/validate", response_model=ValidateResponse)
async def validate_sequence(
    campaign_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> ValidateResponse:
    """Run the publish-time checks against the current saved graph. The UI
    calls this to surface errors before the user hits Publish.
    """
    campaign = await _get_campaign_or_404(db, campaign_id)
    seq = await ensure_default_sequence(db, campaign)
    await db.commit()

    nodes = (await db.execute(
        select(SequenceNode).where(
            SequenceNode.sequence_id == seq.id,
            SequenceNode.deleted_at.is_(None),
        )
    )).scalars().all()
    edges = (await db.execute(
        select(SequenceEdge).where(SequenceEdge.sequence_id == seq.id)
    )).scalars().all()

    # Re-shape into the dict form validate_graph expects (client_id == str(id)).
    nodes_dicts = [
        {
            "client_id": str(n.id),
            "kind": n.kind.value if hasattr(n.kind, "value") else n.kind,
            "is_entry": n.is_entry,
            "config": n.config or {},
        }
        for n in nodes
    ]
    edges_dicts = [
        {
            "from_client_id": str(e.from_node_id),
            "to_client_id": str(e.to_node_id) if e.to_node_id else None,
            "condition": e.condition or {"op": "always"},
        }
        for e in edges
    ]
    errors = validate_graph(nodes_dicts, edges_dicts)
    return ValidateResponse(ok=len(errors) == 0, errors=errors)


@router.post("/campaigns/{campaign_id}/sequence/publish", response_model=PublishResponse)
async def publish_sequence(
    campaign_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> PublishResponse:
    """Validate + flip is_published=True. Already-active lead_sequence_state
    rows keep using whichever node graph they were on at the moment they
    advanced; the new published graph applies from the next state transition.
    """
    campaign = await _get_campaign_or_404(db, campaign_id)
    seq = await ensure_default_sequence(db, campaign)

    nodes = (await db.execute(
        select(SequenceNode).where(
            SequenceNode.sequence_id == seq.id,
            SequenceNode.deleted_at.is_(None),
        )
    )).scalars().all()
    edges = (await db.execute(
        select(SequenceEdge).where(SequenceEdge.sequence_id == seq.id)
    )).scalars().all()

    nodes_dicts = [
        {
            "client_id": str(n.id),
            "kind": n.kind.value if hasattr(n.kind, "value") else n.kind,
            "is_entry": n.is_entry,
            "config": n.config or {},
        }
        for n in nodes
    ]
    edges_dicts = [
        {
            "from_client_id": str(e.from_node_id),
            "to_client_id": str(e.to_node_id) if e.to_node_id else None,
            "condition": e.condition or {"op": "always"},
        }
        for e in edges
    ]
    errors = validate_graph(nodes_dicts, edges_dicts)
    if errors:
        return PublishResponse(ok=False, is_published=seq.is_published, errors=errors)

    seq.is_published = True
    # Re-queue already-finished leads for any newly-added node.  Safe because
    # replace_graph preserves node ids → the send-once idempotency skips
    # everything a lead already did; only the new node(s) fire.
    requeued = await reenroll_for_new_nodes(db, seq)
    await db.commit()
    return PublishResponse(
        ok=True, is_published=True, errors=[], reenrolled_for_new_nodes=requeued,
    )


# --------------------------------------------------------------------------
# Analytics (M5)
# --------------------------------------------------------------------------


def _funnel_order(
    node_ids: list[uuid.UUID],
    entry_node: SequenceNode | None,
    edges: list[SequenceEdge],
) -> list[uuid.UUID]:
    """Order node ids the way leads flow through the sequence: entry node
    first, then breadth-first over outgoing edges (already priority-sorted).
    Nodes unreachable from the entry are appended afterwards in their
    original (creation) order so nothing is dropped from the report."""
    if entry_node is None:
        return list(node_ids)
    adjacency: dict[uuid.UUID, list[uuid.UUID]] = {}
    for e in edges:
        if e.to_node_id is not None:
            adjacency.setdefault(e.from_node_id, []).append(e.to_node_id)

    ordered: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    queue: list[uuid.UUID] = [entry_node.id]
    while queue:
        nid = queue.pop(0)
        if nid in seen:
            continue
        seen.add(nid)
        ordered.append(nid)
        queue.extend(adjacency.get(nid, []))
    # Trailing: anything not reachable from entry, in creation order.
    for nid in node_ids:
        if nid not in seen:
            ordered.append(nid)
    return ordered


@router.get(
    "/campaigns/{campaign_id}/sequence/analytics",
    response_model=SequenceAnalyticsResponse,
)
async def get_sequence_analytics(
    campaign_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> SequenceAnalyticsResponse:
    """Per-node funnel counts derived from lead_step_executions +
    lead_sequence_states. Soft-deleted nodes are excluded — analytics for
    retired topology aren't currently exposed (we'd need a "show history"
    toggle, deferred).
    """
    campaign = await _get_campaign_or_404(db, campaign_id)
    seq = await ensure_default_sequence(db, campaign)
    await db.commit()

    # Live nodes (the current topology).
    nodes = (await db.execute(
        select(SequenceNode).where(
            SequenceNode.sequence_id == seq.id,
            SequenceNode.deleted_at.is_(None),
        )
    )).scalars().all()
    node_ids = [n.id for n in nodes]
    live = {n.id for n in nodes}
    edges = [
        e for e in (await db.execute(
            select(SequenceEdge)
            .where(SequenceEdge.sequence_id == seq.id)
            .order_by(SequenceEdge.priority.asc())
        )).scalars().all()
        # Only edges between live nodes matter for funnel ordering.
        if e.from_node_id in live and (e.to_node_id is None or e.to_node_id in live)
    ]

    # Aggregate step executions by (node, result).
    exec_rows = (await db.execute(
        select(
            LeadStepExecution.node_id,
            LeadStepExecution.result,
            func.count().label("c"),
        )
        .where(LeadStepExecution.node_id.in_(node_ids))
        .group_by(LeadStepExecution.node_id, LeadStepExecution.result)
    )).all() if node_ids else []
    by_node: dict[uuid.UUID, dict[str, int]] = {nid: {"attempted": 0, "sent": 0, "skipped": 0, "failed": 0} for nid in node_ids}
    for nid, result, c in exec_rows:
        bucket = by_node.setdefault(nid, {"attempted": 0, "sent": 0, "skipped": 0, "failed": 0})
        bucket["attempted"] += c
        if result == LeadStepResult.SENT:
            bucket["sent"] += c
        elif result == LeadStepResult.SKIPPED:
            bucket["skipped"] += c
        elif result == LeadStepResult.FAILED:
            bucket["failed"] += c

    # Active leads currently sitting on each node.
    currently_rows = (await db.execute(
        select(
            LeadSequenceState.current_node_id,
            func.count().label("c"),
        )
        .where(
            LeadSequenceState.sequence_id == seq.id,
            LeadSequenceState.status == LeadSequenceStatus.ACTIVE,
            LeadSequenceState.current_node_id.is_not(None),
        )
        .group_by(LeadSequenceState.current_node_id)
    )).all()
    currently: dict[uuid.UUID, int] = {nid: c for nid, c in currently_rows}

    # Of the leads sitting on a node, how many ALREADY have a SENT execution
    # for that very node — they'll skip-and-advance (the send-once guard), so
    # they will NOT be re-sent.  Surfacing this makes "481 here now" legible:
    # e.g. "248 already sent (will skip) · 233 awaiting first send".
    here_sent_rows = (await db.execute(
        select(
            LeadSequenceState.current_node_id,
            func.count(func.distinct(LeadSequenceState.lead_id)).label("c"),
        )
        .join(
            LeadStepExecution,
            and_(
                LeadStepExecution.lead_id == LeadSequenceState.lead_id,
                LeadStepExecution.node_id == LeadSequenceState.current_node_id,
                LeadStepExecution.result == LeadStepResult.SENT,
            ),
        )
        .where(
            LeadSequenceState.sequence_id == seq.id,
            LeadSequenceState.status == LeadSequenceStatus.ACTIVE,
            LeadSequenceState.current_node_id.is_not(None),
        )
        .group_by(LeadSequenceState.current_node_id)
    )).all()
    here_already_sent: dict[uuid.UUID, int] = {nid: c for nid, c in here_sent_rows}

    node_by_id = {n.id: n for n in nodes}
    entry_node = next((n for n in nodes if n.is_entry), None)

    # The legacy first email (compose -> send_lead) does NOT write
    # lead_step_executions — only the sequencer does.  So an entry EMAIL
    # node would otherwise show 0 sent here even though every first email
    # went out.  Fold the real first-email outcome (lead.send_status) into
    # that node so the funnel reflects it.  (EMAIL_REPLY can't be an entry
    # node; a LinkedIn/wait entry IS sequencer-driven, so its executions are
    # already correct and we leave it alone.)
    if entry_node is not None and entry_node.kind == SequenceNodeKind.EMAIL:
        send_rows = (await db.execute(
            select(Lead.send_status, func.count().label("c"))
            .where(Lead.campaign_id == campaign.id)
            .group_by(Lead.send_status)
        )).all()
        by_send: dict[SendStatus, int] = {s: c for s, c in send_rows}
        sent = by_send.get(SendStatus.SENT, 0)
        failed = by_send.get(SendStatus.FAILED, 0)
        bucket = by_node.setdefault(
            entry_node.id, {"attempted": 0, "sent": 0, "skipped": 0, "failed": 0}
        )
        bucket["sent"] = sent
        bucket["failed"] = failed
        bucket["attempted"] = sent + failed

    # Order the funnel the way the sequence flows: entry first, then BFS over
    # live edges (priority order), so the report reads top-to-bottom like the
    # builder.  Any node unreachable from the entry trails afterwards in
    # creation order so nothing is dropped.
    ordered_ids = _funnel_order(node_ids, entry_node, edges)

    per_node = [
        NodeAnalytics(
            node_id=nid,
            kind=node_by_id[nid].kind,
            title=(node_by_id[nid].config or {}).get("title") or None,
            is_entry=node_by_id[nid].is_entry,
            attempted=by_node[nid]["attempted"],
            sent=by_node[nid]["sent"],
            skipped=by_node[nid]["skipped"],
            failed=by_node[nid]["failed"],
            currently_here=currently.get(nid, 0),
            here_already_sent=here_already_sent.get(nid, 0),
        )
        for nid in ordered_ids
    ]

    # Overall status breakdown.
    status_rows = (await db.execute(
        select(LeadSequenceState.status, func.count().label("c"))
        .where(LeadSequenceState.sequence_id == seq.id)
        .group_by(LeadSequenceState.status)
    )).all()
    by_status: dict[LeadSequenceStatus, int] = {s: c for s, c in status_rows}

    return SequenceAnalyticsResponse(
        campaign_id=campaign.id,
        sequence_id=seq.id,
        total_leads=sum(by_status.values()),
        halted=by_status.get(LeadSequenceStatus.HALTED, 0),
        completed=by_status.get(LeadSequenceStatus.COMPLETED, 0),
        active=by_status.get(LeadSequenceStatus.ACTIVE, 0),
        pending=by_status.get(LeadSequenceStatus.PENDING, 0),
        per_node=per_node,
    )
