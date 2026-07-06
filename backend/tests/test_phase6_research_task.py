"""Phase 6: research_lead task (services mocked, real DB)."""
import uuid
from datetime import time
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models import Campaign, Lead, ResearchMode, ResearchStatus
from app.workers.research import _assess_quality, research_lead_async


async def _make_campaign(db_session, *, mode: ResearchMode = ResearchMode.FAST) -> Campaign:
    c = Campaign(
        name="Test",
        goal="Test",
        tone="Friendly",
        sender_name="A",
        sender_email="a@x.com",
        research_mode=mode,
        sample_count=1,
        schedule_days=[0, 1, 2, 3, 4],
        schedule_time_start=time(9, 0),
        schedule_time_end=time(17, 0),
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    return c


async def _make_lead(db_session, campaign: Campaign, **overrides) -> Lead:
    defaults = {
        "email": "lead@example.com",
        "first_name": "Jane",
        "last_name": "Doe",
        "company": "Acme",
        "job_title": "CEO",
    }
    defaults.update(overrides)
    l = Lead(campaign_id=campaign.id, **defaults)
    db_session.add(l)
    await db_session.commit()
    await db_session.refresh(l)
    return l


# ---------- quality assessment ----------


def test_assess_quality_low_with_no_signals():
    assert _assess_quality({}, {}) == "low"


def test_assess_quality_partial_with_one_signal():
    assert _assess_quality({"person_news": ["x"]}, {}) == "partial"
    assert _assess_quality({"company_description": "x"}, {}) == "partial"
    assert _assess_quality({}, {"linkedin_url": "x"}) == "partial"


def test_assess_quality_rich_with_multiple_signals():
    assert _assess_quality({"person_news": ["x"], "company_description": "y"}, {}) == "rich"
    assert _assess_quality({"person_news": ["x"]}, {"linkedin_url": "y"}) == "rich"
    assert _assess_quality(
        {"person_news": ["x"], "company_description": "y"}, {"linkedin_url": "z"}
    ) == "rich"


# ---------- happy path ----------


async def test_fast_mode_runs_merged_research_and_hunter_only(db_session):
    campaign = await _make_campaign(db_session, mode=ResearchMode.FAST)
    lead = await _make_lead(db_session, campaign)

    # One merged research call returns person + company fields (no site_scraper).
    web_mock = AsyncMock(return_value={
        "person_news": ["raised Series B"], "company_news": [],
        "company_description": "AI for SMB", "recent_updates": ["new feature"],
        "industry": "SaaS", "size_hint": "startup", "found": True,
    })
    hunter_mock = AsyncMock(return_value={"deliverable": True, "score": 90})
    apollo_mock = AsyncMock(return_value={"linkedin_url": "should-not-be-called"})

    with patch("app.workers.research.web_research.research_person_web", web_mock), \
         patch("app.workers.research.hunter.verify_email_hunter", hunter_mock), \
         patch("app.workers.research.apollo.enrich_lead_apollo", apollo_mock), \
         patch("app.workers.research.compose_lead.delay") as enqueue:

        result = await research_lead_async(str(lead.id))

    # Apollo not invoked in FAST mode.
    apollo_mock.assert_not_called()
    enqueue.assert_called_once_with(str(lead.id))

    assert result["status"] == "done"
    assert result["quality"] == "rich"  # person_news + company description → 2 signals

    refreshed = await db_session.scalar(select(Lead).where(Lead.id == lead.id))
    await db_session.refresh(refreshed)
    assert refreshed.research_status == ResearchStatus.DONE
    rd = refreshed.research_data
    assert rd["quality"] == "rich"
    assert rd["person_news"] == ["raised Series B"]
    assert rd["company_description"] == "AI for SMB"
    assert rd["recent_updates"] == ["new feature"]
    assert rd["industry"] == "SaaS"
    assert rd["email_deliverable"] is True


async def test_deep_mode_invokes_apollo_when_key_set(db_session, monkeypatch):
    monkeypatch.setattr("app.workers.research.settings.APOLLO_API_KEY", "set-key")
    campaign = await _make_campaign(db_session, mode=ResearchMode.DEEP)
    lead = await _make_lead(db_session, campaign)

    apollo_mock = AsyncMock(return_value={
        "linkedin_url": "https://li/x",
        "linkedin_headline": "CEO at Acme",
        "job_title": "Chief Executive",
        "seniority": "founder",
        "company_industry": "SaaS",
    })

    with patch("app.workers.research.web_research.research_person_web", AsyncMock(return_value={"person_news": [], "company_news": [], "company_description": "", "found": False})), \
         patch("app.workers.research.hunter.verify_email_hunter", AsyncMock(return_value={"deliverable": True, "score": 0})), \
         patch("app.workers.research.apollo.enrich_lead_apollo", apollo_mock), \
         patch("app.workers.research.compose_lead.delay"):
        result = await research_lead_async(str(lead.id))

    apollo_mock.assert_called_once()
    assert result["status"] == "done"
    refreshed = await db_session.scalar(select(Lead).where(Lead.id == lead.id))
    await db_session.refresh(refreshed)
    rd = refreshed.research_data
    assert rd["linkedin_headline"] == "CEO at Acme"
    assert rd["job_title"] == "Chief Executive"
    assert rd["seniority"] == "founder"
    assert rd["industry"] == "SaaS"
    # Only apollo signal → partial.
    assert rd["quality"] == "partial"


async def test_deep_mode_skips_apollo_when_no_key(db_session, monkeypatch):
    monkeypatch.setattr("app.workers.research.settings.APOLLO_API_KEY", "")
    campaign = await _make_campaign(db_session, mode=ResearchMode.DEEP)
    lead = await _make_lead(db_session, campaign)

    apollo_mock = AsyncMock(return_value={"never": "called"})
    with patch("app.workers.research.web_research.research_person_web", AsyncMock(return_value={"person_news": [], "company_news": [], "company_description": "", "found": False})), \
         patch("app.workers.research.hunter.verify_email_hunter", AsyncMock(return_value={"deliverable": True, "score": 0})), \
         patch("app.workers.research.apollo.enrich_lead_apollo", apollo_mock), \
         patch("app.workers.research.compose_lead.delay"):
        await research_lead_async(str(lead.id))

    apollo_mock.assert_not_called()


async def test_status_transitions_to_running_then_done(db_session):
    campaign = await _make_campaign(db_session)
    lead = await _make_lead(db_session, campaign)
    assert lead.research_status == ResearchStatus.PENDING

    with patch("app.workers.research.web_research.research_person_web", AsyncMock(return_value={"person_news": [], "company_news": [], "company_description": "", "found": False})), \
         patch("app.workers.research.hunter.verify_email_hunter", AsyncMock(return_value={"deliverable": True, "score": 0})), \
         patch("app.workers.research.compose_lead.delay"):
        await research_lead_async(str(lead.id))

    refreshed = await db_session.scalar(select(Lead).where(Lead.id == lead.id))
    await db_session.refresh(refreshed)
    assert refreshed.research_status == ResearchStatus.DONE


async def test_web_research_failure_still_completes(db_session):
    """If the (now single) web-research call blows up, the task still finishes
    with empty research + hunter's deliverability, and compose is enqueued."""
    campaign = await _make_campaign(db_session)
    lead = await _make_lead(db_session, campaign)

    with patch("app.workers.research.web_research.research_person_web", AsyncMock(side_effect=RuntimeError("API down"))), \
         patch("app.workers.research.hunter.verify_email_hunter", AsyncMock(return_value={"deliverable": True, "score": 0})), \
         patch("app.workers.research.compose_lead.delay") as enqueue:
        result = await research_lead_async(str(lead.id))

    assert result["status"] == "done"
    enqueue.assert_called_once_with(str(lead.id))
    refreshed = await db_session.scalar(select(Lead).where(Lead.id == lead.id))
    await db_session.refresh(refreshed)
    assert refreshed.research_status == ResearchStatus.DONE
    # web failed → empty research, but hunter still ran and the lead completes.
    assert refreshed.research_data["company_description"] == ""
    assert refreshed.research_data["person_news"] == []
    assert refreshed.research_data["email_deliverable"] is True
    assert refreshed.research_data["quality"] == "low"


async def test_none_mode_skips_all_research_but_enqueues_compose(db_session):
    """'No research' mode makes zero external calls yet still composes."""
    campaign = await _make_campaign(db_session, mode=ResearchMode.NONE)
    lead = await _make_lead(db_session, campaign)

    web_mock = AsyncMock(return_value={})
    hunter_mock = AsyncMock(return_value={"deliverable": True, "score": 0})
    apollo_mock = AsyncMock(return_value={})

    with patch("app.workers.research.web_research.research_person_web", web_mock), \
         patch("app.workers.research.hunter.verify_email_hunter", hunter_mock), \
         patch("app.workers.research.apollo.enrich_lead_apollo", apollo_mock), \
         patch("app.workers.research.compose_lead.delay") as enqueue:

        result = await research_lead_async(str(lead.id))

    # No research provider is touched at all.
    web_mock.assert_not_called()
    hunter_mock.assert_not_called()
    apollo_mock.assert_not_called()
    # Compose still runs (it uses the generic name+company-only prompt).
    enqueue.assert_called_once_with(str(lead.id))

    assert result["status"] == "done"
    assert result["quality"] == "low"

    refreshed = await db_session.scalar(select(Lead).where(Lead.id == lead.id))
    await db_session.refresh(refreshed)
    assert refreshed.research_status == ResearchStatus.DONE
    assert refreshed.research_data["quality"] == "low"
    assert refreshed.research_data["skipped"] is True


async def test_fresh_cache_hit_skips_api_calls_and_reuses_research(db_session):
    """A fresh row in research_cache for the lead's email makes the worker
    reuse the cached research_data, skipping every external API call."""
    from app.services import research_cache as rc_mod
    campaign = await _make_campaign(db_session, mode=ResearchMode.FAST)
    lead = await _make_lead(db_session, campaign, email="hit@example.com")
    await rc_mod.upsert(db_session, "hit@example.com", {
        "quality": "rich", "person_news": ["raised B"],
        "company_description": "AI for SMB",
    })
    await db_session.commit()

    web_mock = AsyncMock(return_value={})       # MUST NOT be called
    hunter_mock = AsyncMock(return_value={})    # MUST NOT be called
    apollo_mock = AsyncMock(return_value={})    # MUST NOT be called

    with patch("app.workers.research.web_research.research_person_web", web_mock), \
         patch("app.workers.research.hunter.verify_email_hunter", hunter_mock), \
         patch("app.workers.research.apollo.enrich_lead_apollo", apollo_mock), \
         patch("app.workers.research.compose_lead.delay") as enqueue:
        result = await research_lead_async(str(lead.id))

    web_mock.assert_not_called()
    hunter_mock.assert_not_called()
    apollo_mock.assert_not_called()
    enqueue.assert_called_once_with(str(lead.id))
    assert result["status"] == "done"
    assert result["quality"] == "rich"

    refreshed = await db_session.scalar(select(Lead).where(Lead.id == lead.id))
    await db_session.refresh(refreshed)
    assert refreshed.research_status == ResearchStatus.DONE
    assert refreshed.research_data["person_news"] == ["raised B"]
    assert refreshed.research_data.get("from_cache") is True


async def test_cache_miss_calls_api_and_upserts_cache(db_session):
    """A cache miss runs the normal pipeline AND warms the cache so a
    second campaign for the same email reuses the result."""
    from app.services import research_cache as rc_mod
    campaign = await _make_campaign(db_session, mode=ResearchMode.FAST)
    lead = await _make_lead(db_session, campaign, email="miss@example.com")

    web_mock = AsyncMock(return_value={
        "person_news": ["news"], "company_news": [],
        "company_description": "ACME inc", "recent_updates": [],
        "industry": "SaaS", "size_hint": "growth", "found": True,
    })
    with patch("app.workers.research.web_research.research_person_web", web_mock), \
         patch("app.workers.research.hunter.verify_email_hunter",
               AsyncMock(return_value={"deliverable": True, "score": 0})), \
         patch("app.workers.research.compose_lead.delay"):
        await research_lead_async(str(lead.id))

    web_mock.assert_called_once()  # cache MISS → api called
    cached = await rc_mod.lookup(db_session, "miss@example.com")
    assert cached is not None
    assert cached["company_description"] == "ACME inc"


async def test_template_mode_skips_all_research_but_enqueues_compose(db_session):
    """'Template' mode (no AI) also makes zero research calls; compose then
    renders the campaign template instead of calling Anthropic."""
    campaign = await _make_campaign(db_session, mode=ResearchMode.TEMPLATE)
    lead = await _make_lead(db_session, campaign)

    web_mock = AsyncMock(return_value={})
    hunter_mock = AsyncMock(return_value={"deliverable": True, "score": 0})
    apollo_mock = AsyncMock(return_value={})

    with patch("app.workers.research.web_research.research_person_web", web_mock), \
         patch("app.workers.research.hunter.verify_email_hunter", hunter_mock), \
         patch("app.workers.research.apollo.enrich_lead_apollo", apollo_mock), \
         patch("app.workers.research.compose_lead.delay") as enqueue:

        result = await research_lead_async(str(lead.id))

    web_mock.assert_not_called()
    hunter_mock.assert_not_called()
    apollo_mock.assert_not_called()
    enqueue.assert_called_once_with(str(lead.id))
    assert result["status"] == "done"

    refreshed = await db_session.scalar(select(Lead).where(Lead.id == lead.id))
    await db_session.refresh(refreshed)
    assert refreshed.research_status == ResearchStatus.DONE


async def test_missing_lead_returns_not_found(db_session):
    with patch("app.workers.research.compose_lead.delay") as enqueue:
        result = await research_lead_async(str(uuid.uuid4()))
    assert result == {"status": "not_found"}
    enqueue.assert_not_called()


async def test_email_deliverable_flag_from_hunter(db_session):
    campaign = await _make_campaign(db_session)
    lead = await _make_lead(db_session, campaign)

    with patch("app.workers.research.web_research.research_person_web", AsyncMock(return_value={"person_news": [], "company_news": [], "company_description": "", "found": False})), \
         patch("app.workers.research.hunter.verify_email_hunter", AsyncMock(return_value={"deliverable": False, "score": 5})), \
         patch("app.workers.research.compose_lead.delay"):
        await research_lead_async(str(lead.id))

    refreshed = await db_session.scalar(select(Lead).where(Lead.id == lead.id))
    await db_session.refresh(refreshed)
    assert refreshed.research_data["email_deliverable"] is False


# ---------- cost-cut: suppression pre-scrub (#2) ----------


async def test_suppressed_lead_skips_research_and_compose(db_session):
    """A lead whose email is on the suppression list must spend ZERO Anthropic
    tokens: no web research, no compose enqueue.  It's marked terminal
    (research+compose DONE, send SUPPRESSED) so progress counters complete."""
    from app.models import ComposeStatus, SendStatus, Suppression, SuppressionReason

    campaign = await _make_campaign(db_session, mode=ResearchMode.FAST)
    lead = await _make_lead(db_session, campaign, email="bounced@acme.com")
    db_session.add(Suppression(email="bounced@acme.com", reason=SuppressionReason.HARD_BOUNCE))
    await db_session.commit()

    web_mock = AsyncMock(return_value={"person_news": [], "found": False})
    with patch("app.workers.research.web_research.research_person_web", web_mock), \
         patch("app.workers.research.hunter.verify_email_hunter", AsyncMock()) as hunter_mock, \
         patch("app.workers.research.compose_lead.delay") as enqueue:
        result = await research_lead_async(str(lead.id))

    assert result["status"] == "skipped_suppressed"
    web_mock.assert_not_called()      # no web-search tokens spent
    hunter_mock.assert_not_called()
    enqueue.assert_not_called()       # no compose (Sonnet) tokens spent

    refreshed = await db_session.scalar(select(Lead).where(Lead.id == lead.id))
    await db_session.refresh(refreshed)
    assert refreshed.research_status == ResearchStatus.DONE
    assert refreshed.compose_status == ComposeStatus.DONE
    assert refreshed.send_status == SendStatus.SUPPRESSED
    assert refreshed.research_data["suppressed"] is True


# ---------- cost-cut: company-level dedup cache (#3) ----------


def test_company_domain_prefers_website_and_skips_free_mail():
    from app.workers.research import _company_domain
    # Website wins, normalised (scheme/www/path stripped).
    assert _company_domain("https://www.acme.com/about", "x@gmail.com") == "acme.com"
    # No website → corporate email domain.
    assert _company_domain("", "jane@acme.com") == "acme.com"
    # No website → free-mail domain is NOT a company (would cross-contaminate).
    assert _company_domain("", "jane@gmail.com") == ""
    assert _company_domain("", "") == ""


async def test_company_research_cached_and_reused_for_same_domain(db_session):
    """The 2nd lead at the same company domain reuses the cached company
    research — the worker passes it to the person search so the company half
    of the search is skipped."""
    full = {
        "person_news": ["did a thing"], "company_news": ["launched X"],
        "company_description": "AI for SMB", "recent_updates": ["v2 shipped"],
        "industry": "SaaS", "size_hint": "startup", "found": True,
    }
    campaign = await _make_campaign(db_session, mode=ResearchMode.FAST)
    lead1 = await _make_lead(db_session, campaign, email="ceo@acme.com")
    lead2 = await _make_lead(db_session, campaign, email="vp@acme.com")

    web_mock = AsyncMock(return_value=full)
    with patch("app.workers.research.web_research.research_person_web", web_mock), \
         patch("app.workers.research.hunter.verify_email_hunter", AsyncMock(return_value={"deliverable": True, "score": 90})), \
         patch("app.workers.research.compose_lead.delay"):
        await research_lead_async(str(lead1.id))
        await research_lead_async(str(lead2.id))

    # First lead: no company cache yet.
    first_kwargs = web_mock.call_args_list[0].kwargs
    assert first_kwargs.get("cached_company") in (None, {})
    # Second lead (email miss, same domain): cached company fields passed in.
    second_kwargs = web_mock.call_args_list[1].kwargs
    cc = second_kwargs.get("cached_company")
    assert cc is not None
    assert cc.get("company_description") == "AI for SMB"
    assert cc.get("industry") == "SaaS"
    assert "person_news" not in cc          # only company-level fields cached
