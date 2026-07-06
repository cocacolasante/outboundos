"""Endpoint for the one-off 'research a client' tool.

Synchronous flow (the user is waiting in the browser):

1. Parse the LinkedIn URL → derive a slug + name guess.
2. Single Anthropic call with web_search to extract identity +
   personalization signals (mode controls depth).
3. Single Anthropic call to compose the outreach message with a strict
   per-message character limit.

No DB writes — research and compose are returned to the user to copy
into LinkedIn / email manually.  Adding a saved-history table would be
a separate feature; out of scope here.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from anthropic import APIError, APIStatusError, AuthenticationError
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import (
    ConnectedAccount,
    CrmActivity,
    CrmActivityDirection,
    CrmActivityType,
    Lead,
    Opportunity,
)
from sqlalchemy import func as sa_func
from app.schemas.research_client import (
    ResearchClientRequest,
    ResearchClientResponse,
    ResearchedProfile,
    SendClientEmailRequest,
    SendClientEmailResponse,
)
from app.services import brevo, compose_client, outreach, research_cache, research_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/research-client", tags=["research-client"])


@router.post("", response_model=ResearchClientResponse)
async def research_client_endpoint(
    req: ResearchClientRequest,
    db: AsyncSession = Depends(get_db),
) -> ResearchClientResponse:
    started = time.perf_counter()

    # Cache key: ``linkedin:<slug>`` reuses the existing research_cache
    # table (the bulk pipeline uses bare emails; this is namespaced so
    # there's no collision risk).  Deep mode bypasses the cache — the
    # user explicitly asked for fresh, deep research.  Cache TTL is
    # ``RESEARCH_CACHE_TTL_DAYS`` (90 days by default).
    slug, _name_guess = research_client.parse_linkedin_url(req.linkedin_url)
    if not slug:
        raise HTTPException(
            status_code=400,
            detail="URL must be a LinkedIn profile URL like https://www.linkedin.com/in/<slug>/",
        )
    cache_key = f"linkedin:{slug}"
    is_deep = (req.research_mode or "").lower() == "deep"

    research: dict[str, Any] | None = None
    cache_hit = False
    if not is_deep:
        cached = await research_cache.lookup(db, cache_key)
        if cached:
            # ``from_cache`` is informational — surfaces in the UI as a
            # tiny hint so the user knows why the call returned instantly.
            cached["from_cache"] = True
            research = cached
            cache_hit = True

    if research is None:
        research = await research_client.research_from_linkedin_url(
            req.linkedin_url, req.research_mode,
        )
        if research.get("error") == "url_not_recognised_as_linkedin_profile":
            raise HTTPException(
                status_code=400,
                detail="URL must be a LinkedIn profile URL like https://www.linkedin.com/in/<slug>/",
            )
        # Only cache successful research — a ``found=False`` row would
        # poison subsequent calls (they'd hit the cache and miss out on
        # a retry that might find the prospect once they appear online).
        if research.get("found"):
            try:
                await research_cache.upsert(db, cache_key, research)
                await db.commit()
            except Exception:  # noqa: BLE001
                # Cache is a perf optimisation; don't fail the user
                # call if the write hiccups.  Roll back so the session
                # isn't left in PendingRollback for anything that
                # touches ``db`` later in this request.
                await db.rollback()

    try:
        composed = await compose_client.compose_for_client(
            output_kind=req.output_kind,
            goal=req.goal, tone=req.tone, sender_name=req.sender_name,
            char_limit=req.char_limit, research=research,
        )
    except ValueError as exc:
        logger.warning("research_client compose failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"compose_failed: {exc}") from exc
    except AuthenticationError as exc:
        # Bad/missing ANTHROPIC_API_KEY in .env.  Surface a clear 502
        # so the frontend toast tells the user what's wrong instead of
        # showing a generic 500.  Same shape applies to research stage,
        # but that one already swallows the error and returns empty
        # research; the failure mode here is unique because compose
        # MUST produce a body to be useful.
        logger.error("Anthropic auth failed in research_client: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=(
                "Anthropic API key is invalid or expired.  Rotate the key "
                "at console.anthropic.com, update ANTHROPIC_API_KEY in .env, "
                "and run: docker compose up -d --force-recreate backend "
                "worker beat"
            ),
        ) from exc
    except (APIError, APIStatusError) as exc:
        # Network / rate-limit / 5xx from Anthropic.  Same idea: clean
        # 502 with the upstream status so we don't masquerade as a
        # server bug.
        logger.warning("Anthropic API error in research_client: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"anthropic_api_error: {exc}",
        ) from exc

    duration_ms = int((time.perf_counter() - started) * 1000)
    return ResearchClientResponse(
        profile=ResearchedProfile(
            first_name=research.get("first_name", ""),
            last_name=research.get("last_name", ""),
            headline=research.get("headline", ""),
            company=research.get("company", ""),
            company_website=research.get("company_website", ""),
            job_title=research.get("job_title", ""),
            industry=research.get("industry", ""),
            found=bool(research.get("found", False)),
            quality=research.get("quality", "low"),
        ),
        research=research,
        subject=composed["subject"],
        body=composed["body"],
        char_count=len(composed["body"]),
        duration_ms=duration_ms,
    )


@router.post("/send", response_model=SendClientEmailResponse)
async def send_client_email(
    req: SendClientEmailRequest,
    db: AsyncSession = Depends(get_db),
) -> SendClientEmailResponse:
    """One-off transactional send out of the 'Research a client' tool.

    Goes via Brevo's transactional API — no campaign / lead row backs
    this send.  Synthetic header IDs (``X-Campaign-ID: research-client``
    plus a UUID per send) let Brevo's event log distinguish these from
    campaign sends without inventing a real Campaign.

    The sender's from-address falls back to ``settings.BREVO_SENDER_EMAIL``
    when the request doesn't override it.  ``settings.BREVO_API_KEY``
    must be configured — same prereq as the bulk pipeline.  All
    Anthropic failures are remapped to 502 with the upstream message
    so the frontend toast surfaces something actionable instead of a
    generic 500.
    """
    # Send + CRM-track via the shared core (also used by signal outreach).
    try:
        result = await outreach.send_and_track(
            db,
            to_email=str(req.to_email),
            to_name=req.to_name,
            subject=req.subject,
            body=req.body,
            sender_name=req.sender_name,
            sender_email=req.sender_email,
            campaign_tag="research-client",
        )
    except outreach.OutreachSendError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    return SendClientEmailResponse(
        message_id=result.message_id,
        sent_at=datetime.now(timezone.utc),
        to_email=req.to_email,
        crm_lead_id=result.crm_lead_id,
        crm_lead_created=result.crm_lead_created,
        crm_activity_logged=result.crm_activity_logged,
    )
