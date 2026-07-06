"""Phase 2: SQLAlchemy models, relationships, cascades, indexes."""
from datetime import time

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models import (
    Campaign,
    CampaignStatus,
    ComposeStatus,
    ConnectedAccount,
    ConnectedAccountTestStatus,
    EmailEvent,
    EmailEventType,
    Lead,
    ResearchMode,
    ResearchStatus,
    SendStatus,
    StyleCorrection,
    Suppression,
    SuppressionReason,
)


def _make_campaign(connected_account_id=None) -> Campaign:
    return Campaign(
        name="Q2 outreach",
        goal="Book a 30-minute discovery call",
        tone="Professional",
        sender_name="Anthony",
        sender_email="anthony@example.com",
        research_mode=ResearchMode.FAST,
        connected_account_id=connected_account_id,
        schedule_days=[0, 1, 2, 3, 4],
        schedule_time_start=time(9, 0),
        schedule_time_end=time(17, 0),
        schedule_timezone="America/New_York",
        max_per_hour=50,
        max_per_day=400,
    )


def _make_account(email="me@gmail.com") -> ConnectedAccount:
    return ConnectedAccount(
        label="Work Gmail",
        email_address=email,
        imap_host="imap.gmail.com",
        username=email,
        password_encrypted="ciphertext",
    )


# ---------- Basic CRUD ----------


async def test_connected_account_create_defaults(db_session):
    acc = _make_account()
    db_session.add(acc)
    await db_session.commit()
    await db_session.refresh(acc)

    assert acc.id is not None
    assert acc.imap_port == 993
    assert acc.imap_use_ssl is True
    assert acc.last_test_status == ConnectedAccountTestStatus.UNTESTED
    assert acc.created_at is not None


async def test_campaign_create_with_array_and_enums(db_session):
    c = _make_campaign()
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)

    assert c.id is not None
    assert c.schedule_days == [0, 1, 2, 3, 4]
    assert c.status == CampaignStatus.DRAFT
    assert c.research_mode == ResearchMode.FAST
    assert c.sample_count == 5  # default
    assert c.min_delay_seconds == 60


async def test_lead_create_with_jsonb(db_session):
    c = _make_campaign()
    db_session.add(c)
    await db_session.flush()
    lead = Lead(
        campaign_id=c.id,
        email="ceo@acme.com",
        first_name="Jane",
        last_name="Doe",
        company="Acme",
        job_title="CEO",
        raw_csv_row={"Email": "ceo@acme.com", "Name": "Jane Doe"},
        research_data={"quality": "rich", "person_news": ["raised Series B"]},
    )
    db_session.add(lead)
    await db_session.commit()
    await db_session.refresh(lead)

    assert lead.research_status == ResearchStatus.PENDING
    assert lead.compose_status == ComposeStatus.PENDING
    assert lead.send_status == SendStatus.PENDING
    assert lead.is_sample is False
    assert lead.raw_csv_row["Email"] == "ceo@acme.com"
    assert lead.research_data["quality"] == "rich"


async def test_email_event_create(db_session):
    c = _make_campaign()
    db_session.add(c)
    await db_session.flush()
    lead = Lead(campaign_id=c.id, email="x@y.com")
    db_session.add(lead)
    await db_session.flush()

    ev = EmailEvent(
        lead_id=lead.id,
        campaign_id=c.id,
        event_type=EmailEventType.OPENED,
        event_data={"ua": "Gmail"},
    )
    db_session.add(ev)
    await db_session.commit()
    await db_session.refresh(ev)

    assert ev.occurred_at is not None
    assert ev.event_type == EmailEventType.OPENED


async def test_suppression_create(db_session):
    s = Suppression(email="bouncer@example.com", reason=SuppressionReason.HARD_BOUNCE)
    db_session.add(s)
    await db_session.commit()
    assert s.added_at is not None


async def test_style_correction_create(db_session):
    c = _make_campaign()
    db_session.add(c)
    await db_session.flush()
    sc = StyleCorrection(campaign_id=c.id, original_body="orig", corrected_body="fixed")
    db_session.add(sc)
    await db_session.commit()
    assert sc.id is not None


# ---------- Cascades ----------


