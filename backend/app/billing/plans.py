"""Plan definitions (multi-tenancy Phase 5).

Plans live in CODE, not the DB: quotas version with the enforcement
logic and there's no runtime-editing requirement at three tiers.  Stripe
price ids come from env (they differ between test/live mode).

Quota semantics:
- ``monthly_quotas`` — metered spend per calendar month, enforced by
  ``entitlements.check_quota`` via the atomic ``usage_counters`` upsert.
- ``static_limits`` — how many of a resource may EXIST, enforced at the
  creation endpoints via ``entitlements.check_static_limit``.
- ``None`` anywhere = unlimited.

The numbers below are the Phase-5 proposal — adjust here (one place)
after the pricing checkpoint.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.billing.entitlements import Meter
from app.config import settings
from app.models import BillingPlan


@dataclass(frozen=True)
class PlanDef:
    label: str
    monthly_quotas: dict[Meter, int | None] = field(default_factory=dict)
    static_limits: dict[str, int | None] = field(default_factory=dict)

    @property
    def stripe_price_id(self) -> str:
        return {
            "Starter": settings.STRIPE_PRICE_STARTER,
            "Pro": settings.STRIPE_PRICE_PRO,
            "Agency": settings.STRIPE_PRICE_AGENCY,
        }[self.label]


PLANS: dict[BillingPlan, PlanDef] = {
    BillingPlan.STARTER: PlanDef(
        label="Starter",
        monthly_quotas={
            Meter.EMAIL_SEND: 1_000,
            Meter.AI_RESEARCH: 1_000,
            Meter.AI_COMPOSE: 1_000,
            Meter.LINKEDIN_ACTION: 200,
        },
        static_limits={
            "active_campaigns": 3,
            "connected_accounts": 1,
            "linkedin_accounts": 1,
            "seats": 1,
        },
    ),
    BillingPlan.PRO: PlanDef(
        label="Pro",
        monthly_quotas={
            Meter.EMAIL_SEND: 10_000,
            Meter.AI_RESEARCH: 10_000,
            Meter.AI_COMPOSE: 10_000,
            Meter.LINKEDIN_ACTION: 1_000,
        },
        static_limits={
            "active_campaigns": 10,
            "connected_accounts": 3,
            "linkedin_accounts": 2,
            "seats": 3,
        },
    ),
    BillingPlan.AGENCY: PlanDef(
        label="Agency",
        monthly_quotas={
            Meter.EMAIL_SEND: 50_000,
            Meter.AI_RESEARCH: 50_000,
            Meter.AI_COMPOSE: 50_000,
            Meter.LINKEDIN_ACTION: 5_000,
        },
        static_limits={
            "active_campaigns": None,
            "connected_accounts": 10,
            "linkedin_accounts": 10,
            "seats": 10,
        },
    ),
}

# Plan applied when a tenant has none set (legacy/unbilled rows and the
# 14-day trial started at signup).
DEFAULT_PLAN = BillingPlan.STARTER
