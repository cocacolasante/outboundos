"""CRM models: opportunities + manually-logged activities.

Salesforce-style layer on top of the outreach engine.  The existing
``Lead`` model doubles as the CRM lead (it gained ``crm_status`` +
``converted_opportunity_id`` and a nullable ``campaign_id`` in
migration 0025).  This module adds:

  Opportunity   — a deal moving through a stage pipeline.
  CrmActivity   — a manually-logged touch (call / email / meeting /
                  note / task) attached to a lead OR an opportunity.

These are deliberately separate from the AUTOMATED history
(``lead_step_executions`` + ``email_events``): those record what the
machine did; CrmActivity records what the human did.  The lead-detail
endpoint merges all three into one timeline.
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, Enum, ForeignKey, Index,
    Integer, LargeBinary, Numeric, Text, func, text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.tenancy.mixin import TenantMixin


class CrmLeadStatus(str, enum.Enum):
    NEW = "new"
    WORKING = "working"
    QUALIFIED = "qualified"
    CONVERTED = "converted"
    UNQUALIFIED = "unqualified"


class OpportunityStage(str, enum.Enum):
    PROSPECTING = "prospecting"
    QUALIFICATION = "qualification"
    PROPOSAL = "proposal"
    NEGOTIATION = "negotiation"
    CLOSED_WON = "closed_won"
    CLOSED_LOST = "closed_lost"


# Default win probability per stage — Salesforce-style heuristics.  Used
# to seed ``probability`` on create / stage change when the user hasn't
# set their own number.
STAGE_DEFAULT_PROBABILITY: dict[OpportunityStage, int] = {
    OpportunityStage.PROSPECTING: 10,
    OpportunityStage.QUALIFICATION: 25,
    OpportunityStage.PROPOSAL: 50,
    OpportunityStage.NEGOTIATION: 75,
    OpportunityStage.CLOSED_WON: 100,
    OpportunityStage.CLOSED_LOST: 0,
}

CLOSED_STAGES = {OpportunityStage.CLOSED_WON, OpportunityStage.CLOSED_LOST}


class CrmActivityType(str, enum.Enum):
    CALL = "call"
    EMAIL = "email"
    MEETING = "meeting"
    NOTE = "note"
    TASK = "task"


class CrmActivityDirection(str, enum.Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class Opportunity(TenantMixin, Base):
    __tablename__ = "crm_opportunities"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    stage: Mapped[OpportunityStage] = mapped_column(
        Enum(OpportunityStage, name="opportunity_stage",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False, default=OpportunityStage.PROSPECTING,
        server_default=OpportunityStage.PROSPECTING.value,
        index=True,
    )
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    close_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    probability: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    loss_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Contact snapshot — copied at conversion so the deal record stays
    # complete even if the source lead row is later deleted.
    first_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    email: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    phone: Mapped[str | None] = mapped_column(Text, nullable=True)
    company: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    source_lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="SET NULL"),
        nullable=True,
    )

    # --- Phase 1 (migration 0038): configurable pipeline + normalized graph ---
    # ``stage`` (enum) is KEPT and dual-written for back-compat; ``stage_id``
    # becomes the source of truth as code migrates onto it.
    pipeline_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pipelines.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    stage_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("opportunity_stages.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    # No FK yet — no users/auth table exists; design-ready for a later refactor.
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    activities: Mapped[list["CrmActivity"]] = relationship(
        back_populates="opportunity",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class CrmActivity(TenantMixin, Base):
    __tablename__ = "crm_activities"
    __table_args__ = (
        CheckConstraint(
            "lead_id IS NOT NULL OR opportunity_id IS NOT NULL",
            name="ck_crm_activities_has_parent",
        ),
        # Upcoming-tasks hot query: open tasks ordered by due date.
        Index(
            "ix_crm_activities_open_tasks",
            "due_at",
            postgresql_where=text(
                "activity_type = 'task' AND completed_at IS NULL"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("crm_opportunities.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # --- Phase 1 (migration 0038): broaden the related-to graph + owner/tenant.
    # Existing lead/opportunity polymorphic link is unchanged; these extend it
    # so activities can also hang off accounts/contacts and be reported across
    # objects.  All nullable — nothing is required to backfill.
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)

    activity_type: Mapped[CrmActivityType] = mapped_column(
        Enum(CrmActivityType, name="crm_activity_type",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    direction: Mapped[CrmActivityDirection | None] = mapped_column(
        Enum(CrmActivityDirection, name="crm_activity_direction",
             values_callable=lambda e: [m.value for m in e]),
        nullable=True,
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When the touch actually happened (a call logged after the fact can
    # be backdated); defaults to now.
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    # --- Agent fields (migration 0027) ---
    # Stamped once a due/overdue reminder email fired for this task —
    # the sweeper's idempotency anchor (one reminder per task, ever).
    reminder_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # positive | neutral | negative — set by the reply classifier on
    # agent-logged inbound emails.  Free text (not an enum) so the
    # classifier vocabulary can evolve without a migration.
    sentiment: Mapped[str | None] = mapped_column(Text, nullable=True)
    # True when the agent (not the human) created this row — lets the
    # UI badge automated entries and queries exclude them.
    is_agent_generated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    opportunity: Mapped["Opportunity | None"] = relationship(back_populates="activities")


class CrmDocument(TenantMixin, Base):
    """File attachment on an opportunity (proposal, contract, quote).

    Bytes live in Postgres — right-sized for a single-operator tool
    (backups capture everything, no object store to run).  The route
    layer enforces a 10MB per-file cap."""

    __tablename__ = "crm_documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("crm_opportunities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


class OpportunityProduct(TenantMixin, Base):
    """Product-of-interest line item (Salesforce OpportunityLineItem,
    lite).  Free-text product name — no global catalog in v1."""

    __tablename__ = "crm_opportunity_products"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("crm_opportunities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_name: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("1"), server_default="1",
    )
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# ============================================================================
# Phase 1 (migration 0038): configurable pipeline + normalized account/contact
# ============================================================================
#
# All tenancy-ready: nullable ``tenant_id`` on every table.  The legacy
# ``OpportunityStage`` enum + ``STAGE_DEFAULT_PROBABILITY`` / ``CLOSED_STAGES``
# above are kept as the seed source + back-compat fallback; the tables below
# make stages first-class + configurable.


class Pipeline(TenantMixin, Base):
    """An ordered set of opportunity stages.  v1 ships a single
    ``is_default`` pipeline (seeded to mirror the legacy enum); the schema
    supports multiple pipelines later without a migration."""

    __tablename__ = "pipelines"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )

    stages: Mapped[list["PipelineStage"]] = relationship(
        back_populates="pipeline",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="PipelineStage.sort_order",
    )


class PipelineStage(TenantMixin, Base):
    """A configurable, ordered stage within a pipeline.  ``key`` mirrors the
    legacy ``OpportunityStage`` enum value for seeded stages so reporting can
    map old rows; ``is_won`` / ``is_lost`` replace the hard-coded
    ``CLOSED_STAGES`` set as data."""

    __tablename__ = "opportunity_stages"
    __table_args__ = (
        Index("ix_opportunity_stages_pipeline_order", "pipeline_id", "sort_order"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pipelines.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # Stable machine key (e.g. "proposal"); seeded to the legacy enum value.
    key: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    default_probability: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_won: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    is_lost: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )

    pipeline: Mapped["Pipeline"] = relationship(back_populates="stages")


class Account(TenantMixin, Base):
    """A company.  Normalizes the ``company`` text denormalized on leads /
    opportunities.  Existing lead handling is unchanged — accounts are linked
    to NEW opportunities/contacts going forward, no historical backfill."""

    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    website: Mapped[str | None] = mapped_column(Text, nullable=True)
    industry: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )

    contacts: Mapped[list["Contact"]] = relationship(
        back_populates="account", passive_deletes=True,
    )


class Contact(TenantMixin, Base):
    """A person, optionally a member of an Account.  Distinct from ``leads``
    (the outreach recipient): a contact is the CRM person record.  Can be
    seeded from a lead via ``source_lead_id`` without disrupting the lead."""

    __tablename__ = "contacts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    first_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    email: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    phone: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )

    account: Mapped["Account | None"] = relationship(back_populates="contacts")


class OpportunityStageChange(TenantMixin, Base):
    """Append-only audit of every opportunity stage move (the Phase 2 Kanban
    writes one per drag).  Stores both the stage FK and the stage ``key`` so
    history survives a stage being renamed/deactivated.  ``source`` is
    'user' for human moves, 'agent' for (suggest-and-approve) automated ones;
    ``changed_by`` is a design-ready owner/user id (no FK — no users table)."""

    __tablename__ = "opportunity_stage_changes"
    __table_args__ = (
        Index("ix_opp_stage_changes_opp_time", "opportunity_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("crm_opportunities.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    from_stage_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    to_stage_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    from_stage_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_stage_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="user", server_default="user")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
