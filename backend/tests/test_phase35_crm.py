"""Phase 35: CRM layer — manual leads, conversion, opportunities, activities."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import (
    CrmActivity,
    Lead,
    Opportunity,
)


def _now():
    return datetime.now(timezone.utc)


# ---------- Manual lead creation ----------

async def test_create_manual_lead_without_campaign(client, db_session):
    resp = await client.post("/crm/leads", json={
        "email": "Jane@Acme.IO ",
        "first_name": "Jane",
        "last_name": "Doe",
        "company": "Acme",
        "job_title": "CFO",
        "notes": "met at conf",
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # Email canonicalised at create.
    assert body["email"] == "jane@acme.io"
    assert body["crm_status"] == "new"

    lead = await db_session.get(Lead, uuid.UUID(body["id"]))
    assert lead.campaign_id is None
    assert lead.notes == "met at conf"


async def test_manual_lead_appears_in_global_list(client):
    created = (await client.post("/crm/leads", json={"email": "solo@x.com"})).json()
    resp = await client.get("/leads")
    assert resp.status_code == 200
    items = resp.json()["items"]
    row = next(i for i in items if i["id"] == created["id"])
    # No campaign — campaign_name None, still listed.
    assert row["campaign_name"] is None
    assert row["crm_status"] == "new"


async def test_create_lead_rejects_bad_email(client):
    resp = await client.post("/crm/leads", json={"email": "not-an-email"})
    assert resp.status_code == 422


async def test_update_crm_status(client):
    created = (await client.post("/crm/leads", json={"email": "s@x.com"})).json()
    resp = await client.patch(f"/crm/leads/{created['id']}", json={"crm_status": "working"})
    assert resp.status_code == 200
    assert resp.json()["crm_status"] == "working"


async def test_update_crm_status_rejects_converted(client):
    """``converted`` is reserved for the convert endpoint."""
    created = (await client.post("/crm/leads", json={"email": "c@x.com"})).json()
    resp = await client.patch(f"/crm/leads/{created['id']}", json={"crm_status": "converted"})
    assert resp.status_code == 400


async def test_update_lead_contact_fields(client, db_session):
    created = (await client.post("/crm/leads", json={"email": "old@x.com"})).json()
    resp = await client.patch(f"/crm/leads/{created['id']}", json={
        "email": "New@Acme.IO", "first_name": "Jane", "last_name": "Roe",
        "company": "Acme", "job_title": "VP Ops", "phone": "555-1212",
        "linkedin_url": "https://linkedin.com/in/jane",
        "company_website": "acme.io", "notes": "updated",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["email"] == "new@acme.io"          # canonicalised
    assert body["first_name"] == "Jane"
    assert body["job_title"] == "VP Ops"
    assert body["company_website"] == "acme.io"

    lead = await db_session.get(Lead, uuid.UUID(created["id"]))
    await db_session.refresh(lead)
    assert lead.email == "new@acme.io"
    assert lead.phone == "555-1212"
    assert lead.notes == "updated"


async def test_update_lead_contact_partial_leaves_others(client, db_session):
    created = (await client.post("/crm/leads", json={
        "email": "p@x.com", "company": "KeepCo", "first_name": "Al",
    })).json()
    # Only change job_title — company + first_name must survive (PATCH).
    resp = await client.patch(f"/crm/leads/{created['id']}", json={"job_title": "Director"})
    assert resp.status_code == 200
    lead = await db_session.get(Lead, uuid.UUID(created["id"]))
    assert lead.job_title == "Director"
    assert lead.company == "KeepCo"
    assert lead.first_name == "Al"


async def test_update_lead_rejects_bad_email(client):
    created = (await client.post("/crm/leads", json={"email": "ok@x.com"})).json()
    resp = await client.patch(f"/crm/leads/{created['id']}", json={"email": "nope"})
    assert resp.status_code == 422


async def test_update_lead_blank_field_clears_it(client, db_session):
    created = (await client.post("/crm/leads", json={
        "email": "z@x.com", "company": "Wipe Me",
    })).json()
    resp = await client.patch(f"/crm/leads/{created['id']}", json={"company": "  "})
    assert resp.status_code == 200
    lead = await db_session.get(Lead, uuid.UUID(created["id"]))
    assert lead.company is None


# ---------- Lead conversion ----------

async def test_convert_lead_creates_opportunity_with_snapshot(client, db_session):
    created = (await client.post("/crm/leads", json={
        "email": "deal@acme.io", "first_name": "Jane", "last_name": "Doe",
        "company": "Acme", "job_title": "CFO", "phone": "+1-555",
        "linkedin_url": "https://www.linkedin.com/in/jane/",
    })).json()

    resp = await client.post(f"/crm/leads/{created['id']}/convert", json={
        "amount": 25000, "name": "Acme — managed IT",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    opp = body["opportunity"]
    # Contact snapshot copied.
    assert opp["first_name"] == "Jane"
    assert opp["company"] == "Acme"
    assert opp["email"] == "deal@acme.io"
    assert opp["linkedin_url"] == "https://www.linkedin.com/in/jane/"
    assert opp["amount"] == 25000
    assert opp["stage"] == "qualification"  # default conversion stage
    assert opp["probability"] == 25         # stage default
    assert opp["source_lead_id"] == created["id"]
    assert body["lead_crm_status"] == "converted"

    # Lead flipped.
    lead = await db_session.get(Lead, uuid.UUID(created["id"]))
    assert lead.crm_status == "converted"
    assert str(lead.converted_opportunity_id) == opp["id"]

    # Conversion logged as an activity spanning both parents.
    act = await db_session.scalar(
        select(CrmActivity).where(CrmActivity.lead_id == lead.id)
    )
    assert act is not None
    assert "converted" in act.subject.lower()
    assert str(act.opportunity_id) == opp["id"]


async def test_convert_already_converted_409s(client):
    created = (await client.post("/crm/leads", json={"email": "dup@x.com"})).json()
    first = await client.post(f"/crm/leads/{created['id']}/convert", json={})
    assert first.status_code == 200
    second = await client.post(f"/crm/leads/{created['id']}/convert", json={})
    assert second.status_code == 409


async def test_convert_default_name_from_company(client):
    created = (await client.post("/crm/leads", json={
        "email": "n@x.com", "company": "Acme Corp",
    })).json()
    resp = await client.post(f"/crm/leads/{created['id']}/convert", json={})
    assert "Acme Corp" in resp.json()["opportunity"]["name"]


# ---------- Opportunities ----------

async def test_opportunity_crud_and_stage_transitions(client):
    # Create.
    resp = await client.post("/crm/opportunities", json={
        "name": "Beta deal", "amount": 10000,
    })
    assert resp.status_code == 201
    opp = resp.json()
    assert opp["stage"] == "prospecting"
    assert opp["probability"] == 10  # stage default

    # Stage advance → probability follows the default.
    resp = await client.patch(f"/crm/opportunities/{opp['id']}", json={"stage": "proposal"})
    assert resp.json()["stage"] == "proposal"
    assert resp.json()["probability"] == 50
    assert resp.json()["closed_at"] is None

    # Close won → closed_at stamped, probability 100.
    resp = await client.patch(f"/crm/opportunities/{opp['id']}", json={"stage": "closed_won"})
    assert resp.json()["closed_at"] is not None
    assert resp.json()["probability"] == 100

    # Reopen → closed_at + loss_reason cleared.
    resp = await client.patch(f"/crm/opportunities/{opp['id']}", json={"stage": "negotiation"})
    assert resp.json()["closed_at"] is None
    assert resp.json()["probability"] == 75


async def test_opportunity_explicit_probability_wins_over_stage_default(client):
    opp = (await client.post("/crm/opportunities", json={"name": "X"})).json()
    resp = await client.patch(
        f"/crm/opportunities/{opp['id']}",
        json={"stage": "proposal", "probability": 80},
    )
    assert resp.json()["probability"] == 80


async def test_opportunity_list_filters(client):
    await client.post("/crm/opportunities", json={"name": "Open deal"})
    closed = (await client.post("/crm/opportunities", json={"name": "Done deal"})).json()
    await client.patch(f"/crm/opportunities/{closed['id']}", json={"stage": "closed_won"})

    all_resp = await client.get("/crm/opportunities")
    assert all_resp.json()["total"] == 2

    open_resp = await client.get("/crm/opportunities", params={"open_only": "true"})
    assert open_resp.json()["total"] == 1
    assert open_resp.json()["items"][0]["name"] == "Open deal"

    search_resp = await client.get("/crm/opportunities", params={"search": "done"})
    assert search_resp.json()["total"] == 1


async def test_pipeline_summary_rolls_up_counts_and_amounts(client):
    await client.post("/crm/opportunities", json={"name": "A", "amount": 1000})
    await client.post("/crm/opportunities", json={"name": "B", "amount": 2000})
    resp = await client.get("/crm/opportunities/pipeline")
    assert resp.status_code == 200
    rows = {r["stage"]: r for r in resp.json()}
    # All stages present even when empty.
    assert set(rows.keys()) == {
        "prospecting", "qualification", "proposal", "negotiation",
        "closed_won", "closed_lost",
    }
    assert rows["prospecting"]["count"] == 2
    assert rows["prospecting"]["total_amount"] == 3000.0
    assert rows["closed_won"]["count"] == 0


async def test_delete_opportunity_unconverts_source_lead(client, db_session):
    created = (await client.post("/crm/leads", json={"email": "uncv@x.com"})).json()
    conv = (await client.post(f"/crm/leads/{created['id']}/convert", json={})).json()
    opp_id = conv["opportunity"]["id"]

    resp = await client.delete(f"/crm/opportunities/{opp_id}")
    assert resp.status_code == 204

    lead = await db_session.get(Lead, uuid.UUID(created["id"]))
    await db_session.refresh(lead)
    assert lead.converted_opportunity_id is None
    # Demoted back to qualified, ready to convert again.
    assert lead.crm_status == "qualified"
    second = await client.post(f"/crm/leads/{created['id']}/convert", json={})
    assert second.status_code == 200


# ---------- Activities ----------

async def test_log_activity_against_lead(client):
    lead = (await client.post("/crm/leads", json={"email": "act@x.com"})).json()
    resp = await client.post("/crm/activities", json={
        "lead_id": lead["id"],
        "activity_type": "call",
        "subject": "Intro call",
        "body": "Talked 20 min, interested in managed IT.",
        "direction": "outbound",
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["activity_type"] == "call"
    assert body["direction"] == "outbound"
    assert body["completed_at"] is None


async def test_activity_requires_a_parent(client):
    resp = await client.post("/crm/activities", json={
        "activity_type": "note", "subject": "orphan",
    })
    assert resp.status_code == 422


async def test_activity_404_on_unknown_parent(client):
    resp = await client.post("/crm/activities", json={
        "lead_id": str(uuid.uuid4()),
        "activity_type": "note", "subject": "x",
    })
    assert resp.status_code == 404


async def test_task_complete_and_reopen(client):
    lead = (await client.post("/crm/leads", json={"email": "task@x.com"})).json()
    due = (_now() + timedelta(days=2)).isoformat()
    task = (await client.post("/crm/activities", json={
        "lead_id": lead["id"], "activity_type": "task",
        "subject": "Send proposal", "due_at": due,
    })).json()
    assert task["completed_at"] is None

    done = await client.patch(f"/crm/activities/{task['id']}", json={"completed": True})
    assert done.json()["completed_at"] is not None

    reopened = await client.patch(f"/crm/activities/{task['id']}", json={"completed": False})
    assert reopened.json()["completed_at"] is None


async def test_logging_activity_autocompletes_due_task(client):
    """Logging a touch (call/email/meeting/note) on a lead closes that lead's
    open due/overdue tasks — the reminder's work has now been done."""
    lead = (await client.post("/crm/leads", json={"email": "autoc@x.com"})).json()
    overdue = (_now() - timedelta(days=1)).isoformat()
    task = (await client.post("/crm/activities", json={
        "lead_id": lead["id"], "activity_type": "task",
        "subject": "Reach out", "due_at": overdue,
    })).json()
    assert task["completed_at"] is None

    # Log a call on the lead → the overdue reach-out task auto-completes.
    logged = await client.post("/crm/activities", json={
        "lead_id": lead["id"], "activity_type": "call",
        "subject": "Called them back", "direction": "outbound",
    })
    assert logged.status_code == 201

    open_tasks = (await client.get(
        f"/crm/activities?lead_id={lead['id']}&open_tasks=true"
    )).json()
    assert open_tasks["total"] == 0  # the task is no longer open


