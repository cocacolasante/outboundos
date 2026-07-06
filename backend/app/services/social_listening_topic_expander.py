"""Expand a plain-English topic into a list of LinkedIn search phrases.

The user types something like ``frustrated with technology`` and we want
to fan that out into 15–30 variations a real prospect might write — both
vendor-frustration phrasings (``frustrated with our MSP``) AND
replacement-search phrasings (``looking for a new phone system``).

One Anthropic call, Haiku (cheap, extraction-shaped task), no web search.
"""
from __future__ import annotations

import logging
from typing import Any

from app.config import settings
from app.services._anthropic import extract_text, get_client, parse_json_array
from app.services.tenant_keys import ambient_api_key

logger = logging.getLogger(__name__)


PROMPT_TEMPLATE = """You are helping a B2B advisor find buying-intent \
posts on Reddit, Twitter/X, and LinkedIn about technology services \
(UCaaS phone systems, internet/connectivity, cybersecurity, cloud, \
MSP/vendor management, CRM/software, nonprofit technology).

Topic: {topic}
Niche: {niche}
Geography: {geography}

Generate 15–25 SHORT SEARCH PHRASES that a web search engine will \
actually return hits for.  These are NOT full sentences — they are \
3-to-6-word keyword combinations that capture the essence of buying \
intent or vendor frustration.  Think "Google query", not "LinkedIn \
post draft".

GOOD examples — short, specific keywords + intent words:
  - "msp slow response time"
  - "ringcentral alternatives small business"
  - "switching from comcast business"
  - "voip dropped calls hybrid"
  - "fed up with msp"
  - "nonprofit crm too expensive"
  - "phone system contract renewal"
  - "cybersecurity vendor recommendations"
  - "cloud costs out of control"
  - "looking for managed it provider"
  - "blackbaud alternatives nonprofit"
  - "tired of our it provider"
  - "rfp managed services"
  - "ciso for hire fractional"

BAD examples — DROPPED because they're too generic OR too verbose:
  - "internet" — single word
  - "phone system" — too generic, surfaces SEO product pages
  - "anyone else fed up with their msp not returning calls" — too long, \
search engines treat 10+ word strings as exact-match and return zero hits
  - "our phone system dropped 14 calls this morning" — too specific, \
nobody else writes EXACTLY that

Rules:
- Output a JSON array of strings only.  No prose, no markdown fence.
- 3 to 8 WORDS per phrase.  Shorter than 3 surfaces SEO content; \
longer than 8 returns zero hits because search engines treat them as \
exact-match queries.
- Lowercase, no quotes inside the strings.
- Combine: a noun (the topic / product / vendor) + an intent word \
(frustrated, switching, alternatives, recommendations, looking for, \
fed up, evaluating, dropped, slow, expensive, RFP).
- Reference real vendor names when sensible (RingCentral, Comcast, \
HubSpot, Salesforce, Blackbaud, Microsoft 365) — these surface MUCH \
better than generic terms.
- Deduplicated.
- Quality > quantity.  10 great short phrases beat 20 mediocre ones.

{extras}
"""


def _format_extras(
    include_keywords: list[str], exclude_keywords: list[str],
) -> str:
    parts: list[str] = []
    if include_keywords:
        parts.append(
            "MUST-INCLUDE keywords (each output should mention at least "
            "one if natural): " + ", ".join(include_keywords)
        )
    if exclude_keywords:
        parts.append(
            "Avoid phrases containing: " + ", ".join(exclude_keywords)
        )
    return "\n".join(parts) if parts else ""


def _normalise(query: str) -> str:
    return " ".join((query or "").lower().strip().split())


_MIN_WORDS_AI_GENERATED = 3
"""Reject AI-generated phrases shorter than this.  Single-word and 2-
word noun-phrase queries are useless for finding posts — they just
surface SEO listicles + product pages instead of personal venting.
The user can still inject any short phrase they want via
``include_keywords`` (those bypass this filter — explicit override)."""

_MAX_WORDS_AI_GENERATED = 8
"""Reject AI-generated phrases LONGER than this.  Search engines treat
10+ word strings as near-exact-match and return zero hits.  This is
the failure mode we hit in production: the AI was generating full
sentences like "anyone else fed up with their msp not returning calls"
which returned 0 raw results across every (source, query) pair."""


def _word_count(s: str) -> int:
    return len([w for w in s.split() if w])


def _filter(
    queries: list[str], include: list[str], exclude: list[str], max_n: int,
) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    # include_keywords first — user-specified phrases that should appear
    # in the run.  We STILL apply the min-words filter to these because
    # users tend to type single-word noun phrases like "hacked" or
    # "phished" that return SEO listicles instead of real intent posts.
    # The user can put any 3+ word phrase here as an explicit override.
    for kw in include:
        n = _normalise(kw)
        if not n or n in seen:
            continue
        if _word_count(n) < _MIN_WORDS_AI_GENERATED:
            continue
        seen.add(n)
        out.append(n)
    # Then the AI-generated set, dropping any that match an exclude term
    # OR is shorter than _MIN_WORDS_AI_GENERATED (those almost always
    # surface SEO content, not real LinkedIn posts).
    excludes_lower = [e.lower().strip() for e in exclude if e and e.strip()]
    for q in queries:
        if not isinstance(q, str):
            continue
        n = _normalise(q)
        if not n or n in seen:
            continue
        wc = _word_count(n)
        if wc < _MIN_WORDS_AI_GENERATED:
            continue
        if wc > _MAX_WORDS_AI_GENERATED:
            continue
        if any(e in n for e in excludes_lower):
            continue
        seen.add(n)
        out.append(n)
        if len(out) >= max_n:
            break
    return out[:max_n]


async def expand_topic(
    topic: str,
    niche: str | None = None,
    geography: str | None = None,
    include_keywords: list[str] | None = None,
    exclude_keywords: list[str] | None = None,
    max_queries: int = 20,
) -> list[str]:
    """Return up to ``max_queries`` deduped, lowercased search phrases.

    Returns ``[]`` on any Anthropic failure (no API key, network down,
    invalid JSON, …) — callers should treat empty as a soft failure and
    surface it to the UI.
    """
    include_keywords = include_keywords or []
    exclude_keywords = exclude_keywords or []

    if not settings.ANTHROPIC_API_KEY:
        # No key configured — fall back to include_keywords only so the
        # feature isn't broken in dev without an API key.
        return _filter([], include_keywords, exclude_keywords, max_queries)

    prompt = PROMPT_TEMPLATE.format(
        topic=topic.strip(),
        niche=(niche or "").strip() or "(any)",
        geography=(geography or "").strip() or "(any)",
        extras=_format_extras(include_keywords, exclude_keywords),
    )

    try:
        message = await get_client(await ambient_api_key("anthropic")).messages.create(
            model=settings.ANTHROPIC_RESEARCH_MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("expand_topic Anthropic call failed: %s", e)
        return _filter([], include_keywords, exclude_keywords, max_queries)

    parsed = parse_json_array(extract_text(message)) or []
    return _filter(parsed, include_keywords, exclude_keywords, max_queries)
