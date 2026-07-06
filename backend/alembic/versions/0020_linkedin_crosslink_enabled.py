"""linkedin_crosslink_enabled column on social_listening_searches.

Revision ID: 0020
Revises: 0019
Create Date: 2026-06-01

Adds a per-search opt-out for the Reddit → LinkedIn cross-link
extraction phase.  Default true — the feature has zero Anthropic
cost, so it's on by default for every search that runs the Reddit
source.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "social_listening_searches",
        sa.Column(
            "linkedin_crosslink_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("social_listening_searches", "linkedin_crosslink_enabled")
