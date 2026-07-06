"""Suggested-reply drafting (Sonnet, optional).

Gated by ``AgentSettings.auto_draft_replies`` (default OFF — it's the
one agent feature with real per-reply cost, ~$0.01-0.02 on Sonnet).
The draft is prose the user may actually send, so it runs on
``settings.ANTHROPIC_AGENT_DRAFT_MODEL`` (Sonnet), not Haiku.

THE DRAFT IS NEVER SENT.  It's stored on the ``draft_reply`` audit row
and surfaced in the Replies triage feed with a Copy button — the human
pastes it into their own mail client.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.services._anthropic import extract_text, get_client
from app.services.tenant_keys import ambient_api_key
from app.services._anthropic_cost import message_cost_usd

logger = logging.getLogger(__name__)

# Intents where a suggested reply is useless — don't spend Sonnet on them.
_NO_DRAFT_INTENTS = {"out_of_office", "unsubscribe", "not_interested"}

_MAX_DRAFT_CHARS = 1500


@dataclass
class DraftResult:
    ok: bool
    body: str = ""
    model: str = ""
    cost_usd: float = 0.0


_PROMPT = """You are drafting a SUGGESTED reply to an inbound email for a \
sales operator to review, edit, and send themselves.  It will never be \
sent automatically.

THE PROSPECT we originally emailed:
{lead_context}

THEIR REPLY:
Subject: {subject}
Body:
{body}

Classification: sentiment={sentiment}, intent={intent} — {summary}

Write the reply body only (no subject line, no commentary, no \
placeholders like [Name] — use what you know or omit gracefully).
Rules:
- Match their energy; be warm, direct, and brief (under 120 words).
- Move toward the next concrete step (a call, an answer, a resource).
- If they asked a question you can't answer from context, acknowledge \
it and promise the specific follow-up rather than inventing facts.
- No pushy closing, no exclamation overload, no "I hope this finds you well".
- Sign off with just the sender's first name: {sender_name}"""


def should_draft(intent: str) -> bool:
    """Drafts are skipped for intents where a reply is pointless."""
    return (intent or "other") not in _NO_DRAFT_INTENTS


def _format_lead_context(lead_context: dict[str, Any] | None) -> str:
    ctx = lead_context or {}
    bits = []
    for key in ("name", "email", "company", "job_title", "campaign_goal"):
        val = ctx.get(key)
        if val:
            bits.append(f"{key}: {val}")
    return "\n".join(bits) or "(no context available)"


async def draft_reply(
    subject: str,
    body_text: str,
    classification: Any,  # reply_sentiment.ReplyClassification
    lead_context: dict[str, Any] | None = None,
    sender_name: str | None = None,
) -> DraftResult:
    """Draft one suggested reply.  Returns ``DraftResult(ok=False)``
    instead of raising on any model problem — drafting is a bonus, it
    must never block the reply pipeline."""
    model = settings.ANTHROPIC_AGENT_DRAFT_MODEL
    prompt = _PROMPT.format(
        lead_context=_format_lead_context(lead_context),
        subject=(subject or "").strip()[:500],
        body=(body_text or "").strip()[:4000] or "(empty body)",
        sentiment=getattr(classification, "sentiment", "neutral"),
        intent=getattr(classification, "intent", "other"),
        summary=getattr(classification, "summary", "") or "(none)",
        sender_name=sender_name or settings.OWNER_NOTIFY_NAME,
    )
    try:
        message = await get_client(await ambient_api_key("anthropic")).messages.create(
            model=model,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001 — drafting is best-effort
        logger.warning("reply draft call failed: %s", exc)
        return DraftResult(ok=False, model=model)

    cost = message_cost_usd(message, model)
    body = extract_text(message).strip()
    if not body:
        return DraftResult(ok=False, model=model, cost_usd=cost)
    return DraftResult(ok=True, body=body[:_MAX_DRAFT_CHARS], model=model, cost_usd=cost)
