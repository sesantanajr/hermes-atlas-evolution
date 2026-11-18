"""Learn from what Hermes recorded before the plugin started observing the profile.

Hermes keeps every session of a profile in `<HERMES_HOME>/state.db`. The first passes of the loop read the
last BACKFILL_DAYS of it, read-only and a few sessions at a time, and replay them through the code the
hooks use: tool results become tool counts and failure signatures, user messages open turns (and, for
the owner, correction and thanks counters), and skills loaded with skill_view are charged with the
outcome of their turn. A failure that recurred last week can therefore become a lesson on the first day.

Only messages older than `observed_since` (when the plugin first loaded in the profile) are read, so
nothing the hooks counted is counted twice. Hermes' in-place compaction archives a session's rows and
re-inserts copies of them (same `message_uid`, sometimes a later timestamp or a shortened body) plus a
summary row: each logical message is replayed once and summaries are skipped. Each slice is written
synchronously in one transaction together with its cursor, after checking that no other pass moved the
cursor, so a pass that is killed half way leaves nothing half counted. A long session is written in parts of
about PAUSE_MESSAGES messages, each cut where one turn ended and the next has not begun, with a cursor inside
the session: the time budget is checked there and memory holds one part, never the whole session. One session
that cannot be replayed is skipped (from where it failed) and counted, never the whole backfill. Text is classified in memory and discarded, exactly as
in the hooks; the history itself is never modified.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .experience import RETENTION_DAYS, day_offset, experience, label, record, utc_day
from .operations import batched
from .signals import (DEFAULT_SLOW_TURN_MS, FeedbackSensor, TurnTracker, owner_turn, record_skill_correction,
                      record_skill_event, record_tool, surface_of)
from .telemetry import ResearchTracker

BACKFILL_DAYS = 14
SLICE_SESSIONS = 25
# Messages read between two pauses of the replay: at a pause what was counted is written with a cursor (inside a
# long session too) and the time budget is checked. A pause waits for a clean point (one turn over, the next not
# begun); a single turn longer than _FORCE_PAUSE messages is cut where it is.
PAUSE_MESSAGES = 2_000
_FORCE_PAUSE = 4 * PAUSE_MESSAGES
# A cursor inside a session: "<session start>|<session id>|@<message timestamp>|<message id as JSON>". A copy older
# than this version reads "<session id>|@…" as the id, so it goes on after that session and never counts it twice.
_INSIDE = re.compile(r"(.*)\|@([^|]+)\|([^|]+)", re.S)
# 2: compaction copies counted once. 3: the owner's failures per day and refused approvals per origin.
# A backfill done by an earlier version is redone once (see _redo). The revision is kept under its own key:
# backfill_version keeps saying "2", so a 0.6.0 copy still running beside this one (a gateway not restarted
# after the update) never redoes, and never undoes, the replay this one made.
VERSION = "3"
_COMPAT_VERSION = "2"
# damaged_history: Hermes' own state.db is damaged where the replay must read it (SQLite says corrupt or not a
# database): the replay ends there, so the lessons' baselines and tidying no longer wait for it for ever.
SETTLED = frozenset({"done", "no_history", "unsupported_history", "damaged_history"})
_TOTALS = ("sessions", "turns", "tool_calls", "failures", "skipped")
# Daily tables the replay writes. Whole UTC days before the first observed day hold nothing else.
_REPLAYED_DAILY = ("tool_daily", "failure_daily", "turn_daily", "feedback_daily", "skill_daily", "skill_outcome_daily",
                   "research_latency", "failure_owner_daily", "approval_daily")
_BASELINE_DAYS = 14   # lessons.BASELINE_DAYS, kept here to avoid an import cycle
_LOOKAHEAD = 64
_IDLE_SECONDS = 600   # a session quiet this long before observation began is not in the middle of a turn
_INTERRUPTED_ENDS = frozenset({"startup_orphan_reap", "ws_orphan_reap", "session_reset"})
_FINAL = frozenset({"stop", "end_turn", "verification_required"})
_REQUIRED = {"sessions": {"id", "source", "started_at"},
             "messages": {"id", "session_id", "role", "content", "tool_call_id", "tool_calls", "tool_name", "timestamp"}}
_OPTIONAL = {"sessions": ("parent_session_id", "ended_at", "end_reason", "user_id")}
_TEXT_ERROR = re.compile(r"(?:error|erro)\b\s*[:\-]|traceback \(most recent call last\)|blocked:", re.I)
# The result Hermes stores for a call its own deadline ended (agent/tool_executor.py, 0.23.1).
_TIMED_OUT = re.compile(r"Error executing tool '[^'\n]{1,96}': (?:timed out after|thread did not return)", re.I)
_STATE_KEYS = ("observed_since", "backfill_cursor", "backfill_done", "backfill_totals", "backfill_revision",
               "backfill_until", "backfill_owner", "backfill_floor")


def _day_start(day: str) -> float:
    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


def owner_fingerprint(owner_sender_ids=(), owner_platforms=()) -> str:
    """A short fingerprint of who counts as the owner. The replay decides, turn by turn, whether the owner was
    served, so a history replayed under other owner settings is replayed again (see step)."""
    body = json.dumps([sorted({str(item) for item in owner_sender_ids}), sorted({str(item) for item in owner_platforms})])
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


_NO_OWNER = owner_fingerprint()   # also what a replay recorded before the fingerprint existed
# Kept in backfill_done while this version replays: 0.5.0 and 0.6.0 leave a replay alone whenever backfill_done
# holds anything, so a copy of theirs still running beside this one never replays slices the old way.
_RUNNING = "running"
_UNKNOWN_OWNER = "unknown"         # replayed where the owner settings could not be read (see step)


def _revision_keys(fingerprint: str | None, floor: str) -> tuple:
    """The cursor rows that say which replay this is: the revision, what 0.6.0 must read, who was the owner,
    and the oldest day it reads (fixed for the whole replay, even across midnight)."""
    return (("backfill_revision", VERSION), ("backfill_version", _COMPAT_VERSION),
            ("backfill_owner", fingerprint or _UNKNOWN_OWNER), ("backfill_floor", floor))


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
    """ok, error, blocked or timeout, read from a stored tool result the way Hermes' tools report failures."""
    if not isinstance(content, str):
        return "ok"
    text = content.lstrip()
    if _TIMED_OUT.match(text):
        return "timeout"
    if text[:1] == "{" and len(text) <= 262_144:
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):   # a payload nested thousands deep is a result, not a crash
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
    except (ValueError, RecursionError):
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
            except (ValueError, RecursionError):
                arguments = {}
        args = arguments if isinstance(arguments, dict) else {}
        for key in ("id", "call_id"):
            if isinstance(call.get(key), str) and call[key]:
                found[call[key]] = (function.get("name"), args)
    return found


