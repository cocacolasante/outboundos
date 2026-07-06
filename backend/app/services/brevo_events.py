"""Shared Brevo event normalisation + persistence.

We pull transactional event data (delivered / opened / clicked / bounced /
spam / unsubscribed) by polling Brevo's ``GET /v3/smtp/statistics/events``
endpoint every few minutes — Brevo's outbound webhook would do the same
job in real time, but it's behind a paid plan tier on some accounts and
needs a public tunnel.  Polling is outbound-only (no tunnel for Brevo)
and works on the free transactional API key.

Both the (now-removed) webhook handler and the poller funnel events
through ``process_event`` so the persistence logic stays in one place.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    EmailEvent,
    EmailEventType,
    Lead,
    LeadStepExecution,
    SuppressionReason,
    canonical_email,
)


# Brevo uses a few different spellings for the same conceptual event across
# the webhook payload, the events API, and historical aliases.  Map any
# known variant onto our internal ``EmailEventType``.
EVENT_MAP: dict[str, EmailEventType] = {
    # Delivery confirmations
    "delivered": EmailEventType.DELIVERED,
    "request": EmailEventType.DELIVERED,  # precedes delivery in webhook payloads
    "requests": EmailEventType.DELIVERED,
    # Opens
    "opened": EmailEventType.OPENED,
    "unique_opened": EmailEventType.OPENED,
    # Clicks
    "click": EmailEventType.CLICKED,
    "clicked": EmailEventType.CLICKED,
    "clicks": EmailEventType.CLICKED,
    "unique_clicked": EmailEventType.CLICKED,
    # Bounces
    "soft_bounce": EmailEventType.SOFT_BOUNCE,
    "softbounce": EmailEventType.SOFT_BOUNCE,
    "softbounces": EmailEventType.SOFT_BOUNCE,
    "hard_bounce": EmailEventType.HARD_BOUNCE,
    "hardbounce": EmailEventType.HARD_BOUNCE,
    "hardbounces": EmailEventType.HARD_BOUNCE,
    # Reputation
    "spam": EmailEventType.SPAM,
    "unsubscribed": EmailEventType.UNSUBSCRIBED,
    # Blocklisted recipient — Brevo refused the send.
    "blocked": EmailEventType.BLOCKED,
    "blocklisted": EmailEventType.BLOCKED,
}

# Which event types automatically add the recipient to the suppression
# list.  Soft bounces deliberately don't — they're transient.
SUPPRESSION_MAP: dict[EmailEventType, SuppressionReason] = {
    EmailEventType.HARD_BOUNCE: SuppressionReason.HARD_BOUNCE,
    EmailEventType.SPAM: SuppressionReason.SPAM,
    EmailEventType.UNSUBSCRIBED: SuppressionReason.UNSUBSCRIBED,
    EmailEventType.BLOCKED: SuppressionReason.BLOCKED,
}


def extract_message_id(event: dict[str, Any]) -> str | None:
    """Webhook used ``message-id`` (with a dash); the events API uses
    ``messageId`` (camelCase).  Try both, strip the optional <...>
    delimiters most mail systems still echo.
    """
    for key in ("messageId", "message-id", "message_id"):
        v = event.get(key)
        if v:
            return str(v).strip("<>")
    return None


def extract_occurred_at(event: dict[str, Any]):
    """The event's real timestamp (Brevo sends ISO-8601 with tz in ``date``).
    Returns None to fall back to the DB default (now) when absent/unparseable —
    matters so a backfill places events on their REAL day, not today, and so the
    24h deliverability window only counts genuinely-recent events."""
    from datetime import datetime
    raw = event.get("date") or event.get("ts") or event.get("time")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def normalise_event_name(raw: Any) -> str:
    """Brevo emits names with mixed case and a couple of suffix styles
    (``hardBounce`` from the API, ``hard_bounce`` from the webhook,
    ``HARD_BOUNCE`` from some queue exports)."""
    return str(raw or "").strip().lower().replace("-", "_")


async def process_event(
    db: AsyncSession, event: dict[str, Any], *, apply_side_effects: bool = True,
) -> bool:
    """Persist a single Brevo event onto the matching Lead.

    Returns True when a row was recorded (lead found, event mapped),
    False when the event was a no-op (unknown event type, unknown
    messageId).  Idempotent at the database layer: if the lead already
    has an identical (event_type, brevo messageId) we skip the insert
    so the poller can safely re-fetch the same day window without
    duplicating event rows.

    ``apply_side_effects=False`` records the event row ONLY — no suppression,
    no circuit-breaker check.  Used by the historical backfill so re-recording
    old bounces/spam doesn't re-suppress or (mis)trip the breaker; suppression
    of those addresses is already handled by the blocklist sync.
    """
    event_type = EVENT_MAP.get(normalise_event_name(event.get("event")))
    if event_type is None:
        return False

    msg_id = extract_message_id(event)
    if not msg_id:
        return False

    # extract_message_id strips the <...> delimiters, but lead.brevo_message_id
    # is stored WITH them (the send path keeps Brevo's raw `<...@...>`), so a
    # bare `==` never matched and every open/click/delivered event was silently
    # dropped.  Match both forms.
    forms = [msg_id, f"<{msg_id}>"]
    lead = await db.scalar(
        select(Lead).where(Lead.brevo_message_id.in_(forms))
    )
    if lead is None:
        # The lead stores only the FIRST email's id; follow-up / reply sends
        # record THEIR Brevo id on lead_step_executions.external_id.  Without
        # this, opens/clicks/bounces on every email after the first are dropped
        # (the analytics under-count badly for multi-step campaigns).
        ex_lead_id = await db.scalar(
            select(LeadStepExecution.lead_id)
            .where(LeadStepExecution.external_id.in_(forms))
            .limit(1)
        )
        if ex_lead_id is not None:
            lead = await db.get(Lead, ex_lead_id)
    if lead is None:
        return False

    # Dedup: if this lead already has an event of this type that came from
    # the same Brevo messageId, treat it as already-processed.  The events
    # API has day-level granularity on `startDate`/`endDate`, so each poll
    # re-fetches today's events repeatedly — without this check we'd
    # accumulate one EmailEvent per poll tick per real event.
    existing = await db.scalar(
        select(EmailEvent.id).where(
            EmailEvent.lead_id == lead.id,
            EmailEvent.event_type == event_type,
        ).limit(1)
    )
    if existing is not None and event_type in {
        EmailEventType.DELIVERED,
        EmailEventType.HARD_BOUNCE,
        EmailEventType.SOFT_BOUNCE,
        EmailEventType.SPAM,
        EmailEventType.UNSUBSCRIBED,
        EmailEventType.BLOCKED,
    }:
        # These are one-shot terminal events per lead — if we already saw
        # one, do not record a second.  Opens and clicks legitimately
        # recur (one per open/click), so we always record those.
        return False

    occurred_at = extract_occurred_at(event)
    ev_kwargs: dict[str, Any] = dict(
        lead_id=lead.id,
        campaign_id=lead.campaign_id,
        event_type=event_type,
        event_data=event,
        # The poller runs tenant-blind (service path) — stamp from the
        # matched lead so the row survives NOT NULL + lands in the right
        # tenant under RLS.
        tenant_id=lead.tenant_id,
    )
    if occurred_at is not None:
        ev_kwargs["occurred_at"] = occurred_at  # the event's REAL time, not now()
    db.add(EmailEvent(**ev_kwargs))

    if not apply_side_effects:
        return True

    # A suppressing event (hard bounce / spam / unsubscribe / blocked) doesn't
    # just add the email to the list — it pulls the recipient out of every
    # current campaign (halts sequences + drops not-yet-sent leads) and blocks
    # future ones.  One shared path with the manual ignore button + the
    # blocklist sync.
    suppression_reason = SUPPRESSION_MAP.get(event_type)
    if suppression_reason is not None:
        from app.services import suppression as _suppression  # avoid import cycle

        await _suppression.suppress_email(
            db, lead.email, suppression_reason, tenant_id=lead.tenant_id,
        )

    # Soft bounces are transient on their own, but repeated ones hurt sender
    # reputation — suppress the address once its soft-bounce count reaches
    # SOFT_BOUNCE_SUPPRESS_THRESHOLD (default 1 = suppress on the first; 0
    # disables).  Counted per-address across campaigns; flush first so the row
    # we just added is included.
    elif (
        event_type == EmailEventType.SOFT_BOUNCE
        and settings.SOFT_BOUNCE_SUPPRESS_THRESHOLD > 0
    ):
        await db.flush()
        soft_count = (await db.scalar(
            select(func.count())
            .select_from(EmailEvent)
            .join(Lead, Lead.id == EmailEvent.lead_id)
            .where(
                EmailEvent.event_type == EmailEventType.SOFT_BOUNCE,
                func.lower(Lead.email) == canonical_email(lead.email),
            )
        )) or 0
        if soft_count >= settings.SOFT_BOUNCE_SUPPRESS_THRESHOLD:
            from app.services import suppression as _suppression  # avoid import cycle

            await _suppression.suppress_email(
                db, lead.email, SuppressionReason.SOFT_BOUNCE,
                tenant_id=lead.tenant_id,
            )

    # Circuit breaker: a fresh HARD_BOUNCE/SPAM is the cheapest moment to
    # re-check just this campaign's health (the beat sweep is the backstop).
    # Flush first so the row we just added counts in the window query.
    if (
        event_type in (EmailEventType.HARD_BOUNCE, EmailEventType.SPAM)
        and lead.campaign_id is not None
    ):
        from app.services import deliverability  # local import to avoid cycles

        await db.flush()
        await deliverability.check_and_trip(db, lead.campaign_id)

    return True
