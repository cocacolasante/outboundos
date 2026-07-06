"""Shared shape for funding-feed discoveries."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DiscoveredOrg:
    """One discovered nonprofit event from a funding feed.

    ``dedup_key`` is globally unique per event (e.g.
    ``grant_awarded:<award_id>`` / ``new_501c3:<ein>``) and is enforced
    UNIQUE on ``prospect_signals`` so a re-poll never re-emits.
    """
    signal_type: str            # 'grant_awarded' | 'new_501c3'
    summary: str
    dedup_key: str
    org_name: str
    state: str | None = None
    ein: str | None = None
    ntee_code: str | None = None
    website: str | None = None
    # Postal address (IRS BMF carries STREET/CITY/STATE/ZIP).  Powers the
    # direct-mail fallback for orgs we can't reach by email.
    mailing_address: dict[str, Any] | None = None
    detail: dict[str, Any] = field(default_factory=dict)
