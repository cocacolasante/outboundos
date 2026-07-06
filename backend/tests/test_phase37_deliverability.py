"""Phase 37 — deliverability guard (Feature B).

Three subsystems:
1. Per-sending-domain throttle: the DOMAIN caps defer a send even when
   the campaign's own caps have headroom; counters bump together.
2. Send-time optimization: defers to the recipient's optimal local hour
   inside the campaign window; respects ``lead.timezone``; returns None
   when "now" already qualifies.
3. Bounce/spam circuit breaker: trips at threshold + min sample only,
   idempotent, requires a human resume (which clears the marker).
"""
from __future__ import annotations

import uuid
from datetime import datetime, time as dt_time, timedelta, timezone
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest
import pytz
from sqlalchemy import select

from app.config import settings
from app.models import (
    Campaign,
    CampaignStatus,
    ComposeStatus,
    EmailEvent,
    EmailEventType,
    Lead,
    Notification,
    NotificationKind,
)
from app.services import deliverability
from app.workers import send as send_mod

pytestmark = pytest.mark.asyncio


@pytest.fixture
def fake_redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


async def _make_campaign(db_session, **kw) -> Campaign:
    defaults = dict(
        name="P37", goal="g", tone="t",
        sender_name="S", sender_email="from@acme-mail.com",
        sample_count=1,
        schedule_days=[], schedule_time_start=dt_time(0, 0),
        schedule_time_end=dt_time(23, 59, 59), schedule_timezone="UTC",
        status=CampaignStatus.RUNNING, min_delay_seconds=0,
    )
    defaults.update(kw)
    c = Campaign(**defaults)
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    return c


async def _make_lead(db_session, campaign, **kw) -> Lead:
    defaults = dict(
        campaign_id=campaign.id, email=f"l{uuid.uuid4().hex[:6]}@x.com",
        compose_status=ComposeStatus.DONE,
        composed_subject="S", composed_body="B",
    )
    defaults.update(kw)
    lead = Lead(**defaults)
    db_session.add(lead)
    await db_session.commit()
    await db_session.refresh(lead)
    return lead


# --------------------------------------------------------------------------
# 1. Per-domain throttle
# --------------------------------------------------------------------------


async def test_domain_hourly_cap_defers_when_campaign_has_headroom(
    db_session, fake_redis, monkeypatch,
):
    monkeypatch.setattr(settings, "DOMAIN_MAX_PER_HOUR", 5)
    campaign = await _make_campaign(db_session, max_per_hour=100)
    # Another campaign already burned the domain's hourly budget.
    await fake_redis.set("rate:domain:acme-mail.com:hour", "5")

    result = await send_mod.check_rate_limits(
        campaign, fake_redis, domain="acme-mail.com",
    )
    assert result["ok"] is False
    assert result["reason"] == "domain_hourly_cap"


async def test_domain_daily_cap_defers_to_tomorrow(db_session, fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "DOMAIN_MAX_PER_DAY", 10)
    campaign = await _make_campaign(db_session)
    await fake_redis.set("rate:domain:acme-mail.com:day", "10")

    result = await send_mod.check_rate_limits(
        campaign, fake_redis, domain="acme-mail.com",
    )
    assert result["reason"] == "domain_daily_cap"
    assert "retry_at" in result


async def test_no_domain_means_no_domain_check(db_session, fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "DOMAIN_MAX_PER_HOUR", 0)  # would trip instantly
    campaign = await _make_campaign(db_session)
    result = await send_mod.check_rate_limits(campaign, fake_redis, domain=None)
    assert result["ok"] is True


async def test_increment_bumps_domain_counters_alongside_campaign(
    db_session, fake_redis,
):
    campaign = await _make_campaign(db_session)
    await send_mod.increment_rate_counters(campaign, fake_redis, domain="acme-mail.com")
    assert await fake_redis.get(f"rate:{campaign.id}:hour") == "1"
    assert await fake_redis.get("rate:domain:acme-mail.com:hour") == "1"
    assert await fake_redis.get("rate:domain:acme-mail.com:day") == "1"


async def test_resolve_sending_domain_prefers_connected_account(db_session):
    from app.models import ConnectedAccount
    from app.services import encryption

    acc = ConnectedAccount(
        label="x", email_address="me@inbox-domain.io",
        imap_host="h", username="me@inbox-domain.io",
        password_encrypted=encryption.encrypt("pw"),
    )
    db_session.add(acc)
    await db_session.flush()
    campaign = await _make_campaign(db_session, connected_account_id=acc.id)
    assert await send_mod.resolve_sending_domain(db_session, campaign) == "inbox-domain.io"

    # No account → falls back to sender_email's domain.
    campaign2 = await _make_campaign(db_session)
    assert await send_mod.resolve_sending_domain(db_session, campaign2) == "acme-mail.com"


# --------------------------------------------------------------------------
# 2. Send-time optimization
# --------------------------------------------------------------------------


