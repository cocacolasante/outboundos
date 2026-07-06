"""Phase 43 — nonprofit funding discovery (USAspending + IRS EO BMF).

Feeds the existing prospect_signals review queue.  Covers feed parsing
(usaspending mapping/pagination/dedup; irs_bmf SUBSECTION+RULING
window), the staging autonomy boundary (campaign-less Lead + signal +
task + one notification, never a campaign/sequence), dedup, and cursor
advance.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select

from app.models import (
    CrmActivity,
    CrmActivityType,
    FundingEnrichmentQueue,
    FundingEnrichmentStatus,
    FundingSourceState,
    Lead,
    LeadSequenceState,
    Notification,
    ProspectSignal,
)
from app.services.funding_sources import irs_bmf, usaspending
from app.services.funding_sources.base import DiscoveredOrg
from app.services.funding_sources.enrichment import ContactResult
from app.workers import funding_signals


def _resolved(email="ed@helpinghands.org", **kw) -> ContactResult:
    return ContactResult(
        status="resolved", domain="helpinghands.org", email=email,
        first_name=kw.get("first_name", "Dana"),
        last_name=kw.get("last_name", "Reed"),
        title=kw.get("title", "Executive Director"),
        via=kw.get("via", "website"),
    )
from app.workers.funding_signals import (
    _poll_irs_bmf_async,
    _poll_usaspending_async,
    _stage_discovery_signal,
    _yyyymm,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# httpx fakes
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, *, json_data=None, text=None):
        self._json = json_data
        self.text = text

    def raise_for_status(self):
        return None

    def json(self):
        return self._json


class _FakeClient:
    """Async-context-manager stand-in for httpx.AsyncClient."""
    def __init__(self, handler):
        self._handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        return self._handler(url, json)

    async def get(self, url, params=None):
        return self._handler(url, params)


def _patch_client(monkeypatch, module, handler):
    monkeypatch.setattr(module, "httpx", _HttpxShim(handler))


class _HttpxShim:
    def __init__(self, handler):
        self._handler = handler

    def AsyncClient(self, *a, **k):  # noqa: N802 — mimic httpx API
        return _FakeClient(self._handler)


# ---------------------------------------------------------------------------
# usaspending feed
# ---------------------------------------------------------------------------


async def test_usaspending_mapping_pagination_dedup(monkeypatch):
    def handler(url, body):
        page = body.get("page", 1)
        if page == 1:
            return _FakeResp(json_data={
                "results": [
                    {"Award ID": "A1", "Recipient Name": "Helping Hands",
                     "Award Amount": 50000, "Awarding Agency": "HHS",
                     "Start Date": "2026-06-01",
                     "recipient_location_state_code": "PA"},
                    {"Award ID": "B2", "Recipient Name": "Food Bank Inc",
                     "Award Amount": 12000, "Awarding Agency": "USDA",
                     "Start Date": "2026-06-02"},
                ],
                "page_metadata": {"page": 1, "hasNext": True},
            })
        return _FakeResp(json_data={
            "results": [
                # B2 repeated across pages → must dedup.
                {"Award ID": "B2", "Recipient Name": "Food Bank Inc",
                 "Award Amount": 12000, "Awarding Agency": "USDA"},
                {"Award ID": "C3", "Recipient Name": "Youth Center",
                 "Award Amount": 9000, "Awarding Agency": "ED"},
            ],
            "page_metadata": {"page": 2, "hasNext": False},
        })

    _patch_client(monkeypatch, usaspending, handler)
    orgs = await usaspending.fetch_recent_awards(date(2026, 6, 1), date(2026, 6, 8))

    keys = [o.dedup_key for o in orgs]
    assert keys == ["grant_awarded:A1", "grant_awarded:B2", "grant_awarded:C3"]
    a1 = orgs[0]
    assert a1.signal_type == "grant_awarded"
    assert a1.org_name == "Helping Hands"
    assert a1.state == "PA"
    assert a1.detail["amount"] == 50000
    assert a1.detail["agency"] == "HHS"
    assert a1.detail["award_id"] == "A1"
    assert "Helping Hands won a federal grant" in a1.summary
    assert "$50,000" in a1.summary


async def test_usaspending_award_amount_filter(monkeypatch):
    """A max_amount adds the award_amounts filter + sorts ascending so a
    tight cap keeps the smallest grants; no bound = no filter, desc sort."""
    captured = {}

    def handler(url, body):
        captured["body"] = body
        return _FakeResp(json_data={"results": [], "page_metadata": {"hasNext": False}})

    _patch_client(monkeypatch, usaspending, handler)
    await usaspending.fetch_recent_awards(
        date(2026, 6, 1), date(2026, 6, 8), min_amount=10000, max_amount=500000,
    )
    f = captured["body"]["filters"]
    assert f["award_amounts"] == [{"lower_bound": 10000.0, "upper_bound": 500000.0}]
    # Always biggest-first (largest grant under the cap = best target).
    assert captured["body"]["order"] == "desc"

    await usaspending.fetch_recent_awards(date(2026, 6, 1), date(2026, 6, 8))
    assert "award_amounts" not in captured["body"]["filters"]
    assert captured["body"]["order"] == "desc"


async def test_usaspending_poll_passes_amount_bounds(db_session, monkeypatch):
    monkeypatch.setattr(funding_signals.settings, "USASPENDING_ENABLED", True)
    monkeypatch.setattr(funding_signals.settings, "USASPENDING_MAX_AWARD_AMOUNT", 500000)
    monkeypatch.setattr(funding_signals.settings, "USASPENDING_MIN_AWARD_AMOUNT", None)
    captured = {}

    async def _fake_fetch(since, until, **kw):
        captured.update(kw)
        return []

    monkeypatch.setattr(funding_signals.usaspending, "fetch_recent_awards", _fake_fetch)
    await _poll_usaspending_async()
    assert captured["max_amount"] == 500000
    assert captured["min_amount"] is None


async def test_irs_fetch_max_orgs_bounds_list(monkeypatch):
    """The IRS fetch caps the parsed list (and keeps the newest rulings)."""
    rows = "\n".join(
        f'{1000+i},ORG {i},3,2026{(i % 12) + 1:02d},P20,1000'
        for i in range(50)
    )
    csv = "EIN,NAME,SUBSECTION,RULING,NTEE_CD,CLASSIFICATION\n" + rows

    def handler(url, body):
        return _FakeResp(text=csv)

    _patch_client(monkeypatch, irs_bmf, handler)
    orgs = await irs_bmf.fetch_new_501c3(["PA"], "202001", max_orgs=5)
    assert len(orgs) == 5                            # bounded


async def test_irs_poll_respects_max_per_run(db_session, monkeypatch):
    monkeypatch.setattr(funding_signals.settings, "IRS_BMF_ENABLED", True)
    monkeypatch.setattr(funding_signals.settings, "IRS_BMF_STATES", ["PA"])
    db_session.add(FundingSourceState(
        source="irs_bmf", enabled=True,
        config={"states": ["PA"], "ruling_lookback_months": 2, "max_per_run": 3},
        cursor={},
    ))
    await db_session.commit()

    captured = {}

    async def _fake_fetch(states, since_ruling, *, max_orgs=None):
        captured["max_orgs"] = max_orgs
        return [_org(dedup=f"new_501c3:{i}", signal_type="new_501c3") for i in range(10)]

    monkeypatch.setattr(funding_signals.irs_bmf, "fetch_new_501c3", _fake_fetch)
    monkeypatch.setattr(
        funding_signals.enrichment, "resolve_contact",
        AsyncMock(return_value=ContactResult(status="no_contact")),
    )
    result = await _poll_irs_bmf_async()
    # Cap 3 → only 3 enriched/queued even though 10 were fetched.
    assert result["queued"] == 3
    assert result["capped"] is True
    assert captured["max_orgs"] == 12                # cap * 4


async def test_funding_source_patch_sets_max_per_run(client, db_session, monkeypatch):
    monkeypatch.setattr(funding_signals.settings, "IRS_BMF_ENABLED", True)
    monkeypatch.setattr(funding_signals.settings, "IRS_BMF_STATES", ["PA"])
    resp = await client.patch("/signals/funding/sources/irs_bmf", json={"max_per_run": 10})
    assert resp.status_code == 200, resp.text
    assert resp.json()["config"]["max_per_run"] == 10


async def test_funding_source_patch_sets_max_award(client, db_session, monkeypatch):
    monkeypatch.setattr(funding_signals.settings, "USASPENDING_ENABLED", True)
    # Set a cap.
    resp = await client.patch("/signals/funding/sources/usaspending", json={
        "lookback_days": 30, "max_award_amount": 250000,
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["config"]["max_award_amount"] == 250000
    # Clear it (explicit null = no cap).
    resp = await client.patch("/signals/funding/sources/usaspending", json={
        "max_award_amount": None,
    })
    assert resp.status_code == 200
    assert resp.json()["config"]["max_award_amount"] is None


async def test_usaspending_outage_returns_empty(monkeypatch):
    def handler(url, body):
        raise RuntimeError("usaspending down")
    _patch_client(monkeypatch, usaspending, handler)
    orgs = await usaspending.fetch_recent_awards(date(2026, 6, 1), date(2026, 6, 8))
    assert orgs == []


# ---------------------------------------------------------------------------
# irs_bmf feed
# ---------------------------------------------------------------------------


_BMF_HEADER = "EIN,NAME,STATE,SUBSECTION,RULING,NTEE_CD,CLASSIFICATION"
_BMF_CSV = "\n".join([
    _BMF_HEADER,
    "11,New Charity,PA,3,202605,P20,1000",       # 501c3, in window → keep
    "22,Old Charity,PA,3,202601,A20,1000",        # 501c3, before window → drop
    "33,Social Welfare,PA,4,202605,,1000",        # 501c4 → drop
    "44,No Ruling,PA,3,,B20,1000",                # blank ruling → drop
    "55,Arts Group,PA,3,202606,A60,1000",         # 501c3, in window → keep
])


async def test_irs_bmf_filters_subsection_and_ruling_window(monkeypatch):
    def handler(url, params):
        assert url.endswith("/eo_pa.csv")
        return _FakeResp(text=_BMF_CSV)
    _patch_client(monkeypatch, irs_bmf, handler)

    orgs = await irs_bmf.fetch_new_501c3(["PA"], since_ruling="202604")
    keys = sorted(o.dedup_key for o in orgs)
    assert keys == ["new_501c3:11", "new_501c3:55"]
    keep = next(o for o in orgs if o.ein == "11")
    assert keep.signal_type == "new_501c3"
    assert keep.org_name == "New Charity"
    assert keep.state == "PA"
    assert keep.ntee_code == "P20"
    assert keep.detail["ruling_date"] == "202605"
    assert keep.detail["classification"] == "1000"


async def test_irs_bmf_state_outage_skipped(monkeypatch):
    def handler(url, params):
        raise RuntimeError("404")
    _patch_client(monkeypatch, irs_bmf, handler)
    orgs = await irs_bmf.fetch_new_501c3(["PA", "NJ"], since_ruling="202601")
    assert orgs == []


# ---------------------------------------------------------------------------
# staging — autonomy boundary
# ---------------------------------------------------------------------------


def _org(dedup="grant_awarded:X1", **kw) -> DiscoveredOrg:
    defaults = dict(
        signal_type="grant_awarded",
        summary="Helping Hands won a federal grant ($50,000)",
        dedup_key=dedup,
        org_name="Helping Hands",
        state="PA",
        ein="99",
        ntee_code="P20",
        website="https://helpinghands.org",
        detail={"amount": 50000, "agency": "HHS", "award_id": "X1"},
    )
    defaults.update(kw)
    return DiscoveredOrg(**defaults)


async def test_staging_with_email_creates_lead_signal_task_notification(
    db_session, monkeypatch,
):
    monkeypatch.setattr(
        funding_signals.enrichment, "resolve_contact",
        AsyncMock(return_value=_resolved()),
    )
    org = _org()
    result = await _stage_discovery_signal(db_session, "usaspending", org)
    await db_session.commit()
    assert result == {"staged": True, "lead_created": True, "task": True, "queued": False}

    # Campaign-less Lead.
    lead = await db_session.scalar(
        select(Lead).where(Lead.email == "ed@helpinghands.org")
    )
    assert lead is not None
    assert lead.campaign_id is None                  # NEVER a campaign
    assert lead.company == "Helping Hands"
    assert lead.job_title == "Executive Director"
    assert lead.research_data["ein"] == "99"
    assert lead.research_data["source"] == "usaspending"

    # ProspectSignal — discovery: source set, watch_id NULL.
    signal = await db_session.scalar(
        select(ProspectSignal).where(ProspectSignal.dedup_key == org.dedup_key)
    )
    assert signal is not None
    assert signal.source == "usaspending"
    assert signal.watch_id is None
    assert signal.signal_type == "grant_awarded"
    assert signal.lead_id == lead.id

    # CRM reach-out task.
    task = await db_session.scalar(
        select(CrmActivity).where(
            CrmActivity.lead_id == lead.id,
            CrmActivity.activity_type == CrmActivityType.TASK,
        )
    )
    assert task is not None
    assert task.subject.startswith("Reach out —")
    assert task.is_agent_generated is True
    assert task.reminder_sent_at is not None

    # One owner notification.
    notifs = (await db_session.execute(
        select(Notification).where(
            Notification.dedup_key == f"prospect_signal:{org.dedup_key}"
        )
    )).scalars().all()
    assert len(notifs) == 1

    # NEVER a sequence enrollment.
    enrollments = (await db_session.execute(
        select(LeadSequenceState).where(LeadSequenceState.lead_id == lead.id)
    )).scalars().all()
    assert enrollments == []


async def test_staging_without_contact_is_gated_to_queue(db_session, monkeypatch):
    """No contact → the org is GATED out of the review queue: no
    ProspectSignal / Lead / task / notification, just a pending
    FundingEnrichmentQueue row the retry worker owns."""
    monkeypatch.setattr(
        funding_signals.enrichment, "resolve_contact",
        AsyncMock(return_value=ContactResult(status="no_domain")),
    )
    org = _org(dedup="new_501c3:77", signal_type="new_501c3",
               summary="New 501(c)(3): Tiny Org (PA)",
               website=None, ein="77",
               mailing_address={"street": "1 Main", "city": "Erie", "state": "PA", "zip": "16501"})
    result = await _stage_discovery_signal(db_session, "irs_bmf", org)
    await db_session.commit()
    assert result == {"staged": False, "lead_created": False, "task": False, "queued": True}

    # NOT in the review queue, no side effects.
    assert (await db_session.execute(select(ProspectSignal))).scalars().first() is None
    assert (await db_session.execute(select(Lead))).scalars().first() is None
    assert (await db_session.execute(select(CrmActivity))).scalars().first() is None
    assert (await db_session.execute(select(Notification))).scalars().first() is None

    # Parked in the deferred-enrichment queue (pending), with the address.
    row = await db_session.scalar(
        select(FundingEnrichmentQueue).where(
            FundingEnrichmentQueue.dedup_key == org.dedup_key
        )
    )
    assert row is not None
    assert row.status == FundingEnrichmentStatus.PENDING
    assert row.attempts == 1
    assert row.next_attempt_at is not None
    assert row.payload["detail"]["mailing_address"]["city"] == "Erie"

    # Re-discovery of the same org doesn't double-queue or re-resolve.
    r2 = await _stage_discovery_signal(db_session, "irs_bmf", org)
    await db_session.commit()
    assert r2["queued"] is False
    n = await db_session.scalar(
        select(func.count()).select_from(FundingEnrichmentQueue)
        .where(FundingEnrichmentQueue.dedup_key == org.dedup_key)
    )
    assert n == 1


async def test_staging_dedup_emits_once(db_session, monkeypatch):
    monkeypatch.setattr(
        funding_signals.enrichment, "resolve_contact",
        AsyncMock(return_value=_resolved()),
    )
    org = _org(dedup="grant_awarded:DUP")
    r1 = await _stage_discovery_signal(db_session, "usaspending", org)
    await db_session.commit()
    r2 = await _stage_discovery_signal(db_session, "usaspending", org)
    await db_session.commit()
    assert r1["staged"] is True
    assert r2["staged"] is False
    count = (await db_session.execute(
        select(func.count()).select_from(ProspectSignal)
        .where(ProspectSignal.dedup_key == "grant_awarded:DUP")
    )).scalar_one()
    assert count == 1


# ---------------------------------------------------------------------------
# poll tasks — disabled no-op, cursor advance, first-run guard
# ---------------------------------------------------------------------------


async def test_usaspending_poll_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(funding_signals.settings, "USASPENDING_ENABLED", False)
    assert await _poll_usaspending_async() == {"skipped": "disabled"}


async def test_irs_bmf_poll_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(funding_signals.settings, "IRS_BMF_ENABLED", False)
    assert await _poll_irs_bmf_async() == {"skipped": "disabled"}


async def test_usaspending_poll_uses_trailing_window(db_session, monkeypatch):
    monkeypatch.setattr(funding_signals.settings, "USASPENDING_ENABLED", True)
    monkeypatch.setattr(funding_signals.settings, "USASPENDING_LOOKBACK_DAYS", 30)
    captured = {}

    async def _fake_fetch(since, until, **kw):
        captured["since"] = since
        captured["until"] = until
        return [_org(dedup="grant_awarded:CUR1")]

    monkeypatch.setattr(funding_signals.usaspending, "fetch_recent_awards", _fake_fetch)
    monkeypatch.setattr(
        funding_signals.enrichment, "resolve_contact",
        AsyncMock(return_value=_resolved()),
    )
    result = await _poll_usaspending_async()
    assert result["fetched"] == 1
    assert result["staged"] == 1

    today = datetime.now(timezone.utc).date()
    # Trailing window: since = today - lookback (NOT a since-cursor), so the
    # configured lookback applies on every run — covering reporting lag.
    assert captured["until"] == today
    assert captured["since"] == today - timedelta(days=30)

    state = (await db_session.execute(select(FundingSourceState).where(FundingSourceState.source == "usaspending"))).scalars().first()
    assert state is not None
    assert state.cursor["last_window_start"] == (today - timedelta(days=30)).isoformat()
    assert state.cursor["last_run_date"] == today.isoformat()
    assert state.last_run_status == "done"


async def test_usaspending_poll_rescans_window_dedup_makes_overlap_free(db_session, monkeypatch):
    """A second poll re-fetches the same award; the dedup_key guard makes it
    a no-op (no duplicate signal), so overlapping windows are safe."""
    monkeypatch.setattr(funding_signals.settings, "USASPENDING_ENABLED", True)
    monkeypatch.setattr(
        funding_signals.usaspending, "fetch_recent_awards",
        AsyncMock(return_value=[_org(dedup="grant_awarded:DUP1")]),
    )
    monkeypatch.setattr(
        funding_signals.enrichment, "resolve_contact",
        AsyncMock(return_value=_resolved()),
    )
    first = await _poll_usaspending_async()
    second = await _poll_usaspending_async()
    assert first["staged"] == 1
    assert second["staged"] == 0          # re-seen award not re-staged

    n = await db_session.scalar(
        select(func.count()).select_from(ProspectSignal).where(
            ProspectSignal.dedup_key == "grant_awarded:DUP1"
        )
    )
    assert n == 1


async def test_irs_bmf_first_run_guard_bounds_window(db_session, monkeypatch):
    monkeypatch.setattr(funding_signals.settings, "IRS_BMF_ENABLED", True)
    monkeypatch.setattr(funding_signals.settings, "IRS_BMF_STATES", ["PA"])
    monkeypatch.setattr(funding_signals.settings, "IRS_BMF_RULING_LOOKBACK_MONTHS", 2)

    captured = {}

    async def _fake_fetch(states, since_ruling, *, max_orgs=None):
        captured["since_ruling"] = since_ruling
        captured["states"] = states
        return []

    monkeypatch.setattr(funding_signals.irs_bmf, "fetch_new_501c3", _fake_fetch)
    result = await _poll_irs_bmf_async()

    today = datetime.now(timezone.utc).date()
    # First run with no cursor → bounded to the lookback floor, NOT the
    # whole historical file.
    assert captured["since_ruling"] == _yyyymm(today, 2)
    assert captured["states"] == ["PA"]
    assert result["since_ruling"] == _yyyymm(today, 2)

    state = (await db_session.execute(select(FundingSourceState).where(FundingSourceState.source == "irs_bmf"))).scalars().first()
    assert state.cursor["last_file_month"] == _yyyymm(today)
    assert state.last_run_status == "done"


async def test_yyyymm_helper():
    assert _yyyymm(date(2026, 6, 15)) == "202606"
    assert _yyyymm(date(2026, 6, 15), 2) == "202604"
    assert _yyyymm(date(2026, 1, 15), 2) == "202511"   # crosses year boundary


# ---------------------------------------------------------------------------
# API surface — source serializer + filter (discovery signals appear)
# ---------------------------------------------------------------------------


async def test_signals_list_exposes_source_and_filters(client, db_session):
    # A discovery signal (watch_id NULL, source set) + a watch signal.
    db_session.add_all([
        ProspectSignal(
            watch_id=None, source="usaspending", signal_type="grant_awarded",
            summary="Helping Hands won a federal grant",
            dedup_key="grant_awarded:API1", detail={"amount": 50000},
        ),
        ProspectSignal(
            watch_id=None, source="irs_bmf", signal_type="new_501c3",
            summary="New 501(c)(3): Tiny Org",
            dedup_key="new_501c3:API2", detail={},
        ),
        # watch-sourced (source NULL) — must still appear, no inner join.
        ProspectSignal(
            watch_id=None, source=None, signal_type="job_change",
            summary="Someone changed roles",
            dedup_key="job_change:API3", detail={},
        ),
    ])
    await db_session.commit()

    # No filter → all three, each carrying its source in the serializer.
    body = (await client.get("/signals")).json()
    assert body["total"] == 3
    by_key = {i["summary"]: i for i in body["items"]}
    assert by_key["Helping Hands won a federal grant"]["source"] == "usaspending"
    assert by_key["Someone changed roles"]["source"] is None

    # source=usaspending → just the grant.
    body = (await client.get("/signals?source=usaspending")).json()
    assert body["total"] == 1
    assert body["items"][0]["source"] == "usaspending"

    # source=watch → only the watch-sourced (source NULL) signal.
    body = (await client.get("/signals?source=watch")).json()
    assert body["total"] == 1
    assert body["items"][0]["source"] is None


# ---------------------------------------------------------------------------
# Settings → Discovery: DB-backed config + API
# ---------------------------------------------------------------------------


async def test_funding_sources_list_seeds_from_env(client, db_session, monkeypatch):
    monkeypatch.setattr(funding_signals.settings, "USASPENDING_ENABLED", True)
    monkeypatch.setattr(funding_signals.settings, "USASPENDING_LOOKBACK_DAYS", 7)
    monkeypatch.setattr(funding_signals.settings, "IRS_BMF_ENABLED", True)
    monkeypatch.setattr(funding_signals.settings, "IRS_BMF_STATES", ["PA", "NJ"])
    # router reads settings.HUNTER_API_KEY for the hunter_configured flag
    from app.routers import signals as signals_router
    monkeypatch.setattr(signals_router.settings, "HUNTER_API_KEY", "")

    resp = await client.get("/signals/funding/sources")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["hunter_configured"] is False
    by_src = {s["source"]: s for s in body["sources"]}
    assert set(by_src) == {"usaspending", "irs_bmf"}
    assert by_src["usaspending"]["enabled"] is True
    assert by_src["usaspending"]["label"] == "USASpending"
    assert by_src["usaspending"]["config"]["lookback_days"] == 7
    assert by_src["irs_bmf"]["config"]["states"] == ["PA", "NJ"]
    assert by_src["usaspending"]["signal_count"] == 0

    # Seeding persisted the rows.
    state = (await db_session.execute(select(FundingSourceState).where(FundingSourceState.source == "usaspending"))).scalars().first()
    assert state is not None and state.enabled is True


async def test_funding_source_patch_updates_config(client, db_session, monkeypatch):
    monkeypatch.setattr(funding_signals.settings, "IRS_BMF_ENABLED", False)
    monkeypatch.setattr(funding_signals.settings, "IRS_BMF_STATES", [])

    resp = await client.patch("/signals/funding/sources/irs_bmf", json={
        "enabled": True,
        "ruling_lookback_months": 3,
        "states": ["pa", " NJ ", "ny", "pa"],   # normalise + dedupe
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is True
    assert body["config"]["ruling_lookback_months"] == 3
    assert body["config"]["states"] == ["PA", "NJ", "NY"]

    state = (await db_session.execute(select(FundingSourceState).where(FundingSourceState.source == "irs_bmf"))).scalars().first()
    await db_session.refresh(state)
    assert state.enabled is True
    assert state.config["states"] == ["PA", "NJ", "NY"]


async def test_funding_source_patch_unknown_404(client):
    resp = await client.patch("/signals/funding/sources/nope", json={"enabled": True})
    assert resp.status_code == 404


async def test_funding_run_now_enqueues_when_enabled(client, db_session, monkeypatch):
    monkeypatch.setattr(funding_signals.settings, "USASPENDING_ENABLED", True)
    called = {}
    # Patch the lazily-imported celery task's .delay so no broker is hit.
    monkeypatch.setattr(
        funding_signals.poll_usaspending, "delay",
        lambda: called.setdefault("delay", True),
    )

    resp = await client.post("/signals/funding/sources/usaspending/run-now")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"enqueued": True}
    assert called.get("delay") is True


async def test_funding_run_now_409_when_disabled(client, db_session, monkeypatch):
    monkeypatch.setattr(funding_signals.settings, "USASPENDING_ENABLED", False)
    resp = await client.post("/signals/funding/sources/usaspending/run-now")
    assert resp.status_code == 409
    assert "disabled" in resp.json()["detail"]


async def test_funding_run_now_409_irs_without_states(client, db_session, monkeypatch):
    monkeypatch.setattr(funding_signals.settings, "IRS_BMF_ENABLED", True)
    monkeypatch.setattr(funding_signals.settings, "IRS_BMF_STATES", [])
    resp = await client.post("/signals/funding/sources/irs_bmf/run-now")
    assert resp.status_code == 409
    assert "state" in resp.json()["detail"].lower()


async def test_worker_honors_db_config_over_env(db_session, monkeypatch):
    """A UI-set DB row wins over env: env says disabled, DB says enabled
    with its own lookback → the poll runs with the DB lookback."""
    # Env default disabled — but a pre-existing DB row (UI-enabled) wins.
    monkeypatch.setattr(funding_signals.settings, "USASPENDING_ENABLED", False)
    db_session.add(FundingSourceState(
        source="usaspending", enabled=True,
        config={"lookback_days": 14}, cursor={},
    ))
    await db_session.commit()

    captured = {}

    async def _fake_fetch(since, until, *, limit=100, **kw):
        captured["since"] = since
        captured["until"] = until
        return []

    monkeypatch.setattr(funding_signals.usaspending, "fetch_recent_awards", _fake_fetch)
    result = await _poll_usaspending_async()
    assert result.get("fetched") == 0          # ran (not skipped)
    # 14-day lookback from the DB config, not the env default 7.
    assert (captured["until"] - captured["since"]).days == 14


# ---------------------------------------------------------------------------
# Signal outreach: draft → send → logged as lead + activity
# ---------------------------------------------------------------------------

from app.models import CrmActivityDirection as _Dir  # noqa: E402


async def _signal_with_staged_lead(db_session, *, email="ed@helpinghands.org"):
    lead = Lead(
        campaign_id=None, email=email, first_name="Dana", last_name="Reed",
        company="Helping Hands", company_website="https://helpinghands.org",
    )
    db_session.add(lead)
    await db_session.flush()
    signal = ProspectSignal(
        watch_id=None, source="usaspending", signal_type="grant_awarded",
        summary="Helping Hands won a federal grant ($50,000) from HHS",
        detail={"amount": 50000, "agency": "HHS", "award_id": "X1"},
        dedup_key=f"grant_awarded:{uuid.uuid4().hex}", lead_id=lead.id,
    )
    db_session.add(signal)
    await db_session.commit()
    await db_session.refresh(signal)
    return signal, lead


async def test_signal_draft_composes_from_signal_and_lead(client, db_session, monkeypatch):
    signal, lead = await _signal_with_staged_lead(db_session)
    monkeypatch.setattr(
        "app.services.signal_outreach.compose_signal_email",
        AsyncMock(return_value={
            "subject": "Congrats on the HHS grant",
            "body": "Hi Dana, congratulations on the grant. Quick call?",
        }),
    )
    resp = await client.post(f"/signals/{signal.id}/draft", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["to_email"] == "ed@helpinghands.org"
    assert body["to_name"] == "Dana Reed"
    assert body["subject"] == "Congrats on the HHS grant"
    assert "congratulations" in body["body"].lower()


async def test_signal_draft_409_without_contact(client, db_session):
    signal = ProspectSignal(
        watch_id=None, source="irs_bmf", signal_type="new_501c3",
        summary="New 501(c)(3): Tiny Org", detail={},
        dedup_key=f"new_501c3:{uuid.uuid4().hex}", lead_id=None,
    )
    db_session.add(signal)
    await db_session.commit()
    resp = await client.post(f"/signals/{signal.id}/draft", json={})
    assert resp.status_code == 409
    assert "nothing to email" in resp.json()["detail"]


async def test_signal_send_logs_activity_and_actions_signal(client, db_session, monkeypatch):
    signal, lead = await _signal_with_staged_lead(db_session)
    # No real Brevo call — stub the shared send core's brevo.send_email.
    monkeypatch.setattr("app.services.outreach.settings.BREVO_API_KEY", "k")
    monkeypatch.setattr(
        "app.services.outreach.brevo.send_email",
        AsyncMock(return_value="msg-sig-1"),
    )

    resp = await client.post(f"/signals/{signal.id}/send", json={
        "subject": "Congrats on the HHS grant",
        "body": "Hi Dana, congratulations. Quick call next week?",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["message_id"] == "msg-sig-1"
    assert body["crm_activity_logged"] is True
    assert body["crm_lead_created"] is False        # logged on the staged lead
    assert body["crm_lead_id"] == str(lead.id)
    assert body["signal_status"] == "actioned"

    # Outbound EMAIL activity on the staged lead.
    act = await db_session.scalar(
        select(CrmActivity).where(
            CrmActivity.lead_id == lead.id,
            CrmActivity.activity_type == CrmActivityType.EMAIL,
        )
    )
    assert act is not None
    assert act.direction is _Dir.OUTBOUND
    assert act.subject == "Congrats on the HHS grant"

    # Signal flipped to actioned.
    await db_session.refresh(signal)
    assert signal.status.value == "actioned"


async def test_signal_send_passes_chosen_sender(client, db_session, monkeypatch):
    from app.models import ConnectedAccount
    from app.services import encryption

    signal, lead = await _signal_with_staged_lead(db_session, email="ed2@hh.org")
    acc = ConnectedAccount(
        label="Outreach", email_address="me@csuitecode.com",
        imap_host="h", username="me@csuitecode.com",
        password_encrypted=encryption.encrypt("pw"),
        signature="Anthony\nCSuite",
    )
    db_session.add(acc)
    await db_session.commit()

    monkeypatch.setattr("app.services.outreach.settings.BREVO_API_KEY", "k")
    send_mock = AsyncMock(return_value="msg-sig-2")
    monkeypatch.setattr("app.services.outreach.brevo.send_email", send_mock)

    resp = await client.post(f"/signals/{signal.id}/send", json={
        "subject": "Hi", "body": "Body here",
        "sender_email": "me@csuitecode.com", "sender_name": "Anthony",
    })
    assert resp.status_code == 200, resp.text
    kwargs = send_mock.call_args.kwargs
    assert kwargs["sender_email"] == "me@csuitecode.com"
    assert kwargs["sender_name"] == "Anthony"
    # The chosen account's signature was rendered into the HTML body.
    assert "CSuite" in kwargs["html_body"]


async def test_signal_send_signature_override(client, db_session, monkeypatch):
    """A per-send signature override replaces the account's signature."""
    from app.models import ConnectedAccount
    from app.services import encryption

    signal, lead = await _signal_with_staged_lead(db_session, email="ed3@hh.org")
    acc = ConnectedAccount(
        label="Outreach", email_address="me@csuitecode.com",
        imap_host="h", username="me@csuitecode.com",
        password_encrypted=encryption.encrypt("pw"),
        signature="Account Default Sig",
    )
    db_session.add(acc)
    await db_session.commit()

    monkeypatch.setattr("app.services.outreach.settings.BREVO_API_KEY", "k")
    send_mock = AsyncMock(return_value="msg-ovr")
    monkeypatch.setattr("app.services.outreach.brevo.send_email", send_mock)

    resp = await client.post(f"/signals/{signal.id}/send", json={
        "subject": "Hi", "body": "Body here",
        "sender_email": "me@csuitecode.com", "sender_name": "Anthony",
        "signature": "Custom Override\nGrantMind Pro",
    })
    assert resp.status_code == 200, resp.text
    html = send_mock.call_args.kwargs["html_body"]
    assert "Custom Override" in html
    assert "GrantMind Pro" in html
    assert "Account Default Sig" not in html        # override wins


