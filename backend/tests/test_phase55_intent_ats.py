"""Phase 6 Track 3 — public ATS board dev-role collector (Greenhouse/Lever/Ashby).

Covers the per-provider normalization (HTTP mocked) and the collector
end-to-end: only ATS-configured orgs polled, only dev/grant titles emitted,
idempotent.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.models import IntentSignalSource, IntentSignalType, Org, OrgSizeBand, Signal
from app.services.funding_sources import ats_boards
from app.services.intent import collect_ats

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 6, 22, tzinfo=timezone.utc)


# ---- client normalization --------------------------------------------------

async def test_fetch_jobs_normalizes_each_provider(monkeypatch):
    payloads = {
        "greenhouse": {"jobs": [{"id": 1, "title": "Development Director",
                                 "absolute_url": "https://gh/1", "updated_at": "2026-06-10T07:00:00-04:00"}]},
        "lever": [{"id": "abc", "text": "Grant Writer", "hostedUrl": "https://lv/abc",
                   "createdAt": 1769868943602}],
        "ashby": {"jobs": [{"id": "x1", "title": "Grants Manager", "jobUrl": "https://ash/x1",
                            "publishedAt": "2026-04-07T17:12:35.753+00:00"}]},
    }

    async def fake_get(url):
        if "greenhouse" in url: return payloads["greenhouse"]
        if "lever" in url: return payloads["lever"]
        return payloads["ashby"]
    monkeypatch.setattr(ats_boards, "_get", fake_get)

    gh = await ats_boards.fetch_jobs("greenhouse", "tok")
    assert gh[0]["title"] == "Development Director" and gh[0]["url"] == "https://gh/1"
    assert gh[0]["posted_at"].year == 2026
    lv = await ats_boards.fetch_jobs("lever", "tok")
    assert lv[0]["title"] == "Grant Writer" and lv[0]["posted_at"] is not None
    ash = await ats_boards.fetch_jobs("ashby", "tok")
    assert ash[0]["title"] == "Grants Manager" and ash[0]["url"] == "https://ash/x1"
    # Unknown provider / empty token.
    assert await ats_boards.fetch_jobs("workday", "tok") == []
    assert await ats_boards.fetch_jobs("greenhouse", "") == []


# ---- collector -------------------------------------------------------------

async def _org(db_session, *, name, ein, ats=None):
    o = Org(name=name, ein=ein.replace("-", ""), state="WI", ntee_code="T31",
            size_band=OrgSizeBand.MID)
    db_session.add(o)
    await db_session.flush()
    if ats:
        collect_ats.set_ats_board(o, *ats)
        await db_session.flush()
    return o


async def test_collect_ats_only_configured_orgs_and_filters_titles(db_session, monkeypatch):
    wired = await _org(db_session, name="Wired Org", ein="11-1111111", ats=("greenhouse", "wired"))
    await _org(db_session, name="Unwired Org", ein="22-2222222")   # no ATS config
    await db_session.commit()

    polled = []
    async def fake_fetch(provider, token):
        polled.append((provider, token))
        return [
            {"id": "j1", "title": "Director of Development", "url": "https://gh/j1", "posted_at": NOW},
            {"id": "j2", "title": "Software Engineer", "url": "https://gh/j2", "posted_at": NOW},
        ]
    monkeypatch.setattr(collect_ats.ats_boards, "fetch_jobs", fake_fetch)

    counts = await collect_ats.collect_ats_dev_roles(db_session, now=NOW)
    assert counts["orgs"] == 1 and counts["matched"] == 1 and counts["new"] == 1
    assert polled == [("greenhouse", "wired")]   # only the configured org

    sig = await db_session.scalar(select(Signal).where(Signal.org_id == wired.id))
    assert sig.signal_type is IntentSignalType.DEV_ROLE_POSTED
    assert sig.source is IntentSignalSource.JOBS
    assert sig.dedupe_key == f"ats:greenhouse:j1:{wired.id}"
    assert sig.evidence_url == "https://gh/j1"

    # Re-run dedups.
    again = await collect_ats.collect_ats_dev_roles(db_session, now=NOW)
    assert again["new"] == 0 and again["deduped"] == 1
    assert await db_session.scalar(select(func.count()).select_from(Signal)) == 1
