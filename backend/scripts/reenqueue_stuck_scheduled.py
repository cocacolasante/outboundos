"""One-shot: re-enqueue ``send_lead`` for leads orphaned in SendStatus.SCHEDULED.

When a campaign was paused mid-flight, send tasks already in the queue
returned ``{status: paused}`` and were acked.  Whatever previously
flipped those leads to SCHEDULED wrote ``scheduled_send_at`` to the row
but nothing reads it — there's no cron / sweeper that re-fires them
once the campaign unpauses.  Without manual intervention, those leads
sit forever.

The fix shipped (2026-05-29) patches ``resume_campaign`` to re-enqueue
PENDING + SCHEDULED leads on resume.  This script is the one-off
remediation for orphans that pre-date the fix.

Usage::

    docker compose exec worker python scripts/reenqueue_stuck_scheduled.py \\
        <campaign_id>

    # or, all campaigns at once:
    docker compose exec worker python scripts/reenqueue_stuck_scheduled.py --all

    # dry-run (don't actually fire):
    docker compose exec worker python scripts/reenqueue_stuck_scheduled.py \\
        <campaign_id> --dry-run

Sends are spaced by the campaign's ``min_delay_seconds`` via
``apply_async(eta=...)`` — exact same pattern as ``_kick_off_full_campaign``
and the new ``resume_campaign`` re-enqueue.  The atomic min-gate in
``check_rate_limits`` is the backstop for residual collisions.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import (
    Campaign,
    CampaignStatus,
    ComposeStatus,
    Lead,
    SendStatus,
)
from app.workers.send import send_lead


async def reenqueue_one(cid: uuid.UUID, dry_run: bool) -> int:
    async with AsyncSessionLocal() as session:
        c = await session.get(Campaign, cid)
        if c is None:
            print(f"  campaign {cid} not found, skipping")
            return 0

        pending_ids = list((await session.execute(
            select(Lead.id).where(
                Lead.campaign_id == c.id,
                Lead.compose_status == ComposeStatus.DONE,
                Lead.send_status.in_((SendStatus.PENDING, SendStatus.SCHEDULED)),
            )
        )).scalars().all())

        print(f"  {c.name!r} ({c.status.value}): {len(pending_ids)} leads to re-enqueue, "
              f"min_delay={c.min_delay_seconds}s")
        if not pending_ids or dry_run:
            return len(pending_ids)

        min_delay = max(c.min_delay_seconds or 0, 0)
        base = datetime.now(timezone.utc)
        for i, lid in enumerate(pending_ids):
            if min_delay:
                send_lead.apply_async(
                    args=[str(lid)], eta=base + timedelta(seconds=i * min_delay)
                )
            else:
                send_lead.delay(str(lid))
        last_eta = base + timedelta(seconds=(len(pending_ids) - 1) * min_delay)
        print(f"    dispatched; last eta {last_eta.isoformat()}")
        return len(pending_ids)


async def main(targets: list[uuid.UUID] | None, dry_run: bool) -> int:
    if targets is None:
        async with AsyncSessionLocal() as session:
            targets = list((await session.execute(
                select(Campaign.id).where(Campaign.status.in_((
                    CampaignStatus.RUNNING, CampaignStatus.PAUSED,
                )))
            )).scalars().all())

    print(f"Targeting {len(targets)} campaign(s){' (dry-run)' if dry_run else ''}")
    total = 0
    for cid in targets:
        total += await reenqueue_one(cid, dry_run)
    print(f"\nTotal leads re-enqueued: {total}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("campaign_id", nargs="?", help="campaign UUID to re-enqueue")
    ap.add_argument("--all", action="store_true",
                    help="re-enqueue every running/paused campaign")
    ap.add_argument("--dry-run", action="store_true",
                    help="report but don't dispatch")
    args = ap.parse_args()

    if args.all and args.campaign_id:
        ap.error("pass either a campaign UUID OR --all, not both")
    if not args.all and not args.campaign_id:
        ap.error("pass a campaign UUID or --all")

    targets = None if args.all else [uuid.UUID(args.campaign_id)]
    sys.exit(asyncio.run(main(targets, args.dry_run)))
