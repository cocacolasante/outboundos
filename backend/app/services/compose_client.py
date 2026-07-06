"""Compose helper for the one-off 'research a client' tool.

Mirrors the bulk pipeline's compose prompts (``app.workers.compose``)
but accepts a user-supplied ``char_limit`` and an ``output_kind`` toggle
so the same flow can produce either a personalized email
(subject + body) or a LinkedIn DM (body only).

Reusing the bulk worker's prompt builders directly was tempting but
they bake in fixed length targets ("under 200 words", "under 100 words")
and the email variant wires in style corrections from the campaign's
``StyleCorrection`` table — neither of which applies to a one-off, user-
parameterised request.  Keeping a dedicated builder here means changes
to the bulk-pipeline copy can't quietly drift this surface.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from app.workers.compose import (
    _call_anthropic,
    _strip_long_dashes,
    _validate_email_json,
    _STYLE_RULES,
)
from app.services.web_research import _parse_json

logger = logging.getLogger(__name__)


OutputKind = Literal["email", "linkedin_dm"]


def _bullet(items: list[str], placeholder: str = "(none)") -> str:
    items = [s for s in items if s]
    return "; ".join(items) if items else placeholder


def _freshness_block() -> str:
    """Cold-outreach freshness rules, embedded in every compose prompt.

    The research stage already filters to a 6-month window with a 1-year
    hard floor, but the LLM doing the actual writing also gets the rule
    so it won't infer / hallucinate an old reference from a hint in the
    research bullets.  Includes today's date so the model knows what
    "the last 6 months" means in absolute terms.
    """
    today = datetime.now(timezone.utc).date()
    six = (today - timedelta(days=183)).isoformat()
    year = (today - timedelta(days=365)).isoformat()
    return (
        "FRESHNESS RULES (strict):\n"
        f"- Today is {today.isoformat()}.\n"
        f"- Only reference work, posts, talks, news, or milestones dated "
        f"{six} or later (roughly the last 6 months).\n"
        f"- NEVER reference anything dated before {year} (over a year old).\n"
        "- Do NOT reference a position the prospect no longer holds.  The "
        "research above lists their CURRENT role; treat that as ground "
        "truth.  Do not name previous employers, previous titles, or "
        "past-company achievements.\n"
        "- If the research signals look stale (no parenthetical date in "
        "the recent window, vague phrasing like \"former\" / \"ex-\"), "
        "SKIP that signal and write a less-personalized message instead "
        "of name-checking a stale reference.\n"
        "- REPOST RULE: if a research bullet describes a REPOST or "
        "RESHARE the prospect made without adding their own commentary "
        "(phrases like \"reposted\", \"reshared\", \"shared a post about\" "
        "with no \"commented\" / \"saying\" / \"added\" qualifier), DO NOT "
        "reference it as if they wrote it.  A naked reshare is not their "
        "content; treating it as their thought reads as cringe in a cold "
        "outreach.  Skip it or paraphrase neutrally (\"I saw the X piece "
        "in your feed\") rather than \"loved your post on X\".  Bullets "
        "that DO include their own commentary (e.g. \"commented on a "
        "repost about X, saying Y\") are fair game — reference what THEY "
        "said, not the original."
    )


def _build_email_prompt(
    *, goal: str, tone: str, sender_name: str, char_limit: int,
    research: dict[str, Any],
) -> str:
    first = research.get("first_name") or ""
    last = research.get("last_name") or ""
    company = research.get("company") or ""
    title = research.get("job_title") or ""
    website = research.get("company_website") or ""
    headline = research.get("headline") or "(unknown)"
    quality = research.get("quality") or "low"
    site_line = f"Website: {website}\n" if website else ""

    if quality == "low":
        # No personalization signals.  Fall back to a generic-but-credible
        # subject + body grounded in name + company only.
        return (
            "You are an expert cold email copywriter writing ONE outreach "
            "email on behalf of a real person.\n"
            f"Campaign goal: {goal}\n"
            f"Tone: {tone}\n"
            f"Sender: {sender_name or '(the sender)'}\n\n"
            "Recipient:\n"
            f"Name: {first} {last}\n"
            f"Company: {company or '(unknown)'}\n"
            f"{site_line}\n"
            "Web research returned no personalization signals.  Use name + "
            "company only.  Focus on value, not flattery.  Do not invent "
            "facts about the recipient.\n\n"
            f"{_freshness_block()}\n\n"
            f"Hard limit: the body must be AT MOST {char_limit} characters "
            "(this includes spaces and newlines).  Aim slightly under.\n\n"
            f"{_STYLE_RULES}\n\n"
            'Respond ONLY with: {"subject": "...", "body": "..."}'
        )

    return (
        "You are an expert cold email copywriter writing ONE outreach "
        "email on behalf of a real person.\n"
        f"Campaign goal: {goal}\n"
        f"Tone: {tone}\n"
        f"Sender: {sender_name or '(the sender)'}\n\n"
        "Recipient (current role — do NOT reference any other role):\n"
        f"Name: {first} {last}, {title} at {company}\n"
        f"{site_line}"
        f"LinkedIn headline: {headline}\n"
        f"Recent person news: {_bullet(research.get('person_news') or [])}\n"
        f"Recent company news: {_bullet(research.get('company_news') or [])}\n"
        f"Company: {research.get('company_description') or '(unknown)'}\n"
        f"Recent company updates: {_bullet(research.get('recent_updates') or [])}\n\n"
        "Write a personalized subject line and email body.  Reference "
        "something SPECIFIC and REAL from the research above.  No "
        "sycophancy.  Do not mention doing research.  Do not invent facts.\n\n"
        f"{_freshness_block()}\n\n"
        f"Hard limit: the body must be AT MOST {char_limit} characters "
        "(spaces and newlines counted).  Aim slightly under to stay safe.\n\n"
        f"{_STYLE_RULES}\n\n"
        'Respond ONLY with: {"subject": "...", "body": "..."}'
    )


def _build_dm_prompt(
    *, goal: str, tone: str, sender_name: str, char_limit: int,
    research: dict[str, Any],
) -> str:
    first = research.get("first_name") or ""
    last = research.get("last_name") or ""
    company = research.get("company") or ""
    title = research.get("job_title") or ""
    website = research.get("company_website") or ""
    headline = research.get("headline") or "(unknown)"
    quality = research.get("quality") or "low"
    site_line = f"Website: {website}\n" if website else ""

    # Subject lines on LinkedIn DMs are the optional "topic" you set when
    # starting a new conversation thread (shows above the body in the
    # recipient's inbox preview).  Keep it short — 3-7 words, no punctuation
    # trickery — so it doesn't look like a sales template.
    subject_instr = (
        "Also produce a SHORT subject line (3-7 words, no exclamation marks, "
        "no clickbait, no all-caps).  This is the message topic shown in the "
        "recipient's inbox preview when you start a new thread."
    )

    if quality == "low":
        return (
            "You are writing ONE short LinkedIn direct message for a cold "
            "outreach.\n"
            f"Goal: {goal}\n"
            f"Tone: {tone}\n"
            f"Sender: {sender_name or '(the sender)'}\n\n"
            "Recipient:\n"
            f"Name: {first} {last}\n"
            f"Company: {company or '(unknown)'}\n"
            f"{site_line}\n"
            "No personalization signals available.  Write a brief, natural "
            "DM.  Conversational, not formal.  No flattery.\n\n"
            f"{subject_instr}\n\n"
            f"{_freshness_block()}\n\n"
            f"Hard limit: the body must be at most {char_limit} characters "
            "(spaces + newlines counted).  LinkedIn DMs are short; aim under.\n\n"
            f"{_STYLE_RULES}\n\n"
            'Respond ONLY with: {"subject": "...", "body": "..."}'
        )

    return (
        "You are writing ONE personalized LinkedIn direct message for a "
        "cold outreach.\n"
        f"Goal: {goal}\n"
        f"Tone: {tone}\n"
        f"Sender: {sender_name or '(the sender)'}\n\n"
        "Recipient (current role — do NOT reference any other role):\n"
        f"Name: {first} {last}, {title} at {company}\n"
        f"{site_line}"
        f"LinkedIn headline: {headline}\n"
        f"Recent person news: {_bullet(research.get('person_news') or [])}\n"
        f"Recent company news: {_bullet(research.get('company_news') or [])}\n"
        f"Company: {research.get('company_description') or '(unknown)'}\n\n"
        "Write a conversational LinkedIn DM that references something "
        "SPECIFIC from the research.  No flattery.  Don't mention researching.\n\n"
        f"{subject_instr}\n\n"
        f"{_freshness_block()}\n\n"
        f"Hard limit: the body must be at most {char_limit} characters "
        "(spaces + newlines counted).  Aim under the limit.\n\n"
        f"{_STYLE_RULES}\n\n"
        'Respond ONLY with: {"subject": "...", "body": "..."}'
    )


async def _generate_email(prompt: str) -> dict[str, str]:
    text = await _call_anthropic(prompt)
    parsed = _validate_email_json(_parse_json(text))
    if parsed:
        return parsed
    stricter = (
        prompt + "\n\nCRITICAL: Previous response was not valid JSON. "
        'Respond ONLY with: {"subject": "...", "body": "..."}'
    )
    text = await _call_anthropic(stricter)
    parsed = _validate_email_json(_parse_json(text))
    if parsed is None:
        raise ValueError("Anthropic returned unparseable JSON after retry")
    return parsed


def _parse_dm_response(parsed: Any) -> dict[str, str] | None:
    if not isinstance(parsed, dict):
        return None
    body = parsed.get("body")
    if not isinstance(body, str) or not body.strip():
        return None
    subject = parsed.get("subject")
    subject = subject.strip() if isinstance(subject, str) else ""
    return {"subject": subject, "body": body.strip()}


async def _generate_dm(prompt: str) -> dict[str, str]:
    """Returns ``{"subject": str, "body": str}``.  Subject may be empty if
    the model declined to produce one (e.g. parse failure that fell back
    to body-only)."""
    text = await _call_anthropic(prompt, "Write the DM. Respond ONLY with JSON.")
    parsed = _parse_dm_response(_parse_json(text))
    if parsed:
        return parsed
    stricter = (
        prompt + '\n\nCRITICAL: Previous response invalid. '
        'Respond ONLY with: {"subject": "...", "body": "..."}'
    )
    text = await _call_anthropic(stricter, "Write the DM. Respond ONLY with JSON.")
    parsed = _parse_dm_response(_parse_json(text))
    if parsed is None:
        raise ValueError("Anthropic returned unparseable JSON after retry")
    return parsed


def _truncate_at_sentence(text: str, limit: int) -> str:
    """Hard-cap ``text`` at ``limit`` characters, preferring a sentence end.

    The LLM is told to stay under the limit but occasionally drifts a few
    characters over.  We cut at the last sentence-ending punctuation
    within the limit so the message still reads cleanly; if there isn't
    one we cut on a word boundary and let the receiver see the truncation.
    """
    if len(text) <= limit:
        return text
    head = text[:limit]
    for stop in (". ", "! ", "? ", ".\n", "!\n", "?\n"):
        idx = head.rfind(stop)
        if idx >= int(limit * 0.6):  # don't accept truncations that lose >40% of the budget
            return head[: idx + 1].rstrip()
    # Fall back to word boundary
    idx = head.rfind(" ")
    if idx >= int(limit * 0.6):
        return head[:idx].rstrip()
    return head.rstrip()


async def compose_for_client(
    *, output_kind: OutputKind, goal: str, tone: str, sender_name: str,
    char_limit: int, research: dict[str, Any],
) -> dict[str, str]:
    """Compose a single outreach message for the researched prospect.

    Returns ``{"subject": str, "body": str}``.  Subject is "" for LinkedIn DM.
    Body is em/en-dash-stripped and hard-capped to ``char_limit``.
    """
    if char_limit < 50 or char_limit > 5000:
        raise ValueError("char_limit must be between 50 and 5000")
    if output_kind == "email":
        prompt = _build_email_prompt(
            goal=goal, tone=tone, sender_name=sender_name,
            char_limit=char_limit, research=research,
        )
        composed = await _generate_email(prompt)
        subject = _strip_long_dashes(composed["subject"])
        body = _strip_long_dashes(composed["body"])
        body = _truncate_at_sentence(body, char_limit)
        return {"subject": subject, "body": body}
    elif output_kind == "linkedin_dm":
        prompt = _build_dm_prompt(
            goal=goal, tone=tone, sender_name=sender_name,
            char_limit=char_limit, research=research,
        )
        composed = await _generate_dm(prompt)
        subject = _strip_long_dashes(composed["subject"])
        body = _strip_long_dashes(composed["body"])
        body = _truncate_at_sentence(body, char_limit)
        return {"subject": subject, "body": body}
    else:
        raise ValueError(f"unknown output_kind: {output_kind!r}")
