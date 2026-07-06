"""Ad-hoc smoke test for UnipileLinkedInProvider against a real account.

Usage (from backend container):
    docker compose exec backend python scripts/test_unipile_endpoints.py \
        --account-id <uuid> --profile-url <linkedin-url> [--writes]

Without --writes, only the read-only endpoints fire:
  - test_connection
  - view_profile (registers a profile-view notification)
  - latest_post_urn
  - inbox_recent_events

With --writes, additionally:
  - follow_profile
  - react_to_post (likes the latest post if any)
  - send_connect_request
  - send_dm (only if already connected — Unipile may 4xx otherwise)
  - comment_on_post (latest post)
  - send_inmail
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.models import LinkedInAccount
from app.services.linkedin.base import (
    AccountRestricted,
    ChallengeRequired,
    ProfileRef,
)
from app.services.linkedin.unipile_impl import UnipileError, UnipileLinkedInProvider


def banner(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


async def run(account_id: uuid.UUID, profile_url: str, do_writes: bool) -> None:
    eng = create_async_engine(settings.DATABASE_URL)
    try:
        async with AsyncSession(eng, expire_on_commit=False) as session:
            account = await session.get(LinkedInAccount, account_id)
            if account is None:
                print(f"ERROR: no LinkedInAccount with id={account_id}")
                sys.exit(2)
            if not account.unipile_account_id:
                print(f"ERROR: account {account.label} has no unipile_account_id")
                sys.exit(2)
            print(f"Using LinkedInAccount label={account.label!r} "
                  f"status={account.status.value} unipile_id={account.unipile_account_id}")

            profile = ProfileRef.from_url(profile_url)
            print(f"Target profile: public_id={profile.public_id!r}")

            provider = UnipileLinkedInProvider()

            banner("1) test_connection")
            try:
                r = await provider.test_connection(account)
                print(f"  ok={r.ok} error={r.error} meta={r.meta}")
            except (ChallengeRequired, AccountRestricted, UnipileError) as e:
                print(f"  RAISED {type(e).__name__}: {e}")

            banner("2) view_profile (ghost-view notification)")
            try:
                r = await provider.view_profile(account, profile)
                print(f"  ok={r.ok} external_id={r.external_id} error={r.error}")
                if r.meta:
                    print(f"  meta keys: {list(r.meta.keys())}")
            except (ChallengeRequired, AccountRestricted, UnipileError) as e:
                print(f"  RAISED {type(e).__name__}: {e}")

            banner("3) latest_post_urn (find a post to react/comment on)")
            latest_urn = None
            try:
                latest_urn = await provider.latest_post_urn(account, profile)
                print(f"  latest_urn={latest_urn!r}")
            except (ChallengeRequired, AccountRestricted, UnipileError) as e:
                print(f"  RAISED {type(e).__name__}: {e}")

            banner("4) inbox_recent_events (last 24h)")
            try:
                events = await provider.inbox_recent_events(
                    account, datetime.now(timezone.utc) - timedelta(days=1),
                )
                print(f"  {len(events)} event(s)")
                for ev in events[:5]:
                    print(f"    kind={ev.kind} from={ev.from_public_id} at={ev.occurred_at}")
            except (ChallengeRequired, AccountRestricted, UnipileError) as e:
                print(f"  RAISED {type(e).__name__}: {e}")

            if not do_writes:
                print()
                print("Writes skipped (no --writes flag). Done.")
                return

            banner("5) follow_profile")
            try:
                r = await provider.follow_profile(account, profile)
                print(f"  ok={r.ok} external_id={r.external_id} error={r.error}")
            except (ChallengeRequired, AccountRestricted, UnipileError) as e:
                print(f"  RAISED {type(e).__name__}: {e}")

            if latest_urn:
                banner("6) react_to_post (LIKE on latest)")
                try:
                    r = await provider.react_to_post(account, latest_urn, "LIKE")
                    print(f"  ok={r.ok} external_id={r.external_id} error={r.error}")
                except (ChallengeRequired, AccountRestricted, UnipileError) as e:
                    print(f"  RAISED {type(e).__name__}: {e}")

            banner("7) send_connect_request")
            try:
                r = await provider.send_connect_request(
                    account, profile,
                    note="Hi — testing our Unipile integration. Feel free to ignore.",
                )
                print(f"  ok={r.ok} external_id={r.external_id} error={r.error}")
            except (ChallengeRequired, AccountRestricted, UnipileError) as e:
                print(f"  RAISED {type(e).__name__}: {e}")

            banner("8) send_dm (will fail with 4xx if not 1st-degree)")
            try:
                r = await provider.send_dm(account, profile, "Test DM — please ignore.")
                print(f"  ok={r.ok} external_id={r.external_id} error={r.error}")
            except (ChallengeRequired, AccountRestricted, UnipileError) as e:
                print(f"  RAISED {type(e).__name__}: {e}")

            if latest_urn:
                banner("9) comment_on_post")
                try:
                    r = await provider.comment_on_post(
                        account, latest_urn, "Smoke-test comment, please ignore.",
                    )
                    print(f"  ok={r.ok} external_id={r.external_id} error={r.error}")
                except (ChallengeRequired, AccountRestricted, UnipileError) as e:
                    print(f"  RAISED {type(e).__name__}: {e}")

            banner("10) send_inmail (requires Premium credits)")
            try:
                r = await provider.send_inmail(
                    account, profile, "Test InMail", "Test InMail body. Please ignore.",
                )
                print(f"  ok={r.ok} external_id={r.external_id} error={r.error}")
                if r.meta and r.meta.get("premium_required"):
                    print("  -> Unipile reports no Premium/Sales Nav credits (expected).")
            except (ChallengeRequired, AccountRestricted, UnipileError) as e:
                print(f"  RAISED {type(e).__name__}: {e}")

    finally:
        await eng.dispose()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--account-id", required=True)
    p.add_argument("--profile-url", required=True)
    p.add_argument("--writes", action="store_true",
                   help="Also run write actions (follow / react / connect / DM / comment / InMail)")
    args = p.parse_args()
    asyncio.run(run(uuid.UUID(args.account_id), args.profile_url, args.writes))


if __name__ == "__main__":
    main()
