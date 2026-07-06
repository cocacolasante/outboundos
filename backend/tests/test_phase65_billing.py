"""Multi-tenancy Phase 5: Stripe billing + entitlement enforcement.

Stripe itself is mocked everywhere (signature construction, customer /
checkout / portal creation) — these tests cover OUR contract: status
gating, atomic metering, plan caps, webhook idempotency + projection,
and the trial started at signup.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.billing import entitlements
from app.billing.entitlements import Meter, QuotaExceeded, check_quota_on
from app.models import BillingPlan, SubscriptionStatus, Tenant, UsageCounter
from app.tenancy.context import current_tenant_id
from tests.conftest import BOOTSTRAP_TENANT_ID

pytestmark = pytest.mark.asyncio


def _now():
    return datetime.now(timezone.utc)


async def _get_bootstrap(db):
    return await db.get(Tenant, BOOTSTRAP_TENANT_ID)


# --- spend_allowed gating matrix ------------------------------------------------


async def test_status_gating_matrix(db_session):
    t = await _get_bootstrap(db_session)

    cases = [
        # (status, trial_ends_at, current_period_end, expected)
        (None, None, None, True),                       # legacy/unbilled
        (SubscriptionStatus.ACTIVE, None, None, True),
        (SubscriptionStatus.TRIALING, _now() + timedelta(days=5), None, True),
        (SubscriptionStatus.TRIALING, _now() - timedelta(days=1), None, False),
        (SubscriptionStatus.PAST_DUE, None, _now() - timedelta(days=2), True),   # in grace
        (SubscriptionStatus.PAST_DUE, None, _now() - timedelta(days=30), False), # past grace
        (SubscriptionStatus.CANCELED, None, None, False),
        (SubscriptionStatus.INCOMPLETE, None, None, False),
    ]
    for status, trial_end, period_end, expected in cases:
        t.subscription_status = status
        t.trial_ends_at = trial_end
        t.current_period_end = period_end
        allowed, reason = entitlements.spend_allowed(t)
        assert allowed is expected, f"{status} → {allowed}, expected {expected} ({reason})"
        if not expected:
            assert reason


async def test_blocked_status_raises_before_any_metering(db_session):
    t = await _get_bootstrap(db_session)
    t.subscription_status = SubscriptionStatus.CANCELED
    await db_session.commit()
    with pytest.raises(QuotaExceeded, match="canceled"):
        await check_quota_on(db_session, Meter.EMAIL_SEND,
                             tenant_id=BOOTSTRAP_TENANT_ID)
    counters = (await db_session.execute(select(UsageCounter))).scalars().all()
    assert counters == []  # denial happened before the increment


# --- Metering --------------------------------------------------------------------


async def test_quota_exceeded_after_plan_limit(db_session, monkeypatch):
    from app.billing import plans as plans_mod

    t = await _get_bootstrap(db_session)
    t.plan = BillingPlan.STARTER
    t.subscription_status = SubscriptionStatus.ACTIVE
    await db_session.commit()

    tiny = plans_mod.PlanDef(label="Starter",
                             monthly_quotas={Meter.EMAIL_SEND: 2},
                             static_limits={})
    monkeypatch.setitem(plans_mod.PLANS, BillingPlan.STARTER, tiny)

    await check_quota_on(db_session, Meter.EMAIL_SEND, tenant_id=BOOTSTRAP_TENANT_ID)
    await check_quota_on(db_session, Meter.EMAIL_SEND, tenant_id=BOOTSTRAP_TENANT_ID)
    with pytest.raises(QuotaExceeded, match="2 email_send"):
        await check_quota_on(db_session, Meter.EMAIL_SEND,
                             tenant_id=BOOTSTRAP_TENANT_ID)

    counter = (await db_session.execute(select(UsageCounter))).scalars().one()
    assert counter.meter == "email_send" and counter.count == 3
    assert counter.period == _now().strftime("%Y%m")


async def test_unmetered_and_tenantless_paths_pass(db_session):
    t = await _get_bootstrap(db_session)
    t.subscription_status = SubscriptionStatus.ACTIVE
    await db_session.commit()
    # No tenant context and no explicit tenant → tenant-blind path, allowed.
    assert current_tenant_id.get() is None
    await check_quota_on(db_session, Meter.EMAIL_SEND)  # no raise
    counters = (await db_session.execute(select(UsageCounter))).scalars().all()
    assert counters == []


# --- Static caps at the create endpoints -----------------------------------------


async def test_campaign_cap_blocks_creation_with_402(client, db_session, monkeypatch):
    from app.billing import plans as plans_mod

    t = await _get_bootstrap(db_session)
    t.plan = BillingPlan.STARTER
    t.subscription_status = SubscriptionStatus.ACTIVE
    await db_session.commit()

    capped = plans_mod.PlanDef(label="Starter", monthly_quotas={},
                               static_limits={"active_campaigns": 1})
    monkeypatch.setitem(plans_mod.PLANS, BillingPlan.STARTER, capped)

    payload = {
        "name": "one", "goal": "g", "tone": "t",
        "sender_name": "s", "sender_email": "s@x.com",
        "schedule_time_start": "09:00:00", "schedule_time_end": "17:00:00",
    }
    assert (await client.post("/campaigns/", json=payload)).status_code == 201
    second = await client.post("/campaigns/", json={**payload, "name": "two"})
    assert second.status_code == 402
    assert "1 active campaigns" in second.json()["detail"]


# --- Signup trial -----------------------------------------------------------------


async def test_register_starts_trial(auth_client, db_session):
    resp = await auth_client.post("/auth/register", json={
        "email": "trial@example.com", "password": "hunter2hunter2",
    })
    assert resp.status_code == 201
    tenant = await db_session.get(Tenant, uuid.UUID(resp.json()["tenant_id"]))
    assert tenant.subscription_status == SubscriptionStatus.TRIALING
    assert tenant.plan == BillingPlan.STARTER
    assert tenant.trial_ends_at is not None and tenant.trial_ends_at > _now()


# --- Stripe webhook ----------------------------------------------------------------


def _sub_event(event_id, customer, status="active", price=None, sub_id="sub_1"):
    return {
        "id": event_id,
        "type": "customer.subscription.updated",
        "data": {"object": {
            "id": sub_id,
            "customer": customer,
            "status": status,
            "current_period_end": int(_now().timestamp()) + 30 * 86400,
            "trial_end": None,
            "items": {"data": [{"price": {"id": price or "price_pro"}}]},
        }},
    }


@pytest.fixture
def stripe_env(monkeypatch):
    import app.routers.stripe_webhooks as hooks

    monkeypatch.setattr(hooks.settings, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(hooks.settings, "STRIPE_PRICE_PRO", "price_pro")
    monkeypatch.setattr(hooks.settings, "STRIPE_PRICE_STARTER", "price_starter")
    events = {}

    def _fake_construct(payload, sig, secret=None):
        assert sig == "sig-ok", "signature header must reach the verifier"
        return events["next"]

    monkeypatch.setattr(hooks, "_construct_event",
                        lambda payload, sig: _fake_construct(payload, sig))
    return events


async def test_webhook_projects_subscription_and_dedups(client, db_session, stripe_env):
    t = await _get_bootstrap(db_session)
    t.stripe_customer_id = "cus_123"
    await db_session.commit()

    stripe_env["next"] = _sub_event("evt_1", "cus_123", status="active")
    resp = await client.post("/webhooks/stripe", content=b"{}",
                             headers={"stripe-signature": "sig-ok"})
    assert resp.status_code == 200 and resp.json() == {"ok": True}

    await db_session.refresh(t)
    assert t.subscription_status == SubscriptionStatus.ACTIVE
    assert t.plan == BillingPlan.PRO
    assert t.stripe_subscription_id == "sub_1"
    assert t.current_period_end is not None

    # Same event id again → deduped, no reprocessing.
    stripe_env["next"] = _sub_event("evt_1", "cus_123", status="canceled")
    resp = await client.post("/webhooks/stripe", content=b"{}",
                             headers={"stripe-signature": "sig-ok"})
    assert resp.json() == {"ok": True, "duplicate": True}
    await db_session.refresh(t)
    assert t.subscription_status == SubscriptionStatus.ACTIVE  # unchanged


async def test_webhook_rejects_bad_signature(client, db_session, stripe_env):
    stripe_env["next"] = _sub_event("evt_sig", "cus_123")
    resp = await client.post("/webhooks/stripe", content=b"{}",
                             headers={"stripe-signature": "sig-WRONG"})
    assert resp.status_code == 400


async def test_webhook_checkout_completed_attaches_customer(client, db_session, stripe_env):
    stripe_env["next"] = {
        "id": "evt_2",
        "type": "checkout.session.completed",
        "data": {"object": {
            "client_reference_id": str(BOOTSTRAP_TENANT_ID),
            "customer": "cus_new",
            "subscription": "sub_new",
        }},
    }
    resp = await client.post("/webhooks/stripe", content=b"{}",
                             headers={"stripe-signature": "sig-ok"})
    assert resp.status_code == 200
    t = await _get_bootstrap(db_session)
    assert t.stripe_customer_id == "cus_new"
    assert t.stripe_subscription_id == "sub_new"


async def test_webhook_subscription_deleted_hard_gates(client, db_session, stripe_env):
    t = await _get_bootstrap(db_session)
    t.stripe_customer_id = "cus_123"
    t.subscription_status = SubscriptionStatus.ACTIVE
    await db_session.commit()

    stripe_env["next"] = {
        "id": "evt_3",
        "type": "customer.subscription.deleted",
        "data": {"object": {"customer": "cus_123", "current_period_end": None}},
    }
    await client.post("/webhooks/stripe", content=b"{}",
                      headers={"stripe-signature": "sig-ok"})
    await db_session.refresh(t)
    assert t.subscription_status == SubscriptionStatus.CANCELED
    allowed, reason = entitlements.spend_allowed(t)
    assert allowed is False and "canceled" in reason


# --- Billing summary endpoint -------------------------------------------------------


async def test_get_billing_summary(client, db_session):
    t = await _get_bootstrap(db_session)
    t.plan = BillingPlan.PRO
    t.subscription_status = SubscriptionStatus.ACTIVE
    await db_session.commit()

    resp = await client.get("/billing")
    assert resp.status_code == 200
    body = resp.json()
    assert body["plan"] == "pro"
    assert body["spend_allowed"] is True
    assert "email_send" in body["usage"]
    assert body["usage"]["email_send"]["quota"] == 10_000


async def test_checkout_400_when_unconfigured(client):
    resp = await client.post("/billing/checkout", json={"plan": "pro"})
    assert resp.status_code == 400
    assert "not configured" in resp.json()["detail"]
