"""Learn from what Hermes recorded before the plugin started observing the profile.

Hermes keeps every session of a profile in `<HERMES_HOME>/state.db`. The first passes of the loop read the
last BACKFILL_DAYS of it, read-only and a few sessions at a time, and replay them through the code the
hooks use: tool results become tool counts and failure signatures, user messages open turns (and, for
the owner, correction and thanks counters), and skills loaded with skill_view are charged with the
outcome of their turn. A failure that recurred last week can therefore become a lesson on the first day.

Only messages older than `observed_since` (when the plugin first loaded in the profile) are read, so
nothing the hooks counted is counted twice. Each slice is written in one transaction together with its
cursor, so a pass that is killed half way leaves nothing half counted. Text is classified in memory and
discarded, exactly as in the hooks; the history itself is never modified.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .experience import experience, record
from .operations import batched
from .signals import DEFAULT_SLOW_TURN_MS, FeedbackSensor, TurnTracker, record_skill_correction, record_skill_event, record_tool
from .telemetry import ResearchTracker

BACKFILL_DAYS = 14
SLICE_SESSIONS = 25
_LOCAL_SOURCES = frozenset({"local", "cli", "tui", "gui", "desktop", "acp", "oneshot"})
_INTERRUPTED_ENDS = frozenset({"startup_orphan_reap", "ws_orphan_reap", "session_reset"})
_FINAL = frozenset({"stop", "end_turn", "verification_required"})
_REQUIRED = {"sessions": {"id", "source", "started_at"},
             "messages": {"id", "session_id", "role", "content", "tool_call_id", "tool_calls", "tool_name", "timestamp"}}
_OPTIONAL = {"sessions": ("parent_session_id", "ended_at", "end_reason", "user_id"), "messages": ("finish_reason",)}
_TEXT_ERROR = re.compile(r"(?:error|erro)\b\s*[:\-]|traceback \(most recent call last\)|blocked:", re.I)
_STATE_KEYS = ("observed_since", "backfill_cursor", "backfill_done", "backfill_totals")


def _day_start(day: str) -> float:
    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


def mark_observed(home: Path, *, now: float) -> None:
    """Remember when the plugin started observing this profile. The first mark wins; for a database that
    already holds live data (an upgrade), the earliest live day counts as the start."""
    def job(conn):
        if conn.execute("SELECT 1 FROM cursor WHERE key='observed_since'").fetchone():
            return
        marks = [now]
        first = conn.execute("SELECT MIN(first_seen) FROM failure").fetchone()[0]
        if isinstance(first, (int, float)):
            marks.append(float(first))
        for table in ("tool_daily", "turn_daily"):
            day = conn.execute(f"SELECT MIN(day) FROM {table}").fetchone()[0]
            if isinstance(day, str):
                try:
                    marks.append(_day_start(day))
                except ValueError:
                    pass
        conn.execute("INSERT OR IGNORE INTO cursor VALUES ('observed_since', ?)", (repr(min(marks)),))

    record(home, job, now=now)


def _status(content) -> str:
    """ok, error or blocked, read from a stored tool result the way Hermes' tools report failures."""
    if not isinstance(content, str):
        return "ok"
    text = content.lstrip()
    if text[:1] == "{" and len(text) <= 262_144:
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, str) and error.strip():
                return "blocked" if error.lstrip()[:8].upper() == "BLOCKED:" else "error"
            code = data.get("exit_code")
            if (type(code) is int and code != 0) or data.get("success") is False:
                return "error"
            return "ok"
    head = text[:200]
    if _TEXT_ERROR.match(head):
        return "blocked" if head[:8].upper() == "BLOCKED:" else "error"
    return "ok"


def _calls(raw) -> dict:
    """{call id: (tool name, arguments)} from an assistant message's tool_calls column."""
    if not isinstance(raw, str) or len(raw) > 1_048_576:
        return {}
    try:
        calls = json.loads(raw)
    except ValueError:
        return {}
    found = {}
    for call in calls if isinstance(calls, list) else ():
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict):
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, str) and len(arguments) <= 65_536:
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = {}
        args = arguments if isinstance(arguments, dict) else {}
        for key in ("id", "call_id"):
            if isinstance(call.get(key), str) and call[key]:
                found[call[key]] = (function.get("name"), args)
    return found


