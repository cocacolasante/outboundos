"""Super-admin surface (Phase 7 hardening).

Gated on ``SUPERADMIN_EMAILS`` (email of the authenticated user).  Every
action writes an ``admin_audit`` row.  Impersonation issues a normal
session cookie bound to the TARGET tenant — from there the admin browses
the app exactly as that tenant (RLS and all); ``get_current_identity``
synthesizes a transient admin membership so /auth/me keeps working.
Log out (or log back in) to stop impersonating.
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import (
    get_current_identity,
    hash_token,
    is_superadmin,
    new_session_token,
    session_expiry,
    set_session_cookie,
)
from app.database import get_public_db
from app.models import (
    AdminAudit,
    BillingPlan,
    Membership,
    SubscriptionStatus,
    Tenant,
    User,
    UserSession,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


async def require_superadmin(identity=Depends(get_current_identity)):
    user, session_row, tenant, membership = identity
    if not is_superadmin(user.email):
        raise HTTPException(status_code=403, detail="superadmin access required")
    return user


def _audit(db: AsyncSession, user: User, action: str,
           target_tenant_id=None, detail: dict | None = None) -> None:
    db.add(AdminAudit(
        actor_user_id=user.id,
        actor_email=user.email,
        action=action,
        target_tenant_id=target_tenant_id,
        detail=detail,
    ))


class AdminTenant(BaseModel):
    id: str
    name: str
    slug: str
    plan: str | None
    subscription_status: str | None
    members: int


class PlanOverride(BaseModel):
    plan: BillingPlan | None = None
    subscription_status: SubscriptionStatus | None = None


@router.get("/tenants", response_model=list[AdminTenant])
async def list_tenants(
    user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_public_db),
) -> list[AdminTenant]:
    rows = (await db.execute(
        select(Tenant, func.count(Membership.id))
        .outerjoin(Membership, Membership.tenant_id == Tenant.id)
        .group_by(Tenant.id)
        .order_by(Tenant.created_at)
    )).all()
    return [
        AdminTenant(
            id=str(t.id), name=t.name, slug=t.slug,
            plan=t.plan.value if t.plan else None,
            subscription_status=(
                t.subscription_status.value if t.subscription_status else None
            ),
            members=count,
        )
        for t, count in rows
    ]


@router.post("/tenants/{tenant_id}/plan", response_model=AdminTenant)
async def override_plan(
    tenant_id: uuid.UUID,
    body: PlanOverride,
    user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_public_db),
) -> AdminTenant:
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="unknown tenant")
    changes: dict = {}
    if body.plan is not None:
        changes["plan"] = {"from": tenant.plan.value if tenant.plan else None,
                           "to": body.plan.value}
        tenant.plan = body.plan
    if body.subscription_status is not None:
        changes["subscription_status"] = {
            "from": tenant.subscription_status.value if tenant.subscription_status else None,
            "to": body.subscription_status.value,
        }
        tenant.subscription_status = body.subscription_status
    _audit(db, user, "plan_override", tenant.id, changes)
    await db.commit()
    members = (await db.execute(
        select(func.count(Membership.id)).where(Membership.tenant_id == tenant.id)
    )).scalar() or 0
    return AdminTenant(
        id=str(tenant.id), name=tenant.name, slug=tenant.slug,
        plan=tenant.plan.value if tenant.plan else None,
        subscription_status=(
            tenant.subscription_status.value if tenant.subscription_status else None
        ),
        members=members,
    )


@router.post("/tenants/{tenant_id}/impersonate")
async def impersonate(
    tenant_id: uuid.UUID,
    request: Request,
    response: Response,
    user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_public_db),
) -> dict:
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="unknown tenant")
    raw_token = new_session_token()
    db.add(UserSession(
        token_hash=hash_token(raw_token),
        user_id=user.id,
        tenant_id=tenant.id,
        expires_at=session_expiry(),
        ip=(request.client.host if request.client else None),
        user_agent=f"IMPERSONATION by {user.email}",
    ))
    _audit(db, user, "impersonate", tenant.id, {"tenant_slug": tenant.slug})
    await db.commit()
    set_session_cookie(response, raw_token)
    logger.warning("SUPERADMIN %s is impersonating tenant %s", user.email, tenant.slug)
    return {"ok": True, "tenant": tenant.slug}


@router.get("/audit")
async def list_audit(
    user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_public_db),
    limit: int = 100,
) -> list[dict]:
    rows = (await db.execute(
        select(AdminAudit).order_by(AdminAudit.created_at.desc()).limit(min(limit, 500))
    )).scalars().all()
    return [
        {
            "actor": r.actor_email,
            "action": r.action,
            "target_tenant_id": str(r.target_tenant_id) if r.target_tenant_id else None,
            "detail": r.detail,
            "at": r.created_at.isoformat(),
        }
        for r in rows
    ]
