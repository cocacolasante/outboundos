"""Phase 36 — daily digest.

Covers: content assembly (overdue / due-today / replies-by-sentiment /
pipeline movement / unsent-alert count), the per-day dedup, the
daily_digest_enabled + AGENT_ENABLED gates, and that the digest email
fires even when OWNER quiet hours would defer a normal alert.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models import (
    CrmActivity,
    CrmActivityDirection,
    CrmActivityType,
    Notification,
    NotificationKind,
    Opportunity,
    OpportunityStage,
)
from app.services import agent_core, notifications
from app.workers.digest import build_digest, render_digest, send_daily_session

pytestmark = pytest.mark.asyncio


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _seed(db_session):
    """One of everything the digest reports on."""
    opp = Opportunity(name="Acme deal", email="a@x.com")
    db_session.add(opp)
    await db_session.flush()

    db_session.add_all([
        # Overdue + due-today open tasks.
        CrmActivity(
            opportunity_id=opp.id, activity_type=CrmActivityType.TASK,
            subject="Overdue: send contract", due_at=_now() - timedelta(hours=20),
        ),
        CrmActivity(
            opportunity_id=opp.id, activity_type=CrmActivityType.TASK,
            subject="Due today: call back", due_at=_now() + timedelta(hours=3),
        ),
        # Inbound replies in the last 24h, mixed sentiment.
        CrmActivity(
            opportunity_id=opp.id, activity_type=CrmActivityType.EMAIL,
            direction=CrmActivityDirection.INBOUND, subject="Re: hi",
            sentiment="positive", is_agent_generated=True,
        ),
        CrmActivity(
            opportunity_id=opp.id, activity_type=CrmActivityType.EMAIL,
            direction=CrmActivityDirection.INBOUND, subject="Re: hi 2",
            sentiment="negative", is_agent_generated=True,
        ),
    ])
    # A closed deal in the last 24h.
    closed = Opportunity(
        name="Won deal", stage=OpportunityStage.CLOSED_WON,
        closed_at=_now() - timedelta(hours=2),
    )
    db_session.add(closed)
    await db_session.commit()
    return opp


async def test_build_and_render_digest_content(db_session):
    await _seed(db_session)
    # One alert that never got emailed.
    await notifications.create_notification(
        db_session, kind=NotificationKind.TASK_DUE,
        title="quiet alert", dedup_key=f"task_due:{uuid.uuid4()}",
    )
    await db_session.commit()

    data = await build_digest(db_session)
    assert len(data["overdue"]) == 1
    assert len(data["due_today"]) == 1
    assert data["replies_by_sentiment"] == {"positive": 1, "negative": 1}
    assert len(data["new_opps"]) == 2  # both created within 24h in this test
    assert len(data["closed_opps"]) == 1
    assert data["unsent_alerts"] == 1

    title, body = render_digest(data)
    assert "1 overdue" in title and "1 due today" in title and "2 replies" in title
    assert "Overdue: send contract" in body
    assert "Due today: call back" in body
    assert "1 positive" in body and "1 negative" in body
    assert "Won deal" in body and "closed won" in body
    assert "not\nemailed" in body or "not emailed" in body


async def test_send_daily_persists_and_dedups_per_day(db_session, monkeypatch):
    monkeypatch.setattr(notifications.settings, "OWNER_NOTIFY_EMAIL", "owner@x.com")
    await _seed(db_session)

    send_mock = AsyncMock(return_value="msg-digest")
    with patch("app.services.notifications.brevo.send_email", new=send_mock):
        result = await send_daily_session(db_session)
        await db_session.commit()

    assert result["sent"] is True and result["emailed"] is True
    send_mock.assert_awaited_once()

    digest_row = await db_session.scalar(select(Notification).where(
        Notification.kind == NotificationKind.DIGEST,
    ))
    assert digest_row is not None
    assert digest_row.emailed_at is not None

    # Second run the same day: dedup, no second email.
    with patch("app.services.notifications.brevo.send_email", new=send_mock):
        result2 = await send_daily_session(db_session)
        await db_session.commit()
    assert result2.get("skipped") == "already_sent_today"
    send_mock.assert_awaited_once()  # still just the one call


async def test_digest_bypasses_quiet_hours(db_session, monkeypatch):
    """The digest email goes out even when quiet hours would defer a
    normal alert — its hour is operator-configured."""
    monkeypatch.setattr(notifications.settings, "OWNER_NOTIFY_EMAIL", "owner@x.com")
    s = await agent_core.get_agent_settings(db_session)
    now_hour = _now().hour
    s.quiet_hours_start_utc = now_hour
    s.quiet_hours_end_utc = (now_hour + 2) % 24
    await db_session.commit()

    send_mock = AsyncMock(return_value="msg-digest-quiet")
    with patch("app.services.notifications.brevo.send_email", new=send_mock):
        result = await send_daily_session(db_session)
        await db_session.commit()

    assert result["emailed"] is True
    send_mock.assert_awaited_once()


async def test_digest_disabled_toggle_skips(db_session):
    s = await agent_core.get_agent_settings(db_session)
    s.daily_digest_enabled = False
    await db_session.commit()

    result = await send_daily_session(db_session)
    assert result.get("skipped") == "daily_digest_disabled"
    assert (await db_session.scalar(select(Notification))) is None


async def test_digest_agent_disabled_skips(db_session, monkeypatch):
    from app.workers import digest as digest_mod

    monkeypatch.setattr(digest_mod.settings, "AGENT_ENABLED", False)
    result = await send_daily_session(db_session)
    assert result.get("skipped") == "agent_disabled"


async def test_empty_digest_renders_calm_message(db_session):
    data = await build_digest(db_session)
    title, body = render_digest(data)
    assert "0 overdue" in title
    assert "Nothing needs attention" in body