def _columns(source: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in source.execute(f"PRAGMA table_info({table})")}


def _state(home: Path) -> dict:
    with experience(home) as conn:
        values = dict(conn.execute(f"SELECT key, value FROM cursor WHERE key IN ({','.join('?' * len(_STATE_KEYS))})",
                                   _STATE_KEYS).fetchall())
    try:
        totals = json.loads(values.get("backfill_totals") or "{}")
    except ValueError:
        totals = {}
    position = (values.get("backfill_cursor") or "").partition("|")
    try:
        since = float(values["observed_since"]) if "observed_since" in values else None
        started = float(position[0]) if position[0] else None
    except ValueError:
        since, started = None, None
    return {"since": since, "cursor": (started, position[2]), "done": values.get("backfill_done"),
            "totals": {key: int(totals.get(key, 0)) for key in ("sessions", "turns", "tool_calls", "failures")}}


class _Replay:
    """Replays sessions through the hooks' trackers. One instance per pass keeps correction windows intact."""

    def __init__(self, home: Path, *, sample: bool, owner_sender_ids, slow_ms: int):
        self.home = home
        self.sample = sample
        self.turns = TurnTracker(home, slow_ms=slow_ms)
        self.sensor = FeedbackSensor(home, owner_sender_ids=owner_sender_ids)
        self.research = ResearchTracker(home)   # web research turns: timing, calls and errors
        self.counts = {"sessions": 0, "turns": 0, "tool_calls": 0, "failures": 0}

    def session(self, source: sqlite3.Connection, row: dict, *, start: float, until: float) -> None:
        sid, parent = row["id"], row.get("parent_session_id") or None
        kind = row.get("source") or ""
        platform = "cli" if kind in _LOCAL_SOURCES else kind or "gateway"
        open_turn, final, last, answered = None, False, None, False
        calls: dict = {}
        self.counts["sessions"] += 1
        messages = source.execute("""SELECT id, role, content, tool_call_id, tool_calls, tool_name, timestamp,
              {finish} AS finish_reason FROM messages WHERE session_id=? AND timestamp >= ? AND timestamp < ?
            ORDER BY timestamp, id""".format(finish="finish_reason" if row.get("_has_finish") else "NULL"),
                                  (sid, start, until))
        for message in messages:
            role, stamp = message["role"], float(message["timestamp"])
            if role == "user":
                if open_turn and not answered:
                    continue   # sent before the agent answered: Hermes handles it in the same turn
                if open_turn:
                    self._finish(open_turn, final=final, ended=last, interrupted=True)
                content = message["content"]
                payload = {"session_id": sid, "platform": platform, "user_message": content if isinstance(content, str) else "",
                           "sender_id": row.get("user_id"), "parent_session_id": parent}
                if {"correction", "reask"} & set(self.sensor.observe(payload, clock=stamp, now=stamp)):
                    record_skill_correction(self.home, self.turns.recent_skills(sid, clock=stamp), now=stamp)
                open_turn, final, last, answered = f"backfill:{sid}:{message['id']}", False, stamp, False
                self.turns.start({"turn_id": open_turn, "session_id": sid, "platform": platform,
                                  "parent_session_id": parent}, clock=stamp)
                self.research.start({"turn_id": open_turn}, clock=stamp)
                self.counts["turns"] += 1
            elif role == "assistant":
                requested = _calls(message["tool_calls"])
                calls.update(requested)
                final, last, answered = message["finish_reason"] in _FINAL and not requested, stamp, True
            elif role == "tool":
                self._tool(message, calls.pop(message["tool_call_id"], (None, {})), sid=sid, turn=open_turn, stamp=stamp)
                final, last, answered = False, stamp, True
        if open_turn:
            continues = source.execute("SELECT 1 FROM messages WHERE session_id=? AND timestamp >= ? LIMIT 1",
                                       (sid, until)).fetchone()
            if continues is None:   # a turn cut by the start of observation is left to the hooks
                self._finish(open_turn, final=final, ended=last,
                             interrupted=(row.get("end_reason") or "") in _INTERRUPTED_ENDS)

    def _tool(self, message, call, *, sid, turn, stamp) -> None:
        name, args = call
        name = message["tool_name"] or name
        status = _status(message["content"])
        payload = {"tool_name": name, "status": status, "result": message["content"], "args": args,
                   "session_id": sid, "turn_id": turn, "tool_call_id": message["tool_call_id"]}
        failure = record_tool(self.home, payload, now=stamp, sample=self.sample)
        self.turns.tool(payload)
        self.research.tool(payload)
        self.counts["tool_calls"] += 1
        self.counts["failures"] += int(failure is not None)
        skill = args.get("name") if name == "skill_view" and status == "ok" else None
        if isinstance(skill, str) and skill:
            self.turns.skill({"action": "loaded", "skill_name": skill, "session_id": sid})
            record_skill_event(self.home, {"action": "loaded", "skill_name": skill}, now=stamp)

    def _finish(self, turn: str, *, final: bool, ended: float, interrupted: bool) -> None:
        if final:
            outcome = {"completed": True}
        elif interrupted:
            outcome = {"interrupted": True}
        else:
            outcome = {"failed": True}
        self.turns.finish({"turn_id": turn, **outcome}, clock=ended, now=ended)
        self.research.finish({"turn_id": turn}, clock=ended, now=ended)


