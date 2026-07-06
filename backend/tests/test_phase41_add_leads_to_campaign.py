"""Phase 41 — bulk-add existing leads into a campaign.

Contracts:
- COPY semantics: a new Lead row enters the target campaign; the source
  (CRM lead or another campaign's lead) is untouched — campaign
  deletion must never destroy CRM history.
- Skips: suppressed emails, emails already in the target, unknown ids,
  in-batch duplicates.
- Pipeline: new rows are sequence-enrolled; research kicks immediately
  for non-draft campaigns and is deferred for drafts; COMPLETE → 409.
"""
from __future__ import annotations

import uuid
from datetime import time as dt_time
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from app.models import (
    Campaign,
    CampaignStatus,
    Lead,
    LeadSequenceState,
    Suppression,
    SuppressionReason,
)

pytestmark = pytest.mark.asyncio


async def _make_campaign(db_session, **kw) -> Campaign:
    defaults = dict(
        name="P41", goal="g", tone="t",
        sender_name="S", sender_email="s@x.com", sample_count=1,
        schedule_days=[], schedule_time_start=dt_time(0, 0),
        schedule_time_end=dt_time(23, 59), schedule_timezone="UTC",
        status=CampaignStatus.RUNNING,
    )
    defaults.update(kw)
    c = Campaign(**defaults)
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    return c


async def _crm_lead(db_session, email="crm@x.com", **kw) -> Lead:
    defaults = dict(
        campaign_id=None, email=email, first_name="Jane", last_name="Doe",
        company="Acme", job_title="CFO", notes="important CRM context",
        timezone="America/New_York",
    )
    defaults.update(kw)
    lead = Lead(**defaults)
    db_session.add(lead)
    await db_session.commit()
    await db_session.refresh(lead)
    return lead


async def test_add_copies_crm_lead_and_kicks_pipeline(client, db_session):
    campaign = await _make_campaign(db_session)
    src = await _crm_lead(db_session)

    with patch("app.routers.leads.ingest_tasks.run_campaign_research") as research:
        resp = await client.post(
            f"/campaigns/{campaign.id}/leads/add",
            json={"lead_ids": [str(src.id)]},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["added"] == 1
    assert body["research_started"] is True
    research.delay.assert_called_once_with(str(campaign.id))

    # A NEW row in the campaign with the contact fields copied…
    copy = await db_session.scalar(select(Lead).where(
        Lead.campaign_id == campaign.id,
    ))
    assert copy is not None
    assert copy.id != src.id
    assert copy.email == "crm@x.com"
    assert copy.first_name == "Jane" and copy.company == "Acme"
    assert copy.timezone == "America/New_York"
    assert copy.notes is None  # CRM notes stay on the source record

    # …enrolled in the campaign's sequence…
    state = await db_session.scalar(select(LeadSequenceState).where(
        LeadSequenceState.lead_id == copy.id,
    ))
    assert state is not None

    # …and the SOURCE is untouched (still campaign-less, history safe).
    await db_session.refresh(src)
    assert src.campaign_id is None
    assert src.notes == "important CRM context"


async def test_add_copies_lead_from_another_campaign(client, db_session):
    c1 = await _make_campaign(db_session, name="origin")
    c2 = await _make_campaign(db_session, name="target")
    src = await _crm_lead(db_session, email="cross@x.com", campaign_id=c1.id)

    with patch("app.routers.leads.ingest_tasks.run_campaign_research"):
        resp = await client.post(
            f"/campaigns/{c2.id}/leads/add",
            json={"lead_ids": [str(src.id)]},
        )
    assert resp.json()["added"] == 1

    # Source still belongs to its original campaign (copy, not move).
    await db_session.refresh(src)
    assert src.campaign_id == c1.id
    target_count = (await db_session.execute(
        select(func.count()).select_from(Lead).where(Lead.campaign_id == c2.id)
    )).scalar_one()
    assert target_count == 1


async def test_add_skips_duplicates_suppressed_and_missing(client, db_session):
    campaign = await _make_campaign(db_session)
    # Already in the target campaign.
    await _crm_lead(db_session, email="dupe@x.com", campaign_id=campaign.id)
    dupe_src = await _crm_lead(db_session, email="dupe@x.com")
    suppressed_src = await _crm_lead(db_session, email="nope@x.com")
    db_session.add(Suppression(email="nope@x.com", reason=SuppressionReason.UNSUBSCRIBED))
    fresh = await _crm_lead(db_session, email="fresh@x.com")
    # In-batch duplicate of fresh (second CRM row, same email).
    fresh_twin = await _crm_lead(db_session, email="fresh@x.com")
    await db_session.commit()

    with patch("app.routers.leads.ingest_tasks.run_campaign_research"):
        resp = await client.post(
            f"/campaigns/{campaign.id}/leads/add",
            json={"lead_ids": [
                str(dupe_src.id), str(suppressed_src.id),
                str(fresh.id), str(fresh_twin.id), str(uuid.uuid4()),
            ]},
        )
    body = resp.json()
    assert body["added"] == 1                # fresh only
    assert body["skipped_duplicate"] == 2    # dupe@ + the in-batch twin
    assert body["skipped_suppressed"] == 1
    assert body["skipped_missing"] == 1


async def test_add_to_draft_defers_research(client, db_session):
    campaign = await _make_campaign(db_session, status=CampaignStatus.DRAFT)
    src = await _crm_lead(db_session, email="draft@x.com")

    with patch("app.routers.leads.ingest_tasks.run_campaign_research") as research:
        resp = await client.post(
            f"/campaigns/{campaign.id}/leads/add",
            json={"lead_ids": [str(src.id)]},
        )
    body = resp.json()
    assert body["added"] == 1
    assert body["research_started"] is False
    research.delay.assert_not_called()
    # Row is staged + enrolled, ready for launch to pick up.
    copy = await db_session.scalar(select(Lead).where(
        Lead.campaign_id == campaign.id,
    ))
    assert copy is not None


async def test_add_to_complete_campaign_409(client, db_session):
    campaign = await _make_campaign(db_session, status=CampaignStatus.COMPLETE)
    src = await _crm_lead(db_session, email="late@x.com")
    resp = await client.post(
        f"/campaigns/{campaign.id}/leads/add",
        json={"lead_ids": [str(src.id)]},
    )
    assert resp.status_code == 409
    assert "complete" in resp.json()["detail"].lower()


async def test_add_nothing_new_is_clean_no_research(client, db_session):
    campaign = await _make_campaign(db_session)
    await _crm_lead(db_session, email="dupe2@x.com", campaign_id=campaign.id)
    src = await _crm_lead(db_session, email="dupe2@x.com")

    with patch("app.routers.leads.ingest_tasks.run_campaign_research") as research:
        resp = await client.post(
            f"/campaigns/{campaign.id}/leads/add",
            json={"lead_ids": [str(src.id)]},
        )
    body = resp.json()
    assert body["added"] == 0
    assert body["research_started"] is False
    research.delay.assert_not_called()
