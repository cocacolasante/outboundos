"""Phase 38 — reply-driven copy loop (Feature A).

Capture → insight → injection:
- process_inbound_reply snapshots a ReplyOutcome (sent copy + verdict);
  campaign-less CRM leads are skipped.
- winning_examples returns only positive-reply bodies for the right
  campaign; angle_summary parses mocked LLM JSON, caches, and only
  re-runs past the new-outcome threshold.
- The compose prompt includes the winning block only when data exists,
  and the user's StyleCorrection block ranks ABOVE it.
"""
from __future__ import annotations

import uuid
from datetime import time as dt_time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models import (
    Campaign,
    CampaignCopyInsights,
    CampaignStatus,
    Lead,
    ReplyOutcome,
)
from app.services import copy_insights
from app.services.agent_core import process_inbound_reply
from app.services.reply_sentiment import ReplyClassification
from app.workers.compose import _build_generic_prompt, _build_personalized_prompt

pytestmark = pytest.mark.asyncio


async def _make_campaign(db_session, **kw) -> Campaign:
    defaults = dict(
        name="P38", goal="g", tone="t",
        sender_name="S", sender_email="s@x.com", sample_count=1,
        schedule_days=[], schedule_time_start=dt_time(0, 0),
        schedule_time_end=dt_time(23, 59), schedule_timezone="UTC",
        status=CampaignStatus.RUNNING,
    )
    defaults.update(kw)
    c = Campaign(**defaults)
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    return c


async def _add_outcome(db_session, campaign, sentiment, *, subject="Subj", body="Body text"):
    o = ReplyOutcome(
        campaign_id=campaign.id, sentiment=sentiment,
        composed_subject=subject, composed_body=body,
    )
    db_session.add(o)
    await db_session.commit()
    return o


def _cls(sentiment="positive") -> ReplyClassification:
    return ReplyClassification(
        sentiment=sentiment, intent="interested", confidence=0.9,
        summary="s", suggested_next_action="reply",
    )


_MSG = {
    "uid": "1", "message_id": f"mid-{uuid.uuid4().hex[:8]}", "in_reply_to": "",
    "references": [], "subject": "Re: Hi", "from_email": "l@x.com",
    "body_text": "yes", "received_at": None,
}


# --------------------------------------------------------------------------
# Capture
# --------------------------------------------------------------------------


async def test_reply_outcome_snapshot_on_campaign_lead(db_session):
    campaign = await _make_campaign(db_session)
    lead = Lead(
        campaign_id=campaign.id, email="l@x.com",
        composed_subject="The subject sent", composed_body="The body sent",
    )
    db_session.add(lead)
    await db_session.commit()

    await process_inbound_reply(db_session, lead, dict(_MSG), _cls())
    await db_session.commit()

    outcome = await db_session.scalar(select(ReplyOutcome))
    assert outcome is not None
    assert outcome.campaign_id == campaign.id
    assert outcome.sentiment == "positive"
    assert outcome.composed_subject == "The subject sent"
    assert outcome.composed_body == "The body sent"

    # Snapshot survives later copy edits.
    lead.composed_body = "edited afterwards"
    await db_session.commit()
    await db_session.refresh(outcome)
    assert outcome.composed_body == "The body sent"


async def test_no_outcome_for_campaignless_lead(db_session):
    lead = Lead(campaign_id=None, email="crm@x.com")
    db_session.add(lead)
    await db_session.commit()

    await process_inbound_reply(db_session, lead, dict(_MSG), _cls())
    await db_session.commit()
    assert (await db_session.scalar(select(ReplyOutcome))) is None


# --------------------------------------------------------------------------
# winning_examples
# --------------------------------------------------------------------------


async def test_winning_examples_positive_only_right_campaign(db_session):
    c1 = await _make_campaign(db_session)
    c2 = await _make_campaign(db_session, name="other")
    await _add_outcome(db_session, c1, "positive", body="winner one")
    await _add_outcome(db_session, c1, "negative", body="loser")
    await _add_outcome(db_session, c2, "positive", body="other campaign winner")

    examples = await copy_insights.winning_examples(db_session, c1.id)
    assert len(examples) == 1
    assert examples[0]["body"] == "winner one"


# --------------------------------------------------------------------------
# angle summary (LLM mocked)
# --------------------------------------------------------------------------


def _fake_message(text: str):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=800, output_tokens=200, server_tool_use=None),
    )


_LLM_JSON = (
    '{"winning_openers": ["question about their stack"], '
    '"subject_patterns": ["short, lowercase"], '
    '"value_framings": ["cost-saving"], '
    '"cta_styles": ["soft ask"], '
    '"avoid": ["long intros"]}'
)


