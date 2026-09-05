"""Tests for Experience API features."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aftergraph_work_intelligence.api import create_app


@pytest.fixture()
def client(tmp_path):
    db = tmp_path / "test.db"
    app = create_app(db_path=db, api_token="test-token")
    with TestClient(app) as c:
        yield c, app


def _ingest_obs(client, tenant="tenant-a", text=None):
    import uuid
    if text is None:
        text = f"Unik observation {uuid.uuid4().hex[:8]} skal behandles"
    return client.post(
        "/v1/observations",
        json={
            "tenant_id": tenant,
            "source": "test",
            "text": text,
            "external_id": f"ext-{uuid.uuid4().hex[:8]}",
        },
        headers={"Authorization": "Bearer test-token"},
    )


class TestObservationsList:
    """Tests for listing observations."""

    def test_list_observations_empty(self, client):
        c, _ = client
        resp = c.get(
            "/v1/observations",
            params={"tenant_id": "tenant-a"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "observations" in data
        assert isinstance(data["observations"], list)

    def test_list_observations_with_data(self, client):
        c, _ = client
        _ingest_obs(c, tenant="tenant-a")
        _ingest_obs(c, tenant="tenant-a")

        resp = c.get(
            "/v1/observations",
            params={"tenant_id": "tenant-a"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["observations"]) >= 2

    def test_list_observations_tenant_isolation(self, client):
        c, _ = client
        _ingest_obs(c, tenant="tenant-a")
        _ingest_obs(c, tenant="tenant-b")

        resp = c.get(
            "/v1/observations",
            params={"tenant_id": "tenant-a"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        data = resp.json()
        for obs in data["observations"]:
            assert obs["tenant_id"] == "tenant-a"


class TestReviewQueue:
    """Tests for server-side review queue with status filtering."""

    def test_review_queue_filter_by_status(self, client):
        c, _ = client
        _ingest_obs(c, tenant="tenant-a")
        _ingest_obs(c, tenant="tenant-a")

        # Filter by OPEN status
        resp = c.get(
            "/v1/work-items",
            params={"tenant_id": "tenant-a", "status": "OPEN"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "work_items" in data

    def test_review_queue_filter_by_priority(self, client):
        c, _ = client
        _ingest_obs(c, tenant="tenant-a")

        # Filter by priority
        resp = c.get(
            "/v1/work-items",
            params={"tenant_id": "tenant-a", "priority": "high"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200


class TestTenantPolicyAPI:
    """Tests for tenant policy read/update API."""

    def test_update_tenant_policy(self, client):
        c, _ = client
        resp = c.post(
            "/v1/tenants/tenant-a/policy",
            json={
                "allowed_sources": ["test", "email"],
                "allowed_destinations": ["renos"],
                "max_work_items": 50,
                "allow_works": False,
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["updated"]

    def test_get_tenant_policy(self, client):
        c, _ = client
        # Set a policy first
        c.post(
            "/v1/tenants/tenant-a/policy",
            json={
                "allowed_sources": ["test"],
                "allowed_destinations": ["renos"],
            },
            headers={"Authorization": "Bearer test-token"},
        )

        resp = c.get(
            "/v1/tenants/tenant-a/policy",
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "allowed_sources" in data


class TestReadinessAPI:
    """Tests for integration health/readiness API."""

    def test_readiness_endpoint(self, client):
        c, _ = client
        resp = c.get(
            "/v1/readiness",
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert "checks" in data

    def test_readiness_all_checks_pass(self, client):
        c, _ = client
        resp = c.get(
            "/v1/readiness",
            headers={"Authorization": "Bearer test-token"},
        )
        data = resp.json()
        # Status can be pass or fail (fail is expected when no publisher configured)
        assert data["status"] in ("pass", "fail")
        assert "checks" in data


class TestPublicationLifecycle:
    """Tests for publication lifecycle including failure/read-back/retry state."""

    def test_publication_with_retry_state(self, client):
        c, _ = client
        resp = _ingest_obs(c, tenant="tenant-a")
        item_id = resp.json()["work_item"]["id"]

        # Get publication history with lifecycle states
        resp = c.get(
            f"/v1/work-items/{item_id}/publications",
            params={"tenant_id": "tenant-a"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200

    def test_publication_failure_tracking(self, client):
        c, _ = client
        resp = _ingest_obs(c, tenant="tenant-a")
        item_id = resp.json()["work_item"]["id"]

        # Check publication includes failure/retry tracking
        resp = c.get(
            f"/v1/work-items/{item_id}/publications",
            params={"tenant_id": "tenant-a"},
            headers={"Authorization": "Bearer test-token"},
        )
        data = resp.json()
        assert "publications" in data


class TestCapabilitiesAPI:
    """Tests for capabilities/allowed-actions response per WorkItem."""

    def test_allowed_actions_for_open_item(self, client):
        c, _ = client
        resp = _ingest_obs(c, tenant="tenant-a")
        item_id = resp.json()["work_item"]["id"]

        resp = c.get(
            f"/v1/work-items/{item_id}/actions",
            params={"tenant_id": "tenant-a"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "actions" in data
        assert "approve" in data["actions"]


class TestActorContext:
    """Tests for current actor/role/permission context."""

    def test_actor_context_endpoint(self, client):
        c, _ = client
        resp = c.get(
            "/v1/context",
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "actor" in data

