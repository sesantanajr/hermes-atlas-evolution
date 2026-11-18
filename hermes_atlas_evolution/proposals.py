"""One reviewable queue for everything the evolution loop wants to change.

Each proposal carries its evidence, a risk class and the exact command that applies it. The plugin itself
only applies changes to its own playbook (activate or retire a lesson). Anything that touches Hermes
(skills, configuration) is shown as a native command for the operator to run.
"""

from __future__ import annotations

import json
import shlex
import sqlite3

from . import lessons as playbook

REOPEN_AFTER_SECONDS = 30 * 86_400
REGRESSION_DAYS = 30
INVESTIGATE_MIN_COUNT = 5   # failures in the window before an untemplated one is worth an investigation
INVESTIGATE_MIN_DAYS = 2
INVESTIGATE_LIMIT = 100     # at most this many investigation proposals are kept open
# Who wrote a lesson decides the risk of showing it to the agent: fixed templates, a model, or the agent itself.
LESSON_RISK = {"derived": "low", "operator": "low", "reflector": "medium", "agent": "high"}
_UNTEMPLATED = ("code_exception", "command_failed", "other", "auth", "network")


def _upsert(conn, seen: set, code: str, kind: str, risk: str, subject: str, evidence: dict, command: str, now: float) -> None:
    seen.add(code)
    body = json.dumps(evidence, sort_keys=True)
    row = conn.execute("SELECT id, status, updated, note FROM proposal WHERE code=?", (code,)).fetchone()
    # an applied change the owner is undoing (actions.undo_applied writes that first) counts as the owner's refusal,
    # even when the record of the undo itself was lost
    refused = row is not None and (row["status"] == "rejected" or row["status"] == "applied" and row["note"] == "undoing")
    if row is None:
        conn.execute("""INSERT INTO proposal(code, kind, risk, subject, evidence, command, status, created, updated)
            VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)""", (code, kind, risk, subject, body, command, now, now))
    elif row["status"] in ("open", "approved"):
        conn.execute("UPDATE proposal SET evidence=?, command=?, risk=? WHERE id=?", (body, command, risk, row["id"]))
    elif row["status"] in ("expired",) or refused and now - row["updated"] >= REOPEN_AFTER_SECONDS \
            or not refused and row["status"] == "applied" and code.startswith("env:") and now - row["updated"] >= 86_400:
        # a change across profiles that is true again a day after it was applied (the owner put it back by hand) is
        # said again; one the owner undid is never applied by itself again (actions.automatic keeps the skip list)
        conn.execute("UPDATE proposal SET evidence=?, command=?, risk=?, status='open', updated=?, note=NULL WHERE id=?",
                     (body, command, risk, now, row["id"]))


# The placeholder of a stored `lessons add` command: one spelling, whatever the pass's language (the menu, the panel
# and the digest write the command again in the reader's language).
_INSTEAD = "<what to do instead>"


def _command_for(conn: sqlite3.Connection, home=None):
    """rest -> `hermes -p <profile> <rest>`, the explicit shape every printed command has (cli._hermes_cmd), for the
    profile `home` (by default the one whose database this is; SQLite names it with links followed, so a linked
    profile folder needs its home given; `default` when it cannot be told)."""
    from .decisions import _home_of

    home = home if home is not None else _home_of(conn)
    if home is None:
        return lambda rest: f"hermes -p default {rest}"
    from .cli import _hermes_cmd

    return lambda rest: _hermes_cmd(home, rest)


def investigate_candidates(conn: sqlite3.Connection, *, now: float, days: int = 7) -> list[dict]:
    """Every untemplated failure of the last `days` that qualifies for an investigation proposal, in the shape of
    signals.failure_summary. Unlike that summary it has no top-N cut, so a proposal does not expire and come back
    as busier failures move it out of the ranking; INVESTIGATE_LIMIT only bounds a pathological profile."""
    from .experience import day_offset, utc_day

    marks = ",".join("?" * len(_UNTEMPLATED))
    rows = conn.execute(f"""SELECT f.signature, f.tool, f.klass, f.hint, SUM(d.count) AS recent,
          COUNT(DISTINCT d.day) AS recent_days
        FROM failure f JOIN failure_daily d ON d.signature=f.signature
        WHERE d.day BETWEEN ? AND ? AND f.klass IN ({marks}) AND f.tool != 'unknown' GROUP BY f.signature
        HAVING SUM(d.count) >= ? AND COUNT(DISTINCT d.day) >= ?
        ORDER BY recent DESC, recent_days DESC, f.signature LIMIT ?""",
                        (day_offset(now, days - 1), utc_day(now), *_UNTEMPLATED, INVESTIGATE_MIN_COUNT,
                         INVESTIGATE_MIN_DAYS, INVESTIGATE_LIMIT)).fetchall()
    return [{"signature": row["signature"], "tool": row["tool"], "class": row["klass"], "hint": row["hint"],
             "count": row["recent"], "days": row["recent_days"]} for row in rows]


