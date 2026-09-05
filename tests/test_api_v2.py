"""TDD tests for the V2 API surface.

The V2 API adds, on top of V1:

- ``POST /v1/work-items/{id}/review``  (approve/reject/snooze/cancel)
- ``POST /v1/work-items/{id}/promote`` (explicit WORKS promotion)
- ``GET  /v1/metrics``                  (observability)
- ``GET  /v1/work-items/{id}/evidence`` (provenance envelope)

These tests use the in-process TestClient and a recording publisher, so they
exercise the real HTTP surface without external services.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from aftergraph_work_intelligence.api import create_app
from aftergraph_work_intelligence.policy import PolicyStore, TenantPolicy
from aftergraph_work_intelligence.publishers import Publisher, PublishReceipt


class RecordingPublisher(Publisher):
    def __init__(self):
        self.calls = []

    def publish(self, destination, work_item, observations):
        self.calls.append((destination, work_item.id))
        return PublishReceipt(destination=destination, external_id="ext-1", response={"ok": True})


def _make_app(tmp_path, policy_store=None, publisher=None):
    return create_app(
        db_path=tmp_path / "api.db",
        publisher=publisher or RecordingPublisher(),
        policy_store=policy_store,
    )


def _create_work(client, text="Vi skal sende kunden en bekræftelse før mandag"):
    r = client.post("/v1/observations", json={
        "tenant_id": "renos",
        "source": "conversation",
        "text": text,
    })
    assert r.status_code == 201, r.text
    return r.json()["work_item"]["id"]


def test_review_approve_endpoint(tmp_path):
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        wid = _create_work(client)
        r = client.post(f"/v1/work-items/{wid}/review", params={"tenant_id": "renos"}, json={
            "action": "approve", "actor": "jonas", "reason": "confirmed",
        })
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "APPROVED"


def test_review_reject_endpoint(tmp_path):
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        wid = _create_work(client)
        r = client.post(f"/v1/work-items/{wid}/review", params={"tenant_id": "renos"}, json={
            "action": "reject", "actor": "jonas", "reason": "not real",
        })
        assert r.status_code == 200
        assert r.json()["status"] == "REJECTED"


def test_review_requires_actor(tmp_path):
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        wid = _create_work(client)
        r = client.post(f"/v1/work-items/{wid}/review", params={"tenant_id": "renos"}, json={
            "action": "approve", "actor": "", "reason": "x",
        })
        # Pydantic rejects empty actor at validation (422).
        assert r.status_code in (400, 422)


def test_promote_endpoint_requires_policy(tmp_path):
    # Default policy: allow_works=False → promotion denied.
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        wid = _create_work(client)
        client.post(f"/v1/work-items/{wid}/review", params={"tenant_id": "renos"}, json={
            "action": "approve", "actor": "jonas",
        })
        r = client.post(f"/v1/work-items/{wid}/promote", params={"tenant_id": "renos"}, json={
            "actor": "jonas",
        })
        assert r.status_code == 403


def test_promote_endpoint_happy_path(tmp_path):
    policy_store = PolicyStore()
    policy_store.put("renos", TenantPolicy(allowed_sources={"conversation"}, allow_works=True))
    app = _make_app(tmp_path, policy_store=policy_store)
    with TestClient(app) as client:
        wid = _create_work(client)
        client.post(f"/v1/work-items/{wid}/review", params={"tenant_id": "renos"}, json={
            "action": "approve", "actor": "jonas",
        })
        r = client.post(f"/v1/work-items/{wid}/promote", params={"tenant_id": "renos"}, json={
            "actor": "jonas",
        })
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "PROMOTED_TO_WORKS"


def test_metrics_endpoint(tmp_path):
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        _create_work(client)
        r = client.get("/v1/metrics")
        assert r.status_code == 200
        body = r.json()
        assert body["count_by_action"]["created"] == 1
        assert body["count_by_tenant"]["renos"] == 1


def test_evidence_endpoint(tmp_path):
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        wid = _create_work(client)
        r = client.get(f"/v1/work-items/{wid}/evidence", params={"tenant_id": "renos"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["schema"] == "aftergraph.work-item-evidence/1.0"
        assert body["identity_chain"]["work_item_id"] == wid
        assert body["observations_count"] == 1
        assert "digest" in body


def test_evidence_endpoint_404_for_unknown_work_item(tmp_path):
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        r = client.get("/v1/work-items/wi_does_not_exist/evidence", params={"tenant_id": "renos"})
        assert r.status_code == 404