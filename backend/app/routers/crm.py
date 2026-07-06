"""CRM endpoints: manual leads, lead conversion, opportunities, activities.

Salesforce-style layer.  The CRM lead IS the existing ``Lead`` model
(nullable campaign_id since migration 0025), so manually-created leads
show up in the same global Leads list the user already works in.
"""
from __future__ import annotations

import math
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from sqlalchemy.orm import selectinload

from app.models import (
    CLOSED_STAGES,
    STAGE_DEFAULT_PROBABILITY,
    CrmActivity,
    CrmActivityType,
    CrmDocument,
    CrmLeadStatus,
    Lead,
    Opportunity,
    OpportunityProduct,
    OpportunityStage,
    OpportunityStageChange,
    Pipeline,
    PipelineStage,
)
from app.services import agent_core
from app.schemas.crm import (
    ActivityCreate,
    ActivityResponse,
    ActivityUpdate,
    ConvertLeadRequest,
    ConvertLeadResponse,
    DocumentResponse,
    LeadCreate,
    LeadCrmUpdate,
    OpportunityCreate,
    OpportunityResponse,
    OpportunityUpdate,
    PaginatedActivities,
    PaginatedOpportunities,
    PipelineResponse,
    PipelineSummary,
    ProductCreate,
    ProductListResponse,
    ProductResponse,
    ProductUpdate,
    StageChangeResponse,
)

