"""Per-query run stats on social_listening_searches.

Revision ID: 0016
Revises: 0015
Create Date: 2026-06-01

Adds ``last_run_stats`` JSONB so the detail view can show why each
expanded query did or didn't produce posts.  Shape:
  {
    "queries": [
      {"query": "...", "raw": 5, "kept": 3,
       "dropped_invalid_url": 0, "dropped_excluded": 0,
       "dropped_duplicate": 0, "dropped_undated": 1, "dropped_stale": 1}
    ],
    "summary": {"total_queries": 20, "total_raw": 100, ...}
  }
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "social_listening_searches",
        sa.Column(
            "last_run_stats",
            postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("social_listening_searches", "last_run_stats")
