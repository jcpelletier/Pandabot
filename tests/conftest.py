"""
Shared pytest fixtures and environment setup.

bot.py uses os.environ["DISCORD_TOKEN"] (raises KeyError if absent), so we
set dummy values here before any test module triggers the import.  tools.py
reads env at import time too — this file runs first so the dummies are in
place before either module is loaded.

bot.py also imports discord, aiohttp, and anthropic — packages that aren't
installed in a minimal dev/test environment.  We stub them in sys.modules so
bot.py can be imported for pure-function tests (split_message, etc.) without
needing the full runtime stack.
"""

import os
import sys
import pytest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# --- Required env vars ---
os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DISCORD_CHANNEL_ID", "123456789012345678")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

# --- Stub heavy runtime deps so bot.py can be imported without the full stack ---
_STUB_MODULES = [
    "discord", "discord.ext", "discord.ext.commands",
    "aiohttp",
    "anthropic",
]
for _mod in _STUB_MODULES:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """
    Redirect scheduler.DB_PATH to a fresh isolated temp file for one test.

    Covers both direct scheduler calls and manage_schedule in tools.py —
    both do `import scheduler` at call time so they pick up the patched path.
    The temp directory (and the DB file) are cleaned up automatically by pytest.
    """
    import scheduler
    db = str(tmp_path / "test_scheduler.db")
    monkeypatch.setattr(scheduler, "DB_PATH", db)
    scheduler.init_db()
    yield db


@pytest.fixture(autouse=False)
def reset_tools_flags(monkeypatch):
    """
    Fixture that resets tools module-level flags and constants back to their
    defaults after each test that modifies them.

    Usage:
        def test_something(reset_tools_flags, monkeypatch):
            monkeypatch.setattr(tools, "ENABLE_JENKINS", False)
            defs = tools._build_tool_definitions()
            ...

    The monkeypatch undo happens automatically after the test.
    """
    import tools
    yield
    # monkeypatch handles teardown automatically; this fixture just documents intent
