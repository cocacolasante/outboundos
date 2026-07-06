"""Phase 36 — Agent reply pipeline.

Three layers:
1. ``reply_sentiment.classify_reply`` — strict-JSON parse into the
   dataclass, neutral fallback on malformed output (Anthropic mocked;
   never hits the network).
2. ``agent_core.process_inbound_reply`` — the autonomy rules:
   positive+unconverted → ONE convert reminder (idempotent on re-run);
   positive+converted → follow-up task on the deal;
   neutral / below-threshold → activity only, no reminder;
   notification dedup holds across re-runs.
3. ``reply_poller`` wiring — the agent path fires for newly-matched
   messages and is gated by ``settings.AGENT_ENABLED``.
"""
from __future__ import annotations

from datetime import datetime, time, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models import (
    AgentAction,
    AgentActionType,
    Campaign,
    CampaignStatus,
    ConnectedAccount,
    ConnectedAccountTestStatus,
    CrmActivity,
    CrmActivityDirection,
    CrmActivityType,
    Lead,
    Notification,
    Opportunity,
)
from app.services import agent_core, encryption, reply_sentiment
from app.services.agent_core import (
    CONVERT_REMINDER_SUBJECT,
    FOLLOWUP_REMINDER_SUBJECT,
    process_inbound_reply,
)
from app.services.reply_sentiment import ReplyClassification, classify_reply

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# reply_sentiment.classify_reply (Anthropic mocked)
# --------------------------------------------------------------------------


def _fake_anthropic_message(text: str):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=900, output_tokens=120, server_tool_use=None),
    )


async def test_classify_reply_parses_valid_json():
    payload = (
        '{"sentiment": "positive", "intent": "meeting_request", '
        '"confidence": 0.91, "summary": "Wants a call next week", '
        '"suggested_next_action": "schedule"}'
    )
    fake_client = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(return_value=_fake_anthropic_message(payload))
        )
    )
    with patch("app.services.reply_sentiment.get_client", return_value=fake_client):
        result = await classify_reply("Re: Hi", "Yes, let's talk Tuesday", {"name": "Jane"})

    assert result.sentiment == "positive"
    assert result.intent == "meeting_request"
    assert result.confidence == 0.91
    assert result.summary == "Wants a call next week"
    assert result.suggested_next_action == "schedule"
    assert result.parse_failed is False
    assert result.cost_usd > 0  # usage tokens × Haiku pricing


async def test_classify_reply_malformed_output_falls_back_neutral():
    fake_client = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(return_value=_fake_anthropic_message("sorry, no JSON here"))
        )
    )
    with patch("app.services.reply_sentiment.get_client", return_value=fake_client):
        result = await classify_reply("Re: Hi", "body", None)

    assert result.parse_failed is True
    assert result.sentiment == "neutral"
    assert result.confidence == 0.0
    assert result.suggested_next_action == "ignore"


async def test_classify_reply_api_error_falls_back_neutral():
    fake_client = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(side_effect=RuntimeError("api down")))
    )
    with patch("app.services.reply_sentiment.get_client", return_value=fake_client):
        result = await classify_reply("Re: Hi", "body", None)
    assert result.parse_failed is True
    assert result.sentiment == "neutral"


async def test_classify_reply_out_of_office_never_positive():
    """Belt-and-suspenders: even if the model labels an autoresponder
    positive, the OOO intent forces sentiment back to neutral."""
    payload = (
        '{"sentiment": "positive", "intent": "out_of_office", '
        '"confidence": 0.95, "summary": "OOO until Monday", '
        '"suggested_next_action": "ignore"}'
    )
    fake_client = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(return_value=_fake_anthropic_message(payload))
        )
    )
    with patch("app.services.reply_sentiment.get_client", return_value=fake_client):
        result = await classify_reply("Auto: OOO", "I am out of office", None)
    assert result.intent == "out_of_office"
    assert result.sentiment == "neutral"


