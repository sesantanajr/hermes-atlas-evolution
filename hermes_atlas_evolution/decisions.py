"""The owner's decisions on what the loop learned, from the terminal or the dashboard panel, and their undo.

Every decision goes through here, so both places behave the same and the history shows all of them:
- a lesson is approved, rejected (while it waits), retired (while active) or reactivated (once retired);
- "approve all" takes the exact list of lessons the owner was shown, each with a fingerprint of the sentence
  shown, and decides only on those still waiting with that same sentence; a lesson the agent proposed is never
  part of it and must be approved on its own;
- a proposal is approved or rejected; a lesson proposal decides its lesson;
- a change of a setting is recorded with its value before, so it can be undone too;
- an entry taken out of Hermes' memory (memory_care.remove) is recorded with its fingerprint and the copy of the
  file kept before, so it can be put back.

The log (`decision_log`) keeps ids, states, setting values and fingerprints only, never lesson or memory text. A decision is undone
only while what it changed is still as the decision left it: an undo never overrides a later decision.
Callers check that the owner is the one deciding (cli._operator_only, the panel's login rule) and own the
transaction.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3

from . import lessons as playbook

ACTIONS = ("approve", "reject", "retire", "reactivate")
WAITING = ("candidate", "quarantined")
ACTORS = ("terminal", "dashboard", "automatic")
HISTORY_LIMIT = 200


def fingerprint(sentence: str) -> str:
    """A short fingerprint of the sentence a person was shown, so a decision applies to that sentence only."""
    return hashlib.sha256(sentence.encode("utf-8", "replace")).hexdigest()[:16]


def _actor(actor: str) -> str:
    return actor if actor in ACTORS else "terminal"


def _state(row) -> dict:
    return {"status": row["status"], "verdict": row["verdict"], "activated": row["activated"]}


def _log(conn, *, now: float, actor: str, kind: str, target: str, action: str, before, after) -> int:
    cursor = conn.execute("""INSERT INTO decision_log(at, actor, kind, target, action, before, after)
        VALUES (?, ?, ?, ?, ?, ?, ?)""", (now, _actor(actor), kind, str(target), action,
                                          json.dumps(before, sort_keys=True), json.dumps(after, sort_keys=True)))
    return cursor.lastrowid


def _settle(conn, code: str, status: str, now: float) -> list[str]:
    """Close the open proposal that asked for this decision, so it does not linger in the queue."""
    cursor = conn.execute("UPDATE proposal SET status=?, updated=? WHERE code=? AND status IN ('open', 'approved')",
                          (status, now, code))
    return [code] if cursor.rowcount else []


def waiting(conn: sqlite3.Connection, *, lang: str = "en") -> list[dict]:
    """The lessons that wait for a decision, each with the sentence and its fingerprint."""
    rows = conn.execute("""SELECT * FROM lesson WHERE status IN ('candidate', 'quarantined')
        ORDER BY CASE origin WHEN 'derived' THEN 0 WHEN 'operator' THEN 1 WHEN 'reflector' THEN 2 ELSE 3 END, id""").fetchall()
    found = []
    for row in rows:
        sentence = playbook.render(row, lang)
        found.append({"id": row["id"], "tool": row["tool"], "class": row["klass"], "origin": row["origin"],
                      "status": row["status"], "lesson": sentence, "fingerprint": fingerprint(sentence),
                      "bulk": row["origin"] != "agent"})
    return found


def decide_lesson(conn: sqlite3.Connection, lesson_id: int, action: str, *, now: float, actor: str = "terminal",
                  lang: str = "en", shown: str | None = None) -> dict:
    """Decide on one lesson. `shown`, when given, is the fingerprint of the sentence the owner saw: if the
    lesson reads differently now (a pass changed it), nothing changes and the result says so."""
    if action not in ACTIONS:
        raise ValueError("Decide with approve, reject, retire or reactivate")
    row = conn.execute("SELECT * FROM lesson WHERE id=?", (lesson_id,)).fetchone()
    if row is None:
        raise ValueError(f"Unknown lesson id {lesson_id}")
    if shown is not None and fingerprint(playbook.render(row, lang)) != shown:
        return {"id": lesson_id, "decided": False, "reason": "changed"}
    allowed = {"approve": WAITING, "reject": WAITING, "retire": ("active",), "reactivate": ("retired",)}[action]
    withdrawn = row["status"] == "retired" and row["verdict"] == playbook.WITHDRAWN
    if action in ("reject", "approve") and withdrawn:
        allowed = ("retired",)   # taken back by the queue cleaning: nobody decided on it yet, the owner may now
    elif action == "reactivate" and withdrawn:
        allowed = ()   # nobody decided on it: that is an approval
    if row["status"] not in allowed:
        return {"id": lesson_id, "decided": False, "reason": "not_" + "_or_".join(allowed or ("allowed",)),
                "status": row["status"]}
    before = _state(row)
    target = "active" if action in ("approve", "reactivate") else "retired"
    playbook.set_status(conn, lesson_id, target, now=now)
    settled = []
    if action == "approve":
        settled = _settle(conn, f"lesson:{lesson_id}", "applied", now)
    elif action == "reject":
        settled = _settle(conn, f"lesson:{lesson_id}", "rejected", now)
    elif action == "retire":
        settled = _settle(conn, f"lesson_retire:{lesson_id}", "applied", now)
    after = {**_state(conn.execute("SELECT * FROM lesson WHERE id=?", (lesson_id,)).fetchone()), "settled": settled}
    decision = _log(conn, now=now, actor=actor, kind="lesson", target=str(lesson_id), action=action, before=before,
                    after=after)
    return {"id": lesson_id, "decided": True, "status": target, "previous": before["status"], "decision": decision}


def approve_all(conn: sqlite3.Connection, shown: dict, *, now: float, actor: str = "terminal", lang: str = "en") -> dict:
    """Approve every lesson in `shown` ({lesson id: fingerprint of the sentence shown}) that still waits with that
    sentence. A lesson the agent proposed is skipped: it is approved on its own, after reading it."""
    approved, skipped = [], []
    for raw_id, mark in shown.items():
        try:
            lesson_id = int(raw_id)
        except (TypeError, ValueError):
            skipped.append({"id": raw_id, "reason": "invalid"})
            continue
        row = conn.execute("SELECT origin FROM lesson WHERE id=?", (lesson_id,)).fetchone()
        if row is None:
            skipped.append({"id": lesson_id, "reason": "unknown"})
            continue
        if row["origin"] == "agent":
            skipped.append({"id": lesson_id, "reason": "agent_needs_single"})
            continue
        result = decide_lesson(conn, lesson_id, "approve", now=now, actor=actor, lang=lang,
                               shown=mark if isinstance(mark, str) else "")
        (approved.append(lesson_id) if result["decided"] else skipped.append({"id": lesson_id, "reason": result["reason"]}))
    return {"approved": approved, "skipped": skipped}


def decide_proposal(conn: sqlite3.Connection, proposal_id: int, action: str, *, now: float, actor: str = "terminal",
                    lang: str = "en", note: str | None = None) -> dict:
    """Approve or reject a proposal. A lesson proposal decides its lesson (and is logged as that decision); any
    other proposal records the owner's answer and returns the command that applies it, which Hermes runs."""
    from . import proposals

    if action not in ("approve", "reject"):
        raise ValueError("Decide with approve or reject")
    row = conn.execute("SELECT * FROM proposal WHERE id=?", (proposal_id,)).fetchone()
    if row is None:
        raise ValueError(f"Unknown proposal id {proposal_id}")
    evidence = json.loads(row["evidence"])
    lesson_id = evidence.get("lesson_id") if isinstance(evidence, dict) else None
    lesson = conn.execute("SELECT * FROM lesson WHERE id=?", (lesson_id,)).fetchone() \
        if row["kind"] in ("lesson", "lesson_retire") and isinstance(lesson_id, int) else None
    before_lesson = _state(lesson) if lesson is not None else None
    result = proposals.decide(conn, proposal_id, action, now=now, note=note)
    if result["status"] == "rejected" and str(result.get("code") or "").startswith("env:"):
        _share_skip(conn, result["code"], add=True)   # whichever profile looks after the Hermes next keeps it
    after_lesson = _state(conn.execute("SELECT * FROM lesson WHERE id=?", (lesson_id,)).fetchone()) \
        if lesson is not None else None
    if before_lesson is not None and after_lesson != before_lesson:
        # the proposal changed its lesson: logged as that lesson's decision, so undoing it restores both
        kind = ("approve" if before_lesson["status"] in WAITING else "reactivate") if after_lesson["status"] == "active" \
            else ("reject" if before_lesson["status"] in WAITING else "retire")
        decision = _log(conn, now=now, actor=actor, kind="lesson", target=str(lesson_id), action=kind,
                        before=before_lesson, after={**after_lesson, "settled": [row["code"]]})
    else:
        decision = _log(conn, now=now, actor=actor, kind="proposal", target=str(proposal_id), action=action,
                        before={"status": row["status"]},
                        after={"status": result["status"], **({"command": result["run_this"]}
                                                               if isinstance(result.get("run_this"), str)
                                                               and result["run_this"] else {})})
    return {**result, "decided": True, "decision": decision}


