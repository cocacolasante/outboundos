"""Phase 6: Hunter.io email verification (httpx mocked)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import hunter


def _client_returning(response: MagicMock) -> MagicMock:
    instance = MagicMock()
    instance.get = AsyncMock(return_value=response)
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=None)
    return MagicMock(return_value=instance)


def _response(payload: dict, status_code: int = 200) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.json = MagicMock(return_value=payload)
    r.raise_for_status = MagicMock()
    return r


async def test_returns_permissive_default_with_no_key(monkeypatch):
    monkeypatch.setattr(hunter.settings, "HUNTER_API_KEY", "")
    result = await hunter.verify_email_hunter("x@y.com")
    assert result == {"deliverable": True, "score": 100}


async def test_parses_valid_status(monkeypatch):
    monkeypatch.setattr(hunter.settings, "HUNTER_API_KEY", "test-key")
    cls = _client_returning(_response({"data": {"status": "valid", "score": 95}}))
    with patch.object(hunter.httpx, "AsyncClient", cls):
        result = await hunter.verify_email_hunter("good@x.com")
    assert result == {"deliverable": True, "score": 95}


async def test_marks_invalid_status_as_undeliverable(monkeypatch):
    monkeypatch.setattr(hunter.settings, "HUNTER_API_KEY", "test-key")
    cls = _client_returning(_response({"data": {"status": "invalid", "score": 10}}))
    with patch.object(hunter.httpx, "AsyncClient", cls):
        result = await hunter.verify_email_hunter("bad@x.com")
    assert result == {"deliverable": False, "score": 10}


async def test_accept_all_is_deliverable(monkeypatch):
    monkeypatch.setattr(hunter.settings, "HUNTER_API_KEY", "test-key")
    cls = _client_returning(_response({"data": {"status": "accept_all", "score": 50}}))
    with patch.object(hunter.httpx, "AsyncClient", cls):
        result = await hunter.verify_email_hunter("ok@x.com")
    assert result["deliverable"] is True


async def test_network_failure_falls_back_to_permissive(monkeypatch):
    monkeypatch.setattr(hunter.settings, "HUNTER_API_KEY", "test-key")
    instance = MagicMock()
    instance.get = AsyncMock(side_effect=Exception("connection refused"))
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=None)
    cls = MagicMock(return_value=instance)
    with patch.object(hunter.httpx, "AsyncClient", cls):
        result = await hunter.verify_email_hunter("x@y.com")
    # Don't block sending on a verifier outage.
    assert result["deliverable"] is True
    assert result["score"] == 0
