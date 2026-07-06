"""Canonical metric helpers — the single source of truth for ratio/rate math
across every reporting surface (campaign analytics, CRM reports, campaign
roll-ups).  Previously `_rate` was copy-pasted into three routers; they all
import this now so a definition can't drift.

See ``docs/crm-metrics.md`` for the standardized metric definitions.
"""
from __future__ import annotations


def rate(numer: float | int, denom: float | int, *, ndigits: int = 4) -> float | None:
    """A ratio metric (open rate, win rate, …) as a 0–1 fraction rounded to
    ``ndigits``.  Returns ``None`` when ``denom`` is missing or ``<= 0`` — that
    signals "no basis to compute" (e.g. 0 sent → no open rate), which the UI
    renders as an em-dash rather than a misleading 0%.
    """
    if denom is None or denom <= 0:
        return None
    return round(numer / denom, ndigits)
