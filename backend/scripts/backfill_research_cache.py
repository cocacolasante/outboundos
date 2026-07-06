"""One-shot: backfill ``research_cache`` from existing per-lead research.

When we shipped the email-keyed research cache (90-day TTL, migration
0013), the table was empty — meaning the next research run on a lead we
already researched would needlessly re-burn the Apollo/Hunter/web fan-
out, even when fresh data is sitting on the lead row.

This script walks every ``leads`` row with non-empty ``research_data``,
groups by canonicalised email (strip + lowercase — same as the service),
picks the freshest entry per email (most recent ``updated_at``), and
upserts it into ``research_cache``.  ``refreshed_at`` is set to the
lead's ``updated_at`` (NOT ``now()``) so a row researched 100 days ago
correctly falls outside the 90-day TTL gate and won't be served as
"fresh" by ``research_cache.lookup``.

Skips:
- ``research_data IS NULL`` or empty dict
- "no-research" blobs (``{"skipped": True}`` — NONE / TEMPLATE modes)
- Leads with a blank email

Idempotent: re-running upserts the same rows.  If the cache already has
a fresher entry for an email (e.g. a real cache write happened after a
campaign run), ``on_conflict_do_update`` will refresh it back to the
lead's timestamp — to avoid that, the script picks the MAX between the
existing cache row's ``refreshed_at`` and the lead's ``updated_at`` and
only writes if it would advance the cache forward.

Usage from the repo root::

    docker compose exec backend python scripts/backfill_research_cache.py
    docker compose exec backend python scripts/backfill_research_cache.py --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import AsyncSessionLocal
from app.models import Lead, ResearchCache


def _canon(email: str | None) -> str:
    return (email or "").strip().lower()


def _is_useful(blob: dict | None) -> bool:
    """A research_data blob is worth caching unless it's empty or explicitly
    a 'no research happened' marker."""
    if not blob:
        return False
    if blob.get("skipped") is True:
        return False
    return True


async def main(dry_run: bool, batch_size: int) -> int:
    scanned = 0
    candidates = 0
    grouped: dict[str, tuple[datetime, dict]] = {}

    async with AsyncSessionLocal() as session:
        # Stream all leads with research_data; SQLAlchemy 2 streaming
        # avoids loading everything into memory at once.
        stmt = (
            select(Lead.email, Lead.research_data, Lead.updated_at)
            .where(Lead.research_data.isnot(None))
            .execution_options(yield_per=batch_size)
        )
        result = await session.stream(stmt)
        async for row in result:
            scanned += 1
            email, data, updated_at = row
            e = _canon(email)
            if not e or not _is_useful(data):
                continue
            candidates += 1
            existing = grouped.get(e)
            if existing is None or updated_at > existing[0]:
                grouped[e] = (updated_at, dict(data))

        print(f"Scanned {scanned} leads with research_data.")
        print(f"{candidates} had useful blobs (skipped/empty filtered).")
        print(f"{len(grouped)} unique emails after dedup.")

        if not grouped:
            return 0

        # Look up existing cache rows for these emails so we don't push
        # a stale lead-update older than a real cached write.
        existing_rows = (await session.execute(
            select(ResearchCache.email, ResearchCache.refreshed_at)
            .where(ResearchCache.email.in_(list(grouped.keys())))
        )).all()
        cache_freshness = {e: r for (e, r) in existing_rows}

        to_write: list[tuple[str, datetime, dict]] = []
        skipped_already_fresher = 0
        for e, (updated_at, blob) in grouped.items():
            existing_ts = cache_freshness.get(e)
            if existing_ts is not None and existing_ts >= updated_at:
                skipped_already_fresher += 1
                continue
            to_write.append((e, updated_at, blob))

        print(f"{skipped_already_fresher} skipped (cache already fresher).")
        print(f"{len(to_write)} rows queued for upsert.")

        if dry_run:
            print("(dry-run — no rows written)")
            return 0

        # Batch the upserts so we don't blow out the wire on huge datasets.
        wrote = 0
        for i in range(0, len(to_write), batch_size):
            chunk = to_write[i:i + batch_size]
            for e, ts, blob in chunk:
                ts_utc = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
                stmt = pg_insert(ResearchCache).values(
                    email=e,
                    research_data=blob,
                    refreshed_at=ts_utc,
                ).on_conflict_do_update(
                    index_elements=["email"],
                    set_={"research_data": blob, "refreshed_at": ts_utc},
                )
                await session.execute(stmt)
            await session.commit()
            wrote += len(chunk)
            print(f"  wrote {wrote}/{len(to_write)}")

        print(f"\nBackfilled {wrote} research_cache rows.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="scan + group but don't write any cache rows")
    ap.add_argument("--batch-size", type=int, default=500,
                    help="rows per commit chunk (default 500)")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.dry_run, args.batch_size)))
