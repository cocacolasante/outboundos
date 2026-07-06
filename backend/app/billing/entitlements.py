"""Entitlements / quota enforcement (multi-tenancy Phase 5).

``check_quota`` runs BEFORE any provider spend (the Brevo POST in the
send path, the Anthropic calls in research/compose, the Unipile action in
the sequencer) so a blocked tenant is stopped before money moves — in
workers as well as the API.  Callers catch :class:`QuotaExceeded` and
surface it as a visible failed/skipped reason (never a retry storm).

Gating (all in :func:`spend_allowed`):
- ``trialing`` / ``active``  → full plan quotas.  A lapsed trial
  (``trial_ends_at`` in the past, no active subscription) denies spend.
- ``past_due``               → spend allowed for ``BILLING_GRACE_DAYS``
  past ``current_period_end``, then denied.
- ``canceled`` / ``incomplete`` → spend denied.
- ``NULL`` status            → legacy/unbilled tenant (pre-Stripe rows):
  allowed on the default plan's quotas.  The hardening phase revisits.

Metering: one atomic upsert-increment per spend on
``usage_counters (tenant, YYYYMM, meter)`` — increment-then-check, so a
denied attempt can nudge the counter past the quota (harmless: the quota
was already exhausted; nothing was spent).

Entitlements always derive from server-side Stripe-projected state on the
tenant row — never client claims.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.tenancy.context import current_tenant_id


class Meter(str, enum.Enum):
    EMAIL_SEND = "email_send"
    AI_RESEARCH = "ai_research"
    AI_COMPOSE = "ai_compose"
    LINKEDIN_ACTION = "linkedin_action"


class QuotaExceeded(Exception):
    """Raised by check_quota when the tenant's plan blocks the spend.
    Callers surface it as a visible failed/skipped reason — never a
    retry storm."""

    def __init__(self, meter: Meter | None, detail: str = ""):
        self.meter = meter
        self.detail = detail
        name = meter.value if meter else "billing"
        super().__init__(f"quota exceeded: {name}" + (f" — {detail}" if detail else ""))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _period(now: datetime | None = None) -> str:
    return (now or _now()).strftime("%Y%m")


def spend_allowed(tenant) -> tuple[bool, str]:
    """(allowed, human reason-when-denied) for the tenant's billing state."""
    from app.models import SubscriptionStatus

    status = tenant.subscription_status
    if status is None:
        return True, ""  # legacy/unbilled — default-plan quotas still apply
    if status == SubscriptionStatus.ACTIVE:
        return True, ""
    if status == SubscriptionStatus.TRIALING:
        if tenant.trial_ends_at is not None and tenant.trial_ends_at < _now():
            return False, "trial expired — subscribe to keep sending"
        return True, ""
    if status == SubscriptionStatus.PAST_DUE:
        anchor = tenant.current_period_end
        if anchor is None or _now() <= anchor + timedelta(days=settings.BILLING_GRACE_DAYS):
            return True, ""
        return False, "payment past due — update billing to keep sending"
    return False, f"subscription {status.value} — subscribe to keep sending"


def plan_for(tenant):
    from app.billing.plans import DEFAULT_PLAN, PLANS

    return PLANS[tenant.plan or DEFAULT_PLAN]


async def _increment_counter(
    session: AsyncSession, tenant_id: uuid.UUID, meter: Meter, amount: int,
) -> int:
    """Atomic upsert-increment; returns the new count."""
    row = await session.execute(
        text(
            "INSERT INTO usage_counters (id, tenant_id, period, meter, count, updated_at) "
            "VALUES (gen_random_uuid(), :tid, :period, :meter, :amount, now()) "
            "ON CONFLICT (tenant_id, period, meter) "
            "DO UPDATE SET count = usage_counters.count + :amount, updated_at = now() "
            "RETURNING count"
        ),
        {"tid": str(tenant_id), "period": _period(),
         "meter": meter.value, "amount": amount},
    )
    new_count = row.scalar()
    await session.commit()
    return int(new_count)


