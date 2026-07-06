"""Celery workers for Social Listening Radar.

Four tasks:
  expand_social_topic     — write expanded_queries onto a search row
  run_social_search       — orchestrator: discover posts, enqueue qualify
  qualify_social_post     — score one post + draft suggested copy
  scheduled_runner        — beat dispatcher: fires due searches every 60s

Discovery is via Anthropic web search (Unipile has no LinkedIn post
search).  All LinkedIn write actions stay manual — these workers only
populate suggestions.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.models import (
    LinkedInAccount,
    LinkedInAccountStatus,
    LinkedInProfileCache,
    SocialListeningOpportunity,
    SocialListeningPost,
    SocialListeningSearch,
    SocialOpportunityStatus,
    SocialPostProvider,
    SocialSearchFrequency,
    SocialSearchStatus,
)
from app.services import (
    social_listening_discovery,
    social_listening_qualifier,
    social_listening_topic_expander,
)
from app.workers.celery_app import celery_app
from app.tenancy.context import for_all_tenants, with_record_tenant

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


# Posts per batched-qualify Anthropic call.  10 is a good balance: low
# enough that prompt + output stay well under model limits, high enough
# to give a meaningful cost cut.  Above ~15 the model starts to drop
# fidelity on later posts in the batch.
QUALIFY_BATCH_SIZE = 10


_FREQUENCY_DELTAS: dict[SocialSearchFrequency, timedelta] = {
    SocialSearchFrequency.EVERY_6H: timedelta(hours=6),
    SocialSearchFrequency.EVERY_12H: timedelta(hours=12),
    SocialSearchFrequency.DAILY: timedelta(days=1),
    SocialSearchFrequency.WEEKLY: timedelta(days=7),
}


def _next_run_at(frequency: SocialSearchFrequency) -> datetime | None:
    """Manual runs don't auto-schedule; everything else gets stamped with
    ``now + interval`` so the beat dispatcher picks them up next cycle."""
    delta = _FREQUENCY_DELTAS.get(frequency)
    if delta is None:
        return None
    return _now() + delta


# ---------------------------------------------------------------------------
# Topic expansion
# ---------------------------------------------------------------------------

async def _expand_social_topic_async(search_id: str) -> dict[str, Any]:
    sid = uuid.UUID(str(search_id))
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            search = await session.get(SocialListeningSearch, sid)
            if search is None:
                return {"status": "not_found"}
            queries = await social_listening_topic_expander.expand_topic(
                topic=search.topic,
                niche=search.niche,
                geography=search.geography,
                include_keywords=list(search.include_keywords or []),
                exclude_keywords=list(search.exclude_keywords or []),
                max_queries=search.max_queries_per_run,
            )
            search.expanded_queries = queries
            await session.commit()
        return {"status": "done", "count": len(queries)}
    finally:
        await engine.dispose()


@celery_app.task(bind=True, name="social_listening.expand_topic", max_retries=2)
def expand_social_topic(self, search_id: str) -> dict[str, Any]:  # noqa: D401
    try:
        return asyncio.run(with_record_tenant(SocialListeningSearch, search_id, _expand_social_topic_async, search_id))
    except Exception as exc:  # noqa: BLE001
        logger.exception("expand_social_topic failed for %s", search_id)
        try:
            raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))
        except self.MaxRetriesExceededError:
            return {"status": "failed", "error": str(exc)}


# ---------------------------------------------------------------------------
# Search run orchestrator
# ---------------------------------------------------------------------------

_WATCHLIST_CONCURRENCY = 10
"""Max in-flight Unipile reads for the watchlist phase.  Unipile's read
surface (``GET /users/...``, ``/users/{id}/posts``) is generous — it
doesn't go through the LinkedIn rate gate that throttles writes — so
10 parallel fetches is comfortable.  At 200 profiles that's 20 batches
of ~2-3s each = ~1min total instead of ~13min sequential."""


async def _lookup_cached_provider_id(
    engine, slug: str,
) -> str | None:
    """Read the slug → URN map from ``linkedin_profile_cache``.  Returns
    None on miss so the caller falls back to a Unipile resolve."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        return await session.scalar(
            select(LinkedInProfileCache.provider_id)
            .where(LinkedInProfileCache.slug == slug)
        )


