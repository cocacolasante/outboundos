"""Tests for ``UnipileLinkedInProvider`` using httpx.MockTransport.

The provider is just a thin async wrapper over Unipile's REST API, so
the tests are mostly about: did we hit the right path with the right
payload, did we map Unipile's error envelopes to our domain exceptions,
did we extract the right IDs from success responses.

Real Unipile API calls are NEVER made here — we wire an
``httpx.MockTransport`` into the provider via its ``transport=`` kwarg.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from app.services.linkedin.base import (
    AccountRestricted,
    ChallengeRequired,
    ProfileRef,
)
from app.services.linkedin.unipile_impl import (
    UnipileError,
    UnipileLinkedInProvider,
)


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------


@dataclass
class _FakeAccount:
    """Stand-in for the LinkedInAccount SQLAlchemy model."""
    id: str = "li-acct-1"
    unipile_account_id: str | None = "up-acct-XYZ"
    pending_challenge_url: str | None = None
    last_error: str | None = None
    status: str | None = None
    session_cookies_encrypted: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


def _provider_with(handler) -> UnipileLinkedInProvider:
    """Build a provider with an injected MockTransport that delegates to ``handler``."""
    transport = httpx.MockTransport(handler)
    return UnipileLinkedInProvider(
        dsn="api-test.unipile.com:13443",
        api_key="test-api-key",
        transport=transport,
    )


def _capture(records: list[httpx.Request]):
    """Build a MockTransport handler that records every request, returning canned 200s.

    The handler can be parametrised in tests by passing a different
    response factory or wrapping it.  By default it returns
    ``{"ok": true}`` to every call.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        records.append(request)
        return httpx.Response(200, json={"ok": True})
    return handler


# --------------------------------------------------------------------------
# _request — base behaviour, error mapping, header injection
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_headers_include_api_key():
    seen: list[httpx.Request] = []
    prov = _provider_with(_capture(seen))
    await prov.fetch_account_status("up-1")
    assert seen[0].headers["X-API-KEY"] == "test-api-key"
    assert seen[0].headers["Accept"] == "application/json"


@pytest.mark.asyncio
async def test_dsn_with_scheme_is_respected():
    seen: list[httpx.Request] = []
    transport = httpx.MockTransport(_capture(seen))
    prov = UnipileLinkedInProvider(
        dsn="https://custom.unipile.com:1313",
        api_key="k",
        transport=transport,
    )
    await prov.fetch_account_status("up-1")
    assert str(seen[0].url).startswith("https://custom.unipile.com:1313/")


@pytest.mark.asyncio
async def test_request_4xx_raises_unipile_error():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={"type": "errors/validation", "title": "bad input", "detail": "username missing"},
        )
    prov = _provider_with(handler)
    with pytest.raises(UnipileError) as ei:
        await prov.fetch_account_status("up-1")
    assert ei.value.status == 422
    assert ei.value.code == "errors/validation"
    assert "username missing" in str(ei.value)


@pytest.mark.asyncio
async def test_request_follows_unicode_slug_redirect():
    """A LinkedIn slug with accented chars makes Unipile 301-redirect to a
    Unicode-normalized URL.  The client must follow it (not surface the HTML
    'Redirecting' page as a failure), so the profile resolves."""
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        if len(calls) == 1:
            return httpx.Response(
                301,
                headers={"Location": "/api/v1/users/rahna%CC%80-wake%CC%A8-x/?account_id=acc-1"},
                text="<!DOCTYPE html>\n<html><title>Redirecting</title></html>",
            )
        return httpx.Response(200, json={"provider_id": "ACoAA-resolved"})

    prov = _provider_with(handler)
    resolved = await prov._resolve_provider_id("acc-1", ProfileRef(public_id="rahnà-wakę-x"))
    assert resolved == "ACoAA-resolved"
    assert len(calls) == 2  # original (301) + followed redirect target (200)


@pytest.mark.asyncio
async def test_checkpoint_error_maps_to_challenge_required():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={"type": "errors/checkpoint", "title": "needs 2fa", "detail": "..."},
        )
    prov = _provider_with(handler)
    with pytest.raises(ChallengeRequired):
        await prov.fetch_account_status("up-1")