async def check_quota_on(
    session: AsyncSession, meter: Meter, amount: int = 1,
    tenant_id: uuid.UUID | None = None,
) -> None:
    """Session-explicit variant (routers/tests).  Raises QuotaExceeded."""
    from app.models import Tenant

    tid = tenant_id if tenant_id is not None else current_tenant_id.get()
    if tid is None:
        return  # tenant-blind paths aren't metered
    tenant = await session.get(Tenant, tid)
    if tenant is None:
        return
    if tenant.subscription_status is None:
        # Legacy/unbilled tenant (pre-Stripe rows, incl. the single-operator
        # bootstrap): exempt by default — quotas arrive with a plan.
        # Hosted deployments flip BILLING_REQUIRE_SUBSCRIPTION once every
        # real tenant is on a plan.
        if settings.BILLING_REQUIRE_SUBSCRIPTION:
            raise QuotaExceeded(None, "no subscription — subscribe to keep sending")
        return
    allowed, reason = spend_allowed(tenant)
    if not allowed:
        raise QuotaExceeded(None, reason)
    quota = plan_for(tenant).monthly_quotas.get(meter)
    if quota is None:
        return
    new_count = await _increment_counter(session, tid, meter, amount)
    if new_count > quota:
        raise QuotaExceeded(
            meter,
            f"{plan_for(tenant).label} plan allows {quota} {meter.value} per month",
        )


async def check_quota(meter: Meter, amount: int = 1) -> None:
    """Ambient-tenant quota gate for the worker/service spend sites.
    Opens its own short-lived engine (workers run one asyncio loop per
    task).  Raises QuotaExceeded; callers convert to a visible reason."""
    tid = current_tenant_id.get()
    if tid is None:
        return
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await check_quota_on(session, meter, amount, tenant_id=tid)
    finally:
        await engine.dispose()


async def check_static_limit(session: AsyncSession, resource: str, model, *filters) -> None:
    """Gate CREATING one more of a resource (active campaigns, connected
    accounts, LinkedIn accounts).  Counts the tenant's existing rows —
    RLS scopes the count in production; the explicit tenant filter keeps
    it honest on owner-role dev/test sessions."""
    from app.models import Tenant

    tid = current_tenant_id.get()
    if tid is None:
        return
    tenant = await session.get(Tenant, tid)
    if tenant is None:
        return
    if tenant.subscription_status is None:
        if settings.BILLING_REQUIRE_SUBSCRIPTION:
            raise QuotaExceeded(None, "no subscription — subscribe to keep sending")
        return  # legacy/unbilled — see check_quota_on
    allowed, reason = spend_allowed(tenant)
    if not allowed:
        raise QuotaExceeded(None, reason)
    limit = plan_for(tenant).static_limits.get(resource)
    if limit is None:
        return
    q = select(func.count()).select_from(model).where(model.tenant_id == tid)
    for f in filters:
        q = q.where(f)
    existing = (await session.execute(q)).scalar() or 0
    if existing >= limit:
        raise QuotaExceeded(
            None,
            f"{plan_for(tenant).label} plan allows {limit} {resource.replace('_', ' ')}",
        )


async def usage_summary(session: AsyncSession) -> dict:
    """Current-period usage vs quotas for the billing UI."""
    from app.models import Tenant, UsageCounter

    tid = current_tenant_id.get()
    if tid is None:
        return {}
    tenant = await session.get(Tenant, tid)
    if tenant is None:
        return {}
    plan = plan_for(tenant)
    rows = (await session.execute(
        select(UsageCounter).where(
            UsageCounter.tenant_id == tid,
            UsageCounter.period == _period(),
        )
    )).scalars().all()
    used = {r.meter: r.count for r in rows}
    return {
        meter.value: {"used": used.get(meter.value, 0), "quota": quota}
        for meter, quota in plan.monthly_quotas.items()
    }
