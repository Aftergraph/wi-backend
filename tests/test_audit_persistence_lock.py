"""Audit persistence must survive contention (regression: silent row loss).

Proven 2026-09-07: concurrent evaluations could hit SQLITE_BUSY on the
shared store connection and the best-effort except swallowed the row —
the evaluation returned 200 with no audit trail. The persist path now
serializes on store._lock (like the api-key path) over the pre-existing
5s busy_timeout.
"""

from __future__ import annotations

import sqlite3
import threading
import time

from fastapi.testclient import TestClient

from aftergraph_work_intelligence.api import (
    AutonomyEvaluateRequest,
    _persist_autonomy_decision,
    create_app,
)


def _evaluation(i: int) -> dict:
    return {
        "request_id": f"adr_{i:08d}",
        "subject": {},
        "risk": {"level": "low"},
        "confidence": {"score": 50},
        "human": {"required": False, "approval_type": "none"},
        "capability": "dependency.patch.merge",
        "decision": "auto_approve",
        "blast_radius": {},
        "blockers": [],
        "evaluated_at": "2026-09-07T00:00:00Z",
    }


def _payload(i: int) -> AutonomyEvaluateRequest:
    # Real request model (not a stub): missing fields fail loudly here
    # instead of being swallowed by _persist's best-effort except.
    return AutonomyEvaluateRequest(
        request_id=f"adr_{i:08d}",
        tenant_id="d",
        repository="Aftergraph/example",
        ref="refs/heads/main",
        head_sha="c" * 40,
        event_key="Aftergraph/example:main:" + "c" * 40,
        capability="dependency.patch.merge",
        objective="Apply a tested patch",
        impact_summary="Intent: apply. Risk: none.",
        evidence=[{"kind": "ci"}],
        tests_passed=True,
        patch_release=True,
        changed_files=["src/api.py"],
        author_permission_tier=20,
        test_coverage_delta=10,
        critical_path_penalty=0,
        auth_or_secret_touched=False,
        proxy_or_ssl_touched=False,
    )


def test_persist_serializes_on_store_lock(tmp_path):
    """While the store lock is held elsewhere, persist must wait, not skip."""
    app = create_app(db_path=tmp_path / "lock.db", api_token="t")
    with TestClient(app):
        store = app.state.store
        done = threading.Event()

        def worker():
            _persist_autonomy_decision(store, _payload(1), _evaluation(1))
            done.set()

        store._lock.acquire()
        try:
            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            assert done.wait(timeout=1.5) is False, "persist bypassed store._lock"
        finally:
            store._lock.release()
        assert done.wait(timeout=10) is True
        thread.join(timeout=10)
        rows = store._db.execute("SELECT COUNT(*) FROM autonomy_decisions").fetchone()[0]
        assert rows == 1


def test_persist_survives_brief_external_lock(tmp_path):
    """A short-lived foreign write lock must delay, not drop, the row."""
    db = tmp_path / "ext.db"
    app = create_app(db_path=db, api_token="t")
    with TestClient(app):
        store = app.state.store
        holder = sqlite3.connect(str(db), timeout=30, check_same_thread=False)
        holder.execute("BEGIN IMMEDIATE")
        try:
            def release():
                time.sleep(1)
                holder.execute("ROLLBACK")

            releaser = threading.Thread(target=release, daemon=True)
            releaser.start()
            _persist_autonomy_decision(store, _payload(2), _evaluation(2))
            releaser.join(timeout=10)
        finally:
            holder.close()
        rows = store._db.execute("SELECT COUNT(*) FROM autonomy_decisions").fetchone()[0]
        assert rows == 1