async def _store_cached_provider_id(
    engine, slug: str, provider_id: str,
) -> None:
    """Upsert the slug → URN mapping.  Slug is PK so on-conflict updates
    the existing row's ``provider_id`` and bumps ``updated_at``."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        stmt = (
            pg_insert(LinkedInProfileCache)
            .values(slug=slug, provider_id=provider_id)
            .on_conflict_do_update(
                index_elements=["slug"],
                set_={"provider_id": provider_id, "updated_at": _now()},
            )
        )
        await session.execute(stmt)
        await session.commit()


async def _fetch_one_watchlist_profile(
    engine,
    provider,
    account: Any,
    search_id: uuid.UUID,
    url: str,
    cutoff: datetime,
) -> tuple[dict[str, Any], list[uuid.UUID]]:
    """Resolve + fetch one watchlist profile.  Returns (stats_row,
    new_post_ids).  Wrapped in a semaphore by the caller for concurrency.

    Reads ``linkedin_profile_cache`` first so we can short-circuit
    Unipile's slug→URN resolution.  On a cache miss the resolution
    happens inside Unipile's ``recent_posts`` call (which sets
    ``ProfileRef.urn`` as a side effect) — we then mirror that back into
    the persistent cache for the next run."""
    from app.services.linkedin import ProfileRef

    slug = _linkedin_slug(url)
    if not slug:
        return (
            {"profile_url": url, "invalid_url": True,
             "raw": 0, "kept": 0, "upserted_new": 0},
            [],
        )

    cached_urn = await _lookup_cached_provider_id(engine, slug)
    profile_ref = ProfileRef(public_id=slug, urn=cached_urn)
    cache_hit = cached_urn is not None

    try:
        raw_posts = await provider.recent_posts(account, profile_ref, limit=10)
    except Exception as exc:  # noqa: BLE001
        logger.warning("watchlist Unipile fetch failed for %s: %s", url, exc)
        raw_posts = []

    # Persist the URN if we just resolved it for the first time.  The
    # provider stamps it onto profile_ref.urn during the call.
    if not cache_hit and profile_ref.urn:
        try:
            await _store_cached_provider_id(engine, slug, profile_ref.urn)
        except Exception as exc:  # noqa: BLE001
            # Cache write is a perf optimisation, not correctness — log
            # but don't fail the watchlist phase.
            logger.warning("watchlist cache write failed for %s: %s", slug, exc)

    kept = 0
    upserted_new = 0
    new_post_ids: list[uuid.UUID] = []
    dropped_stale = 0
    dropped_undated = 0
    async with AsyncSession(engine, expire_on_commit=False) as session:
        for item in raw_posts:
            discovered = _post_from_unipile_payload(item, slug, url)
            if discovered is None:
                continue
            if discovered.post_date is None:
                dropped_undated += 1
                continue
            if discovered.post_date < cutoff:
                dropped_stale += 1
                continue
            kept += 1
            post_id, was_new = await _upsert_post(
                session, search_id, discovered,
                discovered_via=f"watchlist:{url}",
                provider="linkedin",
            )
            if was_new:
                upserted_new += 1
                new_post_ids.append(post_id)
        await session.commit()

    return (
        {
            "profile_url": url, "raw": len(raw_posts), "kept": kept,
            "upserted_new": upserted_new,
            "dropped_stale": dropped_stale,
            "dropped_undated": dropped_undated,
            "cache_hit": cache_hit,
        },
        new_post_ids,
    )


async def _run_watchlist(
    engine,
    search_id: uuid.UUID,
    profile_urls: list[str],
    max_post_age_days: int,
    qualify_budget: int,
) -> tuple[list[dict[str, Any]], list[uuid.UUID]]:
    """For each LinkedIn profile URL in the watchlist, pull recent posts
    via Unipile and upsert them.  Returns (per-profile stats, list of
    newly-inserted post ids ready to qualify).

    Picks the first OK LinkedIn account in the workspace.  If no
    LinkedIn account is connected, every profile stats row records
    ``no_account=True`` and we return zero posts (soft fail).

    Parallelism: profiles are processed in parallel with a semaphore of
    ``_WATCHLIST_CONCURRENCY`` so 200+ profile watchlists finish in
    minutes instead of tens of minutes.  Slug→URN resolution is cached
    in ``linkedin_profile_cache`` so subsequent runs make one Unipile
    call per profile instead of two."""
    from app.services.linkedin import ambient_provider

    cutoff = _now() - timedelta(days=max(1, max_post_age_days))

    # Find a usable LinkedIn account (Unipile-connected).
    async with AsyncSession(engine, expire_on_commit=False) as session:
        account = await session.scalar(
            select(LinkedInAccount).where(
                LinkedInAccount.status == LinkedInAccountStatus.OK,
                LinkedInAccount.unipile_account_id.is_not(None),
            ).limit(1)
        )
    if account is None:
        # Soft-fail: report no_account on every profile, dispatch
        # nothing.  Preserves the contract from before parallelisation.
        return (
            [
                {"profile_url": url, "no_account": True,
                 "raw": 0, "kept": 0, "upserted_new": 0}
                for url in profile_urls
            ],
            [],
        )

    provider = await ambient_provider()
    sem = asyncio.Semaphore(_WATCHLIST_CONCURRENCY)

    async def _bounded(url: str):
        async with sem:
            return await _fetch_one_watchlist_profile(
                engine, provider, account, search_id, url, cutoff,
            )

    results = await asyncio.gather(
        *[_bounded(url) for url in profile_urls],
        return_exceptions=False,
    )

    # Recombine per-profile stats in input order and apply the qualify
    # budget AFTER parallel fan-out (so the cap is deterministic
    # regardless of which task finished first).  ``upserted_new`` in the
    # stat row already reflects the true per-profile insertion count;
    # what we trim here is the qualify-batch queue, not the post rows.
    stats: list[dict[str, Any]] = []
    new_post_ids: list[uuid.UUID] = []
    for stat_row, ids in results:
        stats.append(stat_row)
        for pid in ids:
            if len(new_post_ids) < qualify_budget:
                new_post_ids.append(pid)

    return stats, new_post_ids


_LINKEDIN_SLUG_RE = None  # lazy-set below to avoid re-compiling


def _linkedin_slug(url: str) -> str | None:
    """Extract the slug from a LinkedIn profile URL.  Accepts forms like
    ``https://www.linkedin.com/in/jane-doe`` or with trailing slash /
    query.  Returns None if the URL doesn't look like a profile."""
    global _LINKEDIN_SLUG_RE
    if _LINKEDIN_SLUG_RE is None:
        import re
        _LINKEDIN_SLUG_RE = re.compile(r"linkedin\.com/in/([A-Za-z0-9_\-%]+)", re.I)
    if not url:
        return None
    m = _LINKEDIN_SLUG_RE.search(url)
    return m.group(1).rstrip("/") if m else None


