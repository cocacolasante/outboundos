from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import LinkedInAccount, LinkedInAccountStatus
from app.schemas.linkedin_account import (
    ConnectViaUnipileRequest,
    ConnectViaUnipileResponse,
    DiscoverableUnipileAccount,
    ImportFromUnipileRequest,
    LinkedInAccountResponse,
    LinkedInAccountUpdate,
    LinkedInTestResponse,
    ResolveChallengeRequest,
)
from app.services.linkedin import ambient_provider
from app.services.linkedin.base import ChallengeRequired, AccountRestricted
from app.services.linkedin.unipile_impl import UnipileError, UnipileLinkedInProvider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/linkedin-accounts", tags=["linkedin-accounts"])


async def _get_or_404(db: AsyncSession, account_id: uuid.UUID) -> LinkedInAccount:
    acc = await db.get(LinkedInAccount, account_id)
    if acc is None:
        raise HTTPException(status_code=404, detail="LinkedIn account not found")
    return acc


@router.post(
    "/connect-via-unipile",
    response_model=ConnectViaUnipileResponse,
    status_code=status.HTTP_201_CREATED,
)
async def connect_via_unipile(
    payload: ConnectViaUnipileRequest, db: AsyncSession = Depends(get_db),
) -> ConnectViaUnipileResponse:
    """Begin a Unipile hosted-auth flow.

    Creates a placeholder ``LinkedInAccount`` row and asks Unipile for a
    hosted login URL.  The frontend opens that URL in a new tab; Unipile
    drives the LinkedIn login; on success Unipile fires the
    ``account.connected`` webhook with this account's local id encoded in
    the ``name`` field so our webhook handler can correlate.

    The placeholder row carries no ``linkedin_email`` until Unipile reports
    back — we pull it from the webhook payload and persist it then.
    """
    from app.billing.entitlements import QuotaExceeded, check_static_limit

    try:
        await check_static_limit(db, "linkedin_accounts", LinkedInAccount)
    except QuotaExceeded as exc:
        raise HTTPException(status_code=402, detail=str(exc))

    if not settings.UNIPILE_API_KEY or not settings.UNIPILE_DSN:
        raise HTTPException(
            status_code=503,
            detail="Unipile not configured — set UNIPILE_DSN + UNIPILE_API_KEY.",
        )

    acc = LinkedInAccount(
        label=payload.label,
        linkedin_email="(pending Unipile auth)",
        provider_kind="unipile",
        status=LinkedInAccountStatus.UNTESTED,
    )
    db.add(acc)
    await db.commit()
    await db.refresh(acc)

    # The "name" we pass through to Unipile is our local account id —
    # Unipile echoes it back in webhook events so we can correlate which
    # Unipile account_id belongs to which row.
    provider = await ambient_provider()
    try:
        notify_url = f"{settings.WEBHOOK_BASE_URL.rstrip('/')}/webhooks/unipile"
        link = await provider.create_hosted_auth_link(
            success_redirect_url=payload.success_redirect_url,
            failure_redirect_url=payload.failure_redirect_url,
            notify_url=notify_url,
            name=str(acc.id),
        )
    except UnipileError as exc:
        # Roll back the placeholder row so we don't leave orphans.
        await db.delete(acc)
        await db.commit()
        raise HTTPException(status_code=502, detail=f"Unipile error: {exc}") from exc

    hosted_url = link.get("url") or link.get("hosted_url") or ""
    if not hosted_url:
        await db.delete(acc)
        await db.commit()
        raise HTTPException(
            status_code=502, detail="Unipile returned no hosted URL",
        )
    return ConnectViaUnipileResponse(account_id=acc.id, hosted_url=hosted_url)


@router.post("/{account_id}/sync-unipile", response_model=LinkedInAccountResponse)
async def sync_unipile_status(
    account_id: uuid.UUID, db: AsyncSession = Depends(get_db),
) -> LinkedInAccount:
    """Polling fallback for the hosted-auth flow.

    If the user finishes Unipile's flow but the webhook hasn't reached us
    (dev without a public URL, transient delivery issue, etc.), the
    frontend can call this to pull the current account status straight
    from Unipile.  Idempotent: if the row already has a unipile_account_id
    we just refresh its status.
    """
    acc = await _get_or_404(db, account_id)
    if not acc.unipile_account_id:
        # Hosted flow hasn't completed yet — nothing to sync.  The frontend
        # should keep polling.
        return acc
    provider = await ambient_provider()
    try:
        status_payload = await provider.fetch_account_status(acc.unipile_account_id)
    except UnipileError as exc:
        acc.last_error = str(exc)
        await db.commit()
        await db.refresh(acc)
        return acc
    _apply_unipile_status(acc, status_payload)
    await db.commit()
    await db.refresh(acc)
    return acc


