"""Tests for API key authentication."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aftergraph_work_intelligence.api import create_app


@pytest.fixture()
def app_data(tmp_path):
    db = tmp_path / "test.db"
    app = create_app(db_path=db, api_token="master-token")
    return app


@pytest.fixture()
def client(app_data):
    with TestClient(app_data) as c:
        yield c


@pytest.fixture()
def created_key(client):
    """Create an API key and return it."""
    resp = client.post(
        "/v1/api-keys",
        json={"name": "auth-test"},
        headers={"Authorization": "Bearer master-token"},
    )
    return resp.json()["key"]


AUTH_BEARER = {"Authorization": "Bearer master-token"}


class TestAPIKeyAuth:
    """Test API key authentication via X-API-Key header."""

    def test_bearer_token_still_works(self, client):
        resp = client.get("/v1/work-items?tenant_id=default", headers=AUTH_BEARER)
        assert resp.status_code == 200

    def test_api_key_auth_works(self, client, created_key):
        resp = client.get("/v1/work-items?tenant_id=default", headers={"X-API-Key": created_key})
        assert resp.status_code == 200

    def test_invalid_api_key_rejected(self, client):
        resp = client.get(
            "/v1/work-items?tenant_id=default",
            headers={"X-API-Key": "ak_invalidkey123"},
        )
        assert resp.status_code == 401

    def test_same_prefix_forged_api_key_rejected(self, client, created_key):
        replacement = "0" if created_key[12] != "0" else "1"
        forged = created_key[:12] + replacement + created_key[13:]
        assert forged[:12] == created_key[:12]
        resp = client.get("/v1/work-items?tenant_id=default", headers={"X-API-Key": forged})
        assert resp.status_code == 401

    def test_revoked_api_key_rejected(self, client):
        # Create a key we can track
        resp = client.post("/v1/api-keys", json={"name": "to-revoke"}, headers=AUTH_BEARER)
        key_info = resp.json()
        key_to_revoke = key_info["key"]
        key_id = key_info["id"]

        # Verify it works first
        resp = client.get(
            "/v1/work-items?tenant_id=default",
            headers={"X-API-Key": key_to_revoke},
        )
        assert resp.status_code == 200

        # Revoke it
        client.delete(f"/v1/api-keys/{key_id}", headers=AUTH_BEARER)

        # Now it should fail
        resp = client.get(
            "/v1/work-items?tenant_id=default",
            headers={"X-API-Key": key_to_revoke},
        )
        assert resp.status_code == 401

    def test_no_auth_rejected(self, client):
        resp = client.get("/v1/work-items?tenant_id=default")
        assert resp.status_code == 401

    def test_api_key_can_write(self, client, created_key):
        resp = client.post(
            "/v1/observations",
            json={
                "tenant_id": "default",
                "source": "test",
                "text": "API key auth test",
            },
            headers={"X-API-Key": created_key},
        )
        assert resp.status_code in (200, 201, 202)

    def test_rotated_key_old_rejected(self, client):
        # Create key
        resp = client.post("/v1/api-keys", json={"name": "rotate-test"}, headers=AUTH_BEARER)
        old_key = resp.json()["key"]
        key_id = resp.json()["id"]

        # Verify old key works
        resp = client.get("/v1/work-items?tenant_id=default", headers={"X-API-Key": old_key})
        assert resp.status_code == 200

        # Rotate
        resp = client.post(f"/v1/api-keys/{key_id}/rotate", headers=AUTH_BEARER)
        new_key = resp.json()["key"]

        # Old key should fail
        resp = client.get("/v1/work-items?tenant_id=default", headers={"X-API-Key": old_key})
        assert resp.status_code == 401

        # New key should work
        resp = client.get("/v1/work-items?tenant_id=default", headers={"X-API-Key": new_key})
        assert resp.status_code == 200
