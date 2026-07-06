"""Approximate Anthropic per-message cost estimation.

Reads ``message.usage`` (which contains ``input_tokens``,
``output_tokens``, and — when web_search is used — server-side tool
counts) and multiplies by the published per-million-token prices for
each model.  Used by Social Listening Radar to:
  - Surface a "Run now (~$X.XX)" estimate before a run.
  - Track real spend during a run and abort before exceeding the
    per-search ``max_run_cost_usd`` cap.

Pricing is approximate — exact numbers depend on Anthropic's current
schedule and we only need accuracy within ~20% to make a useful cap.
"""
from __future__ import annotations

from typing import Any

# USD per million tokens.  Sourced from Anthropic's pricing page.  Update
# when the schedule shifts — wrong values just mean the cost estimate
# drifts by ~ a model-line.
_MODEL_PRICING_PER_M: dict[str, dict[str, float]] = {
    # Sonnet 4.6 / 4.7 — same family pricing.
    "claude-sonnet-4-6":           {"input": 3.0,  "output": 15.0},
    "claude-sonnet-4-7":           {"input": 3.0,  "output": 15.0},
    "claude-opus-4-7":             {"input": 15.0, "output": 75.0},
    # Haiku 4.5 — cheap; primary model for non-LinkedIn discovery + qualify.
    "claude-haiku-4-5-20251001":   {"input": 0.80, "output": 4.0},
    "claude-haiku-4-5":            {"input": 0.80, "output": 4.0},
}

# Anthropic's web_search tool charges per call.  $0.01 per search is
# approximate (their published $10 / 1k).
WEB_SEARCH_USD_PER_CALL = 0.010


def _model_prices(model: str) -> dict[str, float]:
    """Return per-M-token pricing for ``model``.  Falls back to Sonnet
    pricing for unknowns so we don't undercount."""
    if model in _MODEL_PRICING_PER_M:
        return _MODEL_PRICING_PER_M[model]
    # Heuristic by family name.
    lower = (model or "").lower()
    if "haiku" in lower:
        return _MODEL_PRICING_PER_M["claude-haiku-4-5"]
    if "opus" in lower:
        return _MODEL_PRICING_PER_M["claude-opus-4-7"]
    return _MODEL_PRICING_PER_M["claude-sonnet-4-6"]


def message_cost_usd(message: Any, model: str) -> float:
    """Approximate USD cost of one Anthropic message based on its
    ``usage`` block.  Returns 0.0 when usage isn't available."""
    usage = getattr(message, "usage", None)
    if usage is None:
        return 0.0
    prices = _model_prices(model)
    inp = int(getattr(usage, "input_tokens", 0) or 0)
    out = int(getattr(usage, "output_tokens", 0) or 0)
    cost = (inp / 1_000_000) * prices["input"] + (out / 1_000_000) * prices["output"]
    # Web-search tool usage shows up as ``server_tool_use.web_search_requests``.
    tool_use = getattr(usage, "server_tool_use", None)
    if tool_use is not None:
        searches = int(getattr(tool_use, "web_search_requests", 0) or 0)
        cost += searches * WEB_SEARCH_USD_PER_CALL
    return cost


def estimate_discovery_cost_usd(
    *,
    num_queries: int,
    sources: list[str],
    linkedin_web_search_enabled: bool,
    max_uses_linkedin: int,
    max_uses_other: int,
    model_linkedin: str = "claude-sonnet-4-6",
    model_other: str = "claude-haiku-4-5-20251001",
) -> dict[str, Any]:
    """Forecast (not measure) the cost of an upcoming run.  Used by the
    Run-now button label and the editor's cost preview.

    The numbers below are calibrated against live runs: a single Haiku
    discovery call with max_uses=2 averages ~$0.01 input + ~$0.01 output
    + 2 × $0.01 web-search = ~$0.04.  A Sonnet LinkedIn call with
    max_uses=5 averages ~$0.10 + $0.05 + 5 × $0.01 = ~$0.20.  Both are
    rough; treat them as ballpark."""
    cost = 0.0
    pair_count = 0
    for src in sources:
        if src == "linkedin":
            if not linkedin_web_search_enabled:
                continue
            cost += num_queries * (
                0.10 + 0.05 + max_uses_linkedin * WEB_SEARCH_USD_PER_CALL
            )
        else:
            cost += num_queries * (
                0.01 + 0.01 + max_uses_other * WEB_SEARCH_USD_PER_CALL
            )
        pair_count += num_queries
    return {
        "discovery_pairs": pair_count,
        "discovery_cost_usd": round(cost, 3),
    }


def estimate_qualify_cost_usd(num_posts: int) -> float:
    """Each qualify call is Haiku, ~2000 input + ~600 output tokens =
    roughly $0.005.  Batched qualification (10 posts/call) is ~$0.012
    per batch — caller picks one or the other."""
    return round(num_posts * 0.005, 3)
