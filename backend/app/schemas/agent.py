"""Pydantic schemas for the agent surface (settings, notifications,
audit log, reply triage feed)."""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models import AgentActionStatus, AgentActionType, NotificationKind


# ============================================================================
# Agent settings (runtime singleton)
# ============================================================================

class AgentSettingsResponse(BaseModel):
    auto_log_replies: bool
    auto_create_convert_reminders: bool
    auto_draft_replies: bool
    stale_opp_nudges_enabled: bool
    daily_digest_enabled: bool
    notify_on_positive_reply: bool
    notify_on_any_reply: bool
    min_confidence_to_act: float
    quiet_hours_start_utc: int | None
    quiet_hours_end_utc: int | None
    # Read-only deployment facts so the UI can be honest about what
    # will actually happen (these come from env, not the DB row).
    agent_enabled: bool
    owner_email_configured: bool
    updated_at: datetime


class AgentSettingsUpdate(BaseModel):
    auto_log_replies: bool | None = None
    auto_create_convert_reminders: bool | None = None
    auto_draft_replies: bool | None = None
    stale_opp_nudges_enabled: bool | None = None
    daily_digest_enabled: bool | None = None
    notify_on_positive_reply: bool | None = None
    notify_on_any_reply: bool | None = None
    min_confidence_to_act: float | None = Field(default=None, ge=0.0, le=1.0)
    quiet_hours_start_utc: int | None = Field(default=None, ge=0, le=23)
    quiet_hours_end_utc: int | None = Field(default=None, ge=0, le=23)
    # Explicitly clear quiet hours (None in the fields above means
    # "don't change"; this flag means "set both to NULL").
    clear_quiet_hours: bool = False


# ============================================================================
# Notifications
# ============================================================================

class NotificationResponse(BaseModel):
    id: uuid.UUID
    kind: NotificationKind
    title: str
    body: str | None
    lead_id: uuid.UUID | None
    opportunity_id: uuid.UUID | None
    activity_id: uuid.UUID | None
    read_at: datetime | None
    emailed_at: datetime | None
    created_at: datetime
    # Linked-record summaries for rendering without extra requests.
    lead_email: str | None = None
    lead_name: str | None = None
    opportunity_name: str | None = None


class PaginatedNotifications(BaseModel):
    items: list[NotificationResponse]
    total: int
    unread: int
    page: int
    page_size: int
    total_pages: int


# ============================================================================
# Audit log
# ============================================================================

class AgentActionResponse(BaseModel):
    id: uuid.UUID
    action_type: AgentActionType
    status: AgentActionStatus
    summary: str
    lead_id: uuid.UUID | None
    opportunity_id: uuid.UUID | None
    activity_id: uuid.UUID | None
    detail: dict | None
    model: str | None
    cost_usd: float | None
    created_at: datetime


class PaginatedAgentActions(BaseModel):
    items: list[AgentActionResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


# ============================================================================
# Reply triage feed
# ============================================================================

class ReplyFeedItem(BaseModel):
    """One inbound reply in the triage feed — the agent-logged email
    activity enriched with lead/deal context and convert eligibility."""
    activity_id: uuid.UUID
    lead_id: uuid.UUID | None
    opportunity_id: uuid.UUID | None
    sentiment: str | None
    subject: str
    body_preview: str | None
    occurred_at: datetime
    lead_email: str | None = None
    lead_name: str | None = None
    lead_company: str | None = None
    # True when the lead exists and hasn't been converted — powers the
    # one-click Convert button (which calls the EXISTING
    # POST /crm/leads/{id}/convert; the agent never converts).
    convert_eligible: bool = False
    converted: bool = False
    # Suggested reply draft (Phase 6 — populated from the draft_reply
    # audit row when auto_draft_replies is on).
    draft_body: str | None = None


class PaginatedReplies(BaseModel):
    items: list[ReplyFeedItem]
    total: int
    page: int
    page_size: int
    total_pages: int
