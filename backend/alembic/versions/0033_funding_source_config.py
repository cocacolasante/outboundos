"""Funding feed runtime config: funding_source_state.enabled + config.

Revision ID: 0033
Revises: 0032
Create Date: 2026-06-15

Moves the nonprofit-discovery feed toggles + config (lookback, states)
off env-only and into ``funding_source_state`` so the Settings →
Discovery panel can edit them live.  The env vars (``USASPENDING_ENABLED``
etc.) become the FIRST-RUN SEED: the worker / API seed these columns
from env when they're NULL, then the DB row is authoritative.

  enabled  BOOLEAN NULL  — runtime on/off (NULL = not yet seeded).
  config   JSONB   NULL  — {lookback_days} | {ruling_lookback_months, states}.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0033"
down_revision: Union[str, None] = "0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "funding_source_state",
        sa.Column("enabled", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "funding_source_state",
        sa.Column("config", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("funding_source_state", "config")
    op.drop_column("funding_source_state", "enabled")
