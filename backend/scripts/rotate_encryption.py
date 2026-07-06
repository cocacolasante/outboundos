"""Re-encrypt every stored secret under the current primary ENCRYPTION_KEY.

Step 2 of the rotation runbook in app/services/encryption.py: run AFTER
moving the old key into ENCRYPTION_KEYS_OLD and setting the new primary
(MultiFernet decrypts with either; this rewrites every token so the old
key can then be dropped).

Usage (inside the backend container, owner DSN — bypasses RLS on purpose):
    python scripts/rotate_encryption.py
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "/app")

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.config import settings  # noqa: E402
from app.models import ConnectedAccount, TenantProviderKey  # noqa: E402
from app.services.encryption import decrypt, encrypt  # noqa: E402


async def main() -> None:
    engine = create_async_engine(settings.DATABASE_URL)
    rotated = {"connected_accounts": 0, "tenant_provider_keys": 0}
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            for acct in (await session.execute(select(ConnectedAccount))).scalars():
                if acct.password_encrypted:
                    # decrypt-then-encrypt: plaintext stays in this scope.
                    acct.password_encrypted = encrypt(decrypt(acct.password_encrypted))
                    rotated["connected_accounts"] += 1
            for row in (await session.execute(select(TenantProviderKey))).scalars():
                row.encrypted_credentials = encrypt(decrypt(row.encrypted_credentials))
                rotated["tenant_provider_keys"] += 1
            await session.commit()
    finally:
        await engine.dispose()
    print(f"rotated: {rotated}")


if __name__ == "__main__":
    asyncio.run(main())
