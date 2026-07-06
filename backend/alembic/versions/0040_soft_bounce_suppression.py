"""Add 'soft_bounce' to the suppression-reason enum.

Revision ID: 0040
Revises: 0039
Create Date: 2026-06-19

Soft bounces are transient, but to protect sender reputation we now suppress an
address once its soft-bounce count reaches SOFT_BOUNCE_SUPPRESS_THRESHOLD
(default 1).  That needs a distinct suppression reason.  (email_event_type
already has 'soft_bounce'.)  Downgrade is a no-op — Postgres can't drop an enum
value.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0040"
down_revision: Union[str, None] = "0039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE suppression_reason ADD VALUE IF NOT EXISTS 'soft_bounce'")


def downgrade() -> None:
    # Postgres cannot drop an enum value; documented no-op.
    pass