async def test_optimal_eta_defers_to_recipient_local_morning(db_session):
    """A lead in America/Los_Angeles gets deferred to its LOCAL morning
    window even when the campaign runs on Eastern time."""
    campaign = await _make_campaign(
        db_session, schedule_timezone="America/New_York",
        send_time_optimization=True,
    )
    lead = await _make_lead(db_session, campaign, timezone="America/Los_Angeles")

    # 2026-06-15 is a Monday; 20:00 UTC = 13:00 PDT (after the morning
    # window) → expect deferral to Tuesday 9am PDT.
    now = datetime(2026, 6, 15, 20, 0, tzinfo=timezone.utc)
    eta = await send_mod.compute_optimal_send_eta(db_session, lead, campaign, now=now)
    assert eta is not None
    la = pytz.timezone("America/Los_Angeles")
    eta_local = eta.astimezone(la)
    assert eta_local.hour in send_mod.DEFAULT_OPTIMAL_HOURS
    assert eta_local.weekday() in send_mod.DEFAULT_OPTIMAL_DAYS
    assert eta > now


async def test_optimal_eta_none_when_already_optimal(db_session):
    campaign = await _make_campaign(
        db_session, schedule_timezone="UTC", send_time_optimization=True,
    )
    lead = await _make_lead(db_session, campaign, timezone="UTC")
    # Tuesday 2026-06-16 at 10:00 UTC — inside the default 9-11 Tue-Thu.
    now = datetime(2026, 6, 16, 10, 0, tzinfo=timezone.utc)
    eta = await send_mod.compute_optimal_send_eta(db_session, lead, campaign, now=now)
    assert eta is None


async def test_optimal_eta_uses_lead_engagement_hours(db_session):
    """A lead with its own open history gets its personal hour, any day."""
    campaign = await _make_campaign(db_session, send_time_optimization=True)
    lead = await _make_lead(db_session, campaign, timezone="UTC")
    # Two opens at 15:xx UTC → 15 becomes the lead's optimal hour.
    for _ in range(2):
        db_session.add(EmailEvent(
            lead_id=lead.id, campaign_id=campaign.id,
            event_type=EmailEventType.OPENED,
            occurred_at=datetime(2026, 6, 10, 15, 30, tzinfo=timezone.utc),
        ))
    await db_session.commit()

    # Friday 08:00 UTC — engagement-derived hours ignore the Tue-Thu rule,
    # so the ETA should be Friday 15:xx, not next Tuesday.
    now = datetime(2026, 6, 19, 8, 0, tzinfo=timezone.utc)
    eta = await send_mod.compute_optimal_send_eta(db_session, lead, campaign, now=now)
    assert eta is not None
    assert eta.astimezone(pytz.UTC).hour == 15
    assert eta.date() == now.date()


async def test_gates_defer_with_optimized_flag(db_session, fake_redis):
    campaign = await _make_campaign(
        db_session, send_time_optimization=True, schedule_timezone="UTC",
    )
    lead = await _make_lead(db_session, campaign, timezone="UTC")

    fixed_eta = datetime(2026, 6, 16, 9, 7, tzinfo=timezone.utc)
    with patch.object(
        send_mod, "compute_optimal_send_eta", new=AsyncMock(return_value=fixed_eta),
    ):
        gates = await send_mod.check_send_gates(db_session, lead, campaign, fake_redis)
    assert gates["ok"] is False
    assert gates["reason"] == "scheduled"
    assert gates["optimized"] is True
    assert gates["retry_at"] == fixed_eta.isoformat()


async def test_gates_skip_optimization_when_toggle_off(db_session, fake_redis):
    campaign = await _make_campaign(db_session, send_time_optimization=False)
    lead = await _make_lead(db_session, campaign)
    with patch.object(
        send_mod, "compute_optimal_send_eta",
        new=AsyncMock(return_value=datetime(2030, 1, 1, tzinfo=timezone.utc)),
    ) as opt_mock:
        gates = await send_mod.check_send_gates(db_session, lead, campaign, fake_redis)
    assert gates["ok"] is True
    opt_mock.assert_not_awaited()
    # OK result carries the resolved domain for the counter bump.
    assert gates["domain"] == "acme-mail.com"


# --------------------------------------------------------------------------
# 3. Circuit breaker
# --------------------------------------------------------------------------


async def _seed_events(db_session, campaign, *, delivered=0, hard_bounce=0, spam=0):
    lead = await _make_lead(db_session, campaign)
    for etype, n in [
        (EmailEventType.DELIVERED, delivered),
        (EmailEventType.HARD_BOUNCE, hard_bounce),
        (EmailEventType.SPAM, spam),
    ]:
        for _ in range(n):
            db_session.add(EmailEvent(
                lead_id=lead.id, campaign_id=campaign.id, event_type=etype,
            ))
    await db_session.commit()


