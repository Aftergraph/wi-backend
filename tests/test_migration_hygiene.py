"""Migration hygiene: fresh databases must migrate silently.

Proven 2026-09-07: migration 2 referenced a table that never existed
(`work_items` vs `intake_work_items`), so it warned on 100% of databases
without ever taking effect; migration 5's ALTER warns on every fresh DB
whose base schema already carries the column. Both are noise that hides
real failures — a failing statement must mean something.
"""

from __future__ import annotations

import logging
import sqlite3

from aftergraph_work_intelligence.migrations import MigrationManager


def _fresh_manager(tmp_path):
    return MigrationManager(tmp_path / "hygiene.db")


def test_fresh_boot_emits_no_migration_warnings(tmp_path, caplog):
    from fastapi.testclient import TestClient

    from aftergraph_work_intelligence.api import create_app

    with caplog.at_level(logging.WARNING, logger="aftergraph.work-intelligence.migrations"):
        app = create_app(db_path=tmp_path / "hygiene.db", api_token="test-token")
        with TestClient(app):
            pass
    assert [r for r in caplog.records if "statement failed" in r.message] == []


def test_add_column_guard_skips_existing_silently(tmp_path, caplog):
    db = tmp_path / "guard.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (a TEXT)")
    conn.commit()
    conn.close()
    manager = MigrationManager(db)
    with caplog.at_level(logging.WARNING, logger="aftergraph.work-intelligence.migrations"):
        assert manager.apply_migration(1, "add_cols", "ALTER TABLE t ADD COLUMN a TEXT; ALTER TABLE t ADD COLUMN b TEXT;") is True
    assert [r for r in caplog.records if "statement failed" in r.message] == []
    cols = [row[1] for row in sqlite3.connect(db).execute("PRAGMA table_info(t)").fetchall()]
    assert "b" in cols
