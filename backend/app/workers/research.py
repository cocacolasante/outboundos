"""Per-lead research worker.

The async core function does all the IO and DB writes. The Celery wrapper
calls it via asyncio.run, retries transient failures with exponential
backoff, and on the final failure marks the lead's research_status as
FAILED so the UI can surface it.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.models import (
    Campaign,
    ComposeStatus,
    Lead,
    ResearchMode,
    ResearchStatus,
    SendStatus,
    Suppression,
    canonical_email,
)
from app.services import apollo, hunter, research_cache, web_research
from app.services.campaign_stop import stop_requested
from app.workers.celery_app import celery_app
from app.tenancy.context import with_record_tenant
from app.workers.compose import compose_lead

try:  # pragma: no cover — import-time only
    import redis as _redis_sync
except Exception:  # noqa: BLE001
    _redis_sync = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Free-mail providers must NEVER be treated as a shared "company" — two leads
# at gmail.com are not the same org, so caching company research by these would
# cross-contaminate.  Lists keyed on a corporate domain are fine.
_FREE_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "outlook.com",
    "hotmail.com", "live.com", "msn.com", "aol.com", "icloud.com", "me.com",
    "proton.me", "protonmail.com", "gmx.com", "mail.com",
}


def _company_domain(company_website: str, email: str) -> str:
    """Best-effort company domain for the company-level research cache.
    Prefers the explicit company website; falls back to the email's domain
    unless it's a free-mail provider.  Empty string => no company caching."""
    site = (company_website or "").strip()
    if site:
        if "://" not in site:
            site = "http://" + site
        host = (urlparse(site).netloc or "").split("@")[-1].split(":")[0].lower()
        if host.startswith("www."):
            host = host[4:]
        if host:
            return host
    e = (email or "").strip().lower()
    if "@" in e:
        dom = e.rsplit("@", 1)[-1]
        if dom and dom not in _FREE_MAIL_DOMAINS:
            return dom
    return ""


def _assess_quality(
    web_data: dict[str, Any],
    apollo_data: dict[str, Any],
) -> str:
    """3-tier quality signal driving the compose worker's prompt selection.

    ``web_data`` is the merged person+company research (person_news,
    company_description, recent_updates, industry, ...).
    """
    has_person = bool(web_data.get("person_news"))
    has_company = (
        bool(web_data.get("company_description"))
        or bool(web_data.get("company_news"))
        or bool(web_data.get("recent_updates"))
    )
    has_enrichment = bool(apollo_data)

    signals = sum([has_person, has_company, has_enrichment])
    if signals == 0:
        return "low"
    if signals >= 2:
        return "rich"
    return "partial"


def _safe(result: Any, default: Any) -> Any:
    """Convert an asyncio.gather exception into a safe default."""
    return default if isinstance(result, BaseException) else result


