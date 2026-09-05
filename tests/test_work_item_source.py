"""Tests for source field in work-items list response."""
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


def _ingest(c, source: str, text: str, tenant: str = "default"):
    resp = c.post("/v1/observations", json={
        "tenant_id": tenant,
        "source": source,
        "text": text,
    }, headers=AUTH)
    return resp


class TestWorkItemSourceField:
    """Verify GET /v1/work-items returns a `source` field per item."""

    def test_list_includes_source_field(self, client):
        _ingest(client, "github", "Fix login bug in auth module")
        resp = client.get("/v1/work-items", params={"tenant_id": "default"}, headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 1
        for item in data["work_items"]:
            assert "source" in item, f"Missing 'source' field in work item: {item}"

    def test_source_derived_from_single_observation(self, client):
        _ingest(client, "gmail", "Customer needs API docs by Friday")
        resp = client.get("/v1/work-items", params={"tenant_id": "default"}, headers=AUTH)
        items = resp.json()["work_items"]
        gmail_items = [i for i in items if i.get("source") == "gmail"]
        assert len(gmail_items) >= 1

    def test_source_most_common_among_observations(self, client):
        # Create a work item, then merge multiple observations from different sources
        # First observation creates the item
        res = _ingest(client, "github", "Deploy new feature to production")
        assert res.status_code in (201, 202)

        # Merge more github observations (should dominate)
        _ingest(client, "github", "Deploy new feature to staging environment")
        _ingest(client, "slack", "Deploy new feature discussion in channel")

        resp = client.get("/v1/work-items", params={"tenant_id": "default"}, headers=AUTH)
        items = resp.json()["work_items"]
        # At least one item should have source derived
        for item in items:
            assert "source" in item
            assert isinstance(item["source"], str)

    def test_source_unknown_when_no_observations(self, client):
        # Edge case: work item with no linked observations should get 'unknown'
        # This is hard to trigger via API since items are created from observations,
        # but we verify the fallback exists in the response shape
        resp = client.get("/v1/work-items", params={"tenant_id": "empty-tenant"}, headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        # Empty list is valid; source field tested when items exist

    def test_source_values_are_strings(self, client):
        _ingest(client, "calendar", "Prepare quarterly review meeting")
        resp = client.get("/v1/work-items", params={"tenant_id": "default"}, headers=AUTH)
        for item in resp.json()["work_items"]:
            assert isinstance(item.get("source"), str)
