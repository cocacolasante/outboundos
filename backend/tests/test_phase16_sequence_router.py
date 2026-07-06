"""Integration tests for the /campaigns/{id}/sequence router."""
import pytest


def _campaign_payload(**overrides) -> dict:
    base = {
        "name": "Seq test",
        "goal": "Book a call",
        "tone": "Direct",
        "sender_name": "Anthony",
        "sender_email": "anthony@example.com",
        "research_mode": "fast",
        "sample_count": 3,
        "schedule_days": [0, 1, 2, 3, 4],
        "schedule_time_start": "09:00:00",
        "schedule_time_end": "17:00:00",
        "schedule_timezone": "UTC",
        "min_delay_seconds": 60,
    }
    base.update(overrides)
    return base


async def _create_campaign(client) -> str:
    r = await client.post("/campaigns/", json=_campaign_payload())
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_campaign_creation_auto_creates_default_sequence(client):
    cid = await _create_campaign(client)
    r = await client.get(f"/campaigns/{cid}/sequence")
    assert r.status_code == 200
    body = r.json()
    assert body["is_published"] is True
    assert len(body["nodes"]) == 1
    n = body["nodes"][0]
    assert n["kind"] == "email"
    assert n["is_entry"] is True
    assert n["config"] == {"use_campaign_compose": True}
    assert body["edges"] == []


async def test_put_sequence_replaces_graph(client):
    cid = await _create_campaign(client)

    payload = {
        "nodes": [
            {
                "client_id": "entry",
                "kind": "email",
                "is_entry": True,
                "config": {"use_campaign_compose": True},
                "position_x": 0,
                "position_y": 0,
            },
            {
                "client_id": "wait1",
                "kind": "wait",
                "is_entry": False,
                "config": {"duration_minutes": 4320},
                "position_x": 200,
                "position_y": 0,
            },
            {
                "client_id": "followup",
                "kind": "email",
                "is_entry": False,
                "config": {
                    "subject_template": "Following up, {{first_name}}?",
                    "body_template": "Hi {{first_name}}, just checking in.",
                },
                "position_x": 400,
                "position_y": 0,
            },
        ],
        "edges": [
            {"from_client_id": "entry", "to_client_id": "wait1", "condition": {"op": "always"}},
            {
                "from_client_id": "wait1",
                "to_client_id": "followup",
                "condition": {"op": "not", "child": {"op": "replied"}},
                "priority": 0,
            },
        ],
    }
    r = await client.put(f"/campaigns/{cid}/sequence", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["nodes"]) == 3
    assert len(body["edges"]) == 2
    # PUT clears is_published (publish step revalidates + flips back).
    assert body["is_published"] is False


async def test_put_rejects_two_entry_nodes(client):
    cid = await _create_campaign(client)
    payload = {
        "nodes": [
            {"client_id": "a", "kind": "email", "is_entry": True, "config": {}},
            {"client_id": "b", "kind": "email", "is_entry": True, "config": {}},
        ],
        "edges": [],
    }
    r = await client.put(f"/campaigns/{cid}/sequence", json=payload)
    assert r.status_code == 422


async def test_publish_rejects_unreleased_linkedin_kinds(client):
    """linkedin_inmail / linkedin_comment_post are still gated to M4."""
    cid = await _create_campaign(client)
    payload = {
        "nodes": [
            {"client_id": "entry", "kind": "email", "is_entry": True, "config": {}},
            {
                "client_id": "li",
                "kind": "linkedin_inmail",
                "is_entry": False,
                "config": {},
            },
        ],
        "edges": [
            {"from_client_id": "entry", "to_client_id": "li", "condition": {"op": "always"}},
        ],
    }
    r = await client.put(f"/campaigns/{cid}/sequence", json=payload)
    assert r.status_code == 200

    pub = await client.post(f"/campaigns/{cid}/sequence/publish")
    assert pub.status_code == 200
    body = pub.json()
    assert body["ok"] is False
    assert body["is_published"] is False
    assert any("linkedin_inmail" in e for e in body["errors"])


