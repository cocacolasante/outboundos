"""Adzuna dev-role collector → Tier-1 ``dev_role_posted`` signals (Phase 6, Track 1).

A posted Development Director / Grant Writer / Foundation Relations role at a
monitored nonprofit is the strongest Tier-1 "act now" signal for the GrantMind
ICP.  We search Adzuna for those role titles and match each posting's employer
to a monitored org by name (high-precision; the org-match IS the nonprofit
filter — only monitored nonprofits produce signals).

``dedupe_key = adzuna:devrole:{posting_id}:{org_id}``.  Idempotent, never
invents facts (summary quotes the real title + employer; evidence_url is the
posting).  No Adzuna key → the client returns [] and this no-ops.

Honest limit (Track 1): matching is by employer NAME, so partial recall (name
variants we don't key on are missed) and the usual aggregator coverage gap —
logged via the new/deduped/unmatched counts.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import IntentSignalSource, IntentSignalType, Signal
from app.services.funding_sources import adzuna
from app.services.intent import matching

logger = logging.getLogger(__name__)

# The strongest Tier-1 signal for this ICP → a high intrinsic strength.
_DEV_ROLE_BASE_SCORE = 80.0

# Role titles that signal active grant-seeking investment.  Searched as exact
# phrases; the posting title is also post-filtered to contain one of them.
DEV_ROLE_TITLES = (
    "development director",
    "director of development",
    "grant writer",
    "grants manager",
    "grants coordinator",
    "foundation relations",
    "director of philanthropy",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event_dt(job: dict, now: datetime) -> datetime:
    return job.get("created") or now


def title_matches_role(title: str | None) -> bool:
    """True if a job title names a development/grants role (shared with the
    ATS collector)."""
    t = (title or "").lower()
    return any(role in t for role in DEV_ROLE_TITLES)


# Backwards-compatible private alias (used within this module).
_title_matches_role = title_matches_role


async def _emit_dev_role_signal(session: AsyncSession, org, job: dict, now: datetime) -> str:
    title = job.get("title") or "a development/grants role"
    summary = f'{org.name} posted a "{title}" role — actively investing in grant-seeking'
    stmt = (
        pg_insert(Signal.__table__)
        .values(
            org_id=org.id, tenant_id=org.tenant_id,
            signal_type=IntentSignalType.DEV_ROLE_POSTED.value,
            source=IntentSignalSource.JOBS.value,
            score=Decimal(str(_DEV_ROLE_BASE_SCORE)),
            event_date=_event_dt(job, now),
            evidence_url=job.get("redirect_url") or "https://www.adzuna.com",
            summary=summary[:500],
            raw_payload={k: (v.isoformat() if hasattr(v, "isoformat") else v)
                         for k, v in job.items()},
            dedupe_key=f"adzuna:devrole:{job['id']}:{org.id}",
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "dedupe_key"])
    )
    result = await session.execute(stmt)
    return "new" if result.rowcount else "deduped"


async def collect_dev_roles(
    session: AsyncSession, *,
    cause_prefixes: list[str] | None = None,
    geographies: list[str] | None = None,
    now: datetime | None = None,
    lookback_days: int | None = None,
    max_per_run: int | None = None,
    titles: tuple[str, ...] = DEV_ROLE_TITLES,
) -> dict[str, int]:
    """Search Adzuna for each role title, match the employer to a monitored org
    by name, and emit a Tier-1 ``dev_role_posted`` signal per matched posting.
    Idempotent via the dedupe_key."""
    now = now or _now()
    lookback_days = lookback_days if lookback_days is not None else settings.INTENT_DEV_ROLE_LOOKBACK_DAYS
    max_per_run = max_per_run if max_per_run is not None else settings.INTENT_DEV_ROLE_MAX_PER_RUN

    counts = {"postings": 0, "matched": 0, "unmatched": 0, "new": 0, "deduped": 0}
    candidates = await matching.candidate_orgs(
        session, cause_prefixes=cause_prefixes, geographies=geographies)
    if not candidates:
        logger.info("collect_dev_roles: no monitored orgs — skipping")
        return counts
    index = matching.index_orgs_by_name(candidates)

    seen_postings: set[str] = set()
    for title in titles:
        if counts["new"] + counts["deduped"] >= max_per_run:
            break
        jobs = await adzuna.search_jobs(title, max_days_old=lookback_days)
        for job in jobs:
            if job["id"] in seen_postings or not _title_matches_role(job.get("title")):
                continue
            seen_postings.add(job["id"])
            counts["postings"] += 1
            org = matching.match_employer(job.get("company"), index)
            if org is None:
                counts["unmatched"] += 1
                continue
            counts["matched"] += 1
            if counts["new"] + counts["deduped"] >= max_per_run:
                break
            outcome = await _emit_dev_role_signal(session, org, job, now)
            counts[outcome] += 1
        await session.commit()
    logger.info("collect_dev_roles: %s", counts)
    return counts
