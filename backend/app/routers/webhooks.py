from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
# Webhooks + unsubscribe are deliberately tenant-blind public surfaces —
# they authenticate their callers by other means (HMAC token / static
# header) and resolve records globally, so they use the RLS-exempt
# service session (owner engine).  This is the ONLY feature router
# allowed to (hardening test).  Anything CREATED here must stamp
# tenant_id explicitly from the resolved record.
from app.database import get_service_db
from app.models import (
    Campaign,
    EmailEvent,
    EmailEventType,
    Lead,
    LinkedInAccount,
    LinkedInAccountStatus,
    LinkedInConnectionStatus,
    Suppression,
    canonical_email,
    SuppressionReason,
    WebhookEvent,
)
from app.services.brevo_events import process_event

logger = logging.getLogger(__name__)

# No prefix — the unsubscribe page lives at /unsubscribe/{lead_id} and
# Unipile posts to /webhooks/unipile (both at root).  We DON'T accept a
# Brevo inbound webhook — that data comes via brevo_events_poller pulling
# the /smtp/statistics/events API on a beat schedule, so we don't need a
# public tunnel for Brevo (and don't have to pay for the inbound add-on).
router = APIRouter(tags=["webhooks"])


# --------------------------------------------------------------------------
# Unsubscribe page
# --------------------------------------------------------------------------


_UNSUBSCRIBE_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Unsubscribed</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 600px; margin: 80px auto; padding: 20px; text-align: center; color: #333; line-height: 1.5;">
<h1 style="font-size: 24px;">You've been unsubscribed</h1>
<p>You will not receive further emails from us.</p>
</body>
</html>
"""

_INVALID_UNSUB_HTML = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Invalid link</title></head>
<body style="font-family: sans-serif; text-align: center; margin: 80px auto;">
<h1>Invalid unsubscribe link</h1>
<p>This link is not valid or has expired.</p>
</body>
</html>
"""

_CONFIRM_UNSUB_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Unsubscribe</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 600px; margin: 80px auto; padding: 20px; text-align: center; color: #333; line-height: 1.5;">
<h1 style="font-size: 24px;">Unsubscribe from these emails?</h1>
<p>Click the button below to stop receiving emails sent to <strong>__EMAIL__</strong>.</p>
<form method="post" action="__ACTION__" style="margin-top: 24px;">
<button type="submit" style="font-size: 16px; padding: 10px 24px; background: #b91c1c; color: white; border: 0; border-radius: 8px; cursor: pointer;">
Confirm unsubscribe
</button>
</form>
<p style="color: #888; font-size: 12px; margin-top: 32px;">
If you didn't mean to click this link, you can close this tab and nothing will change.
</p>
</body>
</html>
"""


def _unsubscribe_token(lead_id: uuid.UUID | str) -> str:
    """HMAC-SHA256 truncated to 32 hex chars, signed with ``SECRET_KEY``.

    Defends against (1) link-prefetchers like Outlook/Defender that GET
    every link in an incoming email — without a per-lead signature any
    automated scanner can mark every prospect on a campaign as
    unsubscribed; (2) attackers who learn a lead_id from one email and
    try to unsubscribe other leads on the same campaign (UUIDs aren't
    secrets).
    """
    if isinstance(lead_id, uuid.UUID):
        msg = lead_id.bytes
    else:
        msg = uuid.UUID(str(lead_id)).bytes
    return hmac.new(
        settings.SECRET_KEY.encode(), msg, hashlib.sha256,
    ).hexdigest()[:32]


def make_unsubscribe_url(lead_id: uuid.UUID | str) -> str:
    """Build the tokenised one-click unsubscribe URL for a lead.  Email
    templates that want an unsubscribe footer should use this helper so
    every link carries a valid signature."""
    return (
        f"{settings.WEBHOOK_BASE_URL.rstrip('/')}/unsubscribe/{lead_id}"
        f"?t={_unsubscribe_token(lead_id)}"
    )


@router.get("/unsubscribe/{lead_id}", response_class=HTMLResponse)
async def unsubscribe_confirm_page(
    lead_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_service_db),
) -> HTMLResponse:
    """Render a confirmation page (no side effects).  Side effects move to
    POST so email-scanner GET prefetches can't auto-unsubscribe leads."""
    token = request.query_params.get("t") or ""
    if not hmac.compare_digest(token, _unsubscribe_token(lead_id)):
        return HTMLResponse(_INVALID_UNSUB_HTML, status_code=404)
    lead = await db.get(Lead, lead_id)
    if lead is None:
        return HTMLResponse(_INVALID_UNSUB_HTML, status_code=404)
    html = (_CONFIRM_UNSUB_HTML
            .replace("__EMAIL__", lead.email)
            .replace("__ACTION__", f"/unsubscribe/{lead_id}?t={token}"))
    return HTMLResponse(html)