router = APIRouter(prefix="/crm", tags=["crm"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Configurable-pipeline helpers (migration 0038).  The legacy ``stage`` enum
# is dual-written with ``stage_id``; seeded stage keys == enum values.
# ---------------------------------------------------------------------------


async def _default_pipeline_id(db: AsyncSession) -> uuid.UUID | None:
    return (await db.execute(
        select(Pipeline.id)
        .where(Pipeline.is_default.is_(True))
        .order_by(Pipeline.created_at)
        .limit(1)
    )).scalars().first()


async def _resolve_stage(
    db: AsyncSession, pipeline_id: uuid.UUID | None, stage_key: str,
) -> PipelineStage | None:
    """The PipelineStage matching ``stage_key`` within ``pipeline_id`` (or the
    default pipeline when None).  Returns None if no pipeline is seeded yet —
    callers leave ``stage_id`` NULL and the legacy enum still drives behavior."""
    pid = pipeline_id or await _default_pipeline_id(db)
    if pid is None:
        return None
    return (await db.execute(
        select(PipelineStage).where(
            PipelineStage.pipeline_id == pid,
            PipelineStage.key == stage_key,
        ).limit(1)
    )).scalars().first()


# ============================================================================
# Manual lead creation + CRM status
# ============================================================================

@router.post("/leads", status_code=201)
async def create_lead(
    payload: LeadCreate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create a campaign-less CRM lead.  It appears in the global Leads
    list immediately; it does NOT enter any compose/send pipeline."""
    lead = Lead(
        campaign_id=None,
        email=payload.email,
        first_name=payload.first_name,
        last_name=payload.last_name,
        company=payload.company,
        job_title=payload.job_title,
        phone=payload.phone,
        linkedin_url=payload.linkedin_url,
        company_website=payload.company_website,
        notes=payload.notes,
        crm_status=payload.crm_status.value,
    )
    db.add(lead)
    await db.commit()
    await db.refresh(lead)
    return {"id": str(lead.id), "email": lead.email, "crm_status": lead.crm_status}


@router.patch("/leads/{lead_id}", status_code=200)
async def update_lead_crm(
    lead_id: uuid.UUID,
    payload: LeadCrmUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Update the CRM-facing lead fields: status + editable contact info
    (email, name, company, job_title, phone, linkedin_url,
    company_website, notes).  PATCH — only sent fields change."""
    lead = await db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")

    updates = payload.model_dump(exclude_unset=True)

    if updates.get("crm_status") is not None:
        if payload.crm_status == CrmLeadStatus.CONVERTED:
            raise HTTPException(
                status_code=400,
                detail="Use POST /crm/leads/{id}/convert to convert a lead",
            )
        lead.crm_status = payload.crm_status.value

    # Editable contact / detail fields (validators already canonicalised
    # email + blanked empty strings to None).
    for f in (
        "email", "first_name", "last_name", "company", "job_title",
        "phone", "linkedin_url", "company_website", "notes",
    ):
        if f in updates:
            setattr(lead, f, updates[f])

    await db.commit()
    await db.refresh(lead)
    return {
        "id": str(lead.id),
        "crm_status": lead.crm_status,
        "email": lead.email,
        "first_name": lead.first_name,
        "last_name": lead.last_name,
        "company": lead.company,
        "job_title": lead.job_title,
        "phone": lead.phone,
        "linkedin_url": lead.linkedin_url,
        "company_website": lead.company_website,
        "notes": lead.notes,
    }


# ============================================================================
# Lead conversion
# ============================================================================

def _opportunity_response(
    opp: Opportunity, activity_count: int = 0, open_task_count: int = 0,
) -> OpportunityResponse:
    resp = OpportunityResponse.model_validate(opp)
    resp.amount = float(opp.amount) if opp.amount is not None else None
    resp.activity_count = activity_count
    resp.open_task_count = open_task_count
    return resp


@router.post("/leads/{lead_id}/convert", response_model=ConvertLeadResponse)
async def convert_lead(
    lead_id: uuid.UUID,
    payload: ConvertLeadRequest,
    db: AsyncSession = Depends(get_db),
) -> ConvertLeadResponse:
    """Convert a lead into an opportunity (Salesforce-style).

    - Contact snapshot copied from the lead onto the opportunity so the
      deal record survives lead deletion.
    - Lead flips to ``crm_status=converted`` and gets
      ``converted_opportunity_id`` set.
    - Existing lead-scoped activities stay on the lead; they remain
      visible from the opportunity via the source-lead link.
    - Idempotent-ish: converting an already-converted lead 409s with the
      existing opportunity id so the UI can deep-link instead of
      double-creating.
    """
    lead = await db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    if lead.converted_opportunity_id is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Lead already converted (opportunity {lead.converted_opportunity_id})",
        )

    full_name = " ".join(filter(None, [lead.first_name, lead.last_name]))
    default_name = (
        f"{lead.company or full_name or lead.email} — {date.today().isoformat()}"
    )
    opp = Opportunity(
        name=payload.name or default_name,
        stage=payload.stage,
        amount=payload.amount,
        close_date=payload.close_date,
        probability=STAGE_DEFAULT_PROBABILITY.get(payload.stage),
        first_name=lead.first_name,
        last_name=lead.last_name,
        email=lead.email,
        phone=lead.phone,
        company=lead.company,
        job_title=lead.job_title,
        linkedin_url=lead.linkedin_url,
        source_lead_id=lead.id,
    )
    # Dual-write the configurable stage graph (back-compat: NULL when no
    # pipeline is seeded — the enum still drives behavior).
    _stage_obj = await _resolve_stage(db, None, payload.stage.value)
    if _stage_obj is not None:
        opp.stage_id = _stage_obj.id
        opp.pipeline_id = _stage_obj.pipeline_id
    db.add(opp)
    await db.flush()

    lead.crm_status = CrmLeadStatus.CONVERTED.value
    lead.converted_opportunity_id = opp.id

    # Conversion is itself a logged activity so the timeline tells the
    # full story.
    db.add(CrmActivity(
        lead_id=lead.id,
        opportunity_id=opp.id,
        activity_type=CrmActivityType.NOTE,
        subject="Lead converted to opportunity",
        body=f"Opportunity: {opp.name}",
    ))

    await db.commit()
    await db.refresh(opp)
    return ConvertLeadResponse(
        opportunity=_opportunity_response(opp, activity_count=1),
        lead_id=lead.id,
        lead_crm_status=CrmLeadStatus.CONVERTED,
    )


# ============================================================================
# Opportunities
# ============================================================================

async def _get_opp_or_404(db: AsyncSession, opp_id: uuid.UUID) -> Opportunity:
    opp = await db.get(Opportunity, opp_id)
    if opp is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    return opp


async def _activity_counts(
    db: AsyncSession, opp_ids: list[uuid.UUID],
) -> dict[uuid.UUID, tuple[int, int]]:
    """{opp_id: (activity_count, open_task_count)}."""
    if not opp_ids:
        return {}
    rows = (await db.execute(
        select(
            CrmActivity.opportunity_id,
            func.count(),
            func.count().filter(and_(
                CrmActivity.activity_type == CrmActivityType.TASK,
                CrmActivity.completed_at.is_(None),
            )),
        )
        .where(CrmActivity.opportunity_id.in_(opp_ids))
        .group_by(CrmActivity.opportunity_id)
    )).all()
    return {r[0]: (r[1], r[2]) for r in rows}


@router.post("/opportunities", response_model=OpportunityResponse, status_code=201)
async def create_opportunity(
    payload: OpportunityCreate,
    db: AsyncSession = Depends(get_db),
) -> OpportunityResponse:
    opp = Opportunity(
        name=payload.name,
        stage=payload.stage,
        amount=payload.amount,
        close_date=payload.close_date,
        probability=(
            payload.probability
            if payload.probability is not None
            else STAGE_DEFAULT_PROBABILITY.get(payload.stage)
        ),
        description=payload.description,
        first_name=payload.first_name,
        last_name=payload.last_name,
        email=(payload.email or "").strip().lower() or None,
        phone=payload.phone,
        company=payload.company,
        job_title=payload.job_title,
        linkedin_url=payload.linkedin_url,
    )
    if payload.stage in CLOSED_STAGES:
        opp.closed_at = _now()
    _stage_obj = await _resolve_stage(db, None, payload.stage.value)
    if _stage_obj is not None:
        opp.stage_id = _stage_obj.id
        opp.pipeline_id = _stage_obj.pipeline_id
    db.add(opp)
    await db.commit()
    await db.refresh(opp)
    return _opportunity_response(opp)


@router.get("/opportunities", response_model=PaginatedOpportunities)
async def list_opportunities(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=500),
    stage: OpportunityStage | None = Query(default=None),
    open_only: bool = Query(default=False),
    search: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> PaginatedOpportunities:
    filters = []
    if stage is not None:
        filters.append(Opportunity.stage == stage)
    if open_only:
        filters.append(Opportunity.stage.notin_(list(CLOSED_STAGES)))
    if search:
        s = f"%{search.strip()}%"
        filters.append(
            Opportunity.name.ilike(s)
            | Opportunity.company.ilike(s)
            | Opportunity.email.ilike(s)
        )

    total = (await db.execute(
        select(func.count()).select_from(Opportunity).where(*filters)
    )).scalar_one()

    rows = (await db.execute(
        select(Opportunity)
        .where(*filters)
        .order_by(Opportunity.updated_at.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )).scalars().all()

    counts = await _activity_counts(db, [o.id for o in rows])
    items = [
        _opportunity_response(o, *counts.get(o.id, (0, 0))) for o in rows
    ]
    return PaginatedOpportunities(
        items=items, total=total, page=page, page_size=page_size,
        total_pages=math.ceil(total / page_size) if total else 0,
    )


@router.get("/opportunities/pipeline", response_model=list[PipelineSummary])
async def pipeline_summary(db: AsyncSession = Depends(get_db)) -> list[PipelineSummary]:
    """Per-stage roll-up for the Kanban board header."""
    rows = (await db.execute(
        select(
            Opportunity.stage,
            func.count(),
            func.coalesce(func.sum(Opportunity.amount), 0),
        ).group_by(Opportunity.stage)
    )).all()
    by_stage = {r[0]: (r[1], float(r[2])) for r in rows}
    return [
        PipelineSummary(
            stage=stage,
            count=by_stage.get(stage, (0, 0.0))[0],
            total_amount=by_stage.get(stage, (0, 0.0))[1],
        )
        for stage in OpportunityStage
    ]


@router.get("/pipelines/default", response_model=PipelineResponse)
async def get_default_pipeline(db: AsyncSession = Depends(get_db)) -> PipelineResponse:
    """The default pipeline + its active stages, ordered — drives the Kanban
    board columns.  Stages are configurable data (migration 0038), so the
    board reflects them rather than the hard-coded enum."""
    p = (await db.execute(
        select(Pipeline)
        .options(selectinload(Pipeline.stages))
        .where(Pipeline.is_default.is_(True))
        .order_by(Pipeline.created_at)
        .limit(1)
    )).scalars().first()
    if p is None:
        raise HTTPException(status_code=404, detail="No default pipeline configured")
    active = [s for s in p.stages if s.is_active]  # relationship is sort_order-ordered
    return PipelineResponse(
        id=p.id, name=p.name, is_default=p.is_default, stages=active,
    )


@router.get("/opportunities/{opp_id}", response_model=OpportunityResponse)
async def get_opportunity(
    opp_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> OpportunityResponse:
    opp = await _get_opp_or_404(db, opp_id)
    counts = await _activity_counts(db, [opp.id])
    return _opportunity_response(opp, *counts.get(opp.id, (0, 0)))


@router.patch("/opportunities/{opp_id}", response_model=OpportunityResponse)
async def update_opportunity(
    opp_id: uuid.UUID,
    payload: OpportunityUpdate,
    db: AsyncSession = Depends(get_db),
) -> OpportunityResponse:
    """Update fields / move stage.  Stage transitions into closed_won /
    closed_lost stamp ``closed_at``; moving back out clears it.  The
    probability auto-follows the stage default UNLESS the user has set
    an explicit probability in the same request."""
    opp = await _get_opp_or_404(db, opp_id)
    updates = payload.model_dump(exclude_unset=True)

    new_stage = updates.get("stage")
    if new_stage is not None and new_stage != opp.stage:
        old_stage = opp.stage
        old_stage_id = opp.stage_id
        opp.stage = new_stage
        if new_stage in CLOSED_STAGES:
            opp.closed_at = _now()
        else:
            opp.closed_at = None
            opp.loss_reason = None
        # Probability follows the stage default unless explicitly set.
        if "probability" not in updates:
            opp.probability = STAGE_DEFAULT_PROBABILITY.get(new_stage)
        # Dual-write the configurable stage graph + log the move.  This is the
        # single mutation point for stage changes (board drag AND the detail
        # stepper both PATCH here), so the audit is centralized.
        new_stage_obj = await _resolve_stage(db, opp.pipeline_id, new_stage.value)
        if new_stage_obj is not None:
            opp.stage_id = new_stage_obj.id
            if opp.pipeline_id is None:
                opp.pipeline_id = new_stage_obj.pipeline_id
        else:
            opp.stage_id = None
        db.add(OpportunityStageChange(
            opportunity_id=opp.id,
            tenant_id=opp.tenant_id,
            from_stage_id=old_stage_id,
            to_stage_id=opp.stage_id,
            from_stage_key=old_stage.value if old_stage is not None else None,
            to_stage_key=new_stage.value,
            source="user",                 # human-initiated drag/stepper
            changed_by=opp.owner_id,        # design-ready (no users table yet)
        ))
    updates.pop("stage", None)

    if "email" in updates and updates["email"]:
        updates["email"] = updates["email"].strip().lower()

    for key, value in updates.items():
        setattr(opp, key, value)
    await db.commit()
    await db.refresh(opp)
    counts = await _activity_counts(db, [opp.id])
    return _opportunity_response(opp, *counts.get(opp.id, (0, 0)))


@router.get(
    "/opportunities/{opp_id}/stage-history",
    response_model=list[StageChangeResponse],
)
async def opportunity_stage_history(
    opp_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> list[StageChangeResponse]:
    """Append-only stage-move audit for one opportunity (newest first).
    Captures old/new stage, source (user|agent), actor, and timestamp."""
    await _get_opp_or_404(db, opp_id)
    rows = (await db.execute(
        select(OpportunityStageChange)
        .where(OpportunityStageChange.opportunity_id == opp_id)
        .order_by(OpportunityStageChange.created_at.desc())
    )).scalars().all()
    return [StageChangeResponse.model_validate(r) for r in rows]


@router.delete("/opportunities/{opp_id}", status_code=204, response_model=None)
async def delete_opportunity(
    opp_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    opp = await _get_opp_or_404(db, opp_id)
    # Un-convert the source lead so it can be converted again later.
    if opp.source_lead_id is not None:
        lead = await db.get(Lead, opp.source_lead_id)
        if lead is not None and lead.converted_opportunity_id == opp.id:
            lead.converted_opportunity_id = None
            if lead.crm_status == CrmLeadStatus.CONVERTED.value:
                lead.crm_status = CrmLeadStatus.QUALIFIED.value
    await db.delete(opp)
    await db.commit()


# ============================================================================
# Activities
# ============================================================================

@router.post("/activities", response_model=ActivityResponse, status_code=201)
async def create_activity(
    payload: ActivityCreate,
    db: AsyncSession = Depends(get_db),
) -> ActivityResponse:
    if payload.lead_id is None and payload.opportunity_id is None:
        raise HTTPException(
            status_code=422,
            detail="Activity needs a lead_id and/or an opportunity_id",
        )
    if payload.lead_id is not None and await db.get(Lead, payload.lead_id) is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    if payload.opportunity_id is not None and await db.get(Opportunity, payload.opportunity_id) is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    activity = CrmActivity(
        lead_id=payload.lead_id,
        opportunity_id=payload.opportunity_id,
        activity_type=payload.activity_type,
        subject=payload.subject,
        body=payload.body,
        direction=payload.direction,
        due_at=payload.due_at,
        occurred_at=payload.occurred_at or _now(),
    )
    db.add(activity)
    await db.flush()
    # Logging a touch (call/email/meeting/note) on a lead/deal closes that
    # record's open due/overdue tasks — the work the reminder was nagging
    # about has now been done.  No-op for TASK activities (more to-do, not
    # "done") and for future-dated tasks.
    if activity.activity_type in agent_core.TOUCH_ACTIVITY_TYPES:
        await agent_core.autocomplete_due_tasks_for_record(
            db, lead_id=activity.lead_id, opportunity_id=activity.opportunity_id,
        )
    await db.commit()
    await db.refresh(activity)
    return ActivityResponse.model_validate(activity)


@router.get("/activities", response_model=PaginatedActivities)
async def list_activities(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    lead_id: uuid.UUID | None = None,
    opportunity_id: uuid.UUID | None = None,
    activity_type: CrmActivityType | None = Query(default=None),
    open_tasks: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
) -> PaginatedActivities:
    """List activities; filter by parent / type.  ``open_tasks=true``
    returns incomplete tasks across ALL parents ordered by due date —
    the 'what's on my plate' view."""
    filters = []
    if lead_id is not None:
        filters.append(CrmActivity.lead_id == lead_id)
    if opportunity_id is not None:
        filters.append(CrmActivity.opportunity_id == opportunity_id)
    if activity_type is not None:
        filters.append(CrmActivity.activity_type == activity_type)
    if open_tasks:
        filters.append(CrmActivity.activity_type == CrmActivityType.TASK)
        filters.append(CrmActivity.completed_at.is_(None))

    total = (await db.execute(
        select(func.count()).select_from(CrmActivity).where(*filters)
    )).scalar_one()

    order = (
        CrmActivity.due_at.asc().nulls_last()
        if open_tasks else CrmActivity.occurred_at.desc()
    )
    rows = (await db.execute(
        select(CrmActivity)
        .where(*filters)
        .order_by(order)
        .limit(page_size)
        .offset((page - 1) * page_size)
    )).scalars().all()

    return PaginatedActivities(
        items=[ActivityResponse.model_validate(a) for a in rows],
        total=total, page=page, page_size=page_size,
        total_pages=math.ceil(total / page_size) if total else 0,
    )


@router.patch("/activities/{activity_id}", response_model=ActivityResponse)
async def update_activity(
    activity_id: uuid.UUID,
    payload: ActivityUpdate,
    db: AsyncSession = Depends(get_db),
) -> ActivityResponse:
    activity = await db.get(CrmActivity, activity_id)
    if activity is None:
        raise HTTPException(status_code=404, detail="Activity not found")

    updates = payload.model_dump(exclude_unset=True)
    completed = updates.pop("completed", None)
    if completed is True and activity.completed_at is None:
        activity.completed_at = _now()
    elif completed is False:
        activity.completed_at = None

    for key, value in updates.items():
        setattr(activity, key, value)
    await db.commit()
    await db.refresh(activity)
    return ActivityResponse.model_validate(activity)


@router.delete("/activities/{activity_id}", status_code=204, response_model=None)
async def delete_activity(
    activity_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    activity = await db.get(CrmActivity, activity_id)
    if activity is None:
        raise HTTPException(status_code=404, detail="Activity not found")
    await db.delete(activity)
    await db.commit()


# ============================================================================
# Documents
# ============================================================================

# 10MB per file — keeps the BYTEA storage sane.  Proposals / contracts /
# quotes fit comfortably; anything bigger belongs in a shared drive with
# a link in the deal description.
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024


@router.post(
    "/opportunities/{opp_id}/documents",
    response_model=DocumentResponse,
    status_code=201,
)
async def upload_document(
    opp_id: uuid.UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
) -> DocumentResponse:
    await _get_opp_or_404(db, opp_id)
    data = await file.read()
    if len(data) == 0:
        raise HTTPException(status_code=422, detail="File is empty")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"File too large ({len(data) / 1024 / 1024:.1f}MB). "
                f"Max {MAX_DOCUMENT_BYTES // 1024 // 1024}MB — for bigger "
                "files, store a shared-drive link in the deal description."
            ),
        )
    doc = CrmDocument(
        opportunity_id=opp_id,
        filename=file.filename or "untitled",
        content_type=file.content_type or "application/octet-stream",
        size_bytes=len(data),
        data=data,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return DocumentResponse.model_validate(doc)


@router.get(
    "/opportunities/{opp_id}/documents",
    response_model=list[DocumentResponse],
)
async def list_documents(
    opp_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> list[DocumentResponse]:
    await _get_opp_or_404(db, opp_id)
    rows = (await db.execute(
        # Explicit column list — skips loading the (potentially large)
        # ``data`` BYTEA for the listing.
        select(
            CrmDocument.id, CrmDocument.opportunity_id, CrmDocument.filename,
            CrmDocument.content_type, CrmDocument.size_bytes, CrmDocument.uploaded_at,
        )
        .where(CrmDocument.opportunity_id == opp_id)
        .order_by(CrmDocument.uploaded_at.desc())
    )).all()
    return [
        DocumentResponse(
            id=r.id, opportunity_id=r.opportunity_id, filename=r.filename,
            content_type=r.content_type, size_bytes=r.size_bytes,
            uploaded_at=r.uploaded_at,
        )
        for r in rows
    ]


@router.get("/documents/{doc_id}/download")
async def download_document(
    doc_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> Response:
    doc = await db.get(CrmDocument, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    # RFC 6266 filename* would handle non-ASCII more completely; a plain
    # quoted filename with stripped quotes covers the realistic cases.
    safe_name = (doc.filename or "download").replace('"', "")
    return Response(
        content=doc.data,
        media_type=doc.content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
        },
    )


@router.delete("/documents/{doc_id}", status_code=204, response_model=None)
async def delete_document(
    doc_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    doc = await db.get(CrmDocument, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    await db.delete(doc)
    await db.commit()


# ============================================================================
# Products of interest
# ============================================================================

def _product_response(p: OpportunityProduct) -> ProductResponse:
    resp = ProductResponse.model_validate(p)
    resp.quantity = float(p.quantity)
    resp.unit_price = float(p.unit_price) if p.unit_price is not None else None
    resp.line_total = (
        round(float(p.quantity) * float(p.unit_price), 2)
        if p.unit_price is not None else None
    )
    return resp


@router.post(
    "/opportunities/{opp_id}/products",
    response_model=ProductResponse,
    status_code=201,
)
async def add_product(
    opp_id: uuid.UUID,
    payload: ProductCreate,
    db: AsyncSession = Depends(get_db),
) -> ProductResponse:
    await _get_opp_or_404(db, opp_id)
    product = OpportunityProduct(
        opportunity_id=opp_id,
        product_name=payload.product_name,
        quantity=payload.quantity,
        unit_price=payload.unit_price,
        notes=payload.notes,
    )
    db.add(product)
    await db.commit()
    await db.refresh(product)
    return _product_response(product)


@router.get(
    "/opportunities/{opp_id}/products",
    response_model=ProductListResponse,
)
async def list_products(
    opp_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> ProductListResponse:
    await _get_opp_or_404(db, opp_id)
    rows = (await db.execute(
        select(OpportunityProduct)
        .where(OpportunityProduct.opportunity_id == opp_id)
        .order_by(OpportunityProduct.created_at.asc())
    )).scalars().all()
    items = [_product_response(p) for p in rows]
    return ProductListResponse(
        items=items,
        products_total=round(sum(i.line_total or 0 for i in items), 2),
    )


@router.patch("/products/{product_id}", response_model=ProductResponse)
async def update_product(
    product_id: uuid.UUID,
    payload: ProductUpdate,
    db: AsyncSession = Depends(get_db),
) -> ProductResponse:
    product = await db.get(OpportunityProduct, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(product, key, value)
    await db.commit()
    await db.refresh(product)
    return _product_response(product)


@router.delete("/products/{product_id}", status_code=204, response_model=None)
async def delete_product(
    product_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    product = await db.get(OpportunityProduct, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    await db.delete(product)
    await db.commit()
