"""First-party auth: register / login / logout / me / forgot / reset.

Multi-tenancy Phase 1 (docs/tenancy-plan.md §5).  These are the only
feature endpoints on ``get_public_db`` (they run before a session
exists); everything else authenticates through ``app.database.get_db``.

Register creates the full identity chain in one transaction:
Tenant → owner User → Membership → UserSession (auto-login).
"""
from __future__ import annotations

import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import passwords
from app.auth.deps import (
    clear_session_cookie,
    get_current_identity,
    hash_token,
    new_session_token,
    session_expiry,
    set_session_cookie,
)
from app.config import settings
from app.database import get_public_db
from app.models.identity import (
    AuthToken,
    AuthTokenPurpose,
    Membership,
    MembershipRole,
    Tenant,
    User,
    UserSession,
)
from app.services.platform_email import send_platform_email
from app.services.tenant_seed import seed_tenant_defaults

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_RESET_TOKEN_TTL_MINUTES = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_email(email: str) -> str:
    return email.strip().lower()


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "workspace"


async def _unique_slug(db: AsyncSession, base: str) -> str:
    slug = base
    while True:
        exists = (
            await db.execute(select(Tenant.id).where(Tenant.slug == slug))
        ).scalar_one_or_none()
        if exists is None:
            return slug
        slug = f"{base}-{secrets.token_hex(3)}"


# --- Schemas ---------------------------------------------------------------


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    tenant_name: str | None = Field(default=None, max_length=200)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(max_length=200)


class ForgotRequest(BaseModel):
    email: EmailStr


class ResetRequest(BaseModel):
    token: str = Field(min_length=10, max_length=500)
    password: str = Field(min_length=8, max_length=200)


class MeResponse(BaseModel):
    user_id: str
    email: str
    tenant_id: str
    tenant_name: str
    tenant_slug: str
    role: str


# --- Helpers ---------------------------------------------------------------


async def _start_session(
    db: AsyncSession, response: Response, request: Request,
    user: User, tenant_id,
) -> None:
    raw_token = new_session_token()
    db.add(UserSession(
        token_hash=hash_token(raw_token),
        user_id=user.id,
        tenant_id=tenant_id,
        expires_at=session_expiry(),
        last_seen_at=_now(),
        ip=(request.client.host if request.client else None),
        user_agent=request.headers.get("user-agent"),
    ))
    set_session_cookie(response, raw_token)


def _me(user: User, tenant: Tenant, role: MembershipRole) -> MeResponse:
    return MeResponse(
        user_id=str(user.id),
        email=user.email,
        tenant_id=str(tenant.id),
        tenant_name=tenant.name,
        tenant_slug=tenant.slug,
        role=role.value,
    )


# --- Endpoints -------------------------------------------------------------


@router.post("/register", response_model=MeResponse, status_code=201)
async def register(
    body: RegisterRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_public_db),
) -> MeResponse:
    email = _canonical_email(body.email)
    existing = (
        await db.execute(select(User.id).where(User.email == email))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="an account with this email already exists")

    tenant_name = (body.tenant_name or "").strip() or f"{email.split('@')[0]}'s workspace"
    tenant = Tenant(name=tenant_name, slug=await _unique_slug(db, _slugify(tenant_name)))
    # Billing (Phase 5): every new workspace starts a time-boxed trial on
    # the default plan; entitlements deny spend once it lapses unsubscribed.
    from app.billing.plans import DEFAULT_PLAN
    from app.models.identity import SubscriptionStatus

    tenant.plan = DEFAULT_PLAN
    tenant.subscription_status = SubscriptionStatus.TRIALING
    tenant.trial_ends_at = _now() + timedelta(days=settings.BILLING_TRIAL_DAYS)
    user = User(email=email, password_hash=passwords.hash_password(body.password))
    db.add(tenant)
    db.add(user)
    await db.flush()
    db.add(Membership(tenant_id=tenant.id, user_id=user.id, role=MembershipRole.OWNER))
    # RLS: the seeding below inserts tenant-owned rows BEFORE any session
    # cookie exists, so the ambient-GUC listener has nothing to stamp —
    # under the non-owner runtime role the WITH CHECK policies would
    # reject them.  Stamp the transaction-local GUC explicitly now that
    # the tenant id is real (no-op when RLS isn't enabled / role is owner).
    await db.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": str(tenant.id)},
    )
    # Per-tenant defaults (Phase 2): the CRM "Default" pipeline + stages and
    # the tenant's AgentSettings row — what migrations seeded app-wide in
    # the single-tenant era.
    await seed_tenant_defaults(db, tenant.id)
    await _start_session(db, response, request, user, tenant.id)
    await db.commit()
    return _me(user, tenant, MembershipRole.OWNER)


