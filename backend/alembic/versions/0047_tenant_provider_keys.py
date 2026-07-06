"""Multi-tenancy Phase 4 (BYOK): tenant_provider_keys.

New tenant-owned table with the full Phase-2/3 treatment inline
(tenant_id NOT NULL + FK + RLS policy) since it's born after both
promotions.

Seed: the OLDEST tenant inherits whatever provider keys are currently in
the environment (encrypted) — the single-operator continuity path, so the
running deployment keeps composing/sending the moment BYOK resolution
replaces the global settings reads.  No tenants (fresh install) or no env
keys → nothing seeded.
"""
import json
import uuid
from typing import Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0047"
down_revision: Union[str, None] = "0046"
branch_labels = None
depends_on = None

_PREDICATE = "(tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"


def _env_seed_blobs() -> dict[str, dict]:
    from app.config import settings

    blobs: dict[str, dict] = {}
    if settings.ANTHROPIC_API_KEY:
        blobs["anthropic"] = {"api_key": settings.ANTHROPIC_API_KEY}
    if settings.BREVO_API_KEY:
        blobs["brevo"] = {
            "api_key": settings.BREVO_API_KEY,
            "sender_email": settings.BREVO_SENDER_EMAIL,
            "sender_name": settings.BREVO_SENDER_NAME,
        }
    if settings.APOLLO_API_KEY:
        blobs["apollo"] = {"api_key": settings.APOLLO_API_KEY}
    if settings.HUNTER_API_KEY:
        blobs["hunter"] = {"api_key": settings.HUNTER_API_KEY}
    if settings.UNIPILE_API_KEY:
        blobs["unipile"] = {
            "dsn": settings.UNIPILE_DSN,
            "api_key": settings.UNIPILE_API_KEY,
        }
    return blobs


def upgrade() -> None:
    op.execute(
        "CREATE TYPE provider_kind AS ENUM "
        "('anthropic', 'brevo', 'apollo', 'hunter', 'unipile')"
    )
    op.execute("CREATE TYPE key_test_status AS ENUM ('untested', 'ok', 'failed')")

    op.create_table(
        "tenant_provider_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id"), nullable=False, index=True),
        sa.Column(
            "provider",
            postgresql.ENUM("anthropic", "brevo", "apollo", "hunter", "unipile",
                            name="provider_kind", create_type=False),
            nullable=False,
        ),
        sa.Column("encrypted_credentials", sa.Text(), nullable=False),
        sa.Column(
            "last_test_status",
            postgresql.ENUM("untested", "ok", "failed",
                            name="key_test_status", create_type=False),
            nullable=False, server_default="untested",
        ),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "provider",
                            name="uq_tenant_provider_keys_tenant_provider",
                            postgresql_nulls_not_distinct=True),
    )

    op.execute("ALTER TABLE tenant_provider_keys ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON tenant_provider_keys "
        f"USING {_PREDICATE} WITH CHECK {_PREDICATE}"
    )

    conn = op.get_bind()
    tid = conn.execute(sa.text(
        "SELECT id FROM tenants ORDER BY created_at LIMIT 1"
    )).scalar()
    from app.config import settings as _settings

    if tid is not None and not _settings.ENCRYPTION_KEY:
        # Can't encrypt the seed without the key (e.g. a bare alembic run
        # outside the app env).  Skip — the operator enters keys via
        # Settings → Integrations instead.
        print("0047: ENCRYPTION_KEY not set — skipping env-key seed")
        tid = None
    if tid is not None:
        from app.services.encryption import encrypt

        for provider, blob in _env_seed_blobs().items():
            conn.execute(
                sa.text(
                    "INSERT INTO tenant_provider_keys "
                    "(id, tenant_id, provider, encrypted_credentials) "
                    "VALUES (:id, :tid, :provider, :blob)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "tid": str(tid),
                    "provider": provider,
                    "blob": encrypt(json.dumps(blob)),
                },
            )


def downgrade() -> None:
    op.drop_table("tenant_provider_keys")
    op.execute("DROP TYPE IF EXISTS key_test_status")
    op.execute("DROP TYPE IF EXISTS provider_kind")
