"""Nonprofit funding discovery workers (feeds prospect_signals).

A discovery PATH into the existing signals review queue — the sibling
relationship social_listening has with signals.

  funding.poll_usaspending — daily: recent nonprofit grant awards.
  funding.poll_irs_bmf      — monthly: newly-ruled 501(c)(3)s.

Autonomy boundary (mirrors workers/signals._apply_signal_actions): a
discovery signal MAY stage a campaign-less Lead (campaign_id=None),
create a "Reach out" CRM task, and send ONE owner notification.  It MAY
NEVER set campaign_id or create a sequence enrollment — reaching out
stays one human click away on the Signals page.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.models import (
    CrmActivity,
    CrmActivityType,
    FundingEnrichmentQueue,
    FundingEnrichmentStatus,
    FundingSourceState,
    Lead,
    NotificationKind,
    ProspectSignal,
)
from app.services import agent_core, notifications
from app.services.funding_sources import enrichment, irs_bmf, usaspending
from app.services.funding_sources.base import DiscoveredOrg
from app.workers.celery_app import celery_app
from app.tenancy.context import for_all_tenants

logger = logging.getLogger(__name__)

USASPENDING_SOURCE = "usaspending"
IRS_BMF_SOURCE = "irs_bmf"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Manual stop — hard-kill an in-flight run + break the redelivery loop
# ---------------------------------------------------------------------------


def _funding_redis():
    """A sync redis client on the broker, or None if it can't connect.

    Used for the cross-process stop flag (Celery control is broadcast +
    async; a plain Redis key is the simplest thing a running task can poll
    between orgs)."""
    try:
        import redis
        return redis.Redis.from_url(celery_app.conf.broker_url)
    except Exception:  # noqa: BLE001
        logger.exception("funding: redis connect failed")
        return None


def _stop_key(source: str) -> str:
    return f"funding:stop:{source}"


def _request_stop(source: str) -> bool:
    """Raise the stop flag so an in-flight run aborts at its next org.
    TTL-bounded so a missed clear can't wedge the feed permanently."""
    r = _funding_redis()
    if r is None:
        return False
    try:
        r.set(_stop_key(source), "1", ex=900)
        return True
    except Exception:  # noqa: BLE001
        logger.exception("funding: set stop flag failed for %s", source)
        return False


def _stop_requested(r, source: str) -> bool:
    if r is None:
        return False
    try:
        return bool(r.exists(_stop_key(source)))
    except Exception:  # noqa: BLE001
        return False


def _clear_stop(source: str) -> None:
    r = _funding_redis()
    if r is None:
        return
    try:
        r.delete(_stop_key(source))
    except Exception:  # noqa: BLE001
        logger.exception("funding: clear stop flag failed for %s", source)


def _purge_broker_messages(task_name: str) -> dict[str, int]:
    """Remove every queued + unacked broker message for ``task_name``.

    The IRS feed can run far longer than the broker ``visibility_timeout``
    (300s); when it does, Redis restores the message and another worker
    picks it up, stacking concurrent copies that each burn Anthropic
    enrichment tokens.  Terminating the running task isn't enough on its
    own — the unacked copy would just be redelivered.  This clears both
    the ready queue and the in-flight ``unacked`` set so it stays dead.
    """
    needle = task_name.encode()
    queue = unacked = 0
    try:
        import redis  # redis-py sync client (already a dependency)

        r = redis.Redis.from_url(celery_app.conf.broker_url)
        for raw in r.lrange("celery", 0, -1):
            if needle in raw:
                queue += r.lrem("celery", 0, raw)
        for field, val in r.hgetall("unacked").items():
            if needle in val:
                r.hdel("unacked", field)
                r.zrem(
                    "unacked_index",
                    field.decode() if isinstance(field, bytes) else field,
                )
                unacked += 1
    except Exception:  # noqa: BLE001 — best-effort; never raise out of a stop
        logger.exception("funding stop: broker purge failed for %s", task_name)
    return {"queue": queue, "unacked": unacked}


