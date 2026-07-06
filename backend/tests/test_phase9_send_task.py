"""Phase 9: send_lead task — rate limits (fakeredis), full send flow (DB + Brevo mocked)."""
import time
import uuid
from datetime import datetime, time as dt_time, timedelta
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest
import pytz
from sqlalchemy import select

from app.models import (
    Campaign,
    CampaignStatus,
    ComposeStatus,
    ConnectedAccount,
    Lead,
    SendStatus,
    Suppression,
    SuppressionReason,
)
from app.workers import send as send_mod


# --------------------------------------------------------------------------
# Helpers / fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def fake_redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


async def _make_campaign(
    db_session, *,
    status: CampaignStatus = CampaignStatus.RUNNING,
    max_per_hour: int | None = None,
    max_per_day: int | None = None,
    min_delay_seconds: int = 0,
) -> Campaign:
    c = Campaign(
        name="Phase 9 test",
        goal="Demo",
        tone="Direct",
        sender_name="Sender",
        sender_email="from@example.com",
        sample_count=1,
        # Empty days + permissive hours → always inside window
        schedule_days=[],
        schedule_time_start=dt_time(0, 0),
        schedule_time_end=dt_time(23, 59, 59),
        schedule_timezone="UTC",
        status=status,
        max_per_hour=max_per_hour,
        max_per_day=max_per_day,
        min_delay_seconds=min_delay_seconds,
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    return c


async def _make_lead(
    db_session, campaign: Campaign, *,
    email: str = "lead@example.com",
    composed_subject: str = "Subject",
    composed_body: str = "Body text",
    compose_status: ComposeStatus = ComposeStatus.DONE,
) -> Lead:
    l = Lead(
        campaign_id=campaign.id, email=email,
        first_name="Jane", last_name="Doe",
        compose_status=compose_status,
        composed_subject=composed_subject,
        composed_body=composed_body,
    )
    db_session.add(l)
    await db_session.commit()
    await db_session.refresh(l)
    return l


# --------------------------------------------------------------------------
# Rate-limit helpers (using fakeredis directly)
# --------------------------------------------------------------------------


async def test_rate_limits_ok_when_no_counters(db_session, fake_redis):
    c = await _make_campaign(db_session, max_per_hour=10, max_per_day=100)
    result = await send_mod.check_rate_limits(c, fake_redis)
    assert result == {"ok": True}


async def test_rate_limits_blocks_when_hour_cap_reached(db_session, fake_redis):
    c = await _make_campaign(db_session, max_per_hour=5)
    await fake_redis.set(f"rate:{c.id}:hour", "5")
    result = await send_mod.check_rate_limits(c, fake_redis)
    assert result["ok"] is False
    assert result["reason"] == "hourly_cap"
    assert result["retry_in"] == 60


async def test_rate_limits_blocks_when_day_cap_reached(db_session, fake_redis):
    c = await _make_campaign(db_session, max_per_day=10)
    await fake_redis.set(f"rate:{c.id}:day", "10")
    result = await send_mod.check_rate_limits(c, fake_redis)
    assert result["ok"] is False
    assert result["reason"] == "daily_cap"
    # retry_at is an ISO string at next midnight
    retry = datetime.fromisoformat(result["retry_at"])
    assert retry.hour == 0
    assert retry.minute == 0


async def test_rate_limits_blocks_when_min_delay_not_elapsed(db_session, fake_redis):
    c = await _make_campaign(db_session, min_delay_seconds=120)
    # A prior send already claimed the min-delay gate (still within window).
    await fake_redis.set(f"rate:{c.id}:min_gate", str(time.time()), ex=120)
    result = await send_mod.check_rate_limits(c, fake_redis)
    assert result["ok"] is False
    assert result["reason"] == "min_delay"
    assert 1 <= result["retry_in"] <= 120


async def test_min_delay_gate_is_atomic(db_session, fake_redis):
    """Only one of several concurrent checks claims the min-delay window; the
    rest get a min_delay deferral.  This is what stops the bulk-of-N burst."""
    c = await _make_campaign(db_session, min_delay_seconds=120)
    first = await send_mod.check_rate_limits(c, fake_redis)
    assert first == {"ok": True}                       # claimed the window
    second = await send_mod.check_rate_limits(c, fake_redis)
    assert second["ok"] is False and second["reason"] == "min_delay"  # gate held


async def test_increment_rate_counters_bumps_caps(db_session, fake_redis):
    c = await _make_campaign(db_session, min_delay_seconds=60)
    await send_mod.increment_rate_counters(c, fake_redis)
    assert await fake_redis.get(f"rate:{c.id}:hour") == "1"
    assert await fake_redis.get(f"rate:{c.id}:day") == "1"
    # The min-delay window is claimed in check_rate_limits (min_gate), not here.
    assert await fake_redis.get(f"rate:{c.id}:last_sent") is None


def test_seconds_until_local_midnight():
    secs = send_mod._seconds_until_local_midnight("America/New_York")
    assert 0 < secs <= 86400
    # bad / missing tz → UTC fallback, still bounded
    assert 0 < send_mod._seconds_until_local_midnight("Not/AZone") <= 86400
    assert 0 < send_mod._seconds_until_local_midnight(None) <= 86400


async def test_daily_counter_resets_on_calendar_day(db_session, fake_redis):
    """The daily cap counter expires at the next local midnight (calendar-day
    reset), not a flat rolling 86400."""
    c = await _make_campaign(db_session)  # schedule_timezone="UTC"
    await send_mod.increment_rate_counters(c, fake_redis)
    day_ttl = await fake_redis.ttl(f"rate:{c.id}:day")
    expected = send_mod._seconds_until_local_midnight("UTC")
    assert 0 < day_ttl <= 86400
    assert abs(day_ttl - expected) <= 5


async def test_increment_sets_ttl_only_on_first_call(db_session, fake_redis):
    c = await _make_campaign(db_session)
    await send_mod.increment_rate_counters(c, fake_redis)
    ttl1 = await fake_redis.ttl(f"rate:{c.id}:hour")
    assert 3500 < ttl1 <= 3600

    # Second call increments but should NOT reset the TTL.
    await fake_redis.set(f"rate:{c.id}:hour", "1", ex=100)  # simulate elapsed
    await send_mod.increment_rate_counters(c, fake_redis)
    ttl2 = await fake_redis.ttl(f"rate:{c.id}:hour")
    assert ttl2 <= 100  # didn't get reset to 3600


# --------------------------------------------------------------------------
# send_lead_async — full task paths
# --------------------------------------------------------------------------


def _patch_send_pipeline(fake_redis, message_id: str = "msg-1"):
    """Mock _new_redis to return fake_redis AND brevo.send_email to return msg_id."""
    redis_patch = patch.object(send_mod, "_new_redis", return_value=fake_redis)
    brevo_patch = patch.object(
        send_mod.brevo, "send_email", AsyncMock(return_value=message_id)
    )
    return redis_patch, brevo_patch


async def test_full_send_flow_marks_sent_and_records_message_id(db_session, fake_redis):
    campaign = await _make_campaign(db_session)
    lead = await _make_lead(db_session, campaign)

    rp, bp = _patch_send_pipeline(fake_redis, message_id="brevo-msg-42")
    with rp, bp as send_mock:
        result = await send_mod.send_lead_async(str(lead.id))

    assert result["status"] == "sent"
    assert result["message_id"] == "brevo-msg-42"

    # Brevo received the right shape.
    kwargs = send_mock.call_args.kwargs
    assert kwargs["to_email"] == "lead@example.com"
    assert kwargs["to_name"] == "Jane Doe"
    assert kwargs["subject"] == "Subject"
    assert "<p>Body text</p>" in kwargs["html_body"]
    assert kwargs["text_body"] == "Body text"
    assert kwargs["campaign_id"] == str(campaign.id)
    assert kwargs["lead_id"] == str(lead.id)

    # Lead saved as sent with message_id.
    refreshed = await db_session.scalar(select(Lead).where(Lead.id == lead.id))
    await db_session.refresh(refreshed)
    assert refreshed.send_status == SendStatus.SENT
    assert refreshed.brevo_message_id == "brevo-msg-42"

    # Counters bumped.
    assert await fake_redis.get(f"rate:{campaign.id}:hour") == "1"
    assert await fake_redis.get(f"rate:{campaign.id}:day") == "1"


async def test_send_injects_campaign_own_signature(db_session, fake_redis):
    campaign = await _make_campaign(db_session)
    campaign.signature = "Anthony Colasante\nacme.com"
    await db_session.commit()
    lead = await _make_lead(db_session, campaign, composed_body="Hi,\n\nBody.\n\nBest,\nAI")

    rp, bp = _patch_send_pipeline(fake_redis)
    with rp, bp as send_mock:
        await send_mod.send_lead_async(str(lead.id))
    html = send_mock.call_args.kwargs["html_body"]
    text = send_mock.call_args.kwargs["text_body"]
    assert "Anthony Colasante" in html and "acme.com" in html
    assert "Anthony Colasante" in text


async def test_send_injects_inherited_account_signature(db_session, fake_redis):
    """Regression: a campaign with NO signature of its own must still send
    its connected account's (Settings) signature — previously the send path
    used campaign.signature (None) and stripped the sign-off, sending an
    UNSIGNED email."""
    acc = ConnectedAccount(
        label="Outreach", email_address="me@acme.com",
        imap_host="imap.acme.com", username="me@acme.com",
        password_encrypted="x", signature="Anthony @ Acme\nacme.com/demo",
    )
    db_session.add(acc)
    await db_session.flush()
    campaign = await _make_campaign(db_session)
    campaign.signature = None                 # inherit from the account
    campaign.connected_account_id = acc.id
    await db_session.commit()
    lead = await _make_lead(db_session, campaign, composed_body="Hi,\n\nBody.\n\nBest,\nAI")

    rp, bp = _patch_send_pipeline(fake_redis)
    with rp, bp as send_mock:
        await send_mod.send_lead_async(str(lead.id))
    html = send_mock.call_args.kwargs["html_body"]
    assert "Anthony @ Acme" in html
    assert "acme.com/demo" in html


async def test_suppressed_email_marked_suppressed_without_send(db_session, fake_redis):
    """A suppressed email never reaches Brevo AND lands in the dedicated
    SUPPRESSED terminal state — NOT FAILED.  The distinction matters:
    FAILED leads surface in the campaign error list and get re-enqueued
    by retry-failed (which would deterministically re-fail at the same
    gate); SUPPRESSED leads are deliberately out of the campaign."""
    campaign = await _make_campaign(db_session)
    lead = await _make_lead(db_session, campaign, email="block@example.com")
    db_session.add(Suppression(email="block@example.com", reason=SuppressionReason.UNSUBSCRIBED))
    await db_session.commit()

    rp, bp = _patch_send_pipeline(fake_redis)
    with rp, bp as send_mock:
        result = await send_mod.send_lead_async(str(lead.id))

    assert result["status"] == "suppressed"
    send_mock.assert_not_called()

    refreshed = await db_session.scalar(select(Lead).where(Lead.id == lead.id))
    await db_session.refresh(refreshed)
    assert refreshed.send_status == SendStatus.SUPPRESSED


async def test_paused_campaign_returns_paused_no_send(db_session, fake_redis):
    campaign = await _make_campaign(db_session, status=CampaignStatus.PAUSED)
    lead = await _make_lead(db_session, campaign)

    rp, bp = _patch_send_pipeline(fake_redis)
    with rp, bp as send_mock:
        result = await send_mod.send_lead_async(str(lead.id))

    assert result["status"] == "paused"
    send_mock.assert_not_called()

    refreshed = await db_session.scalar(select(Lead).where(Lead.id == lead.id))
    await db_session.refresh(refreshed)
    # Pending lead stays pending; paused is a re-enqueue signal, not a state change.
    assert refreshed.send_status == SendStatus.PENDING


async def test_outside_schedule_window_marks_scheduled(db_session, fake_redis):
    # Campaign with a 1-minute window that's effectively never current.
    c = Campaign(
        name="x", goal="x", tone="x",
        sender_name="x", sender_email="x@x.com",
        sample_count=1,
        schedule_days=[0, 1, 2, 3, 4],
        # 00:00 – 00:01 UTC will be outside the window almost always
        schedule_time_start=dt_time(0, 0),
        schedule_time_end=dt_time(0, 1),
        schedule_timezone="UTC",
        status=CampaignStatus.RUNNING,
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    lead = await _make_lead(db_session, c)

    # Force "now" to a known time outside the window via compute_next_send_window patch
    fake_eta = pytz.UTC.localize(datetime(2026, 5, 13, 0, 0))
    with patch.object(send_mod, "compute_next_send_window", return_value=fake_eta):
        rp, bp = _patch_send_pipeline(fake_redis)
        with rp, bp as send_mock:
            result = await send_mod.send_lead_async(str(lead.id))

    assert result["status"] == "scheduled"
    assert result["eta"] == fake_eta.isoformat()
    send_mock.assert_not_called()

    refreshed = await db_session.scalar(select(Lead).where(Lead.id == lead.id))
    await db_session.refresh(refreshed)
    assert refreshed.send_status == SendStatus.SCHEDULED
    assert refreshed.scheduled_send_at is not None


async def test_rate_limited_returns_without_sending(db_session, fake_redis):
    campaign = await _make_campaign(db_session, max_per_hour=5)
    await fake_redis.set(f"rate:{campaign.id}:hour", "5")
    lead = await _make_lead(db_session, campaign)

    rp, bp = _patch_send_pipeline(fake_redis)
    with rp, bp as send_mock:
        result = await send_mod.send_lead_async(str(lead.id))

    assert result["status"] == "rate_limited"
    assert result["reason"] == "hourly_cap"
    send_mock.assert_not_called()

    refreshed = await db_session.scalar(select(Lead).where(Lead.id == lead.id))
    await db_session.refresh(refreshed)
    assert refreshed.send_status == SendStatus.PENDING


async def test_already_sent_lead_is_idempotent(db_session, fake_redis):
    campaign = await _make_campaign(db_session)
    lead = await _make_lead(db_session, campaign)
    lead.send_status = SendStatus.SENT
    await db_session.commit()

    rp, bp = _patch_send_pipeline(fake_redis)
    with rp, bp as send_mock:
        result = await send_mod.send_lead_async(str(lead.id))

    assert result == {"status": "already_sent"}
    send_mock.assert_not_called()


async def test_missing_lead_returns_not_found(db_session, fake_redis):
    rp, bp = _patch_send_pipeline(fake_redis)
    with rp, bp:
        result = await send_mod.send_lead_async(str(uuid.uuid4()))
    assert result == {"status": "not_found"}


# --------------------------------------------------------------------------
# Celery wrapper re-enqueue routing
# --------------------------------------------------------------------------


async def test_celery_wrapper_does_not_reenqueue_on_paused(db_session, fake_redis):
    """Paused is a hard stop — no self-re-enqueue.  The previous behaviour
    (apply_async countdown=300) kept every paused-campaign lead in a 5-min
    retry loop, burning queue depth while the campaign was supposed to be
    "off".  Resume re-enqueues every composed PENDING+SCHEDULED lead, so
    pause stops cleanly and resume IS the trigger."""
    import asyncio as _asyncio
    campaign = await _make_campaign(db_session, status=CampaignStatus.PAUSED)
    lead = await _make_lead(db_session, campaign)
    lead_id = str(lead.id)

    rp, bp = _patch_send_pipeline(fake_redis)
    with rp, bp, patch.object(send_mod.send_lead, "apply_async") as enqueue:
        # send_lead.run() invokes asyncio.run() internally, which refuses to run
        # inside pytest-asyncio's loop. Hop to a worker thread.
        result = await _asyncio.to_thread(send_mod.send_lead.run, lead_id)

    assert result["status"] == "paused"
    enqueue.assert_not_called()


async def test_celery_wrapper_does_not_reenqueue_on_scheduled(db_session, fake_redis):
    """The wrapper no longer self-re-enqueues an out-of-window (scheduled)
    send — the beat-driven ``pace_first_emails`` pacer re-feeds the lead when
    the window opens.  Self-re-enqueue with a far-future eta was the storm
    source (broker redelivers past visibility_timeout)."""
    import asyncio as _asyncio
    c = Campaign(
        name="x", goal="x", tone="x",
        sender_name="x", sender_email="x@x.com",
        sample_count=1,
        schedule_days=[0, 1, 2, 3, 4],
        schedule_time_start=dt_time(0, 0),
        schedule_time_end=dt_time(0, 1),
        schedule_timezone="UTC",
        status=CampaignStatus.RUNNING,
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    lead = await _make_lead(db_session, c)
    lead_id = str(lead.id)

    fake_eta = pytz.UTC.localize(datetime(2026, 5, 13, 9, 0))
    with patch.object(send_mod, "compute_next_send_window", return_value=fake_eta):
        rp, bp = _patch_send_pipeline(fake_redis)
        with rp, bp, patch.object(send_mod.send_lead, "apply_async") as enqueue:
            result = await _asyncio.to_thread(send_mod.send_lead.run, lead_id)

    # Lead is marked SCHEDULED (so the pacer picks it up) but NOT re-enqueued.
    assert result["status"] == "scheduled"
    enqueue.assert_not_called()


# --------------------------------------------------------------------------
# First-email pacer (pace_first_emails)
# --------------------------------------------------------------------------


async def test_pacer_feeds_one_lead_per_window_when_gate_open(db_session, fake_redis):
    """With a min-delay and the gate open, the pacer feeds exactly ONE lead
    (the gate admits one per window); the rest wait for the next tick."""
    c = await _make_campaign(db_session, min_delay_seconds=240, max_per_day=100, max_per_hour=15)
    for i in range(3):
        await _make_lead(db_session, c, email=f"p{i}@x.com")

    rp, _ = _patch_send_pipeline(fake_redis)
    with rp, patch.object(send_mod.send_lead, "delay") as dispatch:
        result = await send_mod._pace_first_emails_async()

    assert dispatch.call_count == 1
    assert result["dispatched"] == 1


async def test_pacer_skips_when_min_gate_held(db_session, fake_redis):
    """When a send is mid-window (min_gate set), the pacer feeds nothing."""
    c = await _make_campaign(db_session, min_delay_seconds=240)
    await _make_lead(db_session, c)
    await fake_redis.set(f"rate:{c.id}:min_gate", str(time.time()), ex=240)

    rp, _ = _patch_send_pipeline(fake_redis)
    with rp, patch.object(send_mod.send_lead, "delay") as dispatch:
        await send_mod._pace_first_emails_async()

    dispatch.assert_not_called()


async def test_pacer_skips_at_daily_cap(db_session, fake_redis):
    c = await _make_campaign(db_session, min_delay_seconds=240, max_per_day=100)
    await _make_lead(db_session, c)
    await fake_redis.set(f"rate:{c.id}:day", "100")

    rp, _ = _patch_send_pipeline(fake_redis)
    with rp, patch.object(send_mod.send_lead, "delay") as dispatch:
        await send_mod._pace_first_emails_async()

    dispatch.assert_not_called()


async def test_pacer_no_delay_feeds_batch_bounded_by_hourly_headroom(db_session, fake_redis):
    """No min-delay → feed a small batch, but never more than the hourly cap
    headroom (so the cap can't be blown by one tick)."""
    c = await _make_campaign(db_session, min_delay_seconds=0, max_per_hour=5)
    for i in range(20):
        await _make_lead(db_session, c, email=f"b{i}@x.com")
    await fake_redis.set(f"rate:{c.id}:hour", "3")  # 2 slots left this hour

    rp, _ = _patch_send_pipeline(fake_redis)
    with rp, patch.object(send_mod.send_lead, "delay") as dispatch:
        await send_mod._pace_first_emails_async()

    assert dispatch.call_count == 2


async def test_pacer_excludes_unsendable_and_non_running(db_session, fake_redis):
    """Pacer skips: empty-body leads (non-email entry), suppressed leads, and
    leads on paused campaigns."""
    # Paused campaign → never fed.
    paused = await _make_campaign(db_session, status=CampaignStatus.PAUSED, min_delay_seconds=0)
    await _make_lead(db_session, paused, email="paused@x.com")

    # Running campaign with a mix of sendable / unsendable leads.
    c = await _make_campaign(db_session, min_delay_seconds=0, max_per_hour=50)
    good = await _make_lead(db_session, c, email="good@x.com")
    # Empty composed body (e.g. non-email entry node) → excluded.
    await _make_lead(db_session, c, email="empty@x.com", composed_body="")
    # Suppressed → excluded.
    await _make_lead(db_session, c, email="supp@x.com")
    db_session.add(Suppression(email="supp@x.com", reason=SuppressionReason.MANUAL))
    await db_session.commit()

    rp, _ = _patch_send_pipeline(fake_redis)
    with rp, patch.object(send_mod.send_lead, "delay") as dispatch:
        await send_mod._pace_first_emails_async()

    dispatched = {call.args[0] for call in dispatch.call_args_list}
    assert dispatched == {str(good.id)}
