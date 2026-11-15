"""Persist read/measure/act/verify outcomes for bounded Linux actions."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .linux_actions import KINDS, SystemPolicy, Systemd, attribute, pair_sample
from .operations import _day

LEASE_SECONDS = 900
COOLDOWN_SECONDS = 3600
TRIAL_WEIGHT = 50
_STATES = "('prepared','active','improved')"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS pair_sample (id INTEGER PRIMARY KEY CHECK (id=1), policy TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS weight_trial (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL, unit TEXT NOT NULL, hermes TEXT NOT NULL, kind TEXT NOT NULL,
    boot TEXT NOT NULL, unit_invocation TEXT NOT NULL, hermes_invocation TEXT NOT NULL,
    previous_weight INTEGER NOT NULL, previous_effective INTEGER NOT NULL,
    applied_weight INTEGER NOT NULL, baseline_psi REAL NOT NULL, started REAL NOT NULL,
    status TEXT NOT NULL, updated REAL NOT NULL, reason TEXT, observed_psi REAL
);
CREATE TABLE IF NOT EXISTS incident (
    code TEXT PRIMARY KEY, key_text TEXT NOT NULL, occurrences INTEGER NOT NULL, last_day TEXT NOT NULL,
    last_seen REAL NOT NULL, severity TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS baseline (day TEXT PRIMARY KEY, average_prompt_tokens REAL, research_p95_ms INTEGER);
"""


@contextmanager
def _journal(home: Path):
    folder = home / "evolution"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder / "supervisor.sqlite3"
    try:
        path.touch(mode=0o600, exist_ok=False)
    except FileExistsError:
        pass
    connection = sqlite3.connect(path, timeout=2)
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(_SCHEMA)
        with connection:
            yield connection
    finally:
        connection.close()


def _active(conn):
    return conn.execute(f"SELECT * FROM weight_trial WHERE status IN {_STATES} ORDER BY id DESC LIMIT 1").fetchone()


def _transition(conn, record, status, reason, now, pressure=None):
    conn.execute("UPDATE weight_trial SET status=?,reason=?,updated=?,observed_psi=? WHERE id=?",
                 (status, reason, now, pressure, record["id"]))
    conn.commit()
    return {"id": record["id"], "resource": record["kind"], "status": status, "reason": reason,
            "observed_pressure": pressure}


def _undo(conn, backend, record, unit, now, reason, pressure=None):
    kind = record["kind"]
    if unit.weight[kind] != record["applied_weight"]:
        return _transition(conn, record, "released", "runtime_weight_changed_externally", now, pressure)
    backend.set_weight(record["scope"], record["unit"], kind, record["previous_weight"])
    verify = backend.unit(record["scope"], record["unit"])
    if (verify and verify.weight[kind] == record["previous_weight"]
            and (not verify.active or verify.effective[kind] == record["previous_effective"])):
        return _transition(conn, record, "rolled_back", reason, now, pressure)
    return _transition(conn, record, record["status"], "rollback_pending_" + reason, now, pressure)


def _check_trial(conn, backend, record, policy, safe, now, *, manual=False):
    kind = record["kind"]
    boot = backend.boot
    if not boot:
        return {"id": record["id"], "status": "pending", "reason": "unverified_boot"}
    if boot != record["boot"]:
        return _transition(conn, record, "released", "host_rebooted", now)
    unit = backend.unit(record["scope"], record["unit"])
    if not unit or unit.weight[kind] is None:
        return {"id": record["id"], "status": "pending", "reason": "background_unit_unverified"}
    if unit.weight[kind] != record["applied_weight"]:
        status = "not_applied" if record["status"] == "prepared" else "released"
        return _transition(conn, record, status, "target_weight_absent", now)
    hermes = backend.unit(record["scope"], record["hermes"])
    pressure = hermes.psi[kind] if hermes and hermes.active else None
    enabled = (policy and policy.mode == "apply" and policy.scope == record["scope"]
               and policy.background_unit == record["unit"] and policy.hermes_unit == record["hermes"]
               and kind in policy.resources)
    elapsed = now - record["started"]
    reason = None
    if manual or not enabled:
        reason = "operator_request" if manual else "policy_changed"
    elif not safe or not hermes or not hermes.active or not unit.active or unit.effective[kind] != record["applied_weight"]:
        reason = "health_or_effect_unverified"
    elif hermes.invocation != record["hermes_invocation"] or unit.invocation != record["unit_invocation"]:
        reason = "service_replaced"
    elif elapsed < 0 or elapsed >= LEASE_SECONDS:
        reason = "lease_expired"
    elif elapsed >= 60 and (pressure is None or pressure > 0.8 * record["baseline_psi"]):
        reason = "insufficient_improvement"
    if reason:
        return _undo(conn, backend, record, unit, now, reason, pressure)
    status = "improved" if elapsed >= 60 else "active"
    return _transition(conn, record, status, "pressure_lower" if elapsed >= 60 else "weight_verified", now, pressure)


