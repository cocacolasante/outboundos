"""Sequence scheduler + step handlers.

Two Celery tasks live here:

- ``advance_sequences``  — beat-scheduled (every 60s). Finds lead_sequence_state
  rows whose next_run_at has arrived and either executes the wait/advances the
  cursor for wait nodes, or dispatches a channel handler for action nodes.

- ``send_email_step``    — channel handler for an `email` follow-up node.
  Records an execution row, sends via Brevo using the node's config, then
  advances the lead's state cursor.

The FIRST email step of any sequence is intentionally NOT routed through this
file: it stays on the legacy ``compose → send_lead`` path so the existing test
suite + every in-flight campaign keep working. When we pick up a lead sitting
on the entry node and its ``lead.send_status`` is already SENT, we skip it and
advance.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import redis.asyncio as aioredis

from app.config import settings
from app.models import (
    Campaign,
    CampaignStatus,
    EmailEvent,
    EmailEventType,
    Lead,
    LeadSequenceState,
    LeadSequenceStatus,
    LeadStepExecution,
    LeadStepResult,
    LinkedInAccount,
    LinkedInAccountStatus,
    LinkedInConnectionStatus,
    SendStatus,
    Sequence,
    SequenceEdge,
    SequenceNode,
    SequenceNodeKind,
    Suppression,
)
from app.services import brevo
from app.services.signature import render_email_with_signature, resolve_campaign_signature
from app.services.linkedin import ambient_provider as ambient_linkedin_provider
from app.services.linkedin.base import (
    AccountRestricted,
    ChallengeRequired,
    ProfileRef,
)
from app.services.sequence_conditions import ConditionContext, evaluate
from app.workers.celery_app import celery_app
from app.tenancy.context import for_all_tenants, with_record_tenant
from app.workers.compose import generate_followup_reply, generate_linkedin_dm_text

logger = logging.getLogger(__name__)

# How often the beat task runs. Mirrored in celery_app.beat_schedule.
ADVANCE_INTERVAL_SECONDS = 60

# How many state rows we process per beat tick. Keeps the tick bounded.
ADVANCE_BATCH_SIZE = 200

# Skip statuses we treat as TRANSIENT — the lead stays on the current node
# and we push next_run_at out so the step retries when the underlying issue
# (account challenge, rate-limit cooldown, restriction) is resolved.
# Anything NOT in this set is permanent: the cursor advances like a sent step.
TRANSIENT_SKIP_STATUSES = {"challenged", "restricted", "rate_limited", "transient_error"}

# At-most-once send guard.  An atomic Redis claim keyed on (lead, node) is set
# immediately before the Brevo send so a redelivered / concurrently-dispatched /
# task-retried send can never deliver the same email twice (the DB
# ``_already_executed_ever`` check is a read, and the SENT row isn't written
# until ``_record_execution_and_advance`` runs AFTER the send returns — a real
# race window).  The TTL only needs to outlast any retry/redelivery window;
# the SENT row becomes the permanent lifetime guard within seconds.
_SENT_CLAIM_TTL_SECONDS = 7 * 24 * 3600


def _is_transient_failure(meta: dict[str, Any] | None) -> bool:
    """Whether a failed LinkedIn action is an infrastructure/transient error
    worth retrying (vs a permanent client error that should skip the lead).

    Keys off the HTTP status Unipile returned (surfaced on the ActionResult
    meta as ``http_status``): server errors (5xx), network failures (our
    synthetic status 0), rate-limit (429), and any stray redirect (3xx — we
    follow redirects, but a leftover shouldn't burn the lead) are transient.
    Specific 4xx client errors (invalid recipient, profile locked, already
    invited, ...) are permanent and advance the cursor as before.  An
    unknown/absent status is treated as permanent so we never retry forever
    on something we can't classify.
    """
    status = (meta or {}).get("http_status")
    if not isinstance(status, int):
        return False
    return status == 0 or status == 429 or 300 <= status < 400 or 500 <= status < 600
# LinkedIn cap deferral reasons that auto-pause a campaign when no email work
# is reachable downstream (nothing useful can run until the cap window resets).
# ``daily_cap`` is also an email (Brevo) gate reason, so callers must ALSO
# check the node is a LinkedIn kind before acting on it.
_LI_CAP_PAUSE_REASONS = {"daily_cap", "connect_cap", "dm_cap"}

# Small cushion added to a cap auto-pause's resume time so the beat resumes a
# campaign AFTER its cap counter has actually expired (not racing the exact
# reset instant), avoiding a resume-then-immediately-recap flap.
_CAP_RESUME_BUFFER_SECONDS = 60


def _seconds_until_midnight(tz_name: str | None) -> int:
    """Seconds from now until the next local midnight in ``tz_name``.

    Used as the TTL for the daily LinkedIn caps so they reset on the
    calendar-day boundary in the campaign's timezone (a "new day" lifts the
    cap), instead of a rolling 24h window anchored to the day's first action.
    Falls back to UTC (still a calendar-day reset) on a missing/invalid tz.
    """
    try:
        tz = ZoneInfo(tz_name) if tz_name else timezone.utc
    except Exception:  # noqa: BLE001 — bad tz string → safe default
        tz = timezone.utc
    now_local = datetime.now(tz)
    next_midnight = (now_local + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    secs = int((next_midnight - now_local).total_seconds())
    return secs if 0 < secs <= 86400 else 86400
# How long to wait before retrying a transient skip. 5 min is short enough
# to be responsive after a user fixes their account, long enough not to
# spin every 60s while they're still working on it.
TRANSIENT_RETRY_MINUTES = 5
# Safety valve: if a node keeps transient-skipping during the current visit,
# give up after this many tries and advance the cursor. Otherwise a
# permanently-broken LinkedIn account would hold a lead forever.
# 10 retries × 5 min ≈ 50 minutes of grace.
MAX_TRANSIENT_RETRIES = 10

# When an action step succeeds but none of its outgoing edge conditions are
# currently satisfied (e.g. linkedin_connect fired, lead is INVITED, the
# DM branch is gated by linkedin_connection==connected), we PARK the lead
# on the current node and re-evaluate the edges every EDGE_WAIT_RETRY_MINUTES.
# This makes "connect, then DM after acceptance" the natural pattern users
# expect from outreach platforms.  After MAX_EDGE_WAIT_DAYS the lead is
# halted so a never-accepted invite doesn't hold a lead forever — users
# wanting different timeout routing can add a `days_since_entered_node`
# fallback edge alongside the conditional one.
EDGE_WAIT_RETRY_MINUTES = 30
MAX_EDGE_WAIT_DAYS = 14

# Edge-condition top-level ops that legitimately wait on an asynchronous
# signal flipping from false→true (a webhook arriving, time passing).  If
# an unmatched edge uses one of these we park instead of halting.
# Compound (and/or) and negation (not) edges still halt immediately — the
# canonical "skip on reply" pattern uses `{op: not, child: {op: replied}}`
# and users expect that to halt as soon as a reply lands, not after 14 days.
DEFERRABLE_OPS = {
    "linkedin_connection",
    "replied",
    "opened",
    "clicked",
    "bounced",
    "days_since_entered_node",
}

_LI_REDIS_CLIENT: aioredis.Redis | None = None


def _li_redis() -> aioredis.Redis:
    global _LI_REDIS_CLIENT
    if _LI_REDIS_CLIENT is None:
        _LI_REDIS_CLIENT = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _LI_REDIS_CLIENT


# Lua: reserve the next staggered dispatch slot for a LinkedIn account.
# Returns "0" when the slot is open now (and records this instant as the
# account's last dispatch), otherwise the epoch-seconds timestamp at which to
# park this lead.  Two keys keep the schedule clean across ticks:
#   KEYS[1] last-dispatch — only advances on an actual dispatch.  A lead is
#           dispatch-eligible when now >= last + interval, so a previously
#           parked lead fires when its slot arrives (no push-back).
#   KEYS[2] high-water — the furthest slot handed out so far.  New parks go to
#           high_water + interval, so leads parked across DIFFERENT ticks get
#           DISTINCT slots (the beat runs more often than the interval, so a
#           naive "stored + interval" piles every non-dispatch tick's leads
#           onto the same timestamp).
# Server-side + atomic so two overlapping ticks can't both claim the slot.
_LI_STAGGER_LUA = """
local last = tonumber(redis.call('GET', KEYS[1]))
local hw = tonumber(redis.call('GET', KEYS[2]))
local now = tonumber(ARGV[1])
local interval = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
if last == nil or now >= last + interval then
  redis.call('SET', KEYS[1], now, 'EX', ttl)
  if hw == nil or hw < now then
    redis.call('SET', KEYS[2], now, 'EX', ttl)
  end
  return '0'
else
  local base = now
  if hw ~= nil and hw > base then base = hw end
  local slot = base + interval
  redis.call('SET', KEYS[2], slot, 'EX', ttl)
  return tostring(slot)
