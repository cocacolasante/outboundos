"""Reddit → LinkedIn cross-link extraction.

Pure-function helpers — no DB, no Anthropic, no HTTP.  Mines Reddit
discussion bodies for ``linkedin.com/posts/...`` and
``linkedin.com/feed/update/...`` URLs and synthesizes a
``DiscoveredPost`` + ``QualificationResult`` for each.

We can't fetch the actual LinkedIn post body cheaply (Unipile exposes
only ``recent_posts(slug)`` which misses anything past the author's
last ~25 entries), so the synthesized opportunity is a placeholder:
score=5 / action=research_further / empty suggested copy.  The user
opens the URL directly to assess buying intent.  $0 Anthropic spend.

This is paired with the Reddit RSS channel — the worker invokes it on
every Reddit ``DiscoveryResult`` post when ``linkedin_crosslink_enabled``
is true on the search.
"""
from __future__ import annotations

import re

from app.models import SocialOpportunityAction, SocialOpportunityCategory
from app.services.social_listening_discovery import DiscoveredPost
from app.services.social_listening_qualifier import QualificationResult


# Two LinkedIn post-URL patterns we accept.  Profile URLs
# (``linkedin.com/in/...``) and company URLs are deliberately excluded —
# they aren't posts.  The character class on the activity URN includes
# only ASCII alnum + a few separators to stop a runaway match.
_LI_URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?linkedin\.com/"
    r"(?:posts/[\w%\-]+_[\w%\-]+|feed/update/urn:li:activity:\d+)"
    r"[\w%/\-?=&.]*",
    re.IGNORECASE,
)

# Trailing characters that often glue onto a URL inside a Reddit body
# but aren't part of the URL itself (Markdown link closers, sentence
# punctuation).  Stripped after a regex match.
_TRAILING_PUNCT = ")]},.;!?\"'"

# Markdown / inline link punctuation that's frequently embedded INSIDE
# a Reddit excerpt right before the URL — also kept out of the match.
_LEADING_PUNCT = "([{<\"'`"


def extract_linkedin_urls(post_text: str) -> list[str]:
    """Find every LinkedIn post URL embedded in ``post_text``.

    Returns the URLs in their canonical form, deduped within this
    single input.  Order-preserved by first appearance (so the same
    URL written twice in one post yields one entry, the first one)."""
    if not post_text or not isinstance(post_text, str):
        return []
    found: list[str] = []
    seen: set[str] = set()
    for match in _LI_URL_RE.finditer(post_text):
        raw = match.group(0).rstrip(_TRAILING_PUNCT).lstrip(_LEADING_PUNCT)
        # Reddit often wraps URLs in `(url)` for Markdown - strip again
        # if the leading punctuation slipped past lstrip via grouping.
        raw = raw.rstrip(_TRAILING_PUNCT)
        url = canonical_url(raw)
        if not url:
            continue
        if url in seen:
            continue
        seen.add(url)
        found.append(url)
    return found


def canonical_url(url: str) -> str:
    """Normalise a LinkedIn URL so two flavours of the same post collapse
    to one row under the existing ``UNIQUE(provider, post_url)`` index.

    - Lowercases the scheme + host (case-insensitive in DNS anyway).
    - Drops any query string and fragment.
    - Strips trailing slash.
    - Leaves the path case intact — slug case can be significant on
      some LinkedIn surfaces, better safe than sorry."""
    if not isinstance(url, str) or not url.strip():
        return ""
    s = url.strip()
    # Strip fragment first (``#``), then query (``?``).
    s = s.split("#", 1)[0]
    s = s.split("?", 1)[0]
    if "://" not in s:
        return ""
    scheme, rest = s.split("://", 1)
    if "/" not in rest:
        return ""
    host, path = rest.split("/", 1)
    canonical = f"{scheme.lower()}://{host.lower()}/{path}"
    if canonical.endswith("/"):
        canonical = canonical[:-1]
    return canonical


def extract_author_slug(url: str) -> str | None:
    """Pull the author slug out of a ``/posts/<slug>_<activity-id>`` URL.

    Returns None for ``/feed/update/...`` URLs (no slug embedded) and
    for anything that doesn't look like a LinkedIn post URL."""
    if not isinstance(url, str):
        return None
    m = re.search(
        r"linkedin\.com/posts/([\w%\-]+)_[\w%\-]+",
        url,
        re.IGNORECASE,
    )
    return m.group(1) if m else None


def extract_activity_urn(url: str) -> str | None:
    """Extract the LinkedIn activity URN if present.  Recognises both
    ``urn:li:activity:<n>`` (used on /feed/update/ URLs) and the
    ``activity-<n>`` token that LinkedIn embeds in /posts/ slugs."""
    if not isinstance(url, str):
        return None
    m = re.search(r"urn:li:activity:(\d+)", url, re.IGNORECASE)
    if m:
        return f"urn:li:activity:{m.group(1)}"
    m = re.search(r"activity[-:](\d+)", url, re.IGNORECASE)
    if m:
        return f"urn:li:activity:{m.group(1)}"
    return None


def build_synthetic_discovered_post(
    linkedin_url: str,
    reddit_post_url: str,
    reddit_excerpt: str,
) -> DiscoveredPost:
    """Wrap an extracted LinkedIn URL in the ``DiscoveredPost`` shape the
    worker's ``_upsert_post`` already accepts.

    We don't know the LinkedIn post body — only the surrounding Reddit
    discussion that surfaced it.  Store the Reddit excerpt as ``post_text``
    (capped at 1000 chars) so the UI has SOMETHING to show when a user
    hovers; the user clicks through to the LinkedIn URL for the real
    content.  ``raw`` carries the cross-link provenance for analytics."""
    canonical = canonical_url(linkedin_url)
    slug = extract_author_slug(canonical)
    return DiscoveredPost(
        post_url=canonical,
        post_text=(reddit_excerpt or "")[:1000],
        provider_post_id=extract_activity_urn(canonical),
        author_name=None,
        author_profile_url=(
            f"https://www.linkedin.com/in/{slug}" if slug else None
        ),
        author_headline=None,
        company_name=None,
        post_date=None,
        raw={
            "source": "reddit_crosslink",
            "reddit_url": reddit_post_url,
        },
    )


def build_synthetic_qualification(reddit_post_url: str) -> QualificationResult:
    """Synthesize the opportunity record for a cross-linked LinkedIn URL
    WITHOUT calling Anthropic.

    Why: we don't have the actual LinkedIn post text, only the Reddit
    excerpt that mentioned it.  Scoring the Reddit excerpt produces a
    misleading number (it's scoring the wrong document).  Instead we
    surface the URL at a neutral score (5) tagged
    ``recommended_action=research_further`` so the user manually opens
    the LinkedIn post and assesses it.

    $0 Anthropic spend, more honest about what we know.  Suggested copy
    is intentionally empty because drafting a comment without seeing
    the post body would be pure hallucination."""
    return QualificationResult(
        score=5,
        category=SocialOpportunityCategory.GENERAL_ADVISORY.value,
        buying_signal=False,
        pain_summary=(
            "Surfaced via Reddit discussion — review the LinkedIn post "
            "for full context."
        ),
        qualification_reason=(
            f"Mentioned in Reddit thread: {reddit_post_url}"
        ),
        suggested_comment="",
        suggested_connection_request="",
        suggested_follow_up="",
        recommended_action=SocialOpportunityAction.RESEARCH_FURTHER.value,
    )
