"""One-shot: clear the ``\\Seen`` flag on replies the previous poller
auto-marked-as-read in the user's mailbox.

The reply poller used to call ``STORE +FLAGS \\Seen`` on every fetched
message to dedup polls.  Side effect: real replies showed up in the
user's Gmail/Outlook as already-read, easy to miss.  The new poller
doesn't touch read/unread state, but historical replies wrongly marked
need to be flipped back.

This script:
1. Iterates every healthy (``last_test_status=OK``) ConnectedAccount.
2. Pulls every ``EmailEvent(REPLIED)`` row from the last ``WINDOW_DAYS``
   (default 7) for that account's campaigns.
3. Reads the ``uid`` we stored on each event's ``event_data``.
4. Logs in to the IMAP server and sends ``STORE -FLAGS \\Seen`` for
   each UID — flipping the message back to unread for the user.

Usage from the repo root::

    docker compose exec backend python scripts/unmark_seen_replies.py
    docker compose exec backend python scripts/unmark_seen_replies.py --days 30 --dry-run

The IMAP UIDs we stored are stable per-mailbox until the user moves the
message or the mailbox is reset.  If a UID can't be found
(``STORE`` returns non-OK), we skip it and count it as an error rather
than aborting.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import (
    Campaign,
    ConnectedAccount,
    ConnectedAccountTestStatus,
    EmailEvent,
    EmailEventType,
)
from app.services import imap_client


async def main(days: int, dry_run: bool) -> int:
    floor = datetime.now(timezone.utc) - timedelta(days=days)
    total_uids = 0
    total_cleared = 0
    total_errors = 0

    async with AsyncSessionLocal() as session:
        accounts = (await session.execute(
            select(ConnectedAccount).where(
                ConnectedAccount.last_test_status == ConnectedAccountTestStatus.OK
            )
        )).scalars().all()

        for acc in accounts:
            print(f"\n=== {acc.label} ({acc.email_address}) ===")

            campaign_ids = (await session.execute(
                select(Campaign.id).where(Campaign.connected_account_id == acc.id)
            )).scalars().all()
            if not campaign_ids:
                print("  no campaigns — skipping")
                continue

            events = (await session.execute(
                select(EmailEvent).where(
                    EmailEvent.event_type == EmailEventType.REPLIED,
                    EmailEvent.campaign_id.in_(campaign_ids),
                    EmailEvent.occurred_at >= floor,
                )
            )).scalars().all()

            uids: list[str] = []
            for ev in events:
                ed = ev.event_data or {}
                uid = ed.get("uid")
                if uid:
                    uids.append(str(uid))

            if not uids:
                print(f"  no REPLIED events in last {days}d — skipping")
                continue

            print(f"  {len(uids)} UIDs to unmark")
            total_uids += len(uids)
            if dry_run:
                continue

            # Run blocking IMAP call on a thread so we don't block the loop.
            result = await asyncio.to_thread(
                imap_client.unmark_seen_uids, acc, uids,
            )
            if not result.get("ok"):
                print(f"  ERROR: {result.get('error')}")
                continue
            print(f"  cleared={result['cleared']} errors={result['errors']}")
            total_cleared += result["cleared"]
            total_errors += result["errors"]

    print()
    print(f"Total UIDs targeted: {total_uids}")
    if dry_run:
        print("(dry-run — no IMAP STORE calls were made)")
    else:
        print(f"Cleared: {total_cleared}, errors: {total_errors}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7,
                    help="how far back to look for REPLIED events (default 7)")
    ap.add_argument("--dry-run", action="store_true",
                    help="don't actually call IMAP STORE")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.days, args.dry_run)))
