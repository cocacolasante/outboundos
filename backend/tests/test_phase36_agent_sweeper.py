"""Phase 36 — Agent reminder sweeper + stale-opportunity nudges.

Idempotency contracts under test:
- A task due soon / overdue produces ONE notification + stamps
  ``reminder_sent_at``; the second sweep is a no-op.
- ``flag_stale_opportunities`` nudges a quiet open deal once (the nudge
  task it creates counts as an open task, blocking re-nudges), skips
  deals with recent activity / open tasks / closed stages, and dedups
  the notification per-deal-per-week.
- Quiet hours defer the email but persist the notification row.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models import (
    CrmActivity,
    CrmActivityType,
    Notification,
    NotificationKind,
    Opportunity,
    OpportunityStage,
)
from app.services import agent_core, notifications
from app.services.agent_core import STALE_OPP_NUDGE_SUBJECT, flag_stale_opportunities
from app.workers.agent_sweeper import sweep_reminders_session

pytestmark = pytest.mark.asyncio


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _make_task(db_session, *, due_in_hours: float, subject="Call Jane") -> CrmActivity:
    opp = Opportunity(name=f"Deal {uuid.uuid4().hex[:6]}", email="d@x.com")
    db_session.add(opp)
    await db_session.flush()
    task = CrmActivity(
        opportunity_id=opp.id,
        activity_type=CrmActivityType.TASK,
        subject=subject,
        due_at=_now() + timedelta(hours=due_in_hours),
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


# --------------------------------------------------------------------------
# sweep_reminders
# --------------------------------------------------------------------------


async def test_due_soon_task_notifies_once(db_session):
    task = await _make_task(db_session, due_in_hours=4)

    counts = await sweep_reminders_session(db_session)
    await db_session.commit()
    assert counts["due_soon"] == 1

    notif = await db_session.scalar(select(Notification))
    assert notif is not None
    assert notif.kind is NotificationKind.TASK_DUE
    assert notif.dedup_key == f"task_due:{task.id}"
    assert "Call Jane" in notif.title
    await db_session.refresh(task)
    assert task.reminder_sent_at is not None

    # Second sweep: no new notification, no error.
    counts2 = await sweep_reminders_session(db_session)
    await db_session.commit()
    assert counts2["due_soon"] == 0 and counts2["overdue"] == 0
    notifs = (await db_session.execute(select(Notification))).scalars().all()
    assert len(notifs) == 1


async def test_overdue_task_notifies_with_overdue_kind(db_session):
    task = await _make_task(db_session, due_in_hours=-30)

    counts = await sweep_reminders_session(db_session)
    await db_session.commit()
    assert counts["overdue"] == 1

    notif = await db_session.scalar(select(Notification))
    assert notif.kind is NotificationKind.TASK_OVERDUE
    assert notif.dedup_key == f"task_overdue:{task.id}"


async def test_far_future_and_completed_tasks_not_swept(db_session):
    await _make_task(db_session, due_in_hours=24 * 7)  # next week
    done = await _make_task(db_session, due_in_hours=2, subject="Done task")
    done.completed_at = _now()
    await db_session.commit()

    counts = await sweep_reminders_session(db_session)
    await db_session.commit()
    assert counts["due_soon"] == 0 and counts["overdue"] == 0
    assert (await db_session.scalar(select(Notification))) is None


async def test_sweep_skipped_when_agent_disabled(db_session, monkeypatch):
    from app.workers import agent_sweeper

    await _make_task(db_session, due_in_hours=2)
    monkeypatch.setattr(agent_sweeper.settings, "AGENT_ENABLED", False)
    counts = await sweep_reminders_session(db_session)
    assert counts.get("skipped") == "agent_disabled"
    assert (await db_session.scalar(select(Notification))) is None


# --------------------------------------------------------------------------
# auto-complete tasks once the lead/deal has been acted on
# --------------------------------------------------------------------------


async def _add_touch(db_session, task, *, kind=CrmActivityType.NOTE, occurred_at=None):
    db_session.add(CrmActivity(
        lead_id=task.lead_id,
        opportunity_id=task.opportunity_id,
        activity_type=kind,
        subject="touch",
        occurred_at=occurred_at or _now(),
    ))
    await db_session.commit()


async def test_sweep_autocompletes_task_with_touch_since_creation(db_session):
    """A touch logged on the deal after the task was created closes the task —
    the reminder sweep auto-completes it instead of notifying."""
    task = await _make_task(db_session, due_in_hours=-30)  # overdue
    await _add_touch(db_session, task)  # logged now (after task creation)

    counts = await sweep_reminders_session(db_session)
    await db_session.commit()

    assert counts["auto_completed"] == 1
    assert counts["overdue"] == 0 and counts["due_soon"] == 0
    await db_session.refresh(task)
    assert task.completed_at is not None
    # No reminder for a task that's already been handled.
    assert (await db_session.scalar(select(Notification))) is None


async def test_sweep_does_not_autocomplete_when_touch_predates_task(db_session):
    """A touch that occurred BEFORE the task was created doesn't count — the
    reminder still fires."""
    task = await _make_task(db_session, due_in_hours=-30)
    await _add_touch(db_session, task, occurred_at=task.created_at - timedelta(hours=1))

    counts = await sweep_reminders_session(db_session)
    await db_session.commit()

    assert counts["auto_completed"] == 0
    assert counts["overdue"] == 1
    await db_session.refresh(task)
    assert task.completed_at is None


async def test_autocomplete_due_tasks_for_record_closes_due_task(db_session):
    """The inline path (called when an activity is logged) closes the record's
    open due/overdue tasks."""
    task = await _make_task(db_session, due_in_hours=-2)
    await _add_touch(db_session, task, kind=CrmActivityType.EMAIL)

    completed = await agent_core.autocomplete_due_tasks_for_record(
        db_session, opportunity_id=task.opportunity_id,
    )
    await db_session.commit()

    assert len(completed) == 1
    await db_session.refresh(task)
    assert task.completed_at is not None


async def test_autocomplete_leaves_future_dated_task_open(db_session):
    """A touch must NOT prematurely close a task scheduled for the future —
    only currently due/overdue tasks are auto-completed."""
    task = await _make_task(db_session, due_in_hours=24 * 7)  # next week
    await _add_touch(db_session, task)

    completed = await agent_core.autocomplete_due_tasks_for_record(
        db_session, opportunity_id=task.opportunity_id,
    )
    await db_session.commit()

    assert completed == []
    await db_session.refresh(task)
    assert task.completed_at is None


# --------------------------------------------------------------------------
# quiet hours
# --------------------------------------------------------------------------


async def test_in_quiet_hours_plain_window():
    base = datetime(2026, 6, 12, tzinfo=timezone.utc)
    assert notifications.in_quiet_hours(base.replace(hour=23), 22, 6) is True
    assert notifications.in_quiet_hours(base.replace(hour=3), 22, 6) is True
    assert notifications.in_quiet_hours(base.replace(hour=12), 22, 6) is False
    assert notifications.in_quiet_hours(base.replace(hour=10), 9, 17) is True
    assert notifications.in_quiet_hours(base.replace(hour=18), 9, 17) is False
    # Disabled / degenerate.
    assert notifications.in_quiet_hours(base.replace(hour=12), None, 6) is False
    assert notifications.in_quiet_hours(base.replace(hour=12), 12, 12) is False


async def test_quiet_hours_defer_email_but_persist_row(db_session, monkeypatch):
    """Inside quiet hours the notification row persists with
    ``emailed_at`` NULL and Brevo is never called."""
    monkeypatch.setattr(notifications.settings, "OWNER_NOTIFY_EMAIL", "owner@x.com")
    agent_settings = await agent_core.get_agent_settings(db_session)
    # Quiet hours = all 24 hours (start==end+wrap trick: use a window
    # covering "now" exactly).
    now_hour = _now().hour
    agent_settings.quiet_hours_start_utc = now_hour
    agent_settings.quiet_hours_end_utc = (now_hour + 2) % 24
    await db_session.commit()

    send_mock = AsyncMock(return_value="msg-1")
    with patch("app.services.notifications.brevo.send_email", new=send_mock):
        outcome = await notifications.notify(
            db_session, agent_settings,
            kind=NotificationKind.TASK_DUE,
            title="t", dedup_key="task_due:quiet-1",
        )
    await db_session.commit()

    assert outcome["notification"] is not None
    assert outcome["emailed"] is False
    send_mock.assert_not_awaited()
    notif = await db_session.scalar(select(Notification))
    assert notif.emailed_at is None


async def test_email_sent_outside_quiet_hours(db_session, monkeypatch):
    monkeypatch.setattr(notifications.settings, "OWNER_NOTIFY_EMAIL", "owner@x.com")
    agent_settings = await agent_core.get_agent_settings(db_session)

    send_mock = AsyncMock(return_value="msg-2")
    with patch("app.services.notifications.brevo.send_email", new=send_mock):
        outcome = await notifications.notify(
            db_session, agent_settings,
            kind=NotificationKind.TASK_DUE,
            title="Task due: call Jane", dedup_key="task_due:loud-1",
        )
    await db_session.commit()

    assert outcome["emailed"] is True
    send_mock.assert_awaited_once()
    kwargs = send_mock.call_args.kwargs
    assert kwargs["to_email"] == "owner@x.com"
    assert "[Agent]" in kwargs["subject"]
    notif = await db_session.scalar(select(Notification))
    assert notif.emailed_at is not None


async def test_notification_uses_dedicated_from_sender(db_session, monkeypatch):
    """Agent alerts send from OWNER_NOTIFY_FROM_* when set, independent of
    the campaign Brevo sender."""
    monkeypatch.setattr(notifications.settings, "OWNER_NOTIFY_EMAIL", "owner@x.com")
    monkeypatch.setattr(notifications.settings, "OWNER_NOTIFY_FROM_EMAIL", "anthony@csuitecode.com")
    monkeypatch.setattr(notifications.settings, "OWNER_NOTIFY_FROM_NAME", "Anthony Colasante")
    monkeypatch.setattr(notifications.settings, "BREVO_SENDER_EMAIL", "support@grantmind.pro")
    monkeypatch.setattr(notifications.settings, "BREVO_SENDER_NAME", "GrantMind Admin")
    agent_settings = await agent_core.get_agent_settings(db_session)

    send_mock = AsyncMock(return_value="msg-from")
    with patch("app.services.notifications.brevo.send_email", new=send_mock):
        await notifications.notify(
            db_session, agent_settings,
            kind=NotificationKind.TASK_DUE,
            title="Task due", dedup_key="task_due:from-1",
        )
    kwargs = send_mock.call_args.kwargs
    assert kwargs["sender_email"] == "anthony@csuitecode.com"
    assert kwargs["sender_name"] == "Anthony Colasante"


async def test_notification_from_sender_falls_back_to_brevo(db_session, monkeypatch):
    """Unset OWNER_NOTIFY_FROM_* → campaign Brevo sender (back-compat)."""
    monkeypatch.setattr(notifications.settings, "OWNER_NOTIFY_EMAIL", "owner@x.com")
    monkeypatch.setattr(notifications.settings, "OWNER_NOTIFY_FROM_EMAIL", "")
    monkeypatch.setattr(notifications.settings, "OWNER_NOTIFY_FROM_NAME", "")
    monkeypatch.setattr(notifications.settings, "BREVO_SENDER_EMAIL", "support@grantmind.pro")
    monkeypatch.setattr(notifications.settings, "BREVO_SENDER_NAME", "GrantMind Admin")
    agent_settings = await agent_core.get_agent_settings(db_session)

    send_mock = AsyncMock(return_value="msg-fb")
    with patch("app.services.notifications.brevo.send_email", new=send_mock):
        await notifications.notify(
            db_session, agent_settings,
            kind=NotificationKind.TASK_DUE,
            title="Task due", dedup_key="task_due:fb-1",
        )
    kwargs = send_mock.call_args.kwargs
    assert kwargs["sender_email"] == "support@grantmind.pro"
    assert kwargs["sender_name"] == "GrantMind Admin"


async def test_no_owner_email_persists_row_without_email(db_session, monkeypatch):
    monkeypatch.setattr(notifications.settings, "OWNER_NOTIFY_EMAIL", "")
    agent_settings = await agent_core.get_agent_settings(db_session)
    send_mock = AsyncMock()
    with patch("app.services.notifications.brevo.send_email", new=send_mock):
        outcome = await notifications.notify(
            db_session, agent_settings,
            kind=NotificationKind.REPLY,
            title="t", dedup_key="reply:noowner-1",
        )
    assert outcome["notification"] is not None
    assert outcome["emailed"] is False
    send_mock.assert_not_awaited()


# --------------------------------------------------------------------------
# flag_stale_opportunities
# --------------------------------------------------------------------------


async def _make_opp(
    db_session, *, stage=OpportunityStage.PROPOSAL, idle_days: int | None = None,
) -> Opportunity:
    opp = Opportunity(name=f"Deal {uuid.uuid4().hex[:6]}", stage=stage, email="o@x.com")
    db_session.add(opp)
    await db_session.flush()
    if idle_days is not None:
        # An activity dated idle_days ago anchors the last-touch time.
        db_session.add(CrmActivity(
            opportunity_id=opp.id,
            activity_type=CrmActivityType.NOTE,
            subject="last touch",
            occurred_at=_now() - timedelta(days=idle_days),
        ))
    await db_session.commit()
    await db_session.refresh(opp)
    return opp


async def test_stale_opp_gets_nudge_task_and_notification(db_session):
    opp = await _make_opp(db_session, idle_days=10)

    result = await flag_stale_opportunities(db_session)
    await db_session.commit()
    assert result["flagged"] == 1

    nudge = await db_session.scalar(select(CrmActivity).where(
        CrmActivity.opportunity_id == opp.id,
        CrmActivity.subject == STALE_OPP_NUDGE_SUBJECT,
    ))
    assert nudge is not None
    assert nudge.is_agent_generated is True
    # Pre-stamped so the reminder sweeper doesn't double-ping.
    assert nudge.reminder_sent_at is not None

    notif = await db_session.scalar(select(Notification).where(
        Notification.kind == NotificationKind.STALE_OPPORTUNITY,
    ))
    assert notif is not None
    assert notif.opportunity_id == opp.id

    # Re-run: the open nudge task blocks a second nudge.
    result2 = await flag_stale_opportunities(db_session)
    await db_session.commit()
    assert result2["flagged"] == 0
    nudges = (await db_session.execute(select(CrmActivity).where(
        CrmActivity.subject == STALE_OPP_NUDGE_SUBJECT,
    ))).scalars().all()
    assert len(nudges) == 1


async def test_recent_activity_opp_not_flagged(db_session):
    await _make_opp(db_session, idle_days=2)  # touched 2 days ago
    result = await flag_stale_opportunities(db_session)
    assert result["flagged"] == 0


async def test_closed_opp_not_flagged(db_session):
    await _make_opp(db_session, stage=OpportunityStage.CLOSED_WON, idle_days=30)
    result = await flag_stale_opportunities(db_session)
    assert result["checked"] == 0
    assert result["flagged"] == 0


async def test_opp_with_open_task_not_flagged(db_session):
    opp = await _make_opp(db_session, idle_days=10)
    db_session.add(CrmActivity(
        opportunity_id=opp.id,
        activity_type=CrmActivityType.TASK,
        subject="Manual follow-up already planned",
    ))
    await db_session.commit()

    result = await flag_stale_opportunities(db_session)
    assert result["flagged"] == 0


async def test_stale_nudges_toggle_off(db_session):
    await _make_opp(db_session, idle_days=10)
    s = await agent_core.get_agent_settings(db_session)
    s.stale_opp_nudges_enabled = False
    await db_session.commit()

    result = await flag_stale_opportunities(db_session)
    assert result.get("skipped") == "stale_opp_nudges_disabled"
    assert result["flagged"] == 0
