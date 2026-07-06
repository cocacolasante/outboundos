"""Anthropic-backed web research for an individual lead.

Returns a stable shape regardless of failures or missing API key so callers
never need to special-case None.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from anthropic import AsyncAnthropic

from app.config import settings
from app.services.tenant_keys import ambient_api_key
from app.services.research_cache import company_fields

logger = logging.getLogger(__name__)

_DEFAULT: dict[str, Any] = {
    "person_news": [],
    "company_news": [],
    "company_description": "",
    "recent_updates": [],
    "industry": "",
    "size_hint": "",
    "found": False,
}

_client: AsyncAnthropic | None = None


def _get_client(api_key: str | None = None) -> AsyncAnthropic:
    # BYOK (Phase 4): a fresh client per call — the key is per-tenant
    # now, so a module-global cache would leak keys across tenants.
    # api_key=None means the caller ran outside tenant context (env).
    return AsyncAnthropic(api_key=api_key or settings.ANTHROPIC_API_KEY or "unset")


def _extract_text(message: Any) -> str:
    parts: list[str] = []
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
    return "\n".join(parts)


def _parse_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    # Some models prepend prose — try to locate the outermost JSON object.
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        data = json.loads(cleaned[start : end + 1])
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


async def research_person_web(
    first_name: str,
    last_name: str,
    company: str,
    job_title: str,
    company_website: str = "",
    cached_company: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not settings.ANTHROPIC_API_KEY:
        return dict(_DEFAULT)

    website_line = (
        f"Company website: {company_website}\n" if company_website else ""
    )

    # Company-dedup fast path: when a prior lead at the same domain already
    # researched the company, we pass that context in and tell the model to
    # spend its (single) search ONLY on the person.  Skipping the company half
    # of the search ingests far fewer result pages — the dominant per-lead
    # cost — while keeping per-person personalization.  Cached company fields
    # are overlaid onto the result below.
    company_ctx = company_fields(cached_company or {})
    if company_ctx:
        ctx_desc = company_ctx.get("company_description") or ""
        ctx_industry = company_ctx.get("industry") or ""
        prompt = (
            "You are researching a PERSON for a personalized cold email.\n"
            f"Subject: {first_name} {last_name}, {job_title} at {company}\n"
            f"{website_line}"
            f"The company is already known — do NOT search for company info:\n"
            f"  What they do: {ctx_desc}\n"
            f"  Industry: {ctx_industry}\n\n"
            "Use web search (ONE focused search) to find ONLY:\n"
            "1. Recent news, achievements, public quotes, or interviews from "
            "this person.\n\n"
            "REPOST RULE (important — applies to person_news only):\n"
            "- A naked LinkedIn repost / reshare — where the person clicked "
            "'repost' but added NO words of their own — is NOT their content.  "
            "Do not include it in person_news.\n"
            "- A repost WITH the person's own added commentary IS their "
            "content; describe it as their COMMENT.\n"
            "- Original posts they wrote themselves: include as normal.\n\n"
            "Respond ONLY with one JSON object, no preamble, no markdown:\n"
            '{"person_news": ["item 1"], "company_news": [], '
            '"company_description": "", "recent_updates": [], "industry": "", '
            '"size_hint": "", "found": true}\n\n'
            'If nothing useful is found about the person, return found: false '
            "and empty lists."
        )
        return await _run(prompt, overlay=company_ctx)

    # ONE web-search call covers both the person and the company.  Previously
    # this was two separate Sonnet+web-search calls per lead (here +
    # site_scraper), which roughly doubled the per-lead cost.  Merged into a
    # single Haiku call with a tighter search budget.
    prompt = (
        "You are researching a person AND their company for a personalized "
        "cold email.\n"
        f"Subject: {first_name} {last_name}, {job_title} at {company}\n"
        f"{website_line}\n"
        "Use web search (be efficient — a couple of focused searches) to find:\n"
        "1. Recent news, achievements, public quotes, or interviews from this person.\n"
        "2. Recent company news, product launches, or announcements.\n"
        "3. A short description of what the company does (1-2 sentences).\n"
        "4. The company's industry / sector.\n"
        '5. A size hint — one of: "startup", "growth", "mid-market", '
        '"enterprise", or "" if unsure.\n\n'
        "REPOST RULE (important — applies to person_news only):\n"
        "- A naked LinkedIn repost / reshare — where the person clicked "
        "'repost' but added NO words of their own — is NOT their content.  "
        "Do not include it in person_news.  The cold-outreach message will "
        "later reference it as if they wrote it, which is wrong and "
        "embarrassing.\n"
        "- A repost WITH the person's own added commentary IS their "
        "content (their thoughts on the reshared item).  Include it, but "
        "describe it as their COMMENT — e.g. \"commented on a repost about "
        "X, saying Y (Apr 2026)\" — not as if they authored the original.\n"
        "- Original posts they wrote themselves: include as normal.\n\n"
        "Respond ONLY with one JSON object, no preamble, no markdown:\n"
        '{"person_news": ["item 1"], "company_news": ["item 1"], '
        '"company_description": "short description", '
        '"recent_updates": ["update 1"], "industry": "SaaS", '
        '"size_hint": "startup", "found": true}\n\n'
        'If nothing useful is found, return found: false and empty lists.'
    )

    return await _run(prompt)


async def _run(prompt: str, overlay: dict[str, Any] | None = None) -> dict[str, Any]:
    """Make the research call, parse it, and return the stable shape.  When
    ``overlay`` (cached company fields) is given, fill any company field the
    model left empty from the cache — so a person-focused search still yields
    full company context."""
    try:
        from app.billing.entitlements import Meter, check_quota

        await check_quota(Meter.AI_RESEARCH)
        message = await _get_client(await ambient_api_key("anthropic")).messages.create(
            model=settings.ANTHROPIC_RESEARCH_MODEL,
            max_tokens=2000,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": settings.RESEARCH_WEB_SEARCH_MAX_USES,
            }],
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("research_person_web API call failed: %s", e)
        out = dict(_DEFAULT)
        if overlay:
            out.update(overlay)
        return out

    data = _parse_json(_extract_text(message))
    if not data:
        out = dict(_DEFAULT)
        if overlay:
            out.update(overlay)
        return out

    result = {
        "person_news": list(data.get("person_news") or []),
        "company_news": list(data.get("company_news") or []),
        "company_description": str(data.get("company_description") or ""),
        "recent_updates": list(data.get("recent_updates") or []),
        "industry": str(data.get("industry") or ""),
        "size_hint": str(data.get("size_hint") or ""),
        "found": bool(data.get("found", False)),
    }
    overlay = overlay or {}
    for key, val in overlay.items():
        # The cached company fields are authoritative — the person-only search
        # was told not to research the company — so fill empties from cache.
        if not result.get(key):
            result[key] = val
    return result
