"""USASpending peer-funded collector → Tier-2 ``peer_funded`` signals.

When a nonprofit *peer* in a monitored org's state lands a federal grant, that's
a warm signal for the monitored org: funding is flowing to its geography, the
awarding agency is active, and a comparable org just proved the money is
gettable.  One award fans out to one signal per monitored org in the same state
— ``dedupe_key = usaspending:peer:{award_id}:{org_id}`` — skipping the award's
own recipient (a self-match isn't a "peer").

Honest matching limit (flagged at the gate): USASpending federal-award records
carry the recipient name + state but NO NTEE/cause code, so peer matching is by
**geography (state)** only — the cause dimension comes from the monitored org's
own NTEE (we know our orgs' causes; we can't know the peer's from this feed).
The awarding agency + program is recorded as evidence context, not matched on.
Federal award data also lags, so the signal carries the long Tier-2 window.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import IntentSignalSource, IntentSignalType, Signal
from app.services.funding_sources import usaspending
from app.services.funding_sources.base import DiscoveredOrg
from app.services.intent import matching

logger = logging.getLogger(__name__)

_PEER_BASE_SCORE = 45.0  # moderate — geo-only match, lower confidence than rev_drop
_AWARD_PAGE = "https://www.usaspending.gov/award/{id}"
_SEARCH_PAGE = "https://www.usaspending.gov/search"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _action_dt(detail: dict, now: datetime) -> datetime:
    raw = detail.get("action_date")
    if isinstance(raw, str) and raw.strip():
        try:
            d = datetime.strptime(raw.strip()[:10], "%Y-%m-%d").date()
            return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        except ValueError:
            pass
    return now


def _evidence_url(detail: dict) -> str:
    gen_id = detail.get("generated_internal_id")
    return _AWARD_PAGE.format(id=gen_id) if gen_id else _SEARCH_PAGE


async def _emit_peer_signal(
    session: AsyncSession, org, award: DiscoveredOrg, now: datetime,
) -> str:
    detail = award.detail or {}
    award_id = detail.get("award_id") or award.dedup_key.split(":", 1)[-1]
    amount = detail.get("amount")
    agency = detail.get("agency")
    amt_phrase = ""
    try:
        if amount:
            amt_phrase = f" (${float(amount):,.0f})"
    except (TypeError, ValueError):
        pass
    summary = f"A peer in {org.state}, {award.org_name}, won a federal grant{amt_phrase}"
    if agency:
        summary += f" from {agency}"

    stmt = (
        pg_insert(Signal.__table__)
        .values(
            org_id=org.id, tenant_id=org.tenant_id,
            signal_type=IntentSignalType.PEER_FUNDED.value,
            source=IntentSignalSource.USASPENDING.value,
            score=Decimal(str(_PEER_BASE_SCORE)),
            event_date=_action_dt(detail, now),
            evidence_url=_evidence_url(detail),
            summary=summary[:500],
            raw_payload=detail,
            dedupe_key=f"usaspending:peer:{award_id}:{org.id}",
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "dedupe_key"])
    )
    result = await session.execute(stmt)
    return "new" if result.rowcount else "deduped"


async def collect_usaspending_peer(
    session: AsyncSession, *,
    cause_prefixes: list[str] | None = None,
    geographies: list[str] | None = None,
    now: datetime | None = None,
    lookback_days: int | None = None,
    max_amount: float | None = None,
    max_per_run: int | None = None,
    max_orgs_per_event: int | None = None,
) -> dict[str, int]:
    """Fetch recent nonprofit federal awards and emit a Tier-2 ``peer_funded``
    signal for each monitored org sharing the award's state (skipping the
    award's own recipient).  Idempotent via the dedupe_key."""
    now = now or _now()
    lookback_days = lookback_days if lookback_days is not None else settings.INTENT_USASPENDING_LOOKBACK_DAYS
    max_per_run = max_per_run if max_per_run is not None else settings.INTENT_USASPENDING_PEER_MAX_PER_RUN
    max_orgs_per_event = (max_orgs_per_event if max_orgs_per_event is not None
                          else settings.INTENT_MATCH_MAX_ORGS_PER_EVENT)
    if max_amount is None:
        max_amount = settings.INTENT_USASPENDING_MAX_AWARD_AMOUNT or None

    counts = {"awards": 0, "new": 0, "deduped": 0, "emitted": 0, "self_skipped": 0}
    candidates = await matching.candidate_orgs(
        session, cause_prefixes=cause_prefixes, geographies=geographies)
    if not candidates:
        logger.info("collect_usaspending_peer: no monitored orgs match cause/geo — skipping")
        return counts
    by_state = matching.group_by_state(candidates)

    since = now.date() - timedelta(days=lookback_days)
    awards = await usaspending.fetch_recent_awards(since, now.date(), max_amount=max_amount)

    for award in awards:
        if counts["emitted"] >= max_per_run:
            break
        state = (award.state or "").upper()
        orgs_in_state = by_state.get(state)
        if not orgs_in_state:
            continue
        counts["awards"] += 1
        award_name = matching.normalize_name(award.org_name)
        for org in orgs_in_state[:max_orgs_per_event]:
            if counts["emitted"] >= max_per_run:
                break
            if matching.normalize_name(org.name) == award_name:
                counts["self_skipped"] += 1
                continue
            outcome = await _emit_peer_signal(session, org, award, now)
            counts[outcome] += 1
            counts["emitted"] += 1
        await session.commit()
    logger.info("collect_usaspending_peer: %s", counts)
    return counts
