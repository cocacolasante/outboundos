"""Phase 6: Apollo.io enrichment (httpx mocked)."""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services import apollo


def _mock_client_returning(*responses) -> tuple[MagicMock, AsyncMock]:
    """Build an httpx.AsyncClient mock whose .post() yields the given responses
    in order. Returns (client_cls_mock, post_mock) for assertion convenience.
    """
    client_instance = MagicMock()
    post_mock = AsyncMock(side_effect=list(responses))
    client_instance.post = post_mock
    client_instance.__aenter__ = AsyncMock(return_value=client_instance)
    client_instance.__aexit__ = AsyncMock(return_value=None)
    client_cls = MagicMock(return_value=client_instance)
    return client_cls, post_mock


def _response(status_code: int, body: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.json = MagicMock(return_value=body or {})
    return r


async def test_returns_empty_when_key_missing(monkeypatch):
    monkeypatch.setattr(apollo.settings, "APOLLO_API_KEY", "")
    result = await apollo.enrich_lead_apollo("a@b.com", "A", "B", "Co")
    assert result == {}


async def test_extracts_person_and_org_fields(monkeypatch):
    monkeypatch.setattr(apollo.settings, "APOLLO_API_KEY", "test-key")
    body = {
        "person": {
            "linkedin_url": "https://linkedin.com/in/jdoe",
            "headline": "CEO at Acme",
            "title": "Chief Executive",
            "seniority": "founder",
            "department": "executive",
            "organization": {
                "estimated_num_employees": 87,
                "industry": "SaaS",
                "latest_funding_stage": "Series A",
            },
        }
    }
    cls, post = _mock_client_returning(_response(200, body))
    with patch.object(apollo.httpx, "AsyncClient", cls):
        result = await apollo.enrich_lead_apollo("a@b.com", "Jane", "Doe", "Acme")

    assert result == {
        "linkedin_url": "https://linkedin.com/in/jdoe",
        "linkedin_headline": "CEO at Acme",
        "job_title": "Chief Executive",
        "seniority": "founder",
        "department": "executive",
        "company_employee_count": 87,
        "company_industry": "SaaS",
        "company_funding_stage": "Series A",
    }
    # Verify api_key was sent in the body, not as a header.
    sent_payload = post.call_args.kwargs["json"]
    assert sent_payload["api_key"] == "test-key"
    assert sent_payload["email"] == "a@b.com"


async def test_returns_empty_when_no_person_in_response(monkeypatch):
    monkeypatch.setattr(apollo.settings, "APOLLO_API_KEY", "test-key")
    cls, _ = _mock_client_returning(_response(200, {"person": None}))
    with patch.object(apollo.httpx, "AsyncClient", cls):
        result = await apollo.enrich_lead_apollo("a@b.com", "J", "D", "Acme")
    assert result == {}


async def test_returns_empty_on_404(monkeypatch):
    monkeypatch.setattr(apollo.settings, "APOLLO_API_KEY", "test-key")
    cls, _ = _mock_client_returning(_response(404))
    with patch.object(apollo.httpx, "AsyncClient", cls):
        result = await apollo.enrich_lead_apollo("a@b.com", "J", "D", "Acme")
    assert result == {}


async def test_retries_on_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(apollo.settings, "APOLLO_API_KEY", "test-key")
    body = {"person": {"linkedin_url": "x", "title": "CEO"}}
    cls, post = _mock_client_returning(
        _response(429), _response(429), _response(200, body)
    )
    with patch.object(apollo.httpx, "AsyncClient", cls), \
         patch.object(apollo.asyncio, "sleep", AsyncMock()):
        result = await apollo.enrich_lead_apollo("a@b.com", "J", "D", "Acme")

    assert post.call_count == 3
    assert result["linkedin_url"] == "x"


async def test_gives_up_after_max_429_attempts(monkeypatch):
    monkeypatch.setattr(apollo.settings, "APOLLO_API_KEY", "test-key")
    cls, post = _mock_client_returning(_response(429), _response(429), _response(429))
    with patch.object(apollo.httpx, "AsyncClient", cls), \
         patch.object(apollo.asyncio, "sleep", AsyncMock()):
        result = await apollo.enrich_lead_apollo("a@b.com", "J", "D", "Acme")
    assert result == {}
    assert post.call_count == 3


async def test_network_error_returns_empty(monkeypatch):
    monkeypatch.setattr(apollo.settings, "APOLLO_API_KEY", "test-key")
    client_instance = MagicMock()
    client_instance.post = AsyncMock(side_effect=httpx.ConnectError("down"))
    client_instance.__aenter__ = AsyncMock(return_value=client_instance)
    client_instance.__aexit__ = AsyncMock(return_value=None)
    cls = MagicMock(return_value=client_instance)
    with patch.object(apollo.httpx, "AsyncClient", cls), \
         patch.object(apollo.asyncio, "sleep", AsyncMock()):
        result = await apollo.enrich_lead_apollo("a@b.com", "J", "D", "Acme")
    assert result == {}
