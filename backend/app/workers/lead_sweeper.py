"""Sweep leads stuck in transient ``RUNNING`` states back to PENDING.

The research + compose workers each set ``status=RUNNING`` and commit
BEFORE making the (long-running) Anthropic API call.  This is the right
shape for UI progress indicators, but a worker crash between the commit
and the final DONE/FAILED commit leaves the row permanently RUNNING.
``/retry-failed`` only picks up FAILED rows, so a stuck RUNNING is
invisible to the operator.

This beat task scans for RUNNING rows older than ``STALE_AFTER_MINUTES``
and flips them back to PENDING + re-enqueues the corresponding worker.
No retry budget: if the lead crashes again, the next sweep catches it
again.  If the lead is genuinely processing normally, ``updated_at``
moves forward when the worker writes the final status — the sweep
window won't trip on healthy work.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.models import ComposeStatus, Lead, ResearchStatus
from app.workers.celery_app import celery_app
from app.tenancy.context import for_all_tenants

logger = logging.getLogger(__name__)

# How long a row can sit in RUNNING before we declare it stuck.  Real
# compose or research calls should never take this long; we pick 15 min
# as a safe ceiling well above Anthropic's slowest p99 response.
STALE_AFTER_MINUTES = 15


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _sweep_async() -> dict[str, int]:
    counts = {"compose_revived": 0, "research_revived": 0}
    cutoff = _now() - timedelta(minutes=STALE_AFTER_MINUTES)
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)

    # Late imports to dodge circulars; these workers import Lead/Campaign
    # which imports back.
    from app.workers.compose import compose_lead
    from app.workers.research import research_lead

    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            stale_compose = (await session.execute(
                select(Lead.id).where(
                    Lead.compose_status == ComposeStatus.RUNNING,
                    Lead.updated_at < cutoff,
                )
            )).scalars().all()
            stale_research = (await session.execute(
                select(Lead.id).where(
                    Lead.research_status == ResearchStatus.RUNNING,
                    Lead.updated_at < cutoff,
                )
            )).scalars().all()

            if stale_compose:
                await session.execute(
                    update(Lead)
                    .where(Lead.id.in_(stale_compose))
                    .values(compose_status=ComposeStatus.PENDING)
                )
                counts["compose_revived"] = len(stale_compose)
                logger.warning(
                    "lead_sweeper: %d compose rows stuck in RUNNING for "
                    ">%d min — flipping back to PENDING and re-enqueueing",
                    len(stale_compose), STALE_AFTER_MINUTES,
                )

            if stale_research:
                await session.execute(
                    update(Lead)
                    .where(Lead.id.in_(stale_research))
                    .values(research_status=ResearchStatus.PENDING)
                )
                counts["research_revived"] = len(stale_research)
                logger.warning(
                    "lead_sweeper: %d research rows stuck in RUNNING for "
                    ">%d min — flipping back to PENDING and re-enqueueing",
                    len(stale_research), STALE_AFTER_MINUTES,
                )

            await session.commit()
    finally:
        await engine.dispose()

    # Re-enqueue outside the transaction so broker hiccups don't roll
    # back the status flip.  Worst case: row is PENDING but no task in
    # the queue — the next sweep tick re-enqueues.
    for lid in stale_compose:
        try:
            compose_lead.delay(str(lid))
        except Exception:  # noqa: BLE001
            logger.exception("lead_sweeper: failed to re-enqueue compose for %s", lid)
    for lid in stale_research:
        try:
            research_lead.delay(str(lid))
        except Exception:  # noqa: BLE001
            logger.exception("lead_sweeper: failed to re-enqueue research for %s", lid)

    return counts


@celery_app.task(name="lead_sweeper.sweep_stale")
def sweep_stale() -> dict[str, int]:
    return asyncio.run(for_all_tenants(_sweep_async))
