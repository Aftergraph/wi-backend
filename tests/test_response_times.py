"""Tests for response time statistics."""

from __future__ import annotations

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


class TestResponseTimeStats:
    """Test response time tracking and stats."""

    def test_response_times_endpoint(self, client):
        # Make some requests to generate data
        for _ in range(5):
            client.get("/v1/work-items?tenant_id=default", headers=AUTH)

        resp = client.get("/v1/response-times", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()

        # Should have stats for work-items endpoint
        assert "/v1/work-items" in data
        stats = data["/v1/work-items"]
        assert stats["count"] >= 5
        assert stats["avg_ms"] >= 0
        assert stats["p50_ms"] >= 0
        assert stats["p95_ms"] >= 0
        assert stats["max_ms"] >= 0

    def test_multiple_endpoints_tracked(self, client):
        client.get("/healthz")
        client.get("/v1/work-items?tenant_id=default", headers=AUTH)

        resp = client.get("/v1/response-times", headers=AUTH)
        data = resp.json()
        assert "/healthz" in data
        assert "/v1/work-items" in data

    def test_percentiles_ordered(self, client):
        # Generate enough data
        for _ in range(20):
            client.get("/healthz")

        resp = client.get("/v1/response-times", headers=AUTH)
        stats = resp.json()["/healthz"]
        # p50 <= p95 <= max
        assert stats["p50_ms"] <= stats["p95_ms"] <= stats["max_ms"]
