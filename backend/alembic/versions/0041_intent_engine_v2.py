"""Signals & Intent Engine v2: orgs, signals, org_intent_scores, icp_intent_profiles.

Revision ID: 0041
Revises: 0040
Create Date: 2026-06-22

Phase 1 of the intent-engine rebuild (Signals & Intent Engine v2).  ALL
ADDITIVE — four new tables that COEXIST with the legacy ``prospect_signals``
and ``icp_profiles`` (nothing is renamed or dropped; the v1→v2 cutover happens
over later phases):

- ``orgs``                — the persistent intent anchor (EIN / NTEE / domain /
                            990 size band); deduped by EIN within a tenant.
- ``signals``             — evidenced intent EVENTS; idempotent on a unique
                            ``dedupe_key``.
- ``org_intent_scores``   — rolled-up intent + tier, one row per org.
- ``icp_intent_profiles`` — per-tenant collection/scoring config.

Each table carries a nullable ``tenant_id`` (tenancy-ready, not enforced).
Downgrade drops only the new objects/types.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0041"
down_revision: Union[str, None] = "0040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _ts(col: str) -> sa.Column:
    return sa.Column(
        col, sa.DateTime(timezone=True),
        server_default=sa.text("now()"), nullable=False,
    )


_SIZE_BAND = postgresql.ENUM(
    "micro", "small", "mid", "large", "major",
    name="org_size_band", create_type=False,
)
_SIGNAL_TYPE = postgresql.ENUM(
    "new_rfp", "dev_role_posted", "lapsed_funder", "rev_drop",
    "new_program", "peer_funded", "new_501c3", "cause_match",
    name="intent_signal_type", create_type=False,
)
_SIGNAL_SOURCE = postgresql.ENUM(
    "propublica", "grants_gov", "usaspending", "irs_bmf", "jobs", "manual",
    name="intent_signal_source", create_type=False,
)
_SIGNAL_STATUS = postgresql.ENUM(
    "new", "scored", "promoted", "suppressed", "expired",
    name="intent_signal_status", create_type=False,
)


def upgrade() -> None:
    op.execute(
        "CREATE TYPE org_size_band AS ENUM "
        "('micro', 'small', 'mid', 'large', 'major')"
    )
    op.execute(
        "CREATE TYPE intent_signal_type AS ENUM "
        "('new_rfp', 'dev_role_posted', 'lapsed_funder', 'rev_drop', "
        "'new_program', 'peer_funded', 'new_501c3', 'cause_match')"
    )
    op.execute(
        "CREATE TYPE intent_signal_source AS ENUM "
        "('propublica', 'grants_gov', 'usaspending', 'irs_bmf', 'jobs', 'manual')"
    )
    op.execute(
        "CREATE TYPE intent_signal_status AS ENUM "
        "('new', 'scored', 'promoted', 'suppressed', 'expired')"
    )

    # ---------------------------------------------------------------- orgs
    op.create_table(
        "orgs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("ein", sa.Text(), nullable=True),
        sa.Column("domain", sa.Text(), nullable=True),
        sa.Column("website", sa.Text(), nullable=True),
        sa.Column("ntee_code", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=True),
        sa.Column("city", sa.Text(), nullable=True),
        sa.Column("annual_revenue", sa.Numeric(16, 2), nullable=True),
        sa.Column("size_band", _SIZE_BAND, nullable=True),
        sa.Column(
            "source_lead_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leads.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("raw", postgresql.JSONB(), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("ix_orgs_tenant_id", "orgs", ["tenant_id"])
    op.create_index("ix_orgs_ein", "orgs", ["ein"])
    op.create_index("ix_orgs_domain", "orgs", ["domain"])
    op.create_index("ix_orgs_ntee_code", "orgs", ["ntee_code"])
    op.create_index("ix_orgs_state", "orgs", ["state"])
    op.create_index("ix_orgs_size_band", "orgs", ["size_band"])
    # Dedup orgs by EIN within a tenant.  Partial (EIN may be NULL pre-
    # enrichment) + NULLS NOT DISTINCT (PG15+) so a NULL tenant_id still
    # dedups by EIN in the current single-tenant reality.
    op.create_index(
        "uq_orgs_tenant_ein", "orgs", ["tenant_id", "ein"],
        unique=True, postgresql_where=sa.text("ein IS NOT NULL"),
        postgresql_nulls_not_distinct=True,
    )

    # ------------------------------------------------------------- signals
    op.create_table(
        "signals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("signal_type", _SIGNAL_TYPE, nullable=False),
        sa.Column("source", _SIGNAL_SOURCE, nullable=False),
        sa.Column("score", sa.Numeric(10, 4), nullable=False),
        sa.Column("event_date", sa.DateTime(timezone=True), nullable=False),
        _ts("detected_at"),
        sa.Column("evidence_url", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("raw_payload", postgresql.JSONB(), nullable=True),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column(
            "status", _SIGNAL_STATUS, nullable=False,
            server_default="new",
        ),
        _ts("created_at"),
        sa.UniqueConstraint("dedupe_key", name="uq_signals_dedupe_key"),
    )
    op.create_index("ix_signals_org_id", "signals", ["org_id"])
    op.create_index("ix_signals_tenant_id", "signals", ["tenant_id"])
    op.create_index("ix_signals_status", "signals", ["status"])
    op.create_index("ix_signals_dedupe_key", "signals", ["dedupe_key"])

    # -------------------------------------------------- org_intent_scores
    op.create_table(
        "org_intent_scores",
        sa.Column(
            "org_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"), primary_key=True,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("intent_score", sa.Numeric(12, 4), nullable=False, server_default="0"),
        sa.Column("tier", sa.Integer(), nullable=False),
        sa.Column(
            "top_signal_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("signals.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("fit_multiplier", sa.Numeric(6, 3), nullable=False, server_default="1"),
        _ts("last_computed_at"),
    )
    op.create_index("ix_org_intent_scores_tenant_id", "org_intent_scores", ["tenant_id"])
    op.create_index("ix_org_intent_scores_tier", "org_intent_scores", ["tier"])

    # ------------------------------------------------ icp_intent_profiles
    op.create_table(
        "icp_intent_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("cause_codes", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("geographies", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("size_band_weights", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("signal_weights", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("half_life_days", sa.Numeric(8, 2), nullable=False, server_default="30"),
        sa.Column("max_signal_age_days", sa.Integer(), nullable=False, server_default="180"),
        sa.Column("promotion_threshold", sa.Numeric(12, 4), nullable=False, server_default="100"),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("ix_icp_intent_profiles_tenant_id", "icp_intent_profiles", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("icp_intent_profiles")
    op.drop_table("org_intent_scores")
    op.drop_table("signals")
    op.drop_table("orgs")
    op.execute("DROP TYPE IF EXISTS intent_signal_status")
    op.execute("DROP TYPE IF EXISTS intent_signal_source")
    op.execute("DROP TYPE IF EXISTS intent_signal_type")
    op.execute("DROP TYPE IF EXISTS org_size_band")
