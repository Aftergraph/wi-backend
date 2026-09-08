"""Tests for dashboard endpoint."""

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


def _create(c, text="Vi skal købe 5 licenser til teamet hurtigt"):
    resp = c.post("/v1/observations", json={
        "tenant_id": "default",
        "source": "manual",
        "text": text,
    }, headers=AUTH)
    if resp.status_code == 201 and resp.json().get("work_item"):
        return resp.json()["work_item"]["id"]
    return None


class TestDashboard:
    """Test HTML dashboard endpoint."""

    def test_dashboard_returns_html(self, client):
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Wie by Aftergraph" in resp.text

    def test_dashboard_empty(self, client):
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "0" in resp.text  # zero counts

    def test_dashboard_with_data(self, client):
        _create(client)
        _create(client, "Vi skal ringe til kunden hurtigt")

        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "Observations" in resp.text
        assert "Work Items" in resp.text
        assert "default" in resp.text  # tenant shown

    def test_dashboard_links_to_docs(self, client):
        resp = client.get("/dashboard")
        assert "/docs" in resp.text

    def test_dashboard_no_auth_required(self, client):
        resp = client.get("/dashboard")
        assert resp.status_code == 200
