"""Prospect-signal endpoints: watches CRUD, signal feed, action/dismiss.

Accepting/actioning a signal stays human — these endpoints toggle
status and tracking only; the worker applies the (bounded) autonomous
actions when a signal is first detected.
"""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import update

from app.config import settings
from app.database import get_db
from app.models import (
    Campaign,
    CampaignStatus,
    FundingSourceState,
    Lead,
    Opportunity,
    ProspectSignal,
    ProspectSignalStatus,
    SignalWatch,
    SignalWatchStatus,
    SignalWatchType,
    SocialSearchFrequency,
)

router = APIRouter(prefix="/signals", tags=["signals"])

# The two nonprofit-discovery feeds (config-driven, not per-target watches).
_FUNDING_SOURCES = ("usaspending", "irs_bmf")


def _blank_to_none(v: str | None) -> str | None:
    if v is None:
        return None
    return v.strip() or None


class WatchCreate(BaseModel):
    watch_type: SignalWatchType
    lead_id: uuid.UUID | None = None
    opportunity_id: uuid.UUID | None = None
    person_name: str | None = Field(default=None, max_length=200)
    company: str | None = Field(default=None, max_length=300)
    email: str | None = Field(default=None, max_length=320)
    linkedin_url: str | None = Field(default=None, max_length=500)
    company_website: str | None = Field(default=None, max_length=500)
    frequency: SocialSearchFrequency = SocialSearchFrequency.DAILY

    _v = field_validator(
        "person_name", "company", "email", "linkedin_url", "company_website",
    )(_blank_to_none)

    @model_validator(mode="after")
    def _required_fields_per_type(self) -> "WatchCreate":
        """Each watch type can only detect with the fields its source
        needs — fail loudly at creation instead of running silently
        useless checks forever.

        - job_change: Apollo's people/match keys on EMAIL.
        - funding / hiring: company-level checks need a COMPANY.
        - custom (runs every detector): at least one of email/company.
        Watches linked to a lead/opportunity inherit those fields from
        the record, so the free-text rules only apply to cold targets.
        """
        if self.lead_id is not None or self.opportunity_id is not None:
            return self
        if self.watch_type == SignalWatchType.JOB_CHANGE and not self.email:
            raise ValueError(
                "job-change watches need an email (title lookups are "
                "matched by email) — add one, or track an existing lead"
            )
        if self.watch_type in (SignalWatchType.FUNDING, SignalWatchType.HIRING) \
                and not self.company:
            raise ValueError(
                f"{self.watch_type.value} watches need a company name"
            )
        if self.watch_type == SignalWatchType.CUSTOM \
                and not (self.email or self.company):
            raise ValueError(
                "custom watches need at least an email or a company"
            )
        return self


class WatchBulkCreate(BaseModel):
    """One watch per company — for pasting a list of companies to
    monitor for funding/hiring.  (Job-change needs a per-person email,
    so it isn't bulk-able by company.)"""
    watch_type: SignalWatchType
    companies: list[str] = Field(min_length=1, max_length=100)
    frequency: SocialSearchFrequency = SocialSearchFrequency.DAILY

    @model_validator(mode="after")
    def _company_types_only(self) -> "WatchBulkCreate":
        if self.watch_type not in (SignalWatchType.FUNDING, SignalWatchType.HIRING):
            raise ValueError(
                "bulk company watches support funding and hiring only "
                "(job-change is matched per-person by email)"
            )
        return self


class WatchUpdate(BaseModel):
    frequency: SocialSearchFrequency | None = None
    status: SignalWatchStatus | None = None
    company: str | None = None
    email: str | None = None

    _v = field_validator("company", "email")(_blank_to_none)


