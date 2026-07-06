"""HTTP-layer tests for the Social Listening Radar router.

CRUD on searches; opportunity feed pagination + filters; PATCH
opportunity; cascade delete; expand-preview synchronous.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models import (
    SocialListeningOpportunity,
    SocialListeningPost,
    SocialListeningSearch,
    SocialOpportunityAction,
    SocialOpportunityCategory,
    SocialOpportunityStatus,
    SocialPostProvider,
    SocialSearchFrequency,
    SocialSearchStatus,
)
from app.services import _anthropic


def _ant(body: str) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=body)])


@pytest.fixture(autouse=True)
def _reset_anthropic_client():
    _anthropic._client = None
    yield
    _anthropic._client = None


def _search_payload(**overrides) -> dict:
    base = {
        "name": "MSP intent radar",
        "topic": "frustrated with our IT provider",
        "niche": "mid-market east coast",
        "geography": "northeast US",
        "include_keywords": ["msp"],
        "exclude_keywords": [],
        "source": "linkedin",
        "frequency": "manual",
        "status": "active",
        "tone": "helpful",
        "sender_name": "Anthony",
        "max_queries_per_run": 15,
        "max_posts_per_query": 20,
        "max_qualified_per_run": 80,
    }
    base.update(overrides)
    return base


# ---- Create / list / detail / update / delete -------------------------------

async def test_create_search_returns_201_and_persists(client, db_session):
    # Patch out the celery enqueue so test doesn't depend on a broker.
    with patch("app.workers.social_listening.expand_social_topic.delay") as mock:
        resp = await client.post("/social-radar/searches", json=_search_payload())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "MSP intent radar"
    assert body["status"] == "active"
    assert body["max_queries_per_run"] == 15
    assert body["expanded_query_count"] == 0
    # Expansion task should have been queued.
    mock.assert_called_once()


async def test_create_search_rejects_blank_name(client):
    resp = await client.post("/social-radar/searches", json=_search_payload(name="   "))
    assert resp.status_code == 422


async def test_list_searches_paginates_and_filters(client, db_session):
    # Seed 3 searches with different statuses.
    db_session.add_all([
        SocialListeningSearch(name="A", topic="t", status=SocialSearchStatus.ACTIVE),
        SocialListeningSearch(name="B", topic="t", status=SocialSearchStatus.PAUSED),
        SocialListeningSearch(name="C MSP help", topic="t", status=SocialSearchStatus.ACTIVE),
    ])
    await db_session.commit()

    # Status filter.
    resp = await client.get("/social-radar/searches", params={"status": "active"})
    assert resp.status_code == 200
    assert resp.json()["total"] == 2

    # Search filter (matches A and "C MSP help").
    resp = await client.get("/social-radar/searches", params={"search": "MSP"})
    assert resp.status_code == 200
    names = {row["name"] for row in resp.json()["items"]}
    assert "C MSP help" in names


async def test_get_search_returns_expanded_queries_and_counts(client, db_session):
    s = SocialListeningSearch(
        name="X", topic="t",
        expanded_queries=["a", "b", "c"],
    )
    db_session.add(s)
    await db_session.commit()
    sid = s.id

    # Add a post + opportunity so the counts populate.
    post = SocialListeningPost(
        search_id=sid, post_url="https://www.linkedin.com/posts/p-activity-1",
        post_text="hello",
    )
    db_session.add(post)
    await db_session.flush()
    db_session.add(SocialListeningOpportunity(
        post_id=post.id, score=8,
        category=SocialOpportunityCategory.MSP,
    ))
    await db_session.commit()

    resp = await client.get(f"/social-radar/searches/{sid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["expanded_queries"] == ["a", "b", "c"]
    assert body["expanded_query_count"] == 3
    assert body["post_count"] == 1
    assert body["opportunity_count"] == 1


async def test_patch_search_topic_change_re_enqueues_expansion(client, db_session):
    s = SocialListeningSearch(name="X", topic="old topic")
    db_session.add(s)
    await db_session.commit()
    sid = s.id

    with patch("app.workers.social_listening.expand_social_topic.delay") as mock:
        resp = await client.patch(
            f"/social-radar/searches/{sid}",
            json={"topic": "brand new topic"},
        )
    assert resp.status_code == 200
    assert resp.json()["topic"] == "brand new topic"
    mock.assert_called_once_with(str(sid))


async def test_create_and_update_respect_max_post_age_days(client, db_session):
    """The lookback window is editable at create AND PATCH time."""
    with patch("app.workers.social_listening.expand_social_topic.delay"):
        resp = await client.post(
            "/social-radar/searches",
            json=_search_payload(max_post_age_days=14),
        )
    assert resp.status_code == 201
    assert resp.json()["max_post_age_days"] == 14
    sid = resp.json()["id"]

    # PATCH to a different value.
    resp = await client.patch(
        f"/social-radar/searches/{sid}",
        json={"max_post_age_days": 90},
    )
    assert resp.status_code == 200
    assert resp.json()["max_post_age_days"] == 90


async def test_create_search_defaults_sources_to_linkedin_and_reddit(client):
    """Creating a search without specifying ``sources`` defaults to
    [linkedin, reddit] — Twitter dropped because Anthropic web_search
    can't reach it and the X API v2 is paid-only."""
    with patch("app.workers.social_listening.expand_social_topic.delay"):
        resp = await client.post(
            "/social-radar/searches",
            json={k: v for k, v in _search_payload().items() if k != "sources"},
        )
    assert resp.status_code == 201
    assert set(resp.json()["sources"]) == {"linkedin", "reddit"}