async def test_signal_send_brevo_failure_502(client, db_session, monkeypatch):
    import httpx as _httpx

    signal, lead = await _signal_with_staged_lead(db_session, email="ed3@hh.org")
    monkeypatch.setattr("app.services.outreach.settings.BREVO_API_KEY", "k")

    def _raise(*a, **k):
        raise _httpx.HTTPError("boom")

    monkeypatch.setattr("app.services.outreach.brevo.send_email", AsyncMock(side_effect=_httpx.HTTPError("boom")))
    resp = await client.post(f"/signals/{signal.id}/send", json={
        "subject": "Hi", "body": "Body",
    })
    assert resp.status_code == 502
    # Signal NOT actioned on a failed send.
    await db_session.refresh(signal)
    assert signal.status.value == "new"


# ---------------------------------------------------------------------------
# Manual stop — terminate an in-flight run + break the redelivery loop
# ---------------------------------------------------------------------------


class _FakeInspect:
    def __init__(self, active, reserved):
        self._active = active
        self._reserved = reserved

    def active(self):
        return self._active

    def reserved(self):
        return self._reserved


class _FakeControl:
    def __init__(self, inspect):
        self._inspect = inspect
        self.revoked = []

    def inspect(self, timeout=2.0):
        return self._inspect

    def revoke(self, tid, terminate=False, signal=None):
        self.revoked.append((tid, terminate, signal))


