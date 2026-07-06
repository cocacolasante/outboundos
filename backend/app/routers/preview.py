from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status as http_status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.sender import campaign_sender_ready
from app.models import (
    Campaign,
    CampaignStatus,
    ComposeStatus,
    Lead,
    ResearchStatus,
    SendStatus,
    StyleCorrection,
)
from app.schemas.preview import (
    ApproveAllResponse,
    PreviewProgress,
    PreviewResponse,
    RejectResponse,
    SamplePreview,
    SampleUpdateRequest,
)

router = APIRouter(tags=["preview"])


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


async def _get_campaign_or_404(db: AsyncSession, campaign_id: uuid.UUID) -> Campaign:
    c = await db.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return c


def _summarize_research(data: dict | None) -> str:
    """Produce a brief human-readable digest of research_data for the preview UI."""
    if not data:
        return "No research data available."
    parts: list[str] = []
    person_news = [x for x in (data.get("person_news") or []) if x]
    if person_news:
        parts.append("Recent: " + "; ".join(person_news[:3]))
    company_desc = (data.get("company_description") or "").strip()
    if company_desc:
        snippet = company_desc if len(company_desc) <= 220 else company_desc[:217] + "..."
        parts.append(f"Company: {snippet}")
    industry = (data.get("industry") or "").strip()
    if industry:
        parts.append(f"Industry: {industry}")
    headline = (data.get("linkedin_headline") or "").strip()
    if headline:
        parts.append(f"Headline: {headline}")
    return " | ".join(parts) if parts else "Limited research findings."


def _sample_to_response(lead: Lead) -> SamplePreview:
    rd = lead.research_data or {}
    return SamplePreview(
        lead_id=lead.id,
        email=lead.email,
        first_name=lead.first_name,
        last_name=lead.last_name,
        company=lead.company,
        research_quality=str(rd.get("quality") or "low"),
        research_summary=_summarize_research(rd),
        composed_subject=lead.composed_subject,
        composed_body=lead.composed_body,
        compose_status=lead.compose_status,
        sample_approved=lead.sample_approved,
    )


# --------------------------------------------------------------------------
# GET preview
# --------------------------------------------------------------------------


