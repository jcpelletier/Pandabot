"""Family wall board tools: chores, task metrics and the family calendar.

Talks to the FamilyChores board server (a separate app, normally on the same
host) over its JSON API. The board owns its SQLite database, the write lock and
the audit trail, and its own Google Calendar proxy, so Pandabot never touches
either directly and needs no secrets of its own:

  GET  /api/board                        people, chores, rewards, state
  GET  /api/board/today                  what is on the board today
  GET  /api/board/instances?from=&to=    dated chore instances with a status
  GET  /api/board/chore-log?id=          one chore's audit trail
  POST /api/board/chore                  create / edit a chore (full object)
  POST /api/board/remove                 {kind: "chore", id}
  POST /api/board/done                   {choreId, person, by, points, date, undo, rotation}
  POST /api/board/ignore                 {choreId, by, reason, date, undo}
  GET  /api/gcal?timeMin=&timeMax=       family calendar (+ read-only personal ones)
  POST /api/gcal/event                   create on the family calendar
  POST /api/gcal/event/update            edit a family calendar event (full shape)

Every function returns text for the LLM. Writes follow the bot's confirmation
pattern: confirmed=False returns a preview ending in "Reply **yes** to
confirm", which the agent loop queues; the user's yes re-runs it with
confirmed=True.

Config (env):
  FAMILY_BOARD_URL   default http://127.0.0.1:8417
  FAMILY_BOARD_TZ    IANA zone for "today" and calendar times; falls back to
                     TZ_NAME, then the host's local zone
"""
from __future__ import annotations

import datetime
import os
import random
import string

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

FAMILY_BOARD_URL = os.environ.get("FAMILY_BOARD_URL", "http://127.0.0.1:8417").rstrip("/")
FAMILY_BOARD_TZ = os.environ.get("FAMILY_BOARD_TZ", "") or os.environ.get("TZ_NAME", "")

TIMEOUT = 15
CONFIRM = "Reply **yes** to confirm."
WRITE_TOOLS = {"manage_family_chore", "mark_family_chore", "family_calendar"}

RECURRENCES = ("daily", "weekdays", "everyotherday", "weekly", "monthly", "seasonal", "once")
DOW_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
STATUS_ICON = {"done": "✅", "todo": "⬜", "late": "🟥", "skipped": "⏭️", "missed": "❌"}
SHARED_WORDS = {"shared", "anyone", "everyone", "family", "household", "anybody"}


class BoardError(Exception):
    pass


# ---------------------------------------------------------------------------
# HTTP + time helpers
# ---------------------------------------------------------------------------

