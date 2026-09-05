"""Tests for background task queue."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from aftergraph_work_intelligence.api import create_app
from aftergraph_work_intelligence.tasks import TaskQueue, TaskStatus


@pytest.fixture()
def client(tmp_path):
    db = tmp_path / "test.db"
    app = create_app(db_path=db, api_token="test-token")
    with TestClient(app) as c:
        yield c


AUTH = {"Authorization": "Bearer test-token"}


class TestTaskQueue:
    """Test the TaskQueue class directly."""

    def test_submit_and_complete(self):
        q = TaskQueue(max_workers=2)
        q.register("noop", lambda x: f"done:{x}")
        task = q.submit("noop", "hello")
        time.sleep(0.5)
        assert task.status == TaskStatus.COMPLETED
        assert task.result == "done:hello"
        q.shutdown()

    def test_submit_unknown_task(self):
        q = TaskQueue(max_workers=1)
        task = q.submit("nonexistent")
        time.sleep(0.5)
        assert task.status == TaskStatus.FAILED
        assert "No function registered" in task.error
        q.shutdown()

    def test_retry_on_failure(self):
        q = TaskQueue(max_workers=1)
        call_count = 0

        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("temporary")
            return "ok"

        q.register("flaky", flaky)
        task = q.submit("flaky")
        time.sleep(1.0)
        assert task.status == TaskStatus.COMPLETED
        assert task.result == "ok"
        assert task.retries >= 1
        q.shutdown()

    def test_permanent_failure_after_retries(self):
        q = TaskQueue(max_workers=1)

        def always_fail():
            raise RuntimeError("permanent")

        q.register("always_fail", always_fail)
        task = q.submit("always_fail")
        task.max_retries = 1
        time.sleep(1.0)
        assert task.status == TaskStatus.FAILED
        assert "permanent" in task.error
        q.shutdown()

    def test_list_tasks(self):
        q = TaskQueue(max_workers=1)
        q.register("noop", lambda: None)
        q.submit("noop")
        q.submit("noop")
        time.sleep(0.5)
        all_tasks = q.list_tasks()
        assert len(all_tasks) >= 2
        completed = q.list_tasks(status=TaskStatus.COMPLETED)
        assert len(completed) >= 2
        q.shutdown()

    def test_get_task(self):
        q = TaskQueue(max_workers=1)
        q.register("noop", lambda: "yes")
        task = q.submit("noop")
        time.sleep(0.5)
        found = q.get_task(task.id)
        assert found is not None
        assert found.status == TaskStatus.COMPLETED
        assert q.get_task("nonexistent-id") is None
        q.shutdown()

    def test_stats(self):
        q = TaskQueue(max_workers=2)
        q.register("noop", lambda: None)
        q.submit("noop")
        time.sleep(0.5)
        stats = q.get_stats()
        assert stats["completed"] >= 1
        assert stats["workers"] == 2
        q.shutdown()


class TestTaskQueueAPI:
    """Test task queue API endpoints."""

    def test_submit_task(self, client):
        resp = client.post("/v1/tasks/submit", json={
            "name": "process_observation",
            "args": ["obs-123", "tenant-a"],
        }, headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert "task_id" in data
        assert data["name"] == "process_observation"

    def test_get_task_status(self, client):
        # Submit a task
        resp = client.post("/v1/tasks/submit", json={
            "name": "process_observation",
            "args": ["obs-456", "tenant-b"],
        }, headers=AUTH)
        task_id = resp.json()["task_id"]

        time.sleep(0.5)

        # Check status
        resp = client.get(f"/v1/tasks/{task_id}", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == task_id
        assert data["status"] in ["pending", "running", "completed"]

    def test_get_task_not_found(self, client):
        resp = client.get("/v1/tasks/nonexistent-id", headers=AUTH)
        assert resp.status_code == 404

    def test_list_tasks(self, client):
        resp = client.get("/v1/tasks", headers=AUTH)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_task_stats(self, client):
        resp = client.get("/v1/tasks/stats", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert "submitted" in data
        assert "completed" in data
        assert "workers" in data

    def test_submit_and_wait_for_completion(self, client):
        resp = client.post("/v1/tasks/submit", json={
            "name": "process_observation",
            "args": ["obs-789", "tenant-c"],
        }, headers=AUTH)
        task_id = resp.json()["task_id"]

        # Wait for completion
        for _ in range(10):
            resp = client.get(f"/v1/tasks/{task_id}", headers=AUTH)
            if resp.json()["status"] == "completed":
                break
            time.sleep(0.2)

        assert resp.json()["status"] == "completed"
        assert resp.json()["result"] is not None
