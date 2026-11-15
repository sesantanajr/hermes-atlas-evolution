"""Read Hermes' cumulative model usage without opening transcripts or pricing APIs."""

from collections import Counter, defaultdict
from contextlib import closing
import math
from pathlib import Path
import re
import sqlite3
import time

from .diagnostics import _checkpoint_unchanged, _regular_stat


_TIMEOUT = 0.4
_ROW_LIMIT = 20_000
_GROUP_LIMIT = 20
_ROUTE_FIELDS = ("task", "provider", "model")
_COUNTERS = (
    "api_call_count", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
)
_COST_FIELDS = ("estimated_cost_usd", "actual_cost_usd")
_COST_STATUSES = frozenset(("actual", "estimated", "included", "unknown"))
_LABEL = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:+/-]{0,127}\Z")
_SENSITIVE_LABEL = re.compile(r"(?:^|/)(?:sk-[a-zA-Z0-9_-]{8,}|gh[pousr]_[a-zA-Z0-9_]{8,}|xox[baprs]-[a-zA-Z0-9-]{8,})")
_USAGE_SQL = """
    SELECT substr(task, 1, 129), substr(billing_provider, 1, 129), substr(model, 1, 129),
           api_call_count, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens,
           estimated_cost_usd, actual_cost_usd, substr(cost_status, 1, 16), first_seen, last_seen
    FROM session_model_usage
    WHERE last_seen BETWEEN ? AND ? OR typeof(last_seen) NOT IN ('integer', 'real') OR last_seen < 0
    LIMIT ?
"""


def _route_label(value, fallback: str) -> str:
    if not isinstance(value, str) or not _LABEL.fullmatch(value):
        return fallback
    if any(part in ("", ".", "..") for part in value.split("/")) or _SENSITIVE_LABEL.search(value):
        return fallback
    return value


def _totals() -> dict:
    return {**dict.fromkeys(("rows", "rows_without_cost_evidence", *_COUNTERS), 0), **dict.fromkeys(_COST_FIELDS)}


def _timestamp(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _known_cost(value, latest_status: str | None = None) -> float | None:
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    if value != 0 or latest_status in ("estimated", "included"):
        return value
    return None


def _accumulate(rollup: dict, counts: tuple, costs: tuple) -> None:
    rollup["rows"] += 1
    both_unknown = all(cost is None for cost in costs)
    rollup["rows_without_cost_evidence"] += int(both_unknown)
    for name, count in zip(_COUNTERS, counts):
        rollup[name] += count
    if not both_unknown:
        for name, cost in zip(_COST_FIELDS, costs):
            if cost is not None:
                rollup[name] = (rollup[name] or 0.0) + cost


def _with_cache(rollup: dict) -> dict:
    prompt = rollup["input_tokens"] + rollup["cache_read_tokens"] + rollup["cache_write_tokens"]
    return {
        **rollup, "prompt_tokens": prompt,
        "cache_read_share": round(rollup["cache_read_tokens"] / prompt, 4) if prompt else None,
    }


def _group_rows(groups: dict, fields: tuple) -> list[dict]:
    leading = sorted(
        groups.items(),
        key=lambda item: (
            -(item[1]["actual_cost_usd"] or item[1]["estimated_cost_usd"] or 0),
            -(item[1]["input_tokens"] + item[1]["output_tokens"] + item[1]["cache_read_tokens"] + item[1]["cache_write_tokens"]),
            item[0],
        ),
    )[:_GROUP_LIMIT]
    return [{**dict(zip(fields, key)), **_with_cache(rollup)} for key, rollup in leading]


def _summarize(rows, *, start: float, deadline: float) -> dict:
    totals = _totals()
    inside = _totals()
    fields = (("task",), ("provider",), ("model",), _ROUTE_FIELDS)
    group_names = ("by_task", "by_provider", "by_model", "by_route")
    buckets = [defaultdict(_totals) for _ in fields]
    timing = Counter()
    statuses = Counter()

    for index, row in enumerate(rows):
        if index >= _ROW_LIMIT:
            return {"status": "row_limit"}
        if time.monotonic() >= deadline:
            return {"status": "query_timed_out"}
        first_seen, last_seen = row[-2:]
        if not _timestamp(last_seen):
            timing["rows_without_last_seen_excluded"] += 1
            continue
        task = "main" if row[0] == "" else _route_label(row[0], "unknown")
        provider = _route_label(row[1], "unknown")
        model = _route_label(row[2], "unknown")
        counts = tuple(value if type(value) is int and value > 0 else 0 for value in row[3:9])
        latest_status = row[11] if row[11] in _COST_STATUSES else "unknown"
        costs = (_known_cost(row[9], latest_status), _known_cost(row[10]))
        statuses[latest_status] += 1
        _accumulate(totals, counts, costs)
        if not _timestamp(first_seen) or first_seen > last_seen:
            timing["rows_without_valid_first_seen"] += 1
        elif first_seen < start:
            timing["rows_crossing_window_start"] += 1
        else:
            _accumulate(inside, counts, costs)
        keys = ((task,), (provider,), (model,), (task, provider, model))
        for group, key in zip(buckets, keys):
            _accumulate(group[key], counts, costs)

    return {
        "status": "ok" if totals["rows"] else "empty",
        "totals": _with_cache(totals),
        "fully_in_window_totals": _with_cache(inside),
        **{name: _group_rows(group, keys) for name, group, keys in zip(group_names, buckets, fields)},
        "coverage": {
            "rows_fully_in_window": inside["rows"],
            "rows_crossing_window_start": timing["rows_crossing_window_start"],
            "rows_without_valid_first_seen": timing["rows_without_valid_first_seen"],
            "rows_without_last_seen_excluded": timing["rows_without_last_seen_excluded"],
            "latest_cost_status_rows": dict(statuses),
            "groups_omitted": {name: max(len(group) - _GROUP_LIMIT, 0) for name, group in zip(group_names, buckets)},
        },
    }


def _read_usage(db: sqlite3.Connection, *, start: float, now: float, deadline: float) -> dict:
    db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    db.execute("PRAGMA query_only=ON")
    db.execute("PRAGMA trusted_schema=OFF")
    db.execute("BEGIN")
    table = db.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'session_model_usage'"
    ).fetchone()
    if table is None:
        return {"status": "unexpected_schema"}
    columns = {row[1] for row in db.execute("PRAGMA table_info(session_model_usage)")}
    required = {*_COUNTERS, *_COST_FIELDS, "task", "billing_provider", "model", "cost_status", "first_seen", "last_seen"}
    if not required.issubset(columns):
        return {"status": "unexpected_schema"}
    rows = db.execute(_USAGE_SQL, (start, now, _ROW_LIMIT + 1))
    return _summarize(rows, start=start, deadline=deadline)


