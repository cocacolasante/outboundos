"""Multi-tenancy Phase 3: the cross-tenant isolation suite.

Runs against a DEDICATED database built by ``alembic upgrade head`` (so the
NOT NULL promotion + RLS policies from 0045/0046 are live — the regular
test DB is built from ``Base.metadata.create_all`` and has neither), with a
non-owner NOBYPASSRLS role standing in for production's ``app_user``.

Covers, per the build brief: tenant A can never read/update/delete tenant
B's rows (DB-level AND through the routers with real auth), missing GUC
fails closed to zero rows, and WITH CHECK rejects cross-tenant writes.
This suite must stay green for the rest of the project.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import uuid

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

RLS_DB = "emailblaster_rls_test"
ADMIN_DSN = "postgresql://emailblaster:emailblaster@postgres:5432/postgres"
OWNER_DSN_PG = f"postgresql://emailblaster:emailblaster@postgres:5432/{RLS_DB}"
OWNER_DSN = f"postgresql+asyncpg://emailblaster:emailblaster@postgres:5432/{RLS_DB}"
APP_ROLE = "rls_test_user"
APP_DSN = f"postgresql+asyncpg://{APP_ROLE}:rls-test-pw@postgres:5432/{RLS_DB}"

TENANT_A = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
TENANT_B = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
CAMPAIGN_A = uuid.UUID("aaaaaaaa-1111-4000-8000-000000000001")
CAMPAIGN_B = uuid.UUID("bbbbbbbb-1111-4000-8000-000000000002")


async def _build_rls_db() -> None:
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{RLS_DB}" WITH (FORCE)')
        await admin.execute(f'CREATE DATABASE "{RLS_DB}"')
    finally:
        await admin.close()

    env = {**os.environ, "DATABASE_URL": OWNER_DSN}
    result = subprocess.run(
        ["alembic", "upgrade", "head"], cwd="/app", env=env,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, f"alembic upgrade failed:\n{result.stderr}"

    owner = await asyncpg.connect(OWNER_DSN_PG)
    try:
        # Non-owner runtime role — same attributes bootstrap_db.sql gives
        # app_user (the role is cluster-wide, so the suite uses its own
        # name to avoid clobbering a dev app_user password).
        await owner.execute(f"""
            DO $$ BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} LOGIN PASSWORD 'rls-test-pw'
                  NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
              END IF;
            END $$;
        """)
        await owner.execute(f"ALTER ROLE {APP_ROLE} NOSUPERUSER NOBYPASSRLS")
        await owner.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
        await owner.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}"
        )
        await owner.execute(
            f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}"
        )

        # Seed two tenants + one campaign each (as OWNER — bypasses RLS,
        # exactly like a migration backfill would).
        for tid, name, slug in ((TENANT_A, "Tenant A", "tenant-a"),
                                (TENANT_B, "Tenant B", "tenant-b")):
            await owner.execute(
                "INSERT INTO tenants (id, name, slug, status) VALUES ($1, $2, $3, 'active')",
                tid, name, slug,
            )
        for cid, tid, name in ((CAMPAIGN_A, TENANT_A, "campaign-a"),
                               (CAMPAIGN_B, TENANT_B, "campaign-b")):
            await owner.execute(
                "INSERT INTO campaigns (id, tenant_id, name, goal, tone, sender_name, "
                "sender_email, research_mode, sample_count, schedule_days, "
                "schedule_time_start, schedule_time_end, schedule_timezone, "
                "min_delay_seconds, status, created_at, updated_at) "
                "VALUES ($1, $2, $3, 'g', 't', 's', 's@x.com', 'fast', 5, '{}', "
                "'09:00', '17:00', 'UTC', 60, 'draft', now(), now())",
                cid, tid, name,
            )
    finally:
        await owner.close()


# Built once at import (same pattern as conftest's test-DB init).
asyncio.run(_build_rls_db())


def _set_guc(tid: uuid.UUID | None):
    if tid is None:
        return text("SELECT set_config('app.tenant_id', '', true)")
    return text(f"SELECT set_config('app.tenant_id', '{tid}', true)")


@pytest_asyncio.fixture
async def app_conn():
    """Raw connection as the NOBYPASSRLS role (per-test engine — loops)."""
    engine = create_async_engine(APP_DSN)
    try:
        async with engine.connect() as conn:
            yield conn
    finally:
        await engine.dispose()


# --- DB-level isolation -------------------------------------------------------


@pytest.mark.asyncio
async def test_role_sanity_no_bypass(app_conn):
    row = (await app_conn.execute(text(
        "SELECT current_user, "
        "(SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user), "
        "(SELECT rolsuper FROM pg_roles WHERE rolname = current_user)"
    ))).first()
    assert row[0] == APP_ROLE and row[1] is False and row[2] is False


@pytest.mark.asyncio
async def test_guc_scopes_selects_to_one_tenant(app_conn):
    await app_conn.execute(_set_guc(TENANT_A))
    names = [r[0] for r in await app_conn.execute(text("SELECT name FROM campaigns"))]
    assert names == ["campaign-a"]

    await app_conn.execute(_set_guc(TENANT_B))
    names = [r[0] for r in await app_conn.execute(text("SELECT name FROM campaigns"))]
    assert names == ["campaign-b"]


@pytest.mark.asyncio
async def test_missing_guc_fails_closed(app_conn):
    count = (await app_conn.execute(text("SELECT count(*) FROM campaigns"))).scalar()
    assert count == 0
    # Empty-string GUC (the NULLIF guard) also fails closed, no cast error.
    await app_conn.execute(_set_guc(None))
    count = (await app_conn.execute(text("SELECT count(*) FROM campaigns"))).scalar()
    assert count == 0


@pytest.mark.asyncio
async def test_with_check_rejects_cross_tenant_insert(app_conn):
    await app_conn.execute(_set_guc(TENANT_A))
    with pytest.raises(Exception, match="row-level security|policy"):
        await app_conn.execute(text(
            "INSERT INTO suppression_list (id, tenant_id, email, reason, added_at) "
            f"VALUES (gen_random_uuid(), '{TENANT_B}', 'x@y.com', 'manual', now())"
        ))


@pytest.mark.asyncio
async def test_cross_tenant_update_and_delete_touch_nothing(app_conn):
    await app_conn.execute(_set_guc(TENANT_A))
    upd = await app_conn.execute(text(
        f"UPDATE campaigns SET name = 'stolen' WHERE id = '{CAMPAIGN_B}'"
    ))
    assert upd.rowcount == 0
    del_ = await app_conn.execute(text(
        f"DELETE FROM campaigns WHERE id = '{CAMPAIGN_B}'"
    ))
    assert del_.rowcount == 0


@pytest.mark.asyncio
async def test_every_tenanted_table_has_the_policy(app_conn):
    """No table can silently miss enforcement: every tenant_id-bearing
    table (minus the un-RLS'd identity layer) must carry the policy."""
    rows = await app_conn.execute(text(
        "SELECT c.table_name FROM information_schema.columns c "
        "WHERE c.column_name = 'tenant_id' AND c.table_schema = 'public'"
    ))
    tenanted = {r[0] for r in rows} - {"memberships", "user_sessions"}
    rows = await app_conn.execute(text(
        "SELECT tablename FROM pg_policies WHERE policyname = 'tenant_isolation'"
    ))
    with_policy = {r[0] for r in rows}
    missing = tenanted - with_policy
    assert not missing, f"tenant_id tables WITHOUT an RLS policy: {sorted(missing)}"
    rls_off = await app_conn.execute(text(
        "SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relname = ANY(:names) AND c.relkind = 'r' "
        "AND n.nspname = 'public' AND NOT c.relrowsecurity"
    ), {"names": list(tenanted)})
    off = [r[0] for r in rls_off]
    assert not off, f"tenant_id tables with RLS not ENABLED: {off}"


# --- Router-level isolation (real auth + RLS) -----------------------------------


@pytest_asyncio.fixture
async def rls_client():
    """The app with REAL auth + REAL RLS: feature sessions run as the
    NOBYPASSRLS role against the alembic-built DB; the service dep gets
    the owner engine exactly like production."""
    from app.database import get_public_db, get_service_db
    from app.main import app

    app_engine = create_async_engine(APP_DSN)
    owner_engine = create_async_engine(OWNER_DSN)
    AppSession = async_sessionmaker(app_engine, expire_on_commit=False)
    OwnerSession = async_sessionmaker(owner_engine, expire_on_commit=False)

    async def _public_db():
        async with AppSession() as session:
            yield session

    async def _service_db():
        async with OwnerSession() as session:
            session.info["rls_exempt"] = True
            yield session

    app.dependency_overrides[get_public_db] = _public_db
    app.dependency_overrides[get_service_db] = _service_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as c:
            yield c
    finally:
        app.dependency_overrides.clear()
        await app_engine.dispose()
        await owner_engine.dispose()


async def _register(client, email, tenant_name):
    resp = await client.post("/auth/register", json={
        "email": email, "password": "hunter2hunter2", "tenant_name": tenant_name,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- Worker-context isolation (Phase 7 hardening) --------------------------------


@pytest.mark.asyncio
async def test_worker_tenant_context_scopes_queries_under_rls():
    """A worker unit-of-work (run_for_tenant on the non-owner role) sees
    ONLY its tenant's rows — the exact mechanism every sweep and
    single-record task rides."""
    from app.models import Campaign
    from app.tenancy.context import run_for_tenant

    engine = create_async_engine(APP_DSN)
    try:
        async with run_for_tenant(TENANT_A, engine) as session:
            names = [c.name for c in
                     (await session.execute(select_campaigns())).scalars()]
            assert names == ["campaign-a"]
        async with run_for_tenant(TENANT_B, engine) as session:
            names = [c.name for c in
                     (await session.execute(select_campaigns())).scalars()]
            assert names == ["campaign-b"]
        # No tenant context (a mis-wired worker): fail-closed, zero rows.
        async with AsyncSession(engine, expire_on_commit=False) as session:
            names = (await session.execute(select_campaigns())).scalars().all()
            assert names == []
    finally:
        await engine.dispose()


def select_campaigns():
    from sqlalchemy import select

    from app.models import Campaign

    return select(Campaign)


@pytest.mark.asyncio
async def test_worker_insert_is_stamped_and_checked_under_rls():
    """TenantMixin stamping + WITH CHECK on the worker path: a row created
    inside run_for_tenant lands in that tenant; forging another tenant's
    id on the insert is rejected by the policy."""
    import uuid as _uuid
    from datetime import time as _time

    from app.models import Campaign
    from app.tenancy.context import run_for_tenant

    engine = create_async_engine(APP_DSN)
    try:
        async with run_for_tenant(TENANT_A, engine) as session:
            row = Campaign(
                name=f"worker-made-{_uuid.uuid4().hex[:6]}", goal="g", tone="t",
                sender_name="s", sender_email="s@x.com",
                schedule_time_start=_time(9, 0), schedule_time_end=_time(17, 0),
            )
            session.add(row)
            await session.commit()
            assert row.tenant_id == TENANT_A  # mixin stamped the ambient tenant

            forged = Campaign(
                name="forged", goal="g", tone="t",
                sender_name="s", sender_email="s@x.com",
                schedule_time_start=_time(9, 0), schedule_time_end=_time(17, 0),
                tenant_id=TENANT_B,
            )
            session.add(forged)
            with pytest.raises(Exception, match="row-level security|policy"):
                await session.commit()
            await session.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_router_isolation_between_two_registered_tenants(rls_client):
    """The end-to-end money test: two real signups, tenant A's campaign is
    invisible to tenant B through the actual routers under RLS."""
    await _register(rls_client, "alice@example.com", "Alice Co")
    created = await rls_client.post("/campaigns/", json={
        "name": "Alice private campaign", "goal": "g", "tone": "t",
        "sender_name": "A", "sender_email": "a@x.com",
        "schedule_time_start": "09:00:00", "schedule_time_end": "17:00:00",
    })
    assert created.status_code == 201, created.text
    campaign_id = created.json()["id"]

    # Alice sees it.
    mine = await rls_client.get("/campaigns/")
    assert any(c["id"] == campaign_id for c in mine.json())

    # Bob registers his own workspace — a fresh cookie replaces Alice's.
    rls_client.cookies.clear()
    await _register(rls_client, "bob@example.com", "Bob Co")

    theirs = await rls_client.get("/campaigns/")
    assert theirs.status_code == 200
    assert all(c["id"] != campaign_id for c in theirs.json()), (
        "tenant B can see tenant A's campaign — cross-tenant leak"
    )
    # Direct fetch by id: RLS hides the row entirely → 404, not 403.
    direct = await rls_client.get(f"/campaigns/{campaign_id}")
    assert direct.status_code == 404
    # And B cannot delete it.
    deleted = await rls_client.delete(f"/campaigns/{campaign_id}")
    assert deleted.status_code == 404
    rls_client.cookies.clear()


@pytest.mark.asyncio
async def test_router_isolation_leads_and_crm(rls_client):
    """Broader router sweep (Phase 7): the global Leads list and the CRM
    surface stay tenant-scoped under RLS too."""
    await _register(rls_client, "carol@example.com", "Carol Co")
    lead = await rls_client.post("/crm/leads", json={
        "email": "prospect@example.com", "first_name": "P", "company": "Acme",
    })
    assert lead.status_code in (200, 201), lead.text
    lead_id = lead.json()["id"]
    mine = await rls_client.get("/leads")
    assert any(l["id"] == lead_id for l in mine.json()["items"])

    rls_client.cookies.clear()
    await _register(rls_client, "dave@example.com", "Dave Co")
    theirs = await rls_client.get("/leads")
    assert theirs.status_code == 200
    assert all(l["id"] != lead_id for l in theirs.json()["items"]), (
        "tenant B can see tenant A's lead — cross-tenant leak"
    )
    assert (await rls_client.get(f"/leads/{lead_id}")).status_code == 404
    rls_client.cookies.clear()
