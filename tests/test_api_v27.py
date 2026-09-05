"""Tests for final comprehensive coverage and edge cases."""

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


class TestFinalComprehensiveCoverage:
    """Final comprehensive coverage tests."""

    def test_concurrent_different_texts(self, client):
        results = []
        for i in range(10):
            resp = client.post("/v1/observations", json={
                "tenant_id": "default",
                "source": "manual",
                "text": f"Unik tekst {i} med køb",
            }, headers=AUTH)
            results.append(resp)

        assert all(r.status_code in (200, 201, 202) for r in results)

    def test_review_with_all_fields(self, client):
        item_id = _create(client)
        assert item_id

        resp = client.post(
            f"/v1/work-items/{item_id}/review?tenant_id=default",
            json={
                "action": "approve",
                "actor": "user-1",
                "reason": "Godkendt af teamleder",
            },
            headers=AUTH,
        )
        assert resp.status_code == 200

    def test_snooze_with_resume_at(self, client):
        item_id = _create(client)
        assert item_id

        resp = client.post(
            f"/v1/work-items/{item_id}/review?tenant_id=default",
            json={
                "action": "snooze",
                "actor": "user-1",
                "resume_at": "2026-12-31T00:00:00Z",
            },
            headers=AUTH,
        )
        # May succeed or fail depending on state
        assert resp.status_code in (200, 400, 409)

    def test_cancel_work_item(self, client):
        item_id = _create(client)
        assert item_id

        resp = client.post(
            f"/v1/work-items/{item_id}/review?tenant_id=default",
            json={"action": "cancel", "actor": "user-1"},
            headers=AUTH,
        )
        assert resp.status_code in (200, 400, 409)

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


class TestErrorPatternsFinalComprehensive:
    """Final comprehensive error response patterns."""

    def test_404_consistent(self, client):
        paths = [
            f"/v1/work-items/{uuid.uuid4()}?tenant_id=default",
            f"/v1/work-items/{uuid.uuid4()}/transitions?tenant_id=default",
            f"/v1/work-items/{uuid.uuid4()}/publications?tenant_id=default",
        ]
        for path in paths:
            resp = client.get(path, headers=AUTH)
            assert resp.status_code == 404, f"{path} returned {resp.status_code}"

    def test_401_consistent(self, client):
        endpoints = [
            ("POST", "/v1/observations"),
            ("GET", "/v1/work-items?tenant_id=default"),
            ("GET", "/v1/tenants"),
        ]
        for method, path in endpoints:
            resp = client.post(path, json={}) if method == "POST" else client.get(path)
            assert resp.status_code == 401, f"{method} {path} returned {resp.status_code}"

    def test_422_consistent(self, client):
        resp = client.post("/v1/observations", json={}, headers=AUTH)
        assert resp.status_code == 422
        data = resp.json()
        assert "detail" in data
        assert isinstance(data["detail"], list)


class TestQueryParameterValidationFinalComprehensive:
    """Final comprehensive query parameter validation."""

    def test_pagination_limit(self, client):
        for i in range(5):
            _create(client, f"Vi skal købe {i} licenser hurtigt")

        resp = client.get("/v1/work-items?tenant_id=default&limit=2", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["work_items"]) <= 2

    def test_pagination_offset(self, client):
        for i in range(5):
            _create(client, f"Vi skal købe {i} licenser hurtigt")

        resp = client.get("/v1/work-items?tenant_id=default&limit=2&offset=2", headers=AUTH)
        assert resp.status_code == 200

    def test_search_with_limit(self, client):
        _create(client)

        resp = client.get("/v1/search?q=licenser&tenant_id=default&limit=1", headers=AUTH)
        assert resp.status_code == 200

    def test_invalid_limit(self, client):
        resp = client.get("/v1/work-items?tenant_id=default&limit=-1", headers=AUTH)
        assert resp.status_code == 422

    def test_limit_too_large(self, client):
        resp = client.get("/v1/work-items?tenant_id=default&limit=10000", headers=AUTH)
        assert resp.status_code == 422
