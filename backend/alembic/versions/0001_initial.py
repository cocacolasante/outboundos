"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-05-12

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "connected_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("email_address", sa.Text(), nullable=False),
        sa.Column("imap_host", sa.Text(), nullable=False),
        sa.Column("imap_port", sa.Integer(), nullable=False, server_default="993"),
        sa.Column("imap_use_ssl", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("password_encrypted", sa.Text(), nullable=False),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_test_status",
            sa.Enum("untested", "ok", "failed", name="connected_account_test_status"),
            nullable=False,
            server_default="untested",
        ),
        sa.Column("last_test_error", sa.Text(), nullable=True),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_connected_accounts_email_address", "connected_accounts", ["email_address"])

    op.create_table(
        "campaigns",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("tone", sa.Text(), nullable=False),
        sa.Column("sender_name", sa.Text(), nullable=False),
        sa.Column("sender_email", sa.Text(), nullable=False),
        sa.Column(
            "research_mode",
            sa.Enum("fast", "deep", name="research_mode"),
            nullable=False,
            server_default="fast",
        ),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="5"),
        sa.Column(
            "connected_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("connected_accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "schedule_days",
            postgresql.ARRAY(sa.Integer()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("schedule_time_start", sa.Time(), nullable=False),
        sa.Column("schedule_time_end", sa.Time(), nullable=False),
        sa.Column("schedule_timezone", sa.String(), nullable=False, server_default="UTC"),
        sa.Column("max_per_hour", sa.Integer(), nullable=True),
        sa.Column("max_per_day", sa.Integer(), nullable=True),
        sa.Column("min_delay_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.Column(
            "status",
            sa.Enum(
                "draft", "previewing", "approved", "running", "paused", "complete",
                name="campaign_status",
            ),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    op.create_table(
        "leads",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "campaign_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("campaigns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("linkedin_url", sa.Text(), nullable=True),
        sa.Column("first_name", sa.Text(), nullable=True),
        sa.Column("last_name", sa.Text(), nullable=True),
        sa.Column("company", sa.Text(), nullable=True),
        sa.Column("job_title", sa.Text(), nullable=True),
        sa.Column("raw_csv_row", postgresql.JSONB(), nullable=True),
        sa.Column("research_data", postgresql.JSONB(), nullable=True),
        sa.Column("composed_subject", sa.Text(), nullable=True),
        sa.Column("composed_body", sa.Text(), nullable=True),
        sa.Column(
            "research_status",
            sa.Enum("pending", "running", "done", "failed", "skipped", name="research_status"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "compose_status",
            sa.Enum("pending", "running", "done", "failed", name="compose_status"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "send_status",
            sa.Enum("pending", "scheduled", "sent", "failed", name="send_status"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("is_sample", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sample_approved", sa.Boolean(), nullable=True),
        sa.Column("style_correction", sa.Text(), nullable=True),
        sa.Column("brevo_message_id", sa.Text(), nullable=True),
        sa.Column("scheduled_send_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_leads_campaign_id", "leads", ["campaign_id"])
    op.create_index("ix_leads_email", "leads", ["email"])
    op.create_index("ix_leads_send_status", "leads", ["send_status"])

    op.create_table(
        "email_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "lead_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "campaign_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("campaigns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "event_type",
            sa.Enum(
                "delivered", "opened", "clicked", "soft_bounce", "hard_bounce",
                "spam", "unsubscribed", "replied",
                name="email_event_type",
            ),
            nullable=False,
        ),
        sa.Column("event_data", postgresql.JSONB(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_email_events_campaign_id", "email_events", ["campaign_id"])
    op.create_index("ix_email_events_lead_id", "email_events", ["lead_id"])

    op.create_table(
        "suppression_list",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column(
            "reason",
            sa.Enum("unsubscribed", "hard_bounce", "spam", "manual", name="suppression_reason"),
            nullable=False,
        ),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("email", name="uq_suppression_list_email"),
    )
    op.create_index("ix_suppression_list_email", "suppression_list", ["email"])

    op.create_table(
        "style_corrections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "campaign_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("campaigns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("original_body", sa.Text(), nullable=False),
        sa.Column("corrected_body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_style_corrections_campaign_id", "style_corrections", ["campaign_id"])


def downgrade() -> None:
    op.drop_index("ix_style_corrections_campaign_id", table_name="style_corrections")
    op.drop_table("style_corrections")

    op.drop_index("ix_suppression_list_email", table_name="suppression_list")
    op.drop_table("suppression_list")

    op.drop_index("ix_email_events_lead_id", table_name="email_events")
    op.drop_index("ix_email_events_campaign_id", table_name="email_events")
    op.drop_table("email_events")

    op.drop_index("ix_leads_send_status", table_name="leads")
    op.drop_index("ix_leads_email", table_name="leads")
    op.drop_index("ix_leads_campaign_id", table_name="leads")
    op.drop_table("leads")

    op.drop_table("campaigns")

    op.drop_index("ix_connected_accounts_email_address", table_name="connected_accounts")
    op.drop_table("connected_accounts")

    bind = op.get_bind()
    for name in (
        "email_event_type",
        "send_status",
        "compose_status",
        "research_status",
        "campaign_status",
        "research_mode",
        "suppression_reason",
        "connected_account_test_status",
    ):
        sa.Enum(name=name).drop(bind, checkfirst=True)
