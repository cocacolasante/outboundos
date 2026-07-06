"""Collector: ProPublica 990 grant-revenue delta → Tier-2 ``rev_drop`` signal.

For each monitored org (has an EIN), pull its year-by-year 990 financials,
compare the latest vs prior "contributions & grants" revenue, and emit a
``rev_drop`` Signal on a material year-over-year drop.  The same call also
refreshes the org's NTEE / state / website / revenue + size band.

Idempotent: ``dedupe_key = propublica:rev_drop:{ein}:{latest_filing_year}`` is
unique, so re-runs never double-emit.  Honest caveat: 990 data lags filing by
1–2 years, so this is a *slow* signal — `event_date` is the fiscal-year end, and
Phase-3 decay should treat it with a long half-life (flagged for that gate).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import IntentSignalSource, IntentSignalType, Org, Signal
from app.services.funding_sources import propublica
from app.services.intent.orgs import normalize_ein, size_band_for_revenue

logger = logging.getLogger(__name__)


def compute_rev_drop(filings: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Latest vs prior contributions/grants revenue.  Returns the drop detail
    when the YoY decline meets the threshold, else None.

    ``filings`` is newest-first ``[{year, contributions, total_revenue}]``.
    """
    usable = [f for f in filings if f.get("contributions") is not None]
    if len(usable) < 2:
        return None
    latest, prior = usable[0], usable[1]
    lc = float(latest["contributions"])
    pc = float(prior["contributions"])
    if pc <= 0:
        return None
    drop = (pc - lc) / pc  # >0 means revenue fell
    if drop < settings.INTENT_REV_DROP_THRESHOLD:
        return None
    return {
        "latest_year": latest["year"], "prior_year": prior["year"],
        "latest": lc, "prior": pc, "drop_fraction": round(drop, 4),
    }


def _drop_score(drop_fraction: float) -> Decimal:
    """Intrinsic strength scales with the drop magnitude (Tier-2 band).
    ~20% drop → ~59, ~50% → ~75, capped at 80."""
    return Decimal(str(round(min(80.0, 45.0 + drop_fraction * 70.0), 2)))


async def collect_for_org(session: AsyncSession, org: Org) -> str:
    """Fetch financials, refresh the org, emit a rev_drop signal if warranted.
    Returns one of: new | deduped | no_signal | no_data | error.  Caller commits.
    """
    norm = normalize_ein(org.ein)
    if not norm:
        return "no_data"
    try:
        fin = await propublica.fetch_financials(norm)
    except Exception:  # noqa: BLE001 — never crash the run on one org
        logger.exception("propublica fetch_financials crashed for org=%s", org.id)
        return "error"
    if fin is None:
        return "no_data"

    # Refresh org identity/financials from the same call (blanks + revenue).
    org.ntee_code = org.ntee_code or fin.get("ntee_code")
    org.state = org.state or fin.get("state")
    org.website = org.website or fin.get("website")
    if fin.get("name") and (not org.name or org.name == "Unknown org"):
        org.name = fin["name"]
    latest_rev = next((f.get("total_revenue") for f in fin["filings"] if f.get("total_revenue") is not None), None)
    if latest_rev is not None:
        org.annual_revenue = Decimal(str(latest_rev))
        org.size_band = size_band_for_revenue(latest_rev)

    delta = compute_rev_drop(fin["filings"])
    if not delta:
        return "no_signal"

    dedupe_key = f"propublica:rev_drop:{norm}:{delta['latest_year']}"
    if await session.scalar(select(Signal.id).where(Signal.dedupe_key == dedupe_key)):
        return "deduped"

    pct = round(delta["drop_fraction"] * 100)
    summary = (
        f"Contributions & grants revenue fell {pct}% "
        f"(${delta['prior']:,.0f} → ${delta['latest']:,.0f}) on the "
        f"FY{delta['latest_year']} 990."
    )
    session.add(Signal(
        org_id=org.id,
        tenant_id=org.tenant_id,
        signal_type=IntentSignalType.REV_DROP,
        source=IntentSignalSource.PROPUBLICA,
        score=_drop_score(delta["drop_fraction"]),
        # 990 fiscal-year end as the event date (data lags — see module note).
        event_date=datetime(delta["latest_year"], 12, 31, tzinfo=timezone.utc),
        evidence_url=propublica.org_page_url(norm),
        summary=summary,
        raw_payload=delta,
        dedupe_key=dedupe_key,
    ))
    return "new"


async def collect_propublica_rev_delta(session: AsyncSession) -> dict[str, int]:
    """Run the collector across monitored orgs (those with an EIN).  Per-run
    cap + per-org commit so one bad org can't roll back the batch.  Observability:
    returns counts of new / deduped / no_signal / no_data / error / scanned."""
    import asyncio

    counts = {"scanned": 0, "new": 0, "deduped": 0, "no_signal": 0, "no_data": 0, "error": 0}
    orgs = (await session.execute(
        select(Org).where(Org.ein.isnot(None)).limit(settings.INTENT_PROPUBLICA_MAX_PER_RUN)
    )).scalars().all()

    for org in orgs:
        counts["scanned"] += 1
        try:
            outcome = await collect_for_org(session, org)
            await session.commit()
        except Exception:  # noqa: BLE001 — isolate per-org failures
            await session.rollback()
            logger.exception("propublica collector failed for org=%s", org.id)
            counts["error"] += 1
            continue
        counts[outcome] = counts.get(outcome, 0) + 1
        if settings.INTENT_COLLECTOR_POLITENESS_SECONDS > 0:
            await asyncio.sleep(settings.INTENT_COLLECTOR_POLITENESS_SECONDS)

    logger.info("collect_propublica_rev_delta: %s", counts)
    return counts
