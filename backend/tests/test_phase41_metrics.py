"""Phase 4 — canonical metric helper (the consolidated `_rate`)."""
from app.services.metrics import rate


def test_rate_normal():
    assert rate(1, 2) == 0.5
    assert rate(1, 3) == 0.3333  # rounded to 4


def test_rate_zero_or_negative_denominator_is_none():
    assert rate(5, 0) is None
    assert rate(5, -1) is None


def test_rate_none_denominator_is_none():
    assert rate(5, None) is None


def test_rate_respects_ndigits():
    assert rate(1, 3, ndigits=2) == 0.33


def test_routers_share_one_rate():
    # All three reporting routers import the same canonical helper now.
    from app.routers.analytics import _rate as a
    from app.routers.campaigns import _rate as c
    from app.routers.reports import _rate as r
    assert a is rate and c is rate and r is rate
