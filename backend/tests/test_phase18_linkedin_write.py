"""Integration tests for M3 LinkedIn write actions:
  - linkedin_connect (with + without note, 300-char cap)
  - linkedin_dm (skip when not 1st-degree)
  - linkedin_invite_to_page (skip when not connected, page_id required)
  - per-kind rate caps (connect, DM, page-invite-per-page)

Real LinkedIn HTTP is monkeypatched at the provider level; we never hit
network.
"""
from datetime import datetime, time, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import (
    Campaign,
    CampaignStatus,
    Lead,
    LeadSequenceState,
    LeadSequenceStatus,
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
    """The redis client is module-cached; pytest-asyncio gives each test a
    fresh event loop, so the cached client from the previous test points at
    a dead loop. Reset it before every test in this file."""
    monkeypatch.setattr(sequencer, "_LI_REDIS_CLIENT", None)


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
        name="m3-test",
        goal="Connect with leads",
        tone="Direct",
        sender_name="A",
        sender_email="a@example.com",
        schedule_days=list(range(7)),
        schedule_time_start=time(0, 0),
        schedule_time_end=time(23, 59),
        schedule_timezone="UTC",
        status=CampaignStatus.RUNNING,
        linkedin_account_id=linkedin_account_id,
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    return c


async def _make_lead(db_session, campaign, *, connection=LinkedInConnectionStatus.UNKNOWN) -> Lead:
    l = Lead(
        campaign_id=campaign.id,
        email="lead@example.com",
        first_name="Cole",
        linkedin_url="https://www.linkedin.com/in/coleburnham/",
        linkedin_connection_status=connection,
        send_status=SendStatus.PENDING,
    )
    db_session.add(l)
    await db_session.commit()
    await db_session.refresh(l)
    return l


async def _build_seq_with_node(db_session, campaign, kind, config) -> SequenceNode:
    """Add `kind` as a non-entry node attached to the campaign's sequence,
    creating the sequence (with an email entry node) only on the first call.
    """
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

    node = SequenceNode(
        sequence_id=seq.id, kind=kind, config=config, is_entry=False,
    )
    db_session.add(node)
    await db_session.flush()
    db_session.add_all([
        SequenceEdge(sequence_id=seq.id, from_node_id=entry.id, to_node_id=node.id, condition={"op": "always"}),
        SequenceEdge(sequence_id=seq.id, from_node_id=node.id, to_node_id=None, condition={"op": "always"}),
    ])
    await db_session.commit()
    return node


def _stub_provider(monkeypatch, **overrides):
    """Patch get_linkedin_provider with a fake. Provide overrides like
    send_connect_request=AsyncMock or just rely on defaults that return OK.
    """
    from app.workers import sequencer as seq_mod

    class _Stub:
        async def view_profile(self, account, profile):
            return ActionResult(ok=True, external_id="urn:li:fsd_profile:x")
        async def follow_profile(self, account, profile):
            return ActionResult(ok=True)
        async def react_to_post(self, account, post_urn, reaction="LIKE"):
            return ActionResult(ok=True)
        async def latest_post_urn(self, account, profile):
            return None
        async def send_connect_request(self, account, profile, note=None):
            return ActionResult(ok=True, external_id=profile.public_id, meta={"note": note})
        async def send_dm(self, account, profile, text):
            return ActionResult(ok=True, external_id="urn:li:fsd_profile:x", meta={"len": len(text)})
        async def invite_to_page(self, account, profile, page_id):
            return ActionResult(ok=True, external_id="urn:li:fsd_profile:x", meta={"page_id": page_id})
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
# linkedin_connect
# --------------------------------------------------------------------------


async def test_connect_with_note_substitutes_and_marks_invited(db_session, monkeypatch):
    acc = await _make_li_account(db_session)
    campaign = await _make_campaign(db_session, linkedin_account_id=acc.id)
    lead = await _make_lead(db_session, campaign)
    node = await _build_seq_with_node(
        db_session, campaign, SequenceNodeKind.LINKEDIN_CONNECT,
        {"note_template": "Hi {{first_name}}, would love to connect."},
    )

    captured = {}
    async def fake_connect(account, profile, note=None):
        captured["note"] = note
        return ActionResult(ok=True, external_id=profile.public_id)
    _stub_provider(monkeypatch, send_connect_request=fake_connect)

    result = await sequencer._send_linkedin_step_async(str(lead.id), str(node.id))
    assert result["status"] == "sent", result
    assert captured["note"] == "Hi Cole, would love to connect."

    # Lead promoted to INVITED optimistically.
    await db_session.refresh(lead)
    assert lead.linkedin_connection_status == LinkedInConnectionStatus.INVITED


async def test_connect_no_note_flag_skips_template(db_session, monkeypatch):
    acc = await _make_li_account(db_session)
    campaign = await _make_campaign(db_session, linkedin_account_id=acc.id)
    lead = await _make_lead(db_session, campaign)
    node = await _build_seq_with_node(
        db_session, campaign, SequenceNodeKind.LINKEDIN_CONNECT,
        {"no_note": True, "note_template": "ignored"},
    )

    captured = {}
    async def fake_connect(account, profile, note=None):
        captured["note"] = note
        return ActionResult(ok=True, external_id=profile.public_id)
    _stub_provider(monkeypatch, send_connect_request=fake_connect)

    result = await sequencer._send_linkedin_step_async(str(lead.id), str(node.id))
    assert result["status"] == "sent"
    assert captured["note"] is None


async def test_connect_skips_when_lead_already_invited(db_session, monkeypatch):
    """Re-running a connect step against a lead with INVITED status (an
    invite was already sent and is pending) must NOT fire a second
    Unipile call.  Two invites to the same person looks automated and
    burns the daily connect cap.  Expected: skip-and-advance with a
    clear reason."""
    acc = await _make_li_account(db_session)
    campaign = await _make_campaign(db_session, linkedin_account_id=acc.id)
    lead = await _make_lead(
        db_session, campaign, connection=LinkedInConnectionStatus.INVITED,
    )
    node = await _build_seq_with_node(
        db_session, campaign, SequenceNodeKind.LINKEDIN_CONNECT,
        {"note_template": "Hi {{first_name}}!"},
    )

    fake_connect_calls = 0
    async def fake_connect(account, profile, note=None):
        nonlocal fake_connect_calls
        fake_connect_calls += 1
        return ActionResult(ok=True)
    _stub_provider(monkeypatch, send_connect_request=fake_connect)

    result = await sequencer._send_linkedin_step_async(str(lead.id), str(node.id))
    assert result["status"] == "skipped"
    assert "already sent" in result["error"]
    assert fake_connect_calls == 0


async def test_connect_skips_when_lead_already_connected(db_session, monkeypatch):
    """A lead that's already a 1st-degree connection doesn't need a new
    invite — Unipile would 4xx and it wastes a rate slot."""
    acc = await _make_li_account(db_session)
    campaign = await _make_campaign(db_session, linkedin_account_id=acc.id)
    lead = await _make_lead(
        db_session, campaign, connection=LinkedInConnectionStatus.CONNECTED,
    )
    node = await _build_seq_with_node(
        db_session, campaign, SequenceNodeKind.LINKEDIN_CONNECT,
        {"no_note": True},
    )

    fake_connect_calls = 0
    async def fake_connect(account, profile, note=None):
        nonlocal fake_connect_calls
        fake_connect_calls += 1
        return ActionResult(ok=True)
    _stub_provider(monkeypatch, send_connect_request=fake_connect)

    result = await sequencer._send_linkedin_step_async(str(lead.id), str(node.id))
    assert result["status"] == "skipped"
    assert "already" in result["error"].lower()
    assert fake_connect_calls == 0


async def test_connect_note_truncated_to_300_chars(db_session, monkeypatch):
    acc = await _make_li_account(db_session)
    campaign = await _make_campaign(db_session, linkedin_account_id=acc.id)
    lead = await _make_lead(db_session, campaign)
    long_note = "x" * 400
    node = await _build_seq_with_node(
        db_session, campaign, SequenceNodeKind.LINKEDIN_CONNECT,
        {"note_template": long_note},
    )

    captured = {}
    async def fake_connect(account, profile, note=None):
        captured["note"] = note
        return ActionResult(ok=True)
    _stub_provider(monkeypatch, send_connect_request=fake_connect)

    await sequencer._send_linkedin_step_async(str(lead.id), str(node.id))
    assert len(captured["note"]) == 300


# --------------------------------------------------------------------------
# linkedin_dm
# --------------------------------------------------------------------------


async def test_dm_skips_when_not_connected(db_session, monkeypatch):
    acc = await _make_li_account(db_session)
    campaign = await _make_campaign(db_session, linkedin_account_id=acc.id)
    # Lead is UNKNOWN (no invite ever sent) → no signal coming, must skip
    # rather than defer forever.
    lead = await _make_lead(
        db_session, campaign, connection=LinkedInConnectionStatus.UNKNOWN,
    )
    node = await _build_seq_with_node(
        db_session, campaign, SequenceNodeKind.LINKEDIN_DM,
        {"text_template": "Thanks for connecting!"},
    )
    _stub_provider(monkeypatch)

    result = await sequencer._send_linkedin_step_async(str(lead.id), str(node.id))
    assert result["status"] == "skipped"
    assert "1st degree" in result["error"]


async def test_dm_defers_when_lead_is_invited(db_session, monkeypatch):
    """A lead with status=INVITED has an outstanding connect request.
    The right move for the DM step isn't to skip-and-advance — that
    silently drops the follow-up message every time.  It's to defer:
    park on the DM node and re-check every EDGE_WAIT_RETRY_MINUTES
    until the webhook flips the lead to CONNECTED."""
    acc = await _make_li_account(db_session)
    campaign = await _make_campaign(db_session, linkedin_account_id=acc.id)
    lead = await _make_lead(
        db_session, campaign, connection=LinkedInConnectionStatus.INVITED,
    )
    node = await _build_seq_with_node(
        db_session, campaign, SequenceNodeKind.LINKEDIN_DM,
        {"text_template": "Thanks for connecting!"},
    )
    # Park the lead's sequence state on this DM node so the entered_at
    # gate (which checks the 14-day timeout) has something to look at.
    seq = (await db_session.execute(
        select(Sequence).where(Sequence.campaign_id == campaign.id)
    )).scalar_one()
    db_session.add(LeadSequenceState(
        lead_id=lead.id, sequence_id=seq.id,
        current_node_id=node.id, entered_current_at=_now(),
        status=LeadSequenceStatus.ACTIVE,
    ))
    await db_session.commit()

    fake_dm_calls = 0
    async def fake_dm(account, profile, text):
        nonlocal fake_dm_calls
        fake_dm_calls += 1
        return ActionResult(ok=True)
    _stub_provider(monkeypatch, send_dm=fake_dm)

    result = await sequencer._send_linkedin_step_async(str(lead.id), str(node.id))
    assert result["status"] == "deferred"
    assert result["reason"] == "waiting_for_connection"
    assert fake_dm_calls == 0
    assert result.get("retry_in", 0) > 0


async def test_dm_gives_up_after_max_edge_wait_days(db_session, monkeypatch):
    """Bounded wait: if a lead has been parked on the DM node for more
    than MAX_EDGE_WAIT_DAYS without the connect being accepted, the
    sequencer gives up and skip-advances rather than waiting forever."""
    acc = await _make_li_account(db_session)
    campaign = await _make_campaign(db_session, linkedin_account_id=acc.id)
    lead = await _make_lead(
        db_session, campaign, connection=LinkedInConnectionStatus.INVITED,
    )
    node = await _build_seq_with_node(
        db_session, campaign, SequenceNodeKind.LINKEDIN_DM,
        {"text_template": "Hi!"},
    )
    # Park state with entered_current_at older than the cap.
    seq = (await db_session.execute(
        select(Sequence).where(Sequence.campaign_id == campaign.id)
    )).scalar_one()
    db_session.add(LeadSequenceState(
        lead_id=lead.id, sequence_id=seq.id,
        current_node_id=node.id,
        entered_current_at=_now() - timedelta(days=sequencer.MAX_EDGE_WAIT_DAYS + 1),
        status=LeadSequenceStatus.ACTIVE,
    ))
    await db_session.commit()

    _stub_provider(monkeypatch)
    result = await sequencer._send_linkedin_step_async(str(lead.id), str(node.id))
    assert result["status"] == "skipped"
    assert "giving up" in result["error"]


async def test_dm_sends_when_connected(db_session, monkeypatch):
    acc = await _make_li_account(db_session)
    campaign = await _make_campaign(db_session, linkedin_account_id=acc.id)
    lead = await _make_lead(
        db_session, campaign, connection=LinkedInConnectionStatus.CONNECTED,
    )
    node = await _build_seq_with_node(
        db_session, campaign, SequenceNodeKind.LINKEDIN_DM,
        {"text_template": "Hi {{first_name}}, thanks for connecting!"},
    )

    captured = {}
    async def fake_dm(account, profile, text):
        captured["text"] = text
        return ActionResult(ok=True)
    _stub_provider(monkeypatch, send_dm=fake_dm)

    result = await sequencer._send_linkedin_step_async(str(lead.id), str(node.id))
    assert result["status"] == "sent"
    assert captured["text"] == "Hi Cole, thanks for connecting!"


async def test_dm_misconfigured_when_no_template(db_session, monkeypatch):
    acc = await _make_li_account(db_session)
    campaign = await _make_campaign(db_session, linkedin_account_id=acc.id)
    lead = await _make_lead(
        db_session, campaign, connection=LinkedInConnectionStatus.CONNECTED,
    )
    node = await _build_seq_with_node(
        db_session, campaign, SequenceNodeKind.LINKEDIN_DM, {},
    )
    _stub_provider(monkeypatch)

    result = await sequencer._send_linkedin_step_async(str(lead.id), str(node.id))
    assert result["status"] == "misconfigured"


# --------------------------------------------------------------------------
# linkedin_invite_to_page
# --------------------------------------------------------------------------


async def test_invite_to_page_skips_when_not_connected(db_session, monkeypatch):
    acc = await _make_li_account(db_session)
    campaign = await _make_campaign(db_session, linkedin_account_id=acc.id)
    lead = await _make_lead(db_session, campaign)  # UNKNOWN connection
    node = await _build_seq_with_node(
        db_session, campaign, SequenceNodeKind.LINKEDIN_INVITE_TO_PAGE,
        {"page_id": "12345678"},
    )
    _stub_provider(monkeypatch)
    result = await sequencer._send_linkedin_step_async(str(lead.id), str(node.id))
    assert result["status"] == "skipped"


async def test_invite_to_page_requires_page_id(db_session, monkeypatch):
    acc = await _make_li_account(db_session)
    campaign = await _make_campaign(db_session, linkedin_account_id=acc.id)
    lead = await _make_lead(
        db_session, campaign, connection=LinkedInConnectionStatus.CONNECTED,
    )
    node = await _build_seq_with_node(
        db_session, campaign, SequenceNodeKind.LINKEDIN_INVITE_TO_PAGE, {},
    )
    _stub_provider(monkeypatch)
    result = await sequencer._send_linkedin_step_async(str(lead.id), str(node.id))
    assert result["status"] == "misconfigured"


async def test_invite_to_page_succeeds(db_session, monkeypatch):
    acc = await _make_li_account(db_session)
    campaign = await _make_campaign(db_session, linkedin_account_id=acc.id)
    lead = await _make_lead(
        db_session, campaign, connection=LinkedInConnectionStatus.CONNECTED,
    )
    node = await _build_seq_with_node(
        db_session, campaign, SequenceNodeKind.LINKEDIN_INVITE_TO_PAGE,
        {"page_id": "99999"},
    )

    captured = {}
    async def fake_invite(account, profile, page_id):
        captured["page_id"] = page_id
        return ActionResult(ok=True)
    _stub_provider(monkeypatch, invite_to_page=fake_invite)

    result = await sequencer._send_linkedin_step_async(str(lead.id), str(node.id))
    assert result["status"] == "sent"
    assert captured["page_id"] == "99999"


# --------------------------------------------------------------------------
# Per-kind rate caps
# --------------------------------------------------------------------------


async def test_connect_cap_blocks_after_subcap_hit(db_session, monkeypatch):
    """Set the connect cap to 1, then run two connects in a row."""
    from app.config import settings
    monkeypatch.setattr(settings, "LINKEDIN_DAILY_ACTION_CAP", 100)
    monkeypatch.setattr(settings, "LINKEDIN_DAILY_CONNECT_CAP", 1)
    monkeypatch.setattr(settings, "LINKEDIN_MIN_ACTION_DELAY_SECONDS", 0)

    import app.workers.sequencer as seq_mod
    monkeypatch.setattr(seq_mod, "_LI_REDIS_CLIENT", None)

    acc = await _make_li_account(db_session)
    campaign = await _make_campaign(db_session, linkedin_account_id=acc.id)
    # Two distinct leads, both UNKNOWN status so the connect-already-sent
    # short-circuit doesn't intercept the second call.
    lead1 = await _make_lead(db_session, campaign)
    lead2 = Lead(
        campaign_id=campaign.id, email="lead2@example.com",
        first_name="Sam",
        linkedin_url="https://www.linkedin.com/in/sam-other/",
        linkedin_connection_status=LinkedInConnectionStatus.UNKNOWN,
        send_status=SendStatus.PENDING,
    )
    db_session.add(lead2)
    await db_session.commit()
    await db_session.refresh(lead2)
    node = await _build_seq_with_node(
        db_session, campaign, SequenceNodeKind.LINKEDIN_CONNECT,
        {"note_template": "Hi {{first_name}}"},
    )
    _stub_provider(monkeypatch)

    rc = seq_mod._li_redis()
    await rc.delete(
        f"li-rate:{acc.id}:day",
        f"li-rate:{acc.id}:day:connect",
        f"li-rate:{acc.id}:last",
    )

    r1 = await sequencer._send_linkedin_step_async(str(lead1.id), str(node.id))
    assert r1["status"] == "sent", r1

    r2 = await sequencer._send_linkedin_step_async(str(lead2.id), str(node.id))
    # Cap-style skips now defer to the exact reset time (Redis TTL)
    # instead of consuming the transient-retry budget.
    assert r2["status"] == "deferred"
    assert r2["reason"] == "connect_cap"
    assert "connect" in r2["error"]


async def test_page_invite_cap_is_per_page(db_session, monkeypatch):
    """Cap=1 for page A; reaching it doesn't block page B."""
    from app.config import settings
    monkeypatch.setattr(settings, "LINKEDIN_DAILY_ACTION_CAP", 100)
    monkeypatch.setattr(settings, "LINKEDIN_MONTHLY_PAGE_INVITE_CAP", 1)
    monkeypatch.setattr(settings, "LINKEDIN_MIN_ACTION_DELAY_SECONDS", 0)

    import app.workers.sequencer as seq_mod
    monkeypatch.setattr(seq_mod, "_LI_REDIS_CLIENT", None)

    acc = await _make_li_account(db_session)
    campaign = await _make_campaign(db_session, linkedin_account_id=acc.id)
    lead = await _make_lead(
        db_session, campaign, connection=LinkedInConnectionStatus.CONNECTED,
    )
    node_a = await _build_seq_with_node(
        db_session, campaign, SequenceNodeKind.LINKEDIN_INVITE_TO_PAGE,
        {"page_id": "PAGE_A"},
    )
    node_b = await _build_seq_with_node(
        db_session, campaign, SequenceNodeKind.LINKEDIN_INVITE_TO_PAGE,
        {"page_id": "PAGE_B"},
    )
    _stub_provider(monkeypatch)

    rc = seq_mod._li_redis()
    await rc.delete(
        f"li-rate:page:PAGE_A:month",
        f"li-rate:page:PAGE_B:month",
        f"li-rate:{acc.id}:day",
        f"li-rate:{acc.id}:last",
    )

    r_a1 = await sequencer._send_linkedin_step_async(str(lead.id), str(node_a.id))
    assert r_a1["status"] == "sent"

    r_a2 = await sequencer._send_linkedin_step_async(str(lead.id), str(node_a.id))
    assert r_a2["status"] == "deferred"
    assert r_a2["reason"] == "page_invite_cap"
    assert "page invite" in r_a2["error"]

    r_b1 = await sequencer._send_linkedin_step_async(str(lead.id), str(node_b.id))
    assert r_b1["status"] == "sent"  # different page, cap not yet hit


# --------------------------------------------------------------------------
# Publish-time config validation
# --------------------------------------------------------------------------


async def test_linkedin_action_never_refires_after_sent_for_same_node(db_session, monkeypatch):
    """Lifetime idempotency: once any LinkedIn action node has produced
    a SENT execution row for a lead, a subsequent dispatch for the same
    (lead, node) must skip-and-advance — no second API call, regardless
    of how the duplicate arrived (re-enrollment, manual cursor reset,
    loop-back sequence, duplicate Celery delivery past the stale-dispatch
    guard).  Catches every action kind via the shared handler check."""
    acc = await _make_li_account(db_session)
    campaign = await _make_campaign(db_session, linkedin_account_id=acc.id)
    lead = await _make_lead(db_session, campaign)
    node = await _build_seq_with_node(
        db_session, campaign, SequenceNodeKind.LINKEDIN_VIEW_PROFILE, {},
    )

    fake_view_calls = 0
    async def fake_view(account, profile):
        nonlocal fake_view_calls
        fake_view_calls += 1
        return ActionResult(ok=True, external_id="urn:li:fsd_profile:x")
    _stub_provider(monkeypatch, view_profile=fake_view)

    # First dispatch fires.
    r1 = await sequencer._send_linkedin_step_async(str(lead.id), str(node.id))
    assert r1["status"] == "sent"
    assert fake_view_calls == 1

    # In production ``_record_execution_and_advance`` (the Celery
    # wrapper) writes the SENT row after the handler returns.  We're
    # calling the handler directly so simulate that step.
    db_session.add(LeadStepExecution(
        lead_id=lead.id, node_id=node.id, result=LeadStepResult.SENT,
    ))
    await db_session.commit()

    # Second dispatch for the same (lead, node) must skip — the SENT
    # execution row trips the lifetime idempotency check.
    r2 = await sequencer._send_linkedin_step_async(str(lead.id), str(node.id))
    assert r2["status"] == "skipped"
    assert "already sent" in r2["error"].lower()
    # No second API call.
    assert fake_view_calls == 1


async def test_followup_email_never_refires_after_sent_for_same_node(db_session, monkeypatch):
    """Same lifetime contract for follow-up email nodes.  A re-enrolled
    lead must not get the same templated email a second time."""
    from app.workers import send as _send_mod
    from app.workers import sequencer as seq_mod
    from app.services import brevo as brevo_mod
    from unittest.mock import AsyncMock

    acc = await _make_li_account(db_session)
    campaign = await _make_campaign(db_session, linkedin_account_id=acc.id)
    lead = await _make_lead(db_session, campaign)
    node = await _build_seq_with_node(
        db_session, campaign, SequenceNodeKind.EMAIL,
        {
            "subject_template": "Following up with {{first_name}}",
            "body_template": "Hey {{first_name}}, circling back.",
        },
    )

    # Bypass send-gate checks (returning ok) so the test focuses on the
    # idempotency contract, not the gate logic.
    monkeypatch.setattr(
        _send_mod, "check_send_gates", AsyncMock(return_value={"ok": True}),
    )
    monkeypatch.setattr(
        _send_mod, "increment_rate_counters", AsyncMock(return_value=None),
    )
    # Stub Brevo so we don't hit the network.
    brevo_send = AsyncMock(return_value="brevo-msg-id-1")
    monkeypatch.setattr(brevo_mod, "send_email", brevo_send)

    r1 = await sequencer._send_email_step_async(str(lead.id), str(node.id))
    assert r1["status"] == "sent"
    assert brevo_send.await_count == 1

    # Simulate the execution-row write that the Celery wrapper would
    # do after the handler returns.
    db_session.add(LeadStepExecution(
        lead_id=lead.id, node_id=node.id, result=LeadStepResult.SENT,
    ))
    await db_session.commit()

    r2 = await sequencer._send_email_step_async(str(lead.id), str(node.id))
    assert r2["status"] == "skipped"
    assert "already sent" in r2["error"].lower()
    # No second Brevo call.
    assert brevo_send.await_count == 1


async def test_publish_rejects_overlong_connect_note(client):
    cid_resp = await client.post("/campaigns/", json={
        "name": "x", "goal": "g", "tone": "Direct",
        "sender_name": "A", "sender_email": "a@example.com",
        "research_mode": "fast",
        "schedule_days": [0, 1, 2, 3, 4],
        "schedule_time_start": "09:00:00",
        "schedule_time_end": "17:00:00",
        "schedule_timezone": "UTC",
    })
    assert cid_resp.status_code == 201
    cid = cid_resp.json()["id"]

    payload = {
        "nodes": [
            {"client_id": "e", "kind": "email", "is_entry": True, "config": {}},
            {
                "client_id": "c", "kind": "linkedin_connect", "is_entry": False,
                "config": {"note_template": "x" * 350},
            },
        ],
        "edges": [
            {"from_client_id": "e", "to_client_id": "c", "condition": {"op": "always"}},
        ],
    }
    await client.put(f"/campaigns/{cid}/sequence", json=payload)
    pub = await client.post(f"/campaigns/{cid}/sequence/publish")
    body = pub.json()
    assert body["ok"] is False
    assert any("note_template" in e and "300" in e for e in body["errors"])


async def test_publish_rejects_linkedin_invite_to_page(client):
    """linkedin_invite_to_page is currently gated out of PUBLISHABLE_KINDS
    because Unipile's /api/v1/linkedin passthrough doesn't allowlist
    voyagerRelationshipsDashInvitations.  Publishing a sequence that uses
    the kind should fail with a clear error pointing at the gated kind."""
    cid_resp = await client.post("/campaigns/", json={
        "name": "x", "goal": "g", "tone": "Direct",
        "sender_name": "A", "sender_email": "a@example.com",
        "research_mode": "fast",
        "schedule_days": [0, 1, 2, 3, 4],
        "schedule_time_start": "09:00:00",
        "schedule_time_end": "17:00:00",
        "schedule_timezone": "UTC",
    })
    cid = cid_resp.json()["id"]

    payload = {
        "nodes": [
            {"client_id": "e", "kind": "email", "is_entry": True, "config": {}},
            {
                "client_id": "i", "kind": "linkedin_invite_to_page", "is_entry": False,
                "config": {"page_id": "112935410"},
            },
        ],
        "edges": [
            {"from_client_id": "e", "to_client_id": "i", "condition": {"op": "always"}},
        ],
    }
    await client.put(f"/campaigns/{cid}/sequence", json=payload)
    pub = await client.post(f"/campaigns/{cid}/sequence/publish")
    body = pub.json()
    assert body["ok"] is False
    assert any("linkedin_invite_to_page" in e for e in body["errors"])


async def test_publish_rejects_linkedin_inmail(client):
    """linkedin_inmail is gated out of PUBLISHABLE_KINDS until Unipile
    Sales Nav API access is enabled on the workspace — otherwise the
    step would silently skip on every lead with `resource_access_restricted`.
    Better to refuse to publish a sequence the user can't actually run."""
    cid_resp = await client.post("/campaigns/", json={
        "name": "x", "goal": "g", "tone": "Direct",
        "sender_name": "A", "sender_email": "a@example.com",
        "research_mode": "fast",
        "schedule_days": [0, 1, 2, 3, 4],
        "schedule_time_start": "09:00:00",
        "schedule_time_end": "17:00:00",
        "schedule_timezone": "UTC",
    })
    cid = cid_resp.json()["id"]

    payload = {
        "nodes": [
            {"client_id": "e", "kind": "email", "is_entry": True, "config": {}},
            {
                "client_id": "im", "kind": "linkedin_inmail", "is_entry": False,
                "config": {"subject_template": "Hey", "body_template": "Hi {{first_name}}"},
            },
        ],
        "edges": [
            {"from_client_id": "e", "to_client_id": "im", "condition": {"op": "always"}},
        ],
    }
    await client.put(f"/campaigns/{cid}/sequence", json=payload)
    pub = await client.post(f"/campaigns/{cid}/sequence/publish")
    body = pub.json()
    assert body["ok"] is False
    assert any("linkedin_inmail" in e for e in body["errors"])


# test_publish_rejects_non_numeric_page_id removed — linkedin_invite_to_page
# is currently gated out of PUBLISHABLE_KINDS, so the numeric-page_id check
# is unreachable from the publish path.  Restore alongside re-enabling the
# kind if Unipile allowlists voyagerRelationshipsDashInvitations.


# --------------------------------------------------------------------------
# Suppression gate on LinkedIn steps
# --------------------------------------------------------------------------


async def test_linkedin_step_blocked_for_suppressed_email(db_session, monkeypatch):
    """A lead whose email is on the suppression list must NOT receive
    LinkedIn outreach.  Without this gate, an ignored lead that gets
    re-enrolled (or whose state drifts back to ACTIVE) resumes connect/
    DM/view steps — only email steps used to re-check suppression."""
    from unittest.mock import AsyncMock
    from app.models import Suppression, SuppressionReason

    acc = await _make_li_account(db_session)
    campaign = await _make_campaign(db_session, linkedin_account_id=acc.id)
    lead = await _make_lead(db_session, campaign)
    node = await _build_seq_with_node(
        db_session, campaign, SequenceNodeKind.LINKEDIN_CONNECT,
        {"no_note": True},
    )
    db_session.add(Suppression(
        email=lead.email.strip().lower(), reason=SuppressionReason.MANUAL,
    ))
    await db_session.commit()

    connect_mock = AsyncMock()
    _stub_provider(monkeypatch, send_connect_request=connect_mock)

    result = await sequencer._send_linkedin_step_async(str(lead.id), str(node.id))
    assert result["status"] == "suppressed", result
    # Unipile never called.
    connect_mock.assert_not_called()


async def test_suppressed_status_halts_sequence_state(db_session, monkeypatch):
    """When the step handler reports ``suppressed``, the recorder HALTS
    the lead's sequence state outright (the whole sequence is moot)
    instead of advancing node-by-node with a skip row per step."""
    from unittest.mock import AsyncMock
    from sqlalchemy import select as _select
    from app.models import (
        LeadSequenceStatus, Suppression, SuppressionReason,
    )

    acc = await _make_li_account(db_session)
    campaign = await _make_campaign(db_session, linkedin_account_id=acc.id)
    lead = await _make_lead(db_session, campaign)
    node = await _build_seq_with_node(
        db_session, campaign, SequenceNodeKind.LINKEDIN_CONNECT,
        {"no_note": True},
    )
    db_session.add(Suppression(
        email=lead.email.strip().lower(), reason=SuppressionReason.MANUAL,
    ))
    seq_id = node.sequence_id
    db_session.add(LeadSequenceState(
        sequence_id=seq_id, lead_id=lead.id, current_node_id=node.id,
        status=LeadSequenceStatus.ACTIVE,
    ))
    await db_session.commit()

    _stub_provider(monkeypatch, send_connect_request=AsyncMock())

    result = await sequencer._send_linkedin_step_async(str(lead.id), str(node.id))
    assert result["status"] == "suppressed"
    await sequencer._record_execution_and_advance(lead.id, node.id, result)

    state = await db_session.scalar(
        _select(LeadSequenceState).where(LeadSequenceState.lead_id == lead.id)
        .execution_options(populate_existing=True)
    )
    assert state.status == LeadSequenceStatus.HALTED
    assert "suppression" in (state.halt_reason or "").lower()
