"""Tests for the 'research a client' one-shot endpoint.

Three layers covered:

1. Pure parser: ``parse_linkedin_url`` — URL shape recognition + name
   guess + hex-suffix stripping.
2. Compose helpers: truncation at sentence boundary + char_limit
   validation + prompt-builder smoke (asserting the char_limit instruction
   makes it into the prompt).
3. End-to-end POST /research-client with the Anthropic client mocked.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import compose_client, research_client
from app.services.compose_client import _truncate_at_sentence
from app.services.research_client import parse_linkedin_url


# ---- Parser ---------------------------------------------------------------


def test_parse_linkedin_url_basic_slug():
    slug, name = parse_linkedin_url("https://www.linkedin.com/in/jane-doe/")
    assert slug == "jane-doe"
    assert name == "Jane Doe"


def test_parse_linkedin_url_strips_hex_suffix():
    slug, name = parse_linkedin_url("https://www.linkedin.com/in/jane-doe-a5898840a")
    assert slug == "jane-doe-a5898840a"
    # The trailing dedup hash is dropped from the human-readable guess.
    assert name == "Jane Doe"


def test_parse_linkedin_url_handles_pub_path():
    slug, name = parse_linkedin_url("https://linkedin.com/pub/john-q-smith/12/345/678")
    assert slug == "john-q-smith"
    assert name == "John Q Smith"


def test_parse_linkedin_url_rejects_non_linkedin_urls():
    assert parse_linkedin_url("https://twitter.com/x") == ("", "")
    assert parse_linkedin_url("https://www.linkedin.com/company/x") == ("", "")
    assert parse_linkedin_url("not a url") == ("", "")
    assert parse_linkedin_url("") == ("", "")


# ---- Compose helpers: truncation -----------------------------------------


def test_truncate_at_sentence_under_limit_returns_unchanged():
    text = "Short message."
    assert _truncate_at_sentence(text, 100) == text


def test_truncate_at_sentence_cuts_at_period():
    text = "First sentence here. Second sentence runs longer than the cap allows for sure."
    out = _truncate_at_sentence(text, 30)
    # The cut should land on the period boundary, not mid-word.
    assert out == "First sentence here."


def test_truncate_at_sentence_falls_back_to_word_boundary():
    text = "one two three four five six seven eight nine ten eleven twelve thirteen"
    out = _truncate_at_sentence(text, 25)
    # No sentence punctuation → fall back to a word boundary, no mid-word cut.
    assert " " not in out[-1:]  # not trailing space
    assert not out.endswith("fou")  # didn't cut mid-word
    assert len(out) <= 25


def test_compose_for_client_rejects_out_of_range_char_limit():
    import asyncio
    with pytest.raises(ValueError):
        asyncio.run(compose_client.compose_for_client(
            output_kind="email", goal="g", tone="t", sender_name="s",
            char_limit=10, research={"quality": "low"},
        ))
    with pytest.raises(ValueError):
        asyncio.run(compose_client.compose_for_client(
            output_kind="email", goal="g", tone="t", sender_name="s",
            char_limit=10000, research={"quality": "low"},
        ))


def test_research_prompt_includes_freshness_rules():
    """The research prompt must:

    1. Explicitly bound news/personalization signals to a 6-month window
       with a 1-year hard floor (regression: a previous prompt asked
       for "last 12 months" company news which let stale references
       leak into compose).
    2. Distinguish between IDENTITY fields (no freshness gate — the
       LinkedIn profile is source of truth for current role) and
       PERSONALIZATION signals (strict freshness).  Regression: a
       previous version of this prompt required Anthropic to find a
       news article within 6 months to confirm the prospect's current
       role.  That left job_title/company blank for typical sales
       prospects with no press coverage — the user reported "no
       research" on 2026-05-21.
    """
    import inspect
    from app.services import research_client as rc
    src = inspect.getsource(rc.research_from_linkedin_url)
    # Freshness anchors for news.
    assert "6 months" in src.lower() or "183 days" in src or "freshness" in src.lower()
    assert "over a year old" in src.lower() or "365" in src
    # Current-role guard.
    assert "current role" in src.lower() or "CURRENT" in src
    # Identity vs personalization separation — must NOT require fresh
    # confirmation for identity fields.
    assert "IDENTITY" in src or "identity" in src
    assert "PERSONALIZATION" in src or "personalization" in src
    # The anti-regression: the prompt must NOT say "verified within the
    # freshness window" for the IDENTITY/CURRENT-role section.
    assert "verified within the freshness window" not in src
    assert "fresh-enough source" not in src


def test_compose_prompts_include_freshness_block_for_all_paths():
    """All four compose prompt paths (email-low, email-rich, dm-low,
    dm-rich) must carry the FRESHNESS RULES block so the message-writing
    LLM doesn't infer a stale reference from the research bullets."""
    low_research = {"quality": "low", "first_name": "Jane", "last_name": "Doe"}
    rich_research = {
        "quality": "rich", "first_name": "Jane", "last_name": "Doe",
        "job_title": "CEO", "company": "Acme",
        "headline": "Founder", "person_news": ["spoke at conf (Apr 2026)"],
        "company_news": ["raised Series B (Mar 2026)"],
        "company_description": "B2B platform",
    }
    for research in (low_research, rich_research):
        for builder in (compose_client._build_email_prompt, compose_client._build_dm_prompt):
            prompt = builder(
                goal="Book a call", tone="warm", sender_name="A",
                char_limit=400, research=research,
            )
            assert "FRESHNESS RULES" in prompt, builder.__name__
            assert "6 months" in prompt, builder.__name__
            assert "over a year old" in prompt.lower() or "over a year" in prompt, builder.__name__
            # And the current-role guard.
            assert "no longer holds" in prompt or "current role" in prompt.lower(), builder.__name__
            # Repost rule — naked reshares must NOT be referenced as if the
            # prospect wrote them.  Belt-and-suspenders against the
            # research-stage filter.
            assert "repost" in prompt.lower() or "reshare" in prompt.lower(), builder.__name__


