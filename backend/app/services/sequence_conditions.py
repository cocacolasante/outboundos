"""Edge condition expression language for sequence routing.

An edge's `condition` column is a JSON document. ``evaluate(condition, ctx)``
returns True/False. The evaluator never raises on bad input — it returns
False and lets the caller decide what to do.

Grammar (all keys lowercase, all op names lowercase):

  always                {"op": "always"}
  not                   {"op": "not", "child": <expr>}
  and / or              {"op": "and"|"or", "children": [<expr>, ...]}

  Lead-state leaves (no parameters except where noted):
    replied             {"op": "replied", "channels": ["email","linkedin"], "within_days": 7}
                        channels defaults to ["email","linkedin"]; within_days optional
    opened              {"op": "opened", "within_days": 3}
    clicked             {"op": "clicked", "within_days": 3}
    bounced             {"op": "bounced"}
    linkedin_connection {"op": "linkedin_connection", "value": "connected"}
    days_since_entered_node {"op": "days_since_entered_node", "gte": 3}

Context shape (``ConditionContext``):
    has_replied_email: bool
    last_reply_email_at: datetime | None
    has_opened: bool
    last_open_at: datetime | None
    has_clicked: bool
    last_click_at: datetime | None
    has_bounced: bool
    linkedin_connection_status: str
    has_replied_linkedin: bool
    last_reply_linkedin_at: datetime | None
    entered_current_at: datetime | None
    now: datetime
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ConditionContext:
    """Per-lead state snapshot used to evaluate edge conditions."""
    has_replied_email: bool = False
    last_reply_email_at: datetime | None = None
    has_opened: bool = False
    last_open_at: datetime | None = None
    has_clicked: bool = False
    last_click_at: datetime | None = None
    has_bounced: bool = False
    linkedin_connection_status: str = "unknown"
    has_replied_linkedin: bool = False
    last_reply_linkedin_at: datetime | None = None
    entered_current_at: datetime | None = None
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _within_days(occurred_at: datetime | None, days: float | int | None, now: datetime) -> bool:
    """Return True when an event occurred within `days` of `now`. When `days`
    is None, just checks the event occurred at all.
    """
    if occurred_at is None:
        return False
    if days is None:
        return True
    return now - _aware(occurred_at, now.tzinfo) <= timedelta(days=float(days))


def _aware(dt: datetime, tz) -> datetime:
    """Coerce naive datetimes into the given tz (assume UTC if none provided)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz or timezone.utc)
    return dt


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

VALID_OPS = {
    "always", "not", "and", "or",
    "replied", "opened", "clicked", "bounced",
    "linkedin_connection", "days_since_entered_node",
}

VALID_CHANNELS = {"email", "linkedin"}

VALID_LINKEDIN_STATUSES = {"unknown", "invited", "connected", "declined", "withdrawn"}


def validate(condition: Any, path: str = "$") -> list[str]:
    """Return a list of human-readable error strings ([] when valid).
    Used at publish time so users see structural problems before runtime.
    """
    errors: list[str] = []
    if not isinstance(condition, dict):
        errors.append(f"{path}: condition must be an object")
        return errors
    op = condition.get("op")
    if op not in VALID_OPS:
        errors.append(f"{path}.op: unknown op '{op}'")
        return errors

    if op == "always":
        return errors
    if op == "not":
        child = condition.get("child")
        if child is None:
            errors.append(f"{path}: 'not' requires 'child'")
        else:
            errors.extend(validate(child, f"{path}.child"))
        return errors
    if op in {"and", "or"}:
        children = condition.get("children")
        if not isinstance(children, list) or not children:
            errors.append(f"{path}: '{op}' requires non-empty 'children'")
            return errors
        for i, c in enumerate(children):
            errors.extend(validate(c, f"{path}.children[{i}]"))
        return errors
    if op == "replied":
        chans = condition.get("channels", ["email", "linkedin"])
        if not isinstance(chans, list) or not chans:
            errors.append(f"{path}.channels: must be a non-empty list")
        else:
            bad = [c for c in chans if c not in VALID_CHANNELS]
            if bad:
                errors.append(f"{path}.channels: unknown channels {bad}")
        if "within_days" in condition and not isinstance(condition["within_days"], (int, float)):
            errors.append(f"{path}.within_days: must be a number")
        return errors
    if op in {"opened", "clicked"}:
        if "within_days" in condition:
            wd = condition["within_days"]
            if not isinstance(wd, (int, float)):
                errors.append(f"{path}.within_days: must be a number")
            elif wd < 0:
                errors.append(f"{path}.within_days: must be a non-negative number")
        return errors
    if op == "bounced":
        return errors
    if op == "linkedin_connection":
        value = condition.get("value")
        if value not in VALID_LINKEDIN_STATUSES:
            errors.append(f"{path}.value: must be one of {sorted(VALID_LINKEDIN_STATUSES)}")
        return errors
    if op == "days_since_entered_node":
        gte = condition.get("gte")
        if not isinstance(gte, (int, float)) or gte < 0:
            errors.append(f"{path}.gte: must be a non-negative number")
        return errors
    return errors  # unreachable


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------


