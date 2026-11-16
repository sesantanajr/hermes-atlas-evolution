"""Actions the plugin takes for the owner (0.8.3): a proposal applied with one click, and automatic actions.

`auto_actions` (a setting the agent cannot widen, see cli.TRUSTED_ONLY) says what runs by itself at the end of each
learning pass:
- "off": nothing; everything waits for the owner.
- "safe" (default): exact repeats in Hermes' memory are taken out (memory_care: the same entry twice, case and
  spacing aside; a copy of the file is kept), nothing else.
- "full": also the reversible changes to Hermes of explain.AUTOMATIC (today: locking a skill the curator keeps
  rewriting), at most MAX_CHANGES_PER_PASS per pass, with Hermes' own command.

Every action is recorded in the decision history with "automatic" as the actor and can be undone from the menu,
the panel or `hermes evolution decisions undo <id>`. An automatic change the owner undoes is never applied
automatically again (`auto_skip:<code>`).
"""

from __future__ import annotations

import json
from pathlib import Path

MODES = ("off", "safe", "full")
MAX_CHANGES_PER_PASS = 3
MAX_MEMORY_PER_PASS = 10


def _hermes(home: Path, args: list, *, runner=None) -> None:
    from .admin import run_hermes

    run_hermes(home, args, runner=runner)


def apply_proposal(home: Path, proposal_id: int, *, now: float, actor: str = "terminal", runner=None) -> dict:
    """Apply a proposal the plugin can apply (explain.plan) with Hermes' own command, and record it with its undo.
    Nothing is held while Hermes runs; the proposal must still be open and unchanged when the command returns."""
    from . import decisions, explain
    from .experience import experience
    from .proposals import listing

    with experience(home) as conn:
        row = conn.execute("SELECT * FROM proposal WHERE id=?", (int(proposal_id),)).fetchone()
        if row is None:
            raise ValueError(f"Unknown proposal id {proposal_id}")
        item = next((entry for entry in listing(conn, status=row["status"]) if entry["id"] == row["id"]), None)
    if item is None or row["status"] not in ("open", "approved"):
        return {"id": proposal_id, "applied": False, "reason": "not_open"}
    found = explain.plan(item)
    if found is None:
        raise ValueError("The plugin cannot apply this proposal; approve it to see the command")
    _hermes(home, found[0], runner=runner)
    with experience(home) as conn:
        conn.execute("UPDATE proposal SET status='applied', updated=? WHERE id=?", (now, row["id"]))
        decision = decisions.record_applied(conn, row["id"], code=row["code"], before=row["status"], apply=found[0],
                                            undo=found[1], now=now, actor=actor)
    return {"id": row["id"], "applied": True, "decision": decision, "undo": "hermes " + " ".join(found[1])}


def undo_applied(home: Path, result: dict, *, now: float, runner=None) -> dict:
    """Undo an applied proposal (from decisions.undo): run its undo command, close the proposal as the owner's
    refusal, and keep the autopilot from applying it again."""
    from . import decisions
    from .experience import experience

    _hermes(home, list(result["undo"]), runner=runner)
    with experience(home) as conn:
        conn.execute("UPDATE proposal SET status='rejected', note='undone', updated=? WHERE id=?",
                     (now, int(result["proposal"])))
        conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES (?, ?)", (f"auto_skip:{result['code']}", str(now)))
        decisions.mark_undone(conn, result["id"], now=now)
    return {**result, "undone": True}


def _memory(home: Path, runtime_config, *, now: float) -> list[dict]:
    from . import memory_care

    removed = []
    for _ in range(MAX_MEMORY_PER_PASS):   # read again after each removal: positions and repeats change
        issues = [item for item in memory_care.review(home, runtime_config)["issues"] if item["kind"] == "exact"]
        if not issues:
            break
        drop = issues[0]["suggested_drop"]
        result = memory_care.remove(home, drop["file"], drop["fingerprint"], now=now, actor="automatic",
                                    runtime_config=runtime_config)
        if not result.get("removed"):
            break
        removed.append({"file": drop["file"], "decision": result["decision"]})
    return removed


def automatic(home: Path, *, now: float, mode: str, runtime_config=None, runner=None, usage=None) -> dict:
    """The automatic actions of one pass. Failures are reported, never raised: the pass itself must not fail."""
    from .experience import experience

    done: dict = {"mode": mode if mode in MODES else "off", "memory": [], "applied": [], "failed": []}
    if done["mode"] == "off":
        return done
    try:
        done["memory"] = _memory(home, runtime_config, now=now)
    except (OSError, ValueError) as error:
        done["failed"].append({"what": "memory", "error": type(error).__name__})
    if done["mode"] != "full":
        return done
    from . import explain

    if usage is None:
        try:
            from .skill_health import read_usage

            usage = read_usage(home)
        except (OSError, ValueError):
            usage = {}
    with experience(home) as conn:
        skipped = {row[0].split(":", 1)[1] for row in conn.execute("SELECT key FROM cursor WHERE key LIKE 'auto_skip:%'")}
        rows = conn.execute(f"""SELECT id, code, kind, subject FROM proposal WHERE status='open'
            AND kind IN ({','.join('?' * len(explain.AUTOMATIC))}) ORDER BY id""", explain.AUTOMATIC).fetchall()
    for row in rows:
        if len(done["applied"]) >= MAX_CHANGES_PER_PASS:
            break
        if row["code"] in skipped:
            continue
        record = usage.get(row["subject"]) if isinstance(usage, dict) else None
        if isinstance(record, dict) and (record.get("locked") or record.get("pinned")):
            with experience(home) as conn:   # already as the proposal asks: nothing to run
                conn.execute("UPDATE proposal SET status='applied', updated=? WHERE id=?", (now, row["id"]))
            continue
        try:
            result = apply_proposal(home, row["id"], now=now, actor="automatic", runner=runner)
        except (OSError, ValueError) as error:
            done["failed"].append({"proposal": row["id"], "error": str(error)[:120]})
            continue
        if result.get("applied"):
            done["applied"].append({"proposal": row["id"], "kind": row["kind"], "subject": row["subject"],
                                    "decision": result["decision"]})
    return done


def recent(conn, *, now: float, days: int = 7) -> list[dict]:
    """The automatic actions of the last `days`, newest first, as the history shows them."""
    rows = conn.execute("SELECT * FROM decision_log WHERE actor='automatic' AND at >= ? ORDER BY id DESC LIMIT 50",
                        (now - days * 86_400,)).fetchall()
    return [{"id": row["id"], "at": row["at"], "kind": row["kind"], "target": row["target"], "action": row["action"],
             "after": json.loads(row["after"]), "undone": row["undone"]} for row in rows]

