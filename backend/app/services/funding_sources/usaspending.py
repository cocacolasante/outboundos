"""USAspending grant-award feed (free, no auth).

POST https://api.usaspending.gov/api/v2/search/spending_by_award/
Verified against the v2 API contract (fedspendingtransparency/
usaspending-api, contracts/v2/search/spending_by_award.md):

  filters.award_type_codes   — '02','03','04','05' = block/formula/
                               project grants + cooperative agreements.
  filters.time_period        — [{start_date, end_date, date_type}],
                               date_type 'action_date'.
  filters.recipient_type_names — the recipient business-category roll-up;
                               'nonprofit' restricts to nonprofit recipients.
  fields                     — display-name strings; response rows are
                               keyed by those names plus internal ids.
  page_metadata.hasNext      — pagination flag.

Field/filter names re-verify against the live contract if the feed
ever returns empty unexpectedly — the parse is defensive (.get on
everything) so an extra/renamed field is a no-op, never a crash.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

import httpx

from app.services.funding_sources.base import DiscoveredOrg

logger = logging.getLogger(__name__)

USASPENDING_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
# Block / formula / project grants + cooperative agreements.
GRANT_AWARD_TYPE_CODES = ["02", "03", "04", "05"]
_FIELDS = [
    "Award ID",
    "Recipient Name",
    "Award Amount",
    "Awarding Agency",
    "Start Date",
    "Award Type",
    # USASpending quirk: the display name "Recipient Location State Code"
    # comes back null, but requesting the raw snake_case key returns the
    # populated value — and that's the key ``_map_award`` reads first.
    # Without requesting it the response omits state (all None).
    "recipient_location_state_code",
]
_TIMEOUT = 30.0
_DEFAULT_PAGE_LIMIT = 100
_MAX_PAGES = 20


def _amount_phrase(amount: Any) -> str:
    try:
        return f" (${float(amount):,.0f})" if amount else ""
    except (TypeError, ValueError):
        return ""


def _map_award(row: dict[str, Any]) -> DiscoveredOrg | None:
    name = (row.get("Recipient Name") or "").strip()
    award_id = (
        row.get("Award ID")
        or row.get("generated_internal_id")
        or row.get("internal_id")
    )
    if not name or not award_id:
        return None
    award_id = str(award_id)
    amount = row.get("Award Amount")
    agency = row.get("Awarding Agency")
    action_date = row.get("Start Date") or row.get("Action Date")
    # State display names vary across award groups; read defensively.
    recipient_state = (
        row.get("recipient_location_state_code")
        or row.get("Place of Performance State Code")
        or row.get("Recipient Location State Code")
    )
    summary = f"{name} won a federal grant{_amount_phrase(amount)}"
    if agency:
        summary += f" from {agency}"
    return DiscoveredOrg(
        signal_type="grant_awarded",
        summary=summary[:300],
        dedup_key=f"grant_awarded:{award_id}",
        org_name=name,
        state=recipient_state,
        detail={
            "amount": amount,
            "agency": agency,
            "award_id": award_id,
            "action_date": action_date,
            "recipient_state": recipient_state,
            # USASpending always returns this id; powers the award-profile
            # evidence URL (https://www.usaspending.gov/award/<id>).
            "generated_internal_id": row.get("generated_internal_id"),
        },
    )


async def fetch_recent_awards(
    since: date,
    until: date,
    *,
    limit: int = _DEFAULT_PAGE_LIMIT,
    max_pages: int = _MAX_PAGES,
    min_amount: float | None = None,
    max_amount: float | None = None,
) -> list[DiscoveredOrg]:
    """Recent nonprofit grant awards with ``action_date`` in [since, until].

    ``min_amount`` / ``max_amount`` bound the award size via the API's
    ``award_amounts`` filter — e.g. ``max_amount=500_000`` skips the
    multi-million grants that go to large, already-well-funded nonprofits
    and surfaces the smaller orgs that are a better outreach fit.

    Never raises — a feed outage logs and returns whatever was gathered
    so the beat task can't be crashed by USAspending being down.
    """
    filters: dict[str, Any] = {
        "award_type_codes": GRANT_AWARD_TYPE_CODES,
        "time_period": [{
            "start_date": since.isoformat(),
            "end_date": until.isoformat(),
            "date_type": "action_date",
        }],
        # Restrict to nonprofit recipients (business-category roll-up).
        "recipient_type_names": ["nonprofit"],
    }
    if min_amount is not None or max_amount is not None:
        # USAspending wants one {lower_bound, upper_bound} object; either
        # bound may be omitted for an open-ended range.
        rng: dict[str, float] = {}
        if min_amount is not None:
            rng["lower_bound"] = float(min_amount)
        if max_amount is not None:
            rng["upper_bound"] = float(max_amount)
        filters["award_amounts"] = [rng]

    body: dict[str, Any] = {
        "filters": filters,
        "fields": _FIELDS,
        # Biggest-first within the (capped) range: the largest grants UNDER
        # the cap are substantial, real-funding mid-size orgs — the best
        # outreach targets — and $0 deobligation/admin records sink to the
        # bottom, out of a tight per-run window.
        "limit": limit,
        "sort": "Award Amount",
        "order": "desc",
    }

    orgs: list[DiscoveredOrg] = []
    seen_keys: set[str] = set()
    page = 1
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        while page <= max_pages:
            body["page"] = page
            try:
                resp = await client.post(USASPENDING_URL, json=body)
                resp.raise_for_status()
                data = resp.json() or {}
            except Exception as exc:  # noqa: BLE001 — feed outage must not crash beat
                logger.warning("USAspending fetch failed (page %s): %s", page, exc)
                break
            results = data.get("results") or []
            for row in results:
                org = _map_award(row)
                if org is None or org.dedup_key in seen_keys:
                    continue
                seen_keys.add(org.dedup_key)
                orgs.append(org)
            if not (data.get("page_metadata") or {}).get("hasNext"):
                break
            page += 1
    return orgs
