"""Per-lead recipient editing + intent-lead "find contact".

PATCH /campaigns/{cid}/leads/{lid} now accepts `email` (validate / canonicalize
/ 409-when-sent); POST .../find-contact resolves a recipient for an
intent-sourced lead (optional website hint).
"""
from __future__ import annotations

from datetime import time

import pytest

from app.models import Campaign, CampaignStatus, ComposeStatus, Lead, Org, SendStatus
from app.routers import campaigns as campaigns_router
from app.services.funding_sources.enrichment import ContactResult
from app.services.intent import enrich as enrich_mod

pytestmark = pytest.mark.asyncio


async def _campaign(db_session):
    c = Campaign(
        name="C", goal="g", tone="warm", sender_name="Op", sender_email="ops@org.com",
        schedule_time_start=time(9, 0), schedule_time_end=time(17, 0),
        status=CampaignStatus.PREVIEWING,
    )
    db_session.add(c)
    await db_session.flush()
    return c


async def _lead(db_session, c, *, email=None, sent=False, intent_org_id=None):
    rd = {"from_intent_engine": True, "intent_org_id": str(intent_org_id)} if intent_org_id else None
    lead = Lead(
        campaign_id=c.id, email=email, company="Org",
        compose_status=ComposeStatus.DONE, composed_subject="s", composed_body="Hi there,",
        send_status=SendStatus.SENT if sent else SendStatus.PENDING, research_data=rd,
    )
    db_session.add(lead)
    await db_session.flush()
    return lead


async def test_patch_sets_recipient(client, db_session):
    c = await _campaign(db_session)
    lead = await _lead(db_session, c)
    await db_session.commit()

    r = await client.patch(f"/campaigns/{c.id}/leads/{lead.id}", json={"email": "  Dev@Org.ORG "})
    assert r.status_code == 200
    await db_session.refresh(lead)
    assert lead.email == "dev@org.org"   # trimmed + lowercased


async def test_patch_rejects_invalid_recipient(client, db_session):
    c = await _campaign(db_session)
    lead = await _lead(db_session, c)
    await db_session.commit()
    r = await client.patch(f"/campaigns/{c.id}/leads/{lead.id}", json={"email": "nope"})
    assert r.status_code == 422


async def test_patch_recipient_blocked_after_sent(client, db_session):
    c = await _campaign(db_session)
    lead = await _lead(db_session, c, email="old@org.com", sent=True)
    await db_session.commit()
    r = await client.patch(f"/campaigns/{c.id}/leads/{lead.id}", json={"email": "new@org.com"})
    assert r.status_code == 409


async def test_find_contact_resolves_for_intent_lead(client, db_session, monkeypatch):
    c = await _campaign(db_session)
    org = Org(name="Helpful", ein="111111111", state="NY")
    db_session.add(org)
    await db_session.flush()
    lead = await _lead(db_session, c, intent_org_id=org.id)
    await db_session.commit()

    async def fake_resolve(o):
        assert o.website == "https://helpful.org"   # website hint applied to the org
        return ContactResult(status="resolved", email="dir@helpful.org",
                             first_name="Dana", via="hunter_domain_search")
    monkeypatch.setattr(enrich_mod, "resolve_contact", fake_resolve)

    r = await client.post(
        f"/campaigns/{c.id}/leads/{lead.id}/find-contact",
        json={"website": "https://helpful.org"},
    )
    assert r.status_code == 200 and r.json()["status"] == "resolved"
    await db_session.refresh(lead)
    assert lead.email == "dir@helpful.org" and lead.first_name == "Dana"
    await db_session.refresh(org)
    assert org.website == "https://helpful.org"


async def test_find_contact_rejects_non_intent_lead(client, db_session):
    c = await _campaign(db_session)
    lead = await _lead(db_session, c)   # no intent_org_id
    await db_session.commit()
    r = await client.post(f"/campaigns/{c.id}/leads/{lead.id}/find-contact", json={})
    assert r.status_code == 400
