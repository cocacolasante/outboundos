"""CRM reporting endpoints (read-only aggregations).

Mounted under ``/crm/reports``.  Standard sales-CRM reports:

  GET /crm/reports/overview     — date-scoped dashboard: KPIs, won/lost
                                  monthly trend, current pipeline by
                                  stage, forecast by close month, loss
                                  reasons, activity breakdown, funnel.
  GET /crm/reports/deals        — closed-won / closed-lost / open / all
                                  deal detail rows (table + CSV export).
  GET /crm/reports/activities   — logged-activity detail rows.

Date semantics: won/lost figures are scoped by ``closed_at`` in the
[start, end] window; pipeline + forecast are a CURRENT snapshot of open
deals; leads/conversions/activities are scoped by their own timestamps.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import (
    CLOSED_STAGES,
    STAGE_DEFAULT_PROBABILITY,
    CrmActivity,
    CrmActivityType,
    Lead,
    Opportunity,
    OpportunityStage,
)
from app.schemas.reports import (
    ActivityBreakdown,
    ActivityReport,
    ActivityRow,
    ConversionFunnel,
    DealReport,
    DealRow,
    ForecastMonth,
    LossReasonRow,
    MonthlyWonLost,
    ReportKpis,
    ReportOverview,
    StagePipeline,
)

router = APIRouter(prefix="/crm/reports", tags=["reports"])

# Cap on detail-table rows returned in one call (UI table + CSV export).
_ROW_CAP = 2000
_DEFAULT_WINDOW_DAYS = 90


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_range(
    start: date | None, end: date | None,
) -> tuple[datetime, datetime, date, date]:
    """Resolve the report window.  Defaults to the last 90 days.  Returns
    (start_dt, end_exclusive_dt, start_date, end_date) — end is an
    inclusive calendar day, so the exclusive datetime is the following
    midnight."""
    end_d = end or _now().date()
    start_d = start or (end_d - timedelta(days=_DEFAULT_WINDOW_DAYS))
    if start_d > end_d:
        raise HTTPException(status_code=422, detail="start must be on or before end")
    start_dt = datetime.combine(start_d, time.min, tzinfo=timezone.utc)
    end_excl = datetime.combine(end_d + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return start_dt, end_excl, start_d, end_d


def _stage_probability(stage: OpportunityStage, probability: int | None) -> int:
    """The probability to weight a deal by — the explicit override, else
    the stage default."""
    if probability is not None:
        return probability
    return STAGE_DEFAULT_PROBABILITY.get(stage, 0)


def _weighted(amount, stage: OpportunityStage, probability: int | None) -> float:
    amt = float(amount or 0)
    return round(amt * _stage_probability(stage, probability) / 100.0, 2)


def _month_keys(start_d: date, end_d: date) -> list[str]:
    """Continuous list of ``YYYY-MM`` from start to end (inclusive) so
    trend charts have no gaps."""
    keys: list[str] = []
    y, m = start_d.year, start_d.month
    while (y, m) <= (end_d.year, end_d.month):
        keys.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return keys


# Canonical ratio metric — shared across all reporting surfaces.
from app.services.metrics import rate as _rate  # noqa: E402


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


@router.get("/overview", response_model=ReportOverview)
async def report_overview(
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> ReportOverview:
    start_dt, end_excl, start_d, end_d = _parse_range(start, end)

    # ---- closed deals in window (small set; aggregate in Python) ----
    closed_rows = (await db.execute(
        select(
            Opportunity.stage,
            Opportunity.amount,
            Opportunity.created_at,
            Opportunity.closed_at,
            Opportunity.loss_reason,
        ).where(
            Opportunity.stage.in_(CLOSED_STAGES),
            Opportunity.closed_at.is_not(None),
            Opportunity.closed_at >= start_dt,
            Opportunity.closed_at < end_excl,
        )
    )).all()

    won_count = won_value = 0
    lost_count = lost_value = 0
    cycle_days: list[float] = []
    month_keys = _month_keys(start_d, end_d)
    monthly = {
        k: {"won_count": 0, "won_value": 0.0, "lost_count": 0, "lost_value": 0.0}
        for k in month_keys
    }
    loss_reasons: dict[str, dict[str, float]] = {}

    for stage, amount, created_at, closed_at, loss_reason in closed_rows:
        amt = float(amount or 0)
        bucket = monthly.get(closed_at.strftime("%Y-%m"))
        if stage == OpportunityStage.CLOSED_WON:
            won_count += 1
            won_value += amt
            if bucket is not None:
                bucket["won_count"] += 1
                bucket["won_value"] += amt
            if created_at is not None:
                cycle_days.append(max(0.0, (closed_at - created_at).total_seconds() / 86400))
        else:  # CLOSED_LOST
            lost_count += 1
            lost_value += amt
            if bucket is not None:
                bucket["lost_count"] += 1
                bucket["lost_value"] += amt
            reason = (loss_reason or "").strip() or "Not specified"
            lr = loss_reasons.setdefault(reason, {"count": 0, "value": 0.0})
            lr["count"] += 1
            lr["value"] += amt

    # ---- open pipeline snapshot (current; not date-scoped) ----
    open_rows = (await db.execute(
        select(
            Opportunity.stage,
            Opportunity.amount,
            Opportunity.probability,
            Opportunity.close_date,
        ).where(Opportunity.stage.not_in(CLOSED_STAGES))
    )).all()

    stage_agg = {
        s: {"count": 0, "total": 0.0, "weighted": 0.0}
        for s in OpportunityStage if s not in CLOSED_STAGES
    }
    forecast: dict[str, dict[str, float]] = {}
    open_count = 0
    open_value = 0.0
    open_weighted = 0.0
    for stage, amount, probability, close_date in open_rows:
        amt = float(amount or 0)
        w = _weighted(amount, stage, probability)
        open_count += 1
        open_value += amt
        open_weighted += w
        sa = stage_agg.get(stage)
        if sa is not None:
            sa["count"] += 1
            sa["total"] += amt
            sa["weighted"] += w
        fkey = close_date.strftime("%Y-%m") if close_date else "unscheduled"
        fc = forecast.setdefault(fkey, {"count": 0, "total": 0.0, "weighted": 0.0})
        fc["count"] += 1
        fc["total"] += amt
        fc["weighted"] += w

    # ---- period counts ----
    deals_created = (await db.execute(
        select(func.count()).select_from(Opportunity).where(
            Opportunity.created_at >= start_dt, Opportunity.created_at < end_excl,
        )
    )).scalar_one()
    conversions = (await db.execute(
        select(func.count()).select_from(Opportunity).where(
            Opportunity.created_at >= start_dt, Opportunity.created_at < end_excl,
            Opportunity.source_lead_id.is_not(None),
        )
    )).scalar_one()
    new_leads = (await db.execute(
        select(func.count()).select_from(Lead).where(
            Lead.created_at >= start_dt, Lead.created_at < end_excl,
        )
    )).scalar_one()

    # ---- activity breakdown ----
    act_type_rows = (await db.execute(
        select(CrmActivity.activity_type, func.count()).where(
            CrmActivity.occurred_at >= start_dt, CrmActivity.occurred_at < end_excl,
        ).group_by(CrmActivity.activity_type)
    )).all()
    by_type = {t.value: n for t, n in act_type_rows}
    activities_total = sum(by_type.values())

    act_dir_rows = (await db.execute(
        select(CrmActivity.direction, func.count()).where(
            CrmActivity.occurred_at >= start_dt, CrmActivity.occurred_at < end_excl,
            CrmActivity.direction.is_not(None),
        ).group_by(CrmActivity.direction)
    )).all()
    by_direction = {d.value: n for d, n in act_dir_rows if d is not None}

    agent_generated = (await db.execute(
        select(func.count()).select_from(CrmActivity).where(
            CrmActivity.occurred_at >= start_dt, CrmActivity.occurred_at < end_excl,
            CrmActivity.is_agent_generated.is_(True),
        )
    )).scalar_one()

    kpis = ReportKpis(
        won_count=won_count,
        won_value=round(won_value, 2),
        lost_count=lost_count,
        lost_value=round(lost_value, 2),
        win_rate=_rate(won_count, won_count + lost_count),
        avg_deal_size=round(won_value / won_count, 2) if won_count else None,
        avg_sales_cycle_days=round(sum(cycle_days) / len(cycle_days), 1) if cycle_days else None,
        open_count=open_count,
        open_value=round(open_value, 2),
        open_weighted_value=round(open_weighted, 2),
        deals_created=deals_created,
        new_leads=new_leads,
        conversions=conversions,
        activities_logged=activities_total,
    )

    return ReportOverview(
        start=start_d,
        end=end_d,
        kpis=kpis,
        won_lost_monthly=[
            MonthlyWonLost(
                month=k,
                won_count=monthly[k]["won_count"],
                won_value=round(monthly[k]["won_value"], 2),
                lost_count=monthly[k]["lost_count"],
                lost_value=round(monthly[k]["lost_value"], 2),
            )
            for k in month_keys
        ],
        pipeline_by_stage=[
            StagePipeline(
                stage=s,
                count=stage_agg[s]["count"],
                total_amount=round(stage_agg[s]["total"], 2),
                weighted_amount=round(stage_agg[s]["weighted"], 2),
            )
            for s in OpportunityStage if s not in CLOSED_STAGES
        ],
        forecast=[
            ForecastMonth(
                month=k,
                count=int(forecast[k]["count"]),
                total_amount=round(forecast[k]["total"], 2),
                weighted_amount=round(forecast[k]["weighted"], 2),
            )
            # Dated months ascending, then the unscheduled bucket last.
            for k in (
                sorted(x for x in forecast if x != "unscheduled")
                + (["unscheduled"] if "unscheduled" in forecast else [])
            )
        ],
        loss_reasons=sorted(
            (
                LossReasonRow(reason=r, count=int(v["count"]), value=round(v["value"], 2))
                for r, v in loss_reasons.items()
            ),
            key=lambda x: (-x.count, -x.value),
        ),
        activity_breakdown=ActivityBreakdown(
            by_type=by_type,
            by_direction=by_direction,
            agent_generated=agent_generated,
            human_logged=activities_total - agent_generated,
            total=activities_total,
        ),
        funnel=ConversionFunnel(
            new_leads=new_leads,
            opportunities_created=deals_created,
            won=won_count,
            lead_to_opp_rate=_rate(deals_created, new_leads),
            opp_to_won_rate=_rate(won_count, deals_created),
        ),
    )


# ---------------------------------------------------------------------------
# Deal detail rows
# ---------------------------------------------------------------------------


def _deal_row(opp: Opportunity) -> DealRow:
    name = " ".join(x for x in [opp.first_name, opp.last_name] if x) or None
    if opp.closed_at is not None:
        age = max(0, (opp.closed_at - opp.created_at).days)
    else:
        age = max(0, (_now() - opp.created_at).days)
    return DealRow(
        id=opp.id,
        name=opp.name,
        stage=opp.stage,
        amount=float(opp.amount) if opp.amount is not None else None,
        probability=opp.probability,
        weighted_amount=_weighted(opp.amount, opp.stage, opp.probability),
        company=opp.company,
        contact_name=name,
        email=opp.email,
        created_at=opp.created_at,
        closed_at=opp.closed_at,
        close_date=opp.close_date,
        age_days=age,
        loss_reason=opp.loss_reason,
    )


@router.get("/deals", response_model=DealReport)
async def report_deals(
    outcome: str = Query(default="won", pattern="^(won|lost|open|all)$"),
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> DealReport:
    """Deal detail rows for the report tables + CSV export.

    ``won``/``lost`` are scoped by ``closed_at`` in the window and
    ordered most-recently-closed first; ``open``/``all`` ignore the date
    window (a snapshot) and order by created_at desc.
    """
    start_dt, end_excl, start_d, end_d = _parse_range(start, end)

    q = select(Opportunity)
    if outcome == "won":
        q = q.where(
            Opportunity.stage == OpportunityStage.CLOSED_WON,
            Opportunity.closed_at.is_not(None),
            Opportunity.closed_at >= start_dt, Opportunity.closed_at < end_excl,
        ).order_by(Opportunity.closed_at.desc())
    elif outcome == "lost":
        q = q.where(
            Opportunity.stage == OpportunityStage.CLOSED_LOST,
            Opportunity.closed_at.is_not(None),
            Opportunity.closed_at >= start_dt, Opportunity.closed_at < end_excl,
        ).order_by(Opportunity.closed_at.desc())
    elif outcome == "open":
        q = q.where(Opportunity.stage.not_in(CLOSED_STAGES)).order_by(
            Opportunity.created_at.desc()
        )
    else:  # all
        q = q.order_by(Opportunity.created_at.desc())

    total = (await db.execute(
        select(func.count()).select_from(q.subquery())
    )).scalar_one()
    rows = (await db.execute(q.limit(_ROW_CAP))).scalars().all()
    items = [_deal_row(o) for o in rows]
    return DealReport(
        outcome=outcome,
        start=start_d,
        end=end_d,
        count=total,
        total_amount=round(sum(i.amount or 0 for i in items), 2),
        items=items,
        truncated=total > len(items),
    )


# ---------------------------------------------------------------------------
# Activity detail rows
# ---------------------------------------------------------------------------


@router.get("/activities", response_model=ActivityReport)
async def report_activities(
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    activity_type: CrmActivityType | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> ActivityReport:
    start_dt, end_excl, start_d, end_d = _parse_range(start, end)

    filters = [
        CrmActivity.occurred_at >= start_dt,
        CrmActivity.occurred_at < end_excl,
    ]
    if activity_type is not None:
        filters.append(CrmActivity.activity_type == activity_type)

    total = (await db.execute(
        select(func.count()).select_from(CrmActivity).where(*filters)
    )).scalar_one()
    rows = (await db.execute(
        select(CrmActivity).where(*filters)
        .order_by(CrmActivity.occurred_at.desc())
        .limit(_ROW_CAP)
    )).scalars().all()

    # Resolve opportunity names + lead/opp contact in batch.
    opp_ids = {a.opportunity_id for a in rows if a.opportunity_id}
    lead_ids = {a.lead_id for a in rows if a.lead_id}
    opps: dict[uuid.UUID, Opportunity] = {}
    leads: dict[uuid.UUID, Lead] = {}
    if opp_ids:
        for o in (await db.execute(
            select(Opportunity).where(Opportunity.id.in_(opp_ids))
        )).scalars():
            opps[o.id] = o
    if lead_ids:
        for ld in (await db.execute(
            select(Lead).where(Lead.id.in_(lead_ids))
        )).scalars():
            leads[ld.id] = ld

    items: list[ActivityRow] = []
    for a in rows:
        opp = opps.get(a.opportunity_id)
        lead = leads.get(a.lead_id)
        contact = None
        if opp is not None:
            contact = opp.email or (
                " ".join(x for x in [opp.first_name, opp.last_name] if x) or None
            )
        if contact is None and lead is not None:
            contact = lead.email
        items.append(ActivityRow(
            id=a.id,
            activity_type=a.activity_type.value,
            direction=a.direction.value if a.direction else None,
            subject=a.subject,
            occurred_at=a.occurred_at,
            due_at=a.due_at,
            completed_at=a.completed_at,
            is_agent_generated=a.is_agent_generated,
            sentiment=a.sentiment,
            lead_id=a.lead_id,
            opportunity_id=a.opportunity_id,
            opportunity_name=opp.name if opp else None,
            contact=contact,
        ))

    return ActivityReport(
        start=start_d,
        end=end_d,
        count=total,
        items=items,
        truncated=total > len(items),
    )