def stop_funding_run(source: str) -> dict[str, Any]:
    """Hard-stop any in-flight run of one feed.

    Revokes + SIGKILLs every active/reserved Celery task for the feed's
    task name across all workers, then purges queued + redelivered copies
    from the broker so the run can't be restored past the visibility
    timeout.  Safe no-op when nothing is running (zero counts).
    """
    task_name = f"funding.poll_{source}"
    # 1) Raise the cooperative stop flag FIRST so a run already mid-staging
    #    aborts at its next org even if the SIGKILL below races or can't
    #    land (e.g. the worker child is blocked in an httpx await).
    stop_flagged = _request_stop(source)

    # 2) Hard-kill every active/reserved copy across workers.
    ids: set[str] = set()
    try:
        insp = celery_app.control.inspect(timeout=2.0)
        for snapshot in (insp.active(), insp.reserved()):
            for worker_tasks in (snapshot or {}).values():
                for t in worker_tasks:
                    if t.get("name") == task_name and t.get("id"):
                        ids.add(t["id"])
    except Exception:  # noqa: BLE001
        logger.exception("funding stop: inspect failed for %s", source)

    for tid in ids:
        try:
            celery_app.control.revoke(tid, terminate=True, signal="SIGKILL")
        except Exception:  # noqa: BLE001
            logger.exception("funding stop: revoke failed for %s", tid)

    # 3) Purge queued + redelivered copies from the broker.
    purged = _purge_broker_messages(task_name)
    return {
        "task": task_name,
        "terminated": sorted(ids),
        "stop_flagged": stop_flagged,
        "purged_queued": purged["queue"],
        "purged_unacked": purged["unacked"],
    }


def _yyyymm(d: date, minus_months: int = 0) -> str:
    """``d`` shifted back ``minus_months`` calendar months, as YYYYMM."""
    total = d.year * 12 + (d.month - 1) - minus_months
    return f"{total // 12:04d}{total % 12 + 1:02d}"


def _env_enabled(source: str) -> bool:
    return (
        settings.USASPENDING_ENABLED if source == USASPENDING_SOURCE
        else settings.IRS_BMF_ENABLED
    )


def _env_config(source: str) -> dict[str, Any]:
    if source == USASPENDING_SOURCE:
        return {
            "lookback_days": settings.USASPENDING_LOOKBACK_DAYS,
            "min_award_amount": settings.USASPENDING_MIN_AWARD_AMOUNT,
            "max_award_amount": settings.USASPENDING_MAX_AWARD_AMOUNT,
            "max_per_run": settings.FUNDING_DISCOVERY_MAX_PER_RUN,
        }
    return {
        "ruling_lookback_months": settings.IRS_BMF_RULING_LOOKBACK_MONTHS,
        "states": list(settings.IRS_BMF_STATES),
        "max_per_run": settings.FUNDING_DISCOVERY_MAX_PER_RUN,
    }


async def _get_or_create_state(session: AsyncSession, source: str) -> FundingSourceState:
    """Fetch the per-source state, seeding ``enabled``/``config`` from the
    env defaults when unset (first run, or NULL after the 0033 migration).
    The env vars are the SEED; the DB row is authoritative afterward, so
    the Settings → Discovery panel can override them live."""
    from app.tenancy.context import current_tenant_id

    q = select(FundingSourceState).where(FundingSourceState.source == source)
    tid = current_tenant_id.get()
    if tid is not None:
        q = q.where(FundingSourceState.tenant_id == tid)
    state = (await session.execute(q.limit(1))).scalars().first()
    if state is None:
        state = FundingSourceState(source=source, cursor={})
        session.add(state)
    if state.enabled is None:
        state.enabled = _env_enabled(source)
    if state.config is None:
        state.config = _env_config(source)
    await session.flush()
    return state