def test_research_prompt_includes_repost_rule():
    """The research prompt must instruct Anthropic to EXCLUDE naked reposts
    (LinkedIn reshares with no added commentary) from person_news, and to
    INCLUDE reposts where the prospect added their own thoughts (described
    as their COMMENT, not as if they wrote the original).  Without this,
    cold-outreach messages reference reshared content as if the prospect
    authored it — wrong and embarrassing."""
    import inspect
    from app.services import research_client as rc
    src = inspect.getsource(rc.research_from_linkedin_url).lower()
    # Names the failure mode.
    assert "repost" in src or "reshare" in src
    # Names the carve-out for commentary.
    assert "commentary" in src or "added" in src or "commented" in src
    # Explicitly excludes the naked case.
    assert "naked" in src or "exclude" in src or "not their content" in src


def test_freshness_block_includes_concrete_dates():
    """The freshness block embeds today's ISO date plus the 6-month and
    1-year floors as concrete ISO dates so the LLM doesn't have to do
    date math (it's bad at it).  Asserting only the structure, not the
    exact dates, since this test runs at variable wall-clock times."""
    block = compose_client._freshness_block()
    # ISO-shaped dates appear three times: today, 6mo floor, 1y floor.
    import re as _re
    dates = _re.findall(r"\d{4}-\d{2}-\d{2}", block)
    assert len(dates) >= 3


def test_email_prompt_includes_char_limit():
    prompt = compose_client._build_email_prompt(
        goal="Book a call", tone="warm", sender_name="Anthony",
        char_limit=450,
        research={"quality": "rich", "first_name": "Jane", "last_name": "Doe",
                  "job_title": "CEO", "company": "Acme",
                  "headline": "Builder of things",
                  "person_news": ["raised Series B"], "company_news": [],
                  "company_description": "B2B platform"},
    )
    assert "450 characters" in prompt
    assert "raised Series B" in prompt


def test_dm_prompt_uses_low_quality_path_when_research_empty():
    prompt = compose_client._build_dm_prompt(
        goal="Open conversation", tone="casual", sender_name="Anthony",
        char_limit=250, research={"quality": "low", "first_name": "Jane"},
    )
    assert "250 characters" in prompt
    assert "No personalization signals" in prompt
    # Subject line must be requested for both DM paths now.
    assert "subject" in prompt.lower()