def _home_of(conn: sqlite3.Connection):
    """The profile's home, from this connection's own database file (<home>/evolution/evolution.sqlite3), or None."""
    from pathlib import Path

    try:
        found = conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        return None
    return Path(found[2]).parent.parent if found and found[2] else None


def _share_skip(conn: sqlite3.Connection, code: str, *, add: bool) -> None:
    """Add or lift a change across profiles in the skip list every profile of this Hermes reads (environment.state).
    The profile's home is found from this connection's own database file. Never fails the decision."""
    try:
        from .environment import forget_skip, remember, root_of

        home = _home_of(conn)
        if home is None:
            return
        root = root_of(home)
        remember(root, skip=[code]) if add else forget_skip(root, code)
    except Exception:
        pass


def _later_decision(conn: sqlite3.Connection, row) -> int | None:
    """A later decision on the same proposal that still stands: only the newest one may be undone, so undoing an
    older "no" never reopens what the owner refused again since."""
    if row["kind"] != "proposal":
        return None
    later = conn.execute("SELECT id FROM decision_log WHERE kind='proposal' AND target=? AND id > ? AND undone IS NULL "
                         "ORDER BY id DESC LIMIT 1", (row["target"], row["id"])).fetchone()
    return later[0] if later else None


def _gone(conn: sqlite3.Connection, row, before: dict, after: dict) -> bool:
    """Whether what an undo needs is gone for good: the copy of a memory file a removal kept (the newest 100 are
    kept), or a folder the plugin moved to the trash (deleted 30 days after it entered)."""
    home = _home_of(conn)
    if home is None:
        return False
    try:
        if row["kind"] == "memory":
            from .memory_care import backup_exists

            return not backup_exists(home, before.get("backup"))
        applied = after.get("applied") if row["kind"] == "proposal" else None
        if isinstance(applied, list) and len(applied) == 3 and applied[1] == "__trash__" \
                and str(applied[0]).startswith("@"):
            import os

            from .actions import _trash_places

            _parents, _live, kept = _trash_places(str(applied[2]))
            return not os.path.lexists(home / "evolution" / "trash" / str(applied[0])[1:] / kept)
    except (OSError, ValueError, TypeError):
        return False
    return False


