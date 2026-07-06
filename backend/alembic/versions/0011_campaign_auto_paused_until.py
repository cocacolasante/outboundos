"""Add auto_paused_until to campaigns (LinkedIn-cap auto-pause/resume).

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-26

When a LinkedIn-only campaign hits its daily cap, the sequencer auto-pauses
it (status -> PAUSED) and stamps ``auto_paused_until`` with the cap-reset
time.  The beat auto-resumes it once that time passes.  NULL means the
campaign was NOT auto-paused (manual pauses stay paused).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column("auto_paused_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("campaigns", "auto_paused_until")
