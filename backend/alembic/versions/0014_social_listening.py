"""Social Listening Radar tables.

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-31

Adds the three tables that power the Social Listening Radar feature:
  social_listening_searches   — one per user-configured topic search
  social_listening_posts      — discovered LinkedIn posts (dedup'd by URL)
  social_listening_opportunities — one per qualified post; AI score + suggested copy

Discovery uses Anthropic's web_search_20250305 tool (Unipile's API has no
LinkedIn post search).  All LinkedIn write actions stay manual — the
opportunity row just holds *suggested* comment/connect/follow-up text.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---- enum definitions (created up front, then referenced with create_type=False) ----

SOCIAL_SEARCH_SOURCE = postgresql.ENUM(
    "linkedin",
    name="social_search_source",
)
SOCIAL_SEARCH_FREQUENCY = postgresql.ENUM(
    "manual", "every_6h", "every_12h", "daily", "weekly",
    name="social_search_frequency",
)
SOCIAL_SEARCH_STATUS = postgresql.ENUM(
    "active", "paused", "archived",
    name="social_search_status",
)
SOCIAL_POST_PROVIDER = postgresql.ENUM(
    "linkedin",
    name="social_post_provider",
)
SOCIAL_OPPORTUNITY_CATEGORY = postgresql.ENUM(
    "ucaas_phone", "internet", "cybersecurity", "cloud",
    "msp", "crm_software", "nonprofit_tech",
    "general_advisory", "not_relevant",
    name="social_opportunity_category",
)
SOCIAL_OPPORTUNITY_ACTION = postgresql.ENUM(
    "ignore", "comment", "connect", "research_further",
    name="social_opportunity_action",
)
SOCIAL_OPPORTUNITY_STATUS = postgresql.ENUM(
    "new", "saved", "commented", "connected",
    "replied", "not_relevant", "archived",
    name="social_opportunity_status",
)


def upgrade() -> None:
    bind = op.get_bind()
    SOCIAL_SEARCH_SOURCE.create(bind, checkfirst=True)
    SOCIAL_SEARCH_FREQUENCY.create(bind, checkfirst=True)
    SOCIAL_SEARCH_STATUS.create(bind, checkfirst=True)
    SOCIAL_POST_PROVIDER.create(bind, checkfirst=True)
    SOCIAL_OPPORTUNITY_CATEGORY.create(bind, checkfirst=True)
    SOCIAL_OPPORTUNITY_ACTION.create(bind, checkfirst=True)
    SOCIAL_OPPORTUNITY_STATUS.create(bind, checkfirst=True)

    # ----- social_listening_searches -----------------------------------------
    op.create_table(
        "social_listening_searches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column(
            "expanded_queries", postgresql.JSONB(),
            nullable=False, server_default="[]",
        ),
        sa.Column("niche", sa.Text(), nullable=True),
        sa.Column("geography", sa.Text(), nullable=True),
        sa.Column(
            "include_keywords", postgresql.JSONB(),
            nullable=False, server_default="[]",
        ),
        sa.Column(
            "exclude_keywords", postgresql.JSONB(),
            nullable=False, server_default="[]",
        ),
        sa.Column(
            "source",
            postgresql.ENUM(name="social_search_source", create_type=False),
            nullable=False, server_default="linkedin",
        ),
        sa.Column(
            "frequency",
            postgresql.ENUM(name="social_search_frequency", create_type=False),
            nullable=False, server_default="manual",
        ),
        sa.Column(
            "status",
            postgresql.ENUM(name="social_search_status", create_type=False),
            nullable=False, server_default="active",
        ),
        sa.Column("tone", sa.Text(), nullable=False, server_default="helpful"),
        sa.Column("sender_name", sa.Text(), nullable=True),
        sa.Column("max_queries_per_run", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("max_posts_per_query", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("max_qualified_per_run", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_status", sa.Text(), nullable=True),
        sa.Column("last_run_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
    )
    op.create_index(
        "ix_social_searches_status_next_run",
        "social_listening_searches",
        ["status", "next_run_at"],
    )

    # ----- social_listening_posts --------------------------------------------
    op.create_table(
        "social_listening_posts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "search_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("social_listening_searches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "provider",
            postgresql.ENUM(name="social_post_provider", create_type=False),
            nullable=False, server_default="linkedin",
        ),
        sa.Column("provider_post_id", sa.Text(), nullable=True),
        sa.Column("post_url", sa.Text(), nullable=False),
        sa.Column("author_name", sa.Text(), nullable=True),
        sa.Column("author_profile_url", sa.Text(), nullable=True),
        sa.Column("author_headline", sa.Text(), nullable=True),
        sa.Column("company_name", sa.Text(), nullable=True),
        sa.Column("post_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("post_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "discovered_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column("discovered_via", sa.Text(), nullable=True),
        sa.Column("raw", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.UniqueConstraint("provider", "post_url", name="uq_social_posts_provider_url"),
    )
    op.create_index("ix_social_posts_search_id", "social_listening_posts", ["search_id"])
    op.create_index("ix_social_posts_post_url", "social_listening_posts", ["post_url"])

    # ----- social_listening_opportunities ------------------------------------
    op.create_table(
        "social_listening_opportunities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "post_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("social_listening_posts.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "category",
            postgresql.ENUM(name="social_opportunity_category", create_type=False),
            nullable=False, server_default="general_advisory",
        ),
        sa.Column("buying_signal", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("pain_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("qualification_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("suggested_comment", sa.Text(), nullable=False, server_default=""),
        sa.Column("suggested_connection_request", sa.Text(), nullable=False, server_default=""),
        sa.Column("suggested_follow_up", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "recommended_action",
            postgresql.ENUM(name="social_opportunity_action", create_type=False),
            nullable=False, server_default="ignore",
        ),
        sa.Column(
            "status",
            postgresql.ENUM(name="social_opportunity_status", create_type=False),
            nullable=False, server_default="new",
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
    )
    op.create_index("ix_social_opps_score", "social_listening_opportunities", ["score"])
    op.create_index("ix_social_opps_category", "social_listening_opportunities", ["category"])
    op.create_index("ix_social_opps_status", "social_listening_opportunities", ["status"])
    op.create_index(
        "ix_social_opps_buying_signal",
        "social_listening_opportunities",
        ["buying_signal"],
    )


def downgrade() -> None:
    op.drop_index("ix_social_opps_buying_signal", table_name="social_listening_opportunities")
    op.drop_index("ix_social_opps_status", table_name="social_listening_opportunities")
    op.drop_index("ix_social_opps_category", table_name="social_listening_opportunities")
    op.drop_index("ix_social_opps_score", table_name="social_listening_opportunities")
    op.drop_table("social_listening_opportunities")

    op.drop_index("ix_social_posts_post_url", table_name="social_listening_posts")
    op.drop_index("ix_social_posts_search_id", table_name="social_listening_posts")
    op.drop_table("social_listening_posts")

    op.drop_index(
        "ix_social_searches_status_next_run", table_name="social_listening_searches",
    )
    op.drop_table("social_listening_searches")

    bind = op.get_bind()
    for enum_obj in (
        SOCIAL_OPPORTUNITY_STATUS,
        SOCIAL_OPPORTUNITY_ACTION,
        SOCIAL_OPPORTUNITY_CATEGORY,
        SOCIAL_POST_PROVIDER,
        SOCIAL_SEARCH_STATUS,
        SOCIAL_SEARCH_FREQUENCY,
        SOCIAL_SEARCH_SOURCE,
    ):
        enum_obj.drop(bind, checkfirst=True)