def _get(path: str, params: dict | None = None) -> dict:
    try:
        r = requests.get(FAMILY_BOARD_URL + path, params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise BoardError(f"family board unreachable at {FAMILY_BOARD_URL}: {exc}") from exc
    return _decode(r, path)


def _post(path: str, body: dict) -> dict:
    try:
        r = requests.post(FAMILY_BOARD_URL + path, json=body, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise BoardError(f"family board unreachable at {FAMILY_BOARD_URL}: {exc}") from exc
    return _decode(r, path)


def _decode(r, path: str) -> dict:
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code == 404 and path.startswith("/api/board/") and not data.get("error"):
        raise BoardError(f"the family board has no {path} endpoint (update FamilyChores)")
    if r.status_code >= 400 or (isinstance(data, dict) and data.get("error")):
        msg = data.get("error") if isinstance(data, dict) else None
        hint = data.get("hint") if isinstance(data, dict) else None
        raise BoardError(f"{path}: {msg or f'HTTP {r.status_code}'}" + (f" ({hint})" if hint else ""))
    return data


def _tz():
    if FAMILY_BOARD_TZ and ZoneInfo is not None:
        try:
            return ZoneInfo(FAMILY_BOARD_TZ)
        except Exception:
            pass
    return datetime.datetime.now().astimezone().tzinfo


def _today() -> str:
    return datetime.datetime.now(_tz()).date().isoformat()


def _add_days(iso: str, n: int) -> str:
    return (datetime.date.fromisoformat(iso) + datetime.timedelta(days=n)).isoformat()


def _parse_date(value: str, default: str | None = None) -> str:
    """ISO date, or today / tomorrow / yesterday / a weekday name (next one)."""
    v = (value or "").strip().lower()
    today = _today()
    if not v:
        if default is None:
            raise BoardError("a date is required")
        return default
    if v == "today":
        return today
    if v == "tomorrow":
        return _add_days(today, 1)
    if v == "yesterday":
        return _add_days(today, -1)
    for i, name in enumerate(DOW_NAMES):
        if v in (name.lower(), name.lower()[:3]):
            t = datetime.date.fromisoformat(today)
            ahead = (i - (t.weekday() + 1) % 7) % 7
            return _add_days(today, ahead)
    try:
        return datetime.date.fromisoformat(v[:10]).isoformat()
    except ValueError:
        raise BoardError(f"can't read date {value!r}; use YYYY-MM-DD")


def _parse_dow(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value % 7
    v = str(value).strip().lower()
    if v.isdigit():
        return int(v) % 7
    for i, name in enumerate(DOW_NAMES):
        if v in (name.lower(), name.lower()[:3]):
            return i
    raise BoardError(f"can't read day of week {value!r}")


# ---------------------------------------------------------------------------
# Name resolution
# ---------------------------------------------------------------------------

def _people_index(board: dict) -> list[dict]:
    return list(board.get("people") or []) + list(board.get("pets") or [])


def _resolve_person(name: str, people: list[dict], allow_shared: bool = True) -> str:
    v = (name or "").strip().lower()
    if not v:
        raise BoardError("a person is required")
    if allow_shared and v in SHARED_WORDS:
        return "shared"
    exact = [p for p in people if v in (p["id"].lower(), (p.get("name") or "").lower())]
    if exact:
        return exact[0]["id"]
    loose = [p for p in people if (p.get("name") or "").lower().startswith(v)
             or v in (p.get("name") or "").lower()]
    if len(loose) == 1:
        return loose[0]["id"]
    names = ", ".join(p.get("name") or p["id"] for p in people)
    if loose:
        raise BoardError(f"{name!r} matches more than one person ({names})")
    raise BoardError(f"no one called {name!r} on the board (people: {names})")


def _resolve_chore(ref: str, chores: list[dict]) -> dict:
    v = (ref or "").strip().lower()
    if not v:
        raise BoardError("a chore (id or title) is required")
    for c in chores:
        if c["id"].lower() == v:
            return c
    exact = [c for c in chores if (c.get("title") or "").strip().lower() == v]
    if len(exact) == 1:
        return exact[0]
    pool = exact or [c for c in chores if v in (c.get("title") or "").lower()]
    if len(pool) == 1:
        return pool[0]
    if pool:
        opts = "; ".join(f"{c['id']} = {c['title']} ({c.get('person')})" for c in pool[:10])
        raise BoardError(f"{ref!r} matches several chores, pass the id: {opts}")
    raise BoardError(f"no chore matching {ref!r}")


def _name(pid, people: list[dict]) -> str:
    if not pid:
        return "anyone"
    if pid == "shared":
        return "Shared"
    for p in people:
        if p["id"] == pid:
            return p.get("name") or pid
    return pid


def recurrence_label(c: dict) -> str:
    rec = c.get("recurrence") or "daily"
    if rec == "once":
        return f"One-time, due {c.get('due')}"
    s = {"daily": "Daily", "weekdays": "Weekdays", "everyotherday": "Every other day",
         "weekly": f"Weekly on {DOW_NAMES[c['dow']]}" if c.get("dow") is not None else "Weekly",
         "monthly": f"Monthly on day {c['dom']}" if c.get("dom") is not None else "Monthly",
         "seasonal": "Once per season"}.get(rec, rec)
    win = c.get("months") if rec == "seasonal" else c.get("seasonal")
    if win:
        s += " (" + " ".join(MONTHS[m - 1] for m in win) + ")"
    if c.get("time"):
        s += f", {c['time']}"
    return s


def _assign_label(c: dict, people: list[dict]) -> str:
    if c.get("person") == "shared":
        rot = c.get("rotation") or {}
        if rot.get("members"):
            return "Shared, takes turns: " + ", ".join(_name(m, people) for m in rot["members"])
        return "Shared (anyone)"
    return _name(c.get("person"), people)


# ---------------------------------------------------------------------------
# query_family_board
# ---------------------------------------------------------------------------

def query_family_board(action: str, person: str = "", chore: str = "",
                       area: str = "", limit: int = 20) -> str:
    try:
        if action == "today":
            return _today_text(person)
        if action == "person":
            if not person:
                return "Error: action='person' needs a person."
            return _today_text(person, detail=True)
        if action == "list_chores":
            return _list_chores(person, area)
        if action == "chore_history":
            return _chore_history(chore, limit)
        if action == "rewards":
            return _rewards_text(person)
        return f"Error: unknown action {action!r}."
    except BoardError as exc:
        return f"Family board error: {exc}"


def _today_text(person: str = "", detail: bool = False) -> str:
    data = _get("/api/board/today")
    people = data.get("people") or []
    items = data.get("items") or []
    pid = _resolve_person(person, people) if person else None

    def owner(it):
        if it.get("status") == "done" and it.get("by"):
            return it["by"]
        return it.get("assignee") or ("shared" if it.get("person") == "shared" else it.get("person"))

    groups: dict[str, list] = {}
    for it in items:
        o = owner(it) or "shared"
        if pid and o != pid and not (pid == "shared" and it.get("person") == "shared"):
            continue
        groups.setdefault(o, []).append(it)

    lines = [f"Family board for {data.get('date')} (household streak: "
             f"{data.get('household', 0)} days)"]
    if not groups:
        lines.append(f"Nothing on the board{' for ' + _name(pid, people) if pid else ''} today.")
    order = [p["id"] for p in people] + ["shared"]
    for o in sorted(groups, key=lambda k: order.index(k) if k in order else 99):
        its = groups[o]
        done = sum(1 for i in its if i["status"] == "done")
        hdr = f"\n**{_name(o, people)}**: {done}/{len(its)} done"
        if o in (data.get("streak") or {}):
            hdr += f", streak {data['streak'][o]}d"
        if o in (data.get("points") or {}):
            hdr += f", {data['points'][o]}★"
        lines.append(hdr)
        for i in its:
            s = f"  {STATUS_ICON.get(i['status'], '•')} {i['title']} [{i['choreId']}]"
            if i["status"] == "late":
                s += f" (overdue from {i['date']}" + (", last day" if i.get("lastDay") else "") + ")"
            elif i.get("seasonEnd"):
                s += f" (once this season, by {i['seasonEnd']})"
            if i["status"] == "done" and i.get("by") and i.get("person") == "shared":
                s += f" by {_name(i['by'], people)}"
            if i.get("time"):
                s += f" · {i['time']}"
            if i.get("reward"):
                s += f" · {i['reward']}★"
            lines.append(s)
    if detail and pid and pid != "shared":
        board = _get("/api/board")
        for p in board.get("people") or []:
            if p["id"] == pid and p.get("role") != "pet":
                pts = (data.get("points") or {}).get(pid, p.get("points", 0))
                thr = p.get("rewardThreshold") or 0
                lines.append(f"\nStars: {pts}★" + (f" (reward goal {thr}★, {max(0, thr - pts)} to go)"
                                                    if thr else ""))
    return "\n".join(lines)


def _list_chores(person: str, area: str) -> str:
    board = _get("/api/board")
    people = _people_index(board)
    chores = board.get("chores") or []
    if person:
        pid = _resolve_person(person, people)
        chores = [c for c in chores if c.get("person") == pid
                  or pid in ((c.get("rotation") or {}).get("members") or [])]
    if area:
        chores = [c for c in chores if (c.get("area") or "").lower() == area.strip().lower()]
    if not chores:
        return "No chores match."
    by_area: dict[str, list] = {}
    for c in chores:
        by_area.setdefault(c.get("area") or "General", []).append(c)
    lines = [f"{len(chores)} chores:"]
    for a in sorted(by_area):
        lines.append(f"\n**{a}**")
        for c in by_area[a]:
            s = f"  [{c['id']}] {c['title']}: {_assign_label(c, people)}; {recurrence_label(c)}"
            if c.get("reward"):
                s += f"; {c['reward']}★"
            if c.get("note"):
                s += f"; note: {c['note']}"
            if c.get("checklist"):
                s += "; checklist: " + ", ".join(c["checklist"])
            lines.append(s)
    return "\n".join(lines)


def _chore_history(ref: str, limit: int) -> str:
    board = _get("/api/board")
    people = _people_index(board)
    c = _resolve_chore(ref, board.get("chores") or [])
    log = _get("/api/board/chore-log", {"id": c["id"]})
    events = (log.get("events") or [])[:max(1, min(int(limit or 20), 100))]
    lines = [f"History for {c['title']} [{c['id']}] ({_assign_label(c, people)}; "
             f"{recurrence_label(c)}), added {log.get('created') or 'before tracking began'}:"]
    if not events:
        lines.append("  (no events)")
    for e in events:
        when = e.get("date") or (e.get("ts") or "")[:10]
        who = e.get("person") or e.get("by")
        s = f"  {when} {e['kind']}"
        if who:
            s += f" ({_name(who, people)})"
        d = e.get("detail") or {}
        if d.get("reason") and d["reason"] != "missed":
            s += f": {d['reason']}"
        if e["kind"] == "edited" and d.get("changes"):
            s += ": " + ", ".join(f"{ch['field']} {ch['old']!r}→{ch['new']!r}" for ch in d["changes"])
        lines.append(s)
    return "\n".join(lines)


def _rewards_text(person: str) -> str:
    board = _get("/api/board")
    people = _people_index(board)
    state = board.get("state") or {}
    rewards = board.get("rewards") or []
    earners = [p for p in board.get("people") or [] if p.get("role") != "pet"]
    if person:
        pid = _resolve_person(person, people, allow_shared=False)
        earners = [p for p in earners if p["id"] == pid]
    lines = ["Stars:"]
    for p in earners:
        pts = (state.get("points") or {}).get(p["id"], p.get("points", 0))
        red = (state.get("redeemed") or {}).get(p["id"]) or {}
        s = f"  {p['name']}: {pts}★"
        if red:
            s += "; redeemed " + ", ".join(
                f"{next((r['title'] for r in rewards if r['id'] == rid), rid)} ×{n}"
                for rid, n in red.items())
        lines.append(s)
    if rewards:
        lines.append("Rewards: " + "; ".join(f"{r['title']} ({r.get('cost', 0)}★)" for r in rewards))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# family_task_metrics
# ---------------------------------------------------------------------------

def _owner(inst: dict) -> str:
    """Whose record an instance counts toward: whoever did it, else whoever it
    was assigned to, else the shared pool."""
    if inst.get("status") == "done" and inst.get("by"):
        return inst["by"]
    return inst.get("assignee") or inst.get("person") or "shared"


def _bucket(inst: dict) -> str:
    st = inst.get("status")
    if st == "late":
        return "open" if inst.get("inGrace") else "missed"
    if st == "todo":
        return "open"
    return st  # done | skipped | missed


def summarize(instances: list[dict]) -> dict:
    """Per-owner and per-chore tallies of done/skipped/missed/open."""
    per_person: dict[str, dict] = {}
    per_chore: dict[str, dict] = {}
    for i in instances:
        b = _bucket(i)
        pp = per_person.setdefault(_owner(i), {"done": 0, "skipped": 0, "missed": 0, "open": 0,
                                               "stars": 0})
        pp[b] += 1
        if b == "done":
            pp["stars"] += int(i.get("reward") or 0)
        pc = per_chore.setdefault(i["choreId"], {"title": i.get("title"), "done": 0,
                                                 "skipped": 0, "missed": 0, "open": 0})
        pc[b] += 1
    return {"people": per_person, "chores": per_chore}


def _rate(t: dict) -> float | None:
    n = t["done"] + t["missed"]
    return (t["done"] / n) if n else None


def _pct(x) -> str:
    return "n/a" if x is None else f"{round(x * 100)}%"


def family_task_metrics(days: int = 7, person: str = "", compare: bool = True) -> str:
    try:
        days = max(1, min(int(days or 7), 90))
        today = _today()
        frm = _add_days(today, -(days - 1))
        cur = _get("/api/board/instances", {"from": frm, "to": today})
        people = cur.get("people") or []
        pid = _resolve_person(person, people) if person else None
        insts = cur.get("instances") or []
        if pid:
            insts = [i for i in insts if _owner(i) == pid]
        s = summarize(insts)
        prev_s = None
        if compare:
            pfrom, pto = _add_days(frm, -days), _add_days(frm, -1)
            prev = _get("/api/board/instances", {"from": pfrom, "to": pto})
            pinsts = prev.get("instances") or []
            if pid:
                pinsts = [i for i in pinsts if _owner(i) == pid]
            prev_s = summarize(pinsts)

        lines = [f"Chore metrics, last {days} day(s) ({frm} to {today})"
                 + (f" for {_name(pid, people)}" if pid else "") + ":",
                 "Completion rate = done / (done + missed); skipped (on purpose) "
                 "and still-open instances don't count against it."]
        order = [p["id"] for p in people] + ["shared"]
        for o in sorted(s["people"], key=lambda k: order.index(k) if k in order else 99):
            t = s["people"][o]
            line = (f"  {_name(o, people)}: {_pct(_rate(t))} "
                    f"(done {t['done']}, missed {t['missed']}, skipped {t['skipped']}, "
                    f"open {t['open']}, earned {t['stars']}★)")
            if prev_s is not None and o in prev_s["people"]:
                pr = _rate(prev_s["people"][o])
                line += f"; previous {days}d: {_pct(pr)}"
            lines.append(line)
        if not s["people"]:
            lines.append("  No chore instances in that window.")
        tot = {k: sum(t[k] for t in s["people"].values()) for k in ("done", "missed", "skipped", "open")}
        overall = f"Overall: {_pct(_rate(tot))} (done {tot['done']}, missed {tot['missed']})"
        if prev_s is not None and prev_s["people"]:
            ptot = {k: sum(t[k] for t in prev_s["people"].values()) for k in ("done", "missed")}
            overall += f"; previous {days}d: {_pct(_rate(ptot))}"
        lines.append(overall)

        worst = sorted((c for c in s["chores"].values() if c["missed"]),
                       key=lambda c: (-c["missed"], c["title"] or ""))[:5]
        if worst:
            lines.append("Most missed: " + "; ".join(
                f"{c['title']} ({c['missed']}× missed, {c['done']}× done)" for c in worst))
        best = sorted((c for c in s["chores"].values() if c["done"] and not c["missed"]),
                      key=lambda c: -c["done"])[:3]
        if best:
            lines.append("Never missed: " + "; ".join(f"{c['title']} ({c['done']}×)" for c in best))
        streak = cur.get("streak") or {}
        if streak:
            lines.append("Current streaks: " + ", ".join(
                f"{_name(k, people)} {v}d" for k, v in streak.items() if not pid or k == pid)
                + f"; household {cur.get('household', 0)}d")
        pts = cur.get("points") or {}
        if pts:
            lines.append("Star balances: " + ", ".join(
                f"{_name(k, people)} {v}★" for k, v in pts.items() if not pid or k == pid))
        return "\n".join(lines)
    except BoardError as exc:
        return f"Family board error: {exc}"


# ---------------------------------------------------------------------------
# manage_family_chore
# ---------------------------------------------------------------------------

def _new_id() -> str:
    return "pb" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


def _months(value) -> list[int] | None:
    if value in (None, "", []):
        return None
    if isinstance(value, str):
        value = [v for v in value.replace(",", " ").split() if v]
    out = []
    for v in value:
        s = str(v).strip().lower()
        if s.isdigit():
            m = int(s)
        else:
            m = next((i + 1 for i, n in enumerate(MONTHS) if s[:3] == n.lower()), 0)
        if not 1 <= m <= 12:
            raise BoardError(f"can't read month {v!r}")
        if m not in out:
            out.append(m)
    return out


def _chore_summary(c: dict, people: list[dict]) -> str:
    s = (f"**{c['title']}** ({c.get('area') or 'General'}): {_assign_label(c, people)}; "
         f"{recurrence_label(c)}")
    if c.get("reward"):
        s += f"; {c['reward']}★"
    if c.get("note"):
        s += f"; note: {c['note']}"
    if c.get("checklist"):
        s += "; checklist: " + ", ".join(c["checklist"])
    return s


def manage_family_chore(action: str, chore: str = "", title: str = "", person: str = "",
                        area: str = "", recurrence: str = "", day_of_week=None,
                        day_of_month=None, months=None, due: str = "", time: str = "",
                        reward=None, note: str | None = None, checklist=None,
                        rotation=None, confirmed: bool = False) -> str:
    try:
        board = _get("/api/board")
        people = _people_index(board)
        chores = board.get("chores") or []
        if action == "delete":
            c = _resolve_chore(chore, chores)
            if not confirmed:
                return (f"Delete chore {_chore_summary(c, people)} [{c['id']}]?\n"
                        "⚠️ Warning: this also erases its whole done/skip history and audit "
                        f"log; it cannot be undone. {CONFIRM}")
            _post("/api/board/remove", {"kind": "chore", "id": c["id"]})
            return f"🗑️ Deleted chore {c['title']}."

        if action == "create":
            if not title.strip():
                return "Error: create needs a title."
            c = {"id": _new_id(), "title": title.strip(), "area": "General",
                 "person": "shared", "recurrence": "daily"}
            before = None
        elif action in ("edit", "reassign"):
            before = _resolve_chore(chore, chores)
            c = dict(before)
            if title.strip():
                c["title"] = title.strip()
        else:
            return f"Error: unknown action {action!r}."

        if person:
            c["person"] = _resolve_person(person, people)
            if c["person"] != "shared":
                c.pop("rotation", None)
        if rotation is not None:
            names = rotation if isinstance(rotation, list) else \
                [x for x in str(rotation).replace(",", " ").split() if x]
            if names:
                members = [_resolve_person(n, people, allow_shared=False) for n in names]
                c["person"] = "shared"
                c["rotation"] = {"members": members, "turn": 0}
            else:
                c.pop("rotation", None)
        if area.strip():
            c["area"] = area.strip()
        if recurrence:
            rec = recurrence.strip().lower().replace(" ", "").replace("-", "")
            rec = {"weekday": "weekdays", "everyother": "everyotherday", "onetime": "once",
                   "oneoff": "once"}.get(rec, rec)
            if rec not in RECURRENCES:
                return f"Error: recurrence must be one of {', '.join(RECURRENCES)}."
            if rec != c.get("recurrence"):
                for k in ("dow", "dom", "due", "months", "anchoredOn"):
                    c.pop(k, None)
            c["recurrence"] = rec
        rec = c.get("recurrence") or "daily"
        if day_of_week not in (None, ""):
            c["dow"] = _parse_dow(day_of_week)
        if day_of_month not in (None, ""):
            dom = int(day_of_month)
            if not 1 <= dom <= 31:
                return "Error: day_of_month must be 1..31."
            c["dom"] = dom
        if due:
            c["due"] = _parse_date(due)
        if months is not None:
            m = _months(months)
            if rec == "seasonal":
                c["months"] = m
            else:
                c["seasonal"] = m
                if m is None:
                    c.pop("seasonal", None)
        if time != "":
            c["time"] = time.strip() or None
        if reward not in (None, ""):
            c["reward"] = int(reward) or None
        if note is not None:
            c["note"] = note.strip() or None
        if checklist is not None:
            items = checklist if isinstance(checklist, list) else \
                [x.strip() for x in str(checklist).split(";")]
            c["checklist"] = [x for x in (str(i).strip() for i in items) if x] or None

        if rec == "weekly" and c.get("dow") is None:
            return "Error: a weekly chore needs day_of_week."
        if rec == "monthly" and c.get("dom") is None:
            return "Error: a monthly chore needs day_of_month."
        if rec == "once" and not c.get("due"):
            return "Error: a one-time chore needs a due date."
        if rec == "seasonal" and not c.get("months"):
            return "Error: a seasonal chore needs months (e.g. [9, 10])."
        if rec == "everyotherday" and not c.get("anchoredOn"):
            c["anchoredOn"] = _today()

        if before is not None:
            changes = [k for k in sorted(set(before) | set(c))
                       if before.get(k) != c.get(k) and k != "created"]
            if not changes:
                return "Nothing to change: the chore already looks like that."
        if not confirmed:
            verb = "Create" if before is None else "Update"
            msg = f"{verb} chore {_chore_summary(c, people)}"
            if before is not None:
                msg += f"\n(was: {_chore_summary(before, people)})"
            return f"{msg}\n{CONFIRM}"
        body = {k: v for k, v in c.items() if v is not None and k != "created"}
        _post("/api/board/chore", body)
        return ("✅ Created" if before is None else "✅ Updated") + \
            f" chore {_chore_summary(c, people)} [{c['id']}]."
    except BoardError as exc:
        return f"Family board error: {exc}"
    except (TypeError, ValueError) as exc:
        return f"Error: {exc}"


# ---------------------------------------------------------------------------
# mark_family_chore
# ---------------------------------------------------------------------------

def mark_family_chore(action: str, chore: str, person: str = "", date: str = "",
                      reason: str = "", by: str = "", confirmed: bool = False) -> str:
    try:
        board = _get("/api/board")
        people = _people_index(board)
        c = _resolve_chore(chore, board.get("chores") or [])
        state = board.get("state") or {}
        today = state.get("date") or _today()
        day = _parse_date(date, default=today)
        if day > today:
            return "Error: can't mark a chore for a future date."
        done_info = (state.get("doneLog") or {}).get(c["id"], {}).get(day)
        ign_info = (state.get("ignoreLog") or {}).get(c["id"], {}).get(day)
        by_id = _resolve_person(by, people, allow_shared=False) if by else None
        rot = c.get("rotation") or {}
        rr = c.get("person") == "shared" and bool(rot.get("members"))
        # The board moves a round-robin turn on for today's instance or an
        # overdue one still on the board; older back-fills leave it alone.
        moves_turn = rr and day >= _add_days(today, -7)
        label = f"{c['title']} on {day}"

        if action == "done":
            if done_info:
                return f"{label} is already done (by {_name(done_info.get('by'), people)})."
            if person:
                doer = _resolve_person(person, people, allow_shared=False)
            elif c.get("person") and c["person"] != "shared":
                doer = c["person"]
            elif rr:
                n = len(rot["members"])
                doer = rot["members"][int(rot.get("turn") or 0) % n]
            else:
                return f"Error: {c['title']} is shared; say who did it (person)."
            pts = int(c.get("reward") or 0)
            if not confirmed:
                return (f"Mark {label} done by {_name(doer, people)}"
                        + (f" (+{pts}★)" if pts else "")
                        + (" (replaces its skip)" if ign_info else "") + f"? {CONFIRM}")
            body = {"choreId": c["id"], "person": doer, "by": by_id or doer,
                    "points": pts, "date": day}
            if moves_turn:
                body["rotation"] = {"members": rot["members"],
                                    "turn": (int(rot.get("turn") or 0) + 1) % len(rot["members"])}
            _post("/api/board/done", body)
            return f"✅ {label} marked done by {_name(doer, people)}" + (f", +{pts}★." if pts else ".")

        if action == "undo":
            if not done_info:
                return f"{label} isn't marked done, nothing to undo."
            if not confirmed:
                return f"Undo the done on {label} (by {_name(done_info.get('by'), people)})? {CONFIRM}"
            body = {"choreId": c["id"], "person": done_info.get("by") or "someone",
                    "undo": True, "date": day}
            if moves_turn:
                n = len(rot["members"])
                body["rotation"] = {"members": rot["members"],
                                    "turn": (int(rot.get("turn") or 0) - 1 + n) % n}
            _post("/api/board/done", body)
            return f"↩️ Undid the done on {label}."

        if action == "skip":
            if ign_info and ign_info.get("reason") != "missed":
                return f"{label} is already skipped."
            if not confirmed:
                warn = " Its done (and stars) will be taken back until the skip is undone." \
                    if done_info else ""
                return (f"Skip {label}" + (f" ({reason})" if reason else "")
                        + f"?{warn} {CONFIRM}")
            _post("/api/board/ignore", {"choreId": c["id"], "by": by_id,
                                        "reason": reason or None, "date": day})
            return f"⏭️ Skipped {label}."

        if action == "unskip":
            if not ign_info:
                return f"{label} isn't skipped."
            if not confirmed:
                return f"Clear the skip on {label}? {CONFIRM}"
            _post("/api/board/ignore", {"choreId": c["id"], "undo": True, "date": day})
            return f"↩️ Cleared the skip on {label}."

        return f"Error: unknown action {action!r}."
    except BoardError as exc:
        return f"Family board error: {exc}"


# ---------------------------------------------------------------------------
# family_calendar
# ---------------------------------------------------------------------------

def _rfc3339(day: str, end: bool = False) -> str:
    d = datetime.date.fromisoformat(day)
    t = datetime.time(23, 59, 59) if end else datetime.time(0, 0)
    return datetime.datetime.combine(d, t, tzinfo=_tz()).isoformat()


def _fetch_events(start: str, end: str) -> list[dict]:
    data = _get("/api/gcal", {"timeMin": _rfc3339(start), "timeMax": _rfc3339(end, end=True)})
    if not data.get("events") and data.get("message"):
        raise BoardError(data["message"])
    return data.get("events") or []


def _ev_day(ev: dict) -> str:
    s = ev.get("start") or ""
    if len(s) == 10:
        return s
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(_tz()).date().isoformat()
    except ValueError:
        return s[:10]


def _ev_time(iso: str) -> str:
    try:
        t = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(_tz())
    except ValueError:
        return iso
    return t.strftime("%I:%M %p").lstrip("0")


def _hhmm(iso: str) -> str:
    try:
        return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(_tz()).strftime("%H:%M")
    except ValueError:
        return ""


def _ev_line(ev: dict) -> str:
    if ev.get("allDay"):
        when = "All day"
        end = ev.get("end") or ""
        if len(end) == 10 and _add_days(ev.get("start"), 1) < end:
            when += f" (to {_add_days(end, -1)})"
    else:
        when = f"{_ev_time(ev.get('start', ''))}–{_ev_time(ev.get('end', ''))}"
    src = ev.get("source") or "family"
    s = f"  {when}: {ev.get('title')}"
    if ev.get("location"):
        s += f" @ {ev['location']}"
    if src != "family":
        s += f" [{src} calendar, read-only]"
    else:
        s += f" [id {ev.get('id')}]"
    if ev.get("recurringEventId") or ev.get("recurrence"):
        s += " (repeats)"
    return s


def _norm_rrule(repeat: str) -> str:
    r = (repeat or "").strip()
    if not r:
        return ""
    r = r[6:] if r.upper().startswith("RRULE:") else r
    if "FREQ=" not in r.upper():
        raise BoardError("repeat must be an RRULE such as FREQ=WEEKLY;BYDAY=TU")
    return "RRULE:" + r


def _hm(value: str) -> str:
    v = (value or "").strip().lower().replace(".", "")
    if not v:
        return ""
    for fmt in ("%H:%M", "%I:%M %p", "%I:%M%p", "%I %p", "%I%p", "%H"):
        try:
            return datetime.datetime.strptime(v, fmt).strftime("%H:%M")
        except ValueError:
            continue
    raise BoardError(f"can't read time {value!r}; use HH:MM")


def family_calendar(action: str, start_date: str = "", days: int = 7, event_id: str = "",
                    date: str = "", title: str = "", time: str = "", end_time: str = "",
                    all_day: bool | None = None, end_date: str = "", location: str | None = None,
                    description: str | None = None, repeat: str = "", new_date: str = "",
                    confirmed: bool = False) -> str:
    try:
        if action == "list":
            start = _parse_date(start_date, default=_today())
            n = max(1, min(int(days or 7), 31))
            end = _add_days(start, n - 1)
            events = _fetch_events(start, end)
            if not events:
                return f"No calendar events {start} to {end}."
            by_day: dict[str, list] = {}
            for ev in events:
                by_day.setdefault(_ev_day(ev), []).append(ev)
            lines = [f"Calendar {start} to {end}:"]
            for d in sorted(by_day):
                dd = datetime.date.fromisoformat(d)
                lines.append(f"\n**{DOW_NAMES[(dd.weekday() + 1) % 7]} {d}**")
                evs = sorted(by_day[d], key=lambda e: (not e.get("allDay"), e.get("start") or ""))
                lines.extend(_ev_line(e) for e in evs)
            return "\n".join(lines)

        tz_name = getattr(_tz(), "key", "") or FAMILY_BOARD_TZ or "UTC"

        if action == "create":
            if not title.strip() or not date:
                return "Error: create needs a title and a date."
            day = _parse_date(date)
            t = _hm(time)
            is_all_day = bool(all_day) or not t
            evt = {"title": title.strip(), "date": day, "allDay": is_all_day,
                   "timeZone": tz_name}
            if not is_all_day:
                evt["time"] = t
                if end_time:
                    evt["endTime"] = _hm(end_time)
            elif end_date:
                # Google's all-day end date is exclusive.
                evt["endDate"] = _add_days(_parse_date(end_date), 1)
            if location:
                evt["location"] = location
            if description:
                evt["description"] = description
            if repeat:
                evt["recurrence"] = _norm_rrule(repeat)
            if not confirmed:
                when = "all day" if is_all_day else f"{t}" + (f"–{evt.get('endTime')}" if evt.get("endTime") else " (30 min)")
                extra = "".join([f", until {end_date}" if end_date and is_all_day else "",
                                 f" @ {location}" if location else "",
                                 f", repeats {evt['recurrence'][6:]}" if repeat else ""])
                return f"Add to the family calendar: **{evt['title']}** on {day}, {when}{extra}? {CONFIRM}"
            res = _post("/api/gcal/event", evt)
            ev = res.get("event") or {}
            return f"📅 Added **{ev.get('title', evt['title'])}** on {day} [id {ev.get('id', '?')}]."

        if action == "edit":
            if not event_id or not date:
                return ("Error: edit needs event_id and the event's current date "
                        "(list the calendar first to find them).")
            day = _parse_date(date)
            ev = next((e for e in _fetch_events(day, day) if e.get("id") == event_id), None)
            if ev is None:
                return f"Error: no event {event_id} on {day}."
            if (ev.get("source") or "family") != "family":
                return "Error: that event is on a personal calendar; only the family calendar can be edited."
            new_day = _parse_date(new_date, default=day)
            evt = {"id": event_id, "title": ev.get("title"), "date": new_day,
                   "allDay": bool(ev.get("allDay")), "timeZone": tz_name,
                   "location": ev.get("location") or "", "description": ev.get("description") or ""}
            if not evt["allDay"]:
                evt["time"], evt["endTime"] = _hhmm(ev.get("start", "")), _hhmm(ev.get("end", ""))
            elif len(ev.get("end") or "") == 10:
                # keep a multi-day all-day event's length when it moves
                span = (datetime.date.fromisoformat(ev["end"])
                        - datetime.date.fromisoformat(ev["start"][:10])).days
                evt["endDate"] = _add_days(new_day, max(span, 1))
            if ev.get("recurringEventId"):
                evt["instance"] = True
            elif ev.get("recurrence"):
                evt["recurrence"] = ev["recurrence"][0]
            if title.strip():
                evt["title"] = title.strip()
            if end_date and evt["allDay"]:
                evt["endDate"] = _add_days(_parse_date(end_date), 1)
            if all_day is True:
                evt["allDay"] = True
                evt.pop("time", None)
                evt.pop("endTime", None)
            if time:
                evt["allDay"] = False
                evt["time"] = _hm(time)
                evt.pop("endDate", None)
                if not end_time and ev.get("end") and not ev.get("allDay"):
                    # keep the same length
                    s0 = datetime.datetime.strptime(_hhmm(ev["start"]), "%H:%M")
                    e0 = datetime.datetime.strptime(_hhmm(ev["end"]), "%H:%M")
                    s1 = datetime.datetime.strptime(evt["time"], "%H:%M")
                    evt["endTime"] = (s1 + (e0 - s0)).strftime("%H:%M")
            if end_time:
                evt["endTime"] = _hm(end_time)
            if location is not None:
                evt["location"] = location
            if description is not None:
                evt["description"] = description
            if repeat:
                if evt.get("instance"):
                    return "Error: that is one occurrence of a repeating event; its repeat rule can't be changed here."
                evt["recurrence"] = _norm_rrule(repeat)
            if not confirmed:
                when = "all day" if evt["allDay"] else f"{evt['time']}–{evt.get('endTime', '')}"
                was = "all day" if ev.get("allDay") else \
                    f"{_hhmm(ev.get('start', ''))}–{_hhmm(ev.get('end', ''))}"
                scope = " (this occurrence only)" if evt.get("instance") else ""
                moved = f" to {new_day}" if new_day != day else ""
                return (f"Change **{ev.get('title')}** on {day}{scope}: now **{evt['title']}**{moved}, "
                        f"{when}" + (f" @ {evt['location']}" if evt.get("location") else "")
                        + f" (was {was}" + (f" @ {ev['location']}" if ev.get("location") else "")
                        + f")? {CONFIRM}")
            res = _post("/api/gcal/event/update", evt)
            out = res.get("event") or {}
            return f"📅 Updated **{out.get('title', evt['title'])}** on {new_day}."

        return f"Error: unknown action {action!r}."
    except BoardError as exc:
        return f"Family board error: {exc}"


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

def tool_definitions() -> list[dict]:
    return [
        {
            "name": "query_family_board",
            "description": (
                "Read the family chore wall board. action='today': every chore on the board "
                "today grouped by person with done / to-do / overdue status, stars and streaks "
                "(answers 'has X done their chores?'). action='person': one person's chores "
                "today plus their stars and reward goal. action='list_chores': every chore "
                "with who it belongs to and its schedule (filter by person or area). "
                "action='chore_history': one chore's log of done / skipped / missed / edited. "
                "action='rewards': star balances and the reward catalog. "
                "Chore ids are shown in [brackets]; pass them to other family tools when a "
                "title is ambiguous."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["today", "person", "list_chores", "chore_history", "rewards"]},
                    "person": {"type": "string", "description": "Family member's name (or 'shared')."},
                    "chore": {"type": "string", "description": "Chore id or title (chore_history)."},
                    "area": {"type": "string", "description": "Area filter for list_chores, e.g. Kitchen."},
                    "limit": {"type": "integer", "description": "Max history events.", "default": 20},
                },
                "required": ["action"],
            },
        },
        {
            "name": "family_task_metrics",
            "description": (
                "Chore completion metrics from the family board over the last N days: per-person "
                "completion rate (done vs missed), skipped and still-open counts, stars earned, "
                "the most-missed and never-missed chores, streaks and star balances, compared "
                "with the N days before. Use for 'how are the kids doing on chores?', weekly "
                "digests and trends."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Window length, 1-90.", "default": 7},
                    "person": {"type": "string", "description": "Limit to one family member."},
                    "compare": {"type": "boolean", "default": True,
                                "description": "Also show the previous window for comparison."},
                },
                "required": [],
            },
        },
        {
            "name": "manage_family_chore",
            "description": (
                "Create, edit, reassign or delete a chore on the family board. Edits change only "
                "the fields you pass. ALWAYS call with confirmed=false first: it returns a "
                "preview to show the user. Do not call with confirmed=true yourself; the bot "
                "runs it when the user replies yes. Recurrence: daily, weekdays, everyotherday, "
                "weekly (needs day_of_week), monthly (needs day_of_month), once (needs due), "
                "seasonal (once per season; needs months). For other recurrences, months limits "
                "the chore to those months of the year."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["create", "edit", "reassign", "delete"]},
                    "chore": {"type": "string", "description": "Existing chore id or title (edit/reassign/delete)."},
                    "title": {"type": "string", "description": "Title (create) or new title (edit)."},
                    "person": {"type": "string",
                               "description": "Who it belongs to: a name, or 'shared' for anyone."},
                    "rotation": {"type": "array", "items": {"type": "string"},
                                 "description": "Names that take turns (makes it a shared round-robin chore). Empty list removes the rotation."},
                    "area": {"type": "string", "description": "Area such as Kitchen, Bedroom, Yard."},
                    "recurrence": {"type": "string", "enum": list(RECURRENCES)},
                    "day_of_week": {"type": "string", "description": "For weekly: Monday..Sunday."},
                    "day_of_month": {"type": "integer", "description": "For monthly: 1-31."},
                    "months": {"type": "array", "items": {"type": "integer"},
                               "description": "Months 1-12 (seasonal window)."},
                    "due": {"type": "string", "description": "For once: YYYY-MM-DD."},
                    "time": {"type": "string", "description": "Time of day label, e.g. morning, evening."},
                    "reward": {"type": "integer", "description": "Stars earned when done."},
                    "note": {"type": "string"},
                    "checklist": {"type": "array", "items": {"type": "string"}},
                    "confirmed": {"type": "boolean", "default": False},
                },
                "required": ["action"],
            },
        },
        {
            "name": "mark_family_chore",
            "description": (
                "Check off a chore on the family board, undo a check-off, skip an instance "
                "(with a reason, e.g. 'sick'), or clear a skip. Done credits the chore's stars "
                "and moves a round-robin turn on, just like tapping it on the board. date "
                "defaults to today. ALWAYS call with confirmed=false first and show the "
                "preview; the bot runs it when the user replies yes."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["done", "undo", "skip", "unskip"]},
                    "chore": {"type": "string", "description": "Chore id or title."},
                    "person": {"type": "string",
                               "description": "Who did it (done). Needed for shared chores without a rotation."},
                    "date": {"type": "string", "description": "YYYY-MM-DD, today or yesterday. Default today."},
                    "reason": {"type": "string", "description": "Why it is skipped."},
                    "by": {"type": "string", "description": "Who is recording it, if different."},
                    "confirmed": {"type": "boolean", "default": False},
                },
                "required": ["action", "chore"],
            },
        },
        {
            "name": "family_calendar",
            "description": (
                "The family Google Calendar shown on the wall board. action='list': events from "
                "start_date for N days, including read-only personal calendars (labelled). "
                "action='create': add an event to the family calendar (no time = all-day; no "
                "end_time = 30 minutes; repeat takes an RRULE like FREQ=WEEKLY;BYDAY=TU). "
                "action='edit': change a family-calendar event by event_id plus its current date "
                "(list first to get both); only the fields you pass change. Deleting events is "
                "not supported. For create and edit, ALWAYS call with confirmed=false first and "
                "show the preview; the bot runs it when the user replies yes."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "create", "edit"]},
                    "start_date": {"type": "string", "description": "list: first day (default today)."},
                    "days": {"type": "integer", "description": "list: number of days, 1-31.", "default": 7},
                    "event_id": {"type": "string", "description": "edit: the event's id from list."},
                    "date": {"type": "string",
                             "description": "create: the event's date. edit: its current date. YYYY-MM-DD, 'tomorrow' or a weekday."},
                    "title": {"type": "string"},
                    "time": {"type": "string", "description": "Start time, HH:MM 24h or '3:30 pm'."},
                    "end_time": {"type": "string"},
                    "all_day": {"type": "boolean"},
                    "end_date": {"type": "string", "description": "Last day of a multi-day all-day event."},
                    "location": {"type": "string"},
                    "description": {"type": "string"},
                    "repeat": {"type": "string", "description": "RRULE, e.g. FREQ=WEEKLY;BYDAY=MO,WE."},
                    "new_date": {"type": "string", "description": "edit: move the event to this date."},
                    "confirmed": {"type": "boolean", "default": False},
                },
                "required": ["action"],
            },
        },
    ]


