"""Phase 9: send-window scheduler unit tests."""
from datetime import datetime, time

import pytz

from app.models import Campaign
from app.workers.send import compute_next_send_window


def _make_campaign(
    *,
    days: list[int],
    start: time = time(9, 0),
    end: time = time(17, 0),
    tz: str = "UTC",
) -> Campaign:
    return Campaign(
        name="x", goal="x", tone="x",
        sender_name="x", sender_email="x@x.com",
        schedule_days=days, schedule_time_start=start, schedule_time_end=end,
        schedule_timezone=tz,
    )


# 2026-05-12 is a Tuesday (weekday=1). Reference uses that consistently.
TUE = datetime(2026, 5, 12, 10, 30)  # Tue 10:30 — inside default 9–17 window
SAT = datetime(2026, 5, 16, 10, 30)  # Saturday


def test_inside_window_returns_none():
    c = _make_campaign(days=[0, 1, 2, 3, 4])
    assert compute_next_send_window(c, now=TUE) is None


def test_before_window_returns_today_at_start():
    c = _make_campaign(days=[0, 1, 2, 3, 4])
    now = datetime(2026, 5, 12, 7, 0)  # Tue 07:00 (before 09:00)
    eta = compute_next_send_window(c, now=now)
    assert eta is not None
    assert eta.year == 2026 and eta.month == 5 and eta.day == 12
    assert eta.time() == time(9, 0)


def test_after_window_jumps_to_next_allowed_day():
    c = _make_campaign(days=[0, 1, 2, 3, 4])  # Mon–Fri
    now = datetime(2026, 5, 12, 19, 0)  # Tue 19:00 (after 17:00)
    eta = compute_next_send_window(c, now=now)
    assert eta is not None
    assert eta.day == 13  # Wed
    assert eta.time() == time(9, 0)


def test_weekend_now_skips_to_monday():
    c = _make_campaign(days=[0, 1, 2, 3, 4])  # weekdays only
    eta = compute_next_send_window(c, now=SAT)
    assert eta is not None
    # Sat 2026-05-16 + 2 days = Mon 2026-05-18
    assert eta.day == 18
    assert eta.weekday() == 0
    assert eta.time() == time(9, 0)


def test_empty_days_means_all_days_allowed():
    c = _make_campaign(days=[])
    # Saturday, inside time window — empty days → allowed.
    eta = compute_next_send_window(c, now=SAT)
    assert eta is None


def test_friday_after_hours_skips_to_monday():
    c = _make_campaign(days=[0, 1, 2, 3, 4])
    # Friday 2026-05-15 at 18:00 → next is Mon 2026-05-18 at 09:00
    fri = datetime(2026, 5, 15, 18, 0)
    eta = compute_next_send_window(c, now=fri)
    assert eta is not None
    assert eta.day == 18
    assert eta.time() == time(9, 0)


def test_respects_timezone():
    # 14:00 UTC == 09:00 EST. So a 9–17 EST window with UTC=14:00 → inside.
    c = _make_campaign(
        days=[0, 1, 2, 3, 4],
        start=time(9, 0), end=time(17, 0),
        tz="America/New_York",
    )
    utc_14 = pytz.UTC.localize(datetime(2026, 5, 12, 14, 0))
    assert compute_next_send_window(c, now=utc_14) is None

    utc_5 = pytz.UTC.localize(datetime(2026, 5, 12, 5, 0))  # 01:00 EST → before
    eta = compute_next_send_window(c, now=utc_5)
    assert eta is not None
    # eta should be 09:00 EST same day, in EST timezone
    assert eta.tzinfo.zone == "America/New_York"
    assert eta.hour == 9
