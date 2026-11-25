"""The evolution cycle: signals -> lessons -> measurement -> proposals.

`cycle` is safe to run on a timer. It reads Hermes' own records, updates the plugin's experience store
and never changes Hermes. Its only effect outside that store is the playbook the agent may see, and only
when the operator selected a lessons mode that allows it.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from . import experiments, lessons, logwatch, proposals, reflector, signals
from .i18n import resolve
from .experience import day_offset, experience, utc_day
from .skill_health import read_usage, skill_health, skill_outcomes

# Set in a runtime configuration that could not be read (a standalone run without a YAML parser): the settings
# that only the Hermes configuration holds, who the owner is among them, are unknown there, not empty.
CONFIG_UNREAD = "_atlas_evolution_config_unread"

DEFAULTS = {"lessons": "auto", "tool_hints": False, "feedback_signals": True, "language": "auto",
            "slow_turn_seconds": 60, "owner_sender_ids": (), "owner_platforms": (), "reflector": True, "reflector_daily_limit": 6,
            "autopilot_minutes": 60, "runtime_guard": True, "mode": "assist", "fleet_managed": True, "overlay_pins": ()}
# The 0.8.3 setting `auto_actions` maps on the mode when `mode` is not set (an owner who chose it keeps it).
LEGACY_ACTIONS = {"off": "manual", "safe": "assist", "full": "auto"}
MODE_ACTIONS = {"auto": "full", "assist": "safe", "manual": "off"}


def _whole(value):
    """An integer, also when a settings form stored it as 60.0."""
    if type(value) is float and value.is_integer():
        return int(value)
    return value


def options(config, runtime_config=None, *, strict: bool = True, problems: list | None = None,
            home: Path | None = None) -> dict:
    """Validated evolution settings.

    strict (the CLI): an invalid value is an error the operator sees. Lenient (inside the gateway): each
    invalid value falls back on its own, and an invalid `lessons` value means `off`, so a typo can never
    switch on what the operator switched off. `home`, when given, lets `fleet_managed` be read exactly as the lead
    profile reads it (environment.fleet_managed: an opt-out in settings.json counts even over config.yaml)."""
    raw = config.get("evolution") if isinstance(config, dict) and isinstance(config.get("evolution"), dict) else {}
    problems = [] if problems is None else problems

    def checked(key, value, valid, message, fallback):
        if valid(value):
            return value
        if strict:
            raise ValueError(message)
        problems.append(key)
        return fallback

    chosen_mode = raw.get("lessons", DEFAULTS["lessons"])
    if chosen_mode is False:   # YAML reads a bare `off` as false, and so does `hermes config set ... lessons off`
        chosen_mode = "off"
    mode = checked("lessons", chosen_mode, lambda value: value in lessons.MODES,
                   "evolution.lessons must be off, suggest or auto", "off")
    slow = checked("slow_turn_seconds", _whole(raw.get("slow_turn_seconds", DEFAULTS["slow_turn_seconds"])),
                   lambda value: type(value) is int and 5 <= value <= 3600,
                   "evolution.slow_turn_seconds must be an integer from 5 to 3600", DEFAULTS["slow_turn_seconds"])
    # A settings form may store a Telegram id typed as a number: ids are compared as text.
    owners = checked("owner_sender_ids", raw.get("owner_sender_ids", ()),
                     lambda value: isinstance(value, (list, tuple)) and len(value) <= 20 and all(
                         isinstance(item, str) and 0 < len(item) <= 200 or type(item) is int and item >= 0
                         for item in value),
                     "evolution.owner_sender_ids must list up to 20 sender ids", ())
    # Platforms where every chat is the owner's: a private bot, the owner's own UI through the API server.
    platforms = checked("owner_platforms", raw.get("owner_platforms", ()),
                        lambda value: isinstance(value, (list, tuple)) and len(value) <= 20 and all(
                            isinstance(item, str) and re.fullmatch(r"[a-z][a-z0-9_.-]{0,47}", item.strip().lower())
                            for item in value),
                        "evolution.owner_platforms must list up to 20 platform names such as telegram or api_server", ())
    daily = checked("reflector_daily_limit", _whole(raw.get("reflector_daily_limit", DEFAULTS["reflector_daily_limit"])),
                    lambda value: type(value) is int and 1 <= value <= 50,
                    "evolution.reflector_daily_limit must be an integer from 1 to 50", DEFAULTS["reflector_daily_limit"])
    # On by default; an invalid value switches it off, never on.
    reflect = checked("reflector", raw.get("reflector", DEFAULTS["reflector"]), lambda value: type(value) is bool,
                      "evolution.reflector must be true or false", False)
    autopilot = checked("autopilot_minutes", _whole(raw.get("autopilot_minutes", DEFAULTS["autopilot_minutes"])),
                        lambda value: type(value) is int and (value == 0 or 15 <= value <= 1440),
                        "evolution.autopilot_minutes must be 0 (manual only) or an integer from 15 to 1440", 0)
    # The operating mode. assist (the default since 0.13.0): only identical memory copies by itself, everything else
    # waits for the owner; auto (the owner's choice): the plugin looks at every profile of this Hermes and applies by
    # itself what is safe and reversible, the rest waits; manual: it only measures and proposes. An invalid value means
    # manual: nothing runs by itself.
    if "mode" in raw:
        chosen = raw.get("mode")
    elif "auto_actions" in raw:
        legacy = raw.get("auto_actions")
        chosen = LEGACY_ACTIONS.get("off" if legacy is False else legacy, legacy)
    else:
        chosen = DEFAULTS["mode"]
    operating = checked("mode", chosen, lambda value: value in MODE_ACTIONS,
                        "evolution.mode must be auto, assist or manual", "manual")
    # config.yaml could not be read (a Python without PyYAML, a file too large, a YAML error): the mode the owner
    # chose there is unknown, so nothing runs by itself. `config_unread` lets status say why.
    unread = isinstance(runtime_config, dict) and bool(runtime_config.get(CONFIG_UNREAD))
    if unread:
        operating = "manual"
    automatic = MODE_ACTIONS[operating]
    # A protection: an invalid value keeps it on.
    guard = checked("runtime_guard", raw.get("runtime_guard", True), lambda value: type(value) is bool,
                    "evolution.runtime_guard must be true or false", True)
    # 0.14.0: builders of overlays whose pin of Hermes' files the drift check watches (read, never run)
    # each "<builder>" or "<builder>|<guide to rebuild the overlay>" (the guide is only said, never read)
    pins = checked("overlay_pins", raw.get("overlay_pins", ()),
                   lambda value: isinstance(value, (list, tuple)) and len(value) <= 8 and all(
                       isinstance(item, str) and item.startswith(("/", "~")) and 1 < len(item) <= 600
                       and not any(char in item for char in "\x00\n\r`") for item in value),
                   "evolution.overlay_pins must list up to 8 absolute paths", ())
    return {"lessons": mode, "tool_hints": raw.get("tool_hints", False) is True, "runtime_guard": guard,
            "mode": operating, "auto_actions": automatic,
            "reflector": reflect, "reflector_daily_limit": daily, "autopilot_minutes": autopilot,
            "feedback_signals": raw.get("feedback_signals", True) is not False,
            "language": resolve(raw.get("language"), runtime_config), "slow_turn_seconds": slow,
            "owner_sender_ids": tuple(str(item) for item in owners),
            "owner_platforms": tuple(dict.fromkeys(item.strip().lower() for item in platforms)),
            "fleet_managed": _fleet_managed(raw, runtime_config, home, unread),
            "overlay_pins": tuple(_pin_entry(item) for item in pins), "config_unread": unread}


def _pin_entry(item: str) -> str:
    builder, bar, guide = item.partition("|")
    return str(Path(builder.strip()).expanduser()) + (f"|{guide.strip()}" if bar and guide.strip() else "")


def _fleet_managed(raw: dict, runtime_config, home, unread: bool) -> bool:
    """Whether the lead profile may act here, read as the lead reads it (environment.fleet_managed): an opt-out is
    false, "false", "off", "no", "0" or 0, in config.yaml or in settings.json (which can only narrow it), and a
    config.yaml that was not read (CONFIG_UNREAD) counts as an opt-out it may hold, with or without `home`. Without
    `home`, from the merged settings only."""
    value = raw.get("fleet_managed", True)
    if home is None and not isinstance(value, str):   # the common case, without importing environment on a hook path
        return not (unread or value is False or type(value) is int and value == 0)
    from .environment import _OFF_WORDS, fleet_managed

    if home is not None:   # an unread config.yaml reads as None there: an opt-out it may hold, so not managed
        config = runtime_config if isinstance(runtime_config, dict) and not unread else None
        return fleet_managed(Path(home), read_config=lambda _home: config)
    return not (unread or value.strip().lower() in _OFF_WORDS)


class Cancelled(Exception):
    """The pass was told to stop at a safe point (`cancelled`: the plugin unloaded, or another process holds the pass
    now that its lease ran out). Each phase before it was committed whole, and the next pass does them all again."""


def _stop_if(cancelled) -> None:
    if cancelled is not None and cancelled():
        raise Cancelled()


def cycle(home: Path, *, now: float, runtime_config=None, config=None, lang: str | None = None, reflect=None,
          strict: bool = True, backfill_budget_s: float = 20.0, deadline: float | None = None, cancelled=None,
          reflector_reload: bool = False) -> dict:
    """Run one evolution pass and return what changed. `lang` overrides the configured language of the output.

    `reflect(instructions, text)` asks a model for a lesson draft; it is used only when the reflector is on.
    `reflector_reload`: inside Hermes, the plugin loaded with the reflector off, so its model access exists only
    after the next load (the reflector's status says so instead of "not available outside Hermes").
    `strict=False` (the autopilot) lets each invalid setting fall back safely instead of failing the pass.
    Until it is done, each pass first replays up to `backfill_budget_s` seconds of Hermes' own history.
    `cancelled()` is asked between phases (and inside the replay): when it says so, the pass raises Cancelled there,
    after the last phase committed whole, and leaves the rest to the next pass."""
    from . import backfill
    from .operations import _prune as prune_all, database

    opts = options(config or {}, runtime_config, strict=strict)
    if lang in lessons.LANGUAGES:
        opts["language"] = lang
    import time as _time

    phases: dict[str, float] = {}   # seconds per phase (0.14.0): what a pass costs, for `status` and the panel
    lap = [_time.monotonic()]

    def timed(name: str) -> None:
        moment = _time.monotonic()
        phases[name] = round(phases.get(name, 0.0) + moment - lap[0], 3)
        lap[0] = moment

    backfill.mark_observed(home, now=now)
    budget = backfill_budget_s if deadline is None else max(0.0, min(backfill_budget_s, deadline - _time.monotonic() - 30))
    owner_known = not (isinstance(runtime_config, dict) and runtime_config.get(CONFIG_UNREAD))
    history = backfill.step(home, now=now, budget_s=budget, owner_sender_ids=opts["owner_sender_ids"],
                            owner_platforms=opts["owner_platforms"], owner_known=owner_known, cancelled=cancelled,
                            sample=opts["reflector"] and opts["lessons"] != "off", slow_ms=opts["slow_turn_seconds"] * 1000)
    timed("history")
    _stop_if(cancelled)
    fleet = _environment(home, now=now, runtime_config=runtime_config, deadline=deadline)
    timed("environment")
    _stop_if(cancelled)
    with experience(home) as conn:
        # Short transactions: hook writers of a running gateway share this database, so no phase keeps the
        # write lock while it reads files or waits for a model.
        prune_all(conn, now)   # the experience tables and the hook counters: hook writes only insert
        conn.commit()
        ingested = 0
        for _ in range(8):   # at most 32 MiB of log per pass
            step = logwatch.ingest_reviews(home, conn, since_day=logwatch.local_days(now, backfill.BACKFILL_DAYS)[0])
            conn.commit()
            ingested += step["reviews"]
            if not step.get("more"):
                break
        created = lessons.derive(conn, now=now, mode=opts["lessons"])
        promoted = lessons.promote(conn, now=now, mode=opts["lessons"])
        # Only once the history is replayed: while it is, failure totals are still being counted (or recounted).
        withdrawn = lessons.tidy(conn, now=now) if history.get("status") in backfill.SETTLED else 0
        conn.commit()
        timed("lessons")
        _stop_if(cancelled)
        if not opts["reflector"]:
            conn.execute("DELETE FROM failure_sample")   # nothing waits for a reflector that is off
            reflection = {"status": "off"}
        elif opts["lessons"] == "off":
            reflector.prune_samples(conn, now=now)   # measure-only mode spends nothing on drafts; excerpts still expire
            reflection = {"status": "lessons_off"}
        elif not callable(reflect):
            # Run outside Hermes (python -m): no model access here; or inside a Hermes that loaded the plugin with the
            # reflector off, until its next load. Excerpts wait for a run that has it and expire after 14 days.
            reflector.prune_samples(conn, now=now)
            reflection = {"status": "needs_restart" if reflector_reload else "unavailable_without_hermes_llm"}
        else:
            reflection = reflector.reflect(conn, reflect, now=now, lang=opts["language"],
                                           daily_limit=opts["reflector_daily_limit"], deadline=deadline)
        conn.commit()
        timed("reflector")
        _stop_if(cancelled)
        # Baselines wait for history the backfill has not settled yet: still replaying ("running"), before the mark
        # ("waiting"), or a pass that could not read it ("unreadable"), so none is frozen from part of the history.
        pending = backfill.replay_until(home) if history.get("status") not in backfill.SETTLED else None
        effects = lessons.evaluate(conn, now=now, mode=opts["lessons"], history_until=pending)
        conn.commit()   # the skill files and the Curator ledger are read with no write lock held
        _stop_if(cancelled)
        skills = skill_health(home, now=now, days=7)
        detected = experiments.detect_changes(conn, home, runtime_config, now=now)
        conn.commit()
        try:
            with database(home) as ops:
                verdicts = experiments.evaluate_changes(conn, ops, now=now)
        except (sqlite3.Error, ValueError, OSError):
            verdicts = experiments.evaluate_changes(conn, None, now=now)
        conn.commit()
        _stop_if(cancelled)
        # Every recurring failure that could warrant an investigation, not only the week's most frequent ones: a
        # proposal must not expire and come back as the ranking moves.
        failures = proposals.investigate_candidates(conn, now=now, days=7)
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
        from . import radar

        from .actions import recover

        recover(conn)   # before the refresh: a claim the loop did not write itself expires there
        conn.commit()   # Hermes' state.db is read with no write lock held on this database
        _stop_if(cancelled)
        reading = radar.scan(home, conn, now=now, own_transform=opts["tool_hints"] and opts["lessons"] != "off")
        pending = proposals.refresh(conn, now=now, skills=skills, reviews=reviews, failures=failures, turns=turns,
                                    changes=verdicts, outcomes=outcomes,
                                    radar=reading["findings"] + list((fleet or {}).get("findings") or ()),
                                    lang=opts["language"], home=home)
        fresh = set(created) | set(promoted)
        new_lessons = [item for item in lessons.listing(conn, lang=opts["language"]) if item["id"] in fresh]
    timed("measure")
    from . import actions

    _stop_if(cancelled)
    automatic = actions.automatic(home, now=now, mode=opts["auto_actions"], runtime_config=runtime_config, usage=usage,
                                  deadline=deadline)
    timed("actions")
    _stop_if(cancelled)
    care = _daily_checks(home, now=now, deadline=deadline)
    timed("care")
    _stop_if(cancelled)
    health = _fleet(home, now=now, deadline=deadline, pins=opts["overlay_pins"])
    shared = _share(home, now=now) if opts["auto_actions"] == "full" else []
    timed("fleet")
    index = None
    try:   # after the health checks: today's index counts what they found
        from . import evolution_index

        with experience(home) as conn:
            index = evolution_index.update(conn, now=now)
    except (sqlite3.Error, ValueError, TypeError) as error:
        from logging import getLogger

        getLogger(__name__).info("Atlas Evolution could not update the evolution index: %s", type(error).__name__)
    if automatic["applied"]:
        with experience(home) as conn:
            pending = proposals.listing(conn, status="open")
    return {"automatic": automatic, "care": care, "evolution_index": index, "phases": phases,
            "fleet": {"findings": list((health or {}).get("signals_findings") or ())
                      + list(((health or {}).get("drift") or {}).get("findings") or ())} if health else None,
            "lessons_shared": shared,
            "environment": (fleet or {}).get("summary"), "now": now, "lessons_mode": opts["lessons"], "history": history, "new_lessons": new_lessons,
            "lessons_promoted": len(promoted), "lessons_withdrawn": withdrawn, "reflector": reflection,
            "lesson_effects": [item for item in effects if item["verdict"]],
            "changes_detected": detected, "change_verdicts": verdicts, "reviews_ingested": ingested,
            "proposals_closed_as_applied": closed, "radar": [item["code"] for item in reading["findings"]],
            "open_proposals": pending}


FLEET_SECONDS = 90   # what a pass must have left to read the whole Hermes' health (fleet.refresh)


def _fleet(home: Path, *, now: float, deadline: float | None, pins=()) -> dict | None:
    """In the lead profile: Hermes' warnings in its log, what the last update left behind and the gateway's memory
    (fleet.refresh), within the pass's deadline. None elsewhere, or when it cannot be read now; never fails the pass."""
    import time as _time

    from . import fleet
    from .system_update import Host

    if not (Path(home) / "config.yaml").exists():
        return None
    if deadline is not None and deadline - _time.monotonic() < FLEET_SECONDS:
        return None
    host = Host()
    host.deadline = _time.monotonic() + 60 if deadline is None else min(deadline - 30, _time.monotonic() + 60)
    try:
        with experience(home) as conn:
            return fleet.refresh(home, conn, now=now, host=host, pins=pins)
    except Exception as error:   # the pass never fails for this
        from logging import getLogger

        getLogger(__name__).info("Atlas Evolution could not read the fleet's health: %s", type(error).__name__)
        return None


def _share(home: Path, *, now: float) -> list:
    """Lessons proven in one profile offered to the others (fleet.share_lessons), from the lead in Auto mode."""
    from . import fleet
    from .admin import profile_name
    from .environment import lead, root_of

    try:
        if lead(root_of(Path(home)), now=now) != profile_name(Path(home)):
            return []
        return fleet.share_lessons(home, now=now)
    except Exception as error:
        from logging import getLogger

        getLogger(__name__).info("Atlas Evolution could not share lessons: %s", type(error).__name__)
        return []


def _environment(home: Path, *, now: float, runtime_config=None, deadline: float | None = None) -> dict | None:
    """Every profile of this Hermes (environment.refresh), when this profile looks after them. Never fails the pass."""
    from . import environment

    if not (Path(home) / "config.yaml").is_file():
        return None   # a folder Hermes never configured (a scratch home): nothing to look after
    try:
        return environment.refresh(home, now=now, deadline=deadline)
    except Exception as error:   # MemoryError from a pathological file included: the pass never fails for this
        from logging import getLogger

        getLogger(__name__).warning("Atlas Evolution could not read the other profiles: %s", type(error).__name__)
        return None


# What a pass must have left to start the daily checks: Hermes' (hermes_care.CHECK_BUDGET_SECONDS at most) and then
# the system's, each within the pass' deadline; with less they wait for the next pass.
DAILY_CHECKS_SECONDS = 300


def _daily_checks(home: Path, *, now: float, deadline: float | None) -> dict:
    """Hermes' own health (hermes_care, every 6 hours) and the operating system's (system_update, daily): read-only,
    no model, no network; their findings go to `status` and the menu. Both end by the pass' `deadline`. Never fails
    the pass."""
    import time as _time

    from . import hermes_care, system_update

    found: dict = {}
    if deadline is not None and deadline - _time.monotonic() < DAILY_CHECKS_SECONDS:
        return found
    # Only for a profile Hermes set up (a scratch folder has no config.yaml).
    if not (Path(home) / "config.yaml").exists():
        return found
    try:
        with experience(home) as conn:
            care = hermes_care.cached(conn, home, now=now, deadline=deadline)
            found["hermes"] = care.get("findings") if isinstance(care, dict) else None
        if Path("/etc/os-release").exists():
            with experience(home) as conn:
                system = system_update.cached(conn, home, now=now, deadline=deadline)
                found["system"] = system.get("findings") if isinstance(system, dict) else None
    except (OSError, ValueError, sqlite3.Error, RecursionError) as error:
        found["error"] = type(error).__name__
    return found


def notify_view(result: dict) -> dict | None:
    """What a quiet scheduled run should print, once: new lessons and drafts, regressions whose verdict became
    final in this run, lessons retired in this run, and high-risk proposals opened in this run."""
    regressions = [item for item in result["change_verdicts"] if item["verdict"] == "regressed" and item["final"]]
    retired = [item for item in result["lesson_effects"] if item["retired"]]
    urgent = [item for item in result["open_proposals"] if item["risk"] == "high" and item["created"] == result.get("now")]
    drafts = result.get("reflector", {}).get("drafted") or []
    automatic = result.get("automatic") or {}
    acted = automatic.get("applied") or automatic.get("memory") or automatic.get("failed") \
        or automatic.get("gateway_repair")
    spiking = result.get("failure_spikes") or []   # set by the caller (insights.untold_spikes): each told once a day
    if not (result["new_lessons"] or regressions or retired or urgent or drafts or acted or spiking):
        return None
    view = {"new_lessons": result["new_lessons"], "reflector_drafts": drafts, "regressions": regressions,
            "lessons_retired": retired, "high_risk_proposals": urgent}
    if spiking:
        view["failure_spikes"] = spiking
    if acted:
        view["automatic_actions"] = automatic
    return view


def digest_schedule_spec(home: Path, *, weekday: int = 1, hour: int = 8, days: int = 7, lang: str = "en",
                         deliver: str = "local") -> dict:
    """A no-agent cron recipe that delivers the digest weekly. Nothing is written and no job is created."""
    import re
    import shlex

    from .i18n import text
    from .admin import UNSAFE_ENV, _profile_args

    if type(weekday) is not int or not 0 <= weekday <= 6 or type(hour) is not int or not 0 <= hour <= 23:
        raise ValueError("Use a weekday from 0 (Sunday) to 6 and an hour from 0 to 23")
    if type(days) is not int or not 1 <= days <= 35 or lang not in lessons.LANGUAGES:
        raise ValueError("Use 1 to 35 days and the language en or pt")
    if not isinstance(deliver, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:@+-]{0,119}", deliver):
        raise ValueError("deliver must be a Hermes delivery target such as local or origin")
    profile = Path(home).expanduser().resolve()
    arguments = [*_profile_args(Path(home).expanduser()), "evolution", "report", "--days", str(days), "--lang", lang]
    script_name = "hermes-atlas-evolution-digest.py"
    script = f'''"""Deliver the Atlas Evolution digest without an agent turn."""
import os
import shutil
import subprocess

launcher = shutil.which("hermes")
if not launcher:
    raise SystemExit({text("Hermes CLI is not on the scheduler PATH.", lang)!r})
# the scheduler's environment, without any approval bypass it may carry (--yolo, auto-approve)
env = {{key: value for key, value in os.environ.items() if key.upper() not in {tuple(UNSAFE_ENV)!r}}}
env["HERMES_HOME"] = {str(profile)!r}
try:   # a fresh pass first, best effort: the digest is delivered even when it fails or runs out of time. A plain
    # pass: nobody reads its output here, so it must not mark today's failure spikes as told
    subprocess.run([launcher, *{arguments[:-6] + ["evolution", "learn"]!r}],
                   capture_output=True, text=True, env=env, encoding="utf-8", errors="replace", timeout=300,
                   check=False)
except (OSError, subprocess.TimeoutExpired):
    pass
try:
    result = subprocess.run([launcher, *{arguments!r}], capture_output=True, text=True, env=env,
                            encoding="utf-8", errors="replace", timeout=120, check=False)
    if result.returncode or len(result.stdout) > 65_536:
        raise SystemExit({text("Atlas Evolution digest failed. Run `hermes evolution report` in this profile to inspect.", lang)!r})
except (OSError, subprocess.TimeoutExpired):
    raise SystemExit({text("Atlas Evolution digest could not be produced.", lang)!r}) from None

print(result.stdout.rstrip())
'''
    create_args = ["hermes", *_profile_args(Path(home).expanduser()), "cron", "create", f"0 {hour} * * {weekday}", "--name",
                   "hermes-atlas-evolution-digest", "--no-agent", "--script", script_name, "--deliver", deliver]
    return {"status": "planned", "profile_home": str(profile), "script_name": script_name,
            "script_path": str(profile / "scripts" / script_name), "script_content": script,
            "create_args": create_args,
            "posix_command": f"HERMES_HOME={shlex.quote(str(profile))} {shlex.join(create_args)}"}