def test_dm_subject_falls_back_to_empty_when_model_omits_it():
    """Body-only DM responses still parse — older mocks / model drift
    shouldn't break the flow; we surface ``subject=""`` and let the UI
    render the body alone."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    body_only = SimpleNamespace(content=[SimpleNamespace(type="text", text='{"body": "Hi"}')])
    create = AsyncMock(return_value=body_only)
    with patch(
        "app.workers.compose._get_client",
        return_value=SimpleNamespace(messages=SimpleNamespace(create=create)),
    ):
        out = asyncio.run(compose_client.compose_for_client(
            output_kind="linkedin_dm", goal="g", tone="t", sender_name="s",
            char_limit=200,
            research={"quality": "low", "first_name": "Jane", "last_name": "Doe"},
        ))
    assert out["body"] == "Hi"
    assert out["subject"] == ""


# ---- End-to-end via the FastAPI client -----------------------------------


def _anthropic_text(body: str) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=body)])


@pytest.fixture(autouse=True)
def _reset_clients():
    research_client._client = None
    # The compose helper uses the bulk worker's _get_client, which is in compose mod.
    from app.workers import compose as compose_mod
    compose_mod._client = None


async def test_rejects_non_linkedin_url(client):
    resp = await client.post("/research-client", json={
        "linkedin_url": "https://example.com/profile",
        "goal": "Book a discovery call",
    })
    assert resp.status_code == 400
    assert "linkedin profile url" in resp.json()["detail"].lower()


async def test_happy_path_email(client):
    """Two Anthropic calls expected: research, then compose."""
    research_response = (
        '{"first_name": "Jane", "last_name": "Doe", '
        '"headline": "Founder", "company": "Acme", '
        '"company_website": "acme.io", "job_title": "CEO", '
        '"industry": "SaaS", "person_news": ["raised Series B"], '
        '"company_news": ["launched Acme Pro"], '
        '"company_description": "B2B platform", '
        '"recent_updates": [], "found": true}'
    )
    compose_response = (
        '{"subject": "Quick thought after your Series B", '
        '"body": "Hi Jane, congrats on the Series B raise. I help SaaS founders ship faster. Worth a 15-min call?"}'
    )

    research_mock = AsyncMock(return_value=_anthropic_text(research_response))
    compose_mock = AsyncMock(return_value=_anthropic_text(compose_response))

    with patch.object(
        research_client, "_get_client",
        return_value=SimpleNamespace(messages=SimpleNamespace(create=research_mock)),
    ), patch(
        "app.workers.compose._get_client",
        return_value=SimpleNamespace(messages=SimpleNamespace(create=compose_mock)),
    ):
        resp = await client.post("/research-client", json={
            "linkedin_url": "https://www.linkedin.com/in/jane-doe/",
            "goal": "Book a discovery call about scaling SaaS",
            "tone": "warm",
            "sender_name": "Anthony",
            "research_mode": "fast",
            "output_kind": "email",
            "char_limit": 600,
        })

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["profile"]["first_name"] == "Jane"
    assert body["profile"]["company"] == "Acme"
    assert body["profile"]["found"] is True
    assert body["profile"]["quality"] == "rich"
    assert "Series B" in body["body"]
    assert body["subject"]
    assert body["char_count"] == len(body["body"])
    assert body["char_count"] <= 600
    # Sanity: each mock was called exactly once.
    assert research_mock.await_count == 1
    assert compose_mock.await_count == 1


async def test_happy_path_linkedin_dm(client):
    """LinkedIn DM path: subject is empty, body present, char limit enforced."""
    research_response = (
        '{"first_name": "Bob", "last_name": "Smith", '
        '"headline": "VP Eng", "company": "Beta Corp", '
        '"company_website": "beta.com", "job_title": "VP Engineering", '
        '"industry": "FinTech", "person_news": ["spoke at Strange Loop"], '
        '"company_news": [], "company_description": "infra", '
        '"recent_updates": [], "found": true}'
    )
    dm_response = (
        '{"subject": "Notes on resilience", '
        '"body": "Hi Bob, caught your Strange Loop talk on resilience. Wanted to swap notes on what we ship at our shop. Worth a quick chat?"}'
    )

    research_mock = AsyncMock(return_value=_anthropic_text(research_response))
    compose_mock = AsyncMock(return_value=_anthropic_text(dm_response))

    with patch.object(
        research_client, "_get_client",
        return_value=SimpleNamespace(messages=SimpleNamespace(create=research_mock)),
    ), patch(
        "app.workers.compose._get_client",
        return_value=SimpleNamespace(messages=SimpleNamespace(create=compose_mock)),
    ):
        resp = await client.post("/research-client", json={
            "linkedin_url": "https://www.linkedin.com/in/bob-smith-9999999a/",
            "goal": "Open a conversation about distributed systems",
            "research_mode": "deep",
            "output_kind": "linkedin_dm",
            "char_limit": 300,
        })

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["subject"] == "Notes on resilience"
    assert "Strange Loop" in body["body"]
    assert body["char_count"] <= 300


async def test_research_uses_haiku_research_client_model_not_sonnet(client):
    """Cost optimisation: research-a-client switched from
    settings.ANTHROPIC_MODEL (Sonnet) to
    settings.ANTHROPIC_RESEARCH_CLIENT_MODEL (Haiku) — same
    extraction task as the bulk pipeline, ~4x cheaper."""
    from app.config import settings as app_settings
    captured = {}

    async def _spy(**kwargs):
        captured["kwargs"] = kwargs
        return _anthropic_text('{"found": false, "person_news": [], "company_news": []}')

    with patch.object(
        research_client, "_get_client",
        return_value=SimpleNamespace(messages=SimpleNamespace(create=_spy)),
    ), patch(
        "app.workers.compose._get_client",
        return_value=SimpleNamespace(messages=SimpleNamespace(
            create=AsyncMock(return_value=_anthropic_text('{"subject": "s", "body": "b"}')),
        )),
    ):
        await client.post("/research-client", json={
            "linkedin_url": "https://www.linkedin.com/in/jane-test/",
            "goal": "x" * 10,
            "research_mode": "fast",
            "output_kind": "email",
            "char_limit": 400,
        })

    assert captured["kwargs"]["model"] == app_settings.ANTHROPIC_RESEARCH_CLIENT_MODEL
    # And the model is in fact Haiku by default — locks the default.
    assert "haiku" in captured["kwargs"]["model"].lower()
    # Fast mode uses the FAST budget; deep would use the deeper one.
    assert captured["kwargs"]["tools"][0]["max_uses"] == app_settings.RESEARCH_CLIENT_FAST_WEB_SEARCH_MAX_USES
    assert app_settings.RESEARCH_CLIENT_FAST_WEB_SEARCH_MAX_USES == 2  # default
    assert app_settings.RESEARCH_CLIENT_DEEP_WEB_SEARCH_MAX_USES == 5  # default


async def test_research_deep_mode_uses_deep_max_uses(client):
    """Deep mode gets the larger search budget so Claude can hunt
    podcast / GitHub / substack signal that fast mode skips."""
    from app.config import settings as app_settings
    captured = {}

    async def _spy(**kwargs):
        captured["kwargs"] = kwargs
        return _anthropic_text('{"found": false, "person_news": [], "company_news": []}')

    with patch.object(
        research_client, "_get_client",
        return_value=SimpleNamespace(messages=SimpleNamespace(create=_spy)),
    ), patch(
        "app.workers.compose._get_client",
        return_value=SimpleNamespace(messages=SimpleNamespace(
            create=AsyncMock(return_value=_anthropic_text('{"subject": "s", "body": "b"}')),
        )),
    ):
        await client.post("/research-client", json={
            "linkedin_url": "https://www.linkedin.com/in/deep-test/",
            "goal": "x" * 10,
            "research_mode": "deep",
            "output_kind": "linkedin_dm",
            "char_limit": 300,
        })

    assert captured["kwargs"]["tools"][0]["max_uses"] == app_settings.RESEARCH_CLIENT_DEEP_WEB_SEARCH_MAX_USES


async def test_research_cache_hit_skips_anthropic_research(client, db_session):
    """Second click on the same LinkedIn URL within the cache TTL must
    skip the Anthropic research call entirely — that's the dominant
    cost lever on repeat lookups."""
    from app.models import ResearchCache
    from datetime import datetime, timezone

    # Pre-seed the cache for this slug.
    db_session.add(ResearchCache(
        email="linkedin:jane-cached",
        research_data={
            "first_name": "Jane", "last_name": "Cached",
            "headline": "From cache", "company": "Acme",
            "company_website": "acme.io", "job_title": "CEO",
            "industry": "SaaS", "found": True, "quality": "rich",
            "person_news": ["cached news (Apr 2026)"],
            "company_news": [], "recent_updates": [],
            "company_description": "cached",
        },
        refreshed_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()

    research_mock = AsyncMock(
        return_value=_anthropic_text('{"found": false}')
    )
    compose_mock = AsyncMock(
        return_value=_anthropic_text('{"subject": "s", "body": "b"}')
    )

    with patch.object(
        research_client, "_get_client",
        return_value=SimpleNamespace(messages=SimpleNamespace(create=research_mock)),
    ), patch(
        "app.workers.compose._get_client",
        return_value=SimpleNamespace(messages=SimpleNamespace(create=compose_mock)),
    ):
        resp = await client.post("/research-client", json={
            "linkedin_url": "https://www.linkedin.com/in/jane-cached/",
            "goal": "Book a call",
            "research_mode": "fast",
            "output_kind": "email",
            "char_limit": 600,
        })

    assert resp.status_code == 200, resp.text
    # The cache hit means the research Anthropic call NEVER happened.
    research_mock.assert_not_called()
    # Compose still runs (we need the actual outreach copy).
    compose_mock.assert_called_once()
    # And the response surfaces from_cache=True so the UI can hint.
    assert resp.json()["research"]["from_cache"] is True
    assert resp.json()["profile"]["first_name"] == "Jane"


async def test_research_cache_miss_writes_after_successful_research(client, db_session):
    """First call on an unseen URL hits Anthropic, then writes to the
    cache so subsequent calls within the TTL are free."""
    from app.models import ResearchCache
    from sqlalchemy import select as _select

    research_response = (
        '{"first_name": "Bob", "last_name": "Fresh", '
        '"headline": "VP", "company": "Beta", '
        '"company_website": "beta.com", "job_title": "VP Eng", '
        '"industry": "FinTech", "person_news": ["talk (Apr 2026)"], '
        '"company_news": [], "company_description": "infra", '
        '"recent_updates": [], "found": true}'
    )
    research_mock = AsyncMock(return_value=_anthropic_text(research_response))
    compose_mock = AsyncMock(
        return_value=_anthropic_text('{"subject": "s", "body": "b"}')
    )

    with patch.object(
        research_client, "_get_client",
        return_value=SimpleNamespace(messages=SimpleNamespace(create=research_mock)),
    ), patch(
        "app.workers.compose._get_client",
        return_value=SimpleNamespace(messages=SimpleNamespace(create=compose_mock)),
    ):
        resp = await client.post("/research-client", json={
            "linkedin_url": "https://www.linkedin.com/in/bob-fresh/",
            "goal": "Book a call",
            "research_mode": "fast",
            "output_kind": "email",
            "char_limit": 600,
        })

    assert resp.status_code == 200
    research_mock.assert_called_once()

    # Cache row was written for the slug-namespaced key.
    cached = await db_session.scalar(
        _select(ResearchCache).where(ResearchCache.email == "linkedin:bob-fresh")
    )
    assert cached is not None
    assert cached.research_data["first_name"] == "Bob"


async def test_research_deep_mode_bypasses_cache(client, db_session):
    """Deep mode explicitly asks for fresh research — must NOT serve
    from the cache even when a hit exists.  Otherwise the user's
    "give me more depth" click would silently no-op."""
    from app.models import ResearchCache
    from datetime import datetime, timezone

    db_session.add(ResearchCache(
        email="linkedin:carol-deep",
        research_data={"first_name": "Carol", "found": True, "quality": "low",
                       "person_news": [], "company_news": [], "recent_updates": []},
        refreshed_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()

    research_response = (
        '{"first_name": "Carol", "last_name": "Deep", '
        '"headline": "Founder", "company": "Gamma", '
        '"company_website": "gamma.com", "job_title": "CEO", '
        '"industry": "Health", "person_news": ["DEEP news (May 2026)"], '
        '"company_news": [], "company_description": "deep", '
        '"recent_updates": [], "found": true}'
    )
    research_mock = AsyncMock(return_value=_anthropic_text(research_response))
    compose_mock = AsyncMock(
        return_value=_anthropic_text('{"subject": "s", "body": "b"}')
    )

    with patch.object(
        research_client, "_get_client",
        return_value=SimpleNamespace(messages=SimpleNamespace(create=research_mock)),
    ), patch(
        "app.workers.compose._get_client",
        return_value=SimpleNamespace(messages=SimpleNamespace(create=compose_mock)),
    ):
        resp = await client.post("/research-client", json={
            "linkedin_url": "https://www.linkedin.com/in/carol-deep/",
            "goal": "Book a call",
            "research_mode": "deep",
            "output_kind": "email",
            "char_limit": 600,
        })

    # Anthropic WAS called despite the cache being primed.
    research_mock.assert_called_once()
    assert "DEEP news" in resp.json()["research"]["person_news"][0]


async def test_research_cache_no_write_when_found_false(client, db_session):
    """If Anthropic comes back with ``found=false`` (no real identity),
    don't poison the cache — a future call might succeed."""
    from app.models import ResearchCache
    from sqlalchemy import select as _select

    research_mock = AsyncMock(
        return_value=_anthropic_text('{"found": false, "first_name": "", "last_name": "",'
                                     ' "person_news": [], "company_news": [], "recent_updates": []}')
    )
    compose_mock = AsyncMock(
        return_value=_anthropic_text('{"subject": "s", "body": "b"}')
    )

    with patch.object(
        research_client, "_get_client",
        return_value=SimpleNamespace(messages=SimpleNamespace(create=research_mock)),
    ), patch(
        "app.workers.compose._get_client",
        return_value=SimpleNamespace(messages=SimpleNamespace(create=compose_mock)),
    ):
        await client.post("/research-client", json={
            "linkedin_url": "https://www.linkedin.com/in/unknown-person/",
            "goal": "Book a call",
            "research_mode": "fast",
            "output_kind": "email",
            "char_limit": 600,
        })

    cached = await db_session.scalar(
        _select(ResearchCache).where(ResearchCache.email == "linkedin:unknown-person")
    )
    assert cached is None


