"""One-off: exercise react_to_post + comment_on_post against a public profile's
latest post.  Used to validate Unipile endpoint shapes against a real account.

Usage:
    docker compose exec backend python scripts/test_unipile_post_actions.py \
        --account-id <uuid> --profile-url <linkedin-url> [--comment "text"]

By default this script only LIKES the latest post.  Pass --comment to also
post a public comment.  Both actions are visible on LinkedIn.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.models import LinkedInAccount
from app.services.linkedin.base import ProfileRef
from app.services.linkedin.unipile_impl import UnipileLinkedInProvider


async def run(account_id: uuid.UUID, profile_url: str, comment: str | None) -> None:
    eng = create_async_engine(settings.DATABASE_URL)
    try:
        async with AsyncSession(eng, expire_on_commit=False) as session:
            account = await session.get(LinkedInAccount, account_id)
            if account is None or not account.unipile_account_id:
                print("ERROR: account missing or has no unipile_account_id")
                sys.exit(2)
            profile = ProfileRef.from_url(profile_url)
            provider = UnipileLinkedInProvider()

            print(f"Looking up latest post for {profile.public_id!r}…")
            urn = await provider.latest_post_urn(account, profile)
            if not urn:
                print("FAIL: latest_post_urn returned None")
                sys.exit(1)
            print(f"  -> {urn}")

            print(f"react_to_post(LIKE, {urn})…")
            r = await provider.react_to_post(account, urn, "LIKE")
            print(f"  ok={r.ok} external_id={r.external_id} error={r.error}")

            if comment:
                print(f"comment_on_post({urn})…")
                r = await provider.comment_on_post(account, urn, comment)
                print(f"  ok={r.ok} external_id={r.external_id} error={r.error}")
    finally:
        await eng.dispose()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--account-id", required=True)
    p.add_argument("--profile-url", required=True)
    p.add_argument("--comment", default=None,
                   help="If set, also POST a comment with this text.")
    args = p.parse_args()
    asyncio.run(run(uuid.UUID(args.account_id), args.profile_url, args.comment))


if __name__ == "__main__":
    main()
