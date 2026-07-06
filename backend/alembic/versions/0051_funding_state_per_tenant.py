"""Phase 8: funding_source_state becomes per-tenant.

Was a global per-feed singleton (PK = source text).  Now one row per
(tenant, source) — surrogate UUID PK, tenant_id NOT NULL + FK + RLS
inline (the isolation suite's completeness check requires the policy on
every tenant_id-bearing table).  Existing rows (≤2) are carried over and
backfilled to the OLDEST tenant, preserving the single-operator's feed
config + cursors.
"""
import uuid
from typing import Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0051"
down_revision: Union[str, None] = "0050"
branch_labels = None
depends_on = None

_PREDICATE = "(tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
_VALUE_COLS = ["source", "enabled", "config", "cursor", "last_run_at", "last_run_status"]


def upgrade() -> None:
    conn = op.get_bind()
    import json

    rows = [dict(r) for r in conn.execute(sa.text(
        f"SELECT {', '.join(_VALUE_COLS)} FROM funding_source_state"
    )).mappings()]

    op.drop_table("funding_source_state")
    op.create_table(
        "funding_source_state",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id"), nullable=False, index=True),
        sa.Column("source", sa.Text(), nullable=False, index=True),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("cursor", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_status", sa.Text(), nullable=True),
        sa.UniqueConstraint("tenant_id", "source",
                            name="uq_funding_state_tenant_source",
                            postgresql_nulls_not_distinct=True),
    )
    op.execute("ALTER TABLE funding_source_state ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON funding_source_state "
        f"USING {_PREDICATE} WITH CHECK {_PREDICATE}"
    )

    if rows:
        tid = conn.execute(sa.text(
            "SELECT id FROM tenants ORDER BY created_at LIMIT 1"
        )).scalar()
        if tid is None:
            tid = uuid.uuid4()
            conn.execute(
                sa.text(
                    "INSERT INTO tenants (id, name, slug, status) "
                    "VALUES (:id, 'Bootstrap', 'bootstrap', 'active')"
                ),
                {"id": str(tid)},
            )
        for r in rows:
            conn.execute(
                sa.text(
                    "INSERT INTO funding_source_state "
                    "(id, tenant_id, source, enabled, config, cursor, "
                    " last_run_at, last_run_status) "
                    "VALUES (:id, :tid, :source, :enabled, :config, :cursor, "
                    "        :last_run_at, :last_run_status)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "tid": str(tid),
                    "source": r["source"],
                    "enabled": r["enabled"],
                    "config": json.dumps(r["config"]) if r["config"] is not None else None,
                    "cursor": json.dumps(r["cursor"]) if r["cursor"] is not None else None,
                    "last_run_at": r["last_run_at"],
                    "last_run_status": r["last_run_status"],
                },
            )


def downgrade() -> None:
    conn = op.get_bind()
    import json

    # Keep the OLDEST tenant's rows (the single-operator's) when collapsing
    # back to the global singleton shape.
    rows = [dict(r) for r in conn.execute(sa.text(
        "SELECT DISTINCT ON (source) source, enabled, config, cursor, "
        "last_run_at, last_run_status "
        "FROM funding_source_state f "
        "JOIN tenants t ON t.id = f.tenant_id "
        "ORDER BY source, t.created_at"
    )).mappings()]

    op.drop_table("funding_source_state")
    op.create_table(
        "funding_source_state",
        sa.Column("source", sa.Text(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("cursor", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_status", sa.Text(), nullable=True),
    )
    for r in rows:
        conn.execute(
            sa.text(
                "INSERT INTO funding_source_state "
                "(source, enabled, config, cursor, last_run_at, last_run_status) "
                "VALUES (:source, :enabled, :config, :cursor, :last_run_at, :last_run_status)"
            ),
            {
                **{k: r[k] for k in ("source", "enabled", "last_run_at", "last_run_status")},
                "config": json.dumps(r["config"]) if r["config"] is not None else None,
                "cursor": json.dumps(r["cursor"]) if r["cursor"] is not None else None,
            },
        )