async def test_anthropic_auth_failure_returns_502_with_actionable_detail(client):
    """A bad/expired ANTHROPIC_API_KEY surfaces as a 502 with a clear
    remediation hint, not a generic 500.  Regression: prior to the fix
    a 401 from Anthropic propagated up through both the research stage
    (caught) and the compose stage (uncaught), and the latter became
    an opaque 500 in the browser console."""
    from anthropic import AuthenticationError

    # The research stage already catches every exception and returns
    # an empty dict, so the failure mode that escapes is the compose
    # call.  Patch the compose stage's Anthropic client to raise
    # AuthenticationError on every messages.create.
    auth_err = AuthenticationError(
        message="Invalid authentication credentials",
        response=SimpleNamespace(status_code=401, headers={}, request=SimpleNamespace()),
        body=None,
    )
    research_response = (
        '{"first_name": "Jane", "last_name": "Doe", '
        '"headline": "", "company": "", "company_website": "", '
        '"job_title": "", "industry": "", "person_news": [], '
        '"company_news": [], "company_description": "", '
        '"recent_updates": [], "found": false}'
    )
    research_mock = AsyncMock(return_value=_anthropic_text(research_response))
    compose_mock = AsyncMock(side_effect=auth_err)

    with patch.object(
        research_client, "_get_client",
        return_value=SimpleNamespace(messages=SimpleNamespace(create=research_mock)),
    ), patch(
        "app.workers.compose._get_client",
        return_value=SimpleNamespace(messages=SimpleNamespace(create=compose_mock)),
    ):
        resp = await client.post("/research-client", json={
            "linkedin_url": "https://www.linkedin.com/in/jane-doe/",
            "goal": "Book a call",
            "char_limit": 300,
            "output_kind": "linkedin_dm",
        })

    assert resp.status_code == 502, resp.text
    detail = resp.json()["detail"].lower()
    # Actionable: tell the user what to do, not just "auth failed".
    assert "anthropic" in detail
    assert "key" in detail
    assert ".env" in detail


