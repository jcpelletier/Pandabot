"""family_board.py: the family wall-board tools, against a fake board API."""
import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import family_board as fb
import tools

TODAY = "2026-11-18"  # a Wednesday

PEOPLE = [
    {"id": "p1", "name": "Alex", "role": "adult", "points": 0, "rewardThreshold": 200},
    {"id": "k1", "name": "Sam", "role": "kid", "points": 40, "rewardThreshold": 100},
    {"id": "k2", "name": "Robin", "role": "kid", "points": 10, "rewardThreshold": 100},
]
CHORES = [
    {"id": "c1", "title": "Tidy bedroom", "area": "Bedroom", "person": "k1",
     "recurrence": "daily", "reward": 5},
    {"id": "c2", "title": "Dishes", "area": "Kitchen", "person": "shared",
     "recurrence": "daily", "rotation": {"members": ["k1", "k2"], "turn": 1}},
    {"id": "c3", "title": "Take out trash", "area": "Kitchen", "person": "shared",
     "recurrence": "weekly", "dow": 3},
]


class FakeBoard:
    def __init__(self):
        self.board = {"people": copy.deepcopy(PEOPLE), "pets": [],
                      "chores": copy.deepcopy(CHORES), "rewards": [{"id": "r1", "title": "Movie", "cost": 50}],
                      "state": {"date": TODAY, "doneLog": {}, "ignoreLog": {},
                                "points": {"k1": 40, "k2": 10, "p1": 0},
                                "streak": {"k1": 2, "k2": 0}, "household": 3, "redeemed": {}}}
        self.posts = []
        self.instances = []
        self.events = []
        self.gets = []

    def get(self, path, params=None):
        self.gets.append((path, params))
        who = [{"id": p["id"], "name": p["name"], "role": p["role"]} for p in self.board["people"]]
        st = self.board["state"]
        if path == "/api/board":
            return copy.deepcopy(self.board)
        if path == "/api/board/today":
            return {"date": TODAY, "people": who, "points": st["points"], "streak": st["streak"],
                    "household": st["household"],
                    "items": [
                        {"date": TODAY, "choreId": "c1", "title": "Tidy bedroom", "person": "k1",
                         "assignee": "k1", "status": "done", "by": "k1", "reward": 5},
                        {"date": TODAY, "choreId": "c2", "title": "Dishes", "person": "shared",
                         "assignee": "k2", "status": "todo"},
                        {"date": "2026-11-11", "choreId": "c3", "title": "Take out trash",
                         "person": "shared", "assignee": None, "status": "late", "red": True},
                    ]}
        if path == "/api/board/instances":
            return {"date": TODAY, "people": who, "instances": self.instances
                    if params["to"] == TODAY else [],
                    "points": st["points"], "streak": st["streak"], "household": 3}
        if path == "/api/board/chore-log":
            return {"created": "2026-10-01", "events": [
                {"ts": None, "kind": "done", "date": "2026-11-17", "person": "k1", "by": "k1",
                 "detail": {}},
                {"ts": None, "kind": "skipped", "date": "2026-11-16", "person": None, "by": "p1",
                 "detail": {"reason": "sick"}}]}
        if path == "/api/gcal":
            return {"events": self.events}
        raise AssertionError(path)

    def post(self, path, body):
        self.posts.append((path, body))
        return {"ok": True, "event": {"id": "new1", "title": body.get("title")}}


@pytest.fixture
def board(monkeypatch):
    f = FakeBoard()
    monkeypatch.setattr(fb, "_get", f.get)
    monkeypatch.setattr(fb, "_post", f.post)
    monkeypatch.setattr(fb, "_today", lambda: TODAY)
    monkeypatch.setattr(fb, "FAMILY_BOARD_TZ", "America/Chicago")
    return f


# --- wiring -----------------------------------------------------------------

