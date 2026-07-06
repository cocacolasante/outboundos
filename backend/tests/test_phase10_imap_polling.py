"""Phase 10: IMAP reply polling — fetch + match logic."""
from datetime import datetime, time, timezone
from unittest.mock import MagicMock, patch

from sqlalchemy import select

from app.models import Campaign, Lead
from app.services import imap_client


# --------------------------------------------------------------------------
# fetch_recent_messages (imaplib mocked)
# --------------------------------------------------------------------------


def _imap_message_bytes(headers: dict[str, str]) -> bytes:
    return ("\r\n".join(f"{k}: {v}" for k, v in headers.items()) + "\r\n").encode()


def test_fetch_recent_messages_parses_headers():
    msg_bytes = _imap_message_bytes({
        "From": "Jane Doe <jane@external.com>",
        "Subject": "Re: Your reach out",
        "Message-ID": "<mid-jane-1>",
        "In-Reply-To": "<msg-abc-123>",
        "References": "<msg-abc-123> <other-msg>",
    })

    m = MagicMock()
    m.login.return_value = ("OK", [])
    m.select.return_value = ("OK", [b"1"])
    m.search.return_value = ("OK", [b"42"])
    m.fetch.return_value = ("OK", [(b"42 (BODY[HEADER] {200}", msg_bytes), b")"])
    m.logout.return_value = ("BYE", [])

    with patch("app.services.imap_client.IMAP4_SSL", return_value=m):
        msgs = imap_client.fetch_recent_messages(
            "imap.gmail.com", 993, True, "u@x.com", "pw",
            since=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )

    assert len(msgs) == 1
    msg = msgs[0]
    assert msg["uid"] == "42"
    assert msg["message_id"] == "mid-jane-1"
    assert msg["in_reply_to"] == "msg-abc-123"
    assert msg["references"] == ["msg-abc-123", "other-msg"]
    assert msg["subject"] == "Re: Your reach out"
    assert msg["from_email"] == "jane@external.com"


def test_fetch_recent_messages_uses_readonly_select_and_peek_fetch():
    """The poller must not touch the user's mailbox state.  Two contract
    checks:

    1. ``client.select("INBOX", readonly=True)`` — the IMAP server can't
       auto-flag Seen on a read-only mailbox, and any accidental
       ``STORE`` call would error rather than silently mutate.
    2. ``client.fetch(uid, "(BODY.PEEK[HEADER])")`` — RFC 3501 §6.4.5
       guarantees ``BODY.PEEK[]`` is side-effect-free; plain ``BODY[]``
       or ``RFC822.HEADER`` would set ``\\Seen``.

    Regression guard: a prior implementation used ``readonly=False`` +
    ``RFC822.HEADER`` + an explicit ``+FLAGS \\Seen`` store, which is
    what we're undoing.
    """
    msg_bytes = _imap_message_bytes({
        "From": "x@y.com", "Subject": "Hi",
        "Message-ID": "<mid-readonly>",
        "In-Reply-To": "<id>",
    })
    m = MagicMock()
    m.search.return_value = ("OK", [b"7"])
    m.select.return_value = ("OK", [b"1"])
    m.fetch.return_value = ("OK", [(b"7 (BODY[HEADER]", msg_bytes)])

    with patch("app.services.imap_client.IMAP4_SSL", return_value=m):
        imap_client.fetch_recent_messages(
            "h", 993, True, "u", "p", datetime(2026, 5, 1),
        )

    # select must be readonly=True
    m.select.assert_called_with("INBOX", readonly=True)
    # EVERY fetch (header pass AND the body pass added for the reply
    # classifier) must use a side-effect-free PEEK form — plain BODY[]
    # sets \Seen.
    assert m.fetch.call_count >= 1
    for call in m.fetch.call_args_list:
        assert "BODY.PEEK[" in call[0][1], call[0][1]
    # And no STORE call (no Seen-flag mutation) was ever issued.
    m.store.assert_not_called()


