"""Multi-tenancy Phase 2: data-model tenancy.

- ``tenant_id UUID`` (nullable, indexed, FK → tenants.id) on every
  tenant-owned/child table (26 new columns; the 13 pre-existing nullable
  columns gain the FK).
- Backfill: every NULL-tenant row lands on the OLDEST tenant (single-
  operator reality), creating a "Bootstrap" tenant only when data exists
  but no tenant does.  NOT NULL promotion is deliberately deferred to a
  later gated migration (workers don't stamp tenant_id yet).
- App-wide constraints become per-tenant with NULLS NOT DISTINCT (PG15),
  preserving today's dedup semantics for worker rows whose tenant_id is
  still NULL: suppression email, research-cache email (PK → surrogate
  UUID), notification/prospect-signal/funding-queue/lookalike dedup keys,
  intent-signal dedupe key, social-post (provider, post_url) [constraint
  NAME kept — the worker upsert targets it], connected-accounts
  single-default-sender partial index.
- ``agent_settings`` rebuilt: int-PK app singleton (id=1) → UUID PK, one
  row per tenant (UNIQUE(tenant_id) NND), + per-tenant notify_email/name.
  The existing row's values are carried over.

Downgrade is real but assumes effectively-single-tenant data (restoring
the app-wide uniques fails if two tenants hold the same email/dedup key —
by then you shouldn't be downgrading past this revision anyway).
"""
import uuid
from typing import Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0044"
down_revision: Union[str, None] = "0043"
branch_labels = None
depends_on = None

# Tables that already carried a nullable tenant_id (migrations 0025/0030/
# 0038/0041) — they only need the FK here.
HAD_TENANT_ID = [
    "leads", "report_definitions",
    "crm_opportunities", "crm_activities", "pipelines", "opportunity_stages",
    "accounts", "contacts", "opportunity_stage_changes",
    "orgs", "signals", "org_intent_scores", "icp_intent_profiles",
]

# Tables gaining the column now (agent_settings is handled by its rebuild).
NEW_TENANT_ID = [
    "campaigns", "connected_accounts", "linkedin_accounts",
    "notifications", "agent_actions",
    "suppression_list", "research_cache",
    "icp_profiles", "lookalike_candidates",
    "signal_watches", "prospect_signals", "funding_enrichment_queue",
    "social_listening_searches", "social_listening_posts",
    "social_listening_opportunities",
    "sequences", "sequence_nodes", "sequence_edges",
    "lead_sequence_states", "lead_step_executions",
    "email_events", "style_corrections",
    "reply_outcomes", "campaign_copy_insights",
    "crm_documents", "crm_opportunity_products",
]

ALL_TENANTED = HAD_TENANT_ID + NEW_TENANT_ID + ["agent_settings"]

# (table, old unique index/constraint on the dedup column, is_constraint,
#  dedup column, new tenant-scoped constraint name)
DEDUP_SWAPS = [
    ("notifications", "ix_notifications_dedup_key", False,
     "dedup_key", "uq_notifications_tenant_dedup"),
    ("prospect_signals", "ix_prospect_signals_dedup_key", False,
     "dedup_key", "uq_prospect_signals_tenant_dedup"),
    ("funding_enrichment_queue", "ix_funding_enrichment_queue_dedup_key", False,
     "dedup_key", "uq_funding_queue_tenant_dedup"),
    ("lookalike_candidates", "ix_lookalike_candidates_dedup_key", False,
     "dedup_key", "uq_lookalike_tenant_dedup"),
    ("signals", "uq_signals_dedupe_key", True,
     "dedupe_key", "uq_signals_tenant_dedupe"),
]

AGENT_SETTINGS_VALUE_COLS = [
    "auto_log_replies", "auto_create_convert_reminders", "auto_draft_replies",
    "stale_opp_nudges_enabled", "daily_digest_enabled",
    "notify_on_positive_reply", "notify_on_any_reply",
    "min_confidence_to_act", "quiet_hours_start_utc", "quiet_hours_end_utc",
    "created_at", "updated_at",
]


