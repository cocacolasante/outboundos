"""Phase 5 — ICP intent profiles (the GrantMind switch).

Covers profile CRUD + activation + presets, the per-type half-life override
wired into scoring, the read-only ranking, and the headline gate: GrantMind's
profile produces a DISTINCT, sensibly-ranked intent list vs a generic profile
over the same orgs + signals.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import (
    IcpIntentProfile, IntentSignalSource, IntentSignalStatus, IntentSignalType,
    Org, OrgSizeBand, Signal,
)
from app.services.intent import profiles as profiles_svc
from app.services.intent import scoring

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 6, 22, tzinfo=timezone.utc)


# ---- profile management ---------------------------------------------------

async def test_create_activates_and_only_one_active(db_session):
    a = await profiles_svc.create_from_preset(db_session, "generic")
    b = await profiles_svc.create_from_preset(db_session, "grantmind")
    await db_session.commit()
    # Creating b (active=True) deactivated a.
    active = await scoring.get_active_profile(db_session)
    assert active.id == b.id and active.name == "GrantMind Pro"
    await db_session.refresh(a)
    assert a.is_active is False
    # GrantMind preset carries the tilt.
    assert b.signal_weights["dev_role_posted"] == 1.6
    assert b.size_band_weights["small"] == 1.4
    assert "nonprofit capacity building" in b.rfp_keywords


async def test_activate_and_update(db_session):
    a = await profiles_svc.create_from_preset(db_session, "generic")
    b = await profiles_svc.create_from_preset(db_session, "grantmind")
    await db_session.commit()
    await profiles_svc.activate_profile(db_session, a, tenant_id=None)
    await db_session.commit()
    assert (await scoring.get_active_profile(db_session)).id == a.id

    await profiles_svc.update_profile(db_session, a, {"promotion_threshold": 55, "name": "Gen2"})
    await db_session.commit()
    await db_session.refresh(a)
    assert float(a.promotion_threshold) == 55 and a.name == "Gen2"


# ---- half-life override wired into scoring --------------------------------

async def test_half_life_override_from_profile(db_session):
    profile = IcpIntentProfile(name="x", half_life_overrides={"rev_drop": 10.0})
    hl, _ = scoring._half_life_and_max_age(IntentSignalType.REV_DROP, profile)
    assert hl == 10.0
    # No override → per-type default.
    hl2, _ = scoring._half_life_and_max_age(IntentSignalType.REV_DROP, None)
    assert hl2 == 540.0


async def test_preview_does_not_mutate_status(db_session):
    org = Org(name="O", ein="111111111", size_band=OrgSizeBand.SMALL)
    db_session.add(org)
    await db_session.flush()
    sig = Signal(
        org_id=org.id, signal_type=IntentSignalType.REV_DROP,
        source=IntentSignalSource.PROPUBLICA, score=Decimal("80"),
        event_date=NOW - timedelta(days=30), evidence_url="u", summary="s",
        dedupe_key="k1", status=IntentSignalStatus.NEW,
    )
    db_session.add(sig)
    await db_session.commit()

    await scoring.preview_org_intent(db_session, org, now=NOW)
    await db_session.refresh(sig)
    assert sig.status is IntentSignalStatus.NEW   # preview never flips


# ---- the headline gate: distinct rankings ---------------------------------

async def _org(db_session, *, name, band, ntee):
    o = Org(name=name, ein=name.replace(" ", "")[:9].ljust(9, "0"),
            state="WI", ntee_code=ntee, size_band=band)
    db_session.add(o)
    await db_session.flush()
    return o


async def _sig(db_session, org, *, stype, score, age=2):
    s = Signal(
        org_id=org.id, signal_type=stype, source=IntentSignalSource.GRANTS_GOV,
        score=Decimal(str(score)), event_date=NOW - timedelta(days=age),
        evidence_url="u", summary="why", dedupe_key=f"k:{org.id}:{stype.value}",
        status=IntentSignalStatus.SCORED,
    )
    db_session.add(s)
    await db_session.flush()


async def test_grantmind_vs_generic_produce_distinct_rankings(db_session):
    # A small/mid org and a major org, each with an identical fresh new_rfp.
    small = await _org(db_session, name="Small Org", band=OrgSizeBand.SMALL, ntee="T31")
    major = await _org(db_session, name="Major Org", band=OrgSizeBand.MAJOR, ntee="T31")
    await _sig(db_session, small, stype=IntentSignalType.NEW_RFP, score=70)
    await _sig(db_session, major, stype=IntentSignalType.NEW_RFP, score=70)
    await db_session.commit()

    grantmind = await profiles_svc.create_from_preset(db_session, "grantmind")
    generic = await profiles_svc.create_from_preset(db_session, "generic")
    await db_session.commit()

    cmp = await profiles_svc.compare_profiles(db_session, grantmind, generic, now=NOW)

    gm = {r["name"]: r for r in cmp["a"]}
    gen = {r["name"]: r for r in cmp["b"]}
    # GrantMind: small (×1.4 fit, ×1.3 new_rfp weight) ranks ABOVE major (×0.2).
    assert cmp["a"][0]["name"] == "Small Org"
    assert gm["Small Org"]["intent_score"] > gm["Major Org"]["intent_score"]
    # Generic: flat weights → both equal (same base signal), distinct from GM.
    assert gen["Small Org"]["intent_score"] == gen["Major Org"]["intent_score"]
    # The two profiles disagree on Small Org's score → distinct lists.
    assert gm["Small Org"]["intent_score"] != gen["Small Org"]["intent_score"]


# ---- API ------------------------------------------------------------------

async def test_profile_api_crud_preset_and_compare(client, db_session):
    # Create both presets via the API.
    r1 = await client.post("/intent/profiles/preset?kind=grantmind")
    assert r1.status_code == 201
    gm_id = r1.json()["id"]
    r2 = await client.post("/intent/profiles/preset?kind=generic")
    gen_id = r2.json()["id"]

    # Generic was created last → it's the active one; list shows both.
    lst = (await client.get("/intent/profiles")).json()
    assert len(lst) == 2
    assert sum(1 for p in lst if p["is_active"]) == 1

    # Activate GrantMind.
    act = await client.post(f"/intent/profiles/{gm_id}/activate")
    assert act.status_code == 200 and act.json()["is_active"] is True

    # Patch + 404.
    patched = await client.patch(f"/intent/profiles/{gm_id}", json={"promotion_threshold": 90})
    assert patched.json()["promotion_threshold"] == 90
    assert (await client.get(f"/intent/profiles/{gm_id}/intent")).status_code == 200
    assert (await client.get(f"/intent/compare?profile_a={gm_id}&profile_b={gen_id}")).status_code == 200
    import uuid
    assert (await client.get(f"/intent/profiles/{uuid.uuid4()}")).status_code == 404
