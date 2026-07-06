"""Agent core: settings singleton, notifications, audit log, CrmActivity agent fields.

Revision ID: 0027
Revises: 0026
Create Date: 2026-06-12

CRM/inbox agent data layer:

1. ``agent_settings`` — single-row runtime config (autonomy toggles,
   confidence threshold, quiet hours) so the operator can tune the
   agent from the UI without a redeploy.  Bootstrapped lazily on first
   read; no seed row here.
2. ``notifications`` — persisted alert feed for the UI bell.  The
   owner-email alert is a side-channel; the row is the source of
   truth.  ``dedup_key`` UNIQUE makes re-polls/re-sweeps idempotent.
3. ``agent_actions`` — append-only audit of every autonomous decision
   (success / skipped / failed) with model + cost accounting.
4. ``crm_activities`` gains ``reminder_sent_at`` (sweeper idempotency),
   ``sentiment`` (classifier output on inbound emails), and
   ``is_agent_generated`` (UI badge + query filter).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0027"
down_revision: Union[str, None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- enums (pre-create; models reference with create_type=False) ----
    op.execute(
        "CREATE TYPE notification_kind AS ENUM "
        "('positive_reply', 'reply', 'task_due', 'task_overdue', "
        "'stale_opportunity', 'digest', 'agent_error')"
    )
    op.execute(
        "CREATE TYPE agent_action_type AS ENUM "
        "('classify_reply', 'log_activity', 'create_reminder', "
        "'send_notification', 'draft_reply', 'flag_stale_opp', 'digest')"
    )
    op.execute(
        "CREATE TYPE agent_action_status AS ENUM "
        "('success', 'skipped', 'failed')"
    )

    # ---- agent_settings (singleton, id=1) ----
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
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )

    # ---- notifications ----
    op.create_table(
        "notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "kind",
            postgresql.ENUM(
                "positive_reply", "reply", "task_due", "task_overdue",
                "stale_opportunity", "digest", "agent_error",
                name="notification_kind", create_type=False,
            ),
            nullable=False,
            index=True,
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column(
            "lead_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leads.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "opportunity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("crm_opportunities.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "activity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("crm_activities.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("dedup_key", sa.Text(), nullable=False, unique=True, index=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("emailed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )

    # ---- agent_actions ----
    op.create_table(
        "agent_actions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "action_type",
            postgresql.ENUM(
                "classify_reply", "log_activity", "create_reminder",
                "send_notification", "draft_reply", "flag_stale_opp", "digest",
                name="agent_action_type", create_type=False,
            ),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(
                "success", "skipped", "failed",
                name="agent_action_status", create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "lead_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leads.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "opportunity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("crm_opportunities.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "activity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("crm_activities.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
            index=True,
        ),
    )

    # ---- crm_activities agent fields ----
    op.add_column(
        "crm_activities",
        sa.Column("reminder_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "crm_activities",
        sa.Column("sentiment", sa.Text(), nullable=True),
    )
    op.add_column(
        "crm_activities",
        sa.Column(
            "is_agent_generated", sa.Boolean(), nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("crm_activities", "is_agent_generated")
    op.drop_column("crm_activities", "sentiment")
    op.drop_column("crm_activities", "reminder_sent_at")
    op.drop_table("agent_actions")
    op.drop_table("notifications")
    op.drop_table("agent_settings")
    op.execute("DROP TYPE agent_action_status")
    op.execute("DROP TYPE agent_action_type")
    op.execute("DROP TYPE notification_kind")