@router.post("/unsubscribe/{lead_id}", response_class=HTMLResponse)
async def unsubscribe(
    lead_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_service_db),
) -> HTMLResponse:
    """Apply the suppression.  Tokenised + POST-only — a scanner GET never
    fires this branch.
    """
    token = request.query_params.get("t") or ""
    if not hmac.compare_digest(token, _unsubscribe_token(lead_id)):
        return HTMLResponse(_INVALID_UNSUB_HTML, status_code=404)
    lead = await db.get(Lead, lead_id)
    if lead is None:
        return HTMLResponse(_INVALID_UNSUB_HTML, status_code=404)

    # Service path = no ambient tenant — stamp tenant_id explicitly from
    # the resolved lead (an unsubscribe suppresses for THAT sender's tenant).
    existing = await db.scalar(
        select(Suppression).where(
            Suppression.email == canonical_email(lead.email),
            Suppression.tenant_id == lead.tenant_id,
        )
    )
    if existing is None:
        db.add(Suppression(
            email=canonical_email(lead.email),
            reason=SuppressionReason.UNSUBSCRIBED,
            tenant_id=lead.tenant_id,
        ))

    db.add(EmailEvent(
        lead_id=lead.id,
        campaign_id=lead.campaign_id,
        event_type=EmailEventType.UNSUBSCRIBED,
        event_data={"source": "one_click_link"},
        tenant_id=lead.tenant_id,
    ))
    await db.commit()
    return HTMLResponse(_UNSUBSCRIBE_HTML)


# --------------------------------------------------------------------------
# Unipile webhooks
# --------------------------------------------------------------------------
#
# Unipile pushes events to us instead of us polling.  Events we care about:
#
#   account.connected     — user finished the hosted-auth flow; payload
#                           includes the new account_id + the "name" we
#                           supplied at link-creation time (our local
#                           LinkedInAccount.id), letting us correlate.
#   account.disconnected  — user revoked / cookies expired; flip status to
#                           FAILED so the sequencer stops dispatching.
#   account.checkpoint    — Unipile is waiting on captcha/2FA.  Flip to
#                           CHALLENGED and surface the URL.
#   message.received      — inbound DM on LinkedIn; update Lead's
#                           linkedin_last_reply_at + connection_status.
#   invitation.accepted   — prospect accepted our connect request; flip
#                           Lead.linkedin_connection_status -> CONNECTED.
#
# Auth: Unipile signs the raw body with HMAC-SHA256 using the shared
# secret configured in their dashboard.  Signature lives in the
# ``X-Unipile-Signature`` header (or ``Unipile-Signature`` depending on
# their delivery flavour).  We constant-time compare.  Missing secret or
# missing/invalid signature → 401 (no work done).


