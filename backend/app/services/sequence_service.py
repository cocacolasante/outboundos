"""Sequence creation + lead enrollment helpers.

Used by:
  - campaigns router: auto-create a default sequence when a campaign is born
  - leads router (confirm_upload): enroll new leads into their campaign's sequence
  - sequences router: replace the graph wholesale on PUT
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Campaign,
    Lead,
    LeadSequenceState,
    LeadSequenceStatus,
    LeadStepExecution,
    Sequence,
    SequenceEdge,
    SequenceNode,
    SequenceNodeKind,
)

# Node kinds that perform an action AND write a lead_step_executions row
# (so a lead "having executed" them is detectable).  The legacy entry email
# is excluded — it's tracked via lead.send_status, not an execution row.
_EXECUTION_RECORDING_KINDS = {
    SequenceNodeKind.EMAIL_REPLY,
    SequenceNodeKind.LINKEDIN_VIEW_PROFILE,
    SequenceNodeKind.LINKEDIN_FOLLOW_PROFILE,
    SequenceNodeKind.LINKEDIN_REACT_POST,
    SequenceNodeKind.LINKEDIN_COMMENT_POST,
    SequenceNodeKind.LINKEDIN_CONNECT,
    SequenceNodeKind.LINKEDIN_DM,
    SequenceNodeKind.LINKEDIN_INMAIL,
    SequenceNodeKind.LINKEDIN_INVITE_TO_PAGE,
}
from app.services.sequence_conditions import detect_cycle, validate


def live_nodes_filter():
    """Reused filter expression for "not soft-deleted" nodes."""
    return SequenceNode.deleted_at.is_(None)


# --------------------------------------------------------------------------
# Default sequence
# --------------------------------------------------------------------------


DEFAULT_ENTRY_NODE_CONFIG: dict[str, Any] = {"use_campaign_compose": True}


async def ensure_default_sequence(db: AsyncSession, campaign: Campaign) -> Sequence:
    """Return the campaign's sequence, creating a default 1-node version if
    none exists. Idempotent.
    """
    existing = await db.scalar(
        select(Sequence).where(Sequence.campaign_id == campaign.id)
    )
    if existing is not None:
        # If the only nodes are soft-deleted (sequence stripped earlier),
        # re-seed an entry node so the campaign has a working default.
        live_entry = await db.scalar(
            select(SequenceNode).where(
                SequenceNode.sequence_id == existing.id,
                SequenceNode.is_entry.is_(True),
                live_nodes_filter(),
            )
        )
        if live_entry is None:
            db.add(SequenceNode(
                sequence_id=existing.id,
                kind=SequenceNodeKind.EMAIL,
                config=DEFAULT_ENTRY_NODE_CONFIG,
                position_x=0,
                position_y=0,
                is_entry=True,
            ))
            await db.flush()
        return existing

    seq = Sequence(campaign_id=campaign.id, is_published=True)
    db.add(seq)
    await db.flush()  # so seq.id is populated

    entry = SequenceNode(
        sequence_id=seq.id,
        kind=SequenceNodeKind.EMAIL,
        config=DEFAULT_ENTRY_NODE_CONFIG,
        position_x=0,
        position_y=0,
        is_entry=True,
    )
    db.add(entry)
    await db.flush()
    return seq


# --------------------------------------------------------------------------
# Lead enrollment
# --------------------------------------------------------------------------


async def enroll_leads(
    db: AsyncSession, campaign_id: uuid.UUID, lead_ids: list[uuid.UUID]
) -> int:
    """Create lead_sequence_states for the given leads, pointing at the
    campaign's published entry node. Skips leads that already have a state row.
    Returns the number of states created.
    """
    if not lead_ids:
        return 0

    seq = await db.scalar(
        select(Sequence).where(Sequence.campaign_id == campaign_id)
    )
    if seq is None:
        return 0
    entry = await db.scalar(
        select(SequenceNode)
        .where(
            SequenceNode.sequence_id == seq.id,
            SequenceNode.is_entry.is_(True),
            live_nodes_filter(),
        )
    )
    if entry is None:
        return 0

    existing_ids = set(
        r[0] for r in (await db.execute(
            select(LeadSequenceState.lead_id)
            .where(LeadSequenceState.lead_id.in_(lead_ids))
        )).all()
    )
    now = datetime.now(timezone.utc)
    created = 0
    for lid in lead_ids:
        if lid in existing_ids:
            continue
        db.add(LeadSequenceState(
            lead_id=lid,
            sequence_id=seq.id,
            current_node_id=entry.id,
            status=LeadSequenceStatus.ACTIVE,
            next_run_at=now,
            entered_current_at=now,
        ))
        created += 1
    return created


# --------------------------------------------------------------------------
# Entry-node introspection
# --------------------------------------------------------------------------


async def get_live_entry_node(
    db: AsyncSession, campaign_id: uuid.UUID
) -> SequenceNode | None:
    """Return the campaign's live (not soft-deleted) entry node, or None."""
    seq = await db.scalar(
        select(Sequence).where(Sequence.campaign_id == campaign_id)
    )
    if seq is None:
        return None
    return await db.scalar(
        select(SequenceNode).where(
            SequenceNode.sequence_id == seq.id,
            SequenceNode.is_entry.is_(True),
            live_nodes_filter(),
        )
    )


