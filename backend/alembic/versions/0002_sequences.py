"""multi-step sequences

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-12

Adds workflow tables for multi-step campaigns:
  sequences, sequence_nodes, sequence_edges,
  lead_sequence_states, lead_step_executions.
Plus two columns on leads for cross-channel state.

Backfill: every existing campaign gets a single-node default sequence with
one `email` entry node so the pre-sequence world keeps working. Every
existing lead gets a lead_sequence_states row pointing at that entry.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---- enum definitions (created up front, then referenced with create_type=False) ----

LINKEDIN_CONNECTION_STATUS = postgresql.ENUM(
    "unknown", "invited", "connected", "declined", "withdrawn",
    name="linkedin_connection_status",
)
SEQUENCE_NODE_KIND = postgresql.ENUM(
    "email", "wait",
    "linkedin_view_profile", "linkedin_follow_profile",
    "linkedin_react_post", "linkedin_comment_post",
    "linkedin_connect", "linkedin_dm", "linkedin_inmail",
    "linkedin_invite_to_page",
    name="sequence_node_kind",
)
LEAD_SEQUENCE_STATUS = postgresql.ENUM(
    "pending", "active", "halted", "completed",
    name="lead_sequence_status",
)
LEAD_STEP_RESULT = postgresql.ENUM(
    "sent", "skipped", "failed",
    name="lead_step_result",
)


def upgrade() -> None:
    bind = op.get_bind()
    LINKEDIN_CONNECTION_STATUS.create(bind, checkfirst=True)
    SEQUENCE_NODE_KIND.create(bind, checkfirst=True)
    LEAD_SEQUENCE_STATUS.create(bind, checkfirst=True)
    LEAD_STEP_RESULT.create(bind, checkfirst=True)

    # ----- leads: new columns ------------------------------------------------
    op.add_column(
        "leads",
        sa.Column(
            "linkedin_connection_status",
            postgresql.ENUM(name="linkedin_connection_status", create_type=False),
            nullable=False,
            server_default="unknown",
        ),
    )
    op.add_column(
        "leads",
        sa.Column("linkedin_last_reply_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ----- sequences ---------------------------------------------------------
    op.create_table(
        "sequences",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "campaign_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("campaigns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("campaign_id", name="uq_sequences_campaign_id"),
    )
    op.create_index("ix_sequences_campaign_id", "sequences", ["campaign_id"])

    op.create_table(
        "sequence_nodes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "sequence_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sequences.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "kind",
            postgresql.ENUM(name="sequence_node_kind", create_type=False),
            nullable=False,
        ),
        sa.Column("config", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("position_x", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("position_y", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_entry", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_sequence_nodes_sequence_id", "sequence_nodes", ["sequence_id"])

    op.create_table(
        "sequence_edges",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "sequence_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sequences.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "from_node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sequence_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "to_node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sequence_nodes.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("condition", postgresql.JSONB(), nullable=False, server_default='{"op": "always"}'),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_sequence_edges_sequence_id", "sequence_edges", ["sequence_id"])
    op.create_index("ix_sequence_edges_from_node_id", "sequence_edges", ["from_node_id"])

    op.create_table(
        "lead_sequence_states",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "lead_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "sequence_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sequences.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "current_node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sequence_nodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(name="lead_sequence_status", create_type=False),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("halt_reason", sa.Text(), nullable=True),
        sa.Column("entered_current_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("lead_id", name="uq_lead_sequence_states_lead_id"),
    )
    op.create_index("ix_lead_sequence_states_lead_id", "lead_sequence_states", ["lead_id"])
    op.create_index("ix_lead_sequence_states_sequence_id", "lead_sequence_states", ["sequence_id"])
    op.create_index("ix_lead_sequence_states_status", "lead_sequence_states", ["status"])
    op.create_index("ix_lead_sequence_states_next_run_at", "lead_sequence_states", ["next_run_at"])

    op.create_table(
        "lead_step_executions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "lead_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sequence_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "result",
            postgresql.ENUM(name="lead_step_result", create_type=False),
            nullable=False,
        ),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("external_meta", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_lead_step_executions_lead_id", "lead_step_executions", ["lead_id"])
    op.create_index("ix_lead_step_executions_node_id", "lead_step_executions", ["node_id"])

    # ----- backfill ----------------------------------------------------------
    # Every existing campaign gets a default published sequence: one email
    # entry node with no outgoing edge (terminates after sending). Every
    # existing lead gets a lead_sequence_states row pointing at that entry.
    op.execute(
        """
        WITH new_sequences AS (
            INSERT INTO sequences (id, campaign_id, is_published, created_at, updated_at)
            SELECT gen_random_uuid(), c.id, true, now(), now()
            FROM campaigns c
            WHERE NOT EXISTS (SELECT 1 FROM sequences s WHERE s.campaign_id = c.id)
            RETURNING id, campaign_id
        ),
        new_nodes AS (
            INSERT INTO sequence_nodes (
                id, sequence_id, kind, config, position_x, position_y,
                is_entry, created_at, updated_at
            )
            SELECT
                gen_random_uuid(), ns.id, 'email'::sequence_node_kind,
                '{"use_campaign_compose": true}'::jsonb,
                0, 0, true, now(), now()
            FROM new_sequences ns
            RETURNING id, sequence_id
        )
        INSERT INTO lead_sequence_states (
            id, lead_id, sequence_id, current_node_id, status,
            next_run_at, entered_current_at, created_at, updated_at
        )
        SELECT
            gen_random_uuid(), l.id, nn.sequence_id, nn.id, 'active'::lead_sequence_status,
            now(), now(), now(), now()
        FROM new_nodes nn
        JOIN sequences s ON s.id = nn.sequence_id
        JOIN leads l ON l.campaign_id = s.campaign_id
        WHERE NOT EXISTS (
            SELECT 1 FROM lead_sequence_states lss WHERE lss.lead_id = l.id
        );
        """
    )


def downgrade() -> None:
    op.drop_index("ix_lead_step_executions_node_id", table_name="lead_step_executions")
    op.drop_index("ix_lead_step_executions_lead_id", table_name="lead_step_executions")
    op.drop_table("lead_step_executions")

    op.drop_index("ix_lead_sequence_states_next_run_at", table_name="lead_sequence_states")
    op.drop_index("ix_lead_sequence_states_status", table_name="lead_sequence_states")
    op.drop_index("ix_lead_sequence_states_sequence_id", table_name="lead_sequence_states")
    op.drop_index("ix_lead_sequence_states_lead_id", table_name="lead_sequence_states")
    op.drop_table("lead_sequence_states")

    op.drop_index("ix_sequence_edges_from_node_id", table_name="sequence_edges")
    op.drop_index("ix_sequence_edges_sequence_id", table_name="sequence_edges")
    op.drop_table("sequence_edges")

    op.drop_index("ix_sequence_nodes_sequence_id", table_name="sequence_nodes")
    op.drop_table("sequence_nodes")

    op.drop_index("ix_sequences_campaign_id", table_name="sequences")
    op.drop_table("sequences")

    op.drop_column("leads", "linkedin_last_reply_at")
    op.drop_column("leads", "linkedin_connection_status")

    bind = op.get_bind()
    for enum_obj in (LEAD_STEP_RESULT, LEAD_SEQUENCE_STATUS, SEQUENCE_NODE_KIND, LINKEDIN_CONNECTION_STATUS):
        enum_obj.drop(bind, checkfirst=True)
