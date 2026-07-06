"""Direct-from-Reddit discovery — bypasses Anthropic entirely.

Anthropic's ``web_search_20250305`` tool (Brave under the hood) returns
~0 Reddit results because Brave deprioritizes Reddit pages.  Reddit's
anonymous JSON API is now 403'd too.  But the **RSS / Atom endpoint
at ``reddit.com/search.rss``** still works without auth and returns
real posts with real timestamps.

This service hits the RSS endpoint directly per query, parses the
Atom XML, and maps each entry into a ``DiscoveryResult.posts`` row —
same downstream shape as Anthropic-discovered posts, but $0 cost and
much higher recall.

Trade-offs:
- Reddit rate-limits anonymous traffic at roughly 60 requests/minute.
  We make one call per query, so well within budget.
- RSS returns at most ~25 entries per query.  For our use case
  (typically 20 queries × 25 = up to 500 candidates per run) this is
  plenty.
- ``<content>`` is HTML-escaped and includes a "submitted by /u/x" preamble.
  Extracted as best-effort post text — not always the full body but
  enough for the qualifier to score on.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

import httpx

from app.services.social_listening_discovery import DiscoveredPost, DiscoveryResult

logger = logging.getLogger(__name__)


_REDDIT_RSS_URL = "https://www.reddit.com/search.rss"
# Reddit blocks bot-y User-Agents but their TOS accepts traffic via a
# realistic browser UA on RSS endpoints.  No auth needed.
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# Atom namespace used in Reddit's RSS.
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


def _strip_html(text: str) -> str:
    """Atom ``<content>`` is HTML-escaped.  Pull a readable plain-text
    body for the qualifier (best-effort)."""
    if not text:
        return ""
    # Unescape entities, strip tags.
    s = html.unescape(text)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        cleaned = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _extract_post_id(url: str) -> str | None:
    """Reddit post URLs look like ``/r/<sub>/comments/<id>/<slug>/``.
    Pull the ``<id>``."""
    if "/comments/" not in url:
        return None
    tail = url.split("/comments/", 1)[1]
    return tail.split("/", 1)[0] or None


def _extract_subreddit(url: str) -> str | None:
    m = re.search(r"/r/([A-Za-z0-9_]+)/", url)
    return m.group(1) if m else None


async def discover_reddit_posts(
    query: str,
    *,
    max_results: int = 25,
    exclude_urls: list[str] | None = None,
    max_post_age_days: int = 30,
) -> DiscoveryResult:
    """Find Reddit posts matching ``query`` via the public RSS endpoint.

    Drop-in replacement for ``social_listening_discovery.discover_posts``
    when ``source == "reddit"`` — returns the same DiscoveryResult shape
    with ``cost_usd=0`` (Reddit RSS is free).
    """
    if not query or not query.strip():
        return DiscoveryResult()

    # NOTE: do NOT add ``t=month`` — Reddit's RSS treats that as a
    # search-side filter that ANDs with the query and very frequently
    # returns an empty feed (0 entries) even for popular queries.
    # ``sort=new`` alone gives us newest-first results; our own
    # ``max_post_age_days`` filter then drops anything past the cutoff.
    params = {
        "q": query.strip(),
        "sort": "new",
        "type": "link",
    }
    excluded = {u.strip() for u in (exclude_urls or []) if isinstance(u, str)}
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, max_post_age_days))

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(
                _REDDIT_RSS_URL, params=params, headers={"User-Agent": _UA},
            )
    except httpx.HTTPError as e:
        logger.warning("reddit RSS network error for %r: %s", query, e)
        return DiscoveryResult(error=f"Reddit RSS network error: {e}")

    if resp.status_code != 200:
        logger.warning("reddit RSS %d for %r: %s", resp.status_code, query, resp.text[:200])
        return DiscoveryResult(
            error=f"Reddit RSS returned HTTP {resp.status_code}",
        )

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        return DiscoveryResult(error=f"Reddit RSS parse error: {e}")

    entries = root.findall("atom:entry", _ATOM_NS)
    result = DiscoveryResult(raw=len(entries))
    seen: set[str] = set()
    for entry in entries:
        link_el = entry.find("atom:link", _ATOM_NS)
        url = (link_el.get("href") if link_el is not None else "") or ""
        if not url or "/comments/" not in url:
            result.dropped_invalid_url += 1
            continue
        if url in excluded:
            result.dropped_excluded += 1
            continue
        if url in seen:
            result.dropped_duplicate += 1
            continue
        seen.add(url)

        # Date: prefer <published> (post creation), fallback to <updated>.
        pub_el = entry.find("atom:published", _ATOM_NS)
        upd_el = entry.find("atom:updated", _ATOM_NS)
        post_date = (
            _parse_iso(pub_el.text if pub_el is not None else None)
            or _parse_iso(upd_el.text if upd_el is not None else None)
        )

        if post_date is not None and post_date < cutoff:
            result.dropped_stale += 1
            continue

        title_el = entry.find("atom:title", _ATOM_NS)
        author_el = entry.find("atom:author/atom:name", _ATOM_NS)
        content_el = entry.find("atom:content", _ATOM_NS)

        title = (title_el.text or "") if title_el is not None else ""
        body = _strip_html((content_el.text or "") if content_el is not None else "")
        # Reddit's RSS content always starts with "submitted by /u/x to /r/y" —
        # strip that boilerplate to give the qualifier the actual post.
        body = re.sub(r"^submitted by /u/\S+\s+(to /r/\S+\s+)?", "", body)
        post_text = (title + "\n\n" + body) if title else body

        result.posts.append(DiscoveredPost(
            post_url=url,
            provider_post_id=_extract_post_id(url),
            post_text=post_text[:5000],
            post_date=post_date,
            author_name=(author_el.text or None) if author_el is not None else None,
            author_profile_url=(
                f"https://www.reddit.com/user/{author_el.text}"
                if author_el is not None and author_el.text else None
            ),
            author_headline=(
                f"r/{_extract_subreddit(url)}" if _extract_subreddit(url) else None
            ),
            raw={"source": "reddit_rss"},
        ))
        if len(result.posts) >= max_results:
            break

    return result
