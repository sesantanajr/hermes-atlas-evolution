"""Experience tables of the evolution loop, stored in the profile's Evolution database.

They hold aggregated tool outcomes, turn outcomes, lessons, tracked changes and proposals. Nothing here
contains conversation text, tool arguments, tool output or raw log lines: only counters, allowlisted
labels and short identifier-like hints. Hook callbacks write through the shared observer connection of
`operations`, so concurrent threads and processes never lose an event to a busy database.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import operations as _operations   # bound at import: a thread of an unloaded copy keeps its own module

RETENTION_DAYS = 90
SAMPLE_DAYS = 14   # redacted failure excerpts the reflector reads: kept only while they can still become a lesson
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
# Tables added after schema 8 without a version bump. A build that does not know a table ignores it, so
# adding tables keeps the database open to older builds, where a new column would break their positional
# inserts. operations creates any that are missing (tables and indexes) when it opens the database.
ADDITIVE_TABLES = (
    # 0.6.0 kept the owner's failures as one total per signature; 0.7.0 keeps them per day (below), so a
    # replay can take back exactly what it counted. Still created, so 0.6.0 keeps working after a downgrade.
    ("failure_owner", "CREATE TABLE IF NOT EXISTS failure_owner (signature TEXT PRIMARY KEY, total INTEGER NOT NULL) "
                      "WITHOUT ROWID"),
    # Failures seen in the owner's own turns (see signals.record_tool): only these let a lesson activate by itself.
    ("failure_owner_daily", "CREATE TABLE IF NOT EXISTS failure_owner_daily (day TEXT NOT NULL, signature TEXT NOT NULL, "
                            "count INTEGER NOT NULL, PRIMARY KEY (day, signature)) WITHOUT ROWID"),
    # Refused approvals per session origin and tool, with the Hermes setting that decided a refusal nobody could
    # answer ('' when someone could have): the radar tells those from the rest and names the setting.
    ("approval_daily", "CREATE TABLE IF NOT EXISTS approval_daily (day TEXT NOT NULL, source TEXT NOT NULL, "
                       "tool TEXT NOT NULL, setting TEXT NOT NULL, count INTEGER NOT NULL, "
                       "PRIMARY KEY (day, source, tool, setting)) WITHOUT ROWID"),
    # Derived lessons that auto mode left waiting only because the owner's own turns had not hit the failure
    # twice yet: they activate by themselves once they have (lessons.promote). The signature guards against
    # a reused lesson id.
    ("lesson_owner_wait", "CREATE TABLE IF NOT EXISTS lesson_owner_wait (lesson_id INTEGER PRIMARY KEY, "
                          "signature TEXT NOT NULL) WITHOUT ROWID"),
    # Lessons a pack brought in (packs.apply), and from where. Their evidence was gathered in another profile, so
    # lessons.tidy does not withdraw them for lacking local failures while they wait.
    ("lesson_import", "CREATE TABLE IF NOT EXISTS lesson_import (lesson_id INTEGER PRIMARY KEY, source TEXT NOT NULL, "
                      "at REAL NOT NULL) WITHOUT ROWID"),
    # The failures and tool calls of a lesson's activation day up to the moment it was activated (lessons.
    # _record_change): the baseline adds them, so a lesson activated right after a one-day burst has one.
    ("lesson_activation", "CREATE TABLE IF NOT EXISTS lesson_activation (lesson_id INTEGER PRIMARY KEY, "
                          "activated REAL NOT NULL, failures INTEGER NOT NULL, calls INTEGER NOT NULL) WITHOUT ROWID"),
    # Active lessons the owner chose to keep by rejecting the proposal to retire them: auto mode no longer
    # retires them for that activation (lessons.evaluate). `activated` ties the decision to one activation.
    ("lesson_keep", "CREATE TABLE IF NOT EXISTS lesson_keep (lesson_id INTEGER PRIMARY KEY, activated REAL NOT NULL, "
                    "at REAL NOT NULL) WITHOUT ROWID"),
    # Reflector calls that raised, per failure signature: after a few in a row the signature waits, so one
    # excerpt a provider keeps refusing does not take every call of every day (reflector.reflect).
    ("reflector_backoff", "CREATE TABLE IF NOT EXISTS reflector_backoff (signature TEXT PRIMARY KEY, "
                          "failures INTEGER NOT NULL, last_attempt REAL NOT NULL, retry_after REAL NOT NULL) WITHOUT ROWID"),
    # The owner's decisions, from the terminal or the dashboard panel (decisions.py): ids, states and setting
    # values only, so each can be listed and undone.
    ("decision_log", "CREATE TABLE IF NOT EXISTS decision_log (id INTEGER PRIMARY KEY, at REAL NOT NULL, actor TEXT NOT NULL, "
                     "kind TEXT NOT NULL, target TEXT NOT NULL, action TEXT NOT NULL, before TEXT NOT NULL, "
                     "after TEXT NOT NULL, undone REAL)"),
    # 0.8.5: what exists around Hermes (ecosystem.py), kept in the lead profile's database so the menu, the panel
    # and any sqlite reader see the same thing. Hermes' own plugin catalog (tiers official, verified, community)
    # and its kill list, the plugin's knowledge base of well-known plugins, and the playbook of situations and the
    # action for each. Rewritten when their source changes.
    ("catalog_plugin", "CREATE TABLE IF NOT EXISTS catalog_plugin (name TEXT PRIMARY KEY, tier TEXT NOT NULL, "
                       "category TEXT NOT NULL, repo TEXT NOT NULL, sha TEXT NOT NULL, version TEXT NOT NULL, "
                       "maintainer TEXT NOT NULL, description TEXT NOT NULL, hooks TEXT NOT NULL, tools TEXT NOT NULL, "
                       "known_issues TEXT NOT NULL) WITHOUT ROWID"),
    ("catalog_removed", "CREATE TABLE IF NOT EXISTS catalog_removed (name TEXT NOT NULL, repo TEXT NOT NULL, "
                        "reason TEXT NOT NULL, PRIMARY KEY (name, repo)) WITHOUT ROWID"),
    ("knowledge_plugin", "CREATE TABLE IF NOT EXISTS knowledge_plugin (name TEXT PRIMARY KEY, role TEXT NOT NULL, "
                         "exclusive INTEGER NOT NULL, summary_en TEXT NOT NULL, summary_pt TEXT NOT NULL, "
                         "transforms TEXT NOT NULL, notes TEXT NOT NULL) WITHOUT ROWID"),
    ("knowledge_obsolete", "CREATE TABLE IF NOT EXISTS knowledge_obsolete (name TEXT PRIMARY KEY, repo TEXT NOT NULL, "
                           "state TEXT NOT NULL, basis TEXT NOT NULL, superseded_by TEXT NOT NULL) WITHOUT ROWID"),
    ("playbook", "CREATE TABLE IF NOT EXISTS playbook (situation TEXT PRIMARY KEY, level TEXT NOT NULL, "
                 "risk TEXT NOT NULL, action TEXT NOT NULL, undo TEXT NOT NULL, title_en TEXT NOT NULL, "
                 "title_pt TEXT NOT NULL, advice_en TEXT NOT NULL, advice_pt TEXT NOT NULL) WITHOUT ROWID"),
    # Per-signature lookups (lessons, promotion, the listing) would scan these tables once per signature.
    ("failure_daily_signature", "CREATE INDEX IF NOT EXISTS failure_daily_signature ON failure_daily(signature)"),
    ("failure_owner_daily_signature",
     "CREATE INDEX IF NOT EXISTS failure_owner_daily_signature ON failure_owner_daily(signature)"),
)
SCHEMA += "".join(f"{statement};\n" for _, statement in ADDITIVE_TABLES)


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def utc_datetime(now: float) -> datetime:
    """A Unix time as an aware UTC datetime on every system. Windows' fromtimestamp refuses times before 1970."""
    return _EPOCH + timedelta(seconds=float(now))