async def test_logging_activity_leaves_future_task_open(client):
    """A touch must not prematurely close a task scheduled for the future."""
    lead = (await client.post("/crm/leads", json={"email": "future@x.com"})).json()
    future = (_now() + timedelta(days=5)).isoformat()
    await client.post("/crm/activities", json={
        "lead_id": lead["id"], "activity_type": "task",
        "subject": "Send proposal next week", "due_at": future,
    })
    await client.post("/crm/activities", json={
        "lead_id": lead["id"], "activity_type": "note", "subject": "quick note",
    })
    open_tasks = (await client.get(
        f"/crm/activities?lead_id={lead['id']}&open_tasks=true"
    )).json()
    assert open_tasks["total"] == 1  # future task stays open


async def test_open_tasks_view_orders_by_due_date(client):
    lead = (await client.post("/crm/leads", json={"email": "due@x.com"})).json()
    later = (_now() + timedelta(days=5)).isoformat()
    sooner = (_now() + timedelta(days=1)).isoformat()
    await client.post("/crm/activities", json={
        "lead_id": lead["id"], "activity_type": "task", "subject": "later", "due_at": later,
    })
    await client.post("/crm/activities", json={
        "lead_id": lead["id"], "activity_type": "task", "subject": "sooner", "due_at": sooner,
    })
    # A completed task must not show.
    done = (await client.post("/crm/activities", json={
        "lead_id": lead["id"], "activity_type": "task", "subject": "done", "due_at": sooner,
    })).json()
    await client.patch(f"/crm/activities/{done['id']}", json={"completed": True})
    # And a note isn't a task.
    await client.post("/crm/activities", json={
        "lead_id": lead["id"], "activity_type": "note", "subject": "not a task",
    })

    resp = await client.get("/crm/activities", params={"open_tasks": "true"})
    body = resp.json()
    assert body["total"] == 2
    assert [i["subject"] for i in body["items"]] == ["sooner", "later"]


