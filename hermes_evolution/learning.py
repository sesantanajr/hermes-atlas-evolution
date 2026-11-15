"""Local operational baselines and feedback. The caller owns authorization and transactions."""

from __future__ import annotations

import math
import re
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping

_DAY = 86_400
_SLOT = 6 * 3_600
_BASELINE_DAYS = 28
_RETENTION_DAYS = 90
_MIN_SAMPLES = 5
_CATEGORIES = {"info": 0, "warning": 1, "critical": 2}
_SENSITIVITY = {"high": (2.5, 0.18), "balanced": (3.5, 0.28), "low": (5.0, 0.45)}
_CODE = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_OUTCOMES = {"improved", "ineffective", "inconclusive"}

# Direction of concern, minimum absolute change, and sane upper limit.
_METRICS = {
    "prompt_tokens": ("high", 750.0, 10_000_000.0),
    "research_ms": ("high", 12_000.0, 86_400_000.0),
    "memory_available_pct": ("low", 5.0, 100.0),
    "memory_psi_pct": ("high", 2.0, 100.0),
    "io_psi_pct": ("high", 2.0, 100.0),
    "cpu_psi_pct": ("high", 3.0, 100.0),
    "tool_error_pct": ("high", 5.0, 100.0),
    "api_error_pct": ("high", 5.0, 100.0),
    "queue_depth": ("high", 3.0, 1_000_000.0),
}

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS learning_sample (
        metric TEXT NOT NULL, slot INTEGER NOT NULL, value REAL NOT NULL,
        PRIMARY KEY (metric, slot)
    )""",
    """CREATE TABLE IF NOT EXISTS learning_alert (
        code TEXT PRIMARY KEY, severity TEXT NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
        last_notice REAL, last_notice_severity TEXT, notices INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS learning_feedback (
        code TEXT PRIMARY KEY, rating TEXT NOT NULL, updated REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS learning_trial (
        action TEXT NOT NULL, trial_id INTEGER NOT NULL, outcome TEXT NOT NULL, observed REAL NOT NULL,
        PRIMARY KEY (action, trial_id)
    )""",
    "CREATE INDEX IF NOT EXISTS learning_trial_observed ON learning_trial(observed)",
)


def initialize(conn: sqlite3.Connection) -> None:
    """Prepare the extension's own tables without committing a caller transaction."""
    for statement in _SCHEMA:
        conn.execute(statement)


def _time(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < 253_402_300_800:
        raise ValueError("now must be a finite Unix timestamp")
    return float(value)


def _valid_code(code: object) -> bool:
    return isinstance(code, str) and _CODE.fullmatch(code) is not None


def _metric_values(metrics: Mapping) -> dict[str, float]:
    return {
        name: float(raw) for name, raw in metrics.items()
        if name in _METRICS and isinstance(raw, (int, float)) and not isinstance(raw, bool)
        and 0 <= raw <= _METRICS[name][2]
    }


def _weighted_median(samples: Iterable[tuple[float, float]]) -> float:
    ordered = sorted(samples)
    half = sum(weight for _, weight in ordered) / 2
    accumulated = 0.0
    for value, weight in ordered:
        accumulated += weight
        if accumulated >= half:
            return value
    return ordered[-1][0]


def _baselines(conn: sqlite3.Connection, values: dict, slot: int, sensitivity: str) -> tuple[dict, list]:
    divisor = _DAY // _SLOT
    grouped = defaultdict(list)
    rows = conn.execute("""SELECT metric,slot,value FROM learning_sample
        WHERE slot>=? AND slot<? AND slot%?=?""", (slot - _BASELINE_DAYS * divisor, slot, divisor, slot % divisor))
    for metric, old_slot, value in rows:
        if metric in values:
            grouped[metric].append((value, 2 ** (-(slot - old_slot) / (7 * divisor))))

    baselines = {}
    alerts = []
    mad_factor, relative_margin = _SENSITIVITY[sensitivity]
    for metric, value in values.items():
        direction, absolute_margin, maximum = _METRICS[metric]
        history = grouped[metric]
        baseline = {"value": value, "samples": len(history), "direction": direction}
        if len(history) >= _MIN_SAMPLES:
            median = _weighted_median(history)
            mad = _weighted_median((abs(sample - median), weight) for sample, weight in history)
            margin = max(absolute_margin, median * relative_margin, mad * mad_factor * 1.4826)
            threshold = max(0.0, median - margin) if direction == "low" else min(maximum, median + margin)
            deviation = (median - value) if direction == "low" else (value - median)
            baseline.update(median=round(median, 3), mad=round(mad, 3), threshold=round(threshold, 3),
                            anomalous=deviation >= margin)
            if baseline["anomalous"]:
                alerts.append({"code": f"baseline_{metric}", "severity": "warning", "source": "baseline", "metric": metric})
        baselines[metric] = baseline
    return baselines, alerts


def _findings(items: Iterable[Mapping]) -> Iterable[dict]:
    for item in items:
        if not isinstance(item, Mapping):
            continue
        code, severity = item.get("code", item.get("id")), item.get("severity")
        if _valid_code(code) and isinstance(severity, str) and severity in _CATEGORIES:
            yield {"code": code, "severity": severity, "source": "finding"}


def _alert_status(conn: sqlite3.Connection, alerts: list[dict], *, now: float, record: bool) -> tuple[list, list]:
    by_code = {}
    for alert in alerts:
        previous = by_code.get(alert["code"])
        if previous is None or _CATEGORIES[alert["severity"]] > _CATEGORIES[previous["severity"]]:
            by_code[alert["code"]] = alert
    delivered, suppressed = [], []
    for code, alert in sorted(by_code.items(), key=lambda item: (-_CATEGORIES[item[1]["severity"]], item[0])):
        severity = alert["severity"]
        previous = conn.execute("SELECT last_notice,last_notice_severity FROM learning_alert WHERE code=?", (code,)).fetchone()
        feedback = conn.execute("SELECT rating,updated FROM learning_feedback WHERE code=?", (code,)).fetchone()
        rating = feedback[0] if feedback is not None and 0 <= now - feedback[1] < 14 * _DAY else None
        interval = 3_600 if severity == "critical" else (_SLOT if rating == "helpful" else 2 * _SLOT)
        reason = None
        if rating == "dismissed" and severity != "critical":
            reason = "recently_dismissed"
        elif previous and previous[0] is not None and now < previous[0] + interval:
            if _CATEGORIES[severity] <= _CATEGORIES.get(previous[1], -1):
                reason = "recently_reported"
        if reason:
            suppressed.append({**alert, "reason": reason})
        else:
            delivered.append(alert)
        if record:
            conn.execute("""INSERT INTO learning_alert VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET severity=excluded.severity,
                    last_seen=MAX(learning_alert.last_seen,excluded.last_seen),
                    last_notice=COALESCE(excluded.last_notice,learning_alert.last_notice),
                    last_notice_severity=COALESCE(excluded.last_notice_severity,learning_alert.last_notice_severity),
                    notices=learning_alert.notices+excluded.notices""",
                         (code, severity, now, now, None if reason else now, None if reason else severity, int(reason is None)))
    return delivered, suppressed


def review(conn: sqlite3.Connection, *, metrics: Mapping, findings: Iterable[Mapping] = (), now: float,
           sensitivity: str = "balanced", record: bool = False) -> dict:
    """Compare a numeric sample to the same UTC time slots, then prioritize notices."""
    observed = _time(now)
    if not isinstance(sensitivity, str) or sensitivity not in _SENSITIVITY:
        raise ValueError("sensitivity must be high, balanced, or low")
    slot = int(observed // _SLOT)
    initialize(conn)
    if record:
        # Begin the write before reading alert state, so two callers cannot deliver the same notice.
        conn.execute("DELETE FROM learning_alert WHERE last_seen<?", (observed - _RETENTION_DAYS * _DAY,))
        conn.execute("DELETE FROM learning_feedback WHERE updated<?", (observed - _RETENTION_DAYS * _DAY,))
        conn.execute("DELETE FROM learning_trial WHERE observed<?", (observed - _RETENTION_DAYS * _DAY,))
        conn.execute("DELETE FROM learning_sample WHERE slot<?", (slot - _RETENTION_DAYS * (_DAY // _SLOT),))

    values = _metric_values(metrics)
    baselines, candidates = _baselines(conn, values, slot, sensitivity)
    alerts, suppressed = _alert_status(conn, [*candidates, *_findings(findings)], now=observed, record=record)
    if record:
        conn.executemany("INSERT OR IGNORE INTO learning_sample VALUES (?,?,?)",
                         ((metric, slot, value) for metric, value in values.items()))
    return {"sensitivity": sensitivity, "baselines": baselines, "alerts": alerts, "suppressed": suppressed,
            "action_cooldowns": action_cooldowns(conn, now=observed)}


def record_feedback(conn: sqlite3.Connection, code: str, rating: str, *, now: float) -> dict:
    """Record a person's judgment of an observed notice, using only fixed ratings."""
    observed = _time(now)
    if not _valid_code(code) or not isinstance(rating, str) or rating not in {"helpful", "dismissed", "reset"}:
        raise ValueError("provide an observed alert code and a fixed feedback rating")
    initialize(conn)
    if conn.execute("SELECT 1 FROM learning_alert WHERE code=?", (code,)).fetchone() is None:
        raise ValueError(f"unknown alert code: {code}")
    if rating == "reset":
        conn.execute("DELETE FROM learning_feedback WHERE code=? AND updated<=?", (code, observed))
    else:
        conn.execute("""INSERT INTO learning_feedback VALUES (?,?,?) ON CONFLICT(code) DO UPDATE SET
            rating=excluded.rating,updated=excluded.updated WHERE excluded.updated>=learning_feedback.updated""",
                     (code, rating, observed))
    current = conn.execute("SELECT rating FROM learning_feedback WHERE code=?", (code,)).fetchone()
    return {"code": code, "feedback": current[0] if current else None}


def record_outcome(conn: sqlite3.Connection, action: str, trial_id: int, outcome: str, *, now: float) -> bool:
    """Store one terminal observation per local trial; ignore replayed observations."""
    observed = _time(now)
    if (not _valid_code(action) or isinstance(trial_id, bool) or not isinstance(trial_id, int)
            or not 0 < trial_id < 2 ** 63 or not isinstance(outcome, str) or outcome not in _OUTCOMES):
        raise ValueError("provide an action code, numeric trial id, and fixed terminal outcome")
    initialize(conn)
    result = conn.execute("INSERT OR IGNORE INTO learning_trial VALUES (?,?,?,?)", (action, trial_id, outcome, observed))
    return result.rowcount == 1


def action_cooldowns(conn: sqlite3.Connection, *, now: float) -> dict:
    """Suggest delaying action kinds with repeated measured failures on distinct intervals."""
    observed = _time(now)
    initialize(conn)
    failures = defaultdict(dict)
    assessed = set()
    rows = conn.execute("""SELECT action,outcome,observed FROM learning_trial
        WHERE observed>? AND observed<=? ORDER BY observed DESC,trial_id DESC""", (observed - 14 * _DAY, observed))
    for action, outcome, when in rows:
        if action in assessed:
            continue
        if outcome == "improved":
            assessed.add(action)
        elif outcome == "ineffective":
            failures[action].setdefault(int(when // _SLOT), when)

    result = {}
    for action, slots in failures.items():
        if len(slots) < 2:
            continue
        hours = min(96, 24 * (2 ** min(2, len(slots) - 2)))
        until = max(slots.values()) + hours * 3_600
        if until > observed:
            result[action] = {"until": until, "ineffective_intervals": len(slots), "reason": "repeated_ineffective_trials"}
    return result


def retrospective(conn: sqlite3.Connection, *, now: float, days: int = 14) -> dict:
    """Return compact measurements, incident judgments and outcomes without session content."""
    observed = _time(now)
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= _RETENTION_DAYS:
        raise ValueError(f"days must be in 1..{_RETENTION_DAYS}")
    initialize(conn)
    start = observed - days * _DAY
    samples = conn.execute("""SELECT metric,COUNT(*),MIN(value),MAX(value),AVG(value) FROM learning_sample
        WHERE slot BETWEEN ? AND ? GROUP BY metric ORDER BY metric""", (int(start // _SLOT), int(observed // _SLOT)))
    metrics = {name: {"intervals": count, "minimum": low, "maximum": high, "mean": round(mean, 3)}
               for name, count, low, high, mean in samples}
    flags = conn.execute("""SELECT a.code,a.severity,a.last_seen,a.notices,f.rating FROM learning_alert a
        LEFT JOIN learning_feedback f ON a.code=f.code WHERE a.last_seen BETWEEN ? AND ?
        ORDER BY a.last_seen DESC,a.code LIMIT 100""", (start, observed))
    alerts = [{"code": code, "severity": severity, "last_seen": seen, "notices_total": count, "feedback": rating}
              for code, severity, seen, count, rating in flags]
    trials = defaultdict(dict)
    outcomes = conn.execute("""SELECT action,outcome,COUNT(*) FROM learning_trial
        WHERE observed BETWEEN ? AND ? GROUP BY action,outcome ORDER BY action,outcome""", (start, observed))
    for action, outcome, count in outcomes:
        trials[action][outcome] = count
    return {"window_days": days, "sample_interval_hours": _SLOT // 3_600, "metrics": metrics, "alerts": alerts,
            "action_outcomes": dict(trials), "action_cooldowns": action_cooldowns(conn, now=observed)}