def _columns(source: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in source.execute(f"PRAGMA table_info({table})")}


class _CursorMoved(Exception):
    """Another pass advanced the backfill while this slice was read: the slice is dropped, not counted twice."""


def _busy(error: BaseException) -> bool:
    from .operations import _busy as busy

    return isinstance(error, sqlite3.Error) and busy(error)


def _damaged(error: BaseException) -> bool:
    from .operations import damaged

    return damaged(error)


class _Unreadable(Exception):
    """Hermes' history failed while a slice was read: the slice is dropped and the pass says so."""


def _count(value) -> int:
    return value if type(value) is int and value >= 0 else 0


def _state(home: Path) -> dict:
    with experience(home) as conn:
        values = dict(conn.execute(f"SELECT key, value FROM cursor WHERE key IN ({','.join('?' * len(_STATE_KEYS))})",
                                   _STATE_KEYS).fetchall())
    try:
        totals = json.loads(values.get("backfill_totals") or "{}")
    except (ValueError, RecursionError):
        totals = {}
    if not isinstance(totals, dict):
        totals = {}
    position = (values.get("backfill_cursor") or "").partition("|")
    try:
        since = float(values["observed_since"]) if "observed_since" in values else None
        started = float(position[0]) if position[0] else None
    except ValueError:
        since, started = None, None
    session, inside = position[2], None
    match = _INSIDE.fullmatch(session) if started is not None else None
    if match:
        try:
            message = json.loads(match.group(3))
            if type(message) in (int, str):
                inside = ((started, match.group(1)), (float(match.group(2)), message))
                session = match.group(1)
        except (ValueError, RecursionError):
            pass   # unreadable: the id as written, so that session is not replayed again
    try:
        until = float(values["backfill_until"]) if "backfill_until" in values else since
    except ValueError:
        until = since
    done = values.get("backfill_done")
    return {"since": since, "until": until, "cursor": (started, session), "inside": inside,
            "position": values.get("backfill_cursor"),
            "done": None if done == _RUNNING else done, "version": values.get("backfill_revision"),
            "owner": values.get("backfill_owner") or _NO_OWNER, "floor": values.get("backfill_floor"),
            "totals": {key: _count(totals.get(key)) for key in _TOTALS}}


