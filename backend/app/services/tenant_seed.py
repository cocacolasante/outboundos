"""Per-tenant seed data (multi-tenancy Phase 2).

Called at signup so every new tenant starts with the same defaults the
single-tenant app got from migrations: the "Default" CRM pipeline with
its 6 stages (mirroring migration 0038's seed) and an AgentSettings row.
Idempotent-by-construction only in the sense that signup runs it exactly
once per tenant; it does not guard against re-runs.
"""
from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentSettings, Pipeline, PipelineStage

# (key, name, sort_order, default_probability, is_won, is_lost) — the same
# ladder migration 0038 seeded, sourced from the legacy stage enum.
_DEFAULT_STAGES = [
    ("prospecting", "Prospecting", 0, 10, False, False),
    ("qualification", "Qualification", 1, 25, False, False),
    ("proposal", "Proposal", 2, 50, False, False),
    ("negotiation", "Negotiation", 3, 75, False, False),
    ("closed_won", "Closed Won", 4, 100, True, False),
    ("closed_lost", "Closed Lost", 5, 0, False, True),
]


async def seed_tenant_defaults(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Create the default pipeline (+stages) and AgentSettings for a new
    tenant.  Caller owns the transaction."""
    pipeline = Pipeline(name="Default", is_default=True, tenant_id=tenant_id)
    session.add(pipeline)
    await session.flush()
    for key, name, order, prob, won, lost in _DEFAULT_STAGES:
        session.add(PipelineStage(
            pipeline_id=pipeline.id,
            tenant_id=tenant_id,
            name=name,
            key=key,
            sort_order=order,
            default_probability=prob,
            is_won=won,
            is_lost=lost,
            is_active=True,
        ))
    session.add(AgentSettings(tenant_id=tenant_id))
