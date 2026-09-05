"""Simple in-memory caching with TTL support."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class CacheEntry:
    value: Any
    created_at: float
    ttl: float
    access_count: int = 0

    @property
    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl


class Cache:
    """Thread-safe in-memory cache with TTL."""

    def __init__(self, default_ttl: float = 300.0, max_size: int = 1000):
        self.default_ttl = default_ttl
        self.max_size = max_size
        self._store: dict[str, CacheEntry] = {}
        self._lock = threading.Lock()
        self._stats = {"hits": 0, "misses": 0, "evictions": 0}

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._stats["misses"] += 1
                return None
            if entry.is_expired:
                del self._store[key]
                self._stats["misses"] += 1
                return None
            entry.access_count += 1
            self._stats["hits"] += 1
            return entry.value

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        with self._lock:
            if len(self._store) >= self.max_size:
                self._evict()
            self._store[key] = CacheEntry(
                value=value,
                created_at=time.time(),
                ttl=ttl or self.default_ttl,
            )

    def delete(self, key: str) -> bool:
        with self._lock:
            if key in self._store:
                del self._store[key]
                return True
            return False

    def clear(self) -> int:
        with self._lock:
            count = len(self._store)
            self._store.clear()
            return count

    def _evict(self) -> None:
        """Evict expired entries, then oldest by access count."""
        expired = [k for k, v in self._store.items() if v.is_expired]
        for k in expired:
            del self._store[k]
            self._stats["evictions"] += 1

        if len(self._store) >= self.max_size:
            oldest = min(self._store.items(), key=lambda x: x[1].access_count)
            del self._store[oldest[0]]
            self._stats["evictions"] += 1

    def stats(self) -> dict:
        with self._lock:
            return {
                **self._stats,
                "size": len(self._store),
                "max_size": self.max_size,
            }

    def cached(self, ttl: float | None = None) -> Callable:
        """Decorator for caching function results."""
        def decorator(func: Callable) -> Callable:
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                cache_key = f"{func.__name__}:{args}:{kwargs}"
                result = self.get(cache_key)
                if result is not None:
                    return result
                result = func(*args, **kwargs)
                self.set(cache_key, result, ttl)
                return result
            return wrapper
        return decorator


# Global cache instance
app_cache = Cache(default_ttl=300, max_size=5000)
