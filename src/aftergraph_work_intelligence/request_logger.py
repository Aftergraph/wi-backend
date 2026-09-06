"""Request/response logging to file for debugging."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO


class RequestLogger:
    """Logs request/response pairs to JSONL file."""

    def __init__(self, log_dir: Path, max_size_mb: int = 100, retention_days: int = 7):
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.max_size_mb = max_size_mb
        self.retention_days = retention_days
        self._lock = threading.Lock()
        self._current_file: TextIO | None = None
        self._current_size = 0
        self._rotate_log()

    def _rotate_log(self) -> None:
        """Rotate to a new log file."""
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        log_path = self.log_dir / f"requests_{timestamp}.jsonl"
        # SIM115-ok: the file handle is intentionally kept open across
        # requests; ownership is tracked by _current_file and closed in close().
        self._current_file = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
        self._current_size = 0

    def _check_rotate(self) -> None:
        """Check if we need to rotate the log file."""
        if self._current_size >= self.max_size_mb * 1024 * 1024:
            if self._current_file is not None:
                self._current_file.close()
            self._rotate_log()

    def log_request(
        self,
        method: str,
        path: str,
        status_code: int,
        duration_ms: float,
        request_size: int = 0,
        response_size: int = 0,
        client_ip: str | None = None,
        request_id: str | None = None,
        error: str | None = None,
    ) -> None:
        """Log a request/response pair."""
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "method": method,
            "path": path,
            "status_code": status_code,
            "duration_ms": round(duration_ms, 2),
            "request_size": request_size,
            "response_size": response_size,
        }
        if client_ip:
            entry["client_ip"] = client_ip
        if request_id:
            entry["request_id"] = request_id
        if error:
            entry["error"] = error

        with self._lock:
            try:
                if self._current_file is None:
                    self._rotate_log()
                log_file = self._current_file
                assert log_file is not None
                log_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
                log_file.flush()
                self._current_size += len(json.dumps(entry))
                self._check_rotate()
            except Exception:
                pass

    def close(self) -> None:
        """Close the current log file."""
        if self._current_file:
            self._current_file.close()

    def get_recent_logs(self, limit: int = 100) -> list[dict]:
        """Read recent log entries."""
        logs = []
        try:
            log_files = sorted(self.log_dir.glob("requests_*.jsonl"), reverse=True)
            for log_file in log_files[:1]:
                with open(log_file, encoding="utf-8") as f:
                    lines = f.readlines()
                    for line in reversed(lines[-limit:]):
                        try:
                            logs.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except Exception:
            pass
        return logs[:limit]

    def cleanup_old_logs(self) -> int:
        """Remove log files older than retention period."""
        cutoff = time.time() - (self.retention_days * 86400)
        removed = 0
        for log_file in self.log_dir.glob("requests_*.jsonl"):
            if log_file.stat().st_mtime < cutoff:
                log_file.unlink()
                removed += 1
        return removed
