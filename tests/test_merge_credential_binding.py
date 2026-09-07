"""Merge evidence binds the CLAIMED actor to the AUTHENTICATED credential.

`actor` in a merge request is caller-supplied free text. The evidence must
therefore also record which credential actually authorized the call, so
spoofed actor strings are visible instead of hiding in the audit trail:
bearer master token, api_key:<key_id>, or webhook:<tenant>.
"""

from fastapi.testclient import TestClient

from aftergraph_work_intelligence.api import create_app

_TOKEN = "binding-test-token"


def _client(tmp_path):
    return TestClient(create_app(db_path=tmp_path / "binding.db", api_token=_TOKEN))


def _ingest(client, headers, tenant, text, external_id):
    response = client.post(
        "/v1/observations",
        json={"tenant_id": tenant, "source": "conversation", "external_id": external_id, "text": text},
        headers=headers,
    )
    assert response.status_code in (200, 201, 202), response.text
    return response.json()["work_item"]["id"]


def _merge(client, headers, source_id, tenant, target_id, actor):
    return client.post(
        f"/v1/work-items/{source_id}/merge",
        params={"tenant_id": tenant},
        json={"actor": actor, "target_work_item_id": target_id},
        headers=headers,
    )


def test_bearer_merge_records_bearer_credential(tmp_path):
    with _client(tmp_path) as client:
        headers = {"Authorization": f"Bearer {_TOKEN}"}
        canonical = _ingest(client, headers, "renos", "Vi skal købe parfumefri sæbe før mandag", "k-1")
        duplicate = _ingest(client, headers, "renos", "Vi skal bestille nye håndklæder til omklædningen", "k-2")
        response = _merge(client, headers, duplicate, "renos", canonical, actor="someone-else")
        assert response.status_code == 200, response.text
        evidence = response.json()["evidence"]
        assert evidence["actor"] == "someone-else"
        assert evidence["authenticated_credential"] == "bearer"


def test_api_key_merge_records_key_id(tmp_path):
    with _client(tmp_path) as client:
        bearer = {"Authorization": f"Bearer {_TOKEN}"}
        created = client.post("/v1/api-keys", json={"name": "binding-probe"}, headers=bearer)
        assert created.status_code == 201, created.text
        key_id = created.json()["id"]
        api_key = created.json()["key"]
        headers = {"X-API-Key": api_key}
        canonical = _ingest(client, headers, "renos", "Vi skal købe parfumefri sæbe før mandag", "k-3")
        duplicate = _ingest(client, headers, "renos", "Vi skal bestille nye håndklæder til omklædningen", "k-4")
        response = _merge(client, headers, duplicate, "renos", canonical, actor="ghost")
        assert response.status_code == 200, response.text
        evidence = response.json()["evidence"]
        assert evidence["actor"] == "ghost"
        assert evidence["authenticated_credential"] == f"api_key:{key_id}"
