"""Phase 45: contact-enrichment pipeline + deferred-enrichment retry.

Covers the domain-discovery → website-scrape → Hunter → role-priority
pipeline (enrichment.resolve_contact) and the daily retry worker that
promotes / backs off / exhausts queued orgs.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.models import (
    CrmActivity,
    CrmActivityType,
    FundingEnrichmentQueue,
    FundingEnrichmentStatus,
    Lead,
    ProspectSignal,
)
from app.services import hunter
from app.services.funding_sources import enrichment
from app.services.funding_sources.base import DiscoveredOrg
from app.services.funding_sources.enrichment import ContactResult
from app.workers import funding_signals

pytestmark = pytest.mark.asyncio


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _org(**kw) -> DiscoveredOrg:
    d = dict(
        signal_type="new_501c3", summary="New 501(c)(3): Helping Hands (PA)",
        dedup_key="new_501c3:99", org_name="Helping Hands", state="PA",
        ein="99", ntee_code="P20", website=None,
    )
    d.update(kw)
    return DiscoveredOrg(**d)


# ---------------------------------------------------------------------------
# Fake httpx client for the website scraper
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, text="", status=200, ctype="text/html"):
        self.text = text
        self.status_code = status
        self.headers = {"content-type": ctype}


class _FakeScrapeClient:
    def __init__(self, pages: dict[str, _Resp]):
        self.pages = pages
        self.calls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        self.calls.append(url)
        # Match by path suffix; default 404.
        for path, resp in self.pages.items():
            if url.endswith(path):
                return resp
        return _Resp(status=404)


# ---------------------------------------------------------------------------
# _discover_domain
# ---------------------------------------------------------------------------


async def test_discover_domain_returns_official(monkeypatch):
    monkeypatch.setattr(
        enrichment, "_web_lookup",
        AsyncMock(return_value={"domain": "https://www.helpinghands.org/"}),
    )
    domain = await enrichment._discover_domain(_org())
    assert domain == "helpinghands.org"


async def test_discover_domain_rejects_aggregators(monkeypatch):
    for bad in ("https://facebook.com/helpinghands",
                "linkedin.com/company/helping-hands",
                "guidestar.org/profile/99"):
        monkeypatch.setattr(
            enrichment, "_web_lookup", AsyncMock(return_value={"domain": bad}),
        )
        assert await enrichment._discover_domain(_org()) is None


# ---------------------------------------------------------------------------
# _scrape_contacts
# ---------------------------------------------------------------------------


async def test_scrape_extracts_on_domain_and_filters_off_domain(monkeypatch):
    html = (
        '<html><body>Reach our team at '
        '<a href="mailto:jane@helpinghands.org">Jane</a> or '
        'vendor@gmail.com (our web host).</body></html>'
    )
    fake = _FakeScrapeClient({"helpinghands.org": _Resp(html)})
    monkeypatch.setattr(enrichment.httpx, "AsyncClient", lambda *a, **k: fake)
    # Echo bare emails (skip the LLM pairing).
    monkeypatch.setattr(
        enrichment, "_pair_contacts_llm",
        AsyncMock(side_effect=lambda text, emails: [
            {"email": e, "first_name": None, "last_name": None, "title": None}
            for e in emails
        ]),
    )
    out = await enrichment._scrape_contacts("helpinghands.org")
    emails = {c["email"] for c in out}
    assert "jane@helpinghands.org" in emails
    assert "vendor@gmail.com" not in emails        # off-domain dropped
    assert all(c["via"] == "website" for c in out)


async def test_scrape_respects_page_cap(monkeypatch):
    fake = _FakeScrapeClient({"x": _Resp("<html>no emails</html>")})  # all 404/empty
    monkeypatch.setattr(enrichment.httpx, "AsyncClient", lambda *a, **k: fake)
    monkeypatch.setattr(enrichment.settings, "FUNDING_SCRAPE_MAX_PAGES", 2)
    await enrichment._scrape_contacts("helpinghands.org")
    assert len(fake.calls) == 2                    # homepage + 1 path only


async def test_scrape_pairs_name_and_title(monkeypatch):
    html = '<html>Executive Director Jane Doe — jane@helpinghands.org</html>'
    fake = _FakeScrapeClient({"helpinghands.org": _Resp(html)})
    monkeypatch.setattr(enrichment.httpx, "AsyncClient", lambda *a, **k: fake)
    monkeypatch.setattr(
        enrichment, "_pair_contacts_llm",
        AsyncMock(return_value=[{
            "email": "jane@helpinghands.org", "first_name": "Jane",
            "last_name": "Doe", "title": "Executive Director",
        }]),
    )
    out = await enrichment._scrape_contacts("helpinghands.org")
    assert out[0]["first_name"] == "Jane"
    assert out[0]["title"] == "Executive Director"


# ---------------------------------------------------------------------------
# hunter.domain_search permissive fallback
# ---------------------------------------------------------------------------


async def test_domain_search_no_key_returns_empty(monkeypatch):
    monkeypatch.setattr(hunter.settings, "HUNTER_API_KEY", "")
    assert await hunter.domain_search("helpinghands.org") == []


async def test_domain_search_respects_free_plan_limit(monkeypatch):
    # The free Hunter plan 400s on limit > 10 — domain_search must send the
    # configured (capped) limit, not a hardcoded 25.
    captured = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"data": {"emails": []}}

    class _FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None):
            captured["params"] = params
            return _Resp()

    monkeypatch.setattr(hunter.settings, "HUNTER_API_KEY", "k")
    monkeypatch.setattr(hunter.httpx, "AsyncClient", lambda *a, **k: _FakeClient())
    await hunter.domain_search("helpinghands.org")
    assert captured["params"]["limit"] == hunter.settings.HUNTER_DOMAIN_SEARCH_LIMIT
    assert captured["params"]["limit"] <= 10


# ---------------------------------------------------------------------------
# resolve_contact statuses + role priority
# ---------------------------------------------------------------------------


async def test_resolve_no_domain(monkeypatch):
    monkeypatch.setattr(enrichment, "_discover_domain", AsyncMock(return_value=None))
    cr = await enrichment.resolve_contact(_org(website=None))
    assert cr.status == "no_domain"


async def test_resolve_no_contact_when_site_has_no_email(monkeypatch):
    monkeypatch.setattr(enrichment, "_scrape_contacts", AsyncMock(return_value=[]))
    monkeypatch.setattr(hunter, "domain_search", AsyncMock(return_value=[]))
    monkeypatch.setattr(enrichment.propublica, "lookup_org", AsyncMock(return_value=None))
    cr = await enrichment.resolve_contact(_org(website="https://helpinghands.org"))
    assert cr.status == "no_contact"
    assert cr.domain == "helpinghands.org"


async def test_resolve_resolved_via_website(monkeypatch):
    monkeypatch.setattr(enrichment, "_scrape_contacts", AsyncMock(return_value=[
        {"email": "jane@helpinghands.org", "first_name": "Jane", "last_name": "Doe",
         "title": "Executive Director", "via": "website", "generic": False},
    ]))
    monkeypatch.setattr(hunter, "verify_email_hunter", AsyncMock(return_value={"deliverable": True}))
    cr = await enrichment.resolve_contact(_org(website="https://helpinghands.org"))
    assert cr.status == "resolved"
    assert cr.email == "jane@helpinghands.org"
    assert cr.via == "website"


async def test_resolve_role_priority_prefers_ed_over_generic(monkeypatch):
    monkeypatch.setattr(enrichment, "_scrape_contacts", AsyncMock(return_value=[
        {"email": "info@helpinghands.org", "first_name": None, "last_name": None,
         "title": None, "via": "website", "generic": True},
        {"email": "dana@helpinghands.org", "first_name": "Dana", "last_name": "Reed",
         "title": "Executive Director", "via": "website", "generic": False},
        {"email": "deb@helpinghands.org", "first_name": "Deb", "last_name": "Lee",
         "title": "Development Director", "via": "website", "generic": False},
    ]))
    monkeypatch.setattr(hunter, "verify_email_hunter", AsyncMock(return_value={"deliverable": True}))
    cr = await enrichment.resolve_contact(_org(website="https://helpinghands.org"))
    assert cr.email == "dana@helpinghands.org"     # ED beats Development + generic


async def test_resolve_generic_only_as_last_resort(monkeypatch):
    monkeypatch.setattr(enrichment, "_scrape_contacts", AsyncMock(return_value=[
        {"email": "info@helpinghands.org", "first_name": None, "last_name": None,
         "title": None, "via": "website", "generic": True},
    ]))
    monkeypatch.setattr(hunter, "verify_email_hunter", AsyncMock(return_value={"deliverable": True}))
    cr = await enrichment.resolve_contact(_org(website="https://helpinghands.org"))
    assert cr.status == "resolved"
    assert cr.email == "info@helpinghands.org"


async def test_resolve_skips_undeliverable_then_takes_next(monkeypatch):
    monkeypatch.setattr(enrichment, "_scrape_contacts", AsyncMock(return_value=[
        {"email": "dana@helpinghands.org", "first_name": "Dana", "last_name": "Reed",
         "title": "Executive Director", "via": "website", "generic": False},
        {"email": "info@helpinghands.org", "first_name": None, "last_name": None,
         "title": None, "via": "website", "generic": True},
    ]))

    async def _verify(email):
        return {"deliverable": email.startswith("info@")}   # ED bounces, info ok

    monkeypatch.setattr(hunter, "verify_email_hunter", _verify)
    cr = await enrichment.resolve_contact(_org(website="https://helpinghands.org"))
    assert cr.status == "resolved"
    assert cr.email == "info@helpinghands.org"


# ---------------------------------------------------------------------------
# Retry worker: promote / backoff / exhaust / direct-mail
# ---------------------------------------------------------------------------


async def _queue_row(db_session, *, attempts=1, dedup="new_501c3:99", mailing=None):
    row = FundingEnrichmentQueue(
        source="irs_bmf", ein="99", dedup_key=dedup, org_name="Helping Hands",
        state="PA", ntee_code="P20", website=None,
        payload={
            "signal_type": "new_501c3",
            "summary": "New 501(c)(3): Helping Hands (PA)",
            "detail": {"mailing_address": mailing} if mailing else {},
        },
        attempts=attempts, last_attempt_at=_now(),
        next_attempt_at=_now() - timedelta(days=1),   # due
        status=FundingEnrichmentStatus.PENDING,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


async def test_retry_promotes_resolved_with_original_dedup_key(db_session, monkeypatch):
    row = await _queue_row(db_session)
    monkeypatch.setattr(
        funding_signals.enrichment, "resolve_contact",
        AsyncMock(return_value=ContactResult(
            status="resolved", domain="helpinghands.org",
            email="dana@helpinghands.org", first_name="Dana", via="website",
        )),
    )
    counts = await funding_signals._retry_enrichment_async()
    assert counts["resolved"] == 1

    # Promoted to a ProspectSignal with the ORIGINAL dedup_key, no duplicate.
    n = await db_session.scalar(
        select(func.count()).select_from(ProspectSignal)
        .where(ProspectSignal.dedup_key == "new_501c3:99")
    )
    assert n == 1
    lead = await db_session.scalar(select(Lead).where(Lead.email == "dana@helpinghands.org"))
    assert lead is not None and lead.campaign_id is None
    await db_session.refresh(row)
    assert row.status == FundingEnrichmentStatus.RESOLVED


async def test_retry_backoff_advances_when_still_unresolved(db_session, monkeypatch):
    monkeypatch.setattr(funding_signals.settings, "FUNDING_ENRICHMENT_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(funding_signals.settings, "FUNDING_ENRICHMENT_RETRY_DAYS", [7, 30, 60])
    row = await _queue_row(db_session, attempts=1)
    monkeypatch.setattr(
        funding_signals.enrichment, "resolve_contact",
        AsyncMock(return_value=ContactResult(status="no_contact", domain="helpinghands.org")),
    )
    before = _now()
    counts = await funding_signals._retry_enrichment_async()
    assert counts["pending"] == 1

    await db_session.refresh(row)
    assert row.status == FundingEnrichmentStatus.PENDING
    assert row.attempts == 2
    # next_attempt advanced ~30 days (backoff index = attempts-1 = 1).
    assert row.next_attempt_at > before + timedelta(days=25)


async def test_retry_exhausts_after_max_no_directmail(db_session, monkeypatch):
    monkeypatch.setattr(funding_signals.settings, "FUNDING_ENRICHMENT_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(funding_signals.settings, "FUNDING_DIRECT_MAIL_FALLBACK", False)
    row = await _queue_row(db_session, attempts=2, mailing={"street": "1 Main", "city": "Erie"})
    monkeypatch.setattr(
        funding_signals.enrichment, "resolve_contact",
        AsyncMock(return_value=ContactResult(status="no_contact")),
    )
    counts = await funding_signals._retry_enrichment_async()
    assert counts["exhausted"] == 1

    await db_session.refresh(row)
    assert row.status == FundingEnrichmentStatus.EXHAUSTED
    # No direct-mail task created when the flag is off.
    assert (await db_session.execute(select(CrmActivity))).scalars().first() is None


async def test_retry_direct_mail_when_flag_on(db_session, monkeypatch):
    monkeypatch.setattr(funding_signals.settings, "FUNDING_ENRICHMENT_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(funding_signals.settings, "FUNDING_DIRECT_MAIL_FALLBACK", True)
    row = await _queue_row(
        db_session, attempts=2,
        mailing={"street": "1 Main St", "city": "Erie", "state": "PA", "zip": "16501"},
    )
    monkeypatch.setattr(
        funding_signals.enrichment, "resolve_contact",
        AsyncMock(return_value=ContactResult(status="no_contact")),
    )
    counts = await funding_signals._retry_enrichment_async()
    assert counts["mailed"] == 1

    await db_session.refresh(row)
    assert row.status == FundingEnrichmentStatus.MAILED
    task = await db_session.scalar(
        select(CrmActivity).where(CrmActivity.activity_type == CrmActivityType.TASK)
    )
    assert task is not None
    assert task.subject.startswith("Direct mail —")
    assert "Erie" in task.body


# ---------------------------------------------------------------------------
# Per-run enrichment cap (bounds a big USASpending window)
# ---------------------------------------------------------------------------


async def test_stage_all_caps_enrichment_per_run(db_session, monkeypatch):
    monkeypatch.setattr(funding_signals.settings, "FUNDING_DISCOVERY_MAX_PER_RUN", 2)
    calls = {"n": 0}

    async def _resolve(org):
        calls["n"] += 1
        return ContactResult(
            status="resolved", domain="helpinghands.org",
            email="ed@helpinghands.org", first_name="Dana", via="website",
        )

    monkeypatch.setattr(funding_signals.enrichment, "resolve_contact", _resolve)
    monkeypatch.setattr(funding_signals, "_funding_redis", lambda: None)  # no stop flag

    orgs = [_org(dedup_key=f"grant_awarded:CAP{i}", signal_type="grant_awarded")
            for i in range(5)]
    out = await funding_signals._stage_all(db_session, "usaspending", orgs)

    assert out["capped"] is True
    assert out["staged"] == 2            # only 2 of 5 enriched this run
    assert calls["n"] == 2               # resolve_contact NOT called for the rest