async def test_create_and_update_can_narrow_sources(client, db_session):
    """User can pick a subset of sources at create + PATCH time."""
    payload = _search_payload(sources=["reddit"])
    with patch("app.workers.social_listening.expand_social_topic.delay"):
        resp = await client.post("/social-radar/searches", json=payload)
    assert resp.status_code == 201
    assert resp.json()["sources"] == ["reddit"]
    sid = resp.json()["id"]

    resp = await client.patch(
        f"/social-radar/searches/{sid}",
        json={"sources": ["reddit", "twitter"]},
    )
    assert resp.status_code == 200
    assert set(resp.json()["sources"]) == {"reddit", "twitter"}


async def test_default_max_post_age_days_is_30(client):
    """Create without specifying max_post_age_days defaults to 30."""
    with patch("app.workers.social_listening.expand_social_topic.delay"):
        resp = await client.post(
            "/social-radar/searches",
            json={k: v for k, v in _search_payload().items() if k != "max_post_age_days"},
        )
    assert resp.status_code == 201
    assert resp.json()["max_post_age_days"] == 30


async def test_patch_search_can_edit_expanded_queries_without_reexpand(client, db_session):
    """User can hand-edit the AI-generated phrase list to remove junk and
    add their own.  This MUST NOT trigger a new expansion (which would
    overwrite their edits)."""
    s = SocialListeningSearch(
        name="X", topic="t",
        expanded_queries=["internet", "phone", "frustrated with our msp"],
    )
    db_session.add(s)
    await db_session.commit()

    new_list = [
        "frustrated with our msp",
        "looking for a new phone system for hybrid team",
        "anyone else fed up with comcast outages",
    ]
    with patch("app.workers.social_listening.expand_social_topic.delay") as expand_mock:
        resp = await client.patch(
            f"/social-radar/searches/{s.id}",
            json={"expanded_queries": new_list},
        )
    assert resp.status_code == 200
    assert resp.json()["expanded_queries"] == new_list
    # Topic didn't change → no re-expansion.
    expand_mock.assert_not_called()


async def test_patch_search_other_fields_does_not_reexpand(client, db_session):
    s = SocialListeningSearch(name="X", topic="t")
    db_session.add(s)
    await db_session.commit()
    sid = s.id

    with patch("app.workers.social_listening.expand_social_topic.delay") as mock:
        resp = await client.patch(
            f"/social-radar/searches/{sid}",
            json={"tone": "warm", "max_queries_per_run": 50},
        )
    assert resp.status_code == 200
    mock.assert_not_called()
    assert resp.json()["tone"] == "warm"
    assert resp.json()["max_queries_per_run"] == 50


