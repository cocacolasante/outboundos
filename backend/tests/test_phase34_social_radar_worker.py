"""Tests for the Social Listening Radar Celery workers.

Covers:
  - ``run_social_search`` dedupes posts on (provider, post_url) re-runs
  - Only NEW posts enqueue ``qualify_social_post``
  - ``qualify_social_post`` upserts (re-qualification overwrites)
  - ``scheduled_runner`` selects active+non-manual+due searches only
  - ``_next_run_at`` math per frequency
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models import (
    SocialListeningOpportunity,
    SocialListeningPost,
    SocialListeningSearch,
    SocialOpportunityCategory,
    SocialOpportunityStatus,
    SocialSearchFrequency,
    SocialSearchStatus,
)
from app.services import _anthropic, social_listening_discovery
from app.services.social_listening_discovery import DiscoveredPost, DiscoveryResult


def _result(posts: list[DiscoveredPost]) -> DiscoveryResult:
    """Helper: wrap a posts list in a DiscoveryResult with the stats
    that the new ``discover_posts`` contract returns."""
    return DiscoveryResult(posts=posts, raw=len(posts))
from app.workers import social_listening as worker_mod


def _ant(body: str) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=body)])


@pytest.fixture(autouse=True)
def _reset_anthropic_client():
    _anthropic._client = None
    yield
    _anthropic._client = None


# ---- _next_run_at math ------------------------------------------------------

def test_next_run_at_manual_is_none():
    assert worker_mod._next_run_at(SocialSearchFrequency.MANUAL) is None


def test_next_run_at_intervals_are_correct():
    before = datetime.now(timezone.utc)
    out_daily = worker_mod._next_run_at(SocialSearchFrequency.DAILY)
    assert out_daily is not None
    assert (out_daily - before) >= timedelta(hours=23, minutes=59)
    assert (out_daily - before) <= timedelta(days=1, minutes=1)

    out_weekly = worker_mod._next_run_at(SocialSearchFrequency.WEEKLY)
    assert (out_weekly - before) >= timedelta(days=6, hours=23)
    assert (out_weekly - before) <= timedelta(days=7, minutes=1)


# ---- run_social_search orchestrator ----------------------------------------

async def test_run_social_search_writes_posts_and_dedupes_on_rerun(db_session):
    """First run inserts; second run with the same discovery output should
    NOT insert duplicates (the UNIQUE(provider, post_url) constraint
    backs the on_conflict_do_nothing upsert)."""
    s = SocialListeningSearch(
        name="X", topic="t", linkedin_web_search_enabled=True,
        expanded_queries=["frustrated with msp"],
        max_queries_per_run=5, max_posts_per_query=10, max_qualified_per_run=10,
    )
    db_session.add(s)
    await db_session.commit()
    sid = s.id

    discovered = [
        DiscoveredPost(
            post_url="https://www.linkedin.com/posts/a-activity-1",
            post_text="rant about msp",
            author_name="Jane",
        ),
        DiscoveredPost(
            post_url="https://www.linkedin.com/posts/b-activity-2",
            post_text="looking for new it",
            author_name="Bob",
        ),
    ]

    # Mock the discovery service AND the qualify-enqueue.
    with patch.object(
        social_listening_discovery, "discover_posts",
        new=AsyncMock(return_value=_result(discovered)),
    ), patch.object(
        worker_mod.qualify_social_posts_batch, "delay",
    ) as qualify_mock:
        result = await worker_mod._run_social_search_async(str(sid))

    assert result["status"] == "done"
    assert result["posts_new"] == 2
    assert qualify_mock.call_count == 1  # batched: 2 posts in 1 batch call

    # Re-run with identical discovery output — no new posts.
    qualify_mock.reset_mock()
    with patch.object(
        social_listening_discovery, "discover_posts",
        new=AsyncMock(return_value=_result(discovered)),
    ), patch.object(
        worker_mod.qualify_social_posts_batch, "delay",
    ) as qualify_mock_2:
        result_2 = await worker_mod._run_social_search_async(str(sid))

    assert result_2["status"] == "done"
    assert result_2["posts_new"] == 0
    qualify_mock_2.assert_not_called()


async def test_run_social_search_passes_existing_post_urls_as_exclude_on_rerun(db_session):
    """First run pulls 2 posts.  Second run should see those URLs passed
    in as exclude_urls to discovery so the LLM doesn't re-return them,
    AND the post-filter drops any that slip through.  Zero qualify tasks
    enqueued on the second run."""
    s = SocialListeningSearch(
        name="X", topic="t", linkedin_web_search_enabled=True,
        expanded_queries=["frustrated with msp"],
        max_queries_per_run=5, max_posts_per_query=10, max_qualified_per_run=10,
    )
    db_session.add(s)
    await db_session.commit()
    sid = s.id

    discovered = [
        DiscoveredPost(
            post_url="https://www.linkedin.com/posts/known-activity-1",
            post_text="msp rant", author_name="Jane",
        ),
        DiscoveredPost(
            post_url="https://www.linkedin.com/posts/known-activity-2",
            post_text="another", author_name="Bob",
        ),
    ]

    discover_mock = AsyncMock(return_value=_result(discovered))
    with patch.object(social_listening_discovery, "discover_posts", new=discover_mock), \
         patch.object(worker_mod.qualify_social_posts_batch, "delay"):
        first = await worker_mod._run_social_search_async(str(sid))
    assert first["posts_new"] == 2

    # First call: exclude_urls is empty (no posts yet).
    first_call_kwargs = discover_mock.await_args_list[0].kwargs
    assert first_call_kwargs.get("exclude_urls") == []

    # Second run — same discovery returns same posts.
    discover_mock.reset_mock()
    discover_mock.return_value = _result(discovered)
    with patch.object(social_listening_discovery, "discover_posts", new=discover_mock), \
         patch.object(worker_mod.qualify_social_posts_batch, "delay") as qualify_mock:
        second = await worker_mod._run_social_search_async(str(sid))

    # Discovery was called with the 2 already-known URLs in exclude_urls.
    second_call_kwargs = discover_mock.await_args_list[0].kwargs
    seen = set(second_call_kwargs["exclude_urls"])
    assert seen == {
        "https://www.linkedin.com/posts/known-activity-1",
        "https://www.linkedin.com/posts/known-activity-2",
    }

    # No new posts → no qualify tasks enqueued.
    assert second["posts_new"] == 0
    qualify_mock.assert_not_called()


async def test_run_social_search_passes_max_post_age_to_discovery(db_session):
    """The search's lookback window propagates to every discovery call."""
    s = SocialListeningSearch(
        name="X", topic="t", linkedin_web_search_enabled=True,
        expanded_queries=["q1", "q2"],
        max_queries_per_run=5, max_posts_per_query=10, max_qualified_per_run=10,
        max_post_age_days=7,
    )
    db_session.add(s)
    await db_session.commit()
    sid = s.id

    discover_mock = AsyncMock(return_value=DiscoveryResult())
    with patch.object(social_listening_discovery, "discover_posts", new=discover_mock), \
         patch.object(worker_mod.qualify_social_posts_batch, "delay"):
        await worker_mod._run_social_search_async(str(sid))

    # Every call passed max_post_age_days=7.
    assert discover_mock.await_count == 2
    for call in discover_mock.await_args_list:
        assert call.kwargs["max_post_age_days"] == 7


