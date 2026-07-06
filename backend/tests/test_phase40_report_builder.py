"""Phase 3 — custom report builder: metadata-driven query service + CRUD.

Verifies the query service resolves only whitelisted fields/operators
(never raw SQL), runs tabular + grouped/aggregated reports with filters and
sorting, and that saved reports CRUD + run end-to-end.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.models import (
    CrmActivity,
    CrmActivityType,
    Opportunity,
    OpportunityStage,
)


async def _seed_opps(db_session):
    now = datetime.now(timezone.utc)
    opps = [
        Opportunity(name="Acme big", stage=OpportunityStage.PROPOSAL, amount=10000, company="Acme"),
        Opportunity(name="Acme small", stage=OpportunityStage.PROPOSAL, amount=2000, company="Acme"),
        Opportunity(name="Beeco deal", stage=OpportunityStage.NEGOTIATION, amount=7000, company="Beeco"),
        Opportunity(name="Won one", stage=OpportunityStage.CLOSED_WON, amount=5000, company="Cee"),
    ]
    db_session.add_all(opps)
    await db_session.commit()
    return opps


# --------------------------------------------------------------------------
# Metadata
# --------------------------------------------------------------------------

async def test_metadata_lists_objects_fields_operators(client):
    resp = await client.get("/reports/metadata")
    assert resp.status_code == 200
    body = resp.json()
    keys = {o["key"] for o in body["objects"]}
    assert {"leads", "opportunities", "activities", "contacts", "accounts"} <= keys
    opp = next(o for o in body["objects"] if o["key"] == "opportunities")
    amount = next(f for f in opp["fields"] if f["key"] == "amount")
    assert amount["type"] == "number"
    assert "gte" in amount["operators"]
    assert "sum" in amount["aggregates"]
    assert "last_30_days" in body["relative_ranges"]


# --------------------------------------------------------------------------
# Run (ad-hoc)
# --------------------------------------------------------------------------

async def test_run_tabular(client, db_session):
    await _seed_opps(db_session)
    resp = await client.post("/reports/run", json={
        "data_source": "opportunities",
        "definition": {
            "columns": ["name", "stage", "amount"],
            "sort": [{"field": "amount", "dir": "desc"}],
        },
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [c["key"] for c in body["columns"]] == ["name", "stage", "amount"]
    assert body["grouped"] is False
    assert body["rows"][0]["amount"] == 10000  # sorted desc
    assert body["row_count"] == 4


async def test_run_filters_number_and_enum(client, db_session):
    await _seed_opps(db_session)
    resp = await client.post("/reports/run", json={
        "data_source": "opportunities",
        "definition": {
            "columns": ["name", "amount"],
            "filters": [
                {"field": "stage", "op": "equals", "value": "proposal"},
                {"field": "amount", "op": "gte", "value": 5000},
            ],
        },
    })
    assert resp.status_code == 200, resp.text
    rows = resp.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["name"] == "Acme big"


async def test_run_grouped_sum_and_count(client, db_session):
    await _seed_opps(db_session)
    resp = await client.post("/reports/run", json={
        "data_source": "opportunities",
        "definition": {
            "group_by": ["stage"],
            "aggregates": [{"field": "amount", "fn": "sum"}, {"fn": "count"}],
            "sort": [{"field": "amount_sum", "dir": "desc"}],
        },
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["grouped"] is True
    cols = {c["key"] for c in body["columns"]}
    assert cols == {"stage", "amount_sum", "count"}
    proposal = next(r for r in body["rows"] if r["stage"] == "proposal")
    assert proposal["amount_sum"] == 12000  # 10000 + 2000
    assert proposal["count"] == 2


async def test_run_relative_date_range(client, db_session):
    await _seed_opps(db_session)
    resp = await client.post("/reports/run", json={
        "data_source": "opportunities",
        "definition": {
            "columns": ["name"],
            "filters": [{"field": "created_at", "op": "relative_range", "value": "last_30_days"}],
        },
    })
    assert resp.status_code == 200, resp.text
    # all just-created rows fall in the last 30 days
    assert resp.json()["row_count"] == 4


async def test_run_contains_filter_on_string(client, db_session):
    await _seed_opps(db_session)
    resp = await client.post("/reports/run", json={
        "data_source": "opportunities",
        "definition": {
            "columns": ["name", "company"],
            "filters": [{"field": "company", "op": "contains", "value": "cme"}],
        },
    })
    assert resp.status_code == 200
    assert resp.json()["row_count"] == 2  # both Acme rows


# --------------------------------------------------------------------------
# Safety — only whitelisted refs; never raw SQL
# --------------------------------------------------------------------------

async def test_run_rejects_unknown_field(client):
    resp = await client.post("/reports/run", json={
        "data_source": "opportunities",
        "definition": {"columns": ["amount", "secret_column"]},
    })
    assert resp.status_code == 400
    assert "unknown field" in resp.json()["detail"]


async def test_run_rejects_unknown_data_source(client):
    resp = await client.post("/reports/run", json={
        "data_source": "users; DROP TABLE leads",
        "definition": {"columns": ["x"]},
    })
    assert resp.status_code == 400


async def test_run_rejects_disallowed_operator(client):
    resp = await client.post("/reports/run", json={
        "data_source": "opportunities",
        # 'contains' is a string op — not allowed on a number field
        "definition": {"filters": [{"field": "amount", "op": "contains", "value": "5"}], "columns": ["name"]},
    })
    assert resp.status_code == 400
    assert "not allowed" in resp.json()["detail"]


async def test_run_rejects_bad_number_value(client):
    resp = await client.post("/reports/run", json={
        "data_source": "opportunities",
        "definition": {"filters": [{"field": "amount", "op": "gte", "value": "abc"}], "columns": ["name"]},
    })
    assert resp.status_code == 400


# --------------------------------------------------------------------------
# CRUD + run saved
# --------------------------------------------------------------------------

async def test_create_validates_definition(client):
    bad = await client.post("/reports", json={
        "name": "Bad", "data_source": "opportunities",
        "definition": {"columns": ["nope"]},
    })
    assert bad.status_code == 400


async def test_report_crud_and_run_saved(client, db_session):
    await _seed_opps(db_session)
    # create
    created = await client.post("/reports", json={
        "name": "Open deals by stage",
        "description": "pipeline",
        "data_source": "opportunities",
        "definition": {
            "group_by": ["stage"],
            "aggregates": [{"field": "amount", "fn": "sum"}],
        },
    })
    assert created.status_code == 201, created.text
    rid = created.json()["id"]

    # list + get
    assert any(r["id"] == rid for r in (await client.get("/reports")).json())
    assert (await client.get(f"/reports/{rid}")).json()["name"] == "Open deals by stage"

    # patch
    patched = await client.patch(f"/reports/{rid}", json={"name": "Renamed"})
    assert patched.json()["name"] == "Renamed"

    # run saved
    run = await client.post(f"/reports/{rid}/run")
    assert run.status_code == 200, run.text
    assert run.json()["grouped"] is True
    assert {c["key"] for c in run.json()["columns"]} == {"stage", "amount_sum"}

    # duplicate
    dup = await client.post(f"/reports/{rid}/duplicate")
    assert dup.status_code == 201
    assert dup.json()["name"] == "Renamed (copy)"
    assert dup.json()["id"] != rid

    # delete
    assert (await client.delete(f"/reports/{rid}")).status_code == 204
    assert (await client.get(f"/reports/{rid}")).status_code == 404


async def test_patch_rejects_definition_referencing_unknown_field(client):
    created = await client.post("/reports", json={
        "name": "Valid", "data_source": "leads",
        "definition": {"columns": ["email"]},
    })
    rid = created.json()["id"]
    bad = await client.patch(f"/reports/{rid}", json={
        "definition": {"columns": ["email", "ssn"]},
    })
    assert bad.status_code == 400