def record_setting(conn: sqlite3.Connection, key: str, before, after, *, now: float, actor: str = "dashboard") -> int:
    """Log a change of one of the plugin's settings (made through Hermes' own settings writer by the caller)."""
    return _log(conn, now=now, actor=actor, kind="setting", target=key, action="set", before={"value": before},
                after={"value": after})


def record_applied(conn: sqlite3.Connection, proposal_id: int, *, code: str, before: str, apply: list, undo: list,
                   now: float, actor: str = "terminal") -> int:
    """Log a proposal the plugin applied with Hermes' command (actions.apply_proposal), with the command that undoes it."""
    return _log(conn, now=now, actor=actor, kind="proposal", target=str(proposal_id), action="apply",
                before={"status": before}, after={"status": "applied", "code": code, "applied": list(apply),
                                                  "undo": list(undo)})


def record_maintenance(conn: sqlite3.Connection, action: str, *, reclaimable, ok: bool, now: float,
                       actor: str = "automatic") -> int:
    """Log Hermes' own maintenance the plugin ran (today `hermes pm gc`). Nothing to undo."""
    return _log(conn, now=now, actor=actor, kind="maintenance", target=action, action="run",
                before={"reclaimable": reclaimable if isinstance(reclaimable, int) else None}, after={"ok": ok})