async def test_run_social_search_watchlist_pulls_via_unipile(db_session):
    """A non-empty linkedin_profile_watchlist drives a per-profile pull
    via Unipile (bypasses web-search indexing).  Each returned post is
    upserted with provider=linkedin and tagged ``discovered_via='watchlist:<url>'``."""
    from app.models import LinkedInAccount, LinkedInAccountStatus

    # Need an OK LinkedIn account in the DB for the watchlist path to fire.
    acc = LinkedInAccount(
        label="Test", linkedin_email="test@example.com",
        status=LinkedInAccountStatus.OK,
        unipile_account_id="abc-123",
    )
    db_session.add(acc)

    s = SocialListeningSearch(
        name="X", topic="t", linkedin_web_search_enabled=True,
        expanded_queries=["frustrated with our msp service"],
        sources=["linkedin"],
        linkedin_profile_watchlist=[
            "https://www.linkedin.com/in/jane-doe",
            "https://www.linkedin.com/in/bob-smith",
        ],
        max_queries_per_run=5, max_posts_per_query=10, max_qualified_per_run=20,
        max_post_age_days=30,
    )
    db_session.add(s)
    await db_session.commit()
    sid = s.id

    now = datetime.now(timezone.utc)

    # Mock Unipile's recent_posts: jane has 2 posts, bob has 0.
    async def _recent_posts(_account, profile, limit=5):
        if profile.public_id == "jane-doe":
            return [
                {
                    "urn": "urn:li:activity:111", "text": "tired of our msp service",
                    "created_at": now.isoformat(),
                    "author": {"name": "Jane Doe", "headline": "CFO at Acme"},
                },
                {
                    "urn": "urn:li:activity:112", "text": "looking at alternatives",
                    "created_at": now.isoformat(),
                    "author": {"name": "Jane Doe", "headline": "CFO at Acme"},
                },
            ]
        return []

    # Discovery: mock empty for the regular LinkedIn web-search path so
    # this test isolates the watchlist contribution.
    async def _no_web_results(**kwargs):
        return DiscoveryResult(raw=0)

    with patch.object(social_listening_discovery, "discover_posts", new=AsyncMock(side_effect=_no_web_results)), \
         patch("app.services.linkedin.UnipileLinkedInProvider.recent_posts",
               new=AsyncMock(side_effect=_recent_posts)), \
         patch.object(worker_mod.qualify_social_posts_batch, "delay") as qualify_mock:
        result = await worker_mod._run_social_search_async(str(sid))

    # 2 posts from jane upserted; bob contributed 0 — but both have stats.
    assert result["posts_new"] == 2
    assert qualify_mock.call_count == 1  # batched: 2 posts in 1 batch call

    posts = (await db_session.execute(
        select(SocialListeningPost).where(SocialListeningPost.search_id == sid)
    )).scalars().all()
    assert len(posts) == 2
    for p in posts:
        assert p.provider.value == "linkedin"
        assert p.discovered_via.startswith("watchlist:")

    # Watchlist stats persisted on the search row.
    await db_session.rollback()
    fresh = (await db_session.execute(
        select(SocialListeningSearch)
        .where(SocialListeningSearch.id == sid)
        .execution_options(populate_existing=True)
    )).scalar_one()
    wl_stats = {row["profile_url"]: row for row in fresh.last_run_stats["watchlist"]}
    assert wl_stats["https://www.linkedin.com/in/jane-doe"]["kept"] == 2
    assert wl_stats["https://www.linkedin.com/in/jane-doe"]["upserted_new"] == 2
    assert wl_stats["https://www.linkedin.com/in/bob-smith"]["kept"] == 0


async def test_run_social_search_watchlist_no_account_soft_fails(db_session):
    """Watchlist requires a connected Unipile LinkedIn account.  When
    none exists, every profile gets a stats row with ``no_account=True``
    and zero posts are pulled.  The rest of the run continues normally."""
    s = SocialListeningSearch(
        name="X", topic="t", linkedin_web_search_enabled=True,
        expanded_queries=["frustrated with our msp service"],
        sources=["linkedin"],
        linkedin_profile_watchlist=["https://www.linkedin.com/in/jane-doe"],
        max_queries_per_run=5, max_posts_per_query=10, max_qualified_per_run=20,
    )
    db_session.add(s)
    await db_session.commit()
    sid = s.id

    async def _no_web_results(**kwargs):
        return DiscoveryResult(raw=0)

    with patch.object(social_listening_discovery, "discover_posts", new=AsyncMock(side_effect=_no_web_results)), \
         patch.object(worker_mod.qualify_social_posts_batch, "delay"):
        result = await worker_mod._run_social_search_async(str(sid))

    assert result["posts_new"] == 0

    await db_session.rollback()
    fresh = (await db_session.execute(
        select(SocialListeningSearch)
        .where(SocialListeningSearch.id == sid)
        .execution_options(populate_existing=True)
    )).scalar_one()
    wl_stats = fresh.last_run_stats["watchlist"]
    assert len(wl_stats) == 1
    assert wl_stats[0]["no_account"] is True


