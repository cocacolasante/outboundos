"""Multi-tenancy Phase 3 (step 3 of 3): row-level security.

ENABLE ROW LEVEL SECURITY + a ``tenant_isolation`` policy on every
tenant-owned table.  The policy keys on the transaction-local
``app.tenant_id`` GUC (stamped by the Session ``after_begin`` listener in
``app/tenancy/context.py``):

    USING / WITH CHECK
      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)

- Missing GUC → NULL predicate → ZERO rows visible: fail-closed, no error.
- ``WITH CHECK`` on the same predicate blocks cross-tenant INSERT/UPDATE
  even if application code forgets to stamp.
- Plain ENABLE (not FORCE): the table OWNER bypasses policies — that's the
  deliberate service path (Alembic, webhooks/unsubscribe via
  ``get_service_db``, the brevo events poller, ``tenant_of``).  Isolation
  therefore only bites when the runtime connects as the non-owner
  ``app_user`` role (scripts/bootstrap_db.sql + APP_DATABASE_URL) — the
  startup check in app/main.py warns loudly when it doesn't.

Global tables (webhook_events, linkedin_profile_cache,
funding_source_state) and the identity layer (tenants, users, memberships,
user_sessions, auth_tokens) carry NO policies by design.
"""
from typing import Union

from alembic import op

revision: str = "0046"
down_revision: Union[str, None] = "0045"
branch_labels = None
depends_on = None

# Same 40 tables as 0044/0045 (lists duplicated — alembic version files
# aren't importable from each other).
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

_PREDICATE = "(tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"


def upgrade() -> None:
    for t in ALL_TENANTED:
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {t} "
            f"USING {_PREDICATE} WITH CHECK {_PREDICATE}"
        )


def downgrade() -> None:
    for t in ALL_TENANTED:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {t}")
        op.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY")
