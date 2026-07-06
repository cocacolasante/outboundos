"""BYOK credential resolution (multi-tenancy Phase 4).

The ONE place tenant provider credentials are read and decrypted.
Resolution contract:

- **In tenant context** (the ``current_tenant_id`` ContextVar is set —
  every request via ``get_db``, every worker via the task-level tenancy
  wrappers): the tenant's own encrypted row is the ONLY source.  No
  global/env fallback — a tenant without a key gets ``None``, exactly
  like today's "key not configured" soft paths.  This is the locked
  full-BYOK decision: the platform never fronts tenant spend.
- **With no tenant context** (only the deliberately tenant-blind paths:
  the brevo events poller until it goes per-tenant, service surfaces):
  fall back to the platform env settings.

Plaintext handling follows the encryption.py contract: decrypted material
lives in the returned frozen dataclass only — never logged, never
serialized into API responses (the settings router returns masked
previews).  This module is on the decrypt-allowlist hardening test.

Caching: per unit-of-work only, via a ContextVar dict that the tenancy
wrappers / ``get_db`` initialize and clear with the same token discipline
as the tenant id itself — never module-global, so credentials can never
leak across tenants or outlive their unit of work.
"""
from __future__ import annotations

import json
import uuid
from contextvars import ContextVar
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.models.tenant_keys import ProviderKind, TenantProviderKey
from app.services import encryption
from app.tenancy.context import current_tenant_id


@dataclass(frozen=True)
class AnthropicCreds:
    api_key: str


@dataclass(frozen=True)
class BrevoCreds:
    api_key: str
    sender_email: str = ""
    sender_name: str = ""


@dataclass(frozen=True)
class ApolloCreds:
    api_key: str


@dataclass(frozen=True)
class HunterCreds:
    api_key: str


@dataclass(frozen=True)
class UnipileCreds:
    dsn: str
    api_key: str


_PARSERS = {
    "anthropic": lambda b: AnthropicCreds(api_key=b.get("api_key", "")),
    "brevo": lambda b: BrevoCreds(
        api_key=b.get("api_key", ""),
        sender_email=b.get("sender_email", ""),
        sender_name=b.get("sender_name", ""),
    ),
    "apollo": lambda b: ApolloCreds(api_key=b.get("api_key", "")),
    "hunter": lambda b: HunterCreds(api_key=b.get("api_key", "")),
    "unipile": lambda b: UnipileCreds(
        dsn=b.get("dsn", ""), api_key=b.get("api_key", ""),
    ),
}

# Per-unit-of-work credentials cache: (tenant_id, provider) -> creds|None.
# Initialized by the tenancy wrappers / get_db (see reset_creds_cache);
# when uninitialized (direct service-level calls in tests) nothing caches.
_creds_cache: ContextVar[dict | None] = ContextVar("_creds_cache", default=None)


def init_creds_cache():
    """Start a fresh per-unit-of-work cache; returns the reset token."""
    return _creds_cache.set({})


def reset_creds_cache(token) -> None:
    _creds_cache.reset(token)


def _env_creds(provider: str):
    """Platform env fallback — ONLY for tenant-blind (no-context) paths."""
    if provider == "anthropic" and settings.ANTHROPIC_API_KEY:
        return AnthropicCreds(api_key=settings.ANTHROPIC_API_KEY)
    if provider == "brevo" and settings.BREVO_API_KEY:
        return BrevoCreds(
            api_key=settings.BREVO_API_KEY,
            sender_email=settings.BREVO_SENDER_EMAIL,
            sender_name=settings.BREVO_SENDER_NAME,
        )
    if provider == "apollo" and settings.APOLLO_API_KEY:
        return ApolloCreds(api_key=settings.APOLLO_API_KEY)
    if provider == "hunter" and settings.HUNTER_API_KEY:
        return HunterCreds(api_key=settings.HUNTER_API_KEY)
    if provider == "unipile" and settings.UNIPILE_API_KEY:
        return UnipileCreds(dsn=settings.UNIPILE_DSN, api_key=settings.UNIPILE_API_KEY)
    return None


def _parse_row(provider: str, encrypted: str):
    blob = json.loads(encryption.decrypt(encrypted))
    creds = _PARSERS[provider](blob)
    # An entered-but-empty key behaves like not-configured.
    return creds if getattr(creds, "api_key", "") else None


async def get_provider_creds(
    session: AsyncSession, provider: str | ProviderKind,
    tenant_id: uuid.UUID | None = None,
):
    """Resolve creds on an EXISTING session (settings router, tests).
    Tenant scoping comes from RLS/the ambient context; pass ``tenant_id``
    to filter explicitly on unscoped sessions."""
    provider = provider.value if isinstance(provider, ProviderKind) else provider
    q = select(TenantProviderKey).where(
        TenantProviderKey.provider == ProviderKind(provider)
    )
    tid = tenant_id if tenant_id is not None else current_tenant_id.get()
    if tid is not None:
        q = q.where(TenantProviderKey.tenant_id == tid)
    row = (await session.execute(q.limit(1))).scalars().first()
    if row is None:
        return None
    return _parse_row(provider, row.encrypted_credentials)


async def ambient_creds(provider: str):
    """Resolve creds for the ambient tenant (or the platform env when no
    tenant context exists).  Returns None when not configured — callers
    keep their existing soft-fail semantics."""
    tid = current_tenant_id.get()
    cache = _creds_cache.get()
    cache_key = (str(tid), provider)
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    if tid is None:
        creds = _env_creds(provider)
    else:
        engine = create_async_engine(
            settings.APP_DATABASE_URL or settings.DATABASE_URL
        )
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                creds = await get_provider_creds(session, provider, tenant_id=tid)
        finally:
            await engine.dispose()

    if cache is not None:
        cache[cache_key] = creds
    return creds


def masked_preview(encrypted: str, provider: str) -> str:
    """Safe display form of a stored credential — last 4 of the api_key.
    Decryption stays inside this module (allowlist)."""
    try:
        blob = json.loads(encryption.decrypt(encrypted))
    except Exception:  # noqa: BLE001 — a corrupt row must not 500 settings
        return "••••"
    key = blob.get("api_key", "")
    return f"••••{key[-4:]}" if len(key) >= 4 else "••••"


async def ambient_api_key(provider: str) -> str | None:
    """Convenience: just the api_key string (None when unconfigured)."""
    creds = await ambient_creds(provider)
    return getattr(creds, "api_key", None) if creds else None
