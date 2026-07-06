"""Score a discovered LinkedIn post for buying intent + draft suggested
copy (comment, connection request, follow-up).

One Anthropic call, Haiku — runs per-post and can hit dozens of posts
per search run, so cost matters more than the marginal quality lift of
Sonnet.

Output is STRICT JSON matching ``QualificationResult``.  The comment is
post-truncated at 500 chars on a sentence boundary in case the model
overshoots.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.models import SocialOpportunityAction, SocialOpportunityCategory
from app.services._anthropic import extract_text, get_client, parse_json_array, parse_json_object
from app.services.tenant_keys import ambient_api_key
from app.services.compose_client import _truncate_at_sentence

logger = logging.getLogger(__name__)


_VALID_CATEGORIES = {m.value for m in SocialOpportunityCategory}
_VALID_ACTIONS = {m.value for m in SocialOpportunityAction}


@dataclass
class QualificationResult:
    score: int  # 1-10
    category: str  # SocialOpportunityCategory value
    buying_signal: bool
    pain_summary: str
    qualification_reason: str
    suggested_comment: str
    suggested_connection_request: str
    suggested_follow_up: str
    recommended_action: str  # SocialOpportunityAction value


# Shared scoring rubric used by BOTH the single-post and batch prompts.
# Most posts are NOT buying signals.  We want strict, harsh scoring so
# the user's queue stays small and signal-dense.
_RUBRIC = """You are screening posts for CSuite Code (an INDEPENDENT \
technology advisor — NOT a vendor).  Goal: surface ONLY posts that \
signal a real B2B BUYING DECISION on the way.  Default to "ignore"; \
score 7+ ONLY when the post clearly meets the buying-signal checklist.

═══════════════════════════════════════════════════
WHAT COUNTS AS A REAL BUYING SIGNAL (score ≥ 5)
═══════════════════════════════════════════════════
The post must do AT LEAST TWO of these:
  1. NAME A SPECIFIC VENDOR/PRODUCT they're using, leaving, or evaluating
     (e.g. "RingCentral", "Comcast Business", "Salesforce", "Blackbaud",
     "our MSP", "our current ISP" with context).
  2. MENTION A BUYING EVENT — contract renewal, RFP, evaluation, migration,
     switching, budget, "shopping for", "looking for recommendations",
     "what alternatives".
  3. POSTER IS A DECISION-MAKER at a BUYING ORG — title like CIO, CTO,
     CISO, IT Director, COO, CFO, Office Manager, IT Manager, Director
     of Ops, founder/owner of a non-tech-vendor business.
  4. TIME-BOUND URGENCY — "renewal next month", "deadline", "before EOY",
     "starting in Q1".

A post that just complains about technology in general WITHOUT a \
specific vendor + buying event is NOT a buying signal.  Score 1-3.

═══════════════════════════════════════════════════
HARD ANTI-PATTERNS — ALWAYS score 1, action=ignore
═══════════════════════════════════════════════════
- CAREER / JOB posts: "what should I do about my ex-employer", "leaving \
my job", "interview advice", "salary negotiation", "rsu vesting", \
employment law, "fired" — NOT a buying signal even if they complain about \
the company's tech.
- VENDOR SELF-PROMOTION: "we built X", "introducing our new product", \
"we help companies with Y", "case study", "here's how I improved Z".  \
The poster is SELLING, not buying.
- BLOG / LISTICLE / ARTICLE content: "Top 5 alternatives to X", "Hidden \
costs of Y", "How to fix Z" — this is content marketing, not a real \
person posting their pain.  The clue: third-person framing, polished \
copy, no first-person ownership of the problem.
- FOR-SALE listings: "[FOR SALE]", "selling my business", "platform for \
sale", "acquisition opportunity".  They're DIVESTING, not buying tech.
- NEWS / COMMENTARY: industry analysis, "thoughts on this trend", \
opinion essays about tech in general.
- OFF-TOPIC: humanitarian aid, hobbies, personal life, non-business \
problems — even if the word "msp" or "voip" appears in passing.
- RECRUITING POSTS: "we're hiring", "looking for engineers", \
"open roles".
- GENERIC RANTS without a vendor: "everything is broken", "tech sucks", \
no specifics.

═══════════════════════════════════════════════════
SCORING SCALE (be strict)
═══════════════════════════════════════════════════
  10 — Decision-maker, names specific vendor + buying event + urgency
   8 — Decision-maker, names specific vendor + buying event (no urgency)
   6 — Names vendor + buying event but unclear title
   5 — Strong intent verb (shopping / evaluating / switching) but \
generic — no vendor named
   3 — Generic complaint, no buying signal, but in a relevant category
   1 — Anti-pattern (career / vendor-promo / listicle / for-sale / \
off-topic)