async def test_activities_filter_by_opportunity(client):
    opp = (await client.post("/crm/opportunities", json={"name": "F"})).json()
    await client.post("/crm/activities", json={
        "opportunity_id": opp["id"], "activity_type": "meeting", "subject": "Demo",
    })
    resp = await client.get("/crm/activities", params={"opportunity_id": opp["id"]})
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["subject"] == "Demo"


async def test_lead_detail_history_includes_crm_activities(client):
    """The unified lead timeline merges manual CRM activities alongside
    the automated sends/events, labelled with the type prefix."""
    lead = (await client.post("/crm/leads", json={"email": "hist@x.com"})).json()
    await client.post("/crm/activities", json={
        "lead_id": lead["id"], "activity_type": "call",
        "subject": "Discovery chat", "direction": "inbound",
    })

    resp = await client.get(f"/leads/{lead['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["crm_status"] == "new"
    crm_items = [h for h in body["history"] if h["kind"] == "crm"]
    assert len(crm_items) == 1
    assert "Call (inbound): Discovery chat" == crm_items[0]["action"]
    assert body["history_counts"]["crm_call"] == 1


async def test_opportunity_activity_counts_rollup(client):
    opp = (await client.post("/crm/opportunities", json={"name": "R"})).json()
    await client.post("/crm/activities", json={
        "opportunity_id": opp["id"], "activity_type": "note", "subject": "n",
    })
    await client.post("/crm/activities", json={
        "opportunity_id": opp["id"], "activity_type": "task", "subject": "t",
    })
    resp = await client.get(f"/crm/opportunities/{opp['id']}")
    body = resp.json()
    assert body["activity_count"] == 2
    assert body["open_task_count"] == 1


# ---------- Documents ----------

async def _make_opp(client, name="Doc deal"):
    return (await client.post("/crm/opportunities", json={"name": name})).json()


async def test_document_upload_list_download_delete(client):
    opp = await _make_opp(client)

    # Upload.
    resp = await client.post(
        f"/crm/opportunities/{opp['id']}/documents",
        files={"file": ("proposal.pdf", b"%PDF-1.4 fake pdf bytes", "application/pdf")},
    )
    assert resp.status_code == 201, resp.text
    doc = resp.json()
    assert doc["filename"] == "proposal.pdf"
    assert doc["content_type"] == "application/pdf"
    assert doc["size_bytes"] == len(b"%PDF-1.4 fake pdf bytes")

    # List (no data payload in the listing).
    listing = (await client.get(f"/crm/opportunities/{opp['id']}/documents")).json()
    assert len(listing) == 1
    assert listing[0]["filename"] == "proposal.pdf"
    assert "data" not in listing[0]

    # Download returns the exact bytes + attachment header.
    dl = await client.get(f"/crm/documents/{doc['id']}/download")
    assert dl.status_code == 200
    assert dl.content == b"%PDF-1.4 fake pdf bytes"
    assert dl.headers["content-type"].startswith("application/pdf")
    assert 'filename="proposal.pdf"' in dl.headers["content-disposition"]

    # Delete.
    rm = await client.delete(f"/crm/documents/{doc['id']}")
    assert rm.status_code == 204
    assert (await client.get(f"/crm/opportunities/{opp['id']}/documents")).json() == []


async def test_document_upload_rejects_oversize(client):
    from app.routers.crm import MAX_DOCUMENT_BYTES
    opp = await _make_opp(client)
    big = b"x" * (MAX_DOCUMENT_BYTES + 1)
    resp = await client.post(
        f"/crm/opportunities/{opp['id']}/documents",
        files={"file": ("huge.bin", big, "application/octet-stream")},
    )
    assert resp.status_code == 413
    assert "too large" in resp.json()["detail"].lower()


async def test_document_upload_rejects_empty_file(client):
    opp = await _make_opp(client)
    resp = await client.post(
        f"/crm/opportunities/{opp['id']}/documents",
        files={"file": ("empty.txt", b"", "text/plain")},
    )
    assert resp.status_code == 422


async def test_document_upload_404_unknown_opportunity(client):
    resp = await client.post(
        f"/crm/opportunities/{uuid.uuid4()}/documents",
        files={"file": ("x.txt", b"hi", "text/plain")},
    )
    assert resp.status_code == 404


async def test_documents_cascade_on_opportunity_delete(client, db_session):
    from app.models import CrmDocument
    opp = await _make_opp(client)
    await client.post(
        f"/crm/opportunities/{opp['id']}/documents",
        files={"file": ("a.txt", b"a", "text/plain")},
    )
    await client.delete(f"/crm/opportunities/{opp['id']}")
    remaining = (await db_session.execute(
        select(CrmDocument).where(CrmDocument.opportunity_id == uuid.UUID(opp["id"]))
    )).scalars().all()
    assert remaining == []


# ---------- Products of interest ----------

async def test_product_add_list_with_line_totals(client):
    opp = await _make_opp(client, name="Product deal")

    r1 = await client.post(f"/crm/opportunities/{opp['id']}/products", json={
        "product_name": "Managed IT (24 seats)", "quantity": 24, "unit_price": 95,
    })
    assert r1.status_code == 201
    assert r1.json()["line_total"] == 2280.0

    # Unpriced product → line_total None, excluded from the total.
    r2 = await client.post(f"/crm/opportunities/{opp['id']}/products", json={
        "product_name": "Network audit",
    })
    assert r2.json()["line_total"] is None
    assert r2.json()["quantity"] == 1.0  # default

    listing = (await client.get(f"/crm/opportunities/{opp['id']}/products")).json()
    assert len(listing["items"]) == 2
    assert listing["products_total"] == 2280.0


async def test_product_update_and_delete(client):
    opp = await _make_opp(client, name="P2")
    p = (await client.post(f"/crm/opportunities/{opp['id']}/products", json={
        "product_name": "Seats", "quantity": 10, "unit_price": 50,
    })).json()

    upd = await client.patch(f"/crm/products/{p['id']}", json={"quantity": 20})
    assert upd.json()["line_total"] == 1000.0

    rm = await client.delete(f"/crm/products/{p['id']}")
    assert rm.status_code == 204
    listing = (await client.get(f"/crm/opportunities/{opp['id']}/products")).json()
    assert listing["items"] == []


async def test_product_rejects_zero_quantity(client):
    opp = await _make_opp(client, name="P3")
    resp = await client.post(f"/crm/opportunities/{opp['id']}/products", json={
        "product_name": "X", "quantity": 0,
    })
    assert resp.status_code == 422
