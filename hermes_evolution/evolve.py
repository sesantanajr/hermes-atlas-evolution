"""The evolution cycle: signals -> lessons -> measurement -> proposals.

`cycle` is safe to run on a timer. It reads Hermes' own records, updates the plugin's experience store
and never changes Hermes. Its only effect outside that store is the playbook the agent may see, and only
when the operator selected a lessons mode that allows it.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from . import experiments, lessons, logwatch, proposals, reflector, signals
from .i18n import resolve
from .experience import day_offset, experience, prune, utc_day
from .skill_health import read_usage, skill_health, skill_outcomes

DEFAULTS = {"lessons": "suggest", "tool_hints": True, "feedback_signals": True, "language": "auto",
            "slow_turn_seconds": 60, "owner_sender_ids": (), "reflector": True, "reflector_daily_limit": 6,
            "autopilot_minutes": 60}


def _whole(value):
    """An integer, also when a settings form stored it as 60.0."""
    if type(value) is float and value.is_integer():
        return int(value)
    return value


def options(config, runtime_config=None, *, strict: bool = True, problems: list | None = None) -> dict:
    """Validated evolution settings.

    strict (the CLI): an invalid value is an error the operator sees. Lenient (inside the gateway): each
    invalid value falls back on its own, and an invalid `lessons` value means `off`, so a typo can never
    switch on what the operator switched off."""
    raw = config.get("evolution") if isinstance(config, dict) and isinstance(config.get("evolution"), dict) else {}
    problems = [] if problems is None else problems

    def checked(key, value, valid, message, fallback):
        if valid(value):
            return value
        if strict:
            raise ValueError(message)
        problems.append(key)
        return fallback

    mode = checked("lessons", raw.get("lessons", DEFAULTS["lessons"]), lambda value: value in lessons.MODES,
                   "evolution.lessons must be off, suggest or auto", "off")
    slow = checked("slow_turn_seconds", _whole(raw.get("slow_turn_seconds", DEFAULTS["slow_turn_seconds"])),
                   lambda value: type(value) is int and 5 <= value <= 3600,
                   "evolution.slow_turn_seconds must be an integer from 5 to 3600", DEFAULTS["slow_turn_seconds"])
    owners = checked("owner_sender_ids", raw.get("owner_sender_ids", ()),
                     lambda value: isinstance(value, (list, tuple)) and len(value) <= 20 and all(
                         isinstance(item, str) and 0 < len(item) <= 200 for item in value),
                     "evolution.owner_sender_ids must list up to 20 sender ids", ())
    daily = checked("reflector_daily_limit", _whole(raw.get("reflector_daily_limit", DEFAULTS["reflector_daily_limit"])),
                    lambda value: type(value) is int and 1 <= value <= 50,
                    "evolution.reflector_daily_limit must be an integer from 1 to 50", DEFAULTS["reflector_daily_limit"])
    # On by default; an invalid value switches it off, never on.
    reflect = checked("reflector", raw.get("reflector", DEFAULTS["reflector"]), lambda value: type(value) is bool,
                      "evolution.reflector must be true or false", False)
    autopilot = checked("autopilot_minutes", _whole(raw.get("autopilot_minutes", DEFAULTS["autopilot_minutes"])),
                        lambda value: type(value) is int and (value == 0 or 15 <= value <= 1440),
                        "evolution.autopilot_minutes must be 0 (manual only) or an integer from 15 to 1440", 0)
    return {"lessons": mode, "tool_hints": raw.get("tool_hints", True) is not False,
            "reflector": reflect, "reflector_daily_limit": daily, "autopilot_minutes": autopilot,
            "feedback_signals": raw.get("feedback_signals", True) is not False,
            "language": resolve(raw.get("language"), runtime_config), "slow_turn_seconds": slow,
            "owner_sender_ids": tuple(owners)}


def cycle(home: Path, *, now: float, runtime_config=None, config=None, lang: str | None = None, reflect=None,
          strict: bool = True) -> dict:
    """Run one evolution pass and return what changed. `lang` overrides the configured language of the output.

    `reflect(instructions, text)` asks a model for a lesson draft; it is used only when the reflector is on.
    `strict=False` (the autopilot) lets each invalid setting fall back safely instead of failing the pass."""
    from .operations import database

    opts = options(config or {}, runtime_config, strict=strict)
    if lang in lessons.LANGUAGES:
        opts["language"] = lang
    with experience(home) as conn:
        # Short transactions: hook writers of a running gateway share this database, so no phase keeps the
        # write lock while it reads files or waits for a model.
        prune(conn, now=now)
        conn.commit()
        ingested = 0
        for _ in range(8):   # at most 32 MiB of log per pass
            step = logwatch.ingest_reviews(home, conn)
            conn.commit()
            ingested += step["reviews"]
            if not step.get("more"):
                break
        created = lessons.derive(conn, now=now, mode=opts["lessons"])
        conn.commit()
        if not opts["reflector"]:
            conn.execute("DELETE FROM failure_sample")   # nothing waits for a reflector that is off
            reflection = {"status": "off"}
        elif opts["lessons"] == "off":
            reflector.prune_samples(conn, now=now)   # measure-only mode spends nothing on drafts; excerpts still expire
            reflection = {"status": "lessons_off"}
        elif not callable(reflect):
            # Run outside Hermes (python -m): no model access here. Excerpts wait for a run inside Hermes and
            # expire after 14 days.
            reflector.prune_samples(conn, now=now)
            reflection = {"status": "unavailable_without_hermes_llm"}
        else:
            reflection = reflector.reflect(conn, reflect, now=now, lang=opts["language"],
                                           daily_limit=opts["reflector_daily_limit"])
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
        start_day, end_day = logwatch.local_days(now, 7)
        reviews = logwatch.review_summary(conn, start_day=start_day, end_day=end_day)
        usage = read_usage(home)
        closed = 0
        for row in conn.execute("SELECT id, subject FROM proposal WHERE status='approved' AND kind='skill_lock'").fetchall():
            record = usage.get(row["subject"])
            if record and (record["locked"] or record["pinned"]):
                conn.execute("UPDATE proposal SET status='applied', updated=? WHERE id=?", (now, row["id"]))
                closed += 1
        outcomes = skill_outcomes(conn, start_day=day_offset(now, 13), end_day=utc_day(now))
        pending = proposals.refresh(conn, now=now, skills=skills, reviews=reviews, failures=failures, turns=turns,
                                    changes=verdicts, outcomes=outcomes, lang=opts["language"])
        new_lessons = [item for item in lessons.listing(conn, lang=opts["language"]) if item["id"] in created]
    return {"now": now, "lessons_mode": opts["lessons"], "new_lessons": new_lessons, "reflector": reflection,
            "lesson_effects": [item for item in effects if item["verdict"]],
            "changes_detected": detected, "change_verdicts": verdicts, "reviews_ingested": ingested,
            "proposals_closed_as_applied": closed, "open_proposals": pending}


def notify_view(result: dict) -> dict | None:
    """What a quiet scheduled run should print, once: new lessons and drafts, regressions whose verdict became
    final in this run, lessons retired in this run, and high-risk proposals opened in this run."""
    regressions = [item for item in result["change_verdicts"] if item["verdict"] == "regressed" and item["final"]]
    retired = [item for item in result["lesson_effects"] if item["retired"]]
    urgent = [item for item in result["open_proposals"] if item["risk"] == "high" and item["created"] == result.get("now")]
    drafts = result.get("reflector", {}).get("drafted") or []
    if not (result["new_lessons"] or regressions or retired or urgent or drafts):
        return None
    return {"new_lessons": result["new_lessons"], "reflector_drafts": drafts, "regressions": regressions,
            "lessons_retired": retired, "high_risk_proposals": urgent}


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
