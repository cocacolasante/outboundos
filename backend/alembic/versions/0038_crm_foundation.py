"""CRM foundation: configurable pipeline/stages, accounts/contacts, stage-change
audit, saved report definitions, owner/tenant columns.

Revision ID: 0038
Revises: 0037
Create Date: 2026-06-18

Phase 1 of the CRM extension (see ``docs/crm-extension-audit.md``).  ALL
ADDITIVE + reversible:

- New tables: ``pipelines``, ``opportunity_stages``, ``accounts``,
  ``contacts``, ``opportunity_stage_changes``, ``report_definitions`` — each
  with a nullable ``tenant_id`` (tenancy-ready, not enforced).
- New nullable columns on existing tables: ``crm_opportunities`` gains
  ``pipeline_id`` / ``stage_id`` / ``account_id`` / ``contact_id`` /
  ``owner_id`` / ``tenant_id``; ``crm_activities`` gains ``account_id`` /
  ``contact_id`` / ``owner_id`` / ``tenant_id``; ``leads`` gains ``tenant_id``.
- The legacy ``opportunity_stage`` enum column on ``crm_opportunities`` is
  KEPT and dual-written for back-compat — nothing is dropped.
- Seeds one ``is_default`` pipeline + 6 stages mirroring the legacy enum
  (same keys/order/probabilities/won-lost flags) and back-fills every existing
  opportunity's ``pipeline_id`` + ``stage_id`` from its ``stage`` enum value.

No data is destroyed; downgrade drops only the new objects/columns.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0038"
down_revision: Union[str, None] = "0037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _ts(col: str) -> sa.Column:
    return sa.Column(
        col, sa.DateTime(timezone=True),
        server_default=sa.text("now()"), nullable=False,
    )


def upgrade() -> None:
    # ---------------------------------------------------------------- pipelines
    op.create_table(
        "pipelines",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("ix_pipelines_tenant_id", "pipelines", ["tenant_id"])

    # -------------------------------------------------------- opportunity_stages
    op.create_table(
        "opportunity_stages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "pipeline_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pipelines.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("default_probability", sa.Integer(), nullable=True),
        sa.Column("is_won", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_lost", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("ix_opportunity_stages_tenant_id", "opportunity_stages", ["tenant_id"])
    op.create_index("ix_opportunity_stages_pipeline_id", "opportunity_stages", ["pipeline_id"])
    op.create_index(
        "ix_opportunity_stages_pipeline_order", "opportunity_stages",
        ["pipeline_id", "sort_order"],
    )

    # ----------------------------------------------------------------- accounts
    op.create_table(
        "accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=True),
        sa.Column("website", sa.Text(), nullable=True),
        sa.Column("industry", sa.Text(), nullable=True),
        sa.Column("size_hint", sa.Text(), nullable=True),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("ix_accounts_tenant_id", "accounts", ["tenant_id"])
    op.create_index("ix_accounts_domain", "accounts", ["domain"])
    op.create_index("ix_accounts_owner_id", "accounts", ["owner_id"])

    # ----------------------------------------------------------------- contacts
    op.create_table(
        "contacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "account_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("first_name", sa.Text(), nullable=True),
        sa.Column("last_name", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("job_title", sa.Text(), nullable=True),
        sa.Column("linkedin_url", sa.Text(), nullable=True),
        sa.Column(
            "source_lead_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leads.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("ix_contacts_tenant_id", "contacts", ["tenant_id"])
    op.create_index("ix_contacts_account_id", "contacts", ["account_id"])
    op.create_index("ix_contacts_email", "contacts", ["email"])
    op.create_index("ix_contacts_source_lead_id", "contacts", ["source_lead_id"])
    op.create_index("ix_contacts_owner_id", "contacts", ["owner_id"])

    # ------------------------------------------------ opportunity_stage_changes
    op.create_table(
        "opportunity_stage_changes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "opportunity_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("crm_opportunities.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("from_stage_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("to_stage_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("from_stage_key", sa.Text(), nullable=True),
        sa.Column("to_stage_key", sa.Text(), nullable=True),
        sa.Column("changed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source", sa.Text(), server_default="user", nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        _ts("created_at"),
    )
    op.create_index("ix_opp_stage_changes_tenant_id", "opportunity_stage_changes", ["tenant_id"])
    op.create_index("ix_opp_stage_changes_opp_id", "opportunity_stage_changes", ["opportunity_id"])
    op.create_index(
        "ix_opp_stage_changes_opp_time", "opportunity_stage_changes",
        ["opportunity_id", "created_at"],
    )

    # ---------------------------------------------------------- report_definitions
    op.create_table(
        "report_definitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("data_source", sa.Text(), nullable=False),
        sa.Column(
            "definition", postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("ix_report_definitions_tenant_id", "report_definitions", ["tenant_id"])
    op.create_index("ix_report_definitions_owner_id", "report_definitions", ["owner_id"])

    # ----------------------------- additive columns on crm_opportunities --------
    op.add_column(
        "crm_opportunities",
        sa.Column(
            "pipeline_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pipelines.id", ondelete="SET NULL"), nullable=True,
        ),
    )
    op.add_column(
        "crm_opportunities",
        sa.Column(
            "stage_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("opportunity_stages.id", ondelete="SET NULL"), nullable=True,
        ),
    )
    op.add_column(
        "crm_opportunities",
        sa.Column(
            "account_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True,
        ),
    )
    op.add_column(
        "crm_opportunities",
        sa.Column(
            "contact_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True,
        ),
    )
    op.add_column("crm_opportunities", sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("crm_opportunities", sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index("ix_crm_opportunities_pipeline_id", "crm_opportunities", ["pipeline_id"])
    op.create_index("ix_crm_opportunities_stage_id", "crm_opportunities", ["stage_id"])
    op.create_index("ix_crm_opportunities_account_id", "crm_opportunities", ["account_id"])
    op.create_index("ix_crm_opportunities_contact_id", "crm_opportunities", ["contact_id"])
    op.create_index("ix_crm_opportunities_owner_id", "crm_opportunities", ["owner_id"])
    op.create_index("ix_crm_opportunities_tenant_id", "crm_opportunities", ["tenant_id"])

    # ------------------------------- additive columns on crm_activities ---------
    op.add_column(
        "crm_activities",
        sa.Column(
            "account_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=True,
        ),
    )
    op.add_column(
        "crm_activities",
        sa.Column(
            "contact_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=True,
        ),
    )
    op.add_column("crm_activities", sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("crm_activities", sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index("ix_crm_activities_account_id", "crm_activities", ["account_id"])
    op.create_index("ix_crm_activities_contact_id", "crm_activities", ["contact_id"])
    op.create_index("ix_crm_activities_owner_id", "crm_activities", ["owner_id"])
    op.create_index("ix_crm_activities_tenant_id", "crm_activities", ["tenant_id"])

    # --------------------------------------- additive column on leads -----------
    op.add_column("leads", sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index("ix_leads_tenant_id", "leads", ["tenant_id"])

    # ----------------------------- seed default pipeline + stages ---------------
    # gen_random_uuid() is built-in on PG13+ (this app is PG15).
    op.execute(
        "INSERT INTO pipelines (id, name, is_default, created_at, updated_at) "
        "VALUES (gen_random_uuid(), 'Default', true, now(), now())"
    )
    # (key, name, sort, probability, is_won, is_lost) mirroring the legacy enum
    # + STAGE_DEFAULT_PROBABILITY + CLOSED_STAGES.
    stages = [
        ("prospecting", "Prospecting", 0, 10, "false", "false"),
        ("qualification", "Qualification", 1, 25, "false", "false"),
        ("proposal", "Proposal", 2, 50, "false", "false"),
        ("negotiation", "Negotiation", 3, 75, "false", "false"),
        ("closed_won", "Closed Won", 4, 100, "true", "false"),
        ("closed_lost", "Closed Lost", 5, 0, "false", "true"),
    ]
    for key, name, order, prob, won, lost in stages:
        op.execute(
            "INSERT INTO opportunity_stages "
            "(id, pipeline_id, name, key, sort_order, default_probability, "
            " is_won, is_lost, is_active, created_at, updated_at) "
            "SELECT gen_random_uuid(), p.id, "
            f"'{name}', '{key}', {order}, {prob}, {won}, {lost}, true, now(), now() "
            "FROM pipelines p WHERE p.is_default = true"
        )

    # ----------------------------- backfill existing opportunities --------------
    op.execute(
        "UPDATE crm_opportunities o "
        "SET pipeline_id = p.id, stage_id = s.id "
        "FROM pipelines p "
        "JOIN opportunity_stages s ON s.pipeline_id = p.id "
        "WHERE p.is_default = true AND s.key = o.stage::text"
    )


def downgrade() -> None:
    # Drop additive columns first (their single-column indexes drop with them).
    op.drop_index("ix_leads_tenant_id", table_name="leads")
    op.drop_column("leads", "tenant_id")

    for ix in (
        "ix_crm_activities_account_id", "ix_crm_activities_contact_id",
        "ix_crm_activities_owner_id", "ix_crm_activities_tenant_id",
    ):
        op.drop_index(ix, table_name="crm_activities")
    op.drop_column("crm_activities", "tenant_id")
    op.drop_column("crm_activities", "owner_id")
    op.drop_column("crm_activities", "contact_id")
    op.drop_column("crm_activities", "account_id")

    for ix in (
        "ix_crm_opportunities_pipeline_id", "ix_crm_opportunities_stage_id",
        "ix_crm_opportunities_account_id", "ix_crm_opportunities_contact_id",
        "ix_crm_opportunities_owner_id", "ix_crm_opportunities_tenant_id",
    ):
        op.drop_index(ix, table_name="crm_opportunities")
    op.drop_column("crm_opportunities", "tenant_id")
    op.drop_column("crm_opportunities", "owner_id")
    op.drop_column("crm_opportunities", "contact_id")
    op.drop_column("crm_opportunities", "account_id")
    op.drop_column("crm_opportunities", "stage_id")
    op.drop_column("crm_opportunities", "pipeline_id")

    # Drop new tables in FK-dependency order.
    op.drop_table("report_definitions")
    op.drop_table("opportunity_stage_changes")
    op.drop_table("contacts")
    op.drop_table("accounts")
    op.drop_table("opportunity_stages")
    op.drop_table("pipelines")