def utc_day(now: float) -> str:
    return utc_datetime(now).date().isoformat()


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
    database = _operations.database

    return database(Path(home), observer=observer)


def record(home: Path, job, *, now: float | None = None) -> None:
    """Apply one counter update through the shared observer connection, queued while the database is busy.

    `job(connection)` may run later on another thread, so it must hold only values that are safe to store.
    """
    _operations.record(Path(home), job, now=now)


# Columns added after a table first shipped: (table, column, declaration).
ADDED_COLUMNS = (("lesson", "baseline_failures", "INTEGER"), ("lesson", "baseline_calls", "INTEGER"))


def ensure_columns(conn: sqlite3.Connection) -> None:
    """Bring tables created by an earlier development schema up to date. Safe to run repeatedly."""
    for table, column, declaration in ADDED_COLUMNS:
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if present and column not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


RETENTION_STEP_DAYS = 2      # how far past the newest day stored a prune may date its retention window
RETENTION_CONFIRM_DAYS = 3   # days stored past the clock the last prune used before a jump of the clock is believed
_DAILY_TABLES = ("tool_daily", "failure_daily", "turn_daily", "feedback_daily", "skill_daily", "review_daily",
                 "skill_outcome_daily", "guard_daily", "failure_owner_daily", "approval_daily")


