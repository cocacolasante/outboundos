"""Phase 34: cross-campaign research cache (lookup / upsert / TTL)."""
from datetime import datetime, timedelta, timezone

from app.models import ResearchCache
from app.services import research_cache


async def test_lookup_missing_returns_none(db_session):
    assert await research_cache.lookup(db_session, "nobody@x.com") is None


async def test_upsert_then_lookup_roundtrips_lowercased(db_session):
    await research_cache.upsert(
        db_session, "Jane@Example.COM",
        {"quality": "rich", "person_news": ["raised B"], "company_description": "AI"},
    )
    await db_session.commit()
    # Lookup is case-insensitive (key is lowercased).
    hit = await research_cache.lookup(db_session, "JANE@example.com")
    assert hit is not None
    assert hit["quality"] == "rich"
    assert hit["person_news"] == ["raised B"]


async def test_upsert_refreshes_existing_row(db_session):
    await research_cache.upsert(db_session, "x@y.com", {"quality": "low"})
    await db_session.commit()
    await research_cache.upsert(db_session, "x@y.com", {"quality": "rich", "person_news": ["x"]})
    await db_session.commit()
    hit = await research_cache.lookup(db_session, "x@y.com")
    assert hit["quality"] == "rich"
    assert hit["person_news"] == ["x"]


async def test_lookup_returns_none_when_stale(db_session, monkeypatch):
    # TTL = 1 day; row is 2 days old → miss.
    from app.services import research_cache as rc_mod
    monkeypatch.setattr(rc_mod.settings, "RESEARCH_CACHE_TTL_DAYS", 1)
    row = ResearchCache(
        email="old@x.com",
        research_data={"quality": "rich"},
        refreshed_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    db_session.add(row)
    await db_session.commit()
    assert await research_cache.lookup(db_session, "old@x.com") is None


async def test_upsert_noop_for_empty_inputs(db_session):
    # Empty email / payload shouldn't write a row.
    await research_cache.upsert(db_session, "", {"quality": "rich"})
    await research_cache.upsert(db_session, "x@y.com", {})
    await db_session.commit()
    assert await research_cache.lookup(db_session, "") is None
    assert await research_cache.lookup(db_session, "x@y.com") is None
