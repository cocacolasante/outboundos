"""Platform transactional email (auth mail: password resets, verify).

This is the ONE platform-owned email path.  Tenant mail (campaigns,
agent notifications) rides tenant credentials; auth mail cannot — a
locked-out user has no tenant context and a tenant's own Brevo key
can't be a dependency of resetting the password that unlocks it.

Soft-fails by design: returns False (and logs) when the platform key
is unconfigured or Brevo errors, so auth flows never 500 on mail.
The reset link itself is still created; in dev you can read it from
the logs' absence and use the DB token if needed.
"""
from __future__ import annotations

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_BREVO_URL = "https://api.brevo.com/v3/smtp/email"
_TIMEOUT_SECONDS = 30


async def send_platform_email(
    *,
    to_email: str,
    subject: str,
    html_body: str,
    text_body: str,
) -> bool:
    """Send one transactional email from the platform sender.  Returns
    True on success, False on any failure (never raises)."""
    if not settings.PLATFORM_BREVO_API_KEY or not settings.PLATFORM_SENDER_EMAIL:
        logger.warning(
            "platform email not configured (PLATFORM_BREVO_API_KEY / "
            "PLATFORM_SENDER_EMAIL) — dropping %r to %s", subject, to_email,
        )
        return False
    payload = {
        "sender": {
            "name": settings.PLATFORM_SENDER_NAME,
            "email": settings.PLATFORM_SENDER_EMAIL,
        },
        "to": [{"email": to_email}],
        "subject": subject,
        "htmlContent": html_body,
        "textContent": text_body,
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                _BREVO_URL,
                json=payload,
                headers={
                    "api-key": settings.PLATFORM_BREVO_API_KEY,
                    "accept": "application/json",
                    "content-type": "application/json",
                },
            )
            resp.raise_for_status()
        return True
    except Exception:  # noqa: BLE001 — auth flows must never 500 on mail
        logger.exception("platform email send failed (%r to %s)", subject, to_email)
        return False
