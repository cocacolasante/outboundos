"""Phase 11: GET /settings/api-status."""
from app.config import settings


async def test_api_status_returns_all_four_keys(client):
    resp = await client.get("/settings/api-status")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"anthropic", "brevo", "apollo", "hunter"}
    for v in body.values():
        assert isinstance(v, bool)


async def test_api_status_reflects_settings_values(client, monkeypatch):
    """BYOK (Phase 4): api-status reflects the TENANT's stored keys, not
    env vars.  The conftest seeds all providers for the bootstrap tenant;
    deleting one flips its flag."""
    body = (await client.get("/settings/api-status")).json()
    assert body == {"anthropic": True, "brevo": True, "apollo": True, "hunter": True}

    assert (await client.delete("/settings/integrations/brevo")).status_code == 204
    assert (await client.delete("/settings/integrations/hunter")).status_code == 204
    body = (await client.get("/settings/api-status")).json()
    assert body == {"anthropic": True, "brevo": False, "apollo": True, "hunter": False}


async def test_api_status_never_returns_actual_key_values(client, monkeypatch):
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "sk-ant-secret-xyz")
    monkeypatch.setattr(settings, "BREVO_API_KEY", "brevo-secret")
    monkeypatch.setattr(settings, "APOLLO_API_KEY", "apollo-secret")
    monkeypatch.setattr(settings, "HUNTER_API_KEY", "hunter-secret")

    text = (await client.get("/settings/api-status")).text
    assert "sk-ant-secret-xyz" not in text
    assert "brevo-secret" not in text
    assert "apollo-secret" not in text
    assert "hunter-secret" not in text
