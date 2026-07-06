"""Signal detection: job-change / funding / hiring checks (Feature C).

Detection is a DIFF against the watch's ``last_seen`` baseline — no
change, no signal — and every emitted signal carries a ``dedup_key``
the worker enforces as UNIQUE, so a re-poll can never re-emit.

Sources, cheapest-first:
  job_change — Apollo ``people/match`` (already wrapped by
               ``apollo.enrich_lead_apollo``) returns the current
               title; compare to last-known.
  funding    — Apollo ``company_funding_stage`` diff, with a capped
               web-search lookup as the fallback when Apollo is dark.
  hiring     — web-search lookup for open roles; emitted when the
               detected role-set changes.

The LLM (Haiku, cost-wrapped) is used ONLY to normalise web-search
results into structured fields — never to decide whether to act.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Lead, Opportunity, SignalWatch, SignalWatchType
from app.services import apollo
from app.services._anthropic import extract_text, get_client, parse_json_object
from app.services.tenant_keys import ambient_api_key
from app.services._anthropic_cost import message_cost_usd

logger = logging.getLogger(__name__)

# Each web lookup ingests result pages as input tokens + per-search fee;
# 2 is enough for a yes/no growth-signal check.
WEB_SEARCH_MAX_USES = 2


@dataclass
class DetectedSignal:
    signal_type: str
    summary: str
    dedup_key: str
    detail: dict[str, Any] = field(default_factory=dict)


async def _target_identity(
    session: AsyncSession, watch: SignalWatch,
) -> dict[str, Any]:
    """Resolve the watch's person/company fields from its linked lead /
    opportunity, falling back to the free-text target fields."""
    first = last = ""
    company = watch.company or ""
    email = watch.email or ""
    known_title = (watch.last_seen or {}).get("job_title")
    if watch.lead_id is not None:
        lead = await session.get(Lead, watch.lead_id)
        if lead is not None:
            first, last = lead.first_name or "", lead.last_name or ""
            company = company or (lead.company or "")
            email = email or lead.email
            known_title = known_title or lead.job_title
    elif watch.opportunity_id is not None:
        opp = await session.get(Opportunity, watch.opportunity_id)
        if opp is not None:
            first, last = opp.first_name or "", opp.last_name or ""
            company = company or (opp.company or "")
            email = email or (opp.email or "")
            known_title = known_title or opp.job_title
    if not (first or last) and watch.person_name:
        parts = watch.person_name.split(" ", 1)
        first = parts[0]
        last = parts[1] if len(parts) > 1 else ""
    return {
        "first_name": first, "last_name": last,
        "company": company, "email": email,
        "known_title": known_title,
        "known_funding": (watch.last_seen or {}).get("funding_stage"),
        "known_roles": (watch.last_seen or {}).get("hiring_roles") or [],
    }


async def _web_lookup(query: str, extraction_prompt: str) -> dict[str, Any] | None:
    """One capped web-search call (Haiku) that returns strict JSON, or
    None on any failure.  Cost is attached under ``_cost_usd``."""
    model = settings.ANTHROPIC_AGENT_MODEL
    try:
        message = await get_client(await ambient_api_key("anthropic")).messages.create(
            model=model,
            max_tokens=500,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": WEB_SEARCH_MAX_USES,
            }],
            messages=[{
                "role": "user",
                "content": f"Search the web for: {query}\n\n{extraction_prompt}",
            }],
        )
    except Exception as exc:  # noqa: BLE001 — detection is best-effort
        logger.warning("signal web lookup failed (%s): %s", query, exc)
        return None
    data = parse_json_object(extract_text(message))
    if data is None:
        return None
    data["_cost_usd"] = round(message_cost_usd(message, model), 6)
    return data


def _norm_title(title: str | None) -> str:
    return (title or "").strip().lower()


async def _detect_job_change(
    session: AsyncSession, watch: SignalWatch, identity: dict[str, Any],
) -> tuple[list[DetectedSignal], dict[str, Any]]:
    """Diff the person's current title (Apollo) against the baseline."""
    seen_update: dict[str, Any] = {}
    if not identity["email"]:
        return [], seen_update  # Apollo match needs an email
    enriched = await apollo.enrich_lead_apollo(
        identity["email"], identity["first_name"],
        identity["last_name"], identity["company"],
    )
    new_title = enriched.get("job_title")
    if not new_title:
        return [], seen_update
    seen_update["job_title"] = new_title
    old_title = identity["known_title"]
    if old_title is None or _norm_title(old_title) == _norm_title(new_title):
        # First sighting just seeds the baseline; same title is a no-op.
        return [], seen_update
    name = f"{identity['first_name']} {identity['last_name']}".strip() or "Tracked contact"
    return [DetectedSignal(
        signal_type="job_change",
        summary=f"{name} changed roles: {old_title} → {new_title}",
        dedup_key=f"job_change:{watch.id}:{_norm_title(new_title)}",
        detail={
            "old_title": old_title,
            "new_title": new_title,
            "company": enriched.get("company_industry") and identity["company"] or identity["company"],
            "seniority": enriched.get("seniority"),
        },
    )], seen_update


