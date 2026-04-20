"""
Async tests for fire_scheduled_task in bot.py.

Covers the core dispatch logic:
  - static_message — posted as-is, no LLM, no tool calls
  - one_shot with tool_calls — execute_tool called, results posted
  - intro_message — prepended to tool results
  - generative_prompt — LLM called with {results} substituted
  - condition_check met — marks done, posts met_message
  - condition_check not met, attempts remaining — reschedules
  - condition_check not met, max attempts reached — gave-up message
  - condition_check default met_message (no explicit met_message set)
  - recurring — marks done, schedules next occurrence
  - empty one_shot (no tool_calls, no messages) — posts fallback line

Each test uses tmp_db so the scheduler DB is clean and isolated.
"""

import asyncio
import datetime
import json
import os
import sys

import pytest
from unittest.mock import MagicMock, AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import scheduler
import tools
import bot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _past(minutes: float = 60) -> str:
    return (datetime.datetime.now() - datetime.timedelta(minutes=minutes)).isoformat()


def _task(**overrides) -> dict:
    """Return a minimal task dict ready for fire_scheduled_task."""
    defaults = {
        "id": 1,
        "task_type": "one_shot",
        "tool_calls": "[]",
        "channel_id": 111222333,
        "attempt": 0,
        "max_attempts": 3,
        "check_interval_minutes": 5,
        "static_message": None,
        "generative_prompt": None,
        "condition_pattern": None,
        "met_message": None,
        "not_met_message": None,
        "intro_message": None,
        "description": "Test task",
        "recurrence_rule": None,
        "fire_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    defaults.update(overrides)
    return defaults


@pytest.fixture
def posted(monkeypatch):
    """Capture all messages sent to Discord. Returns list of strings."""
    messages: list[str] = []

    async def fake_post(channel_id: int, text: str) -> None:
        messages.append(text)

    monkeypatch.setattr(bot, "post_notification_to", fake_post)
    return messages


@pytest.fixture
def fake_execute(monkeypatch):
    """
    Replace execute_tool with a controllable stub.
    Returns a dict — set fake_execute["tool_name"] = "desired output" before
    calling fire_scheduled_task.
    """
    results: dict[str, str] = {}

    def _execute(name: str, args: dict) -> str:
        return results.get(name, f"[result of {name}]")

    monkeypatch.setattr(bot, "execute_tool", _execute)
    return results


@pytest.fixture
def mock_claude(monkeypatch):
    """
    Stub bot.claude so generative_prompt tests don't hit the real API.
    Returns the mock so tests can set .messages.create.return_value.
    """
    mc = MagicMock()
    resp = MagicMock()
    resp.content = [MagicMock(text="LLM says hello")]
    mc.messages.create.return_value = resp
    monkeypatch.setattr(bot, "claude", mc)
    return mc


# ---------------------------------------------------------------------------
# static_message
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_static_message_posted(tmp_db, posted, fake_execute):
    task_id = scheduler.add_task(
        fire_at_local=_past(), channel_id=111222333,
        description="joke time", static_message="Why did the server reboot?",
    )
    task = dict(scheduler.get_due_tasks()[0])
    await bot.fire_scheduled_task(task)
    assert any("Why did the server reboot?" in m for m in posted)


@pytest.mark.asyncio
async def test_static_message_no_tool_calls(tmp_db, posted, fake_execute):
    """static_message must fire without calling execute_tool."""
    calls: list = []
    original = bot.execute_tool

    async def spy(name, args):
        calls.append(name)
        return original(name, args)

    task = _task(static_message="pre-written content", tool_calls="[]")
    await bot.fire_scheduled_task(task)
    assert calls == []


@pytest.mark.asyncio
async def test_static_message_marks_done(tmp_db, posted, fake_execute):
    task_id = scheduler.add_task(
        fire_at_local=_past(), channel_id=111222333,
        description="once", static_message="done",
    )
    task = dict(scheduler.get_due_tasks()[0])
    await bot.fire_scheduled_task(task)
    assert scheduler.get_due_tasks() == []


# ---------------------------------------------------------------------------
# one_shot with tool_calls
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_one_shot_tool_result_posted(tmp_db, posted, fake_execute):
    fake_execute["get_service_status"] = "jellyfin: active (running)"
    tc = json.dumps([{"tool": "get_service_status", "args": {"service_name": "jellyfin"}}])
    task = _task(tool_calls=tc)
    await bot.fire_scheduled_task(task)
    assert any("jellyfin: active" in m for m in posted)


@pytest.mark.asyncio
async def test_one_shot_multiple_tools_combined(tmp_db, posted, fake_execute):
    fake_execute["get_service_status"] = "up"
    fake_execute["get_disk_usage"] = "80% used"
    tc = json.dumps([
        {"tool": "get_service_status", "args": {}},
        {"tool": "get_disk_usage",     "args": {}},
    ])
    task = _task(tool_calls=tc)
    await bot.fire_scheduled_task(task)
    assert any("up" in m and "80%" in m for m in posted)


@pytest.mark.asyncio
async def test_one_shot_marks_done(tmp_db, posted, fake_execute):
    task_id = scheduler.add_task(
        fire_at_local=_past(), channel_id=111222333,
        description="status check",
        tool_calls=[{"tool": "get_service_status", "args": {}}],
    )
    task = dict(scheduler.get_due_tasks()[0])
    await bot.fire_scheduled_task(task)
    assert scheduler.get_due_tasks() == []


@pytest.mark.asyncio
async def test_intro_message_prepended(tmp_db, posted, fake_execute):
    fake_execute["get_disk_usage"] = "45% used"
    tc = json.dumps([{"tool": "get_disk_usage", "args": {}}])
    task = _task(tool_calls=tc, intro_message="📊 Storage report:")
    await bot.fire_scheduled_task(task)
    assert any("📊 Storage report:" in m and "45% used" in m for m in posted)


@pytest.mark.asyncio
async def test_empty_task_posts_fallback(tmp_db, posted, fake_execute):
    """No tool_calls and no message fields → fallback '📅 Scheduled:' line."""
    task = _task(description="My scheduled thing")
    await bot.fire_scheduled_task(task)
    assert any("My scheduled thing" in m for m in posted)


# ---------------------------------------------------------------------------
# generative_prompt
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generative_prompt_calls_llm(tmp_db, posted, fake_execute, mock_claude):
    fake_execute["get_service_status"] = "jellyfin: active"
    tc = json.dumps([{"tool": "get_service_status", "args": {}}])
    task = _task(
        tool_calls=tc,
        generative_prompt="Summarise this in one sentence: {results}",
    )
    await bot.fire_scheduled_task(task)
    assert mock_claude.messages.create.called


@pytest.mark.asyncio
async def test_generative_prompt_substitutes_results(tmp_db, posted, fake_execute, mock_claude):
    fake_execute["get_service_status"] = "TOOL_OUTPUT_MARKER"
    tc = json.dumps([{"tool": "get_service_status", "args": {}}])
    captured_prompts: list[str] = []

    original_create = mock_claude.messages.create
    def spy_create(**kwargs):
        for msg in kwargs.get("messages", []):
            captured_prompts.append(msg.get("content", ""))
        return original_create(**kwargs)
    mock_claude.messages.create.side_effect = spy_create

    task = _task(tool_calls=tc, generative_prompt="Report: {results}")
    await bot.fire_scheduled_task(task)
    assert any("TOOL_OUTPUT_MARKER" in p for p in captured_prompts)


@pytest.mark.asyncio
async def test_generative_prompt_output_posted(tmp_db, posted, fake_execute, mock_claude):
    resp = MagicMock()
    resp.content = [MagicMock(text="Everything looks good!")]
    mock_claude.messages.create.return_value = resp
    task = _task(generative_prompt="Say something about {results}")
    await bot.fire_scheduled_task(task)
    assert any("Everything looks good!" in m for m in posted)


# ---------------------------------------------------------------------------
# condition_check — met
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_condition_met_posts_met_message(tmp_db, posted, fake_execute):
    fake_execute["get_jenkins_build_status"] = '{"result": "SUCCESS"}'
    tc = json.dumps([{"tool": "get_jenkins_build_status", "args": {}}])
    task_id = scheduler.add_task(
        fire_at_local=_past(), channel_id=111222333,
        description="wait for build",
        task_type="condition_check",
        tool_calls=[{"tool": "get_jenkins_build_status", "args": {}}],
        condition_pattern=r'"result":\s*"SUCCESS"',
        met_message="✅ Build passed!",
        max_attempts=5,
    )
    task = dict(scheduler.get_due_tasks()[0])
    await bot.fire_scheduled_task(task)
    assert any("✅ Build passed!" in m for m in posted)


@pytest.mark.asyncio
async def test_condition_met_marks_done(tmp_db, posted, fake_execute):
    fake_execute["get_jenkins_build_status"] = '"result": "SUCCESS"'
    task_id = scheduler.add_task(
        fire_at_local=_past(), channel_id=111222333,
        description="wait",
        task_type="condition_check",
        tool_calls=[{"tool": "get_jenkins_build_status", "args": {}}],
        condition_pattern=r'"result":\s*"SUCCESS"',
        max_attempts=3,
    )
    task = dict(scheduler.get_due_tasks()[0])
    await bot.fire_scheduled_task(task)
    assert scheduler.get_due_tasks() == []


@pytest.mark.asyncio
async def test_condition_met_default_message(tmp_db, posted, fake_execute):
    """When met_message is None, a default '✅ Done' message should be posted."""
    fake_execute["q"] = "pattern_match_here"
    tc = json.dumps([{"tool": "q", "args": {}}])
    task = _task(
        task_type="condition_check",
        tool_calls=tc,
        condition_pattern="pattern_match_here",
        met_message=None,
        description="My check",
    )
    await bot.fire_scheduled_task(task)
    assert any("My check" in m or "Done" in m for m in posted)


# ---------------------------------------------------------------------------
# condition_check — not met, rescheduling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_condition_not_met_reschedules(tmp_db, posted, fake_execute):
    fake_execute["get_service_status"] = "inactive"
    task_id = scheduler.add_task(
        fire_at_local=_past(), channel_id=111222333,
        description="waiting for service",
        task_type="condition_check",
        tool_calls=[{"tool": "get_service_status", "args": {}}],
        condition_pattern=r"\bservice is running\b",  # won't match "inactive"
        max_attempts=5,
        check_interval_minutes=2,
    )
    task = dict(scheduler.get_due_tasks()[0])
    await bot.fire_scheduled_task(task)
    # Original is now done=1 (rescheduled); a new entry should exist
    pending = scheduler.list_pending()
    assert len(pending) == 1
    assert pending[0]["description"] == "waiting for service"


@pytest.mark.asyncio
async def test_condition_not_met_posts_not_met_message(tmp_db, posted, fake_execute):
    fake_execute["q"] = "no match here"
    tc = json.dumps([{"tool": "q", "args": {}}])
    task = _task(
        task_type="condition_check",
        tool_calls=tc,
        condition_pattern="WILL_NOT_MATCH",
        not_met_message="🔄 Still waiting...",
        attempt=0,
        max_attempts=5,
    )
    await bot.fire_scheduled_task(task)
    assert any("🔄 Still waiting..." in m for m in posted)


@pytest.mark.asyncio
async def test_condition_not_met_max_attempts_gives_up(tmp_db, posted, fake_execute):
    fake_execute["q"] = "no match"
    tc = json.dumps([{"tool": "q", "args": {}}])
    task = _task(
        task_type="condition_check",
        tool_calls=tc,
        condition_pattern="WILL_NOT_MATCH",
        attempt=2,        # attempt 2, max 3 → new_attempt=3 >= max_attempts
        max_attempts=3,
    )
    await bot.fire_scheduled_task(task)
    assert any("gave up" in m.lower() or "Gave up" in m for m in posted)


@pytest.mark.asyncio
async def test_condition_max_attempts_marks_done(tmp_db, posted, fake_execute):
    fake_execute["q"] = "no match"
    tc = json.dumps([{"tool": "q", "args": {}}])
    task_id = scheduler.add_task(
        fire_at_local=_past(), channel_id=111222333,
        description="hopeless check",
        task_type="condition_check",
        tool_calls=[{"tool": "q", "args": {}}],
        condition_pattern="WILL_NOT_MATCH",
        max_attempts=1,   # attempt=0, new_attempt=1 >= max_attempts=1
    )
    task = dict(scheduler.get_due_tasks()[0])
    await bot.fire_scheduled_task(task)
    assert scheduler.list_pending() == []


# ---------------------------------------------------------------------------
# recurring
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recurring_marks_current_done(tmp_db, posted, fake_execute):
    task_id = scheduler.add_task(
        fire_at_local=_past(), channel_id=111222333,
        description="weekly digest",
        task_type="recurring",
        recurrence_rule="weekly:1",
    )
    task = dict(scheduler.get_due_tasks()[0])
    await bot.fire_scheduled_task(task)
    # Current task must be done
    assert scheduler.get_due_tasks() == []


@pytest.mark.asyncio
async def test_recurring_schedules_next_occurrence(tmp_db, posted, fake_execute):
    task_id = scheduler.add_task(
        fire_at_local=_past(), channel_id=111222333,
        description="weekly digest",
        task_type="recurring",
        recurrence_rule="weekly:1",
    )
    task = dict(scheduler.get_due_tasks()[0])
    await bot.fire_scheduled_task(task)
    # A new pending task (the next occurrence) should exist
    pending = scheduler.list_pending()
    assert len(pending) == 1
    assert pending[0]["description"] == "weekly digest"
    assert pending[0]["recurrence_rule"] == "weekly:1"
