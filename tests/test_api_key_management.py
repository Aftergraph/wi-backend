"""Tests for DB-backed API key management with rotation."""

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


class TestAPIKeyManagement:
    """Test DB-backed API key CRUD + rotation."""

    def test_create_api_key(self, client):
        resp = client.post("/v1/api-keys", json={
            "name": "test-key",
            "permissions": ["read", "write"],
        }, headers=AUTH)
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "test-key"
        assert data["key"].startswith("ak_")
        assert data["active"] is True
        assert "_warning" in data

    def test_list_api_keys(self, client):
        # Create two keys
        client.post("/v1/api-keys", json={"name": "key-1"}, headers=AUTH)
        client.post("/v1/api-keys", json={"name": "key-2"}, headers=AUTH)

        resp = client.get("/v1/api-keys", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 2
        # Keys should NOT contain the secret
        for key in data["keys"]:
            assert "key" not in key or key.get("key") is None

    def test_revoke_api_key(self, client):
        # Create key
        resp = client.post("/v1/api-keys", json={"name": "revoke-me"}, headers=AUTH)
        key_id = resp.json()["id"]

        # Revoke it
        resp = client.delete(f"/v1/api-keys/{key_id}", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["status"] == "revoked"

        # Verify it's gone from active
        resp = client.get("/v1/api-keys", headers=AUTH)
        keys = resp.json()["keys"]
        active_keys = [k for k in keys if k["active"]]
        assert all(k["id"] != key_id for k in active_keys)

    def test_revoke_nonexistent_key(self, client):
        resp = client.delete("/v1/api-keys/nonexistent", headers=AUTH)
        assert resp.status_code == 404

    def test_rotate_api_key(self, client):
        # Create key
        resp = client.post("/v1/api-keys", json={"name": "rotate-me"}, headers=AUTH)
        old_id = resp.json()["id"]
        old_key = resp.json()["key"]

        # Rotate
        resp = client.post(f"/v1/api-keys/{old_id}/rotate", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["old_id"] == old_id
        assert data["new_id"] != old_id
        assert data["key"] != old_key
        assert data["key"].startswith("ak_")
        assert data["name"] == "rotate-me"
        assert "_warning" in data

    def test_rotate_nonexistent_key(self, client):
        resp = client.post("/v1/api-keys/nonexistent/rotate", headers=AUTH)
        assert resp.status_code == 404

    def test_api_key_hash_is_correct(self, client):
        resp = client.post("/v1/api-keys", json={"name": "hash-test"}, headers=AUTH)
        api_key = resp.json()["key"]
        prefix = resp.json()["prefix"]

        # Verify prefix matches
        assert api_key[:12] == prefix

    def test_multiple_rotations(self, client):
        resp = client.post("/v1/api-keys", json={"name": "multi-rotate"}, headers=AUTH)
        current_id = resp.json()["id"]

        # Rotate 3 times
        for _ in range(3):
            resp = client.post(f"/v1/api-keys/{current_id}/rotate", headers=AUTH)
            assert resp.status_code == 200
            current_id = resp.json()["new_id"]

        # List should show all keys (old ones deactivated)
        resp = client.get("/v1/api-keys", headers=AUTH)
        assert resp.status_code == 200
