"""Deliverability health sweep (Celery beat, every ~15 min).

Backstop for the inline breaker check in ``brevo_events.process_event``
— catches campaigns whose bounce/spam events landed while the inline
check was disabled or raced, and campaigns whose rates crossed the
threshold between event polls.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.models import Campaign, CampaignStatus
from app.services import deliverability
from app.workers.celery_app import celery_app
from app.tenancy.context import for_all_tenants

logger = logging.getLogger(__name__)


async def _sweep_health_async() -> dict[str, Any]:
    if not settings.CIRCUIT_BREAKER_ENABLED:
        return {"skipped": "breaker_disabled"}
    tripped: list[str] = []
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            running_ids = (await session.execute(
                select(Campaign.id).where(Campaign.status == CampaignStatus.RUNNING)
            )).scalars().all()
            for cid in running_ids:
                try:
                    if await deliverability.check_and_trip(session, cid):
                        tripped.append(str(cid))
                except Exception:  # noqa: BLE001 — one bad campaign must not stop the sweep
                    logger.exception("health sweep failed for campaign %s", cid)
            await session.commit()
    finally:
        await engine.dispose()
    if tripped:
        logger.warning("deliverability sweep tripped breaker for: %s", tripped)
    return {"checked": len(running_ids), "tripped": tripped}


@celery_app.task(name="deliverability.sweep_health")
def sweep_health() -> dict[str, Any]:
    return asyncio.run(for_all_tenants(_sweep_health_async))