def test_tools_gated_by_flag(monkeypatch):
    monkeypatch.setattr(tools, "ENABLE_FAMILY_BOARD", False)
    names = {t["name"] for t in tools._build_tool_definitions()}
    assert "query_family_board" not in names
    monkeypatch.setattr(tools, "ENABLE_FAMILY_BOARD", True)
    names = {t["name"] for t in tools._build_tool_definitions()}
    assert {"query_family_board", "family_task_metrics", "manage_family_chore",
            "mark_family_chore", "family_calendar"} <= names


def test_execute_tool_dispatches_even_when_disabled(board, monkeypatch):
    monkeypatch.setattr(tools, "ENABLE_FAMILY_BOARD", False)
    out = tools.execute_tool("query_family_board", {"action": "today"})
    assert "Tidy bedroom" in out


def test_write_tools_preview_with_confirm_phrase(board):
    for name, inputs in [
        ("manage_family_chore", {"action": "create", "title": "Feed fish", "person": "Sam"}),
        ("mark_family_chore", {"action": "done", "chore": "Dishes"}),
        ("family_calendar", {"action": "create", "title": "Dentist", "date": "2026-11-20",
                             "time": "3pm"}),
    ]:
        out = fb.execute(name, inputs)
        assert "Reply **yes** to confirm" in out, (name, out)
        assert name in fb.WRITE_TOOLS
    assert board.posts == []


# --- reads ------------------------------------------------------------------

def test_today_groups_by_person(board):
    out = fb.query_family_board("today")
    assert "**Sam**: 1/1 done" in out and "streak 2d" in out
    assert "**Robin**: 0/1 done" in out
    assert "overdue from 2026-11-11" in out


def test_person_filter_and_reward_goal(board):
    out = fb.query_family_board("person", person="sam")
    assert "Tidy bedroom" in out and "Dishes" not in out
    assert "reward goal 100★, 60 to go" in out


def test_list_chores_filters_rotation_members(board):
    out = fb.query_family_board("list_chores", person="Robin")
    assert "Dishes" in out and "takes turns: Sam, Robin" in out
    assert "Tidy bedroom" not in out
    assert "Weekly on Wednesday" in fb.query_family_board("list_chores", area="kitchen")


def test_chore_history(board):
    out = fb.query_family_board("chore_history", chore="tidy")
    assert "2026-11-17 done (Sam)" in out and "skipped (Alex): sick" in out


def test_unknown_person_lists_names(board):
    out = fb.query_family_board("person", person="Zed")
    assert "no one called 'Zed'" in out and "Alex" in out


def test_ambiguous_chore_asks_for_id(board):
    board.board["chores"].append({"id": "c9", "title": "Tidy kitchen", "person": "k2",
                                  "recurrence": "daily"})
    out = fb.query_family_board("chore_history", chore="tidy")
    assert "matches several chores" in out and "c9" in out


# --- metrics ----------------------------------------------------------------

def test_metrics_rates_and_most_missed(board):
    def inst(cid, title, date, status, assignee="k1", by=None, **kw):
        d = {"choreId": cid, "title": title, "date": date, "status": status,
             "assignee": assignee, "person": assignee, "reward": 5}
        if by:
            d["by"] = by
        d.update(kw)
        return d
    board.instances = [
        inst("c1", "Tidy bedroom", "2026-11-12", "done", by="k1"),
        inst("c1", "Tidy bedroom", "2026-11-13", "done", by="k1"),
        inst("c1", "Tidy bedroom", "2026-11-14", "missed"),
        inst("c1", "Tidy bedroom", "2026-11-15", "late", inGrace=False),
        inst("c1", "Tidy bedroom", "2026-11-16", "skipped"),
        inst("c1", "Tidy bedroom", TODAY, "todo"),
        inst("c3", "Take out trash", "2026-11-17", "late", assignee=None, inGrace=True),
        inst("c2", "Dishes", "2026-11-17", "done", assignee=None, by="k2"),
    ]
    out = fb.family_task_metrics(days=7)
    assert "Sam: 50% (done 2, missed 2, skipped 1, open 1, earned 10★)" in out
    assert "Robin: 100%" in out
    assert "Shared: n/a (done 0, missed 0, skipped 0, open 1" in out
    assert "Most missed: Tidy bedroom (2× missed, 2× done)" in out
    assert "Never missed: Dishes" in out
    paths = [g for g in board.gets if g[0] == "/api/board/instances"]
    assert paths[0][1] == {"from": "2026-11-12", "to": TODAY}
    assert paths[1][1] == {"from": "2026-11-05", "to": "2026-11-11"}


