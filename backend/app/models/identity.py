"""Identity layer: Tenant, User, Membership, UserSession, AuthToken.

Multi-tenancy Phase 1 (see docs/tenancy-plan.md §5).  These five tables
are the ONLY ones queried before a tenant context exists, so they will
never carry RLS policies — every other tenant-owned table gets a
``tenant_id`` FK pointing at ``tenants`` in Phase 2.

Naming guard: the customer entity is ``Tenant`` — never ``Org`` (an
intent-engine prospect company, models/intent.py) or ``Account`` /
``Contact`` (CRM records, models/crm.py).

Security notes:
- ``users.password_hash`` is argon2id via app/auth/passwords.py; it is
  nullable so an SSO-provisioned user (future) can exist without one.
- ``user_sessions.token_hash`` / ``auth_tokens.token_hash`` store the
  SHA-256 of the random secret the client holds — a DB leak is not a
  session/reset-token leak.  The plaintext token is never persisted.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TenantStatus(str, enum.Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CANCELED = "canceled"


class BillingPlan(str, enum.Enum):
    STARTER = "starter"
    PRO = "pro"
    AGENCY = "agency"


class SubscriptionStatus(str, enum.Enum):
    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    INCOMPLETE = "incomplete"


class MembershipRole(str, enum.Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class AuthTokenPurpose(str, enum.Enum):
    PASSWORD_RESET = "password_reset"
    EMAIL_VERIFY = "email_verify"
    # Team invite (Phase 6): the invited User row exists (passwordless)
    # with its Membership already attached — the token just sets the
    # password, reset-style.
    INVITE = "invite"


class Tenant(Base):
    """The paying customer / workspace.

    Billing (Phase 5): exactly one Stripe subscription per tenant, so the
    billing state lives HERE rather than in a subscriptions table —
    Stripe is the system of record for history; these columns are the
    webhook-projected current state.  ``subscription_status`` NULL =
    legacy/unbilled tenant (pre-Stripe reality; treated as an open-ended
    trial until the hardening phase flips the default).
    """

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[TenantStatus] = mapped_column(
        Enum(TenantStatus, name="tenant_status",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=TenantStatus.ACTIVE,
        server_default=TenantStatus.ACTIVE.value,
    )

    # --- Billing (Phase 5) ---
    stripe_customer_id: Mapped[str | None] = mapped_column(
        Text, nullable=True, unique=True,
    )
    stripe_subscription_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan: Mapped[BillingPlan | None] = mapped_column(
        Enum(BillingPlan, name="billing_plan",
             values_callable=lambda e: [m.value for m in e]),
        nullable=True,
    )
    subscription_status: Mapped[SubscriptionStatus | None] = mapped_column(
        Enum(SubscriptionStatus, name="subscription_status",
             values_callable=lambda e: [m.value for m in e]),
        nullable=True,
    )
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    trial_ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(), nullable=False,
    )


class User(Base):
    """A person who can log in.  SSO-ready: ``auth_provider`` +
    ``external_id`` are reserved (always NULL in v1) so OAuth can be
    added later without a migration."""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("auth_provider", "external_id",
                         name="uq_users_auth_provider_external_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    # Always stored lowercased (canonicalised in the auth router).
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    auth_provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(), nullable=False,
    )


class Membership(Base):
    """user ↔ tenant with a role.  One row per pair."""

    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id",
                         name="uq_memberships_tenant_user"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    role: Mapped[MembershipRole] = mapped_column(
        Enum(MembershipRole, name="membership_role",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=MembershipRole.MEMBER,
        server_default=MembershipRole.MEMBER.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


class UserSession(Base):
    """Server-side session.  The browser cookie holds the random token;
    this row holds only its SHA-256.  ``tenant_id`` is the ACTIVE tenant
    for the session (single-membership reality today; a tenant switcher
    later just updates this column)."""

    __tablename__ = "user_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


class AuthToken(Base):
    """Single-use out-of-band token (password reset, email verify).
    Stored hashed; 30-minute expiry; ``used_at`` makes it single-use."""

    __tablename__ = "auth_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    purpose: Mapped[AuthTokenPurpose] = mapped_column(
        Enum(AuthTokenPurpose, name="auth_token_purpose",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
