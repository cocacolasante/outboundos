"""Agent orchestration core.

Pure async functions the workers call, kept out of Celery task modules
so the logic is unit-testable without a broker.  This module is the
single place that enforces the agent's autonomy boundary:

  MAY autonomously:  log activities, create reminder TASKS, create
                     notifications (and email the OWNER), flag stale
                     opportunities.
  MAY NEVER:         convert a lead, send anything to a prospect,
                     change an opportunity stage, delete anything.

Anything in the second list must remain a human-triggered router
action; the agent only *prompts* it via reminders/notifications.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CLOSED_STAGES,
    AgentAction,
    AgentActionStatus,
    AgentActionType,
    AgentSettings,
    CrmActivity,
    CrmActivityDirection,
    CrmActivityType,
    Lead,
    NotificationKind,
    Opportunity,
)
from app.services import notifications

logger = logging.getLogger(__name__)

# Reminder subjects double as the idempotency match key (open agent
# task with this subject for the same parent = already reminded).
CONVERT_REMINDER_SUBJECT = "Convert lead to opportunity"
FOLLOWUP_REMINDER_SUBJECT = "Follow up — positive reply"
STALE_OPP_NUDGE_SUBJECT = "Re-engage — deal has gone quiet"


async def get_agent_settings(session: AsyncSession) -> AgentSettings:
    """Return the AgentSettings row for the ambient tenant (one per tenant
    since multi-tenancy Phase 2 — was the app-wide singleton), creating it
    with defaults on first access so a fresh tenant needs no seed step.

    Tenant resolution: the ``current_tenant_id`` ContextVar (set by the
    request-path ``get_db``).  Workers run without tenant context until the
    worker-context phase — for them this falls back to the oldest row
    (the single-operator reality: exactly one row exists post-backfill).

    Caller owns the transaction — on the create path the new row is
    flushed (so defaults/PK are live) but not committed.
    """
    from app.tenancy.context import current_tenant_id

    tid = current_tenant_id.get()
    stmt = select(AgentSettings)
    if tid is not None:
        stmt = stmt.where(AgentSettings.tenant_id == tid)
    else:
        stmt = stmt.order_by(AgentSettings.created_at)
    row = (await session.execute(stmt.limit(1))).scalars().first()
    if row is None:
        row = AgentSettings(tenant_id=tid)
        session.add(row)
        await session.flush()
        # Re-read so server_default columns (the bool toggles, the
        # confidence threshold) carry real values instead of None.
        await session.refresh(row)
    return row


def record_agent_action(
    session: AsyncSession,
    *,
    action_type: AgentActionType,
    status: AgentActionStatus,
    summary: str,
    lead_id: uuid.UUID | None = None,
    opportunity_id: uuid.UUID | None = None,
    activity_id: uuid.UUID | None = None,
    detail: dict[str, Any] | None = None,
    model: str | None = None,
    cost_usd: float | None = None,
) -> AgentAction:
    """Append one audit row (added to the session, not flushed)."""
    row = AgentAction(
        action_type=action_type,
        status=status,
        summary=summary[:500],
        lead_id=lead_id,
        opportunity_id=opportunity_id,
        activity_id=activity_id,
        detail=detail,
        model=model,
        cost_usd=Decimal(str(round(cost_usd, 6))) if cost_usd else None,
    )
    session.add(row)
    return row


def next_business_day(now: datetime) -> datetime:
    """``now`` + 1 day, rolled forward past Saturday/Sunday."""
    due = now + timedelta(days=1)
    while due.weekday() >= 5:  # 5=Sat, 6=Sun
        due += timedelta(days=1)
    return due


async def _open_agent_task_exists(
    session: AsyncSession,
    *,
    subject: str,
    lead_id: uuid.UUID | None = None,
    opportunity_id: uuid.UUID | None = None,
) -> bool:
    """True when an open (uncompleted) agent-generated task with this
    subject already exists for the given parent — the reminder
    idempotency check."""
    q = select(CrmActivity.id).where(
        CrmActivity.activity_type == CrmActivityType.TASK,
        CrmActivity.completed_at.is_(None),
        CrmActivity.is_agent_generated.is_(True),
        CrmActivity.subject == subject,
    )
    if lead_id is not None:
        q = q.where(CrmActivity.lead_id == lead_id)
    if opportunity_id is not None:
        q = q.where(CrmActivity.opportunity_id == opportunity_id)
    return (await session.scalar(q.limit(1))) is not None


# --------------------------------------------------------------------------
# Auto-complete tasks once the lead/deal has been acted on
# --------------------------------------------------------------------------

# Activity types that count as a real "touch" on a lead/deal.  Logging one
# means the open to-do for that record has been acted on — so a due/overdue
# task whose work has clearly been done stops nagging in the reminder sweep
# and the daily digest.  Creating another TASK is NOT a touch (it's more
# to-do, not "done").
TOUCH_ACTIVITY_TYPES = (
    CrmActivityType.CALL,
    CrmActivityType.EMAIL,
    CrmActivityType.MEETING,
    CrmActivityType.NOTE,
)


async def _task_handled_since_creation(
    session: AsyncSession, task: CrmActivity
) -> bool:
    """True when a touch activity (call/email/meeting/note) was logged on the
    task's lead OR opportunity at/after the task was created — i.e. the work
    the task represents has since been acted on.  Backdated touches that
    occurred BEFORE the task was created don't count."""
    parents = []
    if task.lead_id is not None:
        parents.append(CrmActivity.lead_id == task.lead_id)
    if task.opportunity_id is not None:
        parents.append(CrmActivity.opportunity_id == task.opportunity_id)
    if not parents:
        return False
    hit = await session.scalar(
        select(CrmActivity.id)
        .where(
            CrmActivity.activity_type.in_(TOUCH_ACTIVITY_TYPES),
            CrmActivity.occurred_at >= task.created_at,
            or_(*parents),
        )
        .limit(1)
    )
    return hit is not None


