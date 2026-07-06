"""Phase 6 follow-up — contact enrichment for promoted intent drafts.

enrich_draft_lead resolves a recipient for a draft lead (resolve_contact
mocked), sets email + name, re-renders the draft, and no-ops on
already-has-email / no-contact / missing lead.
"""
from __future__ import annotations

from datetime import time

import pytest
from sqlalchemy import select

from app.models import Campaign, CampaignStatus, ComposeStatus, Lead, Org, ResearchMode
from app.services.funding_sources.enrichment import ContactResult
from app.services.intent import enrich

pytestmark = pytest.mark.asyncio


async def _draft(db_session, *, email=None):
    org = Org(name="Helpful Nonprofit", ein="111111111", state="NY",
              website="https://helpful.org", ntee_code="T31")
    db_session.add(org)
    await db_session.flush()
    camp = Campaign(
        name="Intent drafts — GrantMind Pro", goal="g", tone="warm",
        sender_name="Operator", sender_email="ops@org.com",
        schedule_time_start=time(9, 0), schedule_time_end=time(17, 0),
        status=CampaignStatus.DRAFT, research_mode=ResearchMode.TEMPLATE,
        template_subject="A note for {{company|your team}}",
        template_body="Hi {{first_name|there}},\n\n{{intent_why_now}}\n\nBest,\n{{sender_name}}",
    )
    db_session.add(camp)
    await db_session.flush()
    lead = Lead(
        campaign_id=camp.id, email=email, company=org.name,
        raw_csv_row={"company": org.name, "intent_why_now": "Posted a Development Director role"},
        research_data={"from_intent_engine": True, "intent_org_id": str(org.id)},
        composed_subject="A note for Helpful Nonprofit",
        composed_body="Hi there,\n\nPosted a Development Director role\n\nBest,\nOperator",
        compose_status=ComposeStatus.DONE,
    )
    db_session.add(lead)
    await db_session.flush()
    return org, camp, lead


async def test_enrich_resolves_sets_recipient_and_rerenders(db_session, monkeypatch):
    org, camp, lead = await _draft(db_session)
    await db_session.commit()

    async def fake_resolve(o):
        return ContactResult(status="resolved", domain="helpful.org",
                             email="Brittany@Helpful.org", first_name="Brittany",
                             last_name="Shutz", via="website")
    monkeypatch.setattr(enrich, "resolve_contact", fake_resolve)

    res = await enrich.enrich_draft_lead(db_session, lead.id)
    assert res["status"] == "resolved" and res["email"] == "brittany@helpful.org"

    await db_session.refresh(lead)
    assert lead.email == "brittany@helpful.org"      # canonicalized
    assert lead.first_name == "Brittany"
    assert "Hi Brittany," in lead.composed_body       # re-rendered with the name


async def test_enrich_skips_when_already_has_email(db_session, monkeypatch):
    org, camp, lead = await _draft(db_session, email="already@there.com")
    await db_session.commit()

    async def boom(o):
        raise AssertionError("should not resolve when an email is already set")
    monkeypatch.setattr(enrich, "resolve_contact", boom)

    res = await enrich.enrich_draft_lead(db_session, lead.id)
    assert res["status"] == "already_has_email"


async def test_enrich_noop_when_no_contact(db_session, monkeypatch):
    org, camp, lead = await _draft(db_session)
    await db_session.commit()

    async def fake_resolve(o):
        return ContactResult(status="no_contact", domain="helpful.org")
    monkeypatch.setattr(enrich, "resolve_contact", fake_resolve)

    res = await enrich.enrich_draft_lead(db_session, lead.id)
    assert res["status"] == "no_contact"
    await db_session.refresh(lead)
    assert lead.email is None       # left recipient-less; promotion still stands


async def test_enrich_sanitizes_scrape_artifact_email(db_session, monkeypatch):
    org, camp, lead = await _draft(db_session)
    await db_session.commit()

    async def fake_resolve(o):
        # URL-encoded space artifact from a scrape.
        return ContactResult(status="resolved", email="%20donorserv@tulsacf.org", via="website")
    monkeypatch.setattr(enrich, "resolve_contact", fake_resolve)

    res = await enrich.enrich_draft_lead(db_session, lead.id)
    assert res["status"] == "resolved"
    await db_session.refresh(lead)
    assert lead.email == "donorserv@tulsacf.org"   # %20 stripped


async def test_enrich_rejects_unsalvageable_email(db_session, monkeypatch):
    org, camp, lead = await _draft(db_session)
    await db_session.commit()

    async def fake_resolve(o):
        return ContactResult(status="resolved", email="not an email", via="website")
    monkeypatch.setattr(enrich, "resolve_contact", fake_resolve)

    res = await enrich.enrich_draft_lead(db_session, lead.id)
    assert res["status"] == "invalid_email"
    await db_session.refresh(lead)
    assert lead.email is None


async def test_enrich_missing_lead(db_session):
    import uuid
    res = await enrich.enrich_draft_lead(db_session, uuid.uuid4())
    assert res["status"] == "not_found"
