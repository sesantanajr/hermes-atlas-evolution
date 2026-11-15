"""The evolution cycle: signals -> lessons -> measurement -> proposals.

`cycle` is safe to run on a timer. It reads Hermes' own records, updates the plugin's experience store
and never changes Hermes. Its only effect outside that store is the playbook the agent may see, and only
when the operator selected a lessons mode that allows it.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from . import experiments, lessons, logwatch, proposals, signals
from .experience import day_offset, experience, prune, utc_day
from .skill_health import read_usage, skill_health

DEFAULTS = {"lessons": "suggest", "tool_hints": True, "feedback_signals": True, "language": "auto",
            "slow_turn_seconds": 60, "owner_sender_ids": ()}


def _language(value) -> str:
    if value in lessons.LANGUAGES:
        return value
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        locale = os.environ.get(name, "")
        if locale:
            return "pt" if locale.lower().startswith("pt") else "en"
    return "en"


def options(config) -> dict:
    """Validated evolution settings. Unknown or malformed values fall back to the safe default."""
    raw = config.get("evolution") if isinstance(config, dict) and isinstance(config.get("evolution"), dict) else {}
    mode = raw.get("lessons", DEFAULTS["lessons"])
    if mode not in lessons.MODES:
        raise ValueError("evolution.lessons must be off, suggest or auto")
    slow = raw.get("slow_turn_seconds", DEFAULTS["slow_turn_seconds"])
    if type(slow) is not int or not 5 <= slow <= 3600:
        raise ValueError("evolution.slow_turn_seconds must be an integer from 5 to 3600")
    owners = raw.get("owner_sender_ids", ())
    if not isinstance(owners, (list, tuple)) or len(owners) > 20 or any(
            not isinstance(item, str) or not 0 < len(item) <= 200 for item in owners):
        raise ValueError("evolution.owner_sender_ids must list up to 20 sender ids")
    return {"lessons": mode, "tool_hints": raw.get("tool_hints", True) is not False,
            "feedback_signals": raw.get("feedback_signals", True) is not False,
            "language": _language(raw.get("language", "auto")), "slow_turn_seconds": slow,
            "owner_sender_ids": tuple(owners)}


def cycle(home: Path, *, now: float, runtime_config=None, config=None) -> dict:
    """Run one evolution pass and return what changed."""
    from .operations import database

    opts = options(config or {})
    with experience(home) as conn:
        prune(conn, now=now)
        ingested = 0
        for _ in range(8):   # at most 32 MiB of log per pass
            step = logwatch.ingest_reviews(home, conn)
            ingested += step["reviews"]
            if not step.get("more"):
                break
        created = lessons.derive(conn, now=now, mode=opts["lessons"])
        effects = lessons.evaluate(conn, now=now, mode=opts["lessons"])
        skills = skill_health(home, now=now, days=7)
        detected = experiments.detect_changes(conn, home, runtime_config, now=now)
        try:
            with database(home) as ops:
                verdicts = experiments.evaluate_changes(conn, ops, now=now)
        except (sqlite3.Error, ValueError, OSError):
            verdicts = experiments.evaluate_changes(conn, None, now=now)
        failures = signals.failure_summary(conn, now=now, days=7)
        turns = signals.turn_summary(conn, now=now, days=7, surfaces=("local", "gateway"))
        reviews = logwatch.review_summary(conn, start_day=day_offset(now, 7), end_day=utc_day(now + 86_400))
        usage = read_usage(home)
        closed = 0
        for row in conn.execute("SELECT id, subject FROM proposal WHERE status='approved' AND kind='skill_lock'").fetchall():
            record = usage.get(row["subject"])
            if record and (record["locked"] or record["pinned"]):
                conn.execute("UPDATE proposal SET status='applied', updated=? WHERE id=?", (now, row["id"]))
                closed += 1
        pending = proposals.refresh(conn, now=now, skills=skills, reviews=reviews, failures=failures, turns=turns,
                                    changes=verdicts)
        new_lessons = [item for item in lessons.listing(conn, lang=opts["language"]) if item["id"] in created]
    return {"lessons_mode": opts["lessons"], "new_lessons": new_lessons,
            "lesson_effects": [item for item in effects if item["verdict"]],
            "changes_detected": detected, "change_verdicts": verdicts, "reviews_ingested": ingested,
            "proposals_closed_as_applied": closed, "open_proposals": pending}


def notify_view(result: dict) -> dict | None:
    """What a quiet scheduled run should print: only new lessons, regressions and high-risk proposals."""
    regressions = [item for item in result["change_verdicts"] if item["verdict"] == "regressed"]
    retired = [item for item in result["lesson_effects"] if item["retired"]]
    urgent = [item for item in result["open_proposals"] if item["risk"] == "high"]
    if not (result["new_lessons"] or regressions or retired or urgent):
        return None
    return {"new_lessons": result["new_lessons"], "regressions": regressions, "lessons_retired": retired,
            "high_risk_proposals": urgent}
