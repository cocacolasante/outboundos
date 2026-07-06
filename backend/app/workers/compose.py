"""Compose worker: generate the personalized email for a researched lead.

Picks a generic or personalized prompt based on research quality, calls
Anthropic without tools, parses the JSON response (retrying once with a
stricter system prompt on parse failure), sanitises stray em/en dashes
out of the body, and enqueues send_lead when the campaign is in the
running state.

Outreach emails are sent as individual person-to-person messages, so we
do NOT append a CAN-SPAM-style unsubscribe footer.  The
``/unsubscribe/{lead_id}`` route + Brevo unsubscribed-event handler stay
in place for any reply/bounce/complaint flow that touches them.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from anthropic import AsyncAnthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.services.tenant_keys import ambient_api_key
from app.models import (
    Campaign,
    CampaignStatus,
    ComposeStatus,
    Lead,
    ResearchMode,
    StyleCorrection,
)
from app.services.campaign_stop import stop_requested
from app.services.sequence_service import campaign_sends_legacy_first_email
from app.services.signature import apply_signature, resolve_campaign_signature
from app.services import copy_insights
from app.services.template_render import build_merge_context, render_template
from app.services.web_research import _extract_text, _parse_json
from app.workers.celery_app import celery_app
from app.tenancy.context import with_record_tenant
from app.workers.send import send_lead

try:  # pragma: no cover — import-time only
    import redis as _redis_sync
except Exception:  # noqa: BLE001
    _redis_sync = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_client: AsyncAnthropic | None = None


def _get_client(api_key: str | None = None) -> AsyncAnthropic:
    # BYOK (Phase 4): a fresh client per call — the key is per-tenant
    # now, so a module-global cache would leak keys across tenants.
    # api_key=None means the caller ran outside tenant context (env).
    return AsyncAnthropic(api_key=api_key or settings.ANTHROPIC_API_KEY or "unset")


# --------------------------------------------------------------------------
# Prompt construction
# --------------------------------------------------------------------------


_STYLE_RULES = (
    "STYLE RULES (strict):\n"
    "- This is a person-to-person email written by the sender. Do NOT include "
    "an unsubscribe link, footer, or any CAN-SPAM-style boilerplate.\n"
    "- Do NOT use em dashes or en dashes (no '—', no '–'). Use commas, "
    "periods, parentheses, or rephrase the sentence. This is non-negotiable.\n"
    "- Plain text only. No markdown."
)


def _build_retarget_prompt(
    goal: str, tone: str, sender_name: str,
    first_name: str, last_name: str, company: str,
    retarget_context: dict[str, Any],
    winning_block: str | None = None,
) -> str:
    """Re-engagement copy for a lead who engaged with a prior campaign email
    (clicked a tracked link, or connected on LinkedIn).  References the real
    engagement WITHOUT being creepy about tracking."""
    engaged_via = retarget_context.get("engaged_via") or "email_click"
    orig_subject = (retarget_context.get("original_subject") or "").strip()
    orig_body = (retarget_context.get("original_body") or "").strip()
    clicked_url = (retarget_context.get("clicked_url") or "").strip()
    winning_section = f"{winning_block}\n\n" if winning_block else ""

    if engaged_via == "linkedin_connection":
        signal_line = (
            "This person ACCEPTED your LinkedIn connection after a prior outreach "
            "campaign — a warm signal of interest.\n"
        )
    else:
        signal_line = (
            "This person CLICKED a link in a prior outreach email — a strong signal "
            "of interest. Re-engage on that interest naturally; do NOT explicitly say "
            '"I saw you clicked" (it reads as creepy tracking).\n'
        )
    link_line = f"Link they clicked: {clicked_url}\n" if clicked_url else ""
    orig_block = ""
    if orig_subject or orig_body:
        orig_block = (
            "\nThe email they engaged with (for context — write a NEW message, do not "
            "repeat it):\n"
            f"Subject: {orig_subject or '(none)'}\n"
            f"Body: {orig_body[:1200] or '(none)'}\n"
        )
    return (
        "You are an expert cold email copywriter writing a RE-TARGETING follow-up.\n"
        f"New campaign goal: {goal}\n"
        f"Tone: {tone}\n"
        f"Sender: {sender_name}\n\n"
        "Recipient:\n"
        f"Name: {first_name} {last_name}\n"
        f"Company: {company}\n\n"
        f"{signal_line}"
        f"{link_line}"
        f"{orig_block}\n"
        "Write a short, warm follow-up (under 130 words) that builds on their interest "
        "and moves toward the new campaign goal. Reference the topic/offer they engaged "
        "with, not the tracking.\n\n"
        f"{winning_section}"
        f"{_STYLE_RULES}\n\n"
        'Respond ONLY with a single JSON object: {"subject": "...", "body": "..."}'
    )


def _build_generic_prompt(
    goal: str, tone: str, sender_name: str,
    first_name: str, last_name: str, company: str,
    company_website: str = "",
    winning_block: str | None = None,
) -> str:
    website_line = f"Website: {company_website}\n" if company_website else ""
    winning_section = f"{winning_block}\n\n" if winning_block else ""
    return (
        "You are an expert cold email copywriter.\n"
        f"Campaign goal: {goal}\n"
        f"Tone: {tone}\n"
        f"Sender: {sender_name}\n\n"
        "Recipient:\n"
        f"Name: {first_name} {last_name}\n"
        f"Company: {company}\n"
        f"{website_line}"
        "\nResearch on this person was limited. Use only their name and company.\n"
        "Write a compelling subject line and email body under 150 words. Focus on "
        "value, not flattery.\n\n"
        f"{winning_section}"
        f"{_STYLE_RULES}\n\n"
        'Respond ONLY with a single JSON object: {"subject": "...", "body": "..."}'
    )


def _build_personalized_prompt(
    goal: str, tone: str, sender_name: str,
    first_name: str, last_name: str, company: str, job_title: str,
    research_data: dict[str, Any], corrected_examples: list[str],
    company_website: str = "",
    winning_block: str | None = None,
) -> str:
    person_news = "; ".join(research_data.get("person_news") or []) or "(none)"
    company_news = "; ".join(research_data.get("company_news") or []) or "(none)"
    recent_updates = "; ".join(research_data.get("recent_updates") or []) or "(none)"
    company_desc = research_data.get("company_description") or "(unknown)"
    linkedin_headline = research_data.get("linkedin_headline") or "(unknown)"
    website_line = f"Website: {company_website}\n" if company_website else ""

    if corrected_examples:
        style_block = (
            "Style corrections from user (MATCH THIS STYLE CLOSELY):\n"
            + "\n---\n".join(corrected_examples)
        )
    else:
        style_block = "No style corrections yet. Use your best judgment."

    # The reply-driven winning block is ADDITIVE and subordinate to the
    # user's style corrections above — user voice wins over inference.
    winning_section = f"{winning_block}\n\n" if winning_block else ""

    return (
        "You are an expert cold email copywriter.\n"
        f"Campaign goal: {goal}\n"
        f"Tone: {tone}\n"
        f"Sender: {sender_name}\n\n"
        "Recipient:\n"
        f"Name: {first_name} {last_name}, {job_title} at {company}\n"
        f"{website_line}"
        f"LinkedIn headline: {linkedin_headline}\n"
        f"Recent person news: {person_news}\n"
        f"Recent company news: {company_news}\n"
        f"Company: {company_desc}\n"
        f"Recent company updates: {recent_updates}\n\n"
        f"{style_block}\n\n"
        f"{winning_section}"
        "Write a personalized subject line and email body. Reference something "
        "specific and real from the research above. Keep under 200 words. No "
        "sycophancy. Do not mention doing research.\n\n"
        f"{_STYLE_RULES}\n\n"
        'Respond ONLY with a single JSON object: {"subject": "...", "body": "..."}'
    )


# --------------------------------------------------------------------------
# Anthropic call + parse
# --------------------------------------------------------------------------


def _strip_long_dashes(text: str) -> str:
    """Belt-and-suspenders sanitiser for the prompt's no-em-dashes rule.

    Models sometimes ignore style instructions on the first attempt.  Em
    dashes (U+2014) and en dashes (U+2013) are replaced with ", " — which
    reads naturally for the parenthetical-aside usage that most slipped
    em dashes correspond to.  Standalone hyphens (U+002D) are left alone.
    Collapses any accidental double-spaces that result.
    """
    if not text:
        return text
    for ch in ("—", "–"):
        text = text.replace(f" {ch} ", ", ")
        text = text.replace(ch, ", ")
    # Collapse "X,, Y" or stray double-spaces from the replacements.
    while ",," in text:
        text = text.replace(",,", ",")
    while "  " in text:
        text = text.replace("  ", " ")
    return text


def _validate_email_json(parsed: Any) -> dict[str, str] | None:
    if not isinstance(parsed, dict):
        return None
    subject = parsed.get("subject")
    body = parsed.get("body")
    if not isinstance(subject, str) or not isinstance(body, str):
        return None
    if not subject.strip() or not body.strip():
        return None
    return {"subject": subject.strip(), "body": body.strip()}


async def _call_anthropic(system_prompt: str, user_msg: str = "Write it now. Respond ONLY with the JSON object.") -> str:
    from app.billing.entitlements import Meter, check_quota

    await check_quota(Meter.AI_COMPOSE)
    message = await _get_client(await ambient_api_key("anthropic")).messages.create(
        model=settings.ANTHROPIC_MODEL,
        max_tokens=1000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_msg}],
    )
    return _extract_text(message)


async def _generate_email(system_prompt: str) -> dict[str, str]:
    """Call Anthropic; on parse failure, retry once with a stricter prompt."""
    text = await _call_anthropic(system_prompt)
    parsed = _validate_email_json(_parse_json(text))
    if parsed is not None:
        return parsed

    stricter = (
        system_prompt
        + "\n\nCRITICAL: Your previous response was not valid JSON. "
        'Respond with ONLY one JSON object: {"subject": "...", "body": "..."}. '
        "No markdown fences, no preamble, no commentary, no trailing text."
    )
    text = await _call_anthropic(stricter)
    parsed = _validate_email_json(_parse_json(text))
    if parsed is None:
        raise ValueError("Anthropic returned unparseable JSON after retry")
    return parsed


# --------------------------------------------------------------------------
# LinkedIn DM composition
# --------------------------------------------------------------------------


def _build_linkedin_dm_prompt(
    goal: str, tone: str, sender_name: str,
    first_name: str, last_name: str, company: str, job_title: str,
    research_data: dict[str, Any],
    company_website: str = "",
) -> str:
    quality = (research_data or {}).get("quality", "low")
    website_line = f"Website: {company_website}\n" if company_website else ""
    if quality == "low":
        return (
            "You are writing a short LinkedIn direct message for a cold outreach campaign.\n"
            f"Campaign goal: {goal}\n"
            f"Tone: {tone}\n"
            f"Sender: {sender_name}\n\n"
            "Recipient:\n"
            f"Name: {first_name} {last_name}\n"
            f"Company: {company}\n"
            f"{website_line}"
            "\nWrite a brief, natural LinkedIn DM under 100 words. "
            "Conversational. This is a direct message, not a formal email. "
            "No sycophancy. No hollow flattery.\n\n"
            f"{_STYLE_RULES}\n\n"
            'Respond ONLY with JSON: {"body": "..."}'
        )
    person_news = "; ".join(research_data.get("person_news") or []) or "(none)"
    company_news = "; ".join(research_data.get("company_news") or []) or "(none)"
    company_desc = research_data.get("company_description") or "(unknown)"
    linkedin_headline = research_data.get("linkedin_headline") or "(unknown)"
    return (
        "You are writing a short LinkedIn direct message for a cold outreach campaign.\n"
        f"Campaign goal: {goal}\n"
        f"Tone: {tone}\n"
        f"Sender: {sender_name}\n\n"
        "Recipient:\n"
        f"Name: {first_name} {last_name}, {job_title} at {company}\n"
        f"{website_line}"
        f"LinkedIn headline: {linkedin_headline}\n"
        f"Recent person news: {person_news}\n"
        f"Recent company news: {company_news}\n"
        f"Company: {company_desc}\n\n"
        "Write a personalized LinkedIn DM under 100 words. "
        "Reference something specific and real from the research. "
        "Conversational. This is a direct message on LinkedIn, not a formal email. "
        "No sycophancy. Do not mention doing research.\n\n"
        f"{_STYLE_RULES}\n\n"
        'Respond ONLY with JSON: {"body": "..."}'
    )


def _build_reply_prompt(
    *, goal: str, tone: str, sender_name: str,
    first_name: str, last_name: str, company: str, job_title: str,
    research_data: dict[str, Any], original_subject: str, original_body: str,
    idea: str,
) -> str:
    """Prompt for an in-thread follow-up REPLY to a prior cold email.

    Reuses the lead's existing research (no new research is done) and is
    steered by the user's generalized ``idea`` of what the reply should say.
    """
    quality = (research_data or {}).get("quality", "low")
    idea_line = (
        f"What this follow-up should focus on: {idea}\n" if (idea or "").strip()
        else "Follow-up angle: gently re-surface the original ask without repeating it verbatim.\n"
    )
    research_block = ""
    if quality != "low":
        person_news = "; ".join(research_data.get("person_news") or []) or "(none)"
        company_news = "; ".join(research_data.get("company_news") or []) or "(none)"
        research_block = (
            f"Recent person news: {person_news}\n"
            f"Recent company news: {company_news}\n"
        )
    return (
        "You are writing a SHORT follow-up REPLY in an existing email thread "
        "for a cold outreach campaign.  The recipient never replied to the "
        "first email; this is a nudge in the SAME thread (it will be sent as "
        "a reply, so do NOT restate the original pitch in full and do NOT add "
        "a subject line).\n"
        f"Campaign goal: {goal}\n"
        f"Tone: {tone}\n"
        f"Sender: {sender_name}\n"
        f"{idea_line}\n"
        "Recipient:\n"
        f"Name: {first_name} {last_name}"
        + (f", {job_title} at {company}" if job_title else f" at {company}") + "\n"
        f"{research_block}\n"
        "The original email you are replying to:\n"
        f"Subject: {original_subject}\n"
        f"---\n{original_body}\n---\n\n"
        "Write a brief, natural follow-up reply under 90 words. Open like a "
        "reply (e.g. a quick check-in), reference the original ask lightly, "
        "and give one clear next step. No sycophancy, no hollow flattery, do "
        "not mention doing research.\n\n"
        f"{_STYLE_RULES}\n\n"
        'Respond ONLY with JSON: {"body": "..."}'
    )


async def generate_followup_reply(
    *, goal: str, tone: str, sender_name: str,
    first_name: str, last_name: str, company: str, job_title: str,
    research_data: dict[str, Any], original_subject: str, original_body: str,
    idea: str = "",
) -> str:
    """Compose an in-thread follow-up reply via Anthropic; retry once on
    parse failure.  Does NOT trigger research — reuses ``research_data``."""
    system_prompt = _build_reply_prompt(
        goal=goal, tone=tone, sender_name=sender_name,
        first_name=first_name, last_name=last_name, company=company,
        job_title=job_title, research_data=research_data,
        original_subject=original_subject, original_body=original_body, idea=idea,
    )
    user_msg = "Write the follow-up reply now. Respond ONLY with the JSON object."
    text = await _call_anthropic(system_prompt, user_msg)
    parsed = _parse_json(text)
    if isinstance(parsed, dict) and isinstance(parsed.get("body"), str) and parsed["body"].strip():
        return _strip_long_dashes(parsed["body"].strip())

    stricter = system_prompt + '\n\nCRITICAL: Respond ONLY with {"body": "..."}. No markdown, no preamble.'
    text = await _call_anthropic(stricter, user_msg)
    parsed = _parse_json(text)
    if isinstance(parsed, dict) and isinstance(parsed.get("body"), str) and parsed["body"].strip():
        return _strip_long_dashes(parsed["body"].strip())
    raise ValueError("Anthropic returned unparseable response for follow-up reply after retry")


async def generate_linkedin_dm_text(
    goal: str, tone: str, sender_name: str,
    first_name: str, last_name: str, company: str, job_title: str,
    research_data: dict[str, Any],
    company_website: str = "",
) -> str:
    """Call Anthropic to compose a personalized LinkedIn DM; retry once on parse failure."""
    system_prompt = _build_linkedin_dm_prompt(
        goal=goal, tone=tone, sender_name=sender_name,
        first_name=first_name, last_name=last_name,
        company=company, job_title=job_title,
        research_data=research_data, company_website=company_website,
    )
    text = await _call_anthropic(system_prompt, "Write the LinkedIn DM now. Respond ONLY with the JSON object.")
    parsed = _parse_json(text)
    if isinstance(parsed, dict) and isinstance(parsed.get("body"), str) and parsed["body"].strip():
        return _strip_long_dashes(parsed["body"].strip())

    stricter = (
        system_prompt
        + '\n\nCRITICAL: Respond ONLY with {"body": "..."}. No markdown, no preamble, no trailing text.'
    )
    text = await _call_anthropic(stricter, "Write the LinkedIn DM now. Respond ONLY with the JSON object.")
    parsed = _parse_json(text)
    if isinstance(parsed, dict) and isinstance(parsed.get("body"), str) and parsed["body"].strip():
        return _strip_long_dashes(parsed["body"].strip())
    raise ValueError("Anthropic returned unparseable response for LinkedIn DM after retry")


# --------------------------------------------------------------------------
# Async core
# --------------------------------------------------------------------------


async def _mark_compose_failed(lead_id: str) -> None:
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine) as session:
            lead = await session.get(Lead, uuid.UUID(lead_id))
            if lead is not None:
                lead.compose_status = ComposeStatus.FAILED
                await session.commit()
    finally:
        await engine.dispose()


async def compose_lead_async(lead_id: str) -> dict[str, Any]:
    lid = uuid.UUID(str(lead_id))
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)

    try:
        # Load lead, campaign, style corrections.  Snapshot the strings we
        # need so we don't hold the session open across the API call.
        async with AsyncSession(engine, expire_on_commit=False) as session:
            lead = await session.get(Lead, lid)
            if lead is None:
                return {"status": "not_found"}
            campaign = await session.get(Campaign, lead.campaign_id)
            if campaign is None:
                return {"status": "not_found"}

            # Cooperative stop check — bail BEFORE the (expensive) Anthropic
            # call when the campaign's stop flag is set.  Covers the
            # redelivery window the SIGKILL can't (visibility_timeout=300s).
            if _redis_sync is not None:
                try:
                    r = _redis_sync.Redis.from_url(celery_app.conf.broker_url)
                    if stop_requested(r, lead.campaign_id):
                        return {"status": "stopped", "reason": "campaign_stopped"}
                except Exception:  # noqa: BLE001
                    pass

            # If this campaign's first touch isn't the legacy composed email
            # (the sequence starts with a LinkedIn / wait / etc. node), there
            # is no first email to write.  Skip the AI call and the send
            # entirely — the sequencer drives the first action.  Research
            # already ran, so AI-composed LinkedIn DMs stay personalized.
            if not await campaign_sends_legacy_first_email(session, campaign.id):
                lead.composed_subject = None
                lead.composed_body = None
                lead.compose_status = ComposeStatus.DONE
                await session.commit()
                return {"status": "skipped_non_email_entry"}

            sc_rows = (await session.execute(
                select(StyleCorrection.corrected_body)
                .where(StyleCorrection.campaign_id == campaign.id)
                .order_by(StyleCorrection.created_at.asc())
            )).scalars().all()
            corrected_examples = list(sc_rows)

            # Reply-driven winning block (Feature A) — cache-read only,
            # no LLM call; None until the campaign has positive replies.
            winning_block = await copy_insights.build_winning_block(
                session, campaign.id,
            )

            quality = (lead.research_data or {}).get("quality", "low")
            mode = campaign.research_mode
            ctx = {
                "first_name": lead.first_name or "",
                "last_name": lead.last_name or "",
                "company": lead.company or "",
                "company_website": lead.company_website or "",
                "job_title": lead.job_title or "",
                "research_data": lead.research_data or {},
                "goal": campaign.goal,
                "tone": campaign.tone,
                "sender_name": campaign.sender_name,
            }
            # Snapshot template inputs while the lead row is loaded so the
            # render (below) doesn't touch the closed session.
            template_subject = campaign.template_subject
            template_body = campaign.template_body
            # Per-campaign override if set, else the connected account's
            # Settings signature.
            signature = await resolve_campaign_signature(session, campaign)
            merge_ctx = build_merge_context(lead) if mode == ResearchMode.TEMPLATE else {}
            is_sample = lead.is_sample
            campaign_status_now = campaign.status

            lead.compose_status = ComposeStatus.RUNNING
            await session.commit()

        if mode == ResearchMode.TEMPLATE:
            # Fully-templated path: render merge fields, no Anthropic call,
            # no em-dash sanitiser (the copy is the user's own verbatim text).
            subject_clean = render_template(template_subject, merge_ctx)
            body_clean = render_template(template_body, merge_ctx)
        else:
            # Build prompt + call Anthropic.
            retarget_context = (ctx["research_data"] or {}).get("retarget_context")
            if retarget_context:
                # Re-engagement: reference the email/link they engaged with.
                system_prompt = _build_retarget_prompt(
                    ctx["goal"], ctx["tone"], ctx["sender_name"],
                    ctx["first_name"], ctx["last_name"], ctx["company"],
                    retarget_context, winning_block=winning_block,
                )
            elif quality == "low":
                system_prompt = _build_generic_prompt(
                    ctx["goal"], ctx["tone"], ctx["sender_name"],
                    ctx["first_name"], ctx["last_name"], ctx["company"],
                    ctx["company_website"],
                    winning_block=winning_block,
                )
            else:
                system_prompt = _build_personalized_prompt(
                    ctx["goal"], ctx["tone"], ctx["sender_name"],
                    ctx["first_name"], ctx["last_name"], ctx["company"], ctx["job_title"],
                    ctx["research_data"], corrected_examples,
                    ctx["company_website"],
                    winning_block=winning_block,
                )

            try:
                composed = await _generate_email(system_prompt)
            except ValueError as e:
                # Parse failure — retrying won't help. Mark failed and return.
                logger.warning("compose_lead parse-failed for %s: %s", lead_id, e)
                await _mark_compose_failed(str(lid))
                return {"status": "parse_failed", "error": str(e)}

            # Sanitise stray em/en dashes (the prompt forbids them but models
            # occasionally slip).  No unsubscribe footer — these are individual
            # person-to-person messages, not bulk transactional mail.
            subject_clean = _strip_long_dashes(composed["subject"])
            body_clean = _strip_long_dashes(composed["body"])
            # Swap the AI's sign-off for the campaign signature (contact info,
            # website, calendar link).  Template mode is the user's verbatim
            # copy, so it's left alone.
            body_clean = apply_signature(body_clean, signature)

        # Re-open session to persist the result.
        async with AsyncSession(engine, expire_on_commit=False) as session:
            lead = await session.get(Lead, lid)
            if lead is None:
                return {"status": "not_found"}
            lead.composed_subject = subject_clean
            lead.composed_body = body_clean
            lead.compose_status = ComposeStatus.DONE
            await session.commit()
    finally:
        await engine.dispose()

    # Enqueue send only for non-sample leads on a running campaign.
    send_enqueued = False
    if not is_sample and campaign_status_now == CampaignStatus.RUNNING:
        send_lead.delay(str(lid))
        send_enqueued = True

    return {
        "status": "done",
        "quality": quality,
        "send_enqueued": send_enqueued,
    }


# --------------------------------------------------------------------------
# Celery task wrapper
# --------------------------------------------------------------------------


@celery_app.task(bind=True, name="compose.compose_lead", max_retries=2)
def compose_lead(self, lead_id: str) -> dict[str, Any]:  # noqa: D401
    try:
        return asyncio.run(with_record_tenant(Lead, lead_id, compose_lead_async, lead_id))
    except Exception as exc:  # noqa: BLE001
        logger.exception("compose_lead failed for %s", lead_id)
        try:
            raise self.retry(exc=exc, countdown=60 * (2**self.request.retries))
        except self.MaxRetriesExceededError:
            asyncio.run(with_record_tenant(Lead, lead_id, _mark_compose_failed, lead_id))
            return {"status": "failed", "error": str(exc)}
