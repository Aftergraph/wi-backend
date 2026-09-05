"""Tests for performance, security, and integration."""

from __future__ import annotations

import time
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


class TestPerformance:
    """Verify API performance characteristics."""

    def test_health_endpoint_latency(self, client):
        start = time.time()
        for _ in range(20):
            resp = client.get("/healthz")
            assert resp.status_code == 200
        elapsed = time.time() - start
        # 20 requests should complete in < 2 seconds
        assert elapsed < 2.0

    def test_ingest_latency(self, client):
        start = time.time()
        for i in range(50):
            resp = client.post("/v1/observations", json={
                "tenant_id": "default",
                "source": "manual",
                "text": f"Vi skal købe {i} licenser hurtigt",
            }, headers=AUTH)
            assert resp.status_code in (200, 201, 202)
        elapsed = time.time() - start
        # 50 ingests should complete in < 10 seconds
        assert elapsed < 10.0

    def test_list_latency(self, client):
        # Create some items first
        for i in range(10):
            _create(client, f"Vi skal købe {i} licenser hurtigt")

        start = time.time()
        for _ in range(50):
            resp = client.get("/v1/work-items?tenant_id=default", headers=AUTH)
            assert resp.status_code == 200
        elapsed = time.time() - start
        # 50 list requests should complete in < 5 seconds
        assert elapsed < 5.0

    def test_search_latency(self, client):
        _create(client)

        start = time.time()
        for _ in range(50):
            resp = client.get("/v1/search?q=licenser&tenant_id=default", headers=AUTH)
            assert resp.status_code == 200
        elapsed = time.time() - start
        # 50 searches should complete in < 5 seconds
        assert elapsed < 5.0


class TestSecurity:
    """Verify security characteristics."""

    def test_no_auth_rejected(self, client):
        endpoints = [
            ("POST", "/v1/observations"),
            ("GET", "/v1/work-items?tenant_id=default"),
            ("GET", f"/v1/work-items/{uuid.uuid4()}?tenant_id=default"),
            ("POST", "/v1/webhooks"),
            ("GET", "/v1/webhooks"),
            ("POST", "/v1/api-keys"),
            ("GET", "/v1/api-keys"),
        ]
        for method, path in endpoints:
            resp = client.post(path, json={}) if method == "POST" else client.get(path)
            assert resp.status_code == 401, f"{method} {path} should require auth"

    def test_invalid_token_rejected(self, client):
        resp = client.get("/v1/work-items?tenant_id=default",
                         headers={"Authorization": "Bearer invalid-token"})
        assert resp.status_code == 401

    def test_malformed_auth_header(self, client):
        resp = client.get("/v1/work-items?tenant_id=default",
                         headers={"Authorization": "InvalidFormat"})
        assert resp.status_code == 401

    def test_empty_auth_header(self, client):
        resp = client.get("/v1/work-items?tenant_id=default",
                         headers={"Authorization": ""})
        assert resp.status_code == 401

    def test_health_endpoints_no_auth(self, client):
        for path in ["/healthz", "/healthz/detailed", "/ready", "/live"]:
            resp = client.get(path)
            assert resp.status_code == 200, f"{path} should not require auth"

    def test_xss_in_text(self, client):
        resp = client.post("/v1/observations", json={
            "tenant_id": "default",
            "source": "manual",
            "text": "<script>alert('xss')</script>",
        }, headers=AUTH)
        assert resp.status_code in (200, 201, 202)

    def test_sql_injection_in_text(self, client):
        resp = client.post("/v1/observations", json={
            "tenant_id": "default",
            "source": "manual",
            "text": "'; DROP TABLE work_items; --",
        }, headers=AUTH)
        assert resp.status_code in (200, 201, 202)

    def test_oversized_payload(self, client):
        resp = client.post("/v1/observations", json={
            "tenant_id": "default",
            "source": "manual",
            "text": "x" * 1_000_000,
        }, headers=AUTH)
        # Should reject or handle gracefully
        assert resp.status_code in (200, 201, 202, 413, 422)


class TestIntegration:
    """Verify end-to-end integration scenarios."""

    def test_full_lifecycle(self, client):
        # 1. Create observation
        resp = client.post("/v1/observations", json={
            "tenant_id": "integration-test",
            "source": "manual",
            "text": "Vi skal købe 5 licenser hurtigt",
        }, headers=AUTH)
        assert resp.status_code == 201
        item_id = resp.json()["work_item"]["id"]

        # 2. Get work item
        resp = client.get(f"/v1/work-items/{item_id}?tenant_id=integration-test", headers=AUTH)
        assert resp.status_code == 200

        # 3. Search for it
        resp = client.get("/v1/search?q=licenser&tenant_id=integration-test", headers=AUTH)
        assert resp.status_code == 200

        # 4. Approve it
        resp = client.post(
            f"/v1/work-items/{item_id}/review?tenant_id=integration-test",
            json={"action": "approve", "actor": "test-user"},
            headers=AUTH,
        )
        assert resp.status_code == 200

        # 5. Check transitions
        resp = client.get(f"/v1/work-items/{item_id}/transitions?tenant_id=integration-test", headers=AUTH)
        assert resp.status_code == 200

        # 6. Check monitoring
        resp = client.get("/v1/monitoring", headers=AUTH)
        assert resp.status_code == 200

    def test_multi_tenant_lifecycle(self, client):
        tenants = ["tenant-1", "tenant-2", "tenant-3"]
        
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

    def test_webhook_lifecycle(self, client):
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

        # Verify deleted
        resp = client.get("/v1/webhooks", headers=AUTH)
        assert not any(w["id"] == wh_id for w in resp.json()["webhooks"])

    def test_api_key_lifecycle(self, client):
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