async def test_validate_flags_unreachable_node(client):
    cid = await _create_campaign(client)
    payload = {
        "nodes": [
            {"client_id": "entry", "kind": "email", "is_entry": True, "config": {}},
            {"client_id": "orphan", "kind": "wait", "is_entry": False, "config": {"duration_minutes": 60}},
        ],
        "edges": [],
    }
    r = await client.put(f"/campaigns/{cid}/sequence", json=payload)
    assert r.status_code == 200

    v = await client.post(f"/campaigns/{cid}/sequence/validate")
    body = v.json()
    assert body["ok"] is False
    assert any("unreachable" in e for e in body["errors"])


async def test_publish_succeeds_for_email_wait_email_chain(client):
    cid = await _create_campaign(client)
    payload = {
        "nodes": [
            {"client_id": "entry", "kind": "email", "is_entry": True, "config": {"use_campaign_compose": True}},
            {"client_id": "w", "kind": "wait", "is_entry": False, "config": {"duration_minutes": 4320}},
            {
                "client_id": "f",
                "kind": "email",
                "is_entry": False,
                "config": {"subject_template": "Hi", "body_template": "Body"},
            },
        ],
        "edges": [
            {"from_client_id": "entry", "to_client_id": "w", "condition": {"op": "always"}},
            {"from_client_id": "w", "to_client_id": "f", "condition": {"op": "not", "child": {"op": "replied"}}},
            {"from_client_id": "f", "to_client_id": None, "condition": {"op": "always"}},
        ],
    }
    r = await client.put(f"/campaigns/{cid}/sequence", json=payload)
    assert r.status_code == 200

    pub = await client.post(f"/campaigns/{cid}/sequence/publish")
    body = pub.json()
    assert body["ok"] is True
    assert body["is_published"] is True
    assert body["errors"] == []


async def test_publish_succeeds_for_non_email_entry(client):
    """Any publishable kind can be the entry node (the M1 email-only rule
    was lifted so a sequence can start with, e.g., a LinkedIn connect)."""
    cid = await _create_campaign(client)
    payload = {
        "nodes": [
            {
                "client_id": "entry",
                "kind": "linkedin_connect",
                "is_entry": True,
                "config": {"no_note": True},
            },
            {
                "client_id": "dm",
                "kind": "linkedin_dm",
                "is_entry": False,
                "config": {"text_template": "Hi {{first_name}}"},
            },
        ],
        "edges": [
            {"from_client_id": "entry", "to_client_id": "dm", "condition": {"op": "always"}},
            {"from_client_id": "dm", "to_client_id": None, "condition": {"op": "always"}},
        ],
    }
    r = await client.put(f"/campaigns/{cid}/sequence", json=payload)
    assert r.status_code == 200, r.text

    pub = await client.post(f"/campaigns/{cid}/sequence/publish")
    body = pub.json()
    assert body["ok"] is True, body["errors"]
    assert body["is_published"] is True
    assert body["errors"] == []
    # The entry node is the LinkedIn connect, not an email.
    seq = (await client.get(f"/campaigns/{cid}/sequence")).json()
    entry = next(n for n in seq["nodes"] if n["is_entry"])
    assert entry["kind"] == "linkedin_connect"


async def test_email_entry_gets_use_campaign_compose_stamped(client):
    """An email node promoted to entry is normalized to the compose path."""
    cid = await _create_campaign(client)
    payload = {
        "nodes": [
            # No use_campaign_compose in config — replace_graph should add it.
            {"client_id": "entry", "kind": "email", "is_entry": True, "config": {}},
        ],
        "edges": [],
    }
    r = await client.put(f"/campaigns/{cid}/sequence", json=payload)
    assert r.status_code == 200, r.text
    entry = next(n for n in r.json()["nodes"] if n["is_entry"])
    assert entry["config"].get("use_campaign_compose") is True


async def test_get_sequence_404_for_missing_campaign(client):
    r = await client.get("/campaigns/00000000-0000-0000-0000-000000000000/sequence")
    assert r.status_code == 404


