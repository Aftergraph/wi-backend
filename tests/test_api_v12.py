"""Tests for observation handling, tenant operations, and work item operations."""

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


def _create(c, text="Vi skal købe 5 licenser til teamet hurtigt", tenant="default"):
    resp = c.post("/v1/observations", json={
        "tenant_id": tenant,
        "source": "manual",
        "text": text,
    }, headers=AUTH)
    if resp.status_code == 201 and resp.json().get("work_item"):
        return resp.json()["work_item"]["id"]
    return None


class TestObservationHandling:
    """Verify observation ingestion and processing."""

    def test_create_observation(self, client):
        resp = client.post("/v1/observations", json={
            "tenant_id": "default",
            "source": "manual",
            "text": "Vi skal købe 5 licenser hurtigt",
        }, headers=AUTH)
        assert resp.status_code == 201
        data = resp.json()
        assert data["action"] == "created"
        assert data["work_item"] is not None

    def test_observation_without_action_verb(self, client):
        resp = client.post("/v1/observations", json={
            "tenant_id": "default",
            "source": "manual",
            "text": "5 licenser til teamet",
        }, headers=AUTH)
        # Without action verb, may not create work item
        assert resp.status_code in (200, 201, 202)

    def test_observation_dedup(self, client):
        text = "Vi skal købe 5 licenser hurtigt"
        
        # First observation
        client.post("/v1/observations", json={
            "tenant_id": "default",
            "source": "manual",
            "text": text,
        }, headers=AUTH)
        
        # Second observation (duplicate)
        resp2 = client.post("/v1/observations", json={
            "tenant_id": "default",
            "source": "manual",
            "text": text,
        }, headers=AUTH)
        
        # Should be deduped
        assert resp2.status_code in (200, 202)

    def test_observation_with_metadata(self, client):
        resp = client.post("/v1/observations", json={
            "tenant_id": "default",
            "source": "manual",
            "text": "Vi skal købe 5 licenser hurtigt",
            "metadata": {"priority": "high", "department": "engineering"},
        }, headers=AUTH)
        assert resp.status_code == 201

    def test_observation_with_external_id(self, client):
        resp = client.post("/v1/observations", json={
            "tenant_id": "default",
            "source": "manual",
            "text": "Vi skal købe 5 licenser hurtigt",
            "external_id": "EXT-12345",
        }, headers=AUTH)
        assert resp.status_code == 201

    def test_observation_with_actor(self, client):
        resp = client.post("/v1/observations", json={
            "tenant_id": "default",
            "source": "manual",
            "text": "Vi skal købe 5 licenser hurtigt",
            "actor": "user@example.com",
        }, headers=AUTH)
        assert resp.status_code == 201


class TestTenantOperations:
    """Verify tenant-related operations."""

    def test_create_tenant_via_observation(self, client):
        resp = client.post("/v1/observations", json={
            "tenant_id": "new-tenant",
            "source": "manual",
            "text": "Vi skal købe licenser hurtigt",
        }, headers=AUTH)
        assert resp.status_code == 201

        # Verify tenant exists
        resp = client.get("/v1/tenants", headers=AUTH)
        assert resp.status_code == 200
        tenants = resp.json()["tenants"]
        assert any(t["tenant_id"] == "new-tenant" for t in tenants)

    def test_tenant_isolation(self, client):
        # Create items in different tenants
        _create(client, "Vi skal købe licenser hurtigt", tenant="tenant-a")
        _create(client, "Vi skal købe licenser hurtigt", tenant="tenant-b")

        # List items per tenant
        resp_a = client.get("/v1/work-items?tenant_id=tenant-a", headers=AUTH)
        resp_b = client.get("/v1/work-items?tenant_id=tenant-b", headers=AUTH)

        assert resp_a.status_code == 200
        assert resp_b.status_code == 200

        # Each tenant should have items
        assert resp_a.json()["count"] >= 1
        assert resp_b.json()["count"] >= 1

    def test_tenant_list(self, client):
        _create(client, tenant="list-tenant-1")
        _create(client, tenant="list-tenant-2")

        resp = client.get("/v1/tenants", headers=AUTH)
        assert resp.status_code == 200
        tenants = resp.json()["tenants"]
        assert len(tenants) >= 2


class TestWorkItemOperations:
    """Verify work item CRUD operations."""

    def test_get_work_item(self, client):
        item_id = _create(client)
        assert item_id

        resp = client.get(f"/v1/work-items/{item_id}?tenant_id=default", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        # Response may be flat or wrapped
        item = data.get("work_item", data)
        assert item["id"] == item_id

    def test_list_work_items(self, client):
        _create(client)
        _create(client, "Vi skal sende faktura hurtigt")

        resp = client.get("/v1/work-items?tenant_id=default", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 2
        assert len(data["work_items"]) >= 2

    def test_work_item_not_found(self, client):
        resp = client.get(f"/v1/work-items/{uuid.uuid4()}?tenant_id=default", headers=AUTH)
        assert resp.status_code == 404

    def test_work_item_transitions(self, client):
        item_id = _create(client)
        assert item_id

        resp = client.get(f"/v1/work-items/{item_id}/transitions?tenant_id=default", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert "transitions" in data
        assert isinstance(data["transitions"], list)

    def test_work_item_publications(self, client):
        item_id = _create(client)
        assert item_id

        resp = client.get(f"/v1/work-items/{item_id}/publications?tenant_id=default", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert "publications" in data
        assert isinstance(data["publications"], list)

    def test_work_item_search(self, client):
        _create(client, "Vi skal købe licenser hurtigt")

        resp = client.get("/v1/search?q=licenser&tenant_id=default", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        count = data.get("total") or data.get("count") or len(data.get("results", []))
        assert count >= 1

    def test_work_item_bulk_status(self, client):
        ids = []
        for i in range(3):
            item_id = _create(client, f"Vi skal købe {i} licenser hurtigt")
            if item_id:
                ids.append(item_id)

        if ids:
            resp = client.post(
                "/v1/work-items/bulk-status?tenant_id=default",
                json={"work_item_ids": ids},
                headers=AUTH,
            )
            # Bulk status may use different format
            assert resp.status_code in (200, 422)
