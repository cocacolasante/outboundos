"""Integration tests for M4 LinkedIn premium actions:
  - linkedin_inmail (premium-required skip path + happy path)
  - linkedin_comment_post (latest-post fallback + missing-template misconfigured)
  - shared DM/InMail rate cap
  - publish-time validation of the new kinds

LinkedIn HTTP is monkeypatched at the provider level — no network.
"""
from datetime import datetime, time, timezone

import pytest
from sqlalchemy import select

from app.models import (
    Campaign,
    CampaignStatus,
    Lead,
    LeadStepExecution,
    LeadStepResult,
    LinkedInAccount,
    LinkedInAccountStatus,
    LinkedInConnectionStatus,
    Sequence,
    SequenceEdge,
    SequenceNode,
    SequenceNodeKind,
    SendStatus,
)
from app.services import encryption
from app.services.linkedin.base import ActionResult
from app.workers import sequencer

def _async_provider(stub):
    """BYOK (Phase 4): the sequencer resolves its provider per tenant via
    the async ``ambient_linkedin_provider`` — stubs must be awaitable."""
    async def _get():
        return stub
    return _get




@pytest.fixture(autouse=True)
def _reset_li_redis_singleton(monkeypatch):
    """Pytest-asyncio gives each test a fresh event loop; reset the cached
    redis client so it isn't bound to a dead loop."""
    monkeypatch.setattr(sequencer, "_LI_REDIS_CLIENT", None)


async def _make_li_account(db_session, *, status=LinkedInAccountStatus.OK) -> LinkedInAccount:
    acc = LinkedInAccount(
        label="t",
        linkedin_email="t@example.com",
        password_encrypted=encryption.encrypt("pw"),
        status=status,
    )
    db_session.add(acc)
    await db_session.commit()
    await db_session.refresh(acc)
    return acc


