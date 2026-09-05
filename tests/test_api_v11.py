"""Tests for webhooks, API keys, and health probes."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from aftergraph_work_intelligence.api import create_app


@pytest.fixture()
def client(tmp_path):
    db = tmp_path / "test.db"
    app = create_app(db_path=db, api_token="test-token")
    with TestClient(app) as c:
        yield c


AUTH = {"Authorization": "Bearer test-token"}


class TestWebhooks:
    """Verify webhook CRUD operations."""

    def test_create_webhook(self, client):
        resp = client.post("/v1/webhooks", json={
            "url": "https://example.com/hook",
            "events": ["ingest"],
        }, headers=AUTH)
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
        assert data["url"] == "https://example.com/hook"
        assert data["events"] == ["ingest"]

    def test_list_webhooks(self, client):
        # Create a webhook first
        client.post("/v1/webhooks", json={
            "url": "https://example.com/hook",
            "events": ["ingest"],
        }, headers=AUTH)

        resp = client.get("/v1/webhooks", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert "webhooks" in data
        assert len(data["webhooks"]) >= 1

    def test_delete_webhook(self, client):
        # Create
        resp = client.post("/v1/webhooks", json={
            "url": "https://example.com/hook",
            "events": ["ingest"],
        }, headers=AUTH)
        wh_id = resp.json()["id"]

        # Delete
        resp = client.delete(f"/v1/webhooks/{wh_id}", headers=AUTH)
        assert resp.status_code == 200

        # Verify gone
        resp = client.get("/v1/webhooks", headers=AUTH)
        assert not any(w["id"] == wh_id for w in resp.json()["webhooks"])

    def test_delete_nonexistent_webhook(self, client):
        resp = client.delete(f"/v1/webhooks/{uuid.uuid4()}", headers=AUTH)
        assert resp.status_code == 404

    def test_webhook_requires_auth(self, client):
        resp = client.post("/v1/webhooks", json={
            "url": "https://example.com/hook",
            "events": ["ingest"],
        })
        assert resp.status_code == 401

    def test_webhook_invalid_url(self, client):
        resp = client.post("/v1/webhooks", json={
            "url": "not-a-url",
            "events": ["ingest"],
        }, headers=AUTH)
        assert resp.status_code in (400, 422)

    def test_webhook_empty_events(self, client):
        resp = client.post("/v1/webhooks", json={
            "url": "https://example.com/hook",
            "events": [],
        }, headers=AUTH)
        assert resp.status_code in (201, 422)


class TestAPIKeys:
    """Verify API key lifecycle."""

    def test_create_api_key(self, client):
        resp = client.post("/v1/api-keys", json={"name": "test-key"}, headers=AUTH)
        assert resp.status_code == 201
        data = resp.json()
        assert "key" in data
        assert "id" in data

    def test_list_api_keys(self, client):
        # Create a key first
        client.post("/v1/api-keys", json={"name": "test-key"}, headers=AUTH)

        resp = client.get("/v1/api-keys", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert "keys" in data
        assert len(data["keys"]) >= 1

    def test_revoke_api_key(self, client):
        # Create
        resp = client.post("/v1/api-keys", json={"name": "test-key"}, headers=AUTH)
        key_id = resp.json()["id"]

        # Revoke
        resp = client.delete(f"/v1/api-keys/{key_id}", headers=AUTH)
        assert resp.status_code == 200

    def test_revoke_nonexistent_key(self, client):
        resp = client.delete(f"/v1/api-keys/{uuid.uuid4()}", headers=AUTH)
        assert resp.status_code == 404

    def test_api_key_requires_auth(self, client):
        resp = client.post("/v1/api-keys", json={"name": "test-key"})
        assert resp.status_code == 401

    def test_api_key_lifecycle(self, client):
        # Create
        resp = client.post("/v1/api-keys", json={"name": "lifecycle-key"}, headers=AUTH)
        assert resp.status_code == 201
        key_id = resp.json()["id"]

        # List
        resp = client.get("/v1/api-keys", headers=AUTH)
        assert resp.status_code == 200
        assert any(k["id"] == key_id for k in resp.json()["keys"])

        # Revoke
        resp = client.delete(f"/v1/api-keys/{key_id}", headers=AUTH)
        assert resp.status_code == 200


class TestHealthProbes:
    """Verify health probe endpoints."""

    def test_healthz(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_healthz_detailed(self, client):
        resp = client.get("/healthz/detailed")
        assert resp.status_code == 200
        data = resp.json()
        # Response has status and system info
        assert "status" in data or "system" in data

    def test_ready(self, client):
        resp = client.get("/ready")
        assert resp.status_code == 200
        # Response status may be "ok" or "ready"
        assert resp.json().get("status") in ("ok", "ready")

    def test_live(self, client):
        resp = client.get("/live")
        assert resp.status_code == 200
        # Response status may be "ok" or "alive"
        assert resp.json().get("status") in ("ok", "alive")

    def test_health_probes_no_auth(self, client):
        for path in ["/healthz", "/healthz/detailed", "/ready", "/live"]:
            resp = client.get(path)
            assert resp.status_code == 200, f"{path} should not require auth"

    def test_health_probe_response_time(self, client):
        import time
        start = time.time()
        resp = client.get("/healthz")
        elapsed = time.time() - start
        assert resp.status_code == 200
        assert elapsed < 1.0  # Should respond within 1 second


class TestErrorResponseConsistency:
    """Verify error responses are consistent across endpoints."""

    def test_404_consistent(self, client):
        paths = [
            f"/v1/work-items/{uuid.uuid4()}?tenant_id=default",
            f"/v1/work-items/{uuid.uuid4()}/transitions?tenant_id=default",
            f"/v1/work-items/{uuid.uuid4()}/publications?tenant_id=default",
            f"/v1/webhooks/{uuid.uuid4()}",
            f"/v1/api-keys/{uuid.uuid4()}",
        ]
        for path in paths:
            resp = client.get(path, headers=AUTH)
            # Some paths may return 405 if wrong method
            assert resp.status_code in (404, 405), f"{path} returned {resp.status_code}"

    def test_422_has_detail(self, client):
        resp = client.post("/v1/observations", json={}, headers=AUTH)
        assert resp.status_code == 422
        data = resp.json()
        assert "detail" in data
        assert isinstance(data["detail"], list)
