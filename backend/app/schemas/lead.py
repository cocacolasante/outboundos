from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models import ComposeStatus, LinkedInConnectionStatus, ResearchStatus, SendStatus


class LeadSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    # None for manually-created CRM leads (no campaign).
    campaign_id: uuid.UUID | None
    # None for LinkedIn-only leads staged by signal enrichment.
    email: str | None = None
    first_name: str | None
    last_name: str | None
    company: str | None
    job_title: str | None
    research_status: ResearchStatus
    compose_status: ComposeStatus
    send_status: SendStatus
    is_sample: bool
    sample_approved: bool | None
    scheduled_send_at: datetime | None
    created_at: datetime
    # CRM-lite extras (kept here so the global Leads list can show them).
    notes: str | None = None
    has_notes: bool = False
    campaign_name: str | None = None
    # CRM fields (migration 0025).
    crm_status: str = "new"
    converted_opportunity_id: uuid.UUID | None = None
    # Where the lead is in the campaign's sequence right now.
    #   sequence_status: active | halted | completed | pending | None
    #   sequence_stage:  human label of the current step (node title / kind),
    #                    or a status word (Completed / Halted / Not started).
    sequence_status: str | None = None
    sequence_stage: str | None = None


class LeadEmailUpdate(BaseModel):
    """Edit a lead's recipient, composed email and/or notes."""
    email: str | None = None
    composed_subject: str | None = None
    composed_body: str | None = None
    notes: str | None = None


class ReplyPreviewNode(BaseModel):
    """A reply step the user can preview for a lead.  Lets the UI offer a
    picker when a sequence has more than one ``email_reply`` node."""
    node_id: uuid.UUID
    title: str | None = None
    ai_compose: bool = False
    ai_prompt: str | None = None


class ReplyPreviewResponse(BaseModel):
    """A composed-but-NOT-sent draft of an ``email_reply`` node for one lead,
    so the user can review follow-up copy before it goes out.

    Manual-template replies preview exactly.  AI replies are regenerated
    fresh at send time (``regenerated_at_send``), so the preview is
    representative — the wording the sequencer ultimately sends may differ."""
    node_id: uuid.UUID
    title: str | None = None
    ai_compose: bool
    ai_prompt: str | None = None
    subject: str
    body: str
    regenerated_at_send: bool
    # False when the lead's first email hasn't been composed/sent yet, so
    # there's no original subject to build "Re:" from and (for AI) no prior
    # email body to ground the reply.  The UI shows a caveat.
    has_original_email: bool
    # Every reply step in the sequence so the UI can offer a node picker.
    available_nodes: list[ReplyPreviewNode]


class LeadResponse(LeadSummary):
    """Per-lead detail, including the composed email and research blob."""
    phone: str | None
    linkedin_url: str | None
    raw_csv_row: dict[str, Any] | None
    research_data: dict[str, Any] | None
    composed_subject: str | None
    composed_body: str | None
    style_correction: str | None
    brevo_message_id: str | None
    updated_at: datetime


class LeadHistoryItem(BaseModel):
    """One row on the lead's activity timeline.

    Two source tables feed this:
    - ``LeadStepExecution`` rows (every sequence step we attempted —
      email sends, LinkedIn views/connects/DMs, etc.).  Carries the
      result enum (``sent``, ``skipped``, ``failed``...) and any
      ``external_id`` (Brevo message_id, LinkedIn invitation_id).
    - ``EmailEvent`` rows (delivered / opened / clicked / replied /
      bounced / spam / unsubscribed — what the recipient did).

    The endpoint merges both into one chronologically-sorted list so
    the UI can render a single timeline without doing the merge
    client-side.
    """
    at: datetime
    kind: str           # "execution" | "event" — discriminator for the UI
    action: str         # human-readable label ("Sent email", "Opened email")
    status: str         # success-y / warn-y / fail-y — UI colour cue
    icon: str           # emoji hint so the UI has a default visual
    detail: str | None = None  # error message, sequence node label, etc.
    external_id: str | None = None  # Brevo message_id or LinkedIn invitation_id


class LeadDetail(LeadResponse):
    """Full per-lead view with all the fields the CRM modal renders.

    Adds the LinkedIn outreach state, the activity timeline, and a
    distilled ``research_summary`` so the UI doesn't have to dig
    through the raw ``research_data`` JSONB."""
    linkedin_connection_status: LinkedInConnectionStatus | None = None
    linkedin_last_reply_at: datetime | None = None
    company_website: str | None = None
    company_name: str | None = None  # alias of ``company`` for symmetry with research blobs

    history: list[LeadHistoryItem]
    # Lightweight roll-ups for the UI header pills.
    history_counts: dict[str, int]  # {"sent": 3, "opened": 1, "replied": 0, ...}
    research_summary: dict[str, Any]  # {industry, person_news, company_news, ...}
    # True when the lead's email is on the workspace suppression list —
    # the bulk send pipeline blocks them and the sequencer beat will
    # never advance them.  UI shows a "Suppressed" badge + disables the
    # Ignore button.
    is_suppressed: bool = False
    # Why they're suppressed when ``is_suppressed`` is True (manual,
    # unsubscribed, hard_bounce, spam).  None when not suppressed.
    suppression_reason: str | None = None


class IgnoreLeadResponse(BaseModel):
    """Result of POST ``/leads/{id}/ignore`` — how many lead rows we
    halted (could be more than one if the same email appears in multiple
    campaigns) + which campaigns those rows belonged to."""
    suppressed: bool
    already_suppressed: bool
    leads_halted: int
    campaigns_affected: list[uuid.UUID]


class PaginatedLeads(BaseModel):
    items: list[LeadSummary]
    total: int
    page: int
    page_size: int
    total_pages: int


class UploadPreviewResponse(BaseModel):
    columns: list[str]
    preview_rows: list[dict[str, str]]
    suggested_mapping: dict[str, str]
    total_rows: int


class ConfirmUploadResponse(BaseModel):
    total: int
    suppressed: int
    duplicates_removed: int
    samples_selected: int
    # True when the campaign launched straight into RUNNING (its start node
    # isn't an email, so there are no sample emails to preview/approve).
    auto_launched: bool = False


class AddLeadsToCampaignRequest(BaseModel):
    """Bulk-add existing leads (CRM/manual, lookalike-accepted, or leads
    from another campaign) into a target campaign."""
    lead_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)


class AddLeadsToCampaignResponse(BaseModel):
    added: int
    skipped_duplicate: int
    skipped_suppressed: int
    skipped_missing: int
    # True when research/compose was kicked off immediately (non-draft
    # campaigns).  Draft campaigns process added leads at launch.
    research_started: bool = False
