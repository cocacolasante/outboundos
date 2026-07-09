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
import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
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
    EmailEvent,
    InviteLink,
    Membership,
    SubscriptionStatus,
    Tenant,
    TenantStatus,
    User,
    UserSession,
)
from app.models.usage import UsageCounter

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
    created_at: str | None = None
    owner_email: str | None = None
    email_sends: int = 0


class PlanOverride(BaseModel):
    plan: BillingPlan | None = None
    subscription_status: SubscriptionStatus | None = None


class InviteLinkCreate(BaseModel):
    label: str | None = None
    trial_days: int = 14
    max_uses: int | None = None


class InviteLinkResponse(BaseModel):
    id: str
    code: str
    label: str | None
    trial_days: int
    bypass_billing: bool
    max_uses: int | None
    use_count: int
    revoked: bool
    created_by_email: str
    created_at: str
    url: str


class AdminStats(BaseModel):
    total_tenants: int
    total_users: int
    total_emails_sent: int
    last_signup_at: str | None
    last_signup_email: str | None


# --- Tenants ----------------------------------------------------------------

@router.get("/tenants", response_model=list[AdminTenant])
async def list_tenants(
    user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_public_db),
) -> list[AdminTenant]:
    rows = (await db.execute(
        select(Tenant, func.count(Membership.id))
        .outerjoin(Membership, Membership.tenant_id == Tenant.id)
        .group_by(Tenant.id)
        .order_by(Tenant.created_at.desc())
    )).all()

    tenant_ids = [t.id for t, _ in rows]

    # Owner emails
    from app.models.identity import MembershipRole
    owner_rows = (await db.execute(
        select(Membership.tenant_id, User.email)
        .join(User, User.id == Membership.user_id)
        .where(
            Membership.tenant_id.in_(tenant_ids),
            Membership.role == MembershipRole.OWNER,
        )
    )).all()
    owner_map = {tid: email for tid, email in owner_rows}

    # Email send counts — all time (sum across all periods)
    usage_rows = (await db.execute(
        select(UsageCounter.tenant_id, func.coalesce(func.sum(UsageCounter.count), 0))
        .where(
            UsageCounter.tenant_id.in_(tenant_ids),
            UsageCounter.meter == "email_send",
        )
        .group_by(UsageCounter.tenant_id)
    )).all()
    usage_map = {tid: int(count) for tid, count in usage_rows}

    return [
        AdminTenant(
            id=str(t.id), name=t.name, slug=t.slug,
            plan=t.plan.value if t.plan else None,
            subscription_status=(
                t.subscription_status.value if t.subscription_status else None
            ),
            members=count,
            created_at=t.created_at.isoformat() if t.created_at else None,
            owner_email=owner_map.get(t.id),
            email_sends=usage_map.get(t.id, 0),
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


@router.post("/tenants/{tenant_id}/grant-free")
async def grant_free_access(
    tenant_id: uuid.UUID,
    user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_public_db),
) -> dict:
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="unknown tenant")
    tenant.subscription_status = SubscriptionStatus.ACTIVE
    tenant.plan = tenant.plan or BillingPlan.STARTER
    _audit(db, user, "grant_free", tenant.id, {"plan": tenant.plan.value})
    await db.commit()
    return {"ok": True}


@router.post("/tenants/{tenant_id}/cancel")
async def cancel_access(
    tenant_id: uuid.UUID,
    user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_public_db),
) -> dict:
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="unknown tenant")
    tenant.subscription_status = SubscriptionStatus.CANCELED
    _audit(db, user, "cancel_access", tenant.id)
    await db.commit()
    return {"ok": True}


