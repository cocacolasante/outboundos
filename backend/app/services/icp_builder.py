"""ICP builder: derive the ideal-customer fingerprint from closed-won
deals (Feature D).

One Haiku call (cost-wrapped) summarises the won-deal set into
structured criteria.  Guarded by ``MIN_WON_DEALS`` — below that the
auto profile is marked ``insufficient_data`` and discovery skips it
(a "profile" of 1-2 wins is just noise).
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    IcpProfile,
    IcpProfileSource,
    IcpProfileStatus,
    Opportunity,
    OpportunityStage,
)
from app.services import apollo
from app.services._anthropic import extract_text, get_client, parse_json_object
from app.services.tenant_keys import ambient_api_key
from app.services._anthropic_cost import message_cost_usd

logger = logging.getLogger(__name__)

MIN_WON_DEALS = 3
AUTO_PROFILE_NAME = "Auto ICP (from closed-won deals)"

_CRITERIA_KEYS = (
    "industries", "employee_count_band", "title_patterns",
    "geographies", "funding_stages", "keywords",
)

_PROMPT = """You are deriving an Ideal Customer Profile from a company's \
CLOSED-WON deals.  Below is one block per won deal (company, contact title, \
firmographics where known).

WON DEALS:
{deals}

Extract the common fingerprint.  Be specific to THIS data — do not pad
with generic values.  ``employee_count_band`` is a single "min-max"
string (e.g. "20-200").  Empty lists are fine where no pattern exists.

Respond with ONLY this JSON object:
{{
  "industries": ["..."],
  "employee_count_band": "min-max or empty string",
  "title_patterns": ["..."],
  "geographies": ["..."],
  "funding_stages": ["..."],
  "keywords": ["..."]
}}"""


def _deal_block(opp: Opportunity, enrich: dict[str, Any]) -> str:
    bits = [f"Company: {opp.company or '(unknown)'}"]
    if opp.job_title:
        bits.append(f"Contact title: {opp.job_title}")
    if opp.amount is not None:
        bits.append(f"Deal size: ${opp.amount}")
    for label, key in (
        ("Industry", "company_industry"),
        ("Employees", "company_employee_count"),
        ("Funding stage", "company_funding_stage"),
        ("Seniority", "seniority"),
    ):
        if enrich.get(key):
            bits.append(f"{label}: {enrich[key]}")
    return "\n".join(bits)


async def _get_auto_profile(session: AsyncSession) -> IcpProfile | None:
    return await session.scalar(
        select(IcpProfile).where(
            IcpProfile.source == IcpProfileSource.AUTO_CLOSED_WON,
        )
    )


async def build_icp_from_won(session: AsyncSession) -> IcpProfile:
    """Regenerate the auto ICP profile from closed_won opportunities.
    Caller owns the transaction."""
    won = (await session.execute(
        select(Opportunity).where(
            Opportunity.stage == OpportunityStage.CLOSED_WON,
        ).order_by(Opportunity.closed_at.desc().nulls_last()).limit(25)
    )).scalars().all()

    profile = await _get_auto_profile(session)
    if len(won) < MIN_WON_DEALS:
        if profile is None:
            profile = IcpProfile(
                name=AUTO_PROFILE_NAME,
                source=IcpProfileSource.AUTO_CLOSED_WON,
                status=IcpProfileStatus.INSUFFICIENT_DATA,
                criteria={},
                won_deal_count=len(won),
            )
            session.add(profile)
        else:
            profile.status = IcpProfileStatus.INSUFFICIENT_DATA
            profile.won_deal_count = len(won)
        await session.flush()
        return profile

    # Enrich thin deals via Apollo (best-effort, needs an email).
    blocks = []
    for opp in won:
        enrich: dict[str, Any] = {}
        if opp.email and not opp.company:
            pass  # nothing to enrich against without a company either
        if opp.email:
            try:
                enrich = await apollo.enrich_lead_apollo(
                    opp.email, opp.first_name or "", opp.last_name or "",
                    opp.company or "",
                )
            except Exception:  # noqa: BLE001 — enrichment is optional
                enrich = {}
        blocks.append(_deal_block(opp, enrich))

    model = settings.ANTHROPIC_AGENT_MODEL  # extraction → Haiku
    try:
        message = await get_client(await ambient_api_key("anthropic")).messages.create(
            model=model,
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": _PROMPT.format(deals="\n---\n".join(blocks)),
            }],
        )
        data = parse_json_object(extract_text(message))
    except Exception as exc:  # noqa: BLE001
        logger.warning("ICP summarisation failed: %s", exc)
        data = None

    if data is None:
        # Keep the previous criteria if any; don't blank a good profile
        # because one refresh failed.
        if profile is not None and profile.criteria:
            profile.won_deal_count = len(won)
            await session.flush()
            return profile
        data = {}

    criteria: dict[str, Any] = {}
    for key in _CRITERIA_KEYS:
        val = data.get(key)
        if key == "employee_count_band":
            criteria[key] = str(val or "")[:50]
        else:
            criteria[key] = [str(x)[:100] for x in (val or []) if x][:6]
    if data:
        criteria["_meta"] = {
            "model": model,
            "cost_usd": round(message_cost_usd(message, model), 6),
        }

    if profile is None:
        profile = IcpProfile(
            name=AUTO_PROFILE_NAME,
            source=IcpProfileSource.AUTO_CLOSED_WON,
            status=IcpProfileStatus.READY,
            criteria=criteria,
            won_deal_count=len(won),
        )
        session.add(profile)
    else:
        profile.status = IcpProfileStatus.READY
        profile.criteria = criteria
        profile.won_deal_count = len(won)
    await session.flush()
    return profile