async def _detect_funding(
    session: AsyncSession, watch: SignalWatch, identity: dict[str, Any],
) -> tuple[list[DetectedSignal], dict[str, Any]]:
    """Apollo funding-stage diff, web lookup as the fallback source."""
    seen_update: dict[str, Any] = {}
    company = identity["company"]
    if not company:
        return [], seen_update

    new_stage = None
    detail: dict[str, Any] = {"company": company}
    if identity["email"]:
        enriched = await apollo.enrich_lead_apollo(
            identity["email"], identity["first_name"],
            identity["last_name"], company,
        )
        new_stage = enriched.get("company_funding_stage")
    if not new_stage:
        web = await _web_lookup(
            f'"{company}" funding round raised 2026',
            "Did this company recently announce a new funding round?  "
            "Respond ONLY with JSON: "
            '{"funding_stage": "seed|series_a|series_b|series_c|growth|none", '
            '"amount": "...or empty", "source_url": "...or empty"}.  '
            'Use "none" unless a SPECIFIC recent round is reported.',
        )
        if web and (web.get("funding_stage") or "none") != "none":
            new_stage = web["funding_stage"]
            detail["amount"] = web.get("amount") or None
            detail["source_url"] = web.get("source_url") or None

    if not new_stage:
        return [], seen_update
    seen_update["funding_stage"] = new_stage
    old_stage = identity["known_funding"]
    if old_stage is None or _norm_title(old_stage) == _norm_title(new_stage):
        return [], seen_update
    detail.update({"old_stage": old_stage, "new_stage": new_stage})
    return [DetectedSignal(
        signal_type="funding",
        summary=f"{company} moved to a new funding stage: {new_stage}",
        dedup_key=f"funding:{watch.id}:{_norm_title(new_stage)}",
        detail=detail,
    )], seen_update


async def _detect_hiring(
    session: AsyncSession, watch: SignalWatch, identity: dict[str, Any],
) -> tuple[list[DetectedSignal], dict[str, Any]]:
    """Web lookup for open roles signalling growth/budget."""
    seen_update: dict[str, Any] = {}
    company = identity["company"]
    if not company:
        return [], seen_update
    web = await _web_lookup(
        f'"{company}" careers hiring open positions',
        "Is this company actively hiring?  List up to 5 open role titles "
        "that signal growth or budget (engineering, sales, ops leadership).  "
        'Respond ONLY with JSON: {"hiring": true/false, "roles": ["..."], '
        '"source_url": "...or empty"}.',
    )
    if not web or not web.get("hiring"):
        return [], seen_update
    roles = sorted({str(r).strip() for r in (web.get("roles") or []) if str(r).strip()})[:5]
    if not roles:
        return [], seen_update
    seen_update["hiring_roles"] = roles
    old_roles = sorted(identity["known_roles"])
    if old_roles == roles:
        return [], seen_update  # same posting set — no new signal
    roles_hash = hashlib.sha256("|".join(roles).encode()).hexdigest()[:12]
    return [DetectedSignal(
        signal_type="hiring",
        summary=f"{company} is hiring: {', '.join(roles[:3])}"
                + ("…" if len(roles) > 3 else ""),
        dedup_key=f"hiring:{watch.id}:{roles_hash}",
        detail={
            "company": company,
            "roles": roles,
            "source_url": web.get("source_url") or None,
        },
    )], seen_update


_DETECTORS = {
    SignalWatchType.JOB_CHANGE: (_detect_job_change,),
    SignalWatchType.FUNDING: (_detect_funding,),
    SignalWatchType.HIRING: (_detect_hiring,),
    # A custom watch runs every detector — "tell me anything about them".
    SignalWatchType.CUSTOM: (_detect_job_change, _detect_funding, _detect_hiring),
}


async def detect_for_watch(
    session: AsyncSession, watch: SignalWatch,
) -> tuple[list[DetectedSignal], dict[str, Any]]:
    """Run the watch's detectors.  Returns (signals, last_seen_update) —
    the caller merges the update into ``watch.last_seen`` and persists
    the signals (with dedup)."""
    identity = await _target_identity(session, watch)
    signals: list[DetectedSignal] = []
    seen_update: dict[str, Any] = {}
    for detector in _DETECTORS.get(watch.watch_type, ()):
        try:
            found, update = await detector(session, watch, identity)
            signals.extend(found)
            seen_update.update(update)
        except Exception:  # noqa: BLE001 — one detector must not kill the run
            logger.exception(
                "detector %s failed for watch %s", detector.__name__, watch.id,
            )
    return signals, seen_update
