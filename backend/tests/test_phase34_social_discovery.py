"""Unit tests for ``services/social_listening_discovery.py``.

Asserts: the call uses Anthropic web_search; bad URLs are filtered;
hallucinated post URLs without ``linkedin.com/posts/`` or
``/feed/update/`` are dropped; deduplication on URL; iso date parsing;
empty list on Anthropic failure.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import _anthropic, social_listening_discovery


def _ant(body: str) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=body)])


@pytest.fixture(autouse=True)
def _reset_client():
    _anthropic._client = None
    yield
    _anthropic._client = None


async def test_discover_posts_extracts_valid_linkedin_urls():
    # Post dates must stay inside the default 30-day lookback relative to
    # the real "now" (hardcoded dates rotted stale once the calendar moved
    # past them — same idiom as the other tests in this file).
    from datetime import datetime, timedelta, timezone
    fresh1 = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    fresh2 = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    mock = AsyncMock(return_value=_ant(f"""
    {{
      "posts": [
        {{
          "post_url": "https://www.linkedin.com/posts/jane-doe-a5898840a_msp-rant-activity-1234",
          "author_name": "Jane Doe",
          "author_headline": "CFO at Acme",
          "company_name": "Acme",
          "post_text": "Our MSP is impossible to reach.",
          "post_date": "{fresh1}"
        }},
        {{
          "post_url": "https://www.linkedin.com/feed/update/urn:li:activity:7466191548230303745",
          "author_name": "Bob Smith",
          "author_headline": "IT Director",
          "company_name": "Beta",
          "post_text": "Looking for a new phone system, recommendations?",
          "post_date": "{fresh2}"
        }}
      ]
    }}
    """))
    with patch.object(social_listening_discovery, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=mock))):
        out = await social_listening_discovery.discover_posts(
            query="frustrated with our MSP", max_results=10,
        )

    assert len(out.posts) == 2
    assert out.posts[0].author_name == "Jane Doe"
    assert out.posts[0].post_text.startswith("Our MSP")
    assert out.posts[1].provider_post_id == "urn:li:activity:7466191548230303745"
    # The slug-based URL doesn't carry the URN, so provider_post_id is None.
    assert out.posts[0].provider_post_id is None
    # DiscoveryResult also carries per-drop counters; nothing was dropped here.
    assert out.raw == 2
    assert out.kept == 2


async def test_discover_posts_filters_hallucinated_urls():
    """Anthropic occasionally hallucinates URLs.  Anything that doesn't
    look like a LinkedIn post URL must be dropped."""
    from datetime import datetime, timezone
    fresh = datetime.now(timezone.utc).isoformat()
    mock = AsyncMock(return_value=_ant(f"""
    {{
      "posts": [
        {{"post_url": "https://www.linkedin.com/in/jane-doe", "post_text": "profile not a post", "post_date": "{fresh}"}},
        {{"post_url": "https://example.com/blog/rant", "post_text": "off-platform", "post_date": "{fresh}"}},
        {{"post_url": "https://www.linkedin.com/posts/real-post-activity-1", "post_text": "ok", "post_date": "{fresh}"}}
      ]
    }}
    """))
    with patch.object(social_listening_discovery, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=mock))):
        out = await social_listening_discovery.discover_posts("q", max_results=10)
    assert len(out.posts) == 1
    assert out.posts[0].post_url.endswith("real-post-activity-1")
    # Two URLs were rejected as invalid (profile + off-platform).
    assert out.dropped_invalid_url == 2


async def test_discover_posts_dedupes_on_url():
    from datetime import datetime, timezone
    fresh = datetime.now(timezone.utc).isoformat()
    mock = AsyncMock(return_value=_ant(f"""
    {{"posts": [
        {{"post_url": "https://www.linkedin.com/posts/x-activity-1", "post_text": "a", "post_date": "{fresh}"}},
        {{"post_url": "https://www.linkedin.com/posts/x-activity-1", "post_text": "duplicate", "post_date": "{fresh}"}}
    ]}}
    """))
    with patch.object(social_listening_discovery, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=mock))):
        out = await social_listening_discovery.discover_posts("q", max_results=10)
    assert len(out.posts) == 1
    # One URL was deduplicated within the response.
    assert out.dropped_duplicate == 1


async def test_discover_posts_returns_empty_on_anthropic_failure():
    failing = AsyncMock(side_effect=RuntimeError("503"))
    with patch.object(social_listening_discovery, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=failing))):
        out = await social_listening_discovery.discover_posts("q", max_results=10)
    assert out.posts == []
    assert out.raw == 0
    # The exception message lands on the result so the worker can
    # surface it to the user instead of silently returning 0.
    assert out.error and "503" in out.error


async def test_discover_posts_twitter_keeps_undated_results():
    """Twitter undated posts pass the lenient gate (snowflake decoder
    fails for invalid IDs but the URL pattern is still time-ordered).
    Reddit no longer goes through this code path — it routes to
    ``social_listening_reddit_api.discover_reddit_posts`` instead."""
    mock = AsyncMock(return_value=_ant("""
    {"posts": [
        {"post_url": "https://twitter.com/jane/status/notanumber",
         "post_text": "fed up with our msp"}
    ]}
    """))
    with patch.object(social_listening_discovery, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=mock))):
        out = await social_listening_discovery.discover_posts(
            "msp rant", source="twitter", max_results=10,
        )
    # Twitter undated post is KEPT under the new lenient gate.
    assert len(out.posts) == 1
    assert out.kept_undated == 1
    assert out.dropped_undated == 0


async def test_discover_posts_linkedin_still_drops_undated():
    """LinkedIn keeps the STRICT date gate — undated LinkedIn results
    are usually stale SEO content, so we still drop them."""
    mock = AsyncMock(return_value=_ant("""
    {"posts": [
        {"post_url": "https://www.linkedin.com/pulse/something",
         "post_text": "undated linkedin article"}
    ]}
    """))
    with patch.object(social_listening_discovery, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=mock))):
        out = await social_listening_discovery.discover_posts(
            "msp", source="linkedin", max_results=10,
        )
    assert out.posts == []
    assert out.dropped_undated == 1


async def test_discover_posts_twitter_decodes_date_from_snowflake_id():
    """Twitter snowflake IDs encode milliseconds since 2010-11-04.
    When Anthropic doesn't extract a date, we decode it from the
    /status/<id> URL directly — no guessing."""
    from datetime import datetime, timezone
    # A real-looking modern snowflake ID (epoch + a few years of ms).
    # 1788000000 seconds after Twitter epoch = ~mid-2056-ish? doesn't
    # matter — what matters is the decoder produces SOMETHING valid.
    mock = AsyncMock(return_value=_ant("""
    {"posts": [
        {"post_url": "https://twitter.com/jane/status/1788000000000000000",
         "post_text": "switching providers"}
    ]}
    """))
    with patch.object(social_listening_discovery, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=mock))):
        out = await social_listening_discovery.discover_posts(
            "x", source="twitter",
            max_results=10, max_post_age_days=36500,  # disable freshness
        )
    assert len(out.posts) == 1
    assert out.posts[0].post_date is not None
    # The decoded date should be a reasonable Twitter-era timestamp.
    assert out.posts[0].post_date.tzinfo is timezone.utc
    # snowflake → date is deterministic, so check it's after Twitter's epoch.
    assert out.posts[0].post_date.year >= 2011


async def test_discover_posts_extracts_anthropic_billing_message():
    """Anthropic billing/auth errors come back as a 400 with a nested
    JSON body.  ``_short_error`` plucks the readable ``message`` so the
    UI shows 'Your credit balance is too low...' instead of the raw
    400 envelope.  Tested via the LinkedIn/Twitter path (Reddit no
    longer goes through Anthropic)."""
    billing_str = (
        "Error code: 400 - {'type': 'error', 'error': "
        "{'type': 'invalid_request_error', "
        "'message': 'Your credit balance is too low to access the Anthropic API.'}}"
    )
    failing = AsyncMock(side_effect=Exception(billing_str))
    with patch.object(social_listening_discovery, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=failing))):
        out = await social_listening_discovery.discover_posts(
            "q", source="twitter", max_results=5,
        )
    assert out.error == "Your credit balance is too low to access the Anthropic API."


async def test_discover_posts_uses_web_search_tool():
    """Asserting the web_search_20250305 tool is included in the call —
    if someone refactors the prompt and forgets the tool, the feature is
    effectively broken."""
    captured = {}

    async def _spy(**kwargs):
        captured["kwargs"] = kwargs
        return _ant('{"posts": []}')

    with patch.object(social_listening_discovery, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=_spy))):
        await social_listening_discovery.discover_posts("frustrated", max_results=5)

    tools = captured["kwargs"].get("tools")
    assert isinstance(tools, list) and len(tools) == 1
    assert tools[0]["type"] == "web_search_20250305"


async def test_discover_posts_twitter_uses_haiku_for_cost():
    """Twitter goes through Anthropic web_search — it uses the cheap
    Haiku model.  (Reddit no longer goes through Anthropic at all; it
    uses the direct Reddit RSS endpoint.)"""
    from app.config import settings as app_settings
    for src in ("twitter",):
        captured = {}

        async def _spy(**kwargs):
            captured["kwargs"] = kwargs
            return _ant('{"posts": []}')

        with patch.object(social_listening_discovery, "get_client",
                          return_value=SimpleNamespace(messages=SimpleNamespace(create=_spy))):
            await social_listening_discovery.discover_posts(
                "q", source=src, max_results=5,
            )

        assert captured["kwargs"]["model"] == app_settings.ANTHROPIC_SOCIAL_DISCOVERY_MODEL
        assert "haiku" in captured["kwargs"]["model"].lower()
        assert captured["kwargs"]["tools"][0]["max_uses"] == app_settings.SOCIAL_DISCOVERY_WEB_SEARCH_MAX_USES


async def test_discover_posts_linkedin_uses_sonnet():
    """LinkedIn is the hardest source to mine — Sonnet has better recall
    on buried indexed content.  But ``max_uses`` is hard-capped at 1 (one
    focused site:linkedin.com query is enough; more attempts just spend
    tokens without finding posts because the platform blocks indexing)."""
    from app.config import settings as app_settings
    captured = {}

    async def _spy(**kwargs):
        captured["kwargs"] = kwargs
        return _ant('{"posts": []}')

    with patch.object(social_listening_discovery, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=_spy))):
        await social_listening_discovery.discover_posts(
            "frustrated", source="linkedin", max_results=5,
        )

    assert captured["kwargs"]["model"] == app_settings.LINKEDIN_DISCOVERY_MODEL
    assert "sonnet" in captured["kwargs"]["model"].lower()
    # Hard cap of 1 — see _source_max_uses().
    assert captured["kwargs"]["tools"][0]["max_uses"] == 1


async def test_discover_posts_linkedin_prompt_uses_site_filter():
    """The LinkedIn prompt MUST include the canonical site:linkedin.com
    filter so the single web_search call targets the right pages.
    Without the site filter the model wastes its budget on noise pages."""
    captured = {}

    async def _spy(**kwargs):
        captured["kwargs"] = kwargs
        return _ant('{"posts": []}')

    with patch.object(social_listening_discovery, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=_spy))):
        await social_listening_discovery.discover_posts(
            "frustrated msp", source="linkedin", max_results=5,
        )
    prompt = captured["kwargs"]["messages"][0]["content"]
    assert "site:linkedin.com/pulse" in prompt
    assert "site:linkedin.com/posts" in prompt
    # Old multi-strategy advice removed (we have ONE call).
    assert "Try MULTIPLE strategies" not in prompt


async def test_discover_posts_linkedin_max_uses_is_one_even_when_setting_higher():
    """Even if someone bumps ``LINKEDIN_DISCOVERY_WEB_SEARCH_MAX_USES`` via
    config, the actual max_uses sent to Anthropic stays at 1 — the cap is
    intentional, not a default."""
    from app.config import settings as app_settings
    captured = {}

    async def _spy(**kwargs):
        captured["kwargs"] = kwargs
        return _ant('{"posts": []}')

    with patch.object(app_settings, "LINKEDIN_DISCOVERY_WEB_SEARCH_MAX_USES", 9), \
         patch.object(social_listening_discovery, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=_spy))):
        await social_listening_discovery.discover_posts(
            "x", source="linkedin", max_results=3,
        )
    assert captured["kwargs"]["tools"][0]["max_uses"] == 1


async def test_discover_posts_linkedin_accepts_pulse_urls():
    """LinkedIn Pulse articles (long-form posts at /pulse/) are indexed
    and often carry buying-intent signal.  They count as valid LinkedIn
    posts for our purposes."""
    from datetime import datetime, timezone
    fresh = datetime.now(timezone.utc).isoformat()
    mock = AsyncMock(return_value=_ant(f"""
    {{"posts": [
        {{"post_url": "https://www.linkedin.com/pulse/our-msp-disaster-jane-doe",
          "post_text": "long-form rant", "post_date": "{fresh}"}}
    ]}}
    """))
    with patch.object(social_listening_discovery, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=mock))):
        out = await social_listening_discovery.discover_posts(
            "msp", source="linkedin", max_results=5,
        )
    assert len(out.posts) == 1
    assert "/pulse/" in out.posts[0].post_url


async def test_discover_posts_caps_at_max_results():
    from datetime import datetime, timezone
    fresh = datetime.now(timezone.utc).isoformat()
    posts = ", ".join(
        f'{{"post_url": "https://www.linkedin.com/posts/x-activity-{i}", "post_text": "p{i}", "post_date": "{fresh}"}}'
        for i in range(20)
    )
    mock = AsyncMock(return_value=_ant(f'{{"posts": [{posts}]}}'))
    with patch.object(social_listening_discovery, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=mock))):
        out = await social_listening_discovery.discover_posts("q", max_results=5)
    assert len(out.posts) == 5


async def test_discover_posts_rejects_unknown_source():
    out = await social_listening_discovery.discover_posts("q", source="bluesky", max_results=5)
    assert out.posts == []


async def test_discover_posts_reddit_routes_to_reddit_api_not_anthropic():
    """Reddit discovery used to go through Anthropic web_search but
    that returned ~0 results consistently (Brave deprioritizes Reddit).
    Now it routes to ``social_listening_reddit_api.discover_reddit_posts``
    which hits Reddit's RSS endpoint directly — free + much higher
    recall.  This test asserts Anthropic is NOT called for source=reddit."""
    anthropic_mock = AsyncMock(return_value=_ant('{"posts": []}'))

    async def _fake_reddit(query, **kwargs):
        from app.services.social_listening_discovery import DiscoveredPost, DiscoveryResult
        from datetime import datetime, timezone
        return DiscoveryResult(
            raw=1,
            posts=[DiscoveredPost(
                post_url="https://www.reddit.com/r/sysadmin/comments/abc/x/",
                post_text="rant", post_date=datetime.now(timezone.utc),
            )],
        )

    with patch.object(social_listening_discovery, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=anthropic_mock))), \
         patch("app.services.social_listening_reddit_api.discover_reddit_posts",
               new=_fake_reddit):
        out = await social_listening_discovery.discover_posts(
            "msp rant", source="reddit", max_results=10,
        )

    assert len(out.posts) == 1
    # Critical: Anthropic NOT called for source=reddit.
    anthropic_mock.assert_not_called()


async def test_discover_posts_twitter_source_accepts_status_urls():
    """Twitter/X posts must have /status/<id>; bare profile URLs are dropped."""
    from datetime import datetime, timezone
    fresh = datetime.now(timezone.utc).isoformat()
    mock = AsyncMock(return_value=_ant(f"""
    {{"posts": [
        {{"post_url": "https://twitter.com/janedoe/status/1234567890",
          "post_text": "switching from ringcentral after another outage",
          "post_date": "{fresh}"}},
        {{"post_url": "https://x.com/bobsmith/status/9876543210",
          "post_text": "need recommendations for a phone system",
          "post_date": "{fresh}"}},
        {{"post_url": "https://twitter.com/janedoe",
          "post_text": "this is a profile not a tweet",
          "post_date": "{fresh}"}}
    ]}}
    """))
    with patch.object(social_listening_discovery, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=mock))):
        out = await social_listening_discovery.discover_posts(
            "phone system", source="twitter", max_results=10,
        )

    urls = {p.post_url for p in out.posts}
    assert "https://twitter.com/janedoe/status/1234567890" in urls
    assert "https://x.com/bobsmith/status/9876543210" in urls
    # Profile URL (no /status/) was dropped.
    assert "https://twitter.com/janedoe" not in urls
    assert out.dropped_invalid_url == 1
    # IDs extracted from /status/<id>.
    ids = {p.provider_post_id for p in out.posts}
    assert ids == {"1234567890", "9876543210"}


async def test_discover_posts_linkedin_source_still_uses_linkedin_url_filter():
    """Sanity: passing source='linkedin' rejects reddit URLs."""
    from datetime import datetime, timezone
    fresh = datetime.now(timezone.utc).isoformat()
    mock = AsyncMock(return_value=_ant(f"""
    {{"posts": [
        {{"post_url": "https://www.reddit.com/r/sysadmin/comments/abc/x/",
          "post_text": "wrong platform", "post_date": "{fresh}"}},
        {{"post_url": "https://www.linkedin.com/posts/jane-activity-1",
          "post_text": "right platform", "post_date": "{fresh}"}}
    ]}}
    """))
    with patch.object(social_listening_discovery, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=mock))):
        out = await social_listening_discovery.discover_posts(
            "q", source="linkedin", max_results=10,
        )
    assert len(out.posts) == 1
    assert "linkedin.com" in out.posts[0].post_url
    assert out.dropped_invalid_url == 1


async def test_discover_posts_excludes_already_seen_urls_via_prompt_and_filter():
    """exclude_urls is both (a) injected into the prompt as a "do not
    return these" instruction AND (b) post-filtered defensively in case
    the model ignores it."""
    from datetime import datetime, timezone
    fresh = datetime.now(timezone.utc).isoformat()
    captured = {}

    async def _spy(**kwargs):
        captured["kwargs"] = kwargs
        # Model returns 2 posts — one is in the exclude list, the other isn't.
        return _ant(f"""
        {{"posts": [
            {{"post_url": "https://www.linkedin.com/posts/already-seen-activity-1", "post_text": "stale", "post_date": "{fresh}"}},
            {{"post_url": "https://www.linkedin.com/posts/brand-new-activity-2", "post_text": "fresh", "post_date": "{fresh}"}}
        ]}}
        """)

    seen = [
        "https://www.linkedin.com/posts/already-seen-activity-1",
        "https://www.linkedin.com/posts/another-known-activity-3",
    ]
    with patch.object(social_listening_discovery, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=_spy))):
        out = await social_listening_discovery.discover_posts(
            "q", max_results=10, exclude_urls=seen,
        )

    # Post-filter dropped the already-seen URL even though the model returned it.
    assert len(out.posts) == 1
    assert out.posts[0].post_url.endswith("brand-new-activity-2")
    assert out.dropped_excluded == 1

    # Prompt contains both excluded URLs so future runs nudge the model away.
    prompt = captured["kwargs"]["messages"][0]["content"]
    assert "already-seen-activity-1" in prompt
    assert "another-known-activity-3" in prompt
    assert "DO NOT include" in prompt


async def test_discover_posts_drops_stale_and_undated_posts():
    """The lookback window (max_post_age_days) is strictly enforced.
    Anthropic web_search returns a lot of undated SEO/listicle content
    that turns out to be ancient when checked; we'd rather drop those
    than risk passing them downstream to qualification (which is what
    actually costs money)."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    fresh_iso = (now - timedelta(days=5)).isoformat()
    stale_iso = (now - timedelta(days=120)).isoformat()

    mock = AsyncMock(return_value=_ant(f"""
    {{"posts": [
        {{"post_url": "https://www.linkedin.com/posts/fresh-activity-1",
          "post_date": "{fresh_iso}", "post_text": "fresh"}},
        {{"post_url": "https://www.linkedin.com/posts/stale-activity-2",
          "post_date": "{stale_iso}", "post_text": "stale"}},
        {{"post_url": "https://www.linkedin.com/posts/undated-activity-3",
          "post_text": "no date"}}
    ]}}
    """))
    with patch.object(social_listening_discovery, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=mock))):
        out = await social_listening_discovery.discover_posts(
            "q", max_results=10, max_post_age_days=30,
        )

    urls = {p.post_url for p in out.posts}
    # Only the dated-fresh post survives.
    assert urls == {"https://www.linkedin.com/posts/fresh-activity-1"}
    # Per-drop counters explain WHY each was dropped.
    assert out.dropped_stale == 1
    assert out.dropped_undated == 1


