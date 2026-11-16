"""Actions the plugin takes for the owner (0.8.3): a proposal applied with one click, and automatic actions.

`auto_actions` (a setting the agent cannot widen, see cli.TRUSTED_ONLY) says what runs by itself at the end of each
learning pass:
- "off": nothing; everything waits for the owner.
- "safe" (default): copies of a memory entry that are the very same text, character for character, are taken
  out of Hermes' memory (a copy of the file is kept); nothing else. An entry the owner put back stays.
- "full": also the reversible changes to Hermes of explain.AUTOMATIC (today: locking a skill the curator keeps
  rewriting), at most MAX_CHANGES_PER_PASS per pass, with Hermes' own command; one Hermes refuses waits a day.

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
FAILURE_BACKOFF_SECONDS = 86_400   # an automatic change Hermes refused is tried again a day later
STALE_APPLYING_SECONDS = 900       # a claim left by a process that died while Hermes ran
COMMAND_SECONDS = 180              # Hermes may finish its own maintenance before the command (after an update)


def _hermes(home: Path, args: list, *, runner=None) -> None:
    from .admin import run_hermes

    run_hermes(home, args, runner=runner, timeout=COMMAND_SECONDS, what="command")


def _recover(conn, now: float) -> None:
    conn.execute("UPDATE proposal SET status='open', updated=? WHERE status='applying' AND updated < ?",
                 (now, now - STALE_APPLYING_SECONDS))


def _usage(home: Path) -> dict:
    try:
        from .skill_health import read_usage

        found = read_usage(home)
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def apply_proposal(home: Path, proposal_id: int, *, now: float, actor: str = "terminal", runner=None,
                   usage: dict | None = None) -> dict:
    """Apply a proposal the plugin can apply (explain.plan) with Hermes' own command, and record it.

    The proposal is claimed first (status `applying`, only from open or approved), so a decision made meanwhile in the
    panel or another terminal, or a second click, finds it taken and nothing runs twice; Hermes runs with no
    transaction held; a refusal puts the proposal back as it was. A skill the owner already locked or pinned is left
    as it is and no undo is recorded: undoing must never unlock the owner's own lock."""
    from . import decisions, explain
    from .experience import experience

    with experience(home) as conn:
        _recover(conn, now)
        row = conn.execute("SELECT * FROM proposal WHERE id=?", (int(proposal_id),)).fetchone()
        if row is None:
            raise ValueError(f"Unknown proposal id {proposal_id}")
        found = explain.plan(dict(row))
        if found is None:
            raise ValueError("The plugin cannot apply this proposal; approve it to see the command")
        previous = row["status"]
        claimed = conn.execute("UPDATE proposal SET status='applying', updated=? WHERE id=? AND status IN "
                               "('open', 'approved')", (now, row["id"])).rowcount
    if not claimed:
        return {"id": row["id"], "applied": False, "reason": "not_open"}
    record = (usage if usage is not None else _usage(home)).get(row["subject"])
    if row["kind"] == "skill_lock" and isinstance(record, dict) and (record.get("locked") or record.get("pinned")):
        with experience(home) as conn:
            conn.execute("UPDATE proposal SET status='applied', updated=? WHERE id=? AND status='applying'", (now, row["id"]))
        return {"id": row["id"], "applied": False, "reason": "already"}
    try:
        _hermes(home, found[0], runner=runner)
    except BaseException:
        with experience(home) as conn:
            conn.execute("UPDATE proposal SET status=?, updated=? WHERE id=? AND status='applying'",
                         (previous, now, row["id"]))
        raise
    with experience(home) as conn:
        conn.execute("UPDATE proposal SET status='applied', updated=? WHERE id=? AND status='applying'", (now, row["id"]))
        decision = decisions.record_applied(conn, row["id"], code=row["code"], before=previous, apply=found[0],
                                            undo=found[1], now=now, actor=actor)
    return {"id": row["id"], "applied": True, "decision": decision, "undo": "hermes " + " ".join(found[1])}


def apply_many(home: Path, ids: list, *, now: float, actor: str = "terminal", runner=None) -> dict:
    """Apply several proposals one after the other (the panel's and the menu's "apply all")."""
    applied, skipped = [], []
    usage = _usage(home)
    for raw in ids[:50]:
        try:
            result = apply_proposal(home, int(raw), now=now, actor=actor, runner=runner, usage=usage)
        except (TypeError, ValueError) as error:
            skipped.append({"id": raw, "reason": str(error)[:160]})
            continue
        (applied.append(result["id"]) if result.get("applied") else skipped.append({"id": result["id"],
                                                                                   "reason": result.get("reason")}))
    return {"applied": applied, "skipped": skipped}


