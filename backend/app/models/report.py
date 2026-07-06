"""Saved report definitions for the custom report builder (Phase 1+3).

A report is a STRUCTURED, metadata-driven definition — never raw SQL.  The
``definition`` JSONB stores the report's object, selected columns, filters,
grouping, sort, aggregates, and (relative) date range; the Phase-3 query
service resolves it against a server-side whitelist registry into a
parameterized SQLAlchemy query.  See ``docs/crm-extension-audit.md`` §5.4 for
the JSON shape.

Tenancy-ready: nullable ``tenant_id`` (indexed), queried via a tenant-aware
filter later.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.tenancy.mixin import TenantMixin


class ReportDefinition(TenantMixin, Base):
    __tablename__ = "report_definitions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Reportable object key from the whitelist registry: leads | activities |
    # opportunities | contacts | accounts.  TEXT (not enum) so the registry
    # can grow without a migration.
    data_source: Mapped[str] = mapped_column(Text, nullable=False)
    # Structured definition (columns / filters / group_by / aggregates / sort /
    # date range).  Resolved ONLY through the whitelist — no user SQL.
    definition: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}",
    )
    # Design-ready owner (no FK — no users table yet).
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )
