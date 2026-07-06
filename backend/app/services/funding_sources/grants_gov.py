"""Grants.gov opportunity feed (free, no auth).

  POST https://api.grants.gov/v1/api/search2
  body: {"keyword": "...", "oppStatuses": "posted", "rows": N, "startRecordNum": K}

Verified against the live API (2026-06).  Response shape:

  {"data": {"hitCount": int,
            "oppHits": [{"id", "number", "title", "agencyCode", "agency",
                         "openDate", "closeDate", "oppStatus"}, ...]}}

Dates are ``MM/DD/YYYY`` strings (sometimes empty).  Every parse is defensive
(.get on everything) so a renamed/extra field is a no-op, never a crash, and a
feed outage logs + returns whatever was gathered (never raises) so a Beat task
can't be crashed by Grants.gov being down.
"""
from __future__ import annotations

import html
import logging
from datetime import date, datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GRANTS_GOV_URL = "https://api.grants.gov/v1/api/search2"
OPPORTUNITY_DETAIL_URL = "https://www.grants.gov/search-results-detail/{id}"
_TIMEOUT = 30.0
_DEFAULT_ROWS = 100
_MAX_PAGES = 5


def _parse_date(s: Any) -> date | None:
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y").date()
    except ValueError:
        return None


def _map_opportunity(row: dict[str, Any]) -> dict[str, Any] | None:
    opp_id = row.get("id") or row.get("number")
    if not opp_id:
        return None
    # Grants.gov returns HTML-encoded titles/agencies (e.g. "&ndash;").
    title = html.unescape((row.get("title") or "").strip())
    agency = html.unescape((row.get("agency") or row.get("agencyName") or "").strip())
    return {
        "id": str(opp_id),
        "number": row.get("number"),
        "title": title or None,
        "agency": agency or None,
        "open_date": _parse_date(row.get("openDate")),
        "close_date": _parse_date(row.get("closeDate")),
        "status": (row.get("oppStatus") or "").strip().lower() or None,
        "evidence_url": OPPORTUNITY_DETAIL_URL.format(id=opp_id),
    }


async def search_opportunities(
    keyword: str, *,
    statuses: str = "posted",
    rows: int = _DEFAULT_ROWS,
    max_pages: int = _MAX_PAGES,
) -> list[dict[str, Any]]:
    """Federal funding opportunities matching ``keyword``.  ``statuses`` is the
    pipe-joined Grants.gov status filter (``posted`` = currently open).  Returns
    normalized opportunity dicts (deduped by id).  Never raises."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    start = 0
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for _ in range(max_pages):
            body = {
                "keyword": keyword,
                "oppStatuses": statuses,
                "rows": rows,
                "startRecordNum": start,
            }
            try:
                resp = await client.post(GRANTS_GOV_URL, json=body)
                resp.raise_for_status()
                data = (resp.json() or {}).get("data") or {}
            except Exception as exc:  # noqa: BLE001 — feed outage must not crash beat
                logger.warning("Grants.gov search failed (%r, start %s): %s", keyword, start, exc)
                break
            hits = data.get("oppHits") or []
            if not hits:
                break
            for row in hits:
                opp = _map_opportunity(row)
                if opp is None or opp["id"] in seen:
                    continue
                seen.add(opp["id"])
                out.append(opp)
            if start + rows >= (data.get("hitCount") or 0):
                break
            start += rows
    return out
