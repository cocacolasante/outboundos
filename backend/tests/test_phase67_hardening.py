"""Multi-tenancy Phase 7: hardening.

Per-tenant API rate limiting (fail-open), the super-admin surface
(gating, plan override + audit, impersonation), the
BILLING_REQUIRE_SUBSCRIPTION lever, and MultiFernet key rotation.
"""
from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.models import AdminAudit, BillingPlan, Tenant
from tests.conftest import BOOTSTRAP_TENANT_ID

pytestmark = pytest.mark.asyncio


# --- API rate limiting ---------------------------------------------------------


async def test_rate_limiter_blocks_over_budget(monkeypatch):
    import fakeredis.aioredis

    from app.services import api_rate_limit

    monkeypatch.setattr(api_rate_limit, "_client", fakeredis.aioredis.FakeRedis(
        decode_responses=True,
    ))
    monkeypatch.setattr(api_rate_limit.settings, "API_RATE_LIMIT_PER_MINUTE", 3)
    tid = uuid.uuid4()
    results = [await api_rate_limit.allow_request(tid) for _ in range(5)]
    assert results == [True, True, True, False, False]
    # Another tenant has its own budget.
    assert await api_rate_limit.allow_request(uuid.uuid4()) is True


async def test_rate_limiter_fails_open_on_redis_outage(monkeypatch):
    from app.services import api_rate_limit

    class _Boom:
        async def incr(self, *_):
            raise ConnectionError("redis down")

    monkeypatch.setattr(api_rate_limit, "_client", _Boom())
    monkeypatch.setattr(api_rate_limit.settings, "API_RATE_LIMIT_PER_MINUTE", 1)
    assert await api_rate_limit.allow_request(uuid.uuid4()) is True


async def test_rate_limiter_disabled_at_zero(monkeypatch):
    from app.services import api_rate_limit

    monkeypatch.setattr(api_rate_limit.settings, "API_RATE_LIMIT_PER_MINUTE", 0)
    monkeypatch.setattr(api_rate_limit, "_client", None)  # would crash if touched
    assert await api_rate_limit.allow_request(uuid.uuid4()) is True


# --- Super-admin surface ---------------------------------------------------------


