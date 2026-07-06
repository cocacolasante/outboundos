"""Custom report builder API (Phase 3).

CRUD over saved ``report_definitions`` + a metadata endpoint (the whitelist
for the builder UI) + ad-hoc / saved run endpoints.  All runs go through the
metadata-driven query service — **never** raw user SQL.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import ReportDefinition
from app.schemas.report_builder import (
    ReportCreate,
    ReportDefinitionPayload,
    ReportResponse,
    ReportRunResult,
    ReportUpdate,
)
from app.services import report_query
from app.services.report_registry import build_metadata

router = APIRouter(prefix="/reports", tags=["reports"])


async def _get_or_404(db: AsyncSession, report_id: uuid.UUID) -> ReportDefinition:
    rd = await db.get(ReportDefinition, report_id)
    if rd is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return rd


@router.get("/metadata")
async def report_metadata() -> dict:
    """The whitelist (objects -> fields with type / operators / aggregates)
    that the builder UI renders its controls from."""
    return build_metadata()


@router.get("", response_model=list[ReportResponse])
async def list_reports(db: AsyncSession = Depends(get_db)) -> list[ReportResponse]:
    rows = (await db.execute(
        select(ReportDefinition).order_by(ReportDefinition.updated_at.desc())
    )).scalars().all()
    return [ReportResponse.model_validate(r) for r in rows]


@router.post("", response_model=ReportResponse, status_code=201)
async def create_report(
    payload: ReportCreate, db: AsyncSession = Depends(get_db),
) -> ReportResponse:
    try:
        report_query.validate_definition(payload.data_source, payload.definition)
    except report_query.ReportError as e:
        raise HTTPException(status_code=400, detail=str(e))
    rd = ReportDefinition(
        name=payload.name.strip(),
        description=payload.description,
        data_source=payload.data_source,
        definition=payload.definition,
    )
    db.add(rd)
    await db.commit()
    await db.refresh(rd)
    return ReportResponse.model_validate(rd)


@router.get("/{report_id}", response_model=ReportResponse)
async def get_report(
    report_id: uuid.UUID, db: AsyncSession = Depends(get_db),
) -> ReportResponse:
    return ReportResponse.model_validate(await _get_or_404(db, report_id))


@router.patch("/{report_id}", response_model=ReportResponse)
async def update_report(
    report_id: uuid.UUID, payload: ReportUpdate, db: AsyncSession = Depends(get_db),
) -> ReportResponse:
    rd = await _get_or_404(db, report_id)
    updates = payload.model_dump(exclude_unset=True)
    # Validate the resulting (data_source, definition) against the whitelist.
    new_source = updates.get("data_source", rd.data_source)
    new_def = updates.get("definition", rd.definition)
    if "data_source" in updates or "definition" in updates:
        try:
            report_query.validate_definition(new_source, new_def)
        except report_query.ReportError as e:
            raise HTTPException(status_code=400, detail=str(e))
    if "name" in updates and updates["name"]:
        rd.name = updates["name"].strip()
    if "description" in updates:
        rd.description = updates["description"]
    if "data_source" in updates:
        rd.data_source = updates["data_source"]
    if "definition" in updates and updates["definition"] is not None:
        rd.definition = updates["definition"]
    await db.commit()
    await db.refresh(rd)
    return ReportResponse.model_validate(rd)


@router.post("/{report_id}/duplicate", response_model=ReportResponse, status_code=201)
async def duplicate_report(
    report_id: uuid.UUID, db: AsyncSession = Depends(get_db),
) -> ReportResponse:
    src = await _get_or_404(db, report_id)
    dup = ReportDefinition(
        name=f"{src.name} (copy)",
        description=src.description,
        data_source=src.data_source,
        definition=src.definition,
        owner_id=src.owner_id,
    )
    db.add(dup)
    await db.commit()
    await db.refresh(dup)
    return ReportResponse.model_validate(dup)


@router.delete("/{report_id}", status_code=204, response_model=None)
async def delete_report(report_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> None:
    rd = await _get_or_404(db, report_id)
    await db.delete(rd)
    await db.commit()


@router.post("/run", response_model=ReportRunResult)
async def run_adhoc(
    payload: ReportDefinitionPayload, db: AsyncSession = Depends(get_db),
) -> ReportRunResult:
    """Run a definition without saving (builder preview)."""
    try:
        result = await report_query.run_report(db, payload.data_source, payload.definition)
    except report_query.ReportError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return ReportRunResult(**result)


@router.post("/{report_id}/run", response_model=ReportRunResult)
async def run_saved(
    report_id: uuid.UUID, db: AsyncSession = Depends(get_db),
) -> ReportRunResult:
    rd = await _get_or_404(db, report_id)
    try:
        result = await report_query.run_report(db, rd.data_source, rd.definition)
    except report_query.ReportError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return ReportRunResult(**result)