def _post_from_unipile_payload(
    item: dict[str, Any], slug: str, profile_url: str,
):
    """Best-effort map from a Unipile-returned post dict to a
    ``DiscoveredPost``.  Field names vary across LinkedIn surface
    versions, so we try several common ones."""
    from app.services.social_listening_discovery import DiscoveredPost

    urn = item.get("urn") or item.get("provider_id") or item.get("id") or ""
    post_url = (
        item.get("post_url") or item.get("url")
        or (f"https://www.linkedin.com/feed/update/{urn}" if urn else None)
    )
    if not post_url:
        return None
    text = item.get("text") or item.get("post_text") or item.get("commentary") or ""
    # post_date — Unipile usually returns ``created_at`` ISO; fall back
    # to a few synonyms.
    raw_date = (
        item.get("created_at") or item.get("posted_at")
        or item.get("date") or item.get("post_date")
    )
    post_date = None
    if isinstance(raw_date, str) and raw_date.strip():
        try:
            cleaned = raw_date.strip().replace("Z", "+00:00")
            post_date = datetime.fromisoformat(cleaned)
            if post_date.tzinfo is None:
                post_date = post_date.replace(tzinfo=timezone.utc)
        except ValueError:
            post_date = None
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    return DiscoveredPost(
        post_url=post_url,
        provider_post_id=str(urn) if urn else None,
        post_text=str(text)[:5000],
        post_date=post_date,
        author_name=(author.get("name") or item.get("author_name")) or None,
        author_profile_url=profile_url,
        author_headline=author.get("headline") or item.get("author_headline") or None,
        company_name=author.get("company") or item.get("company_name") or None,
        raw=item,
    )


async def _upsert_post(
    session: AsyncSession,
    search_id: uuid.UUID,
    discovered: "social_listening_discovery.DiscoveredPost",
    discovered_via: str,
    provider: str = "linkedin",
) -> tuple[uuid.UUID, bool]:
    """Insert a row if (provider, post_url) is new; otherwise return the
    existing id.  Returns ``(post_id, was_new)``."""
    new_id = uuid.uuid4()
    stmt = (
        pg_insert(SocialListeningPost)
        .values(
            id=new_id,
            search_id=search_id,
            provider=provider,
            provider_post_id=discovered.provider_post_id,
            post_url=discovered.post_url,
            author_name=discovered.author_name,
            author_profile_url=discovered.author_profile_url,
            author_headline=discovered.author_headline,
            company_name=discovered.company_name,
            post_text=discovered.post_text,
            post_date=discovered.post_date,
            discovered_via=discovered_via,
            raw=discovered.raw or {},
        )
        .on_conflict_do_nothing(
            constraint="uq_social_posts_provider_url",
        )
        .returning(SocialListeningPost.id)
    )
    result = await session.execute(stmt)
    row = result.first()
    if row is not None:
        return row[0], True

    # Conflict — fetch the existing row id.
    existing = await session.execute(
        select(SocialListeningPost.id).where(
            and_(
                SocialListeningPost.provider == provider,
                SocialListeningPost.post_url == discovered.post_url,
            )
        )
    )
    existing_id = existing.scalar_one()
    return existing_id, False


