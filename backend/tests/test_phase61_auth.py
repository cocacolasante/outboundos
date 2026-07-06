"""Multi-tenancy Phase 1: first-party auth + the get_db gate.

Uses the ``auth_client`` fixture (real auth — only ``get_public_db`` is
overridden) so ``authenticate_request`` actually runs.  The regular
``client`` fixture keeps bypassing auth for the pre-existing suite.
"""
from __future__ import annotations

import pytest


REGISTER = {"email": "owner@example.com", "password": "hunter2hunter2",
            "tenant_name": "Acme Outbound"}


async def _register(auth_client, **overrides):
    payload = {**REGISTER, **overrides}
    resp = await auth_client.post("/auth/register", json=payload)
    assert resp.status_code == 201, resp.text
    return resp


# --- Register / login / me --------------------------------------------------


@pytest.mark.asyncio
async def test_register_creates_identity_chain_and_logs_in(auth_client):
    resp = await _register(auth_client)
    body = resp.json()
    assert body["email"] == "owner@example.com"
    assert body["tenant_name"] == "Acme Outbound"
    assert body["tenant_slug"] == "acme-outbound"
    assert body["role"] == "owner"

    # Auto-login: the session cookie from register authenticates /auth/me.
    me = await auth_client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["user_id"] == body["user_id"]


@pytest.mark.asyncio
async def test_register_sets_hardened_cookie(auth_client):
    resp = await _register(auth_client)
    set_cookie = resp.headers.get("set-cookie", "")
    assert "eb_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie.replace("samesite", "SameSite")


