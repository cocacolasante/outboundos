"""Add 'blocked' to the email-event and suppression-reason enums.

Revision ID: 0039
Revises: 0038
Create Date: 2026-06-19

Brevo's transactional data distinguishes a *blocked* recipient (admin-
blocked / blocklisted) from a hard bounce or spam complaint.  We sync that
list into the suppression (ignore) list and also handle the real-time
``blocked`` transactional event, so both enums need the new value.

``ALTER TYPE ... ADD VALUE`` is safe inside Alembic's transaction on PG 12+
as long as the new value isn't USED in the same migration (it isn't — no
rows are written here).  Downgrade is a no-op: Postgres can't drop an enum
value.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0039"
down_revision: Union[str, None] = "0038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE email_event_type ADD VALUE IF NOT EXISTS 'blocked'")
    op.execute("ALTER TYPE suppression_reason ADD VALUE IF NOT EXISTS 'blocked'")


def downgrade() -> None:
    # Postgres cannot drop an enum value; documented no-op.
    pass