async def _run_social_search_async(search_id: str) -> dict[str, Any]:
    sid = uuid.UUID(str(search_id))
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    qualify_task = None
    try:
        # ---- Load + mark running -----------------------------------------
        async with AsyncSession(engine, expire_on_commit=False) as session:
            search = await session.get(SocialListeningSearch, sid)
            if search is None:
                return {"status": "not_found"}
            if search.status != SocialSearchStatus.ACTIVE:
                # Paused / archived — skip gracefully (the beat dispatcher
                # already filters these out; this is defense in depth for
                # manual runs).
                return {"status": "skipped", "reason": "search_not_active"}

            search.last_run_status = "running"
            search.last_run_at = _now()
            search.last_run_error = None
            await session.commit()

            # Snapshot inputs (so we don't hold the row lock while doing
            # external AI calls).
            queries = list(search.expanded_queries or [])
            max_queries = search.max_queries_per_run
            max_posts = search.max_posts_per_query
            max_qualified = search.max_qualified_per_run
            max_post_age = search.max_post_age_days
            frequency = search.frequency
            # Multi-source fan-out.  Default to a single LinkedIn source
            # when the column is empty for backwards compat with rows
            # that pre-date migration 0017.
            sources_list = list(search.sources or [search.source.value]) or ["linkedin"]
            watchlist = list(search.linkedin_profile_watchlist or [])
            linkedin_web_search_enabled = bool(search.linkedin_web_search_enabled)
            linkedin_crosslink_enabled = bool(getattr(search, "linkedin_crosslink_enabled", True))
            max_run_cost_usd = float(search.max_run_cost_usd or 1.0)

        # If we have no expanded queries yet, expand inline.  Otherwise the
        # first run on a brand-new search would always be a no-op.
        if not queries:
            queries = await social_listening_topic_expander.expand_topic(
                topic=search.topic,
                niche=search.niche,
                geography=search.geography,
                include_keywords=list(search.include_keywords or []),
                exclude_keywords=list(search.exclude_keywords or []),
                max_queries=max_queries,
            )
            async with AsyncSession(engine, expire_on_commit=False) as session:
                row = await session.get(SocialListeningSearch, sid)
                if row is not None:
                    row.expanded_queries = queries
                    await session.commit()

        queries = queries[:max_queries]

        # ---- Snapshot already-seen URLs so we don't re-pull / re-qualify
        # posts we've already scored.  Bounded at 200 most-recent so the
        # discovery prompt stays a reasonable size; older posts that fall
        # outside the window will simply re-upsert as a no-op (the
        # UNIQUE(provider, post_url) constraint handles dedup either way).
        async with AsyncSession(engine, expire_on_commit=False) as session:
            seen_urls = list((await session.execute(
                select(SocialListeningPost.post_url)
                .where(SocialListeningPost.search_id == sid)
                .order_by(SocialListeningPost.discovered_at.desc())
                .limit(200)
            )).scalars().all())

        # ---- Discover posts ----------------------------------------------
        # Fan out across every (source, query) pair.  Semaphore caps the
        # concurrency at 4 across the whole grid; each pair is a separate
        # Anthropic call with web_search.
        sem = asyncio.Semaphore(4)

        async def _discover_one(src: str, q: str):
            async with sem:
                disc = await social_listening_discovery.discover_posts(
                    query=q, source=src, max_results=max_posts,
                    exclude_urls=seen_urls,
                    max_post_age_days=max_post_age,
                )
            return src, q, disc

        # Skip LinkedIn web-search pairs entirely when the toggle is
        # off (the default).  LinkedIn web-search is the most expensive
        # part of a run and rarely produces results; the watchlist is
        # the reliable LinkedIn channel.  Other sources are unaffected.
        pairs: list[tuple[str, str]] = []
        skipped_linkedin = 0
        for src in sources_list:
            if src == "linkedin" and not linkedin_web_search_enabled:
                skipped_linkedin = len(queries)
                continue
            for q in queries:
                pairs.append((src, q))

        # Chunked fan-out (still parallel within a chunk, sequential
        # across chunks) so we can check the running cost between
        # chunks and abort BEFORE making more expensive calls if the
        # per-run cap is hit.
        results: list[tuple[str, str, Any]] = []
        running_cost = 0.0
        cost_capped = False
        CHUNK = 5  # 5 parallel pairs per chunk
        for chunk_start in range(0, len(pairs), CHUNK):
            chunk = pairs[chunk_start:chunk_start + CHUNK]
            chunk_results = await asyncio.gather(
                *[_discover_one(src, q) for (src, q) in chunk],
                return_exceptions=False,
            )
            for src, q, disc in chunk_results:
                running_cost += disc.cost_usd
                results.append((src, q, disc))
            if running_cost >= max_run_cost_usd:
                cost_capped = True
                logger.info(
                    "social_listening: cost cap of $%.2f hit after $%.2f "
                    "spent (search=%s); aborting remaining %d pairs",
                    max_run_cost_usd, running_cost, sid,
                    max(0, len(pairs) - len(results)),
                )
                break

        # ---- Upsert + enqueue qualify + collect per-query stats ---------
        new_posts: list[tuple[uuid.UUID, uuid.UUID]] = []  # (post_id, search_id)
        total_seen = 0
        per_query_stats: list[dict[str, Any]] = []
        discovery_errors: list[str] = []  # one entry per failed pair
        async with AsyncSession(engine, expire_on_commit=False) as session:
            for src, query, disc in results:
                if disc.error:
                    discovery_errors.append(disc.error)
                # Discovery-level stats (raw, dropped_*).
                qstats = {
                    "query": query,
                    "source": src,
                    "raw": disc.raw,
                    "kept": disc.kept,
                    "kept_undated": disc.kept_undated,
                    "dropped_invalid_url": disc.dropped_invalid_url,
                    "dropped_excluded": disc.dropped_excluded,
                    "dropped_duplicate": disc.dropped_duplicate,
                    "dropped_undated": disc.dropped_undated,
                    "dropped_stale": disc.dropped_stale,
                    "upserted_new": 0,
                    "upserted_existing": 0,
                }
                for discovered in disc.posts:
                    total_seen += 1
                    post_id, was_new = await _upsert_post(
                        session, sid, discovered, discovered_via=query,
                        provider=src,
                    )
                    if was_new:
                        qstats["upserted_new"] += 1
                        if len(new_posts) < max_qualified:
                            new_posts.append((post_id, sid))
                    else:
                        qstats["upserted_existing"] += 1
                per_query_stats.append(qstats)
            await session.commit()

        # ---- Reddit → LinkedIn cross-link extraction --------------------
        # Mine LinkedIn URLs out of Reddit post bodies.  Zero Anthropic
        # cost — synthesised locally — so we use a fixed neutral score of
        # 5 + recommended_action=research_further.  Each synthesised post
        # gets an opportunity row inline (no qualify-batch enqueue) since
        # we don't have the actual LinkedIn post text to score.
        crosslink_stats: dict[str, int] = {
            "reddit_posts_scanned": 0,
            "linkedin_urls_found": 0,
            "linkedin_posts_new": 0,
            "linkedin_posts_existing": 0,
        }
        if linkedin_crosslink_enabled:
            from app.services import social_listening_linkedin_crosslink as crosslink
            async with AsyncSession(engine, expire_on_commit=False) as session:
                for src, _q, disc in results:
                    if src != "reddit":
                        continue
                    for reddit_post in disc.posts:
                        crosslink_stats["reddit_posts_scanned"] += 1
                        urls = crosslink.extract_linkedin_urls(reddit_post.post_text or "")
                        for li_url in urls:
                            crosslink_stats["linkedin_urls_found"] += 1
                            synthetic = crosslink.build_synthetic_discovered_post(
                                linkedin_url=li_url,
                                reddit_post_url=reddit_post.post_url,
                                reddit_excerpt=reddit_post.post_text or "",
                            )
                            post_id, was_new = await _upsert_post(
                                session, sid, synthetic,
                                discovered_via=f"reddit-crosslink:{reddit_post.post_url}",
                                provider="linkedin",
                            )
                            if was_new:
                                crosslink_stats["linkedin_posts_new"] += 1
                                await _upsert_opportunity(
                                    session, post_id,
                                    crosslink.build_synthetic_qualification(
                                        reddit_post_url=reddit_post.post_url,
                                    ),
                                )
                            else:
                                crosslink_stats["linkedin_posts_existing"] += 1
                await session.commit()

        # ---- LinkedIn watchlist (Unipile direct fetch) ------------------
        # Per-profile direct pull bypasses web-search indexing entirely.
        # Each profile contributes a per-profile stat row keyed by URL.
        watchlist_stats: list[dict[str, Any]] = []
        if watchlist:
            wl_stats, wl_new = await _run_watchlist(
                engine, sid, watchlist, max_post_age, max_qualified - len(new_posts),
            )
            watchlist_stats = wl_stats
            for post_id in wl_new:
                if len(new_posts) < max_qualified:
                    new_posts.append((post_id, sid))

        # Aggregate summary across all (source, query) pairs.
        summary = {
            "total_sources": len(sources_list),
            "total_queries": len(queries),
            "total_pairs": len(per_query_stats),
            "linkedin_web_search_skipped_pairs": skipped_linkedin,
            "cost_usd": round(running_cost, 4),
            "max_run_cost_usd": float(max_run_cost_usd),
            "cost_capped": cost_capped,
            "total_raw": sum(q["raw"] for q in per_query_stats),
            "total_kept": sum(q["kept"] for q in per_query_stats),
            "total_dropped_invalid_url": sum(q["dropped_invalid_url"] for q in per_query_stats),
            "total_dropped_excluded": sum(q["dropped_excluded"] for q in per_query_stats),
            "total_dropped_duplicate": sum(q["dropped_duplicate"] for q in per_query_stats),
            "total_dropped_undated": sum(q["dropped_undated"] for q in per_query_stats),
            "total_dropped_stale": sum(q["dropped_stale"] for q in per_query_stats),
            "total_upserted_new": sum(q["upserted_new"] for q in per_query_stats),
            "ran_at": _now().isoformat(),
        }
        run_stats = {
            "queries": per_query_stats,
            "watchlist": watchlist_stats,
            "crosslink": crosslink_stats,
            "summary": summary,
        }

        # Enqueue qualification AFTER the post rows commit.  Batched at
        # ~10 posts per Anthropic call — one batch task per chunk
        # instead of one task per post.  Cost goes from ~$0.005/post to
        # ~$0.0012/post on Haiku, saving ~70% on the qualify phase.
        try:
            ids = [str(post_id) for post_id, _ in new_posts]
            for i in range(0, len(ids), QUALIFY_BATCH_SIZE):
                chunk = ids[i:i + QUALIFY_BATCH_SIZE]
                qualify_social_posts_batch.delay(chunk, str(sid))
        except Exception:  # noqa: BLE001
            # Broker unreachable in tests — leave the posts as
            # "discovered but unqualified" so a later run can pick them up.
            pass

        # ---- Mark done + persist stats + schedule next ------------------
        next_run = _next_run_at(frequency)
        # When every (or nearly every) discovery pair failed with the
        # same error (e.g. Anthropic out of credits), surface a clear
        # summary on ``last_run_error`` so the user sees it in the UI
        # instead of staring at a "done, 0 posts" result and wondering
        # what happened.
        total_pairs = len(per_query_stats)
        run_error: str | None = None
        if discovery_errors:
            from collections import Counter
            top, top_count = Counter(discovery_errors).most_common(1)[0]
            run_error = (
                f"{top_count} of {total_pairs} discovery calls failed: {top}"
            )

        # If the cost cap fired AND nothing useful came back, surface it
        # in last_run_error.  Otherwise it's just a stat in the summary.
        if cost_capped and not run_error:
            run_error = (
                f"Cost cap reached: spent ${running_cost:.2f} of "
                f"${max_run_cost_usd:.2f} budget after "
                f"{len(results)} of {len(pairs)} discovery calls — "
                "raise the cap or narrow the search."
            )

        async with AsyncSession(engine, expire_on_commit=False) as session:
            row = await session.get(SocialListeningSearch, sid)
            if row is not None:
                row.last_run_status = "done" if not cost_capped else "cost_capped"
                row.last_run_error = run_error
                row.next_run_at = next_run
                row.last_run_stats = run_stats
                await session.commit()

        return {
            "status": "done",
            "queries_run": len(queries),
            "posts_seen": total_seen,
            "posts_new": len(new_posts),
            "next_run_at": next_run.isoformat() if next_run else None,
        }
    finally:
        await engine.dispose()


