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


def cycle(home: Path, *, now: float, runtime_config=None, config=None, lang: str | None = None) -> dict:
    """Run one evolution pass and return what changed. `lang` overrides the configured language of the output."""
    from .operations import database

    opts = options(config or {})
    if lang in lessons.LANGUAGES:
        opts["language"] = lang
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


def digest_schedule_spec(home: Path, *, weekday: int = 1, hour: int = 8, days: int = 7, lang: str = "en",
                         deliver: str = "local") -> dict:
    """A no-agent cron recipe that delivers the digest weekly. Nothing is written and no job is created."""
    import re
    import shlex

    from .native_actions import _profile_args

    if type(weekday) is not int or not 0 <= weekday <= 6 or type(hour) is not int or not 0 <= hour <= 23:
        raise ValueError("Use a weekday from 0 (Sunday) to 6 and an hour from 0 to 23")
    if type(days) is not int or not 1 <= days <= 35 or lang not in lessons.LANGUAGES:
        raise ValueError("Use 1 to 35 days and the language en or pt")
    if not isinstance(deliver, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:@+-]{0,119}", deliver):
        raise ValueError("deliver must be a Hermes delivery target such as local or origin")
    profile = Path(home).expanduser().resolve()
    arguments = [*_profile_args(profile), "evolution", "report", "--days", str(days), "--lang", lang]
    script_name = "atlas-evolution-digest.py"
    script = f'''"""Deliver the Atlas Evolution digest without an agent turn."""
import os
import shutil
import subprocess

launcher = shutil.which("hermes")
if not launcher:
    raise SystemExit("Hermes CLI is not on the scheduler PATH.")
try:
    learn = subprocess.run([launcher, *{arguments[:-6] + ["evolution", "learn", "--notify-only"]!r}],
                           capture_output=True, text=True, env=dict(os.environ, HERMES_HOME={str(profile)!r}),
                           encoding="utf-8", errors="replace", timeout=120, check=False)
    result = subprocess.run([launcher, *{arguments!r}], capture_output=True, text=True,
                            env=dict(os.environ, HERMES_HOME={str(profile)!r}),
                            encoding="utf-8", errors="replace", timeout=120, check=False)
    if learn.returncode or result.returncode or len(result.stdout) > 65_536:
        raise SystemExit("Atlas Evolution digest failed. Run `hermes evolution report` in this profile to inspect.")
except (OSError, subprocess.TimeoutExpired):
    raise SystemExit("Atlas Evolution digest could not be produced.") from None

print(result.stdout.rstrip())
'''
    create_args = ["hermes", *_profile_args(profile), "cron", "create", f"0 {hour} * * {weekday}", "--name",
                   "atlas-evolution-digest", "--no-agent", "--script", script_name, "--deliver", deliver]
    return {"status": "planned", "profile_home": str(profile), "script_name": script_name,
            "script_path": str(profile / "scripts" / script_name), "script_content": script,
            "create_args": create_args,
            "posix_command": f"HERMES_HOME={shlex.quote(str(profile))} {shlex.join(create_args)}"}
