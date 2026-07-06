"""Phase 5: upload + confirm-upload endpoints."""
import json
import uuid
from datetime import time
from unittest.mock import patch

from sqlalchemy import select

from app.models import (
    Campaign,
    CampaignStatus,
    Lead,
    Suppression,
    SuppressionReason,
)


def _campaign_payload(**overrides) -> dict:
    base = {
        "name": "Q2 outreach",
        "goal": "Book a demo",
        "tone": "Professional",
        "sender_name": "Anthony",
        "sender_email": "anthony@example.com",
        "research_mode": "fast",
        "sample_count": 3,
        "schedule_days": [0, 1, 2, 3, 4],
        "schedule_time_start": "09:00:00",
        "schedule_time_end": "17:00:00",
        "schedule_timezone": "UTC",
        "max_per_hour": 50,
        "max_per_day": 400,
        "min_delay_seconds": 60,
    }
    base.update(overrides)
    return base


async def _new_campaign(client, **overrides) -> dict:
    return (await client.post("/campaigns/", json=_campaign_payload(**overrides))).json()


CSV_BASIC = (
    b"Email,First Name,Last Name,Company,Job Title\n"
    b"a@x.com,Alice,Apple,Acme,CEO\n"
    b"b@x.com,Bob,Banana,Beeco,CTO\n"
    b"c@x.com,Cara,Cherry,Citco,COO\n"
    b"d@x.com,Dan,Date,Dotco,Engineer\n"
    b"e@x.com,Eve,Eel,Eelco,VP\n"
)


# ---------- upload-preview ----------