def _watch_dict(w: SignalWatch) -> dict[str, Any]:
    return {
        "id": w.id,
        "watch_type": w.watch_type,
        "lead_id": w.lead_id,
        "opportunity_id": w.opportunity_id,
        "person_name": w.person_name,
        "company": w.company,
        "email": w.email,
        "linkedin_url": w.linkedin_url,
        "company_website": w.company_website,
        "frequency": w.frequency,
        "status": w.status,
        "next_run_at": w.next_run_at,
        "last_run_at": w.last_run_at,
        "last_run_status": w.last_run_status,
        "last_seen": w.last_seen,
        "created_at": w.created_at,
    }


@router.post("/watches", status_code=201)
async def create_watch(
    payload: WatchCreate, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    if payload.lead_id is not None:
        if await db.get(Lead, payload.lead_id) is None:
            raise HTTPException(status_code=404, detail="lead not found")
    if payload.opportunity_id is not None:
        opp = await db.get(Opportunity, payload.opportunity_id)
        if opp is None:
            raise HTTPException(status_code=404, detail="opportunity not found")
        # Job-change detection matches by email — an opp without one
        # would run a silently useless check forever.
        if payload.watch_type == SignalWatchType.JOB_CHANGE and not opp.email:
            raise HTTPException(
                status_code=422,
                detail="this opportunity has no contact email — job-change "
                       "lookups are matched by email",
            )
    if (
        payload.lead_id is None
        and payload.opportunity_id is None
        and not (payload.company or payload.person_name)
    ):
        raise HTTPException(
            status_code=422,
            detail="a watch needs a lead, an opportunity, or a company/person target",
        )
    # Duplicate guard: one active watch per (type, target).
    dup_q = select(SignalWatch.id).where(
        SignalWatch.watch_type == payload.watch_type,
        SignalWatch.status == SignalWatchStatus.ACTIVE,
    )
    if payload.lead_id is not None:
        dup_q = dup_q.where(SignalWatch.lead_id == payload.lead_id)
    elif payload.opportunity_id is not None:
        dup_q = dup_q.where(SignalWatch.opportunity_id == payload.opportunity_id)
    elif payload.email:
        dup_q = dup_q.where(func.lower(SignalWatch.email) == payload.email.lower())
    else:
        dup_q = dup_q.where(func.lower(SignalWatch.company) == (payload.company or "").lower())
    if (await db.scalar(dup_q.limit(1))) is not None:
        raise HTTPException(
            status_code=409,
            detail="already watching this target for that signal type",
        )

    w = SignalWatch(
        watch_type=payload.watch_type,
        lead_id=payload.lead_id,
        opportunity_id=payload.opportunity_id,
        person_name=payload.person_name,
        company=payload.company,
        email=(payload.email or "").lower() or None,
        linkedin_url=payload.linkedin_url,
        company_website=payload.company_website,
        frequency=payload.frequency,
        # First run as soon as the beat ticks (manual stays manual).
        next_run_at=(
            datetime.now(timezone.utc)
            if payload.frequency != SocialSearchFrequency.MANUAL
            else None
        ),
    )
    db.add(w)
    await db.commit()
    await db.refresh(w)
    return _watch_dict(w)


@router.post("/watches/bulk", status_code=201)
async def create_watches_bulk(
    payload: WatchBulkCreate, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """One watch per pasted company (funding/hiring).  Companies that
    already have an active watch of this type are skipped, not errors —
    pasting an overlapping list twice is a no-op for the overlap."""
    # Normalise + de-dupe the pasted list, preserving order.
    companies: list[str] = []
    seen: set[str] = set()
    for raw in payload.companies:
        name = (raw or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        companies.append(name[:300])

    existing = {
        (c or "").lower()
        for (c,) in (await db.execute(
            select(SignalWatch.company).where(
                SignalWatch.watch_type == payload.watch_type,
                SignalWatch.status == SignalWatchStatus.ACTIVE,
                SignalWatch.company.is_not(None),
            )
        )).all()
    }

    created: list[SignalWatch] = []
    skipped_duplicate = 0
    next_run = (
        datetime.now(timezone.utc)
        if payload.frequency != SocialSearchFrequency.MANUAL
        else None
    )
    for name in companies:
        if name.lower() in existing:
            skipped_duplicate += 1
            continue
        existing.add(name.lower())
        w = SignalWatch(
            watch_type=payload.watch_type,
            company=name,
            frequency=payload.frequency,
            next_run_at=next_run,
        )
        db.add(w)
        created.append(w)
    await db.commit()
    return {
        "created": len(created),
        "skipped_duplicate": skipped_duplicate,
        "watch_ids": [str(w.id) for w in created],
    }


@router.get("/watches")
async def list_watches(db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    rows = (await db.execute(
        select(SignalWatch).order_by(SignalWatch.created_at.desc())
    )).scalars().all()
    return [_watch_dict(w) for w in rows]


@router.patch("/watches/{watch_id}")
async def update_watch(
    watch_id: uuid.UUID, payload: WatchUpdate, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    w = await db.get(SignalWatch, watch_id)
    if w is None:
        raise HTTPException(status_code=404, detail="watch not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(w, key, value)
    await db.commit()
    await db.refresh(w)
    return _watch_dict(w)


@router.delete("/watches/{watch_id}", status_code=204, response_model=None)
async def delete_watch(
    watch_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> None:
    w = await db.get(SignalWatch, watch_id)
    if w is None:
        raise HTTPException(status_code=404, detail="watch not found")
    await db.delete(w)
    await db.commit()


@router.post("/watches/{watch_id}/run-now")
async def run_watch_now(
    watch_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    w = await db.get(SignalWatch, watch_id)
    if w is None:
        raise HTTPException(status_code=404, detail="watch not found")
    if w.status != SignalWatchStatus.ACTIVE:
        raise HTTPException(status_code=409, detail="watch is paused")
    from app.workers.signals import run_watch

    run_watch.delay(str(w.id))
    return {"enqueued": True}


# ---------------------------------------------------------------------------
# Signal feed
# ---------------------------------------------------------------------------


@router.get("")
async def list_signals(
    status: ProspectSignalStatus | None = Query(default=None),
    source: str | None = Query(
        default=None,
        description="Discovery feed filter: 'usaspending' | 'irs_bmf' | 'watch' "
                    "(watch = signals with no feed source).",
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    # NB: this queries ProspectSignal directly — NO inner join on
    # signal_watches — so discovery signals (watch_id IS NULL) appear in
    # the review queue alongside watch-sourced ones.
    filters = []
    if status is not None:
        filters.append(ProspectSignal.status == status)
    if source is not None:
        if source == "watch":
            filters.append(ProspectSignal.source.is_(None))
        else:
            filters.append(ProspectSignal.source == source)
    total = (await db.execute(
        select(func.count()).select_from(ProspectSignal).where(*filters)
    )).scalar_one()
    rows = (await db.execute(
        select(ProspectSignal)
        .where(*filters)
        .order_by(ProspectSignal.detected_at.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )).scalars().all()
    # Pull the linked leads' contact basics in one batched query so the
    # detail modal can show who/where we found without a per-row fetch.
    lead_ids = [s.lead_id for s in rows if s.lead_id is not None]
    lead_map: dict[uuid.UUID, dict[str, Any]] = {}
    if lead_ids:
        lead_rows = (await db.execute(
            select(
                Lead.id, Lead.email, Lead.first_name, Lead.last_name,
                Lead.job_title, Lead.company, Lead.company_website, Lead.linkedin_url,
            ).where(Lead.id.in_(lead_ids))
        )).all()
        for lid, email, fn, ln, title, company, website, linkedin in lead_rows:
            lead_map[lid] = {
                "email": email,
                "first_name": fn,
                "last_name": ln,
                "job_title": title,
                "company": company,
                "company_website": website,
                "linkedin_url": linkedin,
            }
    items = [
        {
            "id": s.id,
            "watch_id": s.watch_id,
            "source": s.source,
            "signal_type": s.signal_type,
            "summary": s.summary,
            "detail": s.detail,
            "status": s.status,
            "lead_id": s.lead_id,
            "lead": lead_map.get(s.lead_id),
            "lead_has_email": bool((lead_map.get(s.lead_id) or {}).get("email")),
            "opportunity_id": s.opportunity_id,
            "detected_at": s.detected_at,
        }
        for s in rows
    ]
    return {
        "items": items, "total": total, "page": page,
        "page_size": page_size,
        "total_pages": math.ceil(total / page_size) if total else 0,
    }


async def _set_signal_status(
    db: AsyncSession, signal_id: uuid.UUID, status: ProspectSignalStatus,
) -> dict[str, Any]:
    s = await db.get(ProspectSignal, signal_id)
    if s is None:
        raise HTTPException(status_code=404, detail="signal not found")
    s.status = status
    await db.commit()
    return {"id": str(s.id), "status": s.status.value}


@router.post("/{signal_id}/action")
async def action_signal(
    signal_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    return await _set_signal_status(db, signal_id, ProspectSignalStatus.ACTIONED)


@router.post("/{signal_id}/dismiss")
async def dismiss_signal(
    signal_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    return await _set_signal_status(db, signal_id, ProspectSignalStatus.DISMISSED)


# ---------------------------------------------------------------------------
# On-demand contact enrichment ("Find contact")
# ---------------------------------------------------------------------------


class SignalEnrichResponse(BaseModel):
    found: bool
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    generic: bool = False
    has_email: bool = False
    linkedin_url: str | None = None
    lead_id: uuid.UUID | None = None
    lead_created: bool = False
    already_had_contact: bool = False


@router.post("/{signal_id}/enrich", response_model=SignalEnrichResponse)
async def enrich_signal(
    signal_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> SignalEnrichResponse:
    """Light, low-cost contact lookup: Hunter-first, at most one Haiku
    web-lookup for the domain.  On a hit, stages + links a campaign-less
    Lead so the signal becomes contactable."""
    from app.services import signal_enrichment

    signal = await db.get(ProspectSignal, signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail="signal not found")

    result = await signal_enrichment.enrich_signal_contact(db, signal)
    return SignalEnrichResponse(
        found=result.found,
        email=result.email,
        first_name=result.first_name,
        last_name=result.last_name,
        title=result.title,
        generic=result.generic,
        has_email=result.has_email,
        linkedin_url=result.linkedin_url,
        lead_id=result.lead_id,
        lead_created=result.lead_created,
        already_had_contact=result.already_had_contact,
    )


# ---------------------------------------------------------------------------
# Bulk: add signals' staged leads to a campaign (→ runs the send pipeline)
# ---------------------------------------------------------------------------


class SignalsAddToCampaignRequest(BaseModel):
    signal_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    campaign_id: uuid.UUID


class SignalsAddToCampaignResponse(BaseModel):
    added: int
    skipped_duplicate: int
    skipped_suppressed: int
    skipped_no_contact: int       # selected signals with no staged lead
    research_started: bool
    signals_actioned: int


@router.post("/add-to-campaign", response_model=SignalsAddToCampaignResponse)
async def add_signals_to_campaign(
    payload: SignalsAddToCampaignRequest, db: AsyncSession = Depends(get_db)
) -> SignalsAddToCampaignResponse:
    """Copy the staged leads behind the selected signals into a campaign
    so they run the normal research → compose → send pipeline, and mark
    those signals actioned.

    This is the human-initiated step the discovery autonomy boundary
    leaves open — the system never auto-enrolls, but the user can.
    Signals with no contact (no staged lead) are skipped and left New.
    """
    from app.services import campaign_membership

    campaign = await db.get(Campaign, payload.campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    if campaign.status == CampaignStatus.COMPLETE:
        raise HTTPException(
            status_code=409,
            detail="Campaign is complete — it will never send. Pick a draft or active campaign.",
        )

    signals = list((await db.execute(
        select(ProspectSignal).where(ProspectSignal.id.in_(payload.signal_ids))
    )).scalars().all())
    contactable = [s for s in signals if s.lead_id is not None]
    skipped_no_contact = len(signals) - len(contactable)
    lead_ids = [s.lead_id for s in contactable]

    result = await campaign_membership.add_leads_to_campaign(db, campaign, lead_ids)

    # Mark every contactable selected signal actioned (added or already-in,
    # either way the user has handled it).
    if contactable:
        await db.execute(
            update(ProspectSignal)
            .where(ProspectSignal.id.in_([s.id for s in contactable]))
            .values(status=ProspectSignalStatus.ACTIONED)
        )
        await db.commit()

    return SignalsAddToCampaignResponse(
        added=result.added,
        skipped_duplicate=result.skipped_duplicate,
        skipped_suppressed=result.skipped_suppressed,
        skipped_no_contact=skipped_no_contact,
        research_started=result.research_started,
        signals_actioned=len(contactable),
    )


# ---------------------------------------------------------------------------
# Draft + send outreach for a signal (→ logs a CRM lead + activity)
# ---------------------------------------------------------------------------


class SignalDraftRequest(BaseModel):
    goal: str | None = Field(default=None, max_length=2000)
    tone: str | None = Field(default=None, max_length=120)


class SignalDraftResponse(BaseModel):
    to_email: str
    to_name: str | None
    subject: str
    body: str


class SignalSendRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=998)
    body: str = Field(min_length=1, max_length=50_000)
    to_email: str | None = None          # defaults to the linked lead's email
    sender_email: str | None = None      # defaults to the workspace default sender
    sender_name: str | None = Field(default=None, max_length=120)
    # Per-send signature override: None = use the sender account's signature,
    # "" = send with no signature, text = use this signature verbatim.
    signature: str | None = Field(default=None, max_length=10_000)


class SignalSendResponse(BaseModel):
    message_id: str
    crm_lead_id: str | None = None
    crm_lead_created: bool = False
    crm_activity_logged: bool = False
    signal_status: str


async def _signal_with_lead(db: AsyncSession, signal_id: uuid.UUID):
    """Fetch the signal + its linked lead, 404/409ing when there's no
    contactable lead to draft/send to."""
    signal = await db.get(ProspectSignal, signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail="signal not found")
    if signal.lead_id is None:
        raise HTTPException(
            status_code=409,
            detail="no contact on this signal — nothing to email (a contact "
                   "email wasn't found for this org)",
        )
    lead = await db.get(Lead, signal.lead_id)
    if lead is None or not lead.email:
        raise HTTPException(
            status_code=409,
            detail="the linked lead has no email — can't draft or send",
        )
    return signal, lead


@router.post("/{signal_id}/draft", response_model=SignalDraftResponse)
async def draft_signal_email(
    signal_id: uuid.UUID,
    payload: SignalDraftRequest,
    db: AsyncSession = Depends(get_db),
) -> SignalDraftResponse:
    """Compose an outreach email for a signal, prefilled from the org +
    contact.  Does not send — returns an editable draft."""
    from app.services import signal_outreach

    signal, lead = await _signal_with_lead(db, signal_id)
    detail = signal.detail or {}
    org_name = lead.company or detail.get("company") or detail.get("org_name") or "your organization"
    try:
        composed = await signal_outreach.compose_signal_email(
            signal_type=signal.signal_type,
            summary=signal.summary,
            detail=detail,
            org_name=org_name,
            contact_first_name=lead.first_name,
            goal=payload.goal,
            tone=payload.tone,
            sender_name=settings.BREVO_SENDER_NAME,
        )
    except signal_outreach.SignalComposeError as exc:
        raise HTTPException(status_code=502, detail=f"couldn't draft: {exc}") from exc

    name = " ".join(x for x in [lead.first_name, lead.last_name] if x) or None
    return SignalDraftResponse(
        to_email=lead.email, to_name=name,
        subject=composed["subject"], body=composed["body"],
    )


@router.post("/{signal_id}/send", response_model=SignalSendResponse)
async def send_signal_email(
    signal_id: uuid.UUID,
    payload: SignalSendRequest,
    db: AsyncSession = Depends(get_db),
) -> SignalSendResponse:
    """Send the (possibly edited) outreach via the chosen inbox, log it
    against the signal's staged lead as an outbound email activity, and
    mark the signal actioned."""
    from app.services import outreach

    signal, lead = await _signal_with_lead(db, signal_id)
    to_email = (payload.to_email or lead.email).strip().lower()
    to_name = " ".join(x for x in [lead.first_name, lead.last_name] if x) or None
    sender_name = payload.sender_name or settings.BREVO_SENDER_NAME

    try:
        result = await outreach.send_and_track(
            db,
            to_email=to_email,
            to_name=to_name,
            subject=payload.subject,
            body=payload.body,
            sender_name=sender_name,
            sender_email=payload.sender_email,
            signature=payload.signature,      # None = account default
            lead=lead,                        # log against the staged lead
            campaign_tag="signal-outreach",
        )
    except outreach.OutreachSendError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    # Flip the signal to actioned (separate commit — send_and_track already
    # committed/rolled back the CRM work; re-fetch since it may be expired).
    signal = await db.get(ProspectSignal, signal_id)
    if signal is not None and signal.status != ProspectSignalStatus.ACTIONED:
        signal.status = ProspectSignalStatus.ACTIONED
        await db.commit()

    return SignalSendResponse(
        message_id=result.message_id,
        crm_lead_id=result.crm_lead_id,
        crm_lead_created=result.crm_lead_created,
        crm_activity_logged=result.crm_activity_logged,
        signal_status=ProspectSignalStatus.ACTIONED.value,
    )


# ---------------------------------------------------------------------------
# Nonprofit funding discovery feeds (Settings → Discovery)
# ---------------------------------------------------------------------------

_FUNDING_LABELS = {"usaspending": "USASpending", "irs_bmf": "IRS BMF"}


async def _seed_funding_state(db: AsyncSession, source: str) -> FundingSourceState:
    """Get-or-create the feed's state row, seeding enabled/config from the
    env defaults when unset — same seeding the worker does, so opening the
    panel and the next poll agree."""
    # Lazy import avoids any router↔worker import cycle at module load.
    from app.workers.funding_signals import _env_config, _env_enabled

    state = await db.get(FundingSourceState, source)
    if state is None:
        state = FundingSourceState(source=source, cursor={})
        db.add(state)
    if state.enabled is None:
        state.enabled = _env_enabled(source)
    if state.config is None:
        state.config = _env_config(source)
    await db.flush()
    return state


def _funding_source_dict(state: FundingSourceState, signal_count: int) -> dict[str, Any]:
    return {
        "source": state.source,
        "label": _FUNDING_LABELS.get(state.source, state.source),
        "enabled": bool(state.enabled),
        "config": state.config or {},
        "last_run_at": state.last_run_at,
        "last_run_status": state.last_run_status,
        "cursor": state.cursor or {},
        "signal_count": signal_count,
    }


@router.get("/funding/sources")
async def list_funding_sources(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """The two nonprofit-discovery feeds with their live config + last-run
    state + how many signals each has surfaced.  ``hunter_configured``
    tells the UI whether discovered orgs can be staged as leads (vs.
    notification-only)."""
    out = []
    for src in _FUNDING_SOURCES:
        state = await _seed_funding_state(db, src)
        count = (await db.execute(
            select(func.count()).select_from(ProspectSignal)
            .where(ProspectSignal.source == src)
        )).scalar_one()
        out.append(_funding_source_dict(state, count))
    await db.commit()  # persist any first-time seeding
    return {
        "sources": out,
        "hunter_configured": bool(settings.HUNTER_API_KEY),
    }


class FundingSourceUpdate(BaseModel):
    enabled: bool | None = None
    lookback_days: int | None = Field(default=None, ge=1, le=365)
    # Per-feed lead-pull cap per run (applies to both feeds).
    max_per_run: int | None = Field(default=None, ge=1, le=1000)
    # Award-size bounds (USAspending).  Sent explicitly as null = "no
    # bound"; absent = leave unchanged (handled via model_fields_set).
    min_award_amount: float | None = Field(default=None, ge=0)
    max_award_amount: float | None = Field(default=None, ge=0)
    ruling_lookback_months: int | None = Field(default=None, ge=1, le=24)
    states: list[str] | None = None


@router.patch("/funding/sources/{source}")
async def update_funding_source(
    source: str, payload: FundingSourceUpdate, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    if source not in _FUNDING_SOURCES:
        raise HTTPException(status_code=404, detail="unknown funding source")
    state = await _seed_funding_state(db, source)
    if payload.enabled is not None:
        state.enabled = payload.enabled

    cfg = dict(state.config or {})
    # Per-feed lead-pull cap — applies to both feeds.
    if payload.max_per_run is not None:
        cfg["max_per_run"] = payload.max_per_run
    if source == "usaspending":
        if payload.lookback_days is not None:
            cfg["lookback_days"] = payload.lookback_days
        # Amount bounds: apply when explicitly sent (incl. null = no bound).
        if "min_award_amount" in payload.model_fields_set:
            cfg["min_award_amount"] = payload.min_award_amount
        if "max_award_amount" in payload.model_fields_set:
            cfg["max_award_amount"] = payload.max_award_amount
    else:  # irs_bmf
        if payload.ruling_lookback_months is not None:
            cfg["ruling_lookback_months"] = payload.ruling_lookback_months
        if payload.states is not None:
            # Normalise: trim, uppercase, dedupe, drop blanks.
            seen: set[str] = set()
            clean: list[str] = []
            for s in payload.states:
                st = (s or "").strip().upper()
                if st and st not in seen:
                    seen.add(st)
                    clean.append(st)
            cfg["states"] = clean
    state.config = cfg  # reassign so SQLAlchemy flags the JSONB change

    count = (await db.execute(
        select(func.count()).select_from(ProspectSignal)
        .where(ProspectSignal.source == source)
    )).scalar_one()
    await db.commit()
    await db.refresh(state)
    return _funding_source_dict(state, count)


@router.post("/funding/sources/{source}/run-now")
async def run_funding_source_now(
    source: str, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    if source not in _FUNDING_SOURCES:
        raise HTTPException(status_code=404, detail="unknown funding source")
    state = await _seed_funding_state(db, source)
    await db.commit()
    if not state.enabled:
        raise HTTPException(status_code=409, detail="feed is disabled — enable it first")
    if source == "irs_bmf" and not (state.config or {}).get("states"):
        raise HTTPException(
            status_code=409,
            detail="add at least one state before running the IRS feed",
        )

    from app.workers.funding_signals import poll_irs_bmf, poll_usaspending

    task = poll_usaspending if source == "usaspending" else poll_irs_bmf
    task.delay()
    return {"enqueued": True}


@router.post("/funding/sources/{source}/stop")
async def stop_funding_source(
    source: str, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Hard-stop an in-flight run: terminate the running Celery task(s) for
    this feed and purge any queued / redelivered copies from the broker so
    a long run can't loop past the visibility timeout and keep burning
    Anthropic enrichment tokens.  No-op (zero counts) when nothing runs."""
    if source not in _FUNDING_SOURCES:
        raise HTTPException(status_code=404, detail="unknown funding source")

    from app.workers.funding_signals import stop_funding_run

    result = await run_in_threadpool(stop_funding_run, source)
    stopped_anything = bool(
        result["terminated"] or result["purged_queued"] or result["purged_unacked"]
    )

    state = await _seed_funding_state(db, source)
    if stopped_anything:
        state.last_run_status = "stopped"
        state.last_run_at = datetime.now(timezone.utc)
    count = (await db.execute(
        select(func.count()).select_from(ProspectSignal)
        .where(ProspectSignal.source == source)
    )).scalar_one()
    await db.commit()
    await db.refresh(state)
    return {**_funding_source_dict(state, count), "stopped": result}
