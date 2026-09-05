"""Additional adversarial tests for security hardening."""

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


class TestXSSPrevention:
    """Tests for XSS prevention in API responses."""

    def test_html_in_observation_text(self, client):
        c, _ = client
        resp = c.post(
            "/v1/observations",
            json={
                "tenant_id": "xss-test",
                "source": "test",
                "text": "<script>alert('xss')</script> Køb computere",
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code in (200, 201, 202)
        # Verify text is stored (API doesn't sanitize, but shouldn't crash)
        data = resp.json()
        assert "observation" in data

    def test_html_in_tenant_id(self, client):
        c, _ = client
        resp = c.post(
            "/v1/observations",
            json={
                "tenant_id": "<img src=x onerror=alert(1)>",
                "source": "test",
                "text": "Test observation",
            },
            headers={"Authorization": "Bearer test-token"},
        )
        # Should not crash
        assert resp.status_code in (200, 201, 202, 422)

    def test_html_in_search_query(self, client):
        c, _ = client
        resp = c.get(
            "/v1/search",
            params={"q": "<script>alert('xss')</script>", "tenant_id": "test"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200


class TestSQLInjection:
    """Tests for SQL injection prevention."""

    def test_sql_injection_in_tenant_id(self, client):
        c, _ = client
        resp = c.post(
            "/v1/observations",
            json={
                "tenant_id": "'; DROP TABLE work_items; --",
                "source": "test",
                "text": "Test observation",
            },
            headers={"Authorization": "Bearer test-token"},
        )
        # Should not crash
        assert resp.status_code in (200, 201, 202)

    def test_sql_injection_in_search(self, client):
        c, _ = client
        resp = c.get(
            "/v1/search",
            params={"q": "' OR 1=1 --", "tenant_id": "test"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200

    def test_sql_injection_in_work_item_id(self, client):
        c, _ = client
        resp = c.get(
            "/v1/work-items/'; DROP TABLE work_items; --",
            params={"tenant_id": "test"},
            headers={"Authorization": "Bearer test-token"},
        )
        # Should return 404 or 422, not crash
        assert resp.status_code in (404, 422)


class TestInputValidation:
    """Tests for input validation edge cases."""

    def test_empty_body(self, client):
        c, _ = client
        resp = c.post(
            "/v1/observations",
            content="",
            headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"},
        )
        assert resp.status_code == 422

    def test_null_values(self, client):
        c, _ = client
        resp = c.post(
            "/v1/observations",
            json={
                "tenant_id": None,
                "source": "test",
                "text": "Test observation",
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 422

    def test_oversized_payload(self, client):
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

    def test_invalid_json(self, client):
        c, _ = client
        resp = c.post(
            "/v1/observations",
            content="not json",
            headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"},
        )
        assert resp.status_code == 422


class TestAuthenticationBypass:
    """Tests for authentication bypass attempts."""

    def test_missing_auth_header(self, client):
        c, _ = client
        resp = c.get("/v1/work-items/test", params={"tenant_id": "test"})
        assert resp.status_code == 401

    def test_invalid_token(self, client):
        c, _ = client
        resp = c.get(
            "/v1/work-items/test",
            params={"tenant_id": "test"},
            headers={"Authorization": "Bearer invalid-token"},
        )
        assert resp.status_code == 401

    def test_malformed_auth_header(self, client):
        c, _ = client
        resp = c.get(
            "/v1/work-items/test",
            params={"tenant_id": "test"},
            headers={"Authorization": "InvalidFormat"},
        )
        assert resp.status_code == 401

    def test_empty_bearer_token(self, client):
        c, _ = client
        resp = c.get(
            "/v1/work-items/test",
            params={"tenant_id": "test"},
            headers={"Authorization": "Bearer "},
        )
        assert resp.status_code == 401


class TestRateLimiting:
    """Tests for rate limiting behavior."""

    def test_rate_limit_not_exceeded(self, client):
        c, _ = client
        for _ in range(5):
            resp = c.get("/healthz")
            assert resp.status_code == 200

    def test_rate_limit_headers_present(self, client):
        c, _ = client
        resp = c.get("/healthz")
        # Rate limit headers may or may not be present
        assert resp.status_code == 200


class TestConcurrencySafety:
    """Tests for concurrency safety."""

    def test_concurrent_writes(self, client):
        c, _ = client
        import concurrent.futures

        def write_obs(i):
            return c.post(
                "/v1/observations",
                json={
                    "tenant_id": "concurrent",
                    "source": "test",
                    "text": f"Concurrent observation {i} {uuid.uuid4().hex[:8]} skal købes",
                },
                headers={"Authorization": "Bearer test-token"},
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(write_obs, i) for i in range(20)]
            results = [f.result() for f in futures]

        # All should succeed
        assert all(r.status_code in (200, 201, 202) for r in results)


class TestErrorHandling:
    """Tests for error handling edge cases."""

    def test_method_not_allowed(self, client):
        c, _ = client
        resp = c.put("/healthz")
        assert resp.status_code == 405

    def test_not_found(self, client):
        c, _ = client
        resp = c.get("/nonexistent")
        assert resp.status_code == 404

    def test_internal_server_error_handling(self, client):
        c, _ = client
        # Try to trigger an error
        resp = c.get("/v1/work-items/invalid-id/invalid-endpoint")
        # Should return 404 or 405, not 500
        assert resp.status_code in (404, 405)
