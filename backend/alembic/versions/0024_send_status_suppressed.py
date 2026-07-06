"""Add 'suppressed' to the send_status enum.

Revision ID: 0024
Revises: 0023
Create Date: 2026-06-09

A lead whose email is on the suppression list used to be flipped to
``send_status=FAILED`` when its queued send task hit the suppression
gate.  That misclassified deliberate ignores as delivery failures: they
polluted the campaign error list and the retry-failed endpoint
re-enqueued them in a deterministic fail loop.

``suppressed`` is now its own terminal state.  /errors and retry-failed
filter on FAILED, so suppressed leads naturally drop out of both.

PostgreSQL 12+ allows ``ALTER TYPE ... ADD VALUE`` inside Alembic's
transaction as long as the new value isn't used in the same migration.
Downgrade is a documented no-op (Postgres can't drop enum values).
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE send_status ADD VALUE IF NOT EXISTS 'suppressed'")


def downgrade() -> None:
    # Postgres cannot drop enum values; leaving the value in place is
    # harmless (no rows reference it after a code rollback).
    pass
