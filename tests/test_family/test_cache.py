import time
import pytest
from unittest.mock import MagicMock, patch
from pandabot.family.cache import FamilyDataCache, CACHE_TTL

@pytest.fixture
def mock_reader():
    return MagicMock()

@pytest.fixture
def cache(mock_reader):
    return FamilyDataCache(mock_reader)

def test_first_call_fetches_from_reader(cache, mock_reader):
    mock_reader.get_all_rows.return_value = [{"Name": "Alice"}]

    data = cache.get_data()

    assert data == [{"Name": "Alice"}]
    mock_reader.get_all_rows.assert_called_once()

def test_second_call_returns_cached_data(cache, mock_reader):
    mock_reader.get_all_rows.return_value = [{"Name": "Alice"}]

    cache.get_data()
    data = cache.get_data()

    assert data == [{"Name": "Alice"}]
    mock_reader.get_all_rows.assert_called_once()

def test_expired_cache_refetches(cache, mock_reader):
    mock_reader.get_all_rows.return_value = [{"Name": "Alice"}]

    cache.get_data()

    with patch("time.time", return_value=time.time() + CACHE_TTL + 1):
        cache.get_data()

    assert mock_reader.get_all_rows.call_count == 2

def test_force_refresh_calls_reader_unconditionally(cache, mock_reader):
    mock_reader.get_all_rows.return_value = [{"Name": "Alice"}]

    cache.get_data()
    cache.force_refresh()

    assert mock_reader.get_all_rows.call_count == 2

def test_invalidate_clears_cache(cache, mock_reader):
    mock_reader.get_all_rows.return_value = [{"Name": "Alice"}]

    cache.get_data()
    cache.invalidate()
    cache.get_data()

    assert mock_reader.get_all_rows.call_count == 2