async def _mark_search_failed(search_id: str, error: str) -> None:
    sid = uuid.UUID(str(search_id))
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            row = await session.get(SocialListeningSearch, sid)
            if row is not None:
                row.last_run_status = "failed"
                row.last_run_error = error[:500]
                await session.commit()
    finally:
        await engine.dispose()


@celery_app.task(bind=True, name="social_listening.run_search", max_retries=2)
def run_social_search(self, search_id: str) -> dict[str, Any]:  # noqa: D401
    try:
        return asyncio.run(with_record_tenant(SocialListeningSearch, search_id, _run_social_search_async, search_id))
    except Exception as exc:  # noqa: BLE001
        logger.exception("run_social_search failed for %s", search_id)
        try:
            raise self.retry(exc=exc, countdown=120 * (2 ** self.request.retries))
        except self.MaxRetriesExceededError:
            asyncio.run(with_record_tenant(SocialListeningSearch, search_id, _mark_search_failed, search_id, str(exc)))
            return {"status": "failed", "error": str(exc)}


# ---------------------------------------------------------------------------
# Per-post qualifier
# ---------------------------------------------------------------------------

async def _qualify_social_post_async(post_id: str, search_id: str) -> dict[str, Any]:
    pid = uuid.UUID(str(post_id))
    sid = uuid.UUID(str(search_id))
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        # Load post + search settings.
        async with AsyncSession(engine, expire_on_commit=False) as session:
            post = await session.get(SocialListeningPost, pid)
            if post is None:
                return {"status": "not_found"}
            search = await session.get(SocialListeningSearch, sid)
            if search is None:
                return {"status": "not_found"}
            payload = {
                "post_text": post.post_text or "",
                "author_name": post.author_name,
                "author_headline": post.author_headline,
                "company_name": post.company_name,
                "search_tone": search.tone or "helpful",
                "sender_name": search.sender_name,
            }

        result = await social_listening_qualifier.qualify_post(**payload)
        if result is None:
            return {"status": "skipped", "reason": "qualifier_returned_none"}

        # Upsert the opportunity row keyed by post_id.
        async with AsyncSession(engine, expire_on_commit=False) as session:
            stmt = (
                pg_insert(SocialListeningOpportunity)
                .values(
                    id=uuid.uuid4(),
                    post_id=pid,
                    score=result.score,
                    category=result.category,
                    buying_signal=result.buying_signal,
                    pain_summary=result.pain_summary,
                    qualification_reason=result.qualification_reason,
                    suggested_comment=result.suggested_comment,
                    suggested_connection_request=result.suggested_connection_request,
                    suggested_follow_up=result.suggested_follow_up,
                    recommended_action=result.recommended_action,
                    status=SocialOpportunityStatus.NEW.value,
                )
                .on_conflict_do_update(
                    index_elements=["post_id"],
                    set_={
                        "score": result.score,
                        "category": result.category,
                        "buying_signal": result.buying_signal,
                        "pain_summary": result.pain_summary,
                        "qualification_reason": result.qualification_reason,
                        "suggested_comment": result.suggested_comment,
                        "suggested_connection_request": result.suggested_connection_request,
                        "suggested_follow_up": result.suggested_follow_up,
                        "recommended_action": result.recommended_action,
                        # Don't reset user-edited status / notes on
                        # re-qualification.
                        "updated_at": _now(),
                    },
                )
            )
            await session.execute(stmt)
            await session.commit()

        return {"status": "done", "score": result.score, "category": result.category}
    finally:
        await engine.dispose()


