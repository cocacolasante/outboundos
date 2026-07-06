"""Discover buying-intent social posts matching a search query.

Multi-source: dispatches to per-source prompts + URL validators based
on the ``source`` argument.  Each source has different platform
conventions — LinkedIn rants, Reddit subreddit Q&A, X/Twitter
complaints — so the prompt nudges Claude to find the right thing for
each.

Why not Unipile for LinkedIn?  Their raw Voyager passthrough is on a
narrow allowlist and post-search endpoints aren't on it.  We use
Anthropic's ``web_search_20250305`` tool for all sources.  LinkedIn
recall is poor (the platform aggressively blocks indexing) but Reddit
and X/Twitter are heavily indexed — the multi-source fan-out exists
specifically to compensate for LinkedIn's blind spot.

Defaults to Haiku — discovery is extraction-from-search-results, exact
same shape as ``research_person_web`` which already lives on Haiku.
``settings.ANTHROPIC_SOCIAL_DISCOVERY_MODEL`` and
``settings.SOCIAL_DISCOVERY_WEB_SEARCH_MAX_USES`` are the cost knobs.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings
from app.services._anthropic import extract_text, get_client, parse_json_object
from app.services.tenant_keys import ambient_api_key
from app.services._anthropic_cost import message_cost_usd

logger = logging.getLogger(__name__)


@dataclass
class DiscoveredPost:
    post_url: str
    post_text: str
    provider_post_id: str | None = None
    author_name: str | None = None
    author_profile_url: str | None = None
    author_headline: str | None = None
    company_name: str | None = None
    post_date: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class DiscoveryResult:
    """Posts that survived filtering + per-reason drop counts for the
    one query that produced them.  The worker aggregates these across
    all queries to populate ``last_run_stats``."""
    posts: list[DiscoveredPost] = field(default_factory=list)
    raw: int = 0  # how many entries Anthropic returned (pre-filter)
    dropped_invalid_url: int = 0
    dropped_excluded: int = 0
    dropped_duplicate: int = 0
    dropped_undated: int = 0
    dropped_stale: int = 0
    # Posts kept WITHOUT a parsable post_date.  Only applies to non-
    # LinkedIn sources (Reddit / Twitter) where the URL itself is a
    # strong recency signal: Twitter snowflake IDs are timestamps;
    # Reddit ``/comments/<id>/`` pages surface fresh results first via
    # web-search ranking.  Treated as a SEPARATE counter (not dropped,
    # not regular kept) so the user can see how many came in undated.
    kept_undated: int = 0
    # When the Anthropic call itself threw (billing / auth / rate
    # limit / network), the short readable message lands here so the
    # worker can surface it on ``search.last_run_error``.  Empty / None
    # on success.  Without this, an out-of-credits Anthropic account
    # silently returned ``DiscoveryResult()`` and the user saw 0 posts
    # with no clue why.
    error: str | None = None
    # Approximate USD cost of this single call (model tokens +
    # web_search tool fees).  Worker accumulates these to drive the
    # per-run cost cap and to populate ``last_run_stats.summary.cost_usd``.
    cost_usd: float = 0.0

    @property
    def kept(self) -> int:
        return len(self.posts)


_COMMON_TAIL = """
For each post, extract:
  - post_url        — the canonical {platform_name} URL of the post
  - author_name     — the post author's full name or username
  - author_profile_url — their profile URL on this platform if visible
  - author_headline — their bio / title line (e.g. "CFO at Acme", \
"r/sysadmin top contributor", "@infosec on X")
  - company_name    — their company, if you can infer it
  - post_text       — the body of the post (first 500 chars is fine)
  - post_date       — REQUIRED.  When the post was published, as ISO-8601 \
(YYYY-MM-DD or full timestamp).  If the date isn't visible in the \
search result, infer it from "X weeks ago" / "posted Mar 12" / \
URL-embedded date / contextual cues.  If you cannot determine a date \
with reasonable confidence, OMIT THE WHOLE POST from your output — \
we cannot use posts whose freshness we can't verify.

Return STRICT JSON with this shape (no markdown fence, no prose):
{{
  "posts": [
    {{
      "post_url": "...",
      "author_name": "...",
      "author_profile_url": "...",
      "author_headline": "...",
      "company_name": "...",
      "post_text": "...",
      "post_date": "..."
    }}
  ]
}}