def _apply_unipile_status(acc: LinkedInAccount, payload: dict) -> None:
    """Translate a Unipile account-status dict onto our model fields.

    Unipile's GET /accounts/{id} returns the per-data-stream status under
    ``sources[].status`` rather than top-level — we fall back to the first
    source's status when no rollup field is present.
    """
    src_raw = (
        payload.get("status")
        or payload.get("connection_status")
        or next(
            (s.get("status") for s in (payload.get("sources") or [])
             if isinstance(s, dict) and s.get("status")),
            None,
        )
        or ""
    )
    src = str(src_raw).upper()
    if src in {"OK", "CONNECTED", "ACTIVE"}:
        acc.status = LinkedInAccountStatus.OK
        acc.last_error = None
        acc.pending_challenge_url = None
    elif "CHECKPOINT" in src or "2FA" in src or "OTP" in src:
        acc.status = LinkedInAccountStatus.CHALLENGED
        acc.pending_challenge_url = "https://www.linkedin.com"
        acc.last_error = payload.get("detail") or payload.get("message") or src
    elif "BANNED" in src or "RESTRICTED" in src or "SUSPENDED" in src:
        acc.status = LinkedInAccountStatus.RESTRICTED
        acc.last_error = payload.get("detail") or payload.get("message") or src
    elif src in {"DISCONNECTED", "CREDENTIALS"}:
        acc.status = LinkedInAccountStatus.FAILED
        acc.last_error = payload.get("detail") or payload.get("message") or src
    # Pick up the email if Unipile resolved it.
    email = (
        payload.get("user_email")
        or payload.get("linkedin_email")
        or (payload.get("user") or {}).get("email")
    )
    if email:
        acc.linkedin_email = email


@router.get("/", response_model=list[LinkedInAccountResponse])
async def list_accounts(db: AsyncSession = Depends(get_db)) -> list[LinkedInAccount]:
    rows = (await db.execute(
        select(LinkedInAccount).order_by(LinkedInAccount.created_at.desc())
    )).scalars().all()
    return list(rows)


@router.get(
    "/discoverable",
    response_model=list[DiscoverableUnipileAccount],
)
async def list_discoverable_unipile_accounts(
    db: AsyncSession = Depends(get_db),
) -> list[DiscoverableUnipileAccount]:
    """List LinkedIn accounts that exist in Unipile but aren't yet bound to
    a local ``LinkedInAccount`` row.

    Useful when the user connected LinkedIn via Unipile's dashboard
    (instead of our portal's "Connect via Unipile" hosted flow). They
    can then bind one of these via ``POST /import-from-unipile``.
    """
    if not settings.UNIPILE_API_KEY or not settings.UNIPILE_DSN:
        raise HTTPException(
            status_code=503,
            detail="Unipile not configured — set UNIPILE_DSN + UNIPILE_API_KEY.",
        )

    # IDs already bound locally — we exclude these from the discoverable
    # list so the UI never shows already-imported accounts.
    bound_rows = (await db.execute(
        select(LinkedInAccount.unipile_account_id)
        .where(LinkedInAccount.unipile_account_id.is_not(None))
    )).all()
    bound_ids = {r[0] for r in bound_rows if r[0]}

    provider = await ambient_provider()
    try:
        accounts = await provider.list_accounts()
    except UnipileError as exc:
        raise HTTPException(status_code=502, detail=f"Unipile error: {exc}") from exc

    out: list[DiscoverableUnipileAccount] = []
    for a in accounts:
        aid = a.get("id")
        if not aid or aid in bound_ids:
            continue
        im = (a.get("connection_params") or {}).get("im") or {}
        # Unipile exposes `sources[].status` per data-stream; the
        # account-level rollup is usually the first source's status.
        srcstatus: str | None = None
        for s in (a.get("sources") or []):
            if isinstance(s, dict) and s.get("status"):
                srcstatus = s["status"]
                break
        out.append(DiscoverableUnipileAccount(
            unipile_account_id=aid,
            name=im.get("username") or a.get("name"),
            linkedin_email=a.get("user_email") or a.get("linkedin_email"),
            public_identifier=im.get("publicIdentifier"),
            status=srcstatus,
        ))
    return out


