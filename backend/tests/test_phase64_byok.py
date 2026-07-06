"""Multi-tenancy Phase 4: BYOK — per-tenant provider credentials.

Covers the resolution contract (tenant row wins; NO env fallback in
tenant context; env fallback only without context), the per-unit-of-work
cache, and the settings integrations API (masked previews only — the
plaintext key never leaves the server).
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from app.models import ProviderKind, Tenant, TenantProviderKey
from app.services import tenant_keys
from app.services.encryption import encrypt
from app.tenancy.context import current_tenant_id
from tests.conftest import BOOTSTRAP_TENANT_ID

pytestmark = pytest.mark.asyncio


async def _seed_key(db, tenant_id, provider, blob):
    db.add(TenantProviderKey(
        tenant_id=tenant_id,
        provider=ProviderKind(provider),
        encrypted_credentials=encrypt(json.dumps(blob)),
    ))
    await db.commit()


# --- Resolution contract -------------------------------------------------------


async def test_tenant_row_resolves_and_env_is_ignored_in_tenant_context(
    db_session, monkeypatch,
):
    monkeypatch.setattr(tenant_keys.settings, "ANTHROPIC_API_KEY", "env-key")
    t = Tenant(name="K", slug=f"k-{uuid.uuid4().hex[:6]}")
    db_session.add(t)
    await db_session.flush()
    await _seed_key(db_session, t.id, "anthropic", {"api_key": "tenant-secret"})

    token = current_tenant_id.set(t.id)
    try:
        creds = await tenant_keys.ambient_creds("anthropic")
        assert creds is not None and creds.api_key == "tenant-secret"

        # STRICT: a tenant without a hunter key gets None — never env-key.
        monkeypatch.setattr(tenant_keys.settings, "HUNTER_API_KEY", "env-hunter")
        assert await tenant_keys.ambient_creds("hunter") is None
    finally:
        current_tenant_id.reset(token)


async def test_env_fallback_only_without_tenant_context(monkeypatch):
    monkeypatch.setattr(tenant_keys.settings, "APOLLO_API_KEY", "env-apollo")
    assert current_tenant_id.get() is None
    creds = await tenant_keys.ambient_creds("apollo")
    assert creds is not None and creds.api_key == "env-apollo"

    monkeypatch.setattr(tenant_keys.settings, "APOLLO_API_KEY", "")
    assert await tenant_keys.ambient_creds("apollo") is None


async def test_creds_cache_is_per_unit_of_work(monkeypatch):
    monkeypatch.setattr(tenant_keys.settings, "APOLLO_API_KEY", "first")
    cache_token = tenant_keys.init_creds_cache()
    try:
        first = await tenant_keys.ambient_creds("apollo")
        monkeypatch.setattr(tenant_keys.settings, "APOLLO_API_KEY", "second")
        cached = await tenant_keys.ambient_creds("apollo")
        assert first.api_key == cached.api_key == "first"  # cached
    finally:
        tenant_keys.reset_creds_cache(cache_token)
    fresh = await tenant_keys.ambient_creds("apollo")
    assert fresh.api_key == "second"  # cache died with the unit of work


def test_masked_preview_never_reveals_more_than_last4():
    encrypted = encrypt(json.dumps({"api_key": "sk-ant-supersecret1234"}))
    masked = tenant_keys.masked_preview(encrypted, "anthropic")
    assert masked == "••••1234"
    assert "supersecret" not in masked


# --- Integrations API -----------------------------------------------------------


async def test_put_list_masked_and_delete(client, db_session):
    resp = await client.put("/settings/integrations/anthropic", json={
        "api_key": "sk-ant-tenant-key-9876",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["configured"] is True
    assert body["masked"] == "••••9876"
    assert "sk-ant" not in json.dumps(body)

    listed = await client.get("/settings/integrations")
    by_provider = {r["provider"]: r for r in listed.json()}
    assert by_provider["anthropic"]["masked"] == "••••9876"

    # Row is stamped with the ambient (bootstrap) tenant.
    row = (await db_session.execute(
        select(TenantProviderKey).where(
            TenantProviderKey.provider == ProviderKind.ANTHROPIC,
            TenantProviderKey.tenant_id == BOOTSTRAP_TENANT_ID,
        )
    )).scalars().one()
    assert "sk-ant" not in row.encrypted_credentials  # stored encrypted

    deleted = await client.delete("/settings/integrations/anthropic")
    assert deleted.status_code == 204
    listed = await client.get("/settings/integrations")
    assert {r["provider"]: r["configured"] for r in listed.json()}["anthropic"] is False


async def test_put_unknown_provider_404_and_empty_key_422(client):
    assert (await client.put("/settings/integrations/nope", json={"api_key": "x"})).status_code == 404
    assert (await client.put("/settings/integrations/hunter", json={"api_key": "  "})).status_code == 422


async def test_test_endpoint_persists_status(client, monkeypatch):
    await client.put("/settings/integrations/hunter", json={"api_key": "h-key-4321"})

    import app.routers.settings as settings_router

    async def _ok(provider, creds):
        assert creds.api_key == "h-key-4321"

    monkeypatch.setattr(settings_router, "_probe_provider", _ok)
    resp = await client.post("/settings/integrations/hunter/test")
    assert resp.status_code == 200
    assert resp.json()["last_test_status"] == "ok"

    async def _boom(provider, creds):
        raise RuntimeError("401 unauthorized")

    monkeypatch.setattr(settings_router, "_probe_provider", _boom)
    resp = await client.post("/settings/integrations/hunter/test")
    assert resp.json()["last_test_status"] == "failed"
    assert "401" in resp.json()["last_test_error"]


async def test_api_status_reflects_tenant_keys(client):
    # conftest seeds all five providers for the bootstrap tenant.
    resp = await client.get("/settings/api-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["anthropic"] is True and body["brevo"] is True

    await client.delete("/settings/integrations/apollo")
    resp = await client.get("/settings/api-status")
    assert resp.json()["apollo"] is False
