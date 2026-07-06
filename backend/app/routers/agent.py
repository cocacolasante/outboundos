"""Agent endpoints: runtime settings, notification feed, audit log, and
the reply triage feed.

Deliberately read/toggle-only — the agent's autonomous writes all
happen in workers/services.  The one "action" a user takes from the
triage feed (converting a lead) goes through the EXISTING
``POST /crm/leads/{id}/convert`` so conversion stays a human action
with a single implementation.
"""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import (
    AgentAction,
    AgentActionType,
    CrmActivity,
    CrmActivityDirection,
    CrmActivityType,
    Lead,
    Notification,
    Opportunity,
)
from app.schemas.agent import (
    AgentActionResponse,
    AgentSettingsResponse,
    AgentSettingsUpdate,
    NotificationResponse,
    PaginatedAgentActions,
    PaginatedNotifications,
    PaginatedReplies,
    ReplyFeedItem,
)
from app.services import agent_core

router = APIRouter(prefix="/agent", tags=["agent"])


def _settings_response(row) -> AgentSettingsResponse:
    return AgentSettingsResponse(
        auto_log_replies=row.auto_log_replies,
        auto_create_convert_reminders=row.auto_create_convert_reminders,
        auto_draft_replies=row.auto_draft_replies,
        stale_opp_nudges_enabled=row.stale_opp_nudges_enabled,
        daily_digest_enabled=row.daily_digest_enabled,
        notify_on_positive_reply=row.notify_on_positive_reply,
        notify_on_any_reply=row.notify_on_any_reply,
        min_confidence_to_act=float(row.min_confidence_to_act),
        quiet_hours_start_utc=row.quiet_hours_start_utc,
        quiet_hours_end_utc=row.quiet_hours_end_utc,
        agent_enabled=settings.AGENT_ENABLED,
        owner_email_configured=bool(settings.OWNER_NOTIFY_EMAIL),
        updated_at=row.updated_at,
    )


@router.get("/settings", response_model=AgentSettingsResponse)
async def get_settings(db: AsyncSession = Depends(get_db)) -> AgentSettingsResponse:
    row = await agent_core.get_agent_settings(db)
    await db.commit()  # persists the bootstrap row on first read
    return _settings_response(row)


@router.patch("/settings", response_model=AgentSettingsResponse)
async def update_settings(
    payload: AgentSettingsUpdate,
    db: AsyncSession = Depends(get_db),
) -> AgentSettingsResponse:
    row = await agent_core.get_agent_settings(db)

    data = payload.model_dump(exclude_unset=True)
    clear_quiet = data.pop("clear_quiet_hours", False)
    for field, value in data.items():
        if value is None:
            continue
        if field == "min_confidence_to_act":
            value = Decimal(str(value))
        setattr(row, field, value)
    if clear_quiet:
        row.quiet_hours_start_utc = None
        row.quiet_hours_end_utc = None

    # Quiet hours must be set as a pair.
    if (row.quiet_hours_start_utc is None) != (row.quiet_hours_end_utc is None):
        raise HTTPException(
            status_code=422,
            detail="quiet_hours_start_utc and quiet_hours_end_utc must be set together",
        )

    await db.commit()
    await db.refresh(row)
    return _settings_response(row)


# ============================================================================
# Notifications
# ============================================================================


async def _notification_summaries(
    db: AsyncSession, rows: list[Notification],
) -> dict[uuid.UUID, dict]:
    """lead/opportunity display fields for a page of notifications."""
    lead_ids = {n.lead_id for n in rows if n.lead_id}
    opp_ids = {n.opportunity_id for n in rows if n.opportunity_id}
    leads = {}
    opps = {}
    if lead_ids:
        for lead in (await db.execute(
            select(Lead).where(Lead.id.in_(lead_ids))
        )).scalars():
            leads[lead.id] = lead
    if opp_ids:
        for opp in (await db.execute(
            select(Opportunity).where(Opportunity.id.in_(opp_ids))
        )).scalars():
            opps[opp.id] = opp
    out: dict[uuid.UUID, dict] = {}
    for n in rows:
        lead = leads.get(n.lead_id)
        opp = opps.get(n.opportunity_id)
        out[n.id] = {
            "lead_email": lead.email if lead else None,
            "lead_name": (
                " ".join(x for x in [lead.first_name, lead.last_name] if x) or None
            ) if lead else None,
            "opportunity_name": opp.name if opp else None,
        }
    return out


