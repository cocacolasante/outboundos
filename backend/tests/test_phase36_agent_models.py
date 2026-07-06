"""Phase 36 — Agent data layer (migration 0027).

Covers:
1. AgentSettings singleton bootstrap (created with defaults on first
   read, same row returned on the second).
2. Notification dedup_key uniqueness (the idempotency anchor).
3. AgentAction audit rows persist with JSONB detail + cost.
4. CrmActivity's new agent columns (reminder_sent_at, sentiment,
   is_agent_generated) default correctly.
5. Config: the agent settings exist with documented defaults.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import (
    AgentAction,
    AgentActionStatus,
    AgentActionType,
    AgentSettings,
    CrmActivity,
    CrmActivityType,
    Lead,
    Notification,
    NotificationKind,
)
from app.services.agent_core import get_agent_settings

pytestmark = pytest.mark.asyncio


# ---- AgentSettings get-or-create (per-tenant since Phase 2) ----------------


async def test_get_agent_settings_bootstraps_singleton(db_session):
    row = await get_agent_settings(db_session)
    # No tenant context on a bare session → the legacy no-tenant row.
    assert row.id is not None and row.tenant_id is None
    # Documented defaults.
    assert row.auto_log_replies is True
    assert row.auto_create_convert_reminders is True
    assert row.auto_draft_replies is False
    assert row.stale_opp_nudges_enabled is True
    assert row.daily_digest_enabled is True
    assert row.notify_on_positive_reply is True
    assert row.notify_on_any_reply is False
    assert row.min_confidence_to_act == Decimal("0.60")
    assert row.quiet_hours_start_utc is None
    assert row.quiet_hours_end_utc is None


async def test_get_agent_settings_returns_same_row_twice(db_session):
    first = await get_agent_settings(db_session)
    first.auto_draft_replies = True
    await db_session.commit()

    second = await get_agent_settings(db_session)
    assert second.id == first.id
    assert second.auto_draft_replies is True
    # Still exactly one row.
    rows = (await db_session.execute(select(AgentSettings))).scalars().all()
    assert len(rows) == 1


# ---- Notification dedup ----------------------------------------------------


async def test_notification_dedup_key_is_unique(db_session):
    db_session.add(Notification(
        kind=NotificationKind.POSITIVE_REPLY,
        title="Positive reply from Jane",
        dedup_key="positive_reply:msg-1",
    ))
    await db_session.commit()

    db_session.add(Notification(
        kind=NotificationKind.POSITIVE_REPLY,
        title="Positive reply from Jane (dupe)",
        dedup_key="positive_reply:msg-1",
    ))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()

    rows = (await db_session.execute(select(Notification))).scalars().all()
    assert len(rows) == 1


async def test_notification_links_survive_lead_delete(db_session):
    lead = Lead(campaign_id=None, email="n@x.com")
    db_session.add(lead)
    await db_session.flush()
    notif = Notification(
        kind=NotificationKind.REPLY,
        title="Reply",
        dedup_key="reply:msg-2",
        lead_id=lead.id,
    )
    db_session.add(notif)
    await db_session.commit()

    await db_session.delete(lead)
    await db_session.commit()
    # expire_on_commit=False keeps the identity-map copy stale — re-read
    # from the DB to observe the FK's SET NULL.
    await db_session.refresh(notif)

    assert notif.lead_id is None          # SET NULL, not CASCADE


# ---- AgentAction audit ------------------------------------------------------


async def test_agent_action_persists_detail_and_cost(db_session):
    action = AgentAction(
        action_type=AgentActionType.CLASSIFY_REPLY,
        status=AgentActionStatus.SUCCESS,
        summary="Classified reply as positive (0.91)",
        detail={"sentiment": "positive", "confidence": 0.91},
        model="claude-haiku-4-5-20251001",
        cost_usd=Decimal("0.000412"),
    )
    db_session.add(action)
    await db_session.commit()

    row = await db_session.get(AgentAction, action.id)
    assert row.detail["sentiment"] == "positive"
    assert row.cost_usd == Decimal("0.000412")
    assert row.status is AgentActionStatus.SUCCESS


# ---- CrmActivity agent columns ----------------------------------------------


async def test_crm_activity_agent_columns_default(db_session):
    lead = Lead(campaign_id=None, email="a@x.com")
    db_session.add(lead)
    await db_session.flush()
    act = CrmActivity(
        lead_id=lead.id,
        activity_type=CrmActivityType.NOTE,
        subject="Manual note",
    )
    db_session.add(act)
    await db_session.commit()
    await db_session.refresh(act)

    assert act.reminder_sent_at is None
    assert act.sentiment is None
    assert act.is_agent_generated is False


# ---- Config ------------------------------------------------------------------


async def test_agent_config_defaults():
    from app.config import settings

    assert settings.AGENT_ENABLED is True
    assert "haiku" in settings.ANTHROPIC_AGENT_MODEL
    assert "sonnet" in settings.ANTHROPIC_AGENT_DRAFT_MODEL
    assert settings.AGENT_REMINDER_SWEEP_INTERVAL_MINUTES == 30
    assert settings.AGENT_DIGEST_HOUR_UTC == 12
    assert settings.AGENT_STALE_OPP_DAYS == 7
    assert settings.AGENT_TASK_DUE_SOON_HOURS == 24
    assert settings.OWNER_NOTIFY_NAME == "Operator"