def _signal_detail(org: DiscoveredOrg) -> dict[str, Any]:
    """The detail blob persisted on a ProspectSignal / queue payload —
    feed payload + org identity so a later enrichment can rebuild the org."""
    return {
        **(org.detail or {}),
        "org_name": org.org_name,
        "website": org.website,
        "state": org.state,
        "ein": org.ein,
        "ntee_code": org.ntee_code,
        "mailing_address": org.mailing_address or (org.detail or {}).get("mailing_address"),
    }


async def _stage_resolved_signal(
    session: AsyncSession, src: str, org: DiscoveredOrg, contact: dict[str, Any],
) -> dict[str, Any]:
    """Stage the campaign-less Lead + CRM task + ProspectSignal + one owner
    notification for an org with a RESOLVED contact.  Idempotent on
    ``dedup_key`` (the unique ProspectSignal constraint is the backstop).
    Never touches a campaign."""
    exists = await session.scalar(
        select(ProspectSignal.id).where(
            ProspectSignal.dedup_key == org.dedup_key
        ).limit(1)
    )
    if exists is not None:
        return {"staged": False, "lead_created": False, "task": False, "queued": False}

    agent_settings = await agent_core.get_agent_settings(session)
    email = contact["email"].strip().lower()
    existing = await session.scalar(select(Lead).where(Lead.email == email).limit(1))
    if existing is not None:
        lead = existing
        lead_created = False
    else:
        lead = Lead(
            campaign_id=None,                          # NEVER a campaign
            email=email,
            first_name=contact.get("first_name"),
            last_name=contact.get("last_name"),
            company=org.org_name,
            company_website=org.website or (f"https://{contact['domain']}" if contact.get("domain") else None),
            job_title=contact.get("title"),
            research_data={
                "ein": org.ein,
                "ntee": org.ntee_code,
                "source": src,
                "contact_via": contact.get("via"),
                **(org.detail or {}),
            },
        )
        session.add(lead)
        await session.flush()
        lead_created = True
    lead_id = lead.id

    task = CrmActivity(
        lead_id=lead_id,
        activity_type=CrmActivityType.TASK,
        subject=f"Reach out — {org.summary}"[:300],
        body=(
            f"Discovered via {src} ({org.signal_type}). "
            "Review the signal and reach out while it's fresh."
        ),
        due_at=agent_core.next_business_day(_now()),
        is_agent_generated=True,
        reminder_sent_at=_now(),
    )
    session.add(task)
    await session.flush()

    signal = ProspectSignal(
        watch_id=None,
        source=src,
        signal_type=org.signal_type,
        summary=org.summary,
        detail=_signal_detail(org),
        dedup_key=org.dedup_key,
        lead_id=lead_id,
    )
    session.add(signal)
    await session.flush()

    await notifications.notify(
        session,
        agent_settings,
        kind=NotificationKind.PROSPECT_SIGNAL,
        title=org.summary[:300],
        body=(
            f"Source: {src} ({org.signal_type})\n"
            + (f"New CRM lead staged: {email}\n" if lead_created else "")
            + (f"State: {org.state}\n" if org.state else "")
            + "Open the Signals page to action or dismiss."
        ),
        dedup_key=f"prospect_signal:{org.dedup_key}",
        lead_id=lead_id,
    )
    return {"staged": True, "lead_created": lead_created, "task": True, "queued": False}


def _backoff_days(attempt_index: int) -> int:
    """Days until the next retry for the given (0-based) attempt index;
    clamps to the last configured value for attempts past the list."""
    days = settings.FUNDING_ENRICHMENT_RETRY_DAYS or [7]
    return int(days[min(max(attempt_index, 0), len(days) - 1)])


