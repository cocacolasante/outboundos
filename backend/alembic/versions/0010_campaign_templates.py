"""Add 'template' research mode + template_subject/body columns.

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-26

Fully-templated campaigns: research_mode == 'template' means the compose
worker renders the campaign's template_subject / template_body with
per-lead merge fields ({{first_name}}, {{Company|there}}, ...) instead of
calling Anthropic.  Zero external API calls — no research, no AI.

``ALTER TYPE ... ADD VALUE`` is run first; the two column adds don't
reference the new value, so this is safe inside Alembic's transaction on
PG 15.  Removing an enum value isn't supported by Postgres, so the
downgrade only drops the columns.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE research_mode ADD VALUE IF NOT EXISTS 'template'")
    op.add_column("campaigns", sa.Column("template_subject", sa.Text(), nullable=True))
    op.add_column("campaigns", sa.Column("template_body", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("campaigns", "template_body")
    op.drop_column("campaigns", "template_subject")
    # Postgres can't drop an enum value without recreating the type; the
    # 'template' label is left in place (harmless once no campaign uses it).