async def test_upload_preview_returns_columns_and_suggested_mapping(client):
    campaign = await _new_campaign(client)
    resp = await client.post(
        f"/campaigns/{campaign['id']}/upload",
        files={"file": ("leads.csv", CSV_BASIC, "text/csv")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["columns"] == ["Email", "First Name", "Last Name", "Company", "Job Title"]
    assert body["total_rows"] == 5
    assert len(body["preview_rows"]) == 5  # all 5 fit under preview cap
    assert body["suggested_mapping"] == {
        "Email": "email",
        "First Name": "first_name",
        "Last Name": "last_name",
        "Company": "company",
        "Job Title": "job_title",
    }


async def test_upload_preview_caps_preview_rows_at_five(client):
    campaign = await _new_campaign(client)
    rows = b"Email\n" + b"\n".join(f"u{i}@x.com".encode() for i in range(20)) + b"\n"
    resp = await client.post(
        f"/campaigns/{campaign['id']}/upload",
        files={"file": ("leads.csv", rows, "text/csv")},
    )
    body = resp.json()
    assert body["total_rows"] == 20
    assert len(body["preview_rows"]) == 5


async def test_upload_preview_does_not_persist_leads(client, db_session):
    campaign = await _new_campaign(client)
    await client.post(
        f"/campaigns/{campaign['id']}/upload",
        files={"file": ("leads.csv", CSV_BASIC, "text/csv")},
    )
    count = await db_session.scalar(
        select(Lead).where(Lead.campaign_id == uuid.UUID(campaign["id"]))
    )
    assert count is None


async def test_upload_preview_404_on_unknown_campaign(client):
    resp = await client.post(
        f"/campaigns/{uuid.uuid4()}/upload",
        files={"file": ("x.csv", b"Email\nx@y.com\n", "text/csv")},
    )
    assert resp.status_code == 404


# ---------- confirm-upload ----------


async def test_confirm_upload_inserts_leads_and_enqueues(client, db_session):
    campaign = await _new_campaign(client)
    mapping = {
        "Email": "email",
        "First Name": "first_name",
        "Last Name": "last_name",
        "Company": "company",
        "Job Title": "job_title",
    }

    with patch("app.routers.leads.ingest_tasks.run_campaign_research.delay") as enqueue:
        resp = await client.post(
            f"/campaigns/{campaign['id']}/leads/confirm-upload",
            files={"file": ("leads.csv", CSV_BASIC, "text/csv")},
            data={"mapping": json.dumps(mapping)},
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body == {
        "total": 5, "suppressed": 0, "duplicates_removed": 0,
        "samples_selected": 3, "auto_launched": False,
    }

    enqueue.assert_called_once_with(campaign["id"])

    # Leads persisted
    leads = (await db_session.execute(
        select(Lead).where(Lead.campaign_id == uuid.UUID(campaign["id"]))
    )).scalars().all()
    assert len(leads) == 5
    emails = {l.email for l in leads}
    assert emails == {"a@x.com", "b@x.com", "c@x.com", "d@x.com", "e@x.com"}

    # Sample marking — 3 samples spaced across 5 rows
    sample_emails = {l.email for l in leads if l.is_sample}
    assert len(sample_emails) == 3

    # raw_csv_row preserved
    alice = next(l for l in leads if l.email == "a@x.com")
    assert alice.raw_csv_row["First Name"] == "Alice"
    assert alice.first_name == "Alice"
    assert alice.company == "Acme"


async def test_confirm_upload_lowercases_emails(client, db_session):
    campaign = await _new_campaign(client)
    csv = b"Email\nMixed.Case@Example.com\n"
    with patch("app.routers.leads.ingest_tasks.run_campaign_research.delay"):
        resp = await client.post(
            f"/campaigns/{campaign['id']}/leads/confirm-upload",
            files={"file": ("x.csv", csv, "text/csv")},
            data={"mapping": json.dumps({"Email": "email"})},
        )
    assert resp.status_code == 201
    lead = await db_session.scalar(select(Lead).where(Lead.campaign_id == uuid.UUID(campaign["id"])))
    assert lead.email == "mixed.case@example.com"


async def test_confirm_upload_dedupes_within_csv(client):
    campaign = await _new_campaign(client)
    csv = b"Email\nx@y.com\nX@Y.com\nx@y.com\nz@q.com\n"
    with patch("app.routers.leads.ingest_tasks.run_campaign_research.delay"):
        resp = await client.post(
            f"/campaigns/{campaign['id']}/leads/confirm-upload",
            files={"file": ("x.csv", csv, "text/csv")},
            data={"mapping": json.dumps({"Email": "email"})},
        )
    body = resp.json()
    assert body["total"] == 2
    assert body["duplicates_removed"] == 2


async def test_confirm_upload_skips_suppressed_emails(client, db_session):
    db_session.add(Suppression(email="suppressed@x.com", reason=SuppressionReason.UNSUBSCRIBED))
    await db_session.commit()

    campaign = await _new_campaign(client)
    csv = b"Email\nsuppressed@x.com\nok@x.com\n"
    with patch("app.routers.leads.ingest_tasks.run_campaign_research.delay"):
        resp = await client.post(
            f"/campaigns/{campaign['id']}/leads/confirm-upload",
            files={"file": ("x.csv", csv, "text/csv")},
            data={"mapping": json.dumps({"Email": "email"})},
        )
    body = resp.json()
    assert body == {
        "total": 1, "suppressed": 1, "duplicates_removed": 0,
        "samples_selected": 1, "auto_launched": False,
    }

    leads = (await db_session.execute(
        select(Lead).where(Lead.campaign_id == uuid.UUID(campaign["id"]))
    )).scalars().all()
    assert {l.email for l in leads} == {"ok@x.com"}


async def test_confirm_upload_transitions_campaign_to_previewing(client, db_session):
    campaign = await _new_campaign(client)
    assert campaign["status"] == "draft"

    with patch("app.routers.leads.ingest_tasks.run_campaign_research.delay"):
        await client.post(
            f"/campaigns/{campaign['id']}/leads/confirm-upload",
            files={"file": ("x.csv", CSV_BASIC, "text/csv")},
            data={"mapping": json.dumps({"Email": "email"})},
        )

    refreshed = await db_session.get(Campaign, uuid.UUID(campaign["id"]))
    await db_session.refresh(refreshed)
    assert refreshed.status == CampaignStatus.PREVIEWING


async def test_confirm_upload_auto_launches_for_non_email_entry(client, db_session):
    """A campaign whose sequence starts with a non-email node has no first
    email to preview, so confirm-upload launches it straight into RUNNING."""
    campaign = await _new_campaign(client)
    # Replace the default email-entry sequence with a LinkedIn-connect entry.
    seq_payload = {
        "nodes": [
            {"client_id": "entry", "kind": "linkedin_connect", "is_entry": True,
             "config": {"no_note": True}},
        ],
        "edges": [],
    }
    r = await client.put(f"/campaigns/{campaign['id']}/sequence", json=seq_payload)
    assert r.status_code == 200, r.text

    with patch("app.routers.leads.ingest_tasks.run_campaign_research.delay") as enqueue:
        resp = await client.post(
            f"/campaigns/{campaign['id']}/leads/confirm-upload",
            files={"file": ("x.csv", CSV_BASIC, "text/csv")},
            data={"mapping": json.dumps({"Email": "email"})},
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["auto_launched"] is True
    # Research still runs (for DM personalization).
    enqueue.assert_called_once_with(campaign["id"])

    refreshed = await db_session.get(Campaign, uuid.UUID(campaign["id"]))
    await db_session.refresh(refreshed)
    assert refreshed.status == CampaignStatus.RUNNING


async def test_confirm_upload_rejects_missing_email_mapping(client):
    campaign = await _new_campaign(client)
    resp = await client.post(
        f"/campaigns/{campaign['id']}/leads/confirm-upload",
        files={"file": ("x.csv", CSV_BASIC, "text/csv")},
        data={"mapping": json.dumps({"First Name": "first_name"})},
    )
    assert resp.status_code == 422
    assert "email" in resp.text.lower()


async def test_confirm_upload_rejects_invalid_mapping_json(client):
    campaign = await _new_campaign(client)
    resp = await client.post(
        f"/campaigns/{campaign['id']}/leads/confirm-upload",
        files={"file": ("x.csv", CSV_BASIC, "text/csv")},
        data={"mapping": "not-json"},
    )
    assert resp.status_code == 422


async def test_confirm_upload_404_on_unknown_campaign(client):
    resp = await client.post(
        f"/campaigns/{uuid.uuid4()}/leads/confirm-upload",
        files={"file": ("x.csv", CSV_BASIC, "text/csv")},
        data={"mapping": json.dumps({"Email": "email"})},
    )
    assert resp.status_code == 404


async def test_confirm_upload_ignores_unmapped_fields(client, db_session):
    campaign = await _new_campaign(client)
    csv = b"Email,Notes,First Name\na@x.com,whatever,Alice\n"
    with patch("app.routers.leads.ingest_tasks.run_campaign_research.delay"):
        await client.post(
            f"/campaigns/{campaign['id']}/leads/confirm-upload",
            files={"file": ("x.csv", csv, "text/csv")},
            data={"mapping": json.dumps({"Email": "email", "First Name": "first_name"})},
        )
    lead = await db_session.scalar(
        select(Lead).where(Lead.campaign_id == uuid.UUID(campaign["id"]))
    )
    assert lead.first_name == "Alice"
    # raw_csv_row keeps everything for audit, even unmapped columns.
    assert lead.raw_csv_row["Notes"] == "whatever"


async def test_confirm_upload_with_empty_csv_does_not_enqueue(client):
    campaign = await _new_campaign(client)
    csv = b"Email\n"  # header only, no rows
    with patch("app.routers.leads.ingest_tasks.run_campaign_research.delay") as enqueue:
        resp = await client.post(
            f"/campaigns/{campaign['id']}/leads/confirm-upload",
            files={"file": ("x.csv", csv, "text/csv")},
            data={"mapping": json.dumps({"Email": "email"})},
        )
    assert resp.status_code == 201
    assert resp.json()["total"] == 0
    enqueue.assert_not_called()