async def research_lead_async(lead_id: str) -> dict[str, Any]:
    lid = uuid.UUID(str(lead_id))
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    enqueued = False

    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            lead = await session.get(Lead, lid)
            if lead is None:
                logger.warning("research_lead: lead %s not found", lead_id)
                return {"status": "not_found"}

            campaign = await session.get(Campaign, lead.campaign_id)
            if campaign is None:
                logger.warning("research_lead: campaign for lead %s missing", lead_id)
                return {"status": "not_found"}

            # Pre-scrub: never spend research (or compose) tokens on an address
            # that can't be emailed.  A suppressed lead — hard bounce / spam /
            # unsubscribe / manual ignore — is blocked at the send gate anyway,
            # so the web-search + Sonnet compose spend is pure waste.  Mark it
            # terminal (research + compose DONE, send SUPPRESSED) so the
            # progress counters complete and the pacer skips it, with ZERO
            # Anthropic calls.  This is the cheapest lead to "process".
            canonical = canonical_email(lead.email or "")
            if canonical:
                suppressed = await session.scalar(
                    select(Suppression.email).where(Suppression.email == canonical)
                )
                if suppressed is not None:
                    lead.research_status = ResearchStatus.DONE
                    lead.compose_status = ComposeStatus.DONE
                    lead.send_status = SendStatus.SUPPRESSED
                    lead.research_data = {
                        "quality": "low", "skipped": True, "suppressed": True,
                    }
                    await session.commit()
                    return {"status": "skipped_suppressed"}

            # Cooperative stop check — covers the window where the kill
            # races a redelivery: if the user hit Stop, the campaign's
            # ``campaign:stop:<id>`` Redis key is set and we bail BEFORE
            # the first Anthropic / Apollo / Hunter call.  Cheap probe.
            if _redis_sync is not None:
                try:
                    r = _redis_sync.Redis.from_url(celery_app.conf.broker_url)
                    if stop_requested(r, lead.campaign_id):
                        lead.research_status = ResearchStatus.PENDING
                        await session.commit()
                        return {"status": "stopped", "reason": "campaign_stopped"}
                except Exception:  # noqa: BLE001
                    pass  # flag check is best-effort; never block research on Redis

            lead.research_status = ResearchStatus.RUNNING
            await session.commit()

            # Snapshot the inputs while session is open — we'll use them
            # outside the transaction window for the parallel research calls.
            first_name = lead.first_name or ""
            last_name = lead.last_name or ""
            company = lead.company or ""
            company_website = lead.company_website or ""
            job_title = lead.job_title or ""
            email = lead.email
            mode = campaign.research_mode

        cache_hit = False
        domain = _company_domain(company_website, email)
        cached_company: dict[str, Any] | None = None
        if mode in (ResearchMode.NONE, ResearchMode.TEMPLATE):
            # "No research" / "template" modes: make zero external research
            # calls (no Apollo / Hunter / web).  Hand an empty, low-quality
            # payload to compose, which then uses its generic name+company
            # prompt (NONE) or renders the campaign template (TEMPLATE).
            quality = "low"
            research_data: dict[str, Any] = {"quality": "low", "skipped": True}
        else:
            # Cross-campaign cache: a fresh research_data for this email skips
            # the (expensive) web-search / Apollo / Hunter calls entirely.  See
            # ``RESEARCH_CACHE_TTL_DAYS``.
            cached = None
            async with AsyncSession(engine) as session:
                cached = await research_cache.lookup(session, email)
                # On an email-cache miss, see if a prior lead at the same
                # company already researched it — lets the person search skip
                # the (redundant) company half.
                if not cached and domain:
                    cached_company = await research_cache.company_lookup(session, domain)
            if cached:
                quality = cached.get("quality", "low")
                # Stamp a marker so the audit log makes the cache hit obvious.
                research_data = {**cached, "from_cache": True}
                cache_hit = True

        if mode not in (ResearchMode.NONE, ResearchMode.TEMPLATE) and not cache_hit:
            # ONE web-search call covers person + company (was two: web_research
            # + site_scraper).  Halves the per-lead web-search spend; the merged
            # call returns company_description / recent_updates / industry too.
            tasks: list[asyncio.Future[Any]] = [
                asyncio.ensure_future(
                    web_research.research_person_web(
                        first_name, last_name, company, job_title, company_website,
                        cached_company=cached_company,
                    )
                ),
                asyncio.ensure_future(hunter.verify_email_hunter(email)),
            ]
            run_apollo = mode == ResearchMode.DEEP and bool(settings.APOLLO_API_KEY)
            if run_apollo:
                tasks.append(
                    asyncio.ensure_future(
                        apollo.enrich_lead_apollo(email, first_name, last_name, company)
                    )
                )

            results = await asyncio.gather(*tasks, return_exceptions=True)
            web_data = _safe(results[0], {})
            hunter_data = _safe(results[1], {"deliverable": True, "score": 0})
            apollo_data: dict[str, Any] = _safe(results[2], {}) if run_apollo else {}

            quality = _assess_quality(web_data, apollo_data)
            research_data = {
                "quality": quality,
                "person_news": web_data.get("person_news", []),
                "company_news": web_data.get("company_news", []),
                "company_description": web_data.get("company_description") or "",
                "recent_updates": web_data.get("recent_updates", []),
                "industry": web_data.get("industry") or apollo_data.get("company_industry") or "",
                "linkedin_headline": apollo_data.get("linkedin_headline") or "",
                "job_title": apollo_data.get("job_title") or job_title,
                "seniority": apollo_data.get("seniority") or "",
                "email_deliverable": bool(hunter_data.get("deliverable", True)),
            }

        async with AsyncSession(engine, expire_on_commit=False) as session:
            lead = await session.get(Lead, lid)
            if lead is None:
                logger.warning("research_lead: lead %s vanished mid-flight", lead_id)
                return {"status": "not_found"}
            # Preserve retarget context (the email/link a lead engaged with) so
            # the research step never clobbers it — compose reads it to draft
            # the follow-up.
            existing_ctx = (lead.research_data or {}).get("retarget_context")
            if existing_ctx and "retarget_context" not in research_data:
                research_data = {**research_data, "retarget_context": existing_ctx}
            lead.research_data = research_data
            lead.research_status = ResearchStatus.DONE
            # Warm the cross-campaign cache so the next campaign with this
            # email reuses this research instead of re-spending tokens.  Skip
            # cache hits (no new data) and the NONE/TEMPLATE no-signal payload.
            if (
                not cache_hit
                and mode not in (ResearchMode.NONE, ResearchMode.TEMPLATE)
                and research_data
            ):
                await research_cache.upsert(session, email, research_data)
                # Warm the company-level cache so the next lead at this domain
                # can skip the company half of the search.
                await research_cache.company_upsert(
                    session, domain, research_cache.company_fields(research_data)
                )
            await session.commit()
    finally:
        await engine.dispose()

    # Enqueue compose outside the DB scope so a Celery broker hiccup doesn't
    # roll back the research write.
    compose_lead.delay(str(lid))
    enqueued = True
    return {"status": "done", "quality": quality, "compose_enqueued": enqueued}


async def _mark_lead_failed(lead_id: str) -> None:
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine) as session:
            lead = await session.get(Lead, uuid.UUID(lead_id))
            if lead is not None:
                lead.research_status = ResearchStatus.FAILED
                await session.commit()
    finally:
        await engine.dispose()


@celery_app.task(bind=True, name="research.research_lead", max_retries=2)
def research_lead(self, lead_id: str) -> dict[str, Any]:  # noqa: D401
    try:
        return asyncio.run(with_record_tenant(Lead, lead_id, research_lead_async, lead_id))
    except Exception as exc:  # noqa: BLE001
        logger.exception("research_lead failed for %s", lead_id)
        try:
            raise self.retry(exc=exc, countdown=60 * (2**self.request.retries))
        except self.MaxRetriesExceededError:
            asyncio.run(with_record_tenant(Lead, lead_id, _mark_lead_failed, lead_id))
            return {"status": "failed", "error": str(exc)}