def replay_until(home: Path) -> float | None:
    """The end of the history the backfill replays, or None before the plugin marked when it started observing."""
    return _state(Path(home))["until"]


def _redo(home: Path, *, since: float, now: float, fingerprint: str | None = _NO_OWNER) -> None:
    """Take back what an earlier backfill counted, so the history is replayed again.

    0.5.0 counted each copy Hermes' compaction keeps of a message, 0.6.0 did not count the owner's failures
    per day nor refused approvals per origin, and a replay made under other owner settings attributed the
    turns to someone else. Whole UTC days before the first observed day hold nothing but backfilled counts:
    they are cleared and replayed. The first observed day is shared with the hooks and keeps what it has.
    Failure totals lose what the cleared days added, except days older than the retention window, which the
    replay does not read again; a failure only those days had seen waits for the replay to find it again,
    and lesson baselines frozen from those days are counted again once the replay is done."""
    day = utc_day(since)
    boundary = _day_start(day)
    floor = day_offset(now, RETENTION_DAYS)   # the replay reads no older day, so none is taken back
    with experience(home) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = dict(conn.execute("SELECT key, value FROM cursor WHERE key IN ('backfill_revision', 'backfill_owner')"))
        if current.get("backfill_revision") == VERSION and \
                (fingerprint is None or (current.get("backfill_owner") or _NO_OWNER) == fingerprint):
            return   # another pass redid it first
        for signature, count in conn.execute("SELECT signature, SUM(count) FROM failure_daily WHERE day < ? AND day >= ? "
                                             "GROUP BY signature", (day, floor)).fetchall():
            # sessions restarts at one: the replay counts the history's sessions again
            conn.execute("UPDATE failure SET total=total-?, sessions=1, last_session='' WHERE signature=?", (count, signature))
        conn.execute("DELETE FROM failure WHERE total <= 0")
        for table in _REPLAYED_DAILY:
            conn.execute(f"DELETE FROM {table} WHERE day < ?", (day,))
        conn.execute("DELETE FROM failure_sample WHERE signature NOT IN (SELECT signature FROM failure)")
        conn.execute("DELETE FROM failure_owner")   # 0.6.0's totals: 0.7.0 counts the owner's failures per day
        conn.execute("""UPDATE lesson SET baseline_failures=NULL, baseline_calls=NULL
            WHERE activated IS NOT NULL AND activated < ? AND activated >= ?""",
                     (boundary + _BASELINE_DAYS * 86_400, now - (RETENTION_DAYS - _BASELINE_DAYS) * 86_400))
        conn.execute("DELETE FROM cursor WHERE key IN ('backfill_cursor', 'backfill_totals')")
        conn.executemany("INSERT OR REPLACE INTO cursor VALUES (?, ?)",
                         (("backfill_done", _RUNNING), ("backfill_until", repr(boundary)),
                          *_revision_keys(fingerprint, floor)))


