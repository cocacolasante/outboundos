"""Persistent slug → URN cache for LinkedIn profiles.

Unipile's ``GET /api/v1/users/{slug}`` is the only way to map a public
LinkedIn slug (``jane-doe``) to the canonical member URN
(``ACoAA...``).  Every other endpoint that touches a profile needs the
URN — including ``recent_posts`` used by the Social Radar watchlist.
The mapping is permanent (slugs don't get re-issued to different
members) so we cache it in this table once resolved.

At 200+ watchlist profiles, caching the URN cuts per-run Unipile calls
in half (from 2 per profile to 1) — the difference between ~13min and
~6min runs.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LinkedInProfileCache(Base):
    __tablename__ = "linkedin_profile_cache"

    slug: Mapped[str] = mapped_column(Text, primary_key=True)
    provider_id: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
