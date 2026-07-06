"""Multi-tenancy Phase 2: data-model tenancy.

Covers: TenantMixin insert stamping (request path via the client fixture's
ContextVar, worker path stays NULL), the per-tenant conversions
(agent_settings, default sender, suppression, research cache), signup
seeding, and the NULLS-NOT-DISTINCT dedup semantics for tenant-less rows.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import (
    AgentSettings,
    Campaign,
    ConnectedAccount,
    Pipeline,
    ResearchCache,
    Suppression,
    SuppressionReason,
    Tenant,
)
from tests.conftest import BOOTSTRAP_TENANT_ID

pytestmark = pytest.mark.asyncio


async def _second_tenant(db_session) -> uuid.UUID:
    t = Tenant(name="Other Co", slug=f"other-{uuid.uuid4().hex[:6]}")
    db_session.add(t)
    await db_session.flush()
    return t.id


# --- TenantMixin stamping -----------------------------------------------------


async def test_router_created_rows_are_stamped_with_the_ambient_tenant(client, db_session):
    resp = await client.post("/campaigns/", json={
        "name": "Tenanted campaign", "goal": "g", "tone": "friendly",
        "sender_name": "A", "sender_email": "a@x.com",
        "schedule_time_start": "09:00:00", "schedule_time_end": "17:00:00",
    })
    assert resp.status_code in (200, 201), resp.text
    row = (await db_session.execute(select(Campaign))).scalars().one()
    assert row.tenant_id == BOOTSTRAP_TENANT_ID


async def test_direct_orm_writes_without_context_stay_null(db_session):
    from datetime import time

    db_session.add(Campaign(name="c", goal="g", tone="t",
                            sender_name="s", sender_email="s@x.com",
                            schedule_time_start=time(9, 0),
                            schedule_time_end=time(17, 0)))
    await db_session.commit()
    row = (await db_session.execute(select(Campaign))).scalars().one()
    assert row.tenant_id is None


# --- Per-tenant AgentSettings ---------------------------------------------------


async def test_agent_settings_one_row_per_tenant(auth_client, db_session):
    # Two registrations → two tenants → two seeded AgentSettings rows.
    r1 = await auth_client.post("/auth/register", json={
        "email": "a@example.com", "password": "hunter2hunter2", "tenant_name": "T One",
    })
    auth_client.cookies.clear()
    r2 = await auth_client.post("/auth/register", json={
        "email": "b@example.com", "password": "hunter2hunter2", "tenant_name": "T Two",
    })
    assert r1.status_code == r2.status_code == 201
    rows = (await db_session.execute(select(AgentSettings))).scalars().all()
    tenants = {str(r.tenant_id) for r in rows}
    assert r1.json()["tenant_id"] in tenants
    assert r2.json()["tenant_id"] in tenants
    assert len(rows) == 2


async def test_agent_settings_unique_per_tenant_including_null(db_session):
    db_session.add(AgentSettings(tenant_id=None))
    await db_session.commit()
    db_session.add(AgentSettings(tenant_id=None))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_signup_seeds_default_pipeline(auth_client, db_session):
    resp = await auth_client.post("/auth/register", json={
        "email": "seed@example.com", "password": "hunter2hunter2",
    })
    assert resp.status_code == 201
    tid = uuid.UUID(resp.json()["tenant_id"])
    pipeline = (
        await db_session.execute(select(Pipeline).where(Pipeline.tenant_id == tid))
    ).scalars().one()
    assert pipeline.is_default is True
    from app.models import PipelineStage
    stages = (await db_session.execute(
        select(PipelineStage).where(PipelineStage.pipeline_id == pipeline.id)
    )).scalars().all()
    assert len(stages) == 6
    assert all(s.tenant_id == tid for s in stages)


# --- Per-tenant default sender ---------------------------------------------------


async def test_each_tenant_can_have_its_own_default_sender(db_session):
    t2 = await _second_tenant(db_session)

    def acct(email, tenant_id):
        return ConnectedAccount(
            label=email, email_address=email, imap_host="h", username=email,
            password_encrypted="x", is_default_sender=True, tenant_id=tenant_id,
        )

    db_session.add(acct("one@x.com", BOOTSTRAP_TENANT_ID))
    db_session.add(acct("two@x.com", t2))
    await db_session.commit()  # two defaults, different tenants — fine

    db_session.add(acct("three@x.com", t2))  # second default for t2 — blocked
    with pytest.raises(IntegrityError):
        await db_session.commit()


# --- Per-tenant suppression -------------------------------------------------------


async def test_same_email_suppressed_by_two_tenants(db_session):
    t2 = await _second_tenant(db_session)
    db_session.add(Suppression(email="p@x.com", reason=SuppressionReason.UNSUBSCRIBED,
                               tenant_id=BOOTSTRAP_TENANT_ID))
    db_session.add(Suppression(email="p@x.com", reason=SuppressionReason.UNSUBSCRIBED,
                               tenant_id=t2))
    await db_session.commit()  # per-tenant rows coexist

    # NULLS NOT DISTINCT: two tenant-less rows for one email still collide
    # (legacy worker semantics preserved).
    db_session.add(Suppression(email="q@x.com", reason=SuppressionReason.HARD_BOUNCE))
    await db_session.commit()
    db_session.add(Suppression(email="q@x.com", reason=SuppressionReason.HARD_BOUNCE))
    with pytest.raises(IntegrityError):
        await db_session.commit()


# --- Research cache ---------------------------------------------------------------


async def test_research_cache_upsert_still_dedupes_tenantless_rows(db_session):
    from app.services import research_cache as rc

    await rc.upsert(db_session, "Lead@X.com", {"quality": "high"})
    await rc.upsert(db_session, "lead@x.com", {"quality": "rich", "new": True})
    await db_session.commit()

    rows = (await db_session.execute(select(ResearchCache))).scalars().all()
    assert len(rows) == 1
    assert rows[0].research_data["quality"] == "rich"
    assert (await rc.lookup(db_session, "LEAD@x.com "))["quality"] == "rich"