async def test_discover_posts_prompt_mentions_lookback_cutoff_date():
    """The prompt embeds the cutoff date so the LLM doesn't even surface
    too-old posts."""
    from datetime import datetime, timedelta, timezone
    captured = {}

    async def _spy(**kwargs):
        captured["kwargs"] = kwargs
        return _ant('{"posts": []}')

    expected_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    with patch.object(social_listening_discovery, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=_spy))):
        await social_listening_discovery.discover_posts(
            "q", max_results=5, max_post_age_days=7,
        )

    prompt = captured["kwargs"]["messages"][0]["content"]
    assert expected_cutoff in prompt
    assert "within the last 7 days" in prompt


async def test_discover_posts_caps_exclude_block_at_200_urls():
    """Long-running searches can accumulate thousands of URLs — we don't
    want to balloon the prompt.  Caller passes 500, prompt mentions 200."""
    captured = {}

    async def _spy(**kwargs):
        captured["kwargs"] = kwargs
        return _ant('{"posts": []}')

    many = [f"https://www.linkedin.com/posts/url-activity-{i}" for i in range(500)]
    with patch.object(social_listening_discovery, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=_spy))):
        await social_listening_discovery.discover_posts(
            "q", max_results=10, exclude_urls=many,
        )

    prompt = captured["kwargs"]["messages"][0]["content"]
    # First 200 URLs are in the prompt; the 201st onwards are NOT.
    assert "url-activity-0" in prompt
    assert "url-activity-199" in prompt
    assert "url-activity-200" not in prompt
    assert "url-activity-499" not in prompt