async def test_compose_failure_returns_502(client):
    """Anthropic returns garbage twice → compose helper raises → 502 to caller."""
    research_response = (
        '{"first_name": "X", "last_name": "Y", "headline": "", "company": "Z", '
        '"company_website": "", "job_title": "", "industry": "", '
        '"person_news": [], "company_news": [], "company_description": "", '
        '"recent_updates": [], "found": false}'
    )
    research_mock = AsyncMock(return_value=_anthropic_text(research_response))
    # Compose returns unparseable text both times.
    compose_mock = AsyncMock(return_value=_anthropic_text("not a json blob"))

    with patch.object(
        research_client, "_get_client",
        return_value=SimpleNamespace(messages=SimpleNamespace(create=research_mock)),
    ), patch(
        "app.workers.compose._get_client",
        return_value=SimpleNamespace(messages=SimpleNamespace(create=compose_mock)),
    ):
        resp = await client.post("/research-client", json={
            "linkedin_url": "https://www.linkedin.com/in/foo/",
            "goal": "ping",
            "char_limit": 200,
            "output_kind": "email",
        })
    assert resp.status_code == 502


# ---- POST /research-client/send ------------------------------------------

async def test_send_endpoint_dispatches_to_brevo(client, monkeypatch):
    """Happy path: payload validates, the synthesised HTML/text bodies
    reach Brevo via send_email, and the endpoint returns the message_id
    + sent_at + to_email contract."""
    monkeypatch.setattr(
        "app.routers.research_client.settings.BREVO_API_KEY", "test-key",
    )
    monkeypatch.setattr(
        "app.routers.research_client.settings.BREVO_SENDER_EMAIL",
        "outreach@example.com",
    )

    send_mock = AsyncMock(return_value="brevo-msg-001")
    with patch("app.routers.research_client.brevo.send_email", new=send_mock):
        resp = await client.post("/research-client/send", json={
            "to_email": "jane@example.com",
            "to_name": "Jane Doe",
            "subject": "Quick thought after your Series B",
            "body": "Hi Jane, congrats on the Series B raise.",
            "sender_name": "Anthony",
        })

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["message_id"] == "brevo-msg-001"
    assert body["to_email"] == "jane@example.com"
    assert body["sent_at"]

    # Brevo got the right inputs.  HTML + text bodies are generated from
    # the request body (render_html / render_text), so just verify the
    # plain text is included verbatim somewhere.
    assert send_mock.await_count == 1
    kwargs = send_mock.await_args.kwargs
    assert kwargs["to_email"] == "jane@example.com"
    assert kwargs["to_name"] == "Jane Doe"
    assert kwargs["subject"] == "Quick thought after your Series B"
    assert "congrats on the Series B" in kwargs["text_body"]
    assert kwargs["sender_name"] == "Anthony"
    assert kwargs["sender_email"] == "outreach@example.com"
    assert kwargs["campaign_id"] == "research-client"
    # Synthetic lead UUID — non-empty, not the literal string.
    assert kwargs["lead_id"] and kwargs["lead_id"] != "research-client"


async def test_send_endpoint_uses_explicit_sender_email_when_supplied(client, monkeypatch):
    """When the request carries an explicit ``sender_email``, it wins
    over the BREVO_SENDER_EMAIL fallback."""
    monkeypatch.setattr(
        "app.routers.research_client.settings.BREVO_API_KEY", "test-key",
    )
    monkeypatch.setattr(
        "app.routers.research_client.settings.BREVO_SENDER_EMAIL",
        "default@example.com",
    )

    send_mock = AsyncMock(return_value="msg-x")
    with patch("app.routers.research_client.brevo.send_email", new=send_mock):
        resp = await client.post("/research-client/send", json={
            "to_email": "j@example.com",
            "subject": "s", "body": "b",
            "sender_name": "A",
            "sender_email": "anthony@me.com",
        })
    assert resp.status_code == 200
    assert send_mock.await_args.kwargs["sender_email"] == "anthony@me.com"


async def test_send_endpoint_rejects_invalid_email(client, monkeypatch):
    """Pydantic ``EmailStr`` catches obvious garbage at the route layer
    without burning a Brevo call."""
    monkeypatch.setattr(
        "app.routers.research_client.settings.BREVO_API_KEY", "test-key",
    )
    send_mock = AsyncMock()
    with patch("app.routers.research_client.brevo.send_email", new=send_mock):
        resp = await client.post("/research-client/send", json={
            "to_email": "not-an-email",
            "subject": "s", "body": "b", "sender_name": "A",
        })
    assert resp.status_code == 422
    send_mock.assert_not_called()


async def test_send_endpoint_502_when_brevo_api_key_missing(client, monkeypatch):
    """Missing BREVO_API_KEY → 502 with an actionable message.  Caught
    BEFORE the Brevo call attempt so no spurious network hit."""
    monkeypatch.setattr(
        "app.routers.research_client.settings.BREVO_API_KEY", "",
    )
    send_mock = AsyncMock()
    with patch("app.routers.research_client.brevo.send_email", new=send_mock):
        resp = await client.post("/research-client/send", json={
            "to_email": "j@example.com",
            "subject": "s", "body": "b", "sender_name": "A",
        })
    assert resp.status_code == 502
    assert "brevo_api_key" in resp.json()["detail"].lower()
    send_mock.assert_not_called()


async def test_send_endpoint_502_on_brevo_http_error(client, monkeypatch):
    """A Brevo 4xx/5xx rejection (bad key, unverified sender, etc.)
    surfaces as a 502 with Brevo's status + a hint, not a generic 500."""
    import httpx

    monkeypatch.setattr(
        "app.routers.research_client.settings.BREVO_API_KEY", "test-key",
    )

    fake_response = httpx.Response(
        status_code=401,
        request=httpx.Request("POST", "https://api.brevo.com/v3/smtp/email"),
        text='{"code":"unauthorized","message":"Key not found"}',
    )
    err = httpx.HTTPStatusError("401", request=fake_response.request, response=fake_response)

    send_mock = AsyncMock(side_effect=err)
    with patch("app.routers.research_client.brevo.send_email", new=send_mock):
        resp = await client.post("/research-client/send", json={
            "to_email": "j@example.com",
            "subject": "s", "body": "b", "sender_name": "A",
        })
    assert resp.status_code == 502
    detail = resp.json()["detail"].lower()
    assert "401" in detail
    assert "brevo" in detail


