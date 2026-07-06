"""Intent/trigger prospecting: signal_watches + prospect_signals.

Revision ID: 0030
Revises: 0029
Create Date: 2026-06-12

Feature C: job-change / funding / hiring signals on tracked leads,
opportunities, and cold targets.  ``signal_watches.frequency`` reuses
the existing ``social_search_frequency`` enum.  ``notification_kind``
gains ``prospect_signal`` for the owner alert.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE TYPE signal_watch_type AS ENUM "
        "('job_change', 'funding', 'hiring', 'custom')"
    )
    op.execute(
        "CREATE TYPE signal_watch_status AS ENUM ('active', 'paused')"
    )
    op.execute(
        "CREATE TYPE prospect_signal_status AS ENUM "
        "('new', 'actioned', 'dismissed')"
    )
    op.execute(
        "ALTER TYPE notification_kind ADD VALUE IF NOT EXISTS 'prospect_signal'"
    )

    op.create_table(
        "signal_watches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "watch_type",
            postgresql.ENUM(
                "job_change", "funding", "hiring", "custom",
                name="signal_watch_type", create_type=False,
            ),
            nullable=False,
        ),
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
        sa.Column("person_name", sa.Text(), nullable=True),
        sa.Column("company", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("linkedin_url", sa.Text(), nullable=True),
        sa.Column("company_website", sa.Text(), nullable=True),
        sa.Column(
            "frequency",
            postgresql.ENUM(
                "manual", "every_6h", "every_12h", "daily", "weekly",
                name="social_search_frequency", create_type=False,
            ),
            nullable=False,
            server_default="daily",
        ),
        sa.Column(
            "status",
            postgresql.ENUM(
                "active", "paused",
                name="signal_watch_status", create_type=False,
            ),
            nullable=False,
            server_default="active",
            index=True,
        ),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_status", sa.Text(), nullable=True),
        sa.Column("last_seen", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )

    op.create_table(
        "prospect_signals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "watch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("signal_watches.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("signal_type", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("dedup_key", sa.Text(), nullable=False, unique=True, index=True),
        sa.Column(
            "status",
            postgresql.ENUM(
                "new", "actioned", "dismissed",
                name="prospect_signal_status", create_type=False,
            ),
            nullable=False,
            server_default="new",
            index=True,
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
            "detected_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("prospect_signals")
    op.drop_table("signal_watches")
    op.execute("DROP TYPE prospect_signal_status")
    op.execute("DROP TYPE signal_watch_status")
    op.execute("DROP TYPE signal_watch_type")
    # notification_kind value can't be dropped — documented no-op.
