"""Register our webhooks on a TENANT's Unipile workspace (Phase 8).

BYOK closed the outbound half (tenant API keys); this closes the inbound
half: without webhooks on THEIR workspace, a tenant's LinkedIn replies /
connection-accepts never reach us.  Creates the three canonical webhooks
(messaging, account_status, users) pointing at
``{WEBHOOK_BASE_URL}/webhooks/unipile/{tenant_id}`` with the tenant's own
auth secret.  Idempotent: any existing webhook on the workspace that
points at OUR base URL is deleted first, so re-running (new tunnel host,
rotated secret) converges.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx

from app.config import settings
from app.services.tenant_keys import UnipileCreds

logger = logging.getLogger(__name__)

_SOURCES = ("messaging", "account_status", "users")
_TIMEOUT = 30


async def register_tenant_webhooks(
    creds: UnipileCreds, tenant_id: uuid.UUID, secret: str,
) -> dict[str, Any]:
    """Delete-ours-then-recreate on the tenant's workspace.  Raises
    httpx.HTTPStatusError on Unipile API failures (the settings endpoint
    surfaces it)."""
    if not creds.dsn or not creds.api_key:
        raise ValueError("Unipile is not configured for this workspace")
    base = f"https://{creds.dsn}/api/v1/webhooks"
    request_url = f"{settings.WEBHOOK_BASE_URL.rstrip('/')}/webhooks/unipile/{tenant_id}"
    our_prefix = f"{settings.WEBHOOK_BASE_URL.rstrip('/')}/webhooks/unipile"
    headers = {"X-API-KEY": creds.api_key, "accept": "application/json"}

    deleted = 0
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(base, headers=headers)
        resp.raise_for_status()
        payload = resp.json() or {}
        existing = payload.get("items") if isinstance(payload, dict) else payload
        for wh in existing or []:
            if not isinstance(wh, dict):
                continue
            if str(wh.get("request_url") or "").startswith(our_prefix):
                wh_id = wh.get("id") or wh.get("webhook_id")
                if wh_id:
                    r = await client.delete(f"{base}/{wh_id}", headers=headers)
                    if r.status_code < 400:
                        deleted += 1

        created: list[str] = []
        for source in _SOURCES:
            resp = await client.post(
                base,
                headers={**headers, "content-type": "application/json"},
                json={
                    "name": f"outboundos - {source}",
                    "request_url": request_url,
                    "source": source,
                    "headers": [
                        {"key": "Content-Type", "value": "application/json"},
                        {"key": settings.UNIPILE_WEBHOOK_AUTH_HEADER, "value": secret},
                    ],
                },
            )
            resp.raise_for_status()
            created.append(source)

    logger.info(
        "registered %d unipile webhooks for tenant %s at %s (removed %d stale)",
        len(created), tenant_id, request_url, deleted,
    )
    return {"created": created, "deleted_stale": deleted, "request_url": request_url}
