"""Phase 8 follow-on: per-tenant Unipile webhooks + per-tenant funding state.

Closes the two BYOK gaps: inbound Unipile events from a TENANT's own
workspace (per-tenant URL + per-tenant secret), and the funding-feed
config/cursor moving off the global singleton.
"""
from __future__ import annotations

import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.models import FundingSourceState, Tenant
from app.services import tenant_keys
from app.tenancy.context import current_tenant_id
from tests.conftest import BOOTSTRAP_TENANT_ID

pytestmark = pytest.mark.asyncio


# --- Per-tenant webhook secret + inbound verification ---------------------------


async def test_ensure_webhook_secret_generates_once(db_session):
    s1 = await tenant_keys.ensure_unipile_webhook_secret(db_session, BOOTSTRAP_TENANT_ID)
    s2 = await tenant_keys.ensure_unipile_webhook_secret(db_session, BOOTSTRAP_TENANT_ID)
    assert s1 and s1 == s2  # stable across calls
    creds = await tenant_keys.get_provider_creds(
        db_session, "unipile", tenant_id=BOOTSTRAP_TENANT_ID,
    )
    assert creds.webhook_secret == s1


async def test_tenant_webhook_route_verifies_per_tenant_secret(client, db_session):
    secret = await tenant_keys.ensure_unipile_webhook_secret(
        db_session, BOOTSTRAP_TENANT_ID,
    )
    body = {"id": f"evt-{uuid.uuid4().hex}", "type": "some.unknown.event"}

    ok = await client.post(
        f"/webhooks/unipile/{BOOTSTRAP_TENANT_ID}",
        json=body,
        headers={"X-Unipile-Auth": secret},
    )
    assert ok.status_code == 200
    assert ok.json()["ok"] is True

    wrong = await client.post(
        f"/webhooks/unipile/{BOOTSTRAP_TENANT_ID}",
        json={"id": "evt-2", "type": "x"},
        headers={"X-Unipile-Auth": "not-the-secret"},
    )
    assert wrong.status_code == 401

    unknown = await client.post(
        f"/webhooks/unipile/{uuid.uuid4()}",
        json={"id": "evt-3", "type": "x"},
        headers={"X-Unipile-Auth": secret},
    )
    assert unknown.status_code == 401


async def test_tenant_webhook_route_dedups_like_platform_route(client, db_session):
    secret = await tenant_keys.ensure_unipile_webhook_secret(
        db_session, BOOTSTRAP_TENANT_ID,
    )
    body = {"id": "evt-dup-1", "type": "some.unknown.event"}
    first = await client.post(
        f"/webhooks/unipile/{BOOTSTRAP_TENANT_ID}", json=body,
        headers={"X-Unipile-Auth": secret},
    )
    assert first.status_code == 200 and "duplicate" not in first.json()
    second = await client.post(
        f"/webhooks/unipile/{BOOTSTRAP_TENANT_ID}", json=body,
        headers={"X-Unipile-Auth": secret},
    )
    assert second.json().get("duplicate") is True


async def test_platform_route_still_uses_platform_secret(client, monkeypatch):
    import app.routers.webhooks as hooks

    monkeypatch.setattr(hooks.settings, "UNIPILE_WEBHOOK_SECRET", "platform-secret")
    ok = await client.post(
        "/webhooks/unipile", json={"id": "evt-p1", "type": "x"},
        headers={"X-Unipile-Auth": "platform-secret"},
    )
    assert ok.status_code == 200
    bad = await client.post(
        "/webhooks/unipile", json={"id": "evt-p2", "type": "x"},
        headers={"X-Unipile-Auth": "tenant-ish-secret"},
    )
    assert bad.status_code == 401


# --- Registration ---------------------------------------------------------------


async def test_register_webhooks_endpoint(client, db_session, monkeypatch):
    import app.routers.settings as settings_router

    calls = {}

    async def _fake_register(creds, tenant_id, secret):
        calls["creds"] = creds
        calls["tenant_id"] = tenant_id
        calls["secret"] = secret
        return {"created": ["messaging", "account_status", "users"],
                "deleted_stale": 1,
                "request_url": f"http://localhost:8000/webhooks/unipile/{tenant_id}"}

    monkeypatch.setattr(
        "app.services.unipile_webhooks.register_tenant_webhooks", _fake_register,
    )
    resp = await client.post("/settings/integrations/unipile/register-webhooks")
    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] == ["messaging", "account_status", "users"]
    assert calls["tenant_id"] == BOOTSTRAP_TENANT_ID
    # The secret used for registration is the one persisted in the blob.
    creds = await tenant_keys.get_provider_creds(
        db_session, "unipile", tenant_id=BOOTSTRAP_TENANT_ID,
    )
    assert calls["secret"] == creds.webhook_secret


async def test_register_service_deletes_ours_and_creates_three(monkeypatch):
    from app.services.unipile_webhooks import register_tenant_webhooks

    monkeypatch.setattr(
        "app.services.unipile_webhooks.settings.WEBHOOK_BASE_URL",
        "https://tunnel.example",
    )
    tid = uuid.uuid4()
    seen = {"deleted": [], "created": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"items": [
                {"id": "wh-ours", "request_url": "https://tunnel.example/webhooks/unipile"},
                {"id": "wh-theirs", "request_url": "https://other.example/hook"},
            ]})
        if request.method == "DELETE":
            seen["deleted"].append(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={})
        payload = json.loads(request.content)
        seen["created"].append(payload["source"])
        assert payload["request_url"] == f"https://tunnel.example/webhooks/unipile/{tid}"
        assert {"key": "X-Unipile-Auth", "value": "sekrit"} in payload["headers"]
        return httpx.Response(201, json={"id": "new"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def _patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr("app.services.unipile_webhooks.httpx.AsyncClient", _patched_client)

    creds = tenant_keys.UnipileCreds(dsn="api1.unipile.com:13443", api_key="uk")
    result = await register_tenant_webhooks(creds, tid, "sekrit")
    assert seen["deleted"] == ["wh-ours"]  # only OUR stale hook removed
    assert sorted(seen["created"]) == ["account_status", "messaging", "users"]
    assert result["deleted_stale"] == 1


# --- Per-tenant funding state ------------------------------------------------------


async def test_funding_state_is_per_tenant(db_session):
    from app.workers.funding_signals import _get_or_create_state

    t2 = Tenant(name="Second", slug=f"second-{uuid.uuid4().hex[:6]}")
    db_session.add(t2)
    await db_session.flush()

    token = current_tenant_id.set(BOOTSTRAP_TENANT_ID)
    try:
        s1 = await _get_or_create_state(db_session, "usaspending")
        s1.enabled = True
        s1.cursor = {"last_action_date": "2026-07-01"}
        await db_session.commit()
    finally:
        current_tenant_id.reset(token)

    token = current_tenant_id.set(t2.id)
    try:
        s2 = await _get_or_create_state(db_session, "usaspending")
        # A fresh, independent row — not the bootstrap tenant's state.
        assert s2.id != s1.id
        assert s2.enabled is not True
        assert (s2.cursor or {}) == {}
        assert s2.tenant_id == t2.id
    finally:
        current_tenant_id.reset(token)

    rows = (await db_session.execute(
        select(FundingSourceState).where(FundingSourceState.source == "usaspending")
    )).scalars().all()
    assert len(rows) == 2
