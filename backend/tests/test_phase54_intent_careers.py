"""Phase 6 Track 2 — careers-page dev-role enrichment over warm orgs.

Covers URL/robots helpers, the LLM extractor (Anthropic mocked), and the
collector end-to-end (fetch + extract mocked): warm-org selection, emit on a
hit, no_site / no_role, and idempotency.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.models import (
    IntentSignalSource, IntentSignalStatus, IntentSignalType, Org, OrgSizeBand,
    Signal,
)
from app.services.intent import collect_careers

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 6, 22, tzinfo=timezone.utc)


class _FakeBlock:
    type = "text"
    def __init__(self, text): self.text = text


class _FakeMsg:
    def __init__(self, text): self.content = [_FakeBlock(text)]


class _FakeMessages:
    def __init__(self, text): self._text = text
    async def create(self, **kw): return _FakeMsg(self._text)


class _FakeClient:
    def __init__(self, text): self.messages = _FakeMessages(text)


# ---- helpers ---------------------------------------------------------------

async def test_base_url_normalization():
    f = collect_careers._base_url
    assert f("example.org") == "https://example.org"
    assert f("https://www.example.org/careers") == "https://www.example.org"
    assert f(None) is None and f("") is None


async def test_extract_dev_role_found_and_not(monkeypatch):
    monkeypatch.setattr(collect_careers, "get_client",
                        lambda api_key=None: _FakeClient('{"found": true, "title": "Development Director"}'))
    got = await collect_careers.extract_dev_role("...careers text...", "Org")
    assert got == {"title": "Development Director"}

    monkeypatch.setattr(collect_careers, "get_client",
                        lambda api_key=None: _FakeClient('{"found": false, "title": null}'))
    assert await collect_careers.extract_dev_role("text", "Org") is None
    # Empty page → no LLM call, None.
    assert await collect_careers.extract_dev_role("   ", "Org") is None


# ---- collector -------------------------------------------------------------

async def _org(db_session, *, name, ein, website="https://helpful.org"):
    o = Org(name=name, ein=ein.replace("-", ""), state="WI", ntee_code="T31",
            size_band=OrgSizeBand.MID, website=website)
    db_session.add(o)
    await db_session.flush()
    return o


async def _warm_signal(db_session, org):
    db_session.add(Signal(
        org_id=org.id, signal_type=IntentSignalType.REV_DROP,
        source=IntentSignalSource.PROPUBLICA, score=70, event_date=NOW,
        evidence_url="u", summary="s", dedupe_key=f"rev:{org.id}",
        status=IntentSignalStatus.SCORED,
    ))
    await db_session.flush()


async def test_collect_for_org_emits_and_dedupes(db_session, monkeypatch):
    org = await _org(db_session, name="Helpful NP", ein="11-1111111")
    await db_session.commit()

    async def fake_fetch(base):
        return base + "/careers", "Open role: Development Director ..."
    monkeypatch.setattr(collect_careers, "_fetch_careers_text", fake_fetch)
    async def fake_extract(text, name):
        return {"title": "Development Director"}
    monkeypatch.setattr(collect_careers, "extract_dev_role", fake_extract)

    assert await collect_careers.collect_for_org(db_session, org, NOW) == "new"
    await db_session.commit()

    sig = await db_session.scalar(select(Signal).where(
        Signal.org_id == org.id, Signal.signal_type == IntentSignalType.DEV_ROLE_POSTED))
    assert sig.source is IntentSignalSource.JOBS
    assert sig.dedupe_key == f"careers:devrole:{org.id}:development-director"
    assert sig.evidence_url.endswith("/careers")
    assert "Development Director" in sig.summary

    # Same role re-detected → dedup.
    assert await collect_careers.collect_for_org(db_session, org, NOW) == "deduped"
    await db_session.commit()
    assert await db_session.scalar(select(func.count()).select_from(Signal).where(
        Signal.signal_type == IntentSignalType.DEV_ROLE_POSTED)) == 1


async def test_collect_for_org_no_site_and_no_role(db_session, monkeypatch):
    no_site = await _org(db_session, name="No Site", ein="22-2222222", website=None)
    await db_session.commit()
    assert await collect_careers.collect_for_org(db_session, no_site, NOW) == "no_site"

    org = await _org(db_session, name="Has Site", ein="33-3333333")
    await db_session.commit()
    async def fetch_none(base):
        return None
    monkeypatch.setattr(collect_careers, "_fetch_careers_text", fetch_none)
    assert await collect_careers.collect_for_org(db_session, org, NOW) == "no_role"


async def test_collect_runs_only_on_warm_orgs(db_session, monkeypatch):
    warm = await _org(db_session, name="Warm Org", ein="44-4444444")
    cold = await _org(db_session, name="Cold Org", ein="55-5555555")
    await _warm_signal(db_session, warm)   # only `warm` has a non-dev signal
    await db_session.commit()

    checked = []
    async def fake_fetch(base):
        return base + "/careers", "Development Director"
    async def fake_extract(text, name):
        checked.append(name)
        return {"title": "Development Director"}
    monkeypatch.setattr(collect_careers, "_fetch_careers_text", fake_fetch)
    monkeypatch.setattr(collect_careers, "extract_dev_role", fake_extract)

    counts = await collect_careers.collect_careers_dev_roles(db_session, now=NOW)
    assert counts["checked"] == 1 and counts["new"] == 1
    assert checked == ["Warm Org"]   # cold org never fetched
