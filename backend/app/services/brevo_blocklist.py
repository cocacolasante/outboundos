"""Sync Brevo's blocked-contacts list into our suppression (ignore) list.

Brevo keeps the authoritative record of transactional recipients it will no
longer deliver to — hard bounces, unsubscribes, spam complaints, and
admin/blocklisted contacts — at ``GET /v3/smtp/blockedContacts``.  This module
pulls that list and runs each address through the shared ``suppress_email``
core, so a bounced/blocked/unsubscribed recipient is added to the ignore list,
halted in every current campaign, and blocked from future ones — exactly the
manual ignore behaviour, applied in bulk from Brevo's own data.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SuppressionReason
from app.services import brevo, suppression

logger = logging.getLogger(__name__)


def _map_reason(code: str | None) -> SuppressionReason:
    """Map a Brevo blocked-contact reason code onto our SuppressionReason.

    Brevo codes (as of 2026): ``hardBounce``, ``unsubscribedViaApi`` /
    ``unsubscribedViaMA`` / ``unsubscribedViaEmail``, ``contactFlaggedAsSpam``,
    ``adminBlocked``, ``blocklisted`` …  Anything unrecognised is treated as a
    block (the recipient is on Brevo's no-send list regardless of the label).
    """
    c = (code or "").strip().lower()
    if "bounce" in c:
        return SuppressionReason.HARD_BOUNCE
    if "unsub" in c:
        return SuppressionReason.UNSUBSCRIBED
    if "spam" in c:
        return SuppressionReason.SPAM
    return SuppressionReason.BLOCKED


@dataclass
class BlocklistSyncResult:
    fetched: int = 0
    newly_suppressed: int = 0
    already_suppressed: int = 0
    leads_halted: int = 0
    leads_removed: int = 0  # not-yet-sent leads pulled from the queue


async def apply_blocked_contacts(
    db: AsyncSession, contacts: list[dict]
) -> BlocklistSyncResult:
    """Run each Brevo blocked contact through ``suppress_email``.  Commits in
    one transaction.  Pure of HTTP so it's unit-testable with a contact list."""
    result = BlocklistSyncResult(fetched=len(contacts))
    for contact in contacts:
        email = contact.get("email")
        if not email:
            continue
        reason = _map_reason((contact.get("reason") or {}).get("code"))
        res = await suppression.suppress_email(db, email, reason)
        if not res.suppressed:
            continue
        if res.already_suppressed:
            result.already_suppressed += 1
        else:
            result.newly_suppressed += 1
        result.leads_halted += res.leads_halted
        result.leads_removed += res.leads_marked_suppressed
    await db.commit()
    return result


async def sync_blocklist(
    db: AsyncSession, *, start_date: str | None = None
) -> BlocklistSyncResult:
    """Fetch Brevo's blocked contacts and suppress each.  ``start_date``
    (YYYY-MM-DD) limits to recently-blocked contacts for incremental runs."""
    contacts = await brevo.fetch_blocked_contacts(start_date=start_date)
    result = await apply_blocked_contacts(db, contacts)
    logger.info(
        "brevo blocklist sync: fetched=%d newly_suppressed=%d already=%d "
        "leads_halted=%d leads_removed=%d",
        result.fetched, result.newly_suppressed, result.already_suppressed,
        result.leads_halted, result.leads_removed,
    )
    return result
