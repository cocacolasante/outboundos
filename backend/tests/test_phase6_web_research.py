"""Phase 6: web_research service (Anthropic + web_search, mocked)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import web_research


def _anthropic_text_response(body: str) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=body)])


@pytest.fixture(autouse=True)
def _reset_client():
    """Force a fresh client per test so api-key patches take effect."""
    web_research._client = None
    yield
    web_research._client = None


async def test_returns_default_when_no_api_key(monkeypatch):
    monkeypatch.setattr(web_research.settings, "ANTHROPIC_API_KEY", "")
    result = await web_research.research_person_web("John", "Doe", "Acme", "CEO")
    assert result == {
        "person_news": [],
        "company_news": [],
        "company_description": "",
        "recent_updates": [],
        "industry": "",
        "size_hint": "",
        "found": False,
    }


async def test_parses_valid_json_response(monkeypatch):
    monkeypatch.setattr(web_research.settings, "ANTHROPIC_API_KEY", "test-key")
    response_text = (
        '{"person_news": ["raised Series B"], "company_news": ["launched product"], '
        '"company_description": "AI startup", "found": true}'
    )
    with patch.object(
        web_research, "_get_client",
        return_value=SimpleNamespace(
            messages=SimpleNamespace(create=AsyncMock(return_value=_anthropic_text_response(response_text)))
        ),
    ):
        result = await web_research.research_person_web("Jane", "Doe", "Acme", "CEO")

    assert result["person_news"] == ["raised Series B"]
    assert result["company_news"] == ["launched product"]
    assert result["company_description"] == "AI startup"
    assert result["found"] is True


async def test_strips_markdown_fences(monkeypatch):
    monkeypatch.setattr(web_research.settings, "ANTHROPIC_API_KEY", "test-key")
    text = '```json\n{"person_news": [], "company_news": [], "company_description": "x", "found": false}\n```'
    with patch.object(
        web_research, "_get_client",
        return_value=SimpleNamespace(
            messages=SimpleNamespace(create=AsyncMock(return_value=_anthropic_text_response(text)))
        ),
    ):
        result = await web_research.research_person_web("J", "D", "Acme", "CEO")
    assert result["company_description"] == "x"
    assert result["found"] is False


async def test_extracts_json_from_prose(monkeypatch):
    monkeypatch.setattr(web_research.settings, "ANTHROPIC_API_KEY", "test-key")
    text = (
        'Here is what I found:\n'
        '{"person_news": ["news"], "company_news": [], "company_description": "desc", "found": true}\n'
        'Hope that helps!'
    )
    with patch.object(
        web_research, "_get_client",
        return_value=SimpleNamespace(
            messages=SimpleNamespace(create=AsyncMock(return_value=_anthropic_text_response(text)))
        ),
    ):
        result = await web_research.research_person_web("J", "D", "Acme", "CEO")
    assert result["person_news"] == ["news"]
    assert result["company_description"] == "desc"


async def test_returns_default_on_api_exception(monkeypatch):
    monkeypatch.setattr(web_research.settings, "ANTHROPIC_API_KEY", "test-key")
    with patch.object(
        web_research, "_get_client",
        return_value=SimpleNamespace(
            messages=SimpleNamespace(create=AsyncMock(side_effect=RuntimeError("timeout")))
        ),
    ):
        result = await web_research.research_person_web("J", "D", "Acme", "CEO")
    assert result["found"] is False
    assert result["person_news"] == []


async def test_returns_default_on_unparseable_response(monkeypatch):
    monkeypatch.setattr(web_research.settings, "ANTHROPIC_API_KEY", "test-key")
    with patch.object(
        web_research, "_get_client",
        return_value=SimpleNamespace(
            messages=SimpleNamespace(create=AsyncMock(return_value=_anthropic_text_response("garbage no json")))
        ),
    ):
        result = await web_research.research_person_web("J", "D", "Acme", "CEO")
    assert result["found"] is False


async def test_invokes_web_search_tool(monkeypatch):
    monkeypatch.setattr(web_research.settings, "ANTHROPIC_API_KEY", "test-key")
    create_mock = AsyncMock(return_value=_anthropic_text_response('{}'))
    with patch.object(
        web_research, "_get_client",
        return_value=SimpleNamespace(messages=SimpleNamespace(create=create_mock)),
    ):
        await web_research.research_person_web("J", "D", "Acme", "CEO")

    kwargs = create_mock.call_args.kwargs
    tools = kwargs["tools"]
    assert any(t.get("type") == "web_search_20250305" for t in tools)
    # Cost levers: research runs on the (cheaper) research model with a
    # bounded web-search budget — not the compose model / max_uses 5.
    assert kwargs["model"] == web_research.settings.ANTHROPIC_RESEARCH_MODEL
    search_tool = next(t for t in tools if t.get("type") == "web_search_20250305")
    assert search_tool["max_uses"] == web_research.settings.RESEARCH_WEB_SEARCH_MAX_USES


async def test_merged_call_returns_company_fields(monkeypatch):
    """The single research call now also returns company recent_updates +
    industry + size_hint (previously a separate site_scraper call)."""
    monkeypatch.setattr(web_research.settings, "ANTHROPIC_API_KEY", "test-key")
    response_text = (
        '{"person_news": [], "company_news": [], "company_description": "AI for SMB", '
        '"recent_updates": ["launched v2"], "industry": "SaaS", '
        '"size_hint": "growth", "found": true}'
    )
    with patch.object(
        web_research, "_get_client",
        return_value=SimpleNamespace(
            messages=SimpleNamespace(create=AsyncMock(return_value=_anthropic_text_response(response_text)))
        ),
    ):
        result = await web_research.research_person_web("J", "D", "Acme", "CEO", "acme.com")

    assert result["recent_updates"] == ["launched v2"]
    assert result["industry"] == "SaaS"
    assert result["size_hint"] == "growth"


async def test_research_prompt_contains_repost_rule(monkeypatch):
    """The bulk pipeline prompt must tell Anthropic to EXCLUDE naked
    reposts (no added commentary) from person_news and to KEEP reposts
    where the prospect added their own thoughts — described as their
    COMMENT, not as if they wrote the original.

    Without this rule the compose stage references reshared content as
    if the prospect authored it (\"loved your post on X\") and the
    outreach reads as wrong / embarrassing."""
    monkeypatch.setattr(web_research.settings, "ANTHROPIC_API_KEY", "test-key")
    captured = {}

    async def _spy(**kwargs):
        captured["kwargs"] = kwargs
        return _anthropic_text_response('{}')

    with patch.object(
        web_research, "_get_client",
        return_value=SimpleNamespace(messages=SimpleNamespace(create=_spy)),
    ):
        await web_research.research_person_web("J", "D", "Acme", "CEO")

    prompt = captured["kwargs"]["messages"][0]["content"].lower()
    # Names the failure mode (either "repost" or "reshare" is fine).
    assert "repost" in prompt or "reshare" in prompt
    # Names the commentary carve-out.
    assert "commentary" in prompt or "commented" in prompt or "their thoughts" in prompt
    # Explicitly excludes the naked case.
    assert "naked" in prompt or "exclude" in prompt or "not their content" in prompt


async def test_cached_company_drives_person_focused_search_and_overlay(monkeypatch):
    """When cached company context is supplied the prompt tells the model NOT
    to research the company (person-only search = fewer ingested tokens), and
    the cached company fields are overlaid onto the (empty) company result."""
    monkeypatch.setattr(web_research.settings, "ANTHROPIC_API_KEY", "test-key")
    # The person-only search returns just person_news; company fields empty.
    text = ('{"person_news": ["spoke at a conf"], "company_news": [], '
            '"company_description": "", "recent_updates": [], "industry": "", '
            '"size_hint": "", "found": true}')
    create = AsyncMock(return_value=_anthropic_text_response(text))
    cached_company = {
        "company_description": "AI for SMB", "industry": "SaaS",
        "size_hint": "startup", "company_news": ["launched X"],
    }
    with patch.object(
        web_research, "_get_client",
        return_value=SimpleNamespace(messages=SimpleNamespace(create=create)),
    ):
        result = await web_research.research_person_web(
            "Jane", "Doe", "Acme", "CEO", "acme.com", cached_company=cached_company,
        )

    sent_prompt = create.call_args.kwargs["messages"][0]["content"]
    assert "do NOT search for company info" in sent_prompt
    assert "AI for SMB" in sent_prompt          # cached context embedded
    # Person signal kept; company fields overlaid from cache.
    assert result["person_news"] == ["spoke at a conf"]
    assert result["company_description"] == "AI for SMB"
    assert result["industry"] == "SaaS"
    assert result["company_news"] == ["launched X"]