def _verify_unipile_auth(header_value: str | None) -> bool:
    """Compare the inbound auth header against the configured shared secret.

    Unipile doesn't HMAC-sign request bodies — when creating a webhook
    you specify a custom header + value (e.g. ``X-Unipile-Auth: <secret>``),
    Unipile echoes that exact header back on every delivery, and we
    constant-time compare the value here.  See the Unipile docs section
    "Authentication" on the Webhooks page.
    """
    expected = settings.UNIPILE_WEBHOOK_SECRET
    if not expected:
        # No secret configured → refuse everything to avoid running on
        # forged payloads.  Set UNIPILE_WEBHOOK_SECRET in .env and add the
        # same value to each Unipile webhook's headers list.
        return False
    if not header_value:
        return False
    return hmac.compare_digest(header_value.strip(), expected)


def _verify_brevo_auth(header_value: str | None) -> bool:
    """Same static-shared-secret-header pattern as Unipile, for Brevo's
    outbound event webhook.  Brevo lets you attach custom headers when you
    create a webhook; we set ``X-Brevo-Auth: <secret>`` there and constant-time
    compare it against ``settings.BREVO_WEBHOOK_SECRET``.  No secret configured
    → reject everything (don't act on forged payloads)."""
    expected = settings.BREVO_WEBHOOK_SECRET
    if not expected:
        return False
    if not header_value:
        return False
    return hmac.compare_digest(header_value.strip(), expected)


async def _handle_account_connected(
    db: AsyncSession, payload: dict[str, Any]
) -> None:
    """Bind the Unipile account_id we just learned about to the local row.

    Correlation:
    - The placeholder row was created by POST /linkedin-accounts/connect-via-unipile
      with our local id passed to Unipile as ``name``.  Unipile echoes
      that back in the webhook payload under ``name`` (or ``account.name``).
    - We look up the row by id and stamp ``unipile_account_id`` + email +
      status from the webhook body.
    """
    body = payload.get("data") or payload.get("account") or payload
    local_id_raw = (
        body.get("name")
        or (body.get("account") or {}).get("name")
        or payload.get("name")
    )
    unipile_id = (
        body.get("account_id")
        or body.get("id")
        or (body.get("account") or {}).get("id")
    )
    if not local_id_raw or not unipile_id:
        logger.warning(
            "Unipile account.connected missing name/account_id: %s",
            payload,
        )
        return
    try:
        local_id = uuid.UUID(str(local_id_raw))
    except (ValueError, TypeError):
        logger.warning(
            "Unipile account.connected name not a UUID: %r",
            local_id_raw,
        )
        return
    acc = await db.get(LinkedInAccount, local_id)
    if acc is None:
        logger.warning(
            "Unipile account.connected references unknown local id %s",
            local_id,
        )
        return
    acc.unipile_account_id = str(unipile_id)
    acc.provider_kind = "unipile"
    acc.status = LinkedInAccountStatus.OK
    acc.pending_challenge_url = None
    acc.last_error = None
    email = (
        body.get("user_email")
        or body.get("linkedin_email")
        or (body.get("user") or {}).get("email")
    )
    if email and not acc.linkedin_email.startswith("(pending"):
        # Keep the manually edited label/email if user set one; otherwise
        # take what Unipile gives us.
        pass
    elif email:
        acc.linkedin_email = email


async def _handle_account_status_change(
    db: AsyncSession, payload: dict[str, Any], new_status: LinkedInAccountStatus,
    challenge: bool = False,
) -> None:
    body = payload.get("data") or payload.get("account") or payload
    unipile_id = (
        body.get("account_id")
        or body.get("id")
        or (body.get("account") or {}).get("id")
    )
    if not unipile_id:
        return
    acc = await db.scalar(
        select(LinkedInAccount).where(LinkedInAccount.unipile_account_id == str(unipile_id))
    )
    if acc is None:
        return
    acc.status = new_status
    if challenge:
        acc.pending_challenge_url = "https://www.linkedin.com"
    acc.last_error = (
        body.get("detail")
        or body.get("message")
        or body.get("title")
        or new_status.value
    )