end
"""


async def _reserve_li_stagger_slot(account_key: str, now_ts: float) -> float:
    """Per-account dispatch spacing for LinkedIn steps.

    Returns 0.0 when the account's slot is open right now (and atomically
    claims it), otherwise the epoch-seconds timestamp to park the lead until.
    Releases at most one dispatch per ``LINKEDIN_STAGGER_SECONDS`` per account
    and hands each parked lead a distinct slot (one interval apart), so the
    schedule reflects the true one-per-interval cadence even across the many
    beat ticks that fall within a single interval.
    """
    interval = settings.LINKEDIN_STAGGER_SECONDS
    if interval <= 0:
        return 0.0  # staggering disabled
    client = _li_redis()
    last_key = f"li-stagger:{account_key}:last"
    hw_key = f"li-stagger:{account_key}:hw"
    # TTL must comfortably outlast a full queue's worth of future slots; the
    # high-water key is refreshed on every park, so it only expires once the
    # account goes idle (at which point a stale value is harmless).
    ttl = 86400
    res = await client.eval(
        _LI_STAGGER_LUA, 2, last_key, hw_key,
        str(now_ts), str(interval), str(ttl),
    )
    try:
        return float(res)
    except (TypeError, ValueError):
        return 0.0


async def _li_stagger_key(
    session: AsyncSession,
    state: "LeadSequenceState",
    cache: dict[uuid.UUID, str],
) -> str:
    """Resolve the per-account key a lead's LinkedIn steps stagger against.

    Keyed on the campaign's bound LinkedIn account (rate limits are
    per-account), falling back to the campaign id when no account is set so
    staggering still applies.  ``cache`` memoises campaign → key within a
    single tick to avoid refetching the campaign per lead.
    """
    lead = await session.get(Lead, state.lead_id)
    if lead is None:
        return str(state.lead_id)
    cid = lead.campaign_id
    if cid in cache:
        return cache[cid]
    campaign = await session.get(Campaign, cid)
    key = str((campaign and campaign.linkedin_account_id) or cid)
    cache[cid] = key
    return key


# Lua script: check every rate-limit gate AND claim the slot atomically.
# Returns {ok=1} or {ok=0, reason=..., remaining=N} as a Redis array reply.
# Running this server-side means two concurrent Celery ticks can't both
# pass the cap and double-fire — Redis serialises scripts.  We pay the
# slight downside of counting a slot against the cap even when the
# action subsequently fails (network error mid-call); the alternative
# (check-then-bump-after-success) was racy and let us exceed the cap.
_LI_RATE_ACQUIRE_LUA = """
-- KEYS[1] = last-action key
-- KEYS[2] = daily total key, OR empty string for kinds excluded from
--          the total cap (e.g. view_profile — see _li_rate_acquire).
-- KEYS[3] = per-kind subcap key (or empty string)
-- KEYS[4] = per-page month key (or empty string)
-- ARGV[1] = now (epoch seconds, float-string)
-- ARGV[2] = min_delay_seconds
-- ARGV[3] = daily total cap
-- ARGV[4] = per-kind subcap (ignored when KEYS[3] is empty)
-- ARGV[5] = per-page cap (ignored when KEYS[4] is empty)
-- ARGV[6] = last-key TTL seconds
-- ARGV[7] = daily-key TTL seconds (86400)
-- ARGV[8] = monthly-key TTL seconds (30*86400)

local now = tonumber(ARGV[1])
local min_delay = tonumber(ARGV[2])
local last = redis.call('GET', KEYS[1])
if last then
    local elapsed = now - tonumber(last)
    if elapsed < min_delay then
        local remaining = math.floor(min_delay - elapsed) + 1
        return {0, 'min_delay', remaining}
    end
end

-- For cap rejections, also return the TTL of the counter key so the
-- caller can defer to the exact reset time (instead of using the short
-- 5-min transient-retry interval and burning the lead's retry budget
-- before the daily window rolls over).
if KEYS[2] ~= '' then
    local day = tonumber(redis.call('GET', KEYS[2])) or 0
    if day >= tonumber(ARGV[3]) then
        local ttl = redis.call('TTL', KEYS[2])
        return {0, 'daily_cap', ttl}
    end
end

if KEYS[3] ~= '' then
    local sub = tonumber(redis.call('GET', KEYS[3])) or 0
    if sub >= tonumber(ARGV[4]) then
        local ttl = redis.call('TTL', KEYS[3])
        return {0, 'subcap', ttl}
    end
end

if KEYS[4] ~= '' then
    local page = tonumber(redis.call('GET', KEYS[4])) or 0
    if page >= tonumber(ARGV[5]) then
        local ttl = redis.call('TTL', KEYS[4])
        return {0, 'page_invite_cap', ttl}
    end
end

-- All gates passed.  Claim the slot atomically.
redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[6]))
if KEYS[2] ~= '' then
    redis.call('INCR', KEYS[2])
    redis.call('EXPIRE', KEYS[2], tonumber(ARGV[7]), 'NX')
end
if KEYS[3] ~= '' then
    redis.call('INCR', KEYS[3])
    redis.call('EXPIRE', KEYS[3], tonumber(ARGV[7]), 'NX')
end
if KEYS[4] ~= '' then
    redis.call('INCR', KEYS[4])
    redis.call('EXPIRE', KEYS[4], tonumber(ARGV[8]), 'NX')