async def test_stop_funding_run_revokes_matching_and_purges(monkeypatch):
    """Only the feed's own task ids (across active + reserved, all workers)
    are revoked+terminated; other task names are left alone; the broker
    purge count rides into the result."""
    control = _FakeControl(_FakeInspect(
        active={"w1": [
            {"id": "abc", "name": "funding.poll_irs_bmf"},
            {"id": "other", "name": "send.send_lead"},
        ]},
        reserved={"w1": [{"id": "def", "name": "funding.poll_irs_bmf"}]},
    ))
    monkeypatch.setattr(funding_signals.celery_app, "control", control)
    monkeypatch.setattr(
        funding_signals, "_purge_broker_messages",
        lambda name: {"queue": 3, "unacked": 1},
    )

    result = funding_signals.stop_funding_run("irs_bmf")

    assert result["terminated"] == ["abc", "def"]
    assert {tid for tid, _, _ in control.revoked} == {"abc", "def"}
    assert all(term is True for _, term, _ in control.revoked)
    assert result["purged_queued"] == 3
    assert result["purged_unacked"] == 1


async def test_stop_funding_run_noop_when_idle(monkeypatch):
    control = _FakeControl(_FakeInspect(active={}, reserved={}))
    monkeypatch.setattr(funding_signals.celery_app, "control", control)
    monkeypatch.setattr(
        funding_signals, "_purge_broker_messages",
        lambda name: {"queue": 0, "unacked": 0},
    )

    result = funding_signals.stop_funding_run("usaspending")
    assert result["terminated"] == []
    assert control.revoked == []
    assert result["purged_queued"] == 0


