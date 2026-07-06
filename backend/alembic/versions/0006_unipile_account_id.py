"""add unipile_account_id + relax password_encrypted to nullable

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-14

Unipile-backed LinkedIn accounts don't need a stored password (Unipile
owns the LinkedIn session via its hosted-auth flow).  We track Unipile's
opaque account id so we can call its API on the user's behalf.

Migration plan:
- add ``unipile_account_id`` (nullable, unique-when-set)
- relax ``password_encrypted`` to nullable so Unipile accounts can omit it
- add ``provider_kind`` to label rows as "unipile" vs the legacy "diy"
  paths (Playwright / linkedin-api).  Defaults to "diy" so existing rows
  don't change behaviour; new accounts created via the Unipile hosted
  flow are inserted with "unipile".
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "linkedin_accounts",
        sa.Column("unipile_account_id", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_linkedin_accounts_unipile_account_id",
        "linkedin_accounts",
        ["unipile_account_id"],
        unique=True,
    )
    op.add_column(
        "linkedin_accounts",
        sa.Column(
            "provider_kind",
            sa.Text(),
            nullable=False,
            server_default="diy",
        ),
    )
    # Relax password_encrypted: existing DIY rows already have values,
    # but new Unipile rows won't supply one.
    op.alter_column(
        "linkedin_accounts",
        "password_encrypted",
        existing_type=sa.Text(),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "linkedin_accounts",
        "password_encrypted",
        existing_type=sa.Text(),
        nullable=False,
    )
    op.drop_column("linkedin_accounts", "provider_kind")
    op.drop_index(
        "ix_linkedin_accounts_unipile_account_id",
        table_name="linkedin_accounts",
    )
    op.drop_column("linkedin_accounts", "unipile_account_id")
