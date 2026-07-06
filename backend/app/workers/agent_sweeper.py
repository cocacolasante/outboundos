"""Agent reminder + stale-opportunity sweeps (Celery beat).

Two beat tasks:

  agent_sweeper.sweep_reminders   — every AGENT_REMINDER_SWEEP_INTERVAL
                                    minutes: notify the owner about open
                                    tasks that are due soon or overdue.
  agent_sweeper.sweep_stale_opps  — hourly: nudge open opportunities
                                    with no recent activity (delegates to
                                    agent_core.flag_stale_opportunities).

Idempotency model for reminders is TWO layers:
  1. ``CrmActivity.reminder_sent_at`` — stamped on the task row the
     first time a reminder fires for it; the sweep query filters
     ``IS NULL`` so a task is reminded about exactly once, ever.  (A
     task that got a "due soon" reminder does NOT get a second
     "overdue" one — by design; the owner was already pinged.)
  2. ``Notification.dedup_key`` (``task_due:<id>`` /
     ``task_overdue:<id>``) — the DB-level backstop.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.models import (
    AgentActionStatus,
    AgentActionType,
    CrmActivity,
    CrmActivityType,
    NotificationKind,
)
from app.services import agent_core, notifications
from app.workers.celery_app import celery_app
from app.tenancy.context import for_all_tenants

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_due(due_at: datetime, now: datetime) -> str:
    delta = due_at - now
    hours = abs(delta.total_seconds()) / 3600
    if delta.total_seconds() >= 0:
        return f"due in {hours:.0f}h" if hours >= 1 else "due within the hour"
    return f"overdue by {hours:.0f}h" if hours >= 1 else "just became overdue"


async def sweep_reminders_session(session: AsyncSession) -> dict[str, Any]:
    """Notify about open tasks due within AGENT_TASK_DUE_SOON_HOURS or
    already overdue, once per task (``reminder_sent_at`` anchor).
    Caller owns the transaction."""
    counts = {"due_soon": 0, "overdue": 0, "checked": 0, "auto_completed": 0}
    if not settings.AGENT_ENABLED:
        return {**counts, "skipped": "agent_disabled"}

    agent_settings = await agent_core.get_agent_settings(session)
    now = _now()
    horizon = now + timedelta(hours=settings.AGENT_TASK_DUE_SOON_HOURS)

    tasks = (await session.execute(
        select(CrmActivity).where(
            CrmActivity.activity_type == CrmActivityType.TASK,
            CrmActivity.completed_at.is_(None),
            CrmActivity.reminder_sent_at.is_(None),
            CrmActivity.due_at.is_not(None),
            CrmActivity.due_at <= horizon,
        ).order_by(CrmActivity.due_at)
    )).scalars().all()
    counts["checked"] = len(tasks)

    # Close any candidate the user has already acted on (a touch activity logged
    # on the lead/deal since the task was created) rather than nagging about it.
    autocompleted = await agent_core.complete_handled_tasks(session, list(tasks))
    counts["auto_completed"] = len(autocompleted)
    done_ids = {t.id for t in autocompleted}

    for task in tasks:
        if task.id in done_ids:
            continue
        overdue = task.due_at <= now
        kind = NotificationKind.TASK_OVERDUE if overdue else NotificationKind.TASK_DUE
        outcome = await notifications.notify(
            session,
            agent_settings,
            kind=kind,
            title=f"Task {_fmt_due(task.due_at, now)}: {task.subject}"[:300],
            body=(task.body or "")[:1000] or None,
            dedup_key=f"{kind.value}:{task.id}",
            lead_id=task.lead_id,
            opportunity_id=task.opportunity_id,
            activity_id=task.id,
        )
        # Stamp regardless of email outcome — the notification row (or a
        # prior dedup hit) exists either way; one reminder per task, ever.
        task.reminder_sent_at = now
        counts["overdue" if overdue else "due_soon"] += 1
        agent_core.record_agent_action(
            session,
            action_type=AgentActionType.SEND_NOTIFICATION,
            status=(
                AgentActionStatus.SUCCESS
                if outcome["notification"] is not None
                else AgentActionStatus.SKIPPED
            ),
            summary=f"Task reminder ({kind.value}): {task.subject}"[:300],
            lead_id=task.lead_id,
            opportunity_id=task.opportunity_id,
            activity_id=task.id,
            detail={"deduped": outcome["deduped"], "emailed": outcome["emailed"]},
        )

    return counts


async def _sweep_reminders_async() -> dict[str, Any]:
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            counts = await sweep_reminders_session(session)
            await session.commit()
    finally:
        await engine.dispose()
    if counts.get("due_soon") or counts.get("overdue"):
        logger.info("agent_sweeper.sweep_reminders: %s", counts)
    return counts


@celery_app.task(name="agent_sweeper.sweep_reminders")
def sweep_reminders() -> dict[str, Any]:
    return asyncio.run(for_all_tenants(_sweep_reminders_async))


async def _sweep_stale_opps_async() -> dict[str, Any]:
    if not settings.AGENT_ENABLED:
        return {"skipped": "agent_disabled"}
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            result = await agent_core.flag_stale_opportunities(session)
            await session.commit()
    finally:
        await engine.dispose()
    if result.get("flagged"):
        logger.info("agent_sweeper.sweep_stale_opps: %s", result)
    return result


@celery_app.task(name="agent_sweeper.sweep_stale_opps")
def sweep_stale_opps() -> dict[str, Any]:
    return asyncio.run(for_all_tenants(_sweep_stale_opps_async))
