"""Campaign sending-identity validation.

The Brevo "From" on every campaign email is ``campaign.sender_email`` (see
``workers/send.py``).  Auto-created campaigns — notably the intent-engine draft
campaigns — start with a PLACEHOLDER sender, so we must confirm a real, valid
address is in place before the campaign is allowed to run.
"""
from __future__ import annotations

import re

# Placeholders the engine seeds that must be replaced before a campaign sends.
PLACEHOLDER_SENDER_EMAILS = {"you@example.com", "noreply@example.com"}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_sender_email(email: str | None) -> bool:
    """A non-blank, well-formed, non-placeholder email address."""
    e = (email or "").strip().lower()
    return bool(e) and e not in PLACEHOLDER_SENDER_EMAILS and bool(_EMAIL_RE.match(e))


def campaign_sender_ready(campaign) -> bool:
    """Whether the campaign has a usable From address to send under."""
    return is_valid_sender_email(getattr(campaign, "sender_email", None))
