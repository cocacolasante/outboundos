"""TenantMixin — the one way a table becomes tenant-owned (Phase 2).

Adds an indexed, FK'd ``tenant_id`` column whose insert default reads the
ambient ``current_tenant_id`` ContextVar (set per-request by ``get_db``;
workers get it in the worker-context phase).  Nullable until every writer
stamps it — the NOT NULL promotion is a separate, gated migration after
workers carry tenant context.

The FK deliberately has no ON DELETE action: deleting a tenant with data
must fail until an explicit, audited tenant-deletion path exists
(Phase 7).  Applies to Core inserts too (``Column.default`` fires for
``pg_insert``), so ON CONFLICT upserts stamp the column without changes.
"""
from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from app.tenancy.context import current_tenant_id


class TenantMixin:
    @declared_attr
    def tenant_id(cls) -> Mapped[uuid.UUID | None]:  # noqa: N805
        return mapped_column(
            UUID(as_uuid=True),
            ForeignKey("tenants.id"),
            nullable=True,
            index=True,
            default=lambda: current_tenant_id.get(),
        )
