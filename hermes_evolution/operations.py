"""Profile-local observer metrics and a feedback-driven maintenance loop."""

from __future__ import annotations

import atexit
import hashlib
import math
import os
import re
import sqlite3
import threading
import time
from collections import Counter, OrderedDict, deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

BUCKETS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")
RETENTION_DAYS = 35
RECOVERY_SPACING_SECONDS = 300
OBSERVER_BUSY_SECONDS = 0.035
_OBSERVER_LOCK_SECONDS = 0.05
_OBSERVER_QUEUE = 512
_OBSERVER_LIMIT = 8
SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, day TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS seen_day ON seen(day);
CREATE TABLE IF NOT EXISTS metric (
    day TEXT NOT NULL, lane TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
    outcome TEXT NOT NULL, requests INTEGER NOT NULL, usage_samples INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, cache_read_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL, reasoning_tokens INTEGER NOT NULL, duration_ms INTEGER NOT NULL,
    PRIMARY KEY (day, lane, provider, model, outcome)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS maintenance (
    code TEXT PRIMARY KEY, active INTEGER NOT NULL, first_seen REAL NOT NULL,
    last_seen REAL NOT NULL, samples INTEGER NOT NULL, feedback TEXT, last_day TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS control (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_latency (
    day TEXT NOT NULL, bucket INTEGER NOT NULL, turns INTEGER NOT NULL,
    slow_turns INTEGER NOT NULL, tool_errors INTEGER NOT NULL, tool_calls INTEGER NOT NULL,
    max_ms INTEGER NOT NULL, PRIMARY KEY (day,bucket)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS native_activity (
    day TEXT NOT NULL, hook TEXT NOT NULL, outcome TEXT NOT NULL, events INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL, tokens_before INTEGER NOT NULL, tokens_after INTEGER NOT NULL,
    measured INTEGER NOT NULL, PRIMARY KEY (day,hook,outcome)
) WITHOUT ROWID;
"""


def _database_path(home: Path) -> Path:
    folder = home / "evolution"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder / "evolution.sqlite3"
    # Create the file with private POSIX permissions before SQLite creates sidecars.
    if not path.exists():
        try:
            path.touch(mode=0o600, exist_ok=False)
        except FileExistsError:
            pass
    return path


def _open(path: Path, timeout: float, **options) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=timeout, **options)
    try:
        connection.row_factory = sqlite3.Row
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version in (0, 1, 2):
            if version == 0:
                connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("BEGIN IMMEDIATE;" + SCHEMA + "PRAGMA user_version=3; COMMIT;")
        elif version != 3:
            raise ValueError(f"Unsupported Evolution database schema {version}")
        connection.execute("PRAGMA foreign_keys=ON")
    except BaseException:
        connection.close()
        raise
    return connection


@contextmanager
def database(home: Path, *, observer: bool = False):
    """Open the profile database. Hook callers share one connection per process."""
    if observer:
        with _observer(home).session() as connection:
            yield connection
        return
    connection = _open(_database_path(home), 2)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _busy(error: sqlite3.Error) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    if type(code) is int:
        return code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    return "locked" in str(error)


def _rollback(connection: sqlite3.Connection) -> None:
    try:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass


class _Observer:
    """One connection per process and profile for hook callbacks, with a bounded retry queue.

    Hermes runs each hook callback on its own thread. A connection per event made the threads of a
    single process compete for the write lock, and an event that outlived the busy wait was lost.
    """

    def __init__(self, home: Path):
        self.home = Path(home)
        self.lock = threading.RLock()  # Serializes every use of the shared connection.
        self.guard = threading.Lock()  # Protects the queue for threads that could not wait.
        self.pending: deque = deque()
        self.dropped = 0
        self.connection: sqlite3.Connection | None = None
        self.identity = None

    def _file(self):
        try:
            info = os.stat(self.home / "evolution" / "evolution.sqlite3")
        except OSError:
            return None
        return os.getpid(), info.st_dev, info.st_ino

    def _connect(self) -> sqlite3.Connection:
        # A removed or replaced file, or a forked process, needs a handle of its own.
        current = self._file()
        if self.connection is not None and current is not None and current == self.identity:
            return self.connection
        self._release()
        connection = _open(_database_path(self.home), OBSERVER_BUSY_SECONDS, check_same_thread=False)
        try:
            if connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
                # Counters can lose the last moments before a power failure. WAL keeps the file consistent.
                connection.execute("PRAGMA synchronous=NORMAL")
        except BaseException:
            connection.close()
            raise
        self.connection, self.identity = connection, self._file()
        return connection

    def _release(self) -> None:
        connection, identity = self.connection, self.identity
        self.connection = self.identity = None
        if connection is None:
            return
        if identity is not None and identity[0] != os.getpid():
            _INHERITED.append(connection)  # The handle belongs to the parent process.
            return
        try:
            connection.close()
        except sqlite3.Error:
            pass

    @contextmanager
    def session(self):
        """Lend the shared connection. A caller that cannot get it in time sees a busy database."""
        if not self.lock.acquire(timeout=_OBSERVER_LOCK_SECONDS):
            raise sqlite3.OperationalError("database is locked")
        try:
            connection = self._connect()
            try:
                yield connection
                if connection.in_transaction:
                    connection.execute("COMMIT")
            except BaseException as error:
                _rollback(connection)
                if isinstance(error, sqlite3.Error) and not _busy(error):
                    self._release()
                raise
            try:
                self._write(None, time.time())  # Events that arrived during the caller's transaction.
            except (OSError, ValueError, sqlite3.Error):
                pass
        finally:
            self.lock.release()

    def record(self, job, now: float) -> None:
        """Apply one counter update, or keep it for the next write while the database is busy."""
        if not self.lock.acquire(timeout=_OBSERVER_LOCK_SECONDS):
            self._defer(job)
            return
        try:
            self._write(job, now)
        finally:
            self.lock.release()

    def _defer(self, job) -> None:
        with self.guard:
            if len(self.pending) < _OBSERVER_QUEUE:
                self.pending.append(job)
            else:
                self.dropped += 1

    def _write(self, job, now: float) -> None:
        with self.guard:
            waiting, dropped = list(self.pending), self.dropped
        jobs = waiting + ([job] if job is not None else [])
        if not jobs and not dropped:
            return
        day = _day(now)
        try:
            connection = self._connect()
            if connection.in_transaction:  # A caller of session() on this thread is mid-transaction.
                raise sqlite3.OperationalError("database is locked")
            connection.execute("BEGIN IMMEDIATE")
            try:
                for item in jobs:
                    item(connection)
                if dropped:
                    connection.execute("""INSERT INTO metric VALUES (?, 'observer', '', '', 'dropped', ?, 0, 0, 0, 0, 0, 0, 0)
                        ON CONFLICT DO UPDATE SET requests=requests+excluded.requests""", (day, dropped))
                connection.execute("COMMIT")
            except BaseException:
                _rollback(connection)
                raise
        except (OSError, ValueError):
            raise  # The folder or schema is unusable for now. The bounded queue waits for it.
        except Exception as error:
            if isinstance(error, sqlite3.Error) and _busy(error):
                if job is not None:
                    self._defer(job)
                return
            # An update that cannot be applied must not fail every later write of the queue.
            self._release()
            self._settle(waiting, lost=len(waiting))
            raise
        self._settle(waiting, lost=-dropped)

    def _settle(self, written: list, *, lost: int) -> None:
        with self.guard:
            for _ in written:
                self.pending.popleft()
            self.dropped += lost

    def close(self, wait: float = 0.5) -> None:
        """Write what is waiting, with a longer wait than a hook can afford, and release the handle."""
        if not self.lock.acquire(timeout=wait):
            return
        try:
            try:
                if self.pending or self.dropped:
                    self._connect().execute(f"PRAGMA busy_timeout={int(wait * 1000)}")
                    self._write(None, time.time())
            except (OSError, ValueError, sqlite3.Error):
                pass
            self._release()
        finally:
            self.lock.release()


_OBSERVERS: OrderedDict[str, _Observer] = OrderedDict()
_OBSERVERS_GUARD = threading.Lock()
_INHERITED: list[sqlite3.Connection] = []


def _observer(home: Path) -> _Observer:
    key = os.fspath(home)
    with _OBSERVERS_GUARD:
        observer = _OBSERVERS.get(key)
        if observer is None:
            observer = _OBSERVERS[key] = _Observer(home)
        _OBSERVERS.move_to_end(key)
        retired = [_OBSERVERS.popitem(last=False)[1] for _ in range(len(_OBSERVERS) - _OBSERVER_LIMIT)]
    for other in retired:
        other.close()
    return observer


def record(home: Path, job, *, now: float | None = None) -> None:
    """Run ``job(connection)`` in one write transaction of the shared observer connection.

    The job may run later, on another thread. It must hold only values that are safe to store.
    """
    _observer(home).record(job, time.time() if now is None else now)


def close_observers() -> None:
    """Write waiting events and close this process's shared observer connections."""
    with _OBSERVERS_GUARD:
        observers = list(_OBSERVERS.values())
        _OBSERVERS.clear()
    for observer in observers:
        observer.close()


def _forget_inherited_observers() -> None:
    # A forked child must neither use the parent's SQLite handles nor wait on a lock held at fork time.
    global _OBSERVERS_GUARD
    _INHERITED.extend(item.connection for item in _OBSERVERS.values() if item.connection is not None)
    _OBSERVERS.clear()
    _OBSERVERS_GUARD = threading.Lock()


atexit.register(close_observers)
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_forget_inherited_observers)


def _day(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).date().isoformat()


def _label(value) -> str:
    if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_.:/+-]{1,90}", value):
        lower = value.lower()
        segments = re.split(r"[/:]", lower)
        credential_prefixes = ("sk-", "ghp_", "gho_", "ghs_", "ghr_", "ghu_", "github_pat_", "xox", "akia", "aiza", "hf_")
        # A local server can report the model as its file path. Keep namespaced names, never a path.
        path_like = any(part in ("", ".", "..") for part in lower.split("/"))
        if not path_like and "://" not in lower and not any(segment.startswith(credential_prefixes) for segment in segments):
            return lower
    return "unknown"


def _tokens(value) -> int:
    return min(value, 10**9) if type(value) is int and value > 0 else 0


# Hermes classifies each failed attempt (agent.error_classifier.FailoverReason). Prefer that verdict:
# connection timeouts, Codex quota replies and 529 responses reach the hook with no HTTP status.
_REASON_OUTCOMES = {
    "rate_limit": "rate_limit", "upstream_rate_limit": "rate_limit", "auth": "auth", "auth_permanent": "auth",
    "timeout": "timeout", "overloaded": "upstream", "server_error": "upstream", "upstream_blocked": "upstream",
}
_STATUS_OUTCOMES = {401: "auth", 403: "auth", 408: "timeout", 429: "rate_limit", 500: "upstream",
                    502: "upstream", 503: "upstream", 504: "upstream", 529: "upstream"}
# post_auxiliary_call reports a failure as ``error_type`` plus "<Type>: <text>" and carries no status_code.
_ERROR_TYPE_OUTCOMES = {
    "RateLimitError": "rate_limit", "AuthenticationError": "auth", "PermissionDeniedError": "auth",
    "APITimeoutError": "timeout", "TimeoutError": "timeout", "ReadTimeout": "timeout", "ConnectTimeout": "timeout",
    "WriteTimeout": "timeout", "PoolTimeout": "timeout", "TimeoutException": "timeout",
    "InternalServerError": "upstream", "APIConnectionError": "upstream", "ServiceUnavailableError": "upstream",
    "OverloadedError": "upstream",
}
_ERROR_STATUS = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,80}: Error code: ([0-9]{3})\b")
TOOL_FAILURES = ("error", "timeout")


def _outcome(event: str, info: dict) -> str:
    if event == "api_error" or event == "aux" and info.get("error"):
        reason, code = info.get("reason"), info.get("status_code")
        if isinstance(reason, str) and reason in _REASON_OUTCOMES:
            return _REASON_OUTCOMES[reason]
        if type(code) is int:
            return _STATUS_OUTCOMES.get(code, "error")
        kind, text = info.get("error_type"), info.get("error")
        if isinstance(kind, str) and kind in _ERROR_TYPE_OUTCOMES:
            return _ERROR_TYPE_OUTCOMES[kind]
        # Only the numeric status leaves the provider message.
        reported = _ERROR_STATUS.match(text[:160]) if isinstance(text, str) else None
        return _STATUS_OUTCOMES.get(int(reported[1]), "error") if reported else "error"
    if event == "tool":
        status = info.get("status")
        return status if isinstance(status, str) and status in ("ok", "error", "blocked", "cancelled", "timeout") else "unknown"
    return "ok"


def observe(home: Path, event: str, info: dict, *, now: float | None = None) -> None:
    """Store numeric usage and allowlisted metadata. Never copy raw payload fields."""
    if event not in {"api", "api_error", "aux", "tool"}:
        raise ValueError("Invalid observer event")
    now = time.time() if now is None else now
    outcome = _outcome(event, info)
    if event == "tool":
        lane, provider, model = "tool", "", ""
    else:
        lane = f"aux:{_label(info.get('aux_task'))}" if event == "aux" else "main"
        provider = _label(info.get("provider"))
        model = _label(info.get("response_model") or info.get("model"))
    usage = info.get("usage")
    usage = usage if isinstance(usage, dict) and outcome == "ok" else {}
    measured = int(any(type(usage.get(key)) is int and usage[key] >= 0 for key in BUCKETS[:4]))
    duration = info.get("duration_ms")
    if type(duration) not in (int, float):
        seconds = info.get("api_duration", info.get("duration"))
        duration = seconds * 1000 if type(seconds) in (int, float) else 0
    duration = int(min(duration, 86_400_000)) if 0 <= duration < math.inf else 0
    # The IDs only guard against one event being delivered twice, such as a plugin registered twice.
    correlation = info.get("tool_call_id" if event == "tool" else "api_request_id")
    turn = info.get("turn_id")
    identity = (event, turn, correlation)
    if event == "api_error":
        # Hermes keeps one api_request_id for every retry of a request. The attempt number and its
        # end time separate the failed attempts, and are equal for a repeated delivery.
        retry, ended = info.get("retry_count"), info.get("ended_at")
        attempt = (retry if type(retry) is int else None,
                   ended if type(ended) in (int, float) and math.isfinite(ended) else None)
        identity += attempt if attempt != (None, None) else ()
    delivery = None
    if event != "aux" and all(isinstance(part, str) and 0 < len(part) <= 400 for part in (turn, correlation)):
        delivery = hashlib.sha256(repr(identity).encode()).hexdigest()
    day = _day(now)
    row = (day, lane, provider, model, outcome, measured, *(_tokens(usage.get(key)) for key in BUCKETS), duration)

    def write(conn):
        if delivery and not conn.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)", (delivery, day)).rowcount:
            return
        conn.execute("""
            INSERT INTO metric VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO UPDATE SET requests=requests+1, usage_samples=usage_samples+excluded.usage_samples,
              input_tokens=input_tokens+excluded.input_tokens, output_tokens=output_tokens+excluded.output_tokens,
              cache_read_tokens=cache_read_tokens+excluded.cache_read_tokens,
              cache_write_tokens=cache_write_tokens+excluded.cache_write_tokens,
              reasoning_tokens=reasoning_tokens+excluded.reasoning_tokens, duration_ms=duration_ms+excluded.duration_ms
            """, row)

    record(home, write, now=now)


