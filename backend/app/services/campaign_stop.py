"""Hard-stop a campaign's research + compose pipeline.

Mirrors the funding-discovery stop pattern (``workers/funding_signals.
stop_funding_run``) but tuned for the legacy lead pipeline.  The Stop
button on a campaign drives this — it revokes every in-flight research
/ compose Celery task for the campaign's leads, purges queued + unacked
copies from the broker, raises a cooperative Redis stop flag so a task
that slips past the kill bails before its first Anthropic call, and
pauses the campaign so the legacy send path + sequencer freeze too.

Design notes
- The cooperative flag is the safety net: ``visibility_timeout`` (300 s)
  redelivers unacked messages, so a hard kill alone leaks tokens past
  the 5-min mark.  The worker task body checks the flag at the top and
  no-ops when set, eliminating that window.
- ``send.send_lead`` is included because a paused campaign's send_lead
  tasks are already no-ops at the gate, but their eta-deferred copies
  pile in worker memory and self-retry; purging them clears the
  in-memory build-up.
- Idempotent: running stop twice is safe (zero counts the second time).
- Resume: the user pauses → goal change → "Resume" path re-enrolls
  pending leads.  This module does NOT clear the stop flag; the
  ``resume_campaign`` endpoint does that explicitly so a resume after
  Stop actually proceeds.
"""
from __future__ import annotations

import base64
import json
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Campaign, CampaignStatus, ComposeStatus, Lead, ResearchStatus
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


# Task names this stop hits.  ``send.send_lead`` is in there to drain the
# eta-pile that builds up over a long pause; the actual send gate already
# refuses to ship when the campaign is paused.
_STOPPABLE_TASKS = frozenset({
    "research.research_lead",
    "compose.compose_lead",
    "send.send_lead",
})


def _campaign_redis():  # noqa: ANN202 — return type from sync redis lib
    try:
        import redis

        return redis.Redis.from_url(celery_app.conf.broker_url)
    except Exception:  # noqa: BLE001
        logger.exception("campaign_stop: redis connect failed")
        return None


def stop_key(campaign_id: uuid.UUID | str) -> str:
    return f"campaign:stop:{campaign_id}"


def request_stop(campaign_id: uuid.UUID | str, ttl_seconds: int = 5400) -> bool:
    """Raise the cooperative stop flag for this campaign.  TTL-bounded so
    a missed clear can't wedge the campaign permanently — 90 min is plenty
    of slack for the user to fix config + resume."""
    r = _campaign_redis()
    if r is None:
        return False
    try:
        r.set(stop_key(campaign_id), "1", ex=ttl_seconds)
        return True
    except Exception:  # noqa: BLE001
        logger.exception("campaign_stop: set stop flag failed for %s", campaign_id)
        return False


def stop_requested(r, campaign_id: uuid.UUID | str) -> bool:
    """Cheap per-task probe — called from research/compose worker bodies."""
    if r is None:
        return False
    try:
        return bool(r.exists(stop_key(campaign_id)))
    except Exception:  # noqa: BLE001
        return False


def clear_stop(campaign_id: uuid.UUID | str) -> None:
    """Called by ``resume_campaign`` so a resume after Stop actually runs."""
    r = _campaign_redis()
    if r is None:
        return
    try:
        r.delete(stop_key(campaign_id))
    except Exception:  # noqa: BLE001
        logger.exception("campaign_stop: clear stop flag failed for %s", campaign_id)


def _purge_broker_for_leads(lead_ids: set[str]) -> dict[str, int]:
    """Drop every queued + unacked message whose task is research/compose
    /send for a lead in ``lead_ids``.  Matches by parsing the message body
    rather than grepping the raw bytes — task names are short strings that
    appear in many unrelated places; lead UUIDs are more precise."""
    if not lead_ids:
        return {"queue": 0, "unacked": 0}
    queue = unacked = 0
    try:
        import redis

        r = redis.Redis.from_url(celery_app.conf.broker_url)

        for raw in list(r.lrange("celery", 0, -1)):
            if not _msg_matches(raw, lead_ids):
                continue
            queue += r.lrem("celery", 0, raw)

        for field, val in list(r.hgetall("unacked").items()):
            if not _msg_matches(val, lead_ids):
                continue
            r.hdel("unacked", field)
            r.zrem(
                "unacked_index",
                field.decode() if isinstance(field, bytes) else field,
            )
            unacked += 1
    except Exception:  # noqa: BLE001
        logger.exception("campaign_stop: broker purge failed")
    return {"queue": queue, "unacked": unacked}