@pytest.mark.asyncio
async def test_restricted_error_maps_to_account_restricted():
    """Only the LinkedIn-account-state error codes raise AccountRestricted.
    A generic ``errors/restricted`` does not — earlier impls grepped the
    body for "restricted" anywhere and false-positived on
    ``errors/resource_access_restricted`` (a Unipile plan error)."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"type": "errors/account_restricted", "title": "account banned"},
        )
    prov = _provider_with(handler)
    with pytest.raises(AccountRestricted):
        await prov.fetch_account_status("up-1")


@pytest.mark.asyncio
async def test_resource_access_restricted_does_not_map_to_account_restricted():
    """Unipile's plan-permission error must surface as a plain UnipileError
    so callers don't flip a healthy account to RESTRICTED."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "type": "errors/resource_access_restricted",
                "title": "Resource access restricted",
                "detail": "You don't have access to this resource.",
            },
        )
    prov = _provider_with(handler)
    with pytest.raises(UnipileError):
        await prov.fetch_account_status("up-1")


@pytest.mark.asyncio
async def test_network_error_maps_to_unipile_error():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")
    prov = _provider_with(handler)
    with pytest.raises(UnipileError) as ei:
        await prov.fetch_account_status("up-1")
    assert ei.value.status == 0
    assert ei.value.code == "network"


# --------------------------------------------------------------------------
# Config errors
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_dsn_raises_clean_error():
    prov = UnipileLinkedInProvider(dsn="", api_key="k", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    with pytest.raises(UnipileError) as ei:
        await prov.fetch_account_status("up-1")
    assert "UNIPILE_DSN" in str(ei.value) or "DSN" in str(ei.value)


@pytest.mark.asyncio
async def test_missing_api_key_raises_clean_error():
    prov = UnipileLinkedInProvider(dsn="api.unipile.com:1313", api_key="", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    with pytest.raises(UnipileError) as ei:
        await prov.fetch_account_status("up-1")
    assert "UNIPILE_API_KEY" in str(ei.value) or "API_KEY" in str(ei.value)


@pytest.mark.asyncio
async def test_account_missing_unipile_id_returns_actionable_error():
    acct = _FakeAccount(unipile_account_id=None)
    prov = _provider_with(lambda r: httpx.Response(200, json={}))
    res = await prov.test_connection(acct)
    assert res.ok is False
    assert "hosted login" in res.error or "no_account" in (res.meta or {}).get("code", "")


# --------------------------------------------------------------------------
# Hosted-auth link
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_hosted_auth_link_posts_expected_payload():
    seen: list[httpx.Request] = []
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"url": "https://accounts.unipile.com/x", "id": "link-1"})
    prov = _provider_with(handler)
    out = await prov.create_hosted_auth_link(
        success_redirect_url="https://app.example.com/back",
        failure_redirect_url="https://app.example.com/fail",
        notify_url="https://api.example.com/webhooks/unipile",
        name="my-account",
    )
    assert out["url"].startswith("https://")
    body = json.loads(seen[0].content)
    assert body["providers"] == ["LINKEDIN"]
    assert body["success_redirect_url"] == "https://app.example.com/back"
    assert body["failure_redirect_url"] == "https://app.example.com/fail"
    assert body["notify_url"] == "https://api.example.com/webhooks/unipile"
    assert body["name"] == "my-account"
    assert body["type"] == "create"


# --------------------------------------------------------------------------
# test_connection — status mapping
# --------------------------------------------------------------------------


@pytest.mark.parametrize("upstream_status", ["OK", "CONNECTED", "ACTIVE"])
@pytest.mark.asyncio
async def test_test_connection_ok_statuses(upstream_status):
    prov = _provider_with(lambda r: httpx.Response(200, json={"status": upstream_status}))
    res = await prov.test_connection(_FakeAccount())
    assert res.ok is True


@pytest.mark.parametrize("upstream_status", ["CHECKPOINT", "OTP_REQUIRED_2FA"])
@pytest.mark.asyncio
async def test_test_connection_checkpoint_statuses_signal_challenge(upstream_status):
    prov = _provider_with(lambda r: httpx.Response(200, json={"status": upstream_status}))
    acct = _FakeAccount()
    res = await prov.test_connection(acct)
    assert res.ok is False
    assert (res.meta or {}).get("challenged") is True
    assert acct.pending_challenge_url == "https://www.linkedin.com"


