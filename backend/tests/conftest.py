"""Pytest configuration: set test env vars BEFORE app modules load."""
import asyncio
import os

from cryptography.fernet import Fernet


def _force_env(key: str, value: str) -> None:
    """Set env var, overriding empty values injected by docker-compose."""
    if not os.environ.get(key):
        os.environ[key] = value


# Test database lives on the same postgres instance as dev, in a separate DB.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://outboundos:outboundos@postgres:5432/outboundos_test",
)
ADMIN_DATABASE_URL = os.environ.get(
    "ADMIN_DATABASE_URL",
    "postgresql+asyncpg://outboundos:outboundos@postgres:5432/postgres",
)
TEST_DB_NAME = "outboundos_test"

os.environ["DATABASE_URL"] = TEST_DATABASE_URL
_force_env("REDIS_URL", "redis://localhost:6379/15")
_force_env("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ["SECRET_KEY"] = "test-secret"
_force_env("FRONTEND_URL", "http://localhost:5173")
_force_env("WEBHOOK_BASE_URL", "http://localhost:8000")
# Force-blank UNCONDITIONALLY (docker compose injects the real .env value):
# a configured owner address would make notification tests attempt REAL
# Brevo sends to the operator's inbox.  Tests that need it set monkeypatch
# notifications.settings.OWNER_NOTIFY_EMAIL explicitly.
os.environ["OWNER_NOTIFY_EMAIL"] = ""

import uuid  # noqa: E402

import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

# The tenant every ``client``-fixture request runs as (multi-tenancy
# Phase 2): the TenantMixin insert default stamps this onto rows created
# through routers.  Fixed so tests can assert against it.
BOOTSTRAP_TENANT_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")


# --------------------------------------------------------------------------
# One-time test database + schema initialization (synchronous, before tests).
# pytest-asyncio creates a new event loop per test, and asyncpg pools are
# loop-bound, so each per-test fixture below builds its own engine.
# --------------------------------------------------------------------------


async def _initialize_test_db() -> None:
    admin = create_async_engine(ADMIN_DATABASE_URL, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"'))
        await conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    await admin.dispose()

    import app.models  # noqa: F401 — register Base.metadata
    from app.database import Base

    schema_engine = create_async_engine(TEST_DATABASE_URL)
    async with schema_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await schema_engine.dispose()


asyncio.run(_initialize_test_db())


# --------------------------------------------------------------------------
# Per-test fixtures
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def _engine():
    """Per-test async engine bound to the current event loop. Truncates first."""
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(text(
            "TRUNCATE TABLE lookalike_candidates, icp_profiles, "
            "org_intent_scores, signals, orgs, icp_intent_profiles, "
            "funding_enrichment_queue, "
            "funding_source_state, prospect_signals, signal_watches, "
            "reply_outcomes, campaign_copy_insights, "
            "agent_actions, notifications, agent_settings, "
            "report_definitions, opportunity_stage_changes, "
            "crm_documents, crm_opportunity_products, "
            "crm_activities, crm_opportunities, "
            "contacts, accounts, opportunity_stages, pipelines, "
            "lead_step_executions, lead_sequence_states, "
            "sequence_edges, sequence_nodes, sequences, "
            "email_events, leads, style_corrections, "
            "suppression_list, campaigns, connected_accounts, "
            "linkedin_accounts, linkedin_profile_cache, "
            "webhook_events, research_cache, "
            "social_listening_opportunities, social_listening_posts, "
            "social_listening_searches, "
            "auth_tokens, user_sessions, memberships, users, tenants, "
            "tenant_provider_keys, usage_counters, admin_audit "
            "RESTART IDENTITY CASCADE"
        ))
        # Re-seed the bootstrap tenant the ``client`` fixture's requests run
        # as — router-created rows FK their tenant_id to it (TenantMixin).
        await conn.execute(text(
            "INSERT INTO tenants (id, name, slug, status) "
            f"VALUES ('{BOOTSTRAP_TENANT_ID}', 'Bootstrap', 'bootstrap', 'active')"
        ))
        # BYOK: requests under the client fixture run in tenant context, so
        # provider resolution reads tenant_provider_keys (no env fallback).
        # Seed the bootstrap tenant with the test env's keys.
        import json as _json

        from app.config import settings as _settings
        from app.services.encryption import encrypt as _encrypt

        _blobs = {
            "anthropic": {"api_key": _settings.ANTHROPIC_API_KEY or "test-key"},
            "brevo": {
                "api_key": _settings.BREVO_API_KEY or "test-key",
                "sender_email": _settings.BREVO_SENDER_EMAIL or "test@test.local",
                "sender_name": _settings.BREVO_SENDER_NAME or "Test",
            },
            "apollo": {"api_key": _settings.APOLLO_API_KEY or "test-key"},
            "hunter": {"api_key": _settings.HUNTER_API_KEY or "test-key"},
            "unipile": {
                "dsn": _settings.UNIPILE_DSN or "api.test:443",
                "api_key": _settings.UNIPILE_API_KEY or "test-key",
            },
        }
        for _provider, _blob in _blobs.items():
            await conn.execute(text(
                "INSERT INTO tenant_provider_keys "
                "(id, tenant_id, provider, encrypted_credentials) "
                f"VALUES (gen_random_uuid(), '{BOOTSTRAP_TENANT_ID}', "
                f"'{_provider}', :blob)"
            ), {"blob": _encrypt(_json.dumps(_blob))})
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(_engine):
    async with AsyncSession(_engine, expire_on_commit=False) as session:
        yield session


@pytest_asyncio.fixture
async def client(_engine):
    """AsyncClient against the FastAPI app with the DB deps pointing at the
    test engine.

    Overrides BOTH database dependencies:
      - ``get_db`` — replaced wholesale, which also severs its auth
        sub-dependency, so the ~80 pre-auth test files keep exercising
        feature routers anonymously (multi-tenancy Phase 1).
      - ``get_public_db`` — repointed at the test engine so the webhook /
        unsubscribe / auth endpoints (which use it directly) don't touch
        the module-level engine (loop-bound; breaks across tests).

    raise_app_exceptions=False so the global exception handler can return a
    500 response in tests instead of httpx re-raising the original exception.
    """
    from app.database import get_db, get_public_db, get_service_db
    from app.main import app
    from app.tenancy.context import current_tenant_id

    SessionLocal = async_sessionmaker(_engine, expire_on_commit=False)

    async def _override_get_db():
        # Run the request as the bootstrap tenant so TenantMixin's insert
        # default stamps router-created rows (mirrors what the real get_db
        # does after authentication).
        token = current_tenant_id.set(BOOTSTRAP_TENANT_ID)
        try:
            async with SessionLocal() as session:
                yield session
        finally:
            current_tenant_id.reset(token)

    async def _override_public_db():
        async with SessionLocal() as session:
            yield session

    async def _override_service_db():
        # Webhooks/unsubscribe: tenant-blind, marked rls_exempt like the
        # real service session (the GUC listener skips it).
        async with SessionLocal() as session:
            session.info["rls_exempt"] = True
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_public_db] = _override_public_db
    app.dependency_overrides[get_service_db] = _override_service_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def auth_client(_engine):
    """AsyncClient with REAL auth: only ``get_public_db`` is overridden, so
    ``get_db`` still runs ``authenticate_request`` against the session
    cookie.  Use for auth-flow tests and 401-gating assertions; everything
    else should keep using ``client``."""
    from app.database import get_public_db, get_service_db
    from app.main import app

    SessionLocal = async_sessionmaker(_engine, expire_on_commit=False)

    async def _override_public_db():
        async with SessionLocal() as session:
            yield session

    async def _override_service_db():
        async with SessionLocal() as session:
            session.info["rls_exempt"] = True
            yield session

    app.dependency_overrides[get_public_db] = _override_public_db
    app.dependency_overrides[get_service_db] = _override_service_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as c:
            yield c
    finally:
        app.dependency_overrides.clear()
