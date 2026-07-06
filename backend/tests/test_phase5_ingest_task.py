"""Phase 5: run_campaign_research_async enqueues research_lead jobs."""
import uuid
from datetime import time
from unittest.mock import patch

from app.models import Campaign, Lead, ResearchStatus
from app.workers.ingest import run_campaign_research_async


async def _make_campaign(db_session) -> Campaign:
    c = Campaign(
        name="Test",
        goal="Test",
        tone="Friendly",
        sender_name="A",
        sender_email="a@x.com",
        sample_count=2,
        schedule_days=[0, 1, 2, 3, 4],
        schedule_time_start=time(9, 0),
        schedule_time_end=time(17, 0),
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    return c


async def test_run_campaign_research_enqueues_each_pending_lead(db_session):
    c = await _make_campaign(db_session)
    sample_ids: list[uuid.UUID] = []
    other_ids: list[uuid.UUID] = []
    for i in range(5):
        lead = Lead(
            campaign_id=c.id,
            email=f"l{i}@x.com",
            is_sample=(i < 2),
        )
        db_session.add(lead)
        await db_session.flush()
        (sample_ids if lead.is_sample else other_ids).append(lead.id)
    await db_session.commit()

    with patch("app.workers.ingest.research_lead.delay") as enqueue:
        result = await run_campaign_research_async(str(c.id))

    assert result == {"samples_enqueued": 2, "others_enqueued": 3}

    # All five leads enqueued.
    assert enqueue.call_count == 5
    called_args = [call.args[0] for call in enqueue.call_args_list]

    # Samples enqueued before non-samples.
    sample_calls = {str(i) for i in sample_ids}
    other_calls = {str(i) for i in other_ids}
    first_two = set(called_args[:2])
    last_three = set(called_args[2:])
    assert first_two == sample_calls
    assert last_three == other_calls


async def test_run_campaign_research_skips_already_processed_leads(db_session):
    c = await _make_campaign(db_session)
    db_session.add(Lead(campaign_id=c.id, email="done@x.com", research_status=ResearchStatus.DONE))
    db_session.add(Lead(campaign_id=c.id, email="failed@x.com", research_status=ResearchStatus.FAILED))
    db_session.add(Lead(campaign_id=c.id, email="pending@x.com", research_status=ResearchStatus.PENDING))
    await db_session.commit()

    with patch("app.workers.ingest.research_lead.delay") as enqueue:
        result = await run_campaign_research_async(str(c.id))

    assert result == {"samples_enqueued": 0, "others_enqueued": 1}
    assert enqueue.call_count == 1


async def test_run_campaign_research_no_pending_returns_zero(db_session):
    c = await _make_campaign(db_session)
    with patch("app.workers.ingest.research_lead.delay") as enqueue:
        result = await run_campaign_research_async(str(c.id))
    assert result == {"samples_enqueued": 0, "others_enqueued": 0}
    enqueue.assert_not_called()
