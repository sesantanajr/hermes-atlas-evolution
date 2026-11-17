"""Profile-local observer metrics and a feedback-driven maintenance loop."""

from __future__ import annotations

import atexit
import hashlib
import math
import os
import re
import sqlite3
import sys
import threading
import time
from collections import Counter, OrderedDict, deque
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

BUCKETS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")
RETENTION_DAYS = 35
SCHEMA_VERSION = 8
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
    folder.mkdir(mode=0o700, exist_ok=True)  # A mistyped profile path must fail, not become a new tree.
    path = folder / "evolution.sqlite3"
    # Create the file with private POSIX permissions before SQLite creates sidecars.
    if not path.exists():
        try:
            path.touch(mode=0o600, exist_ok=False)
        except FileExistsError:
            pass
    return path


# ── self-repair: a damaged database is set aside and started again ─────────────────────────────────────────────
# A torn write (power loss, a full disk, a sync tool copying the file while it is written) leaves a file SQLite calls
# "malformed" or "not a database". Every open then failed, quietly, for as long as the process ran: no pass, no lesson,
# no counter. Now the damage is confirmed on a connection of its own, the file (with its -wal and -shm) is renamed
# to evolution.sqlite3.corrupt-<time> next to it, a fresh database starts, and `status` says so for a week.
_DAMAGED = (11, 26)              # SQLITE_CORRUPT, SQLITE_NOTADB
_REPAIRED: set[str] = set()      # once per file and process: a file that breaks again is not renamed in a loop
_KEEP_DAMAGED = 2                # the newest damaged copies kept; older ones are removed
RECOVERED_FILE = "db-recovered.json"


def damaged(error: BaseException) -> bool:
    """Whether a sqlite error says the database file is damaged (not busy, not a schema problem)."""
    if not isinstance(error, sqlite3.DatabaseError):
        return False
    code = getattr(error, "sqlite_errorcode", None)
    if type(code) is int:
        return code & 0xFF in _DAMAGED
    text = str(error).lower()
    return "not a database" in text or "malformed" in text


def _confirmed(path: Path) -> bool:
    """Read the file again on a connection of its own: True only when SQLite itself finds it damaged."""
    from urllib.parse import quote

    try:
        probe = sqlite3.connect(f"file:{quote(os.fspath(path))}?mode=ro", uri=True, timeout=1)
    except sqlite3.Error as error:
        return damaged(error)
    try:
        row = probe.execute("PRAGMA quick_check(1)").fetchone()
        return not (row and str(row[0]).lower() == "ok")
    except sqlite3.Error as error:
        return damaged(error)
    finally:
        probe.close()


def recover_damaged(path: Path, error: BaseException) -> bool:
    """Set a damaged database aside so the next open starts a fresh one. True when it was set aside."""
    key = os.fspath(path)
    if key in _REPAIRED or not damaged(error) or not Path(path).is_file() or not _confirmed(Path(path)):
        return False
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    target = Path(path).with_name(f"{Path(path).name}.corrupt-{stamp}")
    for extra in range(1, 100):   # never over an earlier damaged copy set aside in the same second
        if not target.exists():
            break
        target = Path(path).with_name(f"{Path(path).name}.corrupt-{stamp}-{extra}")
    try:
        os.replace(path, target)
    except OSError:
        return False   # open elsewhere where a rename is refused (Windows), or gone: the next failure tries again
    for suffix in ("-wal", "-shm"):   # they belong to the damaged file: a fresh one must never replay them
        try:
            os.replace(f"{key}{suffix}", f"{target}{suffix}")
        except FileNotFoundError:
            pass
        except OSError:
            try:
                os.unlink(f"{key}{suffix}")
            except OSError:
                pass
    _REPAIRED.add(key)
    _ADDITIVE_READY.discard(key)
    try:
        copies = sorted(Path(path).parent.glob(f"{Path(path).name}.corrupt-*[0-9]"),
                        key=lambda item: item.stat().st_mtime)
        for old in copies[:-_KEEP_DAMAGED]:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(f"{old}{suffix}")
                except OSError:
                    pass
    except OSError:
        pass
    try:
        import json

        note = Path(path).parent / RECOVERED_FILE
        temporary = note.with_name(f".{RECOVERED_FILE}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps({"at": time.time(), "copy": target.name,
                                         "error": getattr(error, "sqlite_errorname", None) or type(error).__name__}),
                             encoding="utf-8")
        os.replace(temporary, note)
    except OSError:
        pass
    from logging import getLogger

    getLogger(__name__).warning("Atlas Evolution found its database damaged (%s); it was set aside as %s and a new "
                                "one started", getattr(error, "sqlite_errorname", None) or type(error).__name__,
                                target.name)
    return True


