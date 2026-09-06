"""E2E test: POST /v1/autonomy/decisions/evaluate through FastAPI."""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from contextlib import contextmanager

from fastapi.testclient import TestClient

from aftergraph_work_intelligence.api import create_app


WEBHOOK_SECRET = "test-webhook-secret-1234567890"


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


@contextmanager
def _client():
    app = create_app(db_path=":memory:", api_token="test-token")
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@contextmanager
def _client_with_webhook():
    app = create_app(db_path=":memory:", api_token="test-token", webhook_secret=WEBHOOK_SECRET)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _sign_payload(body: dict, secret: str = WEBHOOK_SECRET) -> str:
    raw = json.dumps(body, separators=(",", ":")).encode()
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


class TestAutonomyEndpoint:
    def test_low_risk_patch_auto_approved(self):
        with _client() as client:
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
        with _client() as client:
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
        with _client() as client:
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
        with _client() as client:
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
        with _client() as client:
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
        with _client() as client:
            body = _make_request(head_sha="not-a-sha")
            resp = client.post(
                "/v1/autonomy/decisions/evaluate",
                json=body,
                headers={"Authorization": "Bearer test-token"},
            )
            assert resp.status_code == 422

    def test_validation_rejects_empty_evidence(self):
        with _client() as client:
            body = _make_request(evidence=[])
            resp = client.post(
                "/v1/autonomy/decisions/evaluate",
                json=body,
                headers={"Authorization": "Bearer test-token"},
            )
            assert resp.status_code == 422

    def test_unauthenticated_returns_401(self):
        with _client() as client:
            body = _make_request()
            resp = client.post("/v1/autonomy/decisions/evaluate", json=body)
            assert resp.status_code == 401

    def test_blast_radius_computed_from_changed_files(self):
        with _client() as client:
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

    def test_auth_file_scan_blocks_lying_caller(self):
        """Fail-closed: changed_files with auth/ path blocks even if caller
        declares auth_or_secret_touched=false."""
        with _client() as client:
            body = _make_request(
                auth_or_secret_touched=False,
                changed_files=["src/auth/token_signer.py"],
            )
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

    def test_proxy_file_scan_blocks_without_caller_flag(self):
        """Fail-closed: proxy path change blocks even with no proxy flag set."""
        with _client() as client:
            body = _make_request(
                proxy_or_ssl_touched=False,
                changed_files=["Caddyfile"],
            )
            resp = client.post(
                "/v1/autonomy/decisions/evaluate",
                json=body,
                headers={"Authorization": "Bearer test-token"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["decision"] == "blocked"
            assert data["risk"]["level"] == "critical"

    def test_benign_changed_files_do_not_block(self):
        """Normal files in changed_files must not trip the trust-boundary scan."""
        with _client() as client:
            body = _make_request(
                auth_or_secret_touched=False,
                changed_files=["src/routes/health.py", "README.md"],
            )
            resp = client.post(
                "/v1/autonomy/decisions/evaluate",
                json=body,
                headers={"Authorization": "Bearer test-token"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["decision"] == "auto_approve"
            assert data["risk"]["level"] == "low"

    def test_blast_radius_includes_security_and_observability(self):
        """Expanded blast-radius map must recognize security/ and observability/."""
        with _client() as client:
            body = _make_request(
                changed_files=["src/security/encryption.py", "src/observability/metrics.py"],
            )
            resp = client.post(
                "/v1/autonomy/decisions/evaluate",
                json=body,
                headers={"Authorization": "Bearer test-token"},
            )
            assert resp.status_code == 200
            br = resp.json()["blast_radius"]
            assert "trust_boundary" in br["affected_surfaces"] or "encryption" in br["affected_surfaces"]
            assert "metrics" in br["affected_surfaces"] or "logging" in br["affected_surfaces"]

    def test_confidence_score_capped_at_80(self):
        """Confidence score must never exceed 80 even with max bonuses."""
        with _client() as client:
            body = _make_request(
                test_coverage_delta=30,
                author_permission_tier=20,
                critical_path_penalty=0,
                line_churn_penalty=0,
            )
            resp = client.post(
                "/v1/autonomy/decisions/evaluate",
                json=body,
                headers={"Authorization": "Bearer test-token"},
            )
            assert resp.status_code == 200
            confidence = resp.json()["confidence"]
            assert confidence["score"] <= 80

    def test_evaluation_persisted_to_audit_trail(self):
        """POST evaluate must append to the autonomy_decisions audit trail."""
        with _client() as client:
            body = _make_request()
            resp = client.post(
                "/v1/autonomy/decisions/evaluate",
                json=body,
                headers={"Authorization": "Bearer test-token"},
            )
            assert resp.status_code == 200

            history = client.get(
                "/v1/autonomy/decisions/history",
                headers={"Authorization": "Bearer test-token"},
            )
            assert history.status_code == 200
            data = history.json()
            assert data["schema"] == "aftergraph.autonomy-decision-history/1.0"
            assert data["count"] >= 1
            latest = data["decisions"][0]
            assert latest["request_id"] == body["request_id"]
            assert latest["tenant_id"] == "default"
            assert latest["capability"] == "dependency.patch.merge"
            assert latest["decision"] in ("auto_approve", "requires_human_signoff", "blocked")

    def test_history_filters_by_tenant_and_bounds_limit(self):
        """History must filter by tenant_id and clamp limit to max 200."""
        with _client() as client:
            other_body = _make_request(tenant_id="other-tenant")
            client.post(
                "/v1/autonomy/decisions/evaluate",
                json=other_body,
                headers={"Authorization": "Bearer test-token"},
            )

            filtered = client.get(
                "/v1/autonomy/decisions/history?tenant_id=other-tenant&limit=500",
                headers={"Authorization": "Bearer test-token"},
            )
            assert filtered.status_code == 200
            data = filtered.json()
            assert data["count"] == 1
            assert data["decisions"][0]["tenant_id"] == "other-tenant"

            empty = client.get(
                "/v1/autonomy/decisions/history?tenant_id=nonexistent",
                headers={"Authorization": "Bearer test-token"},
            )
            assert empty.json()["count"] == 0

    def test_history_requires_auth(self):
        with _client() as client:
            resp = client.get("/v1/autonomy/decisions/history")
            assert resp.status_code == 401

    def test_webhook_valid_signature_authenticates(self):
        """A valid HMAC-SHA256 signature must authenticate the request."""
        with _client_with_webhook() as client:
            body = _make_request()
            signature = _sign_payload(body)
            resp = client.post(
                "/v1/autonomy/decisions/evaluate",
                json=body,
                headers={"X-Hub-Signature-256": signature},
            )
            assert resp.status_code == 200
            assert resp.json()["decision"] == "auto_approve"

    def test_webhook_invalid_signature_rejected(self):
        """An invalid HMAC signature must be rejected with 401."""
        with _client_with_webhook() as client:
            body = _make_request()
            resp = client.post(
                "/v1/autonomy/decisions/evaluate",
                json=body,
                headers={"X-Hub-Signature-256": "sha256=deadbeef" * 4},
            )
            assert resp.status_code == 401

    def test_webhook_missing_signature_rejected(self):
        """Missing signature must be rejected when webhook secret is configured."""
        with _client_with_webhook() as client:
            body = _make_request()
            resp = client.post(
                "/v1/autonomy/decisions/evaluate",
                json=body,
            )
            assert resp.status_code == 401

    def test_webhook_tampered_body_rejected(self):
        """A valid signature over a different body must be rejected."""
        with _client_with_webhook() as client:
            body = _make_request()
            signature = _sign_payload(body)
            tampered = _make_request(request_id=f"adr_tampered{uuid.uuid4().hex[:10]}")
            resp = client.post(
                "/v1/autonomy/decisions/evaluate",
                json=tampered,
                headers={"X-Hub-Signature-256": signature},
            )
            assert resp.status_code == 401
