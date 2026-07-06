from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Enum, Index, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.tenancy.mixin import TenantMixin

if TYPE_CHECKING:
    from app.models.campaign import Campaign


class ConnectedAccountTestStatus(str, enum.Enum):
    UNTESTED = "untested"
    OK = "ok"
    FAILED = "failed"


class ConnectedAccount(TenantMixin, Base):
    __tablename__ = "connected_accounts"
    __table_args__ = (
        # Partial unique index — only enforces uniqueness on TRUE rows so
        # any number of False rows is fine.  Mirrors the migration 0022
        # index; declared here too so the test DB (built from
        # Base.metadata, not migrations) also gets it.
        # Per-tenant single default sender (Phase 2 — was app-wide).
        # NULLS NOT DISTINCT so the legacy NULL-tenant rows also keep at
        # most one default among themselves.
        Index(
            "ix_connected_accounts_single_default_sender",
            "tenant_id",
            unique=True,
            postgresql_where=text("is_default_sender IS TRUE"),
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    email_address: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    imap_host: Mapped[str] = mapped_column(Text, nullable=False)
    imap_port: Mapped[int] = mapped_column(Integer, nullable=False, default=993, server_default="993")
    imap_use_ssl: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    username: Mapped[str] = mapped_column(Text, nullable=False)
    password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_test_status: Mapped[ConnectedAccountTestStatus] = mapped_column(
        Enum(ConnectedAccountTestStatus, name="connected_account_test_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=ConnectedAccountTestStatus.UNTESTED,
        server_default=ConnectedAccountTestStatus.UNTESTED.value,
    )
    last_test_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Workspace default sender for one-off sends (Research-a-client) and a
    # fallback for any future flow that doesn't carry an explicit from-
    # address.  A partial unique index on this column enforces "at most
    # one default at a time" at the DB layer; the PATCH handler also
    # clears the flag on every other row in the same transaction.  None /
    # all-False means "fall back to settings.BREVO_SENDER_EMAIL".
    is_default_sender: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    # Per-inbox signature block appended to one-off sends (the
    # Research-a-client tool).  The send endpoint resolves the chosen
    # ConnectedAccount by sender_email, then applies this via
    # ``app.services.signature.apply_signature`` — same idempotent helper
    # used by the per-Campaign signature feature.  Null/empty = no
    # append, body goes out as the AI composed it.
    signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    # IMAP poller dedup: last ~500 Message-IDs we've already turned into
    # REPLIED events.  Switching from "UNSEEN SINCE + mark as Seen" to
    # "SINCE + dedup-by-Message-ID" means the poller no longer touches
    # the user's read/unread state — they see new replies as unread in
    # their actual mailbox.  Trimmed in the worker to keep the JSON
    # column from growing unbounded.
    processed_imap_message_ids: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    campaigns: Mapped[list["Campaign"]] = relationship(
        back_populates="connected_account",
        passive_deletes=True,
    )
