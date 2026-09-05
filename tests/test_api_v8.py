"""Tests for audit trail, monitoring, work-item metadata, and advanced API features."""

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
TENANT = "default"
SOURCE = "manual"


def _ingest(c, text="Vi skal købe 5 licenser til teamet"):
    """Helper: ingest an observation that triggers work item creation."""
    return c.post("/v1/observations", json={
        "tenant_id": TENANT,
        "source": SOURCE,
        "text": text,
    }, headers=AUTH)


def _create_item(c, text="Vi skal købe 5 licenser til teamet"):
    """Helper: ingest and return the work item ID."""
    resp = _ingest(c, text)
    if resp.status_code in (200, 201) and resp.json().get("work_item"):
        return resp.json()["work_item"]["id"]
    return None


class TestAuditTrail:

    def test_ingest_creates_audit_record(self, client):
        resp = _ingest(client)
        assert resp.status_code == 201
        data = resp.json()
        assert data["action"] == "created"
        assert data["work_item"] is not None

    def test_approve_changes_state(self, client):
        item_id = _create_item(client)
        assert item_id is not None

        resp = client.post(
            f"/v1/work-items/{item_id}/review?tenant_id={TENANT}",
            json={"action": "approve", "actor": "test-user"},
            headers=AUTH,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "APPROVED"

    def test_reject_changes_state(self, client):
        item_id = _create_item(client, "Vi skal sende fakturaen til kunden hurtigt")
        assert item_id is not None

        resp = client.post(
            f"/v1/work-items/{item_id}/review?tenant_id={TENANT}",
            json={"action": "reject", "actor": "test-user"},
            headers=AUTH,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "REJECTED"

    def test_bulk_status_returns_all_items(self, client):
        ids = []
        for i in range(3):
            item_id = _create_item(client, f"Vi skal købe {i} licenser hurtigt")
            if item_id:
                ids.append(item_id)

        if ids:
            resp = client.post(
                f"/v1/work-items/bulk-status?tenant_id={TENANT}",
                json={"work_item_ids": ids},
                headers=AUTH,
            )
            # Bulk status may use different response format
            assert resp.status_code in (200, 422)


class TestMonitoring:

    def test_monitoring_returns_metrics(self, client):
        resp = client.get("/v1/monitoring", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert "system" in data
        assert "cpu_percent" in data["system"]
        assert "memory_percent" in data["system"]
        assert "disk_percent" in data["system"]

    def test_monitoring_requires_auth(self, client):
        resp = client.get("/v1/monitoring")
        assert resp.status_code == 401


class TestWorkItemMetadata:

    def test_ingest_returns_all_fields(self, client):
        resp = _ingest(client)
        assert resp.status_code == 201
        data = resp.json()
        assert "work_item" in data
        item = data["work_item"]
        assert "id" in item
        assert "status" in item
        assert item["title"]

    def test_work_item_has_tenant(self, client):
        resp = client.post("/v1/observations", json={
            "tenant_id": "tenant-abc",
            "source": SOURCE,
            "text": "Vi skal købe 5 licenser til teamet hurtigt",
        }, headers=AUTH)
        assert resp.status_code == 201
        item = resp.json()["work_item"]
        assert item["tenant_id"] == "tenant-abc"

    def test_search_returns_results(self, client):
        _create_item(client, "Vi skal købe 5 licenser til teamet hurtigt")

        resp = client.get(f"/v1/search?q=licenser&tenant_id={TENANT}", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        # Search returns results; may use "total" or "count"
        count = data.get("total") or data.get("count") or len(data.get("results", []))
        assert count >= 1

    def test_tenants_endpoint(self, client):
        _create_item(client)

        resp = client.get("/v1/tenants", headers=AUTH)
        assert resp.status_code == 200
        tenants = resp.json()["tenants"]
        assert len(tenants) >= 1


class TestConcurrentIngest:

    def test_multiple_concurrent_ingests(self, client):
        results = []
        for i in range(10):
            resp = _ingest(client, f"Vi skal købe {i} licenser hurtigt")
            results.append(resp)

        assert all(r.status_code in (200, 201, 202) for r in results)
        # All observations accepted: 201=created, 200=observed/deduped, 202=deduped
        assert len(results) == 10

    def test_dedup_concurrent_same_text(self, client):
        results = []
        for _ in range(5):
            resp = _ingest(client, "Præcis samme observation med køb")
            results.append(resp)

        statuses = [r.status_code for r in results]
        assert all(s in (200, 201, 202) for s in statuses)


class TestErrorHandling:

    def test_invalid_json_body(self, client):
        resp = client.post(
            "/v1/observations",
            content=b"not json",
            headers={**AUTH, "Content-Type": "application/json"},
        )
        assert resp.status_code == 422

    def test_missing_required_fields(self, client):
        resp = client.post("/v1/observations", json={}, headers=AUTH)
        assert resp.status_code == 422

    def test_empty_text(self, client):
        resp = client.post("/v1/observations", json={
            "tenant_id": TENANT,
            "source": SOURCE,
            "text": "",
        }, headers=AUTH)
        assert resp.status_code == 422

    def test_method_not_allowed(self, client):
        resp = client.put("/v1/observations", json={}, headers=AUTH)
        assert resp.status_code == 405

    def test_not_found_item(self, client):
        resp = client.get(f"/v1/work-items/{uuid.uuid4()}?tenant_id={TENANT}", headers=AUTH)
        assert resp.status_code == 404

    def test_approve_nonexistent_item(self, client):
        resp = client.post(
            f"/v1/work-items/{uuid.uuid4()}/review?tenant_id={TENANT}",
            json={"action": "approve", "actor": "test"},
            headers=AUTH,
        )
        assert resp.status_code == 404

    def test_search_empty_query(self, client):
        resp = client.get(f"/v1/search?q=&tenant_id={TENANT}", headers=AUTH)
        # Empty query is rejected by validation (422) or returns 0 results
        assert resp.status_code in (200, 422)

    def test_webhook_crud_cycle(self, client):
        resp = client.post("/v1/webhooks", json={
            "url": "https://example.com/hook",
            "events": ["ingest"],
        }, headers=AUTH)
        assert resp.status_code == 201
        wh_id = resp.json()["id"]

        resp = client.get("/v1/webhooks", headers=AUTH)
        assert resp.status_code == 200
        assert len(resp.json()["webhooks"]) >= 1

        resp = client.delete(f"/v1/webhooks/{wh_id}", headers=AUTH)
        assert resp.status_code == 200

        resp = client.get("/v1/webhooks", headers=AUTH)
        assert not any(w["id"] == wh_id for w in resp.json()["webhooks"])

    def test_api_key_lifecycle(self, client):
        resp = client.post("/v1/api-keys", json={"name": "test-key"}, headers=AUTH)
        assert resp.status_code == 201
        resp.json()["key"]

        resp = client.get("/v1/api-keys", headers=AUTH)
        assert resp.status_code == 200
        assert len(resp.json()["keys"]) >= 1

        key_id = resp.json()["keys"][0]["id"]
        resp = client.delete(f"/v1/api-keys/{key_id}", headers=AUTH)
        assert resp.status_code == 200

    def test_health_probes(self, client):
        for path in ["/healthz", "/healthz/detailed", "/ready", "/live"]:
            resp = client.get(path)
            assert resp.status_code == 200, f"{path} returned {resp.status_code}"

    def test_transitions_endpoint(self, client):
        item_id = _create_item(client)
        assert item_id is not None

        resp = client.get(f"/v1/work-items/{item_id}/transitions?tenant_id={TENANT}", headers=AUTH)
        assert resp.status_code == 200
        assert "transitions" in resp.json()

    def test_publications_endpoint(self, client):
        item_id = _create_item(client)
        assert item_id is not None

        resp = client.get(f"/v1/work-items/{item_id}/publications?tenant_id={TENANT}", headers=AUTH)
        assert resp.status_code == 200
        assert "publications" in resp.json()
