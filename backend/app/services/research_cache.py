"""Cross-campaign research cache helpers.

Cache is keyed by lowercased email.  ``lookup`` returns the cached
research_data when it's still inside ``RESEARCH_CACHE_TTL_DAYS``;
``upsert`` writes/refreshes the row.  All callers use lowercased,
stripped email — same canonicalisation as the rest of the lead pipeline.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import ResearchCache


def _canon(email: str) -> str:
    return (email or "").strip().lower()


# --- Company-level dedup ----------------------------------------------------
# Several leads in one list often share a company.  The company half of the
# research (what the company does, its industry, recent company news) is
# identical for all of them, so we cache it by domain under a namespaced key
# in the SAME table (domains can't collide with real emails — they're prefixed
# and contain no '@').  The 2nd+ lead at a domain then skips the company half
# of the search; the person half still runs per-lead.
_COMPANY_PREFIX = "company::"
_COMPANY_FIELDS = (
    "company_description",
    "company_news",
    "recent_updates",
    "industry",
    "size_hint",
)


def company_fields(research_data: dict[str, Any]) -> dict[str, Any]:
    """The subset of a research payload shared by every lead at the same
    company — i.e. worth caching by domain.  Drops empty values so a
    no-signal lookup never overwrites a good cached one."""
    return {k: research_data[k] for k in _COMPANY_FIELDS if research_data.get(k)}


async def company_lookup(session: AsyncSession, domain: str) -> dict[str, Any] | None:
    """Cached company-level research for ``domain`` if still fresh, else None."""
    d = (domain or "").strip().lower()
    if not d:
        return None
    return await lookup(session, _COMPANY_PREFIX + d)


async def company_upsert(
    session: AsyncSession, domain: str, company_data: dict[str, Any]
) -> None:
    """Write/refresh the company-level cache for ``domain`` (no-op if blank)."""
    d = (domain or "").strip().lower()
    if not d or not company_data:
        return
    await upsert(session, _COMPANY_PREFIX + d, company_data)


async def lookup(session: AsyncSession, email: str) -> dict[str, Any] | None:
    """Return the cached ``research_data`` for ``email`` if it's still fresh
    (within ``RESEARCH_CACHE_TTL_DAYS``), else None.

    Phase 2 note: the cache is per-tenant at the SCHEMA level (surrogate
    PK + UNIQUE(tenant_id, email)), but this read stays tenant-agnostic
    (freshest row wins) because the research workers that call it don't
    carry tenant context yet — the worker-context phase threads the
    tenant filter through here.
    """
    e = _canon(email)
    if not e:
        return None
    row = (
        await session.execute(
            select(ResearchCache)
            .where(ResearchCache.email == e)
            .order_by(ResearchCache.refreshed_at.desc())
            .limit(1)
        )
    ).scalars().first()
    if row is None:
        return None
    age = datetime.now(timezone.utc) - row.refreshed_at
    if age > timedelta(days=settings.RESEARCH_CACHE_TTL_DAYS):
        return None
    return dict(row.research_data or {})


async def upsert(session: AsyncSession, email: str, research_data: dict[str, Any]) -> None:
    """Write or refresh the cache row for ``email``.  No-op if email is blank
    or research_data is empty (no point caching a no-signal lookup)."""
    e = _canon(email)
    if not e or not research_data:
        return
    stmt = pg_insert(ResearchCache).values(
        email=e,
        research_data=research_data,
        refreshed_at=datetime.now(timezone.utc),
    ).on_conflict_do_update(
        # Arbiter = the tenant-scoped unique (NULLS NOT DISTINCT, so the
        # tenant-less worker rows keep upserting in place).  tenant_id
        # itself comes from the TenantMixin column default.
        index_elements=["tenant_id", "email"],
        set_={
            "research_data": research_data,
            "refreshed_at": datetime.now(timezone.utc),
        },
    )
    await session.execute(stmt)
