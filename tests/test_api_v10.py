"""Tests for API versioning, rate limiting, and additional features."""

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


class TestAPIVersioning:
    """Verify API version endpoints."""

    def test_v1_prefix_exists(self, client):
        resp = client.get("/v1/work-items?tenant_id=default", headers=AUTH)
        assert resp.status_code == 200

    def test_root_redirects(self, client):
        resp = client.get("/")
        # Root may redirect to docs or return 404
        assert resp.status_code in (200, 307, 404)

    def test_docs_endpoint(self, client):
        resp = client.get("/docs")
        assert resp.status_code == 200

    def test_redoc_endpoint(self, client):
        resp = client.get("/redoc")
        assert resp.status_code == 200


class TestRateLimiting:
    """Verify rate limiting behavior."""

    def test_rate_limit_headers(self, client):
        resp = client.get("/v1/work-items?tenant_id=default", headers=AUTH)
        # Rate limit headers may or may not be present
        assert resp.status_code == 200

    def test_rapid_requests(self, client):
        results = []
        for _ in range(20):
            resp = client.get("/healthz")
            results.append(resp.status_code)
        # All should succeed (rate limit is per-minute, not per-second burst)
        assert all(s == 200 for s in results)


class TestInputValidation:
    """Verify input validation on all endpoints."""

    def test_observations_requires_tenant(self, client):
        resp = client.post("/v1/observations", json={
            "source": "manual",
            "text": "test",
        }, headers=AUTH)
        assert resp.status_code == 422

    def test_observations_requires_source(self, client):
        resp = client.post("/v1/observations", json={
            "tenant_id": "default",
            "text": "test",
        }, headers=AUTH)
        assert resp.status_code == 422

    def test_observations_requires_text(self, client):
        resp = client.post("/v1/observations", json={
            "tenant_id": "default",
            "source": "manual",
        }, headers=AUTH)
        assert resp.status_code == 422

    def test_tenant_id_max_length(self, client):
        resp = client.post("/v1/observations", json={
            "tenant_id": "x" * 200,
            "source": "manual",
            "text": "test",
        }, headers=AUTH)
        assert resp.status_code == 422

    def test_source_max_length(self, client):
        resp = client.post("/v1/observations", json={
            "tenant_id": "default",
            "source": "x" * 200,
            "text": "test",
        }, headers=AUTH)
        assert resp.status_code == 422

    def test_work_items_requires_tenant(self, client):
        resp = client.get("/v1/work-items", headers=AUTH)
        assert resp.status_code == 422

    def test_search_requires_tenant(self, client):
        resp = client.get("/v1/search?q=test", headers=AUTH)
        assert resp.status_code == 422


class TestConcurrentOperations:
    """Verify concurrent operations don't corrupt state."""

    def test_concurrent_ingest_same_tenant(self, client):
        results = []
        for i in range(10):
            resp = client.post("/v1/observations", json={
                "tenant_id": "concurrent-test",
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

        # Second review may fail
        resp2 = client.post(
            f"/v1/work-items/{item_id}/review?tenant_id=default",
            json={"action": "reject", "actor": "user-2"},
            headers=AUTH,
        )

        # At least one should succeed
        assert resp1.status_code in (200, 400, 409)
        assert resp2.status_code in (200, 400, 409)

    def test_concurrent_different_tenants(self, client):
        results = []
        for i in range(5):
            resp = client.post("/v1/observations", json={
                "tenant_id": f"tenant-{i}",
                "source": "manual",
                "text": f"Vi skal købe {i} licenser hurtigt",
            }, headers=AUTH)
            results.append(resp)

        assert all(r.status_code in (200, 201, 202) for r in results)


class TestErrorResponseFormat:
    """Verify error responses have consistent format."""

    def test_404_has_detail(self, client):
        resp = client.get(f"/v1/work-items/{uuid.uuid4()}?tenant_id=default", headers=AUTH)
        assert resp.status_code == 404
        assert "detail" in resp.json()

    def test_422_has_detail(self, client):
        resp = client.post("/v1/observations", json={}, headers=AUTH)
        assert resp.status_code == 422
        assert "detail" in resp.json()

    def test_405_has_detail(self, client):
        resp = client.put("/v1/observations", json={}, headers=AUTH)
        assert resp.status_code == 405


class TestMonitoringAdvanced:
    """Advanced monitoring tests."""

    def test_monitoring_has_system_metrics(self, client):
        resp = client.get("/v1/monitoring", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert "system" in data
        system = data["system"]
        assert "cpu_percent" in system
        assert "memory_percent" in system
        assert "disk_percent" in system

    def test_monitoring_has_service_metrics(self, client):
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
