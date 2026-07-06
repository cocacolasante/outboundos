"""Multi-tenancy Phase 6: team invites.

Adds the ``invite`` purpose to auth_tokens (PG 12+ allows ADD VALUE
inside Alembic's transaction as long as the value isn't used in the same
migration).  Downgrade is a documented no-op — Postgres can't drop enum
values (same convention as 0009's research_mode 'none').
"""
from typing import Union

from alembic import op

revision: str = "0049"
down_revision: Union[str, None] = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE auth_token_purpose ADD VALUE IF NOT EXISTS 'invite'")


def downgrade() -> None:
    # Postgres can't drop enum values; harmless to leave in place.
    pass
