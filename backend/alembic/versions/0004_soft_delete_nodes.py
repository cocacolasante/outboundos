"""soft-delete sequence nodes

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-13

Adds ``sequence_nodes.deleted_at`` so graph re-edits can soft-delete the
old topology while keeping historical ``lead_step_executions`` intact —
without this, analytics would lose its underlying data every time the
user reshapes a sequence.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sequence_nodes",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Partial index — only nodes the scheduler should look at. Speeds up the
    # "is this node still live" filter in the advance loop.
    op.create_index(
        "ix_sequence_nodes_live",
        "sequence_nodes",
        ["sequence_id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_sequence_nodes_live", table_name="sequence_nodes")
    op.drop_column("sequence_nodes", "deleted_at")