@router.post("/login", response_model=MeResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_public_db),
) -> MeResponse:
    email = _canonical_email(body.email)
    user = (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    # One generic 401 for unknown email AND bad password — no account
    # enumeration.  Verify against a burner hash when the user is
    # missing so the two paths cost the same.
    if user is None or not user.password_hash:
        passwords.verify_password(passwords.hash_password("burner-password"), body.password)
        raise HTTPException(status_code=401, detail="invalid email or password")
    if not passwords.verify_password(user.password_hash, body.password):
        raise HTTPException(status_code=401, detail="invalid email or password")
    if passwords.needs_rehash(user.password_hash):
        user.password_hash = passwords.hash_password(body.password)

    membership = (
        await db.execute(
            select(Membership)
            .where(Membership.user_id == user.id)
            .order_by(Membership.created_at)
        )
    ).scalars().first()
    if membership is None:
        raise HTTPException(status_code=401, detail="invalid email or password")
    tenant = await db.get(Tenant, membership.tenant_id)

    await _start_session(db, response, request, user, tenant.id)
    await db.commit()
    return _me(user, tenant, membership.role)


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_public_db),
) -> dict:
    """Revoke the current session (idempotent — always clears the cookie)."""
    from app.auth.deps import SESSION_COOKIE_NAME, load_session

    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if raw_token:
        pair = await load_session(db, raw_token)
        if pair is not None:
            pair[0].revoked_at = _now()
            await db.commit()
    clear_session_cookie(response)
    return {"ok": True}


@router.get("/me", response_model=MeResponse)
async def me(identity=Depends(get_current_identity)) -> MeResponse:
    user, _session, tenant, membership = identity
    return _me(user, tenant, membership.role)