end
return {1, '', 0}
"""


# Low-risk read kinds that DON'T count as throttled LinkedIn "actions":
# exempt from BOTH the daily total cap AND the dispatch staggering.  A
# profile view is passive (it only surfaces in "who viewed your profile")
# and doesn't move LinkedIn's bot scorer at human-scale volumes, so making
# it wait behind the per-account stagger slot or count against the daily
# cap just slows a ``view_profile → connect`` warm-up for no risk
# reduction.  Other warm-ups (follow_profile, react_to_post) DO still
# count — they generate visible notifications/feed activity LinkedIn
# polices more closely.  The per-action min-delay still applies even to
# views, as an anti-burst floor (viewing 100 profiles in a minute IS
# detectable).
_UNCOUNTED_ACTION_KINDS = frozenset({
    SequenceNodeKind.LINKEDIN_VIEW_PROFILE,
})


async def _li_rate_acquire(
    account: LinkedInAccount,
    kind: SequenceNodeKind | None = None,
    page_id: str | None = None,
    daily_ttl_seconds: int = 86400,
) -> dict[str, Any]:
    """Atomic check-and-bump.  Returns ``{"ok": True}`` when the slot is
    claimed (counters already incremented), otherwise the same reason
    dict shape ``_li_rate_check`` returned so callers don't need to
    change their handling of skip statuses.

    ``daily_ttl_seconds`` is the TTL stamped on the daily-total + per-kind
    cap counters when first created (NX), i.e. when the cap window resets.
    Callers pass seconds-until-local-midnight so "daily" means the calendar
    day in the campaign's timezone; defaults to a rolling 24h.

    Server-side Lua makes the whole gate sequence single-step so two
    concurrent invocations can't both pass when only one slot remains.
    """
    client = _li_redis()
    aid = str(account.id)
    last_key = f"li-rate:{aid}:last"
    # Exempt low-risk reads from the daily-total cap (see _UNCOUNTED_ACTION_KINDS).
    # Empty key = Lua skips the daily-cap GET, the >= check, and the INCR.
    day_key = "" if kind in _UNCOUNTED_ACTION_KINDS else f"li-rate:{aid}:day"

    sub_key = ""
    sub_cap = 0
    if kind == SequenceNodeKind.LINKEDIN_CONNECT:
        sub_key = f"li-rate:{aid}:day:connect"
        sub_cap = settings.LINKEDIN_DAILY_CONNECT_CAP
    elif kind in (SequenceNodeKind.LINKEDIN_DM, SequenceNodeKind.LINKEDIN_INMAIL):
        sub_key = f"li-rate:{aid}:day:dm"
        sub_cap = settings.LINKEDIN_DAILY_DM_CAP

    page_key = ""
    page_cap = 0
    if kind == SequenceNodeKind.LINKEDIN_INVITE_TO_PAGE:
        if not page_id:
            return {"ok": False, "reason": "misconfigured", "error": "page_id missing"}
        page_key = f"li-rate:page:{page_id}:month"
        page_cap = settings.LINKEDIN_MONTHLY_PAGE_INVITE_CAP

    last_ttl = max(settings.LINKEDIN_MIN_ACTION_DELAY_SECONDS + 10, 60)
    result = await client.eval(
        _LI_RATE_ACQUIRE_LUA,
        4,
        last_key, day_key, sub_key, page_key,
        str(time.time()),
        str(settings.LINKEDIN_MIN_ACTION_DELAY_SECONDS),
        str(settings.LINKEDIN_DAILY_ACTION_CAP),
        str(sub_cap),
        str(page_cap),
        str(last_ttl),
        str(daily_ttl_seconds),
        str(60 * 60 * 24 * 30),
    )
    # Redis returns Lua arrays as Python lists.  Decoded with the
    # `decode_responses=True` client, all elements come back as strings
    # (Lua's integer return is auto-stringified by aioredis here).
    ok_raw, reason, third = result[0], result[1], result[2]
    ok = (str(ok_raw) in ("1", "true", "True", "OK"))
    if ok:
        return {"ok": True}
    reason_str = str(reason or "")
    # ``third`` is min_delay's remaining seconds, OR the Redis TTL of the
    # cap-counter key (whichever the Lua hit).  Both are non-negative
    # ints when meaningful; -1 means "no expiry" (shouldn't happen with
    # our keys); -2 means "key not found".
    try:
        third_int = int(third)
    except (TypeError, ValueError):
        third_int = 0
    payload: dict[str, Any] = {"ok": False, "reason": reason_str}
    if reason_str == "min_delay":
        payload["remaining"] = max(third_int, 1)
        payload["error"] = f"under min delay ({payload['remaining']}s remaining)"
    elif reason_str == "daily_cap":
        payload["error"] = (
            f"daily cap reached ({settings.LINKEDIN_DAILY_ACTION_CAP})"
        )
        if third_int > 0:
            payload["retry_in"] = third_int
    elif reason_str == "subcap":
        if kind == SequenceNodeKind.LINKEDIN_CONNECT:
            payload["reason"] = "connect_cap"
            payload["error"] = (
                f"daily connect cap reached ({settings.LINKEDIN_DAILY_CONNECT_CAP})"
            )
        else:
            payload["reason"] = "dm_cap"
            payload["error"] = (
                f"daily DM/InMail cap reached ({settings.LINKEDIN_DAILY_DM_CAP})"
            )
        if third_int > 0:
            payload["retry_in"] = third_int
    elif reason_str == "page_invite_cap":
        payload["error"] = (
            f"monthly page invite cap reached ({settings.LINKEDIN_MONTHLY_PAGE_INVITE_CAP})"
        )
        if third_int > 0:
            payload["retry_in"] = third_int
    return payload


async def _li_rate_check(
    account: LinkedInAccount,
    kind: SequenceNodeKind | None = None,
    page_id: str | None = None,
) -> dict[str, Any]:
    """Per-account daily cap + min-delay + per-kind subcap.

    `kind` lets us layer a stricter subcap on connects / DMs / page invites.
    `page_id` is required when kind=LINKEDIN_INVITE_TO_PAGE — the monthly cap
    is per company page, not per account.

    Returns ``{"ok": True}`` when the action may fire right now, otherwise
    a dict describing the reason — caller writes a `skipped` execution row.
    """
    client = _li_redis()
    aid = str(account.id)

    # 1. Min-delay between any two actions on the same account.
    last_raw = await client.get(f"li-rate:{aid}:last")
    if last_raw:
        try:
            last_ts = float(last_raw)
        except (TypeError, ValueError):
            last_ts = 0.0
        elapsed = time.time() - last_ts
        if elapsed < settings.LINKEDIN_MIN_ACTION_DELAY_SECONDS:
            remaining = int(settings.LINKEDIN_MIN_ACTION_DELAY_SECONDS - elapsed) + 1
            return {
                "ok": False,
                "reason": "min_delay",
                "remaining": remaining,
                "error": f"under min delay ({remaining}s remaining)",
            }

    # 2. Overall daily cap on the account.
    day_raw = await client.get(f"li-rate:{aid}:day")
    day_count = int(day_raw) if day_raw else 0
    if day_count >= settings.LINKEDIN_DAILY_ACTION_CAP:
        return {
            "ok": False,
            "reason": "daily_cap",
            "error": f"daily cap reached ({settings.LINKEDIN_DAILY_ACTION_CAP})",
        }

    # 3. Per-kind subcap (write actions only).
    if kind == SequenceNodeKind.LINKEDIN_CONNECT:
        sub_raw = await client.get(f"li-rate:{aid}:day:connect")
        sub = int(sub_raw) if sub_raw else 0
        if sub >= settings.LINKEDIN_DAILY_CONNECT_CAP:
            return {
                "ok": False, "reason": "connect_cap",
                "error": f"daily connect cap reached ({settings.LINKEDIN_DAILY_CONNECT_CAP})",
            }
    elif kind in (SequenceNodeKind.LINKEDIN_DM, SequenceNodeKind.LINKEDIN_INMAIL):
        # InMail counts against the same cap as DMs — LinkedIn looks at
        # combined outbound messaging volume per account.
        sub_raw = await client.get(f"li-rate:{aid}:day:dm")
        sub = int(sub_raw) if sub_raw else 0
        if sub >= settings.LINKEDIN_DAILY_DM_CAP:
            return {
                "ok": False, "reason": "dm_cap",
                "error": f"daily DM/InMail cap reached ({settings.LINKEDIN_DAILY_DM_CAP})",
            }
    elif kind == SequenceNodeKind.LINKEDIN_INVITE_TO_PAGE:
        if not page_id:
            return {"ok": False, "reason": "misconfigured", "error": "page_id missing"}
        page_raw = await client.get(f"li-rate:page:{page_id}:month")
        page = int(page_raw) if page_raw else 0
        if page >= settings.LINKEDIN_MONTHLY_PAGE_INVITE_CAP:
            return {
                "ok": False, "reason": "page_invite_cap",
                "error": f"monthly page invite cap reached ({settings.LINKEDIN_MONTHLY_PAGE_INVITE_CAP})",
            }

    return {"ok": True}


async def _li_rate_bump(
    account: LinkedInAccount,
    kind: SequenceNodeKind | None = None,
    page_id: str | None = None,
) -> None:
    client = _li_redis()
    aid = str(account.id)
    pipe = client.pipeline()
    pipe.incr(f"li-rate:{aid}:day")
    pipe.expire(f"li-rate:{aid}:day", 86400, nx=True)
    pipe.set(
        f"li-rate:{aid}:last", str(time.time()),
        ex=max(settings.LINKEDIN_MIN_ACTION_DELAY_SECONDS + 10, 60),
    )
    if kind == SequenceNodeKind.LINKEDIN_CONNECT:
        pipe.incr(f"li-rate:{aid}:day:connect")
        pipe.expire(f"li-rate:{aid}:day:connect", 86400, nx=True)
    elif kind in (SequenceNodeKind.LINKEDIN_DM, SequenceNodeKind.LINKEDIN_INMAIL):
        pipe.incr(f"li-rate:{aid}:day:dm")
        pipe.expire(f"li-rate:{aid}:day:dm", 86400, nx=True)
    elif kind == SequenceNodeKind.LINKEDIN_INVITE_TO_PAGE and page_id:
        # 30-day rolling window approximated as fixed-30-day expiry.
        pipe.incr(f"li-rate:page:{page_id}:month")
        pipe.expire(f"li-rate:page:{page_id}:month", 60 * 60 * 24 * 30, nx=True)
    await pipe.execute()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _reply_subject(original_subject: str) -> str:
    """A reply subject: the original prefixed with ``Re:`` (idempotent — an
    already-``Re:``-prefixed subject is returned unchanged so threads don't
    accumulate ``Re: Re:``)."""
    subj = (original_subject or "").strip()
    if not subj:
        return "Re:"
    if subj.lower().startswith("re:"):
        return subj
    return f"Re: {subj}"


def _substitute(template: str, lead: Lead) -> str:
    """Lightweight {{first_name}} / {{last_name}} / {{company}} / {{job_title}}
    substitution. Missing variables resolve to the empty string.
    """
    if not template:
        return ""
    mapping = {
        "first_name": lead.first_name or "",
        "last_name": lead.last_name or "",
        "company": lead.company or "",
        "job_title": lead.job_title or "",
        "email": lead.email or "",
    }
    out = template
    for k, v in mapping.items():
        out = out.replace("{{" + k + "}}", v).replace("{{ " + k + " }}", v)
    return out


async def _build_condition_context(
    session: AsyncSession, lead: Lead, state: LeadSequenceState
) -> ConditionContext:
    """Snapshot lead-level event/state for use by edge condition evaluator."""
    events = (await session.execute(
        select(EmailEvent.event_type, func.max(EmailEvent.occurred_at))
        .where(EmailEvent.lead_id == lead.id)
        .group_by(EmailEvent.event_type)
    )).all()
    by_type: dict[EmailEventType, datetime] = {row[0]: row[1] for row in events}
    return ConditionContext(
        has_replied_email=EmailEventType.REPLIED in by_type,
        last_reply_email_at=by_type.get(EmailEventType.REPLIED),
        has_opened=EmailEventType.OPENED in by_type,
        last_open_at=by_type.get(EmailEventType.OPENED),
        has_clicked=EmailEventType.CLICKED in by_type,
        last_click_at=by_type.get(EmailEventType.CLICKED),
        has_bounced=(
            EmailEventType.HARD_BOUNCE in by_type
            or EmailEventType.SOFT_BOUNCE in by_type
        ),
        linkedin_connection_status=lead.linkedin_connection_status.value
            if isinstance(lead.linkedin_connection_status, LinkedInConnectionStatus)
            else str(lead.linkedin_connection_status),
        has_replied_linkedin=lead.linkedin_last_reply_at is not None,
        last_reply_linkedin_at=lead.linkedin_last_reply_at,
        entered_current_at=state.entered_current_at,
        now=_now(),
    )


async def _already_executed_this_visit(
    session: AsyncSession,
    state: LeadSequenceState,
    node_id: uuid.UUID,
) -> bool:
    """True if a SENT execution row exists for (lead, node) since the lead
    entered this node — i.e. the action already fired and we're parked
    waiting for an outgoing-edge condition to flip true.  Without this
    check the scheduler would re-dispatch the action on every park tick.
    """
    if state.entered_current_at is None:
        return False
    row = await session.scalar(
        select(LeadStepExecution.id)
        .where(
            LeadStepExecution.lead_id == state.lead_id,
            LeadStepExecution.node_id == node_id,
            LeadStepExecution.attempted_at >= state.entered_current_at,
            LeadStepExecution.result == LeadStepResult.SENT,
        )
        .limit(1)
    )
    return row is not None


async def _already_executed_ever(
    session: AsyncSession,
    lead_id: uuid.UUID,
    node_id: uuid.UUID,
) -> bool:
    """True if ANY SENT execution row exists for (lead, node), regardless
    of when the lead entered the node.

    Lifetime per-node idempotency.  Once an action has successfully
    fired for a lead at a node, it must NEVER fire again — not on
    re-enrollment, not on a manual cursor reset, not after a duplicate
    Celery dispatch slipped past the stale-dispatch guard, not when the
    user wires a sequence that loops back to a previously-executed node.

    Applies to every action kind: view_profile, follow, react, connect,
    DM, page-invite, InMail, comment, and follow-up email.  The "send
    the same touch twice in a sequence" pattern must be modelled as TWO
    separate nodes (which is the only way to get distinct execution rows
    + analytics anyway), not by looping back.
    """
    row = await session.scalar(
        select(LeadStepExecution.id)
        .where(
            LeadStepExecution.lead_id == lead_id,
            LeadStepExecution.node_id == node_id,
            LeadStepExecution.result == LeadStepResult.SENT,
        )
        .limit(1)
    )
    return row is not None


async def _arm_next_run(
    session: AsyncSession, state: LeadSequenceState, node: SequenceNode
) -> None:
    """Set next_run_at on the state row to reflect the node we just entered."""
    if node.kind == SequenceNodeKind.WAIT:
        minutes = int(node.config.get("duration_minutes", 0) or 0)
        state.next_run_at = _now() + timedelta(minutes=minutes)
    else:
        # Action nodes: fire ASAP. Scheduler picks up on the next tick.
        state.next_run_at = _now()
    state.entered_current_at = _now()
    state.status = LeadSequenceStatus.ACTIVE
    state.halt_reason = None


async def _advance_cursor(
    session: AsyncSession, state: LeadSequenceState, current_node: SequenceNode
) -> SequenceNode | None:
    """Evaluate outgoing edges of `current_node`, advance state to the matching
    target node (or set status=halted/completed). Returns the new node, or None
    if the lead exited the sequence.
    """
    lead = await session.get(Lead, state.lead_id)
    if lead is None:
        state.status = LeadSequenceStatus.HALTED
        state.halt_reason = "lead missing"
        return None

    edges = (await session.execute(
        select(SequenceEdge)
        .where(SequenceEdge.from_node_id == current_node.id)
        .order_by(SequenceEdge.priority.asc(), SequenceEdge.created_at.asc())
    )).scalars().all()

    if not edges:
        state.current_node_id = None
        state.next_run_at = None
        state.status = LeadSequenceStatus.COMPLETED
        return None

    ctx = await _build_condition_context(session, lead, state)
    for edge in edges:
        if evaluate(edge.condition, ctx):
            if edge.to_node_id is None:
                state.current_node_id = None
                state.next_run_at = None
                state.status = LeadSequenceStatus.COMPLETED
                return None
            next_node = await session.get(SequenceNode, edge.to_node_id)
            if next_node is None:
                state.status = LeadSequenceStatus.HALTED
                state.halt_reason = f"edge points to missing node {edge.to_node_id}"
                return None
            state.current_node_id = next_node.id
            await _arm_next_run(session, state, next_node)
            return next_node

    # No edge matched.  If at least one edge has a deferrable condition
    # (a positive op that flips false→true on an async signal), park the
    # lead on the current node and re-evaluate later — connect→DM and
    # similar flows depend on waiting for an event (the prospect
    # accepting an invite, replying, opening, etc.).  Negations,
    # compounds, and "always" edges fall through to immediate halt.
    deferrable = [e for e in edges if (e.condition or {}).get("op") in DEFERRABLE_OPS]
    if deferrable and state.entered_current_at is not None:
        entered = state.entered_current_at
        if entered.tzinfo is None:
            entered = entered.replace(tzinfo=timezone.utc)
        age = ctx.now - entered
        if age < timedelta(days=MAX_EDGE_WAIT_DAYS):
            state.status = LeadSequenceStatus.ACTIVE
            state.halt_reason = None
            state.next_run_at = ctx.now + timedelta(minutes=EDGE_WAIT_RETRY_MINUTES)
            return None  # parked; caller treats this same as "not advanced"
        state.status = LeadSequenceStatus.HALTED
        state.halt_reason = (
            f"waited {MAX_EDGE_WAIT_DAYS}d for an outgoing edge condition to "
            "match (e.g. connection request never accepted)"
        )
        state.next_run_at = None
        return None

    state.status = LeadSequenceStatus.HALTED
    state.halt_reason = "no outgoing edge matched"
    state.next_run_at = None
    return None


# --------------------------------------------------------------------------
# Email step handler
# --------------------------------------------------------------------------


async def _send_email_step_async(lead_id: str, node_id: str) -> dict[str, Any]:
    """Send a templated email for a non-entry email node.

    Honours the same pre-send gates as the legacy ``send_lead`` (shared
    via ``send.check_send_gates``): suppression list, campaign paused,
    schedule window, Brevo rate limits.  The body comes from the node's
    config rather than ``lead.composed_*``.

    Gate trips return a status the sequencer treats as "deferred" —
    `_record_execution_and_advance` parks the lead on the current node
    and reschedules to the gate's ``retry_at`` (or ``+retry_in`` seconds)
    without burning the transient-retry budget.
    """
    from app.workers import send as _send_mod  # avoid circular import

    lid = uuid.UUID(str(lead_id))
    nid = uuid.UUID(str(node_id))
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    redis_client = _send_mod._new_redis()
    result: dict[str, Any] = {}

    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            lead = await session.get(Lead, lid)
            node = await session.get(SequenceNode, nid)
            if lead is None or node is None:
                return {"status": "not_found"}
            campaign = await session.get(Campaign, lead.campaign_id)
            if campaign is None:
                return {"status": "not_found"}

            # Lifetime per-node idempotency: never re-send the same email
            # node for the same lead.  Catches re-enrollment, duplicate
            # Celery dispatches that slipped past the stale-dispatch
            # guard, and loop-back sequences.  Multi-touch must use
            # separate nodes.
            if await _already_executed_ever(session, lid, nid):
                return {
                    "status": "skipped",
                    "error": "email already sent for this node — skipping duplicate",
                }

            cfg = node.config or {}
            is_reply = node.kind == SequenceNodeKind.EMAIL_REPLY

            # Resolve subject + threading up front so a misconfigured node
            # bails before consuming a send gate.
            in_reply_to: str | None = None
            if is_reply:
                # Reply in-thread to the lead's original campaign email.
                in_reply_to = lead.brevo_message_id
                if not in_reply_to:
                    return {
                        "status": "skipped",
                        "error": "no previous email to reply to (original email not sent)",
                    }
                subject = _reply_subject(lead.composed_subject or "")
                ai_compose = bool(cfg.get("ai_compose"))
                if not ai_compose and not (cfg.get("body_template") or "").strip():
                    return {"status": "misconfigured", "error": "reply node needs body_template or ai_compose"}
            else:
                subject_tpl = cfg.get("subject_template") or ""
                if not subject_tpl or not (cfg.get("body_template") or ""):
                    return {"status": "misconfigured", "error": "email node missing subject_template or body_template"}
                subject = _substitute(subject_tpl, lead)
                ai_compose = False

            gates = await _send_mod.check_send_gates(session, lead, campaign, redis_client)
            sending_domain = gates.get("domain")
            if not gates.get("ok"):
                reason = gates["reason"]
                if reason == "suppressed":
                    # Permanent — advance the cursor (don't re-try forever).
                    return {"status": "suppressed"}
                # paused / scheduled / min_delay / hourly_cap / daily_cap
                deferred: dict[str, Any] = {
                    "status": "deferred",
                    "reason": reason,
                }
                if "retry_at" in gates:
                    deferred["retry_at"] = gates["retry_at"]
                if "retry_in" in gates:
                    deferred["retry_in"] = gates["retry_in"]
                return deferred

            # Snapshot everything the (possibly slow) AI call + send needs so
            # the session can close first.
            manual_body = _substitute(cfg.get("body_template") or "", lead)
            ctx = {
                "to_email": lead.email,
                "to_name": " ".join(filter(None, [lead.first_name, lead.last_name])) or None,
                "subject": subject,
                "sender_name": campaign.sender_name,
                "sender_email": campaign.sender_email,
                "campaign_id": str(campaign.id),
                "lead_id": str(lead.id),
                "in_reply_to": in_reply_to,
                "is_reply": is_reply,
                "ai_compose": ai_compose,
                "ai_prompt": cfg.get("ai_prompt") or "",
                "manual_body": manual_body,
                "goal": campaign.goal,
                "tone": campaign.tone,
                "first_name": lead.first_name or "",
                "last_name": lead.last_name or "",
                "company": lead.company or "",
                "job_title": lead.job_title or "",
                "research_data": lead.research_data or {},
                "original_subject": lead.composed_subject or "",
                "original_body": lead.composed_body or "",
                "signature": await resolve_campaign_signature(session, campaign) if ai_compose else None,
            }
            campaign_snap = campaign

        # Compose the body (AI reply uses the lead's EXISTING research — no
        # new research is triggered here).
        if ctx["is_reply"] and ctx["ai_compose"]:
            body = await generate_followup_reply(
                goal=ctx["goal"], tone=ctx["tone"], sender_name=ctx["sender_name"],
                first_name=ctx["first_name"], last_name=ctx["last_name"],
                company=ctx["company"], job_title=ctx["job_title"],
                research_data=ctx["research_data"],
                original_subject=ctx["original_subject"],
                original_body=ctx["original_body"],
                idea=ctx["ai_prompt"],
            )
        else:
            body = ctx["manual_body"]

        # One shared renderer for EVERY send path (first email, follow-up,
        # reply, one-off): the HTML signature + inserted links render as real
        # HTML, and the plain-text fallback collapses them to "text (url)" with
        # no raw <a> tags.  ``signature`` is None for manual/non-AI nodes, so
        # render_email_with_signature just renders the body verbatim there.
        html_body, text_body = render_email_with_signature(body, ctx["signature"])

        # At-most-once guard: atomically CLAIM (lead, node) before the send.
        # Whoever claims first is the only task that sends — this closes the
        # race the lifetime DB check can't (the SENT row is written only after
        # this function returns, so a redelivered / concurrent / retried send
        # would otherwise slip past it and double-send).
        claim_key = f"seq:emailsent:{lid}:{nid}"
        claimed = await redis_client.set(
            claim_key, "1", nx=True, ex=_SENT_CLAIM_TTL_SECONDS,
        )
        if not claimed:
            return {
                "status": "skipped",
                "error": "email already sent for this node — skipping duplicate",
            }
        try:
            message_id = await brevo.send_email(
                to_email=ctx["to_email"],
                to_name=ctx["to_name"],
                subject=ctx["subject"],
                html_body=html_body,
                text_body=text_body,
                sender_name=ctx["sender_name"],
                sender_email=ctx["sender_email"],
                campaign_id=ctx["campaign_id"],
                lead_id=ctx["lead_id"],
                in_reply_to=ctx["in_reply_to"],
            )
        except Exception:
            # The send did not verifiably succeed — release the claim so a
            # legitimate retry can try again.  We hold the claim only for a
            # confirmed send, so this never drops a real send on a transient
            # pre-send error, and never double-sends after a successful one.
            await redis_client.delete(claim_key)
            raise
        # Bump Brevo rate counters so follow-ups are metered alongside legacy
        # first-email sends.  Best-effort: a counter-bump failure must NOT raise
        # (a raise here would retry the whole task — the send already happened).
        try:
            await _send_mod.increment_rate_counters(
                campaign_snap, redis_client, domain=sending_domain,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "increment_rate_counters failed after email step send (lead=%s) — "
                "send already delivered, not retrying", lid,
            )
        result = {"status": "sent", "message_id": message_id}
    finally:
        await engine.dispose()
        await redis_client.aclose()

    return result


# --------------------------------------------------------------------------
# LinkedIn step handler
# --------------------------------------------------------------------------


LI_KINDS = {
    SequenceNodeKind.LINKEDIN_VIEW_PROFILE,
    SequenceNodeKind.LINKEDIN_FOLLOW_PROFILE,
    SequenceNodeKind.LINKEDIN_REACT_POST,
    SequenceNodeKind.LINKEDIN_CONNECT,
    SequenceNodeKind.LINKEDIN_DM,
    SequenceNodeKind.LINKEDIN_INVITE_TO_PAGE,
    SequenceNodeKind.LINKEDIN_INMAIL,
    SequenceNodeKind.LINKEDIN_COMMENT_POST,
}


async def _send_linkedin_step_async(lead_id: str, node_id: str) -> dict[str, Any]:
    """Dispatch a LinkedIn warm-up action.

    Returns the same shape as ``_send_email_step_async`` — a status dict
    handed to ``_record_execution_and_advance`` which writes a
    `lead_step_executions` row + advances the cursor.
    """
    lid = uuid.UUID(str(lead_id))
    nid = uuid.UUID(str(node_id))
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            lead = await session.get(Lead, lid)
            node = await session.get(SequenceNode, nid)
            if lead is None or node is None:
                return {"status": "not_found"}

            # Stale-dispatch guard.  Celery tasks that orphan during a
            # worker restart re-deliver after ``visibility_timeout``
            # (300 s) carrying their original ``node_id``.  If the
            # lead's cursor has advanced past that node in the meantime
            # the action is stale — fire it now and we double-execute
            # the prior step's API call (a real ghost-view, connect
            # invite, DM, etc.).  Bail before any API call or rate-slot
            # acquisition.  Return a status the Celery wrapper +
            # ``_record_execution_and_advance`` will recognise as a
            # no-op (no execution row written, no cursor change).
            state = await session.scalar(
                select(LeadSequenceState).where(LeadSequenceState.lead_id == lid)
            )
            if state is not None and state.current_node_id != nid:
                logger.info(
                    "send_linkedin_step: stale dispatch for lead=%s node=%s "
                    "(current cursor is %s) — skipping",
                    lid, nid, state.current_node_id,
                )
                return {"status": "stale_dispatch"}

            # Lifetime per-node idempotency: a SENT execution row for
            # (lead, node) means the action already fired successfully
            # in a prior visit (or a prior Celery delivery of the same
            # task).  Don't re-fire — applies uniformly to every action
            # kind: view_profile, follow, react, connect, DM, page-
            # invite, InMail, comment.  Skip-and-advance with a clear
            # reason so the activity log shows the dedup.
            if await _already_executed_ever(session, lid, nid):
                return {
                    "status": "skipped",
                    "error": "action already sent for this node — skipping duplicate",
                }

            # Suppression gate — LinkedIn steps must respect the
            # workspace suppression list the same way email steps do
            # (via check_send_gates).  An ignored / unsubscribed lead
            # gets a permanent skip; without this, re-enrolling a lead
            # whose email is suppressed would resume LinkedIn outreach
            # while email stays blocked — exactly the half-ignored state
            # the Ignore button promises not to leave.
            sup = await session.scalar(
                select(Suppression).where(
                    Suppression.email == (lead.email or "").strip().lower()
                )
            )
            if sup is not None:
                return {
                    "status": "suppressed",
                    "error": "lead email is on the suppression list",
                }

            campaign = await session.get(Campaign, lead.campaign_id)
            if campaign is None or campaign.linkedin_account_id is None:
                return {
                    "status": "misconfigured",
                    "error": "no LinkedIn account configured on campaign",
                }
            account = await session.get(LinkedInAccount, campaign.linkedin_account_id)
            if account is None:
                return {"status": "not_found", "error": "linkedin account missing"}

            # Honour the campaign's schedule window + paused state.  Same
            # treatment as the email path — defer to the next valid
            # moment via ``next_run_at`` instead of burning a LinkedIn
            # rate-limit slot or firing a public action (connect / DM /
            # comment) at 3 AM.  Even no-touch reads like view_profile
            # respect the window so an account that only operates during
            # business hours stays "looks like a human" to LinkedIn's
            # behavioural scorer.
            from app.models import CampaignStatus  # local — avoid circular
            from app.workers.send import compute_next_send_window
            if campaign.status == CampaignStatus.PAUSED:
                return {"status": "deferred", "reason": "paused",
                        "error": "campaign paused"}
            window_eta = compute_next_send_window(campaign)
            if window_eta is not None:
                return {
                    "status": "deferred",
                    "reason": "scheduled",
                    "error": f"outside campaign send window — next opens {window_eta.isoformat()}",
                    "retry_at": window_eta.isoformat(),
                }

            # Skip if account is in a bad state — don't burn the user's
            # attempts when we know it'll fail. Use distinct status values
            # so _record_execution_and_advance can apply transient-retry
            # behavior for challenged/restricted (user can recover the
            # account) vs. just skip for FAILED creds.
            if account.status == LinkedInAccountStatus.CHALLENGED:
                return {"status": "challenged",
                        "error": f"linkedin account status={account.status.value}"}
            if account.status == LinkedInAccountStatus.RESTRICTED:
                return {"status": "restricted",
                        "error": f"linkedin account status={account.status.value}"}
            if account.status == LinkedInAccountStatus.FAILED:
                return {"status": "skipped",
                        "error": f"linkedin account status={account.status.value}"}

            if not lead.linkedin_url:
                return {"status": "skipped", "error": "lead has no linkedin_url"}

            cfg = node.config or {}
            kind = node.kind

            # Connect step short-circuit: never re-send a connect request to
            # a lead who's already INVITED or already a 1st-degree CONNECTED.
            # Two failure modes this avoids:
            # - Sending a second invite to someone who's already accepted →
            #   Unipile returns errors/invalid_recipient and counts against
            #   the daily connect cap for nothing.
            # - Sending a duplicate invite to someone with one pending →
            #   LinkedIn shows them two invites or silently merges; either
            #   way it makes the sender look automated.
            # Skip-and-advance.  A downstream DM gated by
            # ``linkedin_connection==connected`` will park on the deferrable
            # edge until the webhook flips the lead to CONNECTED.
            if kind == SequenceNodeKind.LINKEDIN_CONNECT:
                if lead.linkedin_connection_status == LinkedInConnectionStatus.CONNECTED:
                    return {
                        "status": "skipped",
                        "error": "already 1st-degree connection — no connect request needed",
                    }
                if lead.linkedin_connection_status == LinkedInConnectionStatus.INVITED:
                    return {
                        "status": "skipped",
                        "error": "connect request already sent — waiting on acceptance",
                    }

            # DMs only fire when we know the lead is a 1st-degree connection.
            # If we have an invite OUTSTANDING (status=INVITED), the right
            # move isn't to skip-and-move-on — it's to defer the DM until
            # the prospect accepts.  Otherwise the user wires up a
            # ``connect → DM`` flow and the DM silently drops every time
            # because the cursor advanced before acceptance.  ``deferred``
            # status parks the lead on the DM node, re-checks every
            # EDGE_WAIT_RETRY_MINUTES, and gives up after MAX_EDGE_WAIT_DAYS
            # — the same cadence the edge-parking code uses.
            # UNKNOWN / DECLINED / NONE → skip (no incoming signal to wait
            # for; deferring forever would silently halt the lead).
            if kind == SequenceNodeKind.LINKEDIN_DM:
                if lead.linkedin_connection_status == LinkedInConnectionStatus.INVITED:
                    entered = state.entered_current_at if state else None
                    if entered is not None:
                        if entered.tzinfo is None:
                            entered = entered.replace(tzinfo=timezone.utc)
                        if (datetime.now(timezone.utc) - entered) >= timedelta(days=MAX_EDGE_WAIT_DAYS):
                            return {
                                "status": "skipped",
                                "error": (
                                    f"waited {MAX_EDGE_WAIT_DAYS}d for connect "
                                    "acceptance — giving up on DM"
                                ),
                            }
                    return {
                        "status": "deferred",
                        "reason": "waiting_for_connection",
                        "error": "DM deferred — waiting for connect acceptance",
                        "retry_in": EDGE_WAIT_RETRY_MINUTES * 60,
                    }
                if lead.linkedin_connection_status != LinkedInConnectionStatus.CONNECTED:
                    return {
                        "status": "skipped",
                        "error": "not connected — DM requires 1st degree",
                    }

            # Page invites only work on 1st-degree connections too.
            if kind == SequenceNodeKind.LINKEDIN_INVITE_TO_PAGE:
                if lead.linkedin_connection_status != LinkedInConnectionStatus.CONNECTED:
                    return {
                        "status": "skipped",
                        "error": "not connected — page invite requires 1st degree",
                    }
                if not cfg.get("page_id"):
                    return {"status": "misconfigured", "error": "page_id missing on node"}

            rate_page_id = cfg.get("page_id") if kind == SequenceNodeKind.LINKEDIN_INVITE_TO_PAGE else None
            # Atomic check-AND-bump.  On `ok=True` the daily / subcap /
            # min-delay counters are already incremented server-side so a
            # concurrent tick that races us reads the updated values and
            # gets denied.  Trade-off: a subsequent action failure
            # counts the slot anyway (no refund), bounded by the cap.
            rate = await _li_rate_acquire(
                account, kind=kind, page_id=rate_page_id,
                # Daily caps reset at local midnight in the campaign's tz, so a
                # "new day" lifts the cap (vs a rolling 24h window).
                daily_ttl_seconds=_seconds_until_midnight(campaign.schedule_timezone),
            )
            if not rate.get("ok"):
                reason = rate.get("reason")
                if reason == "min_delay":
                    # Signal the Celery task to reschedule rather than skip.
                    return {"status": "min_delay_retry", "countdown": rate["remaining"]}
                if reason in {"daily_cap", "connect_cap", "dm_cap", "page_invite_cap"}:
                    # Cap-style skips: defer to the exact reset time the
                    # Lua returned via Redis TTL (no retry-budget burn).
                    # MAX_TRANSIENT_RETRIES is calibrated for fast-resolve
                    # issues (account challenge, min-delay) — without
                    # this branch a lead that hit the daily cap mid-day
                    # would burn through 10 × 5 min and silently advance
                    # past the connect step before midnight.
                    deferred: dict[str, Any] = {
                        "status": "deferred",
                        "reason": reason,
                        "error": rate.get("error"),
                    }
                    if rate.get("retry_in"):
                        deferred["retry_in"] = rate["retry_in"]
                    return deferred
                return {"status": "rate_limited", "error": rate.get("error")}

            profile = ProfileRef.from_url(lead.linkedin_url)
            from app.billing.entitlements import Meter, QuotaExceeded, check_quota

            try:
                await check_quota(Meter.LINKEDIN_ACTION)
            except QuotaExceeded as exc:
                # Terminal, visible skip — advances the cursor like any
                # permanent skip; never burns retries on a plan limit.
                return {"status": "skipped", "error": str(exc)}
            provider = await ambient_linkedin_provider()

            try:
                if kind == SequenceNodeKind.LINKEDIN_VIEW_PROFILE:
                    result = await provider.view_profile(account, profile)
                elif kind == SequenceNodeKind.LINKEDIN_FOLLOW_PROFILE:
                    result = await provider.follow_profile(account, profile)
                elif kind == SequenceNodeKind.LINKEDIN_REACT_POST:
                    post_urn = cfg.get("post_urn")
                    if not post_urn:
                        post_urn = await provider.latest_post_urn(account, profile)
                    if not post_urn:
                        return {"status": "skipped", "error": "no post to react to"}
                    reaction = (cfg.get("reaction") or "LIKE").upper()
                    result = await provider.react_to_post(account, post_urn, reaction)
                elif kind == SequenceNodeKind.LINKEDIN_CONNECT:
                    if cfg.get("no_note"):
                        note = None
                    else:
                        note = _substitute(cfg.get("note_template", ""), lead) or None
                        if note is not None and len(note) > 300:
                            note = note[:300]
                    result = await provider.send_connect_request(account, profile, note)
                    if result.ok:
                        # Optimistically mark INVITED — the poller will flip
                        # to CONNECTED once the lead accepts.
                        lead.linkedin_connection_status = LinkedInConnectionStatus.INVITED
                elif kind == SequenceNodeKind.LINKEDIN_DM:
                    if cfg.get("ai_compose"):
                        try:
                            body = await generate_linkedin_dm_text(
                                goal=campaign.goal,
                                tone=campaign.tone,
                                sender_name=campaign.sender_name,
                                first_name=lead.first_name or "",
                                last_name=lead.last_name or "",
                                company=lead.company or "",
                                job_title=lead.job_title or "",
                                research_data=lead.research_data or {},
                                company_website=lead.company_website or "",
                            )
                        except ValueError as e:
                            return {"status": "failed", "error": f"AI DM compose failed: {e}"}
                    else:
                        text_tpl = cfg.get("text_template") or ""
                        if not text_tpl:
                            return {"status": "misconfigured", "error": "text_template missing (or enable ai_compose)"}
                        body = _substitute(text_tpl, lead)
                    result = await provider.send_dm(account, profile, body)
                elif kind == SequenceNodeKind.LINKEDIN_INVITE_TO_PAGE:
                    result = await provider.invite_to_page(
                        account, profile, str(cfg["page_id"])
                    )
                elif kind == SequenceNodeKind.LINKEDIN_INMAIL:
                    subject_tpl = cfg.get("subject_template") or ""
                    body_tpl = cfg.get("body_template") or ""
                    if not subject_tpl or not body_tpl:
                        return {
                            "status": "misconfigured",
                            "error": "InMail requires subject_template and body_template",
                        }
                    subject = _substitute(subject_tpl, lead)
                    body = _substitute(body_tpl, lead)
                    result = await provider.send_inmail(account, profile, subject, body)
                    # Premium-required gets a distinct skip status so the UI
                    # can surface a precise message.
                    if not result.ok and (result.meta or {}).get("premium_required"):
                        return {
                            "status": "skipped",
                            "error": "InMail unavailable — account needs Premium / Sales Nav credits",
                            "meta": result.meta,
                        }
                elif kind == SequenceNodeKind.LINKEDIN_COMMENT_POST:
                    comment_tpl = cfg.get("comment_template") or ""
                    if not comment_tpl:
                        return {
                            "status": "misconfigured",
                            "error": "comment_template missing",
                        }
                    target = cfg.get("target") or "latest"
                    post_urn = cfg.get("post_urn")
                    if not post_urn:
                        # M4: "latest" + "most_engaged" both fall back to
                        # latest_post_urn() — most_engaged would need a
                        # ranked post fetch which isn't on the provider's
                        # surface. Documented as a known limitation.
                        post_urn = await provider.latest_post_urn(account, profile)
                    if not post_urn:
                        return {"status": "skipped", "error": "no post to comment on"}
                    comment_text = _substitute(comment_tpl, lead)
                    result = await provider.comment_on_post(account, post_urn, comment_text)
                else:
                    return {"status": "misconfigured", "error": f"unsupported kind: {kind.value}"}
            except ChallengeRequired as exc:
                account.status = LinkedInAccountStatus.CHALLENGED
                account.pending_challenge_url = exc.challenge_url or "https://www.linkedin.com"
                account.last_error = str(exc)
                await session.commit()
                return {"status": "challenged", "error": str(exc)}
            except AccountRestricted as exc:
                account.status = LinkedInAccountStatus.RESTRICTED
                account.last_error = str(exc)
                await session.commit()
                return {"status": "restricted", "error": str(exc)}

            # Persist refreshed cookies + optimistic INVITED flag.
            await session.commit()

        if result.ok:
            # Slot was already claimed inside the atomic acquire above —
            # no separate bump.
            return {"status": "sent", "external_id": result.external_id, "meta": result.meta}
        # A transient infra failure (5xx / network / stray redirect) parks the
        # lead and retries instead of permanently skipping it; permanent client
        # errors fall through to "failed" (advance the cursor) as before.
        if _is_transient_failure(result.meta):
            return {
                "status": "transient_error",
                "error": result.error or "transient provider error",
                "meta": result.meta,
            }
        return {"status": "failed", "error": result.error or "unknown error"}
    finally:
        await engine.dispose()


async def _has_downstream_email(session: AsyncSession, node: SequenceNode) -> bool:
    """True if a live EMAIL node is reachable downstream from ``node`` via the
    sequence's edges.  Used to decide whether hitting the LinkedIn cap should
    pause the campaign: if email work is still reachable (email isn't subject
    to the LinkedIn cap), keep running; otherwise nothing can progress until
    the cap resets, so pause.
    """
    edges = (await session.execute(
        select(SequenceEdge).where(SequenceEdge.sequence_id == node.sequence_id)
    )).scalars().all()
    adj: dict[uuid.UUID, list[uuid.UUID]] = {}
    for e in edges:
        if e.to_node_id is not None:
            adj.setdefault(e.from_node_id, []).append(e.to_node_id)

    seen: set[uuid.UUID] = {node.id}
    stack = list(adj.get(node.id, []))
    while stack:
        nid = stack.pop()
        if nid in seen:
            continue
        seen.add(nid)
        nxt = await session.get(SequenceNode, nid)
        if nxt is None or nxt.deleted_at is not None:
            continue
        if nxt.kind in (SequenceNodeKind.EMAIL, SequenceNodeKind.EMAIL_REPLY):
            return True
        stack.extend(adj.get(nid, []))
    return False


async def _record_execution_and_advance(
    lead_id: uuid.UUID, node_id: uuid.UUID, result: dict[str, Any]
) -> None:
    """Persist a lead_step_executions row + decide whether to advance the
    state cursor or retry on the same node.

    A transient skip (LinkedIn account challenged/restricted, rate-limited)
    keeps the lead on the current node and pushes ``next_run_at`` out by
    ``TRANSIENT_RETRY_MINUTES`` — so when the user fixes the underlying
    issue the step runs without needing manual re-enrollment. After
    ``MAX_TRANSIENT_RETRIES`` consecutive transient skips on the same
    node-visit we give up and advance like a normal skip; that prevents a
    permanently-broken account from holding a lead forever.
    """
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            status = result.get("status", "failed")
            # Stale dispatch — the upstream handler bailed without firing
            # the action because the cursor had moved on.  We deliberately
            # don't write an execution row (would pollute the audit log
            # with phantom "attempts" that never actually called Unipile)
            # and we don't touch the cursor (the live current_node_id is
            # by definition different from node_id).
            if status == "stale_dispatch":
                logger.info(
                    "record_execution: skipping stale dispatch for "
                    "lead=%s node=%s", lead_id, node_id,
                )
                return
            mapped: LeadStepResult
            if status == "sent":
                mapped = LeadStepResult.SENT
            elif status in {"suppressed", "skipped", "paused", "rate_limited",
                            "misconfigured", "challenged", "restricted",
                            "not_found", "deferred", "transient_error"}:
                mapped = LeadStepResult.SKIPPED
            else:
                mapped = LeadStepResult.FAILED

            # A ``deferred`` gate-trip (paused / outside window / min_delay /
            # hourly_cap / daily_cap) is "try again later", NOT an attempt that
            # was skipped — the step never reached the channel.  Recording it as
            # a SKIPPED execution row made a parked lead look identical to a real
            # skip in the Activity tab AND wrote a fresh noise row on every
            # re-check (every few minutes per parked lead → unbounded
            # lead_step_executions growth, esp. when a follow-up backlog is
            # starved behind the first-email pacer for the shared rate budget).
            # Park + reschedule below WITHOUT writing a row — same as the
            # ``stale_dispatch`` early-return above.  The transient-retry budget
            # (which counts SKIPPED rows since entered_current_at) only applies
            # to TRANSIENT_SKIP_STATUSES, never to deferred, so dropping the row
            # leaves that accounting unchanged.
            if status != "deferred":
                # LinkedIn handlers return external_id; email handler returns
                # message_id. Either is fine here.
                external = result.get("external_id") or result.get("message_id")
                exec_row = LeadStepExecution(
                    lead_id=lead_id,
                    node_id=node_id,
                    result=mapped,
                    external_id=external,
                    external_meta=result.get("meta") if isinstance(result.get("meta"), dict) else None,
                    error=result.get("error"),
                )
                session.add(exec_row)
                # Flush so the count query below sees this row.
                await session.flush()

            state = await session.scalar(
                select(LeadSequenceState).where(LeadSequenceState.lead_id == lead_id)
            )
            if state is not None:
                node = await session.get(SequenceNode, node_id)
                if node is not None and state.current_node_id == node_id:
                    if status == "deferred":
                        # Pre-send gate tripped (suppression already
                        # advances; only paused / scheduled / rate-limited
                        # arrive here).  Park on the current node and
                        # reschedule to the exact retry time the gate
                        # returned, NO budget consumed — a campaign paused
                        # for a day shouldn't burn through retries and
                        # silently advance past the follow-up.
                        retry_at_str = result.get("retry_at")
                        retry_in = result.get("retry_in")
                        if retry_at_str:
                            try:
                                state.next_run_at = datetime.fromisoformat(retry_at_str)
                            except ValueError:
                                state.next_run_at = _now() + timedelta(minutes=5)
                        elif retry_in:
                            state.next_run_at = _now() + timedelta(seconds=int(retry_in))
                        else:
                            # Paused with no eta — recheck every 5 min.
                            state.next_run_at = _now() + timedelta(minutes=5)
                        state.halt_reason = None
                        logger.info(
                            "Deferred step on lead=%s node=%s (%s) — next attempt at %s",
                            lead_id, node_id, result.get("reason"),
                            state.next_run_at.isoformat() if state.next_run_at else "?",
                        )

                        # Auto-pause: a LinkedIn step hit the daily cap and the
                        # lead has no email node reachable downstream — nothing
                        # useful can run until the cap window resets.  Pause the
                        # campaign (the beat auto-resumes it at the reset time
                        # stamped in auto_paused_until).  Gated on the node being
                        # a LinkedIn kind because "daily_cap" is also a Brevo
                        # (email) gate reason.
                        reason = result.get("reason")
                        retry_in = result.get("retry_in")
                        if (
                            node.kind in LI_KINDS
                            and reason in _LI_CAP_PAUSE_REASONS
                            and retry_in
                            and not await _has_downstream_email(session, node)
                        ):
                            seq = await session.get(Sequence, node.sequence_id)
                            campaign = (
                                await session.get(Campaign, seq.campaign_id)
                                if seq is not None else None
                            )
                            if campaign is not None and campaign.status == CampaignStatus.RUNNING:
                                campaign.status = CampaignStatus.PAUSED
                                # Cap-reset is typically local midnight (when
                                # the LinkedIn daily counter expires) + a
                                # 60s cushion to avoid resume-then-recap.
                                cap_reset = _now() + timedelta(
                                    seconds=int(retry_in) + _CAP_RESUME_BUFFER_SECONDS
                                )
                                # Hold the pause until BOTH conditions hold:
                                # the cap window has reset AND the campaign's
                                # schedule window is open.  Without this, the
                                # campaign auto-resumes at midnight but the
                                # schedule window is closed — every lead in
                                # the beat tick defers individually back to
                                # window-open, churning the queue and dirtying
                                # last_run_at metrics.  Evaluating
                                # compute_next_send_window AT cap_reset gives
                                # the right answer regardless of whether the
                                # cap resets inside or outside the window.
                                from app.workers.send import compute_next_send_window
                                next_window = compute_next_send_window(
                                    campaign, now=cap_reset,
                                )
                                campaign.auto_paused_until = (
                                    max(cap_reset, next_window)
                                    if next_window is not None
                                    else cap_reset
                                )
                                logger.info(
                                    "Auto-paused campaign %s at LinkedIn %s cap; "
                                    "auto-resumes ~%s (cap_reset=%s, "
                                    "next_window=%s)",
                                    campaign.id, reason,
                                    campaign.auto_paused_until.isoformat(),
                                    cap_reset.isoformat(),
                                    next_window.isoformat() if next_window else "now",
                                )
                    elif status == "suppressed":
                        # The lead's email is on the suppression list —
                        # the WHOLE sequence is moot for them, not just
                        # this node.  Halt outright instead of advancing
                        # node-by-node with a skip row per step (which
                        # is what the generic skip branch would do).
                        state.status = LeadSequenceStatus.HALTED
                        state.halt_reason = "email is on the suppression list"
                        state.next_run_at = None
                        logger.info(
                            "Halting lead=%s — suppressed email; sequence stopped",
                            lead_id,
                        )
                    elif status in TRANSIENT_SKIP_STATUSES:
                        # Count prior skips for THIS visit only (since we
                        # entered the node). Re-enrollment resets
                        # entered_current_at, giving the lead a fresh budget.
                        attempt_filter = [
                            LeadStepExecution.lead_id == lead_id,
                            LeadStepExecution.node_id == node_id,
                            LeadStepExecution.result == LeadStepResult.SKIPPED,
                        ]
                        if state.entered_current_at is not None:
                            attempt_filter.append(
                                LeadStepExecution.attempted_at >= state.entered_current_at,
                            )
                        retry_count = (await session.scalar(
                            select(func.count())
                            .select_from(LeadStepExecution)
                            .where(*attempt_filter)
                        )) or 0

                        if retry_count < MAX_TRANSIENT_RETRIES:
                            # Stay on this node; push next_run_at out.
                            state.next_run_at = _now() + timedelta(
                                minutes=TRANSIENT_RETRY_MINUTES,
                            )
                            state.halt_reason = None
                            logger.info(
                                "Transient skip on lead=%s node=%s (%s) — retry %d/%d in %dm",
                                lead_id, node_id, status, retry_count,
                                MAX_TRANSIENT_RETRIES, TRANSIENT_RETRY_MINUTES,
                            )
                        else:
                            logger.warning(
                                "Lead=%s exhausted %d transient retries on node=%s "
                                "(last status=%s) — advancing past it",
                                lead_id, MAX_TRANSIENT_RETRIES, node_id, status,
                            )
                            await _advance_cursor(session, state, node)
                    else:
                        if status == "sent" and exec_row is not None:
                            # Anchor the inter-step wait to THIS send, not the
                            # (possibly stale) node-entry time.  A delayed or
                            # re-queued send — whose entered_current_at can be
                            # days old — would otherwise find the next
                            # days_since_entered_node gate ALREADY satisfied and
                            # fire the following email seconds later, collapsing
                            # the spacing (two emails minutes apart instead of the
                            # configured days).
                            #
                            # Use the execution row's OWN timestamp (not _now())
                            # so it EQUALS entered_current_at — otherwise the
                            # "already executed this visit" guard
                            # (attempted_at >= entered_current_at) would exclude
                            # the just-written SENT row and a parked lead would
                            # re-fire the action.
                            state.entered_current_at = exec_row.attempted_at or _now()
                        await _advance_cursor(session, state, node)
            await session.commit()
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------
# Beat: advance_sequences
# --------------------------------------------------------------------------


async def _advance_sequences_async() -> dict[str, int]:
    """Pull ready state rows and either advance them (wait/already-sent entry)
    or dispatch the appropriate channel task.
    """
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    counts = {
        "advanced_wait": 0,
        "advanced_entry_done": 0,
        "dispatched_email": 0,
        "dispatched_linkedin": 0,
        "staggered_linkedin": 0,
        "halted_unsupported": 0,
    }
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            now = _now()
            # Auto-resume campaigns that were auto-paused at the LinkedIn cap,
            # now that their cap window has reset.  Manual pauses have
            # auto_paused_until = NULL and are left alone.  Runs in this same
            # transaction so the SELECT below picks up the just-resumed leads.
            await session.execute(
                update(Campaign)
                .where(
                    Campaign.status == CampaignStatus.PAUSED,
                    Campaign.auto_paused_until.is_not(None),
                    Campaign.auto_paused_until <= now,
                )
                .values(status=CampaignStatus.RUNNING, auto_paused_until=None)
            )
            # Only advance leads whose campaign is RUNNING.  A paused campaign
            # freezes in place: its leads keep their current node + next_run_at
            # untouched, so the beat doesn't churn them (and doesn't burn the
            # account's stagger slots, which would otherwise starve running
            # campaigns that share the LinkedIn account).  Draft/previewing/
            # complete campaigns aren't driven by the sequencer either.
            # ``of=LeadSequenceState`` scopes the row lock to the state rows so
            # the join to campaigns/sequences doesn't lock those.
            rows = (await session.execute(
                select(LeadSequenceState)
                .join(Sequence, Sequence.id == LeadSequenceState.sequence_id)
                .join(Campaign, Campaign.id == Sequence.campaign_id)
                .where(
                    LeadSequenceState.status == LeadSequenceStatus.ACTIVE,
                    LeadSequenceState.next_run_at.is_not(None),
                    LeadSequenceState.next_run_at <= now,
                    LeadSequenceState.current_node_id.is_not(None),
                    Campaign.status == CampaignStatus.RUNNING,
                )
                .order_by(LeadSequenceState.next_run_at.asc())
                .limit(ADVANCE_BATCH_SIZE)
                .with_for_update(skip_locked=True, of=LeadSequenceState)
            )).scalars().all()

            email_dispatch: list[tuple[str, str]] = []
            linkedin_dispatch: list[tuple[str, str]] = []
            # Memoises campaign -> stagger key for this tick.
            li_stagger_cache: dict[uuid.UUID, str] = {}

            for state in rows:
                node = await session.get(SequenceNode, state.current_node_id)
                if node is None:
                    state.status = LeadSequenceStatus.HALTED
                    state.halt_reason = "current node missing"
                    state.next_run_at = None
                    continue
                if node.deleted_at is not None:
                    # Graph was re-edited and this node was retired. Halt
                    # rather than crash; the user can rebuild the campaign
                    # if they want these leads to continue.
                    state.status = LeadSequenceStatus.HALTED
                    state.halt_reason = "current node deleted (sequence was rebuilt)"
                    state.next_run_at = None
                    continue

                # Wait: just advance.
                if node.kind == SequenceNodeKind.WAIT:
                    await _advance_cursor(session, state, node)
                    counts["advanced_wait"] += 1
                    continue

                # Email entry node: if the legacy pipeline already sent it,
                # advance straight through.
                if node.kind == SequenceNodeKind.EMAIL and node.is_entry:
                    cfg = node.config or {}
                    if cfg.get("use_campaign_compose"):
                        lead = await session.get(Lead, state.lead_id)
                        if lead is not None and lead.send_status == SendStatus.SENT:
                            await _advance_cursor(session, state, node)
                            counts["advanced_entry_done"] += 1
                        else:
                            # Push ourselves out so we don't busy-spin while
                            # the legacy pipeline catches up.
                            state.next_run_at = now + timedelta(minutes=5)
                        continue

                # Follow-up email node (new thread OR in-thread reply):
                # dispatch unless already sent during this visit (parked,
                # waiting for an edge condition).
                if node.kind in (SequenceNodeKind.EMAIL, SequenceNodeKind.EMAIL_REPLY):
                    if await _already_executed_this_visit(session, state, node.id):
                        await _advance_cursor(session, state, node)
                        counts["reevaluated_parked"] = counts.get("reevaluated_parked", 0) + 1
                        continue
                    email_dispatch.append((str(state.lead_id), str(node.id)))
                    state.next_run_at = now + timedelta(minutes=10)
                    counts["dispatched_email"] += 1
                    continue

                # M2: LinkedIn warm-up kinds.
                if node.kind in LI_KINDS:
                    if await _already_executed_this_visit(session, state, node.id):
                        await _advance_cursor(session, state, node)
                        counts["reevaluated_parked"] = counts.get("reevaluated_parked", 0) + 1
                        continue
                    # Stagger: release at most one LinkedIn step per account
                    # per LINKEDIN_STAGGER_SECONDS so a campaign's leads go out
                    # one at a time (lead, wait, next lead) rather than as a
                    # burst.  When the slot isn't open yet, park this lead
                    # until it is without dispatching.  Low-risk reads (profile
                    # views) are exempt — they don't count as throttled actions,
                    # so they fire promptly without consuming a stagger slot.
                    if node.kind not in _UNCOUNTED_ACTION_KINDS:
                        stagger_key = await _li_stagger_key(session, state, li_stagger_cache)
                        slot = await _reserve_li_stagger_slot(stagger_key, now.timestamp())
                        if slot > 0:
                            # Slot not open — park until the distinct slot the
                            # reservation handed back (one interval past the
                            # account's high-water mark, so every waiting lead
                            # gets its own timestamp instead of piling up).
                            state.next_run_at = datetime.fromtimestamp(slot, tz=timezone.utc)
                            counts["staggered_linkedin"] += 1
                            continue
                    linkedin_dispatch.append((str(state.lead_id), str(node.id)))
                    # Push next_run_at out so a slow handler doesn't get
                    # double-dispatched on the next tick.
                    state.next_run_at = now + timedelta(minutes=10)
                    counts["dispatched_linkedin"] += 1
                    continue

                # Anything else is reserved for a future milestone.
                state.status = LeadSequenceStatus.HALTED
                state.halt_reason = f"unsupported node kind: {node.kind.value}"
                state.next_run_at = None
                counts["halted_unsupported"] += 1

            await session.commit()

        # Dispatch outside the transaction so a Celery enqueue failure doesn't
        # roll back state cursor updates.
        for lead_id, node_id in email_dispatch:
            send_email_step.delay(lead_id, node_id)
        for lead_id, node_id in linkedin_dispatch:
            # Random 2-8 s countdown makes action timing less predictable
            # and avoids simultaneous bursts when multiple leads fire together.
            send_linkedin_step.apply_async(
                args=[lead_id, node_id],
                countdown=random.uniform(2, 8),
            )
    finally:
        await engine.dispose()
    return counts


# --------------------------------------------------------------------------
# Celery tasks
# --------------------------------------------------------------------------


@celery_app.task(name="sequencer.advance_sequences")
def advance_sequences() -> dict[str, int]:
    global _LI_REDIS_CLIENT
    _LI_REDIS_CLIENT = None  # fresh client bound to this asyncio.run loop
    return asyncio.run(for_all_tenants(_advance_sequences_async))


@celery_app.task(bind=True, name="sequencer.send_email_step", max_retries=3)
def send_email_step(self, lead_id: str, node_id: str) -> dict[str, Any]:  # noqa: D401
    try:
        result = asyncio.run(with_record_tenant(Lead, lead_id, _send_email_step_async, lead_id, node_id))
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "send_email_step transient failure for lead=%s node=%s", lead_id, node_id
        )
        try:
            raise self.retry(exc=exc, countdown=60 * (2**self.request.retries))
        except self.MaxRetriesExceededError:
            asyncio.run(
                with_record_tenant(
                    Lead, lead_id, _record_execution_and_advance,
                    uuid.UUID(lead_id),
                    uuid.UUID(node_id),
                    {"status": "failed", "error": str(exc)},
                )
            )
            return {"status": "failed", "error": str(exc)}

    asyncio.run(
        with_record_tenant(
            Lead, lead_id, _record_execution_and_advance,
            uuid.UUID(lead_id), uuid.UUID(node_id), result,
        )
    )
    return result


@celery_app.task(bind=True, name="sequencer.send_linkedin_step", max_retries=2)
def send_linkedin_step(self, lead_id: str, node_id: str) -> dict[str, Any]:  # noqa: D401
    global _LI_REDIS_CLIENT
    _LI_REDIS_CLIENT = None  # force fresh client for this event loop
    try:
        result = asyncio.run(with_record_tenant(Lead, lead_id, _send_linkedin_step_async, lead_id, node_id))
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "send_linkedin_step transient failure for lead=%s node=%s", lead_id, node_id
        )
        try:
            raise self.retry(exc=exc, countdown=300 * (2**self.request.retries))
        except self.MaxRetriesExceededError:
            asyncio.run(
                with_record_tenant(
                    Lead, lead_id, _record_execution_and_advance,
                    uuid.UUID(lead_id),
                    uuid.UUID(node_id),
                    {"status": "failed", "error": str(exc)},
                )
            )
            return {"status": "failed", "error": str(exc)}

    # Min-delay: reschedule for the exact remaining window instead of writing
    # a skipped execution row and advancing the cursor past the step.
    if result.get("status") == "min_delay_retry":
        send_linkedin_step.apply_async(
            args=[lead_id, node_id],
            countdown=result["countdown"],
        )
        return result

    asyncio.run(
        with_record_tenant(
            Lead, lead_id, _record_execution_and_advance,
            uuid.UUID(lead_id), uuid.UUID(node_id), result,
        )
    )
    return result
