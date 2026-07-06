"""Deliverability guard: send-time optimization, breaker fields, lead timezone.

Revision ID: 0028
Revises: 0027
Create Date: 2026-06-12

Feature B (deliverability guard):

1. ``campaigns.send_time_optimization`` — defer each send to the
   recipient's optimal local hour within the campaign window.
2. ``campaigns.auto_paused_at`` + ``auto_pause_reason`` — set by the
   bounce/spam circuit breaker.  Unlike ``auto_paused_until`` (LinkedIn
   cap, auto-resumes), a breaker pause requires an explicit human
   Resume, which clears both fields.
3. ``leads.timezone`` — recipient IANA tz from research/enrichment;
   falls back to the campaign tz when NULL.
4. ``notification_kind`` enum gains ``campaign_auto_paused`` for the
   breaker's owner alert.  (PG 12+ allows ADD VALUE inside the Alembic
   txn as long as the value isn't used in the same migration.)
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column(
            "send_time_optimization", sa.Boolean(),
            nullable=False, server_default="false",
        ),
    )
    op.add_column(
        "campaigns",
        sa.Column("auto_paused_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "campaigns",
        sa.Column("auto_pause_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "leads",
        sa.Column("timezone", sa.Text(), nullable=True),
    )
    op.execute(
        "ALTER TYPE notification_kind ADD VALUE IF NOT EXISTS 'campaign_auto_paused'"
    )


def downgrade() -> None:
    # Postgres can't drop enum values — documented no-op for the enum.
    op.drop_column("leads", "timezone")
    op.drop_column("campaigns", "auto_pause_reason")
    op.drop_column("campaigns", "auto_paused_at")
    op.drop_column("campaigns", "send_time_optimization")