@pytest.mark.parametrize("upstream_status", ["BANNED", "RESTRICTED", "SUSPENDED"])
@pytest.mark.asyncio
async def test_test_connection_penalty_statuses_signal_restricted(upstream_status):
    prov = _provider_with(lambda r: httpx.Response(200, json={"status": upstream_status}))
    res = await prov.test_connection(_FakeAccount())
    assert res.ok is False
    assert (res.meta or {}).get("restricted") is True


# --------------------------------------------------------------------------
# view_profile / follow / react / latest_post
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_view_profile_calls_users_endpoint_with_account_id():
    seen: list[httpx.Request] = []
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"provider_id": "urn:li:fsd_profile:ABC", "name": "Test"})
    prov = _provider_with(handler)
    res = await prov.view_profile(_FakeAccount(), ProfileRef(public_id="john-doe"))
    assert res.ok is True
    assert res.external_id == "urn:li:fsd_profile:ABC"
    assert "/api/v1/users/john-doe" in str(seen[0].url)
    assert "account_id=up-acct-XYZ" in str(seen[0].url)


@pytest.mark.asyncio
async def test_view_profile_no_id_returns_error():
    prov = _provider_with(lambda r: httpx.Response(200, json={}))
    res = await prov.view_profile(_FakeAccount(), ProfileRef())
    assert res.ok is False


@pytest.mark.asyncio
async def test_view_profile_propagates_challenge():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"type": "errors/checkpoint", "title": "checkpoint"})
    prov = _provider_with(handler)
    acct = _FakeAccount()
    with pytest.raises(ChallengeRequired):
        await prov.view_profile(acct, ProfileRef(public_id="x"))
    assert acct.pending_challenge_url == "https://www.linkedin.com"


@pytest.mark.asyncio
async def test_follow_profile_posts_to_follow_endpoint():
    seen: list[httpx.Request] = []
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"followed": True})
    prov = _provider_with(handler)
    res = await prov.follow_profile(
        _FakeAccount(), ProfileRef(public_id="j", urn="urn:li:fsd_profile:J"),
    )
    assert res.ok is True
    assert seen[0].method == "POST"
    assert "/api/v1/linkedin" in str(seen[0].url)
    body = json.loads(seen[0].content)
    assert body["account_id"] == "up-acct-XYZ"
    assert body["method"] == "POST"
    assert "followingStates/urn:li:fsd_followingState:urn:li:fsd_profile:J" in body["request_url"]
    assert body["body"] == {"patch": {"$set": {"following": True}}}
    assert body["encoding"] is False


@pytest.mark.asyncio
async def test_react_to_post_sends_reaction_type():
    seen: list[httpx.Request] = []
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(201, json={"object": "ReactionAdded"})
    prov = _provider_with(handler)
    res = await prov.react_to_post(_FakeAccount(), "urn:li:share:1", reaction="celebrate")
    assert res.ok is True
    assert str(seen[0].url).endswith("/api/v1/posts/reaction")
    body = json.loads(seen[0].content)
    assert body == {
        "account_id": "up-acct-XYZ",
        "post_id": "urn:li:share:1",
        "type": "CELEBRATE",
    }


@pytest.mark.asyncio
async def test_latest_post_urn_returns_first_item():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{"urn": "urn:li:share:111"}, {"urn": "urn:li:share:222"}]})
    prov = _provider_with(handler)
    urn = await prov.latest_post_urn(
        _FakeAccount(), ProfileRef(public_id="x", urn="urn:li:fsd_profile:X"),
    )
    assert urn == "urn:li:share:111"


@pytest.mark.asyncio
async def test_latest_post_urn_returns_none_on_error():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "broken"})
    prov = _provider_with(handler)
    urn = await prov.latest_post_urn(
        _FakeAccount(), ProfileRef(public_id="x", urn="urn:li:fsd_profile:X"),
    )
    assert urn is None


@pytest.mark.asyncio
async def test_resolve_provider_id_fetches_via_users_endpoint():
    """When only a public_id is set, the resolver makes a GET /users/{slug}
    to translate it into the canonical provider_id, then caches the result
    on the ProfileRef so the next call doesn't re-fetch."""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"provider_id": "ACoAA-token", "object": "User"})

    prov = _provider_with(handler)
    profile = ProfileRef(public_id="slug-only")
    out = await prov._resolve_provider_id("up-acct-XYZ", profile)
    assert out == "ACoAA-token"
    assert "/api/v1/users/slug-only" in str(seen[0].url)
    # Subsequent calls reuse the cached urn.
    assert profile.urn == "ACoAA-token"


