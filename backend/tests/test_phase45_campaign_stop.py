"""Phase 4.5: campaign Stop button — kill research + compose mid-flight."""
from __future__ import annotations

import json
import base64
import uuid
from datetime import time
from typing import Any
from unittest.mock import patch

import pytest

from app.models import (
    Campaign,
    CampaignStatus,
    ComposeStatus,
    Lead,
    ResearchStatus,
    SendStatus,
)
from app.services import campaign_stop


# Same _Fake* shape as the funding-stop tests use so the patterns match.


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


async def _make_campaign(db_session, *, status=CampaignStatus.RUNNING):
    c = Campaign(
        name="Stop test",
        goal="Demo",
        tone="Direct",
        sender_name="A",
        sender_email="a@x.com",
        sample_count=2,
        schedule_days=[0, 1, 2, 3, 4],
        schedule_time_start=time(9, 0),
        schedule_time_end=time(17, 0),
        status=status,
        min_delay_seconds=0,
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    return c


async def _make_lead(db_session, c, *, email, research_status=ResearchStatus.PENDING,
                     compose_status=ComposeStatus.PENDING):
    lead = Lead(
        campaign_id=c.id, email=email,
        research_status=research_status, compose_status=compose_status,
    )
    db_session.add(lead)
    await db_session.commit()
    await db_session.refresh(lead)
    return lead


def _stub_redis(monkeypatch, store: dict):
    """Replace ``campaign_stop._campaign_redis`` with a tiny fake that
    just records SETEX/DELETE/EXISTS — enough for the service contract."""
    class _R:
        def set(self, k, v, ex=None):
            store[k] = v
        def exists(self, k):
            return 1 if k in store else 0
        def delete(self, k):
            store.pop(k, None)
    monkeypatch.setattr(campaign_stop, "_campaign_redis", lambda: _R())


# ---------- request_stop / clear_stop / stop_requested ----------


def test_request_stop_sets_ttl_key(monkeypatch):
    store: dict = {}
    _stub_redis(monkeypatch, store)
    cid = uuid.uuid4()
    assert campaign_stop.request_stop(cid) is True
    assert store == {f"campaign:stop:{cid}": "1"}


def test_clear_stop_removes_key(monkeypatch):
    store: dict = {f"campaign:stop:abc": "1"}
    _stub_redis(monkeypatch, store)
    campaign_stop.clear_stop("abc")
    assert store == {}


def test_stop_requested_probes_key(monkeypatch):
    store: dict = {}
    _stub_redis(monkeypatch, store)
    cid = uuid.uuid4()
    r = campaign_stop._campaign_redis()
    assert campaign_stop.stop_requested(r, cid) is False
    campaign_stop.request_stop(cid)
    assert campaign_stop.stop_requested(r, cid) is True


# ---------- _msg_matches ----------


def _msg(task: str, lead_id: str) -> bytes:
    body = base64.b64encode(json.dumps([[lead_id], {}, {}]).encode()).decode()
    return json.dumps({"headers": {"task": task}, "body": body}).encode()


def test_msg_matches_research_for_target_lead():
    lid = str(uuid.uuid4())
    assert campaign_stop._msg_matches(_msg("research.research_lead", lid), {lid}) is True


def test_msg_matches_compose_for_target_lead():
    lid = str(uuid.uuid4())
    assert campaign_stop._msg_matches(_msg("compose.compose_lead", lid), {lid}) is True


def test_msg_matches_send_for_target_lead():
    lid = str(uuid.uuid4())
    assert campaign_stop._msg_matches(_msg("send.send_lead", lid), {lid}) is True


def test_msg_matches_ignores_other_task_names():
    lid = str(uuid.uuid4())
    # The needle task is in our set but the task name is NOT stoppable —
    # belt-and-suspenders against grabbing unrelated tasks just because
    # their args happen to be a UUID.
    assert campaign_stop._msg_matches(_msg("sequencer.advance", lid), {lid}) is False


def test_msg_matches_ignores_other_lead_ids():
    targetted = {str(uuid.uuid4())}
    other = str(uuid.uuid4())
    assert campaign_stop._msg_matches(_msg("research.research_lead", other), targetted) is False


# ---------- stop_campaign_pipeline ----------


async def test_stop_pipeline_pauses_running_campaign(db_session, monkeypatch):
    _stub_redis(monkeypatch, {})
    monkeypatch.setattr(
        campaign_stop, "_purge_broker_for_leads", lambda _: {"queue": 0, "unacked": 0},
    )
    monkeypatch.setattr(
        campaign_stop.celery_app, "control",
        _FakeControl(_FakeInspect(active={}, reserved={})),
    )

    c = await _make_campaign(db_session, status=CampaignStatus.RUNNING)
    result = await campaign_stop.stop_campaign_pipeline(db_session, c.id)

    await db_session.refresh(c)
    assert c.status == CampaignStatus.PAUSED
    assert c.auto_pause_reason == "user_stopped"
    assert c.auto_paused_at is not None
    assert result["was_running"] is True
    assert result["stop_flagged"] is True


async def test_stop_pipeline_resets_running_lead_statuses(db_session, monkeypatch):
    _stub_redis(monkeypatch, {})
    monkeypatch.setattr(
        campaign_stop, "_purge_broker_for_leads", lambda _: {"queue": 0, "unacked": 0},
    )
    monkeypatch.setattr(
        campaign_stop.celery_app, "control",
        _FakeControl(_FakeInspect(active={}, reserved={})),
    )

    c = await _make_campaign(db_session)
    # Mix: 2 RUNNING research, 3 RUNNING compose, plus 1 already done.
    r1 = await _make_lead(db_session, c, email="r1@x.com", research_status=ResearchStatus.RUNNING)
    r2 = await _make_lead(db_session, c, email="r2@x.com", research_status=ResearchStatus.RUNNING)
    cm1 = await _make_lead(db_session, c, email="c1@x.com", research_status=ResearchStatus.DONE,
                           compose_status=ComposeStatus.RUNNING)
    cm2 = await _make_lead(db_session, c, email="c2@x.com", research_status=ResearchStatus.DONE,
                           compose_status=ComposeStatus.RUNNING)
    cm3 = await _make_lead(db_session, c, email="c3@x.com", research_status=ResearchStatus.DONE,
                           compose_status=ComposeStatus.RUNNING)
    done = await _make_lead(db_session, c, email="d@x.com", research_status=ResearchStatus.DONE,
                            compose_status=ComposeStatus.DONE)

    result = await campaign_stop.stop_campaign_pipeline(db_session, c.id)

    assert result["running_research_reset"] == 2
    assert result["running_compose_reset"] == 3

    for lead, expected in (
        (r1, ResearchStatus.PENDING), (r2, ResearchStatus.PENDING),
    ):
        await db_session.refresh(lead)
        assert lead.research_status == expected
    for lead in (cm1, cm2, cm3):
        await db_session.refresh(lead)
        assert lead.compose_status == ComposeStatus.PENDING
    await db_session.refresh(done)
    assert done.compose_status == ComposeStatus.DONE  # done is untouched


async def test_stop_pipeline_revokes_only_target_campaigns_tasks(db_session, monkeypatch):
    """Two campaigns share the worker.  Stop on campaign A must revoke
    only A's lead tasks; B's tasks are left alone."""
    _stub_redis(monkeypatch, {})
    monkeypatch.setattr(
        campaign_stop, "_purge_broker_for_leads", lambda _: {"queue": 0, "unacked": 0},
    )

    a = await _make_campaign(db_session)
    b = await _make_campaign(db_session)
    a_lead = await _make_lead(db_session, a, email="a@x.com", research_status=ResearchStatus.RUNNING)
    b_lead = await _make_lead(db_session, b, email="b@x.com", research_status=ResearchStatus.RUNNING)

    control = _FakeControl(_FakeInspect(
        active={"w1": [
            {"id": "tid-a-research", "name": "research.research_lead", "args": [str(a_lead.id)]},
            {"id": "tid-a-compose", "name": "compose.compose_lead", "args": [str(a_lead.id)]},
            {"id": "tid-b-research", "name": "research.research_lead", "args": [str(b_lead.id)]},
            # An unrelated kind for a's lead — not in _STOPPABLE_TASKS.
            {"id": "tid-unrelated", "name": "sequencer.advance", "args": [str(a_lead.id)]},
        ]},
        reserved={"w1": [
            {"id": "tid-a-reserved-send", "name": "send.send_lead", "args": [str(a_lead.id)]},
        ]},
    ))
    monkeypatch.setattr(campaign_stop.celery_app, "control", control)

    result = await campaign_stop.stop_campaign_pipeline(db_session, a.id)

    revoked_ids = {tid for tid, _, _ in control.revoked}
    assert revoked_ids == {"tid-a-research", "tid-a-compose", "tid-a-reserved-send"}
    assert all(term is True for _, term, _ in control.revoked)
    assert result["terminated"] == 3
    assert result["terminated_by_kind"] == {"research": 1, "compose": 1, "send": 1}

    # B's lead is untouched (still RUNNING, no reset).
    await db_session.refresh(b_lead)
    assert b_lead.research_status == ResearchStatus.RUNNING


async def test_stop_pipeline_returns_purge_counts(db_session, monkeypatch):
    _stub_redis(monkeypatch, {})
    monkeypatch.setattr(
        campaign_stop, "_purge_broker_for_leads", lambda _: {"queue": 7, "unacked": 42},
    )
    monkeypatch.setattr(
        campaign_stop.celery_app, "control",
        _FakeControl(_FakeInspect(active={}, reserved={})),
    )
    c = await _make_campaign(db_session)
    result = await campaign_stop.stop_campaign_pipeline(db_session, c.id)
    assert result["purged_queued"] == 7
    assert result["purged_unacked"] == 42


async def test_stop_pipeline_idempotent_on_paused_already(db_session, monkeypatch):
    _stub_redis(monkeypatch, {})
    monkeypatch.setattr(
        campaign_stop, "_purge_broker_for_leads", lambda _: {"queue": 0, "unacked": 0},
    )
    monkeypatch.setattr(
        campaign_stop.celery_app, "control",
        _FakeControl(_FakeInspect(active={}, reserved={})),
    )
    c = await _make_campaign(db_session, status=CampaignStatus.PAUSED)
    result = await campaign_stop.stop_campaign_pipeline(db_session, c.id)
    assert result["was_running"] is False
    await db_session.refresh(c)
    assert c.status == CampaignStatus.PAUSED


async def test_stop_pipeline_not_found(db_session):
    result = await campaign_stop.stop_campaign_pipeline(db_session, uuid.uuid4())
    assert result == {"error": "not_found"}


# ---------- endpoint ----------


async def test_stop_endpoint_returns_breakdown(client, db_session, monkeypatch):
    _stub_redis(monkeypatch, {})
    monkeypatch.setattr(
        campaign_stop, "_purge_broker_for_leads", lambda _: {"queue": 0, "unacked": 0},
    )
    monkeypatch.setattr(
        campaign_stop.celery_app, "control",
        _FakeControl(_FakeInspect(active={}, reserved={})),
    )

    c = await _make_campaign(db_session)
    resp = await client.post(f"/campaigns/{c.id}/stop-pipeline")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["campaign_id"] == str(c.id)
    assert body["stop_flagged"] is True
    assert body["terminated"] == 0  # no active tasks in the fake


async def test_stop_endpoint_completed_409(client, db_session):
    c = await _make_campaign(db_session, status=CampaignStatus.COMPLETE)
    resp = await client.post(f"/campaigns/{c.id}/stop-pipeline")
    assert resp.status_code == 409


async def test_stop_endpoint_unknown_404(client):
    resp = await client.post(f"/campaigns/{uuid.uuid4()}/stop-pipeline")
    assert resp.status_code == 404


# ---------- resume clears the flag ----------


async def test_resume_clears_stop_flag(client, db_session, monkeypatch):
    """After Stop the campaign is paused + flag is set.  Resume must
    clear the flag so newly-dispatched research/compose tasks aren't
    immediately bailed by the cooperative check."""
    store: dict = {}
    _stub_redis(monkeypatch, store)
    monkeypatch.setattr(
        campaign_stop, "_purge_broker_for_leads", lambda _: {"queue": 0, "unacked": 0},
    )
    monkeypatch.setattr(
        campaign_stop.celery_app, "control",
        _FakeControl(_FakeInspect(active={}, reserved={})),
    )
    c = await _make_campaign(db_session)

    # Stop sets the flag.
    await client.post(f"/campaigns/{c.id}/stop-pipeline")
    assert f"campaign:stop:{c.id}" in store

    # Resume clears it.
    with patch("app.workers.send.send_lead.apply_async"), \
         patch("app.workers.send.send_lead.delay"):
        resp = await client.post(f"/campaigns/{c.id}/resume")
    assert resp.status_code == 200, resp.text
    assert f"campaign:stop:{c.id}" not in store
