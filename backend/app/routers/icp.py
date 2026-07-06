"""ICP / lookalike endpoints: profile read+regenerate, ranked candidate
queue, accept (→ campaign-less CRM lead) / reject.

Accepting NEVER touches a sending campaign — bulk add-to-campaign is
the existing CSV/manual flow, one human step later.
"""
from __future__ import annotations

import math
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import (
    IcpProfile,
    IcpProfileSource,
    Lead,
    LookalikeCandidate,
    LookalikeCandidateStatus,
)
from app.services import icp_builder

router = APIRouter(prefix="/icp", tags=["icp"])


def _profile_dict(p: IcpProfile) -> dict[str, Any]:
    criteria = {k: v for k, v in (p.criteria or {}).items() if k != "_meta"}
    return {
        "id": p.id,
        "name": p.name,
        "source": p.source,
        "status": p.status,
        "criteria": criteria,
        "won_deal_count": p.won_deal_count,
        "refreshed_at": p.refreshed_at,
        "min_won_deals": icp_builder.MIN_WON_DEALS,
    }


@router.get("/profile")
async def get_profile(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    p = await db.scalar(
        select(IcpProfile).where(
            IcpProfile.source == IcpProfileSource.AUTO_CLOSED_WON,
        )
    )
    if p is None:
        return {
            "id": None, "status": "missing",
            "min_won_deals": icp_builder.MIN_WON_DEALS,
        }
    return _profile_dict(p)


@router.post("/profile/regenerate")
async def regenerate_profile(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    p = await icp_builder.build_icp_from_won(db)
    await db.commit()
    await db.refresh(p)
    return _profile_dict(p)


class CriteriaUpdate(BaseModel):
    criteria: dict[str, Any]


@router.patch("/profile")
async def update_profile_criteria(
    payload: CriteriaUpdate, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Hand-tune the auto profile's criteria (kept until the next
    closed-won regenerate)."""
    p = await db.scalar(
        select(IcpProfile).where(
            IcpProfile.source == IcpProfileSource.AUTO_CLOSED_WON,
        )
    )
    if p is None:
        raise HTTPException(status_code=404, detail="no ICP profile yet")
    meta = (p.criteria or {}).get("_meta")
    criteria = dict(payload.criteria)
    if meta:
        criteria["_meta"] = meta
    p.criteria = criteria
    await db.commit()
    await db.refresh(p)
    return _profile_dict(p)


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


def _candidate_dict(c: LookalikeCandidate) -> dict[str, Any]:
    return {
        "id": c.id,
        "company": c.company,
        "company_website": c.company_website,
        "contact_name": c.contact_name,
        "job_title": c.job_title,
        "linkedin_url": c.linkedin_url,
        "email": c.email,
        "fit_score": c.fit_score,
        "fit_reason": c.fit_reason,
        "source": c.source,
        "status": c.status,
        "created_lead_id": c.created_lead_id,
        "created_at": c.created_at,
    }


@router.get("/candidates")
async def list_candidates(
    status: LookalikeCandidateStatus | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    filters = []
    if status is not None:
        filters.append(LookalikeCandidate.status == status)
    total = (await db.execute(
        select(func.count()).select_from(LookalikeCandidate).where(*filters)
    )).scalar_one()
    rows = (await db.execute(
        select(LookalikeCandidate)
        .where(*filters)
        .order_by(LookalikeCandidate.fit_score.desc(), LookalikeCandidate.created_at.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )).scalars().all()
    return {
        "items": [_candidate_dict(c) for c in rows],
        "total": total, "page": page, "page_size": page_size,
        "total_pages": math.ceil(total / page_size) if total else 0,
    }


@router.post("/candidates/{candidate_id}/accept")
async def accept_candidate(
    candidate_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Accept → ONE campaign-less CRM lead (idempotent: 409 on
    re-accept).  Adding to a campaign stays a separate human action."""
    c = await db.get(LookalikeCandidate, candidate_id)
    if c is None:
        raise HTTPException(status_code=404, detail="candidate not found")
    if c.status == LookalikeCandidateStatus.ACCEPTED:
        raise HTTPException(status_code=409, detail="already accepted")

    parts = (c.contact_name or "").split(" ", 1)
    email = c.email or f"unknown@{(c.company_website or c.company).strip().lower().replace(' ', '-')}"
    # Reuse an existing lead with this email rather than duplicating.
    lead = await db.scalar(
        select(Lead).where(Lead.email == email.lower()).limit(1)
    )
    if lead is None:
        lead = Lead(
            campaign_id=None,
            email=email.lower(),
            first_name=parts[0] or None,
            last_name=parts[1] if len(parts) > 1 else None,
            company=c.company,
            job_title=c.job_title,
            linkedin_url=c.linkedin_url,
            company_website=c.company_website,
        )
        db.add(lead)
        await db.flush()

    c.status = LookalikeCandidateStatus.ACCEPTED
    c.created_lead_id = lead.id
    await db.commit()
    return {**_candidate_dict(c), "lead_id": str(lead.id)}


@router.post("/candidates/{candidate_id}/reject")
async def reject_candidate(
    candidate_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    c = await db.get(LookalikeCandidate, candidate_id)
    if c is None:
        raise HTTPException(status_code=404, detail="candidate not found")
    c.status = LookalikeCandidateStatus.REJECTED
    await db.commit()
    return _candidate_dict(c)
