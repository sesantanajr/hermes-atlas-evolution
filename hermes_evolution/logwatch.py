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
    rb"^(?P<day>\d{4}-\d{2}-\d{2}) [\d:,]{8,12} \w+ .{0,160}?Background review complete: .{0,80}?"
    rb"calls=(?P<calls>\d{1,7}) in=(?P<input>\d{1,10}) out=(?P<output>\d{1,10}) cache_read=(?P<cache>\d{1,11}) "
    rb"result=(?P<result>none|skill\+memory|skill|memory|error)\b")
RESULTS = ("none", "skill", "memory", "skill+memory", "error")


def ingest_reviews(home: Path, conn) -> dict:
    """Count background reviews logged since the last run. Returns what this run added."""
    path = Path(home) / "logs" / "agent.log"
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return {"status": "log_unavailable", "reviews": 0}
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return {"status": "log_unavailable", "reviews": 0}
        saved = conn.execute("SELECT value FROM cursor WHERE key='agent_log'").fetchone()
        try:
            state = json.loads(saved[0]) if saved else {}
        except ValueError:
            state = {}
        offset = state.get("offset") if state.get("inode") == info.st_ino else None
        if type(offset) is not int or not 0 <= offset <= info.st_size:
            # First run, rotation or truncation: start near the end so old history is not attributed to today.
            offset = max(0, info.st_size - _READ_LIMIT)
            skip_partial = offset > 0
        else:
            skip_partial = False
        with os.fdopen(fd, "rb", closefd=False) as stream:
            stream.seek(offset)
            data = stream.read(_READ_LIMIT)
    except OSError:
        return {"status": "log_unavailable", "reviews": 0}
    finally:
        os.close(fd)
    complete = data.rfind(b"\n") + 1   # never consume a line that is still being written
    lines = data[:complete].split(b"\n")
    if skip_partial and lines:
        lines = lines[1:]
    added = 0
    for line in lines:
        match = _REVIEW.match(line)
        if not match:
            continue
        conn.execute("""INSERT INTO review_daily VALUES (?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT DO UPDATE SET reviews=reviews+1, calls=calls+excluded.calls,
              input_tokens=input_tokens+excluded.input_tokens, output_tokens=output_tokens+excluded.output_tokens,
              cache_read_tokens=cache_read_tokens+excluded.cache_read_tokens""",
                     (match["day"].decode(), match["result"].decode(), int(match["calls"]), int(match["input"]),
                      int(match["output"]), int(match["cache"])))
        added += 1
    conn.execute("INSERT OR REPLACE INTO cursor VALUES ('agent_log', ?)",
                 (json.dumps({"inode": info.st_ino, "offset": offset + complete}),))
    return {"status": "ok", "reviews": added, "bytes_read": complete, "more": offset + complete < info.st_size}


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
