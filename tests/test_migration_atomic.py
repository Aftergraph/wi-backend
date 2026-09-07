"""Migrations are atomic and fail closed (issue #26).

A migration version must never be recorded unless ALL of its statements
succeeded. A failing statement rolls the whole migration back, leaves the
recorded version unchanged, reports deterministically, and stops the run
so later versions never build on a half-applied schema.
"""

from __future__ import annotations

import sqlite3

import pytest

from aftergraph_work_intelligence.migrations import MigrationError, MigrationManager


def _manager(tmp_path):
    return MigrationManager(tmp_path / "atomic.db")


def test_failing_statement_not_recorded_and_rolled_back(tmp_path):
    manager = _manager(tmp_path)
    with pytest.raises(MigrationError):
        manager.apply_migration(
            1, "partial", "CREATE TABLE t (a TEXT); CREATE INDEX i ON missing_table(a);"
        )
    assert manager.get_current_version() == 0
    conn = sqlite3.connect(tmp_path / "atomic.db")
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.close()
    assert "t" not in tables


def test_run_stops_at_first_failure(tmp_path, monkeypatch):
    import aftergraph_work_intelligence.migrations as mig

    calls = []
    real_apply = MigrationManager.apply_migration

    def spy(self, version, name, sql):
        calls.append(version)
        return real_apply(self, version, name, sql)

    monkeypatch.setattr(MigrationManager, "apply_migration", spy)
    monkeypatch.setattr(
        mig,
        "MIGRATIONS",
        [
            (1, "good", "CREATE TABLE t (a TEXT);"),
            (2, "bad", "CREATE INDEX i ON missing_table(a);"),
            (3, "never", "CREATE TABLE u (b TEXT);"),
        ],
    )
    from aftergraph_work_intelligence.migrations import run_migrations

    result = run_migrations(db_path=tmp_path / "stop.db")
    assert result["ok"] is False
    assert result["failed_version"] == 2
    assert result["current_version"] == 1
    assert calls == [1, 2]
    conn = sqlite3.connect(tmp_path / "stop.db")
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.close()
    assert "u" not in tables


def test_v4_era_db_upgrades_through_v5(tmp_path):
    """Production-equivalent drill: v4-era DB (no idempotency_key) upgrades cleanly."""
    from aftergraph_work_intelligence.migrations import run_migrations
    from aftergraph_work_intelligence.store import SQLiteStore

    db = tmp_path / "old.db"
    SQLiteStore(db)
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE intake_transitions DROP COLUMN idempotency_key")
    conn.commit()  # no schema_migrations table yet: a v4-era DB never ran the runner
    conn.close()
    result = run_migrations(db_path=db)
    assert result["ok"] is True
    assert result["current_version"] == 5
    conn = sqlite3.connect(db)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(intake_transitions)").fetchall()}
    conn.close()
    assert "idempotency_key" in cols
