"""Admin invite links for bypassing Stripe billing on signup."""
from typing import Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0052"
down_revision: Union[str, None] = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invite_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("code", sa.Text(), nullable=False, unique=True),
        sa.Column("trial_days", sa.Integer(), nullable=False, server_default="14"),
        sa.Column("bypass_billing", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("max_uses", sa.Integer(), nullable=True),
        sa.Column("use_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by_email", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("invite_links")
