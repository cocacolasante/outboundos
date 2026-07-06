"""Match external funding events (RFPs, peer awards) to monitored orgs.

The Grants.gov + USASpending collectors are *fan-out* collectors: one external
event (a new RFP, a peer's federal award) becomes one signal per matching
monitored org.  Matching is by:

- **cause** — an ICP cause code is a PREFIX of the org's NTEE code (cause "T"
  matches "T31"); empty cause list matches any.
- **geo** — the org's state is in the ICP geographies; empty matches any.

Selecting candidate monitored orgs ONCE per run (not per event) keeps the
fan-out O(events · matched_orgs) without a DB round-trip per event.
"""
from __future__ import annotations

import re
from collections import defaultdict

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Org

_NORM_RE = re.compile(r"[^a-z0-9]+")


def normalize_name(name: str | None) -> str:
    """Lowercased, punctuation-stripped org name for self-match detection."""
    return _NORM_RE.sub(" ", (name or "").lower()).strip()


def org_matches_cause(ntee_code: str | None, cause_prefixes: list[str]) -> bool:
    if not cause_prefixes:
        return True
    code = (ntee_code or "").upper()
    return any(code.startswith(p.upper()) for p in cause_prefixes if p)


async def candidate_orgs(
    session: AsyncSession, *,
    cause_prefixes: list[str] | None = None,
    geographies: list[str] | None = None,
    limit: int = 5000,
) -> list[Org]:
    """Monitored orgs matching the ICP cause (NTEE prefix) + geo (state)."""
    q = select(Org)
    if geographies:
        q = q.where(Org.state.in_([s.upper() for s in geographies if s]))
    if cause_prefixes:
        q = q.where(or_(*[Org.ntee_code.ilike(f"{p}%") for p in cause_prefixes if p]))
    q = q.limit(limit)
    return list((await session.execute(q)).scalars().all())


def group_by_state(orgs: list[Org]) -> dict[str, list[Org]]:
    by_state: dict[str, list[Org]] = defaultdict(list)
    for o in orgs:
        if o.state:
            by_state[o.state.upper()].append(o)
    return dict(by_state)


# Trailing legal/boilerplate tokens to ignore when matching employer names.
_NAME_NOISE = {"inc", "incorporated", "llc", "co", "corp", "corporation", "ltd"}


def name_keys(name: str | None) -> set[str]:
    """Normalized lookup keys for an org/employer name, for HIGH-PRECISION
    matching: the full normalized name, plus variants with a leading "the" or a
    trailing legal suffix dropped (so "The X Foundation, Inc." matches "X
    Foundation").  Conservative on purpose — a false dev-role attribution to the
    wrong org is worse than a missed one (Track 1 accepts partial recall)."""
    norm = normalize_name(name)
    tokens = [t for t in norm.split() if t]
    if not tokens:
        return set()
    keys = {" ".join(tokens)}
    if tokens[0] == "the":
        tokens = tokens[1:]
        keys.add(" ".join(tokens))
    while tokens and tokens[-1] in _NAME_NOISE:
        tokens = tokens[:-1]
        keys.add(" ".join(tokens))
    return {k for k in keys if k}


def index_orgs_by_name(orgs: list[Org]) -> dict[str, Org]:
    """Map every name key → org (first writer wins) for employer-name lookup."""
    index: dict[str, Org] = {}
    for o in orgs:
        for key in name_keys(o.name):
            index.setdefault(key, o)
    return index


def match_employer(employer: str | None, index: dict[str, Org]) -> Org | None:
    """Find the monitored org an employer name refers to, or None."""
    for key in name_keys(employer):
        if key in index:
            return index[key]
    return None
