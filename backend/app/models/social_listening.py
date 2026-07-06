"""Social Listening Radar ORM models.

Three tables that together power an "intent feed" — discover LinkedIn
posts matching a topic, score them for buying intent, and queue
suggested manual responses (NEVER auto-fired).

  SocialListeningSearch         — one per user-configured topic search
  SocialListeningPost           — discovered posts (dedup'd by URL)
  SocialListeningOpportunity    — one per qualified post; AI score + suggested copy
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, Numeric, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.tenancy.mixin import TenantMixin


# ---- enums -----------------------------------------------------------------

class SocialSearchSource(str, enum.Enum):
    LINKEDIN = "linkedin"
    REDDIT = "reddit"
    TWITTER = "twitter"


class SocialSearchFrequency(str, enum.Enum):
    MANUAL = "manual"
    EVERY_6H = "every_6h"
    EVERY_12H = "every_12h"
    DAILY = "daily"
    WEEKLY = "weekly"


class SocialSearchStatus(str, enum.Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class SocialPostProvider(str, enum.Enum):
    LINKEDIN = "linkedin"
    REDDIT = "reddit"
    TWITTER = "twitter"


class SocialOpportunityCategory(str, enum.Enum):
    UCAAS_PHONE = "ucaas_phone"
    INTERNET = "internet"
    CYBERSECURITY = "cybersecurity"
    CLOUD = "cloud"
    MSP = "msp"
    CRM_SOFTWARE = "crm_software"
    NONPROFIT_TECH = "nonprofit_tech"
    GENERAL_ADVISORY = "general_advisory"
    NOT_RELEVANT = "not_relevant"


class SocialOpportunityAction(str, enum.Enum):
    IGNORE = "ignore"
    COMMENT = "comment"
    CONNECT = "connect"
    RESEARCH_FURTHER = "research_further"


class SocialOpportunityStatus(str, enum.Enum):
    NEW = "new"
    SAVED = "saved"
    COMMENTED = "commented"
    CONNECTED = "connected"
    REPLIED = "replied"
    NOT_RELEVANT = "not_relevant"
    ARCHIVED = "archived"


# ---- models ----------------------------------------------------------------

class SocialListeningSearch(TenantMixin, Base):
    __tablename__ = "social_listening_searches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    name: Mapped[str] = mapped_column(Text, nullable=False)
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    # AI-generated phrase list. Cached on the search row so re-runs reuse
    # them; rewritten when the user edits the topic or its modifiers.
    expanded_queries: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]",
    )

    niche: Mapped[str | None] = mapped_column(Text, nullable=True)
    geography: Mapped[str | None] = mapped_column(Text, nullable=True)
    include_keywords: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]",
    )
    exclude_keywords: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]",
    )

    source: Mapped[SocialSearchSource] = mapped_column(
        Enum(SocialSearchSource, name="social_search_source",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False, default=SocialSearchSource.LINKEDIN,
        server_default=SocialSearchSource.LINKEDIN.value,
    )
    # Multi-source fan-out.  Anthropic web_search has poor recall on
    # LinkedIn posts (the platform blocks indexing); Reddit and Twitter/X
    # are much more reliably indexed.  This list controls which platforms
    # the worker iterates over per query.
    sources: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=lambda: ["linkedin"],
        server_default='["linkedin"]',
    )
    # Explicit list of LinkedIn profile URLs to monitor.  Each run fetches
    # each profile's most-recent posts via Unipile (which talks directly
    # to LinkedIn — no indexing dependency) and feeds them through
    # qualification.  This is the reliable LinkedIn channel; the
    # ``sources=[linkedin]`` web-search path is best-effort.
    linkedin_profile_watchlist: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]",
    )
    # Cost controls.  LinkedIn web-search on Sonnet is the most expensive
    # part of a run and rarely produces results (LinkedIn blocks
    # indexing); off by default.  Watchlist still fires when ``linkedin``
    # is in ``sources``.
    linkedin_web_search_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    # Reddit → LinkedIn cross-link extraction.  When the Reddit channel
    # surfaces posts that contain ``linkedin.com/posts/...`` URLs in
    # their body, the worker auto-creates LinkedIn opportunity rows
    # pointing at those URLs.  Zero Anthropic cost — synthesised
    # locally — so it defaults to ON.
    linkedin_crosslink_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true",
    )
    # Soft per-run spend cap.  Worker tracks approx cost from Anthropic
    # token usage and aborts before exceeding this.
    max_run_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, default=Decimal("1.0"),
        server_default="1.0",
    )
    frequency: Mapped[SocialSearchFrequency] = mapped_column(
        Enum(SocialSearchFrequency, name="social_search_frequency",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False, default=SocialSearchFrequency.MANUAL,
        server_default=SocialSearchFrequency.MANUAL.value,
    )
    status: Mapped[SocialSearchStatus] = mapped_column(
        Enum(SocialSearchStatus, name="social_search_status",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False, default=SocialSearchStatus.ACTIVE,
        server_default=SocialSearchStatus.ACTIVE.value,
    )

    # Voice for the AI-suggested comment / connection / follow-up copy.
    tone: Mapped[str] = mapped_column(Text, nullable=False, default="helpful", server_default="helpful")
    sender_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Soft caps (UI-editable per search) — keep a runaway topic from racking
    # up Anthropic spend.  Total per-run AI cost is bounded by:
    #   1 * topic-expand (Haiku)
    #   max_queries_per_run * discovery (Sonnet + web search)
    #   max_qualified_per_run * qualify (Haiku)
    max_queries_per_run: Mapped[int] = mapped_column(
        Integer, nullable=False, default=20, server_default="20",
    )
    max_posts_per_query: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default="30",
    )
    max_qualified_per_run: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, server_default="100",
    )
    # Lookback window — posts older than this are not considered.  The
    # discovery service injects this into the prompt as "only return
    # posts on or after YYYY-MM-DD" AND defensively post-filters any
    # returned post whose ``post_date`` is past the cutoff.  Posts with
    # no parsable date are kept (the LLM occasionally omits the date
    # field and we'd rather keep a borderline match than drop a real
    # signal).
    max_post_age_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default="30",
    )

    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_run_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Per-query diagnostics from the last run: which queries surfaced
    # posts and which were duds.  Shape documented in migration 0016.
    last_run_stats: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    posts: Mapped[list["SocialListeningPost"]] = relationship(
        back_populates="search",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class SocialListeningPost(TenantMixin, Base):
    __tablename__ = "social_listening_posts"
    __table_args__ = (
        # Per-tenant (Phase 2): two tenants discovering the same public post
        # each get their own row.  Constraint NAME is load-bearing — the
        # worker's upsert targets it via ON CONFLICT ON CONSTRAINT.
        # NULLS NOT DISTINCT keeps NULL-tenant worker rows deduping as before.
        UniqueConstraint("tenant_id", "provider", "post_url",
                         name="uq_social_posts_provider_url",
                         postgresql_nulls_not_distinct=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    search_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("social_listening_searches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    provider: Mapped[SocialPostProvider] = mapped_column(
        Enum(SocialPostProvider, name="social_post_provider",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False, default=SocialPostProvider.LINKEDIN,
        server_default=SocialPostProvider.LINKEDIN.value,
    )
    # LinkedIn URN if extractable from the discovered URL; can be NULL
    # because Anthropic web search results don't always include it.
    provider_post_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The canonical dedup key — `UNIQUE(provider, post_url)`.
    post_url: Mapped[str] = mapped_column(Text, nullable=False, index=True)

    author_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    author_profile_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    author_headline: Mapped[str | None] = mapped_column(Text, nullable=True)
    company_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    post_text: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    post_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    discovered_via: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}",
    )

    search: Mapped["SocialListeningSearch"] = relationship(back_populates="posts")
    opportunity: Mapped["SocialListeningOpportunity | None"] = relationship(
        back_populates="post",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class SocialListeningOpportunity(TenantMixin, Base):
    """One row per qualified post.  Re-qualification overwrites in place
    (the FK + UNIQUE on ``post_id`` is the upsert key)."""

    __tablename__ = "social_listening_opportunities"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    post_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("social_listening_posts.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0", index=True)
    category: Mapped[SocialOpportunityCategory] = mapped_column(
        Enum(SocialOpportunityCategory, name="social_opportunity_category",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False, default=SocialOpportunityCategory.GENERAL_ADVISORY,
        server_default=SocialOpportunityCategory.GENERAL_ADVISORY.value,
        index=True,
    )
    buying_signal: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", index=True,
    )

    pain_summary: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    qualification_reason: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    suggested_comment: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    suggested_connection_request: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    suggested_follow_up: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")

    recommended_action: Mapped[SocialOpportunityAction] = mapped_column(
        Enum(SocialOpportunityAction, name="social_opportunity_action",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False, default=SocialOpportunityAction.IGNORE,
        server_default=SocialOpportunityAction.IGNORE.value,
    )
    status: Mapped[SocialOpportunityStatus] = mapped_column(
        Enum(SocialOpportunityStatus, name="social_opportunity_status",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False, default=SocialOpportunityStatus.NEW,
        server_default=SocialOpportunityStatus.NEW.value,
        index=True,
    )

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    post: Mapped["SocialListeningPost"] = relationship(back_populates="opportunity")
