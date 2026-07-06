"""is_default_sender column on connected_accounts.

Revision ID: 0022
Revises: 0021
Create Date: 2026-06-04

The Research-a-client one-off send picks a from-address.  Without an
in-DB default, it always fell back to ``settings.BREVO_SENDER_EMAIL``
even when the user had multiple connected inboxes they'd prefer.
This column flags exactly one ConnectedAccount as the workspace's
default sender; the send endpoint prefers it over the env var.

A partial unique index enforces "at most one default at a time" at the
DB layer; the router's PATCH handler also clears the flag on every
other row inside the same transaction as a belt-and-suspenders.  Both
guards together mean a concurrent set-default request can never leave
two defaults in place.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "connected_accounts",
        sa.Column(
            "is_default_sender",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Partial unique index: only enforces uniqueness on TRUE rows.  Any
    # number of False rows is fine — that's the common state.
    op.create_index(
        "ix_connected_accounts_single_default_sender",
        "connected_accounts",
        ["is_default_sender"],
        unique=True,
        postgresql_where=sa.text("is_default_sender IS TRUE"),
    )


def downgrade() -> None:
    op.drop_index("ix_connected_accounts_single_default_sender", table_name="connected_accounts")
    op.drop_column("connected_accounts", "is_default_sender")