@router.post(
    "/import-from-unipile",
    response_model=LinkedInAccountResponse,
    status_code=status.HTTP_201_CREATED,
)
async def import_from_unipile(
    payload: ImportFromUnipileRequest,
    db: AsyncSession = Depends(get_db),
) -> LinkedInAccount:
    """Bind a Unipile-side LinkedIn account to a fresh local row.

    Idempotency: if a row already exists with this ``unipile_account_id``
    we return 409.  The DB-level unique index also defends against races.
    """
    if not settings.UNIPILE_API_KEY or not settings.UNIPILE_DSN:
        raise HTTPException(
            status_code=503,
            detail="Unipile not configured — set UNIPILE_DSN + UNIPILE_API_KEY.",
        )

    existing = await db.scalar(
        select(LinkedInAccount).where(
            LinkedInAccount.unipile_account_id == payload.unipile_account_id
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"This Unipile account is already bound to local row {existing.id}.",
        )

    provider = await ambient_provider()
    try:
        status_payload = await provider.fetch_account_status(payload.unipile_account_id)
    except UnipileError as exc:
        raise HTTPException(status_code=502, detail=f"Unipile error: {exc}") from exc

    # Pull a reasonable linkedin_email from Unipile's payload.
    im = (status_payload.get("connection_params") or {}).get("im") or {}
    linkedin_email = (
        status_payload.get("user_email")
        or status_payload.get("linkedin_email")
        or im.get("username")
        or "(unknown)"
    )

    acc = LinkedInAccount(
        label=payload.label,
        linkedin_email=linkedin_email,
        provider_kind="unipile",
        unipile_account_id=payload.unipile_account_id,
        status=LinkedInAccountStatus.UNTESTED,
    )
    _apply_unipile_status(acc, status_payload)
    db.add(acc)
    await db.commit()
    await db.refresh(acc)
    return acc


@router.get("/{account_id}", response_model=LinkedInAccountResponse)
async def get_account(
    account_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> LinkedInAccount:
    return await _get_or_404(db, account_id)


@router.patch("/{account_id}", response_model=LinkedInAccountResponse)
async def update_account(
    account_id: uuid.UUID,
    payload: LinkedInAccountUpdate,
    db: AsyncSession = Depends(get_db),
) -> LinkedInAccount:
    acc = await _get_or_404(db, account_id)
    updates = payload.model_dump(exclude_unset=True)
    for k, v in updates.items():
        setattr(acc, k, v)
    await db.commit()
    await db.refresh(acc)
    return acc


@router.delete(
    "/{account_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None,
)
async def delete_account(
    account_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> None:
    acc = await _get_or_404(db, account_id)
    # campaigns.linkedin_account_id is ON DELETE SET NULL at DB level, so
    # campaigns pointing at this account just lose the reference.
    unipile_id = acc.unipile_account_id
    await db.delete(acc)
    await db.commit()
    # Free the Unipile-side resource so we don't leak a dangling session
    # there.  Best-effort: a failure here is logged but does not block the
    # local delete.
    if unipile_id:
        try:
            await (await ambient_provider()).delete_account(unipile_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Unipile delete_account(%s) failed (non-fatal): %s",
                unipile_id, exc,
            )


@router.post("/{account_id}/test", response_model=LinkedInTestResponse)
async def test_account(
    account_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> LinkedInTestResponse:
    """Verify the account is healthy on Unipile's side."""
    acc = await _get_or_404(db, account_id)
    provider = await ambient_provider()
    acc.last_tested_at = datetime.now(timezone.utc)

    try:
        result = await provider.test_connection(acc)
    except ChallengeRequired as exc:
        acc.status = LinkedInAccountStatus.CHALLENGED
        acc.last_error = str(exc)
        acc.pending_challenge_url = exc.challenge_url or "https://www.linkedin.com"
        await db.commit()
        return LinkedInTestResponse(
            ok=False, status=acc.status, error=str(exc),
            challenge_url=acc.pending_challenge_url,
        )
    except AccountRestricted as exc:
        acc.status = LinkedInAccountStatus.RESTRICTED
        acc.last_error = str(exc)
        await db.commit()
        return LinkedInTestResponse(ok=False, status=acc.status, error=str(exc))

    if result.ok:
        acc.status = LinkedInAccountStatus.OK
        acc.last_error = None
        acc.pending_challenge_url = None
    else:
        # The provider's test_connection encodes its own challenge case
        # in meta — surface it here for consistency.
        if result.meta and result.meta.get("challenged"):
            acc.status = LinkedInAccountStatus.CHALLENGED
        elif result.meta and result.meta.get("restricted"):
            acc.status = LinkedInAccountStatus.RESTRICTED
        else:
            acc.status = LinkedInAccountStatus.FAILED
        acc.last_error = result.error
    await db.commit()
    await db.refresh(acc)
    return LinkedInTestResponse(
        ok=result.ok,
        status=acc.status,
        error=result.error,
        challenge_url=acc.pending_challenge_url,
        meta=result.meta,
    )


@router.post("/{account_id}/resolve-challenge", response_model=LinkedInAccountResponse)
async def resolve_challenge(
    account_id: uuid.UUID,
    payload: ResolveChallengeRequest,  # noqa: ARG001 — kept for future use
    db: AsyncSession = Depends(get_db),
) -> LinkedInAccount:
    """User has manually completed the captcha/PIN in their own browser.

    We clear the pending challenge state and flip status back to UNTESTED;
    the user is expected to click "Test" again next.
    """
    acc = await _get_or_404(db, account_id)
    acc.pending_challenge_url = None
    acc.status = LinkedInAccountStatus.UNTESTED
    acc.last_error = None
    await db.commit()
    await db.refresh(acc)
    return acc
