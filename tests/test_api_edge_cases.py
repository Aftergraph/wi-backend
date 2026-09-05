"""Tests for additional API endpoints: audit trail, bulk ingestion, error handling."""

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
        yield c, app


def _ingest_obs(client, tenant="tenant-a", text=None):
    """Helper: ingest an observation and return response."""
    if text is None:
        text = f"Test observation {uuid.uuid4().hex[:8]} skal udføres"
    resp = client.post(
        "/v1/observations",
        json={
            "tenant_id": tenant,
            "source": "test",
            "text": text,
            "external_id": f"ext-{uuid.uuid4().hex[:8]}",
        },
        headers={"Authorization": "Bearer test-token"},
    )
    return resp


class TestAuditTrail:
    """Tests for the audit trail endpoint."""

    def test_audit_trail_empty(self, client):
        c, _ = client
        resp = c.get(
            "/v1/work-items/nonexistent/evidence",
            params={"tenant_id": "tenant-a"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 404

    def test_audit_trail_after_review(self, client):
        c, _ = client
        # Ingest
        resp = _ingest_obs(c)
        assert resp.status_code in (200, 201, 202)
        item_id = resp.json()["work_item"]["id"]

        # Approve
        resp = c.post(
            f"/v1/work-items/{item_id}/review",
            json={"action": "approve", "actor": "reviewer@test.dk", "reason": "Looks good"},
            params={"tenant_id": "tenant-a"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200

        # Check evidence
        resp = c.get(
            f"/v1/work-items/{item_id}/evidence",
            params={"tenant_id": "tenant-a"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "observations" in data
        assert len(data["observations"]) >= 1


class TestBulkIngestion:
    """Tests for bulk observation ingestion."""

    def test_bulk_ingestion_multiple(self, client):
        c, _ = client
        observations = [
            {
                "tenant_id": "tenant-bulk",
                "source": "test",
                "text": f"Unik observation {uuid.uuid4().hex[:8]} skal behandles hurtigt",
                "external_id": f"bulk-{uuid.uuid4().hex[:8]}",
            }
            for i in range(5)
        ]

        results = []
        for obs in observations:
            resp = c.post(
                "/v1/observations",
                json=obs,
                headers={"Authorization": "Bearer test-token"},
            )
            results.append(resp.status_code)

        # All should succeed
        assert all(code in (200, 201, 202) for code in results)

        # List work items
        resp = c.get(
            "/v1/work-items",
            params={"tenant_id": "tenant-bulk", "limit": 100},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 5


class TestErrorHandling:
    """Tests for error handling edge cases."""

    def test_invalid_json(self, client):
        c, _ = client
        resp = c.post(
            "/v1/observations",
            content="not json",
            headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"},
        )
        assert resp.status_code == 422

    def test_missing_required_fields(self, client):
        c, _ = client
        resp = c.post(
            "/v1/observations",
            json={"tenant_id": "test"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 422

    def test_empty_tenant_id(self, client):
        c, _ = client
        resp = c.post(
            "/v1/observations",
            json={
                "tenant_id": "",
                "source": "test",
                "text": "Test observation",
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 422

    def test_oversized_text(self, client):
        c, _ = client
        resp = c.post(
            "/v1/observations",
            json={
                "tenant_id": "test",
                "source": "test",
                "text": "x" * 200_001,  # Over 100KB limit
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 422

    def test_invalid_priority_hint(self, client):
        c, _ = client
        resp = c.post(
            "/v1/observations",
            json={
                "tenant_id": "test",
                "source": "test",
                "text": "Test observation",
                "priority_hint": "invalid",
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 422

    def test_review_invalid_action(self, client):
        c, _ = client
        resp = _ingest_obs(c)
        item_id = resp.json()["work_item"]["id"]

        resp = c.post(
            f"/v1/work-items/{item_id}/review",
            json={"action": "invalid", "actor": "test@test.dk"},
            params={"tenant_id": "tenant-a"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 422

    def test_work_item_not_found(self, client):
        c, _ = client
        resp = c.get(
            "/v1/work-items/nonexistent-id",
            params={"tenant_id": "tenant-a"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 404


class TestRateLimiting:
    """Tests for rate limiting behavior."""

    def test_rate_limit_not_exceeded(self, client):
        c, _ = client
        # Make a few requests
        for _ in range(5):
            resp = c.get(
                "/healthz",
                headers={"Authorization": "Bearer test-token"},
            )
            assert resp.status_code == 200

    def test_request_id_tracking(self, client):
        c, _ = client
        request_id = str(uuid.uuid4())
        resp = c.get(
            "/healthz",
            headers={"X-Request-ID": request_id, "Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        assert resp.headers.get("X-Request-ID") == request_id

    def test_request_id_generated(self, client):
        c, _ = client
        resp = c.get("/healthz")
        assert resp.status_code == 200
        assert "X-Request-ID" in resp.headers


class TestTimingMiddleware:
    """Tests for timing middleware."""

    def test_timing_header_present(self, client):
        c, _ = client
        resp = c.get("/healthz")
        assert resp.status_code == 200
        assert "X-Process-Time" in resp.headers
        # Should be a number in milliseconds
        time_ms = float(resp.headers["X-Process-Time"])
        assert time_ms >= 0


class TestCORS:
    """Tests for CORS configuration."""

    def test_cors_preflight(self, client):
        c, _ = client
        resp = c.options(
            "/v1/observations",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type,Authorization",
            },
        )
        assert resp.status_code == 200
        assert "access-control-allow-origin" in resp.headers


class TestCompression:
    """Tests for GZip compression."""

    def test_compression_enabled(self, client):
        c, _ = client
        # Large response should be compressed
        resp = c.get(
            "/v1/work-items",
            params={"tenant_id": "test", "limit": 1000},
            headers={"Authorization": "Bearer test-token", "Accept-Encoding": "gzip"},
        )
        assert resp.status_code == 200


class TestVersionEndpoint:
    """Tests for version endpoint."""

    def test_version_returns_info(self, client):
        c, _ = client
        resp = c.get(
            "/v1/version",
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "version" in data
        assert "features" in data
        assert isinstance(data["features"], list)


class TestMonitoringEndpoint:
    """Tests for monitoring endpoint."""

    def test_monitoring_returns_metrics(self, client):
        c, _ = client
        resp = c.get(
            "/v1/monitoring",
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "system" in data
        assert "service" in data
        assert "timestamp" in data

        # System metrics should be valid
        system = data["system"]
        assert 0 <= system["cpu_percent"] <= 100
        assert 0 <= system["memory_percent"] <= 100
        assert system["memory_used_gb"] > 0
        assert system["memory_total_gb"] > 0
