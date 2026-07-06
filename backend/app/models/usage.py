"""Usage counters (billing, multi-tenancy Phase 5).

One row per (tenant, YYYYMM period, meter) with an atomically-upserted
count — quota checks never ``count(*)``-scan event tables on the hot
send/compose paths.  Written ONLY through
``billing.entitlements.check_quota``.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.tenancy.mixin import TenantMixin


class UsageCounter(TenantMixin, Base):
    __tablename__ = "usage_counters"
    __table_args__ = (
        UniqueConstraint("tenant_id", "period", "meter",
                         name="uq_usage_counters_tenant_period_meter",
                         postgresql_nulls_not_distinct=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    # Calendar month, e.g. "202607" — quotas are per-month.
    period: Mapped[str] = mapped_column(Text, nullable=False)
    meter: Mapped[str] = mapped_column(Text, nullable=False)
    count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(), nullable=False,
    )