def step(home: Path, *, now: float, budget_s: float = 20.0, slice_sessions: int = SLICE_SESSIONS, sample: bool = False,
         owner_sender_ids=(), slow_ms: int = DEFAULT_SLOW_TURN_MS) -> dict:
    """Replay the next slices of Hermes' history within `budget_s`. Returns the status and running totals."""
    home = Path(home)
    state = _state(home)
    if state["done"]:
        return {"status": state["done"], **state["totals"]}
    if state["since"] is None:
        return {"status": "waiting", **state["totals"]}   # the hooks have not marked the start yet
    path = home / "state.db"
    if not path.is_file():
        return _settle(home, "no_history", state["totals"])
    try:
        source = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return {"status": "unreadable", **state["totals"]}
    try:
        source.row_factory = sqlite3.Row
        try:
            present = {table: _columns(source, table) for table in _REQUIRED}
        except sqlite3.DatabaseError:
            return {"status": "unreadable", **state["totals"]}
        if any(not need <= present[table] for table, need in _REQUIRED.items()):
            return _settle(home, "unsupported_history", state["totals"])
        optional = ", ".join(f"{column}" if column in present["sessions"] else f"NULL AS {column}"
                             for column in _OPTIONAL["sessions"])
        until, start = state["since"], state["since"] - BACKFILL_DAYS * 86_400
        cursor_start, cursor_id = state["cursor"]
        totals = dict(state["totals"])
        replay = _Replay(home, sample=sample, owner_sender_ids=owner_sender_ids, slow_ms=slow_ms)
        deadline = time.monotonic() + budget_s
        while time.monotonic() < deadline:
            rows = source.execute(f"""SELECT id, source, started_at, {optional} FROM sessions
                WHERE started_at < ? AND COALESCE(ended_at, ?) >= ?
                  AND (? IS NULL OR started_at > ? OR (started_at = ? AND id > ?))
                ORDER BY started_at, id LIMIT ?""", (until, now, start, cursor_start, cursor_start, cursor_start,
                                                       cursor_id, slice_sessions)).fetchall()
            if not rows:
                return _settle(home, "done", totals)
            before = dict(replay.counts)
            with batched(home):
                for row in rows:
                    replay.session(source, {**dict(row), "_has_finish": "finish_reason" in present["messages"]},
                                   start=start, until=until)
                cursor_start, cursor_id = float(rows[-1]["started_at"]), str(rows[-1]["id"])
                for key in totals:
                    totals[key] += replay.counts[key] - before[key]
                position, summary = f"{cursor_start!r}|{cursor_id}", json.dumps(totals)
                record(home, lambda conn, p=position, s=summary: conn.executemany(
                    "INSERT OR REPLACE INTO cursor VALUES (?, ?)", (("backfill_cursor", p), ("backfill_totals", s))))
        return {"status": "running", **totals}
    except sqlite3.DatabaseError:
        return {"status": "unreadable", **state["totals"]}
    finally:
        source.close()


def _settle(home: Path, status: str, totals: dict) -> dict:
    """Finish the backfill for good. A history that is missing or of another shape is not retried."""
    record(home, lambda conn: conn.execute("INSERT OR REPLACE INTO cursor VALUES ('backfill_done', ?)", (status,)))
    return {"status": status, **totals}
