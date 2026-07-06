"""Billing endpoints (multi-tenancy Phase 5).

Checkout + customer portal + a read-only billing summary.  Stripe's
Python SDK is synchronous — calls run in a thread so the event loop
never blocks.  Entitlements always derive from the server-side
webhook-projected state on the tenant row, never from anything the
client claims.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.entitlements import spend_allowed, usage_summary
from app.billing.plans import PLANS
from app.config import settings
from app.database import get_db
from app.models import BillingPlan, Tenant
from app.tenancy.context import current_tenant_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])


def _stripe():
    if not settings.STRIPE_SECRET_KEY:
        raise HTTPException(status_code=400, detail="billing is not configured")
    import stripe

    stripe.api_key = settings.STRIPE_SECRET_KEY
    return stripe


async def _tenant_or_401(db: AsyncSession) -> Tenant:
    tid = current_tenant_id.get()
    tenant = await db.get(Tenant, tid) if tid else None
    if tenant is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return tenant


class BillingSummary(BaseModel):
    plan: str | None
    subscription_status: str | None
    trial_ends_at: str | None
    current_period_end: str | None
    spend_allowed: bool
    blocked_reason: str
    usage: dict
    billing_configured: bool


class CheckoutRequest(BaseModel):
    plan: BillingPlan


class UrlResponse(BaseModel):
    url: str


@router.get("", response_model=BillingSummary)
async def get_billing(db: AsyncSession = Depends(get_db)) -> BillingSummary:
    tenant = await _tenant_or_401(db)
    allowed, reason = spend_allowed(tenant)
    return BillingSummary(
        plan=tenant.plan.value if tenant.plan else None,
        subscription_status=(
            tenant.subscription_status.value if tenant.subscription_status else None
        ),
        trial_ends_at=tenant.trial_ends_at.isoformat() if tenant.trial_ends_at else None,
        current_period_end=(
            tenant.current_period_end.isoformat() if tenant.current_period_end else None
        ),
        spend_allowed=allowed,
        blocked_reason=reason,
        usage=await usage_summary(db),
        billing_configured=bool(settings.STRIPE_SECRET_KEY),
    )


async def _ensure_customer(stripe, db: AsyncSession, tenant: Tenant, request: Request) -> str:
    if tenant.stripe_customer_id:
        return tenant.stripe_customer_id
    user = getattr(request.state, "user", None)
    customer = await asyncio.to_thread(
        stripe.Customer.create,
        name=tenant.name,
        email=getattr(user, "email", None),
        metadata={"tenant_id": str(tenant.id)},
    )
    tenant.stripe_customer_id = customer["id"]
    await db.commit()
    return tenant.stripe_customer_id


@router.post("/checkout", response_model=UrlResponse)
async def create_checkout(
    body: CheckoutRequest, request: Request, db: AsyncSession = Depends(get_db),
) -> UrlResponse:
    stripe = _stripe()
    plan_def = PLANS[body.plan]
    price_id = plan_def.stripe_price_id
    if not price_id:
        raise HTTPException(
            status_code=400,
            detail=f"no Stripe price configured for the {plan_def.label} plan",
        )
    tenant = await _tenant_or_401(db)
    customer_id = await _ensure_customer(stripe, db, tenant, request)
    session = await asyncio.to_thread(
        stripe.checkout.Session.create,
        mode="subscription",
        customer=customer_id,
        client_reference_id=str(tenant.id),
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{settings.FRONTEND_URL}/settings?billing=success",
        cancel_url=f"{settings.FRONTEND_URL}/settings?billing=canceled",
    )
    return UrlResponse(url=session["url"])


@router.post("/portal", response_model=UrlResponse)
async def create_portal(request: Request, db: AsyncSession = Depends(get_db)) -> UrlResponse:
    stripe = _stripe()
    tenant = await _tenant_or_401(db)
    if not tenant.stripe_customer_id:
        raise HTTPException(status_code=400, detail="no billing account yet — subscribe first")
    session = await asyncio.to_thread(
        stripe.billing_portal.Session.create,
        customer=tenant.stripe_customer_id,
        return_url=f"{settings.FRONTEND_URL}/settings",
    )
    return UrlResponse(url=session["url"])