async def test_purge_broker_messages_removes_only_matching(monkeypatch):
    """The redis purge drops only this feed's messages from the ready
    queue + the unacked set (incl. its index), leaving send tasks intact."""
    irs = b'{"headers":{"task":"funding.poll_irs_bmf"}}'
    usa = b'{"headers":{"task":"funding.poll_usaspending"}}'
    send = b'{"headers":{"task":"send.send_lead"}}'

    class _FakeRedis:
        def __init__(self):
            self.lists = {"celery": [irs, send, send]}
            self.hash = {b"tag-irs": irs, b"tag-send": send}
            self.zset = {"tag-irs", "tag-send"}

        def lrange(self, key, start, stop):
            return list(self.lists.get(key, []))

        def lrem(self, key, count, value):
            before = len(self.lists[key])
            self.lists[key] = [v for v in self.lists[key] if v != value]
            return before - len(self.lists[key])

        def hgetall(self, key):
            return dict(self.hash)

        def hdel(self, key, field):
            self.hash.pop(field, None)

        def zrem(self, key, member):
            self.zset.discard(member)

    fake = _FakeRedis()

    class _RedisModule:
        class Redis:
            @staticmethod
            def from_url(url):
                return fake

    monkeypatch.setitem(__import__("sys").modules, "redis", _RedisModule)

    out = funding_signals._purge_broker_messages("funding.poll_irs_bmf")
    assert out == {"queue": 1, "unacked": 1}
    assert fake.lists["celery"] == [send, send]  # send tasks untouched
    assert b"tag-irs" not in fake.hash and b"tag-send" in fake.hash
    assert "tag-irs" not in fake.zset and "tag-send" in fake.zset