def test_metrics_person_filter(board):
    board.instances = [
        {"choreId": "c1", "title": "Tidy", "date": TODAY, "status": "done", "by": "k1",
         "assignee": "k1", "person": "k1"},
        {"choreId": "c2", "title": "Dishes", "date": TODAY, "status": "done", "by": "k2",
         "assignee": "k2", "person": "shared"},
    ]
    out = fb.family_task_metrics(days=1, person="Robin", compare=False)
    assert "Robin: 100%" in out and "Sam" not in out.split("Current streaks")[0]


# --- manage_family_chore ----------------------------------------------------

def test_create_weekly_needs_day(board):
    out = fb.manage_family_chore("create", title="Mow lawn", recurrence="weekly")
    assert "needs day_of_week" in out


def test_create_confirmed_posts_full_chore(board):
    out = fb.manage_family_chore("create", title="Mow lawn", person="Alex", area="Yard",
                                 recurrence="weekly", day_of_week="Saturday", reward=10,
                                 months=[5, 6, 7], confirmed=True)
    assert out.startswith("✅ Created")
    path, body = board.posts[-1]
    assert path == "/api/board/chore"
    assert body["id"].startswith("pb") and body["person"] == "p1"
    assert (body["recurrence"], body["dow"], body["seasonal"], body["reward"]) == \
        ("weekly", 6, [5, 6, 7], 10)


def test_edit_keeps_untouched_fields(board):
    fb.manage_family_chore("edit", chore="c1", time="morning", confirmed=True)
    _, body = board.posts[-1]
    assert body == {"id": "c1", "title": "Tidy bedroom", "area": "Bedroom", "person": "k1",
                    "recurrence": "daily", "reward": 5, "time": "morning"}


def test_reassign_preview_and_rotation(board):
    out = fb.manage_family_chore("reassign", chore="Take out trash", rotation=["Sam", "Robin"])
    assert "takes turns: Sam, Robin" in out and "(was:" in out
    fb.manage_family_chore("reassign", chore="Dishes", person="Alex", confirmed=True)
    _, body = board.posts[-1]
    assert body["person"] == "p1" and "rotation" not in body


def test_noop_edit(board):
    assert "Nothing to change" in fb.manage_family_chore("edit", chore="c1", area="Bedroom")


def test_delete_warns_about_history(board):
    out = fb.manage_family_chore("delete", chore="c1")
    assert "erases its whole done/skip history" in out and "Reply **yes**" in out
    fb.manage_family_chore("delete", chore="c1", confirmed=True)
    assert board.posts[-1] == ("/api/board/remove", {"kind": "chore", "id": "c1"})


# --- mark_family_chore ------------------------------------------------------

def test_done_rotation_defaults_to_turn_and_advances(board):
    out = fb.mark_family_chore("done", chore="Dishes")
    assert "done by Robin" in out
    fb.mark_family_chore("done", chore="Dishes", confirmed=True)
    path, body = board.posts[-1]
    assert path == "/api/board/done"
    assert body["person"] == "k2" and body["date"] == TODAY
    assert body["rotation"] == {"members": ["k1", "k2"], "turn": 0}


def test_done_credits_reward(board):
    fb.mark_family_chore("done", chore="c1", confirmed=True)
    _, body = board.posts[-1]
    assert (body["person"], body["points"]) == ("k1", 5)


def test_done_shared_without_rotation_needs_person(board):
    assert "say who did it" in fb.mark_family_chore("done", chore="trash")


def test_done_twice_and_future(board):
    board.board["state"]["doneLog"] = {"c1": {TODAY: {"by": "k1"}}}
    assert "already done" in fb.mark_family_chore("done", chore="c1")
    assert "future" in fb.mark_family_chore("done", chore="c1", date="2026-11-30")


