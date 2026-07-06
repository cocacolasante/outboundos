"""Stripe webhook (multi-tenancy Phase 5).

Signature-verified via ``stripe.Webhook.construct_event``; idempotent via
the same ``webhook_events (provider, event_id)`` insert-claim pattern the
Unipile/Brevo webhooks use.  Handlers do ONE thing: idempotently project
Stripe's subscription state onto the tenant's billing columns — Stripe
stays the system of record; entitlements read only these columns.

Tenant-blind by design (resolves tenants by stripe customer id /
client_reference_id), so it runs on the service session like the other
webhooks — allowlisted in the hardening grep test.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_service_db
from app.models import BillingPlan, SubscriptionStatus, Tenant, WebhookEvent

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])

# Stripe subscription statuses we project; anything else maps to the
# closest gate (unpaid → canceled semantics, incomplete_expired → incomplete).
_STATUS_MAP = {
    "trialing": SubscriptionStatus.TRIALING,
    "active": SubscriptionStatus.ACTIVE,
    "past_due": SubscriptionStatus.PAST_DUE,
    "canceled": SubscriptionStatus.CANCELED,
    "unpaid": SubscriptionStatus.CANCELED,
    "incomplete": SubscriptionStatus.INCOMPLETE,
    "incomplete_expired": SubscriptionStatus.INCOMPLETE,
    "paused": SubscriptionStatus.PAST_DUE,
}


def _plan_from_price(price_id: str | None) -> BillingPlan | None:
    if not price_id:
        return None
    mapping = {
        settings.STRIPE_PRICE_STARTER: BillingPlan.STARTER,
        settings.STRIPE_PRICE_PRO: BillingPlan.PRO,
        settings.STRIPE_PRICE_AGENCY: BillingPlan.AGENCY,
    }
    mapping.pop("", None)
    return mapping.get(price_id)


def _ts(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc) if value else None
    except (TypeError, ValueError):
        return None


async def _tenant_by_customer(db: AsyncSession, customer_id: str | None) -> Tenant | None:
    if not customer_id:
        return None
    return (await db.execute(
        select(Tenant).where(Tenant.stripe_customer_id == customer_id)
    )).scalars().first()


def _subscription_fields(sub: dict) -> dict:
    price_id = None
    items = (sub.get("items") or {}).get("data") or []
    if items:
        price_id = ((items[0] or {}).get("price") or {}).get("id")
    fields = {
        "stripe_subscription_id": sub.get("id"),
        "subscription_status": _STATUS_MAP.get(sub.get("status")),
        "current_period_end": _ts(sub.get("current_period_end")),
        "trial_ends_at": _ts(sub.get("trial_end")),
    }
    plan = _plan_from_price(price_id)
    if plan is not None:
        fields["plan"] = plan
    return fields


async def _apply_subscription(db: AsyncSession, sub: dict) -> bool:
    tenant = await _tenant_by_customer(db, sub.get("customer"))
    if tenant is None:
        logger.warning("stripe subscription for unknown customer %r", sub.get("customer"))
        return False
    for key, value in _subscription_fields(sub).items():
        if value is not None or key in ("current_period_end", "trial_ends_at"):
            setattr(tenant, key, value)
    return True


async def _handle_checkout_completed(db: AsyncSession, obj: dict) -> None:
    """Attach the customer/subscription to the tenant that started checkout
    (client_reference_id) — the subscription.* events fill in the rest."""
    tenant = None
    ref = obj.get("client_reference_id")
    if ref:
        tenant = await db.get(Tenant, ref)
    if tenant is None:
        tenant = await _tenant_by_customer(db, obj.get("customer"))
    if tenant is None:
        logger.warning("stripe checkout for unknown tenant ref=%r", ref)
        return
    if obj.get("customer"):
        tenant.stripe_customer_id = obj["customer"]
    if obj.get("subscription"):
        tenant.stripe_subscription_id = obj["subscription"]


async def _handle_subscription_event(db: AsyncSession, obj: dict) -> None:
    await _apply_subscription(db, obj)


async def _handle_subscription_deleted(db: AsyncSession, obj: dict) -> None:
    tenant = await _tenant_by_customer(db, obj.get("customer"))
    if tenant is None:
        return
    tenant.subscription_status = SubscriptionStatus.CANCELED
    tenant.current_period_end = _ts(obj.get("current_period_end"))


async def _handle_invoice_failed(db: AsyncSession, obj: dict) -> None:
    tenant = await _tenant_by_customer(db, obj.get("customer"))
    if tenant is not None:
        tenant.subscription_status = SubscriptionStatus.PAST_DUE


_HANDLERS = {
    "checkout.session.completed": _handle_checkout_completed,
    "customer.subscription.created": _handle_subscription_event,
    "customer.subscription.updated": _handle_subscription_event,
    "customer.subscription.deleted": _handle_subscription_deleted,
    "invoice.payment_failed": _handle_invoice_failed,
}


def _construct_event(payload: bytes, sig_header: str) -> dict:
    """Isolated for tests to patch; verifies the Stripe signature."""
    import stripe

    return stripe.Webhook.construct_event(
        payload, sig_header, settings.STRIPE_WEBHOOK_SECRET,
    )


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_service_db)) -> dict:
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="stripe webhook not configured")
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        event = _construct_event(payload, sig_header)
    except Exception:  # noqa: BLE001 — bad signature / malformed payload
        raise HTTPException(status_code=400, detail="invalid signature")

    event_id = event.get("id") or ""
    event_type = event.get("type") or ""
    if not event_id:
        raise HTTPException(status_code=400, detail="event missing id")

    # Idempotency claim — same pattern as the Unipile/Brevo webhooks.
    try:
        db.add(WebhookEvent(provider="stripe", event_id=event_id))
        await db.commit()
    except Exception:  # noqa: BLE001 — UniqueViolation = already processed
        await db.rollback()
        logger.info("duplicate stripe event %s — skipping", event_id)
        return {"ok": True, "duplicate": True}

    handler = _HANDLERS.get(event_type)
    if handler is None:
        return {"ok": True, "ignored": event_type}
    obj = ((event.get("data") or {}).get("object")) or {}
    await handler(db, obj)
    await db.commit()
    logger.info("stripe webhook %s processed", event_type)
    return {"ok": True}