@router.delete("/tenants/{tenant_id}")
async def delete_tenant(
    tenant_id: uuid.UUID,
    user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_public_db),
) -> dict:
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="unknown tenant")
    _audit(db, user, "delete_tenant", tenant.id, {"name": tenant.name, "slug": tenant.slug})
    # Revoke all sessions
    await db.execute(
        update(UserSession)
        .where(UserSession.tenant_id == tenant_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=func.now())
    )
    # Delete memberships (cascades from tenant FK)
    await db.execute(
        delete(Membership).where(Membership.tenant_id == tenant_id)
    )
    tenant.status = TenantStatus.CANCELED
    tenant.subscription_status = SubscriptionStatus.CANCELED
    await db.commit()
    return {"ok": True}


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


# --- Invite links -----------------------------------------------------------

@router.get("/invite-links", response_model=list[InviteLinkResponse])
async def list_invite_links(
    user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_public_db),
) -> list[InviteLinkResponse]:
    from app.config import settings
    rows = (await db.execute(
        select(InviteLink).order_by(InviteLink.created_at.desc())
    )).scalars().all()
    return [
        InviteLinkResponse(
            id=str(r.id),
            code=r.code,
            label=r.label,
            trial_days=r.trial_days,
            bypass_billing=r.bypass_billing,
            max_uses=r.max_uses,
            use_count=r.use_count,
            revoked=r.revoked,
            created_by_email=r.created_by_email,
            created_at=r.created_at.isoformat(),
            url=f"{settings.FRONTEND_URL}/login?mode=signup&invite_code={r.code}",
        )
        for r in rows
    ]


@router.post("/invite-links", response_model=InviteLinkResponse, status_code=201)
async def create_invite_link(
    body: InviteLinkCreate,
    user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_public_db),
) -> InviteLinkResponse:
    from app.config import settings
    code = secrets.token_urlsafe(16)
    link = InviteLink(
        code=code,
        trial_days=body.trial_days,
        bypass_billing=True,
        max_uses=body.max_uses,
        created_by_email=user.email,
        label=body.label,
    )
    db.add(link)
    _audit(db, user, "create_invite_link", detail={
        "code": code, "trial_days": body.trial_days, "max_uses": body.max_uses,
    })
    await db.commit()
    await db.refresh(link)
    return InviteLinkResponse(
        id=str(link.id),
        code=link.code,
        label=link.label,
        trial_days=link.trial_days,
        bypass_billing=link.bypass_billing,
        max_uses=link.max_uses,
        use_count=link.use_count,
        revoked=link.revoked,
        created_by_email=link.created_by_email,
        created_at=link.created_at.isoformat(),
        url=f"{settings.FRONTEND_URL}/login?mode=signup&invite_code={link.code}",
    )


@router.post("/invite-links/{link_id}/revoke")
async def revoke_invite_link(
    link_id: uuid.UUID,
    user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_public_db),
) -> dict:
    link = await db.get(InviteLink, link_id)
    if link is None:
        raise HTTPException(status_code=404, detail="unknown invite link")
    link.revoked = True
    _audit(db, user, "revoke_invite_link", detail={"code": link.code})
    await db.commit()
    return {"ok": True}


# --- Stats ------------------------------------------------------------------

@router.get("/stats", response_model=AdminStats)
async def get_stats(
    user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_public_db),
) -> AdminStats:
    total_tenants = (await db.execute(
        select(func.count(Tenant.id))
    )).scalar() or 0

    total_users = (await db.execute(
        select(func.count(User.id))
    )).scalar() or 0

    # Total email sends across all tenants
    total_emails = (await db.execute(
        select(func.coalesce(func.sum(UsageCounter.count), 0))
        .where(UsageCounter.meter == "email_send")
    )).scalar() or 0

    # Last signup
    latest_user = (await db.execute(
        select(User.email, User.created_at)
        .order_by(User.created_at.desc())
        .limit(1)
    )).first()

    return AdminStats(
        total_tenants=total_tenants,
        total_users=total_users,
        total_emails_sent=total_emails,
        last_signup_at=latest_user[1].isoformat() if latest_user else None,
        last_signup_email=latest_user[0] if latest_user else None,
    )


# --- Audit ------------------------------------------------------------------

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
