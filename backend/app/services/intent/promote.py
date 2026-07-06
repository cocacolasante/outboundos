"""Trigger bridge — a promoted org becomes an approval-pending DRAFT (Phase 4).

HARD AUTONOMY BOUNDARY (non-negotiable).  This module:
  - NEVER sends a prospect email,
  - NEVER converts a lead,
  - NEVER sets a campaign RUNNING.
It only stages a DRAFT in the existing campaign system that a human reviews and
approves through the normal approve-all flow.  The campaign is created/kept
``DRAFT`` (which the send pipeline never touches) and the lead is left
``send_status=PENDING`` — nothing leaves until a human approves.

Generated copy references ONLY the evidenced signal — the top signal's
``summary`` (the real "why now") and its ``evidence_url`` — rendered via the
zero-cost template path (no LLM, so no invented funder names, amounts, or
outcomes).  The human edits before approving.

The top signal is marked ``promoted`` only AFTER a draft exists.  Idempotent:
an org that already has an open (un-sent) intent draft is never re-drafted.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    Campaign, CampaignStatus, ComposeStatus, IcpIntentProfile,
    IntentSignalStatus, Lead, NotificationKind, Org, OrgIntentScore,
    ResearchMode, ResearchStatus, SendStatus, Signal,
)
from app.services import notifications
from app.services.intent import scoring
from app.services.template_render import build_merge_context, render_template

logger = logging.getLogger(__name__)

_TIER_LABEL = {1: "Tier 1 — act now", 2: "Tier 2 — warm", 3: "Tier 3 — list"}

# Deterministic draft copy.  The ONLY org-specific facts are merge fields —
# {{intent_why_now}} is the evidenced signal summary, {{intent_evidence_url}}
# its source link — so nothing is invented.  Bracketed guidance reminds the
# human this is a draft to edit before approving.
_DRAFT_SUBJECT = "A note for {{company|your team}}"
_DRAFT_BODY = (
    "Hi {{first_name|there}},\n\n"
    "{{intent_why_now}}\n\n"
    "[DRAFT — review & edit before approving. We can help {{company|your organization}} "
    "act on this; source: {{intent_evidence_url}}]\n\n"
    "Best,\n{{sender_name}}"
)


def _draft_campaign_name(profile: IcpIntentProfile | None) -> str:
    return f"Intent drafts — {profile.name if profile else 'default'}"


async def _get_or_create_draft_campaign(
    session: AsyncSession, profile: IcpIntentProfile | None,
) -> Campaign:
    name = _draft_campaign_name(profile)
    # Reuse the current awaiting-approval batch; never add to one already
    # RUNNING (that would send new drafts without review — a fresh PREVIEWING
    # batch is created instead).
    camp = await session.scalar(
        select(Campaign).where(
            Campaign.name == name,
            Campaign.status.in_([CampaignStatus.PREVIEWING, CampaignStatus.DRAFT]),
        )
    )
    if camp is not None:
        return camp
    camp = Campaign(
        name=name,
        goal="Outreach to orgs surfaced by the intent engine — review every draft before approving.",
        tone="warm, concise, consultative",
        sender_name=settings.OWNER_NOTIFY_NAME or "Your name",
        sender_email=settings.OWNER_NOTIFY_EMAIL or "you@example.com",
        research_mode=ResearchMode.TEMPLATE,   # zero-cost render; no AI, no research
        template_subject=_DRAFT_SUBJECT,
        template_body=_DRAFT_BODY,
        schedule_time_start=time(9, 0),
        schedule_time_end=time(17, 0),
        # PREVIEWING = composed + awaiting the human "Review & Launch" approval
        # (the existing gate).  The pipeline only sends once a human approves
        # it into RUNNING.
        status=CampaignStatus.PREVIEWING,
    )
    session.add(camp)
    await session.flush()
    return camp


async def _existing_open_draft(session: AsyncSession, org_id) -> Lead | None:
    """An un-sent intent draft already staged for this org (idempotency)."""
    q = (
        select(Lead)
        .where(
            Lead.research_data["intent_org_id"].astext == str(org_id),
            Lead.send_status.in_([SendStatus.PENDING, SendStatus.SCHEDULED]),
        )
        .limit(1)
    )
    return await session.scalar(q)


async def promote_org(
    session: AsyncSession, org: Org, score_row: OrgIntentScore,
    profile: IcpIntentProfile | None, *, now: datetime | None = None,
) -> dict:
    """Stage an approval-pending draft for a promotable org.  Returns a status
    dict; mutates nothing externally (no send, no convert, no RUNNING)."""
    now = now or datetime.now(timezone.utc)
    if not scoring.is_promotable(score_row, profile):
        return {"status": "not_eligible"}
    if score_row.top_signal_id is None:
        return {"status": "no_signal"}
    top = await session.get(Signal, score_row.top_signal_id)
    if top is None:
        return {"status": "no_signal"}
    if await _existing_open_draft(session, org.id) is not None:
        return {"status": "already_promoted"}

    camp = await _get_or_create_draft_campaign(session, profile)

    lead = Lead(
        campaign_id=camp.id,
        email=None,                      # contact resolution is the existing enrichment path
        company=org.name,
        raw_csv_row={
            "company": org.name,
            "intent_why_now": top.summary,
            "intent_evidence_url": top.evidence_url,
        },
        research_data={
            "from_intent_engine": True,
            "intent_org_id": str(org.id),
            "intent_signal_id": str(top.id),
            "intent_tier": score_row.tier,
            "intent_score": float(score_row.intent_score),
        },
        research_status=ResearchStatus.DONE,   # TEMPLATE mode → no research runs
        is_sample=True,                         # show on the Review & Launch page
    )
    session.add(lead)
    await session.flush()

    ctx = build_merge_context(lead)
    ctx["sender_name"] = camp.sender_name
    lead.composed_subject = render_template(camp.template_subject, ctx)
    lead.composed_body = render_template(camp.template_body, ctx)
    lead.compose_status = ComposeStatus.DONE
    lead.send_status = SendStatus.PENDING       # explicit: waits for human approval

    top.status = IntentSignalStatus.PROMOTED    # marked promoted only now

    await notifications.create_notification(
        session,
        kind=NotificationKind.PROSPECT_SIGNAL,
        title=f"Intent draft ready: {org.name}",
        body=(
            f"{_TIER_LABEL.get(score_row.tier, 'Tier ?')} · score {float(score_row.intent_score):.0f}\n"
            f"Why now: {top.summary}\n{top.evidence_url}\n\n"
            f'A draft is waiting in "{camp.name}" for your review + approval.'
        ),
        dedup_key=f"intent:promote:{top.id}",
        lead_id=lead.id,
    )
    logger.info("intent.promote: org %s via signal %s → draft lead %s", org.id, top.id, lead.id)
    return {
        "status": "promoted",
        "campaign_id": str(camp.id),
        "lead_id": str(lead.id),
        "signal_id": str(top.id),
    }


async def promote_eligible(
    session: AsyncSession, *, now: datetime | None = None, tenant_id=None,
) -> dict[str, int]:
    """Stage drafts for every promotable org.  Per-org commit.  Never sends."""
    now = now or datetime.now(timezone.utc)
    profile = await scoring.get_active_profile(session, tenant_id=tenant_id)
    rows = (await session.execute(select(OrgIntentScore))).scalars().all()
    counts = {"promoted": 0, "already_promoted": 0, "not_eligible": 0, "no_signal": 0}
    for row in rows:
        if not scoring.is_promotable(row, profile):
            counts["not_eligible"] += 1
            continue
        org = await session.get(Org, row.org_id)
        if org is None:
            continue
        res = await promote_org(session, org, row, profile, now=now)
        counts[res["status"]] = counts.get(res["status"], 0) + 1
        await session.commit()
        # Post-commit (so the lead row exists): fire async contact resolution
        # for the freshly-staged draft.  Best-effort — promotion already
        # succeeded, and the resolver fills the recipient if it can.
        if res["status"] == "promoted" and settings.INTENT_PROMOTE_ENRICH_CONTACT:
            _enqueue_contact_enrichment(res["lead_id"])
    logger.info("intent.promote_eligible: %s", counts)
    return counts


def _enqueue_contact_enrichment(lead_id: str) -> None:
    """Fire the recipient-resolution task; never break promotion if the broker
    is unreachable."""
    try:
        from app.workers.celery_app import celery_app
        celery_app.send_task("intent.enrich_draft_contact", args=[str(lead_id)])
    except Exception as e:  # noqa: BLE001
        logger.warning("could not enqueue intent contact enrichment for %s: %s", lead_id, e)
