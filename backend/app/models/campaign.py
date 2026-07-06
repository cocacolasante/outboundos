from __future__ import annotations

import enum
import uuid
from datetime import datetime, time
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, String, Text, Time, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.tenancy.mixin import TenantMixin

if TYPE_CHECKING:
    from app.models.connected_account import ConnectedAccount
    from app.models.email_event import EmailEvent
    from app.models.lead import Lead
    from app.models.linkedin_account import LinkedInAccount
    from app.models.sequence import Sequence
    from app.models.style_correction import StyleCorrection


class ResearchMode(str, enum.Enum):
    FAST = "fast"
    DEEP = "deep"
    # No external research at all (no Apollo/Hunter/web calls).  The compose
    # worker still runs but falls into its generic name+company-only prompt,
    # so the only API spend is one Anthropic call per lead.
    NONE = "none"
    # No research AND no AI.  The compose worker renders the campaign's
    # `template_subject` / `template_body` with per-lead merge fields
    # (`{{first_name}}`, `{{Company|there}}`, etc.).  Zero external API calls.
    TEMPLATE = "template"


class CampaignStatus(str, enum.Enum):
    DRAFT = "draft"
    PREVIEWING = "previewing"
    APPROVED = "approved"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETE = "complete"


class Campaign(TenantMixin, Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        # Hot path: the pacer/sweeps select RUNNING campaigns per tenant.
        Index("ix_campaigns_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    tone: Mapped[str] = mapped_column(Text, nullable=False)
    sender_name: Mapped[str] = mapped_column(Text, nullable=False)
    sender_email: Mapped[str] = mapped_column(Text, nullable=False)
    research_mode: Mapped[ResearchMode] = mapped_column(
        Enum(ResearchMode, name="research_mode", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=ResearchMode.FAST,
        server_default=ResearchMode.FAST.value,
    )
    # Only used when research_mode == TEMPLATE.  Authored by the user; the
    # compose worker renders these per-lead instead of calling Anthropic.
    template_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    template_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Optional signature block (sender name + contact / website / calendar).
    # When set, the compose worker replaces the AI's sign-off with this on
    # each email, and `POST /campaigns/{id}/apply-signature` applies it in
    # bulk to already-composed emails.
    signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default="5")
    connected_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("connected_accounts.id", ondelete="SET NULL"),
        nullable=True,
    )
    linkedin_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("linkedin_accounts.id", ondelete="SET NULL"),
        nullable=True,
    )
    schedule_days: Mapped[list[int]] = mapped_column(ARRAY(Integer), nullable=False, default=list, server_default="{}")
    schedule_time_start: Mapped[time] = mapped_column(Time, nullable=False)
    schedule_time_end: Mapped[time] = mapped_column(Time, nullable=False)
    schedule_timezone: Mapped[str] = mapped_column(String, nullable=False, default="UTC", server_default="UTC")
    max_per_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_per_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_delay_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60, server_default="60")
    status: Mapped[CampaignStatus] = mapped_column(
        Enum(CampaignStatus, name="campaign_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=CampaignStatus.DRAFT,
        server_default=CampaignStatus.DRAFT.value,
    )
    # When set (and status is PAUSED), the campaign was auto-paused because it
    # hit the LinkedIn daily cap with no email work left.  The sequencer beat
    # auto-resumes it (status -> RUNNING, this -> NULL) once this time passes
    # (the cap window has reset).  NULL for manual pauses, so they stay paused.
    auto_paused_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Deliverability guard (migration 0028).
    # send_time_optimization: defer each send to the recipient's optimal
    # local hour (engagement-derived, else 9-11am Tue-Thu) within the
    # campaign window.
    send_time_optimization: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    # Set by the bounce/spam circuit breaker.  UNLIKE auto_paused_until,
    # a breaker pause NEVER auto-resumes — a human must hit Resume (which
    # clears both fields).  reason is the human-readable trip explanation.
    auto_paused_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    auto_pause_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Stamped by ``update_campaign`` when the goal actually changes on a
    # non-draft campaign.  Powers the "X of Y emails rewritten" progress
    # indicator: leads with ``updated_at >= goal_updated_at`` have been
    # recomposed since the edit; the rest are still queued behind the
    # in-flight ``compose_lead`` tasks.  NULL on campaigns that have never
    # had their goal edited (post-draft) — the rewrite card stays hidden.
    goal_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    connected_account: Mapped["ConnectedAccount | None"] = relationship(back_populates="campaigns")
    linkedin_account: Mapped["LinkedInAccount | None"] = relationship(back_populates="campaigns")
    leads: Mapped[list["Lead"]] = relationship(
        back_populates="campaign",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    events: Mapped[list["EmailEvent"]] = relationship(
        back_populates="campaign",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    style_corrections: Mapped[list["StyleCorrection"]] = relationship(
        back_populates="campaign",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    sequence: Mapped["Sequence | None"] = relationship(
        back_populates="campaign",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
