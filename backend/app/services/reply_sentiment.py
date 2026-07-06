"""Inbound-reply sentiment/intent classification (Haiku, strict JSON).

One Anthropic call per newly-polled reply.  Extraction-shaped task →
``settings.ANTHROPIC_AGENT_MODEL`` (Haiku) keeps the per-reply cost
around $0.001.  The classifier NEVER acts on anything — it returns a
typed result and ``agent_core`` decides what (if anything) to do under
the operator's AgentSettings.

Failure posture: any parse/validation problem returns a neutral
low-confidence fallback with ``parse_failed=True`` so the caller can
write an ``AgentAction(status=failed)`` audit row and move on — a bad
classification must never block reply recording.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.services._anthropic import extract_text, get_client, parse_json_object
from app.services.tenant_keys import ambient_api_key
from app.services._anthropic_cost import message_cost_usd

logger = logging.getLogger(__name__)

_VALID_SENTIMENTS = {"positive", "neutral", "negative"}
_VALID_INTENTS = {
    "interested", "meeting_request", "question", "objection",
    "not_interested", "out_of_office", "unsubscribe", "other",
}
_VALID_NEXT_ACTIONS = {
    "convert", "reply", "schedule", "nurture", "close_lost", "ignore",
}


@dataclass
class ReplyClassification:
    sentiment: str = "neutral"
    intent: str = "other"
    confidence: float = 0.0
    summary: str = ""
    suggested_next_action: str = "ignore"
    parse_failed: bool = False
    model: str = ""
    cost_usd: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


_PROMPT = """You are classifying an inbound email reply to a cold-outreach \
message, for a CRM triage feed.

THE PROSPECT we originally emailed:
{lead_context}

THE REPLY we received:
Subject: {subject}
Body:
{body}

Classify it.  Rules:
- "positive" = genuine buying interest, a meeting ask, or a warm question \
about the offering.  "negative" = clear rejection / unsubscribe / hostility.
Everything else (including ambiguous) is "neutral".
- Auto-replies (out-of-office, "no longer with the company", autoresponders) \
are intent "out_of_office", sentiment "neutral", and LOW priority — never \
mark them positive regardless of wording.
- An unsubscribe request is intent "unsubscribe", sentiment "negative", \
suggested_next_action "close_lost".
- confidence is YOUR certainty in the sentiment label, 0.0-1.0.

Respond with ONLY this JSON object, no prose:
{{
  "sentiment": "positive|neutral|negative",
  "intent": "interested|meeting_request|question|objection|not_interested|out_of_office|unsubscribe|other",
  "confidence": 0.0,
  "summary": "one line, max 140 chars",
  "suggested_next_action": "convert|reply|schedule|nurture|close_lost|ignore"
}}"""


def _format_lead_context(lead_context: dict[str, Any] | None) -> str:
    ctx = lead_context or {}
    bits = []
    for key in ("name", "email", "company", "job_title", "campaign_goal"):
        val = ctx.get(key)
        if val:
            bits.append(f"{key}: {val}")
    return "\n".join(bits) or "(no context available)"


def _fallback(*, model: str, cost: float, raw: dict[str, Any] | None = None) -> ReplyClassification:
    return ReplyClassification(
        sentiment="neutral",
        intent="other",
        confidence=0.0,
        summary="classification failed — manual review",
        suggested_next_action="ignore",
        parse_failed=True,
        model=model,
        cost_usd=cost,
        raw=raw or {},
    )


async def classify_reply(
    subject: str,
    body_text: str,
    lead_context: dict[str, Any] | None = None,
) -> ReplyClassification:
    """Classify one inbound reply.  Returns the neutral fallback (with
    ``parse_failed=True``) instead of raising on model/parse problems."""
    model = settings.ANTHROPIC_AGENT_MODEL
    prompt = _PROMPT.format(
        lead_context=_format_lead_context(lead_context),
        subject=(subject or "").strip()[:500],
        body=(body_text or "").strip()[:4000] or "(empty body)",
    )

    try:
        message = await get_client(await ambient_api_key("anthropic")).messages.create(
            model=model,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001 — API/network; never block the poller
        logger.warning("reply classification call failed: %s", exc)
        return _fallback(model=model, cost=0.0)

    cost = message_cost_usd(message, model)
    data = parse_json_object(extract_text(message))
    if data is None:
        logger.warning("reply classification returned unparseable output")
        return _fallback(model=model, cost=cost)

    sentiment = str(data.get("sentiment", "")).lower().strip()
    intent = str(data.get("intent", "")).lower().strip()
    action = str(data.get("suggested_next_action", "")).lower().strip()
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0

    if sentiment not in _VALID_SENTIMENTS:
        return _fallback(model=model, cost=cost, raw=data)
    if intent not in _VALID_INTENTS:
        intent = "other"
    if action not in _VALID_NEXT_ACTIONS:
        action = "ignore"

    # Belt-and-suspenders on the OOO rule — an autoresponder is never a
    # buying signal no matter what sentiment came back.
    if intent == "out_of_office":
        sentiment = "neutral"

    return ReplyClassification(
        sentiment=sentiment,
        intent=intent,
        confidence=confidence,
        summary=str(data.get("summary", "") or "")[:280],
        suggested_next_action=action,
        parse_failed=False,
        model=model,
        cost_usd=cost,
        raw=data,
    )
