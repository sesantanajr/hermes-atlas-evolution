"""Skill health from Hermes' own bookkeeping: the usage sidecar and the Curator ledger.

Reads metadata only (names, counters, timestamps and content hashes). Skill bodies are never opened.
Answers the question the native self-improvement loop leaves open: after a skill was rewritten, was the
rewrite ever exercised, and is the skill settling or oscillating?
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

_USAGE_LIMIT = 4 * 1024 * 1024
_LEDGER_LIMIT = 8 * 1024 * 1024
_LEDGER_LINES = 20_000
_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}\Z")
_ENTRY_ID = re.compile(r"[0-9a-f]{8,40}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_MUTATIONS = frozenset({"create", "patch", "edit", "write_file", "remove_file"})
CHURN_PATCHES = 8          # rewrites of one skill inside the window that count as churn
UNREUSED_PATCHES = 5       # rewrites never followed by a use
DEAD_AFTER_DAYS = 21


def _read_bounded(path: Path, limit: int, *, tail: bool = False) -> bytes | None:
    """The plugin's one safe reader (environment.read_bytes): never through a link, never a pipe or a device; a larger
    file is refused, or, with `tail`, read from its last `limit` bytes on (from the first whole line)."""
    from .environment import read_bytes

    return read_bytes(Path(path), limit, part="tail" if tail else "")


def _timestamp(value) -> float | None:
    if not isinstance(value, str) or not 10 <= len(value) <= 40:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.timestamp() if moment.tzinfo is not None else None


def read_usage(home: Path) -> dict[str, dict]:
    """Per-skill counters from `<HOME>/skills/.usage.json`. Unknown shapes yield an empty mapping."""
    body = _read_bounded(Path(home) / "skills" / ".usage.json", _USAGE_LIMIT)
    try:
        data = json.loads(body) if body else {}
    except (ValueError, RecursionError):
        return {}
    result = {}
    for name, record in (data.items() if isinstance(data, dict) else ()):
        if not isinstance(name, str) or not _NAME.fullmatch(name) or not isinstance(record, dict):
            continue

        def count(key):
            value = record.get(key)
            return value if type(value) is int and 0 <= value < 10**9 else 0

        state = record.get("state")
        created_by = record.get("created_by")
        result[name] = {
            "use_count": count("use_count"), "view_count": count("view_count"), "patch_count": count("patch_count"),
            "patch_generation": count("patch_generation"),
            "last_reused_patch_generation": count("last_reused_patch_generation"),
            "pinned": record.get("pinned") is True, "locked": record.get("locked") is True,
            "state": state if state in ("active", "stale", "archived") else "unknown",
            "created_by": created_by if created_by in ("agent", "learn", "installed") else None,
            "last_used": _timestamp(record.get("last_used_at")), "last_patched": _timestamp(record.get("last_patched_at")),
            "created": _timestamp(record.get("created_at")),
        }
    return result


def read_ledger(home: Path, *, since: float = 0.0) -> list[dict]:
    """Mutations from `<HOME>/skills/.curator_ledger.jsonl`, oldest first. Only allowlisted fields are kept."""
    body = _read_bounded(Path(home) / "skills" / ".curator_ledger.jsonl", _LEDGER_LIMIT, tail=True)
    if not body:
        return []
    entries = []
    for line in body.splitlines()[-_LEDGER_LINES:]:
        try:
            raw = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(raw, dict):
            continue
        when = _timestamp(raw.get("ts"))
        skill, action, entry_id = raw.get("skill"), raw.get("action"), raw.get("id")
        if (when is None or when < since or not isinstance(skill, str) or not _NAME.fullmatch(skill)
                or action not in _MUTATIONS or not isinstance(entry_id, str) or not _ENTRY_ID.fullmatch(entry_id)):
            continue

        def state(items):
            pairs = []
            for item in items if isinstance(items, list) else ():
                digest = item.get("sha256") if isinstance(item, dict) else None
                path = item.get("path") if isinstance(item, dict) else None
                if isinstance(digest, str) and _SHA.fullmatch(digest) and isinstance(path, str):
                    pairs.append((os.path.basename(path)[:80], digest))
            return tuple(sorted(pairs))

        actor = raw.get("actor")
        entries.append({"id": entry_id, "at": when, "skill": skill, "action": action,
                        "actor": actor if actor in ("curator", "agent", "user") else "unknown",
                        "before": state(raw.get("before")), "after": state(raw.get("after"))})
    entries.sort(key=lambda item: item["at"])
    return entries


def _flipflops(entries: list[dict]) -> int:
    """Count rewrites that restore content this skill already had earlier in the window (A -> B -> A)."""
    seen: dict[str, list[str]] = defaultdict(list)
    flips = 0
    for entry in entries:
        before = dict(entry["before"])
        for path, digest in entry["after"]:
            history = seen[path]
            if not history and path in before:
                history.append(before[path])
            if digest in history[:-1]:
                flips += 1
            history.append(digest)
    return flips


def skill_health(home: Path, *, now: float, days: int = 7, limit: int = 20) -> dict:
    """Report churn, oscillation, unexercised rewrites and dead weight for the profile's skills."""
    usage = read_usage(home)
    ledger = read_ledger(home, since=now - days * 86_400)
    by_skill: dict[str, list[dict]] = defaultdict(list)
    for entry in ledger:
        by_skill[entry["skill"]].append(entry)
    actors = Counter(entry["actor"] for entry in ledger)
    skills, findings = [], []
    for name in sorted(set(usage) | set(by_skill)):
        record = usage.get(name, {})
        entries = by_skill.get(name, [])
        rewrites = len(entries)
        flips = _flipflops(entries)
        unreused = max(0, record.get("patch_generation", 0) - record.get("last_reused_patch_generation", 0))
        last_used = record.get("last_used")
        idle_days = int((now - last_used) // 86_400) if last_used else None
        item = {"skill": name, "rewrites": rewrites, "rewrites_by_review": sum(e["actor"] == "curator" for e in entries),
                "active_days": len({int(e["at"] // 86_400) for e in entries}), "flipflops": flips,
                "uses_total": record.get("use_count", 0), "rewrites_total": record.get("patch_count", 0),
                "unreused_rewrites": unreused, "idle_days": idle_days, "pinned": record.get("pinned", False),
                "locked": record.get("locked", False), "state": record.get("state", "unknown"),
                "created_by": record.get("created_by"), "last_entry": entries[-1]["id"] if entries else None}
        skills.append(item)
        if item["pinned"] or item["locked"]:
            continue
        if rewrites >= CHURN_PATCHES or flips >= 2:
            findings.append({"code": "skill_churn", "skill": name, "severity": "warning", "rewrites": rewrites,
                             "flipflops": flips, "window_days": days, "last_entry": item["last_entry"]})
        elif unreused >= UNREUSED_PATCHES:
            findings.append({"code": "skill_unexercised_rewrites", "skill": name, "severity": "info",
                             "unreused_rewrites": unreused})
        created = record.get("created")
        if (record.get("created_by") == "agent" and record.get("use_count", 0) == 0 and created
                and now - created >= DEAD_AFTER_DAYS * 86_400 and record.get("state") == "active"):
            findings.append({"code": "skill_never_used", "skill": name, "severity": "info",
                             "age_days": int((now - created) // 86_400)})
    skills.sort(key=lambda item: (-item["rewrites"], -item["flipflops"], -item["unreused_rewrites"], item["skill"]))
    return {"window_days": days, "skills_tracked": len(usage), "rewrites": len(ledger),
            "rewrites_by_actor": dict(actors), "skills_rewritten": len(by_skill),
            "top": [item for item in skills if item["rewrites"] or item["unreused_rewrites"] >= UNREUSED_PATCHES][:limit],
            "findings": findings, "basis": "hermes_skill_usage_sidecar_and_curator_ledger_metadata"}


# ── outcome of the turns that loaded each skill ──────────────────────────────────────────────────
MIN_SKILL_TURNS = 8
_RATES = (("failed", 5.0), ("interrupted", 10.0), ("tool_error_turns", 10.0), ("corrected", 5.0), ("slow", 15.0))


def _rate_row(row) -> dict:
    turns = row["turns"]
    rates = {f"{name}_pct": round(100 * row[name] / turns, 1) if turns else None for name, _ in _RATES}
    return {"turns": turns, **rates, "mean_ms": row["duration_ms"] // turns if turns else None}


def skill_window(conn, skill: str, start_day: str, end_day: str) -> dict:
    row = conn.execute("""SELECT COALESCE(SUM(turns),0) AS turns, COALESCE(SUM(failed),0) AS failed,
          COALESCE(SUM(interrupted),0) AS interrupted, COALESCE(SUM(slow),0) AS slow,
          COALESCE(SUM(tool_error_turns),0) AS tool_error_turns, COALESCE(SUM(corrected),0) AS corrected,
          COALESCE(SUM(duration_ms),0) AS duration_ms FROM skill_outcome_daily
        WHERE skill=? AND day BETWEEN ? AND ?""", (skill, start_day, end_day)).fetchone()
    return _rate_row(row)


def compare_skill_windows(before: dict, after: dict, *, min_turns: int = 5) -> dict:
    """Verdict for a skill change from the turns that loaded the skill before and after it."""
    if before["turns"] < min_turns or after["turns"] < min_turns:
        return {"verdict": "insufficient_data", "better": [], "worse": [], "metrics": {}}
    better, worse, metrics = [], [], {}
    for name, margin in _RATES:
        key = f"{name}_pct"
        old, new = before[key], after[key]
        metrics[key] = {"before": old, "after": new}
        if new - old >= margin and new >= 1.25 * old:
            worse.append(key)
        elif old - new >= margin and new <= 0.8 * old:
            better.append(key)
    verdict = "mixed" if worse and better else "regressed" if worse else "improved" if better else "neutral"
    return {"verdict": verdict, "better": better, "worse": worse, "metrics": metrics}


def skill_outcomes(conn, *, start_day: str, end_day: str, min_turns: int = MIN_SKILL_TURNS, limit: int = 20) -> dict:
    """How the turns that loaded each skill went, next to the profile's interactive turns as a whole."""
    profile = conn.execute("""SELECT COALESCE(SUM(turns),0) AS turns, COALESCE(SUM(failed),0) AS failed,
          COALESCE(SUM(interrupted),0) AS interrupted, COALESCE(SUM(slow),0) AS slow,
          COALESCE(SUM(duration_ms),0) AS duration_ms FROM turn_daily
        WHERE day BETWEEN ? AND ? AND surface IN ('local','gateway')""", (start_day, end_day)).fetchone()
    corrections = conn.execute("""SELECT COALESCE(SUM(count),0) FROM feedback_daily
        WHERE day BETWEEN ? AND ? AND kind IN ('correction','reask')""", (start_day, end_day)).fetchone()[0]
    turns = profile["turns"]
    baseline = {"turns": turns,
                "failed_pct": round(100 * profile["failed"] / turns, 1) if turns else None,
                "interrupted_pct": round(100 * profile["interrupted"] / turns, 1) if turns else None,
                "slow_pct": round(100 * profile["slow"] / turns, 1) if turns else None,
                "corrected_pct": round(100 * corrections / turns, 1) if turns else None}
    rows = conn.execute("""SELECT skill, SUM(turns) AS turns, SUM(failed) AS failed, SUM(interrupted) AS interrupted,
          SUM(slow) AS slow, SUM(tool_error_turns) AS tool_error_turns, SUM(corrected) AS corrected,
          SUM(duration_ms) AS duration_ms FROM skill_outcome_daily WHERE day BETWEEN ? AND ?
        GROUP BY skill HAVING SUM(turns) > 0 ORDER BY SUM(turns) DESC, skill LIMIT 500""", (start_day, end_day)).fetchall()
    skills, findings = [], []
    for row in rows:
        item = {"skill": row["skill"], **_rate_row(row)}
        skills.append(item)
        if item["turns"] < min_turns or not turns:
            continue
        worse = [name for name, margin in (("failed", 5.0), ("interrupted", 10.0), ("corrected", 5.0))
                 if baseline[f"{name}_pct"] is not None
                 and item[f"{name}_pct"] - baseline[f"{name}_pct"] >= margin
                 and item[f"{name}_pct"] >= 1.5 * max(baseline[f"{name}_pct"], 1.0)]
        if worse:
            findings.append({"code": "skill_underperforming", "skill": row["skill"], "severity": "warning",
                             "turns": item["turns"], "worse": [f"{name}_pct" for name in worse],
                             "skill_rates": {f"{name}_pct": item[f"{name}_pct"] for name in worse},
                             "profile_rates": {f"{name}_pct": baseline[f"{name}_pct"] for name in worse}})
    return {"profile": baseline, "skills": skills[:limit], "measured_skills": len(skills), "findings": findings,
            "min_turns": min_turns, "basis": "turns_that_loaded_the_skill_versus_all_interactive_turns"}