def recovered(home: Path, *, now: float, days: int = 7) -> dict | None:
    """The last self-repair of this profile's database, when it was in the last `days`."""
    import json

    try:
        found = json.loads((Path(home) / "evolution" / RECOVERED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(found, dict) or not isinstance(found.get("at"), (int, float)) or now - found["at"] > days * 86_400:
        return None
    return {"at": float(found["at"]), "copy": str(found.get("copy") or "")[:120], "error": str(found.get("error") or "")[:40]}


def _open(path: Path, timeout: float, **options) -> sqlite3.Connection:
    try:
        return _open_once(path, timeout, **options)
    except sqlite3.DatabaseError as error:
        if not recover_damaged(path, error):
            raise
    return _open_once(path, timeout, **options)


def _open_once(path: Path, timeout: float, **options) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=timeout, **options)
    try:
        connection.row_factory = sqlite3.Row
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version in (0, 1, 2, 3, 4, 5, 6, 7):
            if version == 0:
                connection.execute("PRAGMA journal_mode=WAL")
            # 4: evolution loop, 5: skill outcomes, 6: reflector, 7: frozen lesson baselines, 8: runtime guard
            from .experience import SCHEMA as experience_schema, ensure_columns

            connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in (SCHEMA + experience_schema).split(";"):
                    if statement.strip():
                        connection.execute(statement)
                ensure_columns(connection)
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        elif version < 0:
            raise ValueError(f"Unsupported Evolution database schema {version}")
        _ensure_additive(connection, path)
        # A newer version of the plugin only ever adds tables and columns, so a database it upgraded still
        # works here after a downgrade: what this version reads and writes is all still there.
        connection.execute("PRAGMA foreign_keys=ON")
        if sys.platform == "darwin":
            # macOS fsync does not order writes: a checkpoint cut by a shutdown can leave half-written pages.
            # Hermes forces full syncs there for its own state.db (hermes_state_wal.py); so does this database.
            connection.execute("PRAGMA checkpoint_fullfsync=1")
            connection.execute("PRAGMA synchronous=FULL")
    except BaseException:
        connection.close()
        raise
    return connection


_ADDITIVE_READY: set[str] = set()


def _ensure_additive(connection: sqlite3.Connection, path: Path) -> None:
    """Create the tables and indexes added after schema 8 (experience.ADDITIVE_TABLES) that this database lacks.
    Checked once per database file and process; the check is a read, so readers never wait for a writer here."""
    key = os.fspath(path)
    if key in _ADDITIVE_READY:
        return
    from .experience import ADDITIVE_TABLES

    present = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'index')")}
    missing = [statement for name, statement in ADDITIVE_TABLES if name not in present]
    if missing:
        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in missing:
                connection.execute(statement)
            connection.execute("COMMIT")
        except BaseException:
            _rollback(connection)
            raise
    _ADDITIVE_READY.add(key)


@contextmanager
def database(home: Path, *, observer: bool = False, timeout: float = 2):
    """Open the profile database. Hook callers share one connection per process. `timeout`: the busy wait of
    a connection of its own (a reader on a hook path passes a short one)."""
    if observer:
        with _observer(home).session() as connection:
            yield connection
        return
    connection = _open(_database_path(home), timeout)
    try:
        with connection:
            yield connection
    except sqlite3.DatabaseError as error:
        if damaged(error):
            connection.close()   # closed first: Windows refuses to rename a file that is open
            recover_damaged(_database_path(home), error)
        raise
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


class _ObserverClosed(sqlite3.OperationalError):
    """This observer was closed (evicted, or at exit). A new one serves the profile; a caller sees a busy database."""

    def __init__(self):
        super().__init__("database is locked")