async def _queue_for_enrichment(
    session: AsyncSession, src: str, org: DiscoveredOrg, domain: str | None,
) -> None:
    """Park a contactless org in the deferred-enrichment queue (pending),
    so it NEVER enters the review queue.  No-op if already queued."""
    exists = await session.scalar(
        select(FundingEnrichmentQueue.id).where(
            FundingEnrichmentQueue.dedup_key == org.dedup_key
        ).limit(1)
    )
    if exists is not None:
        return
    now = _now()
    session.add(FundingEnrichmentQueue(
        source=src,
        ein=org.ein,
        dedup_key=org.dedup_key,
        org_name=org.org_name,
        state=org.state,
        ntee_code=org.ntee_code,
        website=domain or org.website,
        payload={
            "signal_type": org.signal_type,
            "summary": org.summary,
            "detail": _signal_detail(org),
        },
        attempts=1,
        last_attempt_at=now,
        next_attempt_at=now + timedelta(days=_backoff_days(0)),
        status=FundingEnrichmentStatus.PENDING,
    ))
    await session.flush()


async def _stage_discovery_signal(
    session: AsyncSession, src: str, org: DiscoveredOrg,
) -> dict[str, Any]:
    """Gate the review queue: resolve a contact and ONLY stage a
    ProspectSignal when one is found; otherwise park the org in the
    deferred-enrichment queue (the daily retry worker owns it from there).
    Returns {"staged", "lead_created", "task", "queued"}."""
    # Already a signal, or already being nurtured in the queue → no work
    # (and crucially, don't burn a fresh resolve_contact on every re-scan).
    if await session.scalar(
        select(ProspectSignal.id).where(ProspectSignal.dedup_key == org.dedup_key).limit(1)
    ):
        return {"staged": False, "lead_created": False, "task": False, "queued": False}
    if await session.scalar(
        select(FundingEnrichmentQueue.id).where(
            FundingEnrichmentQueue.dedup_key == org.dedup_key
        ).limit(1)
    ):
        return {"staged": False, "lead_created": False, "task": False, "queued": False}

    cr = await enrichment.resolve_contact(org)
    if cr.status == "resolved" and cr.email:
        return await _stage_resolved_signal(session, src, org, {
            "email": cr.email, "first_name": cr.first_name,
            "last_name": cr.last_name, "title": cr.title,
            "via": cr.via, "domain": cr.domain,
        })

    # Gated: contactless orgs never enter the review queue.
    await _queue_for_enrichment(session, src, org, cr.domain)
    return {"staged": False, "lead_created": False, "task": False, "queued": True}


async def _stage_all(
    session: AsyncSession, src: str, orgs: list[DiscoveredOrg],
    *, max_per_run: int | None = None,
) -> dict[str, int]:
    """Stage each org in its OWN transaction so one bad org (failed
    enrichment, notification hiccup) can't roll back the whole batch and
    a dedup guard makes the whole run idempotent.

    ``max_per_run`` hard-caps how many NEW orgs get enriched this run (the
    per-feed lead-pull limit); falls back to the global
    ``FUNDING_DISCOVERY_MAX_PER_RUN``.  Polls the cooperative stop flag
    before each org so a Stop press aborts the run promptly (mid-batch)."""
    staged = leads = tasks = queued = processed = 0
    cap = int(max_per_run if max_per_run is not None else settings.FUNDING_DISCOVERY_MAX_PER_RUN)
    stop_client = _funding_redis()
    capped = False
    for i, org in enumerate(orgs):
        if _stop_requested(stop_client, src):
            logger.info("funding stage for %s aborted by stop request", src)
            break
        # Bound per-run enrichment cost: stop once we've ENRICHED `cap`
        # new orgs.  Dedup-skipped orgs (already a signal/queued) are cheap
        # and don't count, so each daily run advances through the backlog.
        if cap > 0 and processed >= cap:
            capped = True
            logger.info(
                "funding stage for %s hit per-run cap (%s); %s orgs left for next run",
                src, cap, len(orgs) - i,
            )
            break
        try:
            r = await _stage_discovery_signal(session, src, org)
            await session.commit()
            staged += int(r["staged"])
            leads += int(r["lead_created"])
            tasks += int(r["task"])
            queued += int(r.get("queued", False))
            # Only orgs that actually ran enrichment (staged OR queued)
            # count against the cap; dedup no-ops are free.
            if r["staged"] or r.get("queued"):
                processed += 1
        except Exception:  # noqa: BLE001 — one org must not abort the run
            await session.rollback()
            logger.exception("funding stage failed for %s", org.dedup_key)
    return {
        "staged": staged, "leads_created": leads,
        "tasks_created": tasks, "queued": queued, "capped": capped,
    }


