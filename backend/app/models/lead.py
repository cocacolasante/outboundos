from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.tenancy.mixin import TenantMixin

if TYPE_CHECKING:
    from app.models.campaign import Campaign
    from app.models.email_event import EmailEvent


class ResearchStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class ComposeStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class SendStatus(str, enum.Enum):
    PENDING = "pending"
    SCHEDULED = "scheduled"
    SENT = "sent"
    FAILED = "failed"
    # Terminal state for leads whose email is on the suppression list —
    # distinct from FAILED so deliberate ignores don't pollute the
    # campaign error list or get re-enqueued by retry-failed.
    SUPPRESSED = "suppressed"


class LinkedInConnectionStatus(str, enum.Enum):
    UNKNOWN = "unknown"
    INVITED = "invited"
    CONNECTED = "connected"
    DECLINED = "declined"
    WITHDRAWN = "withdrawn"


class Lead(TenantMixin, Base):
    __tablename__ = "leads"
    __table_args__ = (
        # Hot path: the global Leads list + per-campaign lead queries.
        Index("ix_leads_tenant_campaign", "tenant_id", "campaign_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Nullable since migration 0025: a CRM lead can be created manually,
    # outside any campaign.  Campaign-less leads never enter the
    # compose/send pipeline — they're CRM records until the user adds
    # them to a campaign.
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # Nullable since migration 0034: a signal "Find contact" enrichment
    # can stage a LinkedIn-only lead (no email).  Campaign-bound leads
    # always carry an email in practice.
    email: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    phone: Mapped[str | None] = mapped_column(Text, nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    company: Mapped[str | None] = mapped_column(Text, nullable=True)
    company_website: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_csv_row: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    research_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    composed_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    composed_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    research_status: Mapped[ResearchStatus] = mapped_column(
        Enum(ResearchStatus, name="research_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=ResearchStatus.PENDING,
        server_default=ResearchStatus.PENDING.value,
    )
    compose_status: Mapped[ComposeStatus] = mapped_column(
        Enum(ComposeStatus, name="compose_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=ComposeStatus.PENDING,
        server_default=ComposeStatus.PENDING.value,
    )
    send_status: Mapped[SendStatus] = mapped_column(
        Enum(SendStatus, name="send_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=SendStatus.PENDING,
        server_default=SendStatus.PENDING.value,
        index=True,
    )
    is_sample: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    sample_approved: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    style_correction: Mapped[str | None] = mapped_column(Text, nullable=True)
    brevo_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Free-form notes the user adds in the lite-CRM Leads view.  Per
    # (campaign × email) — a lead in a different campaign for the same email
    # gets its own notes row.
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    scheduled_send_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Recipient IANA timezone (e.g. "America/New_York") for send-time
    # optimization — populated by research/enrichment when the company HQ /
    # Apollo location is known; falls back to the campaign tz when NULL.
    timezone: Mapped[str | None] = mapped_column(Text, nullable=True)
    linkedin_connection_status: Mapped[LinkedInConnectionStatus] = mapped_column(
        Enum(
            LinkedInConnectionStatus,
            name="linkedin_connection_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=LinkedInConnectionStatus.UNKNOWN,
        server_default=LinkedInConnectionStatus.UNKNOWN.value,
    )
    linkedin_last_reply_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # ---- CRM fields (migration 0025) ----
    # Salesforce-style lead status.  ``converted`` is set by the
    # lead-conversion endpoint; the rest are user-managed.
    crm_status: Mapped[str] = mapped_column(
        Enum("new", "working", "qualified", "converted", "unqualified",
             name="crm_lead_status"),
        nullable=False, default="new", server_default="new",
    )
    # Set at conversion — the opportunity this lead became.  SET NULL on
    # opportunity delete so the lead survives.
    converted_opportunity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("crm_opportunities.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Tenancy-ready (migration 0038): nullable now, scoped later. Leads remain
    # both the outreach recipient and the CRM lead exactly as before.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    campaign: Mapped["Campaign"] = relationship(back_populates="leads")
    events: Mapped[list["EmailEvent"]] = relationship(
        back_populates="lead",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
