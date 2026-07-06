"""Regression tests for two bugs in the Unipile webhook handler that
silently dropped ``invitation.accepted`` (a.k.a. ``new_relation``)
events on production, leaving 51 INVITED leads parked on the connect
node forever and never firing the downstream DM.

Bug 1: ``_find_lead_for_event`` only looked at nested
``sender``/``from``/``attendee`` objects.  Unipile's ``new_relation``
event puts the user identifier at the TOP LEVEL
(``user_public_identifier``, ``user_provider_id``, ``user_profile_url``)
so every event missed every lead.

Bug 2: ``_handle_message_received`` / ``_handle_invitation_accepted``
unconditionally called ``.get()`` on whatever was at ``payload["data"]``
or ``payload["message"]``.  Some Unipile event flavours send a string
there, raising ``AttributeError`` and 500ing the webhook — Unipile then
retried until our dedup table absorbed it.
"""
from __future__ import annotations

from datetime import time

import pytest
from sqlalchemy import select

from app.models import (
    Campaign,
    Lead,
    LinkedInAccount,
    LinkedInAccountStatus,
    LinkedInConnectionStatus,
)


SECRET = "test-unipile-secret"


@pytest.fixture(autouse=True)
def _wire_secret(monkeypatch):
    monkeypatch.setattr("app.routers.webhooks.settings.UNIPILE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr("app.routers.webhooks.settings.UNIPILE_WEBHOOK_AUTH_HEADER", "X-Unipile-Auth")


async def _seed_campaign_with_lead(
    db_session,
    *,
    unipile_id: str,
    lead_url: str,
    initial_status: LinkedInConnectionStatus = LinkedInConnectionStatus.INVITED,
) -> tuple[LinkedInAccount, Campaign, Lead]:
    acc = LinkedInAccount(
        label="t", linkedin_email="t@x.com",
        provider_kind="unipile", unipile_account_id=unipile_id,
        status=LinkedInAccountStatus.OK,
    )
    db_session.add(acc)
    await db_session.commit()
    await db_session.refresh(acc)

    c = Campaign(
        name="P30", goal="g", tone="t", sender_name="s", sender_email="s@x.com",
        sample_count=1, schedule_days=[0, 1, 2, 3, 4],
        schedule_time_start=time(9, 0), schedule_time_end=time(17, 0),
        linkedin_account_id=acc.id,
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)

    lead = Lead(
        campaign_id=c.id, email="lead@x.com",
        linkedin_url=lead_url,
        linkedin_connection_status=initial_status,
    )
    db_session.add(lead)
    await db_session.commit()
    await db_session.refresh(lead)
    return acc, c, lead


# ---- Bug 1: top-level identifier shape (Unipile new_relation) ------------


async def test_invitation_accepted_matches_top_level_public_identifier(
    client, db_session,
):
    """Real-world Unipile ``new_relation`` event with the slug at the top
    level — handler must match the lead and flip status to CONNECTED."""
    _, _, lead = await _seed_campaign_with_lead(
        db_session,
        unipile_id="up-acct-tl",
        lead_url="https://www.linkedin.com/in/jane-doe/",
    )

    payload = {
        "id": "evt-tl-1",
        "type": "new_relation",
        "account_id": "up-acct-tl",
        "user_provider_id": "ACoAAAfoofoofoo",
        "user_public_identifier": "jane-doe",
        "user_profile_url": "https://www.linkedin.com/in/jane-doe/",
    }
    resp = await client.post(
        "/webhooks/unipile", json=payload,
        headers={"X-Unipile-Auth": SECRET},
    )
    assert resp.status_code == 200, resp.text

    await db_session.refresh(lead)
    assert lead.linkedin_connection_status == LinkedInConnectionStatus.CONNECTED


async def test_invitation_accepted_matches_full_profile_url(
    client, db_session,
):
    """If the slug is missing but the full profile_url is present, the
    handler still finds the lead — we normalise both sides to the last
    path segment."""
    _, _, lead = await _seed_campaign_with_lead(
        db_session,
        unipile_id="up-acct-fu",
        lead_url="https://linkedin.com/in/bob-smith",
    )

    payload = {
        "id": "evt-fu-1",
        "type": "invitation.accepted",
        "account_id": "up-acct-fu",
        "user_profile_url": "https://www.linkedin.com/in/bob-smith/",
    }
    resp = await client.post(
        "/webhooks/unipile", json=payload,
        headers={"X-Unipile-Auth": SECRET},
    )
    assert resp.status_code == 200

    await db_session.refresh(lead)
    assert lead.linkedin_connection_status == LinkedInConnectionStatus.CONNECTED


async def test_invitation_accepted_no_match_leaves_lead_alone(
    client, db_session,
):
    """An event for a slug we don't have a lead for must not flip any
    lead's status (e.g. user accepted an invite outside our pipeline)."""
    _, _, lead = await _seed_campaign_with_lead(
        db_session,
        unipile_id="up-acct-nm",
        lead_url="https://www.linkedin.com/in/alice-jones/",
    )

    payload = {
        "id": "evt-nm-1",
        "type": "new_relation",
        "account_id": "up-acct-nm",
        "user_public_identifier": "someone-else",
    }
    resp = await client.post(
        "/webhooks/unipile", json=payload,
        headers={"X-Unipile-Auth": SECRET},
    )
    assert resp.status_code == 200

    await db_session.refresh(lead)
    assert lead.linkedin_connection_status == LinkedInConnectionStatus.INVITED


# ---- Bug 2: defensive against non-dict nested fields --------------------


async def test_invitation_accepted_with_string_data_field_does_not_crash(
    client, db_session,
):
    """Unipile occasionally sends ``data`` as a string instead of a dict
    (or ``message`` / ``invitation``).  The handler must not crash; the
    top-level identifier still matches the lead."""
    _, _, lead = await _seed_campaign_with_lead(
        db_session,
        unipile_id="up-acct-str",
        lead_url="https://www.linkedin.com/in/carl-king/",
    )

    payload = {
        "id": "evt-str-1",
        "type": "new_relation",
        "account_id": "up-acct-str",
        "user_public_identifier": "carl-king",
        # The bug: prior code did `body = payload.get("data") or
        # payload.get("message") or payload` then `body.get(...)` — when
        # "data" was a string it raised AttributeError.
        "data": "some-string-payload",
    }
    resp = await client.post(
        "/webhooks/unipile", json=payload,
        headers={"X-Unipile-Auth": SECRET},
    )
    assert resp.status_code == 200, resp.text

    await db_session.refresh(lead)
    assert lead.linkedin_connection_status == LinkedInConnectionStatus.CONNECTED


async def test_message_received_with_string_message_field_does_not_crash(
    client, db_session,
):
    """Same defensive contract for the message.received handler."""
    _, _, lead = await _seed_campaign_with_lead(
        db_session,
        unipile_id="up-acct-msg",
        lead_url="https://www.linkedin.com/in/diane-queen/",
        initial_status=LinkedInConnectionStatus.INVITED,
    )

    payload = {
        "id": "evt-msg-1",
        "type": "message.received",
        "account_id": "up-acct-msg",
        "sender": {"public_identifier": "diane-queen"},
        "message": "string body here",  # bug 2 shape
    }
    resp = await client.post(
        "/webhooks/unipile", json=payload,
        headers={"X-Unipile-Auth": SECRET},
    )
    assert resp.status_code == 200, resp.text

    await db_session.refresh(lead)
    # First inbound DM implies a 1st-degree connection.
    assert lead.linkedin_connection_status == LinkedInConnectionStatus.CONNECTED
    assert lead.linkedin_last_reply_at is not None