async def test_funding_stop_endpoint_marks_stopped(client, db_session, monkeypatch):
    monkeypatch.setattr(
        funding_signals, "stop_funding_run",
        lambda source: {
            "task": f"funding.poll_{source}",
            "terminated": ["abc"], "purged_queued": 2, "purged_unacked": 1,
        },
    )
    resp = await client.post("/signals/funding/sources/irs_bmf/stop")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["last_run_status"] == "stopped"
    assert body["stopped"]["terminated"] == ["abc"]


async def test_funding_stop_endpoint_noop_keeps_status(client, db_session, monkeypatch):
    from tests.conftest import BOOTSTRAP_TENANT_ID

    # The router resolves state for the AMBIENT tenant (per-tenant since
    # Phase 8) — seed the row under the client fixture's tenant.
    db_session.add(FundingSourceState(
        source="usaspending", enabled=True, config={"lookback_days": 7},
        cursor={}, last_run_status="done", tenant_id=BOOTSTRAP_TENANT_ID,
    ))
    await db_session.commit()
    monkeypatch.setattr(
        funding_signals, "stop_funding_run",
        lambda source: {
            "task": f"funding.poll_{source}",
            "terminated": [], "purged_queued": 0, "purged_unacked": 0,
        },
    )
    resp = await client.post("/signals/funding/sources/usaspending/stop")
    assert resp.status_code == 200, resp.text
    # Nothing was running → status is left as-is, not forced to "stopped".
    assert resp.json()["last_run_status"] == "done"