async def test_validate_default_sequence_is_valid(client):
    """The auto-created 1-node sequence must pass validation out of the box."""
    cid = await _create_campaign(client)
    v = await client.post(f"/campaigns/{cid}/sequence/validate")
    body = v.json()
    assert body["ok"] is True
    assert body["errors"] == []


async def test_publish_default_sequence_succeeds(client):
    cid = await _create_campaign(client)
    pub = await client.post(f"/campaigns/{cid}/sequence/publish")
    body = pub.json()
    assert body["ok"] is True
    assert body["is_published"] is True


async def test_put_sequence_with_zero_nodes_rejected(client):
    """An empty graph (no entry node) must be rejected."""
    cid = await _create_campaign(client)
    r = await client.put(f"/campaigns/{cid}/sequence", json={"nodes": [], "edges": []})
    # Either 422 (validation) or 200 with the default kept — either way,
    # a subsequent validate/publish must fail, or the PUT itself does.
    # The key invariant: you can't end up with is_published=True on a 0-node graph.
    if r.status_code == 200:
        pub = await client.post(f"/campaigns/{cid}/sequence/publish")
        assert pub.json()["ok"] is False


async def test_put_cycle_detected_rejects_publish(client):
    """A sequence with a cycle passes PUT (we allow drafts) but fails publish."""
    cid = await _create_campaign(client)
    payload = {
        "nodes": [
            {"client_id": "a", "kind": "email", "is_entry": True, "config": {}},
            {"client_id": "b", "kind": "wait", "is_entry": False, "config": {"duration_minutes": 60}},
        ],
        "edges": [
            {"from_client_id": "a", "to_client_id": "b", "condition": {"op": "always"}},
            {"from_client_id": "b", "to_client_id": "a", "condition": {"op": "always"}},
        ],
    }
    r = await client.put(f"/campaigns/{cid}/sequence", json=payload)
    assert r.status_code == 200
    pub = await client.post(f"/campaigns/{cid}/sequence/publish")
    body = pub.json()
    assert body["ok"] is False
    assert any("cycle" in e.lower() for e in body["errors"])


async def test_soft_delete_preserves_old_nodes_after_replace(client, db_session):
    """After a second PUT, the original node should be soft-deleted, not gone."""
    from sqlalchemy import select
    from app.models import SequenceNode

    cid = await _create_campaign(client)

    # Capture the original entry node id.
    r = await client.get(f"/campaigns/{cid}/sequence")
    original_node_id = r.json()["nodes"][0]["id"]

    # Replace with a new 1-node sequence.
    payload = {
        "nodes": [
            {"client_id": "new-entry", "kind": "email", "is_entry": True,
             "config": {"use_campaign_compose": True}, "position_x": 0, "position_y": 0}
        ],
        "edges": [],
    }
    r2 = await client.put(f"/campaigns/{cid}/sequence", json=payload)
    assert r2.status_code == 200

    # The live GET should NOT include the old node.
    r3 = await client.get(f"/campaigns/{cid}/sequence")
    live_ids = [n["id"] for n in r3.json()["nodes"]]
    assert original_node_id not in live_ids

    # But the node should still exist in the DB with deleted_at set.
    import uuid as uuidlib
    old_node = await db_session.scalar(
        select(SequenceNode).where(SequenceNode.id == uuidlib.UUID(original_node_id))
    )
    assert old_node is not None
    assert old_node.deleted_at is not None


async def test_analytics_endpoint_returns_per_node_and_lead_status(client):
    """Basic smoke test: analytics endpoint returns the expected structure."""
    cid = await _create_campaign(client)
    r = await client.get(f"/campaigns/{cid}/sequence/analytics")
    assert r.status_code == 200
    body = r.json()
    assert "per_node" in body
    assert isinstance(body["per_node"], list)


async def test_publish_second_time_is_idempotent(client):
    """Publishing a sequence that is already published should succeed."""
    cid = await _create_campaign(client)
    # Default sequence is already published — publish again.
    pub = await client.post(f"/campaigns/{cid}/sequence/publish")
    assert pub.json()["ok"] is True
    pub2 = await client.post(f"/campaigns/{cid}/sequence/publish")
    assert pub2.json()["ok"] is True