@router.post("/forgot")
async def forgot(body: ForgotRequest, db: AsyncSession = Depends(get_public_db)) -> dict:
    """Always 200 — never reveals whether the email has an account."""
    email = _canonical_email(body.email)
    user = (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if user is not None:
        raw_token = secrets.token_urlsafe(32)
        db.add(AuthToken(
            user_id=user.id,
            purpose=AuthTokenPurpose.PASSWORD_RESET,
            token_hash=hash_token(raw_token),
            expires_at=_now() + timedelta(minutes=_RESET_TOKEN_TTL_MINUTES),
        ))
        await db.commit()
        reset_url = f"{settings.FRONTEND_URL}/login?reset_token={raw_token}"
        await send_platform_email(
            to_email=email,
            subject="Reset your password",
            html_body=(
                f"<p>Someone requested a password reset for this address.</p>"
                f'<p><a href="{reset_url}">Reset your password</a> '
                f"(link expires in {_RESET_TOKEN_TTL_MINUTES} minutes).</p>"
                f"<p>If this wasn't you, ignore this email.</p>"
            ),
            text_body=(
                f"Reset your password (expires in {_RESET_TOKEN_TTL_MINUTES} "
                f"minutes): {reset_url}\nIf this wasn't you, ignore this email."
            ),
        )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Team / workspace management (multi-tenancy Phase 6)
# ---------------------------------------------------------------------------

_INVITE_TOKEN_TTL_DAYS = 7


class InviteRequest(BaseModel):
    email: EmailStr
    role: MembershipRole = MembershipRole.MEMBER


class AcceptInviteRequest(BaseModel):
    token: str = Field(min_length=10, max_length=500)
    password: str = Field(min_length=8, max_length=200)


class TenantUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class TeamMember(BaseModel):
    membership_id: str
    email: str
    role: str
    pending: bool  # invited but hasn't set a password yet


def _require_manager(membership: Membership) -> None:
    if membership.role not in (MembershipRole.OWNER, MembershipRole.ADMIN):
        raise HTTPException(status_code=403, detail="owner or admin role required")


@router.get("/team", response_model=list[TeamMember])
async def list_team(
    identity=Depends(get_current_identity),
    db: AsyncSession = Depends(get_public_db),
) -> list[TeamMember]:
    _user, _sess, tenant, _membership = identity
    rows = (await db.execute(
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(Membership.tenant_id == tenant.id)
        .order_by(Membership.created_at)
    )).all()
    return [
        TeamMember(
            membership_id=str(m.id),
            email=u.email,
            role=m.role.value,
            pending=u.password_hash is None,
        )
        for m, u in rows
    ]


@router.post("/team/invite", response_model=TeamMember, status_code=201)
async def invite_member(
    body: InviteRequest,
    identity=Depends(get_current_identity),
    db: AsyncSession = Depends(get_public_db),
) -> TeamMember:
    """Invite by email.  A new user gets a passwordless account + a
    set-password link (seat consumed at invite — that's what the plan's
    seat limit counts); an existing user just gains a membership."""
    from app.billing.entitlements import QuotaExceeded, check_static_limit
    from app.models.identity import AuthTokenPurpose
    from app.tenancy.context import current_tenant_id

    _user, _sess, tenant, membership = identity
    _require_manager(membership)
    if body.role == MembershipRole.OWNER:
        raise HTTPException(status_code=422, detail="invite as admin or member; ownership is transferred, not granted")

    token_ctx = current_tenant_id.set(tenant.id)  # seat count scopes to this tenant
    try:
        try:
            await check_static_limit(db, "seats", Membership)
        except QuotaExceeded as exc:
            raise HTTPException(status_code=402, detail=str(exc))
    finally:
        current_tenant_id.reset(token_ctx)

    email = _canonical_email(body.email)
    user = (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    invited_new = user is None
    if user is None:
        user = User(email=email, password_hash=None)
        db.add(user)
        await db.flush()

    existing = (
        await db.execute(select(Membership).where(
            Membership.tenant_id == tenant.id, Membership.user_id == user.id,
        ))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="already a member of this workspace")

    new_membership = Membership(tenant_id=tenant.id, user_id=user.id, role=body.role)
    db.add(new_membership)
    await db.flush()

    if invited_new:
        raw_token = secrets.token_urlsafe(32)
        db.add(AuthToken(
            user_id=user.id,
            purpose=AuthTokenPurpose.INVITE,
            token_hash=hash_token(raw_token),
            expires_at=_now() + timedelta(days=_INVITE_TOKEN_TTL_DAYS),
        ))
        invite_url = f"{settings.FRONTEND_URL}/login?invite_token={raw_token}"
        await send_platform_email(
            to_email=email,
            subject=f"You've been invited to {tenant.name}",
            html_body=(
                f"<p>You've been invited to the <b>{tenant.name}</b> workspace.</p>"
                f'<p><a href="{invite_url}">Set your password to join</a> '
                f"(link expires in {_INVITE_TOKEN_TTL_DAYS} days).</p>"
            ),
            text_body=(
                f"You've been invited to {tenant.name}. Set your password to "
                f"join (expires in {_INVITE_TOKEN_TTL_DAYS} days): {invite_url}"
            ),
        )
    await db.commit()
    return TeamMember(
        membership_id=str(new_membership.id),
        email=email,
        role=body.role.value,
        pending=invited_new,
    )


@router.post("/accept-invite")
async def accept_invite(
    body: AcceptInviteRequest, db: AsyncSession = Depends(get_public_db),
) -> dict:
    from app.models.identity import AuthTokenPurpose

    token_row = (
        await db.execute(
            select(AuthToken).where(
                AuthToken.token_hash == hash_token(body.token),
                AuthToken.purpose == AuthTokenPurpose.INVITE,
                AuthToken.used_at.is_(None),
                AuthToken.expires_at > _now(),
            )
        )
    ).scalar_one_or_none()
    if token_row is None:
        raise HTTPException(status_code=400, detail="invalid or expired invite")
    user = await db.get(User, token_row.user_id)
    if user is None:
        raise HTTPException(status_code=400, detail="invalid or expired invite")
    user.password_hash = passwords.hash_password(body.password)
    token_row.used_at = _now()
    await db.commit()
    return {"ok": True}


@router.delete("/team/{membership_id}", status_code=204, response_model=None)
async def remove_member(
    membership_id: uuid.UUID,
    identity=Depends(get_current_identity),
    db: AsyncSession = Depends(get_public_db),
) -> None:
    _user, _sess, tenant, membership = identity
    _require_manager(membership)
    target = await db.get(Membership, membership_id)
    if target is None or target.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="not a member of this workspace")
    if target.role == MembershipRole.OWNER:
        owners = (await db.execute(
            select(Membership).where(
                Membership.tenant_id == tenant.id,
                Membership.role == MembershipRole.OWNER,
            )
        )).scalars().all()
        if len(owners) <= 1:
            raise HTTPException(status_code=409, detail="cannot remove the last owner")
    # Revoke the removed user's sessions for THIS tenant.
    await db.execute(
        update(UserSession)
        .where(
            UserSession.user_id == target.user_id,
            UserSession.tenant_id == tenant.id,
            UserSession.revoked_at.is_(None),
        )
        .values(revoked_at=_now())
    )
    await db.delete(target)
    await db.commit()


@router.patch("/tenant", response_model=MeResponse)
async def rename_tenant(
    body: TenantUpdate,
    identity=Depends(get_current_identity),
    db: AsyncSession = Depends(get_public_db),
) -> MeResponse:
    user, _sess, tenant, membership = identity
    _require_manager(membership)
    tenant.name = body.name.strip()
    await db.commit()
    return _me(user, tenant, membership.role)


@router.post("/reset")
async def reset(body: ResetRequest, db: AsyncSession = Depends(get_public_db)) -> dict:
    token_row = (
        await db.execute(
            select(AuthToken).where(
                AuthToken.token_hash == hash_token(body.token),
                AuthToken.purpose == AuthTokenPurpose.PASSWORD_RESET,
                AuthToken.used_at.is_(None),
                AuthToken.expires_at > _now(),
            )
        )
    ).scalar_one_or_none()
    if token_row is None:
        raise HTTPException(status_code=400, detail="invalid or expired reset token")

    user = await db.get(User, token_row.user_id)
    if user is None:
        raise HTTPException(status_code=400, detail="invalid or expired reset token")
    user.password_hash = passwords.hash_password(body.password)
    token_row.used_at = _now()
    # A reset invalidates every live session for the user.
    await db.execute(
        update(UserSession)
        .where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    await db.commit()
    return {"ok": True}
