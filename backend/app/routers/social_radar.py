"""Social Listening Radar HTTP layer.

Searches + opportunities CRUD + manual run + sync expand-preview.  Long-
running discovery / qualification is delegated to Celery workers via
``apply_async`` / ``.delay()``; only ``expand-preview`` runs the AI
synchronously (it's a fast Haiku call and the user is waiting in the
browser).
"""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import (
    SocialListeningOpportunity,
    SocialListeningPost,
    SocialListeningSearch,
    SocialOpportunityCategory,
    SocialOpportunityStatus,
    SocialPostProvider,
    SocialSearchSource,
    SocialSearchStatus,
)
from app.schemas.social_listening import (
    CleanupStaleResponse,
    ExpandPreviewRequest,
    ExpandPreviewResponse,
    RunCostEstimate,
    PaginatedOpportunities,
    PaginatedSearches,
    RequalifyAllResponse,
    RunSearchResponse,
    SocialListeningOpportunitySummary,
    SocialListeningOpportunityUpdate,
    SocialListeningPostSummary,
    SocialListeningSearchCreate,
    SocialListeningSearchResponse,
    SocialListeningSearchSummary,
    SocialListeningSearchUpdate,
)
from app.services import social_listening_topic_expander

router = APIRouter(prefix="/social-radar", tags=["social-radar"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_search_or_404(
    db: AsyncSession, search_id: uuid.UUID,
) -> SocialListeningSearch:
    s = await db.get(SocialListeningSearch, search_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Search not found")
    return s


async def _get_opportunity_or_404(
    db: AsyncSession, opp_id: uuid.UUID,
) -> SocialListeningOpportunity:
    o = await db.get(SocialListeningOpportunity, opp_id)
    if o is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    return o


async def _search_counts(
    db: AsyncSession, search_ids: list[uuid.UUID],
) -> dict[uuid.UUID, tuple[int, int]]:
    """Return ``{search_id: (post_count, opportunity_count)}`` for a batch."""
    if not search_ids:
        return {}
    post_q = (
        select(SocialListeningPost.search_id, func.count())
        .where(SocialListeningPost.search_id.in_(search_ids))
        .group_by(SocialListeningPost.search_id)
    )
    opp_q = (
        select(SocialListeningPost.search_id, func.count())
        .join(SocialListeningOpportunity, SocialListeningOpportunity.post_id == SocialListeningPost.id)
        .where(SocialListeningPost.search_id.in_(search_ids))
        .group_by(SocialListeningPost.search_id)
    )
    post_counts = {sid: cnt for sid, cnt in (await db.execute(post_q)).all()}
    opp_counts = {sid: cnt for sid, cnt in (await db.execute(opp_q)).all()}
    return {
        sid: (post_counts.get(sid, 0), opp_counts.get(sid, 0)) for sid in search_ids
    }


def _search_summary(s: SocialListeningSearch, post_count: int, opp_count: int) -> SocialListeningSearchSummary:
    return SocialListeningSearchSummary(
        id=s.id,
        name=s.name,
        topic=s.topic,
        niche=s.niche,
        geography=s.geography,
        source=s.source,
        sources=list(s.sources or [s.source.value]),
        frequency=s.frequency,
        status=s.status,
        last_run_at=s.last_run_at,
        next_run_at=s.next_run_at,
        last_run_status=s.last_run_status,
        last_run_error=s.last_run_error,
        expanded_query_count=len(s.expanded_queries or []),
        post_count=post_count,
        opportunity_count=opp_count,
        created_at=s.created_at,
        updated_at=s.updated_at,
    )


def _search_detail(s: SocialListeningSearch, post_count: int, opp_count: int) -> SocialListeningSearchResponse:
    return SocialListeningSearchResponse(
        id=s.id,
        name=s.name,
        topic=s.topic,
        niche=s.niche,
        geography=s.geography,
        source=s.source,
        sources=list(s.sources or [s.source.value]),
        frequency=s.frequency,
        status=s.status,
        last_run_at=s.last_run_at,
        next_run_at=s.next_run_at,
        last_run_status=s.last_run_status,
        last_run_error=s.last_run_error,
        expanded_query_count=len(s.expanded_queries or []),
        post_count=post_count,
        opportunity_count=opp_count,
        created_at=s.created_at,
        updated_at=s.updated_at,
        expanded_queries=list(s.expanded_queries or []),
        include_keywords=list(s.include_keywords or []),
        exclude_keywords=list(s.exclude_keywords or []),
        tone=s.tone,
        sender_name=s.sender_name,
        max_queries_per_run=s.max_queries_per_run,
        max_posts_per_query=s.max_posts_per_query,
        max_qualified_per_run=s.max_qualified_per_run,
        max_post_age_days=s.max_post_age_days,
        last_run_stats=dict(s.last_run_stats or {}),
        linkedin_profile_watchlist=list(s.linkedin_profile_watchlist or []),
        linkedin_web_search_enabled=bool(s.linkedin_web_search_enabled),
        linkedin_crosslink_enabled=bool(s.linkedin_crosslink_enabled),
        max_run_cost_usd=float(s.max_run_cost_usd or 1.0),
    )


# ---------------------------------------------------------------------------
# Searches CRUD
# ---------------------------------------------------------------------------

@router.get("/searches", response_model=PaginatedSearches)
async def list_searches(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    status_filter: SocialSearchStatus | None = Query(default=None, alias="status"),
    source: SocialSearchSource | None = Query(default=None),
    search: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> PaginatedSearches:
    filters: list[Any] = []
    if status_filter is not None:
        filters.append(SocialListeningSearch.status == status_filter)
    if source is not None:
        filters.append(SocialListeningSearch.source == source)
    if search:
        like = f"%{search.strip()}%"
        filters.append(or_(
            SocialListeningSearch.name.ilike(like),
            SocialListeningSearch.topic.ilike(like),
            SocialListeningSearch.niche.ilike(like),
        ))

    where = and_(*filters) if filters else None
    total_q = select(func.count()).select_from(SocialListeningSearch)
    if where is not None:
        total_q = total_q.where(where)
    total = (await db.execute(total_q)).scalar_one() or 0

    q = select(SocialListeningSearch)
    if where is not None:
        q = q.where(where)
    q = (
        q.order_by(SocialListeningSearch.created_at.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    rows = list((await db.execute(q)).scalars().all())
    counts = await _search_counts(db, [r.id for r in rows])

    items = [_search_summary(r, *counts.get(r.id, (0, 0))) for r in rows]
    return PaginatedSearches(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) if total else 0,
    )


@router.post("/searches", response_model=SocialListeningSearchResponse, status_code=201)
async def create_search(
    payload: SocialListeningSearchCreate,
    db: AsyncSession = Depends(get_db),
) -> SocialListeningSearchResponse:
    # Sources: prefer the explicit list when given; otherwise fall back
    # to a single-element list of the legacy ``source`` field for
    # backward-compat with old API clients.
    sources_list = (
        [src.value for src in payload.sources]
        if payload.sources
        else [payload.source.value]
    )
    s = SocialListeningSearch(
        name=payload.name,
        topic=payload.topic,
        niche=payload.niche,
        geography=payload.geography,
        include_keywords=payload.include_keywords or [],
        exclude_keywords=payload.exclude_keywords or [],
        source=payload.source,
        sources=sources_list,
        linkedin_profile_watchlist=list(payload.linkedin_profile_watchlist or []),
        linkedin_web_search_enabled=bool(payload.linkedin_web_search_enabled),
        linkedin_crosslink_enabled=bool(payload.linkedin_crosslink_enabled),
        max_run_cost_usd=payload.max_run_cost_usd,
        frequency=payload.frequency,
        status=payload.status,
        tone=payload.tone,
        sender_name=payload.sender_name,
        max_queries_per_run=payload.max_queries_per_run,
        max_posts_per_query=payload.max_posts_per_query,
        max_qualified_per_run=payload.max_qualified_per_run,
        max_post_age_days=payload.max_post_age_days,
    )
    db.add(s)
    await db.commit()
    await db.refresh(s)

    # Kick off async expansion so the row is usable once the worker
    # finishes — UI polls or refetches.
    try:
        from app.workers.social_listening import expand_social_topic
        expand_social_topic.delay(str(s.id))
    except Exception:  # noqa: BLE001
        # Don't fail the create just because the queue is unreachable in tests.
        pass

    return _search_detail(s, 0, 0)


@router.get("/searches/{search_id}", response_model=SocialListeningSearchResponse)
async def get_search(
    search_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> SocialListeningSearchResponse:
    s = await _get_search_or_404(db, search_id)
    counts = await _search_counts(db, [s.id])
    p, o = counts.get(s.id, (0, 0))
    return _search_detail(s, p, o)


@router.patch("/searches/{search_id}", response_model=SocialListeningSearchResponse)
async def update_search(
    search_id: uuid.UUID,
    payload: SocialListeningSearchUpdate,
    db: AsyncSession = Depends(get_db),
) -> SocialListeningSearchResponse:
    s = await _get_search_or_404(db, search_id)
    updates = payload.model_dump(exclude_unset=True)

    topic_changed = "topic" in updates and updates["topic"] != s.topic
    for key, value in updates.items():
        setattr(s, key, value)
    await db.commit()
    await db.refresh(s)

    if topic_changed:
        try:
            from app.workers.social_listening import expand_social_topic
            expand_social_topic.delay(str(s.id))
        except Exception:  # noqa: BLE001
            pass

    counts = await _search_counts(db, [s.id])
    p, o = counts.get(s.id, (0, 0))
    return _search_detail(s, p, o)


@router.delete("/searches/{search_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_search(
    search_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    s = await _get_search_or_404(db, search_id)
    await db.delete(s)
    await db.commit()


@router.post("/searches/{search_id}/run", response_model=RunSearchResponse)
async def run_search_now(
    search_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> RunSearchResponse:
    s = await _get_search_or_404(db, search_id)
    if s.status != SocialSearchStatus.ACTIVE:
        # Paused/archived searches can't be manually run.  Without this
        # gate the worker would dispatch, then bail with "skipped" inside
        # ``_run_social_search_async`` — costing a task slot and flipping
        # last_run_status to "pending" for no reason.
        raise HTTPException(
            status_code=409,
            detail=f"Cannot run a search in status '{s.status.value}' — set it to 'active' first.",
        )
    if s.last_run_status == "running":
        raise HTTPException(status_code=409, detail="Search is already running")

    s.last_run_status = "pending"
    s.last_run_error = None
    await db.commit()

    try:
        from app.workers.social_listening import run_social_search
        async_result = run_social_search.delay(str(s.id))
        return RunSearchResponse(task_id=getattr(async_result, "id", None), status="queued")
    except Exception:  # noqa: BLE001
        # Soft-fail in tests where the broker isn't reachable.
        return RunSearchResponse(task_id=None, status="queued")


@router.get("/searches/{search_id}/estimate", response_model=RunCostEstimate)
async def estimate_run_cost(
    search_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> RunCostEstimate:
    """Forecast what the next Run-now will cost based on the search's
    current settings.  Used to populate the 'Run now (~$X.XX)' label so
    the user sees spend before they click."""
    from app.config import settings as _settings
    from app.services._anthropic_cost import (
        estimate_discovery_cost_usd,
        estimate_qualify_cost_usd,
    )

    s = await _get_search_or_404(db, search_id)
    sources = list(s.sources or [s.source.value])
    queries_capped = min(
        len(s.expanded_queries or []) or s.max_queries_per_run,
        s.max_queries_per_run,
    )
    disc = estimate_discovery_cost_usd(
        num_queries=queries_capped,
        sources=sources,
        linkedin_web_search_enabled=bool(s.linkedin_web_search_enabled),
        max_uses_linkedin=_settings.LINKEDIN_DISCOVERY_WEB_SEARCH_MAX_USES,
        max_uses_other=_settings.SOCIAL_DISCOVERY_WEB_SEARCH_MAX_USES,
    )
    # Upper-bound the qualify count at max_qualified_per_run because no
    # run dispatches more than that regardless of how many posts surface.
    estimated_new_posts = min(s.max_qualified_per_run, s.max_posts_per_query * 2)
    qualify_cost = estimate_qualify_cost_usd(estimated_new_posts)
    total = round(disc["discovery_cost_usd"] + qualify_cost, 3)

    notes: list[str] = []
    if "linkedin" in sources and not s.linkedin_web_search_enabled:
        notes.append(
            "LinkedIn web-search is OFF; LinkedIn signal will come only "
            "from the watchlist (which costs ~$0 — direct Unipile)."
        )
    if total > float(s.max_run_cost_usd or 1.0):
        notes.append(
            f"⚠ Estimated cost (${total}) exceeds your cap "
            f"(${float(s.max_run_cost_usd):.2f}).  Worker will abort "
            "mid-run if it gets close."
        )

    return RunCostEstimate(
        discovery_pairs=disc["discovery_pairs"],
        discovery_cost_usd=disc["discovery_cost_usd"],
        qualify_estimated_posts=estimated_new_posts,
        qualify_cost_usd=qualify_cost,
        total_cost_usd=total,
        max_run_cost_usd=float(s.max_run_cost_usd or 1.0),
        notes=notes,
    )


@router.post(
    "/searches/{search_id}/requalify-all",
    response_model=RequalifyAllResponse,
)
async def requalify_all_posts(
    search_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> RequalifyAllResponse:
    """Re-qualify every post for this search under the current qualifier
    prompt.  Useful after tightening the scoring rubric — overwrites AI
    fields on each opportunity but PRESERVES user-set ``status`` and
    ``notes`` (the upsert's ``on_conflict_do_update`` set_ clause skips
    them).  Cost: ~$0.10 per 100 posts (Haiku-batched at 10/call)."""
    s = await _get_search_or_404(db, search_id)

    post_ids = list((await db.execute(
        select(SocialListeningPost.id)
        .where(SocialListeningPost.search_id == s.id)
        .order_by(SocialListeningPost.discovered_at.desc())
    )).scalars().all())

    if not post_ids:
        return RequalifyAllResponse(enqueued=0, batches=0)

    # Dispatch in chunks of QUALIFY_BATCH_SIZE — same shape the run
    # orchestrator uses so the cost profile is identical.
    from app.workers.social_listening import (
        QUALIFY_BATCH_SIZE,
        qualify_social_posts_batch,
    )
    ids = [str(pid) for pid in post_ids]
    batches = 0
    for i in range(0, len(ids), QUALIFY_BATCH_SIZE):
        chunk = ids[i:i + QUALIFY_BATCH_SIZE]
        try:
            qualify_social_posts_batch.delay(chunk, str(s.id))
            batches += 1
        except Exception:  # noqa: BLE001
            # Soft-fail in tests where the broker isn't reachable;
            # the count just shrinks.
            pass

    return RequalifyAllResponse(enqueued=len(ids), batches=batches)


@router.post("/searches/{search_id}/cleanup-stale", response_model=CleanupStaleResponse)
async def cleanup_stale_posts(
    search_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> CleanupStaleResponse:
    """Delete posts for this search whose ``post_date`` is older than
    ``max_post_age_days`` OR has no date at all (we can't verify
    freshness on those).  Cascades to opportunities via the FK ON
    DELETE CASCADE.  Used to clean up rows that were pulled before
    the freshness filter shipped, or after a user tightens the lookback
    window."""
    s = await _get_search_or_404(db, search_id)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, s.max_post_age_days))
    result = await db.execute(
        delete(SocialListeningPost).where(
            and_(
                SocialListeningPost.search_id == s.id,
                or_(
                    SocialListeningPost.post_date.is_(None),
                    SocialListeningPost.post_date < cutoff,
                ),
            )
        )
    )
    await db.commit()
    return CleanupStaleResponse(deleted=result.rowcount or 0)


@router.post("/searches/{search_id}/expand-preview", response_model=ExpandPreviewResponse)
async def expand_preview_for_search(
    search_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> ExpandPreviewResponse:
    """Re-run the expansion synchronously and return the result without
    saving.  The body comes from the saved row (so the editor can preview
    with whatever it has)."""
    s = await _get_search_or_404(db, search_id)
    queries = await social_listening_topic_expander.expand_topic(
        topic=s.topic,
        niche=s.niche,
        geography=s.geography,
        include_keywords=list(s.include_keywords or []),
        exclude_keywords=list(s.exclude_keywords or []),
        max_queries=s.max_queries_per_run,
    )
    return ExpandPreviewResponse(queries=queries)


@router.post("/expand-preview", response_model=ExpandPreviewResponse)
async def expand_preview_ad_hoc(
    payload: ExpandPreviewRequest,
) -> ExpandPreviewResponse:
    """Preview an expansion for a NEW search the user is still composing."""
    queries = await social_listening_topic_expander.expand_topic(
        topic=payload.topic,
        niche=payload.niche,
        geography=payload.geography,
        include_keywords=payload.include_keywords or [],
        exclude_keywords=payload.exclude_keywords or [],
        max_queries=payload.max_queries,
    )
    return ExpandPreviewResponse(queries=queries)


# ---------------------------------------------------------------------------
# Opportunities
# ---------------------------------------------------------------------------

def _opportunity_summary(
    o: SocialListeningOpportunity,
    p: SocialListeningPost,
    search_name: str | None,
) -> SocialListeningOpportunitySummary:
    return SocialListeningOpportunitySummary(
        id=o.id,
        post_id=o.post_id,
        search_id=p.search_id,
        search_name=search_name,
        score=o.score,
        category=o.category,
        buying_signal=o.buying_signal,
        pain_summary=o.pain_summary,
        qualification_reason=o.qualification_reason,
        suggested_comment=o.suggested_comment,
        suggested_connection_request=o.suggested_connection_request,
        suggested_follow_up=o.suggested_follow_up,
        recommended_action=o.recommended_action,
        status=o.status,
        notes=o.notes,
        created_at=o.created_at,
        updated_at=o.updated_at,
        post=SocialListeningPostSummary.model_validate(p),
    )


# Sort-key whitelist for ``GET /opportunities``.  Each entry maps a public
# API name → the SQLAlchemy column the user wants to sort by.  Whitelist
# is the single source of truth — adding a new sort key means adding it
# here and nowhere else.  Used by both the router validation and the
# frontend dropdown (kept in sync via tests).
_OPP_SORT_COLUMNS = {
    "score": SocialListeningOpportunity.score,
    "discovered_at": SocialListeningPost.discovered_at,
    "updated_at": SocialListeningOpportunity.updated_at,
}


@router.get("/opportunities", response_model=PaginatedOpportunities)
async def list_opportunities(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    search_id: uuid.UUID | None = None,
    status_filter: SocialOpportunityStatus | None = Query(default=None, alias="status"),
    category: SocialOpportunityCategory | None = Query(default=None),
    source: SocialPostProvider | None = Query(default=None),
    min_score: int | None = Query(default=None, ge=1, le=10),
    buying_signal: bool | None = Query(default=None),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    sort_by: str = Query(default="score"),
    sort_order: str = Query(default="desc", pattern="^(asc|desc)$"),
    db: AsyncSession = Depends(get_db),
) -> PaginatedOpportunities:
    # Validate sort_by against the whitelist.  Anything else 400s rather
    # than silently falling back — the frontend dropdown should never
    # send a bad value.
    if sort_by not in _OPP_SORT_COLUMNS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"sort_by must be one of {sorted(_OPP_SORT_COLUMNS.keys())}, "
                f"got {sort_by!r}"
            ),
        )

    base = (
        select(SocialListeningOpportunity, SocialListeningPost, SocialListeningSearch.name)
        .join(SocialListeningPost, SocialListeningOpportunity.post_id == SocialListeningPost.id)
        .join(SocialListeningSearch, SocialListeningPost.search_id == SocialListeningSearch.id)
    )

    filters: list[Any] = []
    if search_id is not None:
        filters.append(SocialListeningPost.search_id == search_id)
    if status_filter is not None:
        filters.append(SocialListeningOpportunity.status == status_filter)
    if category is not None:
        filters.append(SocialListeningOpportunity.category == category)
    if source is not None:
        filters.append(SocialListeningPost.provider == source)
    if min_score is not None:
        filters.append(SocialListeningOpportunity.score >= min_score)
    if buying_signal is not None:
        filters.append(SocialListeningOpportunity.buying_signal == buying_signal)
    if date_from is not None:
        filters.append(SocialListeningPost.discovered_at >= date_from)
    if date_to is not None:
        filters.append(SocialListeningPost.discovered_at <= date_to)

    where = and_(*filters) if filters else None
    if where is not None:
        base = base.where(where)

    total_q = (
        select(func.count())
        .select_from(SocialListeningOpportunity)
        .join(SocialListeningPost, SocialListeningOpportunity.post_id == SocialListeningPost.id)
    )
    if where is not None:
        total_q = total_q.where(where)
    total = (await db.execute(total_q)).scalar_one() or 0

    primary_col = _OPP_SORT_COLUMNS[sort_by]
    primary = primary_col.asc() if sort_order == "asc" else primary_col.desc()
    # Tiebreaker keeps result order stable when many rows share the
    # primary key (e.g. sorting by score, dozens of 5s).  ``discovered_at``
    # DESC is the natural secondary; only swap it out when discovered_at
    # IS the primary (in which case score becomes the tiebreaker).
    if sort_by == "discovered_at":
        secondary = SocialListeningOpportunity.score.desc()
    else:
        secondary = SocialListeningPost.discovered_at.desc()

    q = (
        base.order_by(primary, secondary)
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    rows = (await db.execute(q)).all()

    items = [
        _opportunity_summary(o, p, search_name) for (o, p, search_name) in rows
    ]
    return PaginatedOpportunities(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) if total else 0,
    )


@router.patch("/opportunities/{opp_id}", response_model=SocialListeningOpportunitySummary)
async def update_opportunity(
    opp_id: uuid.UUID,
    payload: SocialListeningOpportunityUpdate,
    db: AsyncSession = Depends(get_db),
) -> SocialListeningOpportunitySummary:
    o = await _get_opportunity_or_404(db, opp_id)
    updates = payload.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(o, key, value)
    # Always stamp the row so updated_at reflects the action.
    o.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(o)

    p = await db.get(SocialListeningPost, o.post_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Post not found")
    s = await db.get(SocialListeningSearch, p.search_id)
    return _opportunity_summary(o, p, s.name if s else None)
