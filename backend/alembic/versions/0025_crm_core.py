"""CRM core: opportunities, activities, manual leads, lead conversion.

Revision ID: 0025
Revises: 0024
Create Date: 2026-06-11

Salesforce-style CRM layer on top of the outreach engine:

1. ``leads.campaign_id`` becomes NULLABLE — a CRM lead can be created
   manually, outside any campaign.  (Campaign-less leads never enter
   the compose/send pipeline; they're CRM records until the user adds
   them to a campaign via CSV or future assignment.)
2. ``leads.crm_status`` — Salesforce-style lead status: new / working /
   qualified / converted / unqualified.  Existing rows backfill to
   'new'.
3. ``leads.converted_opportunity_id`` — set when the lead is converted;
   links to the opportunity it became.
4. ``crm_opportunities`` — deals.  Stage pipeline (prospecting →
   qualification → proposal → negotiation → closed_won / closed_lost),
   amount, expected close date, probability, contact snapshot (copied
   at conversion so the opp survives lead deletion), source lead link.
5. ``crm_activities`` — manually-logged touches: call / email /
   meeting / note / task.  Attachable to a lead OR an opportunity
   (CHECK enforces at least one parent).  Tasks carry due_at +
   completed_at; calls/emails carry direction.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- enums (pre-create; models reference with create_type=False) ----
    op.execute(
        "CREATE TYPE crm_lead_status AS ENUM "
        "('new', 'working', 'qualified', 'converted', 'unqualified')"
    )
    op.execute(
        "CREATE TYPE opportunity_stage AS ENUM "
        "('prospecting', 'qualification', 'proposal', 'negotiation', "
        "'closed_won', 'closed_lost')"
    )
    op.execute(
        "CREATE TYPE crm_activity_type AS ENUM "
        "('call', 'email', 'meeting', 'note', 'task')"
    )
    op.execute(
        "CREATE TYPE crm_activity_direction AS ENUM ('inbound', 'outbound')"
    )

    # ---- leads: manual creation + conversion ----
    op.alter_column("leads", "campaign_id", nullable=True)
    op.add_column(
        "leads",
        sa.Column(
            "crm_status",
            postgresql.ENUM(
                "new", "working", "qualified", "converted", "unqualified",
                name="crm_lead_status", create_type=False,
            ),
            nullable=False,
            server_default="new",
        ),
    )

    # ---- opportunities ----
    op.create_table(
        "crm_opportunities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "stage",
            postgresql.ENUM(
                "prospecting", "qualification", "proposal", "negotiation",
                "closed_won", "closed_lost",
                name="opportunity_stage", create_type=False,
            ),
            nullable=False,
            server_default="prospecting",
            index=True,
        ),
        sa.Column("amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("close_date", sa.Date(), nullable=True),
        # 0-100; defaulted from the stage on create but user-editable.
        sa.Column("probability", sa.Integer(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        # Set when the stage moves to closed_won / closed_lost.
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("loss_reason", sa.Text(), nullable=True),
        # Contact snapshot — copied from the lead at conversion (or typed
        # for a from-scratch opportunity) so the deal record is complete
        # even if the source lead row is later deleted.
        sa.Column("first_name", sa.Text(), nullable=True),
        sa.Column("last_name", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True, index=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("company", sa.Text(), nullable=True),
        sa.Column("job_title", sa.Text(), nullable=True),
        sa.Column("linkedin_url", sa.Text(), nullable=True),
        # Link back to the source lead — SET NULL so deleting the lead
        # doesn't take the deal down with it.
        sa.Column(
            "source_lead_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leads.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )

    # leads.converted_opportunity_id (added after crm_opportunities exists).
    op.add_column(
        "leads",
        sa.Column(
            "converted_opportunity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("crm_opportunities.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # ---- activities ----
    op.create_table(
        "crm_activities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "lead_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leads.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "opportunity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("crm_opportunities.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "activity_type",
            postgresql.ENUM(
                "call", "email", "meeting", "note", "task",
                name="crm_activity_type", create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        # inbound/outbound — meaningful for calls + emails, NULL otherwise.
        sa.Column(
            "direction",
            postgresql.ENUM(
                "inbound", "outbound",
                name="crm_activity_direction", create_type=False,
            ),
            nullable=True,
        ),
        # Task scheduling: due_at set on tasks (and optionally meetings);
        # completed_at flips when the user checks it off.
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.CheckConstraint(
            "lead_id IS NOT NULL OR opportunity_id IS NOT NULL",
            name="ck_crm_activities_has_parent",
        ),
    )
    # Upcoming-tasks hot query: open tasks ordered by due date.
    op.create_index(
        "ix_crm_activities_open_tasks",
        "crm_activities",
        ["due_at"],
        postgresql_where=sa.text(
            "activity_type = 'task' AND completed_at IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_crm_activities_open_tasks", table_name="crm_activities")
    op.drop_table("crm_activities")
    op.drop_column("leads", "converted_opportunity_id")
    op.drop_table("crm_opportunities")
    op.drop_column("leads", "crm_status")
    op.alter_column("leads", "campaign_id", nullable=False)
    op.execute("DROP TYPE crm_activity_direction")
    op.execute("DROP TYPE crm_activity_type")
    op.execute("DROP TYPE opportunity_stage")
    op.execute("DROP TYPE crm_lead_status")
