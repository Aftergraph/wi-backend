"""V2 metrics recorder.

Exposes per-tenant, per-source, per-action counts derived from the durable
store. The metrics are computed lazily on ``snapshot()``; there is no
incremental counter (the store is the source of truth).
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from typing import Any

from .store import SQLiteStore


class MetricsRecorder:
    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    @classmethod
    def from_store(cls, store: SQLiteStore) -> MetricsRecorder:
        return cls(store)

    def snapshot(self) -> dict[str, Any]:
        db: sqlite3.Connection = self._store._db  # internal but safe
        rows = db.execute(
            "SELECT tenant_id, source FROM intake_observations"
        ).fetchall()
        count_by_source: Counter[str] = Counter()
        count_by_tenant: Counter[str] = Counter()
        for row in rows:
            count_by_source[row["source"]] += 1
            count_by_tenant[row["tenant_id"]] += 1

        # Action counts:
        #   replayed  = number of rows in intake_replays
        #   created   = number of distinct work-items
        #   merged    = sum(max(0, observation_count - 1)) over work-items
        #   observed  = total observations - (replayed + created + merged)
        replay_rows = db.execute("SELECT COUNT(*) AS n FROM intake_replays").fetchone()
        replayed = int(replay_rows["n"]) if replay_rows else 0

        wi_rows = db.execute(
            "SELECT tenant_id, observation_count, status FROM intake_work_items"
        ).fetchall()
        open_work: Counter[str] = Counter()
        for row in wi_rows:
            if row["status"] not in ("DONE", "CANCELLED", "SNOOZED", "REJECTED"):
                open_work[row["tenant_id"]] += 1

        created = len(wi_rows)
        merged = sum(max(0, row["observation_count"] - 1) for row in wi_rows)

        total_observations = len(rows)
        explicit = replayed + created + merged
        observed = max(0, total_observations - explicit)
        count_by_action = Counter({
            "replayed": replayed,
            "created": created,
            "merged": merged,
            "observed": observed,
        })

        return {
            "count_by_action": dict(count_by_action),
            "count_by_source": dict(count_by_source),
            "count_by_tenant": dict(count_by_tenant),
            "open_work_items": dict(open_work),
            "total_observations": total_observations,
            "total_work_items": created,
        }


__all__ = ["MetricsRecorder"]