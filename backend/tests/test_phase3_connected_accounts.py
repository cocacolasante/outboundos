"""Phase 3: connected accounts CRUD + test endpoint."""
from datetime import time
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.models import Campaign, ConnectedAccount, ConnectedAccountTestStatus
from app.services import encryption


def _account_payload(**overrides) -> dict:
    base = {
        "label": "Work Gmail",
        "email_address": "me@gmail.com",
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "imap_use_ssl": True,
        "username": "me@gmail.com",
        "password": "app-password-123",
    }
    base.update(overrides)
    return base


# ---------- Create ----------


async def test_create_account_persists_encrypted_password(client, db_session):
    resp = await client.post("/connected-accounts/", json=_account_payload())
    assert resp.status_code == 201
    body = resp.json()

    # Password must NEVER appear in the response (any field).
    assert "password" not in body
    assert "password_encrypted" not in body
    flattened = str(body)
    assert "app-password-123" not in flattened

    # Stored ciphertext decrypts back to plaintext.
    acc = await db_session.scalar(
        select(ConnectedAccount).where(ConnectedAccount.id == body["id"])
    )
    assert acc is not None
    assert acc.password_encrypted != "app-password-123"
    assert encryption.decrypt(acc.password_encrypted) == "app-password-123"

    # Defaults from the model surface in the response.
    assert body["last_test_status"] == "untested"
    assert body["last_tested_at"] is None
    assert body["imap_port"] == 993


async def test_create_rejects_missing_required_fields(client):
    resp = await client.post("/connected-accounts/", json={"label": "x"})
    assert resp.status_code == 422


async def test_create_rejects_invalid_port(client):
    resp = await client.post("/connected-accounts/", json=_account_payload(imap_port=99999))
    assert resp.status_code == 422


# ---------- List / Get ----------


async def test_list_returns_all_accounts(client):
    await client.post("/connected-accounts/", json=_account_payload(label="A", email_address="a@x.com"))
    await client.post("/connected-accounts/", json=_account_payload(label="B", email_address="b@x.com"))

    resp = await client.get("/connected-accounts/")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    labels = {a["label"] for a in body}
    assert labels == {"A", "B"}