def summarize(conn: sqlite3.Connection, *, days: int, now: float, rates: dict) -> dict:
    if not 1 <= days <= RETENTION_DAYS:
        raise ValueError(f"days must be between 1 and {RETENTION_DAYS}")
    start = _day(now - (days - 1) * 86_400)
    rows = [dict(row) for row in conn.execute("""
        SELECT lane, provider, model, outcome, SUM(requests) AS requests,
          SUM(usage_samples) AS usage_samples, SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
          SUM(cache_read_tokens) AS cache_read_tokens, SUM(cache_write_tokens) AS cache_write_tokens,
          SUM(reasoning_tokens) AS reasoning_tokens, SUM(duration_ms) AS duration_ms
        FROM metric WHERE day BETWEEN ? AND ? AND lane != 'observer' GROUP BY lane, provider, model, outcome
        ORDER BY requests DESC, lane, provider, model, outcome
        """, (start, _day(now)))]
    dropped = conn.execute("SELECT COALESCE(SUM(requests), 0) FROM metric WHERE day BETWEEN ? AND ? AND lane='observer'",
                           (start, _day(now))).fetchone()[0]
    estimate = 0.0
    priced_calls = 0
    for row in rows:
        price = rates.get(row["provider"], {})
        price = price.get(row["model"]) if isinstance(price, dict) else None
        rates_by_bucket = ("input", "output", "cache_read", "cache_write")
        valid = isinstance(price, dict) and all(
            type(price.get(key)) in (int, float) and 0 <= price[key] <= 1_000_000 for key in rates_by_bucket
        )
        row["estimated_usd"] = None
        if valid and row["usage_samples"]:
            amount = sum(row[bucket] * price[key] for bucket, key in zip(BUCKETS, rates_by_bucket)) / 1_000_000
            row["estimated_usd"] = round(amount, 8)
            estimate += amount
            priced_calls += row["usage_samples"]
    llm = [row for row in rows if row["lane"] != "tool" and row["outcome"] == "ok"]
    successful = sum(row["requests"] for row in llm)
    measured = sum(row["usage_samples"] for row in llm)
    return {"window_start_utc": start, "window_end_utc": _day(now), "groups": rows,
            "successful_llm_calls": successful, "usage_samples": measured, "priced_calls": priced_calls,
            "estimated_usd": round(estimate, 8) if priced_calls else None, "dropped_events": dropped}


