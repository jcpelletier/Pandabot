"""
Cache — in-memory TTL cache for family lookup results.

Prevents repeated Google Sheets API calls for the same query.
"""

from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger("panda-bot.family.cache")


class Cache:
    """Simple TTL cache for family query results."""

    def __init__(self, ttl_seconds: int = 300) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        """Return cached value or None if missing/expired."""
        entry = self._store.get(key)
        if entry is None:
            return None
        ts, value = entry
        if time.monotonic() - ts > self._ttl:
            del self._store[key]
            log.debug("Cache expired for key %s", key)
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        """Store *value* in the cache."""
        self._store[key] = (time.monotonic(), value)

    def invalidate(self, key: str | None = None) -> None:
        """Remove one entry or clear the entire cache."""
        if key is None:
            self._store.clear()
            log.info("Cache cleared")
        else:
            self._store.pop(key, None)
