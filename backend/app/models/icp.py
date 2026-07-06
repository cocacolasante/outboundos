"""ICP lookalike expansion (Feature D, migration 0031).

  IcpProfile          — the ideal-customer fingerprint.  The auto
                        profile is derived from closed_won deals and
                        regenerated as deals close; manual profiles are
                        user-authored.  ``criteria`` is structured JSON
                        (industries, employee band, title patterns,
                        geographies, funding stages, keywords).
  LookalikeCandidate  — a discovered prospect awaiting human review.
                        Dedup'd on domain/linkedin against existing
                        leads, opps, and prior candidates.  Accepting
                        creates a campaign-less CRM lead — adding to a
                        sending campaign stays a separate human action.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.tenancy.mixin import TenantMixin


class IcpProfileSource(str, enum.Enum):
    AUTO_CLOSED_WON = "auto_closed_won"
    MANUAL = "manual"


class IcpProfileStatus(str, enum.Enum):
    READY = "ready"
    # Fewer than the minimum closed-won deals — discovery is skipped
    # until enough wins accumulate to make the fingerprint meaningful.
    INSUFFICIENT_DATA = "insufficient_data"


class LookalikeCandidateStatus(str, enum.Enum):
    NEW = "new"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class IcpProfile(TenantMixin, Base):
    __tablename__ = "icp_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[IcpProfileSource] = mapped_column(
        Enum(IcpProfileSource, name="icp_profile_source",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=IcpProfileSource.AUTO_CLOSED_WON,
        server_default=IcpProfileSource.AUTO_CLOSED_WON.value,
    )
    status: Mapped[IcpProfileStatus] = mapped_column(
        Enum(IcpProfileStatus, name="icp_profile_status",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=IcpProfileStatus.READY,
        server_default=IcpProfileStatus.READY.value,
    )
    criteria: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # How many closed-won deals fed the last refresh.
    won_deal_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
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


class LookalikeCandidate(TenantMixin, Base):
    __tablename__ = "lookalike_candidates"
    __table_args__ = (
        # Per-tenant forever-dedup (Phase 2); NULLS NOT DISTINCT keeps
        # NULL-tenant worker rows app-unique like the old UNIQUE(dedup_key).
        UniqueConstraint("tenant_id", "dedup_key",
                         name="uq_lookalike_tenant_dedup",
                         postgresql_nulls_not_distinct=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    icp_profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("icp_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    company: Mapped[str] = mapped_column(Text, nullable=False)
    company_website: Mapped[str | None] = mapped_column(Text, nullable=True)
    contact_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    fit_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fit_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Domain or linkedin_url — the forever-dedup anchor (unique per tenant).
    dedup_key: Mapped[str] = mapped_column(
        Text, nullable=False, index=True,
    )
    status: Mapped[LookalikeCandidateStatus] = mapped_column(
        Enum(LookalikeCandidateStatus, name="lookalike_candidate_status",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=LookalikeCandidateStatus.NEW,
        server_default=LookalikeCandidateStatus.NEW.value,
        index=True,
    )
    # Set when accepted → the campaign-less CRM lead it became.
    created_lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="SET NULL"),
        nullable=True,
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
