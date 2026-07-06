"""Intent engine v2 (Phase 5): per-profile RFP keywords + half-life overrides.

Additive + reversible.  Two JSONB columns on ``icp_intent_profiles``:

- ``rfp_keywords``       — Grants.gov search keywords for the new_rfp collector
                           (was a ``signal_weights._rfp_keywords`` convention).
- ``half_life_overrides`` — per-signal-type half-life overrides (closes the
                           Phase-3 "half-life from the profile, not hardcoded"
                           flag; falls back to the per-type defaults).
"""
from typing import Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0042"
down_revision: Union[str, None] = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "icp_intent_profiles",
        sa.Column("rfp_keywords", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default="[]"),
    )
    op.add_column(
        "icp_intent_profiles",
        sa.Column("half_life_overrides", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("icp_intent_profiles", "half_life_overrides")
    op.drop_column("icp_intent_profiles", "rfp_keywords")
