"""IMAP connectivity helpers.

SECURITY INVARIANT: This is the *only* module that ever holds a decrypted
inbox password. Higher-level callers pass a ConnectedAccount (encrypted) to
``test_imap_with_account`` / ``fetch_recent_with_account`` / ``unmark_seen_uids``; the plaintext is
created in a local variable, used immediately, and deleted before the wrapper
returns. The plaintext is never logged, persisted, or returned to callers.
"""
from __future__ import annotations

import email
import email.utils
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from imaplib import IMAP4, IMAP4_SSL
from typing import Any, TypedDict

logger = logging.getLogger(__name__)

IMAP_TIMEOUT_SECONDS = 10


class ImapTestResult(TypedDict, total=False):
    ok: bool
    message_count: int
    error: str


def test_imap_connection(
    host: str,
    port: int,
    use_ssl: bool,
    username: str,
    password: str,
) -> ImapTestResult:
    """Attempt an IMAP login and count INBOX messages.

    Returns {"ok": True, "message_count": int} on success or
    {"ok": False, "error": str} on any failure. Never raises.
    """
    cls = IMAP4_SSL if use_ssl else IMAP4
    client = None
    try:
        client = cls(host=host, port=port, timeout=IMAP_TIMEOUT_SECONDS)
        client.login(username, password)
        typ, _ = client.select("INBOX", readonly=True)
        if typ != "OK":
            return {"ok": False, "error": "INBOX select failed"}
        typ, data = client.search(None, "ALL")
        if typ != "OK" or not data or data[0] is None:
            count = 0
        else:
            count = len(data[0].split())
        return {"ok": True, "message_count": count}
    except Exception as exc:  # noqa: BLE001 — catch everything: network, auth, ssl, parse
        # Log without including credentials.
        logger.warning("IMAP test failed for %s@%s:%s — %s", username, host, port, exc)
        return {"ok": False, "error": str(exc)}
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------
# Reply polling
# --------------------------------------------------------------------------


_SUBJECT_PREFIX_RE = re.compile(r"^\s*(?:re|fwd?|fw):\s*", re.IGNORECASE)


class FetchedMessage(TypedDict, total=False):
    uid: str
    message_id: str
    in_reply_to: str
    references: list[str]
    subject: str
    from_email: str
    # Plain-text body (text/plain part preferred, stripped-HTML fallback),
    # truncated to _BODY_MAX_CHARS for the reply classifier.
    body_text: str
    # Parsed from the Date header; None when missing/unparseable.
    received_at: datetime | None


# Classifier input cap — replies longer than this carry no extra signal
# and only add input-token cost.
_BODY_MAX_CHARS = 4000

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_BLOCK_RE = re.compile(
    r"<(?:style|script)\b[^>]*>.*?</(?:style|script)>",
    re.IGNORECASE | re.DOTALL,
)


def _extract_body_text(msg: email.message.Message) -> str:
    """Pull a plain-text body from a parsed email: first ``text/plain``
    part wins; falls back to tag-stripped ``text/html``.  Truncated to
    ``_BODY_MAX_CHARS``."""
    plain: str | None = None
    html: str | None = None
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        ctype = (part.get_content_type() or "").lower()
        if ctype not in ("text/plain", "text/html"):
            continue
        if part.is_multipart():
            continue
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
        except Exception:  # noqa: BLE001 — bad charset/encoding, skip part
            continue
        if ctype == "text/plain" and plain is None:
            plain = text
            break  # plain wins; no need to keep walking
        if ctype == "text/html" and html is None:
            html = text
    body = plain
    if body is None and html is not None:
        stripped = _HTML_BLOCK_RE.sub(" ", html)
        stripped = _HTML_TAG_RE.sub(" ", stripped)
        body = re.sub(r"\s+", " ", stripped).strip()
    return (body or "")[:_BODY_MAX_CHARS]


def _parse_received_at(msg: email.message.Message) -> datetime | None:
    raw = msg.get("Date", "") or ""
    if not raw:
        return None
    try:
        return email.utils.parsedate_to_datetime(raw)
    except Exception:  # noqa: BLE001 — malformed Date header
        return None


def _clean_message_id(raw: str) -> str:
    """Strip angle brackets and whitespace from a Message-Id-style header."""
    return raw.strip().strip("<>").strip()


def _clean_subject(subject: str) -> str:
    """Strip leading Re:/Fwd: prefixes (repeated)."""
    cleaned = subject or ""
    while True:
        new = _SUBJECT_PREFIX_RE.sub("", cleaned)
        if new == cleaned:
            return cleaned.strip()
        cleaned = new


