"""Track when a campaign's goal was last edited.

Revision ID: 0037
Revises: 0036
Create Date: 2026-06-15

A ``goal_updated_at`` timestamp stamped by ``update_campaign`` whenever the
goal changes on a non-draft campaign.  Powers the "X of Y emails rewritten
with new goal" progress indicator: with the stamp, the progress endpoint
can count which leads have already been recomposed (their ``updated_at``
is at or after the stamp) vs which are still queued.  Nullable because
campaigns that have never had their goal edited (or were created before
this column existed) carry no rewrite progress to display.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: Union[str, None] = "0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column("goal_updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("campaigns", "goal_updated_at")
