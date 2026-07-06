"""Tests for the direct Reddit RSS discovery service.

Anthropic's web_search returns ~0 Reddit results, so we hit Reddit's
RSS endpoint directly.  These tests mock httpx so they're hermetic.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services import social_listening_reddit_api as r_api
from app.services.social_listening_discovery import discover_posts


def _atom_feed(entries: list[dict]) -> str:
    """Build a minimal Atom feed matching Reddit's RSS shape."""
    body = "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
    body += '<feed xmlns="http://www.w3.org/2005/Atom">'
    for e in entries:
        body += "<entry>"
        body += f"<title>{e.get('title', '')}</title>"
        body += f'<link href="{e["url"]}"/>'
        body += f"<author><name>{e.get('author', 'someuser')}</name></author>"
        body += f"<content type=\"html\">{e.get('content', '')}</content>"
        body += f"<published>{e.get('published')}</published>"
        body += f"<updated>{e.get('updated', e.get('published'))}</updated>"
        body += "</entry>"
    body += "</feed>"
    return body


def _resp(body: str, status: int = 200) -> MagicMock:
    m = MagicMock(spec=httpx.Response)
    m.status_code = status
    m.text = body
    m.content = body.encode("utf-8")
    return m


async def test_reddit_api_parses_real_posts():
    """Happy path: Reddit RSS returns entries with /comments/<id>/ URLs,
    we extract title + body + date + subreddit."""
    now = datetime.now(timezone.utc)
    feed = _atom_feed([
        {
            "url": "https://www.reddit.com/r/sysadmin/comments/abc123/our_msp_disaster/",
            "title": "Our MSP disaster",
            "author": "burned-out-it",
            "content": "submitted by /u/burned-out-it to /r/sysadmin&lt;br&gt;Our MSP keeps missing tickets...",
            "published": (now - timedelta(days=3)).isoformat(),
        },
        {
            "url": "https://www.reddit.com/r/msp/comments/def456/voip_dropped_calls/",
            "title": "Voip dropping calls",
            "author": "anotheruser",
            "content": "submitted by /u/anotheruser to /r/msp&lt;br&gt;Our voip system...",
            "published": (now - timedelta(days=1)).isoformat(),
        },
    ])

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=_resp(feed))

    with patch.object(httpx, "AsyncClient", return_value=mock_client):
        out = await r_api.discover_reddit_posts(
            "msp slow response", max_results=10, max_post_age_days=30,
        )

    assert out.raw == 2
    assert out.kept == 2
    assert out.dropped_stale == 0
    # First post's metadata pulled correctly.
    p = out.posts[0]
    assert "/r/sysadmin/comments/abc123/" in p.post_url
    assert p.provider_post_id == "abc123"
    assert p.author_name == "burned-out-it"
    assert p.author_headline == "r/sysadmin"
    assert p.author_profile_url == "https://www.reddit.com/user/burned-out-it"
    # The "submitted by /u/x" preamble is stripped.
    assert p.post_text.startswith("Our MSP disaster")
    assert "submitted by" not in p.post_text


async def test_reddit_api_drops_stale_posts():
    """Posts older than max_post_age_days are dropped."""
    now = datetime.now(timezone.utc)
    feed = _atom_feed([
        {"url": "https://www.reddit.com/r/sysadmin/comments/fresh/x/",
         "title": "fresh", "published": (now - timedelta(days=3)).isoformat()},
        {"url": "https://www.reddit.com/r/sysadmin/comments/stale/x/",
         "title": "stale", "published": (now - timedelta(days=120)).isoformat()},
    ])

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=_resp(feed))

    with patch.object(httpx, "AsyncClient", return_value=mock_client):
        out = await r_api.discover_reddit_posts(
            "anything", max_results=10, max_post_age_days=30,
        )

    assert out.kept == 1
    assert out.dropped_stale == 1
    assert "fresh" in out.posts[0].post_url


async def test_reddit_api_dedupes_and_excludes_urls():
    """Excluded URLs (previously seen) + duplicate URLs in one feed are dropped."""
    now = datetime.now(timezone.utc).isoformat()
    feed = _atom_feed([
        {"url": "https://www.reddit.com/r/sysadmin/comments/known/x/", "published": now},
        {"url": "https://www.reddit.com/r/sysadmin/comments/new1/x/", "published": now},
        {"url": "https://www.reddit.com/r/sysadmin/comments/new1/x/", "published": now},  # dup
    ])
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=_resp(feed))

    with patch.object(httpx, "AsyncClient", return_value=mock_client):
        out = await r_api.discover_reddit_posts(
            "x", max_results=10, max_post_age_days=30,
            exclude_urls=["https://www.reddit.com/r/sysadmin/comments/known/x/"],
        )
    assert out.kept == 1
    assert out.dropped_excluded == 1
    assert out.dropped_duplicate == 1


async def test_reddit_api_surfaces_http_error():
    """A 403/429 from Reddit lands on ``error`` instead of failing silently."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=_resp("forbidden", status=403))

    with patch.object(httpx, "AsyncClient", return_value=mock_client):
        out = await r_api.discover_reddit_posts("x", max_results=10)
    assert out.posts == []
    assert "403" in (out.error or "")


async def test_discover_posts_dispatches_to_reddit_api_for_source_reddit():
    """The front-door ``discover_posts`` routes source=reddit to the new
    Reddit-RSS service (NOT to Anthropic web_search)."""
    called = {}

    async def _fake_reddit(query, **kwargs):
        called["query"] = query
        called["kwargs"] = kwargs
        from app.services.social_listening_discovery import DiscoveryResult
        return DiscoveryResult(raw=1)

    with patch(
        "app.services.social_listening_reddit_api.discover_reddit_posts",
        new=_fake_reddit,
    ):
        out = await discover_posts(
            "msp slow", source="reddit", max_results=10,
            max_post_age_days=14, exclude_urls=["foo"],
        )

    assert out.raw == 1
    assert called["query"] == "msp slow"
    assert called["kwargs"]["max_results"] == 10
    assert called["kwargs"]["max_post_age_days"] == 14
    assert called["kwargs"]["exclude_urls"] == ["foo"]
