"""Periodically sync Brevo's blocked-contacts list into our suppression list.

Brevo's blocked list (hard bounces / unsubscribes / spam / admin-blocked) is
the authoritative record of recipients it won't deliver to.  This beat task
pulls it and runs each address through the shared suppression core, so those
leads land on the ignore list, get halted in current campaigns, and are blocked
from future ones.

Cadence: ``BREVO_BLOCKLIST_SYNC_INTERVAL_MINUTES`` (default 360 = every 6h).
The real-time events poller already suppresses on bounce/spam/unsubscribe/
blocked events; this sweep is the backstop that also catches anything the
event window missed and reconciles the full list.  ``acks_late=False`` so a
long run isn't redelivered into a duplicate (suppression is idempotent anyway).
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.services import brevo_blocklist
from app.workers.celery_app import celery_app
from app.tenancy.context import for_all_tenants

logger = logging.getLogger(__name__)


async def _sync_async() -> dict[str, int]:
    if not settings.BREVO_API_KEY:
        return {"skipped": "no_brevo_api_key"}
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            try:
                result = await brevo_blocklist.sync_blocklist(session)
            except Exception:  # noqa: BLE001 — never let a Brevo outage crash the beat
                logger.exception("brevo blocklist sync failed")
                return {"error": "sync_failed"}
    finally:
        await engine.dispose()
    return {
        "fetched": result.fetched,
        "newly_suppressed": result.newly_suppressed,
        "already_suppressed": result.already_suppressed,
        "leads_halted": result.leads_halted,
        "leads_removed": result.leads_removed,
    }


@celery_app.task(name="brevo_blocklist_sync.sync", acks_late=False)
def sync_brevo_blocklist() -> dict[str, int]:
    return asyncio.run(for_all_tenants(_sync_async))
