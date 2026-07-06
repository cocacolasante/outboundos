"""Retargeting — build a follow-up campaign from leads who ENGAGED with a
previous campaign (clicked a tracked link, or accepted a LinkedIn connection).

Engaged leads are COPIED into a retarget campaign (fresh Lead rows; the source
stays put), each stamped with ``research_data.retarget_context`` — the email
subject/body they received + the link they clicked — which ``compose`` reads to
draft a re-engagement email referencing their interest.

Dedup is strict: ``add_leads_to_campaign`` skips any lead whose canonical email
is already in the target campaign (and within the same batch), so clicking the
retarget button twice never creates duplicates.

LinkedIn-connection engagement is wired but UNTESTED (Unipile isn't active).
Retarget campaigns are identified by the ``Retarget — `` name prefix (no schema
change), mirroring the intent-drafts convention.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Campaign, CampaignStatus, EmailEvent, EmailEventType, Lead,
    LinkedInConnectionStatus, ResearchMode,
)
from app.services.campaign_membership import AddLeadsResult, add_leads_to_campaign

logger = logging.getLogger(__name__)

RETARGET_PREFIX = "Retarget — "


def is_retarget_campaign(campaign: Campaign) -> bool:
    return (campaign.name or "").startswith(RETARGET_PREFIX)


async def engaged_leads(db: AsyncSession, source_campaign_id: uuid.UUID) -> list[tuple[Lead, dict]]:
    """Leads in the source campaign who engaged, with the context compose uses.
    Email clickers first (strongest), then LinkedIn-connected; de-duped per lead.
    """
    out: list[tuple[Lead, dict]] = []
    seen: set[uuid.UUID] = set()

    # 1) Clicked a tracked link (clicking implies they opened).
    click_rows = (await db.execute(
        select(EmailEvent, Lead)
        .join(Lead, Lead.id == EmailEvent.lead_id)
        .where(
            EmailEvent.campaign_id == source_campaign_id,
            EmailEvent.event_type == EmailEventType.CLICKED,
        )
        .order_by(EmailEvent.occurred_at.desc())   # newest click per lead wins
    )).all()
    for ev, lead in click_rows:
        if lead.id in seen:
            continue
        seen.add(lead.id)
        data = ev.event_data or {}
        out.append((lead, {
            "engaged_via": "email_click",
            "clicked_url": data.get("link") or data.get("url"),
            "clicked_at": str(ev.occurred_at),
            "original_subject": lead.composed_subject,
            "original_body": lead.composed_body,
            "source_campaign_id": str(source_campaign_id),
        }))

    # 2) Accepted a LinkedIn connection (wired, untested — Unipile inactive).
    li_leads = (await db.execute(
        select(Lead).where(
            Lead.campaign_id == source_campaign_id,
            Lead.linkedin_connection_status == LinkedInConnectionStatus.CONNECTED,
        )
    )).scalars().all()
    for lead in li_leads:
        if lead.id in seen:
            continue
        seen.add(lead.id)
        out.append((lead, {
            "engaged_via": "linkedin_connection",
            "original_subject": lead.composed_subject,
            "original_body": lead.composed_body,
            "source_campaign_id": str(source_campaign_id),
        }))
    return out


async def count_engaged(db: AsyncSession, source_campaign_id: uuid.UUID) -> int:
    return len(await engaged_leads(db, source_campaign_id))


async def create_retarget_campaign(
    db: AsyncSession, source: Campaign, *, name: str | None = None, goal: str | None = None,
) -> Campaign:
    """A DRAFT retarget campaign inheriting the source's sender/schedule/pacing.
    research_mode=NONE — these are existing engaged leads, no fresh research
    needed (and it preserves the retarget_context compose references)."""
    camp = Campaign(
        name=name or f"{RETARGET_PREFIX}{source.name}",
        goal=goal or (
            f"Re-engage leads who clicked a link in '{source.name}'. Build on the "
            "interest they already showed and move them toward a reply."
        ),
        tone=source.tone,
        sender_name=source.sender_name,
        sender_email=source.sender_email,
        connected_account_id=source.connected_account_id,
        research_mode=ResearchMode.NONE,
        schedule_days=list(source.schedule_days or []),
        schedule_time_start=source.schedule_time_start,
        schedule_time_end=source.schedule_time_end,
        schedule_timezone=source.schedule_timezone,
        min_delay_seconds=source.min_delay_seconds,
        max_per_hour=source.max_per_hour,
        max_per_day=source.max_per_day,
        status=CampaignStatus.DRAFT,
    )
    db.add(camp)
    await db.flush()
    return camp


async def retarget_into_campaign(
    db: AsyncSession, source: Campaign, target: Campaign,
) -> tuple[AddLeadsResult, int]:
    """Copy the source's engaged leads into the target retarget campaign,
    stamping each with its retarget context.  Returns (result, engaged_count).
    Dedup (no duplicate emails in the target) is enforced by
    add_leads_to_campaign."""
    pairs = await engaged_leads(db, source.id)
    rdbs: dict[uuid.UUID, dict] = {}
    for lead, ctx in pairs:
        rdbs[lead.id] = {
            "quality": "low", "skipped": True, "from_retarget": True,
            "retarget_context": {**ctx, "source_campaign_name": source.name},
        }
    result = await add_leads_to_campaign(
        db, target, [lead.id for lead, _ in pairs], research_data_by_source=rdbs,
    )
    return result, len(pairs)
