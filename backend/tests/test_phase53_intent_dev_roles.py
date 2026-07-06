"""Phase 6 Track 1 — Adzuna dev-role collector → Tier-1 dev_role_posted.

Covers the employer-name matcher, the Adzuna client parser, and the collector
end-to-end with Adzuna mocked: matched employer → signal, unmatched skipped,
title post-filter, idempotency, and the no-key / no-org no-ops.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.models import (
    IntentSignalSource, IntentSignalType, Org, OrgSizeBand, Signal,
)
from app.services.funding_sources import adzuna
from app.services.intent import collect_dev_roles, matching

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 6, 22, tzinfo=timezone.utc)


# ---- matcher + client parser ----------------------------------------------

async def test_name_keys_and_match_employer(db_session):
    org = Org(name="Brooklyn Community Foundation", ein="111111111", state="NY")
    keys = matching.name_keys("The Brooklyn Community Foundation, Inc.")
    assert "brooklyn community foundation" in keys   # drops "the" + "inc"
    index = matching.index_orgs_by_name([org])
    assert matching.match_employer("The Brooklyn Community Foundation Inc", index) is org
    assert matching.match_employer("Unrelated Org", index) is None


async def test_adzuna_map_job_and_parse():
    assert adzuna._parse_created("2026-06-10T14:03:22Z") == datetime(2026, 6, 10, 14, 3, 22, tzinfo=timezone.utc)
    assert adzuna._parse_created("") is None
    job = adzuna._map_job({
        "id": 777, "title": "  Development Director  ",
        "company": {"display_name": "Brooklyn Community Foundation"},
        "location": {"display_name": "Brooklyn, NY", "area": ["US", "New York"]},
        "created": "2026-06-10T00:00:00Z", "redirect_url": "https://adzuna/jobs/777",
    })
    assert job["id"] == "777" and job["title"] == "Development Director"
    assert job["company"] == "Brooklyn Community Foundation"
    assert job["created"] == datetime(2026, 6, 10, tzinfo=timezone.utc)
    assert adzuna._map_job({"title": "no id"}) is None


# ---- collector -------------------------------------------------------------

async def _org(db_session, *, name, ein):
    o = Org(name=name, ein=ein.replace("-", ""), state="NY",
            ntee_code="T31", size_band=OrgSizeBand.MID)
    db_session.add(o)
    await db_session.flush()
    return o


def _job(jid, title, company):
    return {"id": str(jid), "title": title, "company": company,
            "location": "NY", "area": ["US", "New York"],
            "created": datetime(2026, 6, 20, tzinfo=timezone.utc),
            "redirect_url": f"https://adzuna/jobs/{jid}"}


async def test_collect_dev_roles_matches_emits_and_dedupes(db_session, monkeypatch):
    org = await _org(db_session, name="Brooklyn Community Foundation", ein="11-1111111")
    await db_session.commit()

    async def fake_search(phrase, **kw):
        # One matching employer, one unmatched, one off-title (filtered out).
        return [
            _job(1, "Development Director", "The Brooklyn Community Foundation, Inc."),
            _job(2, "Grant Writer", "Some Other Charity"),
            _job(3, "Software Engineer", "Brooklyn Community Foundation"),
        ]
    monkeypatch.setattr(collect_dev_roles.adzuna, "search_jobs", fake_search)

    counts = await collect_dev_roles.collect_dev_roles(
        db_session, titles=("development director",), now=NOW)
    assert counts["new"] == 1 and counts["matched"] == 1 and counts["unmatched"] == 1

    sig = await db_session.scalar(select(Signal).where(Signal.org_id == org.id))
    assert sig.signal_type is IntentSignalType.DEV_ROLE_POSTED
    assert sig.source is IntentSignalSource.JOBS
    assert sig.dedupe_key == f"adzuna:devrole:1:{org.id}"
    assert "Development Director" in sig.summary
    assert sig.evidence_url == "https://adzuna/jobs/1"
    assert sig.event_date == datetime(2026, 6, 20, tzinfo=timezone.utc)

    # Re-run dedups.
    again = await collect_dev_roles.collect_dev_roles(
        db_session, titles=("development director",), now=NOW)
    assert again["new"] == 0 and again["deduped"] == 1
    assert await db_session.scalar(select(func.count()).select_from(Signal)) == 1


async def test_collect_dev_roles_no_orgs_skips_search(db_session, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("should not search without candidate orgs")
    monkeypatch.setattr(collect_dev_roles.adzuna, "search_jobs", boom)
    empty = await collect_dev_roles.collect_dev_roles(db_session, now=NOW)
    assert empty["new"] == 0 and empty["postings"] == 0


async def test_collect_dev_roles_no_key_noop(db_session, monkeypatch):
    # With no Adzuna key the client returns [] regardless of environment.
    from app.config import settings
    monkeypatch.setattr(settings, "ADZUNA_APP_ID", "")
    monkeypatch.setattr(settings, "ADZUNA_APP_KEY", "")
    await _org(db_session, name="X Foundation", ein="22-2222222")
    await db_session.commit()
    real = await collect_dev_roles.collect_dev_roles(db_session, titles=("grant writer",), now=NOW)
    assert real["postings"] == 0 and real["new"] == 0
    assert await db_session.scalar(select(func.count()).select_from(Signal)) == 0
