from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models import CampaignStatus, ComposeStatus


class SamplePreview(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    lead_id: uuid.UUID
    email: str
    first_name: str | None
    last_name: str | None
    company: str | None
    research_quality: str
    research_summary: str
    composed_subject: str | None
    composed_body: str | None
    compose_status: ComposeStatus
    sample_approved: bool | None


class PreviewResponse(BaseModel):
    campaign_id: uuid.UUID
    status: CampaignStatus
    samples: list[SamplePreview]
    all_ready: bool


class SampleUpdateRequest(BaseModel):
    composed_subject: str | None = None
    composed_body: str | None = None
    approved: bool | None = None


class ApproveAllResponse(BaseModel):
    campaign_id: uuid.UUID
    status: CampaignStatus
    samples_approved: int
    leads_dispatched_to_send: int


class RejectResponse(BaseModel):
    campaign_id: uuid.UUID
    status: CampaignStatus
    leads_cleared: int


class PreviewProgress(BaseModel):
    total_leads: int
    researched: int
    composed: int
    sent: int
    failed: int
    # Leads currently mid-compose (compose_status == RUNNING).  Includes
    # both first-time composes and goal-change rewrites — the UI shows
    # this as a "(N composing)" sub-hint on the rewrite card.
    composing: int = 0
    # True when at least one lead still has research or compose work
    # pending/running — i.e. the AI pipeline is active and the
    # "Stop research & compose" button can do something.  False once
    # everything is composed (or terminally failed): the button greys out.
    pipeline_active: bool = False
    # When the goal was last edited on a non-draft campaign.  NULL means
    # the rewrite card stays hidden — nothing to track.
    goal_updated_at: datetime | None = None
    # Total leads in scope for the rewrite: unsent leads whose compose
    # status is DONE (some still on the old goal) or RUNNING (mid-rewrite).
    # Sent leads are frozen and don't count.
    rewrite_total: int = 0
    # How many of ``rewrite_total`` have caught up to the new goal
    # (compose_status == DONE AND updated_at >= goal_updated_at).
    rewrite_done: int = 0
