from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models import LinkedInAccountStatus


class LinkedInAccountUpdate(BaseModel):
    """Mutable fields on an existing LinkedInAccount row.

    All accounts are Unipile-managed — Unipile owns the LinkedIn session,
    so the only thing the caller can change on our side is the display
    label.
    """
    label: str | None = Field(default=None, min_length=1)


class LinkedInAccountResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    label: str
    linkedin_email: str
    status: LinkedInAccountStatus
    last_error: str | None
    last_tested_at: datetime | None
    last_polled_at: datetime | None
    pending_challenge_url: str | None
    created_at: datetime
    updated_at: datetime
    unipile_account_id: str | None = None
    provider_kind: str = "unipile"


class ConnectViaUnipileRequest(BaseModel):
    """Body for POST /linkedin-accounts/connect-via-unipile.

    Caller supplies a label and the front-end URL the user should land on
    once Unipile's hosted flow completes (success/failure variants).
    """
    label: str = Field(min_length=1)
    success_redirect_url: str = Field(min_length=1)
    failure_redirect_url: str | None = None


class ConnectViaUnipileResponse(BaseModel):
    """Returned by POST /linkedin-accounts/connect-via-unipile.

    ``account_id`` is our local LinkedInAccount.id — we create the row
    eagerly so the frontend can poll its status.  ``hosted_url`` is the
    Unipile URL the user opens to complete login.
    """
    account_id: uuid.UUID
    hosted_url: str


class LinkedInTestResponse(BaseModel):
    ok: bool
    status: LinkedInAccountStatus
    error: str | None = None
    challenge_url: str | None = None
    meta: dict[str, Any] | None = None


class DiscoverableUnipileAccount(BaseModel):
    """One LinkedIn account already linked in Unipile that hasn't yet been
    bound to a local LinkedInAccount row.  Returned by
    ``GET /linkedin-accounts/discoverable``.
    """
    unipile_account_id: str
    name: str | None = None           # display name from connection_params.im.username
    linkedin_email: str | None = None # if Unipile exposed it
    public_identifier: str | None = None
    status: str | None = None         # Unipile-side status string


class ImportFromUnipileRequest(BaseModel):
    """Bind a Unipile-side account to a fresh local LinkedInAccount row."""
    unipile_account_id: str = Field(min_length=1)
    label: str = Field(min_length=1)


class ResolveChallengeRequest(BaseModel):
    """Body posted after the user has manually resolved the challenge in
    their own browser. We just flip status back to UNTESTED and clear the
    challenge URL — the next /test call will re-validate.
    """
    note: str | None = None
