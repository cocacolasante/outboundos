"""One-shot reconciliation: flip locally-INVITED leads to CONNECTED
when Unipile reports they're 1st-degree connections.

Background: the inbound-webhook handler used to drop ``new_relation``
events because the lead-matching logic only looked at nested fields
while Unipile puts the public identifier at the top level.  The bug is
fixed going forward but historical events were absorbed by the dedup
table, so leads that accepted before the fix are still parked on the
local ``linkedin_connection_status=INVITED`` cursor and the sequencer
never advances them to the DM step.

This script pulls the full list of 1st-degree connections per Unipile
account via ``GET /api/v1/users/relations`` (paginated), normalises to
public_identifier slugs, and flips any matching lead whose status is
still INVITED or UNKNOWN.

Usage (from the repo root)::

    docker compose exec backend python scripts/reconcile_linkedin_connections.py
    docker compose exec backend python scripts/reconcile_linkedin_connections.py --dry-run

Safe to re-run — only acts on rows whose status is not already CONNECTED.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import (
    Campaign,
    Lead,
    LinkedInAccount,
    LinkedInConnectionStatus,
)
from app.services.linkedin.unipile_impl import UnipileLinkedInProvider


def _slug_from_url(url: str | None) -> str | None:
    if not url:
        return None
    return url.rstrip("/").split("/")[-1].split("?")[0].lower() or None


async def _fetch_all_relations(provider: UnipileLinkedInProvider, unipile_id: str) -> list[dict]:
    """Paginate ``/api/v1/users/relations`` for one Unipile account."""
    items: list[dict] = []
    cursor: str | None = None
    while True:
        params = {"account_id": unipile_id, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        data = await provider._request("GET", "/api/v1/users/relations", params=params)
        if not isinstance(data, dict):
            break
        batch = data.get("items") or []
        items.extend(batch)
        cursor = data.get("cursor")
        if not cursor:
            break
    return items


async def main(dry_run: bool) -> int:
    provider = UnipileLinkedInProvider()
    flipped = 0
    skipped_no_url = 0
    examined = 0

    async with AsyncSessionLocal() as session:
        accounts = (await session.execute(
            select(LinkedInAccount).where(LinkedInAccount.unipile_account_id.is_not(None))
        )).scalars().all()

        for acc in accounts:
            print(f"\n=== {acc.label} ({acc.unipile_account_id}) ===")
            relations = await _fetch_all_relations(provider, acc.unipile_account_id)
            slugs: set[str] = set()
            for r in relations:
                pid = (r.get("public_identifier") or "").lower()
                if pid:
                    slugs.add(pid)
                purl = r.get("public_profile_url") or ""
                s = _slug_from_url(purl)
                if s:
                    slugs.add(s)
            print(f"Unipile returned {len(relations)} relations ({len(slugs)} unique slugs)")

            leads = (await session.execute(
                select(Lead)
                .join(Campaign, Campaign.id == Lead.campaign_id)
                .where(
                    Campaign.linkedin_account_id == acc.id,
                    Lead.linkedin_connection_status.in_([
                        LinkedInConnectionStatus.INVITED,
                        LinkedInConnectionStatus.UNKNOWN,
                    ]),
                )
            )).scalars().all()

            for lead in leads:
                examined += 1
                slug = _slug_from_url(lead.linkedin_url)
                if not slug:
                    skipped_no_url += 1
                    continue
                if slug in slugs:
                    print(
                        f"  flip: {lead.email} ({lead.first_name} {lead.last_name}) "
                        f"slug={slug} {lead.linkedin_connection_status.value}->CONNECTED"
                    )
                    if not dry_run:
                        lead.linkedin_connection_status = LinkedInConnectionStatus.CONNECTED
                    flipped += 1

        if not dry_run:
            await session.commit()

    print()
    print(f"Examined: {examined}")
    print(f"Skipped (no linkedin_url): {skipped_no_url}")
    print(f"Flipped to CONNECTED: {flipped}")
    if dry_run:
        print("(dry-run — no DB changes were persisted)")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="don't commit changes")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.dry_run)))
