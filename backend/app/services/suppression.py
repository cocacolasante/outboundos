"""Workspace suppression (the "ignore list") — one place to add an email to
the list AND pull it out of every campaign.

Used by the manual "ignore" button, the real-time Brevo event poller (hard
bounce / spam / unsubscribe / blocked), and the Brevo blocklist sync, so the
three effects always happen together:

  1. Add the email to the suppression list  → blocks ALL future campaigns
     (the send gates + bulk pipeline reject suppressed emails, even on a fresh
     CSV upload of the same address).
  2. Halt every ACTIVE sequence state for any lead with that email  → stops
     in-flight follow-ups / replies in current campaigns.
  3. Mark every not-yet-sent lead (PENDING/SCHEDULED) with that email
     SUPPRESSED  → removes them from the current send queue and shows them as
     suppressed in the UI.

Idempotent and email-keyed (an email can span several leads/campaigns).  The
caller owns the transaction — this adds/updates rows but does NOT commit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Lead,
    LeadSequenceState,
    LeadSequenceStatus,
    SendStatus,
    Suppression,
    SuppressionReason,
    canonical_email,
)

# Human-readable halt reasons per suppression reason (shown on the halted
# sequence state + the Leads timeline).  The manual one keeps the word
# "ignored" the existing ignore-button contract depends on.
_HALT_REASONS: dict[SuppressionReason, str] = {
    SuppressionReason.MANUAL: "ignored by user (manual suppression)",
    SuppressionReason.HARD_BOUNCE: "suppressed — email hard-bounced (Brevo)",
    SuppressionReason.SPAM: "suppressed — marked as spam (Brevo)",
    SuppressionReason.UNSUBSCRIBED: "suppressed — recipient unsubscribed (Brevo)",
    SuppressionReason.BLOCKED: "suppressed — recipient blocklisted (Brevo)",
    SuppressionReason.SOFT_BOUNCE: "suppressed — soft bounce (protecting sender reputation)",
}


@dataclass
class SuppressionResult:
    email: str
    suppressed: bool = False           # False only when the email was blank/invalid
    already_suppressed: bool = False
    leads_halted: int = 0              # ACTIVE sequence states flipped to HALTED
    leads_marked_suppressed: int = 0   # PENDING/SCHEDULED leads → SUPPRESSED
    campaigns_affected: list[UUID] = field(default_factory=list)


async def suppress_email(
    db: AsyncSession,
    email: str | None,
    reason: SuppressionReason,
    *,
    halt_reason: str | None = None,
    tenant_id=None,
) -> SuppressionResult:
    """Add ``email`` to the suppression list and pull it out of every campaign.

    See module docstring for the three combined effects.  Caller commits.

    ``tenant_id``: explicit tenant stamp for callers running OUTSIDE tenant
    context (the brevo events poller / service paths, which resolve the
    tenant from the matched lead).  In tenant context (request path,
    ``run_for_tenant`` workers) leave it None — RLS scopes the reads and
    the TenantMixin default stamps the insert.
    """
    canonical = canonical_email(email)
    if not canonical:
        return SuppressionResult(email="", suppressed=False)

    result = SuppressionResult(email=canonical, suppressed=True)
    reason_text = halt_reason or _HALT_REASONS.get(reason, f"suppressed ({reason.value})")

    # 1) Upsert the suppression row (unique per tenant + email).
    existing_q = select(Suppression).where(Suppression.email == canonical)
    if tenant_id is not None:
        existing_q = existing_q.where(Suppression.tenant_id == tenant_id)
    existing = await db.scalar(existing_q)
    result.already_suppressed = existing is not None
    if existing is None:
        row = Suppression(email=canonical, reason=reason)
        if tenant_id is not None:
            row.tenant_id = tenant_id
        db.add(row)

    # 2) Halt every ACTIVE sequence state for any lead with this email
    #    (func.lower so mixed-case legacy rows are caught too).
    state_rows = (await db.execute(
        select(LeadSequenceState, Lead.campaign_id)
        .join(Lead, Lead.id == LeadSequenceState.lead_id)
        .where(func.lower(Lead.email) == canonical)
        .where(LeadSequenceState.status == LeadSequenceStatus.ACTIVE)
    )).all()
    campaigns: set[UUID] = set()
    for state, campaign_id in state_rows:
        state.status = LeadSequenceStatus.HALTED
        state.halt_reason = reason_text
        state.next_run_at = None
        result.leads_halted += 1
        if campaign_id is not None:
            campaigns.add(campaign_id)

    # 3) Pull not-yet-sent leads out of the send queue (SENT/FAILED untouched).
    marked = await db.execute(
        update(Lead)
        .where(
            func.lower(Lead.email) == canonical,
            Lead.send_status.in_((SendStatus.PENDING, SendStatus.SCHEDULED)),
        )
        .values(send_status=SendStatus.SUPPRESSED)
    )
    result.leads_marked_suppressed = marked.rowcount or 0

    result.campaigns_affected = sorted(campaigns)
    return result