async def test_breaker_trips_at_bounce_threshold(db_session):
    campaign = await _make_campaign(db_session)
    # 20 outcomes, 2 hard bounces = 10% ≥ 5% threshold.
    await _seed_events(db_session, campaign, delivered=18, hard_bounce=2)

    verdict = await deliverability.evaluate_campaign_health(db_session, campaign.id)
    assert verdict.trip is True
    assert "bounce" in verdict.reason.lower()

    tripped = await deliverability.trip_breaker(db_session, campaign, verdict)
    await db_session.commit()
    assert tripped is True
    assert campaign.status is CampaignStatus.PAUSED
    assert campaign.auto_paused_at is not None
    assert "10.0%" in campaign.auto_pause_reason

    notif = await db_session.scalar(select(Notification).where(
        Notification.kind == NotificationKind.CAMPAIGN_AUTO_PAUSED,
    ))
    assert notif is not None
    assert campaign.name in notif.title


async def test_breaker_needs_min_sample(db_session):
    campaign = await _make_campaign(db_session)
    # 100% bounce rate but only 3 outcomes — below MIN_SAMPLE (20).
    await _seed_events(db_session, campaign, hard_bounce=3)
    verdict = await deliverability.evaluate_campaign_health(db_session, campaign.id)
    assert verdict.trip is False
    assert verdict.sample == 3


async def test_breaker_not_below_rate(db_session):
    campaign = await _make_campaign(db_session)
    # 25 outcomes, 1 bounce = 4% < 5%.
    await _seed_events(db_session, campaign, delivered=24, hard_bounce=1)
    verdict = await deliverability.evaluate_campaign_health(db_session, campaign.id)
    assert verdict.trip is False


async def test_breaker_does_not_double_pause(db_session):
    campaign = await _make_campaign(db_session)
    await _seed_events(db_session, campaign, delivered=10, hard_bounce=10)
    verdict = await deliverability.evaluate_campaign_health(db_session, campaign.id)
    assert await deliverability.trip_breaker(db_session, campaign, verdict) is True
    await db_session.commit()
    first_paused_at = campaign.auto_paused_at

    # Second evaluation: already tripped → no-op, timestamp unchanged.
    assert await deliverability.trip_breaker(db_session, campaign, verdict) is False
    assert campaign.auto_paused_at == first_paused_at


async def test_breaker_leaves_manual_pause_alone(db_session):
    campaign = await _make_campaign(db_session, status=CampaignStatus.PAUSED)
    await _seed_events(db_session, campaign, delivered=10, hard_bounce=10)
    verdict = await deliverability.evaluate_campaign_health(db_session, campaign.id)
    assert await deliverability.trip_breaker(db_session, campaign, verdict) is False
    assert campaign.auto_paused_at is None  # the human's pause, untouched


async def test_resume_clears_breaker_state(client, db_session):
    campaign = await _make_campaign(
        db_session, status=CampaignStatus.PAUSED,
        auto_paused_at=datetime.now(timezone.utc),
        auto_pause_reason="Hard-bounce rate 10% ...",
    )
    resp = await client.post(f"/campaigns/{campaign.id}/resume")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "running"
    assert body["auto_paused_at"] is None
    assert body["auto_pause_reason"] is None


async def test_process_event_hook_trips_breaker(db_session, monkeypatch):
    """A fresh hard-bounce event re-checks the campaign inline."""
    from app.services import brevo_events

    monkeypatch.setattr(settings, "CIRCUIT_BREAKER_MIN_SAMPLE", 5)
    campaign = await _make_campaign(db_session)
    # 4 delivered + 1 existing bounce; the 5th outcome (a new bounce on a
    # fresh lead) crosses 5/6 sample with 2/6 = 33% bounce rate.
    await _seed_events(db_session, campaign, delivered=4, hard_bounce=1)
    bouncer = await _make_lead(db_session, campaign, brevo_message_id="msg-trip-1")

    handled = await brevo_events.process_event(db_session, {
        "event": "hard_bounce", "messageId": "msg-trip-1",
    })
    await db_session.commit()
    assert handled is True
    await db_session.refresh(campaign)
    assert campaign.status is CampaignStatus.PAUSED
    assert campaign.auto_pause_reason is not None


async def test_sweep_health_checks_running_campaigns(db_session, monkeypatch):
    from app.workers.deliverability import _sweep_health_async

    campaign = await _make_campaign(db_session)
    await _seed_events(db_session, campaign, delivered=10, hard_bounce=10)

    # Point the sweep at the test DB.
    from tests.conftest import TEST_DATABASE_URL
    from app.workers import deliverability as deliv_worker
    monkeypatch.setattr(deliv_worker.settings, "DATABASE_URL", TEST_DATABASE_URL)

    result = await _sweep_health_async()
    assert str(campaign.id) in result["tripped"]
    await db_session.refresh(campaign)
    assert campaign.status is CampaignStatus.PAUSED