@router.get("/notifications", response_model=PaginatedNotifications)
async def list_notifications(
    unread: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> PaginatedNotifications:
    filters = []
    if unread:
        filters.append(Notification.read_at.is_(None))

    total = (await db.execute(
        select(func.count()).select_from(Notification).where(*filters)
    )).scalar_one()
    unread_count = (await db.execute(
        select(func.count()).select_from(Notification)
        .where(Notification.read_at.is_(None))
    )).scalar_one()

    rows = list((await db.execute(
        select(Notification)
        .where(*filters)
        .order_by(Notification.created_at.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )).scalars().all())

    summaries = await _notification_summaries(db, rows)
    items = [
        NotificationResponse(
            id=n.id, kind=n.kind, title=n.title, body=n.body,
            lead_id=n.lead_id, opportunity_id=n.opportunity_id,
            activity_id=n.activity_id, read_at=n.read_at,
            emailed_at=n.emailed_at, created_at=n.created_at,
            **summaries[n.id],
        )
        for n in rows
    ]
    return PaginatedNotifications(
        items=items, total=total, unread=unread_count,
        page=page, page_size=page_size,
        total_pages=math.ceil(total / page_size) if total else 0,
    )


@router.post("/notifications/{notification_id}/read", response_model=NotificationResponse)
async def mark_notification_read(
    notification_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> NotificationResponse:
    n = await db.get(Notification, notification_id)
    if n is None:
        raise HTTPException(status_code=404, detail="notification not found")
    if n.read_at is None:
        n.read_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(n)
    summaries = await _notification_summaries(db, [n])
    return NotificationResponse(
        id=n.id, kind=n.kind, title=n.title, body=n.body,
        lead_id=n.lead_id, opportunity_id=n.opportunity_id,
        activity_id=n.activity_id, read_at=n.read_at,
        emailed_at=n.emailed_at, created_at=n.created_at,
        **summaries[n.id],
    )


@router.post("/notifications/read-all")
async def mark_all_notifications_read(
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        update(Notification)
        .where(Notification.read_at.is_(None))
        .values(read_at=datetime.now(timezone.utc))
    )
    await db.commit()
    return {"updated": result.rowcount}


# ============================================================================
# Audit log
# ============================================================================


@router.get("/actions", response_model=PaginatedAgentActions)
async def list_actions(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> PaginatedAgentActions:
    total = (await db.execute(
        select(func.count()).select_from(AgentAction)
    )).scalar_one()
    rows = (await db.execute(
        select(AgentAction)
        .order_by(AgentAction.created_at.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )).scalars().all()
    items = [
        AgentActionResponse(
            id=a.id, action_type=a.action_type, status=a.status,
            summary=a.summary, lead_id=a.lead_id,
            opportunity_id=a.opportunity_id, activity_id=a.activity_id,
            detail=a.detail, model=a.model,
            cost_usd=float(a.cost_usd) if a.cost_usd is not None else None,
            created_at=a.created_at,
        )
        for a in rows
    ]
    return PaginatedAgentActions(
        items=items, total=total, page=page, page_size=page_size,
        total_pages=math.ceil(total / page_size) if total else 0,
    )


# ============================================================================
# Reply triage feed
# ============================================================================


@router.get("/replies", response_model=PaginatedReplies)
async def list_replies(
    sentiment: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> PaginatedReplies:
    """Recent inbound-email activities the agent logged, enriched with
    lead context + convert eligibility — powers the Replies page."""
    filters = [
        CrmActivity.activity_type == CrmActivityType.EMAIL,
        CrmActivity.direction == CrmActivityDirection.INBOUND,
        CrmActivity.is_agent_generated.is_(True),
    ]
    if sentiment:
        filters.append(CrmActivity.sentiment == sentiment)

    total = (await db.execute(
        select(func.count()).select_from(CrmActivity).where(*filters)
    )).scalar_one()

    rows = list((await db.execute(
        select(CrmActivity)
        .where(*filters)
        .order_by(CrmActivity.occurred_at.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )).scalars().all())

    lead_ids = {a.lead_id for a in rows if a.lead_id}
    leads: dict[uuid.UUID, Lead] = {}
    if lead_ids:
        for lead in (await db.execute(
            select(Lead).where(Lead.id.in_(lead_ids))
        )).scalars():
            leads[lead.id] = lead

    # Suggested drafts (Phase 6): latest draft_reply audit row per activity.
    drafts: dict[uuid.UUID, str] = {}
    activity_ids = [a.id for a in rows]
    if activity_ids:
        draft_rows = (await db.execute(
            select(AgentAction)
            .where(
                AgentAction.action_type == AgentActionType.DRAFT_REPLY,
                AgentAction.activity_id.in_(activity_ids),
            )
            .order_by(AgentAction.created_at.asc())
        )).scalars().all()
        for d in draft_rows:  # later rows overwrite — latest wins
            body = (d.detail or {}).get("draft_body")
            if body:
                drafts[d.activity_id] = body

    items = []
    for a in rows:
        lead = leads.get(a.lead_id)
        converted = bool(lead and lead.converted_opportunity_id)
        items.append(ReplyFeedItem(
            activity_id=a.id,
            lead_id=a.lead_id,
            opportunity_id=a.opportunity_id,
            sentiment=a.sentiment,
            subject=a.subject,
            body_preview=(a.body or "")[:280] or None,
            occurred_at=a.occurred_at,
            lead_email=lead.email if lead else None,
            lead_name=(
                " ".join(x for x in [lead.first_name, lead.last_name] if x) or None
            ) if lead else None,
            lead_company=lead.company if lead else None,
            convert_eligible=bool(lead and not converted),
            converted=converted,
            draft_body=drafts.get(a.id),
        ))
    return PaginatedReplies(
        items=items, total=total, page=page, page_size=page_size,
        total_pages=math.ceil(total / page_size) if total else 0,
    )
