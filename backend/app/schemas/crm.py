"""Pydantic schemas for the CRM layer (opportunities + activities +
manual lead creation + lead conversion)."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import (
    CrmActivityDirection,
    CrmActivityType,
    CrmLeadStatus,
    OpportunityStage,
)


def _blank_to_none(v: str | None) -> str | None:
    if v is None:
        return None
    return v if v.strip() else None


# ============================================================================
# Manual lead creation
# ============================================================================

class LeadCreate(BaseModel):
    """Manually create a CRM lead (no campaign, no CSV)."""
    email: str = Field(min_length=3, max_length=320)
    first_name: str | None = Field(default=None, max_length=200)
    last_name: str | None = Field(default=None, max_length=200)
    company: str | None = Field(default=None, max_length=300)
    job_title: str | None = Field(default=None, max_length=300)
    phone: str | None = Field(default=None, max_length=50)
    linkedin_url: str | None = Field(default=None, max_length=500)
    company_website: str | None = Field(default=None, max_length=500)
    notes: str | None = None
    crm_status: CrmLeadStatus = CrmLeadStatus.NEW

    _v_blank = field_validator(
        "first_name", "last_name", "company", "job_title", "phone",
        "linkedin_url", "company_website", "notes",
    )(_blank_to_none)

    @field_validator("email")
    @classmethod
    def _email_shape(cls, v: str) -> str:
        s = (v or "").strip().lower()
        if "@" not in s or "." not in s.split("@")[-1]:
            raise ValueError("invalid email address")
        return s


class LeadCrmUpdate(BaseModel):
    """Update the CRM-facing lead fields: status + editable contact info.

    PATCH semantics — only fields actually sent are changed (the endpoint
    uses ``exclude_unset``).  Blank contact strings clear the field; a
    blank email is rejected only when a non-empty value is sent."""
    crm_status: CrmLeadStatus | None = None
    email: str | None = Field(default=None, max_length=320)
    first_name: str | None = Field(default=None, max_length=200)
    last_name: str | None = Field(default=None, max_length=200)
    company: str | None = Field(default=None, max_length=300)
    job_title: str | None = Field(default=None, max_length=300)
    phone: str | None = Field(default=None, max_length=50)
    linkedin_url: str | None = Field(default=None, max_length=500)
    company_website: str | None = Field(default=None, max_length=500)
    notes: str | None = None

    _v_blank = field_validator(
        "first_name", "last_name", "company", "job_title", "phone",
        "linkedin_url", "company_website",
    )(_blank_to_none)

    @field_validator("email")
    @classmethod
    def _email_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        s = v.strip().lower()
        if not s:
            return None            # explicit clear → no email (LinkedIn-only lead)
        if "@" not in s or "." not in s.split("@")[-1]:
            raise ValueError("invalid email address")
        return s


# ============================================================================
# Lead conversion
# ============================================================================

class ConvertLeadRequest(BaseModel):
    """Convert a lead into an opportunity.  All fields optional — the
    contact snapshot defaults from the lead; the name defaults to
    '<Company or Name> — <today>'."""
    name: str | None = Field(default=None, max_length=300)
    amount: float | None = Field(default=None, ge=0)
    close_date: date | None = None
    stage: OpportunityStage = OpportunityStage.QUALIFICATION

    _v_blank = field_validator("name")(_blank_to_none)


# ============================================================================
# Opportunities
# ============================================================================

class OpportunityCreate(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    stage: OpportunityStage = OpportunityStage.PROSPECTING
    amount: float | None = Field(default=None, ge=0)
    close_date: date | None = None
    probability: int | None = Field(default=None, ge=0, le=100)
    description: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    company: str | None = None
    job_title: str | None = None
    linkedin_url: str | None = None

    _v_blank = field_validator(
        "description", "first_name", "last_name", "email", "phone",
        "company", "job_title", "linkedin_url",
    )(_blank_to_none)


class OpportunityUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=300)
    stage: OpportunityStage | None = None
    amount: float | None = Field(default=None, ge=0)
    close_date: date | None = None
    probability: int | None = Field(default=None, ge=0, le=100)
    description: str | None = None
    loss_reason: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    company: str | None = None
    job_title: str | None = None
    linkedin_url: str | None = None


class OpportunityResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    stage: OpportunityStage
    amount: float | None
    close_date: date | None
    probability: int | None
    description: str | None
    closed_at: datetime | None
    loss_reason: str | None
    first_name: str | None
    last_name: str | None
    email: str | None
    phone: str | None
    company: str | None
    job_title: str | None
    linkedin_url: str | None
    source_lead_id: uuid.UUID | None
    # Phase 1 graph (migration 0038) — all nullable / design-ready.
    pipeline_id: uuid.UUID | None = None
    stage_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime
    # Denormalised roll-up for list cards.
    activity_count: int = 0
    open_task_count: int = 0


class PaginatedOpportunities(BaseModel):
    items: list[OpportunityResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


class PipelineSummary(BaseModel):
    """Kanban roll-up: per-stage count + amount totals so the board
    header can show '3 deals · $42k' without summing client-side."""
    stage: OpportunityStage
    count: int
    total_amount: float


# ---------------------------------------------------------------------------
# Phase 2: configurable stages (board columns) + stage-change audit
# ---------------------------------------------------------------------------


class StageResponse(BaseModel):
    """A configurable pipeline stage — drives the Kanban columns."""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key: str
    name: str
    sort_order: int
    default_probability: int | None
    is_won: bool
    is_lost: bool


class PipelineResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    is_default: bool
    stages: list[StageResponse]


class StageChangeResponse(BaseModel):
    """One row of an opportunity's stage-move audit trail."""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    opportunity_id: uuid.UUID
    from_stage_key: str | None
    to_stage_key: str | None
    source: str
    changed_by: uuid.UUID | None
    note: str | None
    created_at: datetime