class _Replay:
    """Replays sessions through the hooks' trackers. One instance per pass keeps correction windows intact."""

    def __init__(self, home: Path, *, sample: bool, owner_sender_ids, slow_ms: int, columns=frozenset(),
                 owner_platforms=(), session_columns=frozenset()):
        self.home = home
        self.sample = sample
        self.session_columns = session_columns
        self.parents: dict = {}   # session id -> whether it served the owner, for its delegated children
        self.turns = TurnTracker(home, slow_ms=slow_ms)
        self.sensor = FeedbackSensor(home, owner_sender_ids=owner_sender_ids, owner_platforms=owner_platforms)
        self.research = ResearchTracker(home)   # web research turns: timing, calls and errors
        self.counts = dict.fromkeys(_TOTALS, 0)
        self.read = 0   # messages read since the last pause
        self.uids = "message_uid" in columns
        self.select = ("SELECT id, role, content, tool_call_id, tool_calls, tool_name, timestamp, "
                       f"{'finish_reason' if 'finish_reason' in columns else 'NULL'} AS finish_reason, "
                       f"{'message_uid' if self.uids else 'NULL'} AS message_uid, "
                       f"{'_compressed_summary' if '_compressed_summary' in columns else '0'} AS summary "
                       "FROM messages WHERE session_id=? AND timestamp >= ?")

    def _served_owner(self, source: sqlite3.Connection, session, depth: int = 0) -> bool:
        """Whether a session served the owner, read from Hermes' history: a delegated child serves whoever its
        parent served (a child of a child, its grandparent's), and an unknown parent serves someone else."""
        if not isinstance(session, str) or not session or depth > 4:
            return False
        if session not in self.parents:
            user = "user_id" if "user_id" in self.session_columns else "NULL"
            parent = "parent_session_id" if "parent_session_id" in self.session_columns else "NULL"
            try:
                row = source.execute(f"SELECT source, {user} AS user_id, {parent} AS parent_session_id FROM sessions "
                                     "WHERE id=?", (session,)).fetchone()
            except sqlite3.Error:
                row = None
            served = False
            if row is not None:
                surface = surface_of(row["source"] or "gateway")
                served = owner_turn(surface, row["user_id"], self.sensor.owners,
                                    origin=label(str(row["source"] or "unknown").lower()[:48], fallback="unknown"),
                                    owner_platforms=self.sensor.platforms,
                                    parent_owner=surface == "subagent" and
                                    self._served_owner(source, row["parent_session_id"], depth + 1))
            self.parents[session] = served
        return self.parents[session]

    def _messages(self, source: sqlite3.Connection, sid, low: float, high: float | None = None, *, limit: int = 0,
                  after: tuple | None = None, at: tuple | None = None):
        if at is not None:   # (timestamp, id): the rows of that timestamp up to that id
            return source.execute(self.select + " AND timestamp = ? AND id <= ? ORDER BY timestamp, id",
                                  (sid, low, at[0], at[1]))
        sql = self.select + (" AND timestamp < ?" if high is not None else "")
        values = [sid, low] if high is None else [sid, low, high]
        if after is not None:   # (timestamp, id) of the last message an earlier part read
            sql += " AND (timestamp > ? OR (timestamp = ? AND id > ?))"
            values += [after[0], after[0], after[1]]
        return source.execute(sql + " ORDER BY timestamp, id" + (f" LIMIT {int(limit)}" if limit else ""), values)

    @staticmethod
    def _copy(message, seen: set, *, remember: bool = True) -> bool:
        """True for a copy of a message this session already replayed. Hermes' compaction keeps copies of a
        message under its `message_uid`, sometimes with a later timestamp or a shortened body."""
        uid = message["message_uid"]
        if uid:
            keys: tuple = (uid,)   # the bare id (a str), not a tuple: a long session holds one per message
        else:   # a history without message ids: compaction cloned the row byte for byte, same timestamp
            body = repr((message["role"], message["content"], message["tool_call_id"], message["tool_calls"],
                         message["timestamp"])).encode("utf-8", "backslashreplace")
            keys = (("row", hashlib.blake2b(body, digest_size=16).digest()),)
            if message["role"] == "tool" and message["tool_call_id"]:
                keys += (("tool", message["tool_call_id"], message["timestamp"]),)
        if any(key in seen for key in keys):
            return True
        if remember:
            seen.update(keys)
        return False

    def _following(self, source: sqlite3.Connection, sid, until: float, seen: set):
        """Role of the first new message at or after `until`: None when there is none at all, "" when the
        rows there are only compaction summaries and copies."""
        rows = self._messages(source, sid, until, limit=_LOOKAHEAD).fetchall()
        for message in rows:
            if not message["summary"] and not self._copy(message, seen, remember=False):
                return message["role"]
        return None if not rows else ""

    def session(self, source: sqlite3.Connection, row: dict, *, start: float, until: float, resume: tuple | None = None):
        """Replay one session. A generator: it pauses, yielding (timestamp, id) of the last message it read, once
        PAUSE_MESSAGES were read since the last pause, at a clean point (a turn over and the next not begun, before a
        message with a later timestamp, so no copy of a message read is left after it), or anywhere in a turn
        longer than _FORCE_PAUSE. `resume`: such a position, where an earlier pass stopped; the session goes on
        after it, without the windows of the trackers (a correction right there is not charged to a skill)."""
        # The session's own source decides, as in the hooks: kanban and cron are automations, "subagent" is a
        # delegated child, and a child of /new, /branch or a compression stays where its source puts it.
        sid, surface = row["id"], surface_of(row.get("source") or "gateway")
        origin = label(str(row.get("source") or "unknown").lower()[:48], fallback="unknown")
        owner = owner_turn(surface, row.get("user_id"), self.sensor.owners, origin=origin,
                           owner_platforms=self.sensor.platforms,
                           parent_owner=surface == "subagent" and self._served_owner(source, row.get("parent_session_id")))
        # its first message is in the window (a session resumed is past its first turn)
        first = float(row.get("started_at") or 0) >= start and resume is None
        open_turn, final, last, answered = None, False, None, False
        calls: dict = {}
        asked: dict = {}   # call id -> when the assistant asked for it
        started = None
        seen: set = set()
        if resume is None:
            self.counts["sessions"] += 1
        if self.uids and resume is not None:   # copies of messages an earlier part read, or older than the window
            seen.update(uid for (uid,) in source.execute(
                "SELECT message_uid FROM messages WHERE session_id=? AND (timestamp < ? OR (timestamp = ? AND id <= ?))"
                " AND message_uid IS NOT NULL", (sid, resume[0], resume[0], resume[1])))
        elif self.uids and float(row.get("started_at") or 0) < start:   # copies of messages older than the window
            seen.update(uid for (uid,) in source.execute(
                "SELECT message_uid FROM messages WHERE session_id=? AND timestamp < ? AND message_uid IS NOT NULL",
                (sid, start)))
        if resume is not None:
            # A copy without an id has its original's timestamp, so only the rows of the pause's own timestamp can
            # have a copy still ahead: read them again as the earlier part did (a pause between a row and its copy)
            for message in self._messages(source, sid, resume[0], at=resume):
                if not message["summary"]:
                    self._copy(message, seen)
        read = resume   # (timestamp, id) of the last message read
        for message in self._messages(source, sid, start, until, after=resume):
            self.read += 1
            previous, read = read, (float(message["timestamp"]), message["id"])
            # one turn longer than any pause: cut here, though never before the first turn or while a turn waits for
            # its first answer (a user message there joins that turn; after the cut it would open one of its own)
            if self.read >= _FORCE_PAUSE and previous is not None and not first and (open_turn is None or answered):
                self.read = 1
                yield previous
            if message["summary"] or self._copy(message, seen):
                continue   # a compaction summary, or a copy of a message already replayed
            role, stamp = message["role"], read[0]
            if role == "user":
                if open_turn and not answered:
                    continue   # sent before the agent answered: Hermes handles it in the same turn
                if open_turn:
                    self._finish(open_turn, final=final, ended=last, interrupted=True)
                    open_turn = None
                    if self.read > PAUSE_MESSAGES and previous is not None and stamp > previous[0]:
                        self.read = 1   # a clean point: the turn before is written, this one not begun
                        yield previous
                content = message["content"]
                payload = {"session_id": sid, "surface": surface, "user_message": content if isinstance(content, str) else "",
                           "sender_id": row.get("user_id"), "is_first_turn": first, "origin": origin}
                first = False
                if {"correction", "reask"} & set(self.sensor.observe(payload, clock=stamp, now=stamp)):
                    record_skill_correction(self.home, self.turns.recent_skills(sid, clock=stamp), now=stamp)
                open_turn, final, last, answered = f"backfill:{sid}:{message['id']}", False, stamp, False
                self.turns.start({"turn_id": open_turn, "session_id": sid, "surface": surface}, clock=stamp)
                self.research.start({"turn_id": open_turn}, clock=stamp)
                self.counts["turns"] += 1
            elif role == "assistant":
                requested = _calls(message["tool_calls"])
                calls.update(requested)
                asked.update(dict.fromkeys(requested, stamp))
                started = stamp   # the next result's time runs from the request, or from the previous result
                final, last, answered = message["finish_reason"] in _FINAL and not requested, stamp, True
            elif role == "tool":
                # Approximate: from the request (or the previous result of the same request) to this result.
                since = max(asked.pop(message["tool_call_id"], stamp), started if started is not None else stamp)
                self._tool(message, calls.pop(message["tool_call_id"], (None, {})), sid=sid, turn=open_turn, stamp=stamp,
                           owner=owner, origin=origin, duration_ms=int(min(max(stamp - since, 0.0), 3600.0) * 1000))
                started = stamp
                final, last, answered = False, stamp, True
        if open_turn:
            following = self._following(source, sid, until, seen)
            if following is None:   # nothing after it
                if final or row.get("ended_at") is not None or until - last >= _IDLE_SECONDS:
                    self._finish(open_turn, final=final, ended=last,
                                 interrupted=(row.get("end_reason") or "") in _INTERRUPTED_ENDS)
            elif following == "user" and answered or following == "" and final:
                self._finish(open_turn, final=final, ended=last, interrupted=True)   # over before observation began
            # anything else is a turn cut by the start of observation: the hooks saw its end

    def _tool(self, message, call, *, sid, turn, stamp, owner: bool = True, origin: str | None = None,
              duration_ms: int = 0) -> None:
        name, args = call
        name = message["tool_name"] or name
        status = _status(message["content"])
        payload = {"tool_name": name, "status": status, "result": message["content"], "args": args,
                   "session_id": sid, "turn_id": turn, "tool_call_id": message["tool_call_id"], "duration_ms": duration_ms}
        failure = record_tool(self.home, payload, now=stamp, sample=self.sample, owner=owner, origin=origin)
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


