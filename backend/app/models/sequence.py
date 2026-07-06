from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.tenancy.mixin import TenantMixin

if TYPE_CHECKING:
    from app.models.campaign import Campaign


class SequenceNodeKind(str, enum.Enum):
    EMAIL = "email"
    # Reply in-thread to the lead's original campaign email instead of
    # starting a new thread.  Body is AI-written (guided by a prompt) or a
    # manual template.  Never a valid entry node — it needs a prior email.
    EMAIL_REPLY = "email_reply"
    WAIT = "wait"
    # LinkedIn kinds are reserved here so existing rows / API clients can use
    # them, but publish() rejects sequences containing them in M1. M2 lights
    # them up by wiring the LinkedInProvider channel handlers.
    LINKEDIN_VIEW_PROFILE = "linkedin_view_profile"
    LINKEDIN_FOLLOW_PROFILE = "linkedin_follow_profile"
    LINKEDIN_REACT_POST = "linkedin_react_post"
    LINKEDIN_COMMENT_POST = "linkedin_comment_post"
    LINKEDIN_CONNECT = "linkedin_connect"
    LINKEDIN_DM = "linkedin_dm"
    LINKEDIN_INMAIL = "linkedin_inmail"
    LINKEDIN_INVITE_TO_PAGE = "linkedin_invite_to_page"


class LeadSequenceStatus(str, enum.Enum):
    PENDING = "pending"      # row exists, not yet entered the entry node
    ACTIVE = "active"        # sitting on current_node_id, awaiting next_run_at
    HALTED = "halted"        # no outgoing edge matched; needs intervention
    COMPLETED = "completed"  # walked to a terminal node


class LeadStepResult(str, enum.Enum):
    SENT = "sent"
    SKIPPED = "skipped"
    FAILED = "failed"


class Sequence(TenantMixin, Base):
    __tablename__ = "sequences"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    is_published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    campaign: Mapped["Campaign"] = relationship(back_populates="sequence")
    nodes: Mapped[list["SequenceNode"]] = relationship(
        back_populates="sequence",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    edges: Mapped[list["SequenceEdge"]] = relationship(
        back_populates="sequence",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class SequenceNode(TenantMixin, Base):
    __tablename__ = "sequence_nodes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sequence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sequences.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[SequenceNodeKind] = mapped_column(
        Enum(SequenceNodeKind, name="sequence_node_kind", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    # Per-kind config. Examples:
    #   email   -> {"use_campaign_compose": true} or {"subject_template": ..., "body_template": ...}
    #   wait    -> {"duration_minutes": 4320, "skip_weekends": false}
    #   linkedin_connect -> {"note_template": "..."}
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    position_x: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    position_y: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_entry: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Soft-delete: graph re-edits set this to "retired" the old topology
    # while keeping ``lead_step_executions`` rows intact for analytics.
    # Live queries should filter ``deleted_at IS NULL``.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    sequence: Mapped["Sequence"] = relationship(back_populates="nodes")


class SequenceEdge(TenantMixin, Base):
    __tablename__ = "sequence_edges"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sequence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sequences.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    from_node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sequence_nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # NULL to_node_id ⇒ "terminate the sequence on this branch".
    to_node_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sequence_nodes.id", ondelete="CASCADE"),
        nullable=True,
    )
    # See app.services.sequence_conditions for the expression grammar.
    condition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default='{"op": "always"}')
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    sequence: Mapped["Sequence"] = relationship(back_populates="edges")


class LeadSequenceState(TenantMixin, Base):
    __tablename__ = "lead_sequence_states"
    __table_args__ = (
        # Hot path: the sequencer beat selects due ACTIVE states per tenant.
        Index("ix_lead_seq_states_tenant_due", "tenant_id", "status", "next_run_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    sequence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sequences.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    current_node_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sequence_nodes.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[LeadSequenceStatus] = mapped_column(
        Enum(LeadSequenceStatus, name="lead_sequence_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=LeadSequenceStatus.PENDING,
        server_default=LeadSequenceStatus.PENDING.value,
        index=True,
    )
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    halt_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    entered_current_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class LeadStepExecution(TenantMixin, Base):
    __tablename__ = "lead_step_executions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sequence_nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    result: Mapped[LeadStepResult] = mapped_column(
        Enum(LeadStepResult, name="lead_step_result", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