@router.get("/campaigns/{campaign_id}/preview", response_model=PreviewResponse)
async def get_preview(
    campaign_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> PreviewResponse:
    campaign = await _get_campaign_or_404(db, campaign_id)

    samples = list((await db.execute(
        select(Lead)
        .where(Lead.campaign_id == campaign_id, Lead.is_sample.is_(True))
        .order_by(Lead.created_at.asc())
    )).scalars().all())

    all_ready = bool(samples) and all(
        s.compose_status == ComposeStatus.DONE for s in samples
    )

    return PreviewResponse(
        campaign_id=campaign.id,
        status=campaign.status,
        samples=[_sample_to_response(s) for s in samples],
        all_ready=all_ready,
    )


# --------------------------------------------------------------------------
# PATCH sample
# --------------------------------------------------------------------------


@router.patch(
    "/campaigns/{campaign_id}/preview/samples/{lead_id}",
    response_model=SamplePreview,
)
async def update_sample(
    campaign_id: uuid.UUID,
    lead_id: uuid.UUID,
    payload: SampleUpdateRequest,
    db: AsyncSession = Depends(get_db),
) -> SamplePreview:
    lead = await db.get(Lead, lead_id)
    if lead is None or lead.campaign_id != campaign_id:
        raise HTTPException(status_code=404, detail="Sample lead not found")
    if not lead.is_sample:
        raise HTTPException(
            status_code=400,
            detail="Only sample leads can be edited through the preview API",
        )

    updates = payload.model_dump(exclude_unset=True)
    new_body = updates.get("composed_body")
    if new_body is not None and lead.composed_body is not None and new_body != lead.composed_body:
        # Capture the edit as a style correction so future compose runs can
        # learn the user's preferred tone.
        db.add(StyleCorrection(
            campaign_id=lead.campaign_id,
            original_body=lead.composed_body,
            corrected_body=new_body,
        ))

    if "composed_subject" in updates and updates["composed_subject"] is not None:
        lead.composed_subject = updates["composed_subject"]
    if new_body is not None:
        lead.composed_body = new_body
    if "approved" in updates and updates["approved"] is not None:
        lead.sample_approved = bool(updates["approved"])

    await db.commit()
    await db.refresh(lead)
    return _sample_to_response(lead)


# --------------------------------------------------------------------------
# Approve-all (kick off full campaign)
# --------------------------------------------------------------------------


async def _kick_off_full_campaign(
    db: AsyncSession, campaign: Campaign
) -> tuple[int, int]:
    """Transition the campaign to running and queue every already-composed lead
    for sending. Returns (samples_approved, leads_dispatched_to_send).
    """
    # Mark every sample as approved (spec: "Mark all samples as approved").
    approve_result = await db.execute(
        update(Lead)
        .where(Lead.campaign_id == campaign.id, Lead.is_sample.is_(True))
        .values(sample_approved=True)
    )
    samples_approved = approve_result.rowcount or 0

    campaign.status = CampaignStatus.RUNNING

    # Snapshot the composed leads to dispatch — read before commit so the IDs
    # are captured even though the rowset is closed when the transaction
    # commits.
    composed_ids = list((await db.execute(
        select(Lead.id)
        .where(
            Lead.campaign_id == campaign.id,
            Lead.compose_status == ComposeStatus.DONE,
        )
    )).scalars().all())

    await db.commit()

    # No dispatch here.  The campaign is now RUNNING, so the beat-driven
    # ``pace_first_emails`` pacer feeds these composed leads to ``send_lead``
    # at the rate the caps allow.  The old eta-staggered dispatch (sends
    # spaced ``i * min_delay`` apart) produced far-future-eta tasks that the
    # broker (``visibility_timeout=300s``) redelivered every 5 min, storming
    # the queue; the pacer replaces it.
    return samples_approved, len(composed_ids)


@router.post(
    "/campaigns/{campaign_id}/preview/approve-all",
    response_model=ApproveAllResponse,
)
async def approve_all(
    campaign_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> ApproveAllResponse:
    campaign = await _get_campaign_or_404(db, campaign_id)
    if campaign.status != CampaignStatus.PREVIEWING:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot approve a campaign in status '{campaign.status.value}' "
                "(only previewing)"
            ),
        )
    if not campaign_sender_ready(campaign):
        raise HTTPException(
            status_code=409,
            detail="Set a valid sending email (Sender) on the campaign before launching.",
        )

    approved, dispatched = await _kick_off_full_campaign(db, campaign)
    return ApproveAllResponse(
        campaign_id=campaign.id,
        status=campaign.status,
        samples_approved=approved,
        leads_dispatched_to_send=dispatched,
    )


# --------------------------------------------------------------------------
# Reject
# --------------------------------------------------------------------------


@router.post(
    "/campaigns/{campaign_id}/preview/reject",
    response_model=RejectResponse,
)
async def reject_preview(
    campaign_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> RejectResponse:
    campaign = await _get_campaign_or_404(db, campaign_id)
    if campaign.status != CampaignStatus.PREVIEWING:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot reject a campaign in status '{campaign.status.value}' "
                "(only previewing)"
            ),
        )

    # Reset every lead in this campaign to pre-compose state.
    result = await db.execute(
        update(Lead)
        .where(Lead.campaign_id == campaign.id)
        .values(
            compose_status=ComposeStatus.PENDING,
            composed_subject=None,
            composed_body=None,
            sample_approved=None,
        )
    )
    leads_cleared = result.rowcount or 0

    campaign.status = CampaignStatus.DRAFT
    await db.commit()

    return RejectResponse(
        campaign_id=campaign.id,
        status=campaign.status,
        leads_cleared=leads_cleared,
    )


# --------------------------------------------------------------------------
# Progress
# --------------------------------------------------------------------------