# --------------------------------------------------------------------------
# send_connect / send_dm / invite_to_page / inmail / comment
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_connect_uses_invite_endpoint_with_message_trim():
    seen: list[httpx.Request] = []
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"invitation_id": "INV-1"})
    prov = _provider_with(handler)
    note = "x" * 250  # over 200-char floor
    res = await prov.send_connect_request(
        _FakeAccount(), ProfileRef(public_id="j", urn="urn:li:fsd_profile:J"), note=note,
    )
    assert res.ok is True
    body = json.loads(seen[0].content)
    assert body["provider_id"] == "urn:li:fsd_profile:J"
    assert body["account_id"] == "up-acct-XYZ"
    assert len(body["message"]) == 200


@pytest.mark.asyncio
async def test_send_connect_omits_message_when_none():
    seen: list[httpx.Request] = []
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={})
    prov = _provider_with(handler)
    await prov.send_connect_request(
        _FakeAccount(), ProfileRef(public_id="j", urn="urn:li:fsd_profile:J"), note=None,
    )
    body = json.loads(seen[0].content)
    assert "message" not in body


@pytest.mark.asyncio
async def test_send_dm_starts_chat_with_text():
    seen: list[httpx.Request] = []
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"chat_id": "C-1", "message_id": "M-1"})
    prov = _provider_with(handler)
    res = await prov.send_dm(
        _FakeAccount(), ProfileRef(public_id="j", urn="urn:li:fsd_profile:J"), text="hi there",
    )
    assert res.ok is True
    body = json.loads(seen[0].content)
    assert body == {
        "account_id": "up-acct-XYZ",
        "attendees_ids": ["urn:li:fsd_profile:J"],
        "text": "hi there",
    }
    assert res.external_id == "M-1"


@pytest.mark.asyncio
async def test_invite_to_page_posts_voyager_batch_create():
    """invite_to_page goes through the raw Voyager passthrough with the
    exact shape from Unipile's official example (get-raw-data-example
    docs): pre-encoded inviter URN in query_params, x-restli-method
    batch_create header, encoding false."""
    seen: list[httpx.Request] = []
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"elements": []})
    prov = _provider_with(handler)
    res = await prov.invite_to_page(
        _FakeAccount(),
        ProfileRef(public_id="j", urn="urn:li:fsd_profile:J"),
        page_id="9876",
    )
    assert res.ok is True
    assert res.external_id == "urn:li:fsd_profile:J"
    assert seen[0].method == "POST"
    assert "/api/v1/linkedin" in str(seen[0].url)
    body = json.loads(seen[0].content)
    assert body["account_id"] == "up-acct-XYZ"
    assert body["method"] == "POST"
    assert body["request_url"].endswith("/voyager/api/voyagerRelationshipsDashInvitations")
    assert body["body"] == {
        "elements": [
            {
                "inviteeMember": "urn:li:fsd_profile:J",
                "genericInvitationType": "ORGANIZATION",
            }
        ]
    }
    assert body["query_params"] == {
        "inviter": "(organizationUrn:urn%3Ali%3Afsd_company%3A9876)",
    }
    assert body["headers"] == {"x-restli-method": "batch_create"}
    assert body["encoding"] is False


@pytest.mark.asyncio
async def test_invite_to_page_normalizes_bare_provider_id():
    """A bare member token from the resolver hop gets the full
    ``urn:li:fsd_profile:`` prefix before landing in inviteeMember."""
    seen: list[httpx.Request] = []
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.method == "GET" and "/api/v1/users/" in str(req.url):
            return httpx.Response(200, json={"provider_id": "ACoAA-bare"})
        return httpx.Response(200, json={})
    prov = _provider_with(handler)
    res = await prov.invite_to_page(
        _FakeAccount(), ProfileRef(public_id="j"), page_id="9876",
    )
    assert res.ok is True
    body = json.loads(seen[-1].content)
    assert body["body"]["elements"][0]["inviteeMember"] == "urn:li:fsd_profile:ACoAA-bare"


@pytest.mark.asyncio
async def test_invite_to_page_no_id_returns_error():
    seen: list[httpx.Request] = []
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={})
    prov = _provider_with(handler)
    res = await prov.invite_to_page(_FakeAccount(), ProfileRef(), page_id="9876")
    assert res.ok is False
    assert seen == []


