"""Phase 6 (UI) — intent-engine dashboard + run-control endpoints.

Backs the Settings → Intent tab: /intent/status, /orgs/seed, /collectors/run,
/recompute, /promote.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models import (
    Campaign, CampaignStatus, IcpIntentProfile, IntentSignalSource,
    IntentSignalStatus, IntentSignalType, Lead, Org, OrgIntentScore, OrgSizeBand,
    SendStatus, Signal,
)
from app.routers import intent_profiles
from app.services.intent import orgs as orgs_mod

pytestmark = pytest.mark.asyncio

# Anchored to the real clock: recompute applies time-decay from actual
# ``now()``, so a frozen NOW rots — the seeded signal decays a little more
# every real day until it slips under the promotion threshold.
NOW = datetime.now(timezone.utc)


async def _seed(db_session):
    org = Org(name="Helpful NP", ein="111111111", state="NY", ntee_code="T31",
              size_band=OrgSizeBand.MID)
    db_session.add(org)
    await db_session.flush()
    sig = Signal(
        org_id=org.id, signal_type=IntentSignalType.NEW_RFP,
        source=IntentSignalSource.GRANTS_GOV, score=Decimal("70"), event_date=NOW,
        evidence_url="https://grants.gov/x", summary="New RFP — why now",
        dedupe_key="k1", status=IntentSignalStatus.SCORED,
    )
    db_session.add(sig)
    await db_session.flush()
    db_session.add(OrgIntentScore(org_id=org.id, tier=1, intent_score=Decimal("200"),
                                  top_signal_id=sig.id, fit_multiplier=Decimal("1.4")))
    profile = IcpIntentProfile(name="GrantMind Pro", is_active=True,
                               promotion_threshold=Decimal("80"))
    db_session.add(profile)
    await db_session.commit()
    return org, sig, profile


async def test_status_counts(client, db_session):
    await _seed(db_session)
    r = await client.get("/intent/status")
    assert r.status_code == 200
    body = r.json()
    assert body["orgs_total"] == 1
    assert body["signals_by_type"]["new_rfp"] == 1
    assert body["tiers"]["1"] == 1
    assert body["active_profile_id"] is not None
    assert "adzuna_configured" in body


async def test_seed_orgs_endpoint(client, db_session, monkeypatch):
    async def fake_search(query, *, page=0):
        return [{"ein": "92-0155067", "name": "Alaska CF", "ntee_code": "T31",
                 "state": "AK", "city": "Anchorage"}]
    monkeypatch.setattr(orgs_mod.propublica, "search_orgs", fake_search)

    r = await client.post("/intent/orgs/seed", json={"query": "community foundation", "limit": 10})
    assert r.status_code == 200
    body = r.json()
    assert body["created"] == 1 and body["orgs_total"] == 1
    assert await db_session.scalar(select(func.count()).select_from(Org)) == 1


async def test_run_collectors_enqueues(client, db_session, monkeypatch):
    calls = []
    monkeypatch.setattr(intent_profiles.celery_app, "send_task", lambda name: calls.append(name))
    r = await client.post("/intent/collectors/run")
    assert r.status_code == 200
    assert "intent.collect_grants_gov" in r.json()["queued"]
    assert "intent.collect_dev_roles" in calls and len(calls) == 6


async def test_recompute_and_promote_flow(client, db_session):
    org, sig, profile = await _seed(db_session)

    rc = await client.post("/intent/recompute")
    assert rc.status_code == 200 and rc.json()["orgs"] == 1

    pr = await client.post("/intent/promote")
    assert pr.status_code == 200
    body = pr.json()
    assert body["promoted"] == 1 and body["draft_campaign_id"] is not None

    # A PREVIEWING campaign (awaiting approval) + a PENDING lead now exist; nothing sent.
    camp = await db_session.scalar(select(Campaign).where(Campaign.id == body["draft_campaign_id"]))
    assert camp.status is CampaignStatus.PREVIEWING
    lead = await db_session.scalar(select(Lead).where(Lead.campaign_id == camp.id))
    assert lead.send_status is SendStatus.PENDING
    assert sig.summary.split(" — ")[0] in lead.composed_body

    # Status now reports the draft.
    st = (await client.get("/intent/status")).json()
    assert st["draft_campaign_id"] == str(camp.id) and st["draft_pending_leads"] == 1