async def _upsert_opportunity(
    session: AsyncSession, pid: uuid.UUID, result,
) -> None:
    """Insert-or-update the opportunity row for ``pid`` from a
    QualificationResult.  User-set ``status`` and ``notes`` are
    preserved on re-qualification."""
    stmt = (
        pg_insert(SocialListeningOpportunity)
        .values(
            id=uuid.uuid4(),
            post_id=pid,
            score=result.score,
            category=result.category,
            buying_signal=result.buying_signal,
            pain_summary=result.pain_summary,
            qualification_reason=result.qualification_reason,
            suggested_comment=result.suggested_comment,
            suggested_connection_request=result.suggested_connection_request,
            suggested_follow_up=result.suggested_follow_up,
            recommended_action=result.recommended_action,
            status=SocialOpportunityStatus.NEW.value,
        )
        .on_conflict_do_update(
            index_elements=["post_id"],
            set_={
                "score": result.score,
                "category": result.category,
                "buying_signal": result.buying_signal,
                "pain_summary": result.pain_summary,
                "qualification_reason": result.qualification_reason,
                "suggested_comment": result.suggested_comment,
                "suggested_connection_request": result.suggested_connection_request,
                "suggested_follow_up": result.suggested_follow_up,
                "recommended_action": result.recommended_action,
                "updated_at": _now(),
            },
        )
    )
    await session.execute(stmt)


