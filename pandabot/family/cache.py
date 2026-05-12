import time
import threading
import logging

logger = logging.getLogger("panda-bot")

CACHE_TTL = 1800  # 30 minutes in seconds

class FamilyDataCache:
    def __init__(self, reader):
        self._reader = reader
        self._cache: list[dict] | None = None
        self._last_fetch: float | None = None
        self._lock = threading.Lock()

    def get_data(self) -> list[dict]:
        """Thread-safe method to get cached data or fetch if expired."""
        with self._lock:
            now = time.time()
            if self._cache is not None and self._last_fetch is not None:
                if now - self._last_fetch < CACHE_TTL:
                    logger.debug("Family data cache hit")
                    return self._cache

            logger.info("Family data cache miss or expired, fetching from Google Sheets")
            self._cache = self._reader.get_all_rows()
            self._last_fetch = now
            return self._cache

    def force_refresh(self) -> list[dict]:
        """Unconditionally refreshes the cache."""
        with self._lock:
            logger.info("Forcing refresh of family data cache")
            self._cache = self._reader.get_all_rows()
            self._last_fetch = time.time()
            return self._cache

    def invalidate(self):
        """Clears the cache."""
        with self._lock:
            logger.info("Invalidating family data cache")
            self._cache = None
            self._last_fetch = None
