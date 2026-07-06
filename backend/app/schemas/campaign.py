from __future__ import annotations

import uuid
from datetime import datetime, time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import settings
from app.models import CampaignStatus, ResearchMode


# --------------------------------------------------------------------------
# Nested sub-schemas
# --------------------------------------------------------------------------


class ConnectedAccountInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    label: str
    email_address: str


class LeadCounts(BaseModel):
    total: int
    pending: int
    scheduled: int
    sent: int
    failed: int


class FailedLeadInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    lead_id: uuid.UUID
    email: str
    first_name: str | None
    last_name: str | None
    research_status: str
    compose_status: str
    send_status: str
    failed_stage: str  # "research" | "compose" | "send"


class ApplySignatureResponse(BaseModel):
    updated: int


class RetryFailedResponse(BaseModel):
    research_retried: int
    compose_retried: int
    send_retried: int

    @property
    def total(self) -> int:
        return self.research_retried + self.compose_retried + self.send_retried


class CampaignStats(BaseModel):
    sent_count: int
    delivered: int
    opened: int
    clicked: int
    bounced: int
    replied: int
    unsubscribed: int
    open_rate: float | None
    click_rate: float | None
    bounce_rate: float | None
    reply_rate: float | None
    reply_tracking_note: str | None = None
    # False when click tracking is disabled in Brevo — the UI then shows
    # click-rate as "not tracked" rather than a misleading 0%.
    click_tracking_enabled: bool = True


# --------------------------------------------------------------------------
# Validators reused across create + update
# --------------------------------------------------------------------------


def _validate_days(v: list[int] | None) -> list[int] | None:
    if v is None:
        return v
    for d in v:
        if d < 0 or d > 6:
            raise ValueError("schedule_days values must be 0-6")
    return v


def _blank_to_none(v: str | None) -> str | None:
    """Treat an all-whitespace template field as unset so an empty textarea
    doesn't accidentally put the campaign into template mode."""
    if v is None:
        return None
    return v if v.strip() else None


# --------------------------------------------------------------------------
# Create / Update
# --------------------------------------------------------------------------


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    tone: str = Field(min_length=1)
    sender_name: str = Field(min_length=1)
    sender_email: str = Field(min_length=1)
    research_mode: ResearchMode = ResearchMode.FAST
    template_subject: str | None = None
    template_body: str | None = None
    signature: str | None = None
    sample_count: int = Field(default=5, ge=1)
    connected_account_id: uuid.UUID | None = None
    linkedin_account_id: uuid.UUID | None = None
    schedule_days: list[int] = Field(default_factory=list)
    schedule_time_start: time
    schedule_time_end: time
    schedule_timezone: str = "UTC"
    max_per_hour: int | None = Field(default=None, ge=1)
    max_per_day: int | None = Field(default=None, ge=1)
    min_delay_seconds: int = Field(default=60, ge=0)
    send_time_optimization: bool = Field(
        default_factory=lambda: settings.SEND_TIME_OPTIMIZATION_DEFAULT
    )
    # When set, this is a RETARGET campaign — engaged leads (clicked a link /
    # LinkedIn-connected) from the source are copied in on create, and
    # research_mode is forced to NONE (reuse existing leads).
    retarget_source_campaign_id: uuid.UUID | None = None

    _v_days = field_validator("schedule_days")(_validate_days)
    _v_tmpl = field_validator("template_subject", "template_body", "signature")(_blank_to_none)

    @model_validator(mode="after")
    def _check_time_order(self) -> "CampaignCreate":
        if self.schedule_time_start >= self.schedule_time_end:
            raise ValueError("schedule_time_start must be before schedule_time_end")
        return self

    @model_validator(mode="after")
    def _check_template(self) -> "CampaignCreate":
        if self.research_mode == ResearchMode.TEMPLATE and not self.template_body:
            raise ValueError("template_body is required when research_mode is 'template'")
        return self


class CampaignUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    goal: str | None = Field(default=None, min_length=1)
    tone: str | None = Field(default=None, min_length=1)
    sender_name: str | None = Field(default=None, min_length=1)
    sender_email: str | None = Field(default=None, min_length=1)
    research_mode: ResearchMode | None = None
    template_subject: str | None = None
    template_body: str | None = None
    signature: str | None = None
    sample_count: int | None = Field(default=None, ge=1)
    connected_account_id: uuid.UUID | None = None
    linkedin_account_id: uuid.UUID | None = None
    schedule_days: list[int] | None = None
    schedule_time_start: time | None = None
    schedule_time_end: time | None = None
    schedule_timezone: str | None = None
    max_per_hour: int | None = Field(default=None, ge=1)
    max_per_day: int | None = Field(default=None, ge=1)
    min_delay_seconds: int | None = Field(default=None, ge=0)
    send_time_optimization: bool | None = None

    _v_days = field_validator("schedule_days")(_validate_days)
    _v_tmpl = field_validator("template_subject", "template_body", "signature")(_blank_to_none)

    @model_validator(mode="after")
    def _check_time_order(self) -> "CampaignUpdate":
        if (
            self.schedule_time_start is not None
            and self.schedule_time_end is not None
            and self.schedule_time_start >= self.schedule_time_end
        ):
            raise ValueError("schedule_time_start must be before schedule_time_end")
        return self