def fetch_recent_messages(
    host: str,
    port: int,
    use_ssl: bool,
    username: str,
    password: str,
    since: datetime,
    processed_message_ids: set[str] | None = None,
) -> list[FetchedMessage]:
    """Sync IMAP fetch: pull header metadata for INBOX messages dated
    ``since`` or later, skipping any whose ``Message-ID`` is already in
    ``processed_message_ids``.

    Crucially, this function does NOT touch the user's read/unread state:
    - ``INBOX`` is selected ``readonly=True`` so any flag mutation would
      fail loudly rather than silently dirtying the user's mailbox.
    - We fetch via ``BODY.PEEK[HEADER]`` so the IMAP server can't
      auto-flag Seen as a side effect of FETCH (RFC 3501 §6.4.5 spells
      this out — plain ``BODY[]`` sets ``\\Seen``; ``BODY.PEEK[]`` is
      explicitly side-effect-free).
    - No ``STORE +FLAGS \\Seen`` is ever issued.

    Replaces the previous ``fetch_unseen_messages`` which used
    ``UNSEEN SINCE`` + auto-Seen-flagging.  That worked but burned the
    user's unread badge on every reply.

    Raises on any IMAP failure so the caller can record the account as
    broken.
    """
    cls = IMAP4_SSL if use_ssl else IMAP4
    client = cls(host=host, port=port, timeout=IMAP_TIMEOUT_SECONDS)
    out: list[FetchedMessage] = []
    skip_set = processed_message_ids or set()
    try:
        client.login(username, password)
        # Read-only — never mutate the user's mailbox state.
        typ, _ = client.select("INBOX", readonly=True)
        if typ != "OK":
            raise RuntimeError("INBOX select failed")

        since_str = since.strftime("%d-%b-%Y")
        typ, data = client.search(None, f"(SINCE {since_str})")
        if typ != "OK" or not data or data[0] is None:
            return out

        uids = data[0].split()
        for uid in uids:
            # BODY.PEEK[HEADER] is the side-effect-free fetch variant.
            # Header-only first so skip-set messages never pay the
            # full-body transfer.
            typ, msg_data = client.fetch(uid, "(BODY.PEEK[HEADER])")
            if typ != "OK" or not msg_data:
                continue
            for part in msg_data:
                if isinstance(part, tuple) and len(part) > 1:
                    headers = email.message_from_bytes(part[1])
                    message_id = _clean_message_id(
                        headers.get("Message-ID", "") or headers.get("Message-Id", "") or ""
                    )
                    if message_id and message_id in skip_set:
                        # Already processed this one in a previous poll
                        # — don't reparse and don't surface to the caller.
                        break
                    from_raw = headers.get("From", "") or ""
                    _, from_email_addr = email.utils.parseaddr(from_raw)
                    refs_raw = headers.get("References", "") or ""
                    refs = [
                        _clean_message_id(r)
                        for r in refs_raw.split()
                        if r.strip()
                    ]
                    # Body for the reply classifier — full-message PEEK
                    # (still side-effect-free per RFC 3501 §6.4.5), parsed
                    # with the stdlib MIME walker.  Soft-fails to "".
                    body_text = ""
                    try:
                        typ_b, body_data = client.fetch(uid, "(BODY.PEEK[])")
                        if typ_b == "OK" and body_data:
                            for bpart in body_data:
                                if isinstance(bpart, tuple) and len(bpart) > 1:
                                    full = email.message_from_bytes(bpart[1])
                                    body_text = _extract_body_text(full)
                                    break
                    except Exception:  # noqa: BLE001 — body fetch is best-effort
                        logger.debug("body fetch failed for uid %s", uid)
                    out.append(FetchedMessage(
                        uid=uid.decode() if isinstance(uid, bytes) else str(uid),
                        message_id=message_id,
                        in_reply_to=_clean_message_id(headers.get("In-Reply-To", "") or ""),
                        references=refs,
                        subject=headers.get("Subject", "") or "",
                        from_email=(from_email_addr or "").lower(),
                        body_text=body_text,
                        received_at=_parse_received_at(headers),
                    ))
                    break
        return out
    finally:
        try:
            client.logout()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# Async matcher (DB-backed)
# --------------------------------------------------------------------------