async def test_funding_stop_endpoint_unknown_404(client):
    resp = await client.post("/signals/funding/sources/nope/stop")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Bulk add signals' staged leads to a campaign
# ---------------------------------------------------------------------------

from datetime import time as _time  # noqa: E402
from app.models import Campaign, CampaignStatus, LeadSequenceState  # noqa: E402


async def _campaign(db_session, status=CampaignStatus.RUNNING) -> Campaign:
    c = Campaign(
        name="Outreach", goal="g", tone="t",
        sender_name="S", sender_email="s@x.com", sample_count=1,
        schedule_days=[], schedule_time_start=_time(0, 0),
        schedule_time_end=_time(23, 59), schedule_timezone="UTC", status=status,
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    return c


async def _signal_with_lead(db_session, email) -> tuple[ProspectSignal, Lead]:
    lead = Lead(campaign_id=None, email=email, first_name="A", company="Org")
    db_session.add(lead)
    await db_session.flush()
    sig = ProspectSignal(
        watch_id=None, source="usaspending", signal_type="grant_awarded",
        summary="Org won a grant", detail={}, lead_id=lead.id,
        dedup_key=f"grant_awarded:{uuid.uuid4().hex}",
    )
    db_session.add(sig)
    await db_session.commit()
    await db_session.refresh(sig)
    return sig, lead


async def test_signals_add_to_campaign_copies_leads_and_actions(client, db_session, monkeypatch):
    from unittest.mock import MagicMock
    research = MagicMock()
    monkeypatch.setattr("app.workers.ingest.run_campaign_research.delay", research)
    campaign = await _campaign(db_session)
    s1, l1 = await _signal_with_lead(db_session, "a@x.com")
    s2, l2 = await _signal_with_lead(db_session, "b@x.com")
    # A contactless signal in the selection → skipped, stays New.
    s3 = ProspectSignal(
        watch_id=None, source="irs_bmf", signal_type="new_501c3",
        summary="No contact", detail={}, lead_id=None,
        dedup_key=f"new_501c3:{uuid.uuid4().hex}",
    )
    db_session.add(s3)
    await db_session.commit()

    resp = await client.post("/signals/add-to-campaign", json={
        "signal_ids": [str(s1.id), str(s2.id), str(s3.id)],
        "campaign_id": str(campaign.id),
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["added"] == 2
    assert body["skipped_no_contact"] == 1
    assert body["signals_actioned"] == 2
    assert body["research_started"] is True
    research.assert_called_once_with(str(campaign.id))

    # Two new campaign leads (copies — sources stay campaign-less).
    campaign_leads = (await db_session.execute(
        select(Lead).where(Lead.campaign_id == campaign.id)
    )).scalars().all()
    assert {cl.email for cl in campaign_leads} == {"a@x.com", "b@x.com"}
    assert all(cl.id not in (l1.id, l2.id) for cl in campaign_leads)  # copies
    await db_session.refresh(l1)
    assert l1.campaign_id is None  # source untouched

    # Copies are sequence-enrolled (pipeline will run).
    for cl in campaign_leads:
        state = await db_session.scalar(
            select(LeadSequenceState).where(LeadSequenceState.lead_id == cl.id)
        )
        assert state is not None

    # Contactable signals actioned; the contactless one stays New.
    await db_session.refresh(s1); await db_session.refresh(s2); await db_session.refresh(s3)
    assert s1.status.value == "actioned"
    assert s2.status.value == "actioned"
    assert s3.status.value == "new"


async def test_signals_add_to_draft_campaign_defers_research(client, db_session, monkeypatch):
    from unittest.mock import MagicMock
    research = MagicMock()
    monkeypatch.setattr("app.workers.ingest.run_campaign_research.delay", research)
    campaign = await _campaign(db_session, status=CampaignStatus.DRAFT)
    s1, _ = await _signal_with_lead(db_session, "draft@x.com")

    resp = await client.post("/signals/add-to-campaign", json={
        "signal_ids": [str(s1.id)], "campaign_id": str(campaign.id),
    })
    body = resp.json()
    assert body["added"] == 1
    assert body["research_started"] is False
    research.assert_not_called()


async def test_signals_add_to_complete_campaign_409(client, db_session):
    campaign = await _campaign(db_session, status=CampaignStatus.COMPLETE)
    s1, _ = await _signal_with_lead(db_session, "late@x.com")
    resp = await client.post("/signals/add-to-campaign", json={
        "signal_ids": [str(s1.id)], "campaign_id": str(campaign.id),
    })
    assert resp.status_code == 409


async def test_signals_add_unknown_campaign_404(client, db_session):
    s1, _ = await _signal_with_lead(db_session, "x@x.com")
    resp = await client.post("/signals/add-to-campaign", json={
        "signal_ids": [str(s1.id)], "campaign_id": str(uuid.uuid4()),
    })
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# On-demand contact enrichment ("Find contact")
# ---------------------------------------------------------------------------

async def _notification_only_signal(db_session, *, detail=None, summary=None):
    signal = ProspectSignal(
        watch_id=None, source="usaspending", signal_type="grant_awarded",
        summary=summary or "Helping Hands won a federal grant ($50,000) from HHS",
        detail=detail if detail is not None else {
            "amount": 50000, "org_name": "Helping Hands",
            "website": "https://helpinghands.org",
        },
        dedup_key=f"grant_awarded:{uuid.uuid4().hex}", lead_id=None,
    )
    db_session.add(signal)
    await db_session.commit()
    await db_session.refresh(signal)
    return signal


@pytest.fixture
def no_linkedin_lookup(monkeypatch):
    """Default the LinkedIn web-search to "found nobody" so tests that
    only exercise the Hunter/domain path don't make a real Anthropic call."""
    monkeypatch.setattr(
        "app.services.signal_enrichment._web_lookup",
        AsyncMock(return_value=None),
    )


async def test_signal_enrich_finds_contact_and_links_lead(
    client, db_session, monkeypatch, no_linkedin_lookup,
):
    signal = await _notification_only_signal(db_session)
    monkeypatch.setattr(
        "app.services.signal_enrichment.enrichment.resolve_contact",
        AsyncMock(return_value=ContactResult(
            status="resolved", domain="helpinghands.org",
            email="ED@HelpingHands.org", first_name="Dana",
            last_name="Reed", title="Executive Director", via="website",
        )),
    )
    resp = await client.post(f"/signals/{signal.id}/enrich")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["found"] is True
    assert body["lead_created"] is True
    assert body["already_had_contact"] is False
    assert body["email"] == "ed@helpinghands.org"   # canonicalised
    assert body["lead_id"]

    await db_session.refresh(signal)
    assert signal.lead_id is not None
    lead = await db_session.get(Lead, signal.lead_id)
    assert lead.campaign_id is None                  # autonomy boundary
    assert lead.email == "ed@helpinghands.org"
    assert lead.company == "Helping Hands"


async def test_signal_enrich_parses_org_name_from_summary(
    client, db_session, monkeypatch, no_linkedin_lookup,
):
    # No org_name in detail — must be parsed from the IRS-style summary.
    signal = await _notification_only_signal(
        db_session, detail={}, summary="New 501(c)(3): Tiny Org (PA) — IRS ruling 202405",
    )
    captured = {}

    async def _resolve(org):
        captured["org_name"] = org.org_name
        return ContactResult(
            status="resolved", domain="tinyorg.org", email="info@tinyorg.org",
        )

    monkeypatch.setattr(
        "app.services.signal_enrichment.enrichment.resolve_contact", _resolve,
    )
    resp = await client.post(f"/signals/{signal.id}/enrich")
    assert resp.status_code == 200, resp.text
    assert captured["org_name"] == "Tiny Org"
    assert resp.json()["generic"] is True   # info@ → generic mailbox


async def test_signal_enrich_no_contact_found(
    client, db_session, monkeypatch, no_linkedin_lookup,
):
    signal = await _notification_only_signal(db_session)
    monkeypatch.setattr(
        "app.services.signal_enrichment.enrichment.resolve_contact",
        AsyncMock(return_value=ContactResult(status="no_contact")),
    )
    resp = await client.post(f"/signals/{signal.id}/enrich")
    assert resp.status_code == 200, resp.text
    assert resp.json()["found"] is False
    await db_session.refresh(signal)
    assert signal.lead_id is None                    # still notification-only


async def test_signal_enrich_already_has_contact_skips_lookup(client, db_session, monkeypatch):
    signal, lead = await _signal_with_staged_lead(db_session)
    resolver = AsyncMock(return_value=None)
    web = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "app.services.signal_enrichment.enrichment.resolve_contact", resolver,
    )
    monkeypatch.setattr("app.services.signal_enrichment._web_lookup", web)
    resp = await client.post(f"/signals/{signal.id}/enrich")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["found"] is True
    assert body["already_had_contact"] is True
    assert body["lead_id"] == str(lead.id)
    resolver.assert_not_awaited()                    # no API spend
    web.assert_not_awaited()                         # no LinkedIn lookup either


async def test_signal_enrich_reuses_existing_lead_by_email(
    client, db_session, monkeypatch, no_linkedin_lookup,
):
    existing = Lead(campaign_id=None, email="ed@helpinghands.org", first_name="Dana")
    db_session.add(existing)
    await db_session.flush()
    signal = await _notification_only_signal(db_session)
    monkeypatch.setattr(
        "app.services.signal_enrichment.enrichment.resolve_contact",
        AsyncMock(return_value=ContactResult(
            status="resolved", domain="helpinghands.org",
            email="ed@helpinghands.org", first_name="Dana",
        )),
    )
    resp = await client.post(f"/signals/{signal.id}/enrich")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["found"] is True
    assert body["lead_created"] is False
    assert body["lead_id"] == str(existing.id)
    n = await db_session.scalar(
        select(func.count()).select_from(Lead).where(Lead.email == "ed@helpinghands.org")
    )
    assert n == 1                                    # no duplicate


async def test_signal_enrich_linkedin_name_drives_hunter_and_sets_profile(
    client, db_session, monkeypatch,
):
    """LinkedIn search finds a named decision-maker + profile; that name
    drives Hunter's Email Finder (no role-search fallback needed)."""
    signal = await _notification_only_signal(db_session)
    monkeypatch.setattr(
        "app.services.signal_enrichment._web_lookup",
        AsyncMock(return_value={
            "first_name": "Dana", "last_name": "Reed",
            "title": "Executive Director",
            "linkedin_url": "https://www.linkedin.com/in/dana-reed",
            "domain": "helpinghands.org",
        }),
    )
    finder = AsyncMock(return_value={
        "email": "dana@helpinghands.org", "first_name": "Dana",
        "last_name": "Reed", "title": "ED", "generic": False,
    })
    monkeypatch.setattr("app.services.signal_enrichment.hunter.find_email_hunter", finder)
    monkeypatch.setattr(
        "app.services.signal_enrichment.hunter.verify_email_hunter",
        AsyncMock(return_value={"deliverable": True}),
    )
    resolver = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "app.services.signal_enrichment.enrichment.resolve_contact", resolver,
    )

    resp = await client.post(f"/signals/{signal.id}/enrich")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["found"] is True
    assert body["email"] == "dana@helpinghands.org"
    assert body["linkedin_url"] == "https://www.linkedin.com/in/dana-reed"
    # Name-aware Hunter Finder was used; role-search fallback was not.
    assert finder.await_args.kwargs.get("full_name") == "Dana Reed"
    resolver.assert_not_awaited()

    await db_session.refresh(signal)
    lead = await db_session.get(Lead, signal.lead_id)
    assert lead.linkedin_url == "https://www.linkedin.com/in/dana-reed"


async def test_signal_enrich_linkedin_only_creates_lead_without_email(
    client, db_session, monkeypatch,
):
    """A LinkedIn profile is found but no email resolves — a campaign-less
    email-less lead is still staged + linked (for LinkedIn outreach)."""
    signal = await _notification_only_signal(db_session)
    monkeypatch.setattr(
        "app.services.signal_enrichment._web_lookup",
        AsyncMock(return_value={
            "first_name": "Dana", "last_name": "Reed", "title": "ED",
            "linkedin_url": "https://www.linkedin.com/in/dana-reed",
            "domain": "helpinghands.org",
        }),
    )
    monkeypatch.setattr(
        "app.services.signal_enrichment.hunter.find_email_hunter",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.signal_enrichment.enrichment.resolve_contact",
        AsyncMock(return_value=ContactResult(status="no_contact")),
    )
    resp = await client.post(f"/signals/{signal.id}/enrich")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["found"] is True
    assert body["has_email"] is False
    assert body["lead_created"] is True
    assert body["email"] is None
    assert body["linkedin_url"] == "https://www.linkedin.com/in/dana-reed"

    await db_session.refresh(signal)
    assert signal.lead_id is not None
    lead = await db_session.get(Lead, signal.lead_id)
    assert lead.email is None
    assert lead.campaign_id is None
    assert lead.linkedin_url == "https://www.linkedin.com/in/dana-reed"
    assert lead.first_name == "Dana"


async def test_signal_enrich_drops_non_profile_linkedin_url(db_session, monkeypatch):
    """A hallucinated / non-/in/ LinkedIn URL is discarded."""
    from app.services import signal_enrichment
    from app.services.funding_sources.base import DiscoveredOrg

    monkeypatch.setattr(
        "app.services.signal_enrichment._web_lookup",
        AsyncMock(return_value={
            "first_name": "X", "last_name": "Y",
            "linkedin_url": "https://www.linkedin.com/company/helping-hands",
            "domain": "",
        }),
    )
    org = DiscoveredOrg(
        signal_type="grant_awarded", summary="s", dedup_key="k",
        org_name="Helping Hands",
    )
    person = await signal_enrichment._find_linkedin_decision_maker(org)
    assert person["linkedin_url"] is None            # company URL dropped


async def test_signal_list_exposes_lead_has_email(client, db_session):
    """The feed flags whether a linked lead is emailable so the UI can
    show Draft & send vs View-lead."""
    emailable_lead = Lead(campaign_id=None, email="ed@helpinghands.org")
    li_only_lead = Lead(
        campaign_id=None, email=None,
        linkedin_url="https://www.linkedin.com/in/dana-reed",
    )
    db_session.add_all([emailable_lead, li_only_lead])
    await db_session.flush()
    s_email = ProspectSignal(
        watch_id=None, source="usaspending", signal_type="grant_awarded",
        summary="A won a grant", detail={}, lead_id=emailable_lead.id,
        dedup_key=f"grant_awarded:{uuid.uuid4().hex}",
    )
    s_li = ProspectSignal(
        watch_id=None, source="irs_bmf", signal_type="new_501c3",
        summary="New 501(c)(3): B", detail={}, lead_id=li_only_lead.id,
        dedup_key=f"new_501c3:{uuid.uuid4().hex}",
    )
    db_session.add_all([s_email, s_li])
    await db_session.commit()

    resp = await client.get("/signals")
    assert resp.status_code == 200, resp.text
    by_id = {i["id"]: i for i in resp.json()["items"]}
    assert by_id[str(s_email.id)]["lead_has_email"] is True
    assert by_id[str(s_li.id)]["lead_has_email"] is False


async def test_signal_list_includes_lead_contact_info(client, db_session):
    """The feed embeds the linked lead's contact basics (email, name,
    website, LinkedIn) so the detail modal can show what we found."""
    lead = Lead(
        campaign_id=None, email="dana@helpinghands.org",
        first_name="Dana", last_name="Reed", job_title="Executive Director",
        company="Helping Hands", company_website="https://helpinghands.org",
        linkedin_url="https://www.linkedin.com/in/dana-reed",
    )
    db_session.add(lead)
    await db_session.flush()
    sig = ProspectSignal(
        watch_id=None, source="usaspending", signal_type="grant_awarded",
        summary="Helping Hands won a grant", detail={"website": "helpinghands.org"},
        lead_id=lead.id, dedup_key=f"grant_awarded:{uuid.uuid4().hex}",
    )
    db_session.add(sig)
    await db_session.commit()

    resp = await client.get("/signals")
    assert resp.status_code == 200, resp.text
    item = {i["id"]: i for i in resp.json()["items"]}[str(sig.id)]
    ld = item["lead"]
    assert ld["email"] == "dana@helpinghands.org"
    assert ld["first_name"] == "Dana"
    assert ld["job_title"] == "Executive Director"
    assert ld["company_website"] == "https://helpinghands.org"
    assert ld["linkedin_url"] == "https://www.linkedin.com/in/dana-reed"


async def test_signal_list_lead_is_null_without_contact(client, db_session):
    sig = ProspectSignal(
        watch_id=None, source="irs_bmf", signal_type="new_501c3",
        summary="New 501(c)(3): C", detail={}, lead_id=None,
        dedup_key=f"new_501c3:{uuid.uuid4().hex}",
    )
    db_session.add(sig)
    await db_session.commit()
    resp = await client.get("/signals")
    item = {i["id"]: i for i in resp.json()["items"]}[str(sig.id)]
    assert item["lead"] is None


async def test_signal_enrich_unknown_404(client):
    resp = await client.post(f"/signals/{uuid.uuid4()}/enrich")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Cooperative stop flag (makes Stop abort an in-flight run mid-batch)
# ---------------------------------------------------------------------------


class _FakeFlagRedis:
    def __init__(self):
        self.store: dict[str, bytes] = {}

    def set(self, key, val, ex=None):
        self.store[key] = val if isinstance(val, bytes) else str(val).encode()

    def exists(self, key):
        return 1 if key in self.store else 0

    def delete(self, key):
        self.store.pop(key, None)


async def test_stage_all_aborts_when_stop_flag_set(db_session, monkeypatch):
    fake = _FakeFlagRedis()
    fake.set("funding:stop:usaspending", "1")
    monkeypatch.setattr(funding_signals, "_funding_redis", lambda: fake)
    staged = MagicMock()
    monkeypatch.setattr(funding_signals, "_stage_discovery_signal", staged)

    orgs = [_org(dedup=f"grant_awarded:S{i}") for i in range(5)]
    out = await funding_signals._stage_all(db_session, "usaspending", orgs)

    assert out["staged"] == 0
    staged.assert_not_called()          # bailed before processing any org


async def test_stop_funding_run_raises_stop_flag(monkeypatch):
    fake = _FakeFlagRedis()
    monkeypatch.setattr(funding_signals, "_funding_redis", lambda: fake)
    control = _FakeControl(_FakeInspect(active={}, reserved={}))
    monkeypatch.setattr(funding_signals.celery_app, "control", control)
    monkeypatch.setattr(
        funding_signals, "_purge_broker_messages",
        lambda name: {"queue": 0, "unacked": 0},
    )

    result = funding_signals.stop_funding_run("usaspending")
    assert result["stop_flagged"] is True
    assert fake.exists("funding:stop:usaspending")


async def test_poll_clears_stale_stop_flag_on_start(db_session, monkeypatch):
    fake = _FakeFlagRedis()
    fake.set("funding:stop:usaspending", "1")
    monkeypatch.setattr(funding_signals, "_funding_redis", lambda: fake)
    monkeypatch.setattr(funding_signals.settings, "USASPENDING_ENABLED", False)

    # Even a disabled (early-return) poll clears the stale flag first, so a
    # prior Stop can't wedge the next run.
    await _poll_usaspending_async()
    assert not fake.exists("funding:stop:usaspending")
