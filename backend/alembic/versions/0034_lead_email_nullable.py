"""Make leads.email nullable.

Revision ID: 0034
Revises: 0033
Create Date: 2026-06-15

The signal "Find contact" enrichment can resolve a LinkedIn profile for
a decision-maker without an email address.  To stage that as a real CRM
lead (for LinkedIn outreach) we need ``leads.email`` to be optional.
Campaign-bound leads still always carry an email in practice; nothing
relaxes that on the send path (a null-email lead is campaign-less and
never enters compose/send).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: Union[str, None] = "0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("leads", "email", existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    # Cannot safely re-tighten if null-email rows exist; left as a no-op
    # to avoid a failing downgrade.  Backfill + ALTER manually if needed.
    pass