async def test_classify_reply_clamps_confidence():
    payload = (
        '{"sentiment": "negative", "intent": "not_interested", '
        '"confidence": 7.5, "summary": "no", "suggested_next_action": "close_lost"}'
    )
    fake_client = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(return_value=_fake_anthropic_message(payload))
        )
    )
    with patch("app.services.reply_sentiment.get_client", return_value=fake_client):
        result = await classify_reply("Re:", "no thanks", None)
    assert result.confidence == 1.0


# --------------------------------------------------------------------------
# agent_core.process_inbound_reply
# --------------------------------------------------------------------------


def _positive(confidence: float = 0.9) -> ReplyClassification:
    return ReplyClassification(
        sentiment="positive", intent="interested", confidence=confidence,
        summary="Interested, asked for pricing", suggested_next_action="convert",
        model="test-model", cost_usd=0.001,
    )


def _neutral() -> ReplyClassification:
    return ReplyClassification(
        sentiment="neutral", intent="question", confidence=0.8,
        summary="Asked who we are", suggested_next_action="reply",
    )


_MSG = {
    "uid": "10", "message_id": "mid-agent-1", "in_reply_to": "x",
    "references": [], "subject": "Re: Hi", "from_email": "lead@x.com",
    "body_text": "Sounds interesting, what does it cost?",
    "received_at": None,
}


async def _make_lead(db_session, **kw) -> Lead:
    lead = Lead(campaign_id=None, email="lead@x.com", first_name="Jane", **kw)
    db_session.add(lead)
    await db_session.commit()
    await db_session.refresh(lead)
    return lead


async def test_positive_unconverted_logs_activity_and_one_reminder(db_session):
    lead = await _make_lead(db_session)
    result = await process_inbound_reply(db_session, lead, dict(_MSG), _positive())
    await db_session.commit()

    assert result["activity_logged"] is True
    assert result["reminder_created"] is True

    # Inbound email activity with sentiment.
    email_act = await db_session.scalar(select(CrmActivity).where(
        CrmActivity.lead_id == lead.id,
        CrmActivity.activity_type == CrmActivityType.EMAIL,
    ))
    assert email_act is not None
    assert email_act.direction is CrmActivityDirection.INBOUND
    assert email_act.sentiment == "positive"
    assert email_act.is_agent_generated is True

    # Exactly one convert reminder, due in the future.
    reminders = (await db_session.execute(select(CrmActivity).where(
        CrmActivity.lead_id == lead.id,
        CrmActivity.activity_type == CrmActivityType.TASK,
        CrmActivity.subject == CONVERT_REMINDER_SUBJECT,
    ))).scalars().all()
    assert len(reminders) == 1
    assert reminders[0].due_at > datetime.now(timezone.utc)

    # Re-run (same or another positive reply) creates NO second reminder.
    msg2 = {**_MSG, "message_id": "mid-agent-2"}
    result2 = await process_inbound_reply(db_session, lead, msg2, _positive())
    await db_session.commit()
    assert result2["reminder_created"] is False
    reminders = (await db_session.execute(select(CrmActivity).where(
        CrmActivity.lead_id == lead.id,
        CrmActivity.subject == CONVERT_REMINDER_SUBJECT,
    ))).scalars().all()
    assert len(reminders) == 1


async def test_positive_converted_creates_followup_on_deal(db_session):
    opp = Opportunity(name="Deal", email="lead@x.com")
    db_session.add(opp)
    await db_session.flush()
    lead = await _make_lead(db_session, converted_opportunity_id=opp.id)

    result = await process_inbound_reply(db_session, lead, dict(_MSG), _positive())
    await db_session.commit()
    assert result["reminder_created"] is True

    # Follow-up task on the OPPORTUNITY; no convert reminder anywhere.
    followup = await db_session.scalar(select(CrmActivity).where(
        CrmActivity.opportunity_id == opp.id,
        CrmActivity.subject == FOLLOWUP_REMINDER_SUBJECT,
    ))
    assert followup is not None
    convert = await db_session.scalar(select(CrmActivity).where(
        CrmActivity.subject == CONVERT_REMINDER_SUBJECT,
    ))
    assert convert is None

    # The logged email activity spans BOTH parents.
    email_act = await db_session.scalar(select(CrmActivity).where(
        CrmActivity.lead_id == lead.id,
        CrmActivity.activity_type == CrmActivityType.EMAIL,
    ))
    assert email_act.opportunity_id == opp.id


