"""Tests for per-key rate limiting and webhook stats."""

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


class TestRateLimiter:
    """Test the RateLimiter class directly."""

    def test_default_limit(self):
        rl = RateLimiter(requests_per_minute=30)
        assert rl.default_limit == 30
        assert rl.get_limit("any-key") == 30

    def test_per_key_limit(self):
        rl = RateLimiter(requests_per_minute=60)
        rl.set_key_limit("vip", 500)
        assert rl.get_limit("vip") == 500
        assert rl.get_limit("normal") == 60

    def test_is_allowed_under_limit(self):
        rl = RateLimiter(requests_per_minute=5)
        assert rl.is_allowed("client-a") is True
        assert rl.is_allowed("client-a") is True

    def test_is_allowed_at_limit(self):
        rl = RateLimiter(requests_per_minute=3)
        assert rl.is_allowed("client-a") is True
        assert rl.is_allowed("client-a") is True
        assert rl.is_allowed("client-a") is True
        assert rl.is_allowed("client-a") is False

    def test_separate_clients(self):
        rl = RateLimiter(requests_per_minute=2)
        assert rl.is_allowed("a") is True
        assert rl.is_allowed("b") is True
        assert rl.is_allowed("a") is True
        assert rl.is_allowed("b") is True
        assert rl.is_allowed("a") is False  # a hit limit
        assert rl.is_allowed("b") is False  # b hit limit

    def test_per_key_limit_enforcement(self):
        rl = RateLimiter(requests_per_minute=60)
        rl.set_key_limit("low", 2)
        assert rl.is_allowed("low") is True
        assert rl.is_allowed("low") is True
        assert rl.is_allowed("low") is False

    def test_usage_stats(self):
        rl = RateLimiter(requests_per_minute=10)
        rl.is_allowed("c")
        rl.is_allowed("c")
        stats = rl.get_usage("c")
        assert stats["used"] == 2
        assert stats["limit"] == 10
        assert stats["remaining"] == 8

    def test_usage_after_limit(self):
        rl = RateLimiter(requests_per_minute=2)
        rl.is_allowed("x")
        rl.is_allowed("x")
        rl.is_allowed("x")  # rejected
        stats = rl.get_usage("x")
        assert stats["used"] == 2
        assert stats["remaining"] == 0


class TestRateLimitAPI:
    """Test rate limit management API endpoints."""

    def test_get_rate_limit_status(self, client):
        resp = client.get("/v1/rate-limit", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert "default_limit" in data
        assert data["default_limit"] > 0

    def test_set_custom_rate_limit(self, client):
        resp = client.post("/v1/rate-limit", json={
            "key": "test-key-123",
            "limit": 100,
        }, headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["limit"] == 100

    def test_check_specific_key_usage(self, client):
        client.post("/v1/rate-limit", json={
            "key": "my-api-key",
            "limit": 50,
        }, headers=AUTH)

        resp = client.get("/v1/rate-limit?client_id=my-api-key", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["limit"] == 50
        assert "used" in data
        assert "remaining" in data


class TestWebhookStats:
    """Test webhook delivery statistics."""

    def test_webhook_stats_endpoint(self, client):
        resp = client.get("/v1/webhooks/stats", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert "delivery" in data
        assert "registered" in data
        assert "active" in data

    def test_webhook_stats_after_registration(self, client):
        client.post("/v1/webhooks", json={
            "url": "https://example.com/hook",
            "events": ["observation.ingested"],
        }, headers=AUTH)

        resp = client.get("/v1/webhooks/stats", headers=AUTH)
        data = resp.json()
        assert data["registered"] >= 1
        assert data["active"] >= 1

    def test_webhook_stats_delivery_count(self, client):
        resp = client.get("/v1/webhooks/stats", headers=AUTH)
        data = resp.json()
        assert "delivered" in data["delivery"]
        assert "failed" in data["delivery"]