async def test_cascade_delete_campaign_removes_leads_and_events(db_session):
    c = _make_campaign()
    db_session.add(c)
    await db_session.flush()
    lead = Lead(campaign_id=c.id, email="x@y.com")
    db_session.add(lead)
    await db_session.flush()
    ev = EmailEvent(lead_id=lead.id, campaign_id=c.id, event_type=EmailEventType.DELIVERED)
    sc = StyleCorrection(campaign_id=c.id, original_body="o", corrected_body="c")
    db_session.add_all([ev, sc])
    await db_session.commit()

    # Delete via raw SQL to exercise the DB-level ON DELETE CASCADE.
    await db_session.execute(text("DELETE FROM campaigns WHERE id = :i"), {"i": str(c.id)})
    await db_session.commit()

    assert (await db_session.scalar(select(Lead).where(Lead.id == lead.id))) is None
    assert (await db_session.scalar(select(EmailEvent).where(EmailEvent.id == ev.id))) is None
    assert (
        await db_session.scalar(select(StyleCorrection).where(StyleCorrection.id == sc.id))
    ) is None


async def test_cascade_delete_lead_removes_events(db_session):
    c = _make_campaign()
    db_session.add(c)
    await db_session.flush()
    lead = Lead(campaign_id=c.id, email="x@y.com")
    db_session.add(lead)
    await db_session.flush()
    ev = EmailEvent(lead_id=lead.id, campaign_id=c.id, event_type=EmailEventType.OPENED)
    db_session.add(ev)
    await db_session.commit()

    await db_session.execute(text("DELETE FROM leads WHERE id = :i"), {"i": str(lead.id)})
    await db_session.commit()

    assert (await db_session.scalar(select(EmailEvent).where(EmailEvent.id == ev.id))) is None


async def test_delete_connected_account_sets_campaign_fk_null(db_session):
    acc = _make_account()
    db_session.add(acc)
    await db_session.flush()
    c = _make_campaign(connected_account_id=acc.id)
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    assert c.connected_account_id == acc.id

    await db_session.execute(text("DELETE FROM connected_accounts WHERE id = :i"), {"i": str(acc.id)})
    await db_session.commit()

    # Verify with raw SQL so we read DB state rather than the cached ORM
    # instance — the DB-level ON DELETE SET NULL is what we're testing here.
    row = await db_session.execute(
        text("SELECT connected_account_id FROM campaigns WHERE id = :i"),
        {"i": str(c.id)},
    )
    fetched = row.first()
    assert fetched is not None, "campaign was not preserved"
    assert fetched[0] is None, f"expected NULL, got {fetched[0]!r}"


# ---------- Constraints ----------


async def test_suppression_email_unique(db_session):
    db_session.add(Suppression(email="dup@example.com", reason=SuppressionReason.MANUAL))
    await db_session.commit()

    db_session.add(Suppression(email="dup@example.com", reason=SuppressionReason.SPAM))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_lead_campaign_fk_is_optional_since_crm(db_session):
    """Migration 0025 made campaign_id NULLABLE — a CRM lead can be
    created manually outside any campaign.  Campaign-less leads default
    to crm_status='new' and never enter the compose/send pipeline."""
    crm_lead = Lead(email="orphan@example.com")  # no campaign_id — OK now
    db_session.add(crm_lead)
    await db_session.commit()
    await db_session.refresh(crm_lead)
    assert crm_lead.campaign_id is None
    assert crm_lead.crm_status == "new"


# ---------- Indexes ----------


async def test_required_indexes_exist(db_session):
    rows = await db_session.execute(
        text("SELECT tablename, indexname FROM pg_indexes WHERE schemaname='public'")
    )
    pairs = {(r.tablename, r.indexname) for r in rows}
    expected = {
        ("leads", "ix_leads_campaign_id"),
        ("leads", "ix_leads_send_status"),
        ("leads", "ix_leads_email"),
        ("email_events", "ix_email_events_campaign_id"),
        ("email_events", "ix_email_events_lead_id"),
        ("suppression_list", "ix_suppression_list_email"),
        ("connected_accounts", "ix_connected_accounts_email_address"),
    }
    missing = expected - pairs
    assert not missing, f"Missing indexes: {missing}"


# ---------- Relationships ----------


async def test_relationships_load(db_session):
    from sqlalchemy.orm import selectinload

    acc = _make_account()
    db_session.add(acc)
    await db_session.flush()
    c = _make_campaign(connected_account_id=acc.id)
    db_session.add(c)
    await db_session.flush()
    lead = Lead(campaign_id=c.id, email="x@y.com")
    db_session.add(lead)
    await db_session.commit()

    loaded = await db_session.scalar(
        select(Campaign)
        .where(Campaign.id == c.id)
        .options(selectinload(Campaign.leads), selectinload(Campaign.connected_account))
    )
    assert loaded is not None
    assert len(loaded.leads) == 1
    assert loaded.leads[0].email == "x@y.com"
    assert loaded.connected_account.label == "Work Gmail"
