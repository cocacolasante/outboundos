"""Phase 39 — intent/trigger prospecting (Feature C).

Detection contracts:
- Job change emits exactly one signal on a title change, none when
  unchanged, none on first sighting (baseline seeding); dedup holds
  across re-runs.
- Funding/hiring parse mocked Apollo/web responses into structured
  detail; the hiring role-set is the change anchor.

Worker policy:
- run_watch re-surfaces a tracked lead (CRM task + notification);
- a cold target WITH an email gets a campaign-less CRM lead;
- nothing is ever added to a sending campaign.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models import (
    CrmActivity,
    CrmActivityType,
    Lead,
    Notification,
    NotificationKind,
    ProspectSignal,
    SignalWatch,
    SignalWatchStatus,
    SignalWatchType,
    SocialSearchFrequency,
)
from app.services import signal_detection
from app.workers.signals import run_watch_session

pytestmark = pytest.mark.asyncio


async def _make_lead(db_session, **kw) -> Lead:
    defaults = dict(
        campaign_id=None, email=f"l{uuid.uuid4().hex[:6]}@x.com",
        first_name="Jane", last_name="Doe",
        company="Acme", job_title="Director of IT",
    )
    defaults.update(kw)
    lead = Lead(**defaults)
    db_session.add(lead)
    await db_session.commit()
    await db_session.refresh(lead)
    return lead


async def _make_watch(db_session, **kw) -> SignalWatch:
    defaults = dict(
        watch_type=SignalWatchType.JOB_CHANGE,
        frequency=SocialSearchFrequency.DAILY,
        status=SignalWatchStatus.ACTIVE,
    )
    defaults.update(kw)
    w = SignalWatch(**defaults)
    db_session.add(w)
    await db_session.commit()
    await db_session.refresh(w)
    return w


# --------------------------------------------------------------------------
# job change detection
# --------------------------------------------------------------------------


async def test_job_change_emits_once_on_title_change(db_session):
    lead = await _make_lead(db_session)
    watch = await _make_watch(db_session, lead_id=lead.id)

    with patch(
        "app.services.signal_detection.apollo.enrich_lead_apollo",
        new=AsyncMock(return_value={"job_title": "VP of Engineering", "seniority": "vp"}),
    ):
        signals, seen = await signal_detection.detect_for_watch(db_session, watch)

    assert len(signals) == 1
    sig = signals[0]
    assert sig.signal_type == "job_change"
    assert "Director of IT" in sig.summary and "VP of Engineering" in sig.summary
    assert sig.detail["old_title"] == "Director of IT"
    assert sig.detail["new_title"] == "VP of Engineering"
    assert seen["job_title"] == "VP of Engineering"


async def test_job_change_silent_when_title_unchanged(db_session):
    lead = await _make_lead(db_session)
    watch = await _make_watch(db_session, lead_id=lead.id)
    with patch(
        "app.services.signal_detection.apollo.enrich_lead_apollo",
        new=AsyncMock(return_value={"job_title": "Director of IT"}),
    ):
        signals, seen = await signal_detection.detect_for_watch(db_session, watch)
    assert signals == []
    # Baseline still refreshed.
    assert seen["job_title"] == "Director of IT"


async def test_job_change_first_sighting_seeds_baseline_only(db_session):
    """A cold-target watch with no known title: the first Apollo result
    seeds last_seen without emitting (we can't diff against nothing)."""
    watch = await _make_watch(
        db_session, lead_id=None, person_name="Sam Cold",
        company="ColdCo", email="sam@coldco.com",
    )
    with patch(
        "app.services.signal_detection.apollo.enrich_lead_apollo",
        new=AsyncMock(return_value={"job_title": "CTO"}),
    ):
        signals, seen = await signal_detection.detect_for_watch(db_session, watch)
    assert signals == []
    assert seen["job_title"] == "CTO"


# --------------------------------------------------------------------------
# funding + hiring detection (web mocked)
# --------------------------------------------------------------------------


async def test_funding_change_emits_with_structured_detail(db_session):
    lead = await _make_lead(db_session)
    watch = await _make_watch(
        db_session, lead_id=lead.id, watch_type=SignalWatchType.FUNDING,
        last_seen={"funding_stage": "seed"},
    )
    with patch(
        "app.services.signal_detection.apollo.enrich_lead_apollo",
        new=AsyncMock(return_value={"company_funding_stage": "series_b"}),
    ):
        signals, seen = await signal_detection.detect_for_watch(db_session, watch)
    assert len(signals) == 1
    assert signals[0].signal_type == "funding"
    assert signals[0].detail["old_stage"] == "seed"
    assert signals[0].detail["new_stage"] == "series_b"
    assert seen["funding_stage"] == "series_b"


async def test_funding_web_fallback_when_apollo_dark(db_session):
    watch = await _make_watch(
        db_session, watch_type=SignalWatchType.FUNDING,
        company="Acme", person_name="J D",
        last_seen={"funding_stage": "seed"},
    )
    with patch(
        "app.services.signal_detection.apollo.enrich_lead_apollo",
        new=AsyncMock(return_value={}),
    ), patch(
        "app.services.signal_detection._web_lookup",
        new=AsyncMock(return_value={
            "funding_stage": "series_a", "amount": "$12M",
            "source_url": "https://news.example/a", "_cost_usd": 0.01,
        }),
    ):
        signals, seen = await signal_detection.detect_for_watch(db_session, watch)
    assert len(signals) == 1
    assert signals[0].detail["amount"] == "$12M"
    assert signals[0].detail["source_url"] == "https://news.example/a"


async def test_hiring_emits_on_role_set_change_only(db_session):
    watch = await _make_watch(
        db_session, watch_type=SignalWatchType.HIRING, company="Acme",
        last_seen={"hiring_roles": ["IT Manager"]},
    )
    web = AsyncMock(return_value={
        "hiring": True,
        "roles": ["VP Sales", "IT Manager"],
        "source_url": "https://acme.example/careers",
    })
    with patch("app.services.signal_detection._web_lookup", new=web):
        signals, seen = await signal_detection.detect_for_watch(db_session, watch)
    assert len(signals) == 1
    assert sorted(signals[0].detail["roles"]) == ["IT Manager", "VP Sales"]
    assert seen["hiring_roles"] == ["IT Manager", "VP Sales"]

    # Same role set on the next poll → silent.
    watch.last_seen = {"hiring_roles": ["IT Manager", "VP Sales"]}
    with patch("app.services.signal_detection._web_lookup", new=web):
        signals2, _ = await signal_detection.detect_for_watch(db_session, watch)
    assert signals2 == []


# --------------------------------------------------------------------------
# run_watch policy
# --------------------------------------------------------------------------


async def test_run_watch_resurfaces_tracked_lead(db_session):
    lead = await _make_lead(db_session)
    watch = await _make_watch(db_session, lead_id=lead.id)

    with patch(
        "app.services.signal_detection.apollo.enrich_lead_apollo",
        new=AsyncMock(return_value={"job_title": "VP of Engineering"}),
    ):
        result = await run_watch_session(db_session, watch.id)
        await db_session.commit()

    assert result["new_signals"] == 1
    assert result["leads_created"] == 0

    # CRM reach-out task on the lead.
    task = await db_session.scalar(select(CrmActivity).where(
        CrmActivity.lead_id == lead.id,
        CrmActivity.activity_type == CrmActivityType.TASK,
    ))
    assert task is not None
    assert task.subject.startswith("Reach out —")
    assert task.is_agent_generated is True

    # Owner notification.
    notif = await db_session.scalar(select(Notification).where(
        Notification.kind == NotificationKind.PROSPECT_SIGNAL,
    ))
    assert notif is not None

    # Baseline merged + schedule advanced.
    await db_session.refresh(watch)
    assert watch.last_seen["job_title"] == "VP of Engineering"
    assert watch.next_run_at is not None

    # Re-run: dedup holds — no second signal, no second task.
    watch.last_seen = {}  # even if the baseline were lost...
    await db_session.commit()
    with patch(
        "app.services.signal_detection.apollo.enrich_lead_apollo",
        new=AsyncMock(return_value={"job_title": "VP of Engineering"}),
    ):
        # Restore old title so the detector would re-emit the same change.
        lead.job_title = "Director of IT"
        await db_session.commit()
        result2 = await run_watch_session(db_session, watch.id)
        await db_session.commit()
    assert result2["new_signals"] == 0
    sig_count = (await db_session.execute(select(ProspectSignal))).scalars().all()
    assert len(sig_count) == 1


async def test_run_watch_cold_target_creates_campaignless_lead(db_session):
    watch = await _make_watch(
        db_session, watch_type=SignalWatchType.FUNDING,
        company="ColdCo", person_name="Sam Cold", email="sam@coldco.com",
        last_seen={"funding_stage": "seed"},
    )
    with patch(
        "app.services.signal_detection.apollo.enrich_lead_apollo",
        new=AsyncMock(return_value={"company_funding_stage": "series_a"}),
    ):
        result = await run_watch_session(db_session, watch.id)
        await db_session.commit()

    assert result["new_signals"] == 1
    assert result["leads_created"] == 1

    lead = await db_session.scalar(select(Lead).where(Lead.email == "sam@coldco.com"))
    assert lead is not None
    assert lead.campaign_id is None        # NEVER auto-added to a campaign
    assert lead.first_name == "Sam"
    assert lead.company == "ColdCo"

    signal = await db_session.scalar(select(ProspectSignal))
    assert signal.lead_id == lead.id


async def test_run_watch_cold_target_without_email_notifies_only(db_session):
    watch = await _make_watch(
        db_session, watch_type=SignalWatchType.HIRING, company="NoMailCo",
        last_seen={"hiring_roles": []},
    )
    with patch(
        "app.services.signal_detection._web_lookup",
        new=AsyncMock(return_value={"hiring": True, "roles": ["VP Sales"]}),
    ):
        result = await run_watch_session(db_session, watch.id)
        await db_session.commit()

    assert result["new_signals"] == 1
    assert result["leads_created"] == 0
    leads = (await db_session.execute(select(Lead))).scalars().all()
    assert leads == []
    assert (await db_session.scalar(select(Notification))) is not None


async def test_run_watch_skips_paused(db_session):
    watch = await _make_watch(db_session, status=SignalWatchStatus.PAUSED, company="X")
    result = await run_watch_session(db_session, watch.id)
    assert result["status"] == "skipped"


# --------------------------------------------------------------------------
# router
# --------------------------------------------------------------------------


async def test_watch_crud_and_signal_feed(client, db_session):
    # Create a cold-target watch.
    resp = await client.post("/signals/watches", json={
        "watch_type": "hiring", "company": "Acme", "frequency": "daily",
    })
    assert resp.status_code == 201, resp.text
    wid = resp.json()["id"]
    assert resp.json()["next_run_at"] is not None  # scheduled immediately

    # Reject a target-less watch.
    resp = await client.post("/signals/watches", json={"watch_type": "funding"})
    assert resp.status_code == 422

    # Seed a signal + feed reads it; action flips status.
    db_session.add(ProspectSignal(
        watch_id=uuid.UUID(wid), signal_type="hiring",
        summary="Acme is hiring: VP Sales", dedup_key="hiring:test:1",
    ))
    await db_session.commit()

    resp = await client.get("/signals?status=new")
    assert resp.json()["total"] == 1
    sid = resp.json()["items"][0]["id"]

    resp = await client.post(f"/signals/{sid}/action")
    assert resp.json()["status"] == "actioned"
    resp = await client.get("/signals?status=new")
    assert resp.json()["total"] == 0

    # Pause + delete.
    resp = await client.patch(f"/signals/watches/{wid}", json={"status": "paused"})
    assert resp.json()["status"] == "paused"
    resp = await client.delete(f"/signals/watches/{wid}")
    assert resp.status_code == 204


# --------------------------------------------------------------------------
# creation validation + dedup + bulk (Signals build-out)
# --------------------------------------------------------------------------


async def test_watch_required_fields_per_type(client):
    # job_change without email → 422 with a pointed message.
    resp = await client.post("/signals/watches", json={
        "watch_type": "job_change", "person_name": "Jane Doe", "company": "Acme",
    })
    assert resp.status_code == 422
    assert "email" in str(resp.json()["detail"]).lower()

    # funding without company → 422.
    resp = await client.post("/signals/watches", json={
        "watch_type": "funding", "person_name": "Jane Doe",
    })
    assert resp.status_code == 422
    assert "company" in str(resp.json()["detail"]).lower()

    # custom with neither email nor company → 422.
    resp = await client.post("/signals/watches", json={
        "watch_type": "custom", "person_name": "Jane Doe",
    })
    assert resp.status_code == 422

    # job_change WITH an email → 201.
    resp = await client.post("/signals/watches", json={
        "watch_type": "job_change", "person_name": "Jane Doe",
        "email": "jane@acme.com",
    })
    assert resp.status_code == 201, resp.text


async def test_watch_duplicate_target_409(client, db_session):
    lead = await _make_lead(db_session)
    body = {"watch_type": "job_change", "lead_id": str(lead.id)}
    resp = await client.post("/signals/watches", json=body)
    assert resp.status_code == 201
    resp = await client.post("/signals/watches", json=body)
    assert resp.status_code == 409

    # Same company, same type → 409 too (case-insensitive).
    resp = await client.post("/signals/watches", json={
        "watch_type": "hiring", "company": "Acme Corp",
    })
    assert resp.status_code == 201
    resp = await client.post("/signals/watches", json={
        "watch_type": "hiring", "company": "acme corp",
    })
    assert resp.status_code == 409
    # …but a DIFFERENT type on the same company is fine.
    resp = await client.post("/signals/watches", json={
        "watch_type": "funding", "company": "Acme Corp",
    })
    assert resp.status_code == 201


async def test_watch_job_change_on_emailless_opp_422(client, db_session):
    from app.models import Opportunity

    opp = Opportunity(name="No-mail deal", company="Acme")
    db_session.add(opp)
    await db_session.commit()
    resp = await client.post("/signals/watches", json={
        "watch_type": "job_change", "opportunity_id": str(opp.id),
    })
    assert resp.status_code == 422
    assert "email" in resp.json()["detail"].lower()


async def test_bulk_company_watches(client, db_session):
    # Pre-existing active hiring watch on Beta — must be skipped.
    db_session.add(SignalWatch(
        watch_type=SignalWatchType.HIRING, company="Beta Inc",
        frequency=SocialSearchFrequency.DAILY,
    ))
    await db_session.commit()

    resp = await client.post("/signals/watches/bulk", json={
        "watch_type": "hiring",
        "companies": ["Acme Corp", "beta inc", "Gamma LLC", "  ", "Acme Corp"],
        "frequency": "daily",
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["created"] == 2          # Acme + Gamma (Beta dup, blank + repeat dropped)
    assert body["skipped_duplicate"] == 1
    assert len(body["watch_ids"]) == 2

    watches = (await db_session.execute(select(SignalWatch))).scalars().all()
    companies = sorted(w.company for w in watches)
    assert companies == ["Acme Corp", "Beta Inc", "Gamma LLC"]
    # All scheduled for their first run.
    new_ones = [w for w in watches if w.company != "Beta Inc"]
    assert all(w.next_run_at is not None for w in new_ones)


async def test_bulk_rejects_job_change(client):
    resp = await client.post("/signals/watches/bulk", json={
        "watch_type": "job_change", "companies": ["Acme"],
    })
    assert resp.status_code == 422
