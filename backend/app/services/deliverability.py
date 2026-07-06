"""Bounce/spam circuit breaker + deliverability stats.

A campaign whose hard-bounce or spam rate crosses the configured
threshold over the rolling window is auto-PAUSED with a reason and an
owner alert.  Unlike the LinkedIn cap auto-pause (``auto_paused_until``,
auto-resumes at midnight), a breaker pause NEVER auto-resumes — a human
must hit Resume, which clears ``auto_paused_at`` / ``auto_pause_reason``.

Two trigger paths share ``evaluate_campaign_health``:
  1. Inline in ``brevo_events.process_event`` right after a
     HARD_BOUNCE/SPAM event persists (cheap, single-campaign re-check).
  2. The ``deliverability.sweep_health`` beat task (every ~15 min) over
     all RUNNING campaigns as a backstop for missed inline checks.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    Campaign,
    CampaignStatus,
    EmailEvent,
    EmailEventType,
    NotificationKind,
)

logger = logging.getLogger(__name__)


@dataclass
class HealthVerdict:
    trip: bool
    sample: int
    bounce_rate: float
    spam_rate: float
    reason: str = ""


# Events that count as a "send outcome" for the sample denominator.
_SAMPLE_TYPES = (
    EmailEventType.DELIVERED,
    EmailEventType.HARD_BOUNCE,
    EmailEventType.SOFT_BOUNCE,
    EmailEventType.SPAM,
)


async def evaluate_campaign_health(
    session: AsyncSession, campaign_id,
) -> HealthVerdict:
    """Hard-bounce + spam rates over the breaker window.  ``trip`` is True
    only when the sample is big enough AND a rate crosses its threshold."""
    cutoff = datetime.now(timezone.utc) - timedelta(
        hours=settings.CIRCUIT_BREAKER_WINDOW_HOURS
    )
    rows = (await session.execute(
        select(EmailEvent.event_type, func.count())
        .where(
            EmailEvent.campaign_id == campaign_id,
            EmailEvent.event_type.in_(_SAMPLE_TYPES),
            EmailEvent.occurred_at >= cutoff,
        )
        .group_by(EmailEvent.event_type)
    )).all()
    counts = {etype: n for etype, n in rows}
    sample = sum(counts.values())
    bounces = counts.get(EmailEventType.HARD_BOUNCE, 0)
    spam = counts.get(EmailEventType.SPAM, 0)
    bounce_rate = bounces / sample if sample else 0.0
    spam_rate = spam / sample if sample else 0.0

    if sample < settings.CIRCUIT_BREAKER_MIN_SAMPLE:
        return HealthVerdict(False, sample, bounce_rate, spam_rate)

    if bounce_rate >= settings.CIRCUIT_BREAKER_BOUNCE_RATE:
        return HealthVerdict(
            True, sample, bounce_rate, spam_rate,
            reason=(
                f"Hard-bounce rate {bounce_rate:.1%} over the last "
                f"{settings.CIRCUIT_BREAKER_WINDOW_HOURS}h "
                f"({bounces}/{sample} sends) crossed the "
                f"{settings.CIRCUIT_BREAKER_BOUNCE_RATE:.0%} threshold"
            ),
        )
    if spam_rate >= settings.CIRCUIT_BREAKER_SPAM_RATE:
        return HealthVerdict(
            True, sample, bounce_rate, spam_rate,
            reason=(
                f"Spam-complaint rate {spam_rate:.2%} over the last "
                f"{settings.CIRCUIT_BREAKER_WINDOW_HOURS}h "
                f"({spam}/{sample} sends) crossed the "
                f"{settings.CIRCUIT_BREAKER_SPAM_RATE:.1%} threshold"
            ),
        )
    return HealthVerdict(False, sample, bounce_rate, spam_rate)


async def trip_breaker(
    session: AsyncSession, campaign: Campaign, verdict: HealthVerdict,
) -> bool:
    """Pause the campaign + alert the owner.  Idempotent: an already
    breaker-paused campaign (``auto_paused_at`` set) is left alone.
    Returns True when this call actually tripped it."""
    if campaign.auto_paused_at is not None:
        return False
    if campaign.status == CampaignStatus.PAUSED:
        # Manually paused — record the reason but don't fight the human.
        return False

    now = datetime.now(timezone.utc)
    campaign.status = CampaignStatus.PAUSED
    campaign.auto_paused_at = now
    campaign.auto_pause_reason = verdict.reason
    # A breaker pause must never be auto-resumed by the sequencer's
    # LinkedIn-cap path.
    campaign.auto_paused_until = None
    logger.warning(
        "circuit breaker tripped for campaign %s: %s", campaign.id, verdict.reason,
    )

    # Owner alert — reuse the agent notification machinery (persisted bell
    # row + email).  Best-effort: an alert failure must not undo the pause.
    try:
        from app.services import agent_core, notifications

        agent_settings = await agent_core.get_agent_settings(session)
        await notifications.notify(
            session,
            agent_settings,
            kind=NotificationKind.CAMPAIGN_AUTO_PAUSED,
            title=f"Campaign auto-paused: {campaign.name}",
            body=(
                f"{verdict.reason}.\n\nSending stopped to protect the "
                "domain's reputation. Review the lead list / copy, then "
                "Resume the campaign manually."
            ),
            dedup_key=f"campaign_auto_paused:{campaign.id}:{now:%Y-%m-%d}",
            # Explicit stamp: this path is also reached from the tenant-
            # blind brevo events poller (batch commit — the mixin default
            # can't see a per-event ContextVar at flush time).
            tenant_id=campaign.tenant_id,
        )
    except Exception:  # noqa: BLE001 — the pause itself is the critical action
        logger.exception("breaker notification failed for %s", campaign.id)
    return True


async def check_and_trip(session: AsyncSession, campaign_id) -> bool:
    """Evaluate one campaign and trip if unhealthy.  Used by both the
    inline event hook and the beat sweep.  No-ops unless the breaker is
    enabled and the campaign is RUNNING."""
    if not settings.CIRCUIT_BREAKER_ENABLED:
        return False
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None or campaign.status != CampaignStatus.RUNNING:
        return False
    verdict = await evaluate_campaign_health(session, campaign_id)
    if not verdict.trip:
        return False
    return await trip_breaker(session, campaign, verdict)


async def deliverability_stats(session: AsyncSession, campaign_id) -> dict:
    """Window rates + counts for the campaign-detail deliverability strip."""
    cutoff = datetime.now(timezone.utc) - timedelta(
        hours=settings.CIRCUIT_BREAKER_WINDOW_HOURS
    )
    rows = (await session.execute(
        select(EmailEvent.event_type, func.count())
        .where(
            EmailEvent.campaign_id == campaign_id,
            EmailEvent.occurred_at >= cutoff,
        )
        .group_by(EmailEvent.event_type)
    )).all()
    counts = {etype.value: n for etype, n in rows}
    sample = sum(counts.get(t.value, 0) for t in _SAMPLE_TYPES)
    delivered = counts.get("delivered", 0)
    return {
        "window_hours": settings.CIRCUIT_BREAKER_WINDOW_HOURS,
        "sample": sample,
        "delivered": delivered,
        "hard_bounces": counts.get("hard_bounce", 0),
        "soft_bounces": counts.get("soft_bounce", 0),
        "spam": counts.get("spam", 0),
        "opens": counts.get("opened", 0),
        "bounce_rate": (counts.get("hard_bounce", 0) / sample) if sample else 0.0,
        "spam_rate": (counts.get("spam", 0) / sample) if sample else 0.0,
        "open_rate": (counts.get("opened", 0) / delivered) if delivered else 0.0,
        "breaker": {
            "enabled": settings.CIRCUIT_BREAKER_ENABLED,
            "min_sample": settings.CIRCUIT_BREAKER_MIN_SAMPLE,
            "bounce_threshold": settings.CIRCUIT_BREAKER_BOUNCE_RATE,
            "spam_threshold": settings.CIRCUIT_BREAKER_SPAM_RATE,
        },
    }