def _msg_matches(raw: bytes | str, lead_ids: set[str]) -> bool:
    """Decode a broker message and return True iff it's a stoppable task
    whose first positional arg (the lead id) is in our set."""
    try:
        d = json.loads(raw)
        if isinstance(d, list):
            d = d[0]
        headers = d.get("headers") or {}
        if headers.get("task") not in _STOPPABLE_TASKS:
            return False
        body = json.loads(base64.b64decode(d["body"]).decode())
        # Celery body shape: [args, kwargs, embedded].  args[0] is the lead id.
        args = body[0] if body else None
        return bool(args and args[0] in lead_ids)
    except Exception:  # noqa: BLE001
        return False


async def stop_campaign_pipeline(
    session: AsyncSession, campaign_id: uuid.UUID,
) -> dict[str, Any]:
    """Pause the campaign, raise the stop flag, revoke + purge inflight work.

    Returns a per-stage count dict so the UI toast can be specific
    ("stopped 8 research, 231 compose; purged 1,976 deferred sends").
    """
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        return {"error": "not_found"}

    # 1) Cooperative stop flag FIRST so any task that slips past the kill
    #    below bails before its first external call.  TTL covers the user
    #    fixing config + hitting Resume.
    stop_flagged = request_stop(campaign_id)

    # 2) Pause the campaign so the legacy send path and sequencer freeze.
    #    Use the auto-pause fields with a distinct reason so the UI can
    #    show "stopped by user" instead of "paused".
    from datetime import datetime, timezone

    was_running = campaign.status in (CampaignStatus.RUNNING, CampaignStatus.PREVIEWING)
    if campaign.status != CampaignStatus.COMPLETE:
        campaign.status = CampaignStatus.PAUSED
        campaign.auto_paused_at = datetime.now(timezone.utc)
        campaign.auto_pause_reason = "user_stopped"

    # 3) Reset any RUNNING research/compose status so the next Resume can
    #    re-dispatch them — otherwise they'd sit RUNNING forever.  Sweeper
    #    would catch this in 15 min, but doing it inline is cleaner.
    running_research = (await session.execute(
        select(Lead.id).where(
            Lead.campaign_id == campaign_id,
            Lead.research_status == ResearchStatus.RUNNING,
        )
    )).scalars().all()
    running_compose = (await session.execute(
        select(Lead.id).where(
            Lead.campaign_id == campaign_id,
            Lead.compose_status == ComposeStatus.RUNNING,
        )
    )).scalars().all()
    for lid in running_research:
        lead = await session.get(Lead, lid)
        if lead is not None:
            lead.research_status = ResearchStatus.PENDING
    for lid in running_compose:
        lead = await session.get(Lead, lid)
        if lead is not None:
            lead.compose_status = ComposeStatus.PENDING

    # Collect every lead id for this campaign — both the inspect filter
    # AND the broker purge use this set.
    lead_ids = {
        str(lid) for lid in (await session.execute(
            select(Lead.id).where(Lead.campaign_id == campaign_id)
        )).scalars().all()
    }

    await session.commit()

    # 4) Inspect every worker and revoke + SIGKILL stoppable tasks whose
    #    first arg is in our lead set.  Best-effort — partial inspect
    #    snapshots are fine, the cooperative flag covers the gap.
    revoked: list[tuple[str, str]] = []  # (task_name, task_id)
    try:
        insp = celery_app.control.inspect(timeout=2.0)
        for snapshot in (insp.active(), insp.reserved()):
            for tasks in (snapshot or {}).values():
                for t in tasks:
                    if t.get("name") not in _STOPPABLE_TASKS:
                        continue
                    args = t.get("args") or []
                    if not (args and args[0] in lead_ids and t.get("id")):
                        continue
                    revoked.append((t["name"], t["id"]))
    except Exception:  # noqa: BLE001
        logger.exception("campaign_stop: inspect failed for %s", campaign_id)

    for _name, tid in revoked:
        try:
            celery_app.control.revoke(tid, terminate=True, signal="SIGKILL")
        except Exception:  # noqa: BLE001
            logger.exception("campaign_stop: revoke failed for %s", tid)

    # 5) Purge queued + redelivered copies from the broker so they can't
    #    be restored past visibility_timeout.
    purged = _purge_broker_for_leads(lead_ids)

    # Per-task-name breakdown for the response.
    by_kind = {"research": 0, "compose": 0, "send": 0}
    for name, _ in revoked:
        if "research" in name:
            by_kind["research"] += 1
        elif "compose" in name:
            by_kind["compose"] += 1
        elif "send" in name:
            by_kind["send"] += 1

    return {
        "campaign_id": str(campaign_id),
        "was_running": was_running,
        "stop_flagged": stop_flagged,
        "running_research_reset": len(running_research),
        "running_compose_reset": len(running_compose),
        "terminated": len(revoked),
        "terminated_by_kind": by_kind,
        "purged_queued": purged["queue"],
        "purged_unacked": purged["unacked"],
    }
