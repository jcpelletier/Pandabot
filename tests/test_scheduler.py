"""
Scheduler CRUD and recurrence tests.

All tests use the tmp_db fixture (from conftest) which redirects DB_PATH to
a fresh temp file, so tests never touch the real scheduler.db and never
interfere with each other.
"""

import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import scheduler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _past(minutes: float = 60) -> str:
    """Local naive ISO datetime N minutes in the past."""
    return (datetime.datetime.now() - datetime.timedelta(minutes=minutes)).isoformat()


def _future(minutes: float = 60) -> str:
    """Local naive ISO datetime N minutes in the future."""
    return (datetime.datetime.now() + datetime.timedelta(minutes=minutes)).isoformat()


def _add(fire_at: str, **kwargs) -> int:
    defaults = dict(channel_id=1, description="test task")
    defaults.update(kwargs)
    return scheduler.add_task(fire_at_local=fire_at, **defaults)


# ---------------------------------------------------------------------------
# Basic CRUD
# ---------------------------------------------------------------------------

class TestAddAndRetrieve:
    def test_add_returns_integer_id(self, tmp_db):
        task_id = _add(_past())
        assert isinstance(task_id, int)
        assert task_id >= 1

    def test_ids_are_unique(self, tmp_db):
        id1 = _add(_past())
        id2 = _add(_past())
        assert id1 != id2

    def test_due_task_appears_in_get_due(self, tmp_db):
        _add(_past())
        due = scheduler.get_due_tasks()
        assert len(due) == 1

    def test_future_task_not_in_get_due(self, tmp_db):
        _add(_future())
        assert scheduler.get_due_tasks() == []

    def test_mixed_past_future_only_past_returned(self, tmp_db):
        _add(_past(), description="past")
        _add(_future(), description="future")
        due = scheduler.get_due_tasks()
        assert len(due) == 1
        assert due[0]["description"] == "past"

    def test_due_tasks_ordered_oldest_first(self, tmp_db):
        _add(_past(90), description="older")
        _add(_past(30), description="newer")
        due = scheduler.get_due_tasks()
        assert due[0]["description"] == "older"
        assert due[1]["description"] == "newer"

    def test_task_fields_stored_correctly(self, tmp_db):
        task_id = scheduler.add_task(
            fire_at_local=_past(),
            channel_id=42,
            description="my task",
            task_type="condition_check",
            tool_calls=[{"tool": "get_service_status", "args": {"service_name": "jellyfin"}}],
            condition_pattern=r'"result":\s*"SUCCESS"',
            max_attempts=3,
            check_interval_minutes=10,
        )
        due = scheduler.get_due_tasks()
        t = due[0]
        assert t["id"] == task_id
        assert t["channel_id"] == 42
        assert t["description"] == "my task"
        assert t["task_type"] == "condition_check"
        assert t["max_attempts"] == 3
        assert t["check_interval_minutes"] == 10
        assert "get_service_status" in t["tool_calls"]


class TestMarkDone:
    def test_mark_done_removes_from_due(self, tmp_db):
        task_id = _add(_past())
        scheduler.mark_done(task_id)
        assert scheduler.get_due_tasks() == []

    def test_mark_done_removes_from_list_pending(self, tmp_db):
        task_id = _add(_future())
        scheduler.mark_done(task_id)
        assert scheduler.list_pending() == []

    def test_mark_done_idempotent(self, tmp_db):
        task_id = _add(_past())
        scheduler.mark_done(task_id)
        scheduler.mark_done(task_id)  # should not raise
        assert scheduler.get_due_tasks() == []


class TestReschedule:
    def test_reschedule_moves_task_to_future(self, tmp_db):
        task_id = _add(_past())
        assert len(scheduler.get_due_tasks()) == 1

        new_fire = (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=2)
        ).isoformat()
        scheduler.reschedule(task_id, new_fire, new_attempt=1)
        assert scheduler.get_due_tasks() == []

    def test_reschedule_updates_attempt_counter(self, tmp_db):
        task_id = _add(_future())
        new_fire = _future(120)
        # Use raw sqlite to check
        import sqlite3
        scheduler.reschedule(task_id, new_fire, new_attempt=3)
        with sqlite3.connect(tmp_db) as conn:
            row = conn.execute(
                "SELECT attempt FROM scheduled_tasks WHERE id=?", (task_id,)
            ).fetchone()
        assert row[0] == 3


class TestCancelTask:
    def test_cancel_pending_task_returns_true(self, tmp_db):
        task_id = _add(_future())
        assert scheduler.cancel_task(task_id) is True

    def test_cancelled_task_not_in_pending(self, tmp_db):
        task_id = _add(_future())
        scheduler.cancel_task(task_id)
        assert scheduler.list_pending() == []

    def test_cancel_nonexistent_returns_false(self, tmp_db):
        assert scheduler.cancel_task(99999) is False

    def test_cancel_already_done_returns_false(self, tmp_db):
        task_id = _add(_past())
        scheduler.mark_done(task_id)
        assert scheduler.cancel_task(task_id) is False