``buying_signal`` should be ``true`` ONLY when score ≥ 5.

═══════════════════════════════════════════════════
CATEGORY (exact enum string)
═══════════════════════════════════════════════════
  ucaas_phone | internet | cybersecurity | cloud | msp | crm_software \
| nonprofit_tech | general_advisory | not_relevant

Use ``not_relevant`` for ALL anti-patterns above.

═══════════════════════════════════════════════════
RECOMMENDED ACTION
═══════════════════════════════════════════════════
  ignore           — score < 5 OR matches an anti-pattern (DEFAULT)
  comment          — score 5-7, public comment adds genuine value
  connect          — score 8-10, skip the public comment, DM-first
  research_further — score 6+ but the title/company isn't visible

═══════════════════════════════════════════════════
SUGGESTED COPY (only fill these meaningfully if action ≠ ignore;
otherwise leave them empty strings)
═══════════════════════════════════════════════════
- suggested_comment — Helpful, NOT salesy.  Position {sender} as an \
independent advisor.  Add empathy + 1-2 sentences of perspective or a \
question that helps them think.  NO "book a call" / "DM me" / phone \
number / vendor pitch.  Tone: {tone}.  Under 500 characters.
- suggested_connection_request — 1-2 sentence note that references their \
post.  No pitch.  Under 280 characters.
- suggested_follow_up — Sent AFTER they accept the connection.  Helpful, \
specific to their post, opens space for them to share more.  Under 600 \
characters.
"""


_RESPONSE_SHAPE = """Return STRICT JSON only (no markdown fence, no prose):
{{
  "score": <int 1-10>,
  "category": "...",
  "buying_signal": true|false,
  "pain_summary": "1-sentence factual summary of what's in the post",
  "qualification_reason": "1-2 sentences: WHICH checklist items hit, OR which anti-pattern matched",
  "suggested_comment": "...",
  "suggested_connection_request": "...",
  "suggested_follow_up": "...",
  "recommended_action": "ignore|comment|connect|research_further"
}}"""


PROMPT_TEMPLATE = _RUBRIC + "\n" + _RESPONSE_SHAPE + """

POST TO SCORE:
  author: {author}
  headline: {headline}
  company: {company}
  text: {post_text}
"""


def _clamp_score(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return max(1, min(10, n)) if n else 0


def _coerce_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _normalise_category(value: Any) -> str:
    if isinstance(value, str) and value.strip().lower() in _VALID_CATEGORIES:
        return value.strip().lower()
    return SocialOpportunityCategory.GENERAL_ADVISORY.value


def _normalise_action(value: Any) -> str:
    if isinstance(value, str) and value.strip().lower() in _VALID_ACTIONS:
        return value.strip().lower()
    return SocialOpportunityAction.IGNORE.value


BATCH_PROMPT_TEMPLATE = _RUBRIC + """

═══════════════════════════════════════════════════
BATCH I/O
═══════════════════════════════════════════════════
You will score MULTIPLE posts in one pass.  Apply the same checklist
and anti-patterns to EACH post independently.  Most posts in the batch
should score 1-3 with action=ignore — that's expected.

Return STRICT JSON only — an ARRAY of:
  {{"score", "category", "buying_signal", "pain_summary",
   "qualification_reason", "suggested_comment",
   "suggested_connection_request", "suggested_follow_up",
   "recommended_action"}}

The array MUST be in the SAME ORDER as the posts below.  Same number
of entries in as out.  No markdown fence, no prose, no preamble.

