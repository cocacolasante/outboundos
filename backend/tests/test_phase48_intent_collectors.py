"""Phase 2 — ProPublica 990 grant-revenue-delta collector.

Unit-tests the drop math + size banding, and integration-tests the collector
end-to-end with ProPublica mocked: it emits a Tier-2 rev_drop signal with the
contract fields, refreshes the org, and dedupes on re-run.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models import (
    IntentSignalSource, IntentSignalType, Lead, Org, OrgSizeBand, Signal,
)
from app.services.intent import collect_propublica, orgs

pytestmark = pytest.mark.asyncio


# ---- pure helpers ---------------------------------------------------------

async def test_size_band_for_revenue():
    f = orgs.size_band_for_revenue
    assert f(None) is None
    assert f(100_000) is OrgSizeBand.MICRO
    assert f(750_000) is OrgSizeBand.SMALL
    assert f(5_000_000) is OrgSizeBand.MID
    assert f(20_000_000) is OrgSizeBand.LARGE
    assert f(100_000_000) is OrgSizeBand.MAJOR


async def test_compute_rev_drop_detects_material_drop():
    filings = [
        {"year": 2023, "contributions": 700_000, "total_revenue": 900_000},
        {"year": 2022, "contributions": 1_100_000, "total_revenue": 1_200_000},
    ]
    d = collect_propublica.compute_rev_drop(filings)
    assert d is not None
    assert d["latest_year"] == 2023 and d["prior_year"] == 2022
    assert round(d["drop_fraction"], 2) == 0.36


async def test_compute_rev_drop_ignores_small_drop_growth_and_thin_history():
    # 5% drop — under the 20% threshold.
    assert collect_propublica.compute_rev_drop([
        {"year": 2023, "contributions": 950_000},
        {"year": 2022, "contributions": 1_000_000},
    ]) is None
    # Growth.
    assert collect_propublica.compute_rev_drop([
        {"year": 2023, "contributions": 1_200_000},
        {"year": 2022, "contributions": 1_000_000},
    ]) is None
    # Only one usable filing.
    assert collect_propublica.compute_rev_drop([
        {"year": 2023, "contributions": 500_000},
        {"year": 2022, "contributions": None},
    ]) is None


# ---- collector integration (ProPublica mocked) ----------------------------

_FIN = {
    "name": "Helpful Nonprofit Inc",
    "website": "https://helpful.org",
    "ntee_code": "B82",
    "state": "PA",
    "filings": [
        {"year": 2023, "contributions": 700_000, "total_revenue": 900_000},
        {"year": 2022, "contributions": 1_100_000, "total_revenue": 1_200_000},
    ],
}


async def _org(db_session, ein="12-3456789") -> Org:
    org = Org(name="Unknown org", ein=orgs.normalize_ein(ein))
    db_session.add(org)
    await db_session.flush()
    return org


async def test_collector_emits_rev_drop_signal_and_refreshes_org(db_session, monkeypatch):
    async def fake_fin(ein):
        return _FIN
    monkeypatch.setattr(collect_propublica.propublica, "fetch_financials", fake_fin)

    org = await _org(db_session)
    outcome = await collect_propublica.collect_for_org(db_session, org)
    await db_session.commit()
    assert outcome == "new"

    sig = await db_session.scalar(select(Signal).where(Signal.org_id == org.id))
    assert sig.signal_type is IntentSignalType.REV_DROP
    assert sig.source is IntentSignalSource.PROPUBLICA
    assert sig.dedupe_key == "propublica:rev_drop:123456789:2023"
    assert "fell 36%" in sig.summary
    assert sig.evidence_url.endswith("/organizations/123456789")
    assert sig.event_date is not None and float(sig.score) > 0

    # Org refreshed from the same call.
    await db_session.refresh(org)
    assert org.name == "Helpful Nonprofit Inc"
    assert org.ntee_code == "B82" and org.state == "PA"
    assert org.size_band is OrgSizeBand.SMALL   # latest total_revenue 900k
    assert org.annual_revenue == Decimal("900000")


async def test_collector_is_idempotent_on_rerun(db_session, monkeypatch):
    async def fake_fin(ein):
        return _FIN
    monkeypatch.setattr(collect_propublica.propublica, "fetch_financials", fake_fin)

    org = await _org(db_session, ein="98-7654321")
    assert await collect_propublica.collect_for_org(db_session, org) == "new"
    await db_session.commit()
    assert await collect_propublica.collect_for_org(db_session, org) == "deduped"
    await db_session.commit()

    n = await db_session.scalar(select(func.count()).select_from(Signal).where(Signal.org_id == org.id))
    assert n == 1


async def test_collector_no_signal_when_no_drop(db_session, monkeypatch):
    async def fake_fin(ein):
        return {**_FIN, "filings": [
            {"year": 2023, "contributions": 1_050_000, "total_revenue": 1_100_000},
            {"year": 2022, "contributions": 1_000_000, "total_revenue": 1_050_000},
        ]}
    monkeypatch.setattr(collect_propublica.propublica, "fetch_financials", fake_fin)

    org = await _org(db_session, ein="11-1111111")
    assert await collect_propublica.collect_for_org(db_session, org) == "no_signal"
    await db_session.commit()
    assert await db_session.scalar(select(func.count()).select_from(Signal)) == 0


async def test_seed_orgs_from_propublica_search(db_session, monkeypatch):
    # ProPublica search returns real-shaped org dicts; seed upserts them by EIN.
    async def fake_search(query, *, page=0):
        return [
            {"ein": "92-0155067", "name": "Alaska Community Foundation",
             "ntee_code": "T31", "state": "AK", "city": "Anchorage"},
            {"ein": "39-6038248", "name": "Madison Community Foundation",
             "ntee_code": "T31", "state": "WI", "city": "Madison"},
            {"ein": None, "name": "No EIN org"},  # skipped — no dedup anchor
        ]
    monkeypatch.setattr(orgs.propublica, "search_orgs", fake_search)

    result = await orgs.seed_orgs_from_propublica_search(db_session, "community foundation")
    assert result == {"created": 2, "found": 3}

    org = await db_session.scalar(select(Org).where(Org.ein == "920155067"))
    assert org is not None and org.name == "Alaska Community Foundation"
    assert org.ntee_code == "T31" and org.state == "AK"

    # Re-run is idempotent (upsert dedups by EIN).
    again = await orgs.seed_orgs_from_propublica_search(db_session, "community foundation")
    assert again["created"] == 0
    assert await db_session.scalar(select(func.count()).select_from(Org)) == 2


async def test_backfill_orgs_from_existing(db_session):
    # An EIN-bearing funding-discovered lead seeds an org.
    lead = Lead(
        campaign_id=None, email="dev@helpful.org", company="Helpful Nonprofit",
        company_website="https://helpful.org",
        research_data={"ein": "55-5555555", "ntee": "B82", "state": "PA"},
    )
    db_session.add(lead)
    await db_session.commit()

    result = await orgs.backfill_orgs_from_existing(db_session)
    assert result["created"] == 1
    org = await db_session.scalar(select(Org).where(Org.ein == "555555555"))
    assert org is not None
    assert org.source_lead_id == lead.id and org.ntee_code == "B82"

    # Re-run is idempotent (no duplicate org).
    again = await orgs.backfill_orgs_from_existing(db_session)
    assert again["created"] == 0
