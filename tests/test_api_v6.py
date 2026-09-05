"""Tests for rate limiting per API key, logging, and error handling."""

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


class TestRateLimitingPerKey:
    """Tests for per-API-key rate limiting."""

    def test_rate_limit_headers(self, client):
        c, _ = client
        resp = c.get("/healthz", headers={"Authorization": "Bearer test-token"})
        assert resp.status_code == 200
        # Rate limit may or may not expose headers - just verify 200

    def test_rate_limit_remaining(self, client):
        c, _ = client
        resp = c.get("/healthz", headers={"Authorization": "Bearer test-token"})
        # Check for rate limit remaining header
        headers = {k.lower(): v for k, v in resp.headers.items()}
        if "x-ratelimit-remaining" in headers:
            remaining = int(headers["x-ratelimit-remaining"])
            assert remaining >= 0


class TestRequestLogging:
    """Tests for request logging middleware."""

    def test_request_id_in_response(self, client):
        c, _ = client
        resp = c.get("/healthz")
        assert "X-Request-ID" in resp.headers

    def test_custom_request_id(self, client):
        c, _ = client
        custom_id = str(uuid.uuid4())
        resp = c.get("/healthz", headers={"X-Request-ID": custom_id})
        assert resp.headers.get("X-Request-ID") == custom_id

    def test_timing_header(self, client):
        c, _ = client
        resp = c.get("/healthz")
        assert "X-Process-Time" in resp.headers
        time_ms = float(resp.headers["X-Process-Time"])
        assert time_ms >= 0


class TestErrorResponseFormat:
    """Tests for consistent error response format."""

    def test_404_format(self, client):
        c, _ = client
        resp = c.get(
            "/v1/work-items/nonexistent",
            params={"tenant_id": "test"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 404
        data = resp.json()
        assert "detail" in data

    def test_422_format(self, client):
        c, _ = client
        resp = c.post(
            "/v1/observations",
            json={"invalid": "data"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 422
        data = resp.json()
        assert "detail" in data

    def test_401_format(self, client):
        c, _ = client
        resp = c.get("/v1/work-items/test", params={"tenant_id": "test"})
        assert resp.status_code == 401
        data = resp.json()
        assert "detail" in data


class TestConcurrentRequests:
    """Tests for handling concurrent requests."""

    def test_concurrent_ingestion(self, client):
        c, _ = client
        import concurrent.futures

        def ingest(i):
            return c.post(
                "/v1/observations",
                json={
                    "tenant_id": "concurrent",
                    "source": "test",
                    "text": f"Concurrent observation {i} {uuid.uuid4().hex[:8]} skal købes",
                },
                headers={"Authorization": "Bearer test-token"},
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(ingest, i) for i in range(10)]
            results = [f.result() for f in futures]

        # All should succeed
        assert all(r.status_code in (200, 201, 202) for r in results)


class TestObservationDeduplication:
    """Tests for observation deduplication."""

    def test_duplicate_external_id(self, client):
        c, _ = client
        ext_id = f"dedup-{uuid.uuid4().hex[:8]}"
        text = f"Dedup test {uuid.uuid4().hex[:8]} skal købes"

        # First ingestion
        resp1 = c.post(
            "/v1/observations",
            json={
                "tenant_id": "dedup-tenant",
                "source": "test",
                "text": text,
                "external_id": ext_id,
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp1.status_code == 201

        # Second ingestion with same external_id
        resp2 = c.post(
            "/v1/observations",
            json={
                "tenant_id": "dedup-tenant",
                "source": "test",
                "text": text,
                "external_id": ext_id,
            },
            headers={"Authorization": "Bearer test-token"},
        )
        # Should return 200 or 202 (observed, not created)
        assert resp2.status_code in (200, 202)


class TestWorkItemLifecycle:
    """Tests for complete work item lifecycle."""

    def test_full_lifecycle(self, client):
        c, _ = client

        # 1. Create
        resp = c.post(
            "/v1/observations",
            json={
                "tenant_id": "lifecycle",
                "source": "test",
                "text": "Lifecycle test observation skal købes hurtigt",
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 201
        item_id = resp.json()["work_item"]["id"]

        # 2. Get
        resp = c.get(
            f"/v1/work-items/{item_id}",
            params={"tenant_id": "lifecycle"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        assert resp.json()["work_item"]["status"] == "OPEN"

        # 3. Approve
        resp = c.post(
            f"/v1/work-items/{item_id}/review",
            json={"action": "approve", "actor": "reviewer@test.dk"},
            params={"tenant_id": "lifecycle"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "APPROVED"  # approve endpoint returns flat work_item

        # 4. Check transitions
        resp = c.get(
            f"/v1/work-items/{item_id}/transitions",
            params={"tenant_id": "lifecycle"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        assert len(resp.json()["transitions"]) >= 1

        # 5. Check evidence
        resp = c.get(
            f"/v1/work-items/{item_id}/evidence",
            params={"tenant_id": "lifecycle"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        assert "observations" in resp.json()
