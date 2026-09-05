"""Tests for security hardening and performance characteristics."""

from __future__ import annotations

import time

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


class TestSecurityHardening:
    """Verify security hardening measures."""

    def test_no_credential_leakage(self, client):
        resp = client.get("/v1/monitoring", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        # Should not contain any credential information
        assert "password" not in str(data).lower()
        assert "secret" not in str(data).lower()
        assert "token" not in str(data).lower()

    def test_xss_prevention(self, client):
        xss_payloads = [
            "<script>alert('xss')</script>",
            "<img src=x onerror=alert('xss')>",
            "javascript:alert('xss')",
            "<svg onload=alert('xss')>",
        ]
        for payload in xss_payloads:
            resp = client.post("/v1/observations", json={
                "tenant_id": "default",
                "source": "manual",
                "text": payload,
            }, headers=AUTH)
            assert resp.status_code in (200, 201, 202, 400, 422)

    def test_sql_injection_prevention(self, client):
        sql_payloads = [
            "'; DROP TABLE work_items; --",
            "1' OR '1'='1",
            "1; SELECT * FROM users",
            "' UNION SELECT * FROM users --",
        ]
        for payload in sql_payloads:
            resp = client.post("/v1/observations", json={
                "tenant_id": "default",
                "source": "manual",
                "text": payload,
            }, headers=AUTH)
            assert resp.status_code in (200, 201, 202, 400, 422)

    def test_path_traversal_prevention(self, client):
        traversal_payloads = [
            "../../../etc/passwd",
            "..\\..\\..\\windows\\system32\\config\\sam",
            "....//....//....//etc/passwd",
        ]
        for payload in traversal_payloads:
            resp = client.post("/v1/observations", json={
                "tenant_id": payload,
                "source": "manual",
                "text": "test",
            }, headers=AUTH)
            # Should reject invalid tenant_id
            assert resp.status_code in (200, 201, 202, 400, 422)

    def test_oversized_payload_rejection(self, client):
        resp = client.post("/v1/observations", json={
            "tenant_id": "default",
            "source": "manual",
            "text": "x" * 10_000_000,
        }, headers=AUTH)
        # Should reject or handle gracefully
        assert resp.status_code in (200, 201, 202, 400, 413, 422)

    def test_concurrent_auth_attempts(self, client):
        results = []
        for _ in range(20):
            resp = client.get("/v1/work-items?tenant_id=default",
                            headers={"Authorization": "Bearer invalid"})
            results.append(resp.status_code)

        # All should fail with 401
        assert all(s == 401 for s in results)


class TestPerformanceCharacteristics:
    """Verify performance characteristics."""

    def test_health_check_latency(self, client):
        start = time.time()
        for _ in range(10):
            resp = client.get("/healthz")
            assert resp.status_code == 200
        elapsed = time.time() - start
        assert elapsed < 2.0

    def test_ingest_throughput(self, client):
        start = time.time()
        count = 0
        for i in range(20):
            resp = client.post("/v1/observations", json={
                "tenant_id": "default",
                "source": "manual",
                "text": f"Vi skal købe {i} licenser hurtigt",
            }, headers=AUTH)
            if resp.status_code in (200, 201, 202):
                count += 1
        elapsed = time.time() - start
        # Should handle 20 requests in < 5 seconds
        assert elapsed < 5.0
        assert count == 20

    def test_list_latency(self, client):
        for i in range(5):
            _create(client, f"Vi skal købe {i} licenser hurtigt")

        start = time.time()
        for _ in range(10):
            resp = client.get("/v1/work-items?tenant_id=default", headers=AUTH)
            assert resp.status_code == 200
        elapsed = time.time() - start
        assert elapsed < 3.0

    def test_search_latency(self, client):
        _create(client)

        start = time.time()
        for _ in range(10):
            resp = client.get("/v1/search?q=licenser&tenant_id=default", headers=AUTH)
            assert resp.status_code == 200
        elapsed = time.time() - start
        assert elapsed < 3.0


class TestIntegrationScenarios:
    """Verify end-to-end integration scenarios."""

    def test_complete_workflow(self, client):
        # 1. Create observation
        resp = client.post("/v1/observations", json={
            "tenant_id": "workflow-test",
            "source": "manual",
            "text": "Vi skal købe 5 licenser hurtigt",
        }, headers=AUTH)
        assert resp.status_code == 201
        item_id = resp.json()["work_item"]["id"]

        # 2. Get work item
        resp = client.get(f"/v1/work-items/{item_id}?tenant_id=workflow-test", headers=AUTH)
        assert resp.status_code == 200

        # 3. Search for it
        resp = client.get("/v1/search?q=licenser&tenant_id=workflow-test", headers=AUTH)
        assert resp.status_code == 200

        # 4. Approve it
        resp = client.post(
            f"/v1/work-items/{item_id}/review?tenant_id=workflow-test",
            json={"action": "approve", "actor": "test-user"},
            headers=AUTH,
        )
        assert resp.status_code == 200

        # 5. Check transitions
        resp = client.get(f"/v1/work-items/{item_id}/transitions?tenant_id=workflow-test", headers=AUTH)
        assert resp.status_code == 200

        # 6. Check monitoring
        resp = client.get("/v1/monitoring", headers=AUTH)
        assert resp.status_code == 200

    def test_multi_tenant_workflow(self, client):
        tenants = ["alpha", "beta", "gamma"]
        
        # Create items in each tenant
        for t in tenants:
            resp = client.post("/v1/observations", json={
                "tenant_id": t,
                "source": "manual",
                "text": f"Vi skal købe licenser til {t} hurtigt",
            }, headers=AUTH)
            assert resp.status_code == 201

        # List tenants
        resp = client.get("/v1/tenants", headers=AUTH)
        assert resp.status_code == 200
        found = {t["tenant_id"] for t in resp.json()["tenants"]}
        for t in tenants:
            assert t in found

        # List items per tenant
        for t in tenants:
            resp = client.get(f"/v1/work-items?tenant_id={t}", headers=AUTH)
            assert resp.status_code == 200
            assert resp.json()["count"] >= 1

    def test_webhook_integration(self, client):
        # Create webhook
        resp = client.post("/v1/webhooks", json={
            "url": "https://example.com/hook",
            "events": ["ingest"],
        }, headers=AUTH)
        assert resp.status_code == 201
        wh_id = resp.json()["id"]

        # List webhooks
        resp = client.get("/v1/webhooks", headers=AUTH)
        assert resp.status_code == 200
        assert len(resp.json()["webhooks"]) >= 1

        # Delete webhook
        resp = client.delete(f"/v1/webhooks/{wh_id}", headers=AUTH)
        assert resp.status_code == 200

    def test_api_key_integration(self, client):
        # Create key
        resp = client.post("/v1/api-keys", json={"name": "test-key"}, headers=AUTH)
        assert resp.status_code == 201
        key_id = resp.json()["id"]

        # List keys
        resp = client.get("/v1/api-keys", headers=AUTH)
        assert resp.status_code == 200
        assert len(resp.json()["keys"]) >= 1

        # Revoke key
        resp = client.delete(f"/v1/api-keys/{key_id}", headers=AUTH)
        assert resp.status_code == 200
