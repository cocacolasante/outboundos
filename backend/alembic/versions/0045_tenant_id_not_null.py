"""Multi-tenancy Phase 3 (step 2 of 3): promote tenant_id to NOT NULL.

Runs AFTER the worker tenant-context refactor (every writer now stamps
tenant_id: request path via the ambient ContextVar, workers via
run_for_tenant / derive-from-record, service paths explicitly), so no new
NULL rows appear.

Gated: rows that went NULL between 0044 and this deploy are re-backfilled
to the oldest tenant first; if any NULL then remains (no tenant exists but
data does — impossible via the backfill, but belt-and-braces) the
migration ABORTS rather than silently losing rows behind RLS.

NOTE: the ORM models deliberately stay ``nullable=True`` — the test schema
is built by ``Base.metadata.create_all`` and hundreds of direct-ORM test
writes rely on tenant-less rows.  The NOT NULL guarantee is a
database-level (migration-path) property; the RLS isolation suite runs
against THIS schema via ``alembic upgrade head``.
"""
import uuid
from typing import Union

from alembic import op
import sqlalchemy as sa

revision: str = "0045"
down_revision: Union[str, None] = "0044"
branch_labels = None
depends_on = None

# Same 40 tables as 0044's ALL_TENANTED (alembic version files aren't
# importable from each other, so the list is duplicated verbatim).
ALL_TENANTED = [
    "leads", "report_definitions",
    "crm_opportunities", "crm_activities", "pipelines", "opportunity_stages",
    "accounts", "contacts", "opportunity_stage_changes",
    "orgs", "signals", "org_intent_scores", "icp_intent_profiles",
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
    "agent_settings",
]


def upgrade() -> None:
    conn = op.get_bind()

    # Re-backfill anything that went NULL between 0044 and this deploy.
    tid = conn.execute(sa.text(
        "SELECT id FROM tenants ORDER BY created_at LIMIT 1"
    )).scalar()
    for t in ALL_TENANTED:
        has_null = conn.execute(sa.text(
            f"SELECT 1 FROM {t} WHERE tenant_id IS NULL LIMIT 1"
        )).scalar()
        if has_null:
            if tid is None:
                tid = uuid.uuid4()
                conn.execute(
                    sa.text(
                        "INSERT INTO tenants (id, name, slug, status) "
                        "VALUES (:id, 'Bootstrap', 'bootstrap', 'active')"
                    ),
                    {"id": str(tid)},
                )
            conn.execute(
                sa.text(f"UPDATE {t} SET tenant_id = :tid WHERE tenant_id IS NULL"),
                {"tid": str(tid)},
            )

    # The gate: abort loudly rather than promote over NULLs.
    for t in ALL_TENANTED:
        remaining = conn.execute(sa.text(
            f"SELECT count(*) FROM {t} WHERE tenant_id IS NULL"
        )).scalar()
        if remaining:
            raise RuntimeError(
                f"REFUSING to promote {t}.tenant_id to NOT NULL: {remaining} "
                f"NULL rows remain after backfill. Investigate before retrying."
            )

    for t in ALL_TENANTED:
        op.alter_column(t, "tenant_id", nullable=False)


def downgrade() -> None:
    for t in ALL_TENANTED:
        op.alter_column(t, "tenant_id", nullable=True)
