"""Copy existing leads into a campaign (shared core).

Used by the Leads-page bulk "Add to campaign" and the Signals-page
"Add to campaign".  COPY, not move: ``leads.campaign_id`` cascades on
campaign delete, so reassigning a CRM/signal lead would let routine
campaign deletion destroy its CRM history.  The source row is left
untouched; a fresh row enters the campaign's pipeline.

The new rows are sequence-enrolled and (for non-draft campaigns)
research is kicked immediately — sends then follow the campaign's own
status gates (PREVIEWING waits for approve-all, PAUSED waits for
resume).  Draft campaigns hold the rows until launch.

Skips are counted, never errors: emails already in the target campaign,
suppressed emails, unknown lead ids, and in-batch duplicates.  Callers
own the 404/409 (campaign missing / COMPLETE) before calling.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Campaign, CampaignStatus, Lead, Suppression, canonical_email
from app.services.sequence_service import ensure_default_sequence, enroll_leads


@dataclass
class AddLeadsResult:
    added: int = 0
    skipped_duplicate: int = 0
    skipped_suppressed: int = 0
    skipped_missing: int = 0
    research_started: bool = False


async def add_leads_to_campaign(
    db: AsyncSession, campaign: Campaign, lead_ids: list[uuid.UUID],
    *, research_data_by_source: dict[uuid.UUID, dict] | None = None,
) -> AddLeadsResult:
    """Copy the given leads into ``campaign``.  Commits the session.

    ``research_data_by_source`` optionally seeds each NEW lead's
    ``research_data`` from its SOURCE lead id (used by retargeting to carry the
    engagement context the AI references)."""
    requested_ids = list(dict.fromkeys(lead_ids))  # de-dupe, keep order
    if not requested_ids:
        return AddLeadsResult()

    sources = list((await db.execute(
        select(Lead).where(Lead.id.in_(requested_ids))
    )).scalars().all())
    skipped_missing = len(requested_ids) - len(sources)

    emails = {canonical_email(l.email) for l in sources}
    suppressed_set: set[str] = set()
    existing_set: set[str] = set()
    if emails:
        suppressed_set = {
            r[0] for r in (await db.execute(
                select(Suppression.email).where(Suppression.email.in_(emails))
            )).all()
        }
        existing_set = {
            canonical_email(e) for (e,) in (await db.execute(
                select(Lead.email).where(
                    Lead.campaign_id == campaign.id,
                    func.lower(Lead.email).in_(emails),
                )
            )).all()
        }

    new_leads: list[Lead] = []
    skipped_duplicate = 0
    skipped_suppressed = 0
    for src in sources:
        email = canonical_email(src.email)
        if email in suppressed_set:
            skipped_suppressed += 1
            continue
        if email in existing_set:
            skipped_duplicate += 1
            continue
        existing_set.add(email)  # de-dupe within the batch too
        new_leads.append(Lead(
            campaign_id=campaign.id,
            email=email,
            first_name=src.first_name,
            last_name=src.last_name,
            company=src.company,
            job_title=src.job_title,
            phone=src.phone,
            linkedin_url=src.linkedin_url,
            company_website=src.company_website,
            timezone=src.timezone,
            research_data=(research_data_by_source or {}).get(src.id),
        ))

    if new_leads:
        db.add_all(new_leads)
        await db.flush()
        await ensure_default_sequence(db, campaign)
        await enroll_leads(db, campaign.id, [l.id for l in new_leads])
    await db.commit()

    research_started = False
    if new_leads and campaign.status != CampaignStatus.DRAFT:
        # Lazy import keeps the worker (celery) off the service import path.
        from app.workers import ingest as ingest_tasks
        ingest_tasks.run_campaign_research.delay(str(campaign.id))
        research_started = True

    return AddLeadsResult(
        added=len(new_leads),
        skipped_duplicate=skipped_duplicate,
        skipped_suppressed=skipped_suppressed,
        skipped_missing=skipped_missing,
        research_started=research_started,
    )