@router.get(
    "/campaigns/{campaign_id}/preview/progress",
    response_model=PreviewProgress,
)
async def get_progress(
    campaign_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> PreviewProgress:
    campaign = await _get_campaign_or_404(db, campaign_id)

    total = (await db.execute(
        select(func.count()).select_from(Lead).where(Lead.campaign_id == campaign_id)
    )).scalar_one()

    researched = (await db.execute(
        select(func.count()).select_from(Lead).where(
            Lead.campaign_id == campaign_id,
            Lead.research_status == ResearchStatus.DONE,
        )
    )).scalar_one()

    composed = (await db.execute(
        select(func.count()).select_from(Lead).where(
            Lead.campaign_id == campaign_id,
            Lead.compose_status == ComposeStatus.DONE,
        )
    )).scalar_one()

    composing = (await db.execute(
        select(func.count()).select_from(Lead).where(
            Lead.campaign_id == campaign_id,
            Lead.compose_status == ComposeStatus.RUNNING,
        )
    )).scalar_one()

    # The AI pipeline is "active" while any lead still has research or
    # compose work queued (PENDING) or in flight (RUNNING).  Compose-PENDING
    # only counts when research actually finished (DONE) — a research-FAILED
    # lead also sits at compose-PENDING but will never compose, so it's
    # terminal, not active.  Once this hits 0 there's nothing left to stop.
    pipeline_pending = (await db.execute(
        select(func.count()).select_from(Lead).where(
            Lead.campaign_id == campaign_id,
            (
                Lead.research_status.in_(
                    (ResearchStatus.PENDING, ResearchStatus.RUNNING)
                )
                | (
                    (Lead.research_status == ResearchStatus.DONE)
                    & Lead.compose_status.in_(
                        (ComposeStatus.PENDING, ComposeStatus.RUNNING)
                    )
                )
            ),
        )
    )).scalar_one()

    sent = (await db.execute(
        select(func.count()).select_from(Lead).where(
            Lead.campaign_id == campaign_id,
            Lead.send_status == SendStatus.SENT,
        )
    )).scalar_one()

    failed = (await db.execute(
        select(func.count()).select_from(Lead).where(
            Lead.campaign_id == campaign_id,
            (
                (Lead.research_status == ResearchStatus.FAILED)
                | (Lead.compose_status == ComposeStatus.FAILED)
                | (Lead.send_status == SendStatus.FAILED)
            ),
        )
    )).scalar_one()

    # Goal-rewrite progress: when the goal has been edited post-draft, count
    # how many unsent leads have caught up to the new goal vs how many are
    # still queued.  Sent leads are frozen (and stay on the old goal — by
    # design; rewriting an already-delivered email would be incoherent).
    rewrite_total = 0
    rewrite_done = 0
    if campaign.goal_updated_at is not None:
        # Denominator: every unsent lead that's already produced an email
        # (DONE) plus any actively being rewritten (RUNNING).  Pending /
        # failed are excluded — they haven't yet entered the rewrite scope
        # and have their own status track.
        rewrite_total = (await db.execute(
            select(func.count()).select_from(Lead).where(
                Lead.campaign_id == campaign_id,
                Lead.send_status != SendStatus.SENT,
                Lead.compose_status.in_(
                    (ComposeStatus.DONE, ComposeStatus.RUNNING)
                ),
            )
        )).scalar_one()
        # Numerator: leads composed AT OR AFTER the goal edit.  ``updated_at``
        # is bumped on every status transition, so a lead that's transitioned
        # DONE -> RUNNING -> DONE since the stamp has updated_at >= stamp.
        rewrite_done = (await db.execute(
            select(func.count()).select_from(Lead).where(
                Lead.campaign_id == campaign_id,
                Lead.send_status != SendStatus.SENT,
                Lead.compose_status == ComposeStatus.DONE,
                Lead.updated_at >= campaign.goal_updated_at,
            )
        )).scalar_one()

    return PreviewProgress(
        total_leads=total,
        researched=researched,
        composed=composed,
        sent=sent,
        failed=failed,
        composing=composing,
        pipeline_active=pipeline_pending > 0,
        goal_updated_at=campaign.goal_updated_at,
        rewrite_total=rewrite_total,
        rewrite_done=rewrite_done,
    )
