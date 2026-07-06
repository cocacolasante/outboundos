"""LinkedIn provider abstract base + shared types.

Concrete implementations (currently just Unipile) inherit from
``LinkedInProvider``. Workers + routers depend only on this module —
no concrete-impl imports leak above this layer.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import datetime
from typing import Any


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------


class LinkedInProviderError(Exception):
    """Base class for all provider-layer errors."""


class ChallengeRequired(LinkedInProviderError):
    """LinkedIn asked for a captcha/PIN/2FA we can't satisfy programmatically.

    The user has to resolve it in their own browser. ``challenge_url`` (if
    set) is what they should visit; otherwise they just need to log into
    linkedin.com once and re-test.
    """

    def __init__(self, message: str = "LinkedIn challenge required", challenge_url: str | None = None) -> None:
        super().__init__(message)
        self.challenge_url = challenge_url


class AccountRestricted(LinkedInProviderError):
    """LinkedIn has put the account into a penalty box. Stop all actions
    until the user clears it. Different from ``ChallengeRequired`` — there's
    no captcha to solve; the account is in time-out.
    """


# --------------------------------------------------------------------------
# Shared dataclasses
# --------------------------------------------------------------------------


@dataclass
class ProfileRef:
    """How we refer to a LinkedIn profile.

    ``public_id`` is the slug at the end of a profile URL
    (https://www.linkedin.com/in/<public_id>/). ``urn`` is the canonical
    urn:li:fsd_profile: identifier — required for reactions, messaging,
    etc.

    A ProfileRef may have one or both; callers should fill in what they
    can and ``provider.resolve(...)`` fills in the rest.
    """
    public_id: str | None = None
    urn: str | None = None

    @classmethod
    def from_url(cls, url: str) -> "ProfileRef":
        """Extract the public_id slug from a linkedin.com/in/<id>/ URL."""
        if not url:
            return cls()
        slug = url.rstrip("/").split("/")[-1]
        # Strip query strings / trailing pieces.
        slug = slug.split("?")[0]
        return cls(public_id=slug or None)


@dataclass
class ActionResult:
    """Uniform return from any provider action."""
    ok: bool
    external_id: str | None = None  # URN or provider-action-id when applicable
    meta: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class InboundEvent:
    """One inbound event polled from LinkedIn (connection accept, DM, etc.).

    Used to update Lead.linkedin_connection_status / linkedin_last_reply_at.
    """
    kind: str  # "connection_accepted" | "message_received" | "invitation_received"
    from_public_id: str | None
    from_urn: str | None
    occurred_at: datetime
    message_text: str | None = None
    meta: dict[str, Any] | None = None


# --------------------------------------------------------------------------
# Provider ABC
# --------------------------------------------------------------------------


class LinkedInProvider(abc.ABC):
    """All LinkedIn actions go through one of these.

    Concrete impls handle auth + session storage + proxy + rate-limit
    bookkeeping internally. Callers pass a ``LinkedInAccount`` model
    instance; the impl decrypts what it needs.
    """

    # ---- Auth / connectivity ----

    @abc.abstractmethod
    async def test_connection(self, account: Any) -> ActionResult:
        """Verify the account can authenticate against LinkedIn.

        On success: write fresh session cookies back to the account and
        commit. On ChallengeRequired: surface the challenge URL on
        ``account.pending_challenge_url`` and raise.
        """

    # ---- Read / warm-up actions (M2) ----

    @abc.abstractmethod
    async def view_profile(self, account: Any, profile: ProfileRef) -> ActionResult:
        """Fetch a profile (registers a "ghost view" in the lead's notifications)."""

    @abc.abstractmethod
    async def follow_profile(self, account: Any, profile: ProfileRef) -> ActionResult:
        """Follow a profile (low-touch warm-up)."""

    @abc.abstractmethod
    async def react_to_post(
        self, account: Any, post_urn: str, reaction: str = "LIKE"
    ) -> ActionResult:
        """React (like/celebrate/etc.) to a post."""

    @abc.abstractmethod
    async def latest_post_urn(
        self, account: Any, profile: ProfileRef
    ) -> str | None:
        """Return the URN of the lead's most recent post, or None if no posts
        / can't fetch. Used by react_to_post nodes that target "latest".
        """

    # ---- Inbox / status polling (M2) ----

    @abc.abstractmethod
    async def inbox_recent_events(
        self, account: Any, since: datetime
    ) -> list[InboundEvent]:
        """Return events newer than ``since``. Used by the poller to update
        Lead.linkedin_connection_status / linkedin_last_reply_at.
        """

    # ---- Write actions (M3 — stub raises NotImplementedError in M2) ----

    async def send_connect_request(
        self, account: Any, profile: ProfileRef, note: str | None = None
    ) -> ActionResult:
        raise NotImplementedError("send_connect_request lands in M3")

    async def send_dm(
        self, account: Any, profile: ProfileRef, text: str
    ) -> ActionResult:
        raise NotImplementedError("send_dm lands in M3")

    async def invite_to_page(
        self, account: Any, profile: ProfileRef, page_id: str
    ) -> ActionResult:
        raise NotImplementedError("invite_to_page lands in M3")

    async def send_inmail(
        self, account: Any, profile: ProfileRef, subject: str, body: str,
    ) -> ActionResult:
        """Send a LinkedIn InMail to a 2nd/3rd-degree connection.
        Requires Premium/Sales Nav on the calling account.
        """
        raise NotImplementedError("send_inmail lands in M4")

    async def comment_on_post(
        self, account: Any, post_urn: str, comment: str,
    ) -> ActionResult:
        """Comment on a post (visible publicly — flagged as higher risk in UI)."""
        raise NotImplementedError("comment_on_post lands in M4")
