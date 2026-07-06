"""Cost-control columns on social_listening_searches.

Revision ID: 0019
Revises: 0018
Create Date: 2026-06-01

Two new per-search controls:
- ``linkedin_web_search_enabled`` (BOOL, default false) — LinkedIn
  web-search on Sonnet is the most expensive part of a run and rarely
  produces results (LinkedIn blocks indexing).  We disable it by default
  and let the watchlist be the reliable LinkedIn channel.  Power users
  can flip it on per-search if they want to keep trying.
- ``max_run_cost_usd`` (NUMERIC(10,4), default 1.0) — soft per-run
  spend cap.  Worker tracks Anthropic token usage during the run and
  aborts before exceeding this cap.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "social_listening_searches",
        sa.Column(
            "linkedin_web_search_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "social_listening_searches",
        sa.Column(
            "max_run_cost_usd",
            sa.Numeric(10, 4),
            nullable=False,
            server_default="1.0",
        ),
    )


def downgrade() -> None:
    op.drop_column("social_listening_searches", "max_run_cost_usd")
    op.drop_column("social_listening_searches", "linkedin_web_search_enabled")
