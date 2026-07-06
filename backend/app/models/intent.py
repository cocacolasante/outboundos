"""Signals & Intent Engine v2 (migration 0041).

A *signal* is a recent, evidenced EVENT about an org that implies a reason to
act now — not a list-membership fact.  Every signal carries an event date, a
source, an evidence URL, an intrinsic strength score, and a one-line "why now"
summary.  Intent per org is a time-decayed sum of its recent signals, bucketed
into tiers and gated by an ICP profile.

New tables (all coexist with the legacy ``prospect_signals`` / ``icp_profiles``
during the v1→v2 cutover; nothing here touches those):

  Org              — the persistent intent ANCHOR (EIN / NTEE / domain / 990
                     size band).  ``DiscoveredOrg`` was a transient dataclass;
                     this gives signals + scores something durable to hang off.
  Signal           — one evidenced event.  Idempotent on ``dedupe_key``.
  OrgIntentScore   — the per-org rolled-up intent score + tier (one row/org).
  IcpIntentProfile — per-tenant collection/scoring config (cause codes, geo,
                     size-band fit weights, per-signal-type weight overrides,
                     decay half-life, expiry, promotion threshold).

Tenancy: nullable ``tenant_id`` (the scaffolding that exists repo-wide) —
forward-compatible with the deferred RLS refactor, NOT enforced here.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean, DateTime, Enum, ForeignKey, Index, Integer, Numeric, Text,
    UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.tenancy.mixin import TenantMixin


# 990 annual-revenue bands — the org-fit multiplier keys off these (the ICP
# "sweet spot" is small/mid orgs showing activity without big in-house dev).
class OrgSizeBand(str, enum.Enum):
    MICRO = "micro"      # < $250k
    SMALL = "small"      # $250k – $1M
    MID = "mid"          # $1M – $10M
    LARGE = "large"      # $10M – $50M
    MAJOR = "major"      # > $50M


# Signal types, grouped by the tier they imply (the scoring target):
#   Tier 1 (act now): NEW_RFP, DEV_ROLE_POSTED, LAPSED_FUNDER
#   Tier 2 (warm):    REV_DROP, NEW_PROGRAM, PEER_FUNDED
#   Tier 3 (list):    NEW_501C3, CAUSE_MATCH  (scored low, never auto-promoted)
class IntentSignalType(str, enum.Enum):
    NEW_RFP = "new_rfp"
    DEV_ROLE_POSTED = "dev_role_posted"
    LAPSED_FUNDER = "lapsed_funder"
    REV_DROP = "rev_drop"
    NEW_PROGRAM = "new_program"
    PEER_FUNDED = "peer_funded"
    NEW_501C3 = "new_501c3"
    CAUSE_MATCH = "cause_match"


class IntentSignalSource(str, enum.Enum):
    PROPUBLICA = "propublica"
    GRANTS_GOV = "grants_gov"
    USASPENDING = "usaspending"
    IRS_BMF = "irs_bmf"
    JOBS = "jobs"          # Phase 6 (compliant provider TBD)
    MANUAL = "manual"


class IntentSignalStatus(str, enum.Enum):
    NEW = "new"
    SCORED = "scored"
    PROMOTED = "promoted"
    SUPPRESSED = "suppressed"
    EXPIRED = "expired"


class Org(TenantMixin, Base):
    """The intent anchor.  Deduped by EIN within a tenant (partial unique)."""
    __tablename__ = "orgs"
    __table_args__ = (
        # One org per EIN within a tenant.  NULLS NOT DISTINCT (PG15+) so the
        # current single-tenant reality (tenant_id IS NULL) still dedups by
        # EIN; partial so EIN-less orgs (pre-enrichment) aren't constrained.
        Index(
            "uq_orgs_tenant_ein", "tenant_id", "ein", unique=True,
            postgresql_where=text("ein IS NOT NULL"),
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    ein: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    domain: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    website: Mapped[str | None] = mapped_column(Text, nullable=True)
    ntee_code: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    state: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    city: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Latest 990 total revenue + the band derived from it (drives fit).
    annual_revenue: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    size_band: Mapped[OrgSizeBand | None] = mapped_column(
        Enum(OrgSizeBand, name="org_size_band",
             values_callable=lambda e: [m.value for m in e]),
        nullable=True, index=True,
    )
    # If the org was first seen as a lead, keep the back-link (SET NULL so
    # routine lead deletion never cascades away the org/intent history).
    source_lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="SET NULL"), nullable=True,
    )
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(), nullable=False,
    )


class Signal(TenantMixin, Base):
    """One evidenced intent event.  Idempotent via the per-tenant-unique
    ``dedupe_key`` (collectors upsert ON CONFLICT (tenant_id, dedupe_key);
    NULLS NOT DISTINCT keeps NULL-tenant worker rows app-unique)."""
    __tablename__ = "signals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "dedupe_key",
                         name="uq_signals_tenant_dedupe",
                         postgresql_nulls_not_distinct=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    signal_type: Mapped[IntentSignalType] = mapped_column(
        Enum(IntentSignalType, name="intent_signal_type",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    source: Mapped[IntentSignalSource] = mapped_column(
        Enum(IntentSignalSource, name="intent_signal_source",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    # Intrinsic strength of THIS event (decay + ICP weights applied later).
    score: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    # The contract: every signal must carry an event date + an evidence URL.
    event_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    evidence_url: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)  # the "why now" line
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    status: Mapped[IntentSignalStatus] = mapped_column(
        Enum(IntentSignalStatus, name="intent_signal_status",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=IntentSignalStatus.NEW,
        server_default=IntentSignalStatus.NEW.value,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


class OrgIntentScore(TenantMixin, Base):
    """Rolled-up intent for an org — one row per org (org_id is the PK)."""
    __tablename__ = "org_intent_scores"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    intent_score: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), nullable=False, server_default="0",
    )
    tier: Mapped[int] = mapped_column(Integer, nullable=False, index=True)  # 1 | 2 | 3
    top_signal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("signals.id", ondelete="SET NULL"), nullable=True,
    )
    fit_multiplier: Mapped[Decimal] = mapped_column(
        Numeric(6, 3), nullable=False, server_default="1",
    )
    last_computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


class IcpIntentProfile(TenantMixin, Base):
    """Per-tenant collection + scoring config (the GrantMind switch).

    Distinct from the legacy ``icp_profiles`` (lookalike fingerprint from
    closed-won deals); this drives WHICH events count and HOW they score.
    """
    __tablename__ = "icp_intent_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true",
    )
    # Target NTEE/cause codes + geographies (lists).
    cause_codes: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default="[]",
    )
    geographies: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default="[]",
    )
    # size band -> fit multiplier (the org-fit knob; sweet spot = small/mid).
    size_band_weights: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}",
    )
    # signal_type -> weight override (push dev-role / lapsed-funder to the top).
    signal_weights: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}",
    )
    # Grants.gov search keywords for the new_rfp collector (migration 0042).
    rfp_keywords: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default="[]",
    )
    # signal_type -> half_life_days override (migration 0042).  Falls back to
    # the per-type defaults in scoring.SIGNAL_PROFILE when a type is absent.
    half_life_overrides: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}",
    )
    # Global half-life fallback for signal types absent from SIGNAL_PROFILE
    # AND half_life_overrides.
    half_life_days: Mapped[Decimal] = mapped_column(
        Numeric(8, 2), nullable=False, server_default="30",
    )
    max_signal_age_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="180",
    )
    promotion_threshold: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), nullable=False, server_default="100",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(), nullable=False,
    )