async def test_send_endpoint_uses_db_default_sender_when_no_override(client, monkeypatch, db_session):
    """When the request omits sender_email AND a ConnectedAccount is
    marked is_default_sender=True, that account's email wins over
    settings.BREVO_SENDER_EMAIL.  This is the "I set a default in
    Settings, every one-off now uses it" path."""
    from app.models import ConnectedAccount

    monkeypatch.setattr(
        "app.routers.research_client.settings.BREVO_API_KEY", "test-key",
    )
    monkeypatch.setattr(
        "app.routers.research_client.settings.BREVO_SENDER_EMAIL",
        "env-default@example.com",
    )

    db_session.add(ConnectedAccount(
        label="My brand",
        email_address="brand@me.com",
        imap_host="imap.x", imap_port=993, imap_use_ssl=True,
        username="brand@me.com",
        password_encrypted="x",
        is_default_sender=True,
    ))
    await db_session.commit()

    send_mock = AsyncMock(return_value="msg-x")
    with patch("app.routers.research_client.brevo.send_email", new=send_mock):
        resp = await client.post("/research-client/send", json={
            "to_email": "j@example.com",
            "subject": "s", "body": "b", "sender_name": "A",
        })
    assert resp.status_code == 200
    assert send_mock.await_args.kwargs["sender_email"] == "brand@me.com"


async def test_send_endpoint_explicit_override_beats_db_default(client, monkeypatch, db_session):
    """Request-level sender_email override wins over BOTH the DB default
    sender and the env var.  Order of precedence is locked here."""
    from app.models import ConnectedAccount

    monkeypatch.setattr(
        "app.routers.research_client.settings.BREVO_API_KEY", "test-key",
    )
    monkeypatch.setattr(
        "app.routers.research_client.settings.BREVO_SENDER_EMAIL",
        "env-default@example.com",
    )
    db_session.add(ConnectedAccount(
        label="Brand", email_address="brand@me.com",
        imap_host="imap.x", imap_port=993, imap_use_ssl=True,
        username="brand@me.com", password_encrypted="x",
        is_default_sender=True,
    ))
    await db_session.commit()

    send_mock = AsyncMock(return_value="msg-y")
    with patch("app.routers.research_client.brevo.send_email", new=send_mock):
        resp = await client.post("/research-client/send", json={
            "to_email": "j@example.com",
            "subject": "s", "body": "b", "sender_name": "A",
            "sender_email": "explicit@override.com",
        })
    assert resp.status_code == 200
    assert send_mock.await_args.kwargs["sender_email"] == "explicit@override.com"


async def test_send_endpoint_appends_signature_from_picked_connected_account(client, monkeypatch, db_session):
    """When the request sender_email matches a ConnectedAccount and that
    account has a signature, the Brevo bodies (html + text) carry the
    signature appended below the AI-composed body."""
    from app.models import ConnectedAccount

    monkeypatch.setattr(
        "app.routers.research_client.settings.BREVO_API_KEY", "test-key",
    )
    db_session.add(ConnectedAccount(
        label="Brand", email_address="brand@me.com",
        imap_host="imap.x", imap_port=993, imap_use_ssl=True,
        username="brand@me.com", password_encrypted="x",
        signature="Best,\nAnthony\ncsuitecode.com",
    ))
    await db_session.commit()

    send_mock = AsyncMock(return_value="msg-sig")
    with patch("app.routers.research_client.brevo.send_email", new=send_mock):
        resp = await client.post("/research-client/send", json={
            "to_email": "j@example.com",
            "subject": "Hello",
            # Composed body ends without a sign-off — signature is
            # appended after a blank line per apply_signature's fallback.
            "body": "Hi Jane, quick thought after your raise.",
            "sender_name": "Anthony",
            "sender_email": "brand@me.com",
        })

    assert resp.status_code == 200
    kwargs = send_mock.await_args.kwargs
    # Both rendered bodies include the signature.
    assert "csuitecode.com" in kwargs["text_body"]
    assert "Anthony" in kwargs["text_body"]
    # And the original body wasn't lost.
    assert "quick thought after your raise" in kwargs["text_body"]


async def test_send_endpoint_html_signature_renders_as_real_html(client, monkeypatch, db_session):
    """A signature containing <a>/<img> tags must reach the recipient as
    real clickable links and rendered images.  Anti-regression: before
    this change, the signature went through ``render_html`` which would
    HTML-escape the tags into ``&lt;a&gt;`` etc."""
    from app.models import ConnectedAccount

    monkeypatch.setattr(
        "app.routers.research_client.settings.BREVO_API_KEY", "test-key",
    )
    sig_html = (
        'Best,\n<strong>Anthony</strong>\n'
        '<a href="https://csuitecode.com">csuitecode.com</a>\n'
        '<img src="https://example.com/logo.png" alt="CSuite Code" '
        'style="max-width:120px;">'
    )
    db_session.add(ConnectedAccount(
        label="Brand", email_address="brand@me.com",
        imap_host="imap.x", imap_port=993, imap_use_ssl=True,
        username="brand@me.com", password_encrypted="x",
        signature=sig_html,
    ))
    await db_session.commit()

    send_mock = AsyncMock(return_value="msg-html-sig")
    with patch("app.routers.research_client.brevo.send_email", new=send_mock):
        await client.post("/research-client/send", json={
            "to_email": "j@example.com",
            "subject": "Hi",
            # Body has its own AI sign-off ("Best,\nAnthony") that must
            # be stripped so the signature replaces it cleanly.
            "body": "Hi Jane.\n\nQuick thought.\n\nBest,\nAnthony",
            "sender_name": "Anthony",
            "sender_email": "brand@me.com",
        })

    kwargs = send_mock.await_args.kwargs

    # ── HTML body assertions ──────────────────────────────────────────
    html = kwargs["html_body"]
    # Body content preserved.
    assert "Quick thought." in html
    # The AI sign-off was stripped before render (not duplicated).
    assert html.count("Anthony") == 1
    # Signature tags came through as REAL HTML, not escaped.  Bare
    # anchors get the default blue+underline style auto-injected so
    # links look consistent across clients (Gmail strips defaults).
    assert 'href="https://csuitecode.com"' in html
    assert "color:#1d4ed8;text-decoration:underline;" in html
    assert '<img src="https://example.com/logo.png"' in html
    assert "<strong>" in html
    # And the signature lives inside the <body> wrapper, not after.
    body_idx = html.index("<body")
    body_close_idx = html.index("</body>")
    assert body_idx < html.index("csuitecode.com") < body_close_idx

    # ── Plain-text body assertions ────────────────────────────────────
    text = kwargs["text_body"]
    assert "Quick thought." in text
    # Anchor collapsed to "text (URL)".
    assert "csuitecode.com (https://csuitecode.com)" in text
    # Image rendered as bracketed placeholder.
    assert "[image: CSuite Code — https://example.com/logo.png]" in text
    # No raw HTML left in the text version.
    assert "<a " not in text
    assert "<img" not in text
    assert "<strong>" not in text