class _Observer:
    """One connection per process and profile for hook callbacks, with a bounded retry queue.

    Hermes runs each hook callback on its own thread. A connection per event made the threads of a
    single process compete for the write lock, and an event that outlived the busy wait was lost.

    Hook writes only insert and update counters. Retention runs in the maintenance pass (evolve.cycle and the
    tick), never here: a hook must stay cheap.
    """

    def __init__(self, home: Path):
        self.home = Path(home)
        self.lock = threading.RLock()  # Serializes every use of the shared connection.
        self.guard = threading.Lock()  # Protects the queue for threads that could not wait.
        self.pending: deque = deque()
        self.dropped = 0
        self.connection: sqlite3.Connection | None = None
        self.identity = None
        self.closed = False   # set once: a closed observer never opens a connection again
        self._depth = 0       # nested holds of `lock` by its owner thread
        self._checked = float("-inf")   # when the file's identity was last read (time.monotonic)

    def _file(self):
        try:
            info = os.stat(self.home / "evolution" / "evolution.sqlite3")
        except OSError:
            return None
        return os.getpid(), info.st_dev, info.st_ino

    def _connect(self) -> sqlite3.Connection:
        # A removed or replaced file, or a forked process, needs a handle of its own. The file is looked at again
        # at most every IDENTITY_SECONDS: a stat on every hook write was a third of its cost.
        if self.connection is not None and self.identity is not None and self.identity[0] == os.getpid() \
                and 0 <= time.monotonic() - self._checked < IDENTITY_SECONDS:
            return self.connection
        current = self._file()
        self._checked = time.monotonic()
        if self.connection is not None and current is not None and current == self.identity:
            return self.connection
        self._release()
        if self.closed:
            # A thread that still holds a reference to an evicted or closed observer must not leak a new handle.
            raise _ObserverClosed()
        connection = _open(_database_path(self.home), OBSERVER_BUSY_SECONDS, check_same_thread=False)
        try:
            if connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal" and sys.platform != "darwin":
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

    def _enter(self, timeout: float) -> bool:
        if not self.lock.acquire(timeout=timeout):
            return False
        self._depth += 1
        return True

    def _exit(self) -> None:
        try:
            self._depth -= 1
            if self.closed and not self._depth:
                self._release()   # closed while this thread held it: the last holder frees the handle
        finally:
            self.lock.release()

    @contextmanager
    def session(self):
        """Lend the shared connection. A caller that cannot get it in time sees a busy database."""
        if not self._enter(_OBSERVER_LOCK_SECONDS):
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
                    if damaged(error):
                        recover_damaged(_database_path(self.home), error)
                raise
            try:
                self._write(None, time.time())  # Events that arrived during the caller's transaction.
            except (OSError, ValueError, sqlite3.Error):
                pass
        finally:
            self._exit()

    def record(self, job, now: float) -> bool:
        """Apply one counter update, or keep it for the next write while the database is busy.
        False when this observer is closed: the caller hands the job to the profile's current observer."""
        if self.closed:
            return False
        if not self._enter(_OBSERVER_LOCK_SECONDS):
            return self._defer(job)
        try:
            if self.closed:
                return False
            self._write(job, now)
        except _ObserverClosed:
            return False
        finally:
            self._exit()
        return True

    def _defer(self, job) -> bool:
        with self.guard:
            if self.closed:
                return False
            for item in (job if isinstance(job, list) else [job]):
                if len(self.pending) < _OBSERVER_QUEUE:
                    self.pending.append(item)
                else:
                    self.dropped += 1
            return True

    def _write(self, job, now: float) -> None:
        with self.guard:
            waiting, dropped = list(self.pending), self.dropped
        jobs = waiting + (list(job) if isinstance(job, list) else [job] if job is not None else [])
        if not jobs and not dropped:
            return
        day = _day(now)
        try:
            connection = self._connect()
            if connection.in_transaction:  # A caller of session() on this thread is mid-transaction.
                raise sqlite3.OperationalError("database is locked")
            connection.execute("BEGIN IMMEDIATE")
        except _ObserverClosed:
            raise
        except (OSError, ValueError):
            # The folder or the schema is unusable now: the job is kept (bounded) for when it is usable again.
            if job is not None:
                self._defer(job)
            raise
        except sqlite3.Error as error:
            if _busy(error):
                if job is not None:
                    self._defer(job)
                return
            self._release()
            self._settle(waiting, lost=len(waiting))
            if damaged(error):
                recover_damaged(_database_path(self.home), error)
            raise
        lost = 0
        try:
            for item in jobs:
                connection.execute("SAVEPOINT observer_job")
                try:
                    item(connection)
                except Exception as error:
                    if isinstance(error, sqlite3.Error) and _busy(error):
                        raise
                    # This one update cannot be applied (a value SQLite cannot bind, a broken job): it alone is
                    # lost, and it never blocks the queue behind it.
                    connection.execute("ROLLBACK TO observer_job")
                    lost += 1
                connection.execute("RELEASE observer_job")
            if dropped + lost:
                connection.execute("""INSERT INTO metric VALUES (?, 'observer', '', '', 'dropped', ?, 0, 0, 0, 0, 0, 0, 0)
                    ON CONFLICT DO UPDATE SET requests=requests+excluded.requests""", (day, dropped + lost))
            connection.execute("COMMIT")
        except BaseException as error:
            _rollback(connection)
            if isinstance(error, sqlite3.Error) and _busy(error):
                if job is not None:
                    self._defer(job)
                return
            if isinstance(error, Exception):
                # A transaction that cannot be applied must not fail every later write of the queue.
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
        """Write what is waiting and release the handle. Waits at most `wait` for the lock (0: not at all); when
        another thread holds it, that thread releases the handle as it leaves. Never opens a new connection."""
        with self.guard:
            self.closed = True
        if not self._enter(max(0.0, wait)):
            return
        try:
            if self._depth == 1 and (self.pending or self.dropped) and self.connection is not None:
                try:
                    budget = max(wait, OBSERVER_BUSY_SECONDS)
                    self._connect().execute(f"PRAGMA busy_timeout={int(budget * 1000)}")
                    self._write(None, time.time())
                except (OSError, ValueError, sqlite3.Error):
                    pass
        finally:
            self._exit()


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
        other.close(wait=0)   # never blocks the hook thread; a thread still using it frees the handle
    return observer