def is_legacy_first_email_node(node: SequenceNode | None) -> bool:
    """True when the entry node is the campaign's legacy compose-pipeline
    first email.  Every email entry node qualifies — the builder treats an
    entry email as "use the per-lead email Claude composed", which the
    legacy ``compose -> send_lead`` path delivers.  Any other entry kind
    (LinkedIn, wait, ...) means the sequencer drives the first step and no
    standalone first email is sent.

    When no entry node can be resolved (None) we default to True — the
    backward-compatible email behavior.  We only suppress the first email
    when we positively identify a non-email start node.
    """
    if node is None:
        return True
    return node.kind == SequenceNodeKind.EMAIL and node.is_entry


async def campaign_sends_legacy_first_email(
    db: AsyncSession, campaign_id: uuid.UUID
) -> bool:
    """Whether this campaign's first touch is the legacy composed email."""
    return is_legacy_first_email_node(await get_live_entry_node(db, campaign_id))


# --------------------------------------------------------------------------
# Whole-graph replace + publish validation
# --------------------------------------------------------------------------


# Kinds the current milestone allows in published sequences.
#   M1 — email + wait
#   M2 — added read-only LinkedIn warm-ups (view, follow, react)
#   M3 — added LinkedIn write actions (connect, dm, invite_to_page)
#   M4 — added inmail + comment_post
# The constant name stuck as PUBLISHABLE_KINDS_M1 for backwards-compat with
# imports + tests; treat it as PUBLISHABLE_KINDS_CURRENT.
PUBLISHABLE_KINDS_M1 = {
    SequenceNodeKind.EMAIL,
    SequenceNodeKind.EMAIL_REPLY,
    SequenceNodeKind.WAIT,
    SequenceNodeKind.LINKEDIN_VIEW_PROFILE,
    SequenceNodeKind.LINKEDIN_FOLLOW_PROFILE,
    SequenceNodeKind.LINKEDIN_REACT_POST,
    SequenceNodeKind.LINKEDIN_CONNECT,
    SequenceNodeKind.LINKEDIN_DM,
    # LINKEDIN_INVITE_TO_PAGE excluded: HAR-verified Voyager shape is in
    # ``unipile_impl.invite_to_page`` but Unipile's /api/v1/linkedin
    # passthrough whitelist doesn't include
    # ``voyagerRelationshipsDashInvitations`` — every shape variant we
    # tried returns ``errors/malformed_request`` from their forwarder.
    # Re-enable once Unipile support allowlists the endpoint OR ships a
    # packaged "invite-to-follow-page" action.
    #
    # LINKEDIN_INMAIL excluded: the impl is correct (HAR-verified form-
    # encoded body with linkedin[inmail]=true) but Unipile returns 403
    # ``errors/resource_access_restricted`` until Sales Nav API access
    # is enabled on the workspace.  Re-enable once Unipile support
    # turns that on.
    SequenceNodeKind.LINKEDIN_COMMENT_POST,
}


