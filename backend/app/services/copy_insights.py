"""Reply-driven copy insights: which angles earn positive replies.

Three layers, cheapest-first:

  winning_examples      — plain SQL: the composed copy of positive-reply
                          leads on a campaign.  Zero LLM cost.
  refresh_angle_summary — one Haiku call summarising positive vs
                          negative outcome sets into structured angles;
                          cached on ``campaign_copy_insights`` and only
                          re-run after ``MIN_NEW_OUTCOMES`` new outcomes.
  build_winning_block   — formats cache + examples into the compose
                          prompt block.  Reads only; never triggers the
                          LLM, so compose latency/cost is unchanged.

The block is deliberately SUBORDINATE to the user's StyleCorrection
examples — the user's voice always wins over the model's inference.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import CampaignCopyInsights, ReplyOutcome
from app.services._anthropic import extract_text, get_client, parse_json_object
from app.services.tenant_keys import ambient_api_key
from app.services._anthropic_cost import message_cost_usd

logger = logging.getLogger(__name__)

# Re-run the LLM summary only after this many NEW outcomes since the
# last refresh — keeps the loop cheap on slow campaigns.
MIN_NEW_OUTCOMES = 3
# Need at least one positive outcome before any block is injected.
_EXAMPLE_BODY_CAP = 600
_SET_CAP = 10  # outcomes per sentiment fed to the summariser

_INSIGHT_KEYS = (
    "winning_openers", "subject_patterns", "value_framings",
    "cta_styles", "avoid",
)


async def winning_examples(
    session: AsyncSession, campaign_id: uuid.UUID, limit: int = 5,
) -> list[dict[str, str]]:
    """Composed subject+body of this campaign's POSITIVE replies,
    most recent first."""
    rows = (await session.execute(
        select(ReplyOutcome)
        .where(
            ReplyOutcome.campaign_id == campaign_id,
            ReplyOutcome.sentiment == "positive",
        )
        .order_by(ReplyOutcome.created_at.desc())
        .limit(limit)
    )).scalars().all()
    return [
        {
            "subject": r.composed_subject or "",
            "body": (r.composed_body or "")[:_EXAMPLE_BODY_CAP],
        }
        for r in rows
        if (r.composed_body or "").strip()
    ]


async def get_cached_insights(
    session: AsyncSession, campaign_id: uuid.UUID,
) -> CampaignCopyInsights | None:
    return await session.scalar(
        select(CampaignCopyInsights).where(
            CampaignCopyInsights.campaign_id == campaign_id,
        )
    )


def _format_outcomes(rows: list[ReplyOutcome]) -> str:
    parts = []
    for r in rows:
        parts.append(
            f"Subject: {r.composed_subject or '(none)'}\n"
            f"Body: {(r.composed_body or '')[:_EXAMPLE_BODY_CAP]}"
        )
    return "\n---\n".join(parts) or "(none)"


_PROMPT = """You are analysing cold-email outcomes for ONE campaign to \
extract what's working.  Below are the emails that earned POSITIVE \
replies and the ones that earned NEGATIVE replies.

EMAILS THAT EARNED POSITIVE REPLIES:
{positive}

EMAILS THAT EARNED NEGATIVE REPLIES:
{negative}

Extract the patterns.  Be concrete and campaign-specific — quote short
fragments where useful.  Base "avoid" on the negative set (and anything
conspicuously absent from the positive set).  If a category has no
clear signal, return an empty list for it — do not invent patterns.