def _day_start(day) -> float | None:
    try:
        return (datetime.fromisoformat(day).replace(tzinfo=timezone.utc) - _EPOCH).total_seconds()
    except (TypeError, ValueError):
        return None


def retention_clock(conn: sqlite3.Connection, *, now: float) -> float:
    """The time retention counts back from: `now`, but never more than RETENTION_STEP_DAYS past the newest day
    stored. A clock far ahead, such as a machine that booted before its time sync, would otherwise delete every
    aggregate at once.

    Days stored past the clock the previous prune used (kept in the `cursor` table) may have been written by that
    clock that is far ahead, so they count only once there are RETENTION_CONFIRM_DAYS of them: a clock that stayed
    there, as after a real pause, is believed, and retention catches up then."""
    saved = conn.execute("SELECT value FROM cursor WHERE key='retention_day'").fetchone()
    last = _day_start(saved[0]) if saved else None
    bound = "9999" if last is None else utc_day(last + RETENTION_STEP_DAYS * 86_400)
    beyond = conn.execute("SELECT COUNT(*) FROM (" + " UNION ".join(
        f"SELECT day FROM {table} WHERE day > ?" for table in _DAILY_TABLES) + ")", (bound,) * len(_DAILY_TABLES)).fetchone()[0]
    if beyond >= RETENTION_CONFIRM_DAYS:
        bound = "9999"
    newest = conn.execute("SELECT MAX(day) FROM (" + " UNION ALL ".join(
        f"SELECT MAX(day) AS day FROM {table} WHERE day <= ?" for table in _DAILY_TABLES) + ")",
                          (bound,) * len(_DAILY_TABLES)).fetchone()[0]
    start = _day_start(newest)
    clock = now if start is None else min(now, start + RETENTION_STEP_DAYS * 86_400)
    conn.execute("INSERT OR REPLACE INTO cursor VALUES ('retention_day', ?)", (utc_day(clock),))
    return clock


def prune(conn: sqlite3.Connection, *, now: float) -> float:
    """Drop aggregates older than the retention window. Lessons, retired ones included, and open proposals are
    kept. The window counts back from `retention_clock`, which it returns; failure excerpts expire by `now`."""
    excerpts_before = now - SAMPLE_DAYS * 86_400
    now = retention_clock(conn, now=now)
    cutoff = day_offset(now, RETENTION_DAYS)
    for table in _DAILY_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE day < ?", (cutoff,))
    old = now - RETENTION_DAYS * 86_400
    conn.execute("DELETE FROM failure WHERE last_seen < ? AND signature NOT IN "
                 "(SELECT signature FROM lesson WHERE signature IS NOT NULL AND status IN ('active','candidate'))", (old,))
    conn.execute("DELETE FROM change WHERE at < ?", (old,))
    conn.execute("DELETE FROM proposal WHERE status NOT IN ('open','approved') AND updated < ?", (old,))
    # Lessons are kept, retired ones included: a deleted row would free its id for another lesson, and a sentence
    # the owner turned down would come back as new once its record was gone.
    # Excerpts expire here too, so they do not outlive their window when no learning pass runs. They expire by the
    # clock itself: losing one early costs a draft, keeping one late breaks a promise.
    conn.execute("DELETE FROM failure_sample WHERE updated < ? OR signature NOT IN (SELECT signature FROM failure)",
                 (excerpts_before,))
    conn.execute("DELETE FROM reflector_backoff WHERE signature NOT IN (SELECT signature FROM failure)")
    conn.execute("DELETE FROM failure_owner WHERE signature NOT IN (SELECT signature FROM failure)")
    conn.execute("DELETE FROM failure_owner_daily WHERE signature NOT IN (SELECT signature FROM failure)")
    return now
