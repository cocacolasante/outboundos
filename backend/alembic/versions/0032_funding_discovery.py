"""Nonprofit funding discovery: prospect_signals discovery path + funding_source_state.

Revision ID: 0032
Revises: 0031
Create Date: 2026-06-15

Adds a discovery PATH (not a new subsystem) that feeds the existing
``prospect_signals`` review queue from external nonprofit feeds
(USAspending grant awards, IRS EO BMF new 501(c)(3) rulings):

  - ``prospect_signals.watch_id`` becomes NULLABLE — discovery signals
    have no SignalWatch behind them.
  - ``prospect_signals.source`` (indexed) tags the feed:
    'usaspending' | 'irs_bmf' | NULL (= watch-sourced).
  - ``funding_source_state`` holds the per-source cursor / diff baseline
    — the same idea as ``SignalWatch.last_seen``.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0032"
down_revision: Union[str, None] = "0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "prospect_signals", "watch_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.add_column(
        "prospect_signals",
        sa.Column("source", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_prospect_signals_source", "prospect_signals", ["source"],
    )
    op.create_table(
        "funding_source_state",
        sa.Column("source", sa.Text(), primary_key=True),
        sa.Column("cursor", postgresql.JSONB(), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_status", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("funding_source_state")
    op.drop_index("ix_prospect_signals_source", table_name="prospect_signals")
    op.drop_column("prospect_signals", "source")
    # Restore NOT NULL.  (Safe only if no discovery rows linger; this is
    # the documented reverse of the upgrade.)
    op.alter_column(
        "prospect_signals", "watch_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
