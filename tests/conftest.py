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
from unittest.mock import MagicMock

# --- Mock pandabot_core if missing ---
try:
    import pandabot_core
except ImportError:
    # Create a mock pandabot_core package
    pandabot_core = MagicMock()
    sys.modules["pandabot_core"] = pandabot_core
    sys.modules["pandabot_core.llm"] = pandabot_core.llm
    sys.modules["pandabot_core.llm.provider"] = pandabot_core.llm.provider
    sys.modules["pandabot_core.llm.usage"] = pandabot_core.llm.usage
    sys.modules["pandabot_core.llm.loop"] = pandabot_core.llm.loop
    sys.modules["pandabot_core.telemetry"] = pandabot_core.telemetry
    sys.modules["pandabot_core.discord_comms"] = pandabot_core.discord_comms
    sys.modules["pandabot_core.identity"] = pandabot_core.identity
    sys.modules["pandabot_core.scheduler"] = pandabot_core.scheduler
    sys.modules["pandabot_core.testing"] = pandabot_core.testing
    sys.modules["pandabot_core.pm"] = pandabot_core.pm
    sys.modules["pandabot_core.pm.github"] = pandabot_core.pm.github
    sys.modules["pandabot_core.channels"] = pandabot_core.channels
    sys.modules["pandabot_core.code_qa"] = pandabot_core.code_qa

    # Setup some default behaviors for the mocks to avoid common failures
    pandabot_core.llm.provider.get_provider.return_value = MagicMock()
    pandabot_core.llm.provider.get_available_profiles.return_value = []
    pandabot_core.channels.BotChannelMap.from_env.return_value = {}

    # Scheduler state for mocks
    _tasks = []
    def add_task(**kwargs):
        t = {
            "id": len(_tasks) + 1,
            "fire_at": kwargs.get("fire_at_local"),
            "description": kwargs.get("description"),
            "task_type": kwargs.get("task_type", "one_shot"),
            "recurrence_rule": kwargs.get("recurrence_rule"),
            "attempt": 1,
            "max_attempts": kwargs.get("max_attempts", 5),
            "done": False
        }
        _tasks.append(t)
        return t["id"]

    def list_pending():
        return [t for t in _tasks if not t["done"]]

    def cancel_task(tid):
        for t in _tasks:
            if t["id"] == tid and not t["done"]:
                t["done"] = True
                return True
        return False

    def mark_done(tid):
        for t in _tasks:
            if t["id"] == tid:
                t["done"] = True

    pandabot_core.scheduler.add_task.side_effect = add_task
    pandabot_core.scheduler.list_pending.side_effect = list_pending
    pandabot_core.scheduler.cancel_task.side_effect = cancel_task
    pandabot_core.scheduler.mark_done.side_effect = mark_done

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
if isinstance(stub_discord, MagicMock):
    # If it was mocked above, it won't do anything, which is fine for unit tests
    pass
else:
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

    # Reset internal task list for each test using tmp_db
    global _tasks
    _tasks = []

    from pandabot_core import scheduler as core_sched
    if not isinstance(core_sched, MagicMock):
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
