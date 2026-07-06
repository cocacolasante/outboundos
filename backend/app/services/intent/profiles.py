"""ICP intent profile management — the GrantMind switch (Phase 5).

A profile drives WHICH events count and HOW they score:
  - ``cause_codes``        NTEE prefixes the collectors/matcher target,
  - ``geographies``        states to monitor,
  - ``rfp_keywords``       Grants.gov search terms,
  - ``size_band_weights``  the org-fit multiplier (sweet spot small/mid),
  - ``signal_weights``     per-type emphasis (push dev-role / lapsed-funder up),
  - ``half_life_overrides`` per-type decay overrides,
  - ``promotion_threshold`` the outreach-bridge gate.

Collection (Grants.gov / USASpending / ProPublica) and scoring already READ the
active profile; this module makes profiles first-class: create / list / update /
activate / delete, two presets (GrantMind vs a neutral generic baseline), and a
read-only ranking + cross-profile comparison so the same orgs can be ranked
under different profiles without persisting.

Workspace awareness: profiles carry the repo-wide nullable ``tenant_id``.  No
RLS (deferred) — selection is profile-scoped + forward-compatible.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import IcpIntentProfile, Org, Signal
from app.services.intent import scoring

logger = logging.getLogger(__name__)

# GrantMind Pro: nonprofits actively investing in grant-seeking.  Favors
# small/mid orgs (the sweet spot — active but without a big in-house dev shop),
# and pushes the "act now" Tier-1 signals (dev-role posted, lapsed funder, new
# RFP) to the top.
GRANTMIND_PRESET: dict[str, Any] = {
    "name": "GrantMind Pro",
    "cause_codes": ["A", "B", "E", "P", "S", "T"],  # arts/ed/health/human-services/community/philanthropy
    "geographies": [],
    "size_band_weights": {
        "micro": 0.6, "small": 1.4, "mid": 1.4, "large": 0.5, "major": 0.2,
    },
    "signal_weights": {
        "dev_role_posted": 1.6, "lapsed_funder": 1.6, "new_rfp": 1.3,
        "rev_drop": 1.1, "new_program": 1.0, "peer_funded": 0.8,
        "new_501c3": 0.4, "cause_match": 0.3,
    },
    "rfp_keywords": [
        "nonprofit capacity building", "grant writing", "program expansion",
        "community development", "human services",
    ],
    "half_life_overrides": {},
    "promotion_threshold": 80,
}

# A neutral baseline: flat size weights + flat signal weights — ranks purely by
# raw signal strength × decay, with no ICP tilt.  The comparison foil.
GENERIC_PRESET: dict[str, Any] = {
    "name": "Generic",
    "cause_codes": [],
    "geographies": [],
    "size_band_weights": {
        "micro": 1.0, "small": 1.0, "mid": 1.0, "large": 1.0, "major": 1.0,
    },
    "signal_weights": {},
    "rfp_keywords": ["nonprofit", "grant"],
    "half_life_overrides": {},
    "promotion_threshold": 100,
}

PRESETS = {"grantmind": GRANTMIND_PRESET, "generic": GENERIC_PRESET}

_EDITABLE = (
    "name", "cause_codes", "geographies", "size_band_weights", "signal_weights",
    "rfp_keywords", "half_life_overrides", "half_life_days", "max_signal_age_days",
    "promotion_threshold", "is_active",
)


async def list_profiles(session: AsyncSession, tenant_id=None) -> list[IcpIntentProfile]:
    q = select(IcpIntentProfile)
    q = (q.where(IcpIntentProfile.tenant_id.is_(None)) if tenant_id is None
         else q.where(IcpIntentProfile.tenant_id == tenant_id))
    return list((await session.execute(q.order_by(IcpIntentProfile.created_at))).scalars().all())


async def get_profile(session: AsyncSession, profile_id) -> IcpIntentProfile | None:
    return await session.get(IcpIntentProfile, profile_id)


async def create_profile(
    session: AsyncSession, data: dict[str, Any], *, tenant_id=None,
) -> IcpIntentProfile:
    fields = {k: v for k, v in data.items() if k in _EDITABLE}
    activate = fields.pop("is_active", True)
    profile = IcpIntentProfile(tenant_id=tenant_id, **fields)
    session.add(profile)
    await session.flush()
    if activate:
        # Activate against the row's ACTUAL tenant: the TenantMixin column
        # default replaces an explicit None at flush (ambient request
        # context), so the tenant_id argument can be stale by now.
        await activate_profile(session, profile, tenant_id=profile.tenant_id)
    return profile


async def update_profile(
    session: AsyncSession, profile: IcpIntentProfile, data: dict[str, Any],
) -> IcpIntentProfile:
    for k, v in data.items():
        if k in _EDITABLE and k != "is_active":
            setattr(profile, k, v)
    if data.get("is_active") is True:
        await activate_profile(session, profile, tenant_id=profile.tenant_id)
    elif data.get("is_active") is False:
        profile.is_active = False
    await session.flush()
    return profile


async def activate_profile(
    session: AsyncSession, profile: IcpIntentProfile, *, tenant_id=None,
) -> None:
    """Make this the single active profile for its tenant (others deactivated)."""
    stmt = update(IcpIntentProfile).values(is_active=False)
    stmt = (stmt.where(IcpIntentProfile.tenant_id.is_(None)) if tenant_id is None
            else stmt.where(IcpIntentProfile.tenant_id == tenant_id))
    await session.execute(stmt.where(IcpIntentProfile.id != profile.id))
    profile.is_active = True
    await session.flush()


async def delete_profile(session: AsyncSession, profile: IcpIntentProfile) -> None:
    await session.delete(profile)
    await session.flush()


async def create_from_preset(
    session: AsyncSession, kind: str, *, tenant_id=None, name: str | None = None,
) -> IcpIntentProfile:
    preset = dict(PRESETS[kind])
    if name:
        preset["name"] = name
    return await create_profile(session, preset, tenant_id=tenant_id)


async def rank_orgs(
    session: AsyncSession, profile: IcpIntentProfile | None, *,
    now=None, limit: int = 50, tenant_id=None,
) -> list[dict]:
    """Read-only ranked intent list for every org that has a signal, scored
    under ``profile`` (no persistence).  Newest-strongest first."""
    org_ids = (await session.execute(
        select(Org.id).where(Org.tenant_id == tenant_id) if tenant_id is not None
        else select(Org.id)
    )).scalars().all()
    ranked: list[dict] = []
    for oid in org_ids:
        org = await session.get(Org, oid)
        if org is None:
            continue
        r = await scoring.preview_org_intent(session, org, profile=profile, now=now)
        if r["intent_score"] <= 0:
            continue
        ranked.append({
            "org_id": str(org.id), "name": org.name, "state": org.state,
            "ntee_code": org.ntee_code,
            "size_band": org.size_band.value if org.size_band else None,
            "intent_score": round(r["intent_score"], 2), "tier": r["tier"],
            "fit_multiplier": round(r["fit_multiplier"], 3),
            "top_signal_id": str(r["top_signal_id"]) if r["top_signal_id"] else None,
            "why_now": None, "evidence_url": None, "top_signal_type": None,
        })
    ranked.sort(key=lambda x: (x["tier"], -x["intent_score"]))
    ranked = ranked[:limit]

    # Attach the top signal's "why now" + evidence for the visible rows only.
    sig_ids = [r["top_signal_id"] for r in ranked if r["top_signal_id"]]
    if sig_ids:
        sigs = {
            str(s.id): s for s in (await session.execute(
                select(Signal).where(Signal.id.in_(sig_ids))
            )).scalars().all()
        }
        for r in ranked:
            s = sigs.get(r["top_signal_id"])
            if s is not None:
                r["why_now"] = s.summary
                r["evidence_url"] = s.evidence_url
                r["top_signal_type"] = s.signal_type.value
    return ranked


async def compare_profiles(
    session: AsyncSession, profile_a: IcpIntentProfile | None,
    profile_b: IcpIntentProfile | None, *, now=None, limit: int = 50,
) -> dict[str, list[dict]]:
    """Rank the same orgs under two profiles — the distinct-ranking demo."""
    return {
        "a": await rank_orgs(session, profile_a, now=now, limit=limit),
        "b": await rank_orgs(session, profile_b, now=now, limit=limit),
    }
