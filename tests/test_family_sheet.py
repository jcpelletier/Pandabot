import pytest
import asyncio
import time
from unittest.mock import AsyncMock, patch
from pandabot.core.family_sheet.cache import get_family_data, refresh_family_data
import pandabot.core.family_sheet.cache as cache_module

@pytest.fixture(autouse=True)
def reset_cache():
    """Resets the cache before each test."""
    cache_module._cache_data = None
    cache_module._cache_timestamp = 0.0

@pytest.mark.asyncio
async def test_get_family_data_first_call():
    """Tests that the first call to get_family_data() reads from the sheet."""
    mock_data = [{"Name": "Alice", "Role": "Admin"}]

    with patch("pandabot.core.family_sheet.cache.read_family_sheet", new_callable=AsyncMock) as mock_read:
        mock_read.return_value = mock_data

        data = await get_family_data()

        assert data == mock_data
        mock_read.assert_called_once()

@pytest.mark.asyncio
async def test_get_family_data_cached_call():
    """Tests that subsequent calls within TTL return cached data."""
    mock_data = [{"Name": "Alice", "Role": "Admin"}]

    with patch("pandabot.core.family_sheet.cache.read_family_sheet", new_callable=AsyncMock) as mock_read:
        mock_read.return_value = mock_data

        # First call updates cache
        await get_family_data()

        # Second call should return from cache
        data = await get_family_data()

        assert data == mock_data
        mock_read.assert_called_once()

@pytest.mark.asyncio
async def test_get_family_data_expired_cache():
    """Tests that data is re-read after TTL expires."""
    mock_data = [{"Name": "Alice", "Role": "Admin"}]

    with patch("pandabot.core.family_sheet.cache.read_family_sheet", new_callable=AsyncMock) as mock_read:
        mock_read.return_value = mock_data

        # First call
        await get_family_data()

        # Force cache expiration
        cache_module._cache_timestamp = time.time() - (31 * 60)

        # Second call should re-read
        await get_family_data()

        assert mock_read.call_count == 2

@pytest.mark.asyncio
async def test_refresh_family_data():
    """Tests that refresh_family_data() forces a fresh read."""
    mock_data_1 = [{"Name": "Alice", "Role": "Admin"}]
    mock_data_2 = [{"Name": "Alice", "Role": "Superuser"}]

    with patch("pandabot.core.family_sheet.cache.read_family_sheet", new_callable=AsyncMock) as mock_read:
        mock_read.side_effect = [mock_data_1, mock_data_2]

        # Initial read
        await get_family_data()

        # Force refresh
        data = await refresh_family_data()

        assert data == mock_data_2
        assert mock_read.call_count == 2

@pytest.mark.asyncio
async def test_concurrent_access():
    """Tests that concurrent calls don't trigger multiple reads."""
    mock_data = [{"Name": "Alice", "Role": "Admin"}]

    with patch("pandabot.core.family_sheet.cache.read_family_sheet", new_callable=AsyncMock) as mock_read:
        # Simulate a delay in reading
        async def slow_read():
            await asyncio.sleep(0.1)
            return mock_data
        mock_read.side_effect = slow_read

        # Start multiple calls concurrently
        results = await asyncio.gather(
            get_family_data(),
            get_family_data(),
            get_family_data()
        )

        for r in results:
            assert r == mock_data

        # Should only be called once due to locking
        mock_read.assert_called_once()
