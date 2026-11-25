"""What the plugin itself costs the agent, measured where it runs (0.9.2).

Every hook the plugin registers runs inside Hermes' turn: before and after each tool call, on each model call, on
each prompt. Its cost is measured on the spot, in memory (two clock reads and a few list operations per call, about
a microsecond), and written every 10 minutes by the idle clock as one row per process (`self_cost:<pid>` in the
cursor table). `status`, the panel and the digest show it, and `status` says so when the plugin ever adds more than
SLOW_MS to a call in its slow tail: the plugin is meant to cost nothing the owner can feel.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque

SAMPLES = 512           # the most recent calls per hook kept for the tail (p95)
SLOW_MS = 50.0          # a p95 above this, over MIN_CALLS calls, is said in `status`
MIN_CALLS = 200
STALE_SECONDS = 86_400  # a process's row older than this is not shown


class Meter:
    """Calls, total and worst time per hook, and the latest SAMPLES durations for the tail. Thread-safe."""

    def __init__(self):
        self._lock = threading.Lock()
        self._stats: dict[str, list] = {}
        self.since = time.time()

    def add(self, hook: str, seconds: float, cpu: float | None = None) -> None:
        try:
            milliseconds = seconds * 1000.0
            with self._lock:
                entry = self._stats.get(hook)
                if entry is None:
                    entry = self._stats[hook] = [0, 0.0, 0.0, deque(maxlen=SAMPLES), 0.0]
                entry[0] += 1
                entry[1] += milliseconds
                if milliseconds > entry[2]:
                    entry[2] = milliseconds
                entry[3].append(milliseconds)
                if cpu is not None:
                    entry[4] += cpu * 1000.0
        except Exception:   # measuring must never cost the call anything else
            pass

    def wrap(self, hook: str, function):
        """`function` measured under the name `hook`: same arguments, same result, same exceptions. Wall time and the
        thread's own CPU time (0.14.0): in a gateway serving many profiles the wall time includes waiting for the
        interpreter and the disk, which the CPU time does not, so the owner can tell the plugin's work from the wait."""
        clock, add = time.perf_counter, self.add
        cpu = getattr(time, "thread_time", None)

        def measured(*args, **kwargs):
            start = clock()
            started = cpu() if cpu is not None else None
            try:
                return function(*args, **kwargs)
            finally:
                add(hook, clock() - start, cpu() - started if started is not None else None)

        measured.__name__ = getattr(function, "__name__", hook)
        measured.__doc__ = getattr(function, "__doc__", None)
        return measured

    def snapshot(self) -> dict:
        """{hook: {calls, mean_ms, p95_ms, max_ms}} since the plugin loaded."""
        with self._lock:
            items = [(hook, entry[0], entry[1], entry[2], sorted(entry[3]), entry[4] if len(entry) > 4 else 0.0)
                     for hook, entry in self._stats.items()]
        found = {}
        for hook, calls, total, worst, samples, cpu in items:
            if not calls:
                continue
            tail = samples[min(len(samples) - 1, int(len(samples) * 0.95))] if samples else 0.0
            middle = samples[len(samples) // 2] if samples else 0.0
            found[hook] = {"calls": calls, "mean_ms": round(total / calls, 3), "p50_ms": round(middle, 3),
                           "p95_ms": round(tail, 3), "max_ms": round(worst, 3), "cpu_mean_ms": round(cpu / calls, 3)}
        return found


def persist(home, meter: Meter, *, kind: str, now: float | None = None) -> None:
    """Write this process's measurements (one row, replaced each time) through the shared writer."""
    from .experience import record

    found = meter.snapshot()
    if not found:
        return
    now = time.time() if now is None else now
    value = json.dumps({"at": now, "since": meter.since, "kind": kind, "hooks": found}, sort_keys=True)
    key = f"self_cost:{os.getpid()}"
    record(home, lambda conn: conn.execute("INSERT OR REPLACE INTO cursor VALUES (?, ?)", (key, value)), now=now)


def summary(conn, *, now: float) -> dict | None:
    """The plugin's cost across the processes that measured it in the last day: per hook, calls added up, the worst
    p95 and max of any process. Rows older than STALE_SECONDS are deleted. None when nothing was measured."""
    rows = conn.execute("SELECT key, value FROM cursor WHERE key LIKE 'self_cost:%'").fetchall()
    merged: dict[str, dict] = {}
    stale, kinds = [], set()
    for key, value in rows:
        try:
            found = json.loads(value)
            at = float(found.get("at") or 0)
        except (TypeError, ValueError, AttributeError):
            stale.append(key)
            continue
        if not 0 <= now - at <= STALE_SECONDS:
            stale.append(key)
            continue
        kinds.add(str(found.get("kind") or "?"))
        for hook, item in (found.get("hooks") or {}).items():
            if not isinstance(item, dict):
                continue
            entry = merged.setdefault(hook, {"calls": 0, "total_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0,
                                             "cpu_ms": 0.0, "cpu_calls": 0})
            calls = int(item.get("calls") or 0)
            entry["calls"] += calls
            entry["total_ms"] += float(item.get("mean_ms") or 0) * calls
            if item.get("cpu_mean_ms") is not None:   # measured since 0.14.0
                entry["cpu_ms"] += float(item.get("cpu_mean_ms") or 0) * calls
                entry["cpu_calls"] += calls
            entry["p50_ms"] = max(entry["p50_ms"], float(item.get("p50_ms") or item.get("mean_ms") or 0))
            entry["p95_ms"] = max(entry["p95_ms"], float(item.get("p95_ms") or 0))
            entry["max_ms"] = max(entry["max_ms"], float(item.get("max_ms") or 0))
    if stale:
        conn.executemany("DELETE FROM cursor WHERE key=?", [(key,) for key in stale])
    if not merged:
        return None
    hooks = {hook: {"calls": entry["calls"],
                    "mean_ms": round(entry["total_ms"] / entry["calls"], 3) if entry["calls"] else 0.0,
                    "p50_ms": round(entry["p50_ms"], 3), "p95_ms": round(entry["p95_ms"], 3),
                    "max_ms": round(entry["max_ms"], 3),
                    **({"cpu_mean_ms": round(entry["cpu_ms"] / entry["cpu_calls"], 3)} if entry["cpu_calls"] else {})}
             for hook, entry in merged.items()}
    slow = sorted((hook for hook, entry in hooks.items() if entry["calls"] >= MIN_CALLS and entry["p95_ms"] > SLOW_MS),
                  key=lambda hook: -hooks[hook]["p95_ms"])
    return {"hooks": hooks, "processes": len(rows) - len(stale), "kinds": sorted(kinds), "slow": slow}
