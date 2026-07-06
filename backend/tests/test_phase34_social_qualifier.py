"""Unit tests for ``services/social_listening_qualifier.py``.

Asserts strict JSON parsing; score clamped 1-10; category normalised;
comment truncated to <=500 chars; ``None`` returned on Anthropic failure.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.models import SocialOpportunityAction, SocialOpportunityCategory
from app.services import _anthropic, social_listening_qualifier


def _ant(body: str) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=body)])


@pytest.fixture(autouse=True)
def _reset_client():
    _anthropic._client = None
    yield
    _anthropic._client = None


GOOD_RESPONSE = """
{
  "score": 8,
  "category": "msp",
  "buying_signal": true,
  "pain_summary": "Frustrated with current IT provider, slow support.",
  "qualification_reason": "Strong vendor frustration + decision-maker title.",
  "suggested_comment": "Sounds painful — what's the SLA your team actually needs day-to-day? Happy to share what we've seen work for similar shops.",
  "suggested_connection_request": "Saw your post on MSP frustrations — I work with companies in your spot. Open to swapping notes?",
  "suggested_follow_up": "Thanks for connecting. Quick context — I'm an independent advisor, not a vendor. Happy to share what I've seen work for mid-market MSP transitions if it would help.",
  "recommended_action": "comment"
}
"""


async def test_qualify_post_parses_strict_json():
    mock = AsyncMock(return_value=_ant(GOOD_RESPONSE))
    with patch.object(social_listening_qualifier, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=mock))):
        result = await social_listening_qualifier.qualify_post(
            post_text="Our MSP is impossible to reach when something breaks.",
            author_name="Jane Doe",
            author_headline="CFO at Acme",
            company_name="Acme",
            search_tone="helpful",
            sender_name="Anthony",
        )
    assert result is not None
    assert result.score == 8
    assert result.category == SocialOpportunityCategory.MSP.value
    assert result.buying_signal is True
    assert result.recommended_action == SocialOpportunityAction.COMMENT.value
    assert result.suggested_comment.startswith("Sounds painful")


async def test_qualify_post_truncates_long_comment_to_500_chars():
    # Build a deliberately long comment well past 500 chars with sentence breaks.
    sentences = "Sentence. " * 100  # ~1000 chars
    payload = (
        '{"score": 5, "category": "general_advisory", "buying_signal": false, '
        '"pain_summary": "x", "qualification_reason": "y", '
        f'"suggested_comment": "{sentences.strip()}", '
        '"suggested_connection_request": "z", '
        '"suggested_follow_up": "w", "recommended_action": "comment"}'
    )
    mock = AsyncMock(return_value=_ant(payload))
    with patch.object(social_listening_qualifier, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=mock))):
        result = await social_listening_qualifier.qualify_post(
            post_text="anything",
        )
    assert result is not None
    assert len(result.suggested_comment) <= 500


async def test_qualify_post_clamps_invalid_score():
    payload = (
        '{"score": 99, "category": "msp", "buying_signal": true, '
        '"pain_summary": "x", "qualification_reason": "y", '
        '"suggested_comment": "x", "suggested_connection_request": "y", '
        '"suggested_follow_up": "z", "recommended_action": "comment"}'
    )
    mock = AsyncMock(return_value=_ant(payload))
    with patch.object(social_listening_qualifier, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=mock))):
        result = await social_listening_qualifier.qualify_post(post_text="x")
    assert result is not None
    assert result.score == 10  # clamped


async def test_qualify_post_defaults_unknown_category():
    payload = (
        '{"score": 5, "category": "made_up_category", "buying_signal": false, '
        '"pain_summary": "x", "qualification_reason": "y", '
        '"suggested_comment": "x", "suggested_connection_request": "y", '
        '"suggested_follow_up": "z", "recommended_action": "comment"}'
    )
    mock = AsyncMock(return_value=_ant(payload))
    with patch.object(social_listening_qualifier, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=mock))):
        result = await social_listening_qualifier.qualify_post(post_text="x")
    assert result is not None
    assert result.category == SocialOpportunityCategory.GENERAL_ADVISORY.value


async def test_qualify_post_returns_none_on_anthropic_failure():
    failing = AsyncMock(side_effect=RuntimeError("503"))
    with patch.object(social_listening_qualifier, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=failing))):
        result = await social_listening_qualifier.qualify_post(post_text="x")
    assert result is None


async def test_qualify_post_returns_none_on_malformed_json():
    mock = AsyncMock(return_value=_ant("not json"))
    with patch.object(social_listening_qualifier, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=mock))):
        result = await social_listening_qualifier.qualify_post(post_text="x")
    assert result is None


async def test_qualify_post_returns_none_on_empty_post_text():
    """Don't burn an Anthropic call on an empty post."""
    mock = AsyncMock(return_value=_ant(GOOD_RESPONSE))
    with patch.object(social_listening_qualifier, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=mock))):
        result = await social_listening_qualifier.qualify_post(post_text="   ")
    assert result is None
    assert mock.await_count == 0


async def test_qualifier_prompt_lists_anti_patterns():
    """The qualifier prompt must EXPLICITLY name the anti-patterns
    (career/job posts, vendor self-promotion, for-sale listings,
    blog/listicle, off-topic, recruiting) — without these names the
    model scores them generously and the user's queue fills with junk
    like 'What obligations do I have to an ex employer?' or 'we built
    an AI assistant for Odoo'."""
    from app.services.social_listening_qualifier import PROMPT_TEMPLATE
    lowered = PROMPT_TEMPLATE.lower()
    assert "career" in lowered or "job" in lowered or "ex-employer" in lowered or "ex employer" in lowered
    assert "vendor self-promotion" in lowered or "selling" in lowered or "we built" in lowered
    assert "listicle" in lowered or "blog" in lowered or "article" in lowered
    assert "for sale" in lowered or "for-sale" in lowered
    assert "recruit" in lowered or "hiring" in lowered
    # The buying-signal checklist must require a specific vendor mention.
    assert "specific vendor" in lowered or "name a specific" in lowered or "names a specific" in lowered
    # And the scale must reserve high scores for clear buying intent.
    assert "score 7" in lowered or "score ≥ 5" in lowered or "score 7+" in lowered


async def test_qualifier_batch_prompt_reuses_strict_rubric():
    """The batch prompt MUST share the same rubric so the model applies
    identical strictness to every post regardless of dispatch path."""
    from app.services.social_listening_qualifier import (
        BATCH_PROMPT_TEMPLATE,
        PROMPT_TEMPLATE,
    )
    # Both should mention the same anti-pattern names — meaning they
    # use the shared _RUBRIC constant rather than diverging.
    for term in ("career", "vendor self-promotion", "for-sale", "listicle"):
        in_single = term in PROMPT_TEMPLATE.lower()
        in_batch = term in BATCH_PROMPT_TEMPLATE.lower()
        assert in_single == in_batch, (
            f"Anti-pattern '{term}' present in single={in_single} but "
            f"batch={in_batch} — prompts diverged"
        )