async def test_send_endpoint_plain_text_signature_still_works(client, monkeypatch, db_session):
    """Backward compat: a plain-text signature (no HTML tags) must keep
    working — newlines become <br> in the HTML body, identical newlines
    survive in the text body."""
    from app.models import ConnectedAccount

    monkeypatch.setattr(
        "app.routers.research_client.settings.BREVO_API_KEY", "test-key",
    )
    db_session.add(ConnectedAccount(
        label="Brand", email_address="brand@me.com",
        imap_host="imap.x", imap_port=993, imap_use_ssl=True,
        username="brand@me.com", password_encrypted="x",
        signature="Best,\nAnthony\ncsuitecode.com",
    ))
    await db_session.commit()

    send_mock = AsyncMock(return_value="msg-plain-sig")
    with patch("app.routers.research_client.brevo.send_email", new=send_mock):
        await client.post("/research-client/send", json={
            "to_email": "j@example.com",
            "subject": "Hi",
            "body": "Hi Jane.\n\nWorth a chat?",
            "sender_name": "Anthony",
            "sender_email": "brand@me.com",
        })

    kwargs = send_mock.await_args.kwargs
    # HTML body: newlines in signature → <br>.
    assert "Best,<br>" in kwargs["html_body"]
    assert "csuitecode.com" in kwargs["html_body"]
    # Text body: identical newlines.
    assert "Best,\nAnthony\ncsuitecode.com" in kwargs["text_body"]


async def test_send_endpoint_signature_replaces_ai_signoff(client, monkeypatch, db_session):
    """When the composed body ends with the AI's own sign-off ("Best,\\nName"),
    apply_signature swaps from there to the end — no duplicated sign-off."""
    from app.models import ConnectedAccount

    monkeypatch.setattr(
        "app.routers.research_client.settings.BREVO_API_KEY", "test-key",
    )
    db_session.add(ConnectedAccount(
        label="Brand", email_address="brand@me.com",
        imap_host="imap.x", imap_port=993, imap_use_ssl=True,
        username="brand@me.com", password_encrypted="x",
        signature="Best,\nAnthony Colasante\ncsuitecode.com",
    ))
    await db_session.commit()

    send_mock = AsyncMock(return_value="msg-sig2")
    with patch("app.routers.research_client.brevo.send_email", new=send_mock):
        await client.post("/research-client/send", json={
            "to_email": "j@example.com",
            "subject": "Hello",
            # AI already ended the body with a sign-off — must be replaced,
            # not duplicated.
            "body": "Hi Jane.\n\nQuick thought.\n\nBest,\nAnthony",
            "sender_name": "Anthony",
            "sender_email": "brand@me.com",
        })

    text = send_mock.await_args.kwargs["text_body"]
    # Body content preserved.
    assert "Quick thought." in text
    # The signature replaced the AI sign-off — only ONE "Best," line.
    assert text.count("Best,") == 1
    # And the contact line from the signature is present.
    assert "csuitecode.com" in text


async def test_send_endpoint_no_signature_when_account_has_none(client, monkeypatch, db_session):
    """Empty/null signature on the resolved account → body sent verbatim."""
    from app.models import ConnectedAccount

    monkeypatch.setattr(
        "app.routers.research_client.settings.BREVO_API_KEY", "test-key",
    )
    db_session.add(ConnectedAccount(
        label="Plain", email_address="plain@me.com",
        imap_host="imap.x", imap_port=993, imap_use_ssl=True,
        username="plain@me.com", password_encrypted="x",
        signature=None,
    ))
    await db_session.commit()

    send_mock = AsyncMock(return_value="msg-plain")
    with patch("app.routers.research_client.brevo.send_email", new=send_mock):
        await client.post("/research-client/send", json={
            "to_email": "j@example.com",
            "subject": "Hi",
            "body": "Hi Jane, ping me when you can.",
            "sender_name": "A",
            "sender_email": "plain@me.com",
        })

    text = send_mock.await_args.kwargs["text_body"]
    # No signature line added — body unchanged save for the email_template render.
    assert "csuitecode.com" not in text
    # The original body is there.
    assert "ping me when you can" in text


async def test_send_endpoint_unknown_sender_email_skips_signature(client, monkeypatch):
    """When the request's sender_email doesn't match ANY ConnectedAccount,
    there's no signature to look up — the body goes out as-is.  Prevents
    accidentally pulling the wrong account's signature."""
    monkeypatch.setattr(
        "app.routers.research_client.settings.BREVO_API_KEY", "test-key",
    )
    send_mock = AsyncMock(return_value="msg-unknown")
    with patch("app.routers.research_client.brevo.send_email", new=send_mock):
        await client.post("/research-client/send", json={
            "to_email": "j@example.com",
            "subject": "Hi",
            "body": "Body without a signature anywhere.",
            "sender_name": "A",
            "sender_email": "random@nowhere.com",
        })

    text = send_mock.await_args.kwargs["text_body"]
    assert "Body without a signature anywhere" in text


async def test_send_endpoint_default_sender_signature_applies(client, monkeypatch, db_session):
    """When the request omits sender_email AND the workspace default
    sender has a signature, the default sender's signature applies."""
    from app.models import ConnectedAccount

    monkeypatch.setattr(
        "app.routers.research_client.settings.BREVO_API_KEY", "test-key",
    )
    db_session.add(ConnectedAccount(
        label="Default", email_address="default@me.com",
        imap_host="imap.x", imap_port=993, imap_use_ssl=True,
        username="default@me.com", password_encrypted="x",
        signature="--\nAnthony · default sender",
        is_default_sender=True,
    ))
    await db_session.commit()

    send_mock = AsyncMock(return_value="msg-default")
    with patch("app.routers.research_client.brevo.send_email", new=send_mock):
        await client.post("/research-client/send", json={
            "to_email": "j@example.com",
            "subject": "Hi",
            "body": "Body without an explicit sender_email.",
            "sender_name": "A",
        })

    text = send_mock.await_args.kwargs["text_body"]
    assert "default sender" in text