async def test_cleanup_stale_deletes_old_and_undated_posts(client, db_session):
    """The cleanup endpoint removes posts that don't pass the search's
    lookback window — older than max_post_age_days, OR no date at all
    (since we can't verify freshness).  Cascades to opportunities."""
    from datetime import datetime, timedelta, timezone

    s = SocialListeningSearch(name="X", topic="t", max_post_age_days=30)
    db_session.add(s)
    await db_session.flush()
    sid = s.id

    now = datetime.now(timezone.utc)
    fresh = SocialListeningPost(
        search_id=sid, post_url="https://www.linkedin.com/posts/fresh-1",
        post_text="recent", post_date=now - timedelta(days=5),
    )
    stale = SocialListeningPost(
        search_id=sid, post_url="https://www.linkedin.com/posts/stale-1",
        post_text="old", post_date=now - timedelta(days=120),
    )
    undated = SocialListeningPost(
        search_id=sid, post_url="https://www.linkedin.com/posts/undated-1",
        post_text="no date", post_date=None,
    )
    db_session.add_all([fresh, stale, undated])
    await db_session.flush()
    # Add an opportunity on the stale post — cleanup must cascade.
    db_session.add(SocialListeningOpportunity(post_id=stale.id, score=4))
    await db_session.commit()

    resp = await client.post(f"/social-radar/searches/{sid}/cleanup-stale")
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] == 2  # stale + undated

    # Only the fresh post remains.
    remaining = (await db_session.execute(
        select(SocialListeningPost).where(SocialListeningPost.search_id == sid)
    )).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].post_url.endswith("fresh-1")

    # Opportunity on the stale post was cascaded.
    opps_left = (await db_session.execute(
        select(SocialListeningOpportunity)
    )).scalars().all()
    assert opps_left == []


async def test_cleanup_stale_404_for_unknown_search(client):
    import uuid as _uuid
    resp = await client.post(f"/social-radar/searches/{_uuid.uuid4()}/cleanup-stale")
    assert resp.status_code == 404


async def test_requalify_all_dispatches_batches_for_existing_posts(client, db_session):
    """The requalify endpoint enqueues batch-qualify tasks for every
    post in the search.  Used after tightening the scoring rubric to
    rescore the whole backlog cheaply."""
    s = SocialListeningSearch(name="X", topic="t")
    db_session.add(s)
    await db_session.flush()
    # 25 posts → 3 batches at QUALIFY_BATCH_SIZE=10 (10, 10, 5).
    for i in range(25):
        db_session.add(SocialListeningPost(
            search_id=s.id,
            post_url=f"https://www.reddit.com/r/sysadmin/comments/p{i}/x/",
            post_text=f"post {i}",
        ))
    await db_session.commit()

    with patch("app.workers.social_listening.qualify_social_posts_batch.delay") as mock:
        resp = await client.post(f"/social-radar/searches/{s.id}/requalify-all")
    assert resp.status_code == 200
    assert resp.json()["enqueued"] == 25
    assert resp.json()["batches"] == 3
    assert mock.call_count == 3
    # First batch has 10 ids.
    first_call_ids = mock.call_args_list[0].args[0]
    assert len(first_call_ids) == 10


async def test_requalify_all_returns_zero_when_no_posts(client, db_session):
    """Empty search → no batches, no calls."""
    s = SocialListeningSearch(name="X", topic="t")
    db_session.add(s)
    await db_session.commit()

    with patch("app.workers.social_listening.qualify_social_posts_batch.delay") as mock:
        resp = await client.post(f"/social-radar/searches/{s.id}/requalify-all")
    assert resp.status_code == 200
    assert resp.json() == {"enqueued": 0, "batches": 0}
    mock.assert_not_called()


async def test_cleanup_stale_returns_zero_when_nothing_stale(client, db_session):
    """No-op cleanup on a fresh search returns deleted=0 cleanly."""
    s = SocialListeningSearch(name="X", topic="t", max_post_age_days=30)
    db_session.add(s)
    await db_session.commit()
    resp = await client.post(f"/social-radar/searches/{s.id}/cleanup-stale")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 0