async def test_angle_summary_parses_caches_and_thresholds(db_session):
    campaign = await _make_campaign(db_session)
    for _ in range(2):
        await _add_outcome(db_session, campaign, "positive")
    await _add_outcome(db_session, campaign, "negative")

    create_mock = AsyncMock(return_value=_fake_message(_LLM_JSON))
    fake_client = SimpleNamespace(messages=SimpleNamespace(create=create_mock))
    with patch("app.services.copy_insights.get_client", return_value=fake_client):
        insights = await copy_insights.refresh_angle_summary(db_session, campaign.id)
        await db_session.commit()

    assert insights["winning_openers"] == ["question about their stack"]
    assert insights["avoid"] == ["long intros"]
    assert insights["_meta"]["cost_usd"] > 0
    create_mock.assert_awaited_once()

    cached = await db_session.scalar(select(CampaignCopyInsights))
    assert cached.outcome_count_at_refresh == 3

    # Below the new-outcome threshold (3) → cached result, NO new LLM call.
    await _add_outcome(db_session, campaign, "positive")
    with patch("app.services.copy_insights.get_client", return_value=fake_client):
        again = await copy_insights.refresh_angle_summary(db_session, campaign.id)
    assert again["winning_openers"] == ["question about their stack"]
    create_mock.assert_awaited_once()  # still just the one call

    # Two more outcomes crosses the threshold → re-runs.
    await _add_outcome(db_session, campaign, "positive")
    await _add_outcome(db_session, campaign, "negative")
    with patch("app.services.copy_insights.get_client", return_value=fake_client):
        await copy_insights.refresh_angle_summary(db_session, campaign.id)
        await db_session.commit()
    assert create_mock.await_count == 2


async def test_angle_summary_none_without_positives(db_session):
    campaign = await _make_campaign(db_session)
    await _add_outcome(db_session, campaign, "negative")
    create_mock = AsyncMock()
    fake_client = SimpleNamespace(messages=SimpleNamespace(create=create_mock))
    with patch("app.services.copy_insights.get_client", return_value=fake_client):
        result = await copy_insights.refresh_angle_summary(db_session, campaign.id)
    assert result is None
    create_mock.assert_not_awaited()


async def test_angle_summary_keeps_stale_cache_on_llm_failure(db_session):
    campaign = await _make_campaign(db_session)
    db_session.add(CampaignCopyInsights(
        campaign_id=campaign.id,
        insights={"winning_openers": ["old but gold"]},
        outcome_count_at_refresh=0,
    ))
    for _ in range(4):
        await _add_outcome(db_session, campaign, "positive")

    fake_client = SimpleNamespace(messages=SimpleNamespace(
        create=AsyncMock(side_effect=RuntimeError("down"))
    ))
    with patch("app.services.copy_insights.get_client", return_value=fake_client):
        result = await copy_insights.refresh_angle_summary(db_session, campaign.id)
    assert result == {"winning_openers": ["old but gold"]}


# --------------------------------------------------------------------------
# prompt injection
# --------------------------------------------------------------------------


async def test_build_winning_block_none_without_data(db_session):
    campaign = await _make_campaign(db_session)
    assert await copy_insights.build_winning_block(db_session, campaign.id) is None


async def test_build_winning_block_with_data(db_session):
    campaign = await _make_campaign(db_session)
    await _add_outcome(db_session, campaign, "positive", body="great winner body")
    db_session.add(CampaignCopyInsights(
        campaign_id=campaign.id,
        insights={
            "winning_openers": ["lead with their tech stack"],
            "subject_patterns": [], "value_framings": [],
            "cta_styles": [], "avoid": ["jargon"],
        },
        outcome_count_at_refresh=1,
    ))
    await db_session.commit()

    block = await copy_insights.build_winning_block(db_session, campaign.id)
    assert "What's working on THIS campaign" in block
    assert "lead with their tech stack" in block
    assert "jargon" in block
    assert "great winner body" in block
    # Subordination is stated inline.
    assert "style corrections above always" in block


async def test_personalized_prompt_ranks_style_above_winning_block():
    prompt = _build_personalized_prompt(
        "goal", "tone", "Sender", "Jane", "Doe", "Acme", "CFO",
        {"quality": "rich"}, ["USER STYLE EXAMPLE"],
        winning_block="WINNING BLOCK CONTENT",
    )
    assert "USER STYLE EXAMPLE" in prompt
    assert "WINNING BLOCK CONTENT" in prompt
    assert prompt.index("USER STYLE EXAMPLE") < prompt.index("WINNING BLOCK CONTENT")


async def test_prompts_unchanged_without_winning_block():
    p1 = _build_personalized_prompt(
        "goal", "tone", "S", "J", "D", "Acme", "CFO", {}, [],
    )
    p2 = _build_generic_prompt("goal", "tone", "S", "J", "D", "Acme")
    assert "What's working" not in p1
    assert "What's working" not in p2
    g = _build_generic_prompt(
        "goal", "tone", "S", "J", "D", "Acme", winning_block="WIN BLOCK",
    )
    assert "WIN BLOCK" in g


# --------------------------------------------------------------------------
# endpoint
# --------------------------------------------------------------------------


async def test_copy_insights_endpoint(client, db_session):
    campaign = await _make_campaign(db_session)
    await _add_outcome(db_session, campaign, "positive", body="winner")
    await _add_outcome(db_session, campaign, "negative")
    db_session.add(CampaignCopyInsights(
        campaign_id=campaign.id,
        insights={"winning_openers": ["x"], "_meta": {"cost_usd": 0.001}},
        outcome_count_at_refresh=2,
    ))
    await db_session.commit()

    resp = await client.get(f"/campaigns/{campaign.id}/copy-insights")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["outcome_counts"] == {"positive": 1, "neutral": 0, "negative": 1}
    assert body["insights"]["winning_openers"] == ["x"]
    assert "_meta" not in body["insights"]
    assert body["winning_examples"][0]["body"] == "winner"
    assert body["refreshed_at"] is not None
