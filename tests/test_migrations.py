"""Tests for database migrations."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from aftergraph_work_intelligence.api import create_app
from aftergraph_work_intelligence.migrations import MigrationManager, run_migrations


@pytest.fixture()
def client(tmp_path):
    db = tmp_path / "test.db"
    app = create_app(db_path=db, api_token="test-token")
    with TestClient(app) as c:
        yield c


AUTH = {"Authorization": "Bearer test-token"}


class TestMigrationManager:
    """Test MigrationManager directly."""

    def test_get_current_version_empty(self, tmp_path):
        db = tmp_path / "test.db"
        manager = MigrationManager(db)
        assert manager.get_current_version() == 0

    def test_apply_migration(self, tmp_path):
        db = tmp_path / "test.db"
        manager = MigrationManager(db)

        sql = "CREATE TABLE test_table (id INTEGER PRIMARY KEY, name TEXT);"
        result = manager.apply_migration(1, "create_test", sql)
        assert result is True

        # Verify table exists
        with sqlite3.connect(db) as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='test_table'"
            ).fetchall()
            assert len(tables) == 1

        # Verify migration recorded
        assert manager.get_current_version() == 1

    def test_idempotent_migration(self, tmp_path):
        db = tmp_path / "test.db"
        manager = MigrationManager(db)

        sql = "CREATE TABLE test (id INTEGER);"
        manager.apply_migration(1, "test", sql)
        result = manager.apply_migration(1, "test", sql)
        assert result is False

    def test_rollback_migration(self, tmp_path):
        db = tmp_path / "test.db"
        manager = MigrationManager(db)

        manager.apply_migration(1, "create", "CREATE TABLE test (id INTEGER);")
        rollback = "DROP TABLE test;"
        result = manager.rollback_migration(1, rollback)
        assert result is True
        assert manager.get_current_version() == 0

    def test_applied_migrations_list(self, tmp_path):
        db = tmp_path / "test.db"
        manager = MigrationManager(db)

        manager.apply_migration(1, "m1", "CREATE TABLE t1 (id INTEGER);")
        manager.apply_migration(2, "m2", "CREATE TABLE t2 (id INTEGER);")

        applied = manager.get_applied_migrations()
        assert len(applied) == 2
        assert applied[0]["name"] == "m1"
        assert applied[1]["name"] == "m2"


class TestRunMigrations:
    """Test run_migrations function."""

    def test_run_migrations(self, tmp_path):
        db = tmp_path / "test.db"
        result = run_migrations(db)
        assert result["current_version"] == 4
        assert result["total_applied"] == 4

    def test_run_migrations_idempotent(self, tmp_path):
        db = tmp_path / "test.db"
        run_migrations(db)
        result = run_migrations(db)
        assert result["total_applied"] == 0  # Nothing new applied


class TestMigrationAPI:
    """Test migration API endpoints."""

    def test_get_migration_status(self, client):
        resp = client.get("/v1/migrations", headers=AUTH)
        assert resp.status_code == 200
        assert "current_version" in resp.json()

    def test_run_migrations_endpoint(self, client):
        resp = client.post("/v1/migrations/run", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert "current_version" in data
        assert "migrations" in data