async def test_delete_search_cascades_to_posts_and_opportunities(client, db_session):
    s = SocialListeningSearch(name="X", topic="t")
    db_session.add(s)
    await db_session.flush()
    p = SocialListeningPost(
        search_id=s.id,
        post_url="https://www.linkedin.com/posts/p1",
        post_text="x",
    )
    db_session.add(p)
    await db_session.flush()
    db_session.add(SocialListeningOpportunity(post_id=p.id, score=5))
    await db_session.commit()

    resp = await client.delete(f"/social-radar/searches/{s.id}")
    assert resp.status_code == 204

    # All three rows gone.
    resp = await client.get(f"/social-radar/searches/{s.id}")
    assert resp.status_code == 404
    resp = await client.get("/social-radar/opportunities")
    assert resp.json()["total"] == 0


async def test_run_search_409s_when_search_is_paused(client, db_session):
    """A paused or archived search can't be manually run.  Without this
    gate the worker would dispatch, bail with skipped, and flip
    last_run_status=pending for no reason — costing a task slot."""
    s = SocialListeningSearch(
        name="X", topic="t", status=SocialSearchStatus.PAUSED,
    )
    db_session.add(s)
    await db_session.commit()

    resp = await client.post(f"/social-radar/searches/{s.id}/run")
    assert resp.status_code == 409
    assert "paused" in resp.json()["detail"].lower()


async def test_run_search_409s_when_already_running(client, db_session):
    s = SocialListeningSearch(name="X", topic="t", last_run_status="running")
    db_session.add(s)
    await db_session.commit()

    resp = await client.post(f"/social-radar/searches/{s.id}/run")
    assert resp.status_code == 409


async def test_run_search_queues_task_and_marks_pending(client, db_session):
    s = SocialListeningSearch(name="X", topic="t")
    db_session.add(s)
    await db_session.commit()
    sid = s.id

    with patch("app.workers.social_listening.run_social_search.delay") as mock:
        mock.return_value = SimpleNamespace(id="task-123")
        resp = await client.post(f"/social-radar/searches/{sid}/run")
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    assert resp.json()["task_id"] == "task-123"
    mock.assert_called_once_with(str(sid))


# ---- Expand preview (sync) --------------------------------------------------

async def test_expand_preview_ad_hoc_returns_queries(client):
    mock = AsyncMock(return_value=_ant('["frustrated with our msp service", "looking for new it provider"]'))
    from app.services import social_listening_topic_expander
    with patch.object(social_listening_topic_expander, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=mock))):
        resp = await client.post(
            "/social-radar/expand-preview",
            json={"topic": "frustrated with technology", "max_queries": 5},
        )
    assert resp.status_code == 200
    assert resp.json()["queries"] == ["frustrated with our msp service", "looking for new it provider"]


async def test_expand_preview_for_search_uses_saved_row(client, db_session):
    s = SocialListeningSearch(
        name="X", topic="frustrated with our IT provider",
        # include_keywords need 3+ words to pass the filter (same as
        # AI-generated phrases) — single-word "msp" would drop here.
        niche="nonprofits", include_keywords=["our msp slow response"],
    )
    db_session.add(s)
    await db_session.commit()

    captured = {}

    async def _spy(**kwargs):
        captured["kwargs"] = kwargs
        return _ant('["msp slow response time"]')

    from app.services import social_listening_topic_expander
    with patch.object(social_listening_topic_expander, "get_client",
                      return_value=SimpleNamespace(messages=SimpleNamespace(create=_spy))):
        resp = await client.post(f"/social-radar/searches/{s.id}/expand-preview")
    assert resp.status_code == 200
    assert "our msp slow response" in resp.json()["queries"]
    # The prompt should reflect the saved niche.
    assert "nonprofits" in captured["kwargs"]["messages"][0]["content"]


# ---- Opportunities feed -----------------------------------------------------

async def _seed_opportunities(db_session, n=3):
    s = SocialListeningSearch(name="X", topic="t")
    db_session.add(s)
    await db_session.flush()
    for i in range(n):
        p = SocialListeningPost(
            search_id=s.id,
            post_url=f"https://www.linkedin.com/posts/p-activity-{i}",
            post_text=f"post {i}",
        )
        db_session.add(p)
        await db_session.flush()
        db_session.add(SocialListeningOpportunity(
            post_id=p.id,
            score=3 + i * 2,  # 3, 5, 7
            category=SocialOpportunityCategory.MSP if i == 0 else SocialOpportunityCategory.GENERAL_ADVISORY,
            buying_signal=(i == 2),
            status=SocialOpportunityStatus.NEW,
            recommended_action=SocialOpportunityAction.COMMENT,
        ))
    await db_session.commit()
    return s


