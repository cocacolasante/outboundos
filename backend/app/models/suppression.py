from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.tenancy.mixin import TenantMixin


class SuppressionReason(str, enum.Enum):
    UNSUBSCRIBED = "unsubscribed"
    HARD_BOUNCE = "hard_bounce"
    SPAM = "spam"
    MANUAL = "manual"
    # Brevo blocklisted the contact (admin-blocked / blocklisted) — distinct
    # from a hard bounce or spam complaint; synced from Brevo's blocked-contacts
    # list and the real-time "blocked" transactional event.
    BLOCKED = "blocked"
    # Repeated (or, by default, a single) soft bounce — transient on its own
    # but suppressed to protect sender reputation once the per-address soft
    # bounce count reaches SOFT_BOUNCE_SUPPRESS_THRESHOLD.
    SOFT_BOUNCE = "soft_bounce"


def canonical_email(email: str | None) -> str:
    """The ONE canonicalisation every Suppression writer and reader must
    use.  Lead emails are lowercased at CSV ingest, so in practice rows
    match either way — but suppression is a safety list, and a single
    mixed-case write (a future non-CSV lead path, a manual script) must
    not silently bypass it."""
    return (email or "").strip().lower()


class Suppression(TenantMixin, Base):
    __tablename__ = "suppression_list"
    __table_args__ = (
        # Per-tenant suppression (Phase 2): an unsubscribe is from ONE
        # sender.  NULLS NOT DISTINCT keeps legacy/worker rows (tenant_id
        # still NULL until the worker-context phase) deduping app-wide,
        # exactly like the old UNIQUE(email).
        UniqueConstraint("tenant_id", "email", name="uq_suppression_tenant_email",
                         postgresql_nulls_not_distinct=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    reason: Mapped[SuppressionReason] = mapped_column(
        Enum(SuppressionReason, name="suppression_reason", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