def test_undo_hands_turn_back(board):
    board.board["state"]["doneLog"] = {"c2": {TODAY: {"by": "k1"}}}
    fb.mark_family_chore("undo", chore="Dishes", confirmed=True)
    _, body = board.posts[-1]
    assert body["undo"] is True and body["rotation"]["turn"] == 0


def test_skip_and_unskip(board):
    fb.mark_family_chore("skip", chore="c1", date="yesterday", reason="sick", by="Alex",
                         confirmed=True)
    assert board.posts[-1] == ("/api/board/ignore", {"choreId": "c1", "by": "p1",
                                                     "reason": "sick", "date": "2026-11-17"})
    assert "isn't skipped" in fb.mark_family_chore("unskip", chore="c1")


# --- family_calendar --------------------------------------------------------

def test_calendar_list_marks_read_only(board):
    board.events = [
        {"id": "e1", "title": "Soccer", "start": "2026-11-18T16:00:00-06:00",
         "end": "2026-11-18T17:00:00-06:00", "allDay": False, "source": "family"},
        {"id": "e2", "title": "Busy", "start": "2026-11-18", "end": "2026-11-19",
         "allDay": True, "source": "work"},
    ]
    out = fb.family_calendar("list", days=3)
    assert "Wednesday 2026-11-18" in out
    assert "4:00 PM–5:00 PM: Soccer [id e1]" in out
    assert "Busy [work calendar, read-only]" in out
    _, params = board.gets[-1]
    assert params["timeMin"].startswith("2026-11-18T00:00:00")
    assert params["timeMax"].startswith("2026-11-20T23:59:59")


def test_calendar_create_timed(board):
    fb.family_calendar("create", title="Dentist", date="2026-11-20", time="3:30 pm",
                       end_time="16:15", location="Clinic", repeat="FREQ=MONTHLY",
                       confirmed=True)
    path, body = board.posts[-1]
    assert path == "/api/gcal/event"
    assert body == {"title": "Dentist", "date": "2026-11-20", "allDay": False,
                    "timeZone": "America/Chicago", "time": "15:30", "endTime": "16:15",
                    "location": "Clinic", "recurrence": "RRULE:FREQ=MONTHLY"}


def test_calendar_create_multiday_all_day(board):
    fb.family_calendar("create", title="Trip", date="2026-11-26", end_date="2026-11-28",
                       confirmed=True)
    _, body = board.posts[-1]
    assert body["allDay"] is True and body["endDate"] == "2026-11-29"


def test_calendar_edit_moves_time_keeps_length(board):
    board.events = [{"id": "e1", "title": "Soccer", "start": "2026-11-18T16:00:00-06:00",
                     "end": "2026-11-18T17:00:00-06:00", "allDay": False, "source": "family",
                     "location": "Park", "description": "", "recurringEventId": "s1"}]
    out = fb.family_calendar("edit", event_id="e1", date="2026-11-18", time="17:30")
    assert "this occurrence only" in out and "17:30–18:30" in out
    fb.family_calendar("edit", event_id="e1", date="2026-11-18", time="17:30",
                       new_date="2026-11-19", confirmed=True)
    path, body = board.posts[-1]
    assert path == "/api/gcal/event/update"
    assert body["id"] == "e1" and body["instance"] is True
    assert (body["date"], body["time"], body["endTime"], body["location"]) == \
        ("2026-11-19", "17:30", "18:30", "Park")


def test_calendar_edit_refuses_personal_calendar(board):
    board.events = [{"id": "e2", "title": "X", "start": "2026-11-18", "end": "2026-11-19",
                     "allDay": True, "source": "personal"}]
    out = fb.family_calendar("edit", event_id="e2", date="2026-11-18", title="Y")
    assert "only the family calendar" in out


def test_unreachable_board_is_reported(monkeypatch):
    def boom(path, params=None):
        raise fb.BoardError("family board unreachable at http://x: refused")
    monkeypatch.setattr(fb, "_get", boom)
    assert fb.query_family_board("today").startswith("Family board error: family board unreachable")
