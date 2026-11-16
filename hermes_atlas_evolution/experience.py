"""Experience tables of the evolution loop, stored in the profile's Evolution database.

They hold aggregated tool outcomes, turn outcomes, lessons, tracked changes and proposals. Nothing here
contains conversation text, tool arguments, tool output or raw log lines: only counters, allowlisted
labels and short identifier-like hints. Hook callbacks write through the shared observer connection of
`operations`, so concurrent threads and processes never lose an event to a busy database.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

RETENTION_DAYS = 90
_LABEL = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9_.:+-]{0,95}\Z")
_CREDENTIAL = ("sk-", "ghp_", "gho_", "ghs_", "ghr_", "ghu_", "github_pat_", "xox", "akia", "aiza", "hf_")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_daily (
    day TEXT NOT NULL, tool TEXT NOT NULL, calls INTEGER NOT NULL, errors INTEGER NOT NULL,
    blocked INTEGER NOT NULL, duration_ms INTEGER NOT NULL, PRIMARY KEY (day, tool)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS failure (
    signature TEXT PRIMARY KEY, tool TEXT NOT NULL, klass TEXT NOT NULL, hint TEXT NOT NULL,
    first_seen REAL NOT NULL, last_seen REAL NOT NULL, total INTEGER NOT NULL,
    sessions INTEGER NOT NULL, last_session TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS failure_daily (
    day TEXT NOT NULL, signature TEXT NOT NULL, count INTEGER NOT NULL, PRIMARY KEY (day, signature)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS turn_daily (
    day TEXT NOT NULL, surface TEXT NOT NULL, turns INTEGER NOT NULL, completed INTEGER NOT NULL,
    failed INTEGER NOT NULL, interrupted INTEGER NOT NULL, slow INTEGER NOT NULL, with_tools INTEGER NOT NULL,
    tool_errors INTEGER NOT NULL, duration_ms INTEGER NOT NULL, max_ms INTEGER NOT NULL,
    b15 INTEGER NOT NULL, b30 INTEGER NOT NULL, b60 INTEGER NOT NULL, b120 INTEGER NOT NULL,
    b300 INTEGER NOT NULL, b_more INTEGER NOT NULL, PRIMARY KEY (day, surface)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS feedback_daily (
    day TEXT NOT NULL, kind TEXT NOT NULL, count INTEGER NOT NULL, PRIMARY KEY (day, kind)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS skill_daily (
    day TEXT NOT NULL, skill TEXT NOT NULL, action TEXT NOT NULL, count INTEGER NOT NULL,
    PRIMARY KEY (day, skill, action)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS skill_outcome_daily (
    day TEXT NOT NULL, skill TEXT NOT NULL, turns INTEGER NOT NULL, failed INTEGER NOT NULL,
    interrupted INTEGER NOT NULL, slow INTEGER NOT NULL, tool_error_turns INTEGER NOT NULL,
    corrected INTEGER NOT NULL, duration_ms INTEGER NOT NULL, PRIMARY KEY (day, skill)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS failure_sample (
    signature TEXT PRIMARY KEY, excerpt TEXT NOT NULL, updated REAL NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS review_daily (
    day TEXT NOT NULL, result TEXT NOT NULL, reviews INTEGER NOT NULL, calls INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, cache_read_tokens INTEGER NOT NULL,
    PRIMARY KEY (day, result)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS lesson (
    id INTEGER PRIMARY KEY, signature TEXT UNIQUE, tool TEXT NOT NULL, klass TEXT NOT NULL,
    hint TEXT NOT NULL, origin TEXT NOT NULL, status TEXT NOT NULL, text TEXT,
    created REAL NOT NULL, updated REAL NOT NULL, activated REAL,
    shown INTEGER NOT NULL DEFAULT 0, hints INTEGER NOT NULL DEFAULT 0,
    verdict TEXT, rate_before REAL, rate_after REAL, evaluated REAL,
    baseline_failures INTEGER, baseline_calls INTEGER
);
CREATE INDEX IF NOT EXISTS lesson_status ON lesson(status);
CREATE TABLE IF NOT EXISTS change (
    id INTEGER PRIMARY KEY, ref TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, subject TEXT NOT NULL,
    detail TEXT NOT NULL, at REAL NOT NULL, verdict TEXT, evaluated REAL, metrics TEXT
);
CREATE INDEX IF NOT EXISTS change_at ON change(at);
CREATE TABLE IF NOT EXISTS proposal (
    id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, risk TEXT NOT NULL,
    subject TEXT NOT NULL, evidence TEXT NOT NULL, command TEXT NOT NULL, status TEXT NOT NULL,
    created REAL NOT NULL, updated REAL NOT NULL, note TEXT
);
CREATE INDEX IF NOT EXISTS proposal_status ON proposal(status);
CREATE TABLE IF NOT EXISTS cursor (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS guard_daily (
    day TEXT NOT NULL, kind TEXT NOT NULL, count INTEGER NOT NULL, PRIMARY KEY (day, kind)
) WITHOUT ROWID;
"""


def utc_day(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).date().isoformat()


def day_offset(now: float, days: int) -> str:
    """The UTC day `days` before (positive) the day of `now`."""
    return utc_day(now - days * 86_400)


def label(value, *, fallback: str = "unknown") -> str:
    """Return an identifier-like label, or the fallback. Rejects URLs, paths and credential-shaped text."""
    if isinstance(value, str) and _LABEL.fullmatch(value):
        lower = value.lower()
        if "://" not in lower and not any(part.startswith(_CREDENTIAL) for part in re.split(r"[:.]", lower)):
            return value
    return fallback


def experience(home: Path, *, observer: bool = False):
    """Open the profile's Evolution database. Hook callers (`observer=True`) borrow the shared connection."""
    from .operations import database

    return database(Path(home), observer=observer)


def record(home: Path, job, *, now: float | None = None) -> None:
    """Apply one counter update through the shared observer connection, queued while the database is busy.

    `job(connection)` may run later on another thread, so it must hold only values that are safe to store.
    """
    from .operations import record as write

    write(Path(home), job, now=now)


# Columns added after a table first shipped: (table, column, declaration).
ADDED_COLUMNS = (("lesson", "baseline_failures", "INTEGER"), ("lesson", "baseline_calls", "INTEGER"))


def ensure_columns(conn: sqlite3.Connection) -> None:
    """Bring tables created by an earlier development schema up to date. Safe to run repeatedly."""
    for table, column, declaration in ADDED_COLUMNS:
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if present and column not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def prune(conn: sqlite3.Connection, *, now: float) -> None:
    """Drop aggregates older than the retention window. Lessons and open proposals are kept."""
    cutoff = day_offset(now, RETENTION_DAYS)
    for table in ("tool_daily", "failure_daily", "turn_daily", "feedback_daily", "skill_daily", "review_daily",
                  "skill_outcome_daily", "guard_daily"):
        conn.execute(f"DELETE FROM {table} WHERE day < ?", (cutoff,))
    old = now - RETENTION_DAYS * 86_400
    conn.execute("DELETE FROM failure WHERE last_seen < ? AND signature NOT IN "
                 "(SELECT signature FROM lesson WHERE signature IS NOT NULL AND status IN ('active','candidate'))", (old,))
    conn.execute("DELETE FROM change WHERE at < ?", (old,))
    conn.execute("DELETE FROM proposal WHERE status NOT IN ('open','approved') AND updated < ?", (old,))
    conn.execute("DELETE FROM lesson WHERE status='retired' AND updated < ?", (old,))