class TestListPending:
    def test_empty_returns_empty_list(self, tmp_db):
        assert scheduler.list_pending() == []

    def test_shows_undone_tasks(self, tmp_db):
        _add(_future(), description="alpha")
        _add(_future(), description="beta")
        pending = scheduler.list_pending()
        descriptions = [t["description"] for t in pending]
        assert "alpha" in descriptions
        assert "beta" in descriptions

    def test_excludes_done_tasks(self, tmp_db):
        id1 = _add(_future(), description="keep")
        id2 = _add(_future(), description="done")
        scheduler.mark_done(id2)
        pending = scheduler.list_pending()
        assert len(pending) == 1
        assert pending[0]["description"] == "keep"

    def test_includes_both_past_and_future_undone(self, tmp_db):
        _add(_past(), description="overdue")
        _add(_future(), description="upcoming")
        pending = scheduler.list_pending()
        assert len(pending) == 2


# ---------------------------------------------------------------------------
# Recurrence
# ---------------------------------------------------------------------------

class TestScheduleNextRecurring:
    """
    Tests for schedule_next_recurring. We pass a task-like dict with fire_at
    set far enough in the past that the next occurrence is definitely in the
    future regardless of the current date, then verify a new pending task was
    inserted with fire_at > now.
    """

    def _now_utc(self) -> datetime.datetime:
        return datetime.datetime.now(datetime.timezone.utc)

    def _make_task(self, extra_rows: dict) -> dict:
        """Build a minimal task dict suitable for schedule_next_recurring."""
        import sqlite3, json
        task_id = scheduler.add_task(
            fire_at_local=_past(60 * 24 * 7),  # 1 week ago
            channel_id=1,
            description="recurring test",
            task_type="recurring",
            tool_calls=[],
            **{k: v for k, v in extra_rows.items()
               if k not in ("fire_at", "channel_id", "description",
                            "task_type", "tool_calls")},
        )
        with sqlite3.connect(scheduler.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM scheduled_tasks WHERE id=?", (task_id,)
            ).fetchone()
            return dict(row)

    def _get_next_fire_at(self, original_id: int) -> str | None:
        """Return fire_at of the newest task that isn't the original."""
        import sqlite3
        with sqlite3.connect(scheduler.DB_PATH) as conn:
            row = conn.execute(
                "SELECT fire_at FROM scheduled_tasks WHERE id != ? ORDER BY id DESC LIMIT 1",
                (original_id,),
            ).fetchone()
            return row[0] if row else None

    def test_weekly_creates_new_task(self, tmp_db):
        task = self._make_task({"recurrence_rule": "weekly:1"})
        scheduler.schedule_next_recurring(task)
        next_fire = self._get_next_fire_at(task["id"])
        assert next_fire is not None

    def test_weekly_next_fire_is_in_future(self, tmp_db):
        task = self._make_task({"recurrence_rule": "weekly:1"})
        scheduler.schedule_next_recurring(task)
        next_fire = self._get_next_fire_at(task["id"])
        assert next_fire > self._now_utc().isoformat()

    def test_weekly_next_fire_roughly_one_week_out(self, tmp_db):
        task = self._make_task({"recurrence_rule": "weekly:1"})
        scheduler.schedule_next_recurring(task)
        next_fire = self._get_next_fire_at(task["id"])
        dt = datetime.datetime.fromisoformat(next_fire)
        delta = dt - self._now_utc()
        # Should be between 5 and 9 days from now
        assert datetime.timedelta(days=5) < delta < datetime.timedelta(days=9)

    def test_monthly_creates_new_task(self, tmp_db):
        task = self._make_task({"recurrence_rule": "monthly:15"})
        scheduler.schedule_next_recurring(task)
        assert self._get_next_fire_at(task["id"]) is not None

    def test_monthly_next_fire_is_in_future(self, tmp_db):
        task = self._make_task({"recurrence_rule": "monthly:15"})
        scheduler.schedule_next_recurring(task)
        next_fire = self._get_next_fire_at(task["id"])
        assert next_fire > self._now_utc().isoformat()

    def test_monthly_31_does_not_crash(self, tmp_db):
        """Monthly recurrence on day 31 must not raise even for short months."""
        task = self._make_task({"recurrence_rule": "monthly:31"})
        scheduler.schedule_next_recurring(task)  # must not raise
        assert self._get_next_fire_at(task["id"]) is not None

    def test_unknown_rule_does_not_crash_or_insert(self, tmp_db):
        task = self._make_task({"recurrence_rule": "daily:nonsense"})
        scheduler.schedule_next_recurring(task)
        # No new task should have been created
        assert self._get_next_fire_at(task["id"]) is None

    def test_none_rule_does_nothing(self, tmp_db):
        task = self._make_task({"recurrence_rule": None})
        scheduler.schedule_next_recurring(task)
        assert self._get_next_fire_at(task["id"]) is None

    def test_new_task_inherits_description(self, tmp_db):
        import sqlite3
        task = self._make_task({"recurrence_rule": "weekly:1"})
        scheduler.schedule_next_recurring(task)
        next_fire = self._get_next_fire_at(task["id"])
        with sqlite3.connect(scheduler.DB_PATH) as conn:
            row = conn.execute(
                "SELECT description FROM scheduled_tasks WHERE fire_at=?", (next_fire,)
            ).fetchone()
        assert row[0] == "recurring test"

    def test_new_task_inherits_recurrence_rule(self, tmp_db):
        import sqlite3
        task = self._make_task({"recurrence_rule": "weekly:1"})
        scheduler.schedule_next_recurring(task)
        with sqlite3.connect(scheduler.DB_PATH) as conn:
            row = conn.execute(
                "SELECT recurrence_rule FROM scheduled_tasks WHERE id != ? ORDER BY id DESC LIMIT 1",
                (task["id"],),
            ).fetchone()
        assert row[0] == "weekly:1"
