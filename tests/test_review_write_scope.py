"""Review write-path: tenant scoping + resume action.

Proven 2026-09-07: POST /review accepted NO tenant_id and acted on any
tenant's item; POST /promote accepted tenant_id but ignored it. Both are
fail-closed 404 on tenant mismatch now (same semantics as merge). The
resume action returns SNOOZED items to OPEN — previously a snoozed item
could only ever be cancelled, and no clock sweeper exists.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from aftergraph_work_intelligence.api import create_app
from aftergraph_work_intelligence.policy import PolicyStore, TenantPolicy


def _make_app(tmp_path, policy_store=None):
    return create_app(db_path=tmp_path / "scope.db", policy_store=policy_store)


def _ingest(client, tenant, text, external_id):
    r = client.post("/v1/observations", json={
        "tenant_id": tenant, "source": "conversation", "external_id": external_id, "text": text,
    })
    assert r.status_code in (200, 201, 202), r.text
    return r.json()["work_item"]["id"]


def _review(client, wid, tenant, action, actor="jonas", **extra):
    return client.post(f"/v1/work-items/{wid}/review", params={"tenant_id": tenant}, json={
        "action": action, "actor": actor, "reason": "scope-probe", **extra,
    })


def test_review_cross_tenant_is_404(tmp_path):
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        wid = _ingest(client, "alpha", "Vi skal købe parfumefri sæbe før mandag", "x-1")
        r = _review(client, wid, "beta", "approve")
        assert r.status_code == 404, r.text
        # same-tenant still works
        ok = _review(client, wid, "alpha", "approve")
        assert ok.status_code == 200, ok.text


def test_promote_cross_tenant_is_404(tmp_path):
    store = PolicyStore()
    store.put("alpha", TenantPolicy(allowed_sources={"conversation"}, allow_works=True))
    app = _make_app(tmp_path, policy_store=store)
    with TestClient(app) as client:
        wid = _ingest(client, "alpha", "Vi skal købe parfumefri sæbe før mandag", "x-2")
        assert _review(client, wid, "alpha", "approve").status_code == 200
        r = client.post(f"/v1/work-items/{wid}/promote", params={"tenant_id": "beta"}, json={"actor": "jonas"})
        assert r.status_code == 404, r.text
        ok = client.post(f"/v1/work-items/{wid}/promote", params={"tenant_id": "alpha"}, json={"actor": "jonas"})
        assert ok.status_code == 200, ok.text


def test_resume_returns_snoozed_to_open(tmp_path):
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        wid = _ingest(client, "renos", "Vi skal købe parfumefri sæbe før mandag", "x-3")
        snoozed = _review(client, wid, "renos", "snooze", resume_at="2030-01-01T00:00:00Z")
        assert snoozed.status_code == 200, snoozed.text
        assert snoozed.json()["status"] == "SNOOZED"
        resumed = _review(client, wid, "renos", "resume")
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["status"] == "OPEN"


def test_resume_from_open_is_400(tmp_path):
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        wid = _ingest(client, "renos", "Vi skal købe parfumefri sæbe før mandag", "x-4")
        r = _review(client, wid, "renos", "resume")
        assert r.status_code == 400, r.text


def test_allowed_actions_advertise_resume_on_snoozed(tmp_path):
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        wid = _ingest(client, "renos", "Vi skal købe parfumefri sæbe før mandag", "x-5")
        _review(client, wid, "renos", "snooze", resume_at="2030-01-01T00:00:00Z")
        r = client.get(f"/v1/work-items/{wid}/actions", params={"tenant_id": "renos"})
        assert r.status_code == 200, r.text
        assert set(r.json()["actions"]) == {"resume", "cancel"}
