"""Webhook-HMAC authentication is scoped to the read-only evaluator.

Before the path gate, any request carrying an (even junk) X-Hub-Signature-256
header was accepted as ``webhook_pending`` on ALL 51 auth-dependent endpoints,
and only the evaluator ever verified the signature — a fail-open bypass that
required no secret knowledge. These tests lock the fix: junk headers 401
everywhere except the evaluator path, where the signature is still verified.
"""

from fastapi.testclient import TestClient

from aftergraph_work_intelligence.api import create_app as create_core_app
from aftergraph_work_intelligence.secure_api import create_app as create_secure_app

_TOKEN = "scope-test-token"
_SECRET = "scope-test-secret"
_JUNK = {"X-Hub-Signature-256": "junk-not-a-signature"}


def _core_client(tmp_path):
    return TestClient(
        create_core_app(db_path=tmp_path / "scope.db", api_token=_TOKEN, webhook_secret=_SECRET)
    )


def _secure_client(tmp_path):
    return TestClient(
        create_secure_app(db_path=tmp_path / "scope-secure.db", api_token=_TOKEN, webhook_secret=_SECRET)
    )


def _bearer():
    return {"Authorization": f"Bearer {_TOKEN}"}


def _ingest(client, tenant, text, external_id):
    response = client.post(
        "/v1/observations",
        json={"tenant_id": tenant, "source": "conversation", "external_id": external_id, "text": text},
        headers=_bearer(),
    )
    assert response.status_code in (200, 201, 202), response.text
    return response.json()["work_item"]["id"]


def test_junk_webhook_header_cannot_read_core(tmp_path):
    with _core_client(tmp_path) as client:
        response = client.get("/v1/work-items", params={"tenant_id": "renos"}, headers=_JUNK)
        assert response.status_code == 401, response.text


def test_junk_webhook_header_cannot_merge_core(tmp_path):
    with _core_client(tmp_path) as client:
        canonical = _ingest(client, "renos", "Vi skal købe parfumefri sæbe før mandag", "s-1")
        duplicate = _ingest(client, "renos", "Vi skal bestille nye håndklæder til omklædningen", "s-2")
        response = client.post(
            f"/v1/work-items/{duplicate}/merge",
            params={"tenant_id": "renos"},
            json={"actor": "attacker", "target_work_item_id": canonical},
            headers=_JUNK,
        )
        assert response.status_code == 401, response.text
        # legit bearer flow still works
        legit = client.post(
            f"/v1/work-items/{duplicate}/merge",
            params={"tenant_id": "renos"},
            json={"actor": "merge-test", "target_work_item_id": canonical},
            headers=_bearer(),
        )
        assert legit.status_code == 200, legit.text


def test_junk_webhook_header_cannot_merge_secure(tmp_path):
    with _secure_client(tmp_path) as client:
        canonical = _ingest(client, "renos", "Vi skal købe parfumefri sæbe før mandag", "s-3")
        duplicate = _ingest(client, "renos", "Vi skal bestille nye håndklæder til omklædningen", "s-4")
        response = client.post(
            f"/v1/work-items/{duplicate}/merge",
            params={"tenant_id": "renos"},
            json={"actor": "attacker", "target_work_item_id": canonical},
            headers=_JUNK,
        )
        assert response.status_code == 401, response.text
