"""Phase 42 — CRM reporting endpoints (/crm/reports).

Covers the date-scoped overview (KPIs, won/lost trend, pipeline +
forecast snapshots, loss reasons, activity breakdown, funnel) and the
deal / activity detail-row endpoints that back the tables + CSV export.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from app.models import (
    CrmActivity,
    CrmActivityDirection,
    CrmActivityType,
    Lead,
    Opportunity,
    OpportunityStage,
)

pytestmark = pytest.mark.asyncio


def _dt(days_ago: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days_ago)


async def _opp(db_session, **kw) -> Opportunity:
    defaults = dict(name="Deal", company="Acme", email="a@x.com")
    defaults.update(kw)
    o = Opportunity(**defaults)
    db_session.add(o)
    await db_session.commit()
    await db_session.refresh(o)
    return o


# ---------------------------------------------------------------------------
# overview
# ---------------------------------------------------------------------------


async def test_overview_kpis_won_lost_winrate_cycle(client, db_session):
    # Two won (created 30d ago, closed 10d ago → ~20d cycle), one lost.
    await _opp(db_session, name="W1", stage=OpportunityStage.CLOSED_WON,
               amount=10000, created_at=_dt(30), closed_at=_dt(10))
    await _opp(db_session, name="W2", stage=OpportunityStage.CLOSED_WON,
               amount=30000, created_at=_dt(40), closed_at=_dt(20))
    await _opp(db_session, name="L1", stage=OpportunityStage.CLOSED_LOST,
               amount=5000, created_at=_dt(30), closed_at=_dt(5),
               loss_reason="Budget cut")
    # Open deal (snapshot, not date-scoped).
    await _opp(db_session, name="O1", stage=OpportunityStage.PROPOSAL,
               amount=20000, probability=50)

    resp = await client.get("/crm/reports/overview")
    assert resp.status_code == 200, resp.text
    k = resp.json()["kpis"]
    assert k["won_count"] == 2
    assert k["won_value"] == 40000.0
    assert k["lost_count"] == 1
    assert k["lost_value"] == 5000.0
    assert k["win_rate"] == round(2 / 3, 4)
    assert k["avg_deal_size"] == 20000.0
    # ~20d and ~20d cycle.
    assert 18 <= k["avg_sales_cycle_days"] <= 22
    # Open snapshot: 1 deal, 20k, weighted 50%.
    assert k["open_count"] == 1
    assert k["open_value"] == 20000.0
    assert k["open_weighted_value"] == 10000.0


async def test_overview_pipeline_forecast_and_weighting(client, db_session):
    # Proposal (explicit 50%) + negotiation (default 75%).
    await _opp(db_session, stage=OpportunityStage.PROPOSAL, amount=10000,
               probability=50, close_date=date(2026, 8, 15))
    await _opp(db_session, stage=OpportunityStage.NEGOTIATION, amount=8000,
               close_date=date(2026, 8, 20))
    # No close_date → "unscheduled" forecast bucket.
    await _opp(db_session, stage=OpportunityStage.PROSPECTING, amount=4000)

    body = (await client.get("/crm/reports/overview")).json()
    by_stage = {r["stage"]: r for r in body["pipeline_by_stage"]}
    assert by_stage["proposal"]["weighted_amount"] == 5000.0   # 10000 × 50%
    assert by_stage["negotiation"]["weighted_amount"] == 6000.0  # 8000 × 75% default
    # Closed stages are not in the pipeline snapshot.
    assert "closed_won" not in by_stage

    fc = {f["month"]: f for f in body["forecast"]}
    assert fc["2026-08"]["count"] == 2
    assert fc["2026-08"]["weighted_amount"] == 11000.0          # 5000 + 6000
    assert "unscheduled" in fc
    assert body["forecast"][-1]["month"] == "unscheduled"       # always last


async def test_overview_loss_reasons_and_activity_breakdown(client, db_session):
    await _opp(db_session, stage=OpportunityStage.CLOSED_LOST, amount=1000,
               created_at=_dt(20), closed_at=_dt(10), loss_reason="Price")
    await _opp(db_session, stage=OpportunityStage.CLOSED_LOST, amount=2000,
               created_at=_dt(20), closed_at=_dt(9), loss_reason="Price")
    await _opp(db_session, stage=OpportunityStage.CLOSED_LOST, amount=500,
               created_at=_dt(20), closed_at=_dt(8))  # no reason → "Not specified"

    lead = Lead(campaign_id=None, email="x@y.com")
    db_session.add(lead)
    await db_session.flush()
    db_session.add_all([
        CrmActivity(lead_id=lead.id, activity_type=CrmActivityType.CALL,
                    direction=CrmActivityDirection.OUTBOUND, subject="call",
                    occurred_at=_dt(3)),
        CrmActivity(lead_id=lead.id, activity_type=CrmActivityType.EMAIL,
                    direction=CrmActivityDirection.INBOUND, subject="reply",
                    occurred_at=_dt(2), is_agent_generated=True),
        CrmActivity(lead_id=lead.id, activity_type=CrmActivityType.NOTE,
                    subject="note", occurred_at=_dt(1)),
    ])
    await db_session.commit()

    body = (await client.get("/crm/reports/overview")).json()
    reasons = {r["reason"]: r for r in body["loss_reasons"]}
    assert reasons["Price"]["count"] == 2
    assert reasons["Price"]["value"] == 3000.0
    assert reasons["Not specified"]["count"] == 1
    # Sorted by count desc → Price first.
    assert body["loss_reasons"][0]["reason"] == "Price"

    ab = body["activity_breakdown"]
    assert ab["total"] == 3
    assert ab["by_type"] == {"call": 1, "email": 1, "note": 1}
    assert ab["by_direction"] == {"outbound": 1, "inbound": 1}
    assert ab["agent_generated"] == 1
    assert ab["human_logged"] == 2


async def test_overview_funnel_and_conversions(client, db_session):
    # 4 new leads, 2 opps created (1 a conversion), 1 won.
    for i in range(4):
        db_session.add(Lead(campaign_id=None, email=f"l{i}@x.com", created_at=_dt(5)))
    src = Lead(campaign_id=None, email="src@x.com", created_at=_dt(5))
    db_session.add(src)
    await db_session.flush()
    await _opp(db_session, name="conv", stage=OpportunityStage.QUALIFICATION,
               created_at=_dt(4), source_lead_id=src.id)
    await _opp(db_session, name="won", stage=OpportunityStage.CLOSED_WON,
               amount=1000, created_at=_dt(4), closed_at=_dt(2))

    body = (await client.get("/crm/reports/overview")).json()
    f = body["funnel"]
    assert f["new_leads"] == 5
    assert f["opportunities_created"] == 2
    assert f["won"] == 1
    assert f["lead_to_opp_rate"] == round(2 / 5, 4)
    assert f["opp_to_won_rate"] == round(1 / 2, 4)
    assert body["kpis"]["conversions"] == 1   # only the source-lead-linked opp


async def test_overview_respects_date_window(client, db_session):
    # Won 200 days ago — outside the default 90-day window.
    await _opp(db_session, stage=OpportunityStage.CLOSED_WON, amount=99999,
               created_at=_dt(210), closed_at=_dt(200))
    # Won 5 days ago — inside.
    await _opp(db_session, stage=OpportunityStage.CLOSED_WON, amount=1000,
               created_at=_dt(20), closed_at=_dt(5))

    body = (await client.get("/crm/reports/overview")).json()
    assert body["kpis"]["won_count"] == 1
    assert body["kpis"]["won_value"] == 1000.0

    # Widen the window to include the old deal.
    start = (date.today() - timedelta(days=365)).isoformat()
    body = (await client.get(f"/crm/reports/overview?start={start}")).json()
    assert body["kpis"]["won_count"] == 2


async def test_overview_bad_range_422(client):
    resp = await client.get("/crm/reports/overview?start=2026-12-01&end=2026-01-01")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# deals
# ---------------------------------------------------------------------------


async def test_deals_won_rows(client, db_session):
    await _opp(db_session, name="WonDeal", stage=OpportunityStage.CLOSED_WON,
               amount=12000, created_at=_dt(30), closed_at=_dt(10))
    await _opp(db_session, name="LostDeal", stage=OpportunityStage.CLOSED_LOST,
               amount=5000, created_at=_dt(30), closed_at=_dt(8))

    resp = await client.get("/crm/reports/deals?outcome=won")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["total_amount"] == 12000.0
    row = body["items"][0]
    assert row["name"] == "WonDeal"
    assert row["age_days"] == 20          # created 30d, closed 10d ago
    assert row["closed_at"] is not None

    lost = (await client.get("/crm/reports/deals?outcome=lost")).json()
    assert lost["count"] == 1
    assert lost["items"][0]["name"] == "LostDeal"


async def test_deals_open_ignores_window(client, db_session):
    await _opp(db_session, name="OldOpen", stage=OpportunityStage.PROPOSAL,
               amount=7000, probability=50, created_at=_dt(300))
    # Narrow window — open snapshot should still include the old open deal.
    start = (date.today() - timedelta(days=7)).isoformat()
    body = (await client.get(f"/crm/reports/deals?outcome=open&start={start}")).json()
    assert body["count"] == 1
    assert body["items"][0]["weighted_amount"] == 3500.0  # 7000 × 50%
    assert body["items"][0]["age_days"] >= 290


# ---------------------------------------------------------------------------
# activities
# ---------------------------------------------------------------------------


async def test_activities_rows_and_type_filter(client, db_session):
    opp = await _opp(db_session, name="Deal X", email="deal@x.com")
    db_session.add_all([
        CrmActivity(opportunity_id=opp.id, activity_type=CrmActivityType.CALL,
                    direction=CrmActivityDirection.OUTBOUND, subject="Discovery call",
                    occurred_at=_dt(3)),
        CrmActivity(opportunity_id=opp.id, activity_type=CrmActivityType.MEETING,
                    subject="Demo", occurred_at=_dt(1)),
    ])
    await db_session.commit()

    body = (await client.get("/crm/reports/activities")).json()
    assert body["count"] == 2
    # Newest first.
    assert body["items"][0]["subject"] == "Demo"
    assert body["items"][1]["opportunity_name"] == "Deal X"
    assert body["items"][1]["contact"] == "deal@x.com"

    calls = (await client.get("/crm/reports/activities?activity_type=call")).json()
    assert calls["count"] == 1
    assert calls["items"][0]["activity_type"] == "call"


async def test_activities_window_scopes_by_occurred_at(client, db_session):
    lead = Lead(campaign_id=None, email="z@x.com")
    db_session.add(lead)
    await db_session.flush()
    db_session.add_all([
        CrmActivity(lead_id=lead.id, activity_type=CrmActivityType.NOTE,
                    subject="recent", occurred_at=_dt(2)),
        CrmActivity(lead_id=lead.id, activity_type=CrmActivityType.NOTE,
                    subject="old", occurred_at=_dt(120)),
    ])
    await db_session.commit()

    body = (await client.get("/crm/reports/activities")).json()
    subjects = [i["subject"] for i in body["items"]]
    assert subjects == ["recent"]   # 120d-old is outside the 90d default
