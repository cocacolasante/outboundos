"""Shared transactional-send + CRM-tracking core.

One place for "send a one-off email from the chosen inbox and log it to
the CRM" — used by the Research-a-client send and the prospect-signal
outreach send.

It resolves the from-address (explicit override > workspace default
sender > ``BREVO_SENDER_EMAIL``) and applies that account's signature
(same renderer as bulk campaign sends), sends via Brevo, then logs the
touch: an outbound email ``CrmActivity`` against a lead (the one passed
in, else find-or-create by email) plus any matching opportunity.

CRM tracking is best-effort — the email already left Brevo, so a
tracking hiccup logs + rolls back and the send still counts as success.
Brevo failures raise ``OutreachSendError`` (the router maps it to 502).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

import httpx
from sqlalchemy import func as sa_func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    ConnectedAccount,
    CrmActivity,
    CrmActivityDirection,
    CrmActivityType,
    Lead,
    Opportunity,
)
from app.services import agent_core, brevo
from app.services.signature import render_email_with_signature

logger = logging.getLogger(__name__)


@dataclass
class OutreachResult:
    message_id: str
    crm_lead_id: str | None = None
    crm_lead_created: bool = False
    crm_activity_logged: bool = False


class OutreachSendError(Exception):
    """Brevo / config failure during send.  Carries the HTTP status the
    router should surface (always 502 here)."""

    def __init__(self, detail: str, status_code: int = 502):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


async def send_and_track(
    db: AsyncSession,
    *,
    to_email: str,
    subject: str,
    body: str,
    sender_name: str,
    to_name: str | None = None,
    sender_email: str | None = None,
    signature: str | None = None,
    lead: Lead | None = None,
    campaign_tag: str = "research-client",
) -> OutreachResult:
    """Send + CRM-track.  ``lead`` pre-resolves the CRM lead to log
    against (e.g. a signal's already-staged lead); when None we
    find-or-create by email.  ``signature`` overrides the sender account's
    signature for this one send (``""`` = send no signature; ``None`` =
    fall back to the account's).  Raises ``OutreachSendError`` on send
    failure; CRM tracking failures are swallowed (logged + rolled back)."""
    if not settings.BREVO_API_KEY:
        raise OutreachSendError(
            "BREVO_API_KEY is not configured.  Add it to .env and "
            "recreate the backend container."
        )

    # From-address priority: explicit override > workspace default sender
    # > BREVO_SENDER_EMAIL.  We load the full ConnectedAccount so we can
    # apply its signature too (an unmatched override = no signature).
    sender_account: ConnectedAccount | None = None
    if sender_email:
        sender_account = await db.scalar(
            select(ConnectedAccount)
            .where(ConnectedAccount.email_address == sender_email)
            .limit(1)
        )
    else:
        sender_account = await db.scalar(
            select(ConnectedAccount)
            .where(ConnectedAccount.is_default_sender.is_(True))
            .limit(1)
        )
        sender_email = (
            sender_account.email_address if sender_account is not None
            else settings.BREVO_SENDER_EMAIL
        )

    # Explicit per-send signature override wins (incl. "" = no signature);
    # None falls back to the sender account's signature.
    effective_signature = (
        signature if signature is not None
        else (sender_account.signature if sender_account else None)
    )
    html_body, text_body = render_email_with_signature(body, effective_signature)

    try:
        message_id = await brevo.send_email(
            to_email=str(to_email),
            to_name=to_name,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
            sender_name=sender_name,
            sender_email=str(sender_email),
            campaign_id=campaign_tag,
            lead_id=str(uuid.uuid4()),  # synthetic — no campaign backs this
        )
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "outreach send: Brevo %s on send to %s — %s",
            exc.response.status_code, to_email, exc.response.text[:200],
        )
        raise OutreachSendError(
            f"Brevo rejected the send (HTTP {exc.response.status_code}). "
            "Check BREVO_API_KEY validity and the sender email is "
            "verified on your Brevo account."
        ) from exc
    except RuntimeError as exc:
        logger.warning("outreach send: %s", exc)
        raise OutreachSendError(str(exc)) from exc
    except httpx.HTTPError as exc:
        logger.warning("outreach send: network error to Brevo: %s", exc)
        raise OutreachSendError(f"Network error talking to Brevo: {exc}") from exc

    result = OutreachResult(message_id=str(message_id))

    # ── CRM auto-tracking (best-effort) ───────────────────────────────
    try:
        canonical = to_email.strip().lower()
        target_lead = lead
        if target_lead is None:
            # Most-recently-updated lead with this email wins.
            target_lead = await db.scalar(
                select(Lead)
                .where(sa_func.lower(Lead.email) == canonical)
                .order_by(Lead.updated_at.desc())
                .limit(1)
            )
        if target_lead is None:
            first_name = last_name = None
            if to_name:
                parts = to_name.strip().split(None, 1)
                first_name = parts[0] or None
                last_name = parts[1] if len(parts) > 1 else None
            target_lead = Lead(
                campaign_id=None,
                email=canonical,
                first_name=first_name,
                last_name=last_name,
            )
            db.add(target_lead)
            await db.flush()
            result.crm_lead_created = True

        opportunity = await db.scalar(
            select(Opportunity)
            .where(sa_func.lower(Opportunity.email) == canonical)
            .order_by(Opportunity.updated_at.desc())
            .limit(1)
        )
        db.add(CrmActivity(
            lead_id=target_lead.id,
            opportunity_id=opportunity.id if opportunity is not None else None,
            activity_type=CrmActivityType.EMAIL,
            subject=subject[:500],
            body=(body[:1000] + "…") if len(body) > 1000 else body,
            direction=CrmActivityDirection.OUTBOUND,
        ))
        await db.flush()
        # The outbound touch closes any open due/overdue "reach out" task on
        # this lead/deal — the reminder's work has now been done.
        await agent_core.autocomplete_due_tasks_for_record(
            db,
            lead_id=target_lead.id,
            opportunity_id=opportunity.id if opportunity is not None else None,
        )
        await db.commit()
        result.crm_lead_id = str(target_lead.id)
        result.crm_activity_logged = True
    except Exception:  # noqa: BLE001 — tracking is an enhancement on a sent email
        logger.exception("outreach send: CRM tracking failed for %s", to_email)
        await db.rollback()

    return result
