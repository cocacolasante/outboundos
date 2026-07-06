"""Sender-identity validation + launch guards.

A campaign's Brevo "From" is campaign.sender_email; auto-created (intent-engine)
campaigns start with a placeholder, so launch (approve-all) and resume must be
blocked until a real address is set, and the response exposes sender_ready.
"""
from __future__ import annotations

from datetime import time

import pytest

from app.models import Campaign, CampaignStatus, ComposeStatus, Lead, SendStatus
from app.services.sender import is_valid_sender_email

pytestmark = pytest.mark.asyncio


async def test_is_valid_sender_email():
    assert is_valid_sender_email("anthony@example.org") is True
    assert is_valid_sender_email("you@example.com") is False     # placeholder
    assert is_valid_sender_email("") is False
    assert is_valid_sender_email(None) is False
    assert is_valid_sender_email("not-an-email") is False
    assert is_valid_sender_email("  Real@Domain.Co  ") is True   # trimmed/cased


async def _campaign(db_session, *, status, sender_email):
    c = Campaign(
        name="Intent drafts — GrantMind Pro", goal="g", tone="warm",
        sender_name="Operator", sender_email=sender_email,
        schedule_time_start=time(9, 0), schedule_time_end=time(17, 0),
        status=status,
    )
    db_session.add(c)
    await db_session.flush()
    return c


async def test_response_exposes_sender_ready(client, db_session):
    c = await _campaign(db_session, status=CampaignStatus.DRAFT, sender_email="you@example.com")
    await db_session.commit()
    body = (await client.get(f"/campaigns/{c.id}")).json()
    assert body["sender_ready"] is False

    c.sender_email = "real@org.com"
    await db_session.commit()
    body2 = (await client.get(f"/campaigns/{c.id}")).json()
    assert body2["sender_ready"] is True


async def test_resume_blocked_until_valid_sender(client, db_session):
    c = await _campaign(db_session, status=CampaignStatus.PAUSED, sender_email="you@example.com")
    await db_session.commit()

    blocked = await client.post(f"/campaigns/{c.id}/resume")
    assert blocked.status_code == 409 and "sending email" in blocked.json()["detail"].lower()

    c.sender_email = "real@org.com"
    await db_session.commit()
    ok = await client.post(f"/campaigns/{c.id}/resume")
    assert ok.status_code == 200 and ok.json()["status"] == "running"


async def test_approve_all_blocked_until_valid_sender(client, db_session):
    c = await _campaign(db_session, status=CampaignStatus.PREVIEWING, sender_email="you@example.com")
    db_session.add(Lead(
        campaign_id=c.id, email="lead@org.com", company="Org",
        is_sample=True, compose_status=ComposeStatus.DONE,
        composed_subject="s", composed_body="b", send_status=SendStatus.PENDING,
    ))
    await db_session.commit()

    blocked = await client.post(f"/campaigns/{c.id}/preview/approve-all")
    assert blocked.status_code == 409 and "sending email" in blocked.json()["detail"].lower()

    c.sender_email = "real@org.com"
    await db_session.commit()
    ok = await client.post(f"/campaigns/{c.id}/preview/approve-all")
    assert ok.status_code == 200 and ok.json()["status"] == "running"