async def test_run_social_search_fans_out_across_configured_sources(db_session):
    """A search with multiple sources calls discover_posts once per
    (source, query) pair and tags each post with its source provider."""
    s = SocialListeningSearch(
        name="X", topic="t", linkedin_web_search_enabled=True,
        expanded_queries=["frustrated with our msp service"],
        sources=["linkedin", "reddit", "twitter"],
        max_queries_per_run=5, max_posts_per_query=10, max_qualified_per_run=10,
    )
    db_session.add(s)
    await db_session.commit()
    sid = s.id

    now = datetime.now(timezone.utc)

    async def _per_source(**kwargs):
        src = kwargs["source"]
        if src == "linkedin":
            return DiscoveryResult(raw=0)
        if src == "reddit":
            return DiscoveryResult(posts=[DiscoveredPost(
                post_url="https://www.reddit.com/r/msp/comments/x/y/",
                post_text="msp disaster", post_date=now,
            )], raw=1)
        if src == "twitter":
            return DiscoveryResult(posts=[DiscoveredPost(
                post_url="https://twitter.com/jane/status/123",
                post_text="msp pain", post_date=now,
            )], raw=1)
        return DiscoveryResult()

    discover_mock = AsyncMock(side_effect=_per_source)
    with patch.object(social_listening_discovery, "discover_posts", new=discover_mock), \
         patch.object(worker_mod.qualify_social_posts_batch, "delay"):
        result = await worker_mod._run_social_search_async(str(sid))

    # Discover called once per (source, query) = 3 sources * 1 query.
    assert discover_mock.await_count == 3
    sources_called = {c.kwargs["source"] for c in discover_mock.await_args_list}
    assert sources_called == {"linkedin", "reddit", "twitter"}

    # 2 posts upserted (reddit + twitter); each tagged with its provider.
    assert result["posts_new"] == 2
    posts = (await db_session.execute(
        select(SocialListeningPost).where(SocialListeningPost.search_id == sid)
    )).scalars().all()
    providers = {p.provider.value for p in posts}
    assert providers == {"reddit", "twitter"}

    # Stats include the source column per (source, query) entry.
    await db_session.rollback()
    fresh = (await db_session.execute(
        select(SocialListeningSearch)
        .where(SocialListeningSearch.id == sid)
        .execution_options(populate_existing=True)
    )).scalar_one()
    stats = fresh.last_run_stats
    sources_in_stats = {q["source"] for q in stats["queries"]}
    assert sources_in_stats == {"linkedin", "reddit", "twitter"}
    assert stats["summary"]["total_sources"] == 3
    assert stats["summary"]["total_pairs"] == 3


async def test_run_social_search_surfaces_anthropic_errors_to_last_run_error(db_session):
    """When every discovery call fails with the same Anthropic error
    (e.g. out of credits), the worker aggregates them into a clear
    ``last_run_error`` so the user sees it in the UI instead of a silent
    ``done, 0 posts``."""
    s = SocialListeningSearch(
        name="X", topic="t", linkedin_web_search_enabled=True,
        expanded_queries=["q1", "q2", "q3"],
        sources=["linkedin", "reddit"],
        max_queries_per_run=5, max_posts_per_query=10, max_qualified_per_run=10,
    )
    db_session.add(s)
    await db_session.commit()
    sid = s.id

    async def _all_fail(**kwargs):
        return DiscoveryResult(
            error="Your credit balance is too low to access the Anthropic API.",
        )

    with patch.object(social_listening_discovery, "discover_posts", new=AsyncMock(side_effect=_all_fail)), \
         patch.object(worker_mod.qualify_social_posts_batch, "delay"):
        await worker_mod._run_social_search_async(str(sid))

    await db_session.rollback()
    fresh = (await db_session.execute(
        select(SocialListeningSearch)
        .where(SocialListeningSearch.id == sid)
        .execution_options(populate_existing=True)
    )).scalar_one()
    # 3 queries × 2 sources = 6 failed pairs.
    assert fresh.last_run_status == "done"
    assert fresh.last_run_error is not None
    assert "6 of 6" in fresh.last_run_error
    assert "credit balance is too low" in fresh.last_run_error


