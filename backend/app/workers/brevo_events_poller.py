"""Poll Brevo's transactional events API in lieu of the inbound webhook.

Brevo's outbound webhook would push events at us in real time, but it
needs a public tunnel and (on some plans) the Inbound Parsing add-on.
Polling the events endpoint outbound is free with the regular API key
and works behind the localhost-only port binding we use everywhere else.

Cadence: ``BREVO_EVENTS_POLL_INTERVAL_MINUTES`` (default 10).  Lag is
therefore up to 10 min from a real-world event (delivered, bounced,
opened, etc.) to the database row.  Acceptable for cold outreach where
hourly decisions are the norm.

Watermark: ``brevo:events:last_polled_at`` in Redis, ISO-8601 UTC.  We
fetch from ``max(watermark - 5min, today - 1d)`` to ``now`` so a worker
restart catches anything that landed during the gap.  Events older than
24h won't be back-filled — that's a deliberate floor to keep the API
query bounded; the per-event dedup in ``brevo_events.process_event``
makes re-fetching the same window cheap regardless.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.services import brevo
from app.services.brevo_events import process_event
from app.workers.celery_app import celery_app
from app.tenancy.context import for_all_tenants

logger = logging.getLogger(__name__)

# Per-tenant since BYOK (Phase 4): each tenant polls with their own key,
# so each gets their own watermark.  Fresh keys start at the 24h lookback
# floor; process_event's dedup makes the overlap re-scan a no-op.
_WATERMARK_PREFIX = "brevo:events:last_polled_at"


def _watermark_key() -> str:
    from app.tenancy.context import current_tenant_id

    tid = current_tenant_id.get()
    return f"{_WATERMARK_PREFIX}:{tid}" if tid is not None else _WATERMARK_PREFIX


LOOKBACK_FLOOR_HOURS = 24
POLL_OVERLAP_MINUTES = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_redis() -> aioredis.Redis:
    """Fresh per-task client — Celery prefork tasks each get their own
    event loop, and a module-level client carries connection state bound
    to a dead loop.
    """
    return aioredis.from_url(settings.REDIS_URL, decode_responses=True)


def _parse_event_date(raw: Any) -> datetime | None:
    """Brevo emits dates like ``2026-05-18T14:23:00.123Z`` or
    ``2026-05-18T14:23:00+02:00``.  Be tolerant of trailing Z."""
    if not raw:
        return None
    s = str(raw).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


async def _poll_async() -> dict[str, int]:
    counts = {"fetched": 0, "processed": 0, "skipped_old": 0}
    # BYOK (Phase 4): runs per tenant via for_all_tenants — the tenant's
    # own Brevo creds resolve ambiently in fetch_events and the matching
    # SELECTs are RLS-scoped to the tenant.  Tenants without a Brevo key
    # skip entirely (before any client is opened).
    from app.services.tenant_keys import ambient_creds

    if await ambient_creds("brevo") is None:
        return {"skipped": "no_brevo_key"}
    redis_client = _new_redis()
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        now = _now()
        floor = now - timedelta(hours=LOOKBACK_FLOOR_HOURS)

        wm_raw = await redis_client.get(_watermark_key())
        watermark: datetime
        if wm_raw:
            try:
                watermark = datetime.fromisoformat(wm_raw)
                if watermark.tzinfo is None:
                    watermark = watermark.replace(tzinfo=timezone.utc)
            except ValueError:
                watermark = floor
        else:
            watermark = floor

        # Pull back the watermark slightly so we don't miss events that
        # landed at Brevo between our two polls.
        query_start = max(watermark - timedelta(minutes=POLL_OVERLAP_MINUTES), floor)

        try:
            events = await brevo.fetch_events(
                start_date=query_start.date().isoformat(),
                end_date=now.date().isoformat(),
            )
        except Exception:  # noqa: BLE001
            logger.exception("brevo.fetch_events failed")
            return counts

        counts["fetched"] = len(events)
        max_event_date = watermark

        async with AsyncSession(engine, expire_on_commit=False) as session:
            for ev in events:
                ev_date = _parse_event_date(ev.get("date"))
                if ev_date is None:
                    continue
                if ev_date <= watermark:
                    counts["skipped_old"] += 1
                    continue
                try:
                    recorded = await process_event(session, ev)
                except Exception:  # noqa: BLE001
                    logger.exception("brevo process_event failed for %r", ev)
                    continue
                if recorded:
                    counts["processed"] += 1
                if ev_date > max_event_date:
                    max_event_date = ev_date
            await session.commit()

        # Move the watermark to the newest event we actually saw, capped
        # at "now" (clock-skew safety).
        new_watermark = min(max_event_date, now)
        await redis_client.set(_watermark_key(), new_watermark.isoformat())
    finally:
        await engine.dispose()
        await redis_client.aclose()
    return counts


@celery_app.task(name="brevo_events_poller.poll")
def poll_brevo_events() -> dict[str, int]:
    return asyncio.run(for_all_tenants(_poll_async))
