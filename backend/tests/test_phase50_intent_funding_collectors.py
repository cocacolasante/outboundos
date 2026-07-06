"""Phase 2 (cont.) — Grants.gov new_rfp + USASpending peer_funded collectors.

Both are fan-out collectors: an external event (RFP / peer award) matched to
monitored orgs by cause + geo, one signal per (event, org).  Tests cover the
client parsers, the matcher, and each collector end-to-end with the feed
mocked: contract fields, fan-out, self-skip, geo gating, idempotency.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.models import (
    IntentSignalSource, IntentSignalType, Org, OrgSizeBand, Signal,
)
from app.services.funding_sources import grants_gov
from app.services.funding_sources.base import DiscoveredOrg
from app.services.intent import collect_grants_gov, collect_usaspending, matching

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 6, 22, tzinfo=timezone.utc)


# ---- client + matcher helpers ---------------------------------------------

async def test_grants_gov_parse_and_map():
    assert grants_gov._parse_date("06/22/2026") == date(2026, 6, 22)
    assert grants_gov._parse_date("") is None
    assert grants_gov._parse_date(None) is None
    opp = grants_gov._map_opportunity({
        "id": "362903", "number": "OFOP123", "title": "  Capacity &amp; Training &ndash; 2026  ",
        "agency": "HHS", "openDate": "06/01/2026", "closeDate": "08/01/2026",
        "oppStatus": "posted",
    })
    # HTML entities decoded.
    assert opp["id"] == "362903" and opp["title"] == "Capacity & Training – 2026"
    assert opp["open_date"] == date(2026, 6, 1) and opp["close_date"] == date(2026, 8, 1)
    assert opp["evidence_url"].endswith("/search-results-detail/362903")
    assert grants_gov._map_opportunity({"title": "no id"}) is None


async def test_is_new_and_open():
    f = collect_grants_gov._is_new_and_open
    fresh = {"open_date": date(2026, 6, 10), "close_date": date(2026, 8, 1)}
    assert f(fresh, NOW, 30) is True
    stale = {"open_date": date(2026, 1, 1), "close_date": date(2026, 8, 1)}
    assert f(stale, NOW, 30) is False           # posted > 30 days ago
    closed = {"open_date": date(2026, 6, 10), "close_date": date(2026, 6, 1)}
    assert f(closed, NOW, 30) is False           # already closed
    undated = {"open_date": None, "close_date": None}
    assert f(undated, NOW, 30) is True           # no dates → keep


async def test_matcher_cause_geo_and_normalize():
    assert matching.org_matches_cause("T31", ["T"]) is True
    assert matching.org_matches_cause("B82", ["T"]) is False
    assert matching.org_matches_cause("B82", []) is True   # empty = any
    assert matching.normalize_name("The A.C.F., Inc.") == "the a c f inc"


async def _org(db_session, *, name, state, ntee="T31", ein):
    org = Org(name=name, ein=ein.replace("-", ""), state=state,
              ntee_code=ntee, size_band=OrgSizeBand.SMALL)
    db_session.add(org)
    await db_session.flush()
    return org


async def test_candidate_orgs_filters_cause_and_geo(db_session):
    await _org(db_session, name="WI Foundation", state="WI", ein="11-1111111")
    await _org(db_session, name="CA Foundation", state="CA", ein="22-2222222")
    await _org(db_session, name="WI Health", state="WI", ntee="E20", ein="33-3333333")
    await db_session.commit()

    only_t = await matching.candidate_orgs(db_session, cause_prefixes=["T"])
    assert {o.name for o in only_t} == {"WI Foundation", "CA Foundation"}
    wi_t = await matching.candidate_orgs(db_session, cause_prefixes=["T"], geographies=["WI"])
    assert {o.name for o in wi_t} == {"WI Foundation"}


# ---- Grants.gov collector --------------------------------------------------

_OPP = {
    "id": "999001", "number": "HHS-2026-X", "title": "Nonprofit Capacity Building",
    "agency": "HHS", "open_date": date(2026, 6, 10), "close_date": date(2026, 8, 15),
    "status": "posted",
    "evidence_url": "https://www.grants.gov/search-results-detail/999001",
}


async def test_collect_grants_gov_fans_out_and_is_idempotent(db_session, monkeypatch):
    async def fake_search(keyword, **kw):
        return [_OPP]
    monkeypatch.setattr(collect_grants_gov.grants_gov, "search_opportunities", fake_search)

    a = await _org(db_session, name="WI Foundation", state="WI", ein="11-1111111")
    b = await _org(db_session, name="CA Foundation", state="CA", ein="22-2222222")
    await db_session.commit()

    counts = await collect_grants_gov.collect_grants_gov(
        db_session, keywords=["capacity building"], cause_prefixes=["T"], now=NOW)
    assert counts["opportunities"] == 1 and counts["new"] == 2

    sigs = (await db_session.execute(select(Signal))).scalars().all()
    assert len(sigs) == 2
    s = next(x for x in sigs if x.org_id == a.id)
    assert s.signal_type is IntentSignalType.NEW_RFP
    assert s.source is IntentSignalSource.GRANTS_GOV
    assert s.dedupe_key == f"grantsgov:rfp:999001:{a.id}"
    assert "Nonprofit Capacity Building" in s.summary and "HHS" in s.summary
    assert s.evidence_url.endswith("/999001")
    assert s.event_date == datetime(2026, 6, 10, tzinfo=timezone.utc)

    # Re-run dedups (no new rows).
    again = await collect_grants_gov.collect_grants_gov(
        db_session, keywords=["capacity building"], cause_prefixes=["T"], now=NOW)
    assert again["new"] == 0 and again["deduped"] == 2
    assert await db_session.scalar(select(func.count()).select_from(Signal)) == 2


async def test_collect_grants_gov_skips_stale_and_no_candidates(db_session, monkeypatch):
    stale = {**_OPP, "id": "888", "open_date": date(2026, 1, 1)}
    async def fake_search(keyword, **kw):
        return [stale]
    monkeypatch.setattr(collect_grants_gov.grants_gov, "search_opportunities", fake_search)

    # No monitored orgs → skip entirely.
    empty = await collect_grants_gov.collect_grants_gov(
        db_session, keywords=["x"], cause_prefixes=["T"], now=NOW)
    assert empty["new"] == 0 and empty["opportunities"] == 0

    # Org present but opportunity is stale → considered, no emit.
    await _org(db_session, name="WI Foundation", state="WI", ein="11-1111111")
    await db_session.commit()
    counts = await collect_grants_gov.collect_grants_gov(
        db_session, keywords=["x"], cause_prefixes=["T"], now=NOW)
    assert counts["opportunities"] == 0 and counts["new"] == 0


# ---- USASpending peer collector --------------------------------------------

def _award(*, name, state, award_id, amount=500_000, gen_id="ASST_NON_1"):
    return DiscoveredOrg(
        signal_type="grant_awarded", summary=f"{name} won a grant",
        dedup_key=f"grant_awarded:{award_id}", org_name=name, state=state,
        detail={"amount": amount, "agency": "HHS", "award_id": award_id,
                "action_date": "2026-06-01", "recipient_state": state,
                "generated_internal_id": gen_id},
    )


async def test_collect_usaspending_peer_geo_match_self_skip_idempotent(db_session, monkeypatch):
    awards = [
        _award(name="Peer Org of Wisconsin", state="WI", award_id="AW1"),
        _award(name="WI Foundation", state="WI", award_id="AW2"),   # self-match
        _award(name="Texas Org", state="TX", award_id="AW3"),       # no monitored org
    ]
    async def fake_fetch(since, until, **kw):
        return awards
    monkeypatch.setattr(collect_usaspending.usaspending, "fetch_recent_awards", fake_fetch)

    wi = await _org(db_session, name="WI Foundation", state="WI", ein="11-1111111")
    await db_session.commit()

    counts = await collect_usaspending.collect_usaspending_peer(
        db_session, cause_prefixes=["T"], now=NOW)
    # AW1 → 1 peer signal on WI Foundation; AW2 self-skipped; AW3 no org in TX.
    assert counts["new"] == 1 and counts["self_skipped"] == 1

    s = await db_session.scalar(select(Signal).where(Signal.org_id == wi.id))
    assert s.signal_type is IntentSignalType.PEER_FUNDED
    assert s.source is IntentSignalSource.USASPENDING
    assert s.dedupe_key == f"usaspending:peer:AW1:{wi.id}"
    assert "Peer Org of Wisconsin" in s.summary and "WI" in s.summary
    assert s.evidence_url.endswith("/award/ASST_NON_1")
    assert s.event_date == datetime(2026, 6, 1, tzinfo=timezone.utc)

    again = await collect_usaspending.collect_usaspending_peer(
        db_session, cause_prefixes=["T"], now=NOW)
    assert again["new"] == 0 and again["deduped"] == 1
    assert await db_session.scalar(select(func.count()).select_from(Signal)) == 1


async def test_collect_usaspending_peer_no_candidates(db_session, monkeypatch):
    async def fake_fetch(since, until, **kw):
        return [_award(name="Peer", state="WI", award_id="AW1")]
    monkeypatch.setattr(collect_usaspending.usaspending, "fetch_recent_awards", fake_fetch)
    counts = await collect_usaspending.collect_usaspending_peer(
        db_session, cause_prefixes=["T"], now=NOW)
    assert counts["new"] == 0
    assert await db_session.scalar(select(func.count()).select_from(Signal)) == 0
