"""One reviewable queue for everything the evolution loop wants to change.

Each proposal carries its evidence, a risk class and the exact command that applies it. The plugin itself
only applies changes to its own playbook (activate or retire a lesson). Anything that touches Hermes
(skills, configuration) is shown as a native command for the operator to run.
"""

from __future__ import annotations

import json
import sqlite3

from . import lessons as playbook

RISKS = ("low", "medium", "high")
REOPEN_AFTER_SECONDS = 30 * 86_400
_UNTEMPLATED = ("code_exception", "command_failed", "other", "auth", "network")


def _upsert(conn, seen: set, code: str, kind: str, risk: str, subject: str, evidence: dict, command: str, now: float) -> None:
    seen.add(code)
    body = json.dumps(evidence, sort_keys=True)
    row = conn.execute("SELECT id, status, updated FROM proposal WHERE code=?", (code,)).fetchone()
    if row is None:
        conn.execute("""INSERT INTO proposal(code, kind, risk, subject, evidence, command, status, created, updated)
            VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)""", (code, kind, risk, subject, body, command, now, now))
    elif row["status"] in ("open", "approved"):
        conn.execute("UPDATE proposal SET evidence=?, command=?, risk=? WHERE id=?", (body, command, risk, row["id"]))
    elif row["status"] in ("expired",) or row["status"] == "rejected" and now - row["updated"] >= REOPEN_AFTER_SECONDS:
        conn.execute("UPDATE proposal SET evidence=?, command=?, risk=?, status='open', updated=?, note=NULL WHERE id=?",
                     (body, command, risk, now, row["id"]))


def refresh(conn: sqlite3.Connection, *, now: float, skills: dict | None = None, reviews: dict | None = None,
            failures: list | None = None, turns: dict | None = None, changes: list | None = None,
            outcomes: dict | None = None) -> list[dict]:
    """Rebuild open proposals from current evidence. Proposals whose evidence is gone expire."""
    seen: set[str] = set()
    for row in conn.execute("SELECT l.*, COALESCE(f.total,0) AS total, COALESCE(f.sessions,0) AS sessions FROM lesson l "
                            "LEFT JOIN failure f ON f.signature=l.signature WHERE l.status IN ('candidate','quarantined')"):
        _upsert(conn, seen, f"lesson:{row['id']}", "lesson", "low", row["tool"],
                {"lesson_id": row["id"], "origin": row["origin"], "class": row["klass"], "failures_seen": row["total"],
                 "sessions": row["sessions"]},
                f"hermes evolution lessons approve {row['id']}", now)
    for row in conn.execute("SELECT * FROM lesson WHERE status='active' AND verdict='ineffective'"):
        _upsert(conn, seen, f"lesson_retire:{row['id']}", "lesson_retire", "low", row["tool"],
                {"lesson_id": row["id"], "rate_before": row["rate_before"], "rate_after": row["rate_after"]},
                f"hermes evolution lessons retire {row['id']}", now)
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
        if item["class"] in _UNTEMPLATED and item["count"] >= 5 and item["days"] >= 2 and item["tool"] != "unknown":
            _upsert(conn, seen, f"investigate:{item['signature']}", "investigate_failure", "low", item["tool"],
                    {"class": item["class"], "hint": item["hint"], "count": item["count"], "days": item["days"]},
                    f"hermes evolution lessons add --tool {item['tool']} \"<what to do instead>\"", now)
    if turns and turns.get("turns", 0) >= 20 and (turns.get("slow_pct") or 0) >= 30:
        _upsert(conn, seen, "slow_turns", "investigate_latency", "medium", "turns",
                {"turns": turns["turns"], "slow_pct": turns["slow_pct"], "p50_ms": turns["p50_ms"], "p95_ms": turns["p95_ms"]},
                "hermes evolution report --days 7", now)
    for change in changes or ():
        if change.get("verdict") != "regressed":
            continue
        if change["kind"] == "skill":
            row = conn.execute("SELECT detail FROM change WHERE id=?", (change["id"],)).fetchone()
            entry = json.loads(row["detail"]).get("last_entry") if row else None
            if entry:
                _upsert(conn, seen, f"skill_rollback:{change['id']}", "skill_rollback", "high", change["subject"],
                        {"worse": change["worse"], "concurrent_changes": change["concurrent_changes"],
                         "metrics": change["metrics"]}, f"hermes curator rollback {entry}", now)
        elif change["kind"] == "config":
            _upsert(conn, seen, f"config_review:{change['id']}", "config_review", "high", change["subject"],
                    {"worse": change["worse"], "concurrent_changes": change["concurrent_changes"],
                     "metrics": change["metrics"]}, f"hermes config get {change['subject']}", now)
    stale = [row["id"] for row in conn.execute("SELECT id, code FROM proposal WHERE status='open'") if row["code"] not in seen]
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
    note = " ".join(note.split())[:200] if isinstance(note, str) and note.strip() else None
    conn.execute("UPDATE proposal SET status=?, updated=?, note=? WHERE id=?", (status, now, note, proposal_id))
    return {"id": proposal_id, "kind": row["kind"], "status": status, "applied": applied,
            "run_this": row["command"] if status == "approved" else None}


def listing(conn: sqlite3.Connection, *, status: str | None = None, limit: int = 100) -> list[dict]:
    order = "CASE risk WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, created, id"
    rows = conn.execute(f"SELECT * FROM proposal WHERE (? IS NULL OR status=?) ORDER BY {order} LIMIT ?",
                        (status, status, max(1, min(limit, 500)))).fetchall()
    return [{"id": row["id"], "kind": row["kind"], "risk": row["risk"], "subject": row["subject"], "status": row["status"],
             "evidence": json.loads(row["evidence"]), "command": row["command"], "created": row["created"],
             "note": row["note"]} for row in rows]
