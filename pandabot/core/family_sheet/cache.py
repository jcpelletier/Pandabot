import asyncio
import time
from typing import Optional
from .reader import read_family_sheet

# Cache configuration
CACHE_TTL_SECONDS = 30 * 60  # 30 minutes

# In-memory cache state
_cache_data: Optional[list[dict]] = None
_cache_timestamp: float = 0.0
_cache_lock = asyncio.Lock()

async def get_family_data() -> list[dict]:
    """
    Returns family data from the cache if available and fresh.
    Otherwise, reads from the Google Sheet and updates the cache.
    Thread-safe and task-safe.
    """
    async with _cache_lock:
        now = time.time()
        if _cache_data is not None and (now - _cache_timestamp) < CACHE_TTL_SECONDS:
            return _cache_data

        # Cache miss or expired
        return await _refresh_family_data_internal()

async def refresh_family_data() -> list[dict]:
    """
    Forcefully invalidates the cache and re-reads from the Google Sheet.
    """
    async with _cache_lock:
        return await _refresh_family_data_internal()

async def _refresh_family_data_internal() -> list[dict]:
    """
    Internal helper to read data and update cache.
    Should be called while holding _cache_lock.
    """
    data = await read_family_sheet()
    global _cache_data, _cache_timestamp
    _cache_data = data
    _cache_timestamp = time.time()
    return _cache_data
