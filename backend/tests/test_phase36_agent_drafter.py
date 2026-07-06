"""Phase 36 — optional reply drafts (Sonnet, gated by auto_draft_replies).

Contracts:
- Drafter returns the Sonnet text capped at 1500 chars; API failure →
  ok=False without raising.
- should_draft skips pointless intents (OOO / unsubscribe / not_interested).
- process_inbound_reply only drafts when auto_draft_replies is ON; the
  draft lands on a draft_reply audit row (which the /agent/replies feed
  surfaces) and inside the owner notification body.
- Draft failure never blocks the rest of the pipeline.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models import (
    AgentAction,
    AgentActionStatus,
    AgentActionType,
    Lead,
    Notification,
)
from app.services import agent_core, reply_drafter
from app.services.agent_core import process_inbound_reply
from app.services.reply_sentiment import ReplyClassification

pytestmark = pytest.mark.asyncio


def _fake_message(text: str):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=600, output_tokens=150, server_tool_use=None),
    )


def _positive(intent="interested") -> ReplyClassification:
    return ReplyClassification(
        sentiment="positive", intent=intent, confidence=0.9,
        summary="Wants pricing", suggested_next_action="reply",
    )


_MSG = {
    "uid": "10", "message_id": "mid-draft-1", "in_reply_to": "x",
    "references": [], "subject": "Re: Hi", "from_email": "lead@x.com",
    "body_text": "What does it cost?", "received_at": None,
}


# --------------------------------------------------------------------------
# reply_drafter unit
# --------------------------------------------------------------------------


async def test_draft_reply_returns_sonnet_text():
    fake_client = SimpleNamespace(messages=SimpleNamespace(
        create=AsyncMock(return_value=_fake_message("Hi Jane — pricing attached. Tuesday work?\n\nAnthony"))
    ))
    with patch("app.services.reply_drafter.get_client", return_value=fake_client):
        result = await reply_drafter.draft_reply(
            "Re: Hi", "What does it cost?", _positive(), {"name": "Jane"},
            sender_name="Anthony",
        )
    assert result.ok is True
    assert "pricing attached" in result.body
    assert result.cost_usd > 0
    # The draft model (Sonnet) was used.
    assert fake_client.messages.create.call_args.kwargs["model"] == \
        reply_drafter.settings.ANTHROPIC_AGENT_DRAFT_MODEL


async def test_draft_reply_api_failure_returns_not_ok():
    fake_client = SimpleNamespace(messages=SimpleNamespace(
        create=AsyncMock(side_effect=RuntimeError("down"))
    ))
    with patch("app.services.reply_drafter.get_client", return_value=fake_client):
        result = await reply_drafter.draft_reply("s", "b", _positive(), None)
    assert result.ok is False
    assert result.body == ""


async def test_draft_reply_caps_length():
    fake_client = SimpleNamespace(messages=SimpleNamespace(
        create=AsyncMock(return_value=_fake_message("word " * 1000))
    ))
    with patch("app.services.reply_drafter.get_client", return_value=fake_client):
        result = await reply_drafter.draft_reply("s", "b", _positive(), None)
    assert len(result.body) <= 1500


async def test_should_draft_skips_pointless_intents():
    assert reply_drafter.should_draft("interested") is True
    assert reply_drafter.should_draft("question") is True
    assert reply_drafter.should_draft("out_of_office") is False
    assert reply_drafter.should_draft("unsubscribe") is False
    assert reply_drafter.should_draft("not_interested") is False


# --------------------------------------------------------------------------
# pipeline integration
# --------------------------------------------------------------------------


async def _make_lead(db_session) -> Lead:
    lead = Lead(campaign_id=None, email="lead@x.com", first_name="Jane")
    db_session.add(lead)
    await db_session.commit()
    await db_session.refresh(lead)
    return lead


async def _enable_drafts(db_session):
    s = await agent_core.get_agent_settings(db_session)
    s.auto_draft_replies = True
    await db_session.commit()


async def test_pipeline_drafts_when_enabled_and_attaches_everywhere(db_session):
    lead = await _make_lead(db_session)
    await _enable_drafts(db_session)

    draft_mock = AsyncMock(return_value=reply_drafter.DraftResult(
        ok=True, body="Hi Jane — happy to walk through pricing. Tuesday?",
        model="sonnet-test", cost_usd=0.012,
    ))
    with patch("app.services.reply_drafter.draft_reply", new=draft_mock):
        result = await process_inbound_reply(db_session, lead, dict(_MSG), _positive())
        await db_session.commit()

    assert result["draft_created"] is True
    draft_mock.assert_awaited_once()

    # Audit row carries the draft body (the /agent/replies feed reads it).
    action = await db_session.scalar(select(AgentAction).where(
        AgentAction.action_type == AgentActionType.DRAFT_REPLY,
    ))
    assert action.status is AgentActionStatus.SUCCESS
    assert action.detail["draft_body"].startswith("Hi Jane")
    assert action.cost_usd is not None

    # The owner notification includes the suggested reply.
    notif = await db_session.scalar(select(Notification))
    assert "Suggested reply" in notif.body
    assert "walk through pricing" in notif.body


async def test_pipeline_no_draft_when_toggle_off(db_session):
    lead = await _make_lead(db_session)  # auto_draft_replies defaults OFF

    draft_mock = AsyncMock()
    with patch("app.services.reply_drafter.draft_reply", new=draft_mock):
        result = await process_inbound_reply(db_session, lead, dict(_MSG), _positive())
        await db_session.commit()

    assert result["draft_created"] is False
    draft_mock.assert_not_awaited()
    action = await db_session.scalar(select(AgentAction).where(
        AgentAction.action_type == AgentActionType.DRAFT_REPLY,
    ))
    assert action is None  # not even a skip row — the feature is off


async def test_pipeline_skips_draft_for_ooo_intent(db_session):
    lead = await _make_lead(db_session)
    await _enable_drafts(db_session)

    ooo = ReplyClassification(
        sentiment="neutral", intent="out_of_office", confidence=0.9,
        summary="OOO until Monday", suggested_next_action="ignore",
    )
    draft_mock = AsyncMock()
    with patch("app.services.reply_drafter.draft_reply", new=draft_mock):
        result = await process_inbound_reply(db_session, lead, dict(_MSG), ooo)
        await db_session.commit()

    assert result["draft_created"] is False
    draft_mock.assert_not_awaited()
    skip = await db_session.scalar(select(AgentAction).where(
        AgentAction.action_type == AgentActionType.DRAFT_REPLY,
    ))
    assert skip.status is AgentActionStatus.SKIPPED
    assert "out_of_office" in skip.summary


async def test_pipeline_survives_draft_failure(db_session):
    lead = await _make_lead(db_session)
    await _enable_drafts(db_session)

    draft_mock = AsyncMock(return_value=reply_drafter.DraftResult(ok=False, model="m"))
    with patch("app.services.reply_drafter.draft_reply", new=draft_mock):
        result = await process_inbound_reply(db_session, lead, dict(_MSG), _positive())
        await db_session.commit()

    # Pipeline completed: activity + reminder + notification all landed.
    assert result["activity_logged"] is True
    assert result["reminder_created"] is True
    assert result["notified"] is True
    assert result["draft_created"] is False
    failed = await db_session.scalar(select(AgentAction).where(
        AgentAction.action_type == AgentActionType.DRAFT_REPLY,
    ))
    assert failed.status is AgentActionStatus.FAILED
    # Notification body has no dangling draft section.
    notif = await db_session.scalar(select(Notification))
    assert "Suggested reply" not in notif.body