# ---------------------------------------------------------------------------
# USAspending — daily
# ---------------------------------------------------------------------------


async def _poll_usaspending_async() -> dict[str, Any]:
    # Clear any stale stop flag from a previous run so this fresh run isn't
    # pre-aborted.  (A redelivered copy can't pre-clear someone else's stop:
    # acks_late=False on the task means there are no redelivered copies.)
    _clear_stop(USASPENDING_SOURCE)
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            try:
                state = await _get_or_create_state(session, USASPENDING_SOURCE)
                if not state.enabled:
                    await session.commit()
                    return {"skipped": "disabled"}
                cfg = state.config or {}
                cur = dict(state.cursor or {})
                today = _now().date()
                # Always query a TRAILING window of `lookback_days`.  We do
                # NOT advance a since-cursor: USAspending action_date data
                # lags reporting by days-to-weeks, so an award only becomes
                # visible in the API after its action_date has already passed
                # — a cursor parked at the last run day would skip every
                # back-dated award forever (the "0 signals" bug).  Re-scanning
                # the full window each poll is safe and free: the dedup_key
                # guard in `_stage_discovery_signal` (+ the UNIQUE on
                # prospect_signals) makes an already-seen award a no-op, so no
                # duplicate signal/lead/Anthropic/Hunter work happens on overlap.
                lookback = max(
                    int(cfg.get("lookback_days") or settings.USASPENDING_LOOKBACK_DAYS),
                    1,
                )
                since = today - timedelta(days=lookback)
                # Award-size bounds (.get with a default only when the key is
                # absent, so a user-set null = "no bound" is honoured).
                min_amount = (
                    cfg.get("min_award_amount", settings.USASPENDING_MIN_AWARD_AMOUNT)
                )
                max_amount = (
                    cfg.get("max_award_amount", settings.USASPENDING_MAX_AWARD_AMOUNT)
                )
                max_per_run = int(
                    cfg.get("max_per_run") or settings.FUNDING_DISCOVERY_MAX_PER_RUN
                )
                state.last_run_at = _now()
                state.last_run_status = "running"
                await session.commit()  # release the state row before slow I/O

                orgs = await usaspending.fetch_recent_awards(
                    since, today, limit=100,
                    min_amount=min_amount, max_amount=max_amount,
                )
                counts = await _stage_all(
                    session, USASPENDING_SOURCE, orgs, max_per_run=max_per_run,
                )

                state = await _get_or_create_state(session, USASPENDING_SOURCE)
                state.cursor = {
                    **cur,
                    "last_window_start": since.isoformat(),
                    "last_run_date": today.isoformat(),
                }
                state.last_run_at = _now()
                state.last_run_status = "done"
                await session.commit()
                return {"fetched": len(orgs), **counts}
            except Exception:
                await session.rollback()
                state = await _get_or_create_state(session, USASPENDING_SOURCE)
                state.last_run_at = _now()
                state.last_run_status = "error"
                await session.commit()
                raise
    finally:
        await engine.dispose()


