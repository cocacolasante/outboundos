"""Add max_post_age_days to social_listening_searches.

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-01

Caps how far back the discovery service looks.  Default 30 days so any
post older than ~a month isn't pulled.  Per-search configurable so a
"trend research" search can widen to 90+ days while a "real-time intent"
search can tighten to 7.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "social_listening_searches",
        sa.Column(
            "max_post_age_days",
            sa.Integer(),
            nullable=False,
            server_default="30",
        ),
    )


def downgrade() -> None:
    op.drop_column("social_listening_searches", "max_post_age_days")
