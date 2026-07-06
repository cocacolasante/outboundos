"""Hourly refresh of per-campaign copy insights (Feature A).

Compose reads the cached angle summary only — this beat task is the
single place the summariser LLM runs.  A campaign is refreshed when it
has accumulated ≥ ``copy_insights.MIN_NEW_OUTCOMES`` reply outcomes
since its last refresh (or has outcomes but no cache yet); everything
else is skipped at zero cost.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.models import CampaignCopyInsights, ReplyOutcome
from app.services import copy_insights
from app.workers.celery_app import celery_app
from app.tenancy.context import for_all_tenants

logger = logging.getLogger(__name__)


async def _refresh_all_async() -> dict[str, Any]:
    refreshed: list[str] = []
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            # Campaigns with any outcomes at all, with their totals.
            rows = (await session.execute(
                select(ReplyOutcome.campaign_id, func.count())
                .group_by(ReplyOutcome.campaign_id)
            )).all()
            cached = {
                c.campaign_id: c.outcome_count_at_refresh
                for c in (await session.execute(
                    select(CampaignCopyInsights)
                )).scalars()
            }
            for campaign_id, total in rows:
                seen = cached.get(campaign_id)
                if seen is not None and total - seen < copy_insights.MIN_NEW_OUTCOMES:
                    continue
                try:
                    result = await copy_insights.refresh_angle_summary(
                        session, campaign_id,
                    )
                    if result is not None:
                        refreshed.append(str(campaign_id))
                except Exception:  # noqa: BLE001 — one campaign must not stop the rest
                    logger.exception("copy-insights refresh failed for %s", campaign_id)
            await session.commit()
    finally:
        await engine.dispose()
    if refreshed:
        logger.info("copy insights refreshed for: %s", refreshed)
    return {"refreshed": refreshed}


@celery_app.task(name="copy_insights.refresh_all")
def refresh_all() -> dict[str, Any]:
    return asyncio.run(for_all_tenants(_refresh_all_async))