def undo_applied(home: Path, result: dict, *, now: float, runner=None) -> dict:
    """Undo an applied proposal (decisions.undo derived the undo from what was applied): run it, close the proposal as
    the owner's refusal, and keep the autopilot from applying it again."""
    from . import decisions
    from .experience import experience

    _hermes(home, list(result["undo"]), runner=runner)
    with experience(home) as conn:
        conn.execute("UPDATE proposal SET status='rejected', note='undone', updated=? WHERE id=?",
                     (now, int(result["proposal"])))
        if result.get("code"):
            conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES (?, ?)", (f"auto_skip:{result['code']}", str(now)))
        decisions.mark_undone(conn, result["id"], now=now)
    return {**result, "undone": True}


def memory_skip(conn, name: str, mark: str, now: float) -> None:
    """Never take this entry out by itself again (the owner put it back)."""
    conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES (?, ?)", (f"auto_skip:memory:{name}:{mark}", str(now)))


def _memory(home: Path, runtime_config, *, now: float) -> list[dict]:
    """Take out copies of an entry that are the very same text, character for character. An entry that differs in
    anything, case and spacing included (a path, a password, an indented block), is the owner's to decide."""
    from . import memory_care
    from .experience import experience

    with experience(home) as conn:
        skipped = {row[0] for row in conn.execute("SELECT key FROM cursor WHERE key LIKE 'auto_skip:memory:%'")}
    removed = []
    for _ in range(MAX_MEMORY_PER_PASS):   # read again after each removal: positions and repeats change
        issues = [item for item in memory_care.review(home, runtime_config)["issues"]
                  if item["kind"] == "exact" and item.get("identical")
                  and f"auto_skip:memory:{item['suggested_drop']['file']}:{item['suggested_drop']['fingerprint']}" not in skipped]
        if not issues:
            break
        drop = issues[0]["suggested_drop"]
        result = memory_care.remove(home, drop["file"], drop["fingerprint"], now=now, actor="automatic",
                                    runtime_config=runtime_config)
        if not result.get("removed"):
            break
        removed.append({"file": drop["file"], "decision": result["decision"]})
    return removed


def automatic(home: Path, *, now: float, mode: str, runtime_config=None, runner=None, usage=None,
              deadline: float | None = None) -> dict:
    """The automatic actions of one pass. Failures are reported, never raised: the pass itself must not fail.
    `deadline` (time.monotonic) leaves Hermes commands out when one might not finish before it."""
    import time

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

    usage = usage if isinstance(usage, dict) else _usage(home)
    with experience(home) as conn:
        _recover(conn, now)
        held = {}
        for key, value in conn.execute("SELECT key, value FROM cursor WHERE key LIKE 'auto_skip:%' OR key LIKE 'auto_fail:%'"):
            held[key] = value
        rows = conn.execute(f"""SELECT id, code, kind, subject FROM proposal WHERE status='open'
            AND kind IN ({','.join('?' * len(explain.AUTOMATIC))}) ORDER BY id""", explain.AUTOMATIC).fetchall()
    for row in rows:
        if len(done["applied"]) >= MAX_CHANGES_PER_PASS:
            break
        if deadline is not None and deadline - time.monotonic() < COMMAND_SECONDS + 10:
            break
        if row["code"] != f"{row['kind']}:{row['subject']}" or f"auto_skip:{row['code']}" in held:
            continue   # only the proposals the loop writes itself, and never one the owner undid
        try:
            failed_at = float(held.get(f"auto_fail:{row['code']}") or 0)
        except ValueError:
            failed_at = 0
        if now - failed_at < FAILURE_BACKOFF_SECONDS:
            continue
        try:
            result = apply_proposal(home, row["id"], now=now, actor="automatic", runner=runner, usage=usage)
        except (OSError, ValueError) as error:
            done["failed"].append({"proposal": row["id"], "error": str(error)[:120]})
            with experience(home) as conn:
                conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES (?, ?)", (f"auto_fail:{row['code']}", str(now)))
            break   # Hermes refusing one usually refuses the next too: the next pass tries again
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

