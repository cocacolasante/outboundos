"""On-demand contact enrichment for a notification-only signal.

The funding feeds already try ``enrichment.resolve_contact`` once at
discovery time; orgs with no resolvable contact land in the queue as
notification-only (no ``lead_id``).  This is the user-triggered "Find
contact" retry: rebuild the org from the signal and run the SAME
low-cost resolver (Hunter-first; at most ONE Haiku web-lookup, and only
when no domain is already known).  On a hit we stage a campaign-less
Lead and link it to the signal, so the existing Draft / Send /
Add-to-campaign flows light up.

Stays inside the autonomy boundary: this never sets ``campaign_id`` or
enrolls a sequence — it only resolves a contact and stages a CRM lead.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Lead, ProspectSignal, canonical_email
from app.services import hunter
from app.services.funding_sources import enrichment
from app.services.funding_sources.base import DiscoveredOrg
# Reuse the existing capped Haiku web-search helper (cost-wrapped).
from app.services.signal_detection import _web_lookup

logger = logging.getLogger(__name__)

# IRS summary shape: "New 501(c)(3): {name} ({STATE}) — IRS ruling …"
_IRS_SUMMARY_RE = re.compile(r"^New 501\(c\)\(3\):\s*(?P<name>.+?)\s*\(", re.IGNORECASE)
# USAspending summary shape: "{name} won a federal grant …"
_GRANT_SUMMARY_RE = re.compile(r"^(?P<name>.+?)\s+won a federal grant", re.IGNORECASE)


@dataclass
class EnrichResult:
    found: bool = False
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    generic: bool = False
    has_email: bool = False
    linkedin_url: str | None = None
    lead_id: Any = None
    lead_created: bool = False
    already_had_contact: bool = False


def _org_name_from_signal(signal: ProspectSignal) -> str | None:
    """Best org name: stored ``detail.org_name`` else parsed summary."""
    detail = signal.detail or {}
    name = (detail.get("org_name") or detail.get("company") or "").strip()
    if name:
        return name
    summary = signal.summary or ""
    for rx in (_IRS_SUMMARY_RE, _GRANT_SUMMARY_RE):
        m = rx.match(summary)
        if m:
            return m.group("name").strip()
    return None


def _org_from_signal(signal: ProspectSignal) -> DiscoveredOrg | None:
    detail = signal.detail or {}
    org_name = _org_name_from_signal(signal)
    if not org_name:
        return None
    return DiscoveredOrg(
        signal_type=signal.signal_type,
        summary=signal.summary,
        dedup_key=signal.dedup_key,
        org_name=org_name,
        state=detail.get("state") or detail.get("recipient_state"),
        ein=detail.get("ein"),
        ntee_code=detail.get("ntee_code") or detail.get("ntee"),
        website=detail.get("website"),
        detail=detail,
    )


async def _find_linkedin_decision_maker(org: DiscoveredOrg) -> dict[str, Any]:
    """ONE capped Haiku web-search for the org's top decision-maker and
    their public LinkedIn profile.  Also returns the org domain so the
    downstream Hunter / role search can skip a second lookup.  Returns a
    dict with None-able ``first_name`` / ``last_name`` / ``title`` /
    ``linkedin_url`` / ``domain`` (empty dict-ish on failure)."""
    roles = ", ".join(enrichment._ROLE_PRIORITY)
    data = await _web_lookup(
        f'"{org.org_name}" {org.state or ""} ({roles}) LinkedIn'.strip(),
        "Identify the single most senior leadership / fundraising "
        "decision-maker at this nonprofit and their PUBLIC LinkedIn "
        "profile URL.  Respond ONLY with JSON: "
        '{"first_name": "", "last_name": "", "title": "", '
        '"linkedin_url": "https://www.linkedin.com/in/... or empty string", '
        '"domain": "the org website domain or empty string"}.',
    ) or {}
    url = (data.get("linkedin_url") or "").strip()
    if "linkedin.com/in/" not in url.lower():
        url = ""  # drop hallucinated / non-profile URLs
    return {
        "first_name": (data.get("first_name") or "").strip() or None,
        "last_name": (data.get("last_name") or "").strip() or None,
        "title": (data.get("title") or "").strip() or None,
        "linkedin_url": url or None,
        "domain": enrichment._domain_from_website(data.get("domain")),
    }


async def enrich_signal_contact(
    db: AsyncSession, signal: ProspectSignal,
) -> EnrichResult:
    """Resolve a contact for ``signal`` and stage+link a campaign-less Lead.

    Searches LinkedIn (one Haiku web-search) for the org's decision-maker
    — both to find an email via Hunter's name-aware Email Finder and to
    surface the LinkedIn profile — then falls back to the role-priority
    domain search.  Idempotent on a signal that already has a contactable
    lead (returns it untouched, no API spend).  Commits on a hit.
    """
    # Already linked to a lead (email OR LinkedIn-only) → no spend.
    if signal.lead_id is not None:
        lead = await db.get(Lead, signal.lead_id)
        if lead is not None and (lead.email or lead.linkedin_url):
            return EnrichResult(
                found=True, already_had_contact=True,
                email=lead.email, has_email=bool(lead.email),
                first_name=lead.first_name, last_name=lead.last_name,
                title=lead.job_title, linkedin_url=lead.linkedin_url,
                lead_id=lead.id,
            )

    org = _org_from_signal(signal)
    if org is None:
        return EnrichResult(found=False)

    # LinkedIn-guided lookup: a named decision-maker + profile + domain.
    person = await _find_linkedin_decision_maker(org)
    linkedin_url = person.get("linkedin_url")
    full_name = " ".join(
        p for p in (person.get("first_name"), person.get("last_name")) if p
    ).strip()
    # Seed the org website from the LinkedIn-found domain so the
    # role-priority fallback below doesn't spend a second web-search.
    if person.get("domain") and not org.website:
        org.website = f"https://{person['domain']}"

    contact: dict[str, Any] | None = None
    domain = enrichment._domain_from_website(org.website)
    # 1) Name-aware Hunter Email Finder (most accurate) when we have a name.
    if domain and full_name:
        found = await hunter.find_email_hunter(domain, full_name=full_name)
        if found and found.get("email"):
            verdict = await hunter.verify_email_hunter(found["email"])
            if verdict.get("deliverable"):
                contact = found
    # 2) Fall back to the full role-priority pipeline (domain discovery →
    #    website scrape → Hunter domain-search → ProPublica).  It returns a
    #    structured ContactResult; adapt it to the dict shape used below.
    if contact is None:
        cr = await enrichment.resolve_contact(org)
        if cr.status == "resolved" and cr.email:
            local = cr.email.split("@", 1)[0].lower()
            contact = {
                "email": cr.email,
                "first_name": cr.first_name,
                "last_name": cr.last_name,
                "title": cr.title,
                "generic": local in {
                    "info", "contact", "hello", "admin", "office", "mail",
                    "general", "inquiries", "support", "help", "team",
                },
            }

    contact = contact or {}
    email = canonical_email(contact["email"]) if contact.get("email") else None
    # Prefer Hunter's identity fields, fall back to the LinkedIn lookup's.
    first_name = contact.get("first_name") or person.get("first_name")
    last_name = contact.get("last_name") or person.get("last_name")
    title = contact.get("title") or person.get("title")

    # Nothing actionable at all → notification-only, no lead.
    if not email and not linkedin_url:
        return EnrichResult(found=False)

    research_data = {
        "ein": org.ein,
        "ntee": org.ntee_code,
        "source": signal.source,
        **(org.detail or {}),
    }

    # Find-or-create a campaign-less lead.  Dedup by email when we have
    # one (mirrors the worker's discovery-staging branch), else by the
    # LinkedIn profile URL for an email-less (LinkedIn-only) lead.
    lead = None
    if email:
        lead = await db.scalar(select(Lead).where(Lead.email == email).limit(1))
    elif linkedin_url:
        lead = await db.scalar(
            select(Lead).where(Lead.linkedin_url == linkedin_url).limit(1)
        )

    lead_created = False
    if lead is None:
        lead = Lead(
            campaign_id=None,                      # NEVER a campaign
            email=email,
            first_name=first_name,
            last_name=last_name,
            company=org.org_name,
            company_website=org.website,
            job_title=title,
            linkedin_url=linkedin_url,
            research_data=research_data,
        )
        db.add(lead)
        await db.flush()
        lead_created = True
    elif linkedin_url and not lead.linkedin_url:
        # Backfill the profile onto a pre-existing lead.
        lead.linkedin_url = linkedin_url

    signal.lead_id = lead.id
    await db.commit()

    return EnrichResult(
        found=True,
        email=email,
        has_email=bool(email),
        first_name=first_name,
        last_name=last_name,
        title=title,
        generic=bool(contact.get("generic")),
        linkedin_url=linkedin_url,
        lead_id=lead.id,
        lead_created=lead_created,
    )
