"""Compose a short outreach email for a prospect signal.

A signal-tailored composer (distinct from the LinkedIn-research-driven
``compose_client``): it opens on the SPECIFIC public trigger — the
federal grant, the new 501(c)(3) ruling, the job change — so the email
reads as genuine and timely rather than generic.

One Anthropic call on ``settings.ANTHROPIC_MODEL`` (Sonnet — email copy
quality matters).  Returns ``{"subject", "body"}``; the body is
em/en-dash-stripped and hard-capped at ``char_limit``.  No signature is
included — the send path appends the chosen inbox's signature.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.config import settings
from app.services._anthropic import extract_text, get_client, parse_json_object
from app.services.tenant_keys import ambient_api_key
from app.services.compose_client import _strip_long_dashes, _truncate_at_sentence

logger = logging.getLogger(__name__)


class SignalComposeError(RuntimeError):
    """The model call failed or returned unparseable copy."""


def _detail_lines(detail: dict[str, Any]) -> str:
    """Compact, human-readable rendering of the signal's detail payload
    for the prompt (skips internal/empty fields)."""
    skip = {"source_url", "_cost_usd"}
    bits = []
    for key, value in (detail or {}).items():
        if key in skip or value in (None, "", [], {}):
            continue
        label = key.replace("_", " ")
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        bits.append(f"{label}: {value}")
    return "\n".join(bits) or "(no extra detail)"


_PROMPT = """You are writing a short cold-outreach email to a nonprofit, \
prompted by a REAL public signal about them.  Reference the signal \
genuinely — this is why you're reaching out now.

ORGANIZATION: {org_name}
{contact_line}SIGNAL: {summary}
DETAILS:
{detail}

SENDER: {sender_name}
GOAL: {goal}
TONE: {tone}

Rules:
- Open by acknowledging the SPECIFIC signal (the grant / their new \
501(c)(3) status / the change) — congratulate or note it, no empty flattery.
- Be concrete and brief: under {char_limit} characters, plain text, no \
markdown, NO em dashes.
- Exactly one low-friction call to action (a short call / quick reply).
- Do NOT invent facts beyond what's given above.
- Do NOT add a signature or contact block — it's appended automatically. \
End after the CTA (a simple "Best," sign-off line is fine).

Respond with ONLY a JSON object: {{"subject": "...", "body": "..."}}"""


def _default_goal(signal_type: str) -> str:
    if signal_type == "grant_awarded":
        return ("congratulate them on the grant and offer help putting it to "
                "work, then ask for a brief call")
    if signal_type == "new_501c3":
        return ("welcome them as a new nonprofit and offer to help them get "
                "set up, then ask for a brief call")
    return "introduce ourselves, reference the signal, and ask for a brief call"


async def compose_signal_email(
    *,
    signal_type: str,
    summary: str,
    detail: dict[str, Any] | None,
    org_name: str,
    contact_first_name: str | None = None,
    goal: str | None = None,
    tone: str | None = None,
    sender_name: str = "",
    char_limit: int = 900,
) -> dict[str, str]:
    contact_line = (
        f"CONTACT FIRST NAME: {contact_first_name}\n" if contact_first_name else ""
    )
    prompt = _PROMPT.format(
        org_name=org_name or "your organization",
        contact_line=contact_line,
        summary=summary,
        detail=_detail_lines(detail or {}),
        sender_name=sender_name or "our team",
        goal=goal or _default_goal(signal_type),
        tone=tone or "warm and professional",
        char_limit=char_limit,
    )
    try:
        message = await get_client(await ambient_api_key("anthropic")).messages.create(
            model=settings.ANTHROPIC_MODEL,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("signal compose call failed: %s", exc)
        raise SignalComposeError(str(exc)) from exc

    data = parse_json_object(extract_text(message))
    if not data or not data.get("subject") or not data.get("body"):
        raise SignalComposeError("model returned no usable subject/body")

    subject = _strip_long_dashes(str(data["subject"]))[:200].strip()
    body = _truncate_at_sentence(_strip_long_dashes(str(data["body"])), char_limit)
    return {"subject": subject, "body": body}