def evaluate(condition: Any, ctx: ConditionContext) -> bool:
    """Evaluate `condition` against `ctx`. Returns False on any error."""
    try:
        return _eval(condition, ctx)
    except Exception:
        logger.exception("Condition evaluation error: %r", condition)
        return False


def _eval(condition: Any, ctx: ConditionContext) -> bool:
    if not isinstance(condition, dict):
        return False
    op = condition.get("op")

    if op == "always":
        return True
    if op == "not":
        return not _eval(condition.get("child"), ctx)
    if op == "and":
        children = condition.get("children") or []
        return all(_eval(c, ctx) for c in children)
    if op == "or":
        children = condition.get("children") or []
        return any(_eval(c, ctx) for c in children)

    if op == "replied":
        raw_channels = condition.get("channels")
        channels = set(raw_channels) if raw_channels is not None else {"email", "linkedin"}
        within_days = condition.get("within_days")
        for ch in channels:
            if ch == "email" and ctx.has_replied_email and _within_days(ctx.last_reply_email_at, within_days, ctx.now):
                return True
            if ch == "linkedin" and ctx.has_replied_linkedin and _within_days(ctx.last_reply_linkedin_at, within_days, ctx.now):
                return True
        return False

    if op == "opened":
        return ctx.has_opened and _within_days(ctx.last_open_at, condition.get("within_days"), ctx.now)
    if op == "clicked":
        return ctx.has_clicked and _within_days(ctx.last_click_at, condition.get("within_days"), ctx.now)
    if op == "bounced":
        return ctx.has_bounced

    if op == "linkedin_connection":
        return ctx.linkedin_connection_status == condition.get("value")
    if op == "days_since_entered_node":
        if ctx.entered_current_at is None:
            return False
        elapsed = ctx.now - _aware(ctx.entered_current_at, ctx.now.tzinfo)
        return elapsed >= timedelta(days=float(condition.get("gte", 0)))

    return False


# --------------------------------------------------------------------------
# Graph validation (used by router publish + sequence service)
# --------------------------------------------------------------------------


def detect_cycle(node_ids: list[str], edges: list[tuple[str, str | None]]) -> list[str] | None:
    """Return a list of node_ids forming a cycle, or None if the graph is a DAG.

    Edges with to_node_id=None (terminate-on-this-branch) are ignored.
    """
    adj: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for src, dst in edges:
        if dst is None:
            continue
        if src in adj and dst in adj:
            adj[src].append(dst)

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in node_ids}
    parent: dict[str, str | None] = {nid: None for nid in node_ids}

    def dfs(start: str) -> list[str] | None:
        stack: list[tuple[str, int]] = [(start, 0)]
        color[start] = GRAY
        while stack:
            node, idx = stack[-1]
            children = adj[node]
            if idx >= len(children):
                color[node] = BLACK
                stack.pop()
                continue
            child = children[idx]
            stack[-1] = (node, idx + 1)
            if color[child] == WHITE:
                color[child] = GRAY
                parent[child] = node
                stack.append((child, 0))
            elif color[child] == GRAY:
                # Cycle: walk parents from `node` back to `child`.
                cycle = [child, node]
                cursor = parent[node]
                while cursor is not None and cursor != child:
                    cycle.append(cursor)
                    cursor = parent[cursor]
                cycle.reverse()
                return cycle
        return None

    for nid in node_ids:
        if color[nid] == WHITE:
            cyc = dfs(nid)
            if cyc:
                return cyc
    return None
