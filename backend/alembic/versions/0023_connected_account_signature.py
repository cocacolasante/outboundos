"""connected_accounts.signature column.

Revision ID: 0023
Revises: 0022
Create Date: 2026-06-08

Per-inbox signature block (contact info, website, calendar link).  The
Research-a-client send endpoint resolves the chosen ConnectedAccount and
applies its signature via ``app.services.signature.apply_signature``
before posting to Brevo.  Empty/null = no signature appended (the
emitted body matches the AI composer's output verbatim).

Mirrors the per-Campaign ``signature`` column added in migration 0012 —
same idempotent ``apply_signature`` helper is reused.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "connected_accounts",
        sa.Column("signature", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("connected_accounts", "signature")
