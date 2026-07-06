"""Single-prospect research, driven by a LinkedIn URL.

The bulk pipeline ingests a CSV of leads with name/company/title/email
already in hand and feeds those into ``web_research.research_person_web``
+ ``site_scraper.scrape_company_site``.  The "research a client" feature
in the UI works from a single LinkedIn URL with no other inputs, so this
service does a one-shot Anthropic call (with web search) that combines
identity extraction and personalization research into a single dict
shaped like the bulk pipeline's ``research_data``.

Two depth modes mirror the campaign ``ResearchMode``:

- ``fast`` (~10s): one Anthropic call, ``max_uses=3`` for web search.
- ``deep`` (~30-45s): one Anthropic call, ``max_uses=8`` + a more
  thorough prompt that asks for richer personalization signals.

No Unipile call — keeping this Anthropic-only avoids the
"who viewed your profile" notification the prospect would otherwise see.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from anthropic import AsyncAnthropic

from app.config import settings
from app.services.tenant_keys import ambient_api_key
from app.services.web_research import _extract_text, _parse_json

logger = logging.getLogger(__name__)


_DEFAULT_RESEARCH: dict[str, Any] = {
    "first_name": "",
    "last_name": "",
    "headline": "",
    "company": "",
    "company_website": "",
    "job_title": "",
    "person_news": [],
    "company_news": [],
    "company_description": "",
    "recent_updates": [],
    "industry": "",
    "found": False,
    "quality": "low",
}


_client: AsyncAnthropic | None = None


def _get_client(api_key: str | None = None) -> AsyncAnthropic:
    # BYOK (Phase 4): a fresh client per call — the key is per-tenant
    # now, so a module-global cache would leak keys across tenants.
    # api_key=None means the caller ran outside tenant context (env).
    return AsyncAnthropic(api_key=api_key or settings.ANTHROPIC_API_KEY or "unset")


def parse_linkedin_url(url: str) -> tuple[str, str]:
    """Extract the public-identifier slug and a guessed name from a
    LinkedIn URL.

    ``https://www.linkedin.com/in/jane-doe-12345/`` → ``("jane-doe-12345",
    "Jane Doe")``.  Trailing numeric tokens (the dedup suffix LinkedIn
    appends when there's a name collision) are dropped from the guess.

    Returns ``("", "")`` when the URL doesn't look like a LinkedIn profile.
    """
    if not url:
        return "", ""
    try:
        parsed = urlparse(url.strip())
    except Exception:  # noqa: BLE001
        return "", ""
    host = (parsed.netloc or "").lower()
    if "linkedin.com" not in host:
        return "", ""
    parts = [p for p in parsed.path.split("/") if p]
    # LinkedIn profile URLs are /in/<slug> (or rarely /pub/<slug>).
    if len(parts) < 2 or parts[0] not in {"in", "pub"}:
        return "", ""
    slug = parts[1].lower()
    # Strip the dedup suffix: "jane-doe-a1b2c3d4" → "jane-doe".
    name_tokens = slug.split("-")
    # Drop any token that looks like a hex/numeric suffix (>=7 chars and mostly hex).
    while name_tokens and re.fullmatch(r"[0-9a-f]{7,}", name_tokens[-1]):
        name_tokens.pop()
    name_guess = " ".join(t.capitalize() for t in name_tokens if t)
    return slug, name_guess


def _quality_from(data: dict[str, Any]) -> str:
    person = bool(data.get("person_news"))
    company = bool(data.get("company_news")) or bool(data.get("company_description"))
    if person and company:
        return "rich"
    if person or company:
        return "partial"
    return "low"


async def research_from_linkedin_url(
    linkedin_url: str,
    mode: str,
) -> dict[str, Any]:
    """One-shot identity + personalization research for a LinkedIn URL.

    ``mode``: ``"fast"`` (cheap, ~10s) or ``"deep"`` (~30-45s, deeper
    search).  Returns a dict shaped like the bulk pipeline's
    ``research_data`` plus the identity fields (first_name, last_name,
    headline, company, company_website, job_title) the compose prompt
    needs.

    Always returns the default shape — never raises on failure.  Sets
    ``found=False`` if Anthropic comes back empty so the caller can pick a
    minimal "generic" compose prompt.
    """
    slug, name_guess = parse_linkedin_url(linkedin_url)
    if not slug:
        return {**_DEFAULT_RESEARCH, "error": "url_not_recognised_as_linkedin_profile"}

    if not settings.ANTHROPIC_API_KEY:
        return {**_DEFAULT_RESEARCH, "first_name": name_guess.split(" ")[0]}

    is_deep = (mode or "").lower() == "deep"
    max_uses = (
        settings.RESEARCH_CLIENT_DEEP_WEB_SEARCH_MAX_USES if is_deep
        else settings.RESEARCH_CLIENT_FAST_WEB_SEARCH_MAX_USES
    )
    today = datetime.now(timezone.utc).date()
    # "Within 6 months" is the freshness floor.  Anything outside this
    # window must be excluded — stale references in a cold outreach
    # message ("congrats on your last role two years ago") read worse
    # than no personalization at all.  Using fixed day counts keeps the
    # math precise across month boundaries: 183 days ≈ 6 months, 365 ≈ 1y.
    freshness_floor_iso = (today - timedelta(days=183)).isoformat()
    one_year_floor_iso = (today - timedelta(days=365)).isoformat()
    depth_instr = (
        "Be thorough. Search broadly: their recent talks, interviews, "
        "podcast appearances, GitHub or Substack activity, recent job "
        "moves, conference appearances, blog posts they've authored, "
        "and concrete projects they currently lead.  Spend the full "
        "search budget; quality > brevity."
        if is_deep
        else
        "Keep it focused: their CURRENT role, one or two recent "
        "highlights (talk, post, hire, raise, launch), and a one-line "
        "company description.  Don't burn search calls on background."
    )

    prompt = (
        f"You are researching one specific person for a single, hand-written "
        f"outreach message.  Their LinkedIn URL is: {linkedin_url}\n"
        f"Name guess from the URL slug: {name_guess or '(unknown)'}\n"
        f"Today's date: {today.isoformat()}\n\n"
        "RULES — two different bars, do not conflate them:\n"
        "\n"
        "IDENTITY FIELDS (first_name, last_name, headline, company, "
        "job_title, company_website, industry, company_description):\n"
        "- Fill these whenever you can find them.  The LinkedIn profile "
        "itself is the source of truth for current role; you do NOT need "
        "a recent news article to confirm someone's title.\n"
        "- ONLY pick the role they hold RIGHT NOW (their current LinkedIn "
        "headline / most recent position), NOT a prior job.  If LinkedIn "
        "shows two recent positions and you can't tell which is current, "
        "pick the most recent start date or leave the field blank.\n"
        "- Don't guess.  If nothing on the public web identifies them, "
        "leave the field blank — but ordinary professional details "
        "(current employer, title) don't need a press-release source.\n"
        "\n"
        "PERSONALIZATION SIGNALS (person_news, company_news, recent_updates):\n"
        f"- Strict freshness: only items dated {freshness_floor_iso} or "
        "later (last ~6 months).  Anything older is OFF-LIMITS.\n"
        f"- Never include anything dated before {one_year_floor_iso} "
        "(over a year old) under any circumstance.\n"
        "- For EACH item, include a parenthetical month/year tag at the "
        "end (e.g. \"(Apr 2026)\") so the compose stage can sanity-check.\n"
        "- If a recent achievement is from their CURRENT role/company, "
        "great.  Achievements from a PRIOR role/company go in the bin — "
        "the outreach message must not reference work they've moved on from.\n"
        "- REPOST RULE for person_news (LinkedIn / X / blogs etc.): A "
        "naked repost / reshare with NO added commentary from the person "
        "is NOT their content — EXCLUDE it.  Referring to it in an "
        "outreach message (\"loved your post on X\") is wrong: they "
        "didn't write it.  A repost WITH the person's own added "
        "commentary IS their content — INCLUDE it, but describe it as "
        "their COMMENT (e.g. \"commented on a repost about X, saying Y "
        "(Apr 2026)\") so the compose stage knows the original wasn't "
        "theirs.  Original posts they authored: include as normal.\n"
        "- Empty arrays are FINE if no recent activity is found.  Don't "
        "pad with stale items or with naked reshares.\n"
        "\n"
        "Use web search to find:\n"
        "1. Their real first and last name (confirm or correct the URL guess).\n"
        "2. Their CURRENT job title and company (whatever LinkedIn or "
        "their current employer's site says now).\n"
        "3. A one-line professional headline (e.g. their LinkedIn tagline).\n"
        "4. The current company's website domain.\n"
        "5. Recent person news/posts/talks — items from the last 6 months ONLY.\n"
        "6. Recent company news, product launches, fundraising, hires — last 6 months ONLY.\n"
        "7. A short description of the company and its industry.\n\n"
        f"{depth_instr}\n\n"
        "Respond with ONLY one JSON object, no preamble, no markdown:\n"
        "{\n"
        '  "first_name": "...", "last_name": "...",\n'
        '  "headline": "...", "company": "...",\n'
        '  "company_website": "domain.com", "job_title": "...",\n'
        '  "industry": "...",\n'
        '  "person_news": ["...", "..."],\n'
        '  "company_news": ["..."], "company_description": "...",\n'
        '  "recent_updates": ["..."],\n'
        '  "found": true\n'
        "}\n\n"
        "If you can't confidently identify the person, return found=false "
        "with empty arrays and best-effort first_name/last_name from the URL slug."
    )

    try:
        message = await _get_client(await ambient_api_key("anthropic")).messages.create(
            # Was settings.ANTHROPIC_MODEL (Sonnet).  This research call
            # is extraction-from-web-search-results — exact same task
            # profile as the bulk pipeline's research_person_web, which
            # already runs on Haiku.  Switching here makes "Research a
            # client" clicks ~4x cheaper on the dominant ingested-token
            # cost without measurable quality loss for the
            # identity-extraction + recent-news extraction job.
            model=settings.ANTHROPIC_RESEARCH_CLIENT_MODEL,
            max_tokens=3000,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": max_uses}],
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("research_from_linkedin_url failed for %s: %s", linkedin_url, e)
        first_guess = name_guess.split(" ")[0] if name_guess else ""
        last_guess = " ".join(name_guess.split(" ")[1:]) if name_guess else ""
        return {
            **_DEFAULT_RESEARCH,
            "first_name": first_guess, "last_name": last_guess,
            "error": "anthropic_call_failed",
        }

    parsed = _parse_json(_extract_text(message)) or {}
    if not isinstance(parsed, dict):
        parsed = {}

    out: dict[str, Any] = {
        "first_name": str(parsed.get("first_name") or "").strip(),
        "last_name": str(parsed.get("last_name") or "").strip(),
        "headline": str(parsed.get("headline") or "").strip(),
        "company": str(parsed.get("company") or "").strip(),
        "company_website": str(parsed.get("company_website") or "").strip(),
        "job_title": str(parsed.get("job_title") or "").strip(),
        "industry": str(parsed.get("industry") or "").strip(),
        "person_news": [str(x) for x in (parsed.get("person_news") or []) if x],
        "company_news": [str(x) for x in (parsed.get("company_news") or []) if x],
        "company_description": str(parsed.get("company_description") or "").strip(),
        "recent_updates": [str(x) for x in (parsed.get("recent_updates") or []) if x],
        "found": bool(parsed.get("found", False)),
    }
    # Fall back to URL-slug name when Anthropic comes back empty.
    if not out["first_name"] and name_guess:
        tokens = name_guess.split(" ")
        out["first_name"] = tokens[0]
        if len(tokens) > 1 and not out["last_name"]:
            out["last_name"] = " ".join(tokens[1:])
    out["quality"] = _quality_from(out)
    return out