async def _register(auth_client, email, tenant_name):
    resp = await auth_client.post("/auth/register", json={
        "email": email, "password": "hunter2hunter2", "tenant_name": tenant_name,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
def superadmin(monkeypatch):
    from app.auth import deps as auth_deps

    monkeypatch.setattr(auth_deps.settings, "SUPERADMIN_EMAILS",
                        "root@example.com, other-admin@example.com")


async def test_admin_surface_gated(auth_client, superadmin):
    await _register(auth_client, "normal@example.com", "Normal Co")
    assert (await auth_client.get("/admin/tenants")).status_code == 403


async def test_plan_override_writes_audit(auth_client, db_session, superadmin):
    target = await _register(auth_client, "victim@example.com", "Target Co")
    auth_client.cookies.clear()
    await _register(auth_client, "root@example.com", "Admin HQ")

    tenants = await auth_client.get("/admin/tenants")
    assert tenants.status_code == 200
    assert {t["slug"] for t in tenants.json()} >= {"target-co", "admin-hq"}

    resp = await auth_client.post(
        f"/admin/tenants/{target['tenant_id']}/plan",
        json={"plan": "agency", "subscription_status": "active"},
    )
    assert resp.status_code == 200
    assert resp.json()["plan"] == "agency"

    audit = (await db_session.execute(select(AdminAudit))).scalars().all()
    assert len(audit) == 1
    assert audit[0].action == "plan_override"
    assert audit[0].actor_email == "root@example.com"
    assert str(audit[0].target_tenant_id) == target["tenant_id"]
    assert audit[0].detail["plan"]["to"] == "agency"

    feed = await auth_client.get("/admin/audit")
    assert feed.status_code == 200 and feed.json()[0]["action"] == "plan_override"


async def test_impersonation_enters_target_tenant_with_audit(auth_client, db_session, superadmin):
    target = await _register(auth_client, "victim@example.com", "Target Co")
    campaign = await auth_client.post("/campaigns/", json={
        "name": "Victim campaign", "goal": "g", "tone": "t",
        "sender_name": "s", "sender_email": "s@x.com",
        "schedule_time_start": "09:00:00", "schedule_time_end": "17:00:00",
    })
    assert campaign.status_code == 201

    auth_client.cookies.clear()
    await _register(auth_client, "root@example.com", "Admin HQ")
    resp = await auth_client.post(f"/admin/tenants/{target['tenant_id']}/impersonate")
    assert resp.status_code == 200 and resp.json()["tenant"] == "target-co"

    # The new cookie is bound to the TARGET tenant: /auth/me works via the
    # synthesized admin membership and the tenant is the victim's.
    me = await auth_client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["tenant_id"] == target["tenant_id"]
    assert me.json()["role"] == "admin"

    audit_actions = [a.action for a in
                     (await db_session.execute(select(AdminAudit))).scalars()]
    assert "impersonate" in audit_actions


async def test_impersonation_membership_is_never_persisted(auth_client, db_session, superadmin):
    from app.models import Membership

    target = await _register(auth_client, "victim@example.com", "Target Co")
    auth_client.cookies.clear()
    await _register(auth_client, "root@example.com", "Admin HQ")
    await auth_client.post(f"/admin/tenants/{target['tenant_id']}/impersonate")
    await auth_client.get("/auth/me")

    memberships = (await db_session.execute(
        select(Membership).where(
            Membership.tenant_id == uuid.UUID(target["tenant_id"]),
        )
    )).scalars().all()
    assert len(memberships) == 1  # just the real owner — nothing synthesized


# --- BILLING_REQUIRE_SUBSCRIPTION lever ------------------------------------------


async def test_require_subscription_denies_legacy_tenants(db_session, monkeypatch):
    from app.billing import entitlements
    from app.billing.entitlements import Meter, QuotaExceeded, check_quota_on

    t = await db_session.get(Tenant, BOOTSTRAP_TENANT_ID)
    assert t.subscription_status is None  # legacy/unbilled

    # Default: exempt.
    await check_quota_on(db_session, Meter.EMAIL_SEND, tenant_id=BOOTSTRAP_TENANT_ID)

    monkeypatch.setattr(entitlements.settings, "BILLING_REQUIRE_SUBSCRIPTION", True)
    with pytest.raises(QuotaExceeded, match="no subscription"):
        await check_quota_on(db_session, Meter.EMAIL_SEND,
                             tenant_id=BOOTSTRAP_TENANT_ID)


# --- MultiFernet key rotation ------------------------------------------------------


def test_decrypt_accepts_retired_keys_and_encrypts_with_primary(monkeypatch):
    from cryptography.fernet import Fernet as _F

    from app.services import encryption

    old_key = _F.generate_key().decode()
    new_key = _F.generate_key().decode()

    # A token written under the OLD key...
    monkeypatch.setattr(encryption.settings, "ENCRYPTION_KEY", old_key)
    monkeypatch.setattr(encryption.settings, "ENCRYPTION_KEYS_OLD", "")
    monkeypatch.setattr(encryption, "_fernet", None)
    legacy_token = encryption.encrypt("secret-password")

    # ...still decrypts once the key is retired into ENCRYPTION_KEYS_OLD...
    monkeypatch.setattr(encryption.settings, "ENCRYPTION_KEY", new_key)
    monkeypatch.setattr(encryption.settings, "ENCRYPTION_KEYS_OLD", old_key)
    monkeypatch.setattr(encryption, "_fernet", None)
    assert encryption.decrypt(legacy_token) == "secret-password"

    # ...and new tokens use the PRIMARY (decryptable without the old key).
    fresh_token = encryption.encrypt("secret-password")
    monkeypatch.setattr(encryption.settings, "ENCRYPTION_KEYS_OLD", "")
    monkeypatch.setattr(encryption, "_fernet", None)
    assert encryption.decrypt(fresh_token) == "secret-password"
    monkeypatch.setattr(encryption, "_fernet", None)  # leave clean for other tests