"""Before-and-after measurement for every tracked change.

Hermes rewrites skills, operators change configuration and lessons get activated, but nothing checks
whether the profile behaved better afterwards. This module records each change and compares the same
metrics in the week before and the week after. The comparison is correlational: concurrent changes are
counted and reported next to every verdict.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from .experience import day_offset, label, utc_day
from .skill_health import compare_skill_windows, read_ledger, skill_window

WINDOW_DAYS = 7
MIN_AFTER_DAYS = 3
MIN_TURNS = 20
MIN_TOOL_CALLS = 40
MIN_API_CALLS = 30
_INTERACTIVE = ("local", "gateway")
CONFIG_KEYS = (
    ("compression", "enabled"), ("compression", "threshold_tokens"), ("compression", "proactive_prune_tokens"),
    ("model", "default"), ("model", "model"), ("model", "provider"), ("memory", "provider"), ("memory", "nudge_interval"),
    ("skills", "creation_nudge_interval"), ("auxiliary", "background_review", "enabled"),
    ("auxiliary", "background_review", "model"), ("auxiliary", "compression", "model"),
    ("tool_budget", "mcp_result_size_chars"), ("curator", "enabled"), ("curator", "consolidate"),
    ("compression", "threshold"), ("compression", "target_ratio"), ("compression", "protect_last_n"),
    ("skills", "auto_load"),
)
# metric -> (absolute change that matters, lower is better)
_METRICS = {
    "turn_failed_pct": (2.0, True), "turn_slow_pct": (5.0, True), "turn_mean_ms": (3_000.0, True),
    "tool_error_pct": (2.0, True), "owner_corrections_per_100_turns": (2.0, True),
    "uncached_input_tokens_per_call": (300.0, True),
}
WORSE_RATIO, BETTER_RATIO = 0.25, 0.20


def config_snapshot(runtime_config) -> dict[str, str]:
    """Allowlisted scalar settings as short labels. Values that are not plain labels become 'set'."""
    result = {}
    config = runtime_config if isinstance(runtime_config, dict) else {}
    for path in CONFIG_KEYS:
        value = config
        for part in path:
            value = value.get(part) if isinstance(value, dict) else None
        if value is None:
            text = "unset"
        elif isinstance(value, bool):
            text = "true" if value else "false"
        elif type(value) in (int, float) and -10**12 < value < 10**12:
            text = str(value)
        elif isinstance(value, str) and label(value, fallback="") == value:
            text = value
        else:
            # Not a plain label (provider/model, a URL, a mapping): keep a short fingerprint, never the value,
            # so two different settings still read as different.
            text = "set:" + hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:10]
        result[".".join(path)] = text
    plugins = config.get("plugins") if isinstance(config.get("plugins"), dict) else {}
    enabled = sorted(item for item in plugins.get("enabled") or () if isinstance(item, str))
    result["plugins.enabled"] = f"{len(enabled)}:{hashlib.sha256(json.dumps(enabled).encode()).hexdigest()[:8]}"
    return result


def detect_changes(conn: sqlite3.Connection, home: Path, runtime_config, *, now: float) -> list[dict]:
    """Record configuration and skill changes seen since the last run. A configuration that could not be read
    (evolve.CONFIG_UNREAD) is unknown, not empty: nothing is compared, and the last snapshot stays."""
    from .evolve import CONFIG_UNREAD

    found = []
    if isinstance(runtime_config, dict) and runtime_config and not runtime_config.get(CONFIG_UNREAD):
        snapshot = config_snapshot(runtime_config)
        saved = conn.execute("SELECT value FROM cursor WHERE key='config_snapshot'").fetchone()
        try:
            previous = json.loads(saved[0]) if saved else None
        except ValueError:
            previous = None
        if isinstance(previous, dict):
            for key, value in snapshot.items():
                if key in previous and previous[key] != value:
                    detail = json.dumps({"from": previous[key], "to": value})
                    if conn.execute("INSERT OR IGNORE INTO change(ref, kind, subject, detail, at) VALUES (?, 'config', ?, ?, ?)",
                                    (f"config:{key}:{utc_day(now)}", key, detail, now)).rowcount:
                        found.append({"kind": "config", "subject": key, "from": previous[key], "to": value})
        conn.execute("INSERT OR REPLACE INTO cursor VALUES ('config_snapshot', ?)", (json.dumps(snapshot),))
        conn.commit()   # the Curator ledger (up to 8 MiB) is read with no write lock held: hook writers never wait on it
    saved = conn.execute("SELECT value FROM cursor WHERE key='ledger_at'").fetchone()
    try:
        since = float(saved[0]) if saved else now - WINDOW_DAYS * 86_400
    except ValueError:
        since = now - WINDOW_DAYS * 86_400
    grouped: dict[tuple[str, str], list[dict]] = {}
    latest = since
    for entry in read_ledger(home, since=since):
        if entry["at"] <= since:
            continue
        grouped.setdefault((entry["skill"], utc_day(entry["at"])), []).append(entry)
        latest = max(latest, entry["at"])
    for (skill, day), entries in grouped.items():
        ref = f"skill:{skill}:{day}"
        old = conn.execute("SELECT detail, evaluated FROM change WHERE ref=?", (ref,)).fetchone()
        count = len(entries) + (json.loads(old["detail"]).get("rewrites", 0) if old else 0)
        reviews = sum(e["actor"] == "curator" for e in entries) + (json.loads(old["detail"]).get("by_review", 0) if old else 0)
        detail = json.dumps({"rewrites": count, "by_review": reviews, "last_entry": entries[-1]["id"]})
        if old is None:
            conn.execute("INSERT INTO change(ref, kind, subject, detail, at) VALUES (?, 'skill', ?, ?, ?)",
                         (ref, skill, detail, entries[-1]["at"]))
            found.append({"kind": "skill", "subject": skill, "rewrites": count})
        elif old["evaluated"] is None:
            conn.execute("UPDATE change SET detail=?, at=? WHERE ref=?", (detail, entries[-1]["at"], ref))
    conn.execute("INSERT OR REPLACE INTO cursor VALUES ('ledger_at', ?)", (repr(latest),))
    return found


def window_metrics(conn: sqlite3.Connection, ops: sqlite3.Connection | None, start_day: str, end_day: str) -> dict:
    """Outcome metrics between two UTC days (inclusive), with the sample size behind each."""
    marks = ",".join("?" * len(_INTERACTIVE))
    turn = conn.execute(f"""SELECT COALESCE(SUM(turns),0), COALESCE(SUM(failed),0), COALESCE(SUM(slow),0),
          COALESCE(SUM(duration_ms),0) FROM turn_daily WHERE day BETWEEN ? AND ? AND surface IN ({marks})""",
                        (start_day, end_day, *_INTERACTIVE)).fetchone()
    tool = conn.execute("SELECT COALESCE(SUM(calls),0), COALESCE(SUM(errors),0) FROM tool_daily WHERE day BETWEEN ? AND ?",
                        (start_day, end_day)).fetchone()
    corrections = conn.execute("""SELECT COALESCE(SUM(count),0) FROM feedback_daily
        WHERE day BETWEEN ? AND ? AND kind IN ('correction','reask')""", (start_day, end_day)).fetchone()[0]
    result = {"turns": turn[0], "tool_calls": tool[0], "api_calls": 0}
    if turn[0]:
        result.update(turn_failed_pct=round(100 * turn[1] / turn[0], 2), turn_slow_pct=round(100 * turn[2] / turn[0], 2),
                      turn_mean_ms=round(turn[3] / turn[0], 1),
                      owner_corrections_per_100_turns=round(100 * corrections / turn[0], 2))
    if tool[0]:
        result["tool_error_pct"] = round(100 * tool[1] / tool[0], 2)
    if ops is not None:
        try:
            api = ops.execute("""SELECT COALESCE(SUM(usage_samples),0), COALESCE(SUM(input_tokens),0) FROM metric
                WHERE day BETWEEN ? AND ? AND lane='main' AND outcome='ok'""", (start_day, end_day)).fetchone()
        except sqlite3.Error:
            api = None
        if api and api[0]:
            result["api_calls"] = api[0]
            result["uncached_input_tokens_per_call"] = round(api[1] / api[0], 1)
    return result


def compare(before: dict, after: dict) -> dict:
    """Classify the difference between two metric windows."""
    volume = {"turn_failed_pct": ("turns", MIN_TURNS), "turn_slow_pct": ("turns", MIN_TURNS),
              "turn_mean_ms": ("turns", MIN_TURNS), "owner_corrections_per_100_turns": ("turns", MIN_TURNS),
              "tool_error_pct": ("tool_calls", MIN_TOOL_CALLS), "uncached_input_tokens_per_call": ("api_calls", MIN_API_CALLS)}
    better, worse, deltas = [], [], {}
    for name, (absolute, _lower_is_better) in _METRICS.items():
        counter, minimum = volume[name]
        if name not in before or name not in after or before[counter] < minimum or after[counter] < minimum:
            continue
        old, new = before[name], after[name]
        delta = new - old
        ratio = delta / max(abs(old), absolute)
        deltas[name] = {"before": old, "after": new}
        if delta >= absolute and ratio >= WORSE_RATIO:
            worse.append(name)
        elif -delta >= absolute and -ratio >= BETTER_RATIO:
            better.append(name)
    if not deltas:
        verdict = "insufficient_data"
    elif worse and better:
        verdict = "mixed"
    elif worse:
        verdict = "regressed"
    elif better:
        verdict = "improved"
    else:
        verdict = "neutral"
    return {"verdict": verdict, "better": better, "worse": worse, "metrics": deltas}


def evaluate_changes(conn: sqlite3.Connection, ops: sqlite3.Connection | None, *, now: float) -> list[dict]:
    """Compare the week before and after each pending change. A verdict becomes final after a full window."""
    today = utc_day(now)
    results = []
    rows = conn.execute("""SELECT * FROM change WHERE evaluated IS NULL AND kind != 'lesson' AND at <= ?
        ORDER BY at""", (now - MIN_AFTER_DAYS * 86_400,)).fetchall()
    for row in rows:
        before_start, before_end = day_offset(row["at"], WINDOW_DAYS), day_offset(row["at"], 1)
        after_start, after_end = day_offset(row["at"], -1), min(day_offset(row["at"], -WINDOW_DAYS), today)
        before = window_metrics(conn, ops, before_start, before_end)
        after = window_metrics(conn, ops, after_start, after_end)
        if row["kind"] == "skill":
            # A skill rewrite is judged by the turns that loaded that skill, not by the whole profile.
            skill_before = skill_window(conn, row["subject"], before_start, before_end)
            skill_after = skill_window(conn, row["subject"], after_start, after_end)
            outcome = {**compare_skill_windows(skill_before, skill_after), "skill_turns_before": skill_before["turns"],
                       "skill_turns_after": skill_after["turns"]}
        else:
            outcome = compare(before, after)
        concurrent = conn.execute("SELECT COUNT(*) FROM change WHERE id != ? AND at BETWEEN ? AND ?",
                                  (row["id"], row["at"] - WINDOW_DAYS * 86_400, row["at"] + WINDOW_DAYS * 86_400)).fetchone()[0]
        final = now - row["at"] >= (WINDOW_DAYS + 1) * 86_400
        metrics = {**outcome, "samples_before": {k: before[k] for k in ("turns", "tool_calls", "api_calls")},
                   "samples_after": {k: after[k] for k in ("turns", "tool_calls", "api_calls")},
                   "concurrent_changes": concurrent,
                   "method": ("turns_that_loaded_the_skill_week_before_vs_week_after" if row["kind"] == "skill"
                              else "same_metrics_week_before_vs_week_after_correlational")}
        conn.execute("UPDATE change SET verdict=?, metrics=?, evaluated=? WHERE id=?",
                     (outcome["verdict"], json.dumps(metrics), now if final else None, row["id"]))
        results.append({"id": row["id"], "kind": row["kind"], "subject": row["subject"], "final": final, **metrics})
    return results


def listing(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict]:
    rows = conn.execute("SELECT * FROM change ORDER BY at DESC, id DESC LIMIT ?", (max(1, min(limit, 200)),)).fetchall()
    result = []
    for row in rows:
        try:
            detail = json.loads(row["detail"])
            metrics = json.loads(row["metrics"]) if row["metrics"] else None
        except ValueError:
            detail, metrics = {}, None
        result.append({"id": row["id"], "kind": row["kind"], "subject": row["subject"], "at": row["at"], "detail": detail,
                       "verdict": row["verdict"], "final": row["evaluated"] is not None, "measurement": metrics})
    return result
