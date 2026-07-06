"""Ambient tenant context + the RLS GUC plumbing (multi-tenancy Phases 1-3).

One ContextVar carries the active tenant id for the current request /
worker unit-of-work:

- Phase 1: the request-path ``get_db`` sets it after authentication.
- Phase 2: the ``TenantMixin`` insert default stamps it onto new rows.
- Phase 3: the ``after_begin`` listener below writes it into the
  ``app.tenant_id`` Postgres GUC on EVERY transaction, which is what the
  row-level-security policies key on.  Workers enter tenant context via
  :func:`run_for_tenant` (single-record tasks derive the tenant from the
  record via :func:`tenant_of`; sweeps iterate :func:`list_tenant_ids`).

This is deliberately the ONLY ambient tenancy state in the app —
provider credentials etc. are threaded as explicit parameters
(docs/tenancy-plan.md §4).
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import AsyncIterator

from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import Session

from app.config import settings

current_tenant_id: ContextVar[uuid.UUID | None] = ContextVar(
    "current_tenant_id", default=None,
)


@event.listens_for(Session, "after_begin")
def _stamp_tenant_guc(session: Session, transaction, connection) -> None:
    """Write the ambient tenant into the transaction-local ``app.tenant_id``
    GUC at every transaction begin.

    Transaction-local (``set_config(..., true)``) — never session-level SET:
    the pool reset is rollback-only, so a session-level GUC would leak
    across checkouts of the shared API engine.  A listener rather than a
    set-once contextmanager because ``AsyncSessionLocal`` autobegins and a
    mid-flight commit starts a fresh transaction — a one-shot SET would
    silently vanish and RLS would hide every row.

    Skipped for nested transactions (the outer txn already carries it) and
    for sessions the service path marks ``rls_exempt`` (they run on the
    owner engine, which bypasses RLS by role).
    """
    if transaction.nested or session.info.get("rls_exempt"):
        return
    tid = current_tenant_id.get()
    if tid is not None:
        connection.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tid)},
        )


def worker_engine() -> AsyncEngine:
    """Fresh per-invocation engine for Celery task bodies (each task runs
    in its own ``asyncio.run`` loop, so engines are never shared).  Uses
    the non-owner runtime DSN when configured — required once RLS is on."""
    return create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)


def service_worker_engine() -> AsyncEngine:
    """Owner-DSN engine for the narrow tenant-blind worker lookups
    (:func:`tenant_of`).  RLS-exempt by ROLE — use only through the
    helpers here; a hardening test greps for consumers."""
    return create_async_engine(settings.DATABASE_URL)


@asynccontextmanager
async def run_for_tenant(
    tenant_id: uuid.UUID | None, engine: AsyncEngine,
) -> AsyncIterator[AsyncSession]:
    """One tenant-scoped unit of work: sets the ContextVar (the listener
    stamps the GUC on every transaction; the TenantMixin default stamps
    inserts) around a fresh session."""
    from app.services.tenant_keys import init_creds_cache, reset_creds_cache

    token = current_tenant_id.set(tenant_id)
    cache_token = init_creds_cache()
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session
    finally:
        reset_creds_cache(cache_token)
        current_tenant_id.reset(token)


async def list_tenant_ids(engine: AsyncEngine) -> list[uuid.UUID]:
    """Active tenant ids, oldest first.  The tenants table carries no RLS,
    so this works on the normal worker engine without tenant context."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        rows = await session.execute(
            text("SELECT id FROM tenants WHERE status = 'active' ORDER BY created_at")
        )
        return [r[0] for r in rows]


async def default_tenant_id(engine: AsyncEngine) -> uuid.UUID | None:
    """The oldest tenant — the single-operator reality's owner.  Interim
    home for the app-global collectors (funding/intent/ICP/digest) until
    their config moves per-tenant in the BYOK phase."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        row = await session.execute(
            text("SELECT id FROM tenants ORDER BY created_at LIMIT 1")
        )
        first = row.first()
        return first[0] if first else None


async def for_all_tenants(fn, *args, **kwargs) -> dict[str, object]:
    """Run a sweep body once per active tenant, oldest first.

    The task-level tenancy wrapper for beat sweeps: the body keeps its own
    engine/session lifecycle UNCHANGED — this only holds the ambient
    ContextVar per iteration, which the GUC listener turns into RLS
    scoping and the TenantMixin turns into insert stamping.  Per-tenant
    failure isolation: one tenant's exception is logged and the loop
    continues.  Returns ``{tenant_id: body_result | "error"}``.
    """
    import logging

    logger = logging.getLogger(__name__)
    engine = worker_engine()
    try:
        tenant_ids = await list_tenant_ids(engine)
    finally:
        await engine.dispose()

    from app.services.tenant_keys import init_creds_cache, reset_creds_cache

    results: dict[str, object] = {}
    for tid in tenant_ids:
        token = current_tenant_id.set(tid)
        cache_token = init_creds_cache()
        try:
            results[str(tid)] = await fn(*args, **kwargs)
        except Exception:  # noqa: BLE001 — isolation between tenants
            logger.exception("per-tenant sweep failed for tenant %s", tid)
            results[str(tid)] = "error"
        finally:
            reset_creds_cache(cache_token)
            current_tenant_id.reset(token)
    return results


async def with_record_tenant(model, pk, fn, *args, **kwargs):
    """Run a single-record task body under the tenant derived from the
    record (docs/tenancy-plan.md §3 derive-from-record).  Task signatures
    stay stable — the tenant never rides the broker message.  A missing
    record resolves to no tenant; under RLS the body then sees nothing and
    exits through its own ``row is None`` guard."""
    from app.services.tenant_keys import init_creds_cache, reset_creds_cache

    tid = await tenant_of(model, pk)
    token = current_tenant_id.set(tid)
    cache_token = init_creds_cache()
    try:
        return await fn(*args, **kwargs)
    finally:
        reset_creds_cache(cache_token)
        current_tenant_id.reset(token)


async def with_default_tenant(fn, *args, **kwargs):
    """Run an app-global collector (funding/intent/ICP/digest/blocklist)
    under the OLDEST tenant — the single-operator interim until their
    config moves per-tenant in the BYOK phase."""
    from app.services.tenant_keys import init_creds_cache, reset_creds_cache

    engine = worker_engine()
    try:
        tid = await default_tenant_id(engine)
    finally:
        await engine.dispose()
    token = current_tenant_id.set(tid)
    cache_token = init_creds_cache()
    try:
        return await fn(*args, **kwargs)
    finally:
        reset_creds_cache(cache_token)
        current_tenant_id.reset(token)


async def tenant_of(model, pk) -> uuid.UUID | None:
    """Resolve a record's tenant WITHOUT tenant context (the row is
    invisible to the runtime role until the GUC is set — chicken-and-egg),
    via a short-lived owner-DSN engine.  The only worker-side RLS-exempt
    lookup; single-record tasks call it once, then enter
    :func:`run_for_tenant`.  Returns None when the record is gone."""
    if isinstance(pk, str):
        try:
            pk = uuid.UUID(pk)
        except ValueError:
            return None
    engine = service_worker_engine()
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            session.info["rls_exempt"] = True
            return await session.scalar(
                select(model.tenant_id).where(model.id == pk)
            )
    finally:
        await engine.dispose()
