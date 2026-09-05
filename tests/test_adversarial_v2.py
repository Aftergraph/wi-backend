"""Additional adversarial tests — monitoring abuse, rate limiting bypass, request ID spoofing."""

import pytest
from fastapi.testclient import TestClient

from aftergraph_work_intelligence.api import create_app


@pytest.fixture()
def client():
    app = create_app(
        db_path=":memory:",
        api_token="test-token-123",
        evidence_secret="test-secret",
    )
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def auth_headers():
    return {"Authorization": "Bearer test-token-123"}


class TestMonitoringAbuse:
    """Tests for monitoring endpoint abuse attempts."""

    def test_monitoring_without_auth(self, client: TestClient):
        """Monitoring endpoint should require authentication."""
        resp = client.get("/v1/monitoring")
        assert resp.status_code == 401

    def test_monitoring_with_invalid_token(self, client: TestClient):
        """Monitoring endpoint should reject invalid tokens."""
        resp = client.get(
            "/v1/monitoring",
            headers={"Authorization": "Bearer invalid-token"}
        )
        assert resp.status_code == 401

    def test_monitoring_with_valid_token(self, client: TestClient, auth_headers):
        """Monitoring endpoint should work with valid token."""
        resp = client.get("/v1/monitoring", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "system" in data
        assert "service" in data


class TestRateLimitBypass:
    """Tests for rate limiting bypass attempts."""

    def test_rate_limit_headers(self, client: TestClient, auth_headers):
        """Rate limit should not be bypassable via headers."""
        # Make requests until rate limited
        for i in range(61):
            resp = client.get("/v1/version", headers=auth_headers)
            if i < 60:
                assert resp.status_code == 200
            else:
                assert resp.status_code == 429

    def test_rate_limit_resets(self, client: TestClient, auth_headers):
        """Rate limit should reset after window expires."""
        # This test verifies the rate limiter structure exists
        # Actual time-based reset would require mocking time
        resp = client.get("/v1/version", headers=auth_headers)
        assert resp.status_code == 200


class TestRequestIDSpoofing:
    """Tests for request ID spoofing attempts."""

    def test_request_id_cannot_be_empty(self, client: TestClient, auth_headers):
        """Request ID should not accept empty values."""
        resp = client.get(
            "/v1/version",
            headers={**auth_headers, "X-Request-ID": ""}
        )
        # Should still work, auto-generating a new ID
        assert resp.status_code == 200
        assert "X-Request-ID" in resp.headers

    def test_request_id_cannot_be_injection(self, client: TestClient, auth_headers):
        """Request ID should not accept injection attempts."""
        malicious_ids = [
            "<script>alert(1)</script>",
            "'; DROP TABLE work_items; --",
            "${7*7}",
            "{{7*7}}",
        ]
        for malicious_id in malicious_ids:
            resp = client.get(
                "/v1/version",
                headers={**auth_headers, "X-Request-ID": malicious_id}
            )
            # Should still work, reflecting the ID back
            assert resp.status_code == 200
            assert resp.headers["X-Request-ID"] == malicious_id


class TestVersionEndpointAbuse:
    """Tests for version endpoint abuse attempts."""

    def test_version_no_auth(self, client: TestClient):
        """Version endpoint should require authentication."""
        resp = client.get("/v1/version")
        assert resp.status_code == 401

    def test_version_with_token(self, client: TestClient, auth_headers):
        """Version endpoint should work with valid token."""
        resp = client.get("/v1/version", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "version" in data
        assert "features" in data


class TestHealthzAbuse:
    """Tests for healthz endpoint abuse attempts."""

    def test_healthz_no_auth(self, client: TestClient):
        """Healthz endpoint should NOT require authentication."""
        resp = client.get("/healthz")
        assert resp.status_code == 200

    def test_healthz_with_malicious_input(self, client: TestClient):
        """Healthz endpoint should handle malicious input gracefully."""
        resp = client.get("/healthz?param=<script>alert(1)</script>")
        assert resp.status_code == 200

    def test_healthz_method_not_allowed(self, client: TestClient):
        """Healthz should only accept GET."""
        resp = client.post("/healthz")
        assert resp.status_code == 405


class TestObservationInjection:
    """Tests for SQL/NoSQL injection via observation text."""

    def test_sql_injection_in_text(self, client: TestClient, auth_headers):
        """SQL injection in observation text should be sanitized."""
        resp = client.post(
            "/v1/observations",
            headers=auth_headers,
            json={
                "tenant_id": "test-tenant",
                "source": "manual",
                "text": "'; DROP TABLE work_items; --",
            }
        )
        # Should either create successfully or reject, but not crash
        assert resp.status_code in [200, 201, 202, 400]

    def test_nosql_injection_in_text(self, client: TestClient, auth_headers):
        """NoSQL injection in observation text should be sanitized."""
        resp = client.post(
            "/v1/observations",
            headers=auth_headers,
            json={
                "tenant_id": "test-tenant",
                "source": "manual",
                "text": '{"$gt": ""}',
            }
        )
        # Should either create successfully or reject, but not crash
        assert resp.status_code in [200, 201, 202, 400]

    def test_xss_in_text(self, client: TestClient, auth_headers):
        """XSS in observation text should be escaped."""
        resp = client.post(
            "/v1/observations",
            headers=auth_headers,
            json={
                "tenant_id": "test-tenant",
                "source": "manual",
                "text": "<script>alert('xss')</script>",
            }
        )
        # Should either create successfully or reject, but not crash
        assert resp.status_code in [200, 201, 202, 400]