def _unmoved(conn: sqlite3.Connection, expected) -> None:
    moved = conn.execute("SELECT value FROM cursor WHERE key='backfill_cursor'").fetchone()
    if (moved[0] if moved else None) != expected:
        raise _CursorMoved


def step(home: Path, *, now: float, budget_s: float = 20.0, slice_sessions: int = SLICE_SESSIONS, sample: bool = False,
         owner_sender_ids=(), slow_ms: int = DEFAULT_SLOW_TURN_MS, cancelled=None, owner_platforms=(),
         owner_known: bool = True) -> dict:
    """Replay the next slices of Hermes' history within `budget_s`. Returns the status and running totals.

    `owner_known` is False where the owner settings could not be read (a standalone run without a YAML parser):
    such a run never redoes the replay for a change of owner, and what it replays is marked so that the next run
    that knows the owner replays it again."""
    home = Path(home)
    state = _state(home)
    fingerprint = owner_fingerprint(owner_sender_ids, owner_platforms) if owner_known else None
    replayed = state["position"] or state["done"] == "done"
    stale = state["version"] != VERSION
    other_owner = fingerprint is not None and state["owner"] != fingerprint
    if state["since"] is not None and replayed and other_owner and not stale and not _history_readable(home):
        # Nothing could be replayed again: keep what was counted, under the new owner settings from now on.
        record(home, lambda conn: conn.execute("INSERT OR REPLACE INTO cursor VALUES ('backfill_owner', ?)",
                                               (fingerprint,)))
        state = _state(home)
        other_owner = False
    if state["since"] is not None and replayed and (stale or other_owner):
        _redo(home, since=state["since"], now=now, fingerprint=fingerprint)
        state = _state(home)
    if state["done"]:
        return {"status": state["done"], **state["totals"]}
    if state["since"] is None:
        return {"status": "waiting", **state["totals"]}   # the hooks have not marked the start yet
    path = home / "state.db"
    floor = state["floor"] or day_offset(now, RETENTION_DAYS)
    if not path.is_file():
        return _settle(home, "no_history", state["totals"], fingerprint, floor)
    try:
        source = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return {"status": "unreadable", **state["totals"]}
    try:
        source.row_factory = sqlite3.Row
        try:
            present = {table: _columns(source, table) for table in _REQUIRED}
        except sqlite3.DatabaseError as error:
            if _damaged(error):
                return _settle(home, "damaged_history", state["totals"], fingerprint, floor)
            return {"status": "unreadable", **state["totals"]}
        if any(not need <= present[table] for table, need in _REQUIRED.items()):
            return _settle(home, "unsupported_history", state["totals"], fingerprint, floor)
        optional = ", ".join(f"{column}" if column in present["sessions"] else f"NULL AS {column}"
                             for column in _OPTIONAL["sessions"])
        until = state["until"]
        # Never older than what retention keeps: those days would be pruned at once, and a redo cannot take back
        # what they added once their daily counts are gone. The floor is fixed when the replay starts.
        start = max(until - BACKFILL_DAYS * 86_400, _day_start(floor))
        cursor_start, cursor_id = state["cursor"]
        totals = dict(state["totals"])
        replay = _Replay(home, sample=sample, owner_sender_ids=owner_sender_ids, slow_ms=slow_ms,
                         columns=present["messages"], owner_platforms=owner_platforms, session_columns=present["sessions"])
        deadline = time.monotonic() + budget_s
        expected = state["position"]
        inside = state["inside"]   # ((start, id), (timestamp, id)): a session an earlier pass replayed in part
        running = None             # [row, steps]: a session paused in this pass, replayed on in the next part
        try:
            while time.monotonic() < deadline and not (cancelled and cancelled()):
                try:
                    rows = source.execute(f"""SELECT id, source, started_at, {optional} FROM sessions
                        WHERE started_at < ? AND COALESCE(ended_at, ?) >= ?
                          AND (? IS NULL OR started_at > ? OR (started_at = ? AND id {'>=' if inside else '>'} ?))
                        ORDER BY started_at, id LIMIT ?""", (until, now, start, cursor_start, cursor_start, cursor_start,
                                                               cursor_id, slice_sessions)).fetchall()
                except sqlite3.DatabaseError as error:
                    if _damaged(error):
                        return _settle(home, "damaged_history", totals, fingerprint, floor)
                    return {"status": "unreadable", **totals}
                if not rows:
                    return _settle(home, "done", totals, fingerprint, floor)
                queue = [dict(row) for row in rows]
                while queue or running is not None:
                    before = dict(replay.counts)
                    try:
                        # Written synchronously in one transaction that first checks the cursor is still where this
                        # part started: never through the hooks' queue, never half. A part ends with the slice, or
                        # at a pause of the replay (every PAUSE_MESSAGES messages, inside a session too).
                        with batched(home, check=lambda conn, expected=expected: _unmoved(conn, expected)) as jobs:
                            position, running, inside = _part(replay, source, queue, running, inside, jobs,
                                                              start=start, until=until)
                            counted = {key: totals[key] + replay.counts[key] - before[key] for key in totals}
                            summary = json.dumps(counted)
                            record(home, lambda conn, p=position, s=summary: conn.executemany(
                                "INSERT OR REPLACE INTO cursor VALUES (?, ?)",
                                (("backfill_cursor", p), ("backfill_totals", s), ("backfill_done", _RUNNING),
                                 *_revision_keys(fingerprint, floor))))
                    except _CursorMoved:
                        return {"status": "running", **state["totals"]}
                    except _Unreadable:
                        return {"status": "unreadable", **totals}
                    except sqlite3.Error:   # the Evolution database stayed busy: this part is read again next pass
                        return {"status": "running", **totals}
                    totals, expected = counted, position
                    if (queue or running is not None) and (time.monotonic() >= deadline or (cancelled and cancelled())):
                        return {"status": "running", **totals}   # the next pass goes on from the cursor just written
                cursor_start, cursor_id = float(rows[-1]["started_at"]), str(rows[-1]["id"])
        finally:
            if running is not None:
                running[1].close()
        return {"status": "running", **totals}
    finally:
        source.close()