async def test_send_endpoint_falls_back_to_env_when_no_db_default(client, monkeypatch):
    """No DB default-sender row + no request override → env var wins."""
    monkeypatch.setattr(
        "app.routers.research_client.settings.BREVO_API_KEY", "test-key",
    )
    monkeypatch.setattr(
        "app.routers.research_client.settings.BREVO_SENDER_EMAIL",
        "env-default@example.com",
    )
    send_mock = AsyncMock(return_value="msg-z")
    with patch("app.routers.research_client.brevo.send_email", new=send_mock):
        resp = await client.post("/research-client/send", json={
            "to_email": "j@example.com",
            "subject": "s", "body": "b", "sender_name": "A",
        })
    assert resp.status_code == 200
    assert send_mock.await_args.kwargs["sender_email"] == "env-default@example.com"


async def test_send_endpoint_502_on_network_error(client, monkeypatch):
    """A transport-level failure (DNS, connection refused, etc.) also
    becomes a 502 — never bleeds into a 500."""
    import httpx

    monkeypatch.setattr(
        "app.routers.research_client.settings.BREVO_API_KEY", "test-key",
    )
    send_mock = AsyncMock(side_effect=httpx.ConnectError("dns failure"))
    with patch("app.routers.research_client.brevo.send_email", new=send_mock):
        resp = await client.post("/research-client/send", json={
            "to_email": "j@example.com",
            "subject": "s", "body": "b", "sender_name": "A",
        })
    assert resp.status_code == 502
    assert "network" in resp.json()["detail"].lower()


# ---- CRM auto-tracking on send --------------------------------------------

async def test_send_creates_crm_lead_and_logs_email_activity(client, monkeypatch, db_session):
    """Sending to an email with NO existing lead creates a campaign-less
    CRM lead (name split from to_name) and logs an outbound email
    activity against it."""
    from sqlalchemy import func as _f, select as _select
    from app.models import CrmActivity, Lead

    monkeypatch.setattr(
        "app.routers.research_client.settings.BREVO_API_KEY", "test-key",
    )
    send_mock = AsyncMock(return_value="msg-crm-1")
    with patch("app.routers.research_client.brevo.send_email", new=send_mock):
        resp = await client.post("/research-client/send", json={
            "to_email": "Fresh.Prospect@Acme.io",
            "to_name": "Jane Van Doe",
            "subject": "Quick thought",
            "body": "Hi Jane, worth a chat?",
            "sender_name": "Anthony",
        })

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["crm_lead_created"] is True
    assert body["crm_activity_logged"] is True
    assert body["crm_lead_id"]

    lead = await db_session.scalar(
        _select(Lead).where(_f.lower(Lead.email) == "fresh.prospect@acme.io")
    )
    assert lead is not None
    assert lead.campaign_id is None          # CRM lead, no campaign
    assert lead.first_name == "Jane"
    assert lead.last_name == "Van Doe"       # split on FIRST space only
    assert lead.crm_status == "new"

    act = await db_session.scalar(
        _select(CrmActivity).where(CrmActivity.lead_id == lead.id)
    )
    assert act is not None
    assert act.activity_type.value == "email"
    assert act.direction.value == "outbound"
    assert act.subject == "Quick thought"
    assert "worth a chat" in act.body


async def test_send_reuses_existing_lead_no_duplicate(client, monkeypatch, db_session):
    """An email that already has a lead row gets the activity logged on
    the existing record — no second lead created."""
    from sqlalchemy import func as _f, select as _select
    from app.models import CrmActivity, Lead

    monkeypatch.setattr(
        "app.routers.research_client.settings.BREVO_API_KEY", "test-key",
    )
    existing = Lead(campaign_id=None, email="known@x.com", first_name="K")
    db_session.add(existing)
    await db_session.commit()

    send_mock = AsyncMock(return_value="msg-crm-2")
    with patch("app.routers.research_client.brevo.send_email", new=send_mock):
        resp = await client.post("/research-client/send", json={
            # Different casing — must still match the existing row.
            "to_email": "Known@X.com",
            "subject": "Hello again",
            "body": "Following up.",
            "sender_name": "A",
        })

    body = resp.json()
    assert body["crm_lead_created"] is False
    assert body["crm_lead_id"] == str(existing.id)

    leads = (await db_session.execute(
        _select(Lead).where(_f.lower(Lead.email) == "known@x.com")
    )).scalars().all()
    assert len(leads) == 1  # no duplicate

    act = await db_session.scalar(
        _select(CrmActivity).where(CrmActivity.lead_id == existing.id)
    )
    assert act is not None
    assert act.subject == "Hello again"


async def test_send_attaches_activity_to_matching_opportunity(client, monkeypatch, db_session):
    """When an opportunity carries the same email, the logged activity
    spans BOTH parents so the deal timeline captures the touch."""
    from sqlalchemy import select as _select
    from app.models import CrmActivity, Opportunity

    monkeypatch.setattr(
        "app.routers.research_client.settings.BREVO_API_KEY", "test-key",
    )
    opp = Opportunity(name="Deal", email="deal@x.com")
    db_session.add(opp)
    await db_session.commit()

    send_mock = AsyncMock(return_value="msg-crm-3")
    with patch("app.routers.research_client.brevo.send_email", new=send_mock):
        resp = await client.post("/research-client/send", json={
            "to_email": "deal@x.com",
            "subject": "Proposal attached",
            "body": "See attached.",
            "sender_name": "A",
        })

    assert resp.json()["crm_activity_logged"] is True
    act = await db_session.scalar(
        _select(CrmActivity).where(CrmActivity.opportunity_id == opp.id)
    )
    assert act is not None
    assert act.lead_id is not None  # also attached to the (new) lead


async def test_send_succeeds_even_when_crm_tracking_fails(client, monkeypatch):
    """CRM tracking is best-effort: a failure there must not fail the
    send response (the email already went out via Brevo)."""
    monkeypatch.setattr(
        "app.routers.research_client.settings.BREVO_API_KEY", "test-key",
    )
    send_mock = AsyncMock(return_value="msg-crm-4")

    def _explode(*a, **k):
        raise RuntimeError("CRM write blew up")

    # CRM tracking moved into the shared services/outreach core — patch
    # CrmActivity there to force the tracking failure.
    with patch("app.routers.research_client.brevo.send_email", new=send_mock), \
         patch("app.services.outreach.CrmActivity", new=_explode):
        resp = await client.post("/research-client/send", json={
            "to_email": "boom@x.com",
            "subject": "s", "body": "b", "sender_name": "A",
        })

    # Send still reports success; CRM flags show the failure honestly.
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["message_id"] == "msg-crm-4"
    assert body["crm_activity_logged"] is False
    assert body["crm_lead_id"] is None