def recommendations(usage: dict) -> list[dict]:
    by_lane = usage["groups"]
    proposals = []
    for outcome, instruction in (
        ("rate_limit", "Measure concurrent jobs and backoff. Test staggering schedules before changing routing."),
        ("auth", "Review provider credential status using Hermes diagnostics."),
        ("upstream", "Compare upstream incidents and existing fallback behavior before changing models."),
    ):
        count = sum(row["requests"] for row in by_lane if row["outcome"] == outcome)
        if count >= 3:
            proposals.append({"code": f"llm_{outcome}", "severity": "warning", "count": count, "action": instruction})
    aux_tokens = Counter()
    aux_samples = Counter()
    for row in by_lane:
        if row["lane"].startswith("aux:"):
            task = row["lane"][4:]
            aux_samples[task] += row["usage_samples"]
            aux_tokens[task] += sum(row[bucket] for bucket in BUCKETS[:4])
    for task, measured_tokens in aux_tokens.items():
        if aux_samples[task] >= 5 and measured_tokens >= 20_000:
            proposals.append({"code": "aux_" + hashlib.sha256(task.encode()).hexdigest()[:12], "severity": "info",
                              "task": task, "tokens": measured_tokens,
                              "action": "Benchmark the existing auxiliary task on a cheaper model and check quality."})
    main = [row for row in by_lane if row["lane"] == "main" and row["usage_samples"]]
    samples = sum(row["usage_samples"] for row in main)
    prompt = sum(sum(row[bucket] for bucket in (BUCKETS[0], BUCKETS[2], BUCKETS[3])) for row in main)
    hits = sum(row["cache_read_tokens"] for row in main)
    if samples >= 5 and prompt >= 5000 * samples and hits / prompt < 0.15:
        proposals.append({"code": "large_main_prompt", "severity": "info", "average_prompt_tokens": prompt // samples,
                          "cache_share": round(hits / prompt, 3),
                          "action": "Measure tool schemas, injected memory and instruction size; test existing retrieval controls."})
    tools = {row["outcome"]: row["requests"] for row in by_lane if row["lane"] == "tool"}
    errors = sum(tools.get(outcome, 0) for outcome in TOOL_FAILURES)
    attempts = tools.get("ok", 0) + errors
    if attempts >= 10 and errors / attempts >= 0.25:
        proposals.append({"code": "tool_errors", "severity": "warning", "errors": errors, "attempts": attempts,
                          "action": "Use existing traces to find the failing tools and compare retries with successful runs."})
    dropped = usage.get("dropped_events", 0)
    if dropped >= 10:
        proposals.append({"code": "observer_dropped_events", "severity": "info", "count": dropped,
                          "action": "Treat these totals as a lower bound and look for a long writer on the Evolution database."})
    return proposals


