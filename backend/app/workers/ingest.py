"""Campaign ingest tasks.

run_campaign_research fans out research_lead jobs for every pending lead in a
campaign, ordering samples ahead of the rest. The actual research workflow
lives in app.workers.research (Phase 6).
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.models import Campaign, Lead, ResearchStatus
from app.workers.celery_app import celery_app
from app.tenancy.context import with_record_tenant
from app.workers.research import research_lead

logger = logging.getLogger(__name__)


async def run_campaign_research_async(campaign_id: str | uuid.UUID) -> dict[str, int]:
    """Read pending leads for a campaign and queue research jobs.

    Returns a dict with counters for samples and other leads enqueued. The
    per-call engine keeps asyncpg pools bound to the single fresh event loop
    that asyncio.run creates inside the Celery task.
    """
    cid = uuid.UUID(str(campaign_id))

    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            sample_ids = list((await session.execute(
                select(Lead.id)
                .where(
                    Lead.campaign_id == cid,
                    Lead.research_status == ResearchStatus.PENDING,
                    Lead.is_sample.is_(True),
                )
                .order_by(Lead.created_at.asc())
            )).scalars())

            other_ids = list((await session.execute(
                select(Lead.id)
                .where(
                    Lead.campaign_id == cid,
                    Lead.research_status == ResearchStatus.PENDING,
                    Lead.is_sample.is_(False),
                )
                .order_by(Lead.created_at.asc())
            )).scalars())
    finally:
        await engine.dispose()

    samples_enqueued = 0
    for lid in sample_ids:
        research_lead.delay(str(lid))
        samples_enqueued += 1

    others_enqueued = 0
    for lid in other_ids:
        research_lead.delay(str(lid))
        others_enqueued += 1

    logger.info(
        "Enqueued research jobs for campaign %s: %d samples, %d others",
        cid, samples_enqueued, others_enqueued,
    )
    return {"samples_enqueued": samples_enqueued, "others_enqueued": others_enqueued}


@celery_app.task(name="ingest.run_campaign_research")
def run_campaign_research(campaign_id: str) -> dict[str, int]:
    return asyncio.run(with_record_tenant(Campaign, campaign_id, run_campaign_research_async, campaign_id))