def record_gateway_repair(conn: sqlite3.Connection, unit: str, *, before: str, after: str, ok: bool, undo: bool,
                          now: float, actor: str = "automatic") -> int:
    """Log the Mode auto pointing the gateway's unit at the Python Hermes uses now (actions.repair_gateway). It is
    undone with `hermes evolution care undo-gateway`, which puts the file back from the copy kept."""
    return _log(conn, now=now, actor=actor, kind="maintenance", target="gateway_repair", action="run",
                before={"unit": str(unit)[:120], "path": str(before)[:300]},
                after={"ok": bool(ok), "path": str(after)[:300], "undo": "hermes evolution care undo-gateway" if undo
                       else None})


def record_cron(conn: sqlite3.Connection, job: str, *, name: str, schedule: str, deliver: str, now: float,
                actor: str = "terminal") -> int:
    """Log a no-agent job the plugin created with `hermes cron create` (undone with `hermes cron remove <job>`)."""
    return _log(conn, now=now, actor=actor, kind="cron", target=name, action="create", before={},
                after={"job": job, "schedule": schedule, "deliver": deliver})


def record_memory(conn: sqlite3.Connection, name: str, mark: str, *, position: int, backup: str, before: int,
                  after: int, copies: int = 1, now: float, actor: str = "terminal", keep: dict | None = None) -> int:
    """Log an entry taken out of a memory file: its fingerprint, where it was, how many copies the file held, the
    copy kept (file and fingerprint) and the copy of the file kept, never its text."""
    return _log(conn, now=now, actor=actor, kind="memory", target=name, action="remove",
                before={"entries": before, "fingerprint": mark, "position": position, "backup": backup,
                        "copies": copies, **({"keep": keep} if isinstance(keep, dict) else {})},
                after={"entries": after})


def history(conn: sqlite3.Connection, *, lang: str = "en", limit: int = 50) -> list[dict]:
    """The latest decisions, newest first, with the current sentence of each lesson they concern."""
    rows = conn.execute("SELECT * FROM decision_log ORDER BY id DESC LIMIT ?",
                        (max(1, min(int(limit), HISTORY_LIMIT)),)).fetchall()
    found = []
    for row in rows:
        item = {"id": row["id"], "at": row["at"], "actor": row["actor"], "kind": row["kind"], "target": row["target"],
                "action": row["action"], "before": json.loads(row["before"]), "after": json.loads(row["after"]),
                "undone": row["undone"]}
        if row["kind"] == "proposal" and isinstance(item["after"].get("applied"), list):
            from .explain import command_text

            item["command"] = command_text(item["after"]["applied"], lang)   # as the owner would type it
        elif row["kind"] == "proposal" and isinstance(item["after"].get("command"), str):
            item["command"] = item["after"]["command"][:400]   # the command an approved manual proposal asks you to run
        from .admin import SETTINGS

        # what the menu and the panel offer to undo: never Hermes' own maintenance (nothing to undo) nor a setting
        # this version no longer has (0.8.3's auto_actions, now `mode`)
        item["undoable"] = row["undone"] is None and row["kind"] != "maintenance" and not (
            row["kind"] == "setting" and row["target"] not in SETTINGS)
        if item["undoable"] and row["kind"] in ("memory", "proposal"):
            # only the newest decision on a proposal, and only while the copy an undo puts back is still kept
            item["undoable"] = _later_decision(conn, row) is None and not _gone(conn, row, item["before"], item["after"])
        if row["kind"] == "lesson":
            lesson = conn.execute("SELECT * FROM lesson WHERE id=?", (int(row["target"]),)).fetchone()
            item["lesson"] = playbook.render(lesson, lang) if lesson is not None else None
            item["origin"] = lesson["origin"] if lesson is not None else None
            item["templated"] = bool(lesson is not None and playbook.templated(lesson))
        found.append(item)
    return found