async def test_get_account_by_id(client):
    created = (await client.post("/connected-accounts/", json=_account_payload())).json()
    resp = await client.get(f"/connected-accounts/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["label"] == "Work Gmail"


async def test_get_account_not_found(client):
    resp = await client.get("/connected-accounts/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


# ---------- Update ----------


async def test_update_account_relabels(client):
    created = (await client.post("/connected-accounts/", json=_account_payload())).json()
    resp = await client.patch(f"/connected-accounts/{created['id']}", json={"label": "Renamed"})
    assert resp.status_code == 200
    assert resp.json()["label"] == "Renamed"


async def test_update_re_encrypts_password(client, db_session):
    created = (await client.post("/connected-accounts/", json=_account_payload())).json()

    acc = await db_session.scalar(
        select(ConnectedAccount).where(ConnectedAccount.id == created["id"])
    )
    original_ct = acc.password_encrypted

    resp = await client.patch(
        f"/connected-accounts/{created['id']}", json={"password": "new-password-456"}
    )
    assert resp.status_code == 200
    assert "password" not in resp.json()
    assert "new-password-456" not in str(resp.json())

    # Re-read from a fresh fetch — different ciphertext, decrypts to new value.
    refreshed = await db_session.scalar(
        select(ConnectedAccount).where(ConnectedAccount.id == created["id"])
    )
    await db_session.refresh(refreshed)
    assert refreshed.password_encrypted != original_ct
    assert encryption.decrypt(refreshed.password_encrypted) == "new-password-456"


async def test_update_without_password_keeps_existing(client, db_session):
    created = (await client.post("/connected-accounts/", json=_account_payload())).json()
    acc = await db_session.scalar(
        select(ConnectedAccount).where(ConnectedAccount.id == created["id"])
    )
    original_ct = acc.password_encrypted

    resp = await client.patch(
        f"/connected-accounts/{created['id']}", json={"imap_host": "imap.changed.com"}
    )
    assert resp.status_code == 200

    refreshed = await db_session.scalar(
        select(ConnectedAccount).where(ConnectedAccount.id == created["id"])
    )
    await db_session.refresh(refreshed)
    assert refreshed.password_encrypted == original_ct
    assert refreshed.imap_host == "imap.changed.com"


# ---------- Delete + cascade-to-NULL ----------


async def test_delete_account_sets_referencing_campaign_fk_null(client, db_session):
    created = (await client.post("/connected-accounts/", json=_account_payload())).json()
    acc_id = created["id"]

    # Insert a campaign that references this account, via the DB directly
    # (Campaign API arrives in Phase 4).
    import uuid as uuidlib

    campaign = Campaign(
        name="Test campaign",
        goal="Book a demo",
        tone="Friendly",
        sender_name="Anthony",
        sender_email="anthony@example.com",
        connected_account_id=uuidlib.UUID(acc_id),
        schedule_time_start=time(9, 0),
        schedule_time_end=time(17, 0),
    )
    db_session.add(campaign)
    await db_session.commit()
    campaign_id = campaign.id

    resp = await client.delete(f"/connected-accounts/{acc_id}")
    assert resp.status_code == 204

    # Connected account is gone…
    assert (
        await db_session.scalar(select(ConnectedAccount).where(ConnectedAccount.id == acc_id))
    ) is None

    # …but the campaign survives, with the FK nulled out (verified via raw SQL
    # so we bypass the ORM identity map).
    from sqlalchemy import text

    row = (
        await db_session.execute(
            text("SELECT connected_account_id FROM campaigns WHERE id = :i"),
            {"i": str(campaign_id)},
        )
    ).first()
    assert row is not None
    assert row[0] is None


async def test_delete_missing_account_returns_404(client):
    resp = await client.delete("/connected-accounts/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


# ---------- IMAP test endpoint ----------


async def test_test_endpoint_success_updates_status(client, db_session):
    created = (await client.post("/connected-accounts/", json=_account_payload())).json()

    with patch(
        "app.routers.connected_accounts.imap_client.test_imap_connection",
        return_value={"ok": True, "message_count": 42},
    ) as fn:
        resp = await client.post(f"/connected-accounts/{created['id']}/test")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"ok": True, "error": None, "message_count": 42}

    # The handler must pass the decrypted plaintext, not the ciphertext, to imap_client.
    args, kwargs = fn.call_args
    # imap_client.test_imap_connection(host, port, use_ssl, username, password)
    passed = args + tuple(kwargs.values())
    assert "app-password-123" in passed
    assert "imap.gmail.com" in passed

    refreshed = await db_session.scalar(
        select(ConnectedAccount).where(ConnectedAccount.id == created["id"])
    )
    await db_session.refresh(refreshed)
    assert refreshed.last_test_status == ConnectedAccountTestStatus.OK
    assert refreshed.last_test_error is None
    assert refreshed.last_tested_at is not None


async def test_test_endpoint_failure_records_error(client, db_session):
    created = (await client.post("/connected-accounts/", json=_account_payload())).json()

    with patch(
        "app.routers.connected_accounts.imap_client.test_imap_connection",
        return_value={"ok": False, "error": "authentication failed"},
    ):
        resp = await client.post(f"/connected-accounts/{created['id']}/test")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error"] == "authentication failed"
    assert body["message_count"] is None

    refreshed = await db_session.scalar(
        select(ConnectedAccount).where(ConnectedAccount.id == created["id"])
    )
    await db_session.refresh(refreshed)
    assert refreshed.last_test_status == ConnectedAccountTestStatus.FAILED
    assert refreshed.last_test_error == "authentication failed"


# ---------- Status endpoint ----------


async def test_status_endpoint_returns_summary_without_password(client):
    created = (await client.post("/connected-accounts/", json=_account_payload())).json()

    resp = await client.get(f"/connected-accounts/{created['id']}/status")
    assert resp.status_code == 200
    body = resp.json()
    # Spec: id, label, email_address, last_tested_at, last_test_status,
    # last_test_error, last_polled_at
    assert set(body.keys()) >= {
        "id", "label", "email_address",
        "last_tested_at", "last_test_status", "last_test_error", "last_polled_at",
    }
    assert "password" not in body
    assert "password_encrypted" not in body
    assert "imap_host" not in body  # status endpoint returns a lean summary


# ---------- Duplicate email enforcement ----------


async def test_create_duplicate_email_returns_409(client):
    """Two accounts with the same email_address must be rejected."""
    payload = _account_payload(email_address="dup@example.com")
    r1 = await client.post("/connected-accounts/", json=payload)
    assert r1.status_code == 201

    r2 = await client.post("/connected-accounts/", json=payload)
    assert r2.status_code == 409
    assert "dup@example.com" in r2.json()["detail"]


async def test_create_different_emails_both_succeed(client):
    r1 = await client.post("/connected-accounts/", json=_account_payload(email_address="a@x.com"))
    r2 = await client.post("/connected-accounts/", json=_account_payload(email_address="b@x.com"))
    assert r1.status_code == 201
    assert r2.status_code == 201


async def test_update_does_not_block_same_account_email(client):
    """PATCH should be able to update fields without conflicting with its own email."""
    r = await client.post("/connected-accounts/", json=_account_payload(email_address="patch@x.com"))
    acc_id = r.json()["id"]
    # Update the label — email stays the same — should not 409.
    patch_r = await client.patch(f"/connected-accounts/{acc_id}", json={"label": "New label"})
    assert patch_r.status_code == 200
    assert patch_r.json()["label"] == "New label"


async def test_delete_frees_email_for_reuse(client):
    """After deleting an account, its email can be re-registered."""
    payload = _account_payload(email_address="reuse@x.com")
    r1 = await client.post("/connected-accounts/", json=payload)
    acc_id = r1.json()["id"]

    await client.delete(f"/connected-accounts/{acc_id}")

    r2 = await client.post("/connected-accounts/", json=payload)
    assert r2.status_code == 201


# ---------- Default sender (atomic single-default invariant) -------------

async def test_create_defaults_is_default_sender_to_false(client):
    """A freshly created inbox is NOT the default sender — the user has
    to opt in explicitly."""
    resp = await client.post("/connected-accounts/", json=_account_payload(email_address="x@a.com"))
    assert resp.status_code == 201
    assert resp.json()["is_default_sender"] is False


async def test_set_is_default_sender_true_via_patch(client):
    a = await client.post("/connected-accounts/", json=_account_payload(email_address="a@x.com"))
    aid = a.json()["id"]
    resp = await client.patch(
        f"/connected-accounts/{aid}", json={"is_default_sender": True},
    )
    assert resp.status_code == 200
    assert resp.json()["is_default_sender"] is True


async def test_setting_default_sender_clears_other_defaults(client):
    """Promoting a second account to default MUST clear the flag on the
    first one in the same transaction.  Without that, the partial unique
    index would reject the write — we want a graceful swap instead."""
    a = await client.post("/connected-accounts/", json=_account_payload(email_address="a@x.com"))
    b = await client.post("/connected-accounts/", json=_account_payload(email_address="b@x.com"))
    aid = a.json()["id"]
    bid = b.json()["id"]

    # Promote A first.
    await client.patch(f"/connected-accounts/{aid}", json={"is_default_sender": True})
    # Then promote B — A must be demoted.
    resp = await client.patch(f"/connected-accounts/{bid}", json={"is_default_sender": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_default_sender"] is True

    # Refetch A; should now be False.
    refreshed_a = await client.get(f"/connected-accounts/{aid}")
    assert refreshed_a.json()["is_default_sender"] is False


async def test_clearing_is_default_sender_works(client):
    """Setting is_default_sender to False explicitly unsets the flag,
    leaving zero defaults in the workspace (fall back to env var)."""
    a = await client.post("/connected-accounts/", json=_account_payload(email_address="a@x.com"))
    aid = a.json()["id"]
    await client.patch(f"/connected-accounts/{aid}", json={"is_default_sender": True})

    resp = await client.patch(f"/connected-accounts/{aid}", json={"is_default_sender": False})
    assert resp.json()["is_default_sender"] is False


async def test_partial_unique_index_enforces_single_default(client, db_session):
    """Belt-and-suspenders: even if the router logic were bypassed, the
    DB-level partial unique index would reject a second default-sender
    row.  Verified by directly INSERTing two rows with the flag True."""
    from app.models import ConnectedAccount as Acc

    a = Acc(
        label="A", email_address="a@y.com",
        imap_host="i.x", imap_port=993, imap_use_ssl=True,
        username="a@y.com", password_encrypted="x",
        is_default_sender=True,
    )
    b = Acc(
        label="B", email_address="b@y.com",
        imap_host="i.x", imap_port=993, imap_use_ssl=True,
        username="b@y.com", password_encrypted="x",
        is_default_sender=True,
    )
    db_session.add_all([a, b])
    with pytest.raises(Exception):
        await db_session.commit()
