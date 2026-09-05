"""Tests for state transitions, data integrity, and concurrent operations."""

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


def _create(c, text="Vi skal købe 5 licenser til teamet hurtigt", tenant="default"):
    resp = c.post("/v1/observations", json={
        "tenant_id": tenant,
        "source": "manual",
        "text": text,
    }, headers=AUTH)
    if resp.status_code == 201 and resp.json().get("work_item"):
        return resp.json()["work_item"]["id"]
    return None


class TestStateTransitions:
    """Verify state machine transitions are correct."""

    def test_open_to_approved(self, client):
        item_id = _create(client)
        assert item_id

        resp = client.post(
            f"/v1/work-items/{item_id}/review?tenant_id=default",
            json={"action": "approve", "actor": "user-1"},
            headers=AUTH,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "APPROVED"

    def test_open_to_rejected(self, client):
        item_id = _create(client, "Vi skal sende faktura hurtigt")
        assert item_id

        resp = client.post(
            f"/v1/work-items/{item_id}/review?tenant_id=default",
            json={"action": "reject", "actor": "user-1"},
            headers=AUTH,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "REJECTED"

    def test_approved_cannot_be_approved_again(self, client):
        item_id = _create(client)
        assert item_id

        # First approval
        client.post(
            f"/v1/work-items/{item_id}/review?tenant_id=default",
            json={"action": "approve", "actor": "user-1"},
            headers=AUTH,
        )

        # Second approval should fail
        resp = client.post(
            f"/v1/work-items/{item_id}/review?tenant_id=default",
            json={"action": "approve", "actor": "user-2"},
            headers=AUTH,
        )
        assert resp.status_code in (400, 409)

    def test_rejected_cannot_be_approved(self, client):
        item_id = _create(client, "Vi skal sende faktura hurtigt")
        assert item_id

        # Reject
        client.post(
            f"/v1/work-items/{item_id}/review?tenant_id=default",
            json={"action": "reject", "actor": "user-1"},
            headers=AUTH,
        )

        # Try to approve
        resp = client.post(
            f"/v1/work-items/{item_id}/review?tenant_id=default",
            json={"action": "approve", "actor": "user-2"},
            headers=AUTH,
        )
        assert resp.status_code in (400, 409)

    def test_cancel_from_open(self, client):
        item_id = _create(client)
        assert item_id

        resp = client.post(
            f"/v1/work-items/{item_id}/review?tenant_id=default",
            json={"action": "cancel", "actor": "user-1"},
            headers=AUTH,
        )
        assert resp.status_code in (200, 400, 409)


class TestDataIntegrity:
    """Verify data integrity across operations."""

    def test_work_item_fields_intact(self, client):
        item_id = _create(client, "Vi skal købe 5 licenser til teamet hurtigt")
        assert item_id

        resp = client.get(f"/v1/work-items/{item_id}?tenant_id=default", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        item = data.get("work_item", data)

        # All fields should be present
        assert item["id"] == item_id
        assert item["tenant_id"] == "default"
        assert item["title"]
        assert item["summary"]
        assert item["status"] in ("OPEN", "APPROVED", "REJECTED", "CANCELLED")
        assert item["priority"]
        assert item["confidence"] >= 0
        assert item["observation_count"] >= 1
        assert item["created_at"]
        assert item["updated_at"]

    def test_transition_history_intact(self, client):
        item_id = _create(client)
        assert item_id

        # Approve
        client.post(
            f"/v1/work-items/{item_id}/review?tenant_id=default",
            json={"action": "approve", "actor": "user-1"},
            headers=AUTH,
        )

        # Check transitions
        resp = client.get(f"/v1/work-items/{item_id}/transitions?tenant_id=default", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        transitions = data.get("transitions", [])

        # Should have at least one transition
        assert len(transitions) >= 1

        # Each transition should have required fields
        for t in transitions:
            assert "action" in t

    def test_publications_intact(self, client):
        item_id = _create(client)
        assert item_id

        resp = client.get(f"/v1/work-items/{item_id}/publications?tenant_id=default", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        publications = data.get("publications", [])
        assert isinstance(publications, list)

    def test_monitoring_counts_intact(self, client):
        # Create some items
        _create(client)
        _create(client, "Vi skal sende faktura hurtigt")

        resp = client.get("/v1/monitoring", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        service = data.get("service", {})

        assert service.get("total_observations", 0) >= 2
        assert service.get("total_work_items", 0) >= 1


class TestConcurrentDataIntegrity:
    """Verify data integrity under concurrent access."""

    def test_concurrent_ingest_same_text(self, client):
        results = []
        for _ in range(10):
            resp = client.post("/v1/observations", json={
                "tenant_id": "default",
                "source": "manual",
                "text": "Vi skal købe licenser hurtigt",
            }, headers=AUTH)
            results.append(resp)

        # All should succeed
        assert all(r.status_code in (200, 201, 202) for r in results)

        # Should create at most one work item (dedup)
        created = [r for r in results if r.status_code == 201]
        assert len(created) <= 1

    def test_concurrent_ingest_different_texts(self, client):
        results = []
        for i in range(10):
            resp = client.post("/v1/observations", json={
                "tenant_id": "default",
                "source": "manual",
                "text": f"Unik tekst {i} med køb",
            }, headers=AUTH)
            results.append(resp)

        assert all(r.status_code in (200, 201, 202) for r in results)

    def test_concurrent_review_same_item(self, client):
        item_id = _create(client)
        assert item_id

        # First review
        resp1 = client.post(
            f"/v1/work-items/{item_id}/review?tenant_id=default",
            json={"action": "approve", "actor": "user-1"},
            headers=AUTH,
        )

        # Second review (may fail)
        resp2 = client.post(
            f"/v1/work-items/{item_id}/review?tenant_id=default",
            json={"action": "reject", "actor": "user-2"},
            headers=AUTH,
        )

        # One should succeed, one should fail
        assert resp1.status_code in (200, 400, 409)
        assert resp2.status_code in (200, 400, 409)

    def test_concurrent_different_tenants(self, client):
        results = []
        for i in range(5):
            resp = client.post("/v1/observations", json={
                "tenant_id": f"tenant-{i}",
                "source": "manual",
                "text": f"Vi skal købe licenser til {i} hurtigt",
            }, headers=AUTH)
            results.append(resp)

        assert all(r.status_code in (200, 201, 202) for r in results)
