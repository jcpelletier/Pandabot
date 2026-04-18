"""
Layer 1: Pure helper function tests — no I/O, no mocking needed.

Covers:
  tools._csv_set, tools._csv_dict
  tools._fmt_duration, tools._fmt_timestamp, tools._fmt_bytes
  bot.split_message
"""

import os
import sys
import pytest

# Ensure project root is importable regardless of how pytest is invoked
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import tools


# ---------------------------------------------------------------------------
# _csv_set
# ---------------------------------------------------------------------------

class TestCsvSet:
    def test_normal(self):
        os.environ["_TEST_CSV"] = "alpha,beta,gamma"
        result = tools._csv_set("_TEST_CSV", "default")
        assert result == {"alpha", "beta", "gamma"}

    def test_whitespace_stripped(self):
        os.environ["_TEST_CSV"] = " a , b , c "
        result = tools._csv_set("_TEST_CSV", "default")
        assert result == {"a", "b", "c"}

    def test_empty_string_returns_empty_set(self):
        os.environ["_TEST_CSV"] = ""
        result = tools._csv_set("_TEST_CSV", "default")
        assert result == set()

    def test_single_value(self):
        os.environ["_TEST_CSV"] = "only"
        result = tools._csv_set("_TEST_CSV", "default")
        assert result == {"only"}

    def test_falls_back_to_default_when_env_absent(self, monkeypatch):
        monkeypatch.delenv("_TEST_CSV", raising=False)
        result = tools._csv_set("_TEST_CSV", "x,y")
        assert result == {"x", "y"}

    def test_trailing_comma_ignored(self):
        os.environ["_TEST_CSV"] = "a,b,"
        result = tools._csv_set("_TEST_CSV", "default")
        assert result == {"a", "b"}

    def teardown_method(self, method):
        os.environ.pop("_TEST_CSV", None)


# ---------------------------------------------------------------------------
# _csv_dict
# ---------------------------------------------------------------------------

class TestCsvDict:
    def test_normal(self):
        os.environ["_TEST_DICT"] = "rip-video:/var/log/rip.log,rip-cd:/var/log/cd.log"
        result = tools._csv_dict("_TEST_DICT", "")
        assert result == {
            "rip-video": "/var/log/rip.log",
            "rip-cd": "/var/log/cd.log",
        }

    def test_value_with_colon(self):
        # Partition uses the FIRST colon — paths with colons are fine
        os.environ["_TEST_DICT"] = "/dev/sda:SanDisk SSD PLUS (boot)"
        result = tools._csv_dict("_TEST_DICT", "")
        assert result == {"/dev/sda": "SanDisk SSD PLUS (boot)"}

    def test_empty_string_returns_empty_dict(self):
        os.environ["_TEST_DICT"] = ""
        result = tools._csv_dict("_TEST_DICT", "default")
        assert result == {}

    def test_falls_back_to_default(self, monkeypatch):
        monkeypatch.delenv("_TEST_DICT", raising=False)
        result = tools._csv_dict("_TEST_DICT", "a:1,b:2")
        assert result == {"a": "1", "b": "2"}

    def test_item_without_colon_skipped(self):
        os.environ["_TEST_DICT"] = "nocoion,key:val"
        result = tools._csv_dict("_TEST_DICT", "")
        assert "nocoion" not in result
        assert result.get("key") == "val"

    def teardown_method(self, method):
        os.environ.pop("_TEST_DICT", None)


# ---------------------------------------------------------------------------
# _fmt_duration
# ---------------------------------------------------------------------------

class TestFmtDuration:
    def test_zero(self):
        assert tools._fmt_duration(0) == "0s"

    def test_under_60_seconds(self):
        assert tools._fmt_duration(45_000) == "45s"

    def test_exactly_60_seconds(self):
        assert tools._fmt_duration(60_000) == "1m 0s"

    def test_minutes_and_seconds(self):
        assert tools._fmt_duration(90_000) == "1m 30s"

    def test_large_duration(self):
        # 2h 5m 3s = 7503s = 7503000ms
        assert tools._fmt_duration(7_503_000) == "125m 3s"

    def test_59_seconds(self):
        assert tools._fmt_duration(59_999) == "59s"


# ---------------------------------------------------------------------------
# _fmt_timestamp
# ---------------------------------------------------------------------------

class TestFmtTimestamp:
    def test_zero_returns_unknown(self):
        assert tools._fmt_timestamp(0) == "unknown"

    def test_valid_timestamp_is_formatted(self):
        # Just verify it returns a non-empty string in date format, not "unknown"
        import datetime
        # Use a known epoch ms: 2024-01-15 12:00:00 UTC = 1705320000000 ms
        result = tools._fmt_timestamp(1_705_320_000_000)
        assert result != "unknown"
        # Should contain a date-like pattern
        assert "-" in result or "/" in result


# ---------------------------------------------------------------------------
# _fmt_bytes
# ---------------------------------------------------------------------------

class TestFmtBytes:
    def test_bytes(self):
        assert tools._fmt_bytes(500) == "500.0 B"

    def test_kilobytes(self):
        assert tools._fmt_bytes(1024) == "1.0 KB"

    def test_megabytes(self):
        assert tools._fmt_bytes(1024 * 1024) == "1.0 MB"

    def test_gigabytes(self):
        assert tools._fmt_bytes(1024 ** 3) == "1.0 GB"

    def test_fractional_mb(self):
        result = tools._fmt_bytes(int(1.5 * 1024 * 1024))
        assert result == "1.5 MB"


# ---------------------------------------------------------------------------
# split_message (from bot.py)
# ---------------------------------------------------------------------------

class TestSplitMessage:
    @pytest.fixture(autouse=True)
    def import_bot(self):
        import bot
        self.split_message = bot.split_message

    def test_short_string_not_split(self):
        text = "Hello, world!"
        chunks = self.split_message(text)
        assert chunks == [text]

    def test_long_string_is_split(self):
        # Build a string > 1900 chars using many short lines
        lines = [f"line {i:04d}" for i in range(300)]
        text = "\n".join(lines)
        assert len(text) > 1900
        chunks = self.split_message(text)
        assert len(chunks) > 1
        # All chunks fit within the limit
        assert all(len(c) <= 1900 for c in chunks)

    def test_reassembled_equals_original(self):
        lines = [f"line {i:04d}" for i in range(300)]
        text = "\n".join(lines)
        chunks = self.split_message(text)
        assert "".join(chunks) == text

    def test_exactly_at_limit_not_split(self):
        import bot
        text = "x" * bot.DISCORD_MSG_LIMIT
        chunks = self.split_message(text)
        assert len(chunks) == 1

    def test_empty_string(self):
        chunks = self.split_message("")
        assert chunks == [""]