async def _find_lead_for_event(
    db: AsyncSession, account: LinkedInAccount, payload: dict[str, Any],
) -> Lead | None:
    """Match a Unipile event payload to one of our leads.

    Unipile event shapes differ by source.  ``new_relation`` events
    (which we treat as ``invitation.accepted``) put the user data at
    the top level: ``user_provider_id``, ``user_public_identifier``,
    ``user_profile_url``.  ``message_received`` events nest it under
    ``sender``/``from``/``attendee``.  We try both.

    Matching is by slug — extracted from whichever field has the public
    identifier (slug, full profile URL) — compared against each lead's
    ``linkedin_url`` slug.  Falls back to comparing against the raw
    provider id (``ACoAA...``) if the lead carries one, in case the
    payload only exposes the canonical id.
    """
    if not isinstance(payload, dict):
        return None

    # Pull every plausible identifier out of the payload — top-level and
    # nested under sender/from/attendee/user/data.
    candidates: list[str] = []
    sender = payload.get("sender") or payload.get("from") or payload.get("attendee") or {}
    user = payload.get("user") or {}
    nested = payload.get("data") or payload.get("invitation") or payload.get("message") or {}
    if not isinstance(nested, dict):
        nested = {}

    for src in (payload, sender, user, nested):
        if not isinstance(src, dict):
            continue
        for key in (
            "user_public_identifier", "user_provider_id", "user_profile_url",
            "public_identifier", "provider_id", "public_id",
            "profile_url", "url",
        ):
            v = src.get(key)
            if v:
                candidates.append(str(v))

    if not candidates:
        return None

    # Normalise each candidate to its bare slug (last path segment, lower,
    # query stripped).  Keep the originals too so we can match against
    # raw provider ids.
    norm_set: set[str] = set()
    for c in candidates:
        norm_set.add(c.lower())
        slug = c.rstrip("/").split("/")[-1].split("?")[0].lower()
        if slug:
            norm_set.add(slug)

    lead_rows = (await db.execute(
        select(Lead)
        .join(Campaign, Campaign.id == Lead.campaign_id)
        .where(Campaign.linkedin_account_id == account.id)
    )).scalars().all()
    for l in lead_rows:
        if not l.linkedin_url:
            continue
        their_slug = l.linkedin_url.rstrip("/").split("/")[-1].split("?")[0].lower()
        if their_slug and their_slug in norm_set:
            return l
    return None