def upgrade() -> None:
    conn = op.get_bind()

    # --- 1. New tenant_id columns + per-table index -------------------------
    for table in NEW_TENANT_ID:
        op.add_column(table, sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                                       nullable=True))
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])

    # --- 2. agent_settings rebuild (int-PK singleton → per-tenant UUID) -----
    existing = conn.execute(sa.text(
        f"SELECT {', '.join(AGENT_SETTINGS_VALUE_COLS)} FROM agent_settings LIMIT 1"
    )).mappings().first()
    op.drop_table("agent_settings")
    op.create_table(
        "agent_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id"), nullable=True, index=True),
        sa.Column("notify_email", sa.Text(), nullable=True),
        sa.Column("notify_name", sa.Text(), nullable=True),
        sa.Column("auto_log_replies", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("auto_create_convert_reminders", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("auto_draft_replies", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("stale_opp_nudges_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("daily_digest_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("notify_on_positive_reply", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("notify_on_any_reply", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("min_confidence_to_act", sa.Numeric(3, 2), nullable=False, server_default="0.60"),
        sa.Column("quiet_hours_start_utc", sa.Integer(), nullable=True),
        sa.Column("quiet_hours_end_utc", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", name="uq_agent_settings_tenant",
                            postgresql_nulls_not_distinct=True),
    )
    if existing:
        conn.execute(
            sa.text(
                f"INSERT INTO agent_settings (id, {', '.join(AGENT_SETTINGS_VALUE_COLS)}) "
                f"VALUES (:id, {', '.join(':' + c for c in AGENT_SETTINGS_VALUE_COLS)})"
            ),
            {"id": str(uuid.uuid4()), **dict(existing)},
        )

    # --- 3. App-wide uniques → per-tenant (NULLS NOT DISTINCT) --------------
    # suppression_list: UNIQUE(email) → UNIQUE(tenant_id, email)
    op.drop_constraint("uq_suppression_list_email", "suppression_list", type_="unique")
    op.execute("CREATE INDEX IF NOT EXISTS ix_suppression_list_email ON suppression_list (email)")
    op.create_unique_constraint(
        "uq_suppression_tenant_email", "suppression_list", ["tenant_id", "email"],
        postgresql_nulls_not_distinct=True,
    )

    # research_cache: PK(email) → surrogate UUID PK + UNIQUE(tenant_id, email)
    op.add_column("research_cache", sa.Column("id", postgresql.UUID(as_uuid=True), nullable=True))
    op.execute("UPDATE research_cache SET id = gen_random_uuid() WHERE id IS NULL")
    op.alter_column("research_cache", "id", nullable=False)
    op.drop_constraint("research_cache_pkey", "research_cache", type_="primary")
    op.create_primary_key("research_cache_pkey", "research_cache", ["id"])
    op.create_index("ix_research_cache_email", "research_cache", ["email"])
    op.create_unique_constraint(
        "uq_research_cache_tenant_email", "research_cache", ["tenant_id", "email"],
        postgresql_nulls_not_distinct=True,
    )

    # dedup-key tables: unique index/constraint → plain index + tenant-scoped unique
    for table, old_name, is_constraint, col, new_name in DEDUP_SWAPS:
        if is_constraint:
            op.drop_constraint(old_name, table, type_="unique")
        else:
            op.drop_index(old_name, table_name=table)
        op.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_{col} ON {table} ({col})")
        op.create_unique_constraint(
            new_name, table, ["tenant_id", col],
            postgresql_nulls_not_distinct=True,
        )

    # social posts: same constraint NAME, tenant-scoped columns (the worker
    # upsert targets it via ON CONFLICT ON CONSTRAINT).
    op.drop_constraint("uq_social_posts_provider_url", "social_listening_posts", type_="unique")
    op.create_unique_constraint(
        "uq_social_posts_provider_url", "social_listening_posts",
        ["tenant_id", "provider", "post_url"],
        postgresql_nulls_not_distinct=True,
    )

    # connected_accounts: single default sender per TENANT (was app-wide).
    op.drop_index("ix_connected_accounts_single_default_sender",
                  table_name="connected_accounts")
    op.execute(
        "CREATE UNIQUE INDEX ix_connected_accounts_single_default_sender "
        "ON connected_accounts (tenant_id) NULLS NOT DISTINCT "
        "WHERE is_default_sender IS TRUE"
    )

    # --- 4. FKs to tenants for every tenanted table (agent_settings inline) -
    for table in HAD_TENANT_ID + NEW_TENANT_ID:
        op.create_foreign_key(f"fk_{table}_tenant", table, "tenants",
                              ["tenant_id"], ["id"])

    # --- 5. Composite hot-path indexes ---------------------------------------
    op.create_index("ix_campaigns_tenant_status", "campaigns", ["tenant_id", "status"])
    op.create_index("ix_leads_tenant_campaign", "leads", ["tenant_id", "campaign_id"])
    op.create_index("ix_lead_seq_states_tenant_due", "lead_sequence_states",
                    ["tenant_id", "status", "next_run_at"])

    # --- 6. Backfill every NULL-tenant row to the oldest (or new) tenant -----
    has_orphans = any(
        conn.execute(sa.text(
            f"SELECT 1 FROM {t} WHERE tenant_id IS NULL LIMIT 1"
        )).scalar()
        for t in ALL_TENANTED
    )
    if has_orphans:
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
        for t in ALL_TENANTED:
            conn.execute(
                sa.text(f"UPDATE {t} SET tenant_id = :tid WHERE tenant_id IS NULL"),
                {"tid": str(tid)},
            )


def downgrade() -> None:
    conn = op.get_bind()

    op.drop_index("ix_lead_seq_states_tenant_due", table_name="lead_sequence_states")
    op.drop_index("ix_leads_tenant_campaign", table_name="leads")
    op.drop_index("ix_campaigns_tenant_status", table_name="campaigns")

    for table in HAD_TENANT_ID + NEW_TENANT_ID:
        op.drop_constraint(f"fk_{table}_tenant", table, type_="foreignkey")

    # connected_accounts default-sender index back to app-wide.
    op.drop_index("ix_connected_accounts_single_default_sender",
                  table_name="connected_accounts")
    op.execute(
        "CREATE UNIQUE INDEX ix_connected_accounts_single_default_sender "
        "ON connected_accounts (is_default_sender) "
        "WHERE is_default_sender IS TRUE"
    )

    # social posts constraint back to app-wide (same name).
    op.drop_constraint("uq_social_posts_provider_url", "social_listening_posts", type_="unique")
    op.create_unique_constraint(
        "uq_social_posts_provider_url", "social_listening_posts",
        ["provider", "post_url"],
    )

    for table, old_name, is_constraint, col, new_name in DEDUP_SWAPS:
        op.drop_constraint(new_name, table, type_="unique")
        if is_constraint:
            # The plain ix_<table>_<col> index pre-dates 0044 (an earlier
            # migration owns it) — leave it; just restore the constraint.
            op.create_unique_constraint(old_name, table, [col])
        else:
            # old_name IS the plain index my upgrade re-created — swap it
            # back to the original unique index.
            op.execute(f"DROP INDEX IF EXISTS ix_{table}_{col}")
            op.create_index(old_name, table, [col], unique=True)

    # research_cache back to PK(email).
    op.drop_constraint("uq_research_cache_tenant_email", "research_cache", type_="unique")
    op.drop_index("ix_research_cache_email", table_name="research_cache")
    op.drop_constraint("research_cache_pkey", "research_cache", type_="primary")
    op.drop_column("research_cache", "id")
    op.create_primary_key("research_cache_pkey", "research_cache", ["email"])

    # suppression back to app-wide UNIQUE(email).  The plain
    # ix_suppression_list_email index is 0001's — leave it in place.
    op.drop_constraint("uq_suppression_tenant_email", "suppression_list", type_="unique")
    op.create_unique_constraint("uq_suppression_list_email", "suppression_list", ["email"])

    # agent_settings back to the int-PK singleton, values carried over.
    existing = conn.execute(sa.text(
        f"SELECT {', '.join(AGENT_SETTINGS_VALUE_COLS)} FROM agent_settings "
        f"ORDER BY created_at LIMIT 1"
    )).mappings().first()
    op.drop_table("agent_settings")
    op.create_table(
        "agent_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("auto_log_replies", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("auto_create_convert_reminders", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("auto_draft_replies", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("stale_opp_nudges_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("daily_digest_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("notify_on_positive_reply", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("notify_on_any_reply", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("min_confidence_to_act", sa.Numeric(3, 2), nullable=False, server_default="0.60"),
        sa.Column("quiet_hours_start_utc", sa.Integer(), nullable=True),
        sa.Column("quiet_hours_end_utc", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    if existing:
        conn.execute(
            sa.text(
                f"INSERT INTO agent_settings (id, {', '.join(AGENT_SETTINGS_VALUE_COLS)}) "
                f"VALUES (1, {', '.join(':' + c for c in AGENT_SETTINGS_VALUE_COLS)})"
            ),
            dict(existing),
        )

    for table in NEW_TENANT_ID:
        op.drop_index(f"ix_{table}_tenant_id", table_name=table)
        op.drop_column(table, "tenant_id")
