"""ICP lookalike workers (Feature D).

  icp.refresh_profile — daily: regenerate the auto ICP from closed-won
                        deals (skips the LLM below MIN_WON_DEALS).
  icp.discover        — daily, an hour after refresh: find + stage new
                        lookalike candidates, notify the owner with a
                        count.  Candidates are review-queue rows only;
                        accepting one (a human action) creates the lead.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.models import (
    IcpProfileStatus,
    LookalikeCandidate,
    NotificationKind,
)
from app.services import agent_core, icp_builder, lookalike_discovery, notifications
from app.workers.celery_app import celery_app
from app.tenancy.context import for_all_tenants

logger = logging.getLogger(__name__)


async def _refresh_profile_async() -> dict[str, Any]:
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            profile = await icp_builder.build_icp_from_won(session)
            await session.commit()
            result = {
                "status": profile.status.value,
                "won_deals": profile.won_deal_count,
            }
    finally:
        await engine.dispose()
    logger.info("icp.refresh_profile: %s", result)
    return result


@celery_app.task(name="icp.refresh_profile")
def refresh_profile() -> dict[str, Any]:
    return asyncio.run(for_all_tenants(_refresh_profile_async))


async def discover_session(session: AsyncSession) -> dict[str, Any]:
    """Find + persist new candidates for the auto profile.  Caller owns
    the transaction."""
    profile = await icp_builder._get_auto_profile(session)
    if profile is None or profile.status != IcpProfileStatus.READY:
        return {"skipped": "no_ready_profile"}

    candidates = await lookalike_discovery.find_lookalikes(session, profile)
    for c in candidates:
        session.add(LookalikeCandidate(
            icp_profile_id=profile.id,
            company=c.company,
            company_website=c.company_website,
            contact_name=c.contact_name,
            job_title=c.job_title,
            linkedin_url=c.linkedin_url,
            email=(c.email or "").lower() or None,
            fit_score=c.fit_score,
            fit_reason=c.fit_reason,
            source=c.source,
            dedup_key=lookalike_discovery.candidate_dedup_key(c),
        ))
    await session.flush()

    if candidates:
        agent_settings = await agent_core.get_agent_settings(session)
        top = max(candidates, key=lambda c: c.fit_score)
        await notifications.notify(
            session,
            agent_settings,
            kind=NotificationKind.LOOKALIKE_BATCH,
            title=f"{len(candidates)} new lookalike candidate(s) staged",
            body=(
                f"Top fit: {top.company} ({top.fit_score}/100 — "
                f"{top.fit_reason}). Review them on the Lookalikes page."
            ),
            # One batch alert per profile per day.
            dedup_key=f"lookalike_batch:{profile.id}:{profile.refreshed_at:%Y-%m-%d}"
            if profile.refreshed_at else f"lookalike_batch:{profile.id}",
        )
    return {"staged": len(candidates)}


async def _discover_async() -> dict[str, Any]:
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            result = await discover_session(session)
            await session.commit()
    finally:
        await engine.dispose()
    logger.info("icp.discover: %s", result)
    return result


@celery_app.task(name="icp.discover")
def discover() -> dict[str, Any]:
    return asyncio.run(for_all_tenants(_discover_async))
