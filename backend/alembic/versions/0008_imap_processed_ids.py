"""Add processed_imap_message_ids to connected_accounts for poll dedup.

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-20

Lets the reply poller switch from "UNSEEN SINCE + mark Seen" to
"SINCE + dedup by Message-ID" — the latter doesn't touch the user's
read/unread state in their actual mailbox, but needs somewhere to
remember which messages it's already turned into REPLIED events so the
same message doesn't re-fire every 20 minutes for the rest of time.

Bounded set: worker trims to the last 500 entries after each poll.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "connected_accounts",
        sa.Column(
            "processed_imap_message_ids",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("connected_accounts", "processed_imap_message_ids")
