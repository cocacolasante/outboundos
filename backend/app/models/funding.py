"""Nonprofit funding discovery — per-source state (migration 0032).

``FundingSourceState`` is the cursor / diff baseline for each external
feed, the same idea ``SignalWatch.last_seen`` plays for watches.  One
row per source ('usaspending' | 'irs_bmf').
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Enum, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.tenancy.mixin import TenantMixin


class FundingSourceState(Base):
    __tablename__ = "funding_source_state"

    # 'usaspending' | 'irs_bmf'
    source: Mapped[str] = mapped_column(Text, primary_key=True)
    # Runtime on/off, editable from Settings → Discovery (migration 0033).
    # NULL = not yet seeded; the worker/API seed it from the env default.
    enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Runtime config (migration 0033), seeded from env when NULL:
    #   usaspending: {"lookback_days": 7}
    #   irs_bmf:     {"ruling_lookback_months": 2, "states": ["PA", "NJ"]}
    config: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Per-source high-water mark, e.g.
    #   usaspending: {"last_action_date": "2026-06-14"}
    #   irs_bmf:     {"last_file_month": "202606"}
    cursor: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_run_status: Mapped[str | None] = mapped_column(Text, nullable=True)


class FundingEnrichmentStatus(str, enum.Enum):
    PENDING = "pending"        # awaiting (re)try — has not resolved a contact
    RESOLVED = "resolved"      # promoted to a ProspectSignal
    EXHAUSTED = "exhausted"    # gave up after MAX_ATTEMPTS, no direct mail
    MAILED = "mailed"          # exhausted → direct-mail CRM task created


class FundingEnrichmentQueue(TenantMixin, Base):
    """Deferred-enrichment queue (migration 0036).

    Discovered orgs that resolve NO contact never enter the
    ``prospect_signals`` review queue (which must stay actionable).  They
    park here and a daily retry worker re-attempts contact resolution —
    brand-new 501(c)(3)s stand up websites within months — promoting a row
    to a real signal once a contact resolves, or exhausting it (optionally
    into a direct-mail task) after a few tries.
    """
    __tablename__ = "funding_enrichment_queue"
    __table_args__ = (
        UniqueConstraint("tenant_id", "dedup_key",
                         name="uq_funding_queue_tenant_dedup",
                         postgresql_nulls_not_distinct=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    ein: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The org's eventual ProspectSignal dedup_key — unique per tenant so
    # promotion can never double-emit against the prospect_signals
    # constraint (NULLS NOT DISTINCT keeps NULL-tenant rows app-unique).
    dedup_key: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    org_name: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str | None] = mapped_column(Text, nullable=True)
    ntee_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Cached on first discovery / any later domain hit so retries skip the
    # web-search step.
    website: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Full signal payload to rebuild the DiscoveredOrg on promotion:
    # {signal_type, summary, detail (incl. mailing_address)}.
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}",
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )
    status: Mapped[FundingEnrichmentStatus] = mapped_column(
        Enum(FundingEnrichmentStatus, name="funding_enrichment_status",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=FundingEnrichmentStatus.PENDING,
        server_default=FundingEnrichmentStatus.PENDING.value,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