Respond with ONLY this JSON object:
{{
  "winning_openers": ["..."],
  "subject_patterns": ["..."],
  "value_framings": ["..."],
  "cta_styles": ["..."],
  "avoid": ["..."]
}}
Each list: 0-4 short strings (max ~15 words each)."""


async def refresh_angle_summary(
    session: AsyncSession, campaign_id: uuid.UUID, force: bool = False,
) -> dict[str, Any] | None:
    """Refresh (or return cached) angle insights for a campaign.

    Returns the insights dict, or None when there's nothing to learn
    from yet (no positive outcomes).  Only calls the LLM when ``force``
    or ≥ MIN_NEW_OUTCOMES outcomes landed since the last refresh.
    Caller owns the transaction.
    """
    total = (await session.execute(
        select(func.count()).select_from(ReplyOutcome).where(
            ReplyOutcome.campaign_id == campaign_id,
        )
    )).scalar_one()

    cached = await get_cached_insights(session, campaign_id)
    if not force and cached is not None:
        if total - cached.outcome_count_at_refresh < MIN_NEW_OUTCOMES:
            return cached.insights

    positives = (await session.execute(
        select(ReplyOutcome)
        .where(
            ReplyOutcome.campaign_id == campaign_id,
            ReplyOutcome.sentiment == "positive",
        )
        .order_by(ReplyOutcome.created_at.desc())
        .limit(_SET_CAP)
    )).scalars().all()
    if not positives:
        return None  # nothing to learn from yet

    negatives = (await session.execute(
        select(ReplyOutcome)
        .where(
            ReplyOutcome.campaign_id == campaign_id,
            ReplyOutcome.sentiment == "negative",
        )
        .order_by(ReplyOutcome.created_at.desc())
        .limit(_SET_CAP)
    )).scalars().all()

    model = settings.ANTHROPIC_AGENT_MODEL  # extraction task → Haiku
    try:
        message = await get_client(await ambient_api_key("anthropic")).messages.create(
            model=model,
            max_tokens=600,
            messages=[{
                "role": "user",
                "content": _PROMPT.format(
                    positive=_format_outcomes(positives),
                    negative=_format_outcomes(negatives),
                ),
            }],
        )
    except Exception as exc:  # noqa: BLE001 — keep serving the stale cache
        logger.warning("angle summary call failed for %s: %s", campaign_id, exc)
        return cached.insights if cached else None

    data = parse_json_object(extract_text(message))
    if data is None:
        logger.warning("angle summary returned unparseable output for %s", campaign_id)
        return cached.insights if cached else None

    insights: dict[str, Any] = {
        key: [str(x)[:200] for x in data.get(key, []) if x][:4]
        for key in _INSIGHT_KEYS
    }
    insights["_meta"] = {
        "model": model,
        "cost_usd": round(message_cost_usd(message, model), 6),
        "positive_count": len(positives),
        "negative_count": len(negatives),
    }

    if cached is None:
        cached = CampaignCopyInsights(
            campaign_id=campaign_id,
            insights=insights,
            outcome_count_at_refresh=total,
        )
        session.add(cached)
    else:
        cached.insights = insights
        cached.outcome_count_at_refresh = total
        cached.refreshed_at = datetime.now(timezone.utc)
    await session.flush()
    return insights


def _bullets(items: list[str] | None) -> str:
    return "\n".join(f"- {x}" for x in (items or []))


async def build_winning_block(
    session: AsyncSession, campaign_id: uuid.UUID,
) -> str | None:
    """The compose-prompt block.  None when no positive-reply data exists
    — the prompt stays exactly as before until the loop has signal.
    Reads the cache only (never triggers the LLM)."""
    cached = await get_cached_insights(session, campaign_id)
    examples = await winning_examples(session, campaign_id, limit=2)
    if cached is None and not examples:
        return None

    sections: list[str] = [
        "What's working on THIS campaign (mirror these patterns, don't "
        "copy verbatim — and the user's style corrections above always "
        "take precedence over this):",
    ]
    if cached is not None:
        ins = cached.insights
        if ins.get("winning_openers"):
            sections.append("Winning openers:\n" + _bullets(ins["winning_openers"]))
        if ins.get("subject_patterns"):
            sections.append(
                "Subject patterns that get replies:\n" + _bullets(ins["subject_patterns"])
            )
        if ins.get("value_framings"):
            sections.append("Value framings:\n" + _bullets(ins["value_framings"]))
        if ins.get("cta_styles"):
            sections.append("CTA styles:\n" + _bullets(ins["cta_styles"]))
        if ins.get("avoid"):
            sections.append("Avoid:\n" + _bullets(ins["avoid"]))
    if examples:
        sections.append(
            "Example messages that earned positive replies:\n"
            + "\n---\n".join(
                f"Subject: {e['subject']}\n{e['body']}" for e in examples
            )
        )
    return "\n\n".join(sections)
