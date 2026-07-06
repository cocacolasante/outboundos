"""Read-only settings endpoints (no secrets exposed)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.services import brevo_blocklist

router = APIRouter(prefix="/settings", tags=["settings"])


class ApiStatusResponse(BaseModel):
    anthropic: bool
    brevo: bool
    apollo: bool
    hunter: bool


@router.get("/api-status", response_model=ApiStatusResponse)
async def get_api_status(db: AsyncSession = Depends(get_db)) -> ApiStatusResponse:
    """Returns a bool per integration: True if the TENANT has the key
    configured (BYOK since Phase 4 — was the global env)."""
    from app.services.tenant_keys import get_provider_creds

    async def has(provider: str) -> bool:
        return await get_provider_creds(db, provider) is not None

    return ApiStatusResponse(
        anthropic=await has("anthropic"),
        brevo=await has("brevo"),
        apollo=await has("apollo"),
        hunter=await has("hunter"),
    )


class BlocklistSyncResponse(BaseModel):
    fetched: int
    newly_suppressed: int
    already_suppressed: int
    leads_halted: int
    leads_removed: int


@router.post("/brevo/sync-blocklist", response_model=BlocklistSyncResponse)
async def sync_brevo_blocklist(db: AsyncSession = Depends(get_db)) -> BlocklistSyncResponse:
    """Pull Brevo's blocked-contacts list (hard bounces / unsubscribes / spam /
    admin-blocked) and suppress each: add to the ignore list, halt them in
    current campaigns, and block them from future ones.  Idempotent — safe to
    re-run.  Runs the same logic as the scheduled backstop, on demand."""
    from app.services.tenant_keys import get_provider_creds

    if await get_provider_creds(db, "brevo") is None:
        raise HTTPException(status_code=400, detail="Brevo is not configured for this workspace")
    result = await brevo_blocklist.sync_blocklist(db)
    return BlocklistSyncResponse(
        fetched=result.fetched,
        newly_suppressed=result.newly_suppressed,
        already_suppressed=result.already_suppressed,
        leads_halted=result.leads_halted,
        leads_removed=result.leads_removed,
    )


# ---------------------------------------------------------------------------
# BYOK integrations (multi-tenancy Phase 4): per-tenant provider keys.
# Secrets go in encrypted; only masked previews + test status come out.
# ---------------------------------------------------------------------------

import json  # noqa: E402
import uuid as _uuid  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.models import KeyTestStatus, ProviderKind, TenantProviderKey  # noqa: E402
from app.services.encryption import encrypt as _encrypt  # noqa: E402
from app.services.tenant_keys import get_provider_creds, masked_preview  # noqa: E402
from app.tenancy.context import current_tenant_id  # noqa: E402

_PROVIDER_FIELDS: dict[str, tuple[str, ...]] = {
    "anthropic": ("api_key",),
    "brevo": ("api_key", "sender_email", "sender_name"),
    "apollo": ("api_key",),
    "hunter": ("api_key",),
    "unipile": ("dsn", "api_key"),
}


class IntegrationUpdate(BaseModel):
    api_key: str
    sender_email: str | None = None
    sender_name: str | None = None
    dsn: str | None = None


class IntegrationStatus(BaseModel):
    provider: str
    configured: bool
    masked: str | None = None
    last_test_status: str | None = None
    last_tested_at: datetime | None = None
    last_test_error: str | None = None


def _provider_or_404(provider: str) -> ProviderKind:
    try:
        return ProviderKind(provider)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"unknown provider {provider!r}")


async def _tenant_row(db: AsyncSession, kind: ProviderKind) -> TenantProviderKey | None:
    q = select(TenantProviderKey).where(TenantProviderKey.provider == kind)
    tid = current_tenant_id.get()
    if tid is not None:
        q = q.where(TenantProviderKey.tenant_id == tid)
    return (await db.execute(q.limit(1))).scalars().first()


@router.get("/integrations", response_model=list[IntegrationStatus])
async def list_integrations(db: AsyncSession = Depends(get_db)) -> list[IntegrationStatus]:
    out = []
    for provider in _PROVIDER_FIELDS:
        row = await _tenant_row(db, ProviderKind(provider))
        out.append(IntegrationStatus(
            provider=provider,
            configured=row is not None,
            masked=masked_preview(row.encrypted_credentials, provider) if row else None,
            last_test_status=row.last_test_status.value if row else None,
            last_tested_at=row.last_tested_at if row else None,
            last_test_error=row.last_test_error if row else None,
        ))
    return out


@router.put("/integrations/{provider}", response_model=IntegrationStatus)
async def put_integration(
    provider: str, body: IntegrationUpdate, db: AsyncSession = Depends(get_db),
) -> IntegrationStatus:
    kind = _provider_or_404(provider)
    if not body.api_key.strip():
        raise HTTPException(status_code=422, detail="api_key must not be empty")
    blob = {
        field: (getattr(body, field, None) or "").strip()
        for field in _PROVIDER_FIELDS[provider]
    }
    blob["api_key"] = body.api_key.strip()
    encrypted = _encrypt(json.dumps(blob))

    row = await _tenant_row(db, kind)
    if row is None:
        row = TenantProviderKey(provider=kind, encrypted_credentials=encrypted)
        db.add(row)
    else:
        row.encrypted_credentials = encrypted
        row.last_test_status = KeyTestStatus.UNTESTED
        row.last_tested_at = None
        row.last_test_error = None
    await db.commit()
    await db.refresh(row)
    return IntegrationStatus(
        provider=provider,
        configured=True,
        masked=masked_preview(row.encrypted_credentials, provider),
        last_test_status=row.last_test_status.value,
        last_tested_at=row.last_tested_at,
        last_test_error=row.last_test_error,
    )


@router.delete("/integrations/{provider}", status_code=204, response_model=None)
async def delete_integration(provider: str, db: AsyncSession = Depends(get_db)) -> None:
    kind = _provider_or_404(provider)
    row = await _tenant_row(db, kind)
    if row is None:
        raise HTTPException(status_code=404, detail="not configured")
    await db.delete(row)
    await db.commit()


async def _probe_provider(provider: str, creds) -> None:
    """One cheap authenticated call; raises on failure."""
    timeout = 15
    async with httpx.AsyncClient(timeout=timeout) as client:
        if provider == "anthropic":
            r = await client.get(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": creds.api_key, "anthropic-version": "2023-06-01"},
            )
        elif provider == "brevo":
            r = await client.get(
                "https://api.brevo.com/v3/account",
                headers={"api-key": creds.api_key},
            )
        elif provider == "apollo":
            r = await client.get(
                "https://api.apollo.io/v1/auth/health",
                params={"api_key": creds.api_key},
            )
        elif provider == "hunter":
            r = await client.get(
                "https://api.hunter.io/v2/account",
                params={"api_key": creds.api_key},
            )
        else:  # unipile
            r = await client.get(
                f"https://{creds.dsn}/api/v1/accounts",
                headers={"X-API-KEY": creds.api_key},
            )
        r.raise_for_status()


class WebhookRegistrationResult(BaseModel):
    created: list[str]
    deleted_stale: int
    request_url: str


@router.post("/integrations/unipile/register-webhooks",
             response_model=WebhookRegistrationResult)
async def register_unipile_webhooks(db: AsyncSession = Depends(get_db)) -> WebhookRegistrationResult:
    """Register our inbound webhooks on THIS TENANT's Unipile workspace
    (Phase 8 — closes the BYOK inbound half: without these, the tenant's
    LinkedIn replies / connection-accepts never reach us).  Idempotent —
    safe to re-run after a WEBHOOK_BASE_URL change."""
    from app.services.tenant_keys import ensure_unipile_webhook_secret
    from app.services.unipile_webhooks import register_tenant_webhooks

    tid = current_tenant_id.get()
    creds = await get_provider_creds(db, "unipile", tenant_id=tid)
    if creds is None or not creds.dsn:
        raise HTTPException(status_code=400, detail="Unipile is not configured for this workspace")
    secret = await ensure_unipile_webhook_secret(db, tid)
    try:
        result = await register_tenant_webhooks(creds, tid, secret)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Unipile webhook API error: {exc.response.status_code}",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return WebhookRegistrationResult(**result)


@router.post("/integrations/{provider}/test", response_model=IntegrationStatus)
async def test_integration(provider: str, db: AsyncSession = Depends(get_db)) -> IntegrationStatus:
    kind = _provider_or_404(provider)
    row = await _tenant_row(db, kind)
    if row is None:
        raise HTTPException(status_code=404, detail="not configured")
    creds = await get_provider_creds(db, provider)
    error: str | None = None
    if creds is None:
        error = "stored credentials are empty or unreadable"
    else:
        try:
            await _probe_provider(provider, creds)
        except Exception as exc:  # noqa: BLE001 — the outcome IS the result
            error = str(exc)[:500]
    row.last_test_status = KeyTestStatus.FAILED if error else KeyTestStatus.OK
    row.last_tested_at = datetime.now(timezone.utc)
    row.last_test_error = error
    await db.commit()
    return IntegrationStatus(
        provider=provider,
        configured=True,
        masked=masked_preview(row.encrypted_credentials, provider),
        last_test_status=row.last_test_status.value,
        last_tested_at=row.last_tested_at,
        last_test_error=row.last_test_error,
    )
