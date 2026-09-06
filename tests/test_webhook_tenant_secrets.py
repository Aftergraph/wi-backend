"""Per-tenant webhook HMAC secrets: resolution, precedence, cross-tenant rejection."""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid

from fastapi.testclient import TestClient

from aftergraph_work_intelligence.api import (
    any_tenant_webhook_secrets,
    create_app,
    resolve_webhook_secret,
)

GLOBAL_SECRET = "test-global-webhook-secret-1234567890"
TENANT_A_SECRET = "test-tenant-a-webhook-secret-0987654321"


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


def _sign(body: dict, secret: str) -> str:
    raw = json.dumps(body, separators=(",", ":")).encode()
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _post(client: TestClient, body: dict, secret: str):
    return client.post(
        "/v1/autonomy/decisions/evaluate",
        json=body,
        headers={"X-Hub-Signature-256": _sign(body, secret)},
    )


class TestTenantSecretResolution:
    def test_per_tenant_overrides_global(self, monkeypatch):
        monkeypatch.setenv("AFTERGRAPH_WEBHOOK_SECRET_TENANTA", TENANT_A_SECRET)
        assert resolve_webhook_secret("tenantA", GLOBAL_SECRET) == TENANT_A_SECRET
        assert resolve_webhook_secret("other", GLOBAL_SECRET) == GLOBAL_SECRET
        assert resolve_webhook_secret(None, GLOBAL_SECRET) == GLOBAL_SECRET
        assert resolve_webhook_secret("tenantA", None) == TENANT_A_SECRET
        assert resolve_webhook_secret("missing", None) is None

    def test_slug_normalization(self, monkeypatch):
        monkeypatch.setenv("AFTERGRAPH_WEBHOOK_SECRET_ACME_CORP", "s3cr3t")
        assert resolve_webhook_secret("acme-corp", None) == "s3cr3t"
        assert resolve_webhook_secret("acme_corp", None) == "s3cr3t"

    def test_any_tenant_secrets(self, monkeypatch):
        monkeypatch.delenv("AFTERGRAPH_WEBHOOK_SECRET_TENANTA", raising=False)
        assert any_tenant_webhook_secrets() is False
        monkeypatch.setenv("AFTERGRAPH_WEBHOOK_SECRET_TENANTA", "x")
        assert any_tenant_webhook_secrets() is True


class TestTenantWebhookAuth:
    def test_per_tenant_secret_auths_its_tenant(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AFTERGRAPH_WEBHOOK_SECRET_TENANTA", TENANT_A_SECRET)
        app = create_app(db_path=tmp_path / "t1.db", api_token="t")
        with TestClient(app, raise_server_exceptions=False) as client:
            body = _make_request(tenant_id="tenantA")
            assert _post(client, body, TENANT_A_SECRET).status_code == 200

    def test_tenant_a_key_rejected_for_tenant_b(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AFTERGRAPH_WEBHOOK_SECRET_TENANTA", TENANT_A_SECRET)
        app = create_app(db_path=tmp_path / "t2.db", api_token="t")
        with TestClient(app, raise_server_exceptions=False) as client:
            body = _make_request(tenant_id="tenantB")
            assert _post(client, body, TENANT_A_SECRET).status_code == 401

    def test_global_fallback_when_no_per_tenant(self, monkeypatch, tmp_path):
        app = create_app(db_path=tmp_path / "t3.db", api_token="t", webhook_secret=GLOBAL_SECRET)
        with TestClient(app, raise_server_exceptions=False) as client:
            body = _make_request(tenant_id="tenantB")
            assert _post(client, body, GLOBAL_SECRET).status_code == 200

    def test_per_tenant_takes_precedence_over_global(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AFTERGRAPH_WEBHOOK_SECRET_TENANTA", TENANT_A_SECRET)
        app = create_app(db_path=tmp_path / "t4.db", api_token="t", webhook_secret=GLOBAL_SECRET)
        with TestClient(app, raise_server_exceptions=False) as client:
            body = _make_request(tenant_id="tenantA")
            assert _post(client, body, GLOBAL_SECRET).status_code == 401
            assert _post(client, body, TENANT_A_SECRET).status_code == 200

    def test_wrong_secret_rejected(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AFTERGRAPH_WEBHOOK_SECRET_TENANTA", TENANT_A_SECRET)
        app = create_app(db_path=tmp_path / "t5.db", api_token="t")
        with TestClient(app, raise_server_exceptions=False) as client:
            body = _make_request(tenant_id="tenantA")
            assert _post(client, body, "wrong-secret").status_code == 401