def _begin(conn, backend, policy, candidate, pair, now):
    kind = candidate["kind"]
    hermes, other = pair
    current = backend.unit(policy.scope, policy.background_unit)
    if (not current or not current.active or current.invocation != other.invocation or current.group != other.group
            or current.weight[kind] not in (0, 100) or current.effective[kind] != 100):
        return {"status": "skipped", "reason": "background_weight_or_identity_not_standard"}
    gateway = backend.unit(policy.scope, policy.hermes_unit)
    if (not gateway or not gateway.active or gateway.invocation != hermes.invocation or gateway.group != hermes.group
            or gateway.slice != current.slice):
        return {"status": "skipped", "reason": "hermes_identity_changed"}
    boot = backend.boot
    if not boot:
        return {"status": "skipped", "reason": "unverified_boot"}
    ident = conn.execute("""INSERT INTO weight_trial
        (scope,unit,hermes,kind,boot,unit_invocation,hermes_invocation,previous_weight,previous_effective,
         applied_weight,baseline_psi,started,status,updated)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'prepared',?)""",
                         (policy.scope, current.name, policy.hermes_unit, kind, boot, current.invocation,
                          gateway.invocation, current.weight[kind],
                          current.effective[kind], TRIAL_WEIGHT, candidate["pressure_avg10"], now, now)).lastrowid
    conn.commit()
    backend.set_weight(policy.scope, current.name, kind, TRIAL_WEIGHT)
    verified = backend.unit(policy.scope, current.name)
    if not verified or verified.weight[kind] != TRIAL_WEIGHT:
        return {"id": ident, "resource": kind, "status": "pending", "reason": "weight_apply_unverified"}
    record = conn.execute("SELECT * FROM weight_trial WHERE id=?", (ident,)).fetchone()
    if not verified.active or verified.effective[kind] != TRIAL_WEIGHT:
        return _undo(conn, backend, record, verified, now, "effective_weight_unverified")
    return _transition(conn, record, "active", "weight_verified", now)


def _sample(conn, backend, policy, now):
    pair = [backend.unit(policy.scope, unit) for unit in (policy.hermes_unit, policy.background_unit)]
    identity = json.dumps([policy.scope, policy.hermes_unit, policy.background_unit])
    old = conn.execute("SELECT policy,body FROM pair_sample WHERE id=1").fetchone()
    previous = json.loads(old[1]) if old and old[0] == identity else None
    current = pair_sample(*pair, backend.boot, now)
    if current is None:
        conn.execute("DELETE FROM pair_sample WHERE id=1")
        return [], pair
    if (not previous or current["boot"] != previous["boot"] or current["invocations"] != previous["invocations"]
            or now < previous["now"] or now - previous["now"] >= 60):
        conn.execute("INSERT OR REPLACE INTO pair_sample VALUES(1,?,?)", (identity, json.dumps(current)))
        conn.commit()
    return attribute(previous, current, policy.resources), pair