async def complete_handled_tasks(
    session: AsyncSession, tasks: list[CrmActivity]
) -> list[CrmActivity]:
    """Among the given OPEN tasks, mark complete the ones already handled by a
    logged touch activity (sets ``completed_at`` = now + an audit row).  Returns
    the tasks that were completed; the caller owns the transaction.

    Single source of truth for the rule, shared by the reminder sweep + daily
    digest (so neither nags about a task the user already acted on) and the
    activity-create path (so the task closes the moment the touch is logged).
    """
    completed: list[CrmActivity] = []
    now = datetime.now(timezone.utc)
    for task in tasks:
        if task.completed_at is not None:
            continue
        if await _task_handled_since_creation(session, task):
            task.completed_at = now
            completed.append(task)
            record_agent_action(
                session,
                action_type=AgentActionType.LOG_ACTIVITY,
                status=AgentActionStatus.SUCCESS,
                summary=(
                    "Auto-completed task — activity logged on the record "
                    f"since it was created: {task.subject}"
                )[:500],
                lead_id=task.lead_id,
                opportunity_id=task.opportunity_id,
                activity_id=task.id,
                detail={"reason": "activity_logged_after_task"},
            )
    return completed


async def autocomplete_due_tasks_for_record(
    session: AsyncSession,
    *,
    lead_id: uuid.UUID | None = None,
    opportunity_id: uuid.UUID | None = None,
) -> list[CrmActivity]:
    """Complete the record's OPEN, currently due-or-overdue tasks that have been
    handled by a logged touch.  Call right after logging a touch activity on a
    lead/opportunity so the matching reminder closes immediately.

    Scoped to due/overdue tasks (``due_at <= now``) so a touch never prematurely
    closes a task scheduled for the future — matching "overdue or due task"."""
    parents = []
    if lead_id is not None:
        parents.append(CrmActivity.lead_id == lead_id)
    if opportunity_id is not None:
        parents.append(CrmActivity.opportunity_id == opportunity_id)
    if not parents:
        return []
    now = datetime.now(timezone.utc)
    tasks = (await session.execute(
        select(CrmActivity).where(
            CrmActivity.activity_type == CrmActivityType.TASK,
            CrmActivity.completed_at.is_(None),
            CrmActivity.due_at.is_not(None),
            CrmActivity.due_at <= now,
            or_(*parents),
        )
    )).scalars().all()
    return await complete_handled_tasks(session, list(tasks))