Rules:
- Return up to {max_results} posts.
- Skip duplicates.
- Only include posts whose URL matches the {platform_name} post-URL \
patterns above.
- If you can't find any matching posts, return ``{{"posts": []}}``.
- Do NOT fabricate URLs or content — only include what you actually \
saw in search results.
- Prefer NEW posts the user hasn't seen yet — if web_search returns \
multiple candidates, surface ones whose URLs are NOT in the \
already-seen list below.
- HARD FRESHNESS REQUIREMENT: posts MUST be published on or after \
{cutoff_iso} (within the last {max_post_age_days} days).  Today is \
{today_iso}.  Even if a post looks like a perfect match topically, \
DO NOT include it if its date is older than {cutoff_iso}.
- If web_search returns predominantly old articles / SEO content / \
listicles instead of fresh personal posts, return ``{{"posts": []}}`` \
rather than padding with stale matches.

{exclude_block}"""


_LINKEDIN_PROMPT = """You are a researcher helping a B2B advisor find \
LinkedIn POSTS or LinkedIn long-form ARTICLES (Pulse) that match a \
search query indicating someone is venting, asking for help, or \
signaling buying intent.

Search query: "{query}"

LinkedIn aggressively blocks indexing, so direct LinkedIn searches \
return very little.  You have ONE web_search call — spend it on the \
highest-recall query, which is Pulse long-form articles + general \
post pages:

    site:linkedin.com/pulse OR site:linkedin.com/posts "{query}"

(Use that EXACT site filter — it scopes results to LinkedIn long-form \
articles and feed posts without wasting hits on noise like \
``/in/`` profile pages or company pages.)

For each result, the URL MUST contain one of these substrings:
  - linkedin.com/posts/
  - linkedin.com/feed/update/
  - linkedin.com/pulse/

