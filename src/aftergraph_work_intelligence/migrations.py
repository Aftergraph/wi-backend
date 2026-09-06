"""Simple database migration system."""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("aftergraph.work-intelligence.migrations")


class MigrationManager:
    """Simple migration manager for SQLite."""

    def __init__(self, db_path: Path | None = None, connection: sqlite3.Connection | None = None):
        self.db_path = db_path
        self._conn = connection
        self._ensure_migrations_table()

    def _ensure_migrations_table(self) -> None:
        assert self.db_path is not None or self._conn is not None, "migrations need a db_path or connection"
        conn = self._conn or sqlite3.connect(Path(str(self.db_path)))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
        """)
        conn.commit()
        if self._conn is None:
            conn.close()

    def get_current_version(self) -> int:
        conn = self._conn or sqlite3.connect(Path(str(self.db_path)))
        row = conn.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()
        if self._conn is None:
            conn.close()
        return row[0] if row[0] is not None else 0

    def get_applied_migrations(self) -> list[dict]:
        conn = self._conn or sqlite3.connect(Path(str(self.db_path)))
        rows = conn.execute(
            "SELECT version, name, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
        if self._conn is None:
            conn.close()
        return [{"version": r[0], "name": r[1], "applied_at": r[2]} for r in rows]

    def apply_migration(self, version: int, name: str, sql: str) -> bool:
        """Apply a migration if not already applied."""
        current = self.get_current_version()
        if version <= current:
            logger.debug(f"Migration {version} ({name}) already applied")
            return False

        conn = self._conn or sqlite3.connect(Path(str(self.db_path)))
        try:
            for statement in sql.split(";"):
                statement = statement.strip()
                if statement:
                    try:
                        conn.execute(statement)
                    except sqlite3.OperationalError as e:
                        logger.warning(f"Migration {version} ({name}) statement failed: {e}")
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, datetime.now(UTC).isoformat()),
            )
            conn.commit()
        finally:
            if self._conn is None:
                conn.close()
        logger.info(f"Applied migration {version}: {name}")
        return True

    def rollback_migration(self, version: int, rollback_sql: str) -> bool:
        """Rollback a migration."""
        with sqlite3.connect(Path(str(self.db_path))) as conn:
            current = conn.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
            if current != version:
                logger.warning(f"Cannot rollback {version}: current is {current}")
                return False
            for statement in rollback_sql.split(";"):
                statement = statement.strip()
                if statement:
                    try:
                        conn.execute(statement)
                    except sqlite3.OperationalError as e:
                        logger.warning(f"Rollback statement failed: {e}")
            conn.execute("DELETE FROM schema_migrations WHERE version = ?", (version,))
            conn.commit()
        logger.info(f"Rolled back migration {version}")
        return True


# Define migrations
MIGRATIONS = [
    (
        1,
        "add_audit_table",
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            event TEXT NOT NULL,
            actor TEXT NOT NULL,
            target TEXT NOT NULL,
            details_json TEXT DEFAULT '{}',
            ip_address TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_audit_event ON audit_log(event);
        CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor);
        CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
        """,
    ),
    (
        2,
        "add_work_item_priority_index",
        """
        CREATE INDEX IF NOT EXISTS idx_work_items_priority
        ON work_items(priority);
        """,
    ),
    (
        3,
        "add_tenant_policy_indexes",
        """
        CREATE INDEX IF NOT EXISTS idx_tenant_policies_tenant
        ON tenant_policies(tenant_id);
        """,
    ),
    (
        4,
        "add_autonomy_decisions_audit",
        """
        CREATE TABLE IF NOT EXISTS autonomy_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL UNIQUE,
            tenant_id TEXT NOT NULL,
            repository TEXT NOT NULL,
            ref TEXT NOT NULL,
            head_sha TEXT NOT NULL,
            event_key TEXT NOT NULL,
            capability TEXT NOT NULL,
            decision TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            confidence_score INTEGER NOT NULL,
            human_required INTEGER NOT NULL,
            approval_type TEXT NOT NULL,
            blast_radius_json TEXT,
            blockers_json TEXT,
            evaluated_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_autonomy_decisions_tenant
        ON autonomy_decisions(tenant_id);
        CREATE INDEX IF NOT EXISTS idx_autonomy_decisions_evaluated_at
        ON autonomy_decisions(evaluated_at);
        CREATE INDEX IF NOT EXISTS idx_autonomy_decisions_decision
        ON autonomy_decisions(decision);
        """,
    ),
    (
        5,
        "add_transition_idempotency_key",
        """
        ALTER TABLE intake_transitions ADD COLUMN idempotency_key TEXT;
        CREATE INDEX IF NOT EXISTS idx_intake_transitions_idempotency
        ON intake_transitions(idempotency_key);
        """,
    ),
]


def run_migrations(db_path: Path | None = None, connection: sqlite3.Connection | None = None) -> dict:
    """Run all pending migrations. Use connection for in-memory databases."""
    manager = MigrationManager(db_path=db_path, connection=connection)
    results = []

    for version, name, sql in MIGRATIONS:
        applied = manager.apply_migration(version, name, sql)
        results.append({"version": version, "name": name, "applied": applied})

    return {
        "current_version": manager.get_current_version(),
        "migrations": results,
        "total_applied": sum(1 for r in results if r["applied"]),
    }
