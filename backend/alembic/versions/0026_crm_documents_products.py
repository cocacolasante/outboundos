"""CRM documents + products-of-interest line items.

Revision ID: 0026
Revises: 0025
Create Date: 2026-06-11

Two additions to make the opportunity page a full deal record:

1. ``crm_documents`` — file attachments per opportunity (proposals,
   contracts, quotes).  Bytes live in Postgres (BYTEA): right-sized for
   a single-operator tool — no S3 bucket / volume mounts to manage,
   backups capture everything, and the 10MB per-file cap (enforced at
   the route layer) keeps the table sane.
2. ``crm_opportunity_products`` — products-of-interest line items
   (Salesforce OpportunityLineItem, lite): free-text product name +
   quantity x unit price.  No global product catalog in v1 — free text
   keeps it lite; a catalog can be layered on later without a schema
   break.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "crm_documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "opportunity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("crm_opportunities.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("data", postgresql.BYTEA(), nullable=False),
        sa.Column(
            "uploaded_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )

    op.create_table(
        "crm_opportunity_products",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "opportunity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("crm_opportunities.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("product_name", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(12, 2), nullable=False, server_default="1"),
        sa.Column("unit_price", sa.Numeric(14, 2), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("crm_opportunity_products")
    op.drop_table("crm_documents")
