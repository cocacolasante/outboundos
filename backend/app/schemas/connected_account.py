from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models import ConnectedAccountTestStatus


class ConnectedAccountCreate(BaseModel):
    label: str = Field(min_length=1)
    email_address: str = Field(min_length=1)
    imap_host: str = Field(min_length=1)
    imap_port: int = Field(default=993, ge=1, le=65535)
    imap_use_ssl: bool = True
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    # Optional signature block appended to one-off sends from this inbox.
    # See ConnectedAccount.signature for the apply-time semantics.
    signature: str | None = Field(default=None, max_length=5000)


class ConnectedAccountUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1)
    email_address: str | None = Field(default=None, min_length=1)
    imap_host: str | None = Field(default=None, min_length=1)
    imap_port: int | None = Field(default=None, ge=1, le=65535)
    imap_use_ssl: bool | None = None
    username: str | None = Field(default=None, min_length=1)
    password: str | None = Field(default=None, min_length=1)
    # Setting True flips this account to the workspace default sender —
    # the router clears the flag on every other row in the same
    # transaction.  Setting False just clears it on this one (the
    # workspace falls back to settings.BREVO_SENDER_EMAIL).
    is_default_sender: bool | None = None
    # Signature edits are allowed at any time; empty string or null
    # clears the signature so no append happens on subsequent sends.
    signature: str | None = Field(default=None, max_length=5000)


class ConnectedAccountResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    label: str
    email_address: str
    imap_host: str
    imap_port: int
    imap_use_ssl: bool
    username: str
    last_tested_at: datetime | None
    last_test_status: ConnectedAccountTestStatus
    last_test_error: str | None
    last_polled_at: datetime | None
    is_default_sender: bool = False
    signature: str | None = None
    created_at: datetime


class ConnectedAccountStatus(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    label: str
    email_address: str
    last_tested_at: datetime | None
    last_test_status: ConnectedAccountTestStatus
    last_test_error: str | None
    last_polled_at: datetime | None


class ImapTestResponse(BaseModel):
    ok: bool
    error: str | None = None
    message_count: int | None = None
