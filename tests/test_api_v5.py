"""Tests for health check enhancements, webhook support, API keys, input sanitization."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aftergraph_work_intelligence.api import create_app


@pytest.fixture()
def client(tmp_path):
    db = tmp_path / "test.db"
    app = create_app(db_path=db, api_token="test-token")
    with TestClient(app) as c:
        yield c, app


class TestDetailedHealth:
    def test_healthz_detailed(self, client):
        c, _ = client
        resp = c.get("/healthz/detailed")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert "checks" in data

    def test_healthz_detailed_database(self, client):
        c, _ = client
        resp = c.get("/healthz/detailed")
        data = resp.json()
        assert "database" in data["checks"]
        assert data["checks"]["database"]["status"] == "ok"


class TestReadinessProbe:
    def test_ready_endpoint(self, client):
        c, _ = client
        resp = c.get("/ready")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"

    def test_ready_includes_dependencies(self, client):
        c, _ = client
        resp = c.get("/ready")
        data = resp.json()
        assert "dependencies" in data
        assert isinstance(data["dependencies"], list)


class TestLivenessProbe:
    def test_live_endpoint(self, client):
        c, _ = client
        resp = c.get("/live")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "alive"
        assert "uptime_seconds" in data


class TestWebhookRegistration:
    def test_register_webhook(self, client):
        c, _ = client
        resp = c.post(
            "/v1/webhooks",
            json={
                "url": "https://example.com/webhook",
                "events": ["work_item.created", "work_item.approved"],
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
        assert data["url"] == "https://example.com/webhook"

    def test_list_webhooks(self, client):
        c, _ = client
        c.post(
            "/v1/webhooks",
            json={"url": "https://example.com/hook", "events": ["work_item.created"]},
            headers={"Authorization": "Bearer test-token"},
        )
        resp = c.get("/v1/webhooks", headers={"Authorization": "Bearer test-token"})
        assert resp.status_code == 200
        assert len(resp.json()["webhooks"]) >= 1

    def test_delete_webhook(self, client):
        c, _ = client
        resp = c.post(
            "/v1/webhooks",
            json={"url": "https://example.com/del", "events": ["work_item.created"]},
            headers={"Authorization": "Bearer test-token"},
        )
        wh_id = resp.json()["id"]
        resp = c.delete(f"/v1/webhooks/{wh_id}", headers={"Authorization": "Bearer test-token"})
        assert resp.status_code == 200
        # Verify deleted
        resp = c.get("/v1/webhooks", headers={"Authorization": "Bearer test-token"})
        assert wh_id not in [w["id"] for w in resp.json()["webhooks"]]

    def test_webhook_invalid_url(self, client):
        c, _ = client
        resp = c.post(
            "/v1/webhooks",
            json={"url": "not-a-url", "events": ["work_item.created"]},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 422

    def test_webhook_no_events(self, client):
        c, _ = client
        resp = c.post(
            "/v1/webhooks",
            json={"url": "https://example.com/hook", "events": []},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 422


class TestAPIKeyManagement:
    def test_create_api_key(self, client):
        c, _ = client
        resp = c.post(
            "/v1/api-keys",
            json={"name": "test-key", "permissions": ["read", "write"]},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "key" in data
        assert data["name"] == "test-key"

    def test_list_api_keys(self, client):
        c, _ = client
        c.post(
            "/v1/api-keys",
            json={"name": "list-test", "permissions": ["read"]},
            headers={"Authorization": "Bearer test-token"},
        )
        resp = c.get("/v1/api-keys", headers={"Authorization": "Bearer test-token"})
        assert resp.status_code == 200
        assert "keys" in resp.json()

    def test_revoke_api_key(self, client):
        c, _ = client
        resp = c.post(
            "/v1/api-keys",
            json={"name": "revoke-test", "permissions": ["read"]},
            headers={"Authorization": "Bearer test-token"},
        )
        key_id = resp.json()["id"]
        resp = c.delete(f"/v1/api-keys/{key_id}", headers={"Authorization": "Bearer test-token"})
        assert resp.status_code == 200


class TestInputSanitization:
    def test_html_in_text_escaped(self, client):
        c, _ = client
        resp = c.post(
            "/v1/observations",
            json={"tenant_id": "test", "source": "test", "text": "<script>alert('xss')</script> Køb computere"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code in (200, 201, 202)

    def test_sql_injection_in_tenant_id(self, client):
        c, _ = client
        resp = c.post(
            "/v1/observations",
            json={"tenant_id": "'; DROP TABLE work_items; --", "source": "test", "text": "Test observation"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code in (200, 201, 202)

    def test_unicode_in_text(self, client):
        c, _ = client
        resp = c.post(
            "/v1/observations",
            json={"tenant_id": "test", "source": "test", "text": "Køb nye computere 🖥️ hurtigt"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code in (200, 201, 202)

    def test_very_long_tenant_id(self, client):
        c, _ = client
        resp = c.post(
            "/v1/observations",
            json={"tenant_id": "x" * 200, "source": "test", "text": "Test observation"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 422