If a result quotes a LinkedIn post but the URL is on a news / blog / \
aggregator site, DO NOT include it (we can't link to the original \
post manually).

If web_search returns nothing relevant, return ``{{"posts": []}}`` — \
LinkedIn really is the hardest source to mine, and an empty result is \
honest.  DO NOT pad with stale SEO listicles.
""" + _COMMON_TAIL


_REDDIT_PROMPT = """You are a researcher helping a B2B advisor find \
Reddit POSTS or top-level comments that match a search query \
indicating someone is venting, asking for help, or signaling buying \
intent.

Search query: "{query}"

⚠ The query above may be long-form / sentence-like.  Web search \
engines treat 10+ word strings as near-exact-match and return zero \
hits.  USE YOUR web_search BUDGET WISELY: break the query into 2–3 \
short keyword variations (3-6 words each) and search each one.  For \
example, if the query is "anyone else fed up with their msp not \
returning calls", search for:
  - ``site:reddit.com "fed up with msp"``
  - ``site:reddit.com msp slow response``
  - ``site:reddit.com r/sysadmin msp support``

Look at relevant subreddits including but not limited to: \
r/sysadmin, r/msp, r/networking, r/cybersecurity, r/AskNetsec, \
r/smallbusiness, r/Entrepreneur, r/nonprofit, r/ITManagers, r/k12sysadmin, \
r/cloud, r/devops, r/voip, r/telecom.  Reddit is well-indexed so this is \
your primary source of fresh buying-intent signal.

Look for URLs on these patterns:
  - reddit.com/r/<subreddit>/comments/<id>/
  - old.reddit.com/r/<subreddit>/comments/<id>/

Treat the OP of a thread as the "author"; use their username (e.g. \
``u/some-handle``) for author_name.  The subreddit name (e.g. \
"r/sysadmin") makes a great ``author_headline``.
""" + _COMMON_TAIL


_TWITTER_PROMPT_HEAD = """You are a researcher helping a B2B advisor \
find X / Twitter POSTS (tweets) that match a search query indicating \
someone is venting, asking for help, or signaling buying intent.

Search query: "{query}"

⚠ The query above may be long-form / sentence-like.  Web search \
engines treat 10+ word strings as near-exact-match and return zero \
hits.  USE YOUR web_search BUDGET WISELY: break the query into 2–3 \
short keyword variations (3-6 words each) and search each one.  For \
example, if the query is "thinking about switching from comcast \
business after another outage", search for:
  - ``site:twitter.com switching from comcast business``
  - ``site:x.com comcast outage``
  - ``site:twitter.com comcast alternatives``

Public tweets are well-indexed.

Look for URLs on these patterns:
  - twitter.com/<username>/status/<id>
  - x.com/<username>/status/<id>

Use the @handle for author_name.  Bio or title (if visible in the \
snippet) goes in author_headline.
"""


_TWITTER_PROMPT = _TWITTER_PROMPT_HEAD + _COMMON_TAIL


_PROMPT_BY_SOURCE: dict[str, str] = {
    "linkedin": _LINKEDIN_PROMPT,
    "reddit": _REDDIT_PROMPT,
    "twitter": _TWITTER_PROMPT,
}

_PLATFORM_NAME_BY_SOURCE: dict[str, str] = {
    "linkedin": "LinkedIn",
    "reddit": "Reddit",
    "twitter": "X (Twitter)",
}


def _format_exclude(exclude_urls: list[str] | None) -> str:
    if not exclude_urls:
        return ""
    # Cap the list so the prompt doesn't balloon on long-running searches.
    capped = list(exclude_urls)[:200]
    bullets = "\n".join(f"  - {u}" for u in capped)
    return (
        "Already-seen URLs (DO NOT include any of these in your output, "
        "we already have them):\n" + bullets + "\n"
    )


_VALID_URL_FRAGMENTS_BY_SOURCE: dict[str, tuple[str, ...]] = {
    # ``linkedin.com/pulse/`` covers LinkedIn long-form articles which ARE
    # indexed (unlike feed posts) and often carry strong buying-intent
    # signal — they're closer to blog posts than tweets.
    "linkedin": ("linkedin.com/posts/", "linkedin.com/feed/update/", "linkedin.com/pulse/"),
    "reddit": ("reddit.com/r/", "redd.it/"),
    "twitter": ("twitter.com/", "x.com/"),
}


_MODEL_BY_SOURCE: dict[str, str] = {}
_MAX_USES_BY_SOURCE: dict[str, int] = {}


def _source_model(source: str) -> str:
    """Per-source Anthropic model.  LinkedIn benefits from Sonnet's
    better recall on buried indexed content; Reddit/Twitter are
    well-indexed so Haiku is fine."""
    if source == "linkedin":
        return settings.LINKEDIN_DISCOVERY_MODEL
    return settings.ANTHROPIC_SOCIAL_DISCOVERY_MODEL


def _source_max_uses(source: str) -> int:
    if source == "linkedin":
        # Hard-cap LinkedIn at 1 web_search call regardless of the config
        # default.  LinkedIn is a low-yield channel (the platform blocks
        # indexing) — multiple web_search uses spend tokens fanning out
        # across phrasings without finding more posts.  One focused
        # ``site:linkedin.com/...`` query is enough, and stops the
        # toggle-on case from burning ~$1.50/run.
        return min(1, settings.LINKEDIN_DISCOVERY_WEB_SEARCH_MAX_USES)
    return settings.SOCIAL_DISCOVERY_WEB_SEARCH_MAX_USES


def _short_error(exc: Exception) -> str:
    """Turn an Anthropic / httpx exception into a one-liner the user
    can read in the UI.  Anthropic structured errors look like:
        ``Error code: 400 - {'type': 'error', 'error': {'type': '...',
                                                         'message': 'Your credit balance is too low...'}}``
    — pull the readable ``message`` out, otherwise fall back to a
    truncated ``str(exc)``.  Output capped at 240 chars so the UI
    banner stays readable."""
    msg = str(exc) or type(exc).__name__
    if "'message':" in msg:
        try:
            cut = msg.split("'message':", 1)[1]
            extracted = cut.split("'", 2)[1]
            if extracted:
                return extracted[:240]
        except (IndexError, AttributeError):
            pass
    return msg[:240]


def _looks_like_post_url(url: Any, source: str) -> bool:
    if not isinstance(url, str):
        return False
    u = url.strip().lower()
    fragments = _VALID_URL_FRAGMENTS_BY_SOURCE.get(source, ())
    if not fragments:
        return False
    if source == "twitter":
        # Twitter/X URLs must include "/status/<id>" — bare profile URLs
        # don't count as a post.
        return any(frag in u for frag in fragments) and "/status/" in u
    if source == "reddit":
        # Reddit "post" URLs are ``/comments/<id>/...``; just being on
        # reddit.com isn't enough.
        return any(frag in u for frag in fragments) and (
            "/comments/" in u or u.split("redd.it/", 1)[-1]
        )
    return any(frag in u for frag in fragments)


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        # Tolerate trailing "Z" and bare dates.
        cleaned = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


_TWITTER_SNOWFLAKE_EPOCH_MS = 1288834974657
"""Twitter epoch (2010-11-04T01:42:54.657Z).  Tweet IDs are 64-bit
snowflakes — bits 22-63 are the millisecond offset from this epoch.
Lets us decode a post_date directly from any /status/<id> URL even
when Anthropic didn't extract one from the page."""


# Sources where we ENFORCE strict date verification (drop undated posts).
# LinkedIn alone — web-search results for LinkedIn are usually old SEO
# articles, and undated LinkedIn results are commonly stale.  Reddit
# and Twitter URLs encode timestamps directly so we trust them more.
STRICT_DATE_SOURCES: set[str] = {"linkedin"}


def _twitter_id_to_date(url: str) -> datetime | None:
    """Decode the timestamp from a Twitter snowflake ID.  Returns None
    for anything that doesn't look like a numeric ``/status/<id>`` URL."""
    if "/status/" not in url:
        return None
    try:
        tail = url.split("/status/", 1)[1]
        token = tail.split("/", 1)[0].split("?", 1)[0]
        snowflake = int(token)
    except (ValueError, IndexError):
        return None
    if snowflake <= 0:
        return None
    ts_ms = (snowflake >> 22) + _TWITTER_SNOWFLAKE_EPOCH_MS
    try:
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    except (ValueError, OverflowError):
        return None


def _extract_urn(url: str, source: str) -> str | None:
    """Pull the platform-native ID out of a post URL when possible.

    LinkedIn: ``urn:li:activity:<n>`` (only on /feed/update/ URLs).
    Reddit: the post id from ``/comments/<id>/...``.
    Twitter/X: the numeric tweet id from ``/status/<id>``.
    """
    if source == "linkedin":
        if "urn:li:activity:" in url:
            token = url.split("urn:li:activity:", 1)[1]
            token = token.split("/", 1)[0].split("?", 1)[0]
            return "urn:li:activity:" + token if token else None
        return None
    if source == "reddit":
        if "/comments/" in url:
            token = url.split("/comments/", 1)[1]
            token = token.split("/", 1)[0].split("?", 1)[0]
            return token or None
        if "redd.it/" in url:
            token = url.split("redd.it/", 1)[1]
            token = token.split("/", 1)[0].split("?", 1)[0]
            return token or None
        return None
    if source == "twitter":
        if "/status/" in url:
            token = url.split("/status/", 1)[1]
            token = token.split("/", 1)[0].split("?", 1)[0]
            return token or None
        return None
    return None


async def discover_posts(
    query: str,
    source: str = "linkedin",
    max_results: int = 30,
    exclude_urls: list[str] | None = None,
    max_post_age_days: int = 30,
) -> DiscoveryResult:
    """Front door for discovery.  Routes per-source:
      - ``reddit`` → direct Reddit RSS (free, no Anthropic).
      - ``linkedin`` / ``twitter`` → Anthropic web_search.

    Returns a uniform ``DiscoveryResult`` regardless of backend so the
    worker doesn't care which channel surfaced the posts.
    """
    if source == "reddit":
        # Late import to avoid a circular dependency (the Reddit service
        # imports DiscoveryResult / DiscoveredPost from this module).
        from app.services.social_listening_reddit_api import discover_reddit_posts
        return await discover_reddit_posts(
            query, max_results=max_results,
            exclude_urls=exclude_urls,
            max_post_age_days=max_post_age_days,
        )
    return await _discover_via_anthropic(
        query, source=source, max_results=max_results,
        exclude_urls=exclude_urls, max_post_age_days=max_post_age_days,
    )


async def _discover_via_anthropic(
    query: str,
    source: str = "linkedin",
    max_results: int = 30,
    exclude_urls: list[str] | None = None,
    max_post_age_days: int = 30,
) -> DiscoveryResult:
    """Find LinkedIn posts matching ``query``.

    ``exclude_urls`` is a list of already-discovered post URLs the caller
    has already pulled and qualified.  We bake them into the prompt as a
    "do not include these" instruction AND post-filter them defensively
    in case the model returns one anyway.  This means a rerun of the
    same search avoids re-qualifying posts we've already scored.

    ``max_post_age_days`` is the lookback window.  We tell the LLM to
    only return posts on or after ``now - max_post_age_days`` AND post-
    filter any returned post whose parsable ``post_date`` is older.
    Posts with no parsable date are kept (we'd rather keep a borderline
    match than drop a real signal whose date the LLM didn't include).

    Returns an empty ``DiscoveryResult`` on any failure — callers treat
    empty as soft-fail and move on."""
    if not settings.ANTHROPIC_API_KEY:
        return DiscoveryResult()
    prompt_template = _PROMPT_BY_SOURCE.get(source)
    if prompt_template is None:
        logger.warning("discover_posts: unknown source %r", source)
        return DiscoveryResult()

    today = datetime.now(timezone.utc)
    cutoff = today - timedelta(days=max(1, max_post_age_days))
    prompt = prompt_template.format(
        query=query.strip(),
        max_results=max_results,
        exclude_block=_format_exclude(exclude_urls),
        cutoff_iso=cutoff.strftime("%Y-%m-%d"),
        max_post_age_days=max_post_age_days,
        today_iso=today.strftime("%Y-%m-%d"),
        platform_name=_PLATFORM_NAME_BY_SOURCE.get(source, source),
    )

    try:
        message = await get_client(await ambient_api_key("anthropic")).messages.create(
            model=_source_model(source),
            max_tokens=4000,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": _source_max_uses(source),
            }],
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("discover_posts Anthropic call failed for %r: %s", query, e)
        return DiscoveryResult(error=_short_error(e))

    actual_cost = message_cost_usd(message, _source_model(source))
    parsed = parse_json_object(extract_text(message)) or {}
    raw_posts = parsed.get("posts") if isinstance(parsed, dict) else None
    if not isinstance(raw_posts, list):
        return DiscoveryResult(cost_usd=actual_cost)

    excluded = {u.strip() for u in (exclude_urls or []) if isinstance(u, str)}
    result = DiscoveryResult(raw=len(raw_posts), cost_usd=actual_cost)
    seen_urls: set[str] = set()
    for entry in raw_posts:
        if not isinstance(entry, dict):
            result.dropped_invalid_url += 1
            continue
        url = entry.get("post_url")
        if not _looks_like_post_url(url, source):
            result.dropped_invalid_url += 1
            continue
        canonical = url.strip()
        if canonical in excluded:
            result.dropped_excluded += 1
            continue
        if canonical in seen_urls:
            result.dropped_duplicate += 1
            continue
        seen_urls.add(canonical)
        post_date = _parse_iso(entry.get("post_date"))
        # Twitter snowflake fallback: if Anthropic didn't extract a
        # date but the URL is a /status/<id>, decode the timestamp
        # from the snowflake directly.  This is exact, not a guess.
        if post_date is None and source == "twitter":
            post_date = _twitter_id_to_date(canonical)

        # Freshness gate.  STRICT (drop undated) for LinkedIn since
        # undated LinkedIn results are usually stale SEO articles.
        # LENIENT (keep undated) for Reddit/Twitter: their URL
        # patterns are intrinsically time-ordered and web-search
        # ranking surfaces fresh first, so an undated result is
        # almost always recent.  When we have a date and it's past
        # the cutoff, drop it regardless of source.
        if post_date is not None and post_date < cutoff:
            result.dropped_stale += 1
            continue
        if post_date is None:
            if source in STRICT_DATE_SOURCES:
                result.dropped_undated += 1
                continue
            # Lenient: keep but record the separate counter.
            result.kept_undated += 1
        result.posts.append(DiscoveredPost(
            post_url=canonical,
            provider_post_id=_extract_urn(canonical, source),
            author_name=entry.get("author_name") or None,
            author_profile_url=entry.get("author_profile_url") or None,
            author_headline=entry.get("author_headline") or None,
            company_name=entry.get("company_name") or None,
            post_text=(entry.get("post_text") or "")[:5000],
            post_date=post_date,
            raw=entry,
        ))
        if len(result.posts) >= max_results:
            break
    return result