_BATCH = threading.local()
IDENTITY_SECONDS = 2.0   # how often the shared writer checks that the database file is still the one it opened


def record(home: Path, job, *, now: float | None = None) -> None:
    """Run ``job(connection)`` in one write transaction of the shared observer connection.

    The job may run later, on another thread. It must hold only values that are safe to store. Inside
    ``batched(home)`` on the same thread, the job is collected instead and written with the batch.
    """
    jobs = getattr(_BATCH, "jobs", None)
    if jobs is not None and _BATCH.home == Path(home):
        jobs.append(job)
        return
    now = time.time() if now is None else now
    for _ in range(3):   # an observer closed meanwhile (evicted, or at exit) hands the job to the current one
        if _observer(home).record(job, now):
            return


@contextmanager
def coalesced(home: Path):
    """Collect this thread's counter updates for `home` (one hook call: its usage counters and its outcome) and hand
    them to the shared writer together: one write transaction instead of one per update, each update still in a
    savepoint of its own. Inside `batched()` the updates go with that batch instead."""
    if getattr(_BATCH, "jobs", None) is not None:
        yield
        return
    jobs: list = []
    _BATCH.jobs, _BATCH.home = jobs, Path(home)
    try:
        yield
    finally:
        _BATCH.jobs = _BATCH.home = None
        if jobs:
            now = time.time()
            for _ in range(3):   # an observer closed meanwhile hands the jobs to the current one
                if _observer(home).record(jobs, now):
                    break


