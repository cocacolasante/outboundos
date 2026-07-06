"""Tests for the /linkedin-accounts router.

The DIY password+li_at create path was stripped along with the Playwright
provider — all rows now originate from the Unipile hosted-auth flow.  We
insert rows directly via SQLAlchemy here so the CRUD endpoints can be
exercised without standing up Unipile.
"""
import uuid

from app.models import LinkedInAccount, LinkedInAccountStatus
from app.services.linkedin.base import ChallengeRequired, ActionResult


def _async_provider(stub):
    """BYOK (Phase 4): providers resolve per tenant via the async
    ``ambient_provider`` — stubs must be awaitable."""
    async def _get():
        return stub
    return _get



async def _seed_account(db_session) -> LinkedInAccount:
    acc = LinkedInAccount(
        label="throwaway",
        linkedin_email="throwaway@example.com",
        provider_kind="unipile",
        unipile_account_id=f"unipile-{uuid.uuid4()}",
        status=LinkedInAccountStatus.UNTESTED,
    )
    db_session.add(acc)
    await db_session.commit()
    await db_session.refresh(acc)
    return acc


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------


async def test_list_empty(client):
    r = await client.get("/linkedin-accounts/")
    assert r.status_code == 200
    assert r.json() == []


async def test_list_returns_seeded_row(client, db_session):
    acc = await _seed_account(db_session)
    r = await client.get("/linkedin-accounts/")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["id"] == str(acc.id)
    assert body[0]["linkedin_email"] == "throwaway@example.com"


async def test_get_and_404(client, db_session):
    acc = await _seed_account(db_session)
    r = await client.get(f"/linkedin-accounts/{acc.id}")
    assert r.status_code == 200
    r2 = await client.get(f"/linkedin-accounts/{uuid.uuid4()}")
    assert r2.status_code == 404


async def test_patch_updates_label_only(client, db_session):
    acc = await _seed_account(db_session)
    r = await client.patch(
        f"/linkedin-accounts/{acc.id}", json={"label": "renamed"},
    )
    assert r.status_code == 200
    assert r.json()["label"] == "renamed"


async def test_delete(client, db_session):
    acc = await _seed_account(db_session)
    r = await client.delete(f"/linkedin-accounts/{acc.id}")
    assert r.status_code == 204
    r2 = await client.get(f"/linkedin-accounts/{acc.id}")
    assert r2.status_code == 404


# --------------------------------------------------------------------------
# /test + /resolve-challenge
# --------------------------------------------------------------------------


async def test_test_endpoint_success(client, db_session, monkeypatch):
    """Provider returns OK → account flips to OK."""
    acc = await _seed_account(db_session)

    async def fake_test_connection(account):
        return ActionResult(ok=True)

    from unittest.mock import MagicMock
    mock_prov = MagicMock()
    mock_prov.test_connection = fake_test_connection

    import app.routers.linkedin_accounts as _router
    monkeypatch.setattr(_router, "ambient_provider", _async_provider(mock_prov))

    rt = await client.post(f"/linkedin-accounts/{acc.id}/test")
    assert rt.status_code == 200, rt.text
    body = rt.json()
    assert body["ok"] is True
    assert body["status"] == "ok"

    await db_session.refresh(acc)
    refreshed = acc
    assert refreshed.status == LinkedInAccountStatus.OK


async def test_test_endpoint_challenged(client, db_session, monkeypatch):
    acc = await _seed_account(db_session)

    async def fake_test_connection(account):
        raise ChallengeRequired(
            "captcha required", challenge_url="https://www.linkedin.com/checkpoint/x",
        )

    from unittest.mock import MagicMock
    mock_prov = MagicMock()
    mock_prov.test_connection = fake_test_connection

    import app.routers.linkedin_accounts as _router
    monkeypatch.setattr(_router, "ambient_provider", _async_provider(mock_prov))

    rt = await client.post(f"/linkedin-accounts/{acc.id}/test")
    body = rt.json()
    assert body["ok"] is False
    assert body["status"] == "challenged"
    assert body["challenge_url"].startswith("https://www.linkedin.com/checkpoint")

    await db_session.refresh(acc)
    refreshed = acc
    assert refreshed.status == LinkedInAccountStatus.CHALLENGED
    assert refreshed.pending_challenge_url is not None


async def test_resolve_challenge_flips_back_to_untested(client, db_session):
    acc = await _seed_account(db_session)
    acc.status = LinkedInAccountStatus.CHALLENGED
    acc.pending_challenge_url = "https://example.com/check"
    acc.last_error = "captcha required"
    await db_session.commit()

    r = await client.post(f"/linkedin-accounts/{acc.id}/resolve-challenge", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "untested"
    assert body["pending_challenge_url"] is None
