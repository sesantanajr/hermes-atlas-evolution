"""Research turn timing from Hermes hooks, without inspecting conversation text."""

from __future__ import annotations

import hashlib
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from .operations import _day, database

RESEARCH_TOOLS = frozenset({"web_search", "web_extract", "web_fetch", "web_deep_research", "deep_research"})
RESEARCH_LIMIT_MS = 120_000
LATENCY_BUCKETS = (15_000, 30_000, 60_000, 120_000, 240_000, 480_000, 900_000)


@dataclass
class _Turn:
    started: float
    calls: int = 0
    errors: int = 0
    completed_calls: set[bytes] = field(default_factory=set)


def _key(identifier) -> bytes | None:
    if isinstance(identifier, str) and 0 < len(identifier) <= 400:
        return hashlib.sha256(identifier.encode()).digest()
    return None


class ResearchTracker:
    def __init__(self, home: Path):
        self.home = home
        self._turns: OrderedDict[bytes, _Turn] = OrderedDict()
        self._lock = threading.Lock()

    def start(self, payload: dict, *, clock: float | None = None) -> None:
        key = _key(payload.get("turn_id"))
        if not key:
            return
        clock = time.monotonic() if clock is None else clock
        with self._lock:
            if key in self._turns:
                return
            self._turns[key] = _Turn(clock)
            while len(self._turns) > 512 or clock - next(iter(self._turns.values())).started > 4 * 3600:
                self._turns.popitem(last=False)

    def tool(self, payload: dict) -> None:
        tool = payload.get("tool_name")
        if not isinstance(tool, str) or tool not in RESEARCH_TOOLS:
            return
        if payload.get("status") not in ("ok", "error"):
            return
        key = _key(payload.get("turn_id"))
        call = _key(payload.get("tool_call_id"))
        with self._lock:
            turn = self._turns.get(key)
            if turn is None or turn.calls >= 256 or call and call in turn.completed_calls:
                return
            if call:
                turn.completed_calls.add(call)
            turn.calls += 1
            turn.errors += int(payload.get("status") == "error")

    def finish(self, payload: dict, *, clock: float | None = None, now: float | None = None) -> None:
        key = _key(payload.get("turn_id"))
        if not key:
            return
        clock = time.monotonic() if clock is None else clock
        with self._lock:
            turn = self._turns.pop(key, None)
        if not turn or not turn.calls or clock < turn.started:
            return
        duration = min(14_400_000, int((clock - turn.started) * 1000))
        bucket = next((limit for limit in LATENCY_BUCKETS if duration <= limit), 0)
        with database(self.home, observer=True) as conn:
            conn.execute("""INSERT INTO research_latency VALUES (?, ?, 1, ?, ?, ?, ?)
              ON CONFLICT DO UPDATE SET turns=turns+1, slow_turns=slow_turns+excluded.slow_turns,
                tool_errors=tool_errors+excluded.tool_errors, tool_calls=tool_calls+excluded.tool_calls,
                max_ms=MAX(max_ms,excluded.max_ms)""",
                         (_day(time.time() if now is None else now), bucket, int(duration >= RESEARCH_LIMIT_MS),
                          turn.errors, turn.calls, duration))


def research_summary(conn, *, now: float, days: int) -> dict:
    rows = conn.execute("""SELECT bucket,SUM(turns),SUM(slow_turns),SUM(tool_errors),SUM(tool_calls),MAX(max_ms)
          FROM research_latency WHERE day BETWEEN ? AND ? GROUP BY bucket ORDER BY bucket=0,bucket""",
                        (_day(now - (days - 1) * 86400), _day(now))).fetchall()
    turns = sum(row[1] for row in rows)
    maximum = max((row[5] for row in rows), default=None)
    preceding = 0
    p95 = None
    for bucket, count, *_ in rows:
        preceding += count
        if preceding >= math.ceil(turns * 0.95):
            p95 = min(bucket or maximum, maximum)
            break
    return {"turns": turns, "slow_turns": sum(row[2] for row in rows), "tool_errors": sum(row[3] for row in rows),
            "tool_calls": sum(row[4] for row in rows), "p95_ms": p95, "limit_ms": RESEARCH_LIMIT_MS,
            "max_ms": maximum, "basis": "completed_turns_with_known_web_research_tools"}
