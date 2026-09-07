"""Management-plane writes require the bearer master token.

Proven 2026-09-07: api_keys, webhooks, tenant policies, rate limits, cache
and migrations were mutable by ANY valid credential (api keys included) —
credential minting, exfiltration-URL registration, defense reconfiguration
and policy writes with no admin boundary. require_admin gates all of them;
the data plane (observations, work items, review, merge, publish, promote,
tasks, evaluate) is unaffected.
"""

from fastapi.testclient import TestClient

from aftergraph_work_intelligence.api import create_app

_TOKEN = "admin-plane-token"


def _client(tmp_path):
    return TestClient(create_app(db_path=tmp_path / "admin.db", api_token=_TOKEN))


def _bearer():
    return {"Authorization": f"Bearer {_TOKEN}"}


def _api_key_header(client):
    created = client.post("/v1/api-keys", json={"name": "plane-probe"}, headers=_bearer())
    assert created.status_code == 201, created.text
    return {"X-API-Key": created.json()["key"]}


def test_api_key_cannot_set_rate_limit(tmp_path):
    with _client(tmp_path) as client:
        key_headers = _api_key_header(client)
        denied = client.post("/v1/rate-limit", json={"key": "x", "limit": 100}, headers=key_headers)
        assert denied.status_code == 403, denied.text
        allowed = client.post("/v1/rate-limit", json={"key": "x", "limit": 100}, headers=_bearer())
        assert allowed.status_code == 200, allowed.text


def test_api_key_cannot_register_webhook(tmp_path):
    with _client(tmp_path) as client:
        key_headers = _api_key_header(client)
        denied = client.post(
            "/v1/webhooks",
            json={"url": "https://evil.example/hook", "events": ["work_item.merged"]},
            headers=key_headers,
        )
        assert denied.status_code == 403, denied.text
        allowed = client.post(
            "/v1/webhooks",
            json={"url": "https://ops.example/hook", "events": ["work_item.merged"]},
            headers=_bearer(),
        )
        assert allowed.status_code == 201, allowed.text


def test_api_key_cannot_mint_keys(tmp_path):
    with _client(tmp_path) as client:
        key_headers = _api_key_header(client)
        denied = client.post("/v1/api-keys", json={"name": "second"}, headers=key_headers)
        assert denied.status_code == 403, denied.text


def test_api_key_cannot_write_tenant_policy(tmp_path):
    with _client(tmp_path) as client:
        key_headers = _api_key_header(client)
        denied = client.post(
            "/v1/tenants/victim/policy", params={"allow_works": True}, headers=key_headers
        )
        assert denied.status_code == 403, denied.text
        allowed = client.post(
            "/v1/tenants/victim/policy", params={"allow_works": True}, headers=_bearer()
        )
        assert allowed.status_code == 200, allowed.text
