"""Periodic LinkedIn inbox poller.

Runs every ``LINKEDIN_POLL_INTERVAL_MINUTES`` (default 5). For each OK
LinkedIn account, asks the provider for events newer than
``account.last_polled_at`` (or 1 hour ago for first-time polls) and
translates them into lead-state updates:

  - message_received    → Lead.linkedin_last_reply_at
  - connection_accepted → Lead.linkedin_connection_status = 'connected'

Lead matching is by ``lead.linkedin_url`` slug (public_id). Leads whose
URL doesn't match the inbound sender are ignored — most LinkedIn DMs we
get aren't from leads we're tracking.

The poller never raises into the Celery worker — provider errors flip
``account.last_error`` and move on.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.models import (
    Campaign,
    Lead,
    LinkedInAccount,
    LinkedInAccountStatus,
    LinkedInConnectionStatus,
)
from app.services.linkedin import ambient_provider
from app.services.linkedin.base import (
    AccountRestricted,
    ChallengeRequired,
    InboundEvent,
)
from app.workers.celery_app import celery_app
from app.tenancy.context import for_all_tenants

logger = logging.getLogger(__name__)


def _slug_from_url(url: str | None) -> str | None:
    if not url:
        return None
    s = url.rstrip("/").split("/")[-1].split("?")[0]
    return s.lower() or None


async def _poll_account(session: AsyncSession, account: LinkedInAccount) -> dict[str, int]:
    """Pull recent events for one account; apply them to leads.

    Returns a per-event-kind counter for logging.
    """
    counts = {"messages_matched": 0, "messages_total": 0, "connections": 0}
    provider = await ambient_provider()
    since = account.last_polled_at or datetime.now(timezone.utc) - timedelta(hours=1)

    try:
        events: list[InboundEvent] = await provider.inbox_recent_events(account, since)
    except ChallengeRequired as exc:
        account.status = LinkedInAccountStatus.CHALLENGED
        account.last_error = str(exc)
        account.pending_challenge_url = exc.challenge_url or "https://www.linkedin.com"
        return counts
    except AccountRestricted as exc:
        account.status = LinkedInAccountStatus.RESTRICTED
        account.last_error = str(exc)
        return counts
    except Exception as exc:  # noqa: BLE001
        logger.warning("LinkedIn poll failed for account %s: %s", account.id, exc)
        account.last_error = str(exc)
        return counts

    account.last_polled_at = datetime.now(timezone.utc)
    account.last_error = None

    if not events:
        return counts

    # Build a slug index of leads for the campaigns owned by THIS account.
    lead_rows = (await session.execute(
        select(Lead)
        .join(Lead.campaign)
        .where(Lead.campaign.has(linkedin_account_id=account.id))
    )).scalars().all()
    by_slug: dict[str, Lead] = {}
    for l in lead_rows:
        slug = _slug_from_url(l.linkedin_url)
        if slug:
            by_slug[slug] = l

    for ev in events:
        if ev.kind == "message_received":
            counts["messages_total"] += 1
            slug = (ev.from_public_id or "").lower() or None
            lead = by_slug.get(slug) if slug else None
            if lead is None:
                continue
            counts["messages_matched"] += 1
            lead.linkedin_last_reply_at = ev.occurred_at
            # First DM is a strong signal they accepted the connect at some
            # point; promote status if we don't already have it.
            if lead.linkedin_connection_status != LinkedInConnectionStatus.CONNECTED:
                lead.linkedin_connection_status = LinkedInConnectionStatus.CONNECTED
        elif ev.kind == "connection_accepted":
            counts["connections"] += 1
            slug = (ev.from_public_id or "").lower() or None
            lead = by_slug.get(slug) if slug else None
            if lead is not None:
                lead.linkedin_connection_status = LinkedInConnectionStatus.CONNECTED

    return counts


async def _account_has_work(session: AsyncSession, account_id) -> bool:
    """Is there any reason this account's inbox needs polling RIGHT NOW?

    We only care about inbound events from leads we're tracking. A lead is
    only relevant if it's either:
      - INVITED  — waiting on the prospect to accept our connect request
      - CONNECTED — waiting on a reply to a DM we sent / could send

    If every lead in this account's campaigns is in UNKNOWN/DECLINED/WITHDRAWN
    state, polling produces zero useful information and just burns Unipile
    API calls.
    """
    count = await session.scalar(
        select(func.count())
        .select_from(Lead)
        .join(Campaign, Campaign.id == Lead.campaign_id)
        .where(
            Campaign.linkedin_account_id == account_id,
            Lead.linkedin_connection_status.in_([
                LinkedInConnectionStatus.INVITED,
                LinkedInConnectionStatus.CONNECTED,
            ]),
        )
    )
    return bool(count and count > 0)


async def _poll_all_async() -> dict[str, int]:
    totals = {
        "accounts": 0, "skipped_no_work": 0,
        "messages_matched": 0, "messages_total": 0, "connections": 0,
    }
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            accounts = (await session.execute(
                select(LinkedInAccount).where(
                    LinkedInAccount.status == LinkedInAccountStatus.OK
                )
            )).scalars().all()
            for acc in accounts:
                if not await _account_has_work(session, acc.id):
                    # Nothing inbound could possibly be relevant — skip the
                    # Unipile poll entirely. Bump last_polled_at so we
                    # don't poll-spam on the next tick either.
                    acc.last_polled_at = datetime.now(timezone.utc)
                    totals["skipped_no_work"] += 1
                    continue
                totals["accounts"] += 1
                c = await _poll_account(session, acc)
                totals["messages_matched"] += c["messages_matched"]
                totals["messages_total"] += c["messages_total"]
                totals["connections"] += c["connections"]
            await session.commit()
    finally:
        await engine.dispose()
    return totals


@celery_app.task(name="linkedin_poller.poll_all")
def poll_all() -> dict[str, int]:
    return asyncio.run(for_all_tenants(_poll_all_async))