def _extract_unipile_account_id(payload: dict[str, Any]) -> str | None:
    """Pull the Unipile account_id from a webhook payload.  Unipile puts
    it at the top level (most event sources), occasionally nested under
    ``data``/``message``/``invitation``, and (rarely) under ``account.id``.
    """
    if not isinstance(payload, dict):
        return None
    candidates: list[Any] = [
        payload.get("account_id"),
        (payload.get("account") or {}).get("id") if isinstance(payload.get("account"), dict) else None,
    ]
    for nested_key in ("data", "message", "invitation"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            candidates.extend([
                nested.get("account_id"),
                (nested.get("account") or {}).get("id") if isinstance(nested.get("account"), dict) else None,
            ])
    for v in candidates:
        if v:
            return str(v)
    return None


async def _handle_message_received(
    db: AsyncSession, payload: dict[str, Any],
) -> None:
    unipile_id = _extract_unipile_account_id(payload)
    if not unipile_id:
        return
    acc = await db.scalar(
        select(LinkedInAccount).where(LinkedInAccount.unipile_account_id == unipile_id)
    )
    if acc is None:
        return
    lead = await _find_lead_for_event(db, acc, payload)
    if lead is None:
        logger.info(
            "Unipile message.received: no matching lead under account %s",
            unipile_id,
        )
        return
    # Timestamp may live at top-level or in a nested message/data dict.
    ts_raw = None
    for src in (payload, payload.get("data"), payload.get("message")):
        if isinstance(src, dict):
            ts_raw = ts_raw or src.get("timestamp") or src.get("created_at")
    lead.linkedin_last_reply_at = _parse_iso(ts_raw) or datetime.now(timezone.utc)
    # First inbound message implies 1st-degree connection.
    if lead.linkedin_connection_status != LinkedInConnectionStatus.CONNECTED:
        lead.linkedin_connection_status = LinkedInConnectionStatus.CONNECTED


async def _handle_invitation_accepted(
    db: AsyncSession, payload: dict[str, Any],
) -> None:
    unipile_id = _extract_unipile_account_id(payload)
    if not unipile_id:
        return
    acc = await db.scalar(
        select(LinkedInAccount).where(LinkedInAccount.unipile_account_id == unipile_id)
    )
    if acc is None:
        return
    lead = await _find_lead_for_event(db, acc, payload)
    if lead is None:
        logger.info(
            "Unipile invitation.accepted: no matching lead under account %s",
            unipile_id,
        )
        return
    lead.linkedin_connection_status = LinkedInConnectionStatus.CONNECTED
    logger.info(
        "Unipile invitation.accepted: lead=%s flipped to CONNECTED",
        lead.email,
    )


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        s = str(value)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


@router.post("/webhooks/unipile")
async def unipile_webhook(
    request: Request,
    db: AsyncSession = Depends(get_service_db),
) -> dict[str, Any]:
    """Receive Unipile push events.

    Auth: Unipile uses a static shared-secret-in-a-header pattern — when
    creating the webhook you add a custom header (default
    ``X-Unipile-Auth``) and Unipile echoes it on every delivery.  We
    constant-time compare against ``settings.UNIPILE_WEBHOOK_SECRET``.
    Missing or wrong value → 401, no DB writes.

    Idempotency: Unipile delivers at-least-once.  Every payload has an
    event id (in the ``id`` / ``event_id`` / ``webhook_id`` field
    depending on the source); we insert that id + provider="unipile"
    into the ``webhook_events`` table up front.  If the unique
    constraint fires the event has already been processed and we
    short-circuit with 200 + ``duplicate=true`` so Unipile stops
    retrying.  When the id is missing we fall back to a SHA-256 of the
    raw body — same-body duplicates dedup, but different payloads with
    the same logical event will retry (acceptable; that pattern hasn't
    been observed in practice).
    """
    raw = await request.body()
    # Header name is configurable so it can match whatever the user
    # configured in Unipile's webhook dashboard.  Starlette lowercases
    # header keys; we look up case-insensitively via .get().
    auth_value = request.headers.get(settings.UNIPILE_WEBHOOK_AUTH_HEADER.lower())
    if not _verify_unipile_auth(auth_value):
        raise HTTPException(status_code=401, detail="invalid auth header")
    try:
        payload = json.loads(raw)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")

    event_type = (
        payload.get("type")
        or payload.get("event")
        or payload.get("event_name")
        or ""
    ).lower()

    # Idempotency guard.  Try to claim the (provider, event_id) slot
    # before running any handler — if another delivery already wrote it,
    # the unique constraint trips and we return 200 without re-applying
    # state-altering handlers.
    event_id = (
        str(payload.get("id"))
        if payload.get("id") is not None
        else str(payload.get("event_id"))
        if payload.get("event_id") is not None
        else str(payload.get("webhook_id"))
        if payload.get("webhook_id") is not None
        else None
    )
    if not event_id:
        # Fallback: hash the body so byte-identical retries dedup.
        event_id = "sha256:" + hashlib.sha256(raw).hexdigest()
    # Commit the dedup INSERT eagerly so it's persistent regardless of
    # whether we then route to a handler, ignore the event, or one of
    # the handlers fails mid-flight.  The contract: "we've acknowledged
    # this event id; don't deliver it again."  If a handler raises after
    # this commit, the next retry from Unipile is silently dropped — at-
    # most-once on our side.  Acceptable trade-off: at-least-once with
    # double-apply (the prior behaviour) was the actual problem.
    try:
        db.add(WebhookEvent(provider="unipile", event_id=event_id))
        await db.commit()
    except Exception:  # noqa: BLE001
        # UniqueViolation from a duplicate event_id.
        await db.rollback()
        logger.info(
            "Unipile webhook: duplicate event_id=%r type=%r — skipping handler",
            event_id, event_type,
        )
        return {"ok": True, "duplicate": True}
    logger.info("Unipile webhook event=%r id=%r", event_type, event_id)

    # Route by event name.  Unipile occasionally varies the casing /
    # punctuation between versions (account.connected vs ACCOUNT_CONNECTED),
    # so we normalise to lowercase + dot.
    normalised = event_type.replace("_", ".").replace(":", ".")

    if normalised in {"account.connected", "account.created", "creation.success"}:
        await _handle_account_connected(db, payload)
    elif normalised in {"account.disconnected", "account.deleted", "creation.fail"}:
        await _handle_account_status_change(db, payload, LinkedInAccountStatus.FAILED)
    elif normalised in {"account.checkpoint", "account.error.checkpoint"}:
        await _handle_account_status_change(
            db, payload, LinkedInAccountStatus.CHALLENGED, challenge=True,
        )
    elif normalised in {"message.received", "messaging.message.received", "new.message"}:
        await _handle_message_received(db, payload)
    elif normalised in {"invitation.accepted", "user.relation.created", "new.relation"}:
        await _handle_invitation_accepted(db, payload)
    else:
        logger.info("Unipile webhook: ignoring event_type=%r", event_type)
        return {"ok": True, "ignored": True}

    await db.commit()
    return {"ok": True}


@router.post("/webhooks/brevo")
async def brevo_webhook(
    request: Request,
    db: AsyncSession = Depends(get_service_db),
) -> dict[str, Any]:
    """Receive Brevo transactional event pushes in real time.

    Auth: static shared-secret header (``X-Brevo-Auth`` by default) configured
    on the webhook in Brevo and constant-time compared against
    ``settings.BREVO_WEBHOOK_SECRET``.  Missing/wrong → 401, no DB writes.

    Idempotency: Brevo retries on non-2xx (at-least-once).  We claim the event
    id (payload ``id``, else a SHA-256 of the body) in ``webhook_events`` up
    front; a duplicate delivery short-circuits with 200.

    Events funnel through the SAME ``process_event`` the poller uses, so a
    hard bounce / spam / unsubscribe / blocked is suppressed (added to the
    ignore list, halted in current campaigns, blocked from future ones) within
    seconds.  The poller stays on as the reconciliation backstop for anything
    missed while the tunnel/app was down (webhooks aren't replayed).
    """
    raw = await request.body()
    auth_value = request.headers.get(settings.BREVO_WEBHOOK_AUTH_HEADER.lower())
    if not _verify_brevo_auth(auth_value):
        raise HTTPException(status_code=401, detail="invalid auth header")
    try:
        payload = json.loads(raw)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc
    # Brevo's transactional event webhook posts one event per request; accept a
    # list too defensively.
    events = payload if isinstance(payload, list) else [payload]
    if not events or not all(isinstance(e, dict) for e in events):
        raise HTTPException(status_code=400, detail="payload must be an event object or list")

    first = events[0]
    event_id = str(first.get("id")) if first.get("id") is not None else None
    if not event_id:
        # No id (or a batch) → hash the body so byte-identical retries dedup.
        event_id = "sha256:" + hashlib.sha256(raw).hexdigest()
    try:
        db.add(WebhookEvent(provider="brevo", event_id=event_id))
        await db.commit()
    except Exception:  # noqa: BLE001 — UniqueViolation = already processed
        await db.rollback()
        logger.info("Brevo webhook: duplicate event_id=%r — skipping", event_id)
        return {"ok": True, "duplicate": True}

    recorded = 0
    for ev in events:
        try:
            if await process_event(db, ev):
                recorded += 1
        except Exception:  # noqa: BLE001 — one bad event mustn't drop the rest
            logger.exception("Brevo webhook: process_event failed for %r", ev)
    await db.commit()
    logger.info("Brevo webhook: processed %d/%d event(s) id=%r", recorded, len(events), event_id)
    return {"ok": True, "recorded": recorded}
