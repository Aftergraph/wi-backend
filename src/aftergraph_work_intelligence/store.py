from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import Observation, Publication, WorkCandidate, WorkItem

_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS intake_observations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    source TEXT NOT NULL,
    external_id TEXT,
    actor TEXT,
    text TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_intake_observation_external
ON intake_observations(tenant_id, source, external_id)
WHERE external_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS intake_work_items (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    status TEXT NOT NULL,
    priority TEXT NOT NULL,
    owner TEXT,
    due_hint TEXT,
    next_action TEXT NOT NULL,
    confidence REAL NOT NULL,
    canonical_key TEXT NOT NULL,
    canonical_tokens_json TEXT NOT NULL,
    observation_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intake_work_items_tenant_status
ON intake_work_items(tenant_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_intake_work_items_key
ON intake_work_items(tenant_id, canonical_key);

CREATE TABLE IF NOT EXISTS intake_work_item_observations (
    work_item_id TEXT NOT NULL REFERENCES intake_work_items(id) ON DELETE CASCADE,
    observation_id TEXT NOT NULL UNIQUE REFERENCES intake_observations(id) ON DELETE CASCADE,
    linked_at TEXT NOT NULL,
    PRIMARY KEY(work_item_id, observation_id)
);

CREATE TABLE IF NOT EXISTS intake_publications (
    id TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL REFERENCES intake_work_items(id) ON DELETE CASCADE,
    destination TEXT NOT NULL,
    external_id TEXT,
    response_json TEXT NOT NULL,
    published_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intake_publications_work
ON intake_publications(work_item_id, published_at DESC);

CREATE TABLE IF NOT EXISTS intake_replays (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intake_replays_tenant
ON intake_replays(tenant_id, at ASC);
CREATE INDEX IF NOT EXISTS idx_intake_replays_external
ON intake_replays(tenant_id, source, external_id);

CREATE TABLE IF NOT EXISTS intake_transitions (
    id TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL REFERENCES intake_work_items(id) ON DELETE CASCADE,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    resume_at TEXT,
    at TEXT NOT NULL,
    idempotency_key TEXT
);
CREATE INDEX IF NOT EXISTS idx_intake_transitions_work
ON intake_transitions(work_item_id, at ASC);
-- ponytail: idempotency index lives in migration v5, not here — the schema
-- executes before migrations on existing DBs, where the column may not exist yet.

CREATE TABLE IF NOT EXISTS tenant_policies (
    tenant_id TEXT PRIMARY KEY,
    allowed_sources_json TEXT NOT NULL DEFAULT '[]',
    auto_create_work_items INTEGER NOT NULL DEFAULT 1,
    max_work_items INTEGER NOT NULL DEFAULT 0,
    max_priority TEXT NOT NULL DEFAULT 'critical',
    dedupe_threshold REAL NOT NULL DEFAULT 0.72,
    allow_works INTEGER NOT NULL DEFAULT 0,
    allowed_destinations_json TEXT,
    require_approval_for_promotion INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    prefix TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_used_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_prefix
ON api_keys(prefix);
"""


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class SQLiteStore:
    """Durable store for observations, canonical work items, and publish receipts."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def get_tenant_policy(self, tenant_id: str) -> dict | None:
        """Return persisted policy for tenant, or None if not found."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM tenant_policies WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "tenant_id": row["tenant_id"],
                "allowed_sources": json.loads(row["allowed_sources_json"]),
                "auto_create_work_items": bool(row["auto_create_work_items"]),
                "max_work_items": row["max_work_items"],
                "max_priority": row["max_priority"],
                "dedupe_threshold": row["dedupe_threshold"],
                "allow_works": bool(row["allow_works"]),
                "allowed_destinations": json.loads(row["allowed_destinations_json"]) if row["allowed_destinations_json"] else None,
                "require_approval_for_promotion": bool(row["require_approval_for_promotion"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }

    def upsert_tenant_policy(
        self,
        tenant_id: str,
        allowed_sources: list[str],
        auto_create_work_items: bool,
        max_work_items: int,
        max_priority: str,
        dedupe_threshold: float,
        allow_works: bool,
        allowed_destinations: list[str] | None,
        require_approval_for_promotion: bool,
    ) -> None:
        """Insert or update a tenant policy."""
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._db.execute(
                """
                INSERT INTO tenant_policies
                (tenant_id, allowed_sources_json, auto_create_work_items,
                 max_work_items, max_priority, dedupe_threshold, allow_works,
                 allowed_destinations_json, require_approval_for_promotion,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id) DO UPDATE SET
                    allowed_sources_json=excluded.allowed_sources_json,
                    auto_create_work_items=excluded.auto_create_work_items,
                    max_work_items=excluded.max_work_items,
                    max_priority=excluded.max_priority,
                    dedupe_threshold=excluded.dedupe_threshold,
                    allow_works=excluded.allow_works,
                    allowed_destinations_json=excluded.allowed_destinations_json,
                    require_approval_for_promotion=excluded.require_approval_for_promotion,
                    updated_at=excluded.updated_at
                """,
                (
                    tenant_id,
                    json.dumps(list(allowed_sources)),
                    int(auto_create_work_items),
                    max_work_items,
                    max_priority,
                    dedupe_threshold,
                    int(allow_works),
                    json.dumps(list(allowed_destinations)) if allowed_destinations is not None else None,
                    int(require_approval_for_promotion),
                    now,
                    now,
                ),
            )

    def list_tenant_policies(self) -> list[dict]:
        """Return all persisted policies."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM tenant_policies ORDER BY tenant_id"
            ).fetchall()
            return [
                {
                    "tenant_id": row["tenant_id"],
                    "allowed_sources": json.loads(row["allowed_sources_json"]),
                    "auto_create_work_items": bool(row["auto_create_work_items"]),
                    "max_work_items": row["max_work_items"],
                    "max_priority": row["max_priority"],
                    "dedupe_threshold": row["dedupe_threshold"],
                    "allow_works": bool(row["allow_works"]),
                    "allowed_destinations": json.loads(row["allowed_destinations_json"]) if row["allowed_destinations_json"] else None,
                    "require_approval_for_promotion": bool(row["require_approval_for_promotion"]),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                for row in rows
            ]

    def delete_tenant_policy(self, tenant_id: str) -> bool:
        """Delete a tenant policy. Returns True if it existed."""
        with self._lock:
            cursor = self._db.execute(
                "DELETE FROM tenant_policies WHERE tenant_id = ?",
                (tenant_id,),
            )
            return cursor.rowcount > 0

    def create_api_key(self, key_id: str, name: str, key_hash: str, prefix: str) -> dict:
        """Create an API key record."""
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._db.execute(
                """INSERT INTO api_keys (id, name, key_hash, prefix, active, created_at)
                   VALUES (?, ?, ?, ?, 1, ?)""",
                (key_id, name, key_hash, prefix, now),
            )
        return {"id": key_id, "name": name, "prefix": prefix, "active": True, "created_at": now}

    def list_api_keys(self) -> list[dict]:
        """Return all API key records (without secrets)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, name, prefix, active, created_at, last_used_at FROM api_keys ORDER BY created_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def deactivate_api_key(self, key_id: str) -> bool:
        """Deactivate an API key. Returns True if it existed."""
        with self._lock:
            cursor = self._db.execute(
                "UPDATE api_keys SET active = 0 WHERE id = ?", (key_id,)
            )
            return cursor.rowcount > 0

    def validate_api_key(self, prefix: str) -> bool:
        """Check if an API key prefix is active."""
        with self._lock:
            row = self._db.execute(
                "SELECT active FROM api_keys WHERE prefix = ?", (prefix,)
            ).fetchone()
            if row is None or not row["active"]:
                return False
            self._db.execute(
                "UPDATE api_keys SET last_used_at = ? WHERE prefix = ?",
                (datetime.now(UTC).isoformat(), prefix),
            )
            return True

    def create_observation(self, observation: Observation) -> None:
        with self._lock:
            self._db.execute(
                """
                INSERT INTO intake_observations
                (id, tenant_id, source, external_id, actor, text, metadata_json, occurred_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.id,
                    observation.tenant_id,
                    observation.source,
                    observation.external_id,
                    observation.actor,
                    observation.text,
                    json.dumps(observation.metadata, ensure_ascii=False, sort_keys=True),
                    _iso(observation.occurred_at),
                    _iso(observation.created_at),
                ),
            )

    def get_observation(self, observation_id: str) -> Observation | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM intake_observations WHERE id = ?", (observation_id,)
            ).fetchone()
        return self._observation(row) if row else None

    def get_observation_by_external(self, tenant_id: str, source: str, external_id: str) -> Observation | None:
        with self._lock:
            row = self._db.execute(
                """
                SELECT * FROM intake_observations
                WHERE tenant_id = ? AND source = ? AND external_id = ?
                """,
                (tenant_id, source, external_id),
            ).fetchone()
        return self._observation(row) if row else None

    def create_work_item(self, item: WorkItem, observation_id: str) -> None:
        now = _iso(item.created_at)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    """
                    INSERT INTO intake_work_items
                    (id, tenant_id, title, summary, status, priority, owner, due_hint, next_action,
                     confidence, canonical_key, canonical_tokens_json, observation_count, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.id,
                        item.tenant_id,
                        item.title,
                        item.summary,
                        item.status,
                        item.priority,
                        item.owner,
                        item.due_hint,
                        item.next_action,
                        item.confidence,
                        item.canonical_key,
                        json.dumps(item.canonical_tokens, ensure_ascii=False),
                        item.observation_count,
                        now,
                        _iso(item.updated_at),
                    ),
                )
                self._db.execute(
                    """
                    INSERT INTO intake_work_item_observations(work_item_id, observation_id, linked_at)
                    VALUES (?, ?, ?)
                    """,
                    (item.id, observation_id, now),
                )
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def merge_work_item(self, item: WorkItem, candidate: WorkCandidate, observation_id: str, updated_at: datetime) -> WorkItem:
        priority_rank = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        priority = candidate.priority if priority_rank.get(candidate.priority, 1) > priority_rank.get(item.priority, 1) else item.priority
        owner = item.owner or candidate.owner
        due_hint = item.due_hint or candidate.due_hint
        confidence = max(item.confidence, candidate.confidence)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    """
                    UPDATE intake_work_items
                    SET priority = ?, owner = ?, due_hint = ?, next_action = ?, confidence = ?,
                        observation_count = observation_count + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (priority, owner, due_hint, candidate.next_action, confidence, _iso(updated_at), item.id),
                )
                self._db.execute(
                    """
                    INSERT INTO intake_work_item_observations(work_item_id, observation_id, linked_at)
                    VALUES (?, ?, ?)
                    """,
                    (item.id, observation_id, _iso(updated_at)),
                )
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        merged = self.get_work_item(item.id)
        assert merged is not None
        return merged

    def list_open_work_items(self, tenant_id: str, limit: int = 200) -> list[WorkItem]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT * FROM intake_work_items
                WHERE tenant_id = ? AND status NOT IN ('DONE', 'CANCELLED', 'SNOOZED', 'REJECTED')
                ORDER BY updated_at DESC LIMIT ?
                """,
                (tenant_id, max(1, min(limit, 1000))),
            ).fetchall()
        return [self._work_item(row) for row in rows]

    def count_open_work_items(self, tenant_id: str) -> int:
        with self._lock:
            row = self._db.execute(
                """
                SELECT COUNT(*) AS n FROM intake_work_items
                WHERE tenant_id = ? AND status NOT IN ('DONE', 'CANCELLED', 'SNOOZED', 'REJECTED')
                """,
                (tenant_id,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def get_work_item_by_canonical_key(self, tenant_id: str, canonical_key: str) -> WorkItem | None:
        with self._lock:
            row = self._db.execute(
                """
                SELECT * FROM intake_work_items
                WHERE tenant_id = ? AND canonical_key = ?
                """,
                (tenant_id, canonical_key),
            ).fetchone()
        return self._work_item(row) if row else None

    def list_work_items(self, tenant_id: str, limit: int = 100) -> list[WorkItem]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT * FROM intake_work_items
                WHERE tenant_id = ? ORDER BY updated_at DESC LIMIT ?
                """,
                (tenant_id, max(1, min(limit, 1000))),
            ).fetchall()
        return [self._work_item(row) for row in rows]

    def get_work_item(self, work_item_id: str, tenant_id: str | None = None) -> WorkItem | None:
        sql = "SELECT * FROM intake_work_items WHERE id = ?"
        params: tuple[Any, ...] = (work_item_id,)
        if tenant_id is not None:
            sql += " AND tenant_id = ?"
            params = (work_item_id, tenant_id)
        with self._lock:
            row = self._db.execute(sql, params).fetchone()
        return self._work_item(row) if row else None

    def get_work_item_for_observation(self, observation_id: str) -> WorkItem | None:
        with self._lock:
            row = self._db.execute(
                """
                SELECT w.* FROM intake_work_items w
                JOIN intake_work_item_observations l ON l.work_item_id = w.id
                WHERE l.observation_id = ?
                """,
                (observation_id,),
            ).fetchone()
        return self._work_item(row) if row else None

    def observations_for_work_item(self, work_item_id: str) -> list[Observation]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT o.* FROM intake_observations o
                JOIN intake_work_item_observations l ON l.observation_id = o.id
                WHERE l.work_item_id = ? ORDER BY o.occurred_at ASC
                """,
                (work_item_id,),
            ).fetchall()
        return [self._observation(row) for row in rows]

    def save_publication(self, publication: Publication) -> None:
        with self._lock:
            self._db.execute(
                """
                INSERT INTO intake_publications
                (id, work_item_id, destination, external_id, response_json, published_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    publication.id,
                    publication.work_item_id,
                    publication.destination,
                    publication.external_id,
                    json.dumps(publication.response, ensure_ascii=False, sort_keys=True),
                    _iso(publication.published_at),
                ),
            )

    def record_replay(self, tenant_id: str, source: str, external_id: str, observation_id: str, at) -> None:
        with self._lock:
            self._db.execute(
                """
                INSERT INTO intake_replays
                (id, tenant_id, source, external_id, observation_id, at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    f"replay_{uuid.uuid4().hex}",
                    tenant_id,
                    source,
                    external_id,
                    observation_id,
                    _iso(at),
                ),
            )

    def publications_for_work_item(self, work_item_id: str) -> list[Publication]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM intake_publications WHERE work_item_id = ? ORDER BY published_at ASC",
                (work_item_id,),
            ).fetchall()
        return [self._publication(row) for row in rows]

    def write_transition(self, transition, new_status: str, updated_at) -> None:
        # Local import to avoid circular import (transitions imports models).
        from .transitions import _iso as _t_iso  # type: ignore

        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    """
                    INSERT INTO intake_transitions
                    (id, work_item_id, from_state, to_state, actor, reason, resume_at, at, idempotency_key)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        transition.id,
                        transition.work_item_id,
                        transition.from_state,
                        transition.to_state,
                        transition.actor,
                        transition.reason,
                        _t_iso(transition.resume_at) if transition.resume_at is not None else None,
                        _t_iso(transition.at),
                        transition.idempotency_key,
                    ),
                )
                self._db.execute(
                    "UPDATE intake_work_items SET status = ?, updated_at = ? WHERE id = ?",
                    (new_status, _t_iso(updated_at), transition.work_item_id),
                )
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def list_transitions(self, work_item_id: str) -> list:
        from .transitions import Transition, _dt  # type: ignore

        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM intake_transitions WHERE work_item_id = ? ORDER BY at ASC",
                (work_item_id,),
            ).fetchall()
        result = []
        for row in rows:
            resume_at = _dt(row["resume_at"]) if row["resume_at"] else None
            keys = row.keys()
            result.append(
                Transition(
                    id=row["id"],
                    work_item_id=row["work_item_id"],
                    from_state=row["from_state"],
                    to_state=row["to_state"],
                    actor=row["actor"],
                    reason=row["reason"],
                    at=_dt(row["at"]),
                    resume_at=resume_at,
                    idempotency_key=row["idempotency_key"] if "idempotency_key" in keys else None,
                )
            )
        return result

    def find_transition_by_idempotency_key(self, idempotency_key: str) -> list:
        """Return merge/cancel transitions recorded under this key (cross-restart replay)."""
        from .transitions import Transition, _dt  # type: ignore

        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM intake_transitions WHERE idempotency_key = ? ORDER BY at ASC",
                (idempotency_key,),
            ).fetchall()
        result = []
        for row in rows:
            resume_at = _dt(row["resume_at"]) if row["resume_at"] else None
            keys = row.keys()
            result.append(
                Transition(
                    id=row["id"],
                    work_item_id=row["work_item_id"],
                    from_state=row["from_state"],
                    to_state=row["to_state"],
                    actor=row["actor"],
                    reason=row["reason"],
                    at=_dt(row["at"]),
                    resume_at=resume_at,
                    idempotency_key=row["idempotency_key"] if "idempotency_key" in keys else None,
                )
            )
        return result

    @staticmethod
    def _observation(row: sqlite3.Row) -> Observation:
        return Observation(
            id=row["id"],
            tenant_id=row["tenant_id"],
            source=row["source"],
            external_id=row["external_id"],
            actor=row["actor"],
            text=row["text"],
            metadata=json.loads(row["metadata_json"] or "{}"),
            occurred_at=_dt(row["occurred_at"]),
            created_at=_dt(row["created_at"]),
        )

    @staticmethod
    def _work_item(row: sqlite3.Row) -> WorkItem:
        return WorkItem(
            id=row["id"],
            tenant_id=row["tenant_id"],
            title=row["title"],
            summary=row["summary"],
            status=row["status"],
            priority=row["priority"],
            owner=row["owner"],
            due_hint=row["due_hint"],
            next_action=row["next_action"],
            confidence=float(row["confidence"]),
            canonical_key=row["canonical_key"],
            canonical_tokens=tuple(json.loads(row["canonical_tokens_json"] or "[]")),
            observation_count=int(row["observation_count"]),
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    @staticmethod
    def _publication(row: sqlite3.Row) -> Publication:
        return Publication(
            id=row["id"],
            work_item_id=row["work_item_id"],
            destination=row["destination"],
            external_id=row["external_id"],
            response=json.loads(row["response_json"] or "{}"),
            published_at=_dt(row["published_at"]),
        )
