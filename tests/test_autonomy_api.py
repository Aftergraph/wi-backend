"""E2E test: POST /v1/autonomy/decisions/evaluate through FastAPI."""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from aftergraph_work_intelligence.api import create_app


def _make_request(**overrides: object) -> dict:
    body = {
        "request_id": f"adr_{uuid.uuid4().hex[:16]}",
        "tenant_id": "default",
        "repository": "Aftergraph/example",
        "ref": "refs/heads/main",
        "head_sha": "a" * 40,
        "event_key": "Aftergraph/example:main:" + "a" * 40,
        "capability": "dependency.patch.merge",
        "objective": "Merge a patch dependency update",
        "impact_summary": "Intent: apply a tested patch. Risk: an exported signature could change.",
        "evidence": [{"kind": "ci", "source": "github", "observed_at": "2026-09-06T12:00:00Z", "reference": "run:123"}],
        "tests_passed": True,
        "patch_release": True,
        "test_coverage_delta": 30,
        "author_permission_tier": 20,
    }
    body.update(overrides)
    return body


def _client() -> TestClient:
    app = create_app(db_path=":memory:", api_token="test-token")
    return TestClient(app)


class TestAutonomyEndpoint:
    def test_low_risk_patch_auto_approved(self):
        client = _client()
        body = _make_request()
        resp = client.post(
            "/v1/autonomy/decisions/evaluate",
            json=body,
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["decision"] == "auto_approve"
        assert data["risk"]["level"] == "low"
        assert data["human_action"]["required"] is False
        assert data["authority"]["execution_state"] == "not_executed"
        assert data["schema"] == "aftergraph.autonomy-decision/1.0"

    def test_auth_secret_change_is_blocked(self):
        client = _client()
        body = _make_request(auth_or_secret_touched=True)
        resp = client.post(
            "/v1/autonomy/decisions/evaluate",
            json=body,
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["decision"] == "blocked"
        assert data["risk"]["level"] == "critical"
        assert data["human_action"]["required"] is True

    def test_proxy_change_is_blocked(self):
        client = _client()
        body = _make_request(proxy_or_ssl_touched=True)
        resp = client.post(
            "/v1/autonomy/decisions/evaluate",
            json=body,
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["decision"] == "blocked"
        assert data["risk"]["level"] == "critical"

    def test_canary_drift_prepares_rollback(self):
        client = _client()
        body = _make_request(
            capability="deployment.rollback.prepare",
            canary_error_rate=0.006,
        )
        resp = client.post(
            "/v1/autonomy/decisions/evaluate",
            json=body,
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["decision"] == "prepare_rollback"
        assert data["human_action"]["required"] is True

    def test_ci_retry_auto_eligible(self):
        client = _client()
        body = _make_request(
            capability="ci.check.retry",
            patch_release=False,
            transient_ci_error=True,
        )
        resp = client.post(
            "/v1/autonomy/decisions/evaluate",
            json=body,
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["decision"] == "auto_retry"
        assert data["human_action"]["required"] is False

    def test_validation_rejects_bad_sha(self):
        client = _client()
        body = _make_request(head_sha="not-a-sha")
        resp = client.post(
            "/v1/autonomy/decisions/evaluate",
            json=body,
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 422

    def test_validation_rejects_empty_evidence(self):
        client = _client()
        body = _make_request(evidence=[])
        resp = client.post(
            "/v1/autonomy/decisions/evaluate",
            json=body,
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 422

    def test_unauthenticated_returns_401(self):
        client = _client()
        body = _make_request()
        resp = client.post("/v1/autonomy/decisions/evaluate", json=body)
        assert resp.status_code == 401

    def test_blast_radius_computed_from_changed_files(self):
        client = _client()
        body = _make_request(
            changed_files=["src/routes/api.py", "src/policy/engine.py", "tests/test_api.py"],
        )
        resp = client.post(
            "/v1/autonomy/decisions/evaluate",
            json=body,
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        br = resp.json()["blast_radius"]
        assert br["changed_files"] == 3
        assert "api_surface" in br["affected_surfaces"] or "request_handlers" in br["affected_surfaces"]
        assert br["affected_surface_count"] >= 1
