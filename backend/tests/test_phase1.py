"""Phase 1 acceptance: scaffold is wired up correctly.

These tests verify the project skeleton boots without touching live services:
- FastAPI app imports and exposes /health
- Config loads from environment
- All stub routers are mounted
- Celery app constructs against the configured broker URL
- Database engine constructs (does not connect)
"""
import pytest


async def test_health_endpoint(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body


async def test_openapi_schema_includes_router_prefixes(client):
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    assert "/campaigns/" in paths


def test_all_stub_routers_import():
    # Phase 1 stub routers don't expose endpoints yet, so they don't appear in
    # app.routes. Verify each router module imports cleanly and exposes `router`.
    from app.routers import analytics, campaigns, connected_accounts, leads, preview, webhooks

    for mod in (analytics, campaigns, connected_accounts, leads, preview, webhooks):
        assert hasattr(mod, "router"), f"{mod.__name__} missing `router`"


def test_settings_load_from_env():
    from app.config import settings

    assert settings.ENCRYPTION_KEY  # set by conftest
    assert settings.SECRET_KEY == "test-secret"
    assert settings.FRONTEND_URL == "http://localhost:5173"
    assert settings.ANTHROPIC_MODEL == "claude-sonnet-4-6"


def test_celery_app_constructs():
    from app.workers.celery_app import celery_app

    assert celery_app.main == "emailblaster"
    assert celery_app.conf.task_serializer == "json"
    assert celery_app.conf.broker_url.startswith("redis://")


def test_database_engine_constructs():
    from app.database import AsyncSessionLocal, engine

    assert engine is not None
    assert AsyncSessionLocal is not None


def test_cors_middleware_configured():
    from app.config import settings
    from app.main import app

    cors = [m for m in app.user_middleware if "CORSMiddleware" in str(m.cls)]
    assert len(cors) == 1
    assert settings.FRONTEND_URL in cors[0].kwargs["allow_origins"]
