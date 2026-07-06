"""Prospect-signal workers (Feature C).

  signals.scheduled_runner — beat (60s): dispatch due active watches
                             (mirrors social_listening.scheduled_runner).
  signals.run_watch        — one watch: detect → persist deduped
                             signals → action policy.

Action policy (the autonomy boundary):
  - Watch linked to an existing lead/opp → re-surface it: a CRM task
    ("Reach out — <signal>") + an owner notification.
  - Cold target (no lead) WITH an email → create a campaign-less CRM
    lead pre-filled from the signal + notify.
  - Cold target without an email → notification only.
  - NEVER auto-added to a sending campaign — that stays a human click.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.models import (
    CrmActivity,
    CrmActivityType,
    Lead,
    NotificationKind,
    ProspectSignal,
    SignalWatch,
    SignalWatchStatus,
    SocialSearchFrequency,
)
from app.services import agent_core, notifications, signal_detection
from app.workers.celery_app import celery_app
from app.tenancy.context import for_all_tenants, with_record_tenant

logger = logging.getLogger(__name__)

_FREQUENCY_DELTAS: dict[SocialSearchFrequency, timedelta] = {
    SocialSearchFrequency.EVERY_6H: timedelta(hours=6),
    SocialSearchFrequency.EVERY_12H: timedelta(hours=12),
    SocialSearchFrequency.DAILY: timedelta(days=1),
    SocialSearchFrequency.WEEKLY: timedelta(weeks=1),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _next_run_at(frequency: SocialSearchFrequency) -> datetime | None:
    delta = _FREQUENCY_DELTAS.get(frequency)
    return _now() + delta if delta else None


async def _apply_signal_actions(
    session: AsyncSession, watch: SignalWatch, signal: ProspectSignal,
) -> dict[str, Any]:
    """Re-surface an existing record or stage a cold lead — never touch
    a sending campaign."""
    outcome: dict[str, Any] = {"task": False, "lead_created": False}
    agent_settings = await agent_core.get_agent_settings(session)

    lead_id = watch.lead_id
    if lead_id is None and watch.opportunity_id is None and watch.email:
        # Cold target with an email → stage a campaign-less CRM lead.
        existing = await session.scalar(
            select(Lead).where(Lead.email == watch.email.strip().lower()).limit(1)
        )
        if existing is not None:
            lead_id = existing.id
        else:
            parts = (watch.person_name or "").split(" ", 1)
            lead = Lead(
                campaign_id=None,
                email=watch.email.strip().lower(),
                first_name=parts[0] or None,
                last_name=parts[1] if len(parts) > 1 else None,
                company=watch.company,
                linkedin_url=watch.linkedin_url,
                company_website=watch.company_website,
            )
            session.add(lead)
            await session.flush()
            lead_id = lead.id
            outcome["lead_created"] = True
        signal.lead_id = lead_id

    if watch.lead_id is not None or watch.opportunity_id is not None:
        # Re-surface: a reach-out task on the tracked record.
        task = CrmActivity(
            lead_id=watch.lead_id,
            opportunity_id=watch.opportunity_id,
            activity_type=CrmActivityType.TASK,
            subject=f"Reach out — {signal.summary}"[:300],
            body=(
                f"Detected by the {signal.signal_type} watch. "
                "Review the signal and reach out while it's fresh."
            ),
            due_at=agent_core.next_business_day(_now()),
            is_agent_generated=True,
            # The signal notification below IS the ping — don't let the
            # reminder sweeper double-notify for this task.
            reminder_sent_at=_now(),
        )
        session.add(task)
        await session.flush()
        outcome["task"] = True

    await notifications.notify(
        session,
        agent_settings,
        kind=NotificationKind.PROSPECT_SIGNAL,
        title=signal.summary[:300],
        body=(
            f"Signal type: {signal.signal_type}\n"
            + (f"New CRM lead staged: {watch.email}\n" if outcome["lead_created"] else "")
            + "Open the Signals page to action or dismiss."
        ),
        dedup_key=f"prospect_signal:{signal.dedup_key}",
        lead_id=signal.lead_id or watch.lead_id,
        opportunity_id=watch.opportunity_id,
    )
    return outcome


async def run_watch_session(session: AsyncSession, watch_id: uuid.UUID) -> dict[str, Any]:
    """Detect + persist + action for one watch.  Caller owns the txn."""
    watch = await session.get(SignalWatch, watch_id)
    if watch is None:
        return {"status": "not_found"}
    if watch.status != SignalWatchStatus.ACTIVE:
        return {"status": "skipped", "reason": "not_active"}

    detected, seen_update = await signal_detection.detect_for_watch(session, watch)

    new_signals = 0
    leads_created = 0
    for d in detected:
        exists = await session.scalar(
            select(ProspectSignal.id).where(
                ProspectSignal.dedup_key == d.dedup_key
            ).limit(1)
        )
        if exists is not None:
            continue
        signal = ProspectSignal(
            watch_id=watch.id,
            signal_type=d.signal_type,
            summary=d.summary,
            detail=d.detail,
            dedup_key=d.dedup_key,
            lead_id=watch.lead_id,
            opportunity_id=watch.opportunity_id,
        )
        session.add(signal)
        await session.flush()
        outcome = await _apply_signal_actions(session, watch, signal)
        new_signals += 1
        leads_created += int(outcome["lead_created"])

    # Merge the freshly-observed state into the diff baseline.
    if seen_update:
        watch.last_seen = {**(watch.last_seen or {}), **seen_update}
    watch.last_run_at = _now()
    watch.last_run_status = "done"
    watch.next_run_at = _next_run_at(watch.frequency)
    return {
        "status": "done",
        "detected": len(detected),
        "new_signals": new_signals,
        "leads_created": leads_created,
    }


async def _run_watch_async(watch_id: str) -> dict[str, Any]:
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            try:
                result = await run_watch_session(session, uuid.UUID(watch_id))
                await session.commit()
            except Exception:
                await session.rollback()
                # Best-effort error stamp so the UI shows the failure.
                watch = await session.get(SignalWatch, uuid.UUID(watch_id))
                if watch is not None:
                    watch.last_run_status = "error"
                    watch.next_run_at = _next_run_at(watch.frequency)
                    await session.commit()
                raise
    finally:
        await engine.dispose()
    return result


@celery_app.task(name="signals.run_watch")
def run_watch(watch_id: str) -> dict[str, Any]:
    return asyncio.run(with_record_tenant(SignalWatch, watch_id, _run_watch_async, watch_id))


async def _scheduled_runner_async() -> dict[str, Any]:
    dispatched: list[str] = []
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            due = (await session.execute(
                select(SignalWatch).where(
                    SignalWatch.status == SignalWatchStatus.ACTIVE,
                    SignalWatch.frequency != SocialSearchFrequency.MANUAL,
                    SignalWatch.next_run_at.is_not(None),
                    SignalWatch.next_run_at <= _now(),
                    # NULL-safe "not currently running" (vanilla
                    # NULL != 'running' silently excludes fresh rows).
                    or_(
                        SignalWatch.last_run_status.is_(None),
                        SignalWatch.last_run_status != "running",
                    ),
                )
            )).scalars().all()
            for watch in due:
                watch.last_run_status = "running"
                dispatched.append(str(watch.id))
            await session.commit()
    finally:
        await engine.dispose()

    for wid in dispatched:
        try:
            run_watch.delay(wid)
        except Exception:  # noqa: BLE001
            logger.exception("failed to enqueue signal watch %s", wid)
    return {"dispatched": dispatched}


@celery_app.task(name="signals.scheduled_runner")
def scheduled_runner() -> dict[str, Any]:
    return asyncio.run(for_all_tenants(_scheduled_runner_async))