async def test_neutral_logs_activity_no_reminder(db_session):
    lead = await _make_lead(db_session)
    result = await process_inbound_reply(db_session, lead, dict(_MSG), _neutral())
    await db_session.commit()

    assert result["activity_logged"] is True
    assert result["reminder_created"] is False
    tasks = (await db_session.execute(select(CrmActivity).where(
        CrmActivity.activity_type == CrmActivityType.TASK,
    ))).scalars().all()
    assert tasks == []


async def test_below_threshold_positive_skips_reminder(db_session):
    lead = await _make_lead(db_session)
    # Default min_confidence_to_act is 0.60.
    result = await process_inbound_reply(
        db_session, lead, dict(_MSG), _positive(confidence=0.40),
    )
    await db_session.commit()

    assert result["reminder_created"] is False
    # The skip is audited.
    skip = await db_session.scalar(select(AgentAction).where(
        AgentAction.action_type == AgentActionType.CREATE_REMINDER,
    ))
    assert skip is not None
    assert "below threshold" in skip.summary


async def test_auto_log_replies_off_skips_activity(db_session):
    lead = await _make_lead(db_session)
    s = await agent_core.get_agent_settings(db_session)
    s.auto_log_replies = False
    await db_session.commit()

    result = await process_inbound_reply(db_session, lead, dict(_MSG), _neutral())
    await db_session.commit()
    assert result["activity_logged"] is False
    acts = (await db_session.execute(select(CrmActivity))).scalars().all()
    assert acts == []


async def test_notification_dedup_across_reruns(db_session):
    lead = await _make_lead(db_session)
    result = await process_inbound_reply(db_session, lead, dict(_MSG), _positive())
    await db_session.commit()
    assert result["notified"] is True

    # Wipe the reminder so the second run isn't blocked by it, then
    # re-process the SAME message — notification must dedup.
    notifs = (await db_session.execute(select(Notification))).scalars().all()
    assert len(notifs) == 1
    assert notifs[0].dedup_key == "positive_reply:mid-agent-1"
    # OWNER_NOTIFY_EMAIL is empty in tests → email skipped, row persists.
    assert notifs[0].emailed_at is None

    result2 = await process_inbound_reply(db_session, lead, dict(_MSG), _positive())
    await db_session.commit()
    assert result2["notified"] is False
    notifs = (await db_session.execute(select(Notification))).scalars().all()
    assert len(notifs) == 1


async def test_audit_rows_written_for_each_step(db_session):
    lead = await _make_lead(db_session)
    await process_inbound_reply(db_session, lead, dict(_MSG), _positive())
    await db_session.commit()

    actions = (await db_session.execute(select(AgentAction))).scalars().all()
    types = {a.action_type for a in actions}
    assert AgentActionType.CLASSIFY_REPLY in types
    assert AgentActionType.LOG_ACTIVITY in types
    assert AgentActionType.CREATE_REMINDER in types
    assert AgentActionType.SEND_NOTIFICATION in types


# --------------------------------------------------------------------------
# reply_poller wiring
# --------------------------------------------------------------------------