async def _qualify_social_posts_batch_async(
    post_ids: list[str], search_id: str,
) -> dict[str, Any]:
    """Batch sibling of ``_qualify_social_post_async``: loads up to N
    posts at once, qualifies them with ONE Anthropic call, upserts the
    opportunity rows.  ~70% cheaper than per-post calls thanks to
    amortizing the prompt + setup overhead."""
    if not post_ids:
        return {"status": "skipped", "reason": "no_post_ids"}
    sid = uuid.UUID(str(search_id))
    pids = [uuid.UUID(str(p)) for p in post_ids]
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            search = await session.get(SocialListeningSearch, sid)
            if search is None:
                return {"status": "not_found"}
            posts: list[Any] = []
            for pid in pids:
                post = await session.get(SocialListeningPost, pid)
                if post is not None:
                    posts.append(post)

        if not posts:
            return {"status": "skipped", "reason": "no_posts_found"}

        payloads = [{
            "post_text": p.post_text or "",
            "author_name": p.author_name,
            "author_headline": p.author_headline,
            "company_name": p.company_name,
        } for p in posts]

        results = await social_listening_qualifier.qualify_posts(
            payloads,
            search_tone=search.tone or "helpful",
            sender_name=search.sender_name,
        )

        upserted = 0
        async with AsyncSession(engine, expire_on_commit=False) as session:
            for post, result in zip(posts, results):
                if result is None:
                    continue
                await _upsert_opportunity(session, post.id, result)
                upserted += 1
            await session.commit()

        return {"status": "done", "batch": len(posts), "upserted": upserted}
    finally:
        await engine.dispose()


