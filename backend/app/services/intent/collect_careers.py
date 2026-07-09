"""Careers-page dev-role collector → Tier-1 dev_role_posted (Phase 6, Track 2).

Targeted + precise: for orgs ALREADY surfaced as warm by another collector
(any non-dev-role signal), fetch the org's OWN careers page — robots.txt
respected — and LLM-extract any open Development Director / Grant Writer /
Foundation Relations role.  A hit upgrades a Tier-2 org to Tier-1.

Bounded cost: only the high-interest subset is fetched (not the whole monitored
set), capped per run, with a politeness sleep.  Never invents facts — the LLM
extracts ONLY a title actually present on the page; the summary quotes it and
``evidence_url`` is the careers page.  Compliant: own-site pages only, robots
checked, never LinkedIn/Idealist.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import IntentSignalStatus, IntentSignalType, IntentSignalSource, Org, Signal
from app.services._anthropic import extract_text, get_client, parse_json_object
from app.services.tenant_keys import ambient_api_key

logger = logging.getLogger(__name__)

_DEV_ROLE_BASE_SCORE = 80.0
_TAG_RE = re.compile(r"<[^>]+>")
_CAREERS_PATHS = ("/careers", "/jobs", "/about/careers", "/join-us",
                  "/work-with-us", "/employment", "/get-involved/careers", "")
_UA = "Mozilla/5.0 (outboundos intent dev-role check)"
_FETCH_TIMEOUT = 8.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _base_url(website: str | None) -> str | None:
    if not website:
        return None
    w = website.strip()
    if not w.startswith(("http://", "https://")):
        w = "https://" + w
    parsed = urlparse(w)
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else None


async def _robots_allows(client: httpx.AsyncClient, base: str, path: str) -> bool:
    """Honor robots.txt for our path; allow on any fetch/parse failure (a
    missing/broken robots.txt is conventionally permissive)."""
    try:
        resp = await client.get(base + "/robots.txt")
        if resp.status_code >= 400 or not resp.text.strip():
            return True
        rp = RobotFileParser()
        rp.parse(resp.text.splitlines())
        return rp.can_fetch("*", base + path)
    except Exception:  # noqa: BLE001 — robots unreachable → permissive
        return True


async def _fetch_careers_text(base: str) -> tuple[str, str] | None:
    """Return (careers_url, stripped_text) for the first reachable careers
    page, or None.  robots-respecting, HTML only."""
    async with httpx.AsyncClient(
        timeout=_FETCH_TIMEOUT, follow_redirects=True, headers={"User-Agent": _UA},
    ) as client:
        for path in _CAREERS_PATHS:
            url = base + path
            if not await _robots_allows(client, base, path):
                continue
            try:
                resp = await client.get(url)
                if resp.status_code >= 400:
                    continue
                ctype = resp.headers.get("content-type", "").lower()
                if "html" not in ctype and "text" not in ctype:
                    continue
                text = _TAG_RE.sub(" ", resp.text)
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    return url, text
            except Exception:  # noqa: BLE001 — one bad page must not abort
                continue
    return None


async def extract_dev_role(page_text: str, org_name: str) -> dict | None:
    """One Haiku call — extract an OPEN dev/grant role title actually present
    on the page.  Returns {"title": ...} or None.  Never invents a role."""
    if not page_text.strip():
        return None
    prompt = (
        f"Below is text from {org_name}'s careers/jobs page.  If it lists an "
        "OPEN position related to fundraising or grants — e.g. Development "
        "Director, Director of Development, Grant Writer, Grants Manager, "
        "Foundation Relations, Director of Philanthropy — return its exact "
        "posted title.  Only report a title that actually appears as an open "
        "role on the page; do NOT invent or infer one.\n\n"
        f"PAGE TEXT:\n{page_text[:6000]}\n\n"
        'Respond ONLY with JSON: {"found": true|false, "title": "...|null"}.'
    )
    try:
        message = await get_client(await ambient_api_key("anthropic")).messages.create(
            model=settings.ANTHROPIC_AGENT_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        parsed = parse_json_object(extract_text(message))
    except Exception as e:  # noqa: BLE001 — best-effort
        logger.warning("careers extract failed for %s: %s", org_name, e)
        return None
    if not parsed or not parsed.get("found"):
        return None
    title = (parsed.get("title") or "").strip()
    return {"title": title} if title else None


async def collect_for_org(session: AsyncSession, org: Org, now: datetime) -> str:
    """Check one org's careers page; emit a dev_role_posted signal on a hit.
    Returns 'new' | 'deduped' | 'no_site' | 'no_role'."""
    base = _base_url(org.website)
    if base is None:
        return "no_site"
    fetched = await _fetch_careers_text(base)
    if fetched is None:
        return "no_role"
    careers_url, text = fetched
    role = await extract_dev_role(text, org.name)
    if role is None:
        return "no_role"

    title = role["title"]
    key_title = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    summary = f'{org.name} posted a "{title}" role — actively investing in grant-seeking'
    stmt = (
        pg_insert(Signal.__table__)
        .values(
            org_id=org.id, tenant_id=org.tenant_id,
            signal_type=IntentSignalType.DEV_ROLE_POSTED.value,
            source=IntentSignalSource.JOBS.value,
            score=Decimal(str(_DEV_ROLE_BASE_SCORE)),
            event_date=now, evidence_url=careers_url, summary=summary[:500],
            raw_payload={"title": title, "careers_url": careers_url, "via": "careers_page"},
            dedupe_key=f"careers:devrole:{org.id}:{key_title}",
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "dedupe_key"])
    )
    result = await session.execute(stmt)
    return "new" if result.rowcount else "deduped"


async def _warm_org_ids(session: AsyncSession) -> list:
    """Orgs already surfaced as warm — they have ≥1 live signal that ISN'T a
    dev-role (so another collector flagged them).  These are the high-interest
    subset worth the per-org careers fetch."""
    rows = await session.execute(
        select(Signal.org_id).where(
            Signal.signal_type != IntentSignalType.DEV_ROLE_POSTED,
            Signal.status.in_([IntentSignalStatus.NEW, IntentSignalStatus.SCORED,
                               IntentSignalStatus.PROMOTED]),
        ).distinct()
    )
    return [r for (r,) in rows.all()]


async def collect_careers_dev_roles(
    session: AsyncSession, *, now: datetime | None = None, max_orgs: int | None = None,
) -> dict[str, int]:
    """Run the careers-page check over the warm-org subset (bounded)."""
    now = now or _now()
    max_orgs = max_orgs if max_orgs is not None else settings.INTENT_CAREERS_MAX_ORGS_PER_RUN
    counts = {"checked": 0, "new": 0, "deduped": 0, "no_site": 0, "no_role": 0}
    org_ids = (await _warm_org_ids(session))[:max_orgs]
    for oid in org_ids:
        org = await session.get(Org, oid)
        if org is None:
            continue
        counts["checked"] += 1
        outcome = await collect_for_org(session, org, now)
        counts[outcome] = counts.get(outcome, 0) + 1
        await session.commit()
        if settings.INTENT_COLLECTOR_POLITENESS_SECONDS:
            await asyncio.sleep(settings.INTENT_COLLECTOR_POLITENESS_SECONDS)
    logger.info("collect_careers_dev_roles: %s", counts)
    return counts
