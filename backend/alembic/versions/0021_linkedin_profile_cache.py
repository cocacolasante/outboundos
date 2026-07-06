"""linkedin_profile_cache table — persistent slug → URN map.

Revision ID: 0021
Revises: 0020
Create Date: 2026-06-01

Unipile's ``GET /api/v1/users/{slug}`` is the only way to map a public
LinkedIn slug (``jane-doe``) to the canonical member URN
(``ACoAA...``).  Every other Unipile endpoint that touches a profile
needs the URN, including ``recent_posts`` used by the Social Radar
watchlist.  The slug→URN mapping is permanent — once known, it never
changes.  But the worker re-resolves it on EVERY watchlist run.

This table caches the mapping so subsequent runs make ONE Unipile call
per profile (the actual posts fetch) instead of TWO (resolve + fetch).
At 200+ profiles, that's the difference between ~13min runs and ~6min
runs.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "linkedin_profile_cache",
        sa.Column("slug", sa.Text(), primary_key=True),
        sa.Column("provider_id", sa.Text(), nullable=False),
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("linkedin_profile_cache")
