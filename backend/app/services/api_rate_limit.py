"""Per-tenant API rate limiting (Phase 7 hardening).

Fixed-window counter per (tenant, minute) in Redis, checked by ``get_db``
right after authentication — one noisy tenant can't starve the API for
the rest.  Deliberately FAIL-OPEN: a Redis outage must degrade to
"no rate limiting", never to "API down" (availability over strictness
for this control; the money paths have their own gates).

``API_RATE_LIMIT_PER_MINUTE`` (default 600; 0 disables).

The client is a module-level singleton — safe here because the API
process runs ONE event loop (uvicorn); workers never import this
(their loops are per-task, the reason worker code builds fresh clients).
"""
from __future__ import annotations

import logging
import time
import uuid

import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger(__name__)

_client: aioredis.Redis | None = None


def _redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _client


async def allow_request(tenant_id: uuid.UUID) -> bool:
    """True when the tenant is under this minute's budget (or limiting is
    disabled / Redis is down)."""
    limit = settings.API_RATE_LIMIT_PER_MINUTE
    if limit <= 0:
        return True
    minute = int(time.time() // 60)
    key = f"api-rate:{tenant_id}:{minute}"
    try:
        client = _redis()
        count = await client.incr(key)
        if count == 1:
            await client.expire(key, 120)
        return count <= limit
    except Exception:  # noqa: BLE001 — fail-open by design
        logger.warning("api rate limiter unavailable — allowing request")
        return True
