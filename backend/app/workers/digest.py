"""Daily digest email (Celery beat, crontab at AGENT_DIGEST_HOUR_UTC).

One email a day summarising what needs the operator's attention:

  - open tasks due today + overdue tasks
  - inbound replies in the last 24h grouped by sentiment
  - pipeline movement (deals opened / closed in the last 24h)
  - alerts that never got emailed (quiet hours / missing owner email)

The digest bypasses quiet hours on purpose — its send hour is itself
operator-configured, and it's the sweep-up channel for emails that
quiet hours deferred.  Idempotent via the ``digest:<date>`` dedup key,
so a beat double-fire can't send two.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.models import (
    AgentActionStatus,
    AgentActionType,
    CrmActivity,
    CrmActivityDirection,
    CrmActivityType,
    Notification,
    NotificationKind,
    Opportunity,
)
from app.services import agent_core, notifications
from app.workers.celery_app import celery_app
from app.tenancy.context import for_all_tenants

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def build_digest(session: AsyncSession) -> dict[str, Any]:
    """Collect the digest's raw numbers + line items.  Read-only."""
    now = _now()
    day_ago = now - timedelta(hours=24)
    # "Due today" = due within the next 24h.  A rolling horizon (not
    # end-of-calendar-day) keeps the bucket meaningful whatever hour the
    # digest fires — an 11pm digest with a midnight cutoff would show
    # nothing as upcoming.
    horizon = now + timedelta(hours=24)

    open_tasks = (await session.execute(
        select(CrmActivity).where(
            CrmActivity.activity_type == CrmActivityType.TASK,
            CrmActivity.completed_at.is_(None),
            CrmActivity.due_at.is_not(None),
            CrmActivity.due_at <= horizon,
        ).order_by(CrmActivity.due_at)
    )).scalars().all()
    overdue = [t for t in open_tasks if t.due_at < now]
    due_today = [t for t in open_tasks if t.due_at >= now]

    sentiment_rows = (await session.execute(
        select(CrmActivity.sentiment, func.count())
        .where(
            CrmActivity.activity_type == CrmActivityType.EMAIL,
            CrmActivity.direction == CrmActivityDirection.INBOUND,
            CrmActivity.created_at >= day_ago,
        )
        .group_by(CrmActivity.sentiment)
    )).all()
    replies_by_sentiment = {
        (sentiment or "unclassified"): count for sentiment, count in sentiment_rows
    }

    new_opps = (await session.execute(
        select(Opportunity).where(Opportunity.created_at >= day_ago)
    )).scalars().all()
    closed_opps = (await session.execute(
        select(Opportunity).where(
            Opportunity.closed_at.is_not(None),
            Opportunity.closed_at >= day_ago,
        )
    )).scalars().all()

    unsent_alerts = await session.scalar(
        select(func.count()).select_from(Notification).where(
            Notification.emailed_at.is_(None),
            Notification.created_at >= day_ago,
            Notification.kind != NotificationKind.DIGEST,
        )
    ) or 0

    return {
        "overdue": overdue,
        "due_today": due_today,
        "replies_by_sentiment": replies_by_sentiment,
        "new_opps": new_opps,
        "closed_opps": closed_opps,
        "unsent_alerts": int(unsent_alerts),
    }


def render_digest(data: dict[str, Any]) -> tuple[str, str]:
    """(title, body) for the digest notification + email."""
    overdue = data["overdue"]
    due_today = data["due_today"]
    replies = data["replies_by_sentiment"]
    total_replies = sum(replies.values())

    title = (
        f"Daily digest — {len(overdue)} overdue, {len(due_today)} due today, "
        f"{total_replies} replies"
    )

    lines: list[str] = []
    if overdue:
        lines.append(f"OVERDUE TASKS ({len(overdue)}):")
        lines += [f"  • {t.subject} (due {t.due_at:%b %d %H:%M} UTC)" for t in overdue[:10]]
    if due_today:
        lines.append(f"DUE TODAY ({len(due_today)}):")
        lines += [f"  • {t.subject} (due {t.due_at:%H:%M} UTC)" for t in due_today[:10]]
    if total_replies:
        parts = ", ".join(f"{count} {s}" for s, count in sorted(replies.items()))
        lines.append(f"REPLIES (last 24h): {total_replies} — {parts}")
    if data["new_opps"]:
        lines.append(f"NEW DEALS ({len(data['new_opps'])}):")
        lines += [f"  • {o.name}" for o in data["new_opps"][:10]]
    if data["closed_opps"]:
        lines.append(f"CLOSED DEALS ({len(data['closed_opps'])}):")
        lines += [
            f"  • {o.name} — {o.stage.value.replace('_', ' ')}"
            for o in data["closed_opps"][:10]
        ]
    if data["unsent_alerts"]:
        lines.append(
            f"{data['unsent_alerts']} alert(s) from the last 24h were not "
            "emailed (quiet hours or no owner email) — see the in-app feed."
        )
    if not lines:
        lines.append("Nothing needs attention today. 🎉")
    return title, "\n".join(lines)


async def send_daily_session(session: AsyncSession) -> dict[str, Any]:
    """Build + persist + email the daily digest.  Caller owns the
    transaction.  Returns a summary dict."""
    if not settings.AGENT_ENABLED:
        return {"skipped": "agent_disabled"}
    agent_settings = await agent_core.get_agent_settings(session)
    if not agent_settings.daily_digest_enabled:
        return {"skipped": "daily_digest_disabled"}

    # Close any due/overdue task the user has already acted on (a touch logged
    # on the lead/deal since it was created) BEFORE building the digest, so the
    # daily notification doesn't list work that's already done.  build_digest
    # stays read-only — these are completed here, then excluded by its
    # completed_at IS NULL filter.
    candidates = (await session.execute(
        select(CrmActivity).where(
            CrmActivity.activity_type == CrmActivityType.TASK,
            CrmActivity.completed_at.is_(None),
            CrmActivity.due_at.is_not(None),
            CrmActivity.due_at <= _now() + timedelta(hours=24),
        )
    )).scalars().all()
    await agent_core.complete_handled_tasks(session, list(candidates))

    today = _now().strftime("%Y-%m-%d")
    data = await build_digest(session)
    title, body = render_digest(data)

    row = await notifications.create_notification(
        session,
        kind=NotificationKind.DIGEST,
        title=title,
        body=body,
        dedup_key=f"digest:{today}",
    )
    if row is None:
        return {"skipped": "already_sent_today"}

    # The digest deliberately bypasses quiet hours: its hour is operator-
    # configured and it's the sweep-up channel for deferred alerts.
    emailed = await notifications.send_notification_email(row)
    agent_core.record_agent_action(
        session,
        action_type=AgentActionType.DIGEST,
        status=AgentActionStatus.SUCCESS,
        summary=title[:300],
        detail={
            "emailed": emailed,
            "overdue": len(data["overdue"]),
            "due_today": len(data["due_today"]),
            "replies": sum(data["replies_by_sentiment"].values()),
            "unsent_alerts": data["unsent_alerts"],
        },
    )
    return {"sent": True, "emailed": emailed, "title": title}


async def _send_daily_async() -> dict[str, Any]:
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            result = await send_daily_session(session)
            await session.commit()
    finally:
        await engine.dispose()
    logger.info("digest.send_daily: %s", result)
    return result


@celery_app.task(name="digest.send_daily")
def send_daily() -> dict[str, Any]:
    return asyncio.run(for_all_tenants(_send_daily_async))
