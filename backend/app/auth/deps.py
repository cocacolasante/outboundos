"""Auth dependencies + session-token helpers (multi-tenancy Phase 1).

The gate for every feature router lives in ``app.database.get_db``,
which lazily calls :func:`authenticate_request` here (lazy so
``database.py`` never imports this module at import time — models
import ``database.Base``, this module imports models, and a
module-level import in ``database.py`` would complete the cycle).

Session model: the browser holds a random 256-bit token in an
``httpOnly`` cookie; the DB stores only its SHA-256
(``user_sessions.token_hash``).  The plaintext token is never
persisted or logged.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_public_db
from app.models.identity import Membership, Tenant, TenantStatus, User, UserSession
from app.tenancy.context import current_tenant_id

SESSION_COOKIE_NAME = "eb_session"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def new_session_token() -> str:
    """256-bit URL-safe random token for the session cookie."""
    return secrets.token_urlsafe(32)


def session_expiry() -> datetime:
    return _now() + timedelta(days=settings.SESSION_TTL_DAYS)


def set_session_cookie(response, raw_token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        raw_token,
        max_age=settings.SESSION_TTL_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=settings.SESSION_COOKIE_SECURE,
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


async def load_session(
    db: AsyncSession, raw_token: str,
) -> tuple[UserSession, User] | None:
    """Resolve a cookie token to a live (session, user) pair, or None."""
    result = await db.execute(
        select(UserSession, User)
        .join(User, User.id == UserSession.user_id)
        .where(
            UserSession.token_hash == hash_token(raw_token),
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > _now(),
        )
    )
    row = result.first()
    return (row[0], row[1]) if row else None


async def authenticate_request(request: Request, db: AsyncSession) -> uuid.UUID:
    """Authenticate the request from its session cookie and return the
    active tenant id.  Raises 401 when there is no live session and 403
    when the tenant is suspended/canceled.

    Called by ``app.database.get_db`` for EVERY feature endpoint —
    there is deliberately no anonymous path through ``get_db``.
    Stashes the resolved identity on ``request.state`` for handlers
    that need the user (role checks come with team features).
    """
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        raise HTTPException(status_code=401, detail="not authenticated")
    pair = await load_session(db, raw_token)
    if pair is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    session_row, user = pair

    tenant = await db.get(Tenant, session_row.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    if tenant.status != TenantStatus.ACTIVE:
        raise HTTPException(status_code=403, detail=f"tenant {tenant.status.value}")

    request.state.user = user
    request.state.session = session_row
    request.state.tenant = tenant
    return tenant.id


async def get_current_identity(
    request: Request,
    db: AsyncSession = Depends(get_public_db),
) -> tuple[User, UserSession, Tenant, Membership]:
    """Dependency for the /auth endpoints that need the caller's
    identity (``/auth/me``, logout).  Feature routers should NOT use
    this — they get tenancy through ``get_db``."""
    tenant_id = await authenticate_request(request, db)
    user: User = request.state.user
    session_row: UserSession = request.state.session
    tenant: Tenant = request.state.tenant
    membership = (
        await db.execute(
            select(Membership).where(
                Membership.tenant_id == tenant_id,
                Membership.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if membership is None:
        # Super-admin impersonation (Phase 7): the session is bound to a
        # tenant the admin has NO membership in.  Synthesize a transient
        # admin membership (never persisted — impersonation must not
        # pollute tenant data or seat counts).
        if is_superadmin(user.email):
            from app.models.identity import MembershipRole

            membership = Membership(
                tenant_id=tenant_id, user_id=user.id, role=MembershipRole.ADMIN,
            )
        else:
            raise HTTPException(status_code=401, detail="not authenticated")
    return user, session_row, tenant, membership


def is_superadmin(email: str) -> bool:
    allowed = {e.strip().lower() for e in settings.SUPERADMIN_EMAILS.split(",") if e.strip()}
    return bool(allowed) and email.strip().lower() in allowed


__all__ = [
    "SESSION_COOKIE_NAME",
    "authenticate_request",
    "clear_session_cookie",
    "current_tenant_id",
    "get_current_identity",
    "hash_token",
    "load_session",
    "new_session_token",
    "session_expiry",
    "set_session_cookie",
]
