"""Periodic IMAP reply polling.

A single Celery beat task fans out per connected account, calling the IMAP
client to fetch recent messages (read-only, no Seen-flag mutation) and
matching them to outbound leads.  Accounts whose last credential test
failed are skipped so we don't hammer broken inboxes.

Dedup model: we store the last ``MAX_PROCESSED_IDS`` Message-IDs we've
turned into REPLIED events on the account row
(``processed_imap_message_ids``).  A message that's already in the set
is skipped on subsequent polls without re-creating a REPLIED row.  This
replaces the older "mark as Seen" dedup which side-effected the user's
mailbox unread state.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.models import (
    AgentActionStatus,
    AgentActionType,
    Campaign,
    CampaignStatus,
    ConnectedAccount,
    ConnectedAccountTestStatus,
    EmailEvent,
    EmailEventType,
)
from app.services import agent_core, imap_client, reply_sentiment
from app.workers.celery_app import celery_app
from app.tenancy.context import for_all_tenants

logger = logging.getLogger(__name__)

# When an account has never been polled, search back this far on the first run.
INITIAL_LOOKBACK_DAYS = 7
# 1-hour grace window absorbs clock skew + a poll that lands a few
# minutes after a delivery — without this, a message that arrived at
# 09:59:50 but wasn't indexed in the SINCE search until 10:00:01 could
# slip past the SINCE filter.
SINCE_GRACE = timedelta(hours=1)
# Bound on processed_imap_message_ids growth.  500 covers ~50 polls
# worth of replies at 10 replies/poll which is plenty for our cadence
# (every 20 minutes).
MAX_PROCESSED_IDS = 500


async def _run_agent_for_reply(session: AsyncSession, lead, msg: dict) -> None:
    """Classify one inbound reply and hand it to the agent core."""
    campaign_goal = None
    if lead.campaign_id is not None:
        campaign = await session.get(Campaign, lead.campaign_id)
        campaign_goal = getattr(campaign, "goal", None)
    classification = await reply_sentiment.classify_reply(
        subject=msg.get("subject", ""),
        body_text=msg.get("body_text", ""),
        lead_context={
            "name": " ".join(x for x in [lead.first_name, lead.last_name] if x),
            "email": lead.email,
            "company": lead.company,
            "job_title": lead.job_title,
            "campaign_goal": campaign_goal,
        },
    )
    await agent_core.process_inbound_reply(session, lead, dict(msg), classification)


async def poll_account_for_replies(
    account: ConnectedAccount,
    campaign_ids: list[uuid.UUID],
    session: AsyncSession,
) -> dict[str, Any]:
    """Pull new replies for one account and link them to leads.

    Updates ``account.last_polled_at`` and ``account.processed_imap_message_ids``
    on success.  On IMAP/auth failure, marks the account's
    ``last_test_status=FAILED`` so the beat task skips it next cycle.
    Caller is responsible for committing the session.
    """
    raw_since = account.last_polled_at or (
        datetime.now(timezone.utc) - timedelta(days=INITIAL_LOOKBACK_DAYS)
    )
    since = raw_since - SINCE_GRACE
    processed_ids: list[str] = list(account.processed_imap_message_ids or [])
    processed_set: set[str] = set(processed_ids)

    try:
        # Decryption + IMAP call live in imap_client; no plaintext touches
        # this function. Raises on decrypt failure or any IMAP error.
        messages = await asyncio.to_thread(
            imap_client.fetch_recent_with_account, account, since, processed_set,
        )
    except Exception as e:  # noqa: BLE001 — auth, network, ssl, decrypt
        account.last_test_status = ConnectedAccountTestStatus.FAILED
        account.last_test_error = str(e)
        logger.warning(
            "IMAP poll failed for %s: %s", account.email_address, e
        )
        return {"ok": False, "error": str(e), "replies_found": 0}

    replies_found = 0
    newly_processed: list[str] = []
    for msg in messages:
        mid = msg.get("message_id") or ""
        # Mark any fetched message as "seen by us" so it doesn't reprocess
        # even if no lead matched — otherwise unmatched-but-recent messages
        # would keep getting re-parsed every poll cycle.
        if mid:
            newly_processed.append(mid)
        lead = await imap_client.match_message_to_lead(session, msg, campaign_ids)
        if lead is None:
            continue
        session.add(EmailEvent(
            lead_id=lead.id,
            campaign_id=lead.campaign_id,
            event_type=EmailEventType.REPLIED,
            event_data={
                "from": msg["from_email"],
                "subject": msg["subject"],
                "in_reply_to": msg["in_reply_to"],
                "message_id": mid,
                "uid": msg["uid"],
            },
        ))
        replies_found += 1

        # ---- agent pipeline (classify → log → remind → notify) ----
        # Runs ONLY for newly-processed messages: the fetcher already
        # skipped anything in processed_imap_message_ids, so a re-poll
        # can never re-classify (and never double-spends Anthropic).
        # Best-effort: any agent failure logs an audit row and moves on
        # — reply recording must never be blocked by the agent.
        if settings.AGENT_ENABLED:
            try:
                await _run_agent_for_reply(session, lead, msg)
            except Exception as exc:  # noqa: BLE001 — agent must never break polling
                logger.exception("agent reply pipeline failed for %s", mid)
                try:
                    agent_core.record_agent_action(
                        session,
                        action_type=AgentActionType.CLASSIFY_REPLY,
                        status=AgentActionStatus.FAILED,
                        summary=f"agent pipeline crashed: {exc}",
                        lead_id=lead.id,
                    )
                except Exception:  # noqa: BLE001
                    pass

    if newly_processed:
        # Append new IDs, dedup, trim to last MAX_PROCESSED_IDS preserving
        # insertion order (newest at the tail).
        seen: set[str] = set()
        merged: list[str] = []
        for x in [*processed_ids, *newly_processed]:
            if x and x not in seen:
                seen.add(x)
                merged.append(x)
        account.processed_imap_message_ids = merged[-MAX_PROCESSED_IDS:]
    account.last_polled_at = datetime.now(timezone.utc)
    return {"ok": True, "replies_found": replies_found}


async def poll_all_replies_async() -> dict[str, Any]:
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    total_replies = 0
    per_account: list[dict[str, Any]] = []

    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            accounts = list((await session.execute(
                select(ConnectedAccount).where(
                    ConnectedAccount.last_test_status != ConnectedAccountTestStatus.FAILED
                )
            )).scalars().all())

            for account in accounts:
                campaign_ids = list((await session.execute(
                    select(Campaign.id).where(
                        Campaign.connected_account_id == account.id,
                        Campaign.status.in_([
                            CampaignStatus.RUNNING,
                            CampaignStatus.PAUSED,
                            CampaignStatus.COMPLETE,
                        ]),
                    )
                )).scalars().all())

                if not campaign_ids:
                    per_account.append({
                        "account_id": str(account.id),
                        "skipped": "no_active_campaigns",
                    })
                    continue

                result = await poll_account_for_replies(account, campaign_ids, session)
                try:
                    await session.commit()
                except Exception:  # noqa: BLE001
                    await session.rollback()
                    raise

                if result.get("ok"):
                    total_replies += int(result.get("replies_found", 0))
                per_account.append({
                    "account_id": str(account.id),
                    "email": account.email_address,
                    **result,
                })
    finally:
        await engine.dispose()

    logger.info(
        "poll_all_replies finished: %d accounts checked, %d replies found",
        len(per_account), total_replies,
    )
    return {"accounts_checked": len(per_account), "replies_found": total_replies, "details": per_account}


@celery_app.task(name="reply_poller.poll_all_replies")
def poll_all_replies() -> dict[str, Any]:
    return asyncio.run(for_all_tenants(poll_all_replies_async))
