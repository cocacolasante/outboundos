"""Add the 'email_reply' sequence node kind.

Revision ID: 0035
Revises: 0034
Create Date: 2026-06-15

A new node kind that replies in-thread to the lead's original campaign
email instead of starting a new thread.  ``ALTER TYPE ... ADD VALUE`` is
safe inside Alembic's transaction on PG 12+ as long as the new value
isn't USED in the same migration (it isn't — node config lives in the
existing JSONB column, no schema change).  Downgrade is a no-op: Postgres
can't drop an enum value.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0035"
down_revision: Union[str, None] = "0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE sequence_node_kind ADD VALUE IF NOT EXISTS 'email_reply'")


def downgrade() -> None:
    # Postgres cannot drop an enum value; documented no-op.
    pass
