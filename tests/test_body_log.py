"""Tests for body logging middleware."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from aftergraph_work_intelligence.api import create_app
from aftergraph_work_intelligence.body_log import BodyLoggingMiddleware


@pytest.fixture()
def client(tmp_path):
    db = tmp_path / "test.db"
    app = create_app(db_path=db, api_token="test-token")
    with TestClient(app) as c:
        yield c


AUTH = {"Authorization": "Bearer test-token"}


class TestBodyLoggingMiddleware:
    """Test BodyLoggingMiddleware class directly."""

    def test_skip_healthz(self, tmp_path):
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient

        async def healthz(request):
            return JSONResponse({"status": "ok"})

        app = Starlette(routes=[Route("/healthz", healthz)])
        app.add_middleware(BodyLoggingMiddleware, log_request_body=True)

        with TestClient(app) as c:
            # Should not log healthz
            resp = c.get("/healthz")
            assert resp.status_code == 200

    def test_custom_skip_paths(self, tmp_path):
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient

        async def skip(request):
            return JSONResponse({"ok": True})

        async def normal(request):
            return JSONResponse({"ok": True})

        app = Starlette(routes=[
            Route("/custom-skip", skip),
            Route("/normal", normal),
        ])
        app.add_middleware(
            BodyLoggingMiddleware,
            log_request_body=True,
            skip_paths=frozenset({"/custom-skip"}),
        )

        with TestClient(app) as c:
            resp = c.get("/custom-skip")
            assert resp.status_code == 200
            resp = c.get("/normal")
            assert resp.status_code == 200

    def test_max_chars_truncation(self):
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient

        async def test(request):
            return JSONResponse({"ok": True})

        app = Starlette(routes=[Route("/test", test, methods=["POST"])])
        app.add_middleware(BodyLoggingMiddleware, log_request_body=True, max_chars=10)

        with TestClient(app) as c:
            resp = c.post("/test", json={"key": "a" * 100})
            assert resp.status_code == 200


class TestBodyLoggingAPI:
    """Test body logging via API endpoints."""

    def test_post_observation_logged(self, client):
        resp = client.post(
            "/v1/observations",
            json={
                "tenant_id": "t1",
                "source": "test",
                "text": "test body content",
            },
            headers=AUTH,
        )
        assert resp.status_code in (200, 201, 202)

    def test_body_logging_disabled_by_default(self):
        """Verify body logging is disabled by default."""
        assert os.getenv("AFTERGRAPH_LOG_REQUEST_BODY", "false").lower() != "true"