def execute(name: str, inputs: dict) -> str | None:
    """Dispatch a family-board tool call; None when `name` isn't one of ours."""
    i = inputs or {}
    if name == "query_family_board":
        return query_family_board(i.get("action", "today"), i.get("person", ""), i.get("chore", ""),
                                  i.get("area", ""), i.get("limit", 20))
    if name == "family_task_metrics":
        return family_task_metrics(i.get("days", 7), i.get("person", ""), i.get("compare", True))
    if name == "manage_family_chore":
        return manage_family_chore(
            i.get("action", ""), i.get("chore", ""), i.get("title", ""), i.get("person", ""),
            i.get("area", ""), i.get("recurrence", ""), i.get("day_of_week"), i.get("day_of_month"),
            i.get("months"), i.get("due", ""), i.get("time", ""), i.get("reward"), i.get("note"),
            i.get("checklist"), i.get("rotation"), bool(i.get("confirmed", False)))
    if name == "mark_family_chore":
        return mark_family_chore(i.get("action", ""), i.get("chore", ""), i.get("person", ""),
                                 i.get("date", ""), i.get("reason", ""), i.get("by", ""),
                                 bool(i.get("confirmed", False)))
    if name == "family_calendar":
        return family_calendar(
            i.get("action", "list"), i.get("start_date", ""), i.get("days", 7), i.get("event_id", ""),
            i.get("date", ""), i.get("title", ""), i.get("time", ""), i.get("end_time", ""),
            i.get("all_day"), i.get("end_date", ""), i.get("location"), i.get("description"),
            i.get("repeat", ""), i.get("new_date", ""), bool(i.get("confirmed", False)))
    return None
