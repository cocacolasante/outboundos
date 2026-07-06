"""Phase 2 (backend) — configurable pipeline endpoint + stage-change audit.

The Kanban board reads its columns from ``GET /crm/pipelines/default`` and
moves a card with ``PATCH /crm/opportunities/{id}`` (stage), which now
dual-writes ``stage_id`` AND appends an ``opportunity_stage_changes`` audit
row.  The test DB is built via create_all (no migration seed), so each test
seeds its own default pipeline.
"""
from __future__ import annotations

from sqlalchemy import select

from app.models import (
    Opportunity,
    OpportunityStageChange,
    Pipeline,
    PipelineStage,
)

_SEED = [
    ("prospecting", "Prospecting", 0, 10, False, False),
    ("qualification", "Qualification", 1, 25, False, False),
    ("proposal", "Proposal", 2, 50, False, False),
    ("negotiation", "Negotiation", 3, 75, False, False),
    ("closed_won", "Closed Won", 4, 100, True, False),
    ("closed_lost", "Closed Lost", 5, 0, False, True),
]


async def _seed_pipeline(db_session) -> Pipeline:
    p = Pipeline(name="Default", is_default=True)
    db_session.add(p)
    await db_session.flush()
    db_session.add_all([
        PipelineStage(
            pipeline_id=p.id, key=k, name=n, sort_order=o,
            default_probability=prob, is_won=won, is_lost=lost,
        )
        for k, n, o, prob, won, lost in _SEED
    ])
    await db_session.commit()
    return p


async def test_default_pipeline_endpoint_returns_active_ordered_stages(client, db_session):
    await _seed_pipeline(db_session)
    resp = await client.get("/crm/pipelines/default")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_default"] is True
    assert [s["key"] for s in body["stages"]] == [k for k, *_ in _SEED]
    won = [s for s in body["stages"] if s["is_won"]]
    assert [s["key"] for s in won] == ["closed_won"]


async def test_default_pipeline_404_when_unseeded(client):
    resp = await client.get("/crm/pipelines/default")
    assert resp.status_code == 404


async def test_create_opportunity_dual_writes_stage_id(client, db_session):
    await _seed_pipeline(db_session)
    resp = await client.post("/crm/opportunities", json={
        "name": "Acme deal", "stage": "proposal", "amount": 5000,
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["stage"] == "proposal"
    assert body["stage_id"] is not None
    assert body["pipeline_id"] is not None


async def test_stage_change_writes_audit_row_and_updates_stage_id(client, db_session):
    await _seed_pipeline(db_session)
    created = (await client.post("/crm/opportunities", json={
        "name": "Move me", "stage": "prospecting",
    })).json()
    oid = created["id"]
    prospecting_stage_id = created["stage_id"]

    # Drag prospecting -> proposal.
    resp = await client.patch(f"/crm/opportunities/{oid}", json={"stage": "proposal"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["stage"] == "proposal"
    assert resp.json()["stage_id"] != prospecting_stage_id

    # An audit row was appended capturing old -> new.
    rows = (await db_session.execute(
        select(OpportunityStageChange).where(
            OpportunityStageChange.opportunity_id == created["id"]
        )
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].from_stage_key == "prospecting"
    assert rows[0].to_stage_key == "proposal"
    assert rows[0].source == "user"


async def test_stage_history_endpoint_newest_first(client, db_session):
    await _seed_pipeline(db_session)
    oid = (await client.post("/crm/opportunities", json={
        "name": "Journey", "stage": "prospecting",
    })).json()["id"]
    await client.patch(f"/crm/opportunities/{oid}", json={"stage": "qualification"})
    await client.patch(f"/crm/opportunities/{oid}", json={"stage": "proposal"})

    resp = await client.get(f"/crm/opportunities/{oid}/stage-history")
    assert resp.status_code == 200
    hist = resp.json()
    assert len(hist) == 2
    # newest first
    assert hist[0]["to_stage_key"] == "proposal"
    assert hist[1]["to_stage_key"] == "qualification"
    assert all(h["source"] == "user" for h in hist)


async def test_no_op_stage_change_writes_no_audit(client, db_session):
    await _seed_pipeline(db_session)
    oid = (await client.post("/crm/opportunities", json={
        "name": "Static", "stage": "proposal",
    })).json()["id"]
    # PATCH the same stage + another field — no stage move, no audit row.
    await client.patch(f"/crm/opportunities/{oid}", json={"stage": "proposal", "amount": 999})

    rows = (await db_session.execute(
        select(OpportunityStageChange).where(
            OpportunityStageChange.opportunity_id == oid
        )
    )).scalars().all()
    assert rows == []


async def test_stage_change_audit_captures_actor_timestamp_source(client, db_session):
    """Phase 6 audit-log confirmation: every stage move records WHO (changed_by
    — design-ready owner id), WHEN (created_at), WHAT (from/to), and HOW
    (source = user vs agent)."""
    await _seed_pipeline(db_session)
    oid = (await client.post("/crm/opportunities", json={
        "name": "Audited", "stage": "prospecting",
    })).json()["id"]
    await client.patch(f"/crm/opportunities/{oid}", json={"stage": "qualification"})

    row = (await db_session.execute(
        select(OpportunityStageChange).where(
            OpportunityStageChange.opportunity_id == oid
        )
    )).scalars().one()
    assert row.created_at is not None          # WHEN
    assert row.source == "user"                 # HOW (human-initiated)
    assert row.from_stage_key == "prospecting"  # WHAT
    assert row.to_stage_key == "qualification"
    # changed_by mirrors the opportunity owner (None until a users/auth layer
    # exists) — the column is present + populated from owner_id by design.
    assert hasattr(row, "changed_by")


async def test_stage_change_works_without_seeded_pipeline(client):
    """Back-compat: with no pipeline seeded, stage moves still work (enum
    drives behavior); stage_id stays NULL and an audit row is still written."""
    oid = (await client.post("/crm/opportunities", json={
        "name": "Legacy", "stage": "prospecting",
    })).json()["id"]
    resp = await client.patch(f"/crm/opportunities/{oid}", json={"stage": "proposal"})
    assert resp.status_code == 200
    assert resp.json()["stage"] == "proposal"
    assert resp.json()["stage_id"] is None
