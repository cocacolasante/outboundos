"""Public ATS job-board APIs (free, no auth) — Greenhouse / Lever / Ashby.

Many orgs host their careers page on one of these applicant-tracking systems,
each of which exposes a PUBLIC, embed-intended JSON endpoint (verified live
2026-06):

  Greenhouse  GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs
  Lever       GET https://api.lever.co/v0/postings/{company}?mode=json
  Ashby       GET https://api.ashbyhq.com/posting-api/job-board/{token}

All three are normalized to ``{id, title, url, posted_at}``.  Defensive parses,
never raise (an outage / unknown token → []).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 20.0
PROVIDERS = ("greenhouse", "lever", "ashby")

_GREENHOUSE = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
_LEVER = "https://api.lever.co/v0/postings/{token}?mode=json"
_ASHBY = "https://api.ashbyhq.com/posting-api/job-board/{token}"


def _iso(s: Any) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _epoch_ms(v: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(v) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


async def _get(url: str) -> Any:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:  # noqa: BLE001 — outage / unknown token must not crash beat
        logger.warning("ATS fetch failed (%s): %s", url, exc)
        return None


def _norm(job_id: Any, title: Any, url: Any, posted: datetime | None) -> dict[str, Any] | None:
    if job_id is None:
        return None
    t = (title or "").strip()
    return {"id": str(job_id), "title": t or None, "url": (url or None), "posted_at": posted}


async def fetch_jobs(provider: str, token: str) -> list[dict[str, Any]]:
    """Normalized open postings for an org's ATS board.  ``[]`` on outage /
    unknown token / unknown provider."""
    if not token:
        return []
    out: list[dict[str, Any]] = []
    if provider == "greenhouse":
        data = await _get(_GREENHOUSE.format(token=token))
        for j in (data or {}).get("jobs", []) or []:
            row = _norm(j.get("id"), j.get("title"), j.get("absolute_url"), _iso(j.get("updated_at")))
            if row:
                out.append(row)
    elif provider == "lever":
        data = await _get(_LEVER.format(token=token))
        for j in data or []:
            row = _norm(j.get("id"), j.get("text"), j.get("hostedUrl"), _epoch_ms(j.get("createdAt")))
            if row:
                out.append(row)
    elif provider == "ashby":
        data = await _get(_ASHBY.format(token=token))
        for j in (data or {}).get("jobs", []) or []:
            row = _norm(j.get("id"), j.get("title"), j.get("jobUrl") or j.get("applyUrl"),
                        _iso(j.get("publishedAt")))
            if row:
                out.append(row)
    else:
        logger.warning("unknown ATS provider: %r", provider)
    return out
