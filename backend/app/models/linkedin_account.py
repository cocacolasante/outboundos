from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.tenancy.mixin import TenantMixin

if TYPE_CHECKING:
    from app.models.campaign import Campaign


class LinkedInAccountStatus(str, enum.Enum):
    UNTESTED = "untested"
    OK = "ok"
    FAILED = "failed"          # bad creds or network failure
    CHALLENGED = "challenged"  # LinkedIn wants a captcha/PIN — needs user action
    RESTRICTED = "restricted"  # account is in LinkedIn's penalty box; stop all actions


class LinkedInAccount(TenantMixin, Base):
    """A user-connected LinkedIn account.

    All accounts are Unipile-managed: Unipile owns the LinkedIn session
    (cookies, IP, browser fingerprint) and exposes it via API calls keyed
    by ``unipile_account_id``.  The ``password_encrypted`` /
    ``session_cookies_encrypted`` / ``proxy_url`` columns are legacy
    remnants from the DIY Playwright era and stay nullable for any
    pre-strip rows; new rows leave them NULL.
    """
    __tablename__ = "linkedin_accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    linkedin_email: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    # Legacy DIY-era columns.  Always NULL on Unipile rows; kept nullable
    # to avoid a destructive migration on pre-strip data.
    password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_cookies_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    proxy_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Unipile's opaque account id, returned by the hosted-auth flow.
    # Provider methods that need to call Unipile read this off the model.
    unipile_account_id: Mapped[str | None] = mapped_column(
        Text, nullable=True, unique=True,
    )
    # Always "unipile" on rows created post-strip.  Legacy rows from the
    # DIY era still report "diy"; nothing in the codebase branches on this
    # any more, it's purely historical.
    provider_kind: Mapped[str] = mapped_column(
        Text, nullable=False, default="unipile", server_default="diy",
    )
    status: Mapped[LinkedInAccountStatus] = mapped_column(
        Enum(
            LinkedInAccountStatus,
            name="linkedin_account_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=LinkedInAccountStatus.UNTESTED,
        server_default=LinkedInAccountStatus.UNTESTED.value,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Pending challenge URL surfaced after a failed action — user resolves
    # in their own browser then calls /resolve-challenge.
    pending_challenge_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    campaigns: Mapped[list["Campaign"]] = relationship(
        back_populates="linkedin_account",
        passive_deletes=True,
    )