class ConvertLeadResponse(BaseModel):
    opportunity: OpportunityResponse
    lead_id: uuid.UUID
    lead_crm_status: CrmLeadStatus


# ============================================================================
# Activities
# ============================================================================

class ActivityCreate(BaseModel):
    """Log a manual touch against a lead and/or an opportunity.  At
    least one parent is required (matches the DB CHECK constraint)."""
    lead_id: uuid.UUID | None = None
    opportunity_id: uuid.UUID | None = None
    activity_type: CrmActivityType
    subject: str = Field(min_length=1, max_length=500)
    body: str | None = None
    direction: CrmActivityDirection | None = None
    due_at: datetime | None = None
    occurred_at: datetime | None = None

    _v_blank = field_validator("body")(_blank_to_none)


class ActivityUpdate(BaseModel):
    subject: str | None = Field(default=None, min_length=1, max_length=500)
    body: str | None = None
    direction: CrmActivityDirection | None = None
    due_at: datetime | None = None
    occurred_at: datetime | None = None
    # True → stamp completed_at now; False → clear it (reopen the task).
    completed: bool | None = None


class ActivityResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    lead_id: uuid.UUID | None
    opportunity_id: uuid.UUID | None
    activity_type: CrmActivityType
    subject: str
    body: str | None
    direction: CrmActivityDirection | None
    due_at: datetime | None
    completed_at: datetime | None
    occurred_at: datetime
    created_at: datetime
    # Agent fields (migration 0027) — lets the UI badge automated rows.
    sentiment: str | None = None
    is_agent_generated: bool = False


class PaginatedActivities(BaseModel):
    items: list[ActivityResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


# ============================================================================
# Documents
# ============================================================================

class DocumentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    opportunity_id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    uploaded_at: datetime


# ============================================================================
# Products of interest
# ============================================================================

class ProductCreate(BaseModel):
    product_name: str = Field(min_length=1, max_length=300)
    quantity: float = Field(default=1, gt=0)
    unit_price: float | None = Field(default=None, ge=0)
    notes: str | None = None

    _v_blank = field_validator("notes")(_blank_to_none)


class ProductUpdate(BaseModel):
    product_name: str | None = Field(default=None, min_length=1, max_length=300)
    quantity: float | None = Field(default=None, gt=0)
    unit_price: float | None = Field(default=None, ge=0)
    notes: str | None = None


class ProductResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    opportunity_id: uuid.UUID
    product_name: str
    quantity: float
    unit_price: float | None
    notes: str | None
    created_at: datetime
    # Derived: quantity x unit_price (None when no price set).
    line_total: float | None = None


class ProductListResponse(BaseModel):
    items: list[ProductResponse]
    # Sum of priced line totals — shown next to the manual deal amount
    # as a sanity hint ("products add up to $X").
    products_total: float