# Settings whose values name the owner's chat accounts: never shown where someone else may read.
PRIVATE_SETTINGS = ("owner_sender_ids", "owner_platforms")


def masked(history: list[dict], lang: str = "en") -> list[dict]:
    """The history as an agent's terminal or a dashboard without a login may see it: written lesson text is left
    out (only template sentences stay) and the values of the owner's chat accounts become counts."""
    shown = []
    for item in history:
        item = dict(item)
        if item["kind"] == "lesson" and not item.get("templated"):
            item["lesson"], item["text_hidden"] = None, True
        if item["kind"] == "setting" and item["target"] in PRIVATE_SETTINGS:
            item["before"], item["after"] = ({"value": _count(item[side].get("value"), lang)} for side in ("before", "after"))
            item["values_hidden"] = True
        shown.append(item)
    return shown


def _count(value, lang: str = "en") -> str:
    if not isinstance(value, (list, tuple)):
        return "…"
    if lang == "pt":
        return f"{len(value)} {'item' if len(value) == 1 else 'itens'}"
    return f"{len(value)} {'item' if len(value) == 1 else 'items'}"


def mark_undone(conn: sqlite3.Connection, decision_id: int, *, now: float) -> None:
    conn.execute("UPDATE decision_log SET undone=? WHERE id=? AND undone IS NULL", (now, decision_id))


