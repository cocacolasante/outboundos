"""Reply-driven copy loop: reply_outcomes + campaign_copy_insights.

Revision ID: 0029
Revises: 0028
Create Date: 2026-06-12

Feature A: capture which composed copy earned positive/negative replies
(snapshot per classified inbound reply) and cache a per-campaign LLM
summary of the winning angles for injection into compose prompts.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "reply_outcomes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "lead_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leads.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "campaign_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("campaigns.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("sentiment", sa.Text(), nullable=False, index=True),
        sa.Column("intent", sa.Text(), nullable=True),
        sa.Column("composed_subject", sa.Text(), nullable=True),
        sa.Column("composed_body", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )

    op.create_table(
        "campaign_copy_insights",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "campaign_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("campaigns.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("insights", postgresql.JSONB(), nullable=False),
        sa.Column(
            "outcome_count_at_refresh", sa.Integer(),
            nullable=False, server_default="0",
        ),
        sa.Column(
            "refreshed_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("campaign_copy_insights")
    op.drop_table("reply_outcomes")
