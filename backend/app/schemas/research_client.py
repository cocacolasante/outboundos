from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


# Loose RFC-5322-ish email validator.  Avoids the ``email-validator``
# dependency (not in requirements yet); same shape the frontend uses for
# its disable-Send check.  Brevo does the strict validation at delivery
# time, so this is just a fast-fail guard on bogus typos before we burn
# a Brevo API call.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_email_like(v: str) -> str:
    s = (v or "").strip()
    if not _EMAIL_RE.match(s):
        raise ValueError("invalid email address")
    return s


class ResearchClientRequest(BaseModel):
    """Input for the one-off 'research a client' tool."""

    linkedin_url: str = Field(min_length=10, max_length=500)
    goal: str = Field(min_length=3, max_length=2000)
    tone: str = Field(default="professional", max_length=120)
    sender_name: str = Field(default="", max_length=120)
    research_mode: Literal["fast", "deep"] = "fast"
    output_kind: Literal["email", "linkedin_dm"] = "linkedin_dm"
    # Default 600 is a comfortable email; 300 is a comfortable DM.  We keep
    # one default and let the UI override it based on output_kind.
    char_limit: int = Field(default=600, ge=50, le=5000)


class ResearchedProfile(BaseModel):
    first_name: str
    last_name: str
    headline: str
    company: str
    company_website: str
    job_title: str
    industry: str
    found: bool
    quality: str  # "low" | "partial" | "rich"


class ResearchClientResponse(BaseModel):
    profile: ResearchedProfile
    research: dict[str, Any]
    subject: str
    body: str
    char_count: int
    duration_ms: int


# ---- Send-now (one-off) ---------------------------------------------------

class SendClientEmailRequest(BaseModel):
    """One-off transactional send from the 'Research a client' tool.

    No campaign / lead row exists for this flow — the send goes out via
    Brevo with synthetic header IDs.  The user may edit the
    AI-composed subject + body before sending; both come back here as
    plain strings (frontend owns the merge of any edits)."""

    to_email: str
    to_name: str | None = Field(default=None, max_length=200)
    subject: str = Field(min_length=1, max_length=998)  # RFC 5322 line cap
    body: str = Field(min_length=1, max_length=50_000)
    sender_name: str = Field(min_length=1, max_length=120)
    # Optional override; defaults to settings.BREVO_SENDER_EMAIL on the
    # server.  The frontend leaves this blank in v1.
    sender_email: str | None = None

    _v_to = field_validator("to_email")(_validate_email_like)

    @field_validator("sender_email")
    @classmethod
    def _v_sender(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        return _validate_email_like(v)


class SendClientEmailResponse(BaseModel):
    message_id: str
    sent_at: datetime
    to_email: str
    # CRM auto-tracking results (best-effort — None/False when the CRM
    # write failed; the email itself still went out).
    crm_lead_id: str | None = None
    crm_lead_created: bool = False
    crm_activity_logged: bool = False
