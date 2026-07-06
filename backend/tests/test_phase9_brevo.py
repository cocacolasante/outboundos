"""Phase 9: Brevo send_email service (httpx mocked)."""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services import brevo


def _client(post_responses) -> tuple[MagicMock, AsyncMock]:
    if not isinstance(post_responses, list):
        post_responses = [post_responses]
    instance = MagicMock()
    post_mock = AsyncMock(side_effect=post_responses)
    instance.post = post_mock
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=None)
    cls = MagicMock(return_value=instance)
    return cls, post_mock


def _response(status_code: int, body: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.json = MagicMock(return_value=body or {})
    if status_code >= 400:
        r.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "err", request=MagicMock(), response=r
            )
        )
    else:
        r.raise_for_status = MagicMock()
    return r


async def test_raises_when_api_key_missing(monkeypatch):
    monkeypatch.setattr(brevo.settings, "BREVO_API_KEY", "")
    with pytest.raises(RuntimeError, match="Brevo is not configured"):
        await brevo.send_email(
            to_email="a@b.com", to_name=None, subject="s",
            html_body="<p>x</p>", text_body="x",
            sender_name="S", sender_email="s@x.com",
            campaign_id="cid", lead_id="lid",
        )


async def test_posts_with_expected_payload_and_headers(monkeypatch):
    monkeypatch.setattr(brevo.settings, "BREVO_API_KEY", "test-key")
    cls, post = _client(_response(200, {"messageId": "msg-123"}))
    with patch.object(brevo.httpx, "AsyncClient", cls):
        msg_id = await brevo.send_email(
            to_email="lead@x.com", to_name="Lead Name",
            subject="Hello", html_body="<p>Hi</p>", text_body="Hi",
            sender_name="Sender", sender_email="from@x.com",
            campaign_id="campaign-1", lead_id="lead-1",
        )
    assert msg_id == "msg-123"

    args, kwargs = post.call_args
    assert args[0] == brevo.BREVO_URL
    assert kwargs["headers"]["api-key"] == "test-key"

    payload = kwargs["json"]
    assert payload["sender"] == {"name": "Sender", "email": "from@x.com"}
    assert payload["to"] == [{"email": "lead@x.com", "name": "Lead Name"}]
    assert payload["subject"] == "Hello"
    assert payload["htmlContent"] == "<p>Hi</p>"
    assert payload["textContent"] == "Hi"
    # No custom X-Campaign-ID / X-Lead-ID headers — they were dead metadata
    # and read as mailshot/bulk markers to receiving gateways.  With no
    # threading either, the headers block is omitted entirely.
    assert "headers" not in payload


async def test_omits_recipient_name_when_none(monkeypatch):
    monkeypatch.setattr(brevo.settings, "BREVO_API_KEY", "test-key")
    cls, post = _client(_response(200, {"messageId": "m"}))
    with patch.object(brevo.httpx, "AsyncClient", cls):
        await brevo.send_email(
            to_email="lead@x.com", to_name=None,
            subject="s", html_body="<p>h</p>", text_body="h",
            sender_name="S", sender_email="s@x.com",
            campaign_id="c", lead_id="l",
        )
    payload = post.call_args.kwargs["json"]
    assert payload["to"] == [{"email": "lead@x.com"}]
    assert "name" not in payload["to"][0]


async def test_raises_on_non_2xx(monkeypatch):
    monkeypatch.setattr(brevo.settings, "BREVO_API_KEY", "test-key")
    cls, _ = _client(_response(500, {"message": "internal"}))
    with patch.object(brevo.httpx, "AsyncClient", cls):
        with pytest.raises(httpx.HTTPStatusError):
            await brevo.send_email(
                to_email="x@x.com", to_name=None, subject="s",
                html_body="<p>h</p>", text_body="h",
                sender_name="S", sender_email="s@x.com",
                campaign_id="c", lead_id="l",
            )


async def test_raises_when_response_missing_message_id(monkeypatch):
    monkeypatch.setattr(brevo.settings, "BREVO_API_KEY", "test-key")
    cls, _ = _client(_response(200, {}))
    with patch.object(brevo.httpx, "AsyncClient", cls):
        with pytest.raises(RuntimeError, match="messageId"):
            await brevo.send_email(
                to_email="x@x.com", to_name=None, subject="s",
                html_body="<p>h</p>", text_body="h",
                sender_name="S", sender_email="s@x.com",
                campaign_id="c", lead_id="l",
            )


def test_wrap_message_id_normalises_brackets():
    assert brevo._wrap_message_id("abc@host") == "<abc@host>"
    assert brevo._wrap_message_id("<abc@host>") == "<abc@host>"
    assert brevo._wrap_message_id("  abc@host ") == "<abc@host>"
    assert brevo._wrap_message_id("") is None
    assert brevo._wrap_message_id(None) is None


async def test_in_reply_to_sets_threading_headers(monkeypatch):
    monkeypatch.setattr(brevo.settings, "BREVO_API_KEY", "test-key")
    cls, post = _client(_response(200, {"messageId": "msg-9"}))
    with patch.object(brevo.httpx, "AsyncClient", cls):
        await brevo.send_email(
            to_email="lead@x.com", to_name=None,
            subject="Re: Hello", html_body="<p>h</p>", text_body="h",
            sender_name="S", sender_email="s@x.com",
            campaign_id="c", lead_id="l", in_reply_to="orig-id@host",
        )
    headers = post.call_args.kwargs["json"]["headers"]
    assert headers["In-Reply-To"] == "<orig-id@host>"
    assert headers["References"] == "<orig-id@host>"


async def test_no_threading_headers_without_in_reply_to(monkeypatch):
    monkeypatch.setattr(brevo.settings, "BREVO_API_KEY", "test-key")
    cls, post = _client(_response(200, {"messageId": "m"}))
    with patch.object(brevo.httpx, "AsyncClient", cls):
        await brevo.send_email(
            to_email="lead@x.com", to_name=None,
            subject="s", html_body="<p>h</p>", text_body="h",
            sender_name="S", sender_email="s@x.com",
            campaign_id="c", lead_id="l",
        )
    # With no threading and no custom headers, the block is omitted entirely.
    payload = post.call_args.kwargs["json"]
    assert "headers" not in payload
