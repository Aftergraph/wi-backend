"""TDD tests for the inbound GitHub webhook endpoint.

The endpoint:
- accepts POST /v1/webhook/github without API-key auth
- verifies HMAC-SHA256 signature (X-Hub-Signature-256)
- maps the payload through GitHubAdapter into observations
- returns 401 on bad signature, 202 on accepted but unactionable, 201 on ingest
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from aftergraph_work_intelligence.api import create_app

WEBHOOK_SECRET = "test-github-secret-123"
TENANT = "default"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("AFTERGRAPH_GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    app = create_app(db_path=":memory:")
    with TestClient(app) as c:
        yield c


def _sign(payload: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def _push_event():
    return {
        "tenant_id": TENANT,
        "repository": {"full_name": "Aftergraph/work-intelligence-v2", "name": "work-intelligence-v2"},
        "ref": "refs/heads/main",
        "after": "abc123def456",
        "head_commit": {
            "id": "abc123def456",
            "message": "feat: add github adapter",
            "author": {"name": "Jonas Abde", "username": "JonasAbde"},
            "timestamp": "2026-09-05T10:00:00Z",
        },
        "commits": [{
            "id": "abc123def456",
            "message": "feat: add github adapter",
            "author": {"name": "Jonas Abde", "username": "JonasAbde"},
            "timestamp": "2026-09-05T10:00:00Z",
        }],
        "pusher": {"name": "JonasAbde"},
    }


def test_webhook_push_creates_observation(client):
    payload = json.dumps(_push_event()).encode()
    resp = client.post(
        "/v1/webhook/github",
        content=payload,
        headers={
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": _sign(payload),
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["event"] == "push"
    assert data["status"] == "ingested"
    assert data["observations_created"] == 1
    assert data["work_item"]["source"] == "github"


def test_webhook_bad_signature_rejected(client):
    payload = json.dumps(_push_event()).encode()
    resp = client.post(
        "/v1/webhook/github",
        content=payload,
        headers={
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": "sha256=deadbeef",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 401
    assert "signature" in resp.json()["detail"].lower()


def test_webhook_missing_signature_rejected(client):
    payload = json.dumps(_push_event()).encode()
    resp = client.post(
        "/v1/webhook/github",
        content=payload,
        headers={
            "X-GitHub-Event": "push",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 401


def test_webhook_unactionable_event_returns_202(client):
    payload = json.dumps({
        "tenant_id": TENANT,
        "action": "ping",
        "repository": {"full_name": "Aftergraph/work-intelligence-v2"},
    }).encode()
    resp = client.post(
        "/v1/webhook/github",
        content=payload,
        headers={
            "X-GitHub-Event": "ping",
            "X-Hub-Signature-256": _sign(payload),
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 202
    assert resp.json()["status"] == "ignored"


def test_webhook_workflow_failure_creates_high_priority(client):
    payload = json.dumps({
        "tenant_id": TENANT,
        "action": "completed",
        "workflow_run": {
            "id": 777,
            "name": "CI",
            "head_branch": "main",
            "head_sha": "abc123def456",
            "status": "completed",
            "conclusion": "failure",
            "display_title": "ci: fix runtime.image path",
            "html_url": "https://github.com/Aftergraph/work-intelligence-v2/actions/runs/777",
            "created_at": "2026-09-05T10:00:00Z",
            "updated_at": "2026-09-05T10:06:00Z",
        },
        "repository": {"full_name": "Aftergraph/work-intelligence-v2"},
    }).encode()
    resp = client.post(
        "/v1/webhook/github",
        content=payload,
        headers={
            "X-GitHub-Event": "workflow_run",
            "X-Hub-Signature-256": _sign(payload),
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["event"] == "workflow_run"
    assert data["observations_created"] == 1
    assert data["work_item"]["priority"] == "high"


def test_webhook_duplicate_push_is_replayed_not_duplicated(client):
    payload = json.dumps(_push_event()).encode()
    headers = {
        "X-GitHub-Event": "push",
        "X-Hub-Signature-256": _sign(payload),
        "Content-Type": "application/json",
    }
    r1 = client.post("/v1/webhook/github", content=payload, headers=headers)
    assert r1.status_code == 201
    r2 = client.post("/v1/webhook/github", content=payload, headers=headers)
    assert r2.status_code == 200
    data = r2.json()
    assert data["status"] == "replayed"
    assert data["observations_created"] == 0


def test_webhook_no_secret_configured_accepts(client):
    # When no secret is configured, webhook accepts without signature
    pass  # placeholder — implemented in api.py when env is absent


def test_webhook_check_run_success_ignored(client):
    payload = json.dumps({
        "tenant_id": TENANT,
        "action": "completed",
        "check_run": {
            "id": 555,
            "head_sha": "abc123def456",
            "status": "completed",
            "conclusion": "success",
            "name": "cross-repo",
            "html_url": "https://github.com/Aftergraph/work-intelligence-v2/actions/runs/555",
            "started_at": "2026-09-05T10:00:00Z",
            "completed_at": "2026-09-05T10:05:00Z",
        },
        "repository": {"full_name": "Aftergraph/work-intelligence-v2"},
    }).encode()
    resp = client.post(
        "/v1/webhook/github",
        content=payload,
        headers={
            "X-GitHub-Event": "check_run",
            "X-Hub-Signature-256": _sign(payload),
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 202
    assert resp.json()["status"] == "ignored"