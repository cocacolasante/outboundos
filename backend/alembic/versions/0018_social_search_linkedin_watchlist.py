"""LinkedIn profile watchlist on social_listening_searches.

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-01

Web-search discovery of LinkedIn posts is unreliable (LinkedIn blocks
indexing).  The watchlist lets the user explicitly track specific
LinkedIn profiles (e.g. their target prospects' CIO/COO accounts);
the worker fetches each profile's recent posts via Unipile's
``GET /api/v1/users/<id>/posts`` and routes them through qualification.
This gets real, fresh LinkedIn signal that web search can't surface.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "social_listening_searches",
        sa.Column(
            "linkedin_profile_watchlist",
            postgresql.JSONB(),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("social_listening_searches", "linkedin_profile_watchlist")