# --------------------------------------------------------------------------
# Activity
# --------------------------------------------------------------------------


class RecentLeadEvent(BaseModel):
    lead_id: uuid.UUID
    email: str
    first_name: str | None
    last_name: str | None
    company: str | None
    research_status: str
    compose_status: str
    send_status: str
    scheduled_send_at: datetime | None
    updated_at: datetime


class SequenceStepEvent(BaseModel):
    lead_id: uuid.UUID
    email: str
    first_name: str | None
    last_name: str | None
    company: str | None
    node_kind: str
    result: str
    error: str | None
    attempted_at: datetime
    # Activity-tab view dedupes consecutive same-(lead, node) rows so the
    # list isn't polluted by 10 transient-retry skip rows in a row.
    # ``attempt_count`` is the size of the collapsed cluster (>=1; 1 means
    # a normal single row, >1 means we're showing only the latest of N
    # attempts and the UI should surface that fact).
    attempt_count: int = 1
    # First attempt in the cluster.  None when attempt_count == 1.
    earliest_attempted_at: datetime | None = None


class SequenceLeadStateInfo(BaseModel):
    lead_id: uuid.UUID
    email: str
    first_name: str | None
    last_name: str | None
    company: str | None
    status: str
    current_node_kind: str | None
    next_run_at: datetime | None
    halt_reason: str | None


class CampaignActivity(BaseModel):
    """Live snapshot of what the campaign is doing right now."""
    researching: int
    composing: int
    pending_send: int
    scheduled_send: int
    sent: int
    failed: int
    sequence_active: int
    sequence_halted: int
    # Halted specifically because the email is suppressed (bounce/unsub/spam/
    # blocked) — terminal, not re-enrollable; counted apart from sequence_halted.
    sequence_suppressed: int = 0
    sequence_completed: int
    sequence_pending: int
    # When the send window next opens (None = open right now)
    next_window_at: datetime | None
    # Estimated minutes until all pending leads are sent (None = unknown)
    estimated_minutes_remaining: int | None
    recent_events: list[RecentLeadEvent]
    recent_sequence_steps: list[SequenceStepEvent]
    halted_leads: list[SequenceLeadStateInfo]
    upcoming_steps: list[SequenceLeadStateInfo]


# --------------------------------------------------------------------------
# Response
# --------------------------------------------------------------------------


class CampaignResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    goal: str
    tone: str
    sender_name: str
    sender_email: str
    research_mode: ResearchMode
    template_subject: str | None
    template_body: str | None
    # Per-campaign signature OVERRIDE (None = inherit the account's).
    signature: str | None
    # Inherited signature from the bound connected account (Settings).
    account_signature: str | None = None
    sample_count: int
    connected_account_id: uuid.UUID | None
    connected_account: ConnectedAccountInfo | None
    connected_account_configured: bool
    # Whether sender_email is a real, non-placeholder address (gates launch).
    sender_ready: bool = True
    # True for retarget campaigns (identified by the "Retarget — " name prefix).
    is_retarget: bool = False
    linkedin_account_id: uuid.UUID | None
    linkedin_account_configured: bool
    schedule_days: list[int]
    schedule_time_start: time
    schedule_time_end: time
    schedule_timezone: str
    max_per_hour: int | None
    max_per_day: int | None
    min_delay_seconds: int
    status: CampaignStatus
    # Set when the campaign was auto-paused at the LinkedIn cap; the time it
    # will auto-resume.  NULL for manual pauses / running campaigns.
    auto_paused_until: datetime | None
    # Deliverability guard: send-time optimization toggle + circuit-breaker
    # pause marker (auto_paused_at/reason; requires a human Resume).
    send_time_optimization: bool
    auto_paused_at: datetime | None
    auto_pause_reason: str | None
    # Stamped on the most recent post-draft goal edit (NULL on campaigns
    # that have never had their goal rewritten).
    goal_updated_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    lead_counts: LeadCounts
    stats: CampaignStats


def campaign_to_dict(c: Any) -> dict[str, Any]:
    """Extract the campaign's own columns (without computed/nested fields)."""
    return {
        "id": c.id,
        "name": c.name,
        "goal": c.goal,
        "tone": c.tone,
        "sender_name": c.sender_name,
        "sender_email": c.sender_email,
        "research_mode": c.research_mode,
        "template_subject": c.template_subject,
        "template_body": c.template_body,
        "signature": c.signature,
        "sample_count": c.sample_count,
        "connected_account_id": c.connected_account_id,
        "linkedin_account_id": c.linkedin_account_id,
        "schedule_days": c.schedule_days,
        "schedule_time_start": c.schedule_time_start,
        "schedule_time_end": c.schedule_time_end,
        "schedule_timezone": c.schedule_timezone,
        "max_per_hour": c.max_per_hour,
        "max_per_day": c.max_per_day,
        "min_delay_seconds": c.min_delay_seconds,
        "status": c.status,
        "auto_paused_until": c.auto_paused_until,
        "send_time_optimization": c.send_time_optimization,
        "auto_paused_at": c.auto_paused_at,
        "auto_pause_reason": c.auto_pause_reason,
        "goal_updated_at": c.goal_updated_at,
        "created_at": c.created_at,
        "updated_at": c.updated_at,
    }
