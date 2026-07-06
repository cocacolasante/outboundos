from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import ConnectedAccount, ConnectedAccountTestStatus
from app.schemas.connected_account import (
    ConnectedAccountCreate,
    ConnectedAccountResponse,
    ConnectedAccountStatus,
    ConnectedAccountUpdate,
    ImapTestResponse,
)
from app.services import encryption, imap_client  # encryption used for create/update only

router = APIRouter(prefix="/connected-accounts", tags=["connected-accounts"])


async def _get_or_404(db: AsyncSession, account_id: uuid.UUID) -> ConnectedAccount:
    acc = await db.get(ConnectedAccount, account_id)
    if acc is None:
        raise HTTPException(status_code=404, detail="Connected account not found")
    return acc


@router.post("/", response_model=ConnectedAccountResponse, status_code=status.HTTP_201_CREATED)
async def create_account(
    payload: ConnectedAccountCreate,
    db: AsyncSession = Depends(get_db),
) -> ConnectedAccount:
    # Billing (Phase 5): plan cap on connected sending inboxes.
    from app.billing.entitlements import QuotaExceeded, check_static_limit

    try:
        await check_static_limit(db, "connected_accounts", ConnectedAccount)
    except QuotaExceeded as exc:
        raise HTTPException(status_code=402, detail=str(exc))
    existing = await db.execute(
        select(ConnectedAccount).where(ConnectedAccount.email_address == payload.email_address)
    )
    if existing.scalars().first() is not None:
        raise HTTPException(
            status_code=409,
            detail=f"An inbox for {payload.email_address} is already connected.",
        )
    acc = ConnectedAccount(
        label=payload.label,
        email_address=payload.email_address,
        imap_host=payload.imap_host,
        imap_port=payload.imap_port,
        imap_use_ssl=payload.imap_use_ssl,
        username=payload.username,
        password_encrypted=encryption.encrypt(payload.password),
        signature=(payload.signature or None) if payload.signature else None,
    )
    db.add(acc)
    await db.commit()
    await db.refresh(acc)
    return acc


@router.get("/", response_model=list[ConnectedAccountResponse])
async def list_accounts(db: AsyncSession = Depends(get_db)) -> list[ConnectedAccount]:
    result = await db.execute(select(ConnectedAccount).order_by(ConnectedAccount.created_at.desc()))
    return list(result.scalars().all())


@router.get("/{account_id}", response_model=ConnectedAccountResponse)
async def get_account(
    account_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> ConnectedAccount:
    return await _get_or_404(db, account_id)


@router.patch("/{account_id}", response_model=ConnectedAccountResponse)
async def update_account(
    account_id: uuid.UUID,
    payload: ConnectedAccountUpdate,
    db: AsyncSession = Depends(get_db),
) -> ConnectedAccount:
    acc = await _get_or_404(db, account_id)
    updates = payload.model_dump(exclude_unset=True)
    if "password" in updates:
        plaintext = updates.pop("password")
        acc.password_encrypted = encryption.encrypt(plaintext)
    # Default-sender swap is atomic: clear EVERY other row's flag in the
    # same transaction before flipping this one True.  Without this, two
    # concurrent set-default requests could leave two rows marked
    # default (the partial unique index would block one of them, but
    # the failed request would still 500 instead of degrading cleanly).
    promote_to_default = updates.pop("is_default_sender", None)
    for key, value in updates.items():
        setattr(acc, key, value)
    if promote_to_default is True:
        await db.execute(
            update(ConnectedAccount)
            .where(ConnectedAccount.id != acc.id)
            .where(ConnectedAccount.is_default_sender.is_(True))
            .values(is_default_sender=False)
        )
        acc.is_default_sender = True
    elif promote_to_default is False:
        acc.is_default_sender = False
    await db.commit()
    await db.refresh(acc)
    return acc


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_account(
    account_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    acc = await _get_or_404(db, account_id)
    # Campaigns referencing this account get connected_account_id set to NULL
    # automatically by the DB-level ON DELETE SET NULL FK.
    await db.delete(acc)
    await db.commit()


@router.post("/{account_id}/test", response_model=ImapTestResponse)
async def test_account(
    account_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> ImapTestResponse:
    acc = await _get_or_404(db, account_id)
    # All decryption is confined to imap_client; plaintext never enters this
    # router's scope.
    result = await asyncio.to_thread(imap_client.test_imap_with_account, acc)

    acc.last_tested_at = datetime.now(timezone.utc)
    if result.get("ok"):
        acc.last_test_status = ConnectedAccountTestStatus.OK
        acc.last_test_error = None
    else:
        acc.last_test_status = ConnectedAccountTestStatus.FAILED
        acc.last_test_error = result.get("error")
    await db.commit()

    return ImapTestResponse(
        ok=bool(result.get("ok")),
        error=result.get("error"),
        message_count=result.get("message_count"),
    )


@router.get("/{account_id}/status", response_model=ConnectedAccountStatus)
async def get_account_status(
    account_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> ConnectedAccount:
    return await _get_or_404(db, account_id)