def undo(conn: sqlite3.Connection, decision_id: int, *, now: float, actor: str = "terminal", current=...) -> dict:
    """Take back one decision while what it changed is still as it left it. For a setting, `current` is the value the
    plugin uses now: the undo applies only while it is still the value the decision set, and nothing is marked here
    (`restorable` and the value to write back are returned; the caller writes it, then calls mark_undone)."""
    row = conn.execute("SELECT * FROM decision_log WHERE id=?", (decision_id,)).fetchone()
    if row is None:
        raise ValueError(f"Unknown decision id {decision_id}")
    if row["undone"] is not None:
        return {"id": decision_id, "undone": False, "reason": "already_undone"}
    before, after = json.loads(row["before"]), json.loads(row["after"])
    later = _later_decision(conn, row)
    if later is not None:   # the menus say: "it changed since (a later decision or a pass); nothing changed"
        return {"id": decision_id, "undone": False, "reason": "changed_since", "later_decision": later}
    if row["kind"] in ("memory", "proposal") and _gone(conn, row, before, after):
        return {"id": decision_id, "undone": False, "reason": "changed_since", "purged": True}
    if row["kind"] == "setting":
        if current is ... or current != after.get("value"):
            return {"id": decision_id, "undone": False, "kind": "setting", "key": row["target"],
                    "reason": "changed_since" if current is not ... else "unknown_current"}
        return {"id": decision_id, "undone": False, "restorable": True, "kind": "setting", "key": row["target"],
                "restore": before.get("value")}
    if row["kind"] == "maintenance":
        return {"id": decision_id, "undone": False, "reason": "nothing_to_undo"}
    if row["kind"] == "cron":   # a job the plugin created with Hermes' cron: removed with `hermes cron remove`
        job = str(after.get("job") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job):
            return {"id": decision_id, "undone": False, "reason": "changed_since"}
        return {"id": decision_id, "undone": False, "restorable": True, "kind": "cron_create", "job": job}
    if row["kind"] == "memory":   # the caller puts the entry back (memory_care.restore), then calls mark_undone
        return {"id": decision_id, "undone": False, "restorable": True, "kind": "memory", "file": row["target"],
                "fingerprint": before.get("fingerprint"), "backup": before.get("backup"),
                "position": before.get("position"), "copies": before.get("copies", 1)}
    if row["kind"] == "proposal" and "applied" in after:   # applied with a Hermes command
        from .explain import undo_for

        backward = undo_for(after.get("applied"))   # derived again: what the record says it undoes is never run
        current = conn.execute("SELECT status FROM proposal WHERE id=?", (int(row["target"]),)).fetchone()
        if backward is None or current is None or current["status"] != "applied":
            return {"id": decision_id, "undone": False, "reason": "changed_since"}
        return {"id": decision_id, "undone": False, "restorable": True, "kind": "proposal_apply",
                "proposal": int(row["target"]), "code": str(after.get("code") or "")[:200], "undo": backward}
    if row["kind"] == "proposal":
        current = conn.execute("SELECT status FROM proposal WHERE id=?", (int(row["target"]),)).fetchone()
        if current is None or current["status"] != after.get("status"):
            return {"id": decision_id, "undone": False, "reason": "changed_since"}
        conn.execute("UPDATE proposal SET status=?, updated=? WHERE id=?", (before["status"], now, int(row["target"])))
        conn.execute("UPDATE decision_log SET undone=? WHERE id=?", (now, decision_id))
        if after.get("status") == "rejected":   # the owner takes the "no" back: the Mode auto may apply it again
            code = conn.execute("SELECT code FROM proposal WHERE id=?", (int(row["target"]),)).fetchone()
            if code is not None:
                conn.execute("DELETE FROM cursor WHERE key=?", (f"auto_skip:{code[0]}",))
                if str(code[0]).startswith("env:"):
                    _share_skip(conn, code[0], add=False)
        return {"id": decision_id, "undone": True, "kind": "proposal"}
    lesson_id = int(row["target"])
    current = conn.execute("SELECT * FROM lesson WHERE id=?", (lesson_id,)).fetchone()
    if current is None or current["status"] != after.get("status") or current["activated"] != after.get("activated"):
        return {"id": decision_id, "undone": False, "reason": "changed_since"}
    if before["status"] == "active":   # a retire is undone by activating the lesson again: a new measurement
        playbook.set_status(conn, lesson_id, "active", now=now)
    elif before["status"] == "retired" and after.get("status") == "active":   # an undone reactivation
        playbook.set_status(conn, lesson_id, "retired", now=now)
    else:   # back to waiting, as it was before the approval or the rejection
        if after.get("status") == "active" and after.get("activated") is not None:
            # the approval started a measurement that never ran: its tracked change and activation counts go too
            conn.execute("DELETE FROM change WHERE ref=?", (f"lesson:{lesson_id}:{int(after['activated'])}",))
            conn.execute("DELETE FROM lesson_activation WHERE lesson_id=? AND activated=?", (lesson_id, after["activated"]))
        conn.execute("UPDATE lesson SET status=?, verdict=?, activated=?, updated=? WHERE id=?",
                     (before["status"], before.get("verdict"), before.get("activated"), now, lesson_id))
    for code in after.get("settled") or ():
        conn.execute("UPDATE proposal SET status='open', updated=? WHERE code=?", (now, code))
    conn.execute("UPDATE decision_log SET undone=? WHERE id=?", (now, decision_id))
    return {"id": decision_id, "undone": True, "kind": "lesson", "lesson": lesson_id, "status": before["status"]}
