"""Phase 3: IMAP client. imaplib is mocked — no real network."""
from unittest.mock import MagicMock, patch

from app.services import imap_client


def _mock_client(message_ids: bytes = b"1 2 3 4 5") -> MagicMock:
    m = MagicMock()
    m.select.return_value = ("OK", [b"5"])
    m.search.return_value = ("OK", [message_ids])
    return m


def test_success_returns_message_count():
    m = _mock_client(b"1 2 3 4 5")
    with patch("app.services.imap_client.IMAP4_SSL", return_value=m) as cls:
        result = imap_client.test_imap_connection(
            "imap.gmail.com", 993, True, "me@gmail.com", "app-password"
        )

    cls.assert_called_once_with(host="imap.gmail.com", port=993, timeout=10)
    m.login.assert_called_once_with("me@gmail.com", "app-password")
    m.select.assert_called_once_with("INBOX", readonly=True)
    m.logout.assert_called_once()
    assert result == {"ok": True, "message_count": 5}


def test_empty_inbox_returns_zero():
    m = _mock_client(b"")
    with patch("app.services.imap_client.IMAP4_SSL", return_value=m):
        result = imap_client.test_imap_connection("h", 993, True, "u", "p")
    assert result == {"ok": True, "message_count": 0}


def test_login_failure_returns_error():
    m = MagicMock()
    m.login.side_effect = Exception("authentication failed")
    with patch("app.services.imap_client.IMAP4_SSL", return_value=m):
        result = imap_client.test_imap_connection("h", 993, True, "u", "wrong")

    assert result["ok"] is False
    assert "authentication failed" in result["error"]
    # logout should still be attempted even after failure.
    m.logout.assert_called_once()


def test_connection_failure_returns_error():
    with patch(
        "app.services.imap_client.IMAP4_SSL", side_effect=OSError("Network unreachable")
    ):
        result = imap_client.test_imap_connection("bad.host", 993, True, "u", "p")
    assert result["ok"] is False
    assert "Network unreachable" in result["error"]


def test_non_ssl_uses_plain_imap_class():
    m = _mock_client()
    with patch("app.services.imap_client.IMAP4", return_value=m) as plain_cls, \
         patch("app.services.imap_client.IMAP4_SSL") as ssl_cls:
        result = imap_client.test_imap_connection("h", 143, False, "u", "p")

    plain_cls.assert_called_once_with(host="h", port=143, timeout=10)
    ssl_cls.assert_not_called()
    assert result["ok"] is True


def test_inbox_select_failure_returns_error():
    m = MagicMock()
    m.select.return_value = ("NO", [b"permission denied"])
    with patch("app.services.imap_client.IMAP4_SSL", return_value=m):
        result = imap_client.test_imap_connection("h", 993, True, "u", "p")
    assert result["ok"] is False
    assert "INBOX select failed" in result["error"]
