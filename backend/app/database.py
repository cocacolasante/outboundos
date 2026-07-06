from typing import AsyncGenerator

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings
from app.tenancy.context import current_tenant_id


class Base(DeclarativeBase):
    pass


# Runtime engine: the non-owner APP_DATABASE_URL when configured (required
# once RLS enforcement lands — owners bypass policies), else the owner
# DATABASE_URL (single-tenant dev reality).  Alembic always uses the owner.
engine = create_async_engine(
    settings.APP_DATABASE_URL or settings.DATABASE_URL,
    echo=False,
    future=True,
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# Owner-DSN engine for the deliberately tenant-blind surfaces (webhooks,
# unsubscribe): they resolve records globally by external identifiers and
# must not be filtered by RLS.  RLS-exempt by ROLE (owners bypass policies);
# sessions are additionally marked so the GUC listener skips them.
service_engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    future=True,
    pool_pre_ping=True,
)

ServiceSessionLocal = async_sessionmaker(
    service_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_service_db() -> AsyncGenerator[AsyncSession, None]:
    """TENANT-BLIND database session on the owner engine (bypasses RLS).

    Only the webhook/unsubscribe surfaces may depend on this — they
    authenticate by other means (HMAC token / static header), resolve rows
    globally, and must stamp ``tenant_id`` EXPLICITLY on anything they
    create (the TenantMixin default has no ambient tenant here).  A
    hardening test greps consumers against an allowlist.
    """
    async with ServiceSessionLocal() as session:
        session.info["rls_exempt"] = True
        try:
            yield session
        finally:
            await session.close()


async def get_public_db() -> AsyncGenerator[AsyncSession, None]:
    """UNAUTHENTICATED database session.

    Multi-tenancy Phase 1: only auth endpoints and the deliberately
    tenant-blind public surfaces (webhooks, unsubscribe) may depend on
    this — everything else goes through :func:`get_db`, which requires
    a live session cookie.  A hardening test greps for consumers
    against an allowlist; do not add one without updating it.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def get_db(
    request: Request,
    session: AsyncSession = Depends(get_public_db),
) -> AsyncGenerator[AsyncSession, None]:
    """Authenticated, tenant-scoped database session.

    Every feature router depends on this (unchanged import surface).
    Since Phase 1 it authenticates the request's session cookie,
    resolves the active tenant, and holds ``current_tenant_id`` for
    the duration of the request — Phase 2 hangs insert-stamping off
    that ContextVar and Phase 3's RLS listener reads it.

    The auth import is lazy: ``app.auth.deps`` imports the identity
    models, which import ``Base`` from this module — a module-level
    import here would be a circular import.
    """
    from fastapi import HTTPException
    from sqlalchemy import text

    from app.auth.deps import authenticate_request
    from app.services.api_rate_limit import allow_request
    from app.services.tenant_keys import init_creds_cache, reset_creds_cache

    tenant_id = await authenticate_request(request, session)
    # Per-tenant API rate limit (Phase 7) — after auth so the key is the
    # tenant, before any work.  Fail-open inside allow_request.
    if not await allow_request(tenant_id):
        raise HTTPException(status_code=429, detail="rate limit exceeded — slow down")
    token = current_tenant_id.set(tenant_id)
    cache_token = init_creds_cache()
    try:
        # The auth queries above already opened this session's transaction
        # (autobegin) BEFORE the ContextVar was set, so the after_begin
        # listener had nothing to stamp — set the GUC on the live
        # transaction explicitly; the listener covers every later one
        # (post-commit) in the request.
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        yield session
    finally:
        reset_creds_cache(cache_token)
        current_tenant_id.reset(token)
