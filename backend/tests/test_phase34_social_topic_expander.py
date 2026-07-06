"""Unit tests for ``services/social_listening_topic_expander.py``.

Anthropic is mocked at the ``_anthropic.get_client`` boundary.  Tests
assert dedupe, lowercase, include/exclude keyword handling, the
max_queries cap, and the "no API key → empty fallback" path.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import _anthropic, social_listening_topic_expander


def _ant(body: str) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=body)])


@pytest.fixture(autouse=True)
def _reset_client():
    _anthropic._client = None
    yield
    _anthropic._client = None


async def test_expand_topic_returns_deduped_lowercased_queries():
    mock = AsyncMock(return_value=_ant(
        '["Frustrated with our MSP service", "frustrated with our msp service", '
        '"looking for a new phone system", "  IT vendor issues are killing us  "]'
    ))
    with patch.object(social_listening_topic_expander, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=mock))):
        out = await social_listening_topic_expander.expand_topic(
            topic="frustrated with technology",
            niche="mid-market",
            geography="east coast",
            max_queries=10,
        )
    # Deduplicated (the two "frustrated with our msp service" collapse), all
    # lowercased + whitespace-trimmed.
    assert out == [
        "frustrated with our msp service",
        "looking for a new phone system",
        "it vendor issues are killing us",
    ]
    assert mock.await_count == 1


async def test_expand_topic_caps_at_max_queries():
    # Use 4+ word phrases so the min-words filter doesn't drop them.
    mock = AsyncMock(return_value=_ant(
        '[' + ",".join(f'"looking for vendor option {i}"' for i in range(50)) + ']'
    ))
    with patch.object(social_listening_topic_expander, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=mock))):
        out = await social_listening_topic_expander.expand_topic(
            topic="X", max_queries=5,
        )
    assert len(out) == 5


async def test_expand_topic_appends_include_keywords_verbatim():
    mock = AsyncMock(return_value=_ant('["only this one ai-generated phrase"]'))
    with patch.object(social_listening_topic_expander, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=mock))):
        out = await social_listening_topic_expander.expand_topic(
            topic="X",
            include_keywords=["our IT provider", "Stop the bleeding"],
            max_queries=10,
        )
    # Include keywords come FIRST, lowercased; then the LLM output.
    assert out[0] == "our it provider"
    assert out[1] == "stop the bleeding"
    assert "only this one ai-generated phrase" in out


async def test_expand_topic_filters_out_exclude_keywords():
    mock = AsyncMock(return_value=_ant(
        '["frustrated with hubspot keeps glitching", "looking for a new MSP partner", '
        '"HubSpot is too expensive for us", "switching from our salesforce instance"]'
    ))
    with patch.object(social_listening_topic_expander, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=mock))):
        out = await social_listening_topic_expander.expand_topic(
            topic="X",
            exclude_keywords=["hubspot"],
            max_queries=10,
        )
    assert "frustrated with hubspot keeps glitching" not in out
    assert "hubspot is too expensive for us" not in out
    assert "looking for a new msp partner" in out
    assert "switching from our salesforce instance" in out


async def test_expand_topic_returns_empty_on_anthropic_failure():
    failing = AsyncMock(side_effect=RuntimeError("API down"))
    with patch.object(social_listening_topic_expander, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=failing))):
        out = await social_listening_topic_expander.expand_topic(
            topic="X", max_queries=10,
        )
    assert out == []


async def test_expand_topic_returns_empty_when_anthropic_returns_garbage():
    """Malformed JSON shouldn't crash."""
    garbage = AsyncMock(return_value=_ant("not json at all"))
    with patch.object(social_listening_topic_expander, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=garbage))):
        out = await social_listening_topic_expander.expand_topic(
            topic="X", max_queries=10,
        )
    assert out == []


async def test_expand_topic_drops_too_long_phrases_from_ai_output():
    """10+ word phrases return zero hits on Google/Brave because web
    search engines treat them as exact-match.  Server-side filter caps
    AI output at 8 words to prevent the "$20 spent, 0 raw results" mode
    we hit in production."""
    mock = AsyncMock(return_value=_ant(
        '['
        '"anyone else fed up with their msp not returning calls",'  # 10w — DROP
        '"our phone system dropped 14 calls during the meeting this morning",'  # 11w — DROP
        '"fed up with msp",'  # 4w — keep
        '"ringcentral alternatives small business",'  # 4w — keep
        '"voip dropped calls"'  # 3w — keep
        ']'
    ))
    with patch.object(social_listening_topic_expander, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=mock))):
        out = await social_listening_topic_expander.expand_topic(
            topic="x", max_queries=20,
        )
    assert "anyone else fed up with their msp not returning calls" not in out
    assert "our phone system dropped 14 calls during the meeting this morning" not in out
    assert "fed up with msp" in out
    assert "ringcentral alternatives small business" in out
    assert "voip dropped calls" in out


async def test_expand_topic_drops_short_phrases_from_ai_output():
    """Single-word and 2-word noun-phrase entries surface SEO content
    rather than real intent-posts.  Filter drops anything shorter than
    3 words.  Long (>8 word) entries return ~zero hits on Brave search,
    so they're also dropped."""
    mock = AsyncMock(return_value=_ant(
        '['
        '"internet",'  # 1 word — DROP (too short)
        '"voip",'  # 1 word — DROP
        '"phone system",'  # 2 words — DROP
        '"msp slow response",'  # 3 words — keep
        '"ringcentral alternatives small business",'  # 4 words — keep
        '"anyone else fed up with their msp not returning calls"'  # 10w — DROP (too long)
        ']'
    ))
    with patch.object(social_listening_topic_expander, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=mock))):
        out = await social_listening_topic_expander.expand_topic(
            topic="frustrated with technology", max_queries=20,
        )
    assert "internet" not in out
    assert "voip" not in out
    assert "phone system" not in out
    assert "msp slow response" in out
    assert "ringcentral alternatives small business" in out
    assert "anyone else fed up with their msp not returning calls" not in out


async def test_expand_topic_filters_short_include_keywords_too():
    """User-supplied include_keywords are ALSO subject to the min-words
    filter.  Users tend to type bare-noun overrides like "msp" / "ucaas"
    that surface SEO product pages instead of real intent posts.  3+
    word phrases pass through; shorter ones get dropped."""
    mock = AsyncMock(return_value=_ant('["frustrated with our it provider"]'))
    with patch.object(social_listening_topic_expander, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=mock))):
        out = await social_listening_topic_expander.expand_topic(
            topic="x",
            # Mix of single-word junk + good 3-4 word phrases.
            include_keywords=["msp", "ucaas", "our msp slow", "looking for new vendor"],
            max_queries=20,
        )
    assert "msp" not in out
    assert "ucaas" not in out
    assert "our msp slow" in out
    assert "looking for new vendor" in out


async def test_expand_topic_prompt_includes_niche_geography_and_extras():
    """Smoke check that the configured modifiers actually reach Anthropic."""
    captured = {}

    async def _spy(**kwargs):
        captured["kwargs"] = kwargs
        return _ant("[]")

    with patch.object(social_listening_topic_expander, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=_spy))):
        await social_listening_topic_expander.expand_topic(
            topic="frustrated with technology",
            niche="mid-market nonprofits",
            geography="northeast US",
            include_keywords=["msp"],
            exclude_keywords=["hubspot"],
            max_queries=20,
        )
    prompt = captured["kwargs"]["messages"][0]["content"]
    assert "frustrated with technology" in prompt
    assert "mid-market nonprofits" in prompt
    assert "northeast US" in prompt
    assert "msp" in prompt
    assert "hubspot" in prompt
