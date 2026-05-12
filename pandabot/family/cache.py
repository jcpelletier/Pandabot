import time
import logging
import threading

logger = logging.getLogger("panda-bot")

class FamilyCache:
    def __init__(self, sheet_reader, ttl_seconds=1800):
        self.sheet_reader = sheet_reader
        self.ttl_seconds = ttl_seconds
        self._cache = None
        self._last_refresh = 0
        self._lock = threading.Lock()

    def get_all_members(self):
        with self._lock:
            now = time.time()
            if self._cache is None or (now - self._last_refresh) > self.ttl_seconds:
                if self._cache is None:
                    logger.debug("Family cache miss: first call")
                else:
                    logger.debug("Family cache miss: TTL expired")
                self._refresh_no_lock()
            else:
                logger.debug("Family cache hit")
            return self._cache

    def _refresh_no_lock(self):
        self._cache = self.sheet_reader.get_all_members()
        self._last_refresh = time.time()
        logger.debug(f"Family cache refreshed: {len(self._cache)} members")

    def find_member(self, name):
        members = self.get_all_members()
        return self.sheet_reader.find_member(name, members=members)

    def search(self, query):
        members = self.get_all_members()
        return self.sheet_reader.search(query, members=members)

    def refresh(self):
        with self._lock:
            self._refresh_no_lock()

    def invalidate(self):
        with self._lock:
            self._cache = None
            self._last_refresh = 0
            logger.debug("Family cache invalidated")