async def _poller_fixture(db_session):
    acc = ConnectedAccount(
        label="Work", email_address="me@gmail.com",
        imap_host="imap.gmail.com", username="me@gmail.com",
        password_encrypted=encryption.encrypt("pw"),
        last_test_status=ConnectedAccountTestStatus.OK,
    )
    db_session.add(acc)
    await db_session.commit()
    await db_session.refresh(acc)
    c = Campaign(
        name="P36", goal="book a call", tone="t",
        sender_name="s", sender_email="s@x.com", sample_count=1,
        connected_account_id=acc.id,
        schedule_days=[0, 1, 2, 3, 4],
        schedule_time_start=time(9, 0), schedule_time_end=time(17, 0),
        status=CampaignStatus.RUNNING,
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    lead = Lead(
        campaign_id=c.id, email="lead@external.com",
        brevo_message_id="msg-orig", composed_subject="Hi",
    )
    db_session.add(lead)
    await db_session.commit()
    await db_session.refresh(lead)
    return acc, c, lead


_POLL_MSG = {
    "uid": "10", "message_id": "mid-poll-1", "in_reply_to": "msg-orig",
    "references": [], "subject": "Re: Hi", "from_email": "lead@external.com",
    "body_text": "Yes, interested!", "received_at": None,
}


async def test_poller_runs_agent_for_matched_reply(db_session):
    from app.workers.reply_poller import poll_account_for_replies

    acc, c, lead = await _poller_fixture(db_session)
    with patch(
        "app.workers.reply_poller.imap_client.fetch_recent_with_account",
        return_value=[dict(_POLL_MSG)],
    ), patch(
        "app.workers.reply_poller.reply_sentiment.classify_reply",
        new=AsyncMock(return_value=_positive()),
    ) as classify_mock:
        result = await poll_account_for_replies(acc, [c.id], db_session)
        await db_session.commit()

    assert result["replies_found"] == 1
    classify_mock.assert_awaited_once()
    # The lead context carried the campaign goal.
    assert classify_mock.call_args.kwargs["lead_context"]["campaign_goal"] == "book a call"

    # Agent side-effects landed: inbound activity + convert reminder.
    email_act = await db_session.scalar(select(CrmActivity).where(
        CrmActivity.lead_id == lead.id,
        CrmActivity.activity_type == CrmActivityType.EMAIL,
    ))
    assert email_act is not None and email_act.sentiment == "positive"
    reminder = await db_session.scalar(select(CrmActivity).where(
        CrmActivity.subject == CONVERT_REMINDER_SUBJECT,
    ))
    assert reminder is not None


async def test_poller_skips_agent_when_disabled(db_session, monkeypatch):
    from app.workers import reply_poller as rp

    acc, c, lead = await _poller_fixture(db_session)
    monkeypatch.setattr(rp.settings, "AGENT_ENABLED", False)
    with patch(
        "app.workers.reply_poller.imap_client.fetch_recent_with_account",
        return_value=[dict(_POLL_MSG)],
    ), patch(
        "app.workers.reply_poller.reply_sentiment.classify_reply",
        new=AsyncMock(return_value=_positive()),
    ) as classify_mock:
        result = await rp.poll_account_for_replies(acc, [c.id], db_session)
        await db_session.commit()

    # Reply still recorded; agent never invoked.
    assert result["replies_found"] == 1
    classify_mock.assert_not_awaited()
    acts = (await db_session.execute(select(CrmActivity))).scalars().all()
    assert acts == []


async def test_poller_agent_failure_does_not_break_reply_recording(db_session):
    from app.models import EmailEvent, EmailEventType
    from app.workers.reply_poller import poll_account_for_replies

    acc, c, lead = await _poller_fixture(db_session)
    with patch(
        "app.workers.reply_poller.imap_client.fetch_recent_with_account",
        return_value=[dict(_POLL_MSG)],
    ), patch(
        "app.workers.reply_poller.reply_sentiment.classify_reply",
        new=AsyncMock(side_effect=RuntimeError("classifier exploded")),
    ):
        result = await poll_account_for_replies(acc, [c.id], db_session)
        await db_session.commit()

    assert result["ok"] is True
    assert result["replies_found"] == 1
    event = await db_session.scalar(select(EmailEvent).where(
        EmailEvent.lead_id == lead.id,
        EmailEvent.event_type == EmailEventType.REPLIED,
    ))
    assert event is not None