POSTS:
{posts_block}
"""


def _format_one_post(idx: int, payload: dict) -> str:
    return (
        f"--- POST #{idx + 1} ---\n"
        f"author: {payload.get('author_name') or '(unknown)'}\n"
        f"headline: {payload.get('author_headline') or '(unknown)'}\n"
        f"company: {payload.get('company_name') or '(unknown)'}\n"
        f"text: {(payload.get('post_text') or '').strip()[:3000]}\n"
    )


def _build_result(parsed: dict[str, Any]) -> QualificationResult:
    comment = _coerce_str(parsed.get("suggested_comment"))
    if len(comment) > 500:
        comment = _truncate_at_sentence(comment, 500)
    connect = _coerce_str(parsed.get("suggested_connection_request"))
    if len(connect) > 280:
        connect = _truncate_at_sentence(connect, 280)
    follow_up = _coerce_str(parsed.get("suggested_follow_up"))
    if len(follow_up) > 600:
        follow_up = _truncate_at_sentence(follow_up, 600)
    return QualificationResult(
        score=_clamp_score(parsed.get("score")),
        category=_normalise_category(parsed.get("category")),
        buying_signal=bool(parsed.get("buying_signal")),
        pain_summary=_coerce_str(parsed.get("pain_summary"))[:1000],
        qualification_reason=_coerce_str(parsed.get("qualification_reason"))[:1500],
        suggested_comment=comment,
        suggested_connection_request=connect,
        suggested_follow_up=follow_up,
        recommended_action=_normalise_action(parsed.get("recommended_action")),
    )


async def qualify_posts(
    posts: list[dict[str, Any]],
    *,
    search_tone: str = "helpful",
    sender_name: str | None = None,
) -> list[QualificationResult | None]:
    """Batch-qualify up to ~10 posts in ONE Anthropic call.  Each input
    dict shape mirrors ``qualify_post`` kwargs (post_text, author_name,
    author_headline, company_name).  Returns a list aligned with the
    input — ``None`` for any slot the model couldn't qualify.

    Saves ~70% on qualification cost: instead of N Anthropic calls of
    ~$0.005 each, we make 1 call of ~$0.01-0.015 that produces N
    results."""
    if not posts:
        return []
    if not settings.ANTHROPIC_API_KEY:
        return [None] * len(posts)

    # Filter out entries with no text — keep alignment by tracking
    # original indices so we can re-pad the output later.
    indexed = [(i, p) for i, p in enumerate(posts) if (p.get("post_text") or "").strip()]
    out: list[QualificationResult | None] = [None] * len(posts)
    if not indexed:
        return out

    posts_block = "\n".join(
        _format_one_post(local_i, p) for local_i, (_, p) in enumerate(indexed)
    )
    prompt = BATCH_PROMPT_TEMPLATE.format(
        sender=sender_name or "the advisor",
        tone=search_tone or "helpful",
        posts_block=posts_block,
    )

    try:
        message = await get_client(await ambient_api_key("anthropic")).messages.create(
            model=settings.ANTHROPIC_RESEARCH_MODEL,
            # Output scales with batch size — 250 tokens of JSON per
            # post is comfortable, so 10 posts × 250 ≈ 2500.  Bump
            # slightly above that for safety.
            max_tokens=max(2000, len(indexed) * 280),
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("qualify_posts Anthropic call failed: %s", e)
        return out

    parsed = parse_json_array(extract_text(message))
    if not isinstance(parsed, list):
        return out
    for local_i, item in enumerate(parsed):
        if local_i >= len(indexed):
            break
        if not isinstance(item, dict):
            continue
        original_idx, _ = indexed[local_i]
        try:
            out[original_idx] = _build_result(item)
        except Exception as e:  # noqa: BLE001
            logger.warning("qualify_posts: failed to build result #%s: %s", local_i, e)
            out[original_idx] = None
    return out


async def qualify_post(
    *,
    post_text: str,
    author_name: str | None = None,
    author_headline: str | None = None,
    company_name: str | None = None,
    search_tone: str = "helpful",
    sender_name: str | None = None,
) -> QualificationResult | None:
    """Run the LLM qualifier.  Returns ``None`` on Anthropic failure or
    malformed JSON — caller skips writing an opportunity row in that case
    so the post can be re-qualified later."""
    if not settings.ANTHROPIC_API_KEY:
        return None
    if not (post_text or "").strip():
        return None

    prompt = PROMPT_TEMPLATE.format(
        sender=sender_name or "the advisor",
        tone=search_tone or "helpful",
        author=author_name or "(unknown)",
        headline=author_headline or "(unknown)",
        company=company_name or "(unknown)",
        post_text=post_text.strip()[:4000],
    )

    try:
        message = await get_client(await ambient_api_key("anthropic")).messages.create(
            model=settings.ANTHROPIC_RESEARCH_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("qualify_post Anthropic call failed: %s", e)
        return None

    parsed = parse_json_object(extract_text(message))
    if not parsed:
        return None

    comment = _coerce_str(parsed.get("suggested_comment"))
    if len(comment) > 500:
        comment = _truncate_at_sentence(comment, 500)

    connect = _coerce_str(parsed.get("suggested_connection_request"))
    if len(connect) > 280:
        connect = _truncate_at_sentence(connect, 280)

    follow_up = _coerce_str(parsed.get("suggested_follow_up"))
    if len(follow_up) > 600:
        follow_up = _truncate_at_sentence(follow_up, 600)

    return QualificationResult(
        score=_clamp_score(parsed.get("score")),
        category=_normalise_category(parsed.get("category")),
        buying_signal=bool(parsed.get("buying_signal")),
        pain_summary=_coerce_str(parsed.get("pain_summary"))[:1000],
        qualification_reason=_coerce_str(parsed.get("qualification_reason"))[:1500],
        suggested_comment=comment,
        suggested_connection_request=connect,
        suggested_follow_up=follow_up,
        recommended_action=_normalise_action(parsed.get("recommended_action")),
    )