async def _make_campaign(db_session, *, linkedin_account_id) -> Campaign:
    c = Campaign(
        name="m4-test",
        goal="g", tone="Direct", sender_name="A", sender_email="a@example.com",
        schedule_days=list(range(7)),
        schedule_time_start=time(0, 0), schedule_time_end=time(23, 59),
        schedule_timezone="UTC", status=CampaignStatus.RUNNING,
        linkedin_account_id=linkedin_account_id,
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    return c


async def _make_lead(db_session, campaign, *, connection=LinkedInConnectionStatus.UNKNOWN) -> Lead:
    l = Lead(
        campaign_id=campaign.id,
        email="lead@example.com", first_name="Cole",
        linkedin_url="https://www.linkedin.com/in/coleburnham/",
        linkedin_connection_status=connection,
        send_status=SendStatus.PENDING,
    )
    db_session.add(l)
    await db_session.commit()
    await db_session.refresh(l)
    return l


async def _build_seq_with_node(db_session, campaign, kind, config) -> SequenceNode:
    seq = (await db_session.execute(
        select(Sequence).where(Sequence.campaign_id == campaign.id)
    )).scalar_one_or_none()
    if seq is None:
        seq = Sequence(campaign_id=campaign.id, is_published=True)
        db_session.add(seq)
        await db_session.flush()
        entry = SequenceNode(
            sequence_id=seq.id, kind=SequenceNodeKind.EMAIL,
            config={"use_campaign_compose": True}, is_entry=True,
        )
        db_session.add(entry)
        await db_session.flush()
    else:
        entry = (await db_session.execute(
            select(SequenceNode).where(
                SequenceNode.sequence_id == seq.id,
                SequenceNode.is_entry.is_(True),
            )
        )).scalar_one()
    node = SequenceNode(sequence_id=seq.id, kind=kind, config=config, is_entry=False)
    db_session.add(node)
    await db_session.flush()
    db_session.add_all([
        SequenceEdge(sequence_id=seq.id, from_node_id=entry.id, to_node_id=node.id, condition={"op": "always"}),
        SequenceEdge(sequence_id=seq.id, from_node_id=node.id, to_node_id=None, condition={"op": "always"}),
    ])
    await db_session.commit()
    return node


def _stub_provider(monkeypatch, **overrides):
    from app.workers import sequencer as seq_mod

    class _Stub:
        async def view_profile(self, account, profile):
            return ActionResult(ok=True)
        async def follow_profile(self, account, profile):
            return ActionResult(ok=True)
        async def react_to_post(self, *a, **k):
            return ActionResult(ok=True)
        async def latest_post_urn(self, account, profile):
            return "urn:li:activity:1234567"
        async def send_connect_request(self, *a, **k):
            return ActionResult(ok=True)
        async def send_dm(self, *a, **k):
            return ActionResult(ok=True)
        async def invite_to_page(self, *a, **k):
            return ActionResult(ok=True)
        async def send_inmail(self, account, profile, subject, body):
            return ActionResult(ok=True, external_id="urn:li:fsd_profile:x")
        async def comment_on_post(self, account, post_urn, comment):
            return ActionResult(ok=True, external_id=post_urn)
        async def test_connection(self, *a, **k):
            return ActionResult(ok=True)
        async def inbox_recent_events(self, *a, **k):
            return []

    instance = _Stub()
    for k, v in overrides.items():
        setattr(instance, k, v)
    monkeypatch.setattr(seq_mod, "ambient_linkedin_provider", _async_provider(instance))
    return instance


# --------------------------------------------------------------------------
# linkedin_inmail
# --------------------------------------------------------------------------


async def test_inmail_happy_path_substitutes_subject_and_body(db_session, monkeypatch):
    acc = await _make_li_account(db_session)
    campaign = await _make_campaign(db_session, linkedin_account_id=acc.id)
    lead = await _make_lead(db_session, campaign)
    node = await _build_seq_with_node(
        db_session, campaign, SequenceNodeKind.LINKEDIN_INMAIL,
        {
            "subject_template": "Quick question, {{first_name}}",
            "body_template": "Hi {{first_name}}, hope this lands well.",
        },
    )

    captured = {}
    async def fake_inmail(account, profile, subject, body):
        captured["subject"] = subject
        captured["body"] = body
        return ActionResult(ok=True, external_id="urn:li:fsd_profile:x")
    _stub_provider(monkeypatch, send_inmail=fake_inmail)

    result = await sequencer._send_linkedin_step_async(str(lead.id), str(node.id))
    assert result["status"] == "sent", result
    assert captured["subject"] == "Quick question, Cole"
    assert captured["body"].startswith("Hi Cole")


async def test_inmail_premium_required_is_skipped_cleanly(db_session, monkeypatch):
    acc = await _make_li_account(db_session)
    campaign = await _make_campaign(db_session, linkedin_account_id=acc.id)
    lead = await _make_lead(db_session, campaign)
    node = await _build_seq_with_node(
        db_session, campaign, SequenceNodeKind.LINKEDIN_INMAIL,
        {
            "subject_template": "Hi {{first_name}}",
            "body_template": "Hi {{first_name}}",
        },
    )

    async def fake_inmail(account, profile, subject, body):
        return ActionResult(
            ok=False,
            error="InMail unavailable",
            meta={"premium_required": True},
        )
    _stub_provider(monkeypatch, send_inmail=fake_inmail)

    result = await sequencer._send_linkedin_step_async(str(lead.id), str(node.id))
    assert result["status"] == "skipped"
    assert "Premium" in result["error"]


async def test_inmail_misconfigured_when_templates_missing(db_session, monkeypatch):
    acc = await _make_li_account(db_session)
    campaign = await _make_campaign(db_session, linkedin_account_id=acc.id)
    lead = await _make_lead(db_session, campaign)
    node = await _build_seq_with_node(
        db_session, campaign, SequenceNodeKind.LINKEDIN_INMAIL, {},
    )
    _stub_provider(monkeypatch)

    result = await sequencer._send_linkedin_step_async(str(lead.id), str(node.id))
    assert result["status"] == "misconfigured"


# --------------------------------------------------------------------------
# linkedin_comment_post
# --------------------------------------------------------------------------


async def test_comment_post_uses_latest_post_when_no_urn(db_session, monkeypatch):
    acc = await _make_li_account(db_session)
    campaign = await _make_campaign(db_session, linkedin_account_id=acc.id)
    lead = await _make_lead(db_session, campaign)
    node = await _build_seq_with_node(
        db_session, campaign, SequenceNodeKind.LINKEDIN_COMMENT_POST,
        {"comment_template": "Loved this, {{first_name}}!"},
    )

    captured = {}
    async def fake_comment(account, post_urn, comment):
        captured["urn"] = post_urn
        captured["text"] = comment
        return ActionResult(ok=True, external_id=post_urn)
    _stub_provider(monkeypatch, comment_on_post=fake_comment)

    result = await sequencer._send_linkedin_step_async(str(lead.id), str(node.id))
    assert result["status"] == "sent"
    assert captured["urn"] == "urn:li:activity:1234567"
    assert captured["text"] == "Loved this, Cole!"


async def test_comment_post_skips_when_no_post(db_session, monkeypatch):
    acc = await _make_li_account(db_session)
    campaign = await _make_campaign(db_session, linkedin_account_id=acc.id)
    lead = await _make_lead(db_session, campaign)
    node = await _build_seq_with_node(
        db_session, campaign, SequenceNodeKind.LINKEDIN_COMMENT_POST,
        {"comment_template": "Hi {{first_name}}"},
    )

    async def no_post(account, profile):
        return None
    _stub_provider(monkeypatch, latest_post_urn=no_post)

    result = await sequencer._send_linkedin_step_async(str(lead.id), str(node.id))
    assert result["status"] == "skipped"
    assert "no post" in result["error"]


async def test_comment_post_requires_template(db_session, monkeypatch):
    acc = await _make_li_account(db_session)
    campaign = await _make_campaign(db_session, linkedin_account_id=acc.id)
    lead = await _make_lead(db_session, campaign)
    node = await _build_seq_with_node(
        db_session, campaign, SequenceNodeKind.LINKEDIN_COMMENT_POST, {},
    )
    _stub_provider(monkeypatch)
    result = await sequencer._send_linkedin_step_async(str(lead.id), str(node.id))
    assert result["status"] == "misconfigured"


# --------------------------------------------------------------------------
# Shared DM/InMail cap
# --------------------------------------------------------------------------


async def test_inmail_counts_against_dm_cap(db_session, monkeypatch):
    """One DM + one InMail with DM cap=1 → the InMail gets rate-limited."""
    from app.config import settings
    monkeypatch.setattr(settings, "LINKEDIN_DAILY_ACTION_CAP", 100)
    monkeypatch.setattr(settings, "LINKEDIN_DAILY_DM_CAP", 1)
    monkeypatch.setattr(settings, "LINKEDIN_MIN_ACTION_DELAY_SECONDS", 0)

    acc = await _make_li_account(db_session)
    campaign = await _make_campaign(db_session, linkedin_account_id=acc.id)
    lead = await _make_lead(
        db_session, campaign, connection=LinkedInConnectionStatus.CONNECTED,
    )
    dm_node = await _build_seq_with_node(
        db_session, campaign, SequenceNodeKind.LINKEDIN_DM,
        {"text_template": "hi {{first_name}}"},
    )
    inmail_node = await _build_seq_with_node(
        db_session, campaign, SequenceNodeKind.LINKEDIN_INMAIL,
        {"subject_template": "x", "body_template": "y"},
    )
    _stub_provider(monkeypatch)

    rc = sequencer._li_redis()
    await rc.delete(
        f"li-rate:{acc.id}:day",
        f"li-rate:{acc.id}:day:dm",
        f"li-rate:{acc.id}:last",
    )

    r1 = await sequencer._send_linkedin_step_async(str(lead.id), str(dm_node.id))
    assert r1["status"] == "sent"

    r2 = await sequencer._send_linkedin_step_async(str(lead.id), str(inmail_node.id))
    # Cap-style skips defer to the cap-reset time (Redis TTL) instead of
    # burning the transient-retry budget.
    assert r2["status"] == "deferred"
    assert r2["reason"] == "dm_cap"
    assert "DM/InMail" in r2["error"]


# --------------------------------------------------------------------------
# Publish-time validation
# --------------------------------------------------------------------------


async def _make_campaign_via_api(client) -> str:
    r = await client.post("/campaigns/", json={
        "name": "x", "goal": "g", "tone": "Direct",
        "sender_name": "A", "sender_email": "a@example.com",
        "research_mode": "fast",
        "schedule_days": [0, 1, 2, 3, 4],
        "schedule_time_start": "09:00:00",
        "schedule_time_end": "17:00:00",
        "schedule_timezone": "UTC",
    })
    assert r.status_code == 201
    return r.json()["id"]


# test_publish_requires_inmail_subject removed — linkedin_inmail is
# currently gated out of PUBLISHABLE_KINDS while Unipile's Sales Nav API
# access is locked down on the workspace.  Publish rejects the kind
# before the per-config subject check fires; restore this test alongside
# re-enabling the kind in sequence_service.PUBLISHABLE_KINDS_M1.


async def test_publish_rejects_invalid_comment_target(client):
    cid = await _make_campaign_via_api(client)
    await client.put(f"/campaigns/{cid}/sequence", json={
        "nodes": [
            {"client_id": "e", "kind": "email", "is_entry": True, "config": {}},
            {
                "client_id": "c", "kind": "linkedin_comment_post", "is_entry": False,
                "config": {"comment_template": "nice", "target": "random"},
            },
        ],
        "edges": [
            {"from_client_id": "e", "to_client_id": "c", "condition": {"op": "always"}},
        ],
    })
    pub = await client.post(f"/campaigns/{cid}/sequence/publish")
    body = pub.json()
    assert body["ok"] is False
    assert any("target" in e for e in body["errors"])
