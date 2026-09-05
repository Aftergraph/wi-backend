"""Tests for webhook delivery and API key persistence."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from aftergraph_work_intelligence.api import create_app


@pytest.fixture()
def client(tmp_path):
    db = tmp_path / "test.db"
    app = create_app(db_path=db, api_token="test-token")
    with TestClient(app) as c:
        yield c


AUTH = {"Authorization": "Bearer test-token"}


def _create(c, text="Vi skal købe 5 licenser til teamet hurtigt"):
    resp = c.post("/v1/observations", json={
        "tenant_id": "default",
        "source": "manual",
        "text": text,
    }, headers=AUTH)
    if resp.status_code == 201 and resp.json().get("work_item"):
        return resp.json()["work_item"]["id"]
    return None


class MockWebhookHandler(BaseHTTPRequestHandler):
    """Simple handler that records received webhooks."""
    received = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        sig = self.headers.get("X-Webhook-Signature", "")
        MockWebhookHandler.received.append({
            "path": self.path,
            "body": json.loads(body) if body else {},
            "signature": sig,
        })
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass  # Suppress logs


class TestWebhookDelivery:
    """Test webhook delivery on events."""

    def test_webhook_fired_on_ingest(self, client):
        MockWebhookHandler.received.clear()

        # Start mock server
        server = HTTPServer(("127.0.0.1", 0), MockWebhookHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()

        try:
            # Register webhook
            resp = client.post("/v1/webhooks", json={
                "url": f"http://127.0.0.1:{port}/hook",
                "events": ["observation.ingested"],
            }, headers=AUTH)
            assert resp.status_code == 201

            # Ingest observation
            resp = client.post("/v1/observations", json={
                "tenant_id": "default",
                "source": "manual",
                "text": "Vi skal købe licenser hurtigt",
            }, headers=AUTH)
            assert resp.status_code in (200, 201, 202)

            # Give webhook time to fire
            time.sleep(0.5)

            assert len(MockWebhookHandler.received) >= 1
            event = MockWebhookHandler.received[-1]["body"]
            assert event["event"] == "observation.ingested"
        finally:
            server.shutdown()

    def test_webhook_fired_on_review(self, client):
        MockWebhookHandler.received.clear()

        server = HTTPServer(("127.0.0.1", 0), MockWebhookHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()

        try:
            # Register webhook
            client.post("/v1/webhooks", json={
                "url": f"http://127.0.0.1:{port}/hook",
                "events": ["work_item.approve"],
            }, headers=AUTH)

            # Create work item
            item_id = _create(client)
            assert item_id

            # Review it
            resp = client.post(
                f"/v1/work-items/{item_id}/review?tenant_id=default",
                json={"action": "approve", "actor": "user-1"},
                headers=AUTH,
            )
            assert resp.status_code == 200

            time.sleep(0.5)
            assert len(MockWebhookHandler.received) >= 1
            assert MockWebhookHandler.received[-1]["body"]["event"] == "work_item.approve"
        finally:
            server.shutdown()

    def test_webhook_signature(self, client):
        MockWebhookHandler.received.clear()

        server = HTTPServer(("127.0.0.1", 0), MockWebhookHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()

        try:
            secret = "my-secret-key"
            client.post("/v1/webhooks", json={
                "url": f"http://127.0.0.1:{port}/hook",
                "events": ["observation.ingested"],
                "secret": secret,
            }, headers=AUTH)

            client.post("/v1/observations", json={
                "tenant_id": "default",
                "source": "manual",
                "text": "Vi skal købe licenser hurtigt",
            }, headers=AUTH)

            time.sleep(0.5)
            assert len(MockWebhookHandler.received) >= 1
            sig = MockWebhookHandler.received[-1]["signature"]
            assert sig.startswith("sha256=")

            # Verify signature
            body = json.dumps(MockWebhookHandler.received[-1]["body"]).encode()
            expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            assert sig == f"sha256={expected}"
        finally:
            server.shutdown()

    def test_webhook_not_fired_for_unsubscribed_event(self, client):
        MockWebhookHandler.received.clear()

        server = HTTPServer(("127.0.0.1", 0), MockWebhookHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()

        try:
            # Register webhook for different event
            client.post("/v1/webhooks", json={
                "url": f"http://127.0.0.1:{port}/hook",
                "events": ["work_item.promoted"],
            }, headers=AUTH)

            # Ingest observation (different event)
            client.post("/v1/observations", json={
                "tenant_id": "default",
                "source": "manual",
                "text": "Vi skal købe licenser hurtigt",
            }, headers=AUTH)

            time.sleep(0.5)
            assert len(MockWebhookHandler.received) == 0
        finally:
            server.shutdown()


class TestAPIKeyPersistence:
    """Test API key database persistence."""

    def test_create_and_list_api_keys(self, client):
        # Create key
        resp = client.post("/v1/api-keys", json={"name": "test-key"}, headers=AUTH)
        assert resp.status_code == 201
        key_id = resp.json()["id"]

        # List keys
        resp = client.get("/v1/api-keys", headers=AUTH)
        assert resp.status_code == 200
        keys = resp.json()["keys"]
        assert len(keys) >= 1
        assert any(k["id"] == key_id for k in keys)

    def test_deactivate_api_key(self, client):
        resp = client.post("/v1/api-keys", json={"name": "deactivate-me"}, headers=AUTH)
        assert resp.status_code == 201
        key_id = resp.json()["id"]

        # Deactivate
        resp = client.delete(f"/v1/api-keys/{key_id}", headers=AUTH)
        assert resp.status_code == 200

        # Verify deactivated
        resp = client.get("/v1/api-keys", headers=AUTH)
        keys = resp.json()["keys"]
        key = next(k for k in keys if k["id"] == key_id)
        assert key["active"] is False or key.get("active") == 0

    def test_deactivate_nonexistent_key(self, client):
        resp = client.delete(f"/v1/api-keys/{uuid.uuid4()}", headers=AUTH)
        assert resp.status_code == 404
