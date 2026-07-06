"""Pydantic schemas for the CRM reporting layer.

Standard sales-CRM reports: a date-scoped overview (KPIs + chart
aggregations) plus detail-row endpoints for closed-won / closed-lost /
open deals and logged activities (the rows the UI tables + CSV export
read from).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel

from app.models import OpportunityStage


# ---------------------------------------------------------------------------
# Overview dashboard
# ---------------------------------------------------------------------------


class ReportKpis(BaseModel):
    """Headline numbers for the report window.

    Won/lost are scoped by ``closed_at`` in [start, end].  Open figures
    are a CURRENT snapshot (pipeline is "where things stand now", not a
    historical slice).  Created/leads/conversions/activities are scoped
    by their own timestamps within the window.
    """
    won_count: int
    won_value: float
    lost_count: int
    lost_value: float
    win_rate: float | None          # won / (won + lost)
    avg_deal_size: float | None     # won_value / won_count
    avg_sales_cycle_days: float | None  # avg(closed_at - created_at), won deals
    open_count: int
    open_value: float
    open_weighted_value: float      # Σ amount × probability
    deals_created: int
    new_leads: int
    conversions: int
    activities_logged: int


class MonthlyWonLost(BaseModel):
    month: str          # YYYY-MM
    won_count: int
    won_value: float
    lost_count: int
    lost_value: float


class StagePipeline(BaseModel):
    """Current open-pipeline snapshot per stage (closed stages excluded)."""
    stage: OpportunityStage
    count: int
    total_amount: float
    weighted_amount: float


class ForecastMonth(BaseModel):
    """Open deals grouped by expected close month.  ``month`` is
    ``"unscheduled"`` for open deals with no close_date."""
    month: str
    count: int
    total_amount: float
    weighted_amount: float


class LossReasonRow(BaseModel):
    reason: str
    count: int
    value: float


class ActivityBreakdown(BaseModel):
    by_type: dict[str, int]
    by_direction: dict[str, int]
    agent_generated: int
    human_logged: int
    total: int


class ConversionFunnel(BaseModel):
    """Period activity, top-of-funnel to closed.  Counts are scoped to
    the window by each step's own timestamp (so this is period
    throughput, not a strict single-cohort funnel)."""
    new_leads: int
    opportunities_created: int
    won: int
    lead_to_opp_rate: float | None
    opp_to_won_rate: float | None


class ReportOverview(BaseModel):
    start: date
    end: date
    kpis: ReportKpis
    won_lost_monthly: list[MonthlyWonLost]
    pipeline_by_stage: list[StagePipeline]
    forecast: list[ForecastMonth]
    loss_reasons: list[LossReasonRow]
    activity_breakdown: ActivityBreakdown
    funnel: ConversionFunnel


# ---------------------------------------------------------------------------
# Detail rows (tables + CSV export)
# ---------------------------------------------------------------------------


class DealRow(BaseModel):
    id: uuid.UUID
    name: str
    stage: OpportunityStage
    amount: float | None
    probability: int | None
    weighted_amount: float
    company: str | None
    contact_name: str | None
    email: str | None
    created_at: datetime
    closed_at: datetime | None
    close_date: date | None
    age_days: int | None        # created→closed (closed deals) or created→now (open)
    loss_reason: str | None


class DealReport(BaseModel):
    outcome: str                # won | lost | open | all
    start: date
    end: date
    count: int
    total_amount: float
    items: list[DealRow]
    truncated: bool             # True when more rows exist than the cap


class ActivityRow(BaseModel):
    id: uuid.UUID
    activity_type: str
    direction: str | None
    subject: str
    occurred_at: datetime
    due_at: datetime | None
    completed_at: datetime | None
    is_agent_generated: bool
    sentiment: str | None
    lead_id: uuid.UUID | None
    opportunity_id: uuid.UUID | None
    opportunity_name: str | None
    contact: str | None         # lead/opp email or name for the row


class ActivityReport(BaseModel):
    start: date
    end: date
    count: int
    items: list[ActivityRow]
    truncated: bool