# acks_late=False (overrides the global True): a funding poll can run far
# longer than the broker visibility_timeout (300s).  With acks-late the
# message sits in `unacked` and Redis REDELIVERS it past the timeout,
# stacking concurrent copies that each keep "pulling" — and make the Stop
# button look broken (it kills one copy while another was just redelivered).
# Acking on receipt means the long run is never redelivered; if the worker
# dies mid-run the periodic beat simply re-runs it next tick.
@celery_app.task(name="funding.poll_usaspending", acks_late=False)
def poll_usaspending() -> dict[str, Any]:
    return asyncio.run(for_all_tenants(_poll_usaspending_async))


# ---------------------------------------------------------------------------
# IRS EO BMF — monthly
# ---------------------------------------------------------------------------


async def _poll_irs_bmf_async() -> dict[str, Any]:
    _clear_stop(IRS_BMF_SOURCE)
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            try:
                state = await _get_or_create_state(session, IRS_BMF_SOURCE)
                cfg = state.config or {}
                states = list(cfg.get("states") or [])
                if not state.enabled or not states:
                    await session.commit()
                    return {"skipped": "disabled"}
                cur = dict(state.cursor or {})
                today = _now().date()
                lookback_months = int(
                    cfg.get("ruling_lookback_months")
                    or settings.IRS_BMF_RULING_LOOKBACK_MONTHS
                )
                floor = _yyyymm(today, lookback_months)
                last = cur.get("last_file_month")
                # FIRST RUN GUARD: with no cursor, bound to the lookback so
                # we never blast the entire historical file.
                since_ruling = floor if last is None else max(last, floor)
                # Per-feed lead-pull cap (safeguard against a huge run).
                max_per_run = int(
                    cfg.get("max_per_run") or settings.FUNDING_DISCOVERY_MAX_PER_RUN
                )
                state.last_run_at = _now()
                state.last_run_status = "running"
                await session.commit()

                # Fetch a little extra (cap * 4) so dedup-skipped already-seen
                # orgs don't starve the run, but still bound the parsed list.
                orgs = await irs_bmf.fetch_new_501c3(
                    states, since_ruling, max_orgs=max(max_per_run * 4, max_per_run),
                )
                counts = await _stage_all(
                    session, IRS_BMF_SOURCE, orgs, max_per_run=max_per_run,
                )

                state = await _get_or_create_state(session, IRS_BMF_SOURCE)
                state.cursor = {**cur, "last_file_month": _yyyymm(today)}
                state.last_run_at = _now()
                state.last_run_status = "done"
                await session.commit()
                return {"fetched": len(orgs), "since_ruling": since_ruling, **counts}
            except Exception:
                await session.rollback()
                state = await _get_or_create_state(session, IRS_BMF_SOURCE)
                state.last_run_at = _now()
                state.last_run_status = "error"
                await session.commit()
                raise
    finally:
        await engine.dispose()


@celery_app.task(name="funding.poll_irs_bmf", acks_late=False)
def poll_irs_bmf() -> dict[str, Any]:
    return asyncio.run(for_all_tenants(_poll_irs_bmf_async))


# ---------------------------------------------------------------------------
# Deferred-enrichment retry — daily
# ---------------------------------------------------------------------------


def _org_from_queue(row: FundingEnrichmentQueue) -> DiscoveredOrg:
    """Rebuild a minimal DiscoveredOrg from a queue row for re-resolution.
    Reuses the ORIGINAL dedup_key so a promotion can never double-emit."""
    detail = dict(row.payload.get("detail") or {})
    return DiscoveredOrg(
        signal_type=row.payload.get("signal_type") or "new_501c3",
        summary=row.payload.get("summary") or row.org_name,
        dedup_key=row.dedup_key,
        org_name=row.org_name,
        state=row.state,
        ein=row.ein,
        ntee_code=row.ntee_code,
        website=row.website,
        mailing_address=detail.get("mailing_address"),
        detail=detail,
    )


def _format_address(addr: dict[str, Any] | None) -> str:
    if not addr:
        return "(no address on file)"
    line = ", ".join(p for p in (
        addr.get("street"), addr.get("city"),
        " ".join(filter(None, [addr.get("state"), addr.get("zip")])).strip(),
    ) if p)
    return line or "(no address on file)"