def test_fetch_recent_messages_search_uses_since_not_unseen():
    """SINCE-only search (not UNSEEN SINCE).  Read-state filtering is
    moved out of the IMAP query and into our Message-ID dedup, so
    messages stay unread for the user."""
    m = MagicMock()
    m.search.return_value = ("OK", [b""])
    m.select.return_value = ("OK", [b"1"])
    with patch("app.services.imap_client.IMAP4_SSL", return_value=m):
        imap_client.fetch_recent_messages(
            "h", 993, True, "u", "p", datetime(2026, 5, 1, tzinfo=timezone.utc),
        )

    search_args = m.search.call_args
    criteria = search_args[0][1]
    assert "UNSEEN" not in criteria
    assert "SINCE" in criteria


def test_fetch_recent_messages_skips_already_processed_message_ids():
    """Caller-supplied processed set short-circuits per-message — the
    matching message is never surfaced to the caller, so reply_poller
    won't re-record a REPLIED event for it."""
    seen_bytes = _imap_message_bytes({
        "From": "x@y.com", "Subject": "Old",
        "Message-ID": "<already-processed>",
    })
    fresh_bytes = _imap_message_bytes({
        "From": "z@y.com", "Subject": "New",
        "Message-ID": "<brand-new>",
    })
    m = MagicMock()
    m.search.return_value = ("OK", [b"1 2"])
    m.select.return_value = ("OK", [b"1"])
    # Two FETCH calls (one per uid); return different bodies.
    m.fetch.side_effect = [
        ("OK", [(b"1 (BODY[HEADER]", seen_bytes)]),
        ("OK", [(b"2 (BODY[HEADER]", fresh_bytes)]),
    ]

    with patch("app.services.imap_client.IMAP4_SSL", return_value=m):
        msgs = imap_client.fetch_recent_messages(
            "h", 993, True, "u", "p", datetime(2026, 5, 1),
            processed_message_ids={"already-processed"},
        )

    assert len(msgs) == 1
    assert msgs[0]["message_id"] == "brand-new"


def test_fetch_recent_no_messages_returns_empty():
    m = MagicMock()
    m.search.return_value = ("OK", [b""])
    m.select.return_value = ("OK", [b"1"])
    with patch("app.services.imap_client.IMAP4_SSL", return_value=m):
        msgs = imap_client.fetch_recent_messages(
            "h", 993, True, "u", "p", datetime(2026, 5, 1),
        )
    assert msgs == []


def test_unmark_seen_uids_clears_flag_on_inbox():
    """The one-shot remediation script removes ``\\Seen`` from the
    given UIDs so replies the previous poller wrongly auto-marked pop
    back to unread in the user's mailbox."""
    from app.services import encryption

    class _Acc:
        imap_host = "h"
        imap_port = 993
        imap_use_ssl = True
        username = "u@x.com"
        password_encrypted = encryption.encrypt("pw")

    m = MagicMock()
    m.login.return_value = ("OK", [])
    m.select.return_value = ("OK", [b"1"])
    m.store.return_value = ("OK", [])
    m.logout.return_value = ("BYE", [])

    with patch("app.services.imap_client.IMAP4_SSL", return_value=m):
        result = imap_client.unmark_seen_uids(_Acc(), ["7", "8", "9"])

    assert result == {"ok": True, "cleared": 3, "errors": 0}
    # readonly=False this time (we're explicitly mutating flags).
    m.select.assert_called_with("INBOX", readonly=False)
    m.store.assert_any_call("7", "-FLAGS", "\\Seen")
    m.store.assert_any_call("8", "-FLAGS", "\\Seen")
    m.store.assert_any_call("9", "-FLAGS", "\\Seen")


# --------------------------------------------------------------------------
# match_message_to_lead (DB-backed)
# --------------------------------------------------------------------------


