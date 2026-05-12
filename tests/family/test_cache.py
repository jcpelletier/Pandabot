import pytest
import time
from unittest.mock import MagicMock
from pandabot.family.cache import FamilyCache
from pandabot.family.sheet_reader import FamilySheetReader

class MockReader:
    def __init__(self):
        self.call_count = 0
        self.members = [{"first_name": "John"}]

    def get_all_members(self):
        self.call_count += 1
        return self.members

    def find_member(self, name, members=None):
        return FamilySheetReader.find_member(self, name, members=members)

    def search(self, query, members=None):
        return FamilySheetReader.search(self, query, members=members)

def test_cache_hit_and_miss():
    reader = MockReader()
    cache = FamilyCache(reader, ttl_seconds=1)

    # First call: miss, reads from reader
    members = cache.get_all_members()
    assert len(members) == 1
    assert reader.call_count == 1

    # Second call: hit, uses cache
    members = cache.get_all_members()
    assert reader.call_count == 1

    # Wait for TTL to expire
    time.sleep(1.1)
    members = cache.get_all_members()
    assert reader.call_count == 2

def test_cache_refresh():
    reader = MockReader()
    cache = FamilyCache(reader)

    cache.get_all_members()
    assert reader.call_count == 1

    cache.refresh()
    assert reader.call_count == 2

def test_cache_invalidate():
    reader = MockReader()
    cache = FamilyCache(reader)

    cache.get_all_members()
    assert reader.call_count == 1

    cache.invalidate()
    cache.get_all_members()
    assert reader.call_count == 2

def test_cache_delegation():
    # Verify that cache methods use the cached data
    reader = MockReader()
    reader.members = [
        {"first_name": "John", "last_name": "Doe", "discord_name": "jd", "notes": "pizza"},
        {"first_name": "Jane", "last_name": "Smith", "discord_name": "js", "notes": "pasta"}
    ]
    cache = FamilyCache(reader)

    # Prime the cache
    cache.get_all_members()
    assert reader.call_count == 1

    # Search should use cache, not call reader again
    results = cache.search("pizza")
    assert len(results) == 1
    assert reader.call_count == 1

    member = cache.find_member("Jane")
    assert member["first_name"] == "Jane"
    assert reader.call_count == 1
