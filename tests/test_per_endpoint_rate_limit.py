"""Tests for per-endpoint rate limiting."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aftergraph_work_intelligence.api import RateLimiter, create_app


@pytest.fixture()
def client(tmp_path):
    db = tmp_path / "test.db"
    app = create_app(db_path=db, api_token="test-token")
    with TestClient(app) as c:
        yield c


AUTH = {"Authorization": "Bearer test-token"}


class TestPerEndpointRateLimiter:
    """Test RateLimiter with per-endpoint limits."""

    def test_global_limit(self):
        rl = RateLimiter(requests_per_minute=3)
        assert rl.is_allowed("client-a") is True
        assert rl.is_allowed("client-a") is True
        assert rl.is_allowed("client-a") is True
        assert rl.is_allowed("client-a") is False

    def test_per_endpoint_limit(self):
        rl = RateLimiter(requests_per_minute=100)
        rl.set_endpoint_limit("/v1/observations", 2)
        
        # Observations limited to 2
        assert rl.is_allowed("c", "/v1/observations") is True
        assert rl.is_allowed("c", "/v1/observations") is True
        assert rl.is_allowed("c", "/v1/observations") is False

    def test_different_endpoints_independent(self):
        rl = RateLimiter(requests_per_minute=100)
        rl.set_endpoint_limit("/v1/observations", 1)
        rl.set_endpoint_limit("/v1/work-items", 1)
        
        assert rl.is_allowed("c", "/v1/observations") is True
        assert rl.is_allowed("c", "/v1/observations") is False  # observations at limit
        assert rl.is_allowed("c", "/v1/work-items") is True  # work-items still ok

    def test_endpoint_usage_stats(self):
        rl = RateLimiter(requests_per_minute=100)
        rl.set_endpoint_limit("/v1/observations", 5)
        
        rl.is_allowed("c", "/v1/observations")
        rl.is_allowed("c", "/v1/observations")
        
        stats = rl.get_endpoint_usage("c", "/v1/observations")
        assert stats["used"] == 2
        assert stats["limit"] == 5
        assert stats["remaining"] == 3

    def test_endpoint_without_limit_uses_default(self):
        rl = RateLimiter(requests_per_minute=3)
        
        # No endpoint limit set, uses global limit
        for _ in range(3):
            assert rl.is_allowed("c", "/v1/unknown") is True
        assert rl.is_allowed("c", "/v1/unknown") is False


class TestPerEndpointAPI:
    """Test per-endpoint rate limiting via API."""

    def test_set_endpoint_limit(self, client):
        resp = client.post("/v1/rate-limit", json={
            "key": "/v1/observations",
            "limit": 5,
        }, headers=AUTH)
        assert resp.status_code == 200

    def test_rate_limit_status(self, client):
        resp = client.get("/v1/rate-limit", headers=AUTH)
        assert resp.status_code == 200
        assert "default_limit" in resp.json()