async def process_inbound_reply(
    session: AsyncSession,
    lead: Lead,
    msg: dict[str, Any],
    classification: Any,  # reply_sentiment.ReplyClassification
) -> dict[str, Any]:
    """Run the agent's reply pipeline for ONE newly-polled inbound reply.

    Steps (each individually gated by AgentSettings):
      1. Log an inbound-email CrmActivity carrying the sentiment.
      2. Positive + confident + unconverted → idempotent convert-reminder
         task (due next business day).
      3. Positive + already converted → follow-up task on the deal.
      4. Notifications per the notify_* toggles, deduped on message id.

    Every step writes an AgentAction audit row.  Caller owns the
    transaction (commit after).  Returns a summary dict for logging.
    """
    agent_settings = await get_agent_settings(session)
    message_id = (msg.get("message_id") or "") or f"uid:{msg.get('uid', uuid.uuid4().hex)}"
    now = datetime.now(timezone.utc)
    result: dict[str, Any] = {
        "activity_logged": False,
        "reminder_created": False,
        "draft_created": False,
        "notified": False,
    }

    sentiment = getattr(classification, "sentiment", "neutral")
    confidence = float(getattr(classification, "confidence", 0.0))
    summary_line = getattr(classification, "summary", "") or ""
    classify_detail = {
        "sentiment": sentiment,
        "intent": getattr(classification, "intent", "other"),
        "confidence": confidence,
        "summary": summary_line,
        "suggested_next_action": getattr(classification, "suggested_next_action", ""),
        "message_id": message_id,
    }

    # ---- audit the classification itself ----
    record_agent_action(
        session,
        action_type=AgentActionType.CLASSIFY_REPLY,
        status=(
            AgentActionStatus.FAILED
            if getattr(classification, "parse_failed", False)
            else AgentActionStatus.SUCCESS
        ),
        summary=f"Classified reply as {sentiment} ({confidence:.2f})",
        lead_id=lead.id,
        detail=classify_detail,
        model=getattr(classification, "model", None) or None,
        cost_usd=float(getattr(classification, "cost_usd", 0.0) or 0.0),
    )

    converted_opp_id = getattr(lead, "converted_opportunity_id", None)

    # ---- 0. reply-outcome capture (Feature A copy loop) ----
    # Snapshot what was actually sent alongside the verdict it earned —
    # the lead's composed copy is editable later, so attribution must not
    # read it lazily.  Campaign-less CRM leads have no campaign copy to
    # attribute, so they're skipped.
    if lead.campaign_id is not None:
        from app.models import ReplyOutcome  # local import to avoid cycles

        session.add(ReplyOutcome(
            lead_id=lead.id,
            campaign_id=lead.campaign_id,
            sentiment=sentiment,
            intent=getattr(classification, "intent", None),
            composed_subject=lead.composed_subject,
            composed_body=lead.composed_body,
        ))

    # ---- 1. log the inbound email activity ----
    activity: CrmActivity | None = None
    if agent_settings.auto_log_replies:
        activity = CrmActivity(
            lead_id=lead.id,
            # Converted lead → mirror onto the deal timeline too.
            opportunity_id=converted_opp_id,
            activity_type=CrmActivityType.EMAIL,
            direction=CrmActivityDirection.INBOUND,
            subject=(msg.get("subject") or "(no subject)")[:500],
            body=(msg.get("body_text") or "")[:4000] or None,
            sentiment=sentiment,
            is_agent_generated=True,
            occurred_at=msg.get("received_at") or now,
        )
        session.add(activity)
        await session.flush()
        result["activity_logged"] = True
        record_agent_action(
            session,
            action_type=AgentActionType.LOG_ACTIVITY,
            status=AgentActionStatus.SUCCESS,
            summary=f"Logged inbound reply from {msg.get('from_email', '?')}",
            lead_id=lead.id,
            opportunity_id=converted_opp_id,
            activity_id=activity.id,
        )
    else:
        record_agent_action(
            session,
            action_type=AgentActionType.LOG_ACTIVITY,
            status=AgentActionStatus.SKIPPED,
            summary="auto_log_replies disabled",
            lead_id=lead.id,
        )

    # ---- 2./3. reminder tasks on a confident positive ----
    is_positive = sentiment == "positive"
    confident = confidence >= float(agent_settings.min_confidence_to_act)
    if is_positive and not confident:
        record_agent_action(
            session,
            action_type=AgentActionType.CREATE_REMINDER,
            status=AgentActionStatus.SKIPPED,
            summary=(
                f"confidence {confidence:.2f} below threshold "
                f"{agent_settings.min_confidence_to_act}"
            ),
            lead_id=lead.id,
        )
    elif is_positive and converted_opp_id is None:
        if not agent_settings.auto_create_convert_reminders:
            record_agent_action(
                session,
                action_type=AgentActionType.CREATE_REMINDER,
                status=AgentActionStatus.SKIPPED,
                summary="auto_create_convert_reminders disabled",
                lead_id=lead.id,
            )
        elif await _open_agent_task_exists(
            session, subject=CONVERT_REMINDER_SUBJECT, lead_id=lead.id,
        ):
            record_agent_action(
                session,
                action_type=AgentActionType.CREATE_REMINDER,
                status=AgentActionStatus.SKIPPED,
                summary="open convert reminder already exists",
                lead_id=lead.id,
            )
        else:
            reminder = CrmActivity(
                lead_id=lead.id,
                activity_type=CrmActivityType.TASK,
                subject=CONVERT_REMINDER_SUBJECT,
                body=(
                    f"Positive reply received ({confidence:.0%} confidence): "
                    f"{summary_line}"
                )[:1000],
                due_at=next_business_day(now),
                is_agent_generated=True,
            )
            session.add(reminder)
            await session.flush()
            result["reminder_created"] = True
            record_agent_action(
                session,
                action_type=AgentActionType.CREATE_REMINDER,
                status=AgentActionStatus.SUCCESS,
                summary="Created convert-lead reminder (due next business day)",
                lead_id=lead.id,
                activity_id=reminder.id,
            )
    elif is_positive and converted_opp_id is not None:
        if await _open_agent_task_exists(
            session, subject=FOLLOWUP_REMINDER_SUBJECT,
            opportunity_id=converted_opp_id,
        ):
            record_agent_action(
                session,
                action_type=AgentActionType.CREATE_REMINDER,
                status=AgentActionStatus.SKIPPED,
                summary="open follow-up reminder already exists on the deal",
                lead_id=lead.id,
                opportunity_id=converted_opp_id,
            )
        else:
            followup = CrmActivity(
                lead_id=lead.id,
                opportunity_id=converted_opp_id,
                activity_type=CrmActivityType.TASK,
                subject=FOLLOWUP_REMINDER_SUBJECT,
                body=(
                    f"Positive reply received ({confidence:.0%} confidence): "
                    f"{summary_line}"
                )[:1000],
                due_at=next_business_day(now),
                is_agent_generated=True,
            )
            session.add(followup)
            await session.flush()
            result["reminder_created"] = True
            record_agent_action(
                session,
                action_type=AgentActionType.CREATE_REMINDER,
                status=AgentActionStatus.SUCCESS,
                summary="Created follow-up reminder on the converted deal",
                lead_id=lead.id,
                opportunity_id=converted_opp_id,
                activity_id=followup.id,
            )

    # ---- 4. suggested reply draft (optional, Sonnet) ----
    draft_body: str | None = None
    if agent_settings.auto_draft_replies and activity is not None:
        from app.services import reply_drafter  # local import to avoid cycles

        intent = getattr(classification, "intent", "other")
        if not reply_drafter.should_draft(intent):
            record_agent_action(
                session,
                action_type=AgentActionType.DRAFT_REPLY,
                status=AgentActionStatus.SKIPPED,
                summary=f"no draft for intent={intent}",
                lead_id=lead.id,
                activity_id=activity.id,
            )
        else:
            draft = await reply_drafter.draft_reply(
                subject=msg.get("subject", ""),
                body_text=msg.get("body_text", ""),
                classification=classification,
                lead_context={
                    "name": " ".join(
                        x for x in [lead.first_name, lead.last_name] if x
                    ),
                    "email": lead.email,
                    "company": lead.company,
                    "job_title": lead.job_title,
                },
            )
            if draft.ok:
                draft_body = draft.body
                result["draft_created"] = True
            record_agent_action(
                session,
                action_type=AgentActionType.DRAFT_REPLY,
                status=(
                    AgentActionStatus.SUCCESS if draft.ok else AgentActionStatus.FAILED
                ),
                summary=(
                    "Drafted suggested reply" if draft.ok
                    else "reply draft failed — see logs"
                ),
                lead_id=lead.id,
                activity_id=activity.id,
                # The triage feed reads draft_body off this audit row.
                detail={"draft_body": draft.body} if draft.ok else None,
                model=draft.model or None,
                cost_usd=draft.cost_usd,
            )

    # ---- 5. notifications ----
    lead_name = " ".join(
        x for x in [lead.first_name, lead.last_name] if x
    ) or lead.email
    should_notify_positive = (
        is_positive and confident and agent_settings.notify_on_positive_reply
    )
    should_notify_any = agent_settings.notify_on_any_reply
    if should_notify_positive or should_notify_any:
        kind = (
            NotificationKind.POSITIVE_REPLY
            if should_notify_positive
            else NotificationKind.REPLY
        )
        notif_body = (
            f"{summary_line}\n\nSubject: {msg.get('subject', '')}\n"
            f"From: {msg.get('from_email', '')}"
        )
        if draft_body:
            notif_body += f"\n\n--- Suggested reply (review before sending) ---\n{draft_body}"
        outcome = await notifications.notify(
            session,
            agent_settings,
            kind=kind,
            title=(
                f"Positive reply from {lead_name}"
                if should_notify_positive
                else f"Reply from {lead_name}"
            ),
            body=notif_body,
            dedup_key=f"{kind.value}:{message_id}",
            lead_id=lead.id,
            opportunity_id=converted_opp_id,
            activity_id=activity.id if activity is not None else None,
        )
        result["notified"] = outcome["notification"] is not None
        record_agent_action(
            session,
            action_type=AgentActionType.SEND_NOTIFICATION,
            status=(
                AgentActionStatus.SUCCESS
                if outcome["notification"] is not None
                else AgentActionStatus.SKIPPED
            ),
            summary=(
                "Notified owner of reply"
                if outcome["notification"] is not None
                else "notification deduped (already sent for this message)"
            ),
            lead_id=lead.id,
            detail={"deduped": outcome["deduped"], "emailed": outcome["emailed"]},
        )

    return result


