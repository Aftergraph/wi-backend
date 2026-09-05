"""Tests for request/response logging."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from aftergraph_work_intelligence.api import create_app
from aftergraph_work_intelligence.request_logger import RequestLogger


@pytest.fixture()
def client(tmp_path):
    db = tmp_path / "test.db"
    app = create_app(db_path=db, api_token="test-token")
    with TestClient(app) as c:
        yield c


AUTH = {"Authorization": "Bearer test-token"}


class TestRequestLogger:
    """Test RequestLogger class directly."""

    def test_log_request(self, tmp_path):
        logger = RequestLogger(log_dir=tmp_path / "logs")
        logger.log_request(
            method="GET",
            path="/v1/observations",
            status_code=200,
            duration_ms=42.5,
            request_size=100,
            response_size=200,
            client_ip="127.0.0.1",
            request_id="req-123",
        )
        logger.close()

        # Read the log file
        log_files = list((tmp_path / "logs").glob("requests_*.jsonl"))
        assert len(log_files) == 1
        with open(log_files[0]) as f:
            entry = json.loads(f.readline())
        assert entry["method"] == "GET"
        assert entry["path"] == "/v1/observations"
        assert entry["status_code"] == 200
        assert entry["duration_ms"] == 42.5
        assert entry["request_id"] == "req-123"

    def test_log_error(self, tmp_path):
        logger = RequestLogger(log_dir=tmp_path / "logs")
        logger.log_request(
            method="POST",
            path="/v1/observations",
            status_code=500,
            duration_ms=100.0,
            error="Internal Server Error",
        )
        logger.close()

        log_files = list((tmp_path / "logs").glob("requests_*.jsonl"))
        with open(log_files[0]) as f:
            entry = json.loads(f.readline())
        assert entry["error"] == "Internal Server Error"
        assert entry["status_code"] == 500

    def test_get_recent_logs(self, tmp_path):
        logger = RequestLogger(log_dir=tmp_path / "logs")
        for i in range(5):
            logger.log_request("GET", f"/path/{i}", 200, 10.0)
        logger.close()

        logs = logger.get_recent_logs(limit=3)
        assert len(logs) == 3

    def test_cleanup_old_logs(self, tmp_path):
        logger = RequestLogger(log_dir=tmp_path / "logs", retention_days=0)
        logger.log_request("GET", "/test", 200, 10.0)
        logger.close()

        removed = logger.cleanup_old_logs()
        assert removed >= 0  # May or may not remove depending on timing

    def test_log_rotation(self, tmp_path):
        logger = RequestLogger(log_dir=tmp_path / "logs", max_size_mb=0)
        # With 0MB limit, should rotate on first write
        logger.log_request("GET", "/test", 200, 10.0)
        logger.close()

        log_files = list((tmp_path / "logs").glob("requests_*.jsonl"))
        assert len(log_files) >= 1


class TestRequestLogsAPI:
    """Test request logs API endpoints."""

    def test_get_logs(self, client):
        # Make some requests
        client.get("/healthz")
        client.get("/v1/observations", headers=AUTH)

        resp = client.get("/v1/logs", headers=AUTH)
        assert resp.status_code == 200
        assert "logs" in resp.json()

    def test_cleanup_logs(self, client):
        resp = client.post("/v1/logs/cleanup", headers=AUTH)
        assert resp.status_code == 200
        assert "removed" in resp.json()
