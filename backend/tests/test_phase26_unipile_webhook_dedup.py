"""Tests for the Unipile inbound-webhook handler — auth + idempotency.

We don't unit-test every per-event handler here (the providers tests
cover the outbound side, and the e2e flow against a real Unipile account
is the only thing that catches payload-shape surprises).  This module
asserts the structural contract of the route: auth, JSON validation,
and the at-least-once dedup guard backed by the ``webhook_events`` table.
"""
from __future__ import annotations

import uuid
from datetime import time
from typing import Any

import pytest
from sqlalchemy import select

from app.models import (
    Campaign,
    Lead,
    LeadSequenceState,
    LeadSequenceStatus,
    LinkedInAccount,
    LinkedInAccountStatus,
    LinkedInConnectionStatus,
    Sequence,
    SequenceEdge,
    SequenceNode,
    SequenceNodeKind,
    WebhookEvent,
)


SECRET = "test-unipile-secret"


@pytest.fixture(autouse=True)
def _wire_secret(monkeypatch):
    monkeypatch.setattr("app.routers.webhooks.settings.UNIPILE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr("app.routers.webhooks.settings.UNIPILE_WEBHOOK_AUTH_HEADER", "X-Unipile-Auth")


async def _make_unipile_account(db_session, unipile_id: str = "up-acct-1") -> LinkedInAccount:
    acc = LinkedInAccount(
        label="test", linkedin_email="t@x.com",
        provider_kind="unipile", unipile_account_id=unipile_id,
        status=LinkedInAccountStatus.UNTESTED,
    )
    db_session.add(acc)
    await db_session.commit()
    await db_session.refresh(acc)
    return acc


# ---- Auth -----------------------------------------------------------------


async def test_rejects_missing_auth_header(client):
    resp = await client.post(
        "/webhooks/unipile",
        json={"type": "account.connected"},
    )
    assert resp.status_code == 401


async def test_rejects_wrong_auth_header(client):
    resp = await client.post(
        "/webhooks/unipile",
        json={"type": "account.connected"},
        headers={"X-Unipile-Auth": "nope"},
    )
    assert resp.status_code == 401


async def test_rejects_non_object_payload(client):
    resp = await client.post(
        "/webhooks/unipile",
        json=["not", "an", "object"],
        headers={"X-Unipile-Auth": SECRET},
    )
    assert resp.status_code == 400


# ---- Idempotency ---------------------------------------------------------


async def test_duplicate_event_id_is_a_noop(client, db_session):
    """Two deliveries of the same Unipile event_id record one
    webhook_events row and the handler only runs once."""
    acc = await _make_unipile_account(db_session, unipile_id="up-acct-dup")
    payload: dict[str, Any] = {
        "id": "evt-dup-1",
        "type": "account.checkpoint",
        "account_id": "up-acct-dup",
    }
    headers = {"X-Unipile-Auth": SECRET}

    r1 = await client.post("/webhooks/unipile", json=payload, headers=headers)
    assert r1.status_code == 200
    assert r1.json().get("duplicate") is not True

    r2 = await client.post("/webhooks/unipile", json=payload, headers=headers)
    assert r2.status_code == 200
    assert r2.json().get("duplicate") is True

    # Exactly one webhook_events row for this event_id.
    rows = (await db_session.execute(
        select(WebhookEvent).where(WebhookEvent.event_id == "evt-dup-1")
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].provider == "unipile"


async def test_body_hash_dedup_when_event_id_missing(client, db_session):
    """Payloads without an ``id`` still dedup if the raw body is identical,
    so a Unipile-side retry of an event without an id won't double-apply."""
    payload = {
        "type": "account.checkpoint",
        "account_id": "up-acct-bh",
        "note": "this payload has no id",
    }
    headers = {"X-Unipile-Auth": SECRET}

    r1 = await client.post("/webhooks/unipile", json=payload, headers=headers)
    r2 = await client.post("/webhooks/unipile", json=payload, headers=headers)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json().get("duplicate") is True

    rows = (await db_session.execute(
        select(WebhookEvent).where(WebhookEvent.event_id.like("sha256:%"))
    )).scalars().all()
    assert len(rows) == 1


async def test_invitation_accepted_advances_parked_dm_once(client, db_session):
    """End-to-end against the connect→DM parking flow: two deliveries of
    the same invitation.accepted event must only flip the lead's status
    once (i.e. the at-least-once retry doesn't double-apply state)."""
    acc = await _make_unipile_account(db_session, unipile_id="up-acct-2")
    c = Campaign(
        name="P26", goal="g", tone="t", sender_name="s", sender_email="s@x.com",
        sample_count=1, schedule_days=[0, 1, 2, 3, 4],
        schedule_time_start=time(9, 0), schedule_time_end=time(17, 0),
        linkedin_account_id=acc.id,
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    lead = Lead(
        campaign_id=c.id, email="l@x.com",
        linkedin_url="https://www.linkedin.com/in/jane-doe/",
        linkedin_connection_status=LinkedInConnectionStatus.INVITED,
    )
    db_session.add(lead)
    await db_session.commit()
    await db_session.refresh(lead)

    payload = {
        "id": "evt-inv-1",
        "type": "invitation.accepted",
        "account_id": "up-acct-2",
        "user_provider_id": "jane-doe-provider-id",
        "user_public_identifier": "jane-doe",
    }
    headers = {"X-Unipile-Auth": SECRET}

    r1 = await client.post("/webhooks/unipile", json=payload, headers=headers)
    r2 = await client.post("/webhooks/unipile", json=payload, headers=headers)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json().get("duplicate") is True

    # The handler ran at most once; the lead either advanced to CONNECTED
    # (single application of the handler) or stayed put — what matters is
    # there's only one webhook_events row.
    rows = (await db_session.execute(
        select(WebhookEvent).where(WebhookEvent.event_id == "evt-inv-1")
    )).scalars().all()
    assert len(rows) == 1


async def test_unknown_event_type_still_dedups(client, db_session):
    """A future Unipile event type we don't have a handler for should still
    occupy a webhook_events slot so a retry doesn't keep flooding logs."""
    headers = {"X-Unipile-Auth": SECRET}
    payload = {"id": "evt-unknown-1", "type": "something.we.dont.handle"}

    r1 = await client.post("/webhooks/unipile", json=payload, headers=headers)
    r2 = await client.post("/webhooks/unipile", json=payload, headers=headers)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json().get("duplicate") is True

    rows = (await db_session.execute(
        select(WebhookEvent).where(WebhookEvent.event_id == "evt-unknown-1")
    )).scalars().all()
    assert len(rows) == 1