def refresh(conn: sqlite3.Connection, *, now: float, skills: dict | None = None, reviews: dict | None = None,
            failures: list | None = None, turns: dict | None = None, changes: list | None = None,
            outcomes: dict | None = None, radar: list | None = None, lang: str = "en", home=None) -> list[dict]:
    """Rebuild open proposals from current evidence. Proposals whose evidence is gone expire.

    A lesson proposal carries the exact sentence the agent would read. Regressions come from the stored
    verdicts of the last 30 days, so a high-risk proposal stays open while its evidence stands.

    The rebuild is one write transaction, taken first (BEGIN IMMEDIATE waits for the hooks' writes like any write).
    Started by a read instead, an insert made while hooks commit failed at once with SQLITE_BUSY_SNAPSHOT (the read
    saw an older database), with no busy wait: 2 to 45% of the passes of a busy gateway failed there."""
    seen: set[str] = set()
    command = _command_for(conn, home)
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    for row in conn.execute("SELECT l.*, COALESCE(f.total,0) AS total, COALESCE(f.sessions,0) AS sessions FROM lesson l "
                            "LEFT JOIN failure f ON f.signature=l.signature WHERE l.status IN ('candidate','quarantined')"
                            ).fetchall():
        _upsert(conn, seen, f"lesson:{row['id']}", "lesson", LESSON_RISK.get(row["origin"], "high"), row["tool"],
                {"lesson_id": row["id"], "origin": row["origin"], "class": row["klass"], "failures_seen": row["total"],
                 "sessions": row["sessions"], "lesson": playbook.render(row, lang)},
                command(f"evolution lessons approve {row['id']}"), now)
    for row in conn.execute("SELECT * FROM lesson WHERE status='active' AND verdict='ineffective'").fetchall():
        _upsert(conn, seen, f"lesson_retire:{row['id']}", "lesson_retire", "low", row["tool"],
                {"lesson_id": row["id"], "rate_before": row["rate_before"], "rate_after": row["rate_after"],
                 "basis": playbook.basis_of(conn, row["id"])},   # per call or per day: explain words the rates by it
                command(f"evolution lessons retire {row['id']}"), now)
    for finding in (skills or {}).get("findings", ()):
        skill = finding["skill"]
        if finding["code"] == "skill_churn":
            _upsert(conn, seen, f"skill_lock:{skill}", "skill_lock", "medium", skill,
                    {"rewrites": finding["rewrites"], "flipflops": finding["flipflops"],
                     "window_days": finding["window_days"], "review_first": f"hermes curator ledger --skill {skill}"},
                    f"hermes curator lock {skill}", now)
        elif finding["code"] == "skill_unexercised_rewrites":
            _upsert(conn, seen, f"skill_review:{skill}", "skill_review", "low", skill,
                    {"unreused_rewrites": finding["unreused_rewrites"]}, f"hermes curator ledger --skill {skill}", now)
        elif finding["code"] == "skill_never_used":
            _upsert(conn, seen, f"skill_archive:{skill}", "skill_archive", "low", skill,
                    {"age_days": finding["age_days"]}, f"hermes curator archive {skill}", now)
    for finding in (outcomes or {}).get("findings", ()):
        skill = finding["skill"]
        _upsert(conn, seen, f"skill_outcome:{skill}", "skill_outcome", "low", skill,
                {"turns": finding["turns"], "worse": finding["worse"], "skill_rates": finding["skill_rates"],
                 "profile_rates": finding["profile_rates"]}, f"hermes curator ledger --skill {skill}", now)
    if reviews and reviews.get("reviews", 0) >= 10 and (reviews.get("empty_pct") or 0) >= 50:
        _upsert(conn, seen, "review_interval", "review_interval", "medium", "skills.creation_nudge_interval",
                {"reviews": reviews["reviews"], "empty_pct": reviews["empty_pct"], "model_calls": reviews["model_calls"],
                 "input_tokens": reviews["input_tokens"], "output_tokens": reviews["output_tokens"]},
                "hermes config set skills.creation_nudge_interval 20", now)
    for item in failures or ():
        if (item["class"] in _UNTEMPLATED and item["count"] >= INVESTIGATE_MIN_COUNT and item["days"] >= INVESTIGATE_MIN_DAYS
                and item["tool"] != "unknown"):
            _upsert(conn, seen, f"investigate:{item['signature']}", "investigate_failure", "low", item["tool"],
                    {"class": item["class"], "hint": item["hint"], "count": item["count"], "days": item["days"]},
                    command(f"evolution lessons add --tool {shlex.quote(str(item['tool']))} \"{_INSTEAD}\""), now)
    if turns and turns.get("turns", 0) >= 20 and (turns.get("slow_pct") or 0) >= 30:
        _upsert(conn, seen, "slow_turns", "investigate_latency", "medium", "turns",
                {"turns": turns["turns"], "slow_pct": turns["slow_pct"], "p50_ms": turns["p50_ms"], "p95_ms": turns["p95_ms"]},
                command("evolution report --days 7"), now)
    for finding in radar or ():   # read from Hermes' own records by radar.scan; each names its inspection command
        _upsert(conn, seen, finding["code"], finding["kind"], finding["risk"], finding["subject"], finding["evidence"],
                finding["command"], now)
    del changes   # kept for callers; the stored verdicts below are the source of truth
    for row in conn.execute("""SELECT * FROM change WHERE verdict='regressed' AND kind IN ('skill','config')
                               AND at >= ?""", (now - REGRESSION_DAYS * 86_400,)).fetchall():
        try:
            detail, measured = json.loads(row["detail"]), json.loads(row["metrics"] or "{}")
        except ValueError:
            continue
        evidence = {"worse": measured.get("worse"), "concurrent_changes": measured.get("concurrent_changes"),
                    "metrics": measured.get("metrics"), "final": row["evaluated"] is not None}
        if row["kind"] == "skill" and isinstance(detail.get("last_entry"), str):
            _upsert(conn, seen, f"skill_rollback:{row['id']}", "skill_rollback", "high", row["subject"], evidence,
                    f"hermes curator rollback {detail['last_entry']}", now)
        elif row["kind"] == "config":
            _upsert(conn, seen, f"config_review:{row['id']}", "config_review", "high", row["subject"], evidence,
                    f"hermes config get {row['subject']}", now)
    stale = [row["id"] for row in conn.execute("SELECT id, code FROM proposal WHERE status='open'").fetchall()
             if row["code"] not in seen]
    conn.executemany("UPDATE proposal SET status='expired', updated=? WHERE id=?", [(now, item) for item in stale])
    return listing(conn, status="open")


