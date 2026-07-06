"""Per-tenant provider credentials (BYOK, multi-tenancy Phase 4).

One row per (tenant, provider).  ``encrypted_credentials`` is a Fernet
token over a JSON blob so multi-field creds fit one column:

    anthropic: {"api_key": ...}
    brevo:     {"api_key": ..., "sender_email": ..., "sender_name": ...}
    apollo:    {"api_key": ...}
    hunter:    {"api_key": ...}
    unipile:   {"dsn": ..., "api_key": ...}

Decryption happens ONLY in services/tenant_keys.py (the decrypt-allowlist
hardening test enforces it); plaintext never leaves local scope.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.tenancy.mixin import TenantMixin


class ProviderKind(str, enum.Enum):
    ANTHROPIC = "anthropic"
    BREVO = "brevo"
    APOLLO = "apollo"
    HUNTER = "hunter"
    UNIPILE = "unipile"


class KeyTestStatus(str, enum.Enum):
    UNTESTED = "untested"
    OK = "ok"
    FAILED = "failed"


class TenantProviderKey(TenantMixin, Base):
    __tablename__ = "tenant_provider_keys"
    __table_args__ = (
        UniqueConstraint("tenant_id", "provider",
                         name="uq_tenant_provider_keys_tenant_provider",
                         postgresql_nulls_not_distinct=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    provider: Mapped[ProviderKind] = mapped_column(
        Enum(ProviderKind, name="provider_kind",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    encrypted_credentials: Mapped[str] = mapped_column(Text, nullable=False)
    last_test_status: Mapped[KeyTestStatus] = mapped_column(
        Enum(KeyTestStatus, name="key_test_status",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=KeyTestStatus.UNTESTED,
        server_default=KeyTestStatus.UNTESTED.value,
    )
    last_tested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_test_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(), nullable=False,
    )
