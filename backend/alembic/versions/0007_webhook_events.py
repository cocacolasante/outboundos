"""webhook_events dedup table for inbound webhooks

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-18

Unipile's webhook delivers events at-least-once.  Without a dedup
table, a duplicate ``invitation.accepted`` or ``account.connected``
silently re-applies the handler — e.g. clobbering a manually-set
DECLINED connection back to CONNECTED, or resetting a status the user
just edited.  This table stores ``(provider, event_id)`` pairs we've
already processed; the webhook handler does an insert-or-skip up front
and exits early on duplicates.

Schema is generic enough to cover the Brevo events poller too if we
later want a second-layer dedup beyond the in-app
``brevo_events.process_event`` deduper.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "webhook_events",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("provider", "event_id", name="uq_webhook_events_provider_event"),
    )
    op.create_index(
        "ix_webhook_events_received_at",
        "webhook_events",
        ["received_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_events_received_at", table_name="webhook_events")
    op.drop_table("webhook_events")
