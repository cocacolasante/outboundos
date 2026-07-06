"""ICP lookalike expansion: icp_profiles + lookalike_candidates.

Revision ID: 0031
Revises: 0030
Create Date: 2026-06-12

Feature D: derive an ideal-customer fingerprint from closed_won deals,
discover firmographically similar prospects, and stage them in a
review queue.  ``notification_kind`` gains ``lookalike_batch`` for the
"N new candidates" owner alert.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0031"
down_revision: Union[str, None] = "0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE TYPE icp_profile_source AS ENUM ('auto_closed_won', 'manual')"
    )
    op.execute(
        "CREATE TYPE icp_profile_status AS ENUM ('ready', 'insufficient_data')"
    )
    op.execute(
        "CREATE TYPE lookalike_candidate_status AS ENUM "
        "('new', 'accepted', 'rejected')"
    )
    op.execute(
        "ALTER TYPE notification_kind ADD VALUE IF NOT EXISTS 'lookalike_batch'"
    )

    op.create_table(
        "icp_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "source",
            postgresql.ENUM(
                "auto_closed_won", "manual",
                name="icp_profile_source", create_type=False,
            ),
            nullable=False,
            server_default="auto_closed_won",
        ),
        sa.Column(
            "status",
            postgresql.ENUM(
                "ready", "insufficient_data",
                name="icp_profile_status", create_type=False,
            ),
            nullable=False,
            server_default="ready",
        ),
        sa.Column("criteria", postgresql.JSONB(), nullable=False),
        sa.Column("won_deal_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "refreshed_at", sa.DateTime(timezone=True),
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
    )

    op.create_table(
        "lookalike_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "icp_profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("icp_profiles.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("company", sa.Text(), nullable=False),
        sa.Column("company_website", sa.Text(), nullable=True),
        sa.Column("contact_name", sa.Text(), nullable=True),
        sa.Column("job_title", sa.Text(), nullable=True),
        sa.Column("linkedin_url", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("fit_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fit_reason", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("dedup_key", sa.Text(), nullable=False, unique=True, index=True),
        sa.Column(
            "status",
            postgresql.ENUM(
                "new", "accepted", "rejected",
                name="lookalike_candidate_status", create_type=False,
            ),
            nullable=False,
            server_default="new",
            index=True,
        ),
        sa.Column(
            "created_lead_id",
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


def downgrade() -> None:
    op.drop_table("lookalike_candidates")
    op.drop_table("icp_profiles")
    op.execute("DROP TYPE lookalike_candidate_status")
    op.execute("DROP TYPE icp_profile_status")
    op.execute("DROP TYPE icp_profile_source")
    # notification_kind value can't be dropped — documented no-op.
