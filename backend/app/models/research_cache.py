"""Cross-campaign research cache, keyed by lowercased email.

When a lead with a known email is added to a NEW campaign, the research
worker reuses the cached research_data (if it's still within the
freshness window) instead of re-spending Anthropic web-search tokens.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.tenancy.mixin import TenantMixin


class ResearchCache(TenantMixin, Base):
    __tablename__ = "research_cache"
    __table_args__ = (
        # Per-tenant cache (Phase 2): a research blob was paid for by one
        # tenant's Anthropic key — never shared across tenants.  NULLS NOT
        # DISTINCT preserves the old UNIQUE(email) dedup for worker rows
        # whose tenant_id is still NULL (workers get context next phase).
        UniqueConstraint("tenant_id", "email", name="uq_research_cache_tenant_email",
                         postgresql_nulls_not_distinct=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    # Lowercased + stripped email — canonical key within a tenant.
    email: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    research_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # Last time the research was refreshed.  Combined with
    # ``RESEARCH_CACHE_TTL_DAYS`` from config to decide cache hits.
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
