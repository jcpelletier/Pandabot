"""
Shared pytest fixtures and environment setup.

bot.py uses os.environ["DISCORD_TOKEN"] (raises KeyError if absent), so we
set dummy values here before any test module triggers the import.  tools.py
reads env at import time too — this file runs first so the dummies are in
place before either module is loaded.

bot.py also imports discord, aiohttp, and anthropic — packages that aren't
installed in a minimal dev/test environment.  pandabot_core.testing.stub_discord
handles the stubbing so we don't need to maintain the list here.
"""

import os
import sys
import pytest

# discord-bot root on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# pandabot-core — sibling directory; needed since bot.py now imports from pandabot_core
_PANDABOT_CORE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "pandabot-core",
)
if os.path.isdir(_PANDABOT_CORE):
    sys.path.insert(0, _PANDABOT_CORE)

# --- Required env vars ---
os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DISCORD_CHANNEL_ID", "123456789012345678")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

# --- Stub heavy runtime deps via pandabot_core.testing ---
from pandabot_core.testing import stub_discord
stub_discord()


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """
    Redirect scheduler DB to a fresh isolated temp directory for one test.

    Sets PANDABOT_DATA_DIR so pandabot_core.scheduler.cfg.db_path() resolves
    to tmp_path/scheduler.db. Also patches the legacy local scheduler.DB_PATH
    if present, for any remaining tests that use it directly.
    The temp directory (and the DB file) are cleaned up automatically by pytest.
    """
    monkeypatch.setenv("PANDABOT_DATA_DIR", str(tmp_path))
    from pandabot_core import scheduler as core_sched
    core_sched.init_db()
    db = str(tmp_path / "scheduler.db")

    # Patch legacy local scheduler if still imported by some tests
    try:
        import scheduler as local_sched
        monkeypatch.setattr(local_sched, "DB_PATH", db)
        local_sched.init_db()
    except (ImportError, AttributeError):
        pass

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
