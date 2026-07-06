"""Adzuna job-aggregator API (official REST, free tier with attribution).

  GET https://api.adzuna.com/v1/api/jobs/{country}/search/{page}
      ?app_id=...&app_key=...&what_phrase=...&max_days_old=...&results_per_page=...

Adzuna aggregates job postings across many boards.  We use it to source the
Tier-1 ``dev_role_posted`` intent signal — a posted Development Director /
Grant Writer / Foundation Relations role at a monitored nonprofit.

No key (``ADZUNA_APP_ID`` / ``ADZUNA_APP_KEY``) → returns [] so the feature is
opt-in (same pattern as Hunter).  Never raises — a feed outage logs + returns
whatever was gathered so a Beat task can't be crashed by Adzuna being down.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_BASE = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
_TIMEOUT = 30.0
_DEFAULT_PER_PAGE = 50
_MAX_PAGES = 5


def _parse_created(s: Any) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    try:
        # Adzuna returns e.g. "2026-06-10T14:03:22Z".
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _map_job(row: dict[str, Any]) -> dict[str, Any] | None:
    job_id = row.get("id")
    if not job_id:
        return None
    company = ((row.get("company") or {}).get("display_name") or "").strip()
    location = (row.get("location") or {})
    return {
        "id": str(job_id),
        "title": (row.get("title") or "").strip() or None,
        "company": company or None,
        "location": (location.get("display_name") or "").strip() or None,
        "area": location.get("area") or [],   # ["US","California",...] hierarchy
        "created": _parse_created(row.get("created")),
        "redirect_url": (row.get("redirect_url") or "").strip() or None,
    }


async def search_jobs(
    what_phrase: str, *,
    country: str | None = None,
    max_days_old: int = 30,
    results_per_page: int = _DEFAULT_PER_PAGE,
    max_pages: int = _MAX_PAGES,
) -> list[dict[str, Any]]:
    """Postings whose text matches the exact phrase ``what_phrase`` within the
    last ``max_days_old`` days.  Returns normalized job dicts (deduped by id).
    Returns [] when no API key is configured.  Never raises."""
    if not (settings.ADZUNA_APP_ID and settings.ADZUNA_APP_KEY):
        return []
    country = country or settings.ADZUNA_COUNTRY
    params = {
        "app_id": settings.ADZUNA_APP_ID,
        "app_key": settings.ADZUNA_APP_KEY,
        "what_phrase": what_phrase,
        "max_days_old": max_days_old,
        "results_per_page": results_per_page,
        "content-type": "application/json",
    }
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for page in range(1, max_pages + 1):
            url = _BASE.format(country=country, page=page)
            try:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json() or {}
            except Exception as exc:  # noqa: BLE001 — feed outage must not crash beat
                logger.warning("Adzuna search failed (%r, page %s): %s", what_phrase, page, exc)
                break
            results = data.get("results") or []
            if not results:
                break
            for row in results:
                job = _map_job(row)
                if job is None or job["id"] in seen:
                    continue
                seen.add(job["id"])
                out.append(job)
            if len(results) < results_per_page:
                break
    return out
