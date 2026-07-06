"""Phase 15: exception handler, request logging, /errors, /retry-failed,
and the imap_client password-decryption confinement."""
import logging
import uuid
from datetime import time
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.models import (
    Campaign,
    ComposeStatus,
    Lead,
    ResearchStatus,
    SendStatus,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


async def _make_campaign(db_session) -> Campaign:
    c = Campaign(
        name="P15", goal="g", tone="t",
        sender_name="s", sender_email="s@x.com",
        sample_count=1,
        schedule_days=[0, 1, 2, 3, 4],
        schedule_time_start=time(9, 0), schedule_time_end=time(17, 0),
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    return c


# --------------------------------------------------------------------------
# Exception handler
# --------------------------------------------------------------------------


async def test_unhandled_exception_returns_500_with_error_envelope(client):
    """If an endpoint raises an uncaught exception, the global handler should
    serialize a {error, detail} JSON response with status 500."""
    from app.main import app

    @app.get("/__boom__")
    async def _boom():
        raise RuntimeError("kaboom")

    try:
        resp = await client.get("/__boom__")
        assert resp.status_code == 500
        body = resp.json()
        assert body["error"] == "internal_error"
        assert "detail" in body
    finally:
        # Strip our ad-hoc route so other tests don't see it.
        app.router.routes = [r for r in app.router.routes if getattr(r, "path", "") != "/__boom__"]


async def test_http_exception_still_returns_default_shape(client):
    """The catch-all should NOT swallow FastAPI's normal HTTPException handler —
    404s keep the legacy {"detail": "..."} shape for client compatibility."""
    resp = await client.get(f"/campaigns/{uuid.uuid4()}")
    assert resp.status_code == 404
    body = resp.json()
    assert "detail" in body
    # Critically: 404 didn't get rewritten to status 500
    assert body.get("error") != "internal_error"


async def test_request_logging_middleware_runs(client, caplog):
    """Smoke test: middleware emits an info line per request."""
    with caplog.at_level(logging.INFO, logger="app.main"):
        resp = await client.get("/health")
    assert resp.status_code == 200
    matching = [r for r in caplog.records if "/health" in r.getMessage()]
    assert matching, "expected at least one log line for /health"


# --------------------------------------------------------------------------
# /errors endpoint
# --------------------------------------------------------------------------


async def test_errors_endpoint_returns_failed_leads(client, db_session):
    campaign = await _make_campaign(db_session)
    # Failed at different stages
    db_session.add(Lead(
        campaign_id=campaign.id, email="r@x.com",
        research_status=ResearchStatus.FAILED,
    ))
    db_session.add(Lead(
        campaign_id=campaign.id, email="c@x.com",
        research_status=ResearchStatus.DONE,
        compose_status=ComposeStatus.FAILED,
    ))
    db_session.add(Lead(
        campaign_id=campaign.id, email="s@x.com",
        research_status=ResearchStatus.DONE,
        compose_status=ComposeStatus.DONE,
        send_status=SendStatus.FAILED,
    ))
    # Healthy lead — should NOT appear.
    db_session.add(Lead(
        campaign_id=campaign.id, email="ok@x.com",
        research_status=ResearchStatus.DONE,
        compose_status=ComposeStatus.DONE,
        send_status=SendStatus.SENT,
    ))
    await db_session.commit()

    resp = await client.get(f"/campaigns/{campaign.id}/errors")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 3
    by_email = {it["email"]: it for it in items}
    assert by_email["r@x.com"]["failed_stage"] == "research"
    assert by_email["c@x.com"]["failed_stage"] == "compose"
    assert by_email["s@x.com"]["failed_stage"] == "send"


async def test_errors_endpoint_empty_when_no_failures(client, db_session):
    campaign = await _make_campaign(db_session)
    resp = await client.get(f"/campaigns/{campaign.id}/errors")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_errors_endpoint_404_for_unknown_campaign(client):
    resp = await client.get(f"/campaigns/{uuid.uuid4()}/errors")
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# /retry-failed endpoint
# --------------------------------------------------------------------------


async def test_retry_failed_resets_status_and_enqueues_each_stage(client, db_session):
    campaign = await _make_campaign(db_session)
    r_lead = Lead(campaign_id=campaign.id, email="r@x.com", research_status=ResearchStatus.FAILED)
    c_lead = Lead(
        campaign_id=campaign.id, email="c@x.com",
        research_status=ResearchStatus.DONE, compose_status=ComposeStatus.FAILED,
    )
    s_lead = Lead(
        campaign_id=campaign.id, email="s@x.com",
        research_status=ResearchStatus.DONE, compose_status=ComposeStatus.DONE,
        send_status=SendStatus.FAILED,
    )
    db_session.add_all([r_lead, c_lead, s_lead])
    await db_session.commit()

    with patch("app.workers.research.research_lead.delay") as research_q, \
         patch("app.workers.compose.compose_lead.delay") as compose_q, \
         patch("app.workers.send.send_lead.delay") as send_q:
        resp = await client.post(f"/campaigns/{campaign.id}/retry-failed")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"research_retried": 1, "compose_retried": 1, "send_retried": 1}

    research_q.assert_called_once_with(str(r_lead.id))
    compose_q.assert_called_once_with(str(c_lead.id))
    send_q.assert_called_once_with(str(s_lead.id))

    # Statuses reset to pending on each retried lead.
    refreshed_r = await db_session.scalar(select(Lead).where(Lead.id == r_lead.id))
    await db_session.refresh(refreshed_r)
    assert refreshed_r.research_status == ResearchStatus.PENDING

    refreshed_c = await db_session.scalar(select(Lead).where(Lead.id == c_lead.id))
    await db_session.refresh(refreshed_c)
    assert refreshed_c.compose_status == ComposeStatus.PENDING

    refreshed_s = await db_session.scalar(select(Lead).where(Lead.id == s_lead.id))
    await db_session.refresh(refreshed_s)
    assert refreshed_s.send_status == SendStatus.PENDING


async def test_retry_failed_no_failures_is_noop(client, db_session):
    campaign = await _make_campaign(db_session)
    with patch("app.workers.research.research_lead.delay") as research_q, \
         patch("app.workers.compose.compose_lead.delay") as compose_q, \
         patch("app.workers.send.send_lead.delay") as send_q:
        resp = await client.post(f"/campaigns/{campaign.id}/retry-failed")
    assert resp.status_code == 200
    assert resp.json() == {"research_retried": 0, "compose_retried": 0, "send_retried": 0}
    research_q.assert_not_called()
    compose_q.assert_not_called()
    send_q.assert_not_called()


async def test_retry_failed_404_for_unknown_campaign(client):
    resp = await client.post(f"/campaigns/{uuid.uuid4()}/retry-failed")
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# Password decryption is confined to imap_client
# --------------------------------------------------------------------------


def test_test_imap_with_account_decrypts_internally(monkeypatch):
    """test_imap_with_account decrypts the stored ciphertext and forwards the
    plaintext to test_imap_connection. The plaintext never enters the caller."""
    from app.services import encryption, imap_client

    # Patch test_imap_connection to capture the password it receives.
    captured = {}

    def fake_test(host, port, ssl, user, password):
        captured["password"] = password
        return {"ok": True, "message_count": 7}

    monkeypatch.setattr(imap_client, "test_imap_connection", fake_test)

    class _Acc:
        password_encrypted = encryption.encrypt("super-secret")
        imap_host = "imap.example.com"
        imap_port = 993
        imap_use_ssl = True
        username = "me@example.com"
        email_address = "me@example.com"

    result = imap_client.test_imap_with_account(_Acc())

    assert result == {"ok": True, "message_count": 7}
    assert captured["password"] == "super-secret"


def test_test_imap_with_account_returns_failure_on_bad_token(monkeypatch):
    from app.services import imap_client

    class _Acc:
        password_encrypted = "not-a-valid-fernet-token"
        email_address = "x@y.com"
        imap_host = "h"
        imap_port = 993
        imap_use_ssl = True
        username = "u"

    result = imap_client.test_imap_with_account(_Acc())
    assert result["ok"] is False
    assert "credential decrypt failed" in result["error"]


def test_fetch_recent_with_account_raises_on_bad_token():
    from app.services import imap_client
    from cryptography.fernet import InvalidToken

    class _Acc:
        password_encrypted = "garbage"
        imap_host = "h"
        imap_port = 993
        imap_use_ssl = True
        username = "u"
        email_address = "x@y.com"

    from datetime import datetime, timezone
    with pytest.raises(InvalidToken):
        imap_client.fetch_recent_with_account(_Acc(), datetime.now(timezone.utc))


def test_no_decrypt_calls_outside_imap_client():
    """Smoke check: searching the codebase for stray ``encryption.decrypt``
    calls outside the allowlisted modules should turn up nothing. Catches
    the 'someone added a decrypt call somewhere else' regression.

    Allowlist:
      - imap_client.py — encrypted IMAP-password consumer (only remaining
        plaintext-decrypt path now that the DIY LinkedIn providers were
        stripped in favour of Unipile, which never receives a plaintext
        password from us).
      - encryption.py — the encrypt/decrypt helpers themselves.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    # tenant_keys.py: the BYOK credential resolver (multi-tenancy Phase 4)
    # — decrypts per-tenant provider keys; plaintext stays local-scope.
    allowed = {"imap_client.py", "encryption.py", "tenant_keys.py"}
    offenders = []
    for path in root.rglob("*.py"):
        if path.name in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        if "encryption.decrypt(" in text:
            offenders.append(str(path.relative_to(root)))
    assert not offenders, (
        f"encryption.decrypt() must only be called inside the allowlisted "
        f"modules ({sorted(allowed)}) but was found in: {offenders}"
    )