@pytest.mark.asyncio
async def test_invite_to_page_maps_unipile_error():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"type": "errors/malformed_request", "title": "bad shape"},
        )
    prov = _provider_with(handler)
    res = await prov.invite_to_page(
        _FakeAccount(),
        ProfileRef(public_id="j", urn="urn:li:fsd_profile:J"),
        page_id="9876",
    )
    assert res.ok is False
    assert (res.meta or {}).get("code") == "errors/malformed_request"


@pytest.mark.asyncio
async def test_send_inmail_signals_premium_required_when_unipile_says_so():
    """The resolver hop returns a provider_id; the POST /chats that follows
    returns 402, and we map that to premium_required."""
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/api/v1/users/" in str(req.url):
            return httpx.Response(200, json={"provider_id": "ACoAA-classic"})
        return httpx.Response(
            402,
            json={"type": "errors/premium_required", "title": "no inmail credits"},
        )
    prov = _provider_with(handler)
    res = await prov.send_inmail(_FakeAccount(), ProfileRef(public_id="j"), subject="hi", body="msg")
    assert res.ok is False
    assert (res.meta or {}).get("premium_required") is True


@pytest.mark.asyncio
async def test_send_inmail_uses_form_encoded_sales_navigator_body():
    """Happy path: form-encoded body with bracket-notation linkedin[*] keys.
    Unipile translates the classic provider_id to its Sales Nav variant
    internally when ``linkedin[api]=sales_navigator`` is set."""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.method == "GET" and "/api/v1/users/" in str(req.url):
            return httpx.Response(200, json={"provider_id": "ACoAA-classic"})
        return httpx.Response(201, json={"object": "ChatStarted", "chat_id": "C-9"})

    prov = _provider_with(handler)
    res = await prov.send_inmail(
        _FakeAccount(),
        ProfileRef(public_id="j"),
        subject="Quick question",
        body="Hello, this is an InMail.",
    )
    assert res.ok is True
    post_req = next(r for r in seen if r.method == "POST")
    assert str(post_req.url).endswith("/api/v1/chats")
    assert post_req.headers["content-type"].startswith("application/x-www-form-urlencoded")
    from urllib.parse import parse_qsl
    pairs = parse_qsl(post_req.content.decode(), keep_blank_values=True)
    assert ("account_id", "up-acct-XYZ") in pairs
    assert ("attendees_ids", "ACoAA-classic") in pairs
    assert ("subject", "Quick question") in pairs
    assert ("text", "Hello, this is an InMail.") in pairs
    assert ("linkedin[api]", "sales_navigator") in pairs
    assert ("linkedin[inmail]", "true") in pairs


@pytest.mark.asyncio
async def test_comment_on_post_posts_to_comments_endpoint():
    seen: list[httpx.Request] = []
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"comment_id": "CMT-1"})
    prov = _provider_with(handler)
    res = await prov.comment_on_post(_FakeAccount(), "urn:li:share:1", "nice post")
    assert res.ok is True
    body = json.loads(seen[0].content)
    assert body == {"account_id": "up-acct-XYZ", "text": "nice post"}
    # account_id no longer in the query string.
    assert "account_id" not in str(seen[0].url)


# --------------------------------------------------------------------------
# inbox_recent_events — polling fallback
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbox_recent_events_returns_events_after_since():
    since = datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)
    new_ts = (since + timedelta(minutes=10)).isoformat()
    old_ts = (since - timedelta(minutes=10)).isoformat()

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "items": [
                {
                    "id": "chat-new",
                    "last_message": {
                        "timestamp": new_ts,
                        "text": "hello back",
                        "sender": {"provider_id": "lead-1", "urn": "urn:li:fsd_profile:ABC"},
                    },
                },
                {
                    "id": "chat-old",
                    "last_message": {
                        "timestamp": old_ts,
                        "text": "ignore me",
                        "sender": {"provider_id": "lead-old"},
                    },
                },
            ]
        })
    prov = _provider_with(handler)
    events = await prov.inbox_recent_events(_FakeAccount(), since)
    assert len(events) == 1
    assert events[0].kind == "message_received"
    assert events[0].from_public_id == "lead-1"
    assert events[0].message_text == "hello back"


@pytest.mark.asyncio
async def test_inbox_recent_events_returns_empty_on_error():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})
    prov = _provider_with(handler)
    events = await prov.inbox_recent_events(_FakeAccount(), datetime.now(timezone.utc))
    assert events == []