async def _retry_one(session: AsyncSession, row: FundingEnrichmentQueue) -> str:
    """Re-resolve one queued org.  Returns the new status string."""
    org = _org_from_queue(row)
    cr = await enrichment.resolve_contact(org)
    now = _now()
    row.last_attempt_at = now
    if cr.domain and not row.website:
        row.website = cr.domain

    if cr.status == "resolved" and cr.email:
        # PROMOTE — stage the full signal reusing the original dedup_key.
        await _stage_resolved_signal(session, row.source, org, {
            "email": cr.email, "first_name": cr.first_name,
            "last_name": cr.last_name, "title": cr.title,
            "via": cr.via, "domain": cr.domain,
        })
        row.status = FundingEnrichmentStatus.RESOLVED
        return "resolved"

    row.attempts += 1
    if row.attempts >= int(settings.FUNDING_ENRICHMENT_MAX_ATTEMPTS):
        addr = (row.payload.get("detail") or {}).get("mailing_address")
        if settings.FUNDING_DIRECT_MAIL_FALLBACK and addr:
            # Stage a campaign-less (email-less) Lead to carry the org +
            # address, and hang the direct-mail task off it.  CrmActivity
            # requires a parent; this keeps the mailing target in the CRM
            # without ever enrolling it in a campaign.
            lead = Lead(
                campaign_id=None,
                email=None,
                company=row.org_name,
                research_data={
                    "ein": row.ein, "ntee": row.ntee_code,
                    "source": row.source, "mailing_address": addr,
                    "direct_mail": True,
                },
            )
            session.add(lead)
            await session.flush()
            session.add(CrmActivity(
                lead_id=lead.id,
                activity_type=CrmActivityType.TASK,
                subject=f"Direct mail — {row.org_name}"[:300],
                body=(
                    f"No email could be found for {row.org_name} after "
                    f"{row.attempts} attempts. Send a letter:\n"
                    f"{_format_address(addr)}\n"
                    f"(Source: {row.source}, EIN {row.ein or 'n/a'})"
                ),
                due_at=agent_core.next_business_day(now),
                is_agent_generated=True,
                reminder_sent_at=now,
            ))
            row.status = FundingEnrichmentStatus.MAILED
            return "mailed"
        row.status = FundingEnrichmentStatus.EXHAUSTED
        return "exhausted"

    row.next_attempt_at = now + timedelta(days=_backoff_days(row.attempts - 1))
    return "pending"


async def _retry_enrichment_async() -> dict[str, Any]:
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    counts = {"processed": 0, "resolved": 0, "exhausted": 0, "mailed": 0, "pending": 0}
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            now = _now()
            rows = (await session.execute(
                select(FundingEnrichmentQueue)
                .where(
                    FundingEnrichmentQueue.status == FundingEnrichmentStatus.PENDING,
                    FundingEnrichmentQueue.next_attempt_at <= now,
                )
                .order_by(FundingEnrichmentQueue.next_attempt_at.asc())
                .limit(int(settings.FUNDING_ENRICHMENT_BATCH))
            )).scalars().all()

            for row in rows:
                dedup_key = row.dedup_key  # capture before any rollback expires it
                try:
                    outcome = await _retry_one(session, row)
                    await session.commit()
                    counts["processed"] += 1
                    counts[outcome] = counts.get(outcome, 0) + 1
                except Exception:  # noqa: BLE001 — one row must not abort the sweep
                    await session.rollback()
                    logger.exception("funding retry failed for %s", dedup_key)
    finally:
        await engine.dispose()
    return counts


@celery_app.task(name="funding.retry_enrichment", acks_late=False)
def retry_enrichment() -> dict[str, Any]:
    return asyncio.run(for_all_tenants(_retry_enrichment_async))
