"""ATS public-board dev-role collector → Tier-1 dev_role_posted (Phase 6, Track 3).

For monitored orgs known to host their careers page on Greenhouse / Lever /
Ashby, poll the public board API directly (free, no auth) and emit a Tier-1
``dev_role_posted`` for each open development/grants role.

The org's ATS board is stored on ``Org.raw['ats'] = {'provider', 'token'}``.
Token DISCOVERY is intentionally out of scope here (manual / a future helper) —
this collector runs only on orgs already configured.  Coverage is low for small
nonprofits (most don't use these ATSs), which is why this is the free bonus
track, not the primary one.

``dedupe_key = ats:{provider}:{job_id}:{org_id}``.  Idempotent; quotes the real
posted title; ``evidence_url`` is the live posting.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import IntentSignalSource, IntentSignalType, Org, Signal
from app.services.funding_sources import ats_boards
from app.services.intent.collect_dev_roles import title_matches_role

logger = logging.getLogger(__name__)

_DEV_ROLE_BASE_SCORE = 80.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def set_ats_board(org: Org, provider: str, token: str) -> None:
    """Attach an ATS board config to an org (manual token discovery)."""
    raw = dict(org.raw or {})
    raw["ats"] = {"provider": provider, "token": token}
    org.raw = raw


def _ats_config(org: Org) -> tuple[str, str] | None:
    ats = (org.raw or {}).get("ats") if org.raw else None
    if not isinstance(ats, dict):
        return None
    provider = (ats.get("provider") or "").lower().strip()
    token = (ats.get("token") or "").strip()
    if provider in ats_boards.PROVIDERS and token:
        return provider, token
    return None


async def _emit(session: AsyncSession, org: Org, provider: str, job: dict, now: datetime) -> str:
    title = job.get("title") or "a development/grants role"
    summary = f'{org.name} posted a "{title}" role — actively investing in grant-seeking'
    stmt = (
        pg_insert(Signal.__table__)
        .values(
            org_id=org.id, tenant_id=org.tenant_id,
            signal_type=IntentSignalType.DEV_ROLE_POSTED.value,
            source=IntentSignalSource.JOBS.value,
            score=Decimal(str(_DEV_ROLE_BASE_SCORE)),
            event_date=job.get("posted_at") or now,
            evidence_url=job.get("url") or f"https://{provider}.example",
            summary=summary[:500],
            raw_payload={"provider": provider, **{k: (v.isoformat() if hasattr(v, "isoformat") else v)
                                                  for k, v in job.items()}},
            dedupe_key=f"ats:{provider}:{job['id']}:{org.id}",
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "dedupe_key"])
    )
    result = await session.execute(stmt)
    return "new" if result.rowcount else "deduped"


async def collect_ats_dev_roles(
    session: AsyncSession, *, now: datetime | None = None, max_per_run: int | None = None,
) -> dict[str, int]:
    """Poll every ATS-configured monitored org's public board, emit a Tier-1
    dev_role_posted for each open dev/grants role.  Idempotent."""
    now = now or _now()
    max_per_run = max_per_run if max_per_run is not None else settings.INTENT_ATS_MAX_PER_RUN
    counts = {"orgs": 0, "postings": 0, "matched": 0, "new": 0, "deduped": 0}

    orgs = (await session.execute(select(Org).where(Org.raw.isnot(None)))).scalars().all()
    for org in orgs:
        cfg = _ats_config(org)
        if cfg is None:
            continue
        provider, token = cfg
        counts["orgs"] += 1
        jobs = await ats_boards.fetch_jobs(provider, token)
        for job in jobs:
            counts["postings"] += 1
            if not title_matches_role(job.get("title")):
                continue
            counts["matched"] += 1
            if counts["new"] + counts["deduped"] >= max_per_run:
                break
            counts[await _emit(session, org, provider, job, now)] += 1
        await session.commit()
    logger.info("collect_ats_dev_roles: %s", counts)
    return counts
