"""Resolve a recipient contact for a promoted intent draft (Phase 6 follow-up).

The promotion bridge stages a DRAFT lead with no recipient (``email=None``).
This fills it in: it reuses the existing cheapest-first contact resolver
(``funding_sources.enrichment.resolve_contact`` — domain discovery → website
scrape → Hunter domain-search → ProPublica → role-priority verified pick) and,
on a hit, sets the lead's email + name and re-renders the draft so the copy
opens with the real person.

Run async (a Celery task fired after promotion) so the slow/costly external
calls never block the promote request, and so it's bounded to freshly-drafted
leads.  Best-effort: a miss leaves the lead recipient-less (set it by hand or
via the existing "Find contact" flow) — promotion already succeeded.  Stays
within the autonomy boundary: it only fills a draft's recipient; it never sends.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import unquote

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Campaign, ComposeStatus, Lead, Org
from app.services.funding_sources.base import DiscoveredOrg
from app.services.funding_sources.enrichment import ContactResult, resolve_contact
from app.services.template_render import build_merge_context, render_template

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _clean_email(raw: str | None) -> str | None:
    """Sanitize a resolved email: URL-decode scrape artifacts (e.g. a leading
    ``%20``), strip, lowercase, and validate the shape.  None if not a valid
    address."""
    if not raw:
        return None
    e = unquote(raw).strip().lower()
    return e if _EMAIL_RE.match(e) else None


def _org_to_discovered(org: Org) -> DiscoveredOrg:
    return DiscoveredOrg(
        signal_type="intent", summary="", dedup_key=f"intent:{org.id}",
        org_name=org.name, state=org.state, ein=org.ein,
        ntee_code=org.ntee_code, website=org.website,
    )


async def resolve_org_contact(org: Org) -> ContactResult:
    """Thin wrapper: resolve a deliverable contact for an intent Org."""
    return await resolve_contact(_org_to_discovered(org))


async def enrich_draft_lead(
    session: AsyncSession, lead_id, *,
    force: bool = False, force_website: str | None = None,
) -> dict:
    """Resolve + attach a recipient to a promoted draft lead, then re-render
    the draft.  No-op if the lead is gone / already has an email (unless
    ``force``) / has no org.  ``force_website`` overrides the org's website
    first (manual "find contact" with a hint).  Caller-independent (commits)."""
    lead = await session.get(Lead, lead_id)
    if lead is None:
        return {"status": "not_found"}
    if lead.email and not force:
        return {"status": "already_has_email"}

    rd = lead.research_data or {}
    org_id = rd.get("intent_org_id")
    if not org_id:
        return {"status": "no_org"}
    org = await session.get(Org, org_id)
    if org is None:
        return {"status": "no_org"}

    if force_website and force_website.strip():
        org.website = force_website.strip()
        await session.commit()   # persist the hint even if resolution misses

    result = await resolve_org_contact(org)
    if result.status != "resolved" or not result.email:
        return {"status": result.status}
    clean = _clean_email(result.email)
    if not clean:
        logger.warning("intent.enrich: rejecting malformed email %r for lead %s", result.email, lead_id)
        return {"status": "invalid_email"}

    lead.email = clean
    if not lead.first_name and result.first_name:
        lead.first_name = result.first_name
    if not lead.last_name and result.last_name:
        lead.last_name = result.last_name

    # Re-render the draft so "Hi {{first_name|there}}" picks up the real name.
    campaign = await session.get(Campaign, lead.campaign_id) if lead.campaign_id else None
    if campaign is not None and campaign.template_body:
        ctx = build_merge_context(lead)
        ctx["sender_name"] = campaign.sender_name
        lead.composed_subject = render_template(campaign.template_subject, ctx)
        lead.composed_body = render_template(campaign.template_body, ctx)
        lead.compose_status = ComposeStatus.DONE

    await session.commit()
    logger.info("intent.enrich: lead %s → %s (%s)", lead_id, result.email, result.via)
    return {"status": "resolved", "email": lead.email, "via": result.via}