def decide(conn: sqlite3.Connection, proposal_id: int, decision: str, *, now: float, note: str | None = None) -> dict:
    """Approve or reject one proposal. Lesson proposals take effect immediately; native ones await the operator."""
    if decision not in ("approve", "reject"):
        raise ValueError("Decide with approve or reject")
    row = conn.execute("SELECT * FROM proposal WHERE id=?", (proposal_id,)).fetchone()
    if row is None:
        raise ValueError(f"Unknown proposal id {proposal_id}")
    if row["status"] not in ("open", "approved"):
        raise ValueError(f"Proposal {proposal_id} is already {row['status']}")
    evidence = json.loads(row["evidence"])
    status, applied = "rejected", None
    if decision == "approve":
        if row["kind"] in ("lesson", "lesson_retire"):
            applied = playbook.set_status(conn, evidence["lesson_id"], "active" if row["kind"] == "lesson" else "retired", now=now)
            status = "applied"
        else:
            status = "approved"
    elif row["kind"] == "lesson":
        playbook.set_status(conn, evidence["lesson_id"], "retired", now=now)
    elif row["kind"] == "lesson_retire":   # the owner keeps the lesson: auto mode must not retire it anyway
        playbook.keep(conn, evidence["lesson_id"], now=now)
    note = " ".join(note.split())[:200] if isinstance(note, str) and note.strip() else None
    conn.execute("UPDATE proposal SET status=?, updated=?, note=? WHERE id=?", (status, now, note, proposal_id))
    if status == "rejected" and row["kind"] not in ("lesson", "lesson_retire"):
        # The owner said no: a change the Mode auto could apply by itself is never applied by it again, also after
        # the proposal reopens (30 days later, or once the history is pruned). Undoing the rejection lifts it.
        conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES (?, ?)", (f"auto_skip:{row['code']}", str(now)))
    return {"id": proposal_id, "kind": row["kind"], "status": status, "applied": applied, "code": row["code"],
            "run_this": row["command"] if status == "approved" else None}


def listing(conn: sqlite3.Connection, *, status: str | None = None, limit: int = 100) -> list[dict]:
    order = "CASE risk WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, created, id"
    rows = conn.execute(f"SELECT * FROM proposal WHERE (? IS NULL OR status=?) ORDER BY {order} LIMIT ?",
                        (status, status, max(1, min(limit, 500)))).fetchall()
    return [{"id": row["id"], "kind": row["kind"], "risk": row["risk"], "subject": row["subject"], "status": row["status"],
             "evidence": json.loads(row["evidence"]), "command": row["command"], "created": row["created"],
             "note": row["note"]} for row in rows]
