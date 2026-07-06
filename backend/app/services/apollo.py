"""Apollo.io person enrichment.

Returns {} when the key is missing, no match is found, or the API hard-fails
after retries. 429 responses get exponential backoff (1s, 2s, 4s).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.config import settings
from app.services.tenant_keys import ambient_api_key

logger = logging.getLogger(__name__)

APOLLO_URL = "https://api.apollo.io/v1/people/match"
_MAX_ATTEMPTS = 3
_TIMEOUT_SECONDS = 30.0


async def enrich_lead_apollo(
    email: str,
    first_name: str,
    last_name: str,
    company: str,
) -> dict[str, Any]:
    api_key = await ambient_api_key("apollo")
    if not api_key:
        return {}

    payload = {
        "api_key": api_key,
        "email": email,
        "first_name": first_name or None,
        "last_name": last_name or None,
        "organization_name": company or None,
    }
    payload = {k: v for k, v in payload.items() if v not in (None, "")}

    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = await client.post(APOLLO_URL, json=payload)
            except httpx.HTTPError as e:
                logger.warning("Apollo request error (attempt %s): %s", attempt + 1, e)
                if attempt == _MAX_ATTEMPTS - 1:
                    return {}
                await asyncio.sleep(2**attempt)
                continue

            if resp.status_code == 429:
                logger.info("Apollo rate-limited, retrying after %ss", 2**attempt)
                await asyncio.sleep(2**attempt)
                continue
            if resp.status_code >= 500:
                logger.warning("Apollo %s on attempt %s", resp.status_code, attempt + 1)
                if attempt == _MAX_ATTEMPTS - 1:
                    return {}
                await asyncio.sleep(2**attempt)
                continue
            if resp.status_code >= 400:
                # 401/403/404 — no point retrying.
                logger.info("Apollo returned %s for %s", resp.status_code, email)
                return {}

            try:
                data = resp.json() or {}
            except ValueError:
                return {}

            person = data.get("person") or {}
            if not person:
                return {}
            org = person.get("organization") or {}
            return {
                "linkedin_url": person.get("linkedin_url"),
                "linkedin_headline": person.get("headline"),
                "job_title": person.get("title"),
                "seniority": person.get("seniority"),
                "department": person.get("department"),
                "company_employee_count": org.get("estimated_num_employees"),
                "company_industry": org.get("industry"),
                "company_funding_stage": org.get("latest_funding_stage"),
            }
    return {}


# ---------------------------------------------------------------------------
# Search (Feature D — lookalike discovery)
#
# NOTE: Apollo's *search* endpoints (mixed_companies / mixed_people) may
# require a paid plan tier — free/basic keys often get 403 or empty
# results.  Callers (lookalike_discovery) treat 403/empty as "search
# unavailable" and fall back to web-research discovery.
# ---------------------------------------------------------------------------

APOLLO_ORG_SEARCH_URL = "https://api.apollo.io/v1/mixed_companies/search"
APOLLO_PEOPLE_SEARCH_URL = "https://api.apollo.io/v1/mixed_people/search"


async def _apollo_search(url: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Shared search POST with the same retry/backoff posture as
    ``enrich_lead_apollo``.  Returns None when the key is missing, the
    plan lacks search access (401/403), or the API hard-fails."""
    api_key = await ambient_api_key("apollo")
    if not api_key:
        return None
    body = {"api_key": api_key, **payload}
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = await client.post(url, json=body)
            except httpx.HTTPError as e:
                logger.warning("Apollo search error (attempt %s): %s", attempt + 1, e)
                if attempt == _MAX_ATTEMPTS - 1:
                    return None
                await asyncio.sleep(2**attempt)
                continue
            if resp.status_code == 429:
                await asyncio.sleep(2**attempt)
                continue
            if resp.status_code >= 500:
                if attempt == _MAX_ATTEMPTS - 1:
                    return None
                await asyncio.sleep(2**attempt)
                continue
            if resp.status_code >= 400:
                # 401/403 = plan tier without search access; 404 = bad query.
                logger.info("Apollo search returned %s — treating as unavailable", resp.status_code)
                return None
            try:
                return resp.json() or {}
            except ValueError:
                return None
    return None


async def search_organizations(
    *,
    industries: list[str] | None = None,
    employee_ranges: list[str] | None = None,
    keywords: str | None = None,
    per_page: int = 25,
) -> list[dict[str, Any]]:
    """Firmographic org search.  Returns [] when search is unavailable."""
    payload: dict[str, Any] = {"page": 1, "per_page": per_page}
    if industries:
        payload["organization_industry_tag_ids"] = None  # tag ids need lookup
        payload["q_organization_keyword_tags"] = industries
    if employee_ranges:
        payload["organization_num_employees_ranges"] = employee_ranges
    if keywords:
        payload["q_organization_name"] = keywords
    data = await _apollo_search(APOLLO_ORG_SEARCH_URL, payload)
    if not data:
        return []
    orgs = data.get("organizations") or data.get("accounts") or []
    return [
        {
            "company": o.get("name"),
            "company_website": o.get("website_url"),
            "industry": o.get("industry"),
            "employee_count": o.get("estimated_num_employees"),
            "funding_stage": o.get("latest_funding_stage"),
            "linkedin_url": o.get("linkedin_url"),
        }
        for o in orgs
        if o.get("name")
    ]


async def search_people(
    *,
    titles: list[str] | None = None,
    industries: list[str] | None = None,
    employee_ranges: list[str] | None = None,
    per_page: int = 25,
) -> list[dict[str, Any]]:
    """Title+firmographic people search.  Returns [] when unavailable."""
    payload: dict[str, Any] = {"page": 1, "per_page": per_page}
    if titles:
        payload["person_titles"] = titles
    if industries:
        payload["q_organization_keyword_tags"] = industries
    if employee_ranges:
        payload["organization_num_employees_ranges"] = employee_ranges
    data = await _apollo_search(APOLLO_PEOPLE_SEARCH_URL, payload)
    if not data:
        return []
    people = data.get("people") or []
    out = []
    for p in people:
        org = p.get("organization") or {}
        out.append({
            "contact_name": " ".join(filter(None, [p.get("first_name"), p.get("last_name")])) or None,
            "job_title": p.get("title"),
            "email": p.get("email"),
            "linkedin_url": p.get("linkedin_url"),
            "company": org.get("name"),
            "company_website": org.get("website_url"),
            "industry": org.get("industry"),
            "employee_count": org.get("estimated_num_employees"),
            "funding_stage": org.get("latest_funding_stage"),
        })
    return [c for c in out if c["company"]]
