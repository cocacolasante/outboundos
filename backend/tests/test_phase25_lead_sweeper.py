"""Tests for lead_sweeper.sweep_stale.

The sweeper flips Lead rows stuck in RUNNING (compose or research) back
to PENDING + re-enqueues the corresponding Celery worker.  Stuck =
``updated_at < now - STALE_AFTER_MINUTES``.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pytest
from sqlalchemy import select, update

from app.models import (
    Campaign,
    ComposeStatus,
    Lead,
    ResearchStatus,
)
from app.workers import lead_sweeper


async def _make_campaign(db_session) -> Campaign:
    c = Campaign(
        name="P25", goal="g", tone="t",
        sender_name="s", sender_email="s@x.com",
        sample_count=1,
        schedule_days=[0, 1, 2, 3, 4],
        schedule_time_start=time(9, 0),
        schedule_time_end=time(17, 0),
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    return c


async def _force_updated_at(db_session, lead_id, when: datetime) -> None:
    """Bypass SQLAlchemy's onupdate=now() by issuing a direct UPDATE."""
    await db_session.execute(
        update(Lead).where(Lead.id == lead_id).values(updated_at=when)
    )
    await db_session.commit()


async def test_revives_stale_compose_running(db_session, monkeypatch):
    c = await _make_campaign(db_session)
    lead = Lead(campaign_id=c.id, email="a@x.com", compose_status=ComposeStatus.RUNNING)
    db_session.add(lead)
    await db_session.commit()
    await db_session.refresh(lead)
    await _force_updated_at(
        db_session, lead.id,
        datetime.now(timezone.utc) - timedelta(minutes=lead_sweeper.STALE_AFTER_MINUTES + 1),
    )

    enqueued: list[str] = []
    monkeypatch.setattr(
        "app.workers.compose.compose_lead.delay",
        lambda lid: enqueued.append(("compose", str(lid))),
    )

    counts = await lead_sweeper._sweep_async()
    assert counts["compose_revived"] == 1

    refreshed = await db_session.scalar(select(Lead).where(Lead.id == lead.id))
    await db_session.refresh(refreshed)
    assert refreshed.compose_status == ComposeStatus.PENDING
    assert enqueued == [("compose", str(lead.id))]


async def test_revives_stale_research_running(db_session, monkeypatch):
    c = await _make_campaign(db_session)
    lead = Lead(campaign_id=c.id, email="b@x.com", research_status=ResearchStatus.RUNNING)
    db_session.add(lead)
    await db_session.commit()
    await db_session.refresh(lead)
    await _force_updated_at(
        db_session, lead.id,
        datetime.now(timezone.utc) - timedelta(minutes=lead_sweeper.STALE_AFTER_MINUTES + 1),
    )

    enqueued: list[str] = []
    monkeypatch.setattr(
        "app.workers.research.research_lead.delay",
        lambda lid: enqueued.append(("research", str(lid))),
    )

    counts = await lead_sweeper._sweep_async()
    assert counts["research_revived"] == 1

    refreshed = await db_session.scalar(select(Lead).where(Lead.id == lead.id))
    await db_session.refresh(refreshed)
    assert refreshed.research_status == ResearchStatus.PENDING
    assert enqueued == [("research", str(lead.id))]


async def test_ignores_fresh_running(db_session, monkeypatch):
    """A lead that JUST entered RUNNING (within the threshold) is NOT
    swept — the worker is still legitimately processing it."""
    c = await _make_campaign(db_session)
    lead = Lead(campaign_id=c.id, email="c@x.com", compose_status=ComposeStatus.RUNNING)
    db_session.add(lead)
    await db_session.commit()

    enqueued: list[str] = []
    monkeypatch.setattr(
        "app.workers.compose.compose_lead.delay",
        lambda lid: enqueued.append(str(lid)),
    )

    counts = await lead_sweeper._sweep_async()
    assert counts["compose_revived"] == 0
    assert enqueued == []


async def test_does_not_touch_done_or_failed(db_session, monkeypatch):
    c = await _make_campaign(db_session)
    done_lead = Lead(campaign_id=c.id, email="d@x.com", compose_status=ComposeStatus.DONE)
    failed_lead = Lead(campaign_id=c.id, email="e@x.com", compose_status=ComposeStatus.FAILED)
    db_session.add_all([done_lead, failed_lead])
    await db_session.commit()
    # Make both old; even so they should not be swept.
    long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    await db_session.refresh(done_lead)
    await db_session.refresh(failed_lead)
    await _force_updated_at(db_session, done_lead.id, long_ago)
    await _force_updated_at(db_session, failed_lead.id, long_ago)

    enqueued: list[str] = []
    monkeypatch.setattr(
        "app.workers.compose.compose_lead.delay",
        lambda lid: enqueued.append(str(lid)),
    )

    counts = await lead_sweeper._sweep_async()
    assert counts["compose_revived"] == 0
    assert enqueued == []

    # And the statuses are unchanged.
    await db_session.refresh(done_lead)
    await db_session.refresh(failed_lead)
    assert done_lead.compose_status == ComposeStatus.DONE
    assert failed_lead.compose_status == ComposeStatus.FAILED
