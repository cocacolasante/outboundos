"""LinkedIn provider backed by Unipile's hosted API.

Unipile runs real desktop Chrome browsers in cloud VMs on residential IPs.
We never handle LinkedIn credentials or run a browser ourselves — the user
links their LinkedIn account once through Unipile's hosted-auth flow and
we just store the resulting ``unipile_account_id``.  All write actions
(connect, DM, follow, etc.) become straightforward HTTPS calls to Unipile.

This replaces the older Playwright/hybrid/HTTP impls which were
permanently fighting LinkedIn's bot-detection.  Real browser + residential
IP defeats the detection layer because LinkedIn sees a genuine human-shaped
session.

SECURITY: this module never touches ``encryption.decrypt`` — there is no
LinkedIn password to decrypt.  The Unipile API key is read from
``settings.UNIPILE_API_KEY`` and sent in an ``X-API-KEY`` header.

API SHAPE NOTES:
Unipile's REST surface uses ``/api/v1/...`` paths and is rooted at
``https://<DSN>`` where DSN is the customer's tenant host (e.g.
``api12.unipile.com:13443``).  Some endpoint paths and payload shapes
below are documented as the canonical form Unipile publishes — they
occasionally evolve.  All of them are confined to this module, so when
Unipile changes one we only change one place.  When a method maps to
multiple Unipile endpoints (e.g. send_dm: find chat → send message), the
sequence is spelled out in each method.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import settings
from app.services.linkedin.base import (
    AccountRestricted,
    ActionResult,
    ChallengeRequired,
    InboundEvent,
    LinkedInProvider,
    LinkedInProviderError,
    ProfileRef,
)

logger = logging.getLogger(__name__)

# -- HTTP timeouts --------------------------------------------------------
# Reads can be fast; writes can include LinkedIn's slow Voyager calls
# behind Unipile's proxy.  Account creation polls until LinkedIn either
# returns a session or asks for a checkpoint, which can take 30-60s.
_REQUEST_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
_LONG_TIMEOUT = httpx.Timeout(180.0, connect=10.0)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class UnipileError(LinkedInProviderError):
    """Unipile API returned a non-2xx response we can't recover from.

    Attributes ``status``, ``code``, and ``body`` carry the raw signal so
    upstream callers (sequencer, router) can branch on them.
    """

    def __init__(self, status: int, code: str | None, body: str, message: str | None = None):
        self.status = status
        self.code = code
        self.body = body
        super().__init__(message or f"unipile {status} {code or ''}: {body[:200]}")


# --------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------


class UnipileLinkedInProvider(LinkedInProvider):
    """LinkedInProvider impl that delegates every action to Unipile."""

    PROVIDER = "LINKEDIN"

    def __init__(
        self,
        *,
        dsn: str | None = None,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # `None` ⇒ fall back to settings; `""` ⇒ explicitly empty (tests use
        # this to assert the "no DSN configured" error path).
        self._dsn = dsn if dsn is not None else settings.UNIPILE_DSN
        self._api_key = api_key if api_key is not None else settings.UNIPILE_API_KEY
        # Tests inject httpx.MockTransport here.
        self._transport = transport

    # ----- HTTP plumbing -------------------------------------------------

    def _base_url(self) -> str:
        if not self._dsn:
            raise UnipileError(
                0, "config_missing",
                "UNIPILE_DSN not set — required for the Unipile provider.",
                message="Unipile is not configured (UNIPILE_DSN missing).",
            )
        # DSN already includes the host (and usually a non-443 port).
        # If the caller supplied a bare host we still want https.
        if "://" in self._dsn:
            return self._dsn.rstrip("/")
        return f"https://{self._dsn}".rstrip("/")

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise UnipileError(
                0, "config_missing",
                "UNIPILE_API_KEY not set — required for the Unipile provider.",
                message="Unipile is not configured (UNIPILE_API_KEY missing).",
            )
        return {
            "X-API-KEY": self._api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _client(self, *, long: bool = False) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "base_url": self._base_url(),
            "headers": self._headers(),
            "timeout": _LONG_TIMEOUT if long else _REQUEST_TIMEOUT,
            # Follow redirects: Unipile 301-redirects GET /users/{slug} to a
            # Unicode-normalized (NFD + percent-encoded) URL when a LinkedIn
            # public identifier contains accented characters (e.g.
            # "rahnà-wakę-..." -> "rahna%CC%80-wake%CC%A8-...").  Without this
            # httpx returns the 301's HTML "Redirecting" page, which we'd
            # surface as a bogus failure and the lead's profile never resolves.
            "follow_redirects": True,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        data: list[tuple[str, Any]] | dict[str, Any] | None = None,
        long: bool = False,
    ) -> dict[str, Any] | list[Any]:
        """Issue a single request and normalize Unipile's error envelope.

        Pass ``json=`` for JSON bodies, or ``data=`` for form-encoded
        bodies (Unipile's InMail path requires form encoding with
        bracket-notation keys like ``linkedin[inmail]``).  When ``data``
        is set the Content-Type header is overridden accordingly.
        """
        headers = None
        content_bytes: bytes | None = None
        if data is not None:
            # Pre-encode so httpx ships an AsyncByteStream (passing a raw
            # dict/list to ``data=`` produces a sync ByteStream which the
            # AsyncClient rejects).
            from urllib.parse import urlencode
            pairs = list(data.items()) if isinstance(data, dict) else list(data)
            content_bytes = urlencode(pairs).encode("utf-8")
            headers = dict(self._headers())
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        async with self._client(long=long) as client:
            try:
                if content_bytes is not None:
                    resp = await client.request(
                        method, path, params=params,
                        content=content_bytes, headers=headers,
                    )
                else:
                    resp = await client.request(method, path, params=params, json=json)
            except httpx.RequestError as exc:
                logger.warning("Unipile %s %s network error: %s", method, path, exc)
                raise UnipileError(
                    0, "network",
                    str(exc),
                    message=f"unipile network error: {exc}",
                ) from exc

        if resp.status_code == 204:
            return {}

        text = resp.text
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = None

        if 200 <= resp.status_code < 300:
            if isinstance(body, (dict, list)):
                return body
            return {"raw": text}

        # Error path — Unipile envelopes typically look like:
        #   {"status": 422, "type": "errors/checkpoint", "title": "...", "detail": "..."}
        # or
        #   {"code": 422, "type": "...", "title": "...", "message": "..."}
        code: str | None = None
        message: str | None = None
        if isinstance(body, dict):
            code = body.get("type") or body.get("code") or body.get("error")
            message = body.get("detail") or body.get("title") or body.get("message")
        if not message:
            message = text[:300]
        logger.warning(
            "Unipile %s %s -> %s code=%s body=%r",
            method, path, resp.status_code, code, text[:300],
        )
        self._raise_for_special_codes(resp.status_code, code, text)
        raise UnipileError(resp.status_code, code, text, message=message)

    @staticmethod
    def _raise_for_special_codes(status: int, code: str | None, body: str) -> None:
        """Map Unipile error codes to our domain exceptions.

        We only match the ``code`` (Unipile's stable error-type identifier),
        not the prose detail — earlier impls grepped the body which
        false-positived on things like ``errors/resource_access_restricted``
        (a Unipile plan/permission error, not a LinkedIn account state).
        """
        code_str = (code or "").lower()
        if any(t in code_str for t in (
            "checkpoint", "captcha", "2fa", "otp",
        )):
            raise ChallengeRequired(
                f"Unipile reports LinkedIn checkpoint required ({status})",
                challenge_url="https://www.linkedin.com",
            )
        # Be precise: only the LinkedIn-account-state errors map to
        # AccountRestricted.  ``errors/resource_access_restricted`` and
        # similar API-access errors are plain UnipileErrors.
        if code_str in {
            "errors/account_restricted",
            "errors/account_banned",
            "errors/account_suspended",
            "errors/linkedin_restricted",
            "errors/linkedin_banned",
        }:
            raise AccountRestricted(
                f"Unipile reports LinkedIn account restricted ({status})"
            )

    # ----- Account-level helpers (used by router + provider methods) -----

    @staticmethod
    def _account_id(account: Any) -> str:
        aid = getattr(account, "unipile_account_id", None)
        if not aid:
            raise UnipileError(
                0, "no_account",
                "LinkedInAccount has no unipile_account_id; finish the hosted login flow first.",
                message="LinkedIn account has not completed the Unipile hosted login.",
            )
        return aid

    async def create_hosted_auth_link(
        self,
        *,
        success_redirect_url: str,
        failure_redirect_url: str | None = None,
        notify_url: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Ask Unipile for a hosted-auth URL the user can open in their browser.

        Used by the linkedin-accounts router when the user clicks
        "Connect via Unipile".  Unipile returns ``{ "url": "...", "id": "..." }``
        — the URL we open in a new tab, the ID we'll later match against
        webhook ``account.connected`` events to persist
        ``unipile_account_id``.
        """
        payload: dict[str, Any] = {
            "type": "create",
            "providers": [self.PROVIDER],
            "api_url": self._base_url(),
            "success_redirect_url": success_redirect_url,
        }
        if failure_redirect_url:
            payload["failure_redirect_url"] = failure_redirect_url
        if notify_url:
            payload["notify_url"] = notify_url
        if name:
            payload["name"] = name
        result = await self._request("POST", "/api/v1/hosted/accounts/link", json=payload)
        return result if isinstance(result, dict) else {"raw": result}

    async def fetch_account_status(self, unipile_account_id: str) -> dict[str, Any]:
        """Return Unipile's view of the account (status, provider, last error)."""
        result = await self._request("GET", f"/api/v1/accounts/{unipile_account_id}")
        return result if isinstance(result, dict) else {"raw": result}

    async def list_accounts(self) -> list[dict[str, Any]]:
        """Return every account currently linked to this Unipile tenant.

        Used to import accounts that were connected via Unipile's dashboard
        (outside our portal flow) — we can present them as "discoverable"
        and let the user bind one to a fresh local ``LinkedInAccount`` row.
        Filters to LinkedIn-typed accounts; ignores GOOGLE_OAUTH / IMAP / etc.
        """
        result = await self._request("GET", "/api/v1/accounts")
        if not isinstance(result, dict):
            return []
        items = result.get("items") or []
        return [a for a in items if str(a.get("type", "")).upper() == "LINKEDIN"]

    async def delete_account(self, unipile_account_id: str) -> None:
        """Tear down a linked account on Unipile's side (user removed it locally)."""
        await self._request("DELETE", f"/api/v1/accounts/{unipile_account_id}")

    # ----- LinkedInProvider abstract methods -----------------------------

    async def test_connection(self, account: Any) -> ActionResult:
        """Verify the linked Unipile account is still authenticated.

        We just hit Unipile's account-status endpoint.  Unipile owns the
        LinkedIn session; if it can't reach LinkedIn (checkpoint, expired
        cookies, etc.) it surfaces that through the account.status field.
        """
        try:
            aid = self._account_id(account)
            status = await self.fetch_account_status(aid)
        except ChallengeRequired as exc:
            account.pending_challenge_url = exc.challenge_url or "https://www.linkedin.com"
            return ActionResult(ok=False, error=str(exc), meta={"challenged": True})
        except UnipileError as exc:
            return ActionResult(ok=False, error=str(exc), meta={"code": exc.code, "http_status": exc.status})

        # Unipile statuses we care about:
        #   "OK" / "CONNECTED" / "ACTIVE"        — good
        #   "CREDENTIALS" / "DISCONNECTED"       — re-auth needed
        #   "CHECKPOINT"                         — challenge
        #   "BANNED" / "RESTRICTED"              — penalty box
        # GET /accounts/{id} returns the per-data-stream status under
        # ``sources[].status`` rather than a top-level field, so we fall
        # back to that when the rollup fields are missing.
        srcstatus = (
            status.get("status")
            or status.get("connection_status")
            or next(
                (s.get("status") for s in (status.get("sources") or [])
                 if isinstance(s, dict) and s.get("status")),
                None,
            )
            or ""
        )
        srcstatus = str(srcstatus).upper()
        if srcstatus in {"OK", "CONNECTED", "ACTIVE"}:
            return ActionResult(ok=True, meta=status)
        if "CHECKPOINT" in srcstatus or "2FA" in srcstatus:
            account.pending_challenge_url = "https://www.linkedin.com"
            return ActionResult(
                ok=False, error=f"unipile status={srcstatus}", meta={"challenged": True, **status},
            )
        if "BANNED" in srcstatus or "RESTRICTED" in srcstatus or "SUSPENDED" in srcstatus:
            return ActionResult(
                ok=False, error=f"unipile status={srcstatus}",
                meta={"restricted": True, **status},
            )
        return ActionResult(ok=False, error=f"unipile status={srcstatus or 'unknown'}", meta=status)

    async def _resolve_provider_id(
        self, account_id: str, profile: ProfileRef
    ) -> str | None:
        """Turn a ProfileRef into Unipile's canonical member provider_id.

        Many Unipile endpoints (``/users/invite``, ``/chats``,
        ``/users/follow``, ``/users/{id}/posts``) reject LinkedIn public
        slugs with ``errors/invalid_recipient`` or "User ID does not match
        provider's expected format" — they require the
        ``ACoAA...`` token returned by ``GET /users/{slug}``.

        If the ProfileRef already carries a ``urn`` we trust it.  Otherwise
        we resolve via a single profile fetch and return the
        ``provider_id`` field from the response (caching the result on the
        ProfileRef so a follow-up call in the same step doesn't re-fetch).
        """
        if profile.urn:
            return profile.urn
        if not profile.public_id:
            return None
        resolved = await self._request(
            "GET", f"/api/v1/users/{profile.public_id}",
            params={"account_id": account_id},
        )
        if isinstance(resolved, dict):
            provider_id = (
                resolved.get("provider_id")
                or resolved.get("id")
                or resolved.get("urn")
            )
            if provider_id:
                profile.urn = provider_id
                return provider_id
        return None

    async def view_profile(self, account: Any, profile: ProfileRef) -> ActionResult:
        """Register a profile view (ghost view) via Unipile's user-fetch endpoint.

        Unipile's ``GET /users/{provider_id}?account_id=X`` fetches the
        profile through the user's session, which surfaces in LinkedIn's
        "Who viewed your profile" feed the same way a manual visit does.
        """
        try:
            aid = self._account_id(account)
            provider_id = profile.public_id or profile.urn
            if not provider_id:
                return ActionResult(ok=False, error="view_profile requires a public_id or urn")
            data = await self._request(
                "GET", f"/api/v1/users/{provider_id}",
                params={"account_id": aid},
            )
        except ChallengeRequired as exc:
            account.pending_challenge_url = exc.challenge_url or "https://www.linkedin.com"
            raise
        except UnipileError as exc:
            return ActionResult(ok=False, error=str(exc), meta={"code": exc.code, "http_status": exc.status})

        urn = None
        if isinstance(data, dict):
            urn = data.get("provider_id") or data.get("urn") or data.get("id")
        return ActionResult(ok=True, external_id=urn, meta=data if isinstance(data, dict) else None)

    async def follow_profile(self, account: Any, profile: ProfileRef) -> ActionResult:
        """Follow a LinkedIn profile.

        Unipile doesn't package a follow endpoint — it has to go through
        their raw Voyager passthrough at ``POST /api/v1/linkedin``, hitting
        LinkedIn's ``followingStates`` patch URL with the member's
        ``fsd_profile`` URN.  Confirmed against the Unipile developer docs
        + their Node SDK source.
        """
        try:
            aid = self._account_id(account)
            provider_id = await self._resolve_provider_id(aid, profile)
            if not provider_id:
                return ActionResult(ok=False, error="follow_profile requires a public_id or urn")
            # Normalise — Unipile returns the bare member token; LinkedIn's
            # followingStates URL needs the full ``urn:li:fsd_profile:`` URN.
            if provider_id.startswith("urn:li:fsd_profile:"):
                fsd_urn = provider_id
            else:
                fsd_urn = f"urn:li:fsd_profile:{provider_id}"
            data = await self._request(
                "POST", "/api/v1/linkedin",
                json={
                    "account_id": aid,
                    "method": "POST",
                    "request_url": (
                        "https://www.linkedin.com/voyager/api/feed/dash/"
                        f"followingStates/urn:li:fsd_followingState:{fsd_urn}"
                    ),
                    "body": {"patch": {"$set": {"following": True}}},
                    "encoding": False,
                },
            )
        except ChallengeRequired as exc:
            account.pending_challenge_url = exc.challenge_url or "https://www.linkedin.com"
            raise
        except UnipileError as exc:
            return ActionResult(ok=False, error=str(exc), meta={"code": exc.code, "http_status": exc.status})
        return ActionResult(ok=True, external_id=provider_id, meta=data if isinstance(data, dict) else None)

    async def react_to_post(
        self, account: Any, post_urn: str, reaction: str = "LIKE"
    ) -> ActionResult:
        try:
            aid = self._account_id(account)
            # Body-based endpoint (no URN in path) — the `/posts/{urn}/reactions`
            # path-style variant 404s.  Returns 201 ``{"object":"ReactionAdded"}``
            # on success.
            data = await self._request(
                "POST", "/api/v1/posts/reaction",
                json={
                    "account_id": aid,
                    "post_id": post_urn,
                    "type": (reaction or "LIKE").upper(),
                },
            )
        except ChallengeRequired as exc:
            account.pending_challenge_url = exc.challenge_url or "https://www.linkedin.com"
            raise
        except UnipileError as exc:
            return ActionResult(ok=False, error=str(exc), meta={"code": exc.code, "http_status": exc.status})
        return ActionResult(ok=True, external_id=post_urn, meta=data if isinstance(data, dict) else None)

    async def latest_post_urn(self, account: Any, profile: ProfileRef) -> str | None:
        try:
            aid = self._account_id(account)
            provider_id = await self._resolve_provider_id(aid, profile)
            if not provider_id:
                return None
            data = await self._request(
                "GET", f"/api/v1/users/{provider_id}/posts",
                params={"account_id": aid, "limit": 1},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unipile latest_post_urn failed: %s", exc)
            return None
        items: list[Any] = []
        if isinstance(data, dict):
            items = data.get("items") or data.get("posts") or []
        elif isinstance(data, list):
            items = data
        if not items:
            return None
        first = items[0] if isinstance(items[0], dict) else {}
        return first.get("urn") or first.get("provider_id") or first.get("id")

    async def recent_posts(
        self, account: Any, profile: ProfileRef, limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Return up to ``limit`` of this profile's most-recent posts as
        raw dicts (caller maps into ``DiscoveredPost``).  Used by the
        Social Listening Radar watchlist — fetches LinkedIn posts
        DIRECTLY via the platform without any indexing dependency.

        Empty list on any failure (auth, unknown profile, network) —
        callers treat soft-fail as "no posts for this profile this run".
        """
        try:
            aid = self._account_id(account)
            provider_id = await self._resolve_provider_id(aid, profile)
            if not provider_id:
                return []
            data = await self._request(
                "GET", f"/api/v1/users/{provider_id}/posts",
                params={"account_id": aid, "limit": max(1, min(limit, 25))},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unipile recent_posts failed: %s", exc)
            return []
        items: list[Any] = []
        if isinstance(data, dict):
            items = data.get("items") or data.get("posts") or []
        elif isinstance(data, list):
            items = data
        return [it for it in items if isinstance(it, dict)]

    async def send_connect_request(
        self, account: Any, profile: ProfileRef, note: str | None = None
    ) -> ActionResult:
        try:
            aid = self._account_id(account)
            provider_id = await self._resolve_provider_id(aid, profile)
            if not provider_id:
                return ActionResult(ok=False, error="send_connect_request requires a public_id or urn")
            payload: dict[str, Any] = {
                "provider_id": provider_id,
                "account_id": aid,
            }
            if note and note.strip():
                # LinkedIn enforces 200 char cap for Free, 300 for Premium.
                # 200 is the safe floor.
                payload["message"] = note.strip()[:200]
            data = await self._request(
                "POST", "/api/v1/users/invite",
                json=payload,
                long=True,
            )
        except ChallengeRequired as exc:
            account.pending_challenge_url = exc.challenge_url or "https://www.linkedin.com"
            raise
        except UnipileError as exc:
            return ActionResult(ok=False, error=str(exc), meta={"code": exc.code, "http_status": exc.status})
        return ActionResult(ok=True, external_id=provider_id, meta=data if isinstance(data, dict) else None)

    async def send_dm(self, account: Any, profile: ProfileRef, text: str) -> ActionResult:
        try:
            aid = self._account_id(account)
            provider_id = await self._resolve_provider_id(aid, profile)
            if not provider_id:
                return ActionResult(ok=False, error="send_dm requires a public_id or urn")
            # Unipile's "start chat" endpoint takes the attendee provider_id and
            # the message body in one call (no need to look up an existing chat).
            data = await self._request(
                "POST", "/api/v1/chats",
                json={
                    "account_id": aid,
                    "attendees_ids": [provider_id],
                    "text": text,
                },
                long=True,
            )
        except ChallengeRequired as exc:
            account.pending_challenge_url = exc.challenge_url or "https://www.linkedin.com"
            raise
        except UnipileError as exc:
            return ActionResult(ok=False, error=str(exc), meta={"code": exc.code, "http_status": exc.status})

        msg_id = None
        if isinstance(data, dict):
            msg_id = data.get("message_id") or data.get("chat_id") or data.get("id")
        return ActionResult(ok=True, external_id=msg_id, meta=data if isinstance(data, dict) else None)

    async def invite_to_page(
        self, account: Any, profile: ProfileRef, page_id: str
    ) -> ActionResult:
        """Invite a connection to follow a company page.

        Blocked on Unipile's side, not ours.  The correct Voyager endpoint
        was HAR-captured from LinkedIn's admin UI:

            POST /voyager/api/voyagerRelationshipsDashInvitations
                ?inviter=(organizationUrn:urn:li:fsd_company:<PAGE_ID>)
            x-restli-method: batch_create
            body: {"elements":[{"inviteeMember":"urn:li:fsd_profile:<MEMBER>",
                                "genericInvitationType":"ORGANIZATION"}]}

        and routed through Unipile's raw passthrough at
        ``POST /api/v1/linkedin`` (the same passthrough ``follow_profile``
        uses successfully).  Both the inlined-query and the documented
        ``query_params`` forms return Unipile's
        ``errors/malformed_request`` from their forwarder.  Direct probing
        shows Unipile's passthrough rejects *any* request to
        ``voyagerRelationshipsDashInvitations`` (and to other voyager
        paths like ``identity/profiles/me``) — the passthrough whitelist
        is narrower than the docs suggest.  ``follow_profile`` works only
        because ``feed/dash/followingStates`` is on the allowlist.

        To unblock: contact Unipile support and ask them to whitelist
        ``voyagerRelationshipsDashInvitations`` for our workspace, or
        request that they package the action natively.  Once they do,
        re-add ``LINKEDIN_INVITE_TO_PAGE`` to ``PUBLISHABLE_KINDS_M1``
        and restore the impl from git history (commit message will
        mention "wire invite_to_page via Voyager passthrough").
        """
        return ActionResult(
            ok=False,
            error=(
                "linkedin_invite_to_page is blocked on Unipile's passthrough "
                "whitelist — the voyagerRelationshipsDashInvitations endpoint "
                "is not allowlisted.  Contact Unipile support to enable."
            ),
            meta={"code": "unipile_passthrough_blocked", "page_id": page_id},
        )

    async def send_inmail(
        self, account: Any, profile: ProfileRef, subject: str, body: str
    ) -> ActionResult:
        """Send a LinkedIn InMail via Sales Navigator.

        Unipile reuses ``POST /api/v1/chats`` for InMail but with three
        wrinkles documented in their reference:
          1. Body MUST be form-encoded (JSON is silently rewritten and the
             ``linkedin[*]`` nested object never reaches LinkedIn).
          2. The ``linkedin`` envelope uses bracket-notation form keys:
             ``linkedin[api]=sales_navigator`` + ``linkedin[inmail]=true``.
          3. The attendee id must be the **Sales Nav** flavour of the
             provider_id (``ACw...``), not the classic feed provider_id
             (``ACoAA...``).  We fetch it via
             ``GET /users/{public_id}?linkedin_sections=sales_navigator``.
        """
        try:
            aid = self._account_id(account)
            sn_id = await self._resolve_provider_id(aid, profile)
            if not sn_id:
                return ActionResult(ok=False, error="send_inmail requires a public_id or urn")
            # Pass the classic provider_id; Unipile resolves the Sales Nav
            # variant internally when ``linkedin[api]=sales_navigator`` is
            # set in the form body.  A separate ``linkedin_sections`` param
            # accepts only profile-section enums (about/experience/etc.),
            # not API flavours.
            form = [
                ("account_id", aid),
                ("attendees_ids", sn_id),
                ("text", body),
                ("subject", subject),
                ("linkedin[api]", "sales_navigator"),
                ("linkedin[inmail]", "true"),
            ]
            data = await self._request(
                "POST", "/api/v1/chats", data=form, long=True,
            )
        except ChallengeRequired as exc:
            account.pending_challenge_url = exc.challenge_url or "https://www.linkedin.com"
            raise
        except UnipileError as exc:
            # Unipile signals "no InMail credits / not Premium" via a specific
            # error code; surface that distinctly so the sequencer can route
            # it to a clean "premium_required" skip rather than a hard failure.
            if exc.code and ("premium" in str(exc.code).lower() or "inmail" in str(exc.code).lower()):
                return ActionResult(
                    ok=False, error=str(exc),
                    meta={"premium_required": True, "code": exc.code},
                )
            return ActionResult(ok=False, error=str(exc), meta={"code": exc.code, "http_status": exc.status})
        return ActionResult(ok=True, external_id=sn_id, meta=data if isinstance(data, dict) else None)

    async def comment_on_post(
        self, account: Any, post_urn: str, comment: str
    ) -> ActionResult:
        try:
            aid = self._account_id(account)
            # account_id goes in the body, not the query string — Unipile
            # 400s with "Required property" otherwise.
            data = await self._request(
                "POST", f"/api/v1/posts/{post_urn}/comments",
                json={"account_id": aid, "text": comment},
            )
        except ChallengeRequired as exc:
            account.pending_challenge_url = exc.challenge_url or "https://www.linkedin.com"
            raise
        except UnipileError as exc:
            return ActionResult(ok=False, error=str(exc), meta={"code": exc.code, "http_status": exc.status})
        return ActionResult(ok=True, external_id=post_urn, meta=data if isinstance(data, dict) else None)

    async def inbox_recent_events(
        self, account: Any, since: datetime
    ) -> list[InboundEvent]:
        """Used as a polling fallback when webhooks aren't reaching us.

        With Unipile, the preferred path is the /webhooks/unipile endpoint —
        Unipile pushes events to us in real time so we don't need to poll.
        This method stays available for backfill / dev / one-shot resync.
        """
        try:
            aid = self._account_id(account)
            # Unipile's `after` param requires strict ISO-8601 with
            # millisecond precision and a literal Z suffix
            # (`YYYY-MM-DDTHH:MM:SS.sssZ`). Python's default isoformat()
            # emits microseconds + `+00:00`, which Unipile 400s on.
            utc = since.astimezone(timezone.utc)
            since_iso = utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"
            data = await self._request(
                "GET", "/api/v1/chats",
                params={"account_id": aid, "limit": 20, "after": since_iso},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unipile inbox_recent_events failed: %s", exc)
            return []

        chats: list[Any] = []
        if isinstance(data, dict):
            chats = data.get("items") or data.get("chats") or []
        elif isinstance(data, list):
            chats = data

        events: list[InboundEvent] = []
        for chat in chats:
            if not isinstance(chat, dict):
                continue
            last_msg = chat.get("last_message") or {}
            ts_raw = last_msg.get("timestamp") or chat.get("updated_at") or chat.get("created_at")
            occurred_at = _parse_iso(ts_raw) or since
            if occurred_at <= since:
                continue
            sender = last_msg.get("sender") or {}
            from_public_id = sender.get("provider_id") or sender.get("public_identifier")
            from_urn = sender.get("urn") or sender.get("provider_urn")
            events.append(InboundEvent(
                kind="message_received",
                from_public_id=from_public_id,
                from_urn=from_urn,
                occurred_at=occurred_at,
                message_text=last_msg.get("text") or last_msg.get("body"),
                meta={"chat_id": chat.get("id") or chat.get("chat_id")},
            ))
        return events


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        s = str(value)
        # Some providers emit Z; datetime.fromisoformat needs +00:00
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None