@celery_app.task(bind=True, name="social_listening.qualify_posts_batch", max_retries=2)
def qualify_social_posts_batch(
    self, post_ids: list[str], search_id: str,
) -> dict[str, Any]:  # noqa: D401
    try:
        return asyncio.run(with_record_tenant(SocialListeningSearch, search_id, _qualify_social_posts_batch_async, post_ids, search_id))
    except Exception as exc:  # noqa: BLE001
        logger.exception("qualify_social_posts_batch failed for %d posts", len(post_ids or []))
        try:
            raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))
        except self.MaxRetriesExceededError:
            return {"status": "failed", "error": str(exc)}


@celery_app.task(bind=True, name="social_listening.qualify_post", max_retries=2)
def qualify_social_post(self, post_id: str, search_id: str) -> dict[str, Any]:  # noqa: D401
    try:
        return asyncio.run(with_record_tenant(SocialListeningSearch, search_id, _qualify_social_post_async, post_id, search_id))
    except Exception as exc:  # noqa: BLE001
        logger.exception("qualify_social_post failed for post=%s", post_id)
        try:
            raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))
        except self.MaxRetriesExceededError:
            return {"status": "failed", "error": str(exc)}


# ---------------------------------------------------------------------------
# Beat dispatcher
# ---------------------------------------------------------------------------

async def _scheduled_runner_async() -> dict[str, Any]:
    """Select active+non-manual searches whose next_run_at is due, enqueue
    a run for each.  Mirrors the sequencer.advance_sequences pattern."""
    engine = create_async_engine(settings.APP_DATABASE_URL or settings.DATABASE_URL)
    enqueued: list[str] = []
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            now = _now()
            rows = (await session.execute(
                select(SocialListeningSearch.id).where(
                    and_(
                        SocialListeningSearch.status == SocialSearchStatus.ACTIVE,
                        SocialListeningSearch.frequency != SocialSearchFrequency.MANUAL,
                        SocialListeningSearch.next_run_at.is_not(None),
                        SocialListeningSearch.next_run_at <= now,
                        # Don't double-dispatch if a previous run is still
                        # in flight.  NULL last_run_status (fresh row)
                        # counts as "not running"; we must say so
                        # explicitly because SQL ``NULL != 'running'``
                        # evaluates to NULL (excluded by WHERE).
                        or_(
                            SocialListeningSearch.last_run_status.is_(None),
                            SocialListeningSearch.last_run_status != "running",
                        ),
                    )
                )
            )).scalars().all()
            for sid in rows:
                enqueued.append(str(sid))
    finally:
        await engine.dispose()

    for sid in enqueued:
        try:
            run_social_search.delay(sid)
        except Exception:  # noqa: BLE001
            # Broker unreachable in tests; surface but don't fail the beat.
            logger.warning("scheduled_runner: failed to enqueue %s", sid)

    return {"enqueued": len(enqueued)}


@celery_app.task(name="social_listening.scheduled_runner")
def scheduled_runner() -> dict[str, Any]:
    return asyncio.run(for_all_tenants(_scheduled_runner_async))
