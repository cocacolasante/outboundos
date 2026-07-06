"""Grants.gov new-RFP collector → Tier-1 ``new_rfp`` signals.

A newly-posted federal funding opportunity matching the ICP's cause keywords is
a Tier-1 "act now" event for every monitored org in that cause: there is a live
RFP with a deadline they could pursue (and that GrantMind could help them win).
One opportunity fans out to one signal per matching monitored org —
``dedupe_key = grantsgov:rfp:{opportunity_id}:{org_id}`` — so the same RFP never
re-emits for the same org, while still reaching every org it's relevant to.

Matching: the keyword search constrains cause; candidate orgs are the monitored
orgs in the ICP cause (NTEE prefix) + geo.  Federal RFPs are generally
nationally eligible, so we don't geo-filter the RFP itself — the ICP
geographies already bound which orgs we monitor.

Never invents opportunity facts: the summary quotes the real title + agency,
and ``evidence_url`` links the Grants.gov detail page.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import IntentSignalSource, IntentSignalType, Signal
from app.services.funding_sources import grants_gov
from app.services.intent import matching

logger = logging.getLogger(__name__)

# Intrinsic strength of a fresh, deadline-bearing federal RFP (Tier 1).
_RFP_BASE_SCORE = 70.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event_dt(opp: dict, now: datetime) -> datetime:
    d = opp.get("open_date")
    if d is None:
        return now
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _is_new_and_open(opp: dict, now: datetime, lookback_days: int) -> bool:
    """Recently posted AND not already closed — i.e. still actionable."""
    open_d = opp.get("open_date")
    close_d = opp.get("close_date")
    if open_d is not None:
        age = (now.date() - open_d).days
        if age < 0 or age > lookback_days:
            return False
    if close_d is not None and close_d < now.date():
        return False
    return True


async def _emit_rfp_signal(session: AsyncSession, org, opp: dict, now: datetime) -> str:
    title = opp.get("title") or opp.get("number") or "a federal grant opportunity"
    agency = opp.get("agency")
    summary = f'New RFP "{title}"'
    if agency:
        summary += f" from {agency}"
    summary += " — matches your cause area"
    if opp.get("close_date"):
        summary += f" (closes {opp['close_date'].isoformat()})"

    stmt = (
        pg_insert(Signal.__table__)
        .values(
            org_id=org.id, tenant_id=org.tenant_id,
            signal_type=IntentSignalType.NEW_RFP.value,
            source=IntentSignalSource.GRANTS_GOV.value,
            score=Decimal(str(_RFP_BASE_SCORE)),
            event_date=_event_dt(opp, now),
            evidence_url=opp["evidence_url"],
            summary=summary[:500],
            raw_payload={k: (v.isoformat() if hasattr(v, "isoformat") else v)
                         for k, v in opp.items()},
            dedupe_key=f"grantsgov:rfp:{opp['id']}:{org.id}",
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "dedupe_key"])
    )
    result = await session.execute(stmt)
    return "new" if result.rowcount else "deduped"


async def collect_grants_gov(
    session: AsyncSession, *,
    keywords: list[str],
    cause_prefixes: list[str] | None = None,
    geographies: list[str] | None = None,
    now: datetime | None = None,
    lookback_days: int | None = None,
    max_per_run: int | None = None,
    max_orgs_per_event: int | None = None,
) -> dict[str, int]:
    """Search Grants.gov for the ICP cause keywords, match each new+open
    opportunity to monitored orgs in cause+geo, and emit a Tier-1 ``new_rfp``
    signal per (opportunity, org).  Idempotent via the dedupe_key."""
    now = now or _now()
    lookback_days = lookback_days if lookback_days is not None else settings.INTENT_RFP_LOOKBACK_DAYS
    max_per_run = max_per_run if max_per_run is not None else settings.INTENT_GRANTS_GOV_MAX_PER_RUN
    max_orgs_per_event = (max_orgs_per_event if max_orgs_per_event is not None
                          else settings.INTENT_MATCH_MAX_ORGS_PER_EVENT)

    counts = {"opportunities": 0, "new": 0, "deduped": 0, "emitted": 0}
    candidates = await matching.candidate_orgs(
        session, cause_prefixes=cause_prefixes, geographies=geographies)
    if not candidates:
        logger.info("collect_grants_gov: no monitored orgs match cause/geo — skipping")
        return counts

    seen_opp_ids: set[str] = set()
    for keyword in keywords:
        if counts["emitted"] >= max_per_run:
            break
        opps = await grants_gov.search_opportunities(keyword)
        for opp in opps:
            if opp["id"] in seen_opp_ids or not _is_new_and_open(opp, now, lookback_days):
                continue
            seen_opp_ids.add(opp["id"])
            counts["opportunities"] += 1
            for org in candidates[:max_orgs_per_event]:
                if counts["emitted"] >= max_per_run:
                    break
                outcome = await _emit_rfp_signal(session, org, opp, now)
                counts[outcome] += 1
                counts["emitted"] += 1
            await session.commit()
    logger.info("collect_grants_gov: %s", counts)
    return counts