async def test_run_social_search_clears_last_run_error_on_clean_run(db_session):
    """A successful run (no discovery errors) clears any stale
    last_run_error from a previous failed run."""
    s = SocialListeningSearch(
        name="X", topic="t", linkedin_web_search_enabled=True,
        expanded_queries=["q1"],
        sources=["linkedin"],
        max_queries_per_run=5, max_posts_per_query=10, max_qualified_per_run=10,
        last_run_error="stale error from a previous run",
    )
    db_session.add(s)
    await db_session.commit()
    sid = s.id

    async def _clean(**kwargs):
        return DiscoveryResult(raw=0)  # no posts but also no error

    with patch.object(social_listening_discovery, "discover_posts", new=AsyncMock(side_effect=_clean)), \
         patch.object(worker_mod.qualify_social_posts_batch, "delay"):
        await worker_mod._run_social_search_async(str(sid))

    await db_session.rollback()
    fresh = (await db_session.execute(
        select(SocialListeningSearch)
        .where(SocialListeningSearch.id == sid)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert fresh.last_run_error is None


async def test_run_social_search_persists_per_query_stats(db_session):
    """After a run, ``last_run_stats`` on the search row holds per-query
    raw/kept/dropped counters AND an aggregated summary so the UI can
    show the user WHICH phrases produced posts and WHICH were duds."""
    s = SocialListeningSearch(
        name="X", topic="t", linkedin_web_search_enabled=True,
        expanded_queries=["frustrated with our msp", "looking for new it"],
        max_queries_per_run=5, max_posts_per_query=10, max_qualified_per_run=10,
        max_post_age_days=30,
    )
    db_session.add(s)
    await db_session.commit()
    sid = s.id

    productive_posts = [
        DiscoveredPost(
            post_url="https://www.linkedin.com/posts/a-activity-1",
            post_text="rant", post_date=datetime.now(timezone.utc),
        ),
        DiscoveredPost(
            post_url="https://www.linkedin.com/posts/b-activity-2",
            post_text="more", post_date=datetime.now(timezone.utc),
        ),
    ]

    async def _discover_per_query(**kwargs):
        # First query surfaces 2 fresh + 1 stale internally; second
        # query produces nothing.
        if kwargs["query"] == "frustrated with our msp":
            return DiscoveryResult(
                posts=productive_posts, raw=3, dropped_stale=1,
            )
        return DiscoveryResult(raw=0)

    with patch.object(social_listening_discovery, "discover_posts", new=AsyncMock(side_effect=_discover_per_query)), \
         patch.object(worker_mod.qualify_social_posts_batch, "delay"):
        await worker_mod._run_social_search_async(str(sid))

    # Persisted on the search row.
    await db_session.rollback()
    fresh = (await db_session.execute(
        select(SocialListeningSearch)
        .where(SocialListeningSearch.id == sid)
        .execution_options(populate_existing=True)
    )).scalar_one()
    stats = fresh.last_run_stats
    assert isinstance(stats, dict)
    by_query = {q["query"]: q for q in stats["queries"]}
    assert by_query["frustrated with our msp"]["kept"] == 2
    assert by_query["frustrated with our msp"]["dropped_stale"] == 1
    assert by_query["frustrated with our msp"]["upserted_new"] == 2
    assert by_query["looking for new it"]["kept"] == 0
    assert stats["summary"]["total_kept"] == 2
    assert stats["summary"]["total_dropped_stale"] == 1


async def test_run_social_search_skips_linkedin_web_search_when_toggle_off(db_session):
    """Default behavior: ``linkedin_web_search_enabled=False`` means
    the worker skips all (linkedin, query) pairs entirely.  No Anthropic
    calls fire for LinkedIn; the watchlist still runs (separately)."""
    s = SocialListeningSearch(
        name="X", topic="t",
        expanded_queries=["q1", "q2", "q3"],
        sources=["linkedin", "reddit"],
        linkedin_web_search_enabled=False,  # the default
        max_queries_per_run=5, max_posts_per_query=10, max_qualified_per_run=10,
    )
    db_session.add(s)
    await db_session.commit()
    sid = s.id

    async def _by_source(**kwargs):
        return DiscoveryResult(raw=0)

    discover_mock = AsyncMock(side_effect=_by_source)
    with patch.object(social_listening_discovery, "discover_posts", new=discover_mock), \
         patch.object(worker_mod.qualify_social_posts_batch, "delay"):
        await worker_mod._run_social_search_async(str(sid))

    # 2 sources × 3 queries = 6 pairs total — but linkedin is skipped
    # (toggle off) so we only call discovery for the 3 reddit pairs.
    assert discover_mock.await_count == 3
    sources_called = {c.kwargs["source"] for c in discover_mock.await_args_list}
    assert sources_called == {"reddit"}

    await db_session.rollback()
    fresh = (await db_session.execute(
        select(SocialListeningSearch)
        .where(SocialListeningSearch.id == sid)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert fresh.last_run_stats["summary"]["linkedin_web_search_skipped_pairs"] == 3


async def test_run_social_search_aborts_mid_run_at_cost_cap(db_session):
    """When the running cost crosses ``max_run_cost_usd``, the worker
    aborts the remaining discovery chunks and surfaces a clear error.
    Status flips to ``cost_capped`` (distinct from ``done``)."""
    s = SocialListeningSearch(
        name="X", topic="t",
        # 8 queries so we get multiple chunks (CHUNK=5 in the worker).
        expanded_queries=[f"q{i}" for i in range(8)],
        sources=["reddit"],
        linkedin_web_search_enabled=False,
        max_run_cost_usd=0.10,
        max_queries_per_run=8, max_posts_per_query=10, max_qualified_per_run=10,
    )
    db_session.add(s)
    await db_session.commit()
    sid = s.id

    async def _expensive(**kwargs):
        return DiscoveryResult(raw=0, cost_usd=0.05)

    discover_mock = AsyncMock(side_effect=_expensive)
    with patch.object(social_listening_discovery, "discover_posts", new=discover_mock), \
         patch.object(worker_mod.qualify_social_posts_batch, "delay"):
        await worker_mod._run_social_search_async(str(sid))

    # First chunk of 5 ran (5 × $0.05 = $0.25 > cap $0.10); next chunk aborted.
    assert discover_mock.await_count == 5

    await db_session.rollback()
    fresh = (await db_session.execute(
        select(SocialListeningSearch)
        .where(SocialListeningSearch.id == sid)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert fresh.last_run_status == "cost_capped"
    assert "Cost cap reached" in (fresh.last_run_error or "")
    assert fresh.last_run_stats["summary"]["cost_capped"] is True
    assert fresh.last_run_stats["summary"]["cost_usd"] >= 0.10


async def test_run_social_search_batches_qualify_dispatches(db_session):
    """20 new posts should dispatch as 2 batches of 10 (QUALIFY_BATCH_SIZE
    = 10), not 20 separate task delays.  Cuts qualify cost ~70%."""
    s = SocialListeningSearch(
        name="X", topic="t",
        expanded_queries=["q1"],
        sources=["reddit"],
        linkedin_web_search_enabled=False,
        max_queries_per_run=5, max_posts_per_query=50, max_qualified_per_run=50,
    )
    db_session.add(s)
    await db_session.commit()
    sid = s.id

    now = datetime.now(timezone.utc)
    discovered = [
        DiscoveredPost(
            post_url=f"https://www.reddit.com/r/sysadmin/comments/p{i}/x/",
            post_text=f"post {i}", post_date=now,
        )
        for i in range(20)
    ]

    with patch.object(
        social_listening_discovery, "discover_posts",
        new=AsyncMock(return_value=_result(discovered)),
    ), patch.object(
        worker_mod.qualify_social_posts_batch, "delay",
    ) as qualify_mock:
        await worker_mod._run_social_search_async(str(sid))

    # 20 posts / 10 per batch = 2 batches dispatched.
    assert qualify_mock.call_count == 2
    total_ids = sum(len(call.args[0]) for call in qualify_mock.call_args_list)
    assert total_ids == 20


async def test_run_social_search_skips_when_search_not_active(db_session):
    s = SocialListeningSearch(
        name="X", topic="t", linkedin_web_search_enabled=True,
        expanded_queries=["q"],
        status=SocialSearchStatus.PAUSED,
    )
    db_session.add(s)
    await db_session.commit()

    out = await worker_mod._run_social_search_async(str(s.id))
    assert out == {"status": "skipped", "reason": "search_not_active"}


async def test_run_social_search_returns_not_found_for_missing_search():
    out = await worker_mod._run_social_search_async(str(uuid.uuid4()))
    assert out == {"status": "not_found"}


async def test_run_social_search_sets_next_run_at_for_scheduled_frequency(db_session):
    s = SocialListeningSearch(
        name="X", topic="t", linkedin_web_search_enabled=True,
        expanded_queries=["q"],
        frequency=SocialSearchFrequency.DAILY,
        max_queries_per_run=5, max_posts_per_query=10, max_qualified_per_run=10,
    )
    db_session.add(s)
    await db_session.commit()
    sid = s.id

    with patch.object(
        social_listening_discovery, "discover_posts",
        new=AsyncMock(return_value=DiscoveryResult()),
    ), patch.object(
        worker_mod.qualify_social_posts_batch, "delay",
    ):
        await worker_mod._run_social_search_async(str(sid))

    db_session.expire_all()
    fresh = await db_session.get(SocialListeningSearch, sid)
    assert fresh.last_run_status == "done"
    assert fresh.next_run_at is not None
    assert (fresh.next_run_at - datetime.now(timezone.utc)) > timedelta(hours=23)


async def test_run_social_search_caps_qualified_at_max_qualified_per_run(db_session):
    """Even if we discover 20 NEW posts, only ``max_qualified_per_run`` of
    them should be enqueued."""
    s = SocialListeningSearch(
        name="X", topic="t", linkedin_web_search_enabled=True,
        expanded_queries=["q"],
        max_queries_per_run=5, max_posts_per_query=50, max_qualified_per_run=3,
    )
    db_session.add(s)
    await db_session.commit()
    sid = s.id

    discovered = [
        DiscoveredPost(
            post_url=f"https://www.linkedin.com/posts/p-activity-{i}",
            post_text=f"p{i}",
        )
        for i in range(20)
    ]

    with patch.object(
        social_listening_discovery, "discover_posts",
        new=AsyncMock(return_value=_result(discovered)),
    ), patch.object(
        worker_mod.qualify_social_posts_batch, "delay",
    ) as qualify_mock:
        result = await worker_mod._run_social_search_async(str(sid))

    # All 20 posts are saved (we want the full dedup history), but only
    # 3 qualify tasks were enqueued.
    assert result["posts_new"] == 3
    assert qualify_mock.call_count == 1  # batched: 3 posts in 1 batch call


# ---- qualify_social_post upsert --------------------------------------------

async def test_qualify_social_post_upserts_overwriting_previous(db_session):
    s = SocialListeningSearch(name="X", topic="t", tone="helpful", sender_name="Anthony", linkedin_web_search_enabled=True)
    db_session.add(s)
    await db_session.flush()
    p = SocialListeningPost(
        search_id=s.id,
        post_url="https://www.linkedin.com/posts/x-activity-1",
        post_text="rant",
    )
    db_session.add(p)
    await db_session.commit()

    first_run = '{"score": 4, "category": "msp", "buying_signal": false, ' \
                '"pain_summary": "x", "qualification_reason": "y", ' \
                '"suggested_comment": "v1", "suggested_connection_request": "y", ' \
                '"suggested_follow_up": "z", "recommended_action": "ignore"}'
    second_run = '{"score": 9, "category": "cybersecurity", "buying_signal": true, ' \
                 '"pain_summary": "a", "qualification_reason": "b", ' \
                 '"suggested_comment": "v2", "suggested_connection_request": "c", ' \
                 '"suggested_follow_up": "d", "recommended_action": "comment"}'

    mock_1 = AsyncMock(return_value=_ant(first_run))
    from app.services import social_listening_qualifier
    with patch.object(social_listening_qualifier, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=mock_1))):
        await worker_mod._qualify_social_post_async(str(p.id), str(s.id))

    # User saves the opportunity in the meantime.  Release any cached
    # snapshot from the test session so the worker's write is visible.
    await db_session.rollback()
    opp = (await db_session.execute(
        select(SocialListeningOpportunity).where(
            SocialListeningOpportunity.post_id == p.id
        )
    )).scalar_one()
    opp.status = SocialOpportunityStatus.SAVED
    opp.notes = "user notes"
    await db_session.commit()

    # Re-qualify (e.g. user manually re-ran the search).
    mock_2 = AsyncMock(return_value=_ant(second_run))
    with patch.object(social_listening_qualifier, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=mock_2))):
        await worker_mod._qualify_social_post_async(str(p.id), str(s.id))

    # AI-set fields overwritten; user-set fields preserved.  The worker
    # writes through a separate engine, so we force the test session to
    # re-read instead of returning its cached opp instance.
    await db_session.rollback()
    opp = (await db_session.execute(
        select(SocialListeningOpportunity)
        .where(SocialListeningOpportunity.post_id == p.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert opp.score == 9
    assert opp.suggested_comment == "v2"
    assert opp.category == SocialOpportunityCategory.CYBERSECURITY
    # User edits survive re-qualification.
    assert opp.status == SocialOpportunityStatus.SAVED
    assert opp.notes == "user notes"


async def test_qualify_social_post_skips_when_qualifier_returns_none(db_session):
    s = SocialListeningSearch(name="X", topic="t", linkedin_web_search_enabled=True)
    db_session.add(s)
    await db_session.flush()
    p = SocialListeningPost(
        search_id=s.id,
        post_url="https://www.linkedin.com/posts/x-1",
        post_text="rant",
    )
    db_session.add(p)
    await db_session.commit()

    failing = AsyncMock(side_effect=RuntimeError("nope"))
    from app.services import social_listening_qualifier
    with patch.object(social_listening_qualifier, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=failing))):
        result = await worker_mod._qualify_social_post_async(str(p.id), str(s.id))
    assert result["status"] == "skipped"

    # No opportunity row created.
    count = (await db_session.execute(
        select(SocialListeningOpportunity).where(
            SocialListeningOpportunity.post_id == p.id
        )
    )).scalars().all()
    assert count == []


# ---- Scheduled runner (beat dispatcher) ------------------------------------

async def test_scheduled_runner_picks_up_active_due_non_manual_searches(db_session):
    now = datetime.now(timezone.utc)
    past = now - timedelta(minutes=5)
    future = now + timedelta(hours=1)

    db_session.add_all([
        # active + due + daily — SHOULD enqueue
        SocialListeningSearch(
            name="due-daily", linkedin_web_search_enabled=True, topic="t",
            status=SocialSearchStatus.ACTIVE,
            frequency=SocialSearchFrequency.DAILY,
            next_run_at=past,
        ),
        # active + due + manual — should NOT enqueue
        SocialListeningSearch(
            name="manual", linkedin_web_search_enabled=True, topic="t",
            status=SocialSearchStatus.ACTIVE,
            frequency=SocialSearchFrequency.MANUAL,
            next_run_at=past,
        ),
        # active + due + already running — should NOT enqueue
        SocialListeningSearch(
            name="running", linkedin_web_search_enabled=True, topic="t",
            status=SocialSearchStatus.ACTIVE,
            frequency=SocialSearchFrequency.DAILY,
            next_run_at=past,
            last_run_status="running",
        ),
        # paused — should NOT enqueue
        SocialListeningSearch(
            name="paused", linkedin_web_search_enabled=True, topic="t",
            status=SocialSearchStatus.PAUSED,
            frequency=SocialSearchFrequency.DAILY,
            next_run_at=past,
        ),
        # not yet due — should NOT enqueue
        SocialListeningSearch(
            name="future", linkedin_web_search_enabled=True, topic="t",
            status=SocialSearchStatus.ACTIVE,
            frequency=SocialSearchFrequency.DAILY,
            next_run_at=future,
        ),
        # no next_run_at — should NOT enqueue
        SocialListeningSearch(
            name="null-next", linkedin_web_search_enabled=True, topic="t",
            status=SocialSearchStatus.ACTIVE,
            frequency=SocialSearchFrequency.DAILY,
            next_run_at=None,
        ),
    ])
    await db_session.commit()

    with patch.object(worker_mod.run_social_search, "delay") as mock:
        result = await worker_mod._scheduled_runner_async()
    assert result == {"enqueued": 1}
    mock.assert_called_once()


# ---- Reddit → LinkedIn cross-link extraction --------------------------------

async def test_run_social_search_crosslink_extracts_linkedin_urls_from_reddit_bodies(db_session):
    """A Reddit post body containing 2 LinkedIn URLs (one /posts/ and one
    /feed/update/) → 2 new ``provider=linkedin`` posts upserted, each with
    a synthesised opportunity (score=5, action=research_further), and
    ``qualify_social_posts_batch`` is NOT called for the crosslink posts
    (it IS called once for the actual Reddit post that surfaced them)."""
    s = SocialListeningSearch(
        name="X", topic="t",
        expanded_queries=["msp horror stories"],
        sources=["reddit"],
        linkedin_web_search_enabled=False,
        linkedin_crosslink_enabled=True,
        max_queries_per_run=5, max_posts_per_query=10, max_qualified_per_run=10,
    )
    db_session.add(s)
    await db_session.commit()
    sid = s.id

    now = datetime.now(timezone.utc)
    reddit_body = (
        "Lots of folks venting in this thread.  Saw this one earlier: "
        "https://www.linkedin.com/posts/jane-doe-abc123_msp-rant-activity-7284763839472640000 "
        "and the original is at "
        "https://www.linkedin.com/feed/update/urn:li:activity:99999/"
    )
    discovered = [
        DiscoveredPost(
            post_url="https://www.reddit.com/r/msp/comments/abc/thread/",
            post_text=reddit_body, post_date=now,
            author_name="u/red", author_headline="r/msp",
        ),
    ]

    with patch.object(
        social_listening_discovery, "discover_posts",
        new=AsyncMock(return_value=_result(discovered)),
    ), patch.object(
        worker_mod.qualify_social_posts_batch, "delay",
    ) as qualify_mock:
        await worker_mod._run_social_search_async(str(sid))

    # The Reddit post still gets a qualify-batch dispatch (1 post, 1 call).
    # The 2 LinkedIn cross-link posts do NOT — they were synthesised with
    # opportunities inline.
    assert qualify_mock.call_count == 1
    assert len(qualify_mock.call_args_list[0].args[0]) == 1

    # 2 LinkedIn posts upserted with the right provider + discovered_via.
    posts = (await db_session.execute(
        select(SocialListeningPost)
        .where(SocialListeningPost.search_id == sid)
        .where(SocialListeningPost.provider == "linkedin")
    )).scalars().all()
    assert len(posts) == 2
    assert all(p.discovered_via.startswith("reddit-crosslink:") for p in posts)

    # Both LinkedIn posts got synthesised opportunities (score=5, research_further).
    opps = (await db_session.execute(
        select(SocialListeningOpportunity)
        .join(SocialListeningPost,
              SocialListeningPost.id == SocialListeningOpportunity.post_id)
        .where(SocialListeningPost.provider == "linkedin")
    )).scalars().all()
    assert len(opps) == 2
    for opp in opps:
        assert opp.score == 5
        assert opp.recommended_action.value == "research_further"
        assert opp.buying_signal is False
        assert opp.category == SocialOpportunityCategory.GENERAL_ADVISORY

    # crosslink_stats recorded on last_run_stats.
    await db_session.rollback()
    fresh = (await db_session.execute(
        select(SocialListeningSearch).where(SocialListeningSearch.id == sid)
        .execution_options(populate_existing=True)
    )).scalar_one()
    cl = fresh.last_run_stats.get("crosslink") or {}
    assert cl.get("reddit_posts_scanned") == 1
    assert cl.get("linkedin_urls_found") == 2
    assert cl.get("linkedin_posts_new") == 2
    assert cl.get("linkedin_posts_existing") == 0


async def test_run_social_search_crosslink_disabled_skips_extraction(db_session):
    """When ``linkedin_crosslink_enabled=False``, no LinkedIn posts are
    synthesised from Reddit bodies and crosslink_stats stays at zeros."""
    s = SocialListeningSearch(
        name="X", topic="t",
        expanded_queries=["q"],
        sources=["reddit"],
        linkedin_web_search_enabled=False,
        linkedin_crosslink_enabled=False,
        max_queries_per_run=5, max_posts_per_query=10, max_qualified_per_run=10,
    )
    db_session.add(s)
    await db_session.commit()
    sid = s.id

    now = datetime.now(timezone.utc)
    discovered = [
        DiscoveredPost(
            post_url="https://www.reddit.com/r/x/comments/y/z/",
            post_text="See https://www.linkedin.com/posts/jane_x_activity-12345 for context.",
            post_date=now,
        ),
    ]

    with patch.object(
        social_listening_discovery, "discover_posts",
        new=AsyncMock(return_value=_result(discovered)),
    ), patch.object(
        worker_mod.qualify_social_posts_batch, "delay",
    ):
        await worker_mod._run_social_search_async(str(sid))

    # No LinkedIn posts upserted.
    li_posts = (await db_session.execute(
        select(SocialListeningPost)
        .where(SocialListeningPost.search_id == sid)
        .where(SocialListeningPost.provider == "linkedin")
    )).scalars().all()
    assert li_posts == []

    # Stats present (always written) but zeros (loop didn't run).
    await db_session.rollback()
    fresh = (await db_session.execute(
        select(SocialListeningSearch).where(SocialListeningSearch.id == sid)
        .execution_options(populate_existing=True)
    )).scalar_one()
    cl = fresh.last_run_stats.get("crosslink") or {}
    assert cl.get("reddit_posts_scanned", 0) == 0
    assert cl.get("linkedin_urls_found", 0) == 0


async def test_run_social_search_crosslink_dedupes_same_url_across_reddit_posts(db_session):
    """Two Reddit posts both mentioning the SAME LinkedIn URL must collapse
    to one ``provider=linkedin`` row via the existing UNIQUE constraint."""
    s = SocialListeningSearch(
        name="X", topic="t",
        expanded_queries=["q"],
        sources=["reddit"],
        linkedin_web_search_enabled=False,
        linkedin_crosslink_enabled=True,
        max_queries_per_run=5, max_posts_per_query=10, max_qualified_per_run=10,
    )
    db_session.add(s)
    await db_session.commit()
    sid = s.id

    now = datetime.now(timezone.utc)
    li_url = "https://www.linkedin.com/posts/jane_x_activity-12345"
    discovered = [
        DiscoveredPost(
            post_url="https://www.reddit.com/r/a/comments/1/x/",
            post_text=f"First reference: {li_url}",
            post_date=now,
        ),
        DiscoveredPost(
            post_url="https://www.reddit.com/r/b/comments/2/y/",
            # Same LinkedIn URL but with tracking params — canonicaliser
            # should collapse to the same row.
            post_text=f"Look: {li_url}?utm_source=share",
            post_date=now,
        ),
    ]

    with patch.object(
        social_listening_discovery, "discover_posts",
        new=AsyncMock(return_value=_result(discovered)),
    ), patch.object(
        worker_mod.qualify_social_posts_batch, "delay",
    ):
        await worker_mod._run_social_search_async(str(sid))

    li_posts = (await db_session.execute(
        select(SocialListeningPost)
        .where(SocialListeningPost.search_id == sid)
        .where(SocialListeningPost.provider == "linkedin")
    )).scalars().all()
    assert len(li_posts) == 1

    # crosslink_stats: 2 reddit posts scanned, 2 URLs found, 1 NEW + 1 existing.
    await db_session.rollback()
    fresh = (await db_session.execute(
        select(SocialListeningSearch).where(SocialListeningSearch.id == sid)
        .execution_options(populate_existing=True)
    )).scalar_one()
    cl = fresh.last_run_stats.get("crosslink") or {}
    assert cl.get("reddit_posts_scanned") == 2
    assert cl.get("linkedin_urls_found") == 2
    assert cl.get("linkedin_posts_new") == 1
    assert cl.get("linkedin_posts_existing") == 1


# ---- Watchlist scalability: provider-id cache + parallel fan-out ------------

async def test_run_watchlist_cache_hit_pre_fills_profile_urn(db_session):
    """When ``linkedin_profile_cache`` already has the slug → URN
    mapping, the worker MUST pre-populate ``ProfileRef.urn`` before
    calling ``recent_posts`` so Unipile's internal ``_resolve_provider_id``
    short-circuits (skips the slug→URN ``GET /users/{slug}`` call).
    This is the 50% per-profile cost cut on subsequent runs."""
    from app.models import LinkedInAccount, LinkedInAccountStatus, LinkedInProfileCache

    db_session.add(LinkedInAccount(
        label="T", linkedin_email="t@example.com",
        status=LinkedInAccountStatus.OK, unipile_account_id="acc-1",
    ))
    db_session.add(LinkedInProfileCache(
        slug="jane-doe", provider_id="ACoAAcached123",
    ))
    s = SocialListeningSearch(
        name="X", topic="t",
        expanded_queries=["q"],
        sources=["reddit"],  # no web-search; isolate watchlist
        linkedin_profile_watchlist=["https://www.linkedin.com/in/jane-doe"],
        linkedin_web_search_enabled=False,
        max_queries_per_run=5, max_posts_per_query=10, max_qualified_per_run=10,
    )
    db_session.add(s)
    await db_session.commit()
    sid = s.id

    captured_refs: list = []

    async def _spy(_account, profile, limit=5):
        # Capture the ProfileRef so we can assert on its urn state.
        captured_refs.append({"public_id": profile.public_id, "urn": profile.urn})
        return []

    async def _no_web(**kwargs):
        return DiscoveryResult(raw=0)

    with patch.object(social_listening_discovery, "discover_posts", new=AsyncMock(side_effect=_no_web)), \
         patch("app.services.linkedin.UnipileLinkedInProvider.recent_posts",
               new=AsyncMock(side_effect=_spy)), \
         patch.object(worker_mod.qualify_social_posts_batch, "delay"):
        await worker_mod._run_social_search_async(str(sid))

    assert len(captured_refs) == 1
    assert captured_refs[0]["public_id"] == "jane-doe"
    # Cache hit pre-populated the URN — Unipile's internal resolver will
    # see profile.urn != None and skip the network call.
    assert captured_refs[0]["urn"] == "ACoAAcached123"

    # Stats row marks the cache hit.
    await db_session.rollback()
    fresh = (await db_session.execute(
        select(SocialListeningSearch).where(SocialListeningSearch.id == sid)
        .execution_options(populate_existing=True)
    )).scalar_one()
    row = next(r for r in fresh.last_run_stats["watchlist"]
               if r["profile_url"].endswith("jane-doe"))
    assert row.get("cache_hit") is True


async def test_run_watchlist_cache_miss_persists_resolved_urn(db_session):
    """On a cache miss, the URN that Unipile resolves during
    ``recent_posts`` (stamped onto ``ProfileRef.urn`` by the provider)
    must be upserted into ``linkedin_profile_cache`` so the next run
    is a cache hit."""
    from app.models import LinkedInAccount, LinkedInAccountStatus, LinkedInProfileCache

    db_session.add(LinkedInAccount(
        label="T", linkedin_email="t@example.com",
        status=LinkedInAccountStatus.OK, unipile_account_id="acc-1",
    ))
    s = SocialListeningSearch(
        name="X", topic="t",
        expanded_queries=["q"],
        sources=["reddit"],
        linkedin_profile_watchlist=["https://www.linkedin.com/in/bob-smith"],
        linkedin_web_search_enabled=False,
        max_queries_per_run=5, max_posts_per_query=10, max_qualified_per_run=10,
    )
    db_session.add(s)
    await db_session.commit()
    sid = s.id

    async def _resolves_inline(_account, profile, limit=5):
        # The real Unipile provider stamps profile.urn during this call.
        # Simulate that side effect so the worker can pick up the URN.
        profile.urn = "ACoAAresolved456"
        return []

    async def _no_web(**kwargs):
        return DiscoveryResult(raw=0)

    with patch.object(social_listening_discovery, "discover_posts", new=AsyncMock(side_effect=_no_web)), \
         patch("app.services.linkedin.UnipileLinkedInProvider.recent_posts",
               new=AsyncMock(side_effect=_resolves_inline)), \
         patch.object(worker_mod.qualify_social_posts_batch, "delay"):
        await worker_mod._run_social_search_async(str(sid))

    # The resolved URN is now persisted for next time.
    cached = await db_session.scalar(
        select(LinkedInProfileCache).where(LinkedInProfileCache.slug == "bob-smith")
    )
    assert cached is not None
    assert cached.provider_id == "ACoAAresolved456"

    # And the stats row marks this as a miss.
    await db_session.rollback()
    fresh = (await db_session.execute(
        select(SocialListeningSearch).where(SocialListeningSearch.id == sid)
        .execution_options(populate_existing=True)
    )).scalar_one()
    row = next(r for r in fresh.last_run_stats["watchlist"]
               if r["profile_url"].endswith("bob-smith"))
    assert row.get("cache_hit") is False


async def test_run_watchlist_processes_profiles_in_parallel(db_session):
    """20 profiles must all be fetched and produce stats rows.  With
    sequential code this took ~40s on a 2s-per-call mock; with the
    semaphore=10 fan-out we want it to complete in under a few wall
    seconds.  Verified two ways: (1) every profile has a stats row,
    (2) the recent_posts mock was called the right number of times."""
    from app.models import LinkedInAccount, LinkedInAccountStatus

    db_session.add(LinkedInAccount(
        label="T", linkedin_email="t@example.com",
        status=LinkedInAccountStatus.OK, unipile_account_id="acc-1",
    ))
    urls = [
        f"https://www.linkedin.com/in/person-{i:02d}"
        for i in range(20)
    ]
    s = SocialListeningSearch(
        name="X", topic="t",
        expanded_queries=["q"],
        sources=["reddit"],
        linkedin_profile_watchlist=urls,
        linkedin_web_search_enabled=False,
        max_queries_per_run=5, max_posts_per_query=10, max_qualified_per_run=10,
    )
    db_session.add(s)
    await db_session.commit()
    sid = s.id

    call_count = 0

    async def _stub(_account, profile, limit=5):
        nonlocal call_count
        call_count += 1
        # Stamp a fresh URN so the cache write fires.
        profile.urn = f"urn-{profile.public_id}"
        return []

    async def _no_web(**kwargs):
        return DiscoveryResult(raw=0)

    with patch.object(social_listening_discovery, "discover_posts", new=AsyncMock(side_effect=_no_web)), \
         patch("app.services.linkedin.UnipileLinkedInProvider.recent_posts",
               new=AsyncMock(side_effect=_stub)), \
         patch.object(worker_mod.qualify_social_posts_batch, "delay"):
        await worker_mod._run_social_search_async(str(sid))

    assert call_count == 20

    await db_session.rollback()
    fresh = (await db_session.execute(
        select(SocialListeningSearch).where(SocialListeningSearch.id == sid)
        .execution_options(populate_existing=True)
    )).scalar_one()
    wl_stats = fresh.last_run_stats["watchlist"]
    # Every profile in the input has a stats row in the output.
    assert len(wl_stats) == 20
    urls_in_stats = {r["profile_url"] for r in wl_stats}
    assert urls_in_stats == set(urls)


async def test_run_watchlist_no_account_still_returns_one_row_per_profile(db_session):
    """When no LinkedIn account is connected, the parallelisation must
    not change the soft-fail contract: every profile gets a stats row
    flagged ``no_account=True`` and zero Unipile calls happen."""
    s = SocialListeningSearch(
        name="X", topic="t",
        expanded_queries=["q"],
        sources=["reddit"],
        linkedin_profile_watchlist=[
            "https://www.linkedin.com/in/a",
            "https://www.linkedin.com/in/b",
            "https://www.linkedin.com/in/c",
        ],
        linkedin_web_search_enabled=False,
        max_queries_per_run=5, max_posts_per_query=10, max_qualified_per_run=10,
    )
    db_session.add(s)
    await db_session.commit()
    sid = s.id

    async def _no_web(**kwargs):
        return DiscoveryResult(raw=0)
    fetch_mock = AsyncMock()
    with patch.object(social_listening_discovery, "discover_posts", new=AsyncMock(side_effect=_no_web)), \
         patch("app.services.linkedin.UnipileLinkedInProvider.recent_posts", new=fetch_mock), \
         patch.object(worker_mod.qualify_social_posts_batch, "delay"):
        await worker_mod._run_social_search_async(str(sid))

    fetch_mock.assert_not_called()
    await db_session.rollback()
    fresh = (await db_session.execute(
        select(SocialListeningSearch).where(SocialListeningSearch.id == sid)
        .execution_options(populate_existing=True)
    )).scalar_one()
    wl_stats = fresh.last_run_stats["watchlist"]
    assert len(wl_stats) == 3
    for row in wl_stats:
        assert row["no_account"] is True
        assert row["upserted_new"] == 0

