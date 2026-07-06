"""Seed a workspace + owner from the command line (Phase 7).

For fresh deployments where signing up through the UI isn't convenient
(CI, infra-as-code) — creates Tenant + owner User + Membership + the
per-tenant defaults, exactly like /auth/register (minus the session).

Usage (inside the backend container):
    python scripts/bootstrap_tenant.py --email you@x.com --password '...' \
        --name "My Workspace"
Idempotent-ish: refuses to run if the email already has an account.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

sys.path.insert(0, "/app")

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.auth import passwords  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import Membership, MembershipRole, Tenant, User  # noqa: E402
from app.services.tenant_seed import seed_tenant_defaults  # noqa: E402


async def main(email: str, password: str, name: str) -> None:
    engine = create_async_engine(settings.DATABASE_URL)  # owner: bypasses RLS for seeding
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            existing = await session.scalar(
                select(User).where(User.email == email.lower())
            )
            if existing is not None:
                print(f"REFUSING: {email} already has an account")
                sys.exit(1)
            slug = "-".join(name.lower().split()) or "workspace"
            tenant = Tenant(name=name, slug=slug)
            user = User(email=email.lower(),
                        password_hash=passwords.hash_password(password))
            session.add_all([tenant, user])
            await session.flush()
            session.add(Membership(tenant_id=tenant.id, user_id=user.id,
                                   role=MembershipRole.OWNER))
            await seed_tenant_defaults(session, tenant.id)
            await session.commit()
            print(f"created tenant {tenant.slug} ({tenant.id}) with owner {email}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    asyncio.run(main(args.email, args.password, args.name))