@contextmanager
def batched(home: Path, *, check=None, timeout: float = 10.0):
    """Collect this thread's counter updates for `home` and write them in one transaction on a clean exit.

    The backfill replays thousands of past events: one transaction per slice instead of one per event,
    and a slice that fails half way writes nothing. The write is synchronous, on a connection of its own,
    never through the hooks' queue: `check(connection)` runs first inside the same write transaction and
    raises to drop the batch (another pass moved on meanwhile). A database that stays busy for `timeout`
    seconds raises sqlite3.OperationalError and nothing is written. Other threads (the hooks) are not
    affected. The yielded list is the batch: a caller may drop what it appended since a mark."""
    if getattr(_BATCH, "jobs", None) is not None:
        raise RuntimeError("batched() does not nest")
    jobs: list = []
    _BATCH.jobs, _BATCH.home = jobs, Path(home)
    try:
        yield jobs
    finally:
        _BATCH.jobs = _BATCH.home = None
    if not jobs and check is None:
        return
    connection = _open(_database_path(Path(home)), timeout)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            if check is not None:
                check(connection)
            for item in jobs:
                item(connection)
            connection.execute("COMMIT")
        except BaseException:
            _rollback(connection)
            raise
    finally:
        connection.close()


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
    from .experience import utc_day

    return utc_day(now)


def _label(value) -> str:
    if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_.:/+-]{1,90}", value):
        lower = value.lower()
        segments = re.split(r"[/:]", lower)
        credential_prefixes = ("sk-", "ghp_", "gho_", "ghs_", "ghr_", "ghu_", "github_pat_", "xox", "akia", "aiza", "hf_")
        # A local server can report the model as its file path. Keep namespaced names, never a path.
        path_like = any(part in ("", ".", "..") for part in lower.split("/")) or re.match(r"[a-z]:/", lower)
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
    # Hermes keeps one api_request_id for every retry of a request and reuses it after a refunded pass,
    # and a provider may repeat a tool_call_id in a later response. The attempt number, the end time and
    # the request of a tool call tell real events apart, and are equal for a repeated delivery.
    correlation = info.get("tool_call_id" if event == "tool" else "api_request_id")
    turn = info.get("turn_id")
    if event == "tool":
        request = info.get("api_request_id")
        attempt = (request if isinstance(request, str) and 0 < len(request) <= 400 else None,)
    else:
        retry, ended = info.get("retry_count"), info.get("ended_at")
        attempt = (retry if type(retry) is int and event == "api_error" else None,
                   ended if type(ended) in (int, float) and math.isfinite(ended) else None)
    identity = (event, turn, correlation, *(attempt if any(part is not None for part in attempt) else ()))
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


SEEN_DAYS = 2   # delivery ids only guard against an event delivered twice, which happens within seconds


def _prune(conn: sqlite3.Connection, now: float) -> None:
    from .experience import prune, utc_datetime

    # The clock the experience tables were pruned by: never far past the last prune, so a clock that is far ahead
    # does not delete these counters either.
    today = utc_datetime(prune(conn, now=now)).date()
    cutoff = (today - timedelta(days=RETENTION_DAYS - 1)).isoformat()
    for table in ("metric", "research_latency", "native_activity"):
        conn.execute(f"DELETE FROM {table} WHERE day < ?", (cutoff,))
    conn.execute("DELETE FROM seen WHERE day < ?", ((today - timedelta(days=SEEN_DAYS - 1)).isoformat(),))


def run_policy(conn: sqlite3.Connection, findings: list[dict], *, now: float, recovery_ready: bool = True) -> dict:
    """Advance decisions at most once per UTC day of evidence and report transitions."""
    today = _day(now)
    _prune(conn, now)
    conn.execute("DELETE FROM maintenance WHERE active=0 AND feedback IS NULL AND last_seen < ?",
                 (now - 90 * 86400,))
    order = {"critical": 0, "error": 0, "warning": 1, "info": 2}
    current = {}
    for item in findings:  # One decision per code. A code reported twice keeps its most severe report.
        known = current.get(item["code"])
        if known is None or order.get(item.get("severity"), 3) < order.get(known.get("severity"), 3):
            current[item["code"]] = item
    findings = list(current.values())
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
    feedback_order = {"helpful": 0, "dismissed": 2}
    findings.sort(key=lambda item: (order.get(item.get("severity"), 3), feedback_order.get(item["feedback"], 1),
                                    -item["observed_days"], item["code"]))
    return {"mode": mode, "changes": changed, "findings": findings}
