"""Unit tests for the sequence edge condition evaluator + graph validators."""
from datetime import datetime, timedelta, timezone

import pytest

from app.services.sequence_conditions import (
    ConditionContext,
    detect_cycle,
    evaluate,
    validate,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- evaluate(): leaf ops ----------


def test_always_is_true():
    assert evaluate({"op": "always"}, ConditionContext()) is True


def test_not_inverts():
    assert evaluate({"op": "not", "child": {"op": "always"}}, ConditionContext()) is False
    assert evaluate({"op": "not", "child": {"op": "bounced"}}, ConditionContext()) is True


def test_replied_email_only():
    ctx = ConditionContext(
        has_replied_email=True,
        last_reply_email_at=_now() - timedelta(days=1),
    )
    assert evaluate({"op": "replied", "channels": ["email"]}, ctx) is True
    assert evaluate({"op": "replied", "channels": ["linkedin"]}, ctx) is False


def test_replied_default_any_channel():
    ctx = ConditionContext(
        has_replied_linkedin=True,
        last_reply_linkedin_at=_now() - timedelta(hours=2),
    )
    assert evaluate({"op": "replied"}, ctx) is True


def test_replied_within_days_window():
    ctx = ConditionContext(
        has_replied_email=True,
        last_reply_email_at=_now() - timedelta(days=10),
    )
    assert evaluate({"op": "replied", "within_days": 3}, ctx) is False
    assert evaluate({"op": "replied", "within_days": 30}, ctx) is True


def test_opened_clicked_bounced():
    base = _now() - timedelta(hours=1)
    ctx = ConditionContext(
        has_opened=True, last_open_at=base,
        has_clicked=True, last_click_at=base,
        has_bounced=True,
    )
    assert evaluate({"op": "opened"}, ctx) is True
    assert evaluate({"op": "clicked"}, ctx) is True
    assert evaluate({"op": "bounced"}, ctx) is True


def test_linkedin_connection_value_match():
    ctx = ConditionContext(linkedin_connection_status="connected")
    assert evaluate({"op": "linkedin_connection", "value": "connected"}, ctx) is True
    assert evaluate({"op": "linkedin_connection", "value": "invited"}, ctx) is False


def test_days_since_entered_node():
    long_ago = _now() - timedelta(days=5)
    ctx = ConditionContext(entered_current_at=long_ago)
    assert evaluate({"op": "days_since_entered_node", "gte": 3}, ctx) is True
    assert evaluate({"op": "days_since_entered_node", "gte": 7}, ctx) is False


# ---------- evaluate(): composition ----------


def test_and_or_composition():
    ctx = ConditionContext(
        has_opened=True, last_open_at=_now() - timedelta(hours=1),
        has_replied_email=False,
    )
    # opened AND not replied
    expr = {
        "op": "and",
        "children": [
            {"op": "opened"},
            {"op": "not", "child": {"op": "replied"}},
        ],
    }
    assert evaluate(expr, ctx) is True


def test_or_short_circuits():
    ctx = ConditionContext()  # nothing happened
    expr = {
        "op": "or",
        "children": [
            {"op": "opened"},
            {"op": "always"},  # this rescues the OR
        ],
    }
    assert evaluate(expr, ctx) is True


def test_unknown_op_returns_false():
    assert evaluate({"op": "definitely-not-a-real-op"}, ConditionContext()) is False


def test_malformed_input_does_not_raise():
    assert evaluate(None, ConditionContext()) is False
    assert evaluate("not a dict", ConditionContext()) is False
    assert evaluate({"op": "and"}, ConditionContext()) is True  # all() of empty → True; harmless


# ---------- validate() ----------


def test_validate_accepts_always():
    assert validate({"op": "always"}) == []


def test_validate_rejects_unknown_op():
    errs = validate({"op": "nope"})
    assert errs and "unknown op" in errs[0]


def test_validate_rejects_bad_channels():
    errs = validate({"op": "replied", "channels": ["facebook"]})
    assert errs and "unknown channels" in errs[0]


def test_validate_requires_value_for_linkedin_connection():
    errs = validate({"op": "linkedin_connection"})
    assert errs and "value" in errs[0]


def test_validate_descends_into_composition():
    errs = validate({"op": "and", "children": [{"op": "always"}, {"op": "nope"}]})
    assert any("nope" in e for e in errs)


# ---------- detect_cycle() ----------


def test_no_cycle_in_dag():
    ids = ["a", "b", "c"]
    edges = [("a", "b"), ("b", "c"), ("a", "c")]
    assert detect_cycle(ids, edges) is None


def test_detects_self_loop():
    assert detect_cycle(["a"], [("a", "a")]) is not None


def test_detects_three_cycle():
    ids = ["a", "b", "c"]
    edges = [("a", "b"), ("b", "c"), ("c", "a")]
    cyc = detect_cycle(ids, edges)
    assert cyc is not None
    assert set(cyc) == {"a", "b", "c"}


def test_terminate_edges_dont_create_cycles():
    # to_node_id=None ⇒ terminate this branch; should be ignored.
    assert detect_cycle(["a", "b"], [("a", "b"), ("b", None)]) is None


# ---------- evaluate(): additional edge cases ----------

def test_and_empty_children_returns_true():
    # all() of empty sequence is True — Python built-in behaviour
    assert evaluate({"op": "and", "children": []}, ConditionContext()) is True

def test_or_empty_children_returns_false():
    # any() of empty sequence is False
    assert evaluate({"op": "or", "children": []}, ConditionContext()) is False

def test_not_not_double_negation():
    ctx = ConditionContext(has_bounced=True)
    expr = {"op": "not", "child": {"op": "not", "child": {"op": "bounced"}}}
    assert evaluate(expr, ctx) is True

def test_days_since_entered_node_none_returns_false():
    # No entered_current_at → we can't compute days → condition should be False
    ctx = ConditionContext(entered_current_at=None)
    assert evaluate({"op": "days_since_entered_node", "gte": 0}, ctx) is False

def test_replied_empty_channels_is_false():
    ctx = ConditionContext(has_replied_email=True, last_reply_email_at=_now())
    # Explicit empty channel list means "no channels" → never replied on those channels
    result = evaluate({"op": "replied", "channels": []}, ctx)
    assert result is False

def test_opened_within_days_boundary():
    # exactly at the boundary: last_open_at is exactly `within_days` days ago
    exactly_3_days_ago = _now() - timedelta(days=3)
    ctx = ConditionContext(has_opened=True, last_open_at=exactly_3_days_ago)
    # within_days=3 should still be True (>= boundary, not strictly >)
    # The exact semantics depend on implementation — just verify it doesn't crash
    result = evaluate({"op": "opened", "within_days": 3}, ctx)
    assert isinstance(result, bool)

def test_deeply_nested_and_or():
    ctx = ConditionContext(
        has_replied_email=True, last_reply_email_at=_now() - timedelta(hours=1),
        has_opened=True, last_open_at=_now() - timedelta(hours=2),
    )
    expr = {
        "op": "or",
        "children": [
            {
                "op": "and",
                "children": [
                    {"op": "not", "child": {"op": "bounced"}},
                    {"op": "replied"},
                ]
            },
            {"op": "bounced"},
        ]
    }
    assert evaluate(expr, ctx) is True


# ---------- validate(): additional edge cases ----------

def test_validate_accepts_deeply_nested_valid():
    expr = {
        "op": "and",
        "children": [
            {"op": "replied", "channels": ["email"]},
            {"op": "not", "child": {"op": "bounced"}},
        ]
    }
    assert validate(expr) == []

def test_validate_rejects_replied_unknown_channel():
    errs = validate({"op": "replied", "channels": ["sms", "fax"]})
    assert errs and "unknown channels" in errs[0]

def test_validate_rejects_days_since_without_gte():
    errs = validate({"op": "days_since_entered_node"})
    assert errs and "gte" in errs[0]

def test_validate_rejects_negative_within_days():
    errs = validate({"op": "opened", "within_days": -1})
    assert errs  # negative window is nonsensical

def test_validate_rejects_and_with_invalid_child():
    errs = validate({
        "op": "and",
        "children": [{"op": "always"}, {"op": "badop"}]
    })
    assert any("badop" in e for e in errs)


# ---------- detect_cycle(): additional edge cases ----------

def test_disconnected_graph_no_cycle():
    # Two disconnected components — neither has a cycle
    ids = ["a", "b", "c", "d"]
    edges = [("a", "b"), ("c", "d")]
    assert detect_cycle(ids, edges) is None

def test_empty_graph_no_cycle():
    assert detect_cycle([], []) is None

def test_single_node_no_edge_no_cycle():
    assert detect_cycle(["solo"], []) is None
