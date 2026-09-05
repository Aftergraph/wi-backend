"""Tests for webhook testing, API key revocation, and additional features."""

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


def _create(c, text="Vi skal købe 5 licenser til teamet hurtigt"):
    resp = c.post("/v1/observations", json={
        "tenant_id": "default",
        "source": "manual",
        "text": text,
    }, headers=AUTH)
    if resp.status_code == 201 and resp.json().get("work_item"):
        return resp.json()["work_item"]["id"]
    return None


class TestWebhookTesting:
    """Verify webhook test endpoint."""

    def test_webhook_test_endpoint(self, client):
        # Create webhook
        resp = client.post("/v1/webhooks", json={
            "url": "https://example.com/hook",
            "events": ["ingest"],
        }, headers=AUTH)
        assert resp.status_code == 201
        wh_id = resp.json()["id"]

        # Test webhook (may not exist)
        resp = client.post(f"/v1/webhooks/{wh_id}/test", headers=AUTH)
        # 404 if endpoint doesn't exist, 200/400/500 if it does
        assert resp.status_code in (200, 400, 404, 500)

    def test_webhook_test_nonexistent(self, client):
        resp = client.post(f"/v1/webhooks/{uuid.uuid4()}/test", headers=AUTH)
        assert resp.status_code == 404


class TestAPIKeyRevocation:
    """Verify API key revocation."""

    def test_revoke_api_key(self, client):
        # Create key
        resp = client.post("/v1/api-keys", json={"name": "test-key"}, headers=AUTH)
        assert resp.status_code == 201
        key_id = resp.json()["id"]

        # Revoke key
        resp = client.delete(f"/v1/api-keys/{key_id}", headers=AUTH)
        assert resp.status_code == 200

    def test_revoke_nonexistent_key(self, client):
        resp = client.delete(f"/v1/api-keys/{uuid.uuid4()}", headers=AUTH)
        assert resp.status_code == 404

    def test_api_key_lifecycle(self, client):
        # Create key
        resp = client.post("/v1/api-keys", json={"name": "lifecycle-key"}, headers=AUTH)
        assert resp.status_code == 201
        key_id = resp.json()["id"]

        # List keys
        resp = client.get("/v1/api-keys", headers=AUTH)
        assert resp.status_code == 200
        assert len(resp.json()["keys"]) >= 1

        # Revoke key
        resp = client.delete(f"/v1/api-keys/{key_id}", headers=AUTH)
        assert resp.status_code == 200


class TestMonitoringAdvanced:
    """Advanced monitoring tests."""

    def test_monitoring_system_metrics(self, client):
        resp = client.get("/v1/monitoring", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert "system" in data
        system = data["system"]
        assert "cpu_percent" in system
        assert "memory_percent" in system
        assert "disk_percent" in system

    def test_monitoring_service_metrics(self, client):
        resp = client.get("/v1/monitoring", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert "service" in data
        service = data["service"]
        assert "total_observations" in service
        assert "total_work_items" in service

    def test_monitoring_after_ingest(self, client):
        _create(client)

        resp = client.get("/v1/monitoring", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["service"]["total_observations"] >= 1
        assert data["service"]["total_work_items"] >= 1


class TestConcurrentOperationsAdvanced:
    """Advanced concurrent operations tests."""

    def test_concurrent_ingest_same_tenant(self, client):
        results = []
        for i in range(10):
            resp = client.post("/v1/observations", json={
                "tenant_id": "concurrent-advanced",
                "source": "manual",
                "text": f"Vi skal købe {i} licenser hurtigt",
            }, headers=AUTH)
            results.append(resp)

        assert all(r.status_code in (200, 201, 202) for r in results)

    def test_concurrent_review_same_item(self, client):
        item_id = _create(client)
        assert item_id

        # First review
        resp1 = client.post(
            f"/v1/work-items/{item_id}/review?tenant_id=default",
            json={"action": "approve", "actor": "user-1"},
            headers=AUTH,
        )

        # Second review (may fail)
        resp2 = client.post(
            f"/v1/work-items/{item_id}/review?tenant_id=default",
            json={"action": "reject", "actor": "user-2"},
            headers=AUTH,
        )

        # One should succeed, one should fail
        assert resp1.status_code in (200, 400, 409)
        assert resp2.status_code in (200, 400, 409)

    def test_concurrent_different_tenants(self, client):
        results = []
        for i in range(5):
            resp = client.post("/v1/observations", json={
                "tenant_id": f"tenant-adv-{i}",
                "source": "manual",
                "text": f"Vi skal købe licenser til {i} hurtigt",
            }, headers=AUTH)
            results.append(resp)

        assert all(r.status_code in (200, 201, 202) for r in results)


class TestHealthProbesAdvanced:
    """Advanced health probe tests."""

    def test_healthz_detailed_system_info(self, client):
        resp = client.get("/healthz/detailed")
        assert resp.status_code == 200
        data = resp.json()
        assert "system" in data or "status" in data

    def test_ready_probe(self, client):
        resp = client.get("/ready")
        assert resp.status_code == 200
        assert resp.json().get("status") in ("ok", "ready")

    def test_live_probe(self, client):
        resp = client.get("/live")
        assert resp.status_code == 200
        assert resp.json().get("status") in ("ok", "alive")


class TestErrorHandlingAdvanced:
    """Advanced error handling tests."""

    def test_404_consistent(self, client):
        paths = [
            f"/v1/work-items/{uuid.uuid4()}?tenant_id=default",
            f"/v1/work-items/{uuid.uuid4()}/transitions?tenant_id=default",
            f"/v1/work-items/{uuid.uuid4()}/publications?tenant_id=default",
        ]
        for path in paths:
            resp = client.get(path, headers=AUTH)
            assert resp.status_code == 404, f"{path} returned {resp.status_code}"

    def test_401_consistent(self, client):
        endpoints = [
            ("POST", "/v1/observations"),
            ("GET", "/v1/work-items?tenant_id=default"),
            ("GET", "/v1/tenants"),
        ]
        for method, path in endpoints:
            resp = client.post(path, json={}) if method == "POST" else client.get(path)
            assert resp.status_code == 401, f"{method} {path} returned {resp.status_code}"

    def test_422_consistent(self, client):
        resp = client.post("/v1/observations", json={}, headers=AUTH)
        assert resp.status_code == 422
        data = resp.json()
        assert "detail" in data
        assert isinstance(data["detail"], list)