async def match_message_to_lead(
    session,  # AsyncSession; avoid type import cycles
    msg: FetchedMessage,
    campaign_ids: list[uuid.UUID],
):
    """Return the Lead that the inbound reply pertains to, or None.

    Match order:
      1. In-Reply-To against Lead.brevo_message_id (scoped to campaign_ids)
      2. References (any matching brevo_message_id)
      3. Cleaned Subject + from-email match against Lead.composed_subject / Lead.email
    """
    from sqlalchemy import select  # local import to keep service-layer light

    from app.models import Lead  # noqa: E402

    if not campaign_ids:
        return None

    if msg["in_reply_to"]:
        lead = await session.scalar(
            select(Lead).where(
                Lead.brevo_message_id == msg["in_reply_to"],
                Lead.campaign_id.in_(campaign_ids),
            )
        )
        if lead is not None:
            return lead

    if msg["references"]:
        lead = await session.scalar(
            select(Lead).where(
                Lead.brevo_message_id.in_(msg["references"]),
                Lead.campaign_id.in_(campaign_ids),
            )
        )
        if lead is not None:
            return lead

    clean_subject = _clean_subject(msg["subject"])
    if clean_subject and msg["from_email"]:
        lead = await session.scalar(
            select(Lead).where(
                Lead.composed_subject == clean_subject,
                Lead.email == msg["from_email"],
                Lead.campaign_id.in_(campaign_ids),
            )
        )
        if lead is not None:
            return lead

    return None


# --------------------------------------------------------------------------
# Account-aware wrappers
#
# These are the only entry points callers outside this module should use.
# They decrypt the account's stored password into a local variable, hand it
# to the low-level IMAP function, then delete it before returning. The
# plaintext password never leaves this module.
# --------------------------------------------------------------------------


def test_imap_with_account(account: Any) -> ImapTestResult:
    """Test connectivity for a ConnectedAccount. Decrypts the stored password
    only for the duration of the IMAP call. Returns the same shape as
    ``test_imap_connection``; never raises.
    """
    from app.services import encryption  # local import to avoid cycles

    try:
        password = encryption.decrypt(account.password_encrypted)
    except Exception as e:  # noqa: BLE001 — wrong key, corrupt token, etc.
        logger.warning(
            "IMAP credential decrypt failed for %s: %s",
            getattr(account, "email_address", "<unknown>"), e,
        )
        return {"ok": False, "error": f"credential decrypt failed: {e}"}

    try:
        return test_imap_connection(
            account.imap_host,
            account.imap_port,
            account.imap_use_ssl,
            account.username,
            password,
        )
    finally:
        del password


def fetch_recent_with_account(
    account: Any,
    since: datetime,
    processed_message_ids: set[str] | None = None,
) -> list[FetchedMessage]:
    """Fetch recent INBOX messages for a ConnectedAccount, skipping any
    Message-IDs we've already processed.  Decrypts the password only for
    the duration of the call.  Raises on decrypt failure or any IMAP
    error — caller is responsible for marking the account failed.

    Replaces the old ``fetch_unseen_with_account`` (which auto-flagged
    fetched messages as Seen, dirtying the user's mailbox).
    """
    from app.services import encryption  # local import to avoid cycles

    password = encryption.decrypt(account.password_encrypted)
    try:
        return fetch_recent_messages(
            account.imap_host,
            account.imap_port,
            account.imap_use_ssl,
            account.username,
            password,
            since,
            processed_message_ids,
        )
    finally:
        del password


def unmark_seen_uids(account: Any, uids: list[str]) -> dict[str, Any]:
    """Clear the ``\\Seen`` flag on a list of UIDs in INBOX.

    One-shot remediation for replies that the previous poller wrongly
    auto-marked as read.  Decrypts the account password only for the
    duration of the call.  Returns ``{"ok": bool, "cleared": int,
    "errors": int}``; never raises.
    """
    from app.services import encryption  # local import to avoid cycles

    if not uids:
        return {"ok": True, "cleared": 0, "errors": 0}

    try:
        password = encryption.decrypt(account.password_encrypted)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"decrypt failed: {exc}", "cleared": 0, "errors": 0}

    cls = IMAP4_SSL if account.imap_use_ssl else IMAP4
    cleared = 0
    errors = 0
    try:
        client = cls(host=account.imap_host, port=account.imap_port, timeout=IMAP_TIMEOUT_SECONDS)
        try:
            client.login(account.username, password)
            typ, _ = client.select("INBOX", readonly=False)
            if typ != "OK":
                return {"ok": False, "error": "INBOX select failed", "cleared": 0, "errors": 0}
            for uid in uids:
                try:
                    typ, _ = client.store(uid, "-FLAGS", "\\Seen")
                    if typ == "OK":
                        cleared += 1
                    else:
                        errors += 1
                except Exception:  # noqa: BLE001
                    errors += 1
        finally:
            try:
                client.logout()
            except Exception:  # noqa: BLE001
                pass
    finally:
        del password
    return {"ok": True, "cleared": cleared, "errors": errors}