def run_policy(conn: sqlite3.Connection, findings: list[dict], *, now: float, recovery_ready: bool = True) -> dict:
    """Advance decisions at most once per UTC day of evidence and report transitions."""
    today = _day(now)
    cutoff = (datetime.fromtimestamp(now, timezone.utc).date() - timedelta(days=RETENTION_DAYS - 1)).isoformat()
    conn.execute("DELETE FROM seen WHERE day < ?", (cutoff,))
    conn.execute("DELETE FROM metric WHERE day < ?", (cutoff,))
    conn.execute("DELETE FROM research_latency WHERE day < ?", (cutoff,))
    conn.execute("DELETE FROM native_activity WHERE day < ?", (cutoff,))
    conn.execute("DELETE FROM maintenance WHERE active=0 AND feedback IS NULL AND last_seen < ?",
                 (now - 90 * 86400,))
    current = {item["code"]: item for item in findings}
    prior = {row["code"]: dict(row) for row in conn.execute("SELECT * FROM maintenance")}
    changed = []
    for code, item in current.items():
        old = prior.get(code)
        if old is None or not old["active"]:
            changed.append({"code": code, "transition": "opened"})
        conn.execute("""INSERT INTO maintenance(code,active,first_seen,last_seen,samples,last_day) VALUES (?,1,?,?,1,?)
            ON CONFLICT(code) DO UPDATE SET active=1,last_seen=excluded.last_seen,last_day=excluded.last_day,
            samples=CASE WHEN active=0 THEN 1 ELSE samples + (last_day != excluded.last_day) END
            """, (code, now, now, today))
        item["observed_days"] = 1 if not old or not old["active"] else old["samples"] + (old["last_day"] != today)
        item["feedback"] = old["feedback"] if old else None
    for code, old in prior.items():
        if old["active"] and code not in current:
            conn.execute("UPDATE maintenance SET active=0 WHERE code=?", (code,))
            changed.append({"code": code, "transition": "cleared"})
    severe = any(item.get("severity") in ("critical", "error") or item.get("reduce_context") for item in findings)
    state = conn.execute("SELECT value FROM control WHERE key='mode'").fetchone()
    previous = state[0] if state else "normal"
    # Pause only the extension's optional context reinforcement. Recover after two healthy ticks.
    clear_checks = 0
    last_clear = 0.0
    if severe or previous == "conservative" and not recovery_ready:
        mode = "conservative"
    elif previous == "conservative":
        state = dict(conn.execute("SELECT key,value FROM control WHERE key IN ('clear_checks','last_clear')"))
        clear_checks = int(state.get("clear_checks", 0))
        last_clear = float(state.get("last_clear", 0))
        if not clear_checks or now - last_clear >= RECOVERY_SPACING_SECONDS:
            clear_checks += 1
            last_clear = now
        mode = "normal" if clear_checks >= 2 else "conservative"
    else:
        mode = "normal"
    conn.executemany("INSERT OR REPLACE INTO control VALUES(?,?)", (
        ("mode", mode), ("clear_checks", str(clear_checks)), ("last_clear", str(last_clear)),
    ))
    if previous != mode:
        changed.append({"code": "evolution_mode", "transition": mode})
    order = {"critical": 0, "error": 0, "warning": 1, "info": 2}
    feedback_order = {"helpful": 0, None: 1, "dismissed": 2}
    findings.sort(key=lambda item: (order.get(item.get("severity"), 3), feedback_order[item["feedback"]],
                                    -item["observed_days"], item["code"]))
    return {"mode": mode, "changes": changed, "findings": findings}
