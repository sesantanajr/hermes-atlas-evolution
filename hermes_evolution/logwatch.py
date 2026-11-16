"""Incremental, bounded reading of Hermes' agent.log for signals no hook exposes.

Hermes 0.23.1 has no hook for a finished background review, so the yield of the self-improvement loop
(how many reviews ran, what they cost and whether they produced anything) is taken from one structured
log line. A cursor keeps each run from counting a line twice. Only numbers and a fixed result label are kept.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

_READ_LIMIT = 4 * 1024 * 1024
_REVIEW = re.compile(
    rb"^(?P<day>\d{4}-\d{2}-\d{2}) [\d:,]{8,12} INFO(?: \[[^\]\n]{1,200}\])? agent\.background_review: "
    rb"Background review complete: thread=[\w.-]{1,40} "
    rb"calls=(?P<calls>\d{1,7}) in=(?P<input>\d{1,10}) out=(?P<output>\d{1,10}) cache_read=(?P<cache>\d{1,11}) "
    rb"result=(?P<result>none|skill\+memory|skill|memory|error)\b")
RESULTS = ("none", "skill", "memory", "skill+memory", "error")


_ROTATED = 5   # agent.log.1 … agent.log.5


def _files(home: Path) -> list[tuple[Path, os.stat_result]]:
    """Regular log files, current first, then agent.log.1 … agent.log.5. Links are ignored."""
    folder = Path(home) / "logs"
    found = []
    for name in ("agent.log", *(f"agent.log.{index}" for index in range(1, _ROTATED + 1))):
        try:
            info = os.lstat(folder / name)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            found.append((folder / name, info))
    return found


def _read(path: Path, inode: int, offset: int) -> tuple[bytes, int] | None:
    """Up to _READ_LIMIT bytes from `offset` of the file that still has `inode`, and its size."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_ino != inode or not 0 <= offset <= info.st_size:
            return None
        with os.fdopen(fd, "rb", closefd=False) as stream:
            stream.seek(offset)
            return stream.read(_READ_LIMIT), info.st_size
    except OSError:
        return None
    finally:
        os.close(fd)


def ingest_reviews(home: Path, conn, *, since_day: str | None = None) -> dict:
    """Count background reviews logged since the last run. Returns what this run added.

    The first run also reads the rotated files, oldest first, counting lines from `since_day` on (each line
    carries its own date). After a rotation, the rest of the old file is read before the new one, so lines
    written just before Hermes renamed the log are not lost."""
    files = _files(home)
    current = next(((path, info) for path, info in files if path.name == "agent.log"), None)
    if current is None:
        return {"status": "log_unavailable", "reviews": 0}
    saved = conn.execute("SELECT value FROM cursor WHERE key='agent_log'").fetchone()
    try:
        state = json.loads(saved[0]) if saved else None
    except ValueError:
        state = None
    by_inode = {info.st_ino: path for path, info in files}
    if not isinstance(state, dict):   # first run: the rotated history inside the window, then the current file
        queue = [[info.st_ino, 0] for path, info in reversed(files) if path.name != "agent.log"]
        state = {"queue": queue, "inode": current[1].st_ino, "offset": 0, "since": since_day}
    elif state.get("inode") != current[1].st_ino:   # rotated since the last run: finish the old file first
        queue = list(state.get("queue") or [])
        if isinstance(state.get("inode"), int) and type(state.get("offset")) is int:
            queue.append([state["inode"], state["offset"]])
        state = {"queue": queue, "inode": current[1].st_ino, "offset": 0, "since": None}
    queue = [item for item in state.get("queue") or []
             if isinstance(item, list) and len(item) == 2 and item[0] in by_inode]
    if queue:   # a rotated file is complete: its last line is consumed even without a newline
        inode, offset = queue[0]
        chunk = _read(by_inode[inode], inode, offset) if type(offset) is int else None
        if chunk is None:
            queue.pop(0)
            data, complete, done = b"", 0, True
        else:
            data, size = chunk
            complete = len(data) if offset + len(data) >= size else data.rfind(b"\n") + 1
            done = offset + complete >= size
            if done:
                queue.pop(0)
            else:
                queue[0] = [inode, offset + complete]
        more = True
    else:
        offset = state.get("offset")
        chunk = _read(current[0], current[1].st_ino, offset if type(offset) is int else 0)
        if chunk is None:   # truncated or replaced in place: start over
            chunk = _read(current[0], current[1].st_ino, 0) or (b"", 0)
            offset = 0
        data, size = chunk
        complete = data.rfind(b"\n") + 1   # never consume a line that is still being written
        state["offset"] = offset + complete
        more = state["offset"] < size
    added = _count(conn, data[:complete], since=state.get("since"))
    state["queue"] = queue
    if not queue and not more:
        state["since"] = None
    conn.execute("INSERT OR REPLACE INTO cursor VALUES ('agent_log', ?)", (json.dumps(state),))
    return {"status": "ok", "reviews": added, "bytes_read": complete, "more": bool(queue) or more}


def _count(conn, data: bytes, *, since: str | None) -> int:
    added = 0
    for line in data.split(b"\n"):
        match = _REVIEW.match(line)
        if not match or (since and match["day"].decode() < since):
            continue
        conn.execute("""INSERT INTO review_daily VALUES (?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT DO UPDATE SET reviews=reviews+1, calls=calls+excluded.calls,
              input_tokens=input_tokens+excluded.input_tokens, output_tokens=output_tokens+excluded.output_tokens,
              cache_read_tokens=cache_read_tokens+excluded.cache_read_tokens""",
                     (match["day"].decode(), match["result"].decode(), int(match["calls"]), int(match["input"]),
                      int(match["output"]), int(match["cache"])))
        added += 1
    return added


def local_days(now: float, days: int) -> tuple[str, str]:
    """First and last local calendar day of a window: agent.log stamps lines in the process's local time."""
    import time as _time

    def day(moment):
        return _time.strftime("%Y-%m-%d", _time.localtime(moment))

    return day(now - (days - 1) * 86_400), day(now)


def review_summary(conn, *, start_day: str, end_day: str) -> dict:
    rows = conn.execute("""SELECT result, SUM(reviews) AS reviews, SUM(calls) AS calls, SUM(input_tokens) AS input_tokens,
          SUM(output_tokens) AS output_tokens, SUM(cache_read_tokens) AS cache_read_tokens
        FROM review_daily WHERE day BETWEEN ? AND ? GROUP BY result""", (start_day, end_day)).fetchall()
    by_result = {row["result"]: row["reviews"] for row in rows}
    reviews = sum(by_result.values())
    productive = sum(count for result, count in by_result.items() if result in ("skill", "memory", "skill+memory"))
    return {"reviews": reviews, "by_result": by_result, "productive": productive,
            "empty_pct": round(100 * by_result.get("none", 0) / reviews, 1) if reviews else None,
            "model_calls": sum(row["calls"] for row in rows),
            "input_tokens": sum(row["input_tokens"] for row in rows),
            "output_tokens": sum(row["output_tokens"] for row in rows),
            "cache_read_tokens": sum(row["cache_read_tokens"] for row in rows),
            "basis": "background_review_lines_in_agent_log_local_day"}