@pytest.mark.asyncio
async def test_register_duplicate_email_409(auth_client):
    await _register(auth_client)
    resp = await auth_client.post("/auth/register", json=REGISTER)
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_register_rejects_short_password(auth_client):
    resp = await auth_client.post(
        "/auth/register", json={**REGISTER, "password": "short"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_login_ok_and_wrong_credentials_are_indistinguishable(auth_client):
    await _register(auth_client)
    auth_client.cookies.clear()

    ok = await auth_client.post("/auth/login", json={
        "email": "OWNER@example.com",  # canonicalised lowercase
        "password": REGISTER["password"],
    })
    assert ok.status_code == 200
    assert ok.json()["role"] == "owner"

    auth_client.cookies.clear()
    bad_pw = await auth_client.post("/auth/login", json={
        "email": REGISTER["email"], "password": "wrong-password",
    })
    unknown = await auth_client.post("/auth/login", json={
        "email": "nobody@example.com", "password": "wrong-password",
    })
    assert bad_pw.status_code == unknown.status_code == 401
    assert bad_pw.json()["detail"] == unknown.json()["detail"]


# --- The get_db gate ---------------------------------------------------------


@pytest.mark.asyncio
async def test_feature_endpoints_401_without_session(auth_client):
    for path in ("/leads", "/campaigns/", "/agent/settings"):
        resp = await auth_client.get(path)
        assert resp.status_code == 401, f"{path} -> {resp.status_code}"


@pytest.mark.asyncio
async def test_feature_endpoint_works_with_session(auth_client):
    await _register(auth_client)
    resp = await auth_client.get("/leads")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_health_and_auth_endpoints_stay_public(auth_client):
    assert (await auth_client.get("/health")).status_code == 200
    assert (await auth_client.post(
        "/auth/forgot", json={"email": "nobody@example.com"},
    )).status_code == 200


# --- Logout / session revocation ---------------------------------------------


@pytest.mark.asyncio
async def test_logout_revokes_the_session(auth_client):
    await _register(auth_client)
    assert (await auth_client.get("/auth/me")).status_code == 200

    # Capture the cookie, log out, then present the OLD cookie again —
    # revocation must be server-side, not just cookie deletion.
    old_cookie = auth_client.cookies.get("eb_session")
    resp = await auth_client.post("/auth/logout")
    assert resp.status_code == 200

    auth_client.cookies.set("eb_session", old_cookie)
    assert (await auth_client.get("/auth/me")).status_code == 401
    assert (await auth_client.get("/leads")).status_code == 401


# --- Password reset ----------------------------------------------------------


@pytest.mark.asyncio
async def test_forgot_reset_flow_revokes_sessions(auth_client, monkeypatch):
    sent: list[dict] = []

    async def _capture(**kwargs):
        sent.append(kwargs)
        return True

    import app.routers.auth as auth_router
    monkeypatch.setattr(auth_router, "send_platform_email", _capture)

    await _register(auth_client)
    resp = await auth_client.post("/auth/forgot", json={"email": REGISTER["email"]})
    assert resp.status_code == 200
    assert len(sent) == 1
    # Extract the raw token from the reset URL in the captured email.
    token = sent[0]["text_body"].split("reset_token=")[1].split()[0]

    resp = await auth_client.post(
        "/auth/reset", json={"token": token, "password": "a-new-password-9"},
    )
    assert resp.status_code == 200

    # The pre-reset session (from register) is revoked...
    assert (await auth_client.get("/auth/me")).status_code == 401
    # ...the token is single-use...
    again = await auth_client.post(
        "/auth/reset", json={"token": token, "password": "another-pass-10"},
    )
    assert again.status_code == 400
    # ...old password dead, new password logs in.
    auth_client.cookies.clear()
    old = await auth_client.post("/auth/login", json={
        "email": REGISTER["email"], "password": REGISTER["password"],
    })
    assert old.status_code == 401
    new = await auth_client.post("/auth/login", json={
        "email": REGISTER["email"], "password": "a-new-password-9",
    })
    assert new.status_code == 200


@pytest.mark.asyncio
async def test_forgot_unknown_email_is_silent(auth_client, monkeypatch):
    sent: list[dict] = []

    async def _capture(**kwargs):
        sent.append(kwargs)
        return True

    import app.routers.auth as auth_router
    monkeypatch.setattr(auth_router, "send_platform_email", _capture)

    resp = await auth_client.post("/auth/forgot", json={"email": "ghost@example.com"})
    assert resp.status_code == 200
    assert sent == []


# --- CSRF origin check --------------------------------------------------------


@pytest.mark.asyncio
async def test_mutating_cross_origin_request_403(auth_client):
    await _register(auth_client)
    resp = await auth_client.post(
        "/auth/logout", headers={"origin": "https://evil.example"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_frontend_origin_passes_csrf_check(auth_client):
    await _register(auth_client)
    resp = await auth_client.post(
        "/auth/logout", headers={"origin": "http://localhost:5173"},
    )
    assert resp.status_code == 200


# --- Hardening: get_public_db allowlist ---------------------------------------


def _grep_offenders(needle: str, allowed: set[str]) -> list[str]:
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    offenders = []
    for path in root.rglob("*.py"):
        if path.name in allowed:
            continue
        if needle in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(root)))
    return offenders


def test_no_get_public_db_outside_allowlist():
    """``get_public_db`` skips authentication — only the auth endpoints
    (pre-session) may use it.  Mirrors the ``encryption.decrypt``
    allowlist grep in test_phase15_hardening."""
    allowed = {
        "database.py",   # defines it (and get_db composes it)
        "auth.py",       # the auth endpoints themselves (pre-session)
        "deps.py",       # auth dependency plumbing
        "admin.py",      # superadmin plane (identity-gated, cross-tenant)
    }
    offenders = _grep_offenders("get_public_db", allowed)
    assert not offenders, (
        f"get_public_db bypasses auth and may only be used in {sorted(allowed)} "
        f"but was found in: {offenders}"
    )


def test_no_get_service_db_outside_allowlist():
    """``get_service_db`` bypasses auth AND row-level security (owner
    engine, rls_exempt) — only the deliberately tenant-blind public
    surfaces may use it, and they must stamp tenant_id explicitly on
    anything they create."""
    allowed = {
        "database.py",         # defines it
        "webhooks.py",         # unsubscribe + Unipile/Brevo webhooks
        "stripe_webhooks.py",  # Stripe events (tenant by customer id)
    }
    offenders = _grep_offenders("get_service_db", allowed)
    assert not offenders, (
        f"get_service_db bypasses auth AND RLS; allowed only in "
        f"{sorted(allowed)} but found in: {offenders}"
    )


def test_no_service_worker_engine_outside_allowlist():
    """``service_worker_engine`` (owner DSN, RLS-exempt) exists solely for
    the ``tenant_of`` derive-from-record lookup in the tenancy plumbing."""
    allowed = {"context.py"}  # app/tenancy/context.py defines and uses it
    offenders = _grep_offenders("service_worker_engine", allowed)
    assert not offenders, (
        f"service_worker_engine is RLS-exempt; allowed only in "
        f"{sorted(allowed)} but found in: {offenders}"
    )
