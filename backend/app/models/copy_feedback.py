"""Reply-driven copy feedback loop (Feature A, migration 0029).

Two tables:

  ReplyOutcome          — one row per classified inbound reply, with a
                          SNAPSHOT of the composed subject/body that
                          earned it.  Snapshotting matters: the lead's
                          composed copy is editable after the fact, and
                          attribution must point at what was actually
                          sent.
  CampaignCopyInsights  — per-campaign cached LLM summary of what's
                          working (winning openers / subject patterns /
                          what to avoid).  Refreshed only when enough
                          NEW outcomes accumulated since the last pass,
                          so compose never pays an extra LLM call.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.tenancy.mixin import TenantMixin


class ReplyOutcome(TenantMixin, Base):
    __tablename__ = "reply_outcomes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="SET NULL"),
        nullable=True,
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sentiment: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    intent: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Snapshot of the copy that was actually sent.
    composed_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    composed_body: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


class CampaignCopyInsights(TenantMixin, Base):
    __tablename__ = "campaign_copy_insights"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    # Structured LLM output: winning_openers, subject_patterns,
    # value_framings, cta_styles, avoid (lists of short strings).
    insights: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # Total ReplyOutcome count at refresh time — the staleness anchor
    # (re-run only when count - this >= the new-outcome threshold).
    outcome_count_at_refresh: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
