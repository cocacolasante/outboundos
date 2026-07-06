"""ProPublica Nonprofit Explorer API (free, no auth) — by EIN.

  GET https://projects.propublica.org/nonprofits/api/v2/organizations/<ein>.json

Returns the org profile + a list of filings.  We pull a best-effort
officer name (from the most recent filing that exposes one) to seed a
Hunter email-finder lookup.

IMPORTANT (see the feature design): this only covers orgs that have
filed a 990 / 990-EZ / 990-PF — it EXCLUDES the smallest 990-N filers,
so it mostly helps the established USASpending cohort, NOT brand-new
501(c)(3)s.  Treat it as an optional assist, never the primary path.
Never raises — an outage / unknown EIN returns None.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_API = "https://projects.propublica.org/nonprofits/api/v2/organizations/{ein}.json"
_SEARCH_API = "https://projects.propublica.org/nonprofits/api/v2/search.json"
_TIMEOUT = 15.0
_EIN_RE = re.compile(r"\D")


def _normalize_ein(ein: str | None) -> str | None:
    """ProPublica wants the 9-digit EIN with no dash."""
    digits = _EIN_RE.sub("", ein or "")
    return digits or None


def _officer_name(filings: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """Pull a (first, last) officer name from the newest filing that has one."""
    for f in sorted(filings, key=lambda x: x.get("tax_prd_yr") or 0, reverse=True):
        # Filing payloads vary across form types; read defensively.
        full = (
            f.get("officer_name")
            or f.get("formation_officer")
            or f.get("compnsatncurrofcr_name")
            or ""
        ).strip()
        if full:
            parts = full.split()
            if len(parts) >= 2:
                return parts[0], parts[-1]
            return full, None
    return None, None


# ProPublica filing fields carrying "contributions & grants" revenue.  The
# 990 long form exposes ``totcntrbgfts`` (Part VIII line 1h); 990-EZ filings
# vary, so we read a small priority list defensively.
_CONTRIB_FIELDS = ("totcntrbgfts", "totcntrbgftsgrntp", "contrib")
_TOTREV_FIELDS = ("totrevenue", "totrevnue", "totrev")


def _first_num(filing: dict[str, Any], fields: tuple[str, ...]) -> float | None:
    for f in fields:
        v = filing.get(f)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


async def search_orgs(query: str, *, page: int = 0) -> list[dict[str, Any]]:
    """Search ProPublica Nonprofit Explorer (free, no auth) → real orgs with
    real EINs.  Returns ``[{ein, name, ntee_code, state, city}]`` (one page,
    up to 100).  Used to seed the monitored-org set by cause/keyword.  Never
    raises (outage → empty list)."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(_SEARCH_API, params={"q": query, "page": page})
            resp.raise_for_status()
            data = resp.json() or {}
    except Exception as e:  # noqa: BLE001 — best-effort
        logger.warning("ProPublica search failed for %r: %s", query, e)
        return []
    out: list[dict[str, Any]] = []
    for o in data.get("organizations") or []:
        ein = o.get("ein")
        if ein is None:
            continue
        out.append({
            "ein": str(ein),
            "name": (o.get("name") or "").strip() or None,
            "ntee_code": (o.get("ntee_code") or "").strip() or None,
            "state": (o.get("state") or "").strip() or None,
            "city": (o.get("city") or "").strip() or None,
        })
    return out


def org_page_url(ein: str | None) -> str | None:
    """Public ProPublica Nonprofit Explorer page for the EIN (evidence URL)."""
    norm = _normalize_ein(ein)
    return f"https://projects.propublica.org/nonprofits/organizations/{norm}" if norm else None


async def fetch_financials(ein: str | None) -> dict[str, Any] | None:
    """Return the org's identity + year-by-year 990 financials for intent
    scoring, or None on miss/outage (never raises).

    Shape::

        {"name", "website", "ntee_code", "state",
         "filings": [{"year": int, "contributions": float|None,
                      "total_revenue": float|None}, ...]}  # newest first

    Only orgs that have filed a 990/990-EZ/990-PF appear (excludes the
    smallest 990-N filers) — same caveat as ``lookup_org``.
    """
    norm = _normalize_ein(ein)
    if not norm:
        return None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(_API.format(ein=norm))
            resp.raise_for_status()
            data = resp.json() or {}
    except Exception as e:  # noqa: BLE001 — best-effort, never crash a collector
        logger.warning("ProPublica financials lookup failed for EIN %s: %s", norm, e)
        return None

    org = data.get("organization") or {}
    raw_filings = data.get("filings_with_data") or []
    filings: list[dict[str, Any]] = []
    for f in raw_filings:
        year = f.get("tax_prd_yr")
        if not year:
            continue
        filings.append({
            "year": int(year),
            "contributions": _first_num(f, _CONTRIB_FIELDS),
            "total_revenue": _first_num(f, _TOTREV_FIELDS),
        })
    filings.sort(key=lambda x: x["year"], reverse=True)
    return {
        "name": (org.get("name") or "").strip() or None,
        "website": (org.get("website") or "").strip() or None,
        "ntee_code": (org.get("ntee_code") or "").strip() or None,
        "state": (org.get("state") or "").strip() or None,
        "filings": filings,
    }


async def lookup_org(ein: str | None) -> dict[str, Any] | None:
    """Return ``{name, website, first_name, last_name}`` for the EIN, or
    None when nothing usable is found.  Best-effort, never raises."""
    norm = _normalize_ein(ein)
    if not norm:
        return None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(_API.format(ein=norm))
            resp.raise_for_status()
            data = resp.json() or {}
    except Exception as e:  # noqa: BLE001 — optional assist, never crash the feed
        logger.warning("ProPublica lookup failed for EIN %s: %s", norm, e)
        return None

    org = data.get("organization") or {}
    filings = (
        (data.get("filings_with_data") or [])
        + (data.get("filings_without_data") or [])
    )
    first, last = _officer_name(filings)
    website = (org.get("website") or "").strip() or None
    if not (first or website):
        return None
    return {
        "name": (org.get("name") or "").strip() or None,
        "website": website,
        "first_name": first,
        "last_name": last,
    }
