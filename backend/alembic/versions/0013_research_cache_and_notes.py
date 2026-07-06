"""Add research_cache table + leads.notes column.

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-28

``research_cache`` is a cross-campaign cache of per-email research, so a
second campaign adding the same lead reuses the merged ``research_data``
(if still within ``RESEARCH_CACHE_TTL_DAYS``) instead of re-spending
Anthropic web-search tokens.  ``leads.notes`` powers the lite-CRM Leads
view: free-form text per (campaign × lead).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "research_cache",
        sa.Column("email", sa.Text(), primary_key=True),
        sa.Column(
            "research_data", sa.dialects.postgresql.JSONB(), nullable=False
        ),
        sa.Column(
            "refreshed_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    op.add_column("leads", sa.Column("notes", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("leads", "notes")
    op.drop_table("research_cache")