async def test_opportunities_list_sorts_by_score_desc(client, db_session):
    await _seed_opportunities(db_session, n=3)
    resp = await client.get("/social-radar/opportunities")
    assert resp.status_code == 200
    scores = [it["score"] for it in resp.json()["items"]]
    assert scores == [7, 5, 3]


async def test_opportunities_filters(client, db_session):
    await _seed_opportunities(db_session, n=3)

    resp = await client.get("/social-radar/opportunities", params={"min_score": 5})
    assert resp.json()["total"] == 2

    resp = await client.get(
        "/social-radar/opportunities", params={"category": "msp"},
    )
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["category"] == "msp"

    resp = await client.get(
        "/social-radar/opportunities", params={"buying_signal": "true"},
    )
    assert resp.json()["total"] == 1


async def test_opportunities_includes_embedded_post_and_search_name(client, db_session):
    s = await _seed_opportunities(db_session, n=1)
    resp = await client.get("/social-radar/opportunities")
    item = resp.json()["items"][0]
    assert item["search_name"] == s.name
    assert "post" in item
    assert item["post"]["post_url"].startswith("https://www.linkedin.com/")


async def test_patch_opportunity_updates_status_and_notes(client, db_session):
    s = await _seed_opportunities(db_session, n=1)
    opp_id = (await client.get("/social-radar/opportunities")).json()["items"][0]["id"]

    resp = await client.patch(
        f"/social-radar/opportunities/{opp_id}",
        json={"status": "saved", "notes": "follow up next week"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "saved"
    assert resp.json()["notes"] == "follow up next week"


async def test_patch_opportunity_404_for_unknown(client):
    resp = await client.patch(
        f"/social-radar/opportunities/{uuid.uuid4()}", json={"status": "saved"},
    )
    assert resp.status_code == 404


# ---- Opportunities feed: source filter + sort controls ----------------------

async def _seed_opportunities_mixed_sources(db_session):
    """Three opportunities across LinkedIn / Reddit / Twitter with
    distinct scores + discovered timestamps so sort orderings are
    visible in test assertions."""
    from datetime import datetime, timedelta, timezone

    s = SocialListeningSearch(name="X", topic="t")
    db_session.add(s)
    await db_session.flush()

    now = datetime.now(timezone.utc)
    spec = [
        # (provider, score, days_ago)
        ("linkedin", 8, 1),  # newest, mid score
        ("reddit",   3, 5),  # oldest, lowest score
        ("twitter",  6, 3),  # middle on both
    ]
    posts = []
    for i, (provider, _score, days_ago) in enumerate(spec):
        p = SocialListeningPost(
            search_id=s.id, provider=provider,
            post_url=f"https://example.test/{provider}/{i}",
            post_text=f"post {i}",
            discovered_at=now - timedelta(days=days_ago),
        )
        db_session.add(p)
        posts.append(p)
        await db_session.flush()
    for p, (_provider, score, _days_ago) in zip(posts, spec):
        db_session.add(SocialListeningOpportunity(
            post_id=p.id, score=score,
            category=SocialOpportunityCategory.GENERAL_ADVISORY,
            status=SocialOpportunityStatus.NEW,
            recommended_action=SocialOpportunityAction.COMMENT,
        ))
    await db_session.commit()
    return s


async def test_opportunities_filters_by_source(client, db_session):
    """The new ``source`` query param scopes the feed to one provider."""
    await _seed_opportunities_mixed_sources(db_session)

    resp = await client.get("/social-radar/opportunities", params={"source": "reddit"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["post"]["provider"] == "reddit"

    resp = await client.get("/social-radar/opportunities", params={"source": "linkedin"})
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["post"]["provider"] == "linkedin"

    # No filter → all three.
    resp = await client.get("/social-radar/opportunities")
    assert resp.json()["total"] == 3


async def test_opportunities_source_filter_rejects_unknown_value(client, db_session):
    await _seed_opportunities_mixed_sources(db_session)
    resp = await client.get("/social-radar/opportunities", params={"source": "facebook"})
    # FastAPI 422 on enum validation failure — protects against silent
    # filter-on-typo-returns-everything bugs.
    assert resp.status_code == 422


async def test_opportunities_sort_by_score_asc(client, db_session):
    await _seed_opportunities_mixed_sources(db_session)
    resp = await client.get(
        "/social-radar/opportunities",
        params={"sort_by": "score", "sort_order": "asc"},
    )
    scores = [it["score"] for it in resp.json()["items"]]
    assert scores == [3, 6, 8]


async def test_opportunities_sort_by_score_desc_is_default(client, db_session):
    """No sort params → matches the documented default (score DESC).
    Locks the default behaviour against accidental changes."""
    await _seed_opportunities_mixed_sources(db_session)
    resp = await client.get("/social-radar/opportunities")
    scores = [it["score"] for it in resp.json()["items"]]
    assert scores == [8, 6, 3]


async def test_opportunities_sort_by_discovered_at_desc(client, db_session):
    await _seed_opportunities_mixed_sources(db_session)
    resp = await client.get(
        "/social-radar/opportunities",
        params={"sort_by": "discovered_at", "sort_order": "desc"},
    )
    # Seed: linkedin (1 day ago, newest), twitter (3 days), reddit (5 days, oldest).
    providers = [it["post"]["provider"] for it in resp.json()["items"]]
    assert providers == ["linkedin", "twitter", "reddit"]


async def test_opportunities_sort_by_discovered_at_asc(client, db_session):
    await _seed_opportunities_mixed_sources(db_session)
    resp = await client.get(
        "/social-radar/opportunities",
        params={"sort_by": "discovered_at", "sort_order": "asc"},
    )
    providers = [it["post"]["provider"] for it in resp.json()["items"]]
    assert providers == ["reddit", "twitter", "linkedin"]


async def test_opportunities_sort_by_updated_at(client, db_session):
    """``updated_at`` keys off the opportunity row's updated_at — the
    same timestamp that bumps when the user changes status/notes.
    Seeded rows are committed in order so they're ordered the same way
    by both score and updated_at; that's enough to verify the column
    is honoured (no 500, no silent fallback)."""
    await _seed_opportunities_mixed_sources(db_session)
    resp = await client.get(
        "/social-radar/opportunities",
        params={"sort_by": "updated_at", "sort_order": "desc"},
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 3


async def test_opportunities_sort_by_rejects_unknown_key(client, db_session):
    await _seed_opportunities_mixed_sources(db_session)
    resp = await client.get(
        "/social-radar/opportunities",
        params={"sort_by": "title"},
    )
    assert resp.status_code == 400
    assert "sort_by" in resp.json()["detail"]


async def test_opportunities_sort_order_rejects_unknown_value(client, db_session):
    await _seed_opportunities_mixed_sources(db_session)
    resp = await client.get(
        "/social-radar/opportunities",
        params={"sort_order": "sideways"},
    )
    # Pattern-validated by FastAPI → 422.
    assert resp.status_code == 422


async def test_opportunities_source_and_sort_compose(client, db_session):
    """Source filter and sort_by play nicely together — selecting reddit
    + score-asc returns only Reddit posts in score-asc order."""
    await _seed_opportunities_mixed_sources(db_session)
    # Add one more reddit row for an actual ordering test.
    s = (await db_session.execute(select(SocialListeningSearch))).scalars().first()
    p2 = SocialListeningPost(
        search_id=s.id, provider="reddit",
        post_url="https://example.test/reddit/extra",
        post_text="extra",
    )
    db_session.add(p2)
    await db_session.flush()
    db_session.add(SocialListeningOpportunity(
        post_id=p2.id, score=9,
        category=SocialOpportunityCategory.GENERAL_ADVISORY,
        status=SocialOpportunityStatus.NEW,
        recommended_action=SocialOpportunityAction.COMMENT,
    ))
    await db_session.commit()

    resp = await client.get(
        "/social-radar/opportunities",
        params={"source": "reddit", "sort_by": "score", "sort_order": "asc"},
    )
    body = resp.json()
    assert body["total"] == 2
    assert [it["score"] for it in body["items"]] == [3, 9]
    assert all(it["post"]["provider"] == "reddit" for it in body["items"])
