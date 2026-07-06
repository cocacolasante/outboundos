"""Deferred-enrichment queue for funding discovery.

Revision ID: 0036
Revises: 0035
Create Date: 2026-06-15

Orgs that resolve NO contact at discovery time must NOT flood the
``prospect_signals`` review queue.  They park in ``funding_enrichment_queue``
(status=pending) and a daily retry worker re-attempts contact resolution,
promoting a row to a real signal once a contact resolves (reusing the
ORIGINAL dedup_key so promotion can never double-emit) or exhausting it
(optionally into a direct-mail task) after a few tries.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0036"
down_revision: Union[str, None] = "0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATUS = postgresql.ENUM(
    "pending", "resolved", "exhausted", "mailed",
    name="funding_enrichment_status",
)


def upgrade() -> None:
    bind = op.get_bind()
    _STATUS.create(bind, checkfirst=True)
    op.create_table(
        "funding_enrichment_queue",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("ein", sa.Text(), nullable=True),
        sa.Column("dedup_key", sa.Text(), nullable=False),
        sa.Column("org_name", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=True),
        sa.Column("ntee_code", sa.Text(), nullable=True),
        sa.Column("website", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(
                "pending", "resolved", "exhausted", "mailed",
                name="funding_enrichment_status", create_type=False,
            ),
            nullable=False, server_default="pending",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    op.create_index(
        "ix_funding_enrichment_queue_dedup_key",
        "funding_enrichment_queue", ["dedup_key"], unique=True,
    )
    op.create_index(
        "ix_funding_enrichment_queue_next_attempt_at",
        "funding_enrichment_queue", ["next_attempt_at"],
    )
    op.create_index(
        "ix_funding_enrichment_queue_status",
        "funding_enrichment_queue", ["status"],
    )


def downgrade() -> None:
    op.drop_index("ix_funding_enrichment_queue_status", table_name="funding_enrichment_queue")
    op.drop_index("ix_funding_enrichment_queue_next_attempt_at", table_name="funding_enrichment_queue")
    op.drop_index("ix_funding_enrichment_queue_dedup_key", table_name="funding_enrichment_queue")
    op.drop_table("funding_enrichment_queue")
    _STATUS.drop(op.get_bind(), checkfirst=True)
