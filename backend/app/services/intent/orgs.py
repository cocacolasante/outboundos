"""Org ingestion + dedup for the intent engine.

Orgs are the persistent intent anchor.  ``upsert_org`` find-or-creates by
normalized EIN (the partial-unique ``(tenant_id, ein)`` index dedups), and
``backfill_orgs_from_existing`` seeds the monitored set from EIN-bearing data
the v1 discovery already produced (funding-discovered leads + prospect_signals)
so v2 collectors have real orgs to run against on day one.
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Lead, Org, OrgSizeBand, ProspectSignal
from app.services.funding_sources import propublica

logger = logging.getLogger(__name__)

_EIN_RE = re.compile(r"\D")


def normalize_ein(ein: str | None) -> str | None:
    """9-digit EIN, no dash — the stored/dedup form."""
    digits = _EIN_RE.sub("", ein or "")
    return digits or None


def size_band_for_revenue(revenue: Decimal | float | None) -> OrgSizeBand | None:
    """990 annual-revenue → size band (drives the org-fit multiplier)."""
    if revenue is None:
        return None
    r = float(revenue)
    if r < 250_000:
        return OrgSizeBand.MICRO
    if r < 1_000_000:
        return OrgSizeBand.SMALL
    if r < 10_000_000:
        return OrgSizeBand.MID
    if r < 50_000_000:
        return OrgSizeBand.LARGE
    return OrgSizeBand.MAJOR


async def upsert_org(
    session: AsyncSession,
    *,
    ein: str | None,
    name: str | None = None,
    ntee_code: str | None = None,
    state: str | None = None,
    website: str | None = None,
    domain: str | None = None,
    annual_revenue: Decimal | float | None = None,
    source_lead_id: Any = None,
    tenant_id: Any = None,
) -> Org | None:
    """Find-or-create an org by normalized EIN (within a tenant).  Backfills
    any newly-known field on an existing row.  Returns None for a blank EIN
    (EIN is the dedup anchor; orgs without one aren't ingested here).  Caller
    commits.
    """
    norm = normalize_ein(ein)
    if not norm:
        return None

    q = select(Org).where(Org.ein == norm)
    q = q.where(Org.tenant_id.is_(None)) if tenant_id is None else q.where(Org.tenant_id == tenant_id)
    org = await session.scalar(q)

    rev = Decimal(str(annual_revenue)) if annual_revenue is not None else None
    band = size_band_for_revenue(rev)

    if org is None:
        org = Org(
            tenant_id=tenant_id, name=(name or "Unknown org").strip(), ein=norm,
            ntee_code=ntee_code, state=state, website=website, domain=domain,
            annual_revenue=rev, size_band=band, source_lead_id=source_lead_id,
        )
        session.add(org)
        await session.flush()
        return org

    # Backfill only blanks (don't clobber better existing data); revenue/band
    # always refresh to the latest known.
    if name and (not org.name or org.name == "Unknown org"):
        org.name = name.strip()
    org.ntee_code = org.ntee_code or ntee_code
    org.state = org.state or state
    org.website = org.website or website
    org.domain = org.domain or domain
    if rev is not None:
        org.annual_revenue = rev
        org.size_band = band
    if source_lead_id and org.source_lead_id is None:
        org.source_lead_id = source_lead_id
    return org


async def seed_orgs_from_propublica_search(
    session: AsyncSession,
    query: str,
    *,
    limit: int = 50,
    tenant_id: Any = None,
) -> dict[str, int]:
    """Seed the monitored-org set from ProPublica's public search API (real
    EINs + names — public facts, never fabricated).  Find-or-creates each org
    by EIN; the per-org 990 financials are fetched later by the collector.
    Idempotent (upsert dedups by EIN).  Caller-independent — commits itself.
    """
    found = await propublica.search_orgs(query)
    created = 0
    for o in found[:limit]:
        norm = normalize_ein(o.get("ein"))
        if not norm:
            continue
        before = await session.scalar(select(Org.id).where(Org.ein == norm))
        await upsert_org(
            session, ein=o["ein"], name=o.get("name"),
            ntee_code=o.get("ntee_code"), state=o.get("state"),
            tenant_id=tenant_id,
        )
        if before is None:
            created += 1
    await session.commit()
    logger.info(
        "seed_orgs_from_propublica_search(%r): %d new of %d found",
        query, created, len(found),
    )
    return {"created": created, "found": len(found)}


async def backfill_orgs_from_existing(session: AsyncSession) -> dict[str, int]:
    """Seed ``orgs`` from EIN-bearing v1 data: funding-discovered leads
    (``research_data.ein``) + prospect_signals (``detail.ein``).  Idempotent
    (upsert dedups by EIN)."""
    created = 0
    seen: set[str] = set()

    leads = (await session.execute(
        select(Lead).where(Lead.research_data.isnot(None))
    )).scalars().all()
    for lead in leads:
        rd = lead.research_data or {}
        ein = rd.get("ein")
        norm = normalize_ein(ein)
        if not norm or norm in seen:
            continue
        before = await session.scalar(select(Org.id).where(Org.ein == norm))
        await upsert_org(
            session, ein=ein, name=lead.company or rd.get("org_name"),
            ntee_code=rd.get("ntee") or rd.get("ntee_code"),
            state=rd.get("state"), website=lead.company_website,
            source_lead_id=lead.id,
        )
        seen.add(norm)
        if before is None:
            created += 1

    sigs = (await session.execute(
        select(ProspectSignal).where(ProspectSignal.detail.isnot(None))
    )).scalars().all()
    for sig in sigs:
        d = sig.detail or {}
        ein = d.get("ein")
        norm = normalize_ein(ein)
        if not norm or norm in seen:
            continue
        before = await session.scalar(select(Org.id).where(Org.ein == norm))
        await upsert_org(
            session, ein=ein, name=d.get("org_name"),
            ntee_code=d.get("ntee_code") or d.get("ntee"),
            state=d.get("state"), website=d.get("website"),
        )
        seen.add(norm)
        if before is None:
            created += 1

    await session.commit()
    logger.info("backfill_orgs_from_existing: %d new orgs (%d EINs seen)", created, len(seen))
    return {"created": created, "eins_seen": len(seen)}
