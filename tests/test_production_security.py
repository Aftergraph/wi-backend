from __future__ import annotations

from fastapi.testclient import TestClient

from aftergraph_work_intelligence.secure_api import create_app

FRONTEND_ORIGIN = "https://work-intelligence.rendetalje.dk"


def test_secure_factory_fails_closed_without_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("AFTERGRAPH_API_TOKEN", raising=False)
    app = create_app(db_path=tmp_path / "secure.db")
    with TestClient(app) as client:
        response = client.get("/v1/work-items", params={"tenant_id": "renos"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_secure_factory_rejects_before_payload_validation(tmp_path, monkeypatch):
    monkeypatch.delenv("AFTERGRAPH_API_TOKEN", raising=False)
    app = create_app(db_path=tmp_path / "secure.db")
    with TestClient(app) as client:
        response = client.post("/v1/observations", json={})
    assert response.status_code == 401


def test_health_remains_public_and_has_security_headers(tmp_path, monkeypatch):
    monkeypatch.delenv("AFTERGRAPH_API_TOKEN", raising=False)
    app = create_app(db_path=tmp_path / "secure.db")
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.headers["strict-transport-security"].startswith("max-age=")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"


def test_arbitrary_cors_origin_is_denied(tmp_path):
    app = create_app(db_path=tmp_path / "secure.db", api_token="master-token")
    with TestClient(app) as client:
        response = client.options(
            "/v1/observations",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers


def test_frontend_cors_origin_is_allowlisted(tmp_path):
    app = create_app(db_path=tmp_path / "secure.db", api_token="master-token")
    with TestClient(app) as client:
        response = client.options(
            "/v1/observations",
            headers={
                "Origin": FRONTEND_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == FRONTEND_ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"
    assert response.headers["vary"] == "Origin"


def test_api_key_requires_full_secret_not_only_stored_prefix(tmp_path):
    app = create_app(db_path=tmp_path / "secure.db", api_token="master-token")
    bearer = {"Authorization": "Bearer master-token"}
    with TestClient(app) as client:
        created = client.post("/v1/api-keys", json={"name": "production"}, headers=bearer)
        assert created.status_code == 201
        key = created.json()["key"]

        valid = client.get(
            "/v1/work-items",
            params={"tenant_id": "renos"},
            headers={"X-API-Key": key},
        )
        assert valid.status_code == 200

        replacement = "0" if key[12] != "0" else "1"
        forged_same_prefix = key[:12] + replacement + key[13:]
        assert forged_same_prefix[:12] == key[:12]
        forged = client.get(
            "/v1/work-items",
            params={"tenant_id": "renos"},
            headers={"X-API-Key": forged_same_prefix},
        )
        assert forged.status_code == 401


def test_github_webhook_fails_closed_when_hmac_secret_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("AFTERGRAPH_GITHUB_WEBHOOK_SECRET", raising=False)
    app = create_app(db_path=tmp_path / "secure.db", api_token="master-token")
    with TestClient(app) as client:
        response = client.post(
            "/v1/webhook/github",
            json={"ref": "refs/heads/main"},
            headers={"X-GitHub-Event": "push"},
        )
    assert response.status_code == 503


def test_webhook_hmac_passes_production_middleware(tmp_path):
    """Valid HMAC-SHA256 signature must pass ProductionSecurityMiddleware."""
    import hashlib
    import hmac as _hmac_mod
    import json as _json

    secret = "test-production-webhook-secret"
    body = {
        "request_id": "adr_webhooktest123",
        "tenant_id": "default",
        "repository": "Aftergraph/test",
        "ref": "refs/heads/main",
        "head_sha": "a" * 40,
        "event_key": "push",
        "capability": "dependency.patch.merge",
        "objective": "test",
        "impact_summary": "test",
        "evidence": [{"kind": "test"}],
        "tests_passed": True,
        "patch_release": True,
        "changed_files": [],
    }
    raw = _json.dumps(body, separators=(",", ":")).encode()
    sig = _hmac_mod.new(secret.encode(), raw, hashlib.sha256).hexdigest()

    app = create_app(
        db_path=tmp_path / "secure.db",
        api_token="master-token",
        webhook_secret=secret,
    )
    with TestClient(app) as client:
        resp = client.post(
            "/v1/autonomy/decisions/evaluate",
            content=raw,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": f"sha256={sig}",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["decision"] in ("auto_approve", "manual_review", "reject")


def test_webhook_hmac_rejected_with_wrong_secret(tmp_path):
    """Invalid HMAC signature must be rejected through production middleware."""
    import hashlib
    import hmac as _hmac_mod
    import json as _json

    body = {"test": "data"}
    raw = _json.dumps(body).encode()
    sig = _hmac_mod.new(b"wrong-secret", raw, hashlib.sha256).hexdigest()

    app = create_app(
        db_path=tmp_path / "secure.db",
        api_token="master-token",
        webhook_secret="correct-secret",
    )
    with TestClient(app) as client:
        resp = client.post(
            "/v1/autonomy/decisions/evaluate",
            content=raw,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": f"sha256={sig}",
            },
        )
    assert resp.status_code == 401
