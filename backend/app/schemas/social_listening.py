"""Pydantic schemas for the Social Listening Radar feature.

The internal ``DiscoveredPost`` / ``QualificationResult`` dataclasses
that the services return live alongside those services — they're not
exposed via the HTTP layer.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any  # noqa: F401 — used in Field type annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models import (
    SocialOpportunityAction,
    SocialOpportunityCategory,
    SocialOpportunityStatus,
    SocialPostProvider,
    SocialSearchFrequency,
    SocialSearchSource,
    SocialSearchStatus,
)


def _blank_to_none(v: str | None) -> str | None:
    """Treat an all-whitespace string as None.  Mirrors the same helper in
    `schemas/campaign.py`."""
    if v is None:
        return None
    return v if v.strip() else None


# ============================================================================
# Searches
# ============================================================================

class SocialListeningSearchCreate(BaseModel):
    name: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    niche: str | None = None
    geography: str | None = None
    include_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    source: SocialSearchSource = SocialSearchSource.LINKEDIN
    # New multi-source fan-out.  Default to all 3 because Reddit + Twitter
    # actually get indexed by web search; LinkedIn alone often returns 0
    # results.  Old API clients can still pass just ``source`` — if they
    # do, the create endpoint mirrors it into ``sources=[source]``.
    sources: list[SocialSearchSource] = Field(
        # Default: LinkedIn (watchlist channel is the reliable one) +
        # Reddit (direct RSS, no Anthropic — high recall, zero cost).
        # Twitter dropped from defaults: Anthropic web_search can't reach
        # it and the X API v2 is $100/mo paid.  Users who specifically
        # want it can still opt in via the source picker.
        default_factory=lambda: [
            SocialSearchSource.LINKEDIN,
            SocialSearchSource.REDDIT,
        ]
    )
    # Optional LinkedIn profile URLs to monitor every run.  Worker
    # fetches each profile's recent posts via Unipile.  No indexing
    # required — this is the reliable LinkedIn channel.
    linkedin_profile_watchlist: list[str] = Field(default_factory=list)
    # LinkedIn web-search is expensive (Sonnet) and rarely produces
    # results — opt-in.  Watchlist still runs whenever LinkedIn is in
    # ``sources``.
    linkedin_web_search_enabled: bool = False
    # Reddit → LinkedIn cross-link extraction: mine LinkedIn URLs out of
    # Reddit discussion bodies and surface them as research-further
    # opportunities.  Zero Anthropic cost, on by default.
    linkedin_crosslink_enabled: bool = True
    # Soft per-run spend cap in USD.
    max_run_cost_usd: float = Field(default=1.0, ge=0.05, le=100.0)
    frequency: SocialSearchFrequency = SocialSearchFrequency.MANUAL
    status: SocialSearchStatus = SocialSearchStatus.ACTIVE
    tone: str = "helpful"
    sender_name: str | None = None
    max_queries_per_run: int = Field(default=20, ge=1, le=100)
    max_posts_per_query: int = Field(default=30, ge=1, le=100)
    max_qualified_per_run: int = Field(default=100, ge=1, le=500)
    max_post_age_days: int = Field(default=30, ge=1, le=3650)

    _v_name = field_validator("name", "niche", "geography", "sender_name")(_blank_to_none)
    _v_topic = field_validator("topic")(_blank_to_none)

    @model_validator(mode="after")
    def _name_topic_required(self) -> "SocialListeningSearchCreate":
        # _blank_to_none can turn whitespace-only into None; restore the
        # error if the user effectively passed nothing.
        if not (self.name or "").strip():
            raise ValueError("name is required")
        if not (self.topic or "").strip():
            raise ValueError("topic is required")
        return self


class SocialListeningSearchUpdate(BaseModel):
    name: str | None = None
    topic: str | None = None
    niche: str | None = None
    geography: str | None = None
    include_keywords: list[str] | None = None
    exclude_keywords: list[str] | None = None
    frequency: SocialSearchFrequency | None = None
    status: SocialSearchStatus | None = None
    sources: list[SocialSearchSource] | None = None
    linkedin_profile_watchlist: list[str] | None = None
    linkedin_web_search_enabled: bool | None = None
    linkedin_crosslink_enabled: bool | None = None
    max_run_cost_usd: float | None = Field(default=None, ge=0.05, le=100.0)
    tone: str | None = None
    sender_name: str | None = None
    max_queries_per_run: int | None = Field(default=None, ge=1, le=100)
    max_posts_per_query: int | None = Field(default=None, ge=1, le=100)
    max_qualified_per_run: int | None = Field(default=None, ge=1, le=500)
    max_post_age_days: int | None = Field(default=None, ge=1, le=3650)
    # The user can hand-edit the AI-generated phrase list (add, remove,
    # tweak) — this lets them iterate when expansion is producing junk.
    # Editing this does NOT trigger re-expansion; only a topic change does.
    expanded_queries: list[str] | None = None

    _v_blank = field_validator(
        "name", "topic", "niche", "geography", "tone", "sender_name",
    )(_blank_to_none)


class SocialListeningSearchSummary(BaseModel):
    """Lightweight list view — excludes the (potentially large)
    ``expanded_queries`` array.  Includes denormalized counts for the
    Searches tab."""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    topic: str
    niche: str | None
    geography: str | None
    source: SocialSearchSource
    sources: list[SocialSearchSource] = Field(default_factory=list)
    frequency: SocialSearchFrequency
    status: SocialSearchStatus
    last_run_at: datetime | None
    next_run_at: datetime | None
    last_run_status: str | None
    last_run_error: str | None
    expanded_query_count: int = 0
    post_count: int = 0
    opportunity_count: int = 0
    created_at: datetime
    updated_at: datetime


class SocialListeningSearchResponse(SocialListeningSearchSummary):
    """Detail view — includes the full expanded_queries list and the soft
    caps that the Searches tab editor needs."""
    expanded_queries: list[str] = Field(default_factory=list)
    include_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    tone: str
    sender_name: str | None
    max_queries_per_run: int
    max_posts_per_query: int
    max_qualified_per_run: int
    max_post_age_days: int
    last_run_stats: dict[str, Any] = Field(default_factory=dict)
    linkedin_profile_watchlist: list[str] = Field(default_factory=list)
    linkedin_web_search_enabled: bool = False
    linkedin_crosslink_enabled: bool = True
    max_run_cost_usd: float = 1.0


class PaginatedSearches(BaseModel):
    items: list[SocialListeningSearchSummary]
    total: int
    page: int
    page_size: int
    total_pages: int


# ============================================================================
# Expand preview (synchronous AI call so the UI can show queries before save)
# ============================================================================

class ExpandPreviewRequest(BaseModel):
    topic: str = Field(min_length=1)
    niche: str | None = None
    geography: str | None = None
    include_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    max_queries: int = Field(default=20, ge=1, le=100)


class ExpandPreviewResponse(BaseModel):
    queries: list[str]


# ============================================================================
# Posts (summary embedded in the opportunity feed)
# ============================================================================

class SocialListeningPostSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider: SocialPostProvider
    post_url: str
    author_name: str | None
    author_profile_url: str | None
    author_headline: str | None
    company_name: str | None
    post_text: str
    post_date: datetime | None
    discovered_at: datetime
    discovered_via: str | None


# ============================================================================
# Opportunities
# ============================================================================

class SocialListeningOpportunitySummary(BaseModel):
    """Feed-row shape.  Embeds the underlying post so the frontend
    doesn't need a second roundtrip per row."""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    post_id: uuid.UUID
    search_id: uuid.UUID
    search_name: str | None = None
    score: int
    category: SocialOpportunityCategory
    buying_signal: bool
    pain_summary: str
    qualification_reason: str
    suggested_comment: str
    suggested_connection_request: str
    suggested_follow_up: str
    recommended_action: SocialOpportunityAction
    status: SocialOpportunityStatus
    notes: str | None
    created_at: datetime
    updated_at: datetime
    post: SocialListeningPostSummary


class SocialListeningOpportunityUpdate(BaseModel):
    status: SocialOpportunityStatus | None = None
    notes: str | None = None


class PaginatedOpportunities(BaseModel):
    items: list[SocialListeningOpportunitySummary]
    total: int
    page: int
    page_size: int
    total_pages: int


# ============================================================================
# Run-now response
# ============================================================================

class RunSearchResponse(BaseModel):
    task_id: str | None = None
    status: str  # "queued" or "already_running"


class CleanupStaleResponse(BaseModel):
    deleted: int


class RequalifyAllResponse(BaseModel):
    """Result of ``POST /searches/{id}/requalify-all`` — count of posts
    queued for batch re-qualification."""
    enqueued: int
    batches: int


class RunCostEstimate(BaseModel):
    """Pre-run cost forecast.  Surfaced on the "Run now" button so the
    user sees what they're about to spend."""
    discovery_pairs: int
    discovery_cost_usd: float
    qualify_estimated_posts: int
    qualify_cost_usd: float
    total_cost_usd: float
    max_run_cost_usd: float
    notes: list[str] = Field(default_factory=list)