async def _make_campaign_with_lead(
    db_session, *, brevo_message_id: str, composed_subject: str = "S",
    email: str = "lead@external.com",
) -> Lead:
    c = Campaign(
        name="P10", goal="g", tone="t",
        sender_name="s", sender_email="s@x.com",
        sample_count=1,
        schedule_days=[0, 1, 2, 3, 4],
        schedule_time_start=time(9, 0), schedule_time_end=time(17, 0),
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    l = Lead(
        campaign_id=c.id, email=email,
        composed_subject=composed_subject,
        brevo_message_id=brevo_message_id,
    )
    db_session.add(l)
    await db_session.commit()
    await db_session.refresh(l)
    return l


async def test_match_by_in_reply_to(db_session):
    lead = await _make_campaign_with_lead(db_session, brevo_message_id="msg-AAA")
    msg = {
        "uid": "1",
        "in_reply_to": "msg-AAA",
        "references": [],
        "subject": "Re: anything",
        "from_email": "anyone@x.com",
    }
    matched = await imap_client.match_message_to_lead(
        db_session, msg, [lead.campaign_id]
    )
    assert matched is not None
    assert matched.id == lead.id


async def test_match_by_references_when_in_reply_to_missing(db_session):
    lead = await _make_campaign_with_lead(db_session, brevo_message_id="msg-BBB")
    msg = {
        "uid": "1",
        "in_reply_to": "",
        "references": ["unknown", "msg-BBB"],
        "subject": "Re: x",
        "from_email": "anyone@x.com",
    }
    matched = await imap_client.match_message_to_lead(
        db_session, msg, [lead.campaign_id]
    )
    assert matched is not None
    assert matched.id == lead.id


async def test_match_falls_back_to_subject_and_email(db_session):
    lead = await _make_campaign_with_lead(
        db_session,
        brevo_message_id="msg-CCC",
        composed_subject="Quick question",
        email="lead@external.com",
    )
    msg = {
        "uid": "1",
        "in_reply_to": "",
        "references": [],
        "subject": "Re: Quick question",
        "from_email": "lead@external.com",
    }
    matched = await imap_client.match_message_to_lead(
        db_session, msg, [lead.campaign_id]
    )
    assert matched is not None
    assert matched.id == lead.id


async def test_subject_fallback_requires_email_match(db_session):
    lead = await _make_campaign_with_lead(
        db_session,
        brevo_message_id="msg-DDD",
        composed_subject="Quick question",
        email="lead@external.com",
    )
    msg = {
        "uid": "1",
        "in_reply_to": "",
        "references": [],
        "subject": "Re: Quick question",
        "from_email": "imposter@elsewhere.com",
    }
    matched = await imap_client.match_message_to_lead(
        db_session, msg, [lead.campaign_id]
    )
    assert matched is None


async def test_match_scoped_to_campaign_ids(db_session):
    # Lead is in campaign A; we search only campaign B → no match.
    lead = await _make_campaign_with_lead(db_session, brevo_message_id="msg-EEE")
    other_campaign = Campaign(
        name="Other", goal="g", tone="t",
        sender_name="s", sender_email="s@x.com",
        sample_count=1,
        schedule_days=[0], schedule_time_start=time(9, 0), schedule_time_end=time(17, 0),
    )
    db_session.add(other_campaign)
    await db_session.commit()

    msg = {
        "uid": "1", "in_reply_to": "msg-EEE", "references": [],
        "subject": "Re: x", "from_email": "any@x.com",
    }
    matched = await imap_client.match_message_to_lead(
        db_session, msg, [other_campaign.id]
    )
    assert matched is None


# --------------------------------------------------------------------------
# Body extraction (agent reply classifier input)
# --------------------------------------------------------------------------


def _full_message_bytes(headers: dict[str, str], body: str) -> bytes:
    head = "\r\n".join(f"{k}: {v}" for k, v in headers.items())
    return (head + "\r\n\r\n" + body).encode()


def test_fetch_recent_messages_extracts_plain_text_body():
    """The second (body) fetch parses text/plain into body_text and the
    Date header into received_at."""
    headers = {
        "From": "jane@external.com",
        "Subject": "Re: hello",
        "Message-ID": "<mid-body-1>",
        "Date": "Thu, 11 Jun 2026 09:30:00 +0000",
        "Content-Type": "text/plain; charset=utf-8",
    }
    header_bytes = _imap_message_bytes(headers)
    full_bytes = _full_message_bytes(headers, "Yes — let's talk Tuesday.\r\nJane")

    m = MagicMock()
    m.select.return_value = ("OK", [b"1"])
    m.search.return_value = ("OK", [b"5"])
    # First fetch = header pass, second = full-body pass.
    m.fetch.side_effect = [
        ("OK", [(b"5 (BODY[HEADER]", header_bytes)]),
        ("OK", [(b"5 (BODY[]", full_bytes)]),
    ]

    with patch("app.services.imap_client.IMAP4_SSL", return_value=m):
        msgs = imap_client.fetch_recent_messages(
            "h", 993, True, "u", "p", datetime(2026, 5, 1, tzinfo=timezone.utc),
        )

    assert len(msgs) == 1
    assert "let's talk Tuesday" in msgs[0]["body_text"]
    assert msgs[0]["received_at"] is not None
    assert msgs[0]["received_at"].year == 2026


def test_fetch_recent_messages_html_fallback_strips_tags():
    """No text/plain part → fall back to tag-stripped text/html."""
    boundary = "BOUNDARY42"
    headers = {
        "From": "j@x.com",
        "Subject": "Re: hi",
        "Message-ID": "<mid-html-1>",
        "Content-Type": f'multipart/alternative; boundary="{boundary}"',
    }
    body = (
        f"--{boundary}\r\n"
        "Content-Type: text/html; charset=utf-8\r\n\r\n"
        "<html><body><p>Sounds <b>great</b>, send the deck.</p></body></html>\r\n"
        f"--{boundary}--\r\n"
    )
    m = MagicMock()
    m.select.return_value = ("OK", [b"1"])
    m.search.return_value = ("OK", [b"6"])
    m.fetch.side_effect = [
        ("OK", [(b"6 (BODY[HEADER]", _imap_message_bytes(headers))]),
        ("OK", [(b"6 (BODY[]", _full_message_bytes(headers, body))]),
    ]

    with patch("app.services.imap_client.IMAP4_SSL", return_value=m):
        msgs = imap_client.fetch_recent_messages(
            "h", 993, True, "u", "p", datetime(2026, 5, 1, tzinfo=timezone.utc),
        )

    assert len(msgs) == 1
    text = msgs[0]["body_text"]
    assert "Sounds" in text and "great" in text and "send the deck" in text
    assert "<" not in text  # tags stripped


def test_fetch_recent_messages_body_truncated_to_cap():
    headers = {
        "From": "j@x.com", "Subject": "Re: hi",
        "Message-ID": "<mid-long-1>",
        "Content-Type": "text/plain; charset=utf-8",
    }
    long_body = "word " * 3000  # ~15000 chars
    m = MagicMock()
    m.select.return_value = ("OK", [b"1"])
    m.search.return_value = ("OK", [b"7"])
    m.fetch.side_effect = [
        ("OK", [(b"7 (BODY[HEADER]", _imap_message_bytes(headers))]),
        ("OK", [(b"7 (BODY[]", _full_message_bytes(headers, long_body))]),
    ]
    with patch("app.services.imap_client.IMAP4_SSL", return_value=m):
        msgs = imap_client.fetch_recent_messages(
            "h", 993, True, "u", "p", datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
    assert len(msgs[0]["body_text"]) <= 4000


def test_fetch_recent_messages_body_fetch_failure_soft_fails():
    """A failed body fetch must not drop the message — headers still
    surface with an empty body_text."""
    headers = {
        "From": "j@x.com", "Subject": "Re: hi",
        "Message-ID": "<mid-bodyfail-1>",
    }
    m = MagicMock()
    m.select.return_value = ("OK", [b"1"])
    m.search.return_value = ("OK", [b"8"])
    m.fetch.side_effect = [
        ("OK", [(b"8 (BODY[HEADER]", _imap_message_bytes(headers))]),
        RuntimeError("body fetch exploded"),
    ]
    with patch("app.services.imap_client.IMAP4_SSL", return_value=m):
        msgs = imap_client.fetch_recent_messages(
            "h", 993, True, "u", "p", datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
    assert len(msgs) == 1
    assert msgs[0]["body_text"] == ""