def native_billing(home: Path, *, days: int, now: float) -> dict:
    """Report whole usage rows whose last recorded call falls in the window."""
    if type(days) is not int or not 1 <= days <= 3650 or not _timestamp(now):
        raise ValueError("Provide 1 to 3650 days and a valid Unix timestamp.")
    start = max(now - days * 86400, 0)
    result = {
        "source": "hermes_session_model_usage",
        "window": {"days": days, "from_epoch": start, "through_epoch": now, "basis": "cumulative_rows_last_seen_in_window"},
    }
    path = home / "state.db"
    info = _regular_stat(path)
    if info is None:
        return {**result, "status": "missing_or_unreadable"}
    checkpoint_only = False
    try:
        path = path.resolve(strict=True)
        with path.open("rb") as stream:
            header = stream.read(20)
        if header[:16] != b"SQLite format 3\x00":
            return {**result, "status": "unreadable"}
        sidecars = [path.with_name(f"{path.name}-{suffix}") for suffix in ("wal", "shm")]
        if header[18:20] == b"\x02\x02":
            present = [sidecar.exists() or sidecar.is_symlink() for sidecar in sidecars]
            if any(present) and (not all(present) or any(_regular_stat(sidecar) is None for sidecar in sidecars)):
                return {**result, "status": "wal_sidecars_unavailable"}
            # A closed WAL profile can be read from its checkpoint without creating sidecars.
            checkpoint_only = not any(present)
        query = "?mode=ro&immutable=1" if checkpoint_only else "?mode=ro&readonly_shm=1"
        result["mode"] = "checkpoint_only" if checkpoint_only else "live"
        deadline = time.monotonic() + _TIMEOUT
        with closing(sqlite3.connect(path.as_uri() + query, uri=True, timeout=_TIMEOUT)) as db:
            summary = _read_usage(db, start=start, now=now, deadline=deadline)
        if checkpoint_only and not _checkpoint_unchanged(path, info, sidecars):
            return {**result, "status": "snapshot_changed"}
        return {**result, **summary}
    except sqlite3.Error as exc:
        if checkpoint_only and not _checkpoint_unchanged(path, info, sidecars):
            return {**result, "status": "snapshot_changed"}
        code = getattr(exc, "sqlite_errorcode", 0) or 0
        errors = {sqlite3.SQLITE_INTERRUPT: "query_timed_out", sqlite3.SQLITE_BUSY: "busy", sqlite3.SQLITE_LOCKED: "busy"}
        return {**result, "status": errors.get(code & 255, "unreadable")}
    except (OSError, ValueError, OverflowError):
        return {**result, "status": "unreadable"}
