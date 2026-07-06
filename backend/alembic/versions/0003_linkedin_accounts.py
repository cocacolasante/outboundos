"""LinkedIn accounts

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-12

Adds:
  - linkedin_accounts table for DIY LinkedIn provider credentials
  - campaigns.linkedin_account_id FK (nullable, ON DELETE SET NULL)
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


LINKEDIN_ACCOUNT_STATUS = postgresql.ENUM(
    "untested", "ok", "failed", "challenged", "restricted",
    name="linkedin_account_status",
)


def upgrade() -> None:
    bind = op.get_bind()
    LINKEDIN_ACCOUNT_STATUS.create(bind, checkfirst=True)

    op.create_table(
        "linkedin_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("linkedin_email", sa.Text(), nullable=False),
        sa.Column("password_encrypted", sa.Text(), nullable=False),
        sa.Column("session_cookies_encrypted", sa.Text(), nullable=True),
        sa.Column("proxy_url", sa.Text(), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(name="linkedin_account_status", create_type=False),
            nullable=False,
            server_default="untested",
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pending_challenge_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_linkedin_accounts_linkedin_email", "linkedin_accounts", ["linkedin_email"])

    op.add_column(
        "campaigns",
        sa.Column(
            "linkedin_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("linkedin_accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("campaigns", "linkedin_account_id")
    op.drop_index("ix_linkedin_accounts_linkedin_email", table_name="linkedin_accounts")
    op.drop_table("linkedin_accounts")
    LINKEDIN_ACCOUNT_STATUS.drop(op.get_bind(), checkfirst=True)
