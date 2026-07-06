"""Multi-tenancy Phase 6: team / workspace management.

Invite (new user = passwordless account + set-password token, seat
consumed at invite; existing user = membership only), accept-invite,
member removal (last-owner guard, session revocation), tenant rename,
role gating, and the plan seat cap.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import BillingPlan, Membership, SubscriptionStatus, Tenant, User

pytestmark = pytest.mark.asyncio

OWNER = {"email": "owner@example.com", "password": "hunter2hunter2",
         "tenant_name": "Team Co"}


async def _register_owner(auth_client):
    resp = await auth_client.post("/auth/register", json=OWNER)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
def sent_mail(monkeypatch):
    sent = []

    async def _capture(**kwargs):
        sent.append(kwargs)
        return True

    import app.routers.auth as auth_router
    monkeypatch.setattr(auth_router, "send_platform_email", _capture)
    return sent


@pytest.fixture
def roomy_seats(monkeypatch):
    """Registration starts a STARTER trial (1 seat, filled by the owner) —
    tests that exercise invites need headroom."""
    from app.billing import plans as plans_mod

    roomy = plans_mod.PlanDef(label="Starter", monthly_quotas={},
                              static_limits={"seats": 5})
    monkeypatch.setitem(plans_mod.PLANS, BillingPlan.STARTER, roomy)


async def test_invite_new_user_full_flow(auth_client, db_session, sent_mail, roomy_seats):
    me = await _register_owner(auth_client)

    resp = await auth_client.post("/auth/team/invite", json={
        "email": "colleague@example.com", "role": "member",
    })
    assert resp.status_code == 201, resp.text
    assert resp.json()["pending"] is True

    team = (await auth_client.get("/auth/team")).json()
    assert {m["email"] for m in team} == {"owner@example.com", "colleague@example.com"}

    # The invite email carries the set-password link.
    assert len(sent_mail) == 1
    token = sent_mail[0]["text_body"].split("invite_token=")[1].split()[0]

    # Accept: set the password, then log in and see the OWNER's workspace.
    accept = await auth_client.post("/auth/accept-invite", json={
        "token": token, "password": "a-solid-password-1",
    })
    assert accept.status_code == 200

    auth_client.cookies.clear()
    login = await auth_client.post("/auth/login", json={
        "email": "colleague@example.com", "password": "a-solid-password-1",
    })
    assert login.status_code == 200
    assert login.json()["tenant_id"] == me["tenant_id"]
    assert login.json()["role"] == "member"

    # Token is single-use.
    again = await auth_client.post("/auth/accept-invite", json={
        "token": token, "password": "another-pass-22",
    })
    assert again.status_code == 400


async def test_invite_existing_user_adds_membership_without_email(auth_client, sent_mail, roomy_seats):
    await _register_owner(auth_client)
    owner_cookie = auth_client.cookies.get("eb_session")

    auth_client.cookies.clear()
    await auth_client.post("/auth/register", json={
        "email": "other@example.com", "password": "hunter2hunter2",
        "tenant_name": "Other Co",
    })

    auth_client.cookies.clear()
    auth_client.cookies.set("eb_session", owner_cookie)
    resp = await auth_client.post("/auth/team/invite", json={
        "email": "other@example.com", "role": "admin",
    })
    assert resp.status_code == 201
    assert resp.json()["pending"] is False
    assert sent_mail == []  # existing account: no set-password email

    dup = await auth_client.post("/auth/team/invite", json={
        "email": "other@example.com", "role": "admin",
    })
    assert dup.status_code == 409


async def test_member_cannot_invite_and_owner_cannot_be_invited(auth_client, db_session, sent_mail, roomy_seats):
    me = await _register_owner(auth_client)
    assert (await auth_client.post("/auth/team/invite", json={
        "email": "x@example.com", "role": "owner",
    })).status_code == 422

    # Demote path: create a member session and try to invite.
    await auth_client.post("/auth/team/invite", json={
        "email": "member@example.com", "role": "member",
    })
    token = sent_mail[-1]["text_body"].split("invite_token=")[1].split()[0]
    await auth_client.post("/auth/accept-invite", json={
        "token": token, "password": "member-pass-123",
    })
    auth_client.cookies.clear()
    await auth_client.post("/auth/login", json={
        "email": "member@example.com", "password": "member-pass-123",
    })
    resp = await auth_client.post("/auth/team/invite", json={
        "email": "y@example.com", "role": "member",
    })
    assert resp.status_code == 403


async def test_seat_cap_blocks_invite_with_402(auth_client, db_session, sent_mail, monkeypatch):
    me = await _register_owner(auth_client)
    # The trial runs on STARTER (1 seat) — the owner already fills it.
    resp = await auth_client.post("/auth/team/invite", json={
        "email": "second@example.com", "role": "member",
    })
    assert resp.status_code == 402
    assert "1 seats" in resp.json()["detail"]


async def test_remove_member_and_last_owner_guard(auth_client, db_session, sent_mail, monkeypatch):
    from app.billing import plans as plans_mod

    me = await _register_owner(auth_client)
    # Give the trial tenant room to invite.
    roomy = plans_mod.PlanDef(label="Starter", monthly_quotas={},
                              static_limits={"seats": 5})
    monkeypatch.setitem(plans_mod.PLANS, BillingPlan.STARTER, roomy)

    invited = await auth_client.post("/auth/team/invite", json={
        "email": "temp@example.com", "role": "member",
    })
    assert invited.status_code == 201
    membership_id = invited.json()["membership_id"]

    removed = await auth_client.delete(f"/auth/team/{membership_id}")
    assert removed.status_code == 204
    team = (await auth_client.get("/auth/team")).json()
    assert len(team) == 1

    # The last owner cannot be removed.
    owner_mid = team[0]["membership_id"]
    resp = await auth_client.delete(f"/auth/team/{owner_mid}")
    assert resp.status_code == 409


async def test_rename_tenant(auth_client, db_session):
    me = await _register_owner(auth_client)
    resp = await auth_client.patch("/auth/tenant", json={"name": "Renamed Co"})
    assert resp.status_code == 200
    assert resp.json()["tenant_name"] == "Renamed Co"
    t = await db_session.get(Tenant, __import__("uuid").UUID(me["tenant_id"]))
    assert t.name == "Renamed Co"
