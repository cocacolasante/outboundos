"""Multi-source discovery: reddit / twitter / linkedin.

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-01

Anthropic web_search has poor recall on LinkedIn posts (the platform
blocks indexing); Reddit and X/Twitter are much more reliably indexed.
This migration:
  1. Adds ``reddit`` and ``twitter`` to ``social_post_provider``.
  2. Adds a ``sources`` JSONB array on ``social_listening_searches``
     so a single search can fan out across multiple platforms.  Backfilled
     from the existing ``source`` column (every old row becomes a
     single-element list).

The single ``source`` enum column is kept for now as a deprecated
backward-compat shim.  A follow-up can drop it once frontend / API
clients stop reading it.

``ALTER TYPE ... ADD VALUE`` is safe inside Alembic's transaction on
PG 12+ provided we don't USE the new value in the same transaction —
the backfill only references existing values so this is fine.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Extend the enum with the two new values.
    op.execute("ALTER TYPE social_post_provider ADD VALUE IF NOT EXISTS 'reddit'")
    op.execute("ALTER TYPE social_post_provider ADD VALUE IF NOT EXISTS 'twitter'")

    # 2. New sources column.  Default ``["linkedin"]`` so existing rows
    # don't break the API contract (frontend will see a populated list).
    op.add_column(
        "social_listening_searches",
        sa.Column(
            "sources",
            postgresql.JSONB(),
            nullable=False,
            server_default='["linkedin"]',
        ),
    )

    # Backfill: copy the single ``source`` enum into a one-element list.
    op.execute(
        "UPDATE social_listening_searches "
        "SET sources = jsonb_build_array(source::text) "
        "WHERE sources = '[\"linkedin\"]'::jsonb AND source IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("social_listening_searches", "sources")
    # The enum values themselves can't be dropped without recreating the
    # type + rewriting dependent columns; leaving them is harmless.
