"""Phase 40 — ICP lookalike expansion (Feature D).

- build_icp_from_won requires the min-deal threshold (insufficient_data
  below it) and produces structured criteria from mocked deals + LLM.
- find_lookalikes dedups against existing leads/opps/candidates, falls
  back to web research when Apollo search is unavailable, and scores
  with reasons (rule-based, deterministic).
- Accept creates exactly ONE campaign-less lead and links
  created_lead_id; re-accept 409s; reject creates nothing.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models import (
    IcpProfile,
    IcpProfileSource,
    IcpProfileStatus,
    Lead,
    LookalikeCandidate,
    LookalikeCandidateStatus,
    Opportunity,
    OpportunityStage,
)
from app.services import icp_builder, lookalike_discovery
from app.services.lookalike_discovery import Candidate, score_candidate

pytestmark = pytest.mark.asyncio


async def _won_deal(db_session, company="Acme", title="CFO", email=None) -> Opportunity:
    o = Opportunity(
        name=f"{company} deal", stage=OpportunityStage.CLOSED_WON,
        company=company, job_title=title, email=email,
    )
    db_session.add(o)
    await db_session.commit()
    return o


def _fake_message(text: str):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=500, output_tokens=200, server_tool_use=None),
    )


_CRITERIA_JSON = (
    '{"industries": ["managed IT services"], "employee_count_band": "20-200", '
    '"title_patterns": ["CFO", "VP Finance"], "geographies": ["US Northeast"], '
    '"funding_stages": [], "keywords": ["compliance"]}'
)


# --------------------------------------------------------------------------
# ICP builder
# --------------------------------------------------------------------------


async def test_build_icp_insufficient_below_threshold(db_session):
    await _won_deal(db_session)
    await _won_deal(db_session, company="Beta")

    create_mock = AsyncMock()
    fake_client = SimpleNamespace(messages=SimpleNamespace(create=create_mock))
    with patch("app.services.icp_builder.get_client", return_value=fake_client):
        profile = await icp_builder.build_icp_from_won(db_session)
        await db_session.commit()

    assert profile.status is IcpProfileStatus.INSUFFICIENT_DATA
    assert profile.won_deal_count == 2
    create_mock.assert_not_awaited()  # no LLM spend below the threshold


async def test_build_icp_produces_structured_criteria(db_session):
    for company in ("Acme", "Beta", "Gamma"):
        await _won_deal(db_session, company=company)

    fake_client = SimpleNamespace(messages=SimpleNamespace(
        create=AsyncMock(return_value=_fake_message(_CRITERIA_JSON))
    ))
    with patch("app.services.icp_builder.get_client", return_value=fake_client):
        profile = await icp_builder.build_icp_from_won(db_session)
        await db_session.commit()

    assert profile.status is IcpProfileStatus.READY
    assert profile.criteria["industries"] == ["managed IT services"]
    assert profile.criteria["employee_count_band"] == "20-200"
    assert profile.criteria["_meta"]["cost_usd"] > 0
    assert profile.won_deal_count == 3

    # Re-run updates the SAME auto profile (no duplicates).
    with patch("app.services.icp_builder.get_client", return_value=fake_client):
        await icp_builder.build_icp_from_won(db_session)
        await db_session.commit()
    rows = (await db_session.execute(select(IcpProfile))).scalars().all()
    assert len(rows) == 1


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------


async def test_score_candidate_rules():
    criteria = {
        "industries": ["managed it services"],
        "employee_count_band": "20-200",
        "title_patterns": ["CFO"],
        "funding_stages": ["series_a"],
        "keywords": ["compliance"],
    }
    strong = Candidate(
        company="FitCo", job_title="CFO",
        raw={"industry": "Managed IT Services", "employee_count": 80,
             "funding_stage": "series_a"},
    )
    score, reason = score_candidate(strong, criteria)
    assert score == 90  # 30 + 25 + 25 + 10 (no keyword in blob)
    assert "industry match" in reason and "title match" in reason

    weak = Candidate(company="MissCo", raw={})
    score, reason = score_candidate(weak, criteria)
    assert score == 0
    assert "weak fit" in reason


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------


async def _ready_profile(db_session) -> IcpProfile:
    p = IcpProfile(
        name="Auto", source=IcpProfileSource.AUTO_CLOSED_WON,
        status=IcpProfileStatus.READY,
        criteria={
            "industries": ["managed it services"],
            "employee_count_band": "20-200",
            "title_patterns": ["CFO"],
            "funding_stages": [], "keywords": [],
        },
        won_deal_count=3,
    )
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


async def test_find_lookalikes_dedups_against_pipeline(db_session):
    profile = await _ready_profile(db_session)
    # Existing lead at fitco.com — must NOT resurface.
    db_session.add(Lead(campaign_id=None, email="cfo@fitco.com", company="FitCo"))
    await db_session.commit()

    people = [
        {"company": "FitCo", "company_website": "https://www.fitco.com",
         "contact_name": "A B", "job_title": "CFO", "email": "cfo@fitco.com",
         "industry": "managed it services", "employee_count": 80,
         "linkedin_url": None, "funding_stage": None},
        {"company": "FreshCo", "company_website": "https://freshco.io",
         "contact_name": "C D", "job_title": "CFO", "email": None,
         "industry": "managed it services", "employee_count": 50,
         "linkedin_url": None, "funding_stage": None},
    ]
    with patch(
        "app.services.lookalike_discovery.apollo.search_people",
        new=AsyncMock(return_value=people),
    ):
        out = await lookalike_discovery.find_lookalikes(db_session, profile)

    companies = [c.company for c in out]
    assert companies == ["FreshCo"]          # FitCo dedup'd by domain
    assert out[0].fit_score > 0
    assert out[0].fit_reason


async def test_find_lookalikes_web_fallback_when_apollo_dark(db_session):
    profile = await _ready_profile(db_session)
    web_rows = [Candidate(company="WebCo", company_website="https://webco.com",
                          source="web_research",
                          raw={"industry": "managed it services", "employee_count": 30})]
    with patch(
        "app.services.lookalike_discovery.apollo.search_people",
        new=AsyncMock(return_value=[]),
    ), patch(
        "app.services.lookalike_discovery.apollo.search_organizations",
        new=AsyncMock(return_value=[]),
    ), patch(
        "app.services.lookalike_discovery._web_fallback",
        new=AsyncMock(return_value=web_rows),
    ) as web_mock:
        out = await lookalike_discovery.find_lookalikes(db_session, profile)
    web_mock.assert_awaited_once()
    assert [c.company for c in out] == ["WebCo"]
    assert out[0].fit_score >= 55  # industry + employee band


# --------------------------------------------------------------------------
# accept / reject (router)
# --------------------------------------------------------------------------


async def _staged_candidate(db_session, **kw) -> LookalikeCandidate:
    profile = await _ready_profile(db_session)
    defaults = dict(
        icp_profile_id=profile.id, company="FreshCo",
        company_website="https://freshco.io", contact_name="Casey Doe",
        job_title="CFO", email="casey@freshco.io",
        fit_score=80, fit_reason="industry match",
        dedup_key=f"freshco-{uuid.uuid4().hex[:6]}.io",
    )
    defaults.update(kw)
    c = LookalikeCandidate(**defaults)
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    return c


async def test_accept_creates_one_campaignless_lead(client, db_session):
    c = await _staged_candidate(db_session)
    resp = await client.post(f"/icp/candidates/{c.id}/accept")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["created_lead_id"] is not None

    lead = await db_session.get(Lead, uuid.UUID(body["created_lead_id"]))
    assert lead.campaign_id is None          # never auto-added to a campaign
    assert lead.email == "casey@freshco.io"
    assert lead.first_name == "Casey"
    assert lead.company == "FreshCo"

    # Re-accept → 409, still exactly one lead.
    resp = await client.post(f"/icp/candidates/{c.id}/accept")
    assert resp.status_code == 409
    leads = (await db_session.execute(select(Lead))).scalars().all()
    assert len(leads) == 1


async def test_reject_creates_nothing(client, db_session):
    c = await _staged_candidate(db_session)
    resp = await client.post(f"/icp/candidates/{c.id}/reject")
    assert resp.json()["status"] == "rejected"
    assert (await db_session.execute(select(Lead))).scalars().all() == []


async def test_candidates_list_sorted_by_fit(client, db_session):
    await _staged_candidate(db_session, company="LowCo", fit_score=20)
    await _staged_candidate(db_session, company="HighCo", fit_score=95)
    resp = await client.get("/icp/candidates?status=new")
    items = resp.json()["items"]
    assert [i["company"] for i in items] == ["HighCo", "LowCo"]


async def test_profile_endpoint_missing_then_regenerate(client, db_session):
    resp = await client.get("/icp/profile")
    assert resp.json()["status"] == "missing"
    assert resp.json()["min_won_deals"] == icp_builder.MIN_WON_DEALS

    # Regenerate with too few deals → insufficient_data.
    resp = await client.post("/icp/profile/regenerate")
    assert resp.json()["status"] == "insufficient_data"
