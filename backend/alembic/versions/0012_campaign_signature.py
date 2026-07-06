"""Add signature column to campaigns.

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-27

Optional per-campaign email signature (sender name + contact / website /
calendar link).  When set, the compose worker swaps the AI's generated
sign-off for this block, and a bulk endpoint applies it to already-composed
emails.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("campaigns", sa.Column("signature", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("campaigns", "signature")
