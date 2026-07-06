"""Intent-engine ICP profile endpoints (Phase 5).

CRUD + activate + presets for ``icp_intent_profiles`` (the GrantMind switch),
plus a read-only ranked intent list under a profile and a two-profile
comparison.  Nothing here sends or promotes — promotion is the Phase-4 bridge,
gated by a human.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import (
    Campaign, CampaignStatus, IcpIntentProfile, Lead, Org, OrgIntentScore,
    SendStatus, Signal,
)
from app.services.intent import orgs as orgs_svc
from app.services.intent import profiles as profiles_svc
from app.services.intent import promote as promote_svc
from app.services.intent import scoring
from app.workers.celery_app import celery_app

router = APIRouter(prefix="/intent", tags=["intent"])

# The collector Celery tasks the "Run collectors" button fires (by name, so the
# router doesn't import the worker module).
_COLLECTOR_TASKS = [
    "intent.collect_grants_gov",
    "intent.collect_usaspending_peer",
    "intent.collect_dev_roles",
    "intent.collect_ats_dev_roles",
    "intent.collect_careers_dev_roles",
    "intent.collect_propublica_rev_delta",
]


class ProfileIn(BaseModel):
    name: str
    cause_codes: list[str] = Field(default_factory=list)
    geographies: list[str] = Field(default_factory=list)
    size_band_weights: dict[str, float] = Field(default_factory=dict)
    signal_weights: dict[str, float] = Field(default_factory=dict)
    rfp_keywords: list[str] = Field(default_factory=list)
    half_life_overrides: dict[str, float] = Field(default_factory=dict)
    half_life_days: float | None = None
    max_signal_age_days: int | None = None
    promotion_threshold: float | None = None
    is_active: bool = True


class ProfilePatch(BaseModel):
    name: str | None = None
    cause_codes: list[str] | None = None
    geographies: list[str] | None = None
    size_band_weights: dict[str, float] | None = None
    signal_weights: dict[str, float] | None = None
    rfp_keywords: list[str] | None = None
    half_life_overrides: dict[str, float] | None = None
    half_life_days: float | None = None
    max_signal_age_days: int | None = None
    promotion_threshold: float | None = None
    is_active: bool | None = None


def _to_dict(p: IcpIntentProfile) -> dict[str, Any]:
    return {
        "id": p.id, "name": p.name, "is_active": p.is_active,
        "cause_codes": p.cause_codes, "geographies": p.geographies,
        "size_band_weights": p.size_band_weights, "signal_weights": p.signal_weights,
        "rfp_keywords": p.rfp_keywords, "half_life_overrides": p.half_life_overrides,
        "half_life_days": float(p.half_life_days),
        "max_signal_age_days": p.max_signal_age_days,
        "promotion_threshold": float(p.promotion_threshold),
        "created_at": p.created_at, "updated_at": p.updated_at,
    }


@router.get("/profiles")
async def list_profiles(db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    # Phase 2: creation stamps the ambient tenant (TenantMixin default), so
    # the list must filter on the same tenant — the profiles service was
    # already tenant-parameterized, only the router hardcoded None.
    from app.tenancy.context import current_tenant_id

    return [_to_dict(p) for p in
            await profiles_svc.list_profiles(db, tenant_id=current_tenant_id.get())]


@router.post("/profiles", status_code=201)
async def create_profile(body: ProfileIn, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    from app.tenancy.context import current_tenant_id

    p = await profiles_svc.create_profile(db, body.model_dump(),
                                          tenant_id=current_tenant_id.get())
    await db.commit()
    await db.refresh(p)   # load server-default timestamps
    return _to_dict(p)


@router.post("/profiles/preset", status_code=201)
async def create_preset(
    kind: str = Query(..., pattern="^(grantmind|generic)$"),
    name: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    p = await profiles_svc.create_from_preset(db, kind, name=name)
    await db.commit()
    await db.refresh(p)   # load server-default timestamps
    return _to_dict(p)


async def _require(db: AsyncSession, profile_id: uuid.UUID) -> IcpIntentProfile:
    p = await profiles_svc.get_profile(db, profile_id)
    if p is None:
        raise HTTPException(404, "profile not found")
    return p


@router.get("/profiles/{profile_id}")
async def get_profile(profile_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return _to_dict(await _require(db, profile_id))


@router.patch("/profiles/{profile_id}")
async def patch_profile(
    profile_id: uuid.UUID, body: ProfilePatch, db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    p = await _require(db, profile_id)
    p = await profiles_svc.update_profile(db, p, body.model_dump(exclude_unset=True))
    await db.commit()
    await db.refresh(p)
    return _to_dict(p)


@router.post("/profiles/{profile_id}/activate")
async def activate_profile(profile_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    p = await _require(db, profile_id)
    await profiles_svc.activate_profile(db, p, tenant_id=p.tenant_id)
    await db.commit()
    await db.refresh(p)
    return _to_dict(p)


@router.delete("/profiles/{profile_id}", status_code=204, response_model=None)
async def delete_profile(profile_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> None:
    p = await _require(db, profile_id)
    await profiles_svc.delete_profile(db, p)
    await db.commit()


@router.get("/profiles/{profile_id}/intent")
async def ranked_intent(
    profile_id: uuid.UUID, limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """Ranked intent list of monitored orgs under this profile (read-only)."""
    p = await _require(db, profile_id)
    return await profiles_svc.rank_orgs(db, p, limit=limit)


@router.get("/compare")
async def compare(
    profile_a: uuid.UUID, profile_b: uuid.UUID,
    limit: int = Query(50, ge=1, le=500), db: AsyncSession = Depends(get_db),
) -> dict[str, list[dict[str, Any]]]:
    a = await _require(db, profile_a)
    b = await _require(db, profile_b)
    return await profiles_svc.compare_profiles(db, a, b, limit=limit)


# --------------------------------------------------------------------------
# Engine dashboard + run controls (drive the Settings → Intent tab)
# --------------------------------------------------------------------------

_DRAFT_NAME_PREFIX = "Intent drafts — "


async def _draft_campaign(db: AsyncSession) -> Campaign | None:
    # Newest intent-draft campaign regardless of status, so the UI always links
    # to it (PREVIEWING awaiting approval, or RUNNING after launch).
    return await db.scalar(
        select(Campaign)
        .where(Campaign.name.like(f"{_DRAFT_NAME_PREFIX}%"))
        .order_by(Campaign.created_at.desc())
    )


@router.get("/status")
async def status(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Overview for the dashboard: counts, active profile, draft campaign,
    and whether the Adzuna key is configured."""
    orgs_total = await db.scalar(select(func.count()).select_from(Org)) or 0
    by_type = (await db.execute(
        select(Signal.signal_type, func.count()).group_by(Signal.signal_type)
    )).all()
    by_tier = (await db.execute(
        select(OrgIntentScore.tier, func.count()).group_by(OrgIntentScore.tier)
    )).all()
    active = await scoring.get_active_profile(db)
    draft = await _draft_campaign(db)
    draft_leads = 0
    if draft is not None:
        draft_leads = await db.scalar(
            select(func.count()).select_from(Lead).where(
                Lead.campaign_id == draft.id, Lead.send_status == SendStatus.PENDING)
        ) or 0
    return {
        "adzuna_configured": bool(settings.ADZUNA_APP_ID and settings.ADZUNA_APP_KEY),
        "active_profile_id": active.id if active else None,
        "orgs_total": orgs_total,
        "signals_by_type": {t.value: c for t, c in by_type},
        "tiers": {str(t): c for t, c in by_tier},
        "draft_campaign_id": draft.id if draft else None,
        "draft_pending_leads": draft_leads,
    }


class SeedIn(BaseModel):
    query: str
    limit: int = Field(50, ge=1, le=100)


@router.post("/orgs/seed")
async def seed_orgs(body: SeedIn, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Seed monitored orgs from ProPublica search by cause keyword (real EINs)."""
    result = await orgs_svc.seed_orgs_from_propublica_search(db, body.query, limit=body.limit)
    result["orgs_total"] = await db.scalar(select(func.count()).select_from(Org)) or 0
    return result


@router.post("/collectors/run")
async def run_collectors(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Enqueue all intent collectors (they hit external APIs — async)."""
    for name in _COLLECTOR_TASKS:
        celery_app.send_task(name)
    return {"queued": _COLLECTOR_TASKS}


@router.post("/recompute")
async def recompute(db: AsyncSession = Depends(get_db)) -> dict[str, int]:
    """Recompute every org's intent now (decay + ICP weight + fit)."""
    return await scoring.recompute_all_intent(db)


@router.post("/promote")
async def promote(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Stage approval-pending DRAFTS for promotable orgs now (never sends)."""
    counts = await promote_svc.promote_eligible(db)
    draft = await _draft_campaign(db)
    counts["draft_campaign_id"] = str(draft.id) if draft else None
    return counts
