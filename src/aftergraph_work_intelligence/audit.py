"""Audit trail for security-critical operations."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class AuditEntry:
    timestamp: str
    event: str
    actor: str
    target: str
    details: dict[str, Any] = field(default_factory=dict)
    ip_address: str | None = None


class AuditLog:
    """Thread-safe in-memory audit log for critical operations."""

    def __init__(self, max_entries: int = 10000):
        self._entries: list[AuditEntry] = []
        self._lock = threading.Lock()
        self._max_entries = max_entries

    def record(
        self,
        event: str,
        actor: str,
        target: str,
        details: dict[str, Any] | None = None,
        ip_address: str | None = None,
    ) -> AuditEntry:
        entry = AuditEntry(
            timestamp=datetime.now(UTC).isoformat(),
            event=event,
            actor=actor,
            target=target,
            details=details or {},
            ip_address=ip_address,
        )
        with self._lock:
            self._entries.append(entry)
            # Trim old entries
            if len(self._entries) > self._max_entries:
                self._entries = self._entries[-self._max_entries:]
        return entry

    def query(
        self,
        event: str | None = None,
        actor: str | None = None,
        target: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        with self._lock:
            entries = list(self._entries)

        if event:
            entries = [e for e in entries if e.event == event]
        if actor:
            entries = [e for e in entries if e.actor == actor]
        if target:
            entries = [e for e in entries if target in e.target]

        return [
            {
                "timestamp": e.timestamp,
                "event": e.event,
                "actor": e.actor,
                "target": e.target,
                "details": e.details,
                "ip_address": e.ip_address,
            }
            for e in entries[-limit:]
        ]

    def count(self, event: str | None = None) -> int:
        with self._lock:
            if event:
                return sum(1 for e in self._entries if e.event == event)
            return len(self._entries)
