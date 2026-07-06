"""Intent/trigger prospecting (Feature C, migration 0030).

Sibling of the Social Listening subsystem: tracked entities →
scheduled checks → detected signals → review queue.

  SignalWatch    — what we monitor: a job-change / funding / hiring
                   check against a lead, an opportunity, or a free-text
                   cold target.  Carries ``last_seen`` (the last-known
                   title / funding stage / hiring roles) so detection
                   is a DIFF, not a snapshot — no change, no signal.
  ProspectSignal — one detected event, deduped forever on
                   ``dedup_key`` so a re-poll never re-emits.

Autonomy boundary: a signal may create a CRM task, a notification, or
a campaign-less CRM lead — it may NEVER add anyone to a sending
campaign; that stays one human click away.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.tenancy.mixin import TenantMixin
from app.models.social_listening import SocialSearchFrequency


class SignalWatchType(str, enum.Enum):
    JOB_CHANGE = "job_change"
    FUNDING = "funding"
    HIRING = "hiring"
    CUSTOM = "custom"


class SignalWatchStatus(str, enum.Enum):
    ACTIVE = "active"
    PAUSED = "paused"


class ProspectSignalStatus(str, enum.Enum):
    NEW = "new"
    ACTIONED = "actioned"
    DISMISSED = "dismissed"


class SignalWatch(TenantMixin, Base):
    __tablename__ = "signal_watches"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    watch_type: Mapped[SignalWatchType] = mapped_column(
        Enum(SignalWatchType, name="signal_watch_type",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )

    # Target: a pipeline record OR a free-text cold target.
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
    person_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    company: Mapped[str | None] = mapped_column(Text, nullable=True)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    company_website: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Reuses the social-listening frequency enum (same PG type).
    frequency: Mapped[SocialSearchFrequency] = mapped_column(
        Enum(SocialSearchFrequency, name="social_search_frequency",
             values_callable=lambda e: [m.value for m in e],
             create_type=False),
        nullable=False,
        default=SocialSearchFrequency.DAILY,
        server_default=SocialSearchFrequency.DAILY.value,
    )
    status: Mapped[SignalWatchStatus] = mapped_column(
        Enum(SignalWatchStatus, name="signal_watch_status",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=SignalWatchStatus.ACTIVE,
        server_default=SignalWatchStatus.ACTIVE.value,
        index=True,
    )

    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_run_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Last-known state per signal dimension — the diff baseline:
    # {"job_title": ..., "funding_stage": ..., "hiring_roles": [...]}.
    last_seen: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ProspectSignal(TenantMixin, Base):
    __tablename__ = "prospect_signals"
    __table_args__ = (
        # Per-tenant dedup (Phase 2); NULLS NOT DISTINCT preserves the old
        # app-wide UNIQUE(dedup_key) semantics for NULL-tenant worker rows.
        UniqueConstraint("tenant_id", "dedup_key",
                         name="uq_prospect_signals_tenant_dedup",
                         postgresql_nulls_not_distinct=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    # NULLABLE since migration 0032: discovery signals (USAspending / IRS
    # BMF feeds) have no SignalWatch behind them — they flow straight into
    # this review queue.  Watch-sourced signals still set it.
    watch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("signal_watches.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # Discovery feed tag (migration 0032): 'usaspending' | 'irs_bmf', or
    # NULL for watch-sourced signals.
    source: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    signal_type: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    # Structured payload: old/new title, round + amount, roles, source URL.
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    dedup_key: Mapped[str] = mapped_column(
        Text, nullable=False, index=True,
    )
    status: Mapped[ProspectSignalStatus] = mapped_column(
        Enum(ProspectSignalStatus, name="prospect_signal_status",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=ProspectSignalStatus.NEW,
        server_default=ProspectSignalStatus.NEW.value,
        index=True,
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="SET NULL"),
        nullable=True,
    )
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("crm_opportunities.id", ondelete="SET NULL"),
        nullable=True,
    )
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