def validate_graph(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> list[str]:
    """Structural + semantic validation, used by /validate and /publish.

    Returns [] on success or a list of human-readable error messages.

    Checks:
      - exactly one entry node
      - each edge's condition is a valid expression
      - graph is a DAG (no cycles)
      - all nodes are reachable from the entry
      - kinds outside PUBLISHABLE_KINDS_M1 are rejected

    Any publishable kind may be the entry node.  When the entry is an email
    node the legacy compose pipeline sends it as the campaign's first email;
    when it's anything else, the sequencer drives the first action and no
    standalone first email is sent (see ``campaign_sends_legacy_first_email``).
    """
    errors: list[str] = []

    entries = [n for n in nodes if n.get("is_entry")]
    if len(entries) != 1:
        errors.append(f"sequence must have exactly one entry node (found {len(entries)})")
        return errors
    entry = entries[0]

    for i, n in enumerate(nodes):
        kind = n.get("kind")
        try:
            kind_enum = SequenceNodeKind(kind)
        except ValueError:
            errors.append(f"node has unknown kind: {kind}")
            continue
        if kind_enum not in PUBLISHABLE_KINDS_M1:
            errors.append(
                f"node kind '{kind}' is not yet enabled "
                "(InMail + comment_post ship in M4)"
            )
            continue

        # Per-kind config checks. These mirror runtime checks but catch
        # mistakes BEFORE the campaign goes live.
        cfg = n.get("config") or {}
        label = f"nodes[{i}] ({kind})"
        if kind_enum == SequenceNodeKind.WAIT:
            duration = cfg.get("duration_minutes")
            if not isinstance(duration, (int, float)) or duration <= 0:
                errors.append(f"{label}: duration_minutes must be a positive number")
        elif kind_enum == SequenceNodeKind.EMAIL and not n.get("is_entry"):
            if not cfg.get("subject_template") or not cfg.get("body_template"):
                errors.append(
                    f"{label}: follow-up email needs subject_template + body_template"
                )
        elif kind_enum == SequenceNodeKind.EMAIL_REPLY:
            if n.get("is_entry"):
                errors.append(
                    f"{label}: a reply node can't be the entry node "
                    "(there's no previous email to reply to)"
                )
            if not cfg.get("ai_compose") and not (cfg.get("body_template") or "").strip():
                errors.append(
                    f"{label}: reply node needs body_template (or enable ai_compose)"
                )
        elif kind_enum == SequenceNodeKind.LINKEDIN_CONNECT:
            if not cfg.get("no_note"):
                note = cfg.get("note_template", "")
                if not isinstance(note, str):
                    errors.append(f"{label}: note_template must be a string")
                elif len(note) > 300:
                    errors.append(
                        f"{label}: note_template is {len(note)} chars "
                        "(LinkedIn caps connect notes at 300)"
                    )
        elif kind_enum == SequenceNodeKind.LINKEDIN_DM:
            if not cfg.get("ai_compose") and not cfg.get("text_template"):
                errors.append(f"{label}: text_template is required (or enable ai_compose)")
        elif kind_enum == SequenceNodeKind.LINKEDIN_INVITE_TO_PAGE:
            page_id = cfg.get("page_id")
            if not page_id:
                errors.append(f"{label}: page_id is required")
            elif not str(page_id).strip().isdigit():
                errors.append(f"{label}: page_id must be a numeric company ID")
        elif kind_enum == SequenceNodeKind.LINKEDIN_INMAIL:
            if not cfg.get("subject_template"):
                errors.append(f"{label}: subject_template is required")
            if not cfg.get("body_template"):
                errors.append(f"{label}: body_template is required")
        elif kind_enum == SequenceNodeKind.LINKEDIN_COMMENT_POST:
            if not cfg.get("comment_template"):
                errors.append(f"{label}: comment_template is required")
            target = cfg.get("target", "latest")
            if target not in {"latest", "most_engaged"}:
                errors.append(f"{label}: target must be 'latest' or 'most_engaged'")

    for i, e in enumerate(edges):
        cond_errs = validate(e.get("condition", {"op": "always"}), f"edges[{i}].condition")
        errors.extend(cond_errs)

    node_ids = [n["client_id"] for n in nodes]
    edge_pairs = [(e["from_client_id"], e.get("to_client_id")) for e in edges]
    cycle = detect_cycle(node_ids, edge_pairs)
    if cycle:
        errors.append(f"graph contains a cycle: {' -> '.join(cycle + [cycle[0]])}")

    # Reachability from entry
    reachable: set[str] = set()
    stack = [entry["client_id"]]
    adj: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for src, dst in edge_pairs:
        if dst is not None and src in adj and dst in adj:
            adj[src].append(dst)
    while stack:
        cur = stack.pop()
        if cur in reachable:
            continue
        reachable.add(cur)
        stack.extend(adj.get(cur, []))
    unreachable = set(node_ids) - reachable
    if unreachable:
        errors.append(f"{len(unreachable)} node(s) are unreachable from the entry")

    return errors


async def replace_graph(
    db: AsyncSession,
    sequence: Sequence,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> dict[str, uuid.UUID]:
    """Persist the new topology, PRESERVING the ids of unchanged nodes.
    Returns a mapping of client_id → persisted node UUID.

    Also clears ``is_published`` since the graph just changed.

    **Node-identity preservation (why):** a node the builder sent with an
    existing node's UUID as its ``client_id`` AND the same kind is UPDATED
    IN PLACE (config / entry / position) — its id is kept.  Only genuinely
    new nodes (builder-generated ``n_...`` client_ids, or a kind change) get
    a fresh id; existing live nodes absent from the payload are soft-deleted.

    This matters for editing a RUNNING campaign:
    - ``lead_step_executions`` keep matching their node ids, so the
      per-node send-once idempotency still works → re-walking a lead never
      re-sends a node it already did.
    - In-flight leads' ``current_node_id`` stays valid (no mass "current
      node deleted" halt just because the graph was re-saved).
    - A newly-added node has a new id with no execution history, so it
      fires for every lead that reaches it (incl. re-enrolled ones).
    Removed nodes are still soft-deleted, preserving their analytics.
    """
    now = datetime.now(timezone.utc)

    # Hard-delete edges — they're cheap and have no analytics value.
    existing_edges = (await db.execute(
        select(SequenceEdge).where(SequenceEdge.sequence_id == sequence.id)
    )).scalars().all()
    for e in existing_edges:
        await db.delete(e)

    # Index the LIVE nodes so we can update-in-place / preserve ids.
    existing_nodes = (await db.execute(
        select(SequenceNode).where(
            SequenceNode.sequence_id == sequence.id,
            live_nodes_filter(),
        )
    )).scalars().all()
    existing_by_id: dict[uuid.UUID, SequenceNode] = {n.id: n for n in existing_nodes}

    sequence.is_published = False

    client_to_db: dict[str, uuid.UUID] = {}
    kept_ids: set[uuid.UUID] = set()
    for n in nodes:
        is_entry = bool(n.get("is_entry", False))
        kind = SequenceNodeKind(n["kind"])
        config = n.get("config") or {}
        # An email entry node always means "send the campaign-composed first
        # email" — stamp the flag the sequencer keys off of so a node the
        # builder promoted to entry behaves like the seeded default.
        if is_entry and kind == SequenceNodeKind.EMAIL:
            config = {**config, "use_campaign_compose": True}

        # Match an existing live node by UUID client_id + same kind → update
        # in place (preserve id).  Anything else → create a fresh node.
        matched: SequenceNode | None = None
        try:
            cid_uuid = uuid.UUID(str(n["client_id"]))
        except (ValueError, AttributeError, TypeError):
            cid_uuid = None
        if cid_uuid is not None and cid_uuid in existing_by_id:
            candidate = existing_by_id[cid_uuid]
            if candidate.kind == kind:
                matched = candidate

        if matched is not None:
            matched.config = config
            matched.is_entry = is_entry
            if "position_x" in n:
                matched.position_x = int(n["position_x"])
            if "position_y" in n:
                matched.position_y = int(n["position_y"])
            client_to_db[n["client_id"]] = matched.id
            kept_ids.add(matched.id)
        else:
            node = SequenceNode(
                sequence_id=sequence.id,
                kind=kind,
                config=config,
                position_x=int(n.get("position_x", 0)),
                position_y=int(n.get("position_y", 0)),
                is_entry=is_entry,
            )
            db.add(node)
            await db.flush()
            client_to_db[n["client_id"]] = node.id
            kept_ids.add(node.id)

    # Soft-delete live nodes that survived neither as an in-place update nor
    # a recreate (genuinely removed, or whose kind changed).
    for nid, node in existing_by_id.items():
        if nid not in kept_ids:
            node.deleted_at = now
    await db.flush()

    for e in edges:
        db.add(SequenceEdge(
            sequence_id=sequence.id,
            from_node_id=client_to_db[e["from_client_id"]],
            to_node_id=client_to_db[e["to_client_id"]] if e.get("to_client_id") else None,
            condition=e.get("condition") or {"op": "always"},
            priority=int(e.get("priority", 0)),
        ))

    return client_to_db


async def reenroll_for_new_nodes(db: AsyncSession, sequence: Sequence) -> int:
    """Re-queue COMPLETED / HALTED leads that have a live action node they
    haven't executed yet — i.e. a node was added after they finished.

    Each such lead is reset to its most-recent STILL-LIVE executed node (so
    it re-evaluates that node's now-updated outgoing edges and flows into
    the new node), or the entry node when it has no live execution.  Because
    ``replace_graph`` preserves node ids, the per-node send-once idempotency
    skips everything the lead already did — only the genuinely-new node(s)
    fire, so nothing is re-sent.  Returns how many leads were re-queued.

    Call AFTER publishing the (changed) graph.  A no-op when no live action
    node is unexecuted for a lead (e.g. a config-only edit).
    """
    live_nodes = (await db.execute(
        select(SequenceNode).where(
            SequenceNode.sequence_id == sequence.id,
            live_nodes_filter(),
        )
    )).scalars().all()
    live_ids = {n.id for n in live_nodes}
    entry = next((n for n in live_nodes if n.is_entry), None)
    if entry is None:
        return 0
    # Live nodes that record an execution (so "did the lead do it?" is known).
    action_ids = {
        n.id for n in live_nodes
        if n.kind in _EXECUTION_RECORDING_KINDS
        or (n.kind == SequenceNodeKind.EMAIL and not n.is_entry)
    }
    if not action_ids:
        return 0   # nothing actionable to re-run

    states = (await db.execute(
        select(LeadSequenceState).where(
            LeadSequenceState.sequence_id == sequence.id,
            LeadSequenceState.status.in_(
                (LeadSequenceStatus.COMPLETED, LeadSequenceStatus.HALTED)
            ),
        )
    )).scalars().all()
    if not states:
        return 0

    lead_ids = [s.lead_id for s in states]
    # Per-lead: which nodes they've executed + their latest still-live one.
    exec_rows = (await db.execute(
        select(
            LeadStepExecution.lead_id,
            LeadStepExecution.node_id,
            LeadStepExecution.attempted_at,
        )
        .where(LeadStepExecution.lead_id.in_(lead_ids))
        .order_by(LeadStepExecution.attempted_at.asc())
    )).all()
    executed: dict[uuid.UUID, set[uuid.UUID]] = {}
    last_live: dict[uuid.UUID, uuid.UUID] = {}
    last_live_at: dict[uuid.UUID, datetime] = {}
    for lid, nid, at in exec_rows:
        executed.setdefault(lid, set()).add(nid)
        if nid in live_ids:
            last_live[lid] = nid   # asc order → last assignment wins
            last_live_at[lid] = at

    now = datetime.now(timezone.utc)
    requeued = 0
    for s in states:
        done = executed.get(s.lead_id, set())
        if not (action_ids - done):
            continue   # already executed every live action node → nothing new
        s.current_node_id = last_live.get(s.lead_id, entry.id)
        s.status = LeadSequenceStatus.ACTIVE
        s.halt_reason = None
        s.next_run_at = now
        # Anchor entered_current_at to when the lead ACTUALLY reached this
        # node — NOT "now".  Resetting it to now on every publish restarts
        # time-based edge waits (e.g. "reply 2 days after the first email"),
        # so a lead that's been waiting for days never crosses the gate if
        # the sequence is edited.  Use the node's real execution time when we
        # have it (a re-published downstream node), else preserve the lead's
        # existing entry timestamp (the entry email's send/enrol time), and
        # only fall back to now when there's nothing to anchor to.
        anchor = last_live_at.get(s.lead_id) or s.entered_current_at or now
        s.entered_current_at = anchor
        requeued += 1
    return requeued
