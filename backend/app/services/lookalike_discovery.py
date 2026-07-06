"""Lookalike discovery + scoring (Feature D).

Primary source: Apollo's search endpoints (people first — they carry a
contact; orgs as a top-up).  Apollo *search* may be behind a paid plan
tier; on 403/empty we fall back to one capped Haiku web-search call
that extracts company/contact rows from public results.

Scoring is RULE-BASED against the ICP criteria (deterministic, free,
explainable) — the LLM is only ever the fallback *discovery* channel,
never the judge.  Dedup is forever: against existing leads, opps, and
prior candidates, anchored on the company domain (or LinkedIn URL).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Lead, LookalikeCandidate, Opportunity
from app.services import apollo
from app.services._anthropic import extract_text, get_client, parse_json_array
from app.services.tenant_keys import ambient_api_key
from app.services._anthropic_cost import message_cost_usd

logger = logging.getLogger(__name__)

WEB_SEARCH_MAX_USES = 3
DEFAULT_LIMIT = 25


@dataclass
class Candidate:
    company: str
    company_website: str | None = None
    contact_name: str | None = None
    job_title: str | None = None
    linkedin_url: str | None = None
    email: str | None = None
    fit_score: int = 0
    fit_reason: str = ""
    source: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


_WWW_RE = re.compile(r"^www\.", re.IGNORECASE)


def _domain_of(url_or_email: str | None) -> str | None:
    if not url_or_email:
        return None
    s = url_or_email.strip().lower()
    if "@" in s:
        return s.rsplit("@", 1)[1] or None
    s = re.sub(r"^https?://", "", s).split("/", 1)[0]
    s = _WWW_RE.sub("", s)
    return s or None


def candidate_dedup_key(c: Candidate) -> str:
    return (
        _domain_of(c.company_website)
        or _domain_of(c.email)
        or (c.linkedin_url or "").strip().lower()
        or f"company:{c.company.strip().lower()}"
    )


def _parse_band(band: str | None) -> tuple[int, int] | None:
    if not band:
        return None
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", str(band))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def score_candidate(c: Candidate, criteria: dict[str, Any]) -> tuple[int, str]:
    """Deterministic 0-100 fit score + one-line reason."""
    score = 0
    reasons: list[str] = []

    industries = [str(x).lower() for x in criteria.get("industries") or []]
    cand_industry = str(c.raw.get("industry") or "").lower()
    if industries and cand_industry:
        if any(i in cand_industry or cand_industry in i for i in industries):
            score += 30
            reasons.append(f"industry match ({c.raw.get('industry')})")

    band = _parse_band(criteria.get("employee_count_band"))
    count = c.raw.get("employee_count")
    if band and isinstance(count, (int, float)):
        lo, hi = band
        if lo <= count <= hi:
            score += 25
            reasons.append(f"{int(count)} employees in band {lo}-{hi}")

    titles = [str(x).lower() for x in criteria.get("title_patterns") or []]
    cand_title = (c.job_title or "").lower()
    if titles and cand_title:
        if any(t in cand_title or cand_title in t for t in titles):
            score += 25
            reasons.append(f"title match ({c.job_title})")

    stages = [str(x).lower() for x in criteria.get("funding_stages") or []]
    cand_stage = str(c.raw.get("funding_stage") or "").lower()
    if stages and cand_stage and cand_stage in stages:
        score += 10
        reasons.append(f"funding stage {cand_stage}")

    keywords = [str(x).lower() for x in criteria.get("keywords") or []]
    blob = f"{c.company} {cand_industry} {cand_title}".lower()
    if keywords and any(k in blob for k in keywords):
        score += 10
        reasons.append("keyword match")

    if not reasons:
        reasons.append("weak fit — no criteria matched")
    return min(score, 100), "; ".join(reasons)


async def _web_fallback(criteria: dict[str, Any], limit: int) -> list[Candidate]:
    """One capped Haiku web-search call extracting company/contact rows.
    Used only when Apollo search is unavailable."""
    industries = ", ".join(criteria.get("industries") or []) or "B2B companies"
    band = criteria.get("employee_count_band") or "small to mid-size"
    titles = ", ".join(criteria.get("title_patterns") or []) or "decision makers"
    geos = ", ".join(criteria.get("geographies") or [])
    query = f"{industries} companies {band} employees {geos}".strip()

    model = settings.ANTHROPIC_AGENT_MODEL
    try:
        message = await get_client(await ambient_api_key("anthropic")).messages.create(
            model=model,
            max_tokens=1000,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": WEB_SEARCH_MAX_USES,
            }],
            messages=[{
                "role": "user",
                "content": (
                    f"Search the web for: {query}\n\n"
                    f"Find up to {min(limit, 10)} REAL companies matching that "
                    f"profile (think: who employs a {titles}).  Respond ONLY "
                    'with a JSON array: [{"company": "...", '
                    '"company_website": "...", "industry": "...", '
                    '"employee_count": 0}] — omit fields you can\'t verify; '
                    "never invent companies."
                ),
            }],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("lookalike web fallback failed: %s", exc)
        return []
    rows = parse_json_array(extract_text(message)) or []
    cost = round(message_cost_usd(message, model), 6)
    out = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("company"):
            continue
        out.append(Candidate(
            company=str(r["company"])[:300],
            company_website=r.get("company_website"),
            source="web_research",
            raw={**r, "_cost_usd": cost},
        ))
    return out


async def _existing_dedup_keys(session: AsyncSession) -> set[str]:
    """Domains/URLs already in the pipeline — leads, opps, prior
    candidates — that discovery must never re-surface."""
    keys: set[str] = set()
    for email, website in (await session.execute(
        select(Lead.email, Lead.company_website)
    )).all():
        for v in (_domain_of(email), _domain_of(website)):
            if v:
                keys.add(v)
    for email, linkedin in (await session.execute(
        select(Opportunity.email, Opportunity.linkedin_url)
    )).all():
        d = _domain_of(email)
        if d:
            keys.add(d)
        if linkedin:
            keys.add(linkedin.strip().lower())
    for (dk,) in (await session.execute(
        select(LookalikeCandidate.dedup_key)
    )).all():
        keys.add(dk)
    return keys


async def find_lookalikes(
    session: AsyncSession,
    profile,
    limit: int = DEFAULT_LIMIT,
) -> list[Candidate]:
    """Discover, score, and dedup lookalike candidates for an ICP
    profile.  Returns NEW candidates only (not yet persisted)."""
    criteria = profile.criteria or {}
    band = _parse_band(criteria.get("employee_count_band"))
    employee_ranges = [f"{band[0]},{band[1]}"] if band else None

    raw: list[Candidate] = []
    # People search first — candidates with a contact are actionable.
    people = await apollo.search_people(
        titles=criteria.get("title_patterns") or None,
        industries=criteria.get("industries") or None,
        employee_ranges=employee_ranges,
        per_page=limit,
    )
    for p in people:
        raw.append(Candidate(
            company=p["company"],
            company_website=p.get("company_website"),
            contact_name=p.get("contact_name"),
            job_title=p.get("job_title"),
            linkedin_url=p.get("linkedin_url"),
            email=p.get("email"),
            source="apollo_people",
            raw=p,
        ))
    if not raw:
        orgs = await apollo.search_organizations(
            industries=criteria.get("industries") or None,
            employee_ranges=employee_ranges,
            per_page=limit,
        )
        for o in orgs:
            raw.append(Candidate(
                company=o["company"],
                company_website=o.get("company_website"),
                linkedin_url=o.get("linkedin_url"),
                source="apollo_orgs",
                raw=o,
            ))
    if not raw:
        # Apollo search unavailable (plan tier / no key) → web fallback.
        raw = await _web_fallback(criteria, limit)

    existing = await _existing_dedup_keys(session)
    out: list[Candidate] = []
    seen_this_run: set[str] = set()
    for c in raw:
        key = candidate_dedup_key(c)
        if key in existing or key in seen_this_run:
            continue
        seen_this_run.add(key)
        c.fit_score, c.fit_reason = score_candidate(c, criteria)
        out.append(c)
        if len(out) >= limit:
            break
    out.sort(key=lambda c: -c.fit_score)
    return out