def _cursor(row: dict, inside: tuple | None = None) -> str:
    """The cursor after a session, or inside it after the message `inside` ((timestamp, id))."""
    position = f"{float(row['started_at'])!r}|{row['id']}"
    return position if inside is None else f"{position}|@{float(inside[0])!r}|{json.dumps(inside[1])}"


def _part(replay: _Replay, source: sqlite3.Connection, queue: list, running, inside, jobs: list, *, start: float,
          until: float):
    """Replay sessions from `queue` (the paused one, `running`, first) into `jobs` up to the next pause: the end of
    the slice, PAUSE_MESSAGES messages read, or a pause inside a session. (cursor, running, inside) for the caller."""
    position = None
    while queue or running is not None:
        if running is None:
            row = queue.pop(0)
            key = (float(row["started_at"]), str(row["id"]))
            resume = inside[1] if inside is not None and inside[0] == key else None
            inside = None   # only the first session of the pass can be the one left in part
            running = [row, replay.session(source, row, start=start, until=until, resume=resume)]
        row, steps = running
        mark, counts = len(jobs), dict(replay.counts)
        try:
            paused = next(steps, None)
        except sqlite3.Error as error:
            if _busy(error):
                raise _Unreadable from error   # Hermes is writing: this part is read again next pass
            # a damaged page in one session's messages: the rest of that session is skipped, never the backfill
            del jobs[mark:]
            replay.counts = {**counts, "skipped": counts["skipped"] + 1}
            paused = None
        except Exception:   # one session that cannot be replayed is skipped, not the backfill
            del jobs[mark:]
            replay.counts = {**counts, "skipped": counts["skipped"] + 1}
            paused = None
        if paused is not None:
            return _cursor(row, paused), running, inside
        running, position = None, _cursor(row)
        if replay.read >= PAUSE_MESSAGES and queue:
            replay.read = 0
            return position, None, inside
    return position, None, inside


def _history_readable(home: Path) -> bool:
    """Whether Hermes' history can be replayed now: state.db opens and has the tables the replay reads."""
    path = Path(home) / "state.db"
    if not path.is_file():
        return False
    try:
        source = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=2)
        try:
            return all(need <= _columns(source, table) for table, need in _REQUIRED.items())
        finally:
            source.close()
    except sqlite3.Error:
        return False


def _settle(home: Path, status: str, totals: dict, fingerprint: str | None = _NO_OWNER, floor: str = "") -> dict:
    """Finish the backfill for good. A history that is missing or of another shape is not retried."""
    record(home, lambda conn: conn.executemany("INSERT OR REPLACE INTO cursor VALUES (?, ?)",
                                               (("backfill_done", status), *_revision_keys(fingerprint, floor))))
    return {"status": status, **totals}