async def flag_stale_opportunities(session: AsyncSession) -> dict[str, Any]:
    """Nudge open opportunities that have gone quiet.

    A deal counts as stale when its most recent CrmActivity
    ``occurred_at`` (falling back to the deal's own ``updated_at`` when
    it has no activities) is older than ``AGENT_STALE_OPP_DAYS`` AND it
    has no open task already (an open task means the operator is
    already on the hook for it — including a previous nudge).

    For each stale deal: one nudge TASK (due next business day,
    ``reminder_sent_at`` pre-stamped so the reminder sweeper doesn't
    double-ping — the stale notification IS the ping) + one
    ``stale_opportunity`` notification deduped per-deal-per-ISO-week,
    so a deal that stays quiet re-surfaces at most weekly after the
    nudge task is completed.

    Caller owns the transaction.
    """
    from app.config import settings as app_settings

    agent_settings = await get_agent_settings(session)
    result: dict[str, Any] = {"checked": 0, "flagged": 0}
    if not agent_settings.stale_opp_nudges_enabled:
        result["skipped"] = "stale_opp_nudges_disabled"
        return result

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=app_settings.AGENT_STALE_OPP_DAYS)

    open_opps = (await session.execute(
        select(Opportunity).where(Opportunity.stage.not_in(CLOSED_STAGES))
    )).scalars().all()
    result["checked"] = len(open_opps)

    for opp in open_opps:
        latest_activity = await session.scalar(
            select(CrmActivity.occurred_at)
            .where(CrmActivity.opportunity_id == opp.id)
            .order_by(CrmActivity.occurred_at.desc())
            .limit(1)
        )
        last_touch = latest_activity or opp.updated_at
        if last_touch is None or last_touch > cutoff:
            continue

        has_open_task = (await session.scalar(
            select(CrmActivity.id).where(
                CrmActivity.opportunity_id == opp.id,
                CrmActivity.activity_type == CrmActivityType.TASK,
                CrmActivity.completed_at.is_(None),
            ).limit(1)
        )) is not None
        if has_open_task:
            continue

        idle_days = (now - last_touch).days
        nudge = CrmActivity(
            opportunity_id=opp.id,
            lead_id=opp.source_lead_id,
            activity_type=CrmActivityType.TASK,
            subject=STALE_OPP_NUDGE_SUBJECT,
            body=(
                f"No activity on \"{opp.name}\" for {idle_days} days "
                f"(stage: {opp.stage.value}).  Reach out or update the stage."
            ),
            due_at=next_business_day(now),
            is_agent_generated=True,
            # The stale notification below IS the ping — pre-stamp so the
            # reminder sweeper doesn't send a second one for this task.
            reminder_sent_at=now,
        )
        session.add(nudge)
        await session.flush()

        outcome = await notifications.notify(
            session,
            agent_settings,
            kind=NotificationKind.STALE_OPPORTUNITY,
            title=f"Deal gone quiet: {opp.name} ({idle_days}d idle)"[:300],
            body=nudge.body,
            # Per-deal-per-ISO-week dedup: a still-stale deal re-surfaces
            # at most weekly (and only once its nudge task is closed).
            dedup_key=f"stale_opportunity:{opp.id}:{now.strftime('%G-W%V')}",
            opportunity_id=opp.id,
            lead_id=opp.source_lead_id,
            activity_id=nudge.id,
        )
        result["flagged"] += 1
        record_agent_action(
            session,
            action_type=AgentActionType.FLAG_STALE_OPP,
            status=AgentActionStatus.SUCCESS,
            summary=f"Flagged stale deal ({idle_days}d idle): {opp.name}"[:300],
            opportunity_id=opp.id,
            activity_id=nudge.id,
            detail={
                "idle_days": idle_days,
                "deduped": outcome["deduped"],
                "emailed": outcome["emailed"],
            },
        )

    return result
