"""Tests for GET /v1/work-items/{id}/execution-status (works readback).

Uses a real HTTP fake works-execution (stdlib http.server in a thread):
no mocks as final evidence, real round-trips for enroll + status fetch.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient

from aftergraph_work_intelligence.api import create_app
from aftergraph_work_intelligence.models import ObservationInput, Publication, utc_now
from aftergraph_work_intelligence.publishers import (
    WorksPublisher,
    build_publish_router,
)


class _FakeWorksHandler(BaseHTTPRequestHandler):
    enroll_calls = 0
    mode = "ok"  # ok | notfound | error | unauthorized_once
    served_ids: list = []

    def log_message(self, *args):  # quiet
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/v1/workers/enroll":
            type(self).enroll_calls += 1
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            self._send(200, {"token": f"tok-{type(self).enroll_calls}"})
        else:
            self._send(404, {})

    def do_GET(self):
        parts = self.path.split("/")
        work_id = parts[-1] if len(parts) >= 4 and parts[-2] == "works" else None
        if work_id is None:
            self._send(404, {})
            return
        type(self).served_ids.append(work_id)
        mode = type(self).mode
        if mode == "notfound":
            self._send(404, {"detail": "nope"})
        elif mode == "error":
            self._send(500, {"detail": "boom"})
        elif mode == "unauthorized_once" and len(type(self).served_ids) == 1:
            self._send(401, {"detail": "stale"})
        else:
            self._send(200, {"id": work_id, "state": "RUNNING", "progress": 0.5})


def _start_fake(mode="ok"):
    _FakeWorksHandler.mode = mode
    _FakeWorksHandler.enroll_calls = 0
    _FakeWorksHandler.served_ids = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeWorksHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _seed_item(app, tenant_id="acme"):
    # Seed through the service layer (no HTTP auth involved).
    result = app.state.service.ingest(ObservationInput(
        tenant_id=tenant_id,
        source="conversation",
        external_id="voice-9",
        text="Deploy the release before Friday",
    ))
    assert result.work_item is not None
    return result.work_item.id


def _save_works_publication(app, work_id, external_id):
    import uuid

    store = app.state.store
    store.save_publication(Publication(
        id=f"pub_{uuid.uuid4().hex}",
        work_item_id=work_id,
        destination="works",
        external_id=external_id,
        response={"id": external_id},
        published_at=utc_now(),
    ))


def test_execution_status_ok_and_prefix(tmp_path):
    server, thread = _start_fake()
    try:
        pub = WorksPublisher(base_url=f"http://127.0.0.1:{server.server_port}", enroll_secret="s3cret")
        app = create_app(db_path=tmp_path / "api.db", publisher=pub)
        with TestClient(app) as client:
            work_id = _seed_item(app)
            _save_works_publication(app, work_id, "works:work-abc")
            res = client.get(f"/v1/work-items/{work_id}/execution-status",
                             params={"tenant_id": "acme"})
            assert res.status_code == 200
            body = res.json()
            assert body["external_id"] == "works:work-abc"
            assert body["status"]["state"] == "RUNNING"
            assert _FakeWorksHandler.served_ids == ["work-abc"]
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_execution_status_via_router_and_reenroll(tmp_path):
    server, thread = _start_fake(mode="unauthorized_once")
    try:
        pub = build_publish_router({"works": WorksPublisher(
            base_url=f"http://127.0.0.1:{server.server_port}", enroll_secret="s3cret")})
        app = create_app(db_path=tmp_path / "api.db", publisher=pub)
        with TestClient(app) as client:
            work_id = _seed_item(app)
            _save_works_publication(app, work_id, "work-abc")
            res = client.get(f"/v1/work-items/{work_id}/execution-status",
                             params={"tenant_id": "acme"})
            assert res.status_code == 200
            assert res.json()["status"]["state"] == "RUNNING"
            assert _FakeWorksHandler.enroll_calls == 2
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_execution_status_not_found_cases(tmp_path):
    server, thread = _start_fake()
    try:
        pub = WorksPublisher(base_url=f"http://127.0.0.1:{server.server_port}", enroll_secret="s3cret")
        app = create_app(db_path=tmp_path / "api.db", publisher=pub)
        with TestClient(app) as client:
            res = client.get("/v1/work-items/wi_missing/execution-status",
                             params={"tenant_id": "acme"})
            assert res.status_code == 404
            work_id = _seed_item(app)
            res = client.get(f"/v1/work-items/{work_id}/execution-status",
                             params={"tenant_id": "acme"})
            assert res.status_code == 404
            assert "not published" in res.json()["detail"]
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_execution_status_upstream_errors(tmp_path):
    server, thread = _start_fake(mode="notfound")
    try:
        pub = WorksPublisher(base_url=f"http://127.0.0.1:{server.server_port}", enroll_secret="s3cret")
        app = create_app(db_path=tmp_path / "api.db", publisher=pub)
        with TestClient(app) as client:
            work_id = _seed_item(app)
            _save_works_publication(app, work_id, "work-gone")
            res = client.get(f"/v1/work-items/{work_id}/execution-status",
                             params={"tenant_id": "acme"})
            assert res.status_code == 404
    finally:
        server.shutdown()
        thread.join(timeout=2)
    server, thread = _start_fake(mode="error")
    try:
        pub = WorksPublisher(base_url=f"http://127.0.0.1:{server.server_port}", enroll_secret="s3cret")
        app = create_app(db_path=tmp_path / "api.db", publisher=pub)
        with TestClient(app) as client:
            work_id = _seed_item(app)
            _save_works_publication(app, work_id, "work-abc")
            res = client.get(f"/v1/work-items/{work_id}/execution-status",
                             params={"tenant_id": "acme"})
            assert res.status_code == 502
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_execution_status_unconfigured_503(tmp_path):
    router = build_publish_router({})
    app = create_app(db_path=tmp_path / "api.db", publisher=router)
    with TestClient(app) as client:
        work_id = _seed_item(app)
        _save_works_publication(app, work_id, "work-abc")
        res = client.get(f"/v1/work-items/{work_id}/execution-status",
                         params={"tenant_id": "acme"})
        assert res.status_code == 503
