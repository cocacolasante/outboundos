"""Add 'none' value to the research_mode enum.

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-26

Adds a third research mode: "no research".  Campaigns set to this mode
skip every external research call (Apollo / Hunter / web) — the compose
worker still runs, but using its generic name+company-only prompt, so
the only API spend is a single Anthropic call per lead.

PostgreSQL 12+ allows ``ALTER TYPE ... ADD VALUE`` inside a transaction
block as long as the new value isn't *used* in the same transaction
(we don't), so this is safe under Alembic's wrapping transaction on the
PG 15 we run.  Removing an enum value is not supported by Postgres, so
``downgrade`` is a documented no-op.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE research_mode ADD VALUE IF NOT EXISTS 'none'")


def downgrade() -> None:
    # Postgres cannot drop a value from an enum type without recreating the
    # type and rewriting every dependent column.  Leaving the value in place
    # is harmless (nothing references it once campaigns move off 'none').
    pass