def supervise(conn, backend, policy: SystemPolicy | None, *, now: float, safe: bool, rollback=False,
              allow_kind=None) -> tuple[list, dict]:
    record = _active(conn)
    if not record and (rollback or not policy):
        return [], {"status": "idle", "reason": "no_active_trial" if rollback else "no_systemd_policy"}
    scope = record["scope"] if record else policy.scope
    with backend.lock(scope) as locked:
        if not locked:
            return [], {"status": "skipped", "reason": "systemd_action_lock_unavailable"}
        record = _active(conn)
        if record:
            if record["scope"] != scope:
                return [], {"status": "skipped", "reason": "different_action_in_progress"}
            return [], _check_trial(conn, backend, record, policy, safe, now, manual=rollback)
        if rollback:
            return [], {"status": "idle", "reason": "no_active_trial"}
        candidates, pair = _sample(conn, backend, policy, now)
        if not candidates or policy.mode != "apply" or not safe:
            return candidates, {"status": "observing", "reason": "evidence_or_health_required" if policy.mode == "apply"
                                else "observe_mode"}
        recent = conn.execute("SELECT 1 FROM weight_trial WHERE started >= ? LIMIT 1", (now - COOLDOWN_SECONDS,)).fetchone()
        if recent:
            return candidates, {"status": "skipped", "reason": "cooldown"}
        for candidate in candidates:
            if allow_kind and not allow_kind(candidate):
                continue
            ineffective = conn.execute("""SELECT 1 FROM weight_trial WHERE scope=? AND unit=? AND kind=? AND started>=?
                AND reason IN ('insufficient_improvement','effective_weight_unverified','health_or_effect_unverified') LIMIT 1""",
                                       (policy.scope, policy.background_unit, candidate["kind"], now - 86400)).fetchone()
            if not ineffective:
                return candidates, _begin(conn, backend, policy, candidate, pair, now)
        return candidates, {"status": "skipped", "reason": "diagnosis_gate_or_ineffective_previous_trial"}


def journal_history(home: Path) -> dict:
    path = home / "evolution" / "supervisor.sqlite3"
    if not path.is_file():
        return {"actions": [], "incidents": []}
    with _journal(home) as conn:
        trials = conn.execute("""SELECT id,scope,unit,hermes,kind,previous_weight,applied_weight,baseline_psi,observed_psi,
             started,status,updated,reason FROM weight_trial ORDER BY id DESC LIMIT 100""")
        return {"actions": [dict(row) for row in trials], "incidents": incident_history(conn)}


def incident_history(conn) -> list[dict]:
    return [dict(row) for row in conn.execute("SELECT * FROM incident ORDER BY last_seen DESC,code LIMIT 100")]


def learned_baseline(conn, *, now: float) -> dict:
    prior = conn.execute("""SELECT average_prompt_tokens,research_p95_ms FROM baseline
                        WHERE day BETWEEN ? AND ? ORDER BY day""", (_day(now - 35 * 86400), _day(now - 86400)))
    values = list(prior)
    return {key: [row[index] for row in values if row[index] is not None]
            for index, key in enumerate(("average_prompt_tokens", "research_p95_ms"))}


def record_observations(conn, diagnoses: list[dict], usage: dict, research: dict, *, now: float) -> None:
    today = _day(now)
    for issue in diagnoses:
        conn.execute("""INSERT INTO incident VALUES(?,?,1,?,?,?) ON CONFLICT(code) DO UPDATE SET
            key_text=excluded.key_text, occurrences=occurrences+(last_day!=excluded.last_day),
            last_day=excluded.last_day,last_seen=excluded.last_seen,severity=excluded.severity""",
                     (issue["code"], issue["hypothesis"][:400], today, now, issue["severity"]))
    metrics = baseline_metrics(usage, research)
    if any(value is not None for value in metrics.values()):
        conn.execute("INSERT OR REPLACE INTO baseline VALUES(?,?,?)",
                     (today, metrics["average_prompt_tokens"], metrics["research_p95_ms"]))
    conn.execute("DELETE FROM baseline WHERE day < ?", (_day(now - 35 * 86400),))
    conn.execute("DELETE FROM incident WHERE last_seen < ?", (now - 90 * 86400,))
    conn.execute("DELETE FROM weight_trial WHERE status NOT IN " + _STATES + " AND updated < ?", (now - 90 * 86400,))


def baseline_metrics(usage: dict, research: dict) -> dict:
    main = [row for row in usage["groups"] if row["lane"] == "main" and row["usage_samples"]]
    samples = sum(row["usage_samples"] for row in main)
    input_tokens = sum(row["input_tokens"] + row["cache_read_tokens"] + row["cache_write_tokens"] for row in main)
    return {"average_prompt_tokens": input_tokens / samples if samples >= 5 else None,
            "research_p95_ms": research["p95_ms"] if research["turns"] >= 5 else None}
