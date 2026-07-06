from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.sequence import (
    LeadSequenceStatus,
    LeadStepResult,
    SequenceNodeKind,
)


# --------------------------------------------------------------------------
# Sub-shapes for nodes/edges that the UI sends in PUT /sequence
# --------------------------------------------------------------------------


class SequenceNodeIn(BaseModel):
    """A node as posted by the UI.

    `client_id` lets the UI tie temporary IDs from the canvas back to the
    persisted UUIDs in the response, so a 'create + immediately edit again'
    flow keeps working without an extra roundtrip.
    """
    client_id: str = Field(min_length=1)
    kind: SequenceNodeKind
    config: dict[str, Any] = Field(default_factory=dict)
    position_x: int = 0
    position_y: int = 0
    is_entry: bool = False


class SequenceEdgeIn(BaseModel):
    from_client_id: str = Field(min_length=1)
    to_client_id: str | None = None  # None ⇒ terminate the branch
    condition: dict[str, Any] = Field(default_factory=lambda: {"op": "always"})
    priority: int = 0


class SequenceUpdate(BaseModel):
    """Whole-sequence replace. The router wipes the existing nodes/edges and
    writes these. That's intentional: it keeps the API trivially correct
    even when the user reshapes the graph extensively, and the canvas UI
    posts the whole graph on save.
    """
    nodes: list[SequenceNodeIn]
    edges: list[SequenceEdgeIn]

    @model_validator(mode="after")
    def _check_entry(self) -> "SequenceUpdate":
        entries = [n for n in self.nodes if n.is_entry]
        if len(entries) != 1:
            raise ValueError(
                f"sequence must have exactly one entry node, got {len(entries)}"
            )
        client_ids = [n.client_id for n in self.nodes]
        if len(set(client_ids)) != len(client_ids):
            raise ValueError("node client_id values must be unique")
        valid_ids = set(client_ids)
        for e in self.edges:
            if e.from_client_id not in valid_ids:
                raise ValueError(f"edge.from_client_id '{e.from_client_id}' is not a node")
            if e.to_client_id is not None and e.to_client_id not in valid_ids:
                raise ValueError(f"edge.to_client_id '{e.to_client_id}' is not a node")
        return self


# --------------------------------------------------------------------------
# Read shapes returned by GET
# --------------------------------------------------------------------------


class SequenceNodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    kind: SequenceNodeKind
    config: dict[str, Any]
    position_x: int
    position_y: int
    is_entry: bool


class SequenceEdgeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    from_node_id: uuid.UUID
    to_node_id: uuid.UUID | None
    condition: dict[str, Any]
    priority: int


class SequenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    campaign_id: uuid.UUID
    is_published: bool
    nodes: list[SequenceNodeOut]
    edges: list[SequenceEdgeOut]
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------
# Validate / publish
# --------------------------------------------------------------------------


class ValidateResponse(BaseModel):
    ok: bool
    errors: list[str] = Field(default_factory=list)


class PublishResponse(BaseModel):
    ok: bool
    is_published: bool
    errors: list[str] = Field(default_factory=list)
    # How many already-finished (completed/halted) leads were re-queued to
    # pick up a newly-added node on this publish.
    reenrolled_for_new_nodes: int = 0


# --------------------------------------------------------------------------
# Read shapes for lead state + executions (used by campaign detail page)
# --------------------------------------------------------------------------


class LeadSequenceStateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    lead_id: uuid.UUID
    sequence_id: uuid.UUID
    current_node_id: uuid.UUID | None
    status: LeadSequenceStatus
    next_run_at: datetime | None
    halt_reason: str | None
    entered_current_at: datetime | None


class LeadStepExecutionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    lead_id: uuid.UUID
    node_id: uuid.UUID
    attempted_at: datetime
    result: LeadStepResult
    external_id: str | None
    error: str | None


# --------------------------------------------------------------------------
# Analytics (M5)
# --------------------------------------------------------------------------


class NodeAnalytics(BaseModel):
    node_id: uuid.UUID
    # Node identity so the UI can label + order the funnel without a second
    # fetch of the sequence graph.
    kind: SequenceNodeKind
    title: str | None = None  # builder-set config.title, else None
    is_entry: bool = False
    attempted: int  # total lead_step_executions on this node
    sent: int       # result = SENT
    skipped: int    # result = SKIPPED
    failed: int     # result = FAILED
    currently_here: int  # active lead_sequence_states pointing at this node
    # Of currently_here, how many have ALREADY sent this node — they'll
    # skip-and-advance (no resend); the rest are awaiting their first send.
    here_already_sent: int = 0


class SequenceAnalyticsResponse(BaseModel):
    campaign_id: uuid.UUID
    sequence_id: uuid.UUID
    total_leads: int          # total lead_sequence_states for this sequence
    halted: int               # status = HALTED
    completed: int            # status = COMPLETED
    active: int               # status = ACTIVE
    pending: int              # status = PENDING (rare in practice)
    per_node: list[NodeAnalytics]
