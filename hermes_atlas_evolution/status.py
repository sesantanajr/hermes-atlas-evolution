"""`hermes evolution status`: is the plugin observing, learning and protecting this profile right now?

It reads only the plugin's own store and, for the other profiles of the same Hermes root, how many sessions
they had (read-only), so it can point out a busy profile that does not have the plugin: one gateway serves
many profiles, but each profile loads its own plugins.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import quote

from .experience import experience, utc_day

REPOSITORY = "sesantanajr/hermes-atlas-evolution"
PLUGIN_DIR = "hermes-atlas-evolution"


def plugin_version() -> str:
    manifest = Path(__file__).resolve().parent.parent / "plugin.yaml"
    try:
        match = re.search(r'^version:\s*"?([\d.]+)"?', manifest.read_text(encoding="utf-8"), re.M)
    except OSError:
        match = None
    return match.group(1) if match else "unknown"


def process_kind() -> str:
    """gateway, dashboard or cli: long-running hosts are the ones that can keep old code loaded."""
    words = [os.path.basename(str(item)) for item in sys.argv[:6]]
    if "gateway" in words and "run" in words:
        return "gateway"
    if "dashboard" in words:
        return "dashboard"
    return "cli"


def record_runtime(home: Path, *, now: float, version: str | None = None, pid: int | None = None) -> None:
    """Remember which plugin version a long-running host loaded, so `status` can tell when the code on disk
    is newer than what the gateway runs (`hermes plugins update` does not reload a running gateway)."""
    from .experience import record

    kind = process_kind() if pid is None else "gateway"
    if kind == "cli":
        return
    value = json.dumps({"version": version or plugin_version(), "kind": kind, "since": now})
    key = f"runtime:{os.getpid() if pid is None else int(pid)}"
    record(home, lambda conn: conn.execute("INSERT OR REPLACE INTO cursor VALUES (?, ?)", (key, value)), now=now)


def _alive(pid: int) -> bool | None:
    """True or False on POSIX; None where a liveness probe is not safe (on Windows os.kill would end it)."""
    if os.name != "posix":
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _runtimes(conn, disk: str) -> tuple[list[dict], list[str]]:
    """(live hosts that loaded another version, keys of hosts that are gone)."""
    stale, gone = [], []
    for key, value in conn.execute("SELECT key, value FROM cursor WHERE key LIKE 'runtime:%'").fetchall():
        try:
            pid, data = int(key.split(":", 1)[1]), json.loads(value)
        except (ValueError, TypeError):
            gone.append(key)
            continue
        alive = _alive(pid)
        if alive is False:
            gone.append(key)
        elif alive and isinstance(data, dict) and data.get("version") != disk:
            stale.append({"pid": pid, "kind": data.get("kind"), "version": data.get("version"), "since": _iso(data.get("since"))})
    return stale, gone


def _iso(stamp) -> str | None:
    try:
        from .experience import utc_datetime

        return utc_datetime(float(stamp)).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _sessions(home: Path, *, since: float) -> int | None:
    path = home / "state.db"
    if not path.is_file():
        return None
    try:
        source = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=1)
        try:
            return source.execute("SELECT COUNT(*) FROM sessions WHERE started_at >= ?", (since,)).fetchone()[0]
        finally:
            source.close()
    except sqlite3.Error:
        return None


def chat_senders(home: Path, *, since: float, owners=(), platforms=()) -> list[dict]:
    """Chat channels of this profile since `since`, read-only from its state.db: per origin (telegram,
    api_server, ...) the sessions, their distinct senders with their session counts, and whether the owner
    settings already cover the channel. Local surfaces, automations and subagents are left out."""
    from .signals import surface_of

    path = Path(home) / "state.db"
    if not path.is_file():
        return []
    try:
        source = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=1)
        try:
            rows = source.execute("""SELECT source, user_id, COUNT(*) FROM sessions WHERE started_at >= ?
                GROUP BY source, user_id ORDER BY COUNT(*) DESC LIMIT 400""", (since,)).fetchall()
        finally:
            source.close()
    except sqlite3.Error:
        return []
    owners, platforms = {str(item) for item in owners}, set(platforms)
    channels: dict[str, dict] = {}
    for origin, sender, count in rows:
        name = str(origin or "unknown").lower()[:48]
        if surface_of(name) != "gateway":
            continue
        channel = channels.setdefault(name, {"origin": name, "sessions": 0, "senders": [], "covered": name in platforms})
        channel["sessions"] += count
        listed = sender is not None and str(sender) in owners
        channel["covered"] = channel["covered"] or listed
        channel["senders"].append({"id": None if sender is None else str(sender)[:200], "sessions": count, "owner": listed})
    return sorted(channels.values(), key=lambda item: -item["sessions"])


OWNER_PREFIXES = ("hosted-owner:",)   # sender ids a Hermes host gives the owner of a hosted bot room


def owner_suggestions(channels: list[dict], *, owners=(), platforms=()) -> list[dict]:
    """Who is probably the owner, from the chat channels (chat_senders), for the panel to offer with one click.

    Only signals that mean "the owner" by construction are suggested: the API server (only clients holding its
    key write there, such as the owner's own interface) and a sender id a Hermes host marks as the owner
    (`hosted-owner:`). A phone number or a messaging id is never guessed: those are listed for the owner to pick.
    """
    owners, platforms = {str(item) for item in owners}, set(platforms)
    found = []
    for channel in channels:
        origin = channel.get("origin")
        if origin == "api_server" and origin not in platforms:
            found.append({"kind": "platform", "value": origin, "reason": "own_interface", "sessions": channel.get("sessions", 0)})
        for sender in channel.get("senders") or ():
            value = sender.get("id")
            if isinstance(value, str) and value.startswith(OWNER_PREFIXES) and value not in owners:
                found.append({"kind": "sender", "value": value, "reason": "hosted_owner", "channel": origin,
                              "sessions": sender.get("sessions", 0)})
    return found


def _digest_scheduled(home: Path, *, paused_too: bool = True) -> bool:
    """Whether Hermes' scheduler holds the weekly digest of this plugin for the profile (a job the owner paused
    counts: it is theirs to resume, and a second one would deliver twice)."""
    from .workloads import _load_json

    status, data = _load_json(Path(home) / "cron" / "jobs.json", 4 * 1024 * 1024)
    jobs = data.get("jobs") if status == "ok" and isinstance(data, dict) else None
    return any(isinstance(job, dict) and (paused_too or job.get("enabled") is not False)
               and job.get("name") in ("hermes-atlas-evolution-digest", "hermes-evolution-digest")
               for job in jobs or ())


LEGACY_DIRS = ("hermes-evolution",)   # the plugin id used up to 0.5.0


def _command(name: str, rest: str) -> str:
    return f"hermes {'' if name == 'default' else f'-p {name} '}{rest}"


def _profile_state(folder: Path) -> tuple[str, str | None, int, int]:
    """(plugin state, last pass, active lessons, lessons waiting) of a profile, read-only from its own database.

    "both": the new copy is there but so is the old one, which keeps running while the new one stays inactive."""
    new = (folder / "plugins" / PLUGIN_DIR).is_dir()
    old = any((folder / "plugins" / legacy / "plugin.yaml").is_file() and not _is_this_plugin(folder / "plugins" / legacy)
              for legacy in LEGACY_DIRS)
    if new and old:
        state = "both"
    elif new or (folder / "plugins" / LEGACY_DIRS[0]).is_dir() and not old:
        state = "installed"
    elif old:
        state = "legacy"
    else:
        return "missing", None, 0, 0
    path = folder / "evolution" / "evolution.sqlite3"
    if not path.is_file():
        return state, None, 0, 0
    try:
        source = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=1)
        try:
            row = source.execute("SELECT value FROM cursor WHERE key='loop_status'").fetchone()
            counts = dict(source.execute("SELECT status, COUNT(*) FROM lesson GROUP BY status").fetchall())
        finally:
            source.close()
    except sqlite3.Error:
        return state, None, 0, 0
    try:
        last = _iso(json.loads(row[0]).get("at")) if row else None
    except (ValueError, AttributeError):
        last = None
    return state, last, counts.get("active", 0), counts.get("candidate", 0) + counts.get("quarantined", 0)


def _is_this_plugin(folder: Path) -> bool:
    """True when a folder named after the old id holds this version (a clone updated in place)."""
    try:
        manifest = (folder / "plugin.yaml").read_text(encoding="utf-8", errors="replace")[:65_536]
    except OSError:
        return False
    return re.search(rf"^name:\s*[\"']?{re.escape(PLUGIN_DIR)}[\"']?\s*$", manifest, re.M) is not None


def _profiles(home: Path, *, now: float) -> list[dict]:
    """Every profile of the same Hermes root: sessions in 7 days and the plugin's state there.

    One gateway serves many profiles, but each profile loads its own plugins, so the profile you happen to be
    in may not be the one doing the work."""
    home = Path(home).resolve()
    root = home.parent.parent if home.parent.name == "profiles" else home
    candidates = [("default", root)]
    try:
        candidates += [(entry.name, entry) for entry in sorted((root / "profiles").iterdir()) if entry.is_dir()][:200]
    except OSError:
        pass
    rows = []
    for name, folder in candidates:
        state, last, active, waiting = _profile_state(folder)
        rows.append({"profile": name, "current": folder.resolve() == home,
                     "sessions_7d": _sessions(folder, since=now - 7 * 86_400) or 0, "plugin": state,
                     "last_pass": last, "lessons_active": active, "lessons_waiting": waiting,
                     "install": _command(name, f"plugins install {REPOSITORY} --enable"),
                     "migrate": f"{_command(name, 'plugins remove ' + LEGACY_DIRS[0])} && " + (
                         "hermes gateway restart" if state == "both"
                         else _command(name, f"plugins install {REPOSITORY} --enable")),
                     "status": _command(name, "evolution status")})
    return rows


def never_ran(home: Path) -> bool:
    """True when no pass ever finished in this profile (the autopilot has not had its first turn or tick)."""
    path = Path(home) / "evolution" / "evolution.sqlite3"
    if not path.is_file():
        return True
    try:
        source = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=1)
        try:
            return source.execute("SELECT 1 FROM cursor WHERE key='loop_status'").fetchone() is None
        finally:
            source.close()
    except sqlite3.Error:
        return False


# Findings of hermes_care and system_update that `status` and the menu show, as attention codes.
CARE_ATTENTION = ("hermes_gateway_crashed", "os_held_by_plugin", "hermes_gateway_heartbeat_stale", "hermes_gateway_memory_growth", "hermes_state_db_unreadable",
                  "hermes_state_db_damaged", "hermes_state_db_wal_large", "hermes_disk_critical", "hermes_disk_low",
                  "hermes_disk_filling", "hermes_storage_growth", "hermes_memory_low", "hermes_swap_full",
                  "hermes_cpu_saturated", "hermes_unused_tools", "hermes_gateway_not_at_boot",
                  "os_dpkg_interrupted", "os_package_manager_stuck", "os_apt_broken", "os_reboot_required", "os_security_updates", "os_unattended_off",
                  "os_lists_stale", "os_unit_not_enabled")


def _environment(home: Path) -> dict | None:
    """The lead profile's last look at every profile (environment.refresh), read only."""
    try:
        from . import environment

        lead = environment.lead_home(home)
        if lead is None or not (lead / "evolution" / "evolution.sqlite3").is_file():
            return None
        source = sqlite3.connect(f"file:{quote(str(lead / 'evolution' / 'evolution.sqlite3'))}?mode=ro", uri=True, timeout=1)
        try:
            row = source.execute("SELECT value FROM cursor WHERE key='environment'").fetchone()
            found = json.loads(row[0]) if row else None
            if isinstance(found, dict):   # what was applied, undone or dismissed since is not shown again
                found["findings"] = environment.still_open(source, found)
                found["lead_mode"] = environment.profile_mode(lead)
        finally:
            source.close()
    except (OSError, ValueError, sqlite3.Error, RecursionError):
        return None
    return found if isinstance(found, dict) else None


def _doctor(home: Path, environment: dict | None) -> dict:
    """{profile: {at, ok, checks, fail, warn, optional, issues, seconds | error}}: the last run of Hermes' own doctor
    checks in each profile (0.9.0), as the profile that looks after the whole Hermes stored it. Read only; {} when
    there is none yet or it cannot be read."""
    found = (environment or {}).get("doctor")
    if not isinstance(found, dict):
        found = {}
        try:   # an older record without the summary: the raw runs in the lead's database
            from . import doctor, environment as fleet

            lead = fleet.lead_home(home)
            path = lead / "evolution" / "evolution.sqlite3" if lead is not None else None
            if path is not None and path.is_file():
                source = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=1)
                try:
                    runs = doctor.stored(source)
                finally:
                    source.close()
                for name, item in runs.items():
                    if isinstance(item, dict) and isinstance(item.get("result"), dict):
                        found[name] = {"at": item.get("at"), **doctor.summary(item["result"])}
        except Exception:   # ImportError, a damaged record, a busy database: no doctor line
            return {}
    shown = {}
    for name, item in list(found.items())[:200]:
        if isinstance(name, str) and isinstance(item, dict):
            shown[name] = {**{key: item.get(key) for key in ("ok", "checks", "fail", "warn", "optional", "issues",
                                                              "seconds", "error") if key in item},
                           "at": _iso(item.get("at"))}
    return shown


def human_size(value) -> str:
    """Bytes for a person: 236.0 KB, 12.5 MB, 0.7 GB (from half a gigabyte up, sizes are said in GB)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "?"
    size = abs(value)
    for unit, factor, floor in (("TB", 2**40, 2**40), ("GB", 2**30, 2**29), ("MB", 2**20, 2**20), ("KB", 2**10, 2**10)):
        if size >= floor:
            return f"{value / factor:.1f} {unit}"
    return f"{int(value)} B"


_gb = human_size   # the name the attention texts used before 0.9.0
_SIZE_DOT = re.compile(r"(\d)\.(\d+) (B|KB|MB|GB|TB)\b")


def decimal_comma(text: str, lang: str) -> str:
    """Sizes written with a decimal comma in Portuguese (0,7 GB): applied to a whole page once it is written."""
    return _SIZE_DOT.sub(r"\1,\2 \3", text) if lang == "pt" and isinstance(text, str) else text


def local_time(value, lang: str = "en") -> str:
    """A Unix time or an ISO 8601 text in the machine's local time, with its zone: 16/11/2026 14:05 -03."""
    from datetime import datetime, timezone

    from .experience import utc_datetime

    try:
        if isinstance(value, str):
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            moment = utc_datetime(float(value))
        else:
            return "?"
        moment = moment.astimezone()
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"
    zone = moment.strftime("%Z") or moment.strftime("%z")
    return moment.strftime("%d/%m/%Y %H:%M" if lang == "pt" else "%Y-%m-%d %H:%M") + (f" {zone}" if zone else "")


def plural(count, one: str, many: str) -> str:
    """`one` or `many` with the count in place of {n}: "1 ativa", "2 ativas"."""
    return (one if count == 1 else many).format(n=count)


def _today_label(now: float, lang: str) -> str:
    """The counters of "today" are kept per UTC day: say when that day began, in UTC and in local time."""
    from datetime import datetime, timedelta

    from .experience import utc_datetime

    try:
        start = utc_datetime(now).replace(hour=0, minute=0, second=0, microsecond=0).astimezone()
        here = datetime.fromtimestamp(now).astimezone()
    except (OverflowError, OSError, ValueError):
        start = here = None
    if start is None or start.utcoffset() == timedelta(0):
        return "Desde 00:00 UTC" if lang == "pt" else "Since 00:00 UTC"
    clock = start.strftime("%H:%M")
    if start.date() < here.date():
        return (f"Desde 00:00 UTC ({clock} de ontem no seu horário)" if lang == "pt"
                else f"Since 00:00 UTC ({clock} yesterday your time)")
    return f"Desde 00:00 UTC ({clock} no seu horário)" if lang == "pt" else f"Since 00:00 UTC ({clock} your time)"


def build(home: Path, *, now: float | None = None, config=None, runtime_config=None) -> dict:
    from .evolve import options
    from .signals import guard_summary

    now = time.time() if now is None else now
    opts = options(config or {}, runtime_config, strict=False)
    today = utc_day(now)
    with experience(home) as conn:
        cursor = dict(conn.execute("SELECT key, value FROM cursor WHERE key IN "
                                   "('observed_since','loop_status','backfill_done','backfill_totals',?)",
                                   (f"reflector:{today}",)).fetchall())
        tools = conn.execute("SELECT COALESCE(SUM(calls),0) FROM tool_daily WHERE day=?", (today,)).fetchone()[0]
        turns = conn.execute("SELECT COALESCE(SUM(turns),0) FROM turn_daily WHERE day=?", (today,)).fetchone()[0]
        calls = conn.execute("SELECT COALESCE(SUM(requests),0) FROM metric WHERE day=? AND lane='main'", (today,)).fetchone()[0]
        lessons = dict(conn.execute("SELECT status, COUNT(*) FROM lesson GROUP BY status").fetchall())
        withdrawn = conn.execute("SELECT COUNT(*) FROM lesson WHERE status='retired' AND verdict='withdrawn'").fetchone()[0]
        drafts = conn.execute("SELECT COUNT(*) FROM lesson WHERE origin='reflector' AND status='quarantined'").fetchone()[0]
        proposals = conn.execute("SELECT COUNT(*) FROM proposal WHERE status='open'").fetchone()[0]
        vetoed = guard_summary(conn, now=now, days=7)
        automatic = conn.execute("SELECT COUNT(*) FROM decision_log WHERE actor='automatic' AND at >= ? AND undone IS NULL",
                                 (now - 7 * 86_400,)).fetchone()[0]
        stale, gone = _runtimes(conn, plugin_version())
    if gone:
        from .experience import record

        record(home, lambda conn: conn.executemany("DELETE FROM cursor WHERE key=?", [(key,) for key in gone]), now=now)
    try:
        last = json.loads(cursor["loop_status"]) if "loop_status" in cursor else None
    except ValueError:
        last = None
    try:
        totals = json.loads(cursor.get("backfill_totals") or "{}")
    except ValueError:
        totals = {}
    report = {
        "profile": "default" if Path(home).resolve().parent.name != "profiles" else Path(home).resolve().name,
        "plugin_version": plugin_version(), "observing_since": _iso(cursor.get("observed_since")),
        "today": {"tool_calls": tools, "turns": turns, "model_calls": calls},
        "autopilot": {"minutes": opts["autopilot_minutes"],
                      "last_pass": {**last, "at": _iso(last.get("at"))} if isinstance(last, dict) else None},
        "backfill": {"status": cursor.get("backfill_done") or ("running" if totals else "waiting"), **totals},
        "reflector": {"enabled": opts["reflector"], "calls_today": int(cursor.get(f"reflector:{today}") or 0),
                      "daily_limit": opts["reflector_daily_limit"], "drafts_waiting": drafts},
        "lessons": {"mode": opts["lessons"], "active": lessons.get("active", 0),
                    "awaiting_approval": lessons.get("candidate", 0) + lessons.get("quarantined", 0),
                    "retired": lessons.get("retired", 0) - withdrawn, "withdrawn": withdrawn},
        "proposals_open": proposals,
        "auto_actions": {"mode": opts["mode"], "last_7d": automatic},
        "runtime_guard": {"enabled": opts["runtime_guard"], "state": _guard_state(opts["runtime_guard"]),
                          "vetoed_7d": sum(vetoed.values())},
        "profiles": _profiles(Path(home), now=now),
        "stale_runtimes": stale,
    }
    report["profiles_without_plugin"] = sorted(
        (row for row in report["profiles"] if row["plugin"] == "missing" and row["sessions_7d"] and not row["current"]),
        key=lambda row: -row["sessions_7d"])
    report["legacy_installs"] = [row for row in report["profiles"] if row["plugin"] in ("legacy", "both")]
    channels = chat_senders(home, since=now - 7 * 86_400, owners=opts["owner_sender_ids"], platforms=opts["owner_platforms"])
    report["chats"] = [{key: value for key, value in channel.items() if key != "senders"} | {"senders": len(channel["senders"])}
                       for channel in channels]   # counts only: the ids stay in `hermes evolution owner`
    report["digest_scheduled"] = _digest_scheduled(home)
    report["environment"] = _environment(home)
    report["doctor"] = _doctor(home, report["environment"])
    mine =next((row for row in report["profiles"] if row["current"]), None)
    busiest = max((row for row in report["profiles"] if row["plugin"] == "installed" and not row["current"]),
                  key=lambda row: row["sessions_7d"], default=None)
    report["busier_profile"] = (busiest if busiest and mine and busiest["sessions_7d"] >= 10
                                and busiest["sessions_7d"] > 4 * mine["sessions_7d"] else None)
    attention = []
    if stale:
        attention.append("stale_runtime")
    if not (tools or turns or calls):
        attention.append("no_events_today")
    if opts["autopilot_minutes"] == 0:
        attention.append("autopilot_off")
    elif report["autopilot"]["last_pass"] is None:
        attention.append("no_autopilot_pass")
    elif report["autopilot"]["last_pass"].get("ok") is False:
        attention.append("autopilot_failing")
    if report["lessons"]["awaiting_approval"]:
        attention.append("lessons_waiting")
    if report["legacy_installs"]:
        attention.append("legacy_install")
    if report["busier_profile"]:
        attention.append("busier_profile")
    if report["profiles_without_plugin"]:
        attention.append("profiles_without_plugin")
    if any(not channel["covered"] for channel in report["chats"]):
        attention.append("owner_unknown")
    if (report["lessons"]["awaiting_approval"] or report["proposals_open"]) and not report["digest_scheduled"]:
        attention.append("digest_unscheduled")
    from .cli import ignored_settings, settings as stored_settings

    from .cli import TRUSTED_ONLY

    try:   # values the agent could have written there are not used (cli.TRUSTED_ONLY); say so, by their name there
        report["settings_ignored"] = sorted(TRUSTED_ONLY[key][0][-1] for key in ignored_settings(stored_settings(home)))
    except (OSError, ValueError, RecursionError):
        report["settings_ignored"] = []
    if report["settings_ignored"]:
        attention.append("settings_ignored")
    try:   # changes the running gateway applies only at the next load of the plugin (integration.RELOAD_KEYS)
        with experience(home) as conn:
            row = conn.execute("SELECT value FROM cursor WHERE key='settings_reload'").fetchone()
        waiting = json.loads(row[0]) if row else []
        report["settings_reload"] = [key for key in waiting if isinstance(key, str) and key.isidentifier()][:8]
    except (OSError, ValueError, sqlite3.Error):
        report["settings_reload"] = []
    if report["settings_reload"]:
        attention.append("settings_reload")
    # Hermes' own health and the operating system's, as the last learning pass read them (hermes_care, system_update)
    care = system = None
    try:
        with experience(home) as conn:
            rows = dict(conn.execute("SELECT key, value FROM cursor WHERE key IN ('care_check', 'system_check')").fetchall())
        care = json.loads(rows["care_check"]) if "care_check" in rows else None
        system = json.loads(rows["system_check"]) if "system_check" in rows else None
    except (OSError, ValueError, sqlite3.Error):
        pass
    report["care"], report["system"] = care, system
    for code in (care or {}).get("findings") or ():
        if f"hermes_{code}" in CARE_ATTENTION:
            attention.append(f"hermes_{code}")
    for code in (system or {}).get("findings") or ():
        if f"os_{code}" in CARE_ATTENTION:
            attention.append(f"os_{code}")
    report["attention"] = attention
    return report


_TEXT = {
    "en": {"title": "Atlas Evolution {version} · profile `{profile}`", "since": "Observing since",
           "tool_calls": ("{n} tool call", "{n} tool calls"), "turns": ("{n} turn", "{n} turns"),
           "model_calls": ("{n} model call", "{n} model calls"),
           "autopilot": "Learning pass", "every": "every {minutes} min",
           "off": "off", "pass_off": "off (run `hermes evolution learn` yourself)",
           "last": "last one", "ok": "ok", "failed": "failed",
           "never": "it has not run in this profile yet; `hermes evolution learn` runs one now",
           "backfill": "Hermes history", "reflector": "Reflector", "on": "on", "guard_on": "on", "guard_off": "off",
           "calls": "calls today",
           "drafts": ("{n} draft waiting", "{n} drafts waiting"), "lessons": "Lessons",
           "active": ("{n} active", "{n} active"),
           "awaiting": ("{n} awaiting approval", "{n} awaiting approval"), "retired": ("{n} retired", "{n} retired"),
           "withdrawn": ("{n} dropped from the queue without a decision", "{n} dropped from the queue without a decision"),
           "proposals": "Open proposals",
           "lesson_modes": {"auto": "automatic: built-in templates apply by themselves, the rest waits for you",
                            "suggest": "on approval: every lesson waits for you",
                            "off": "off: measured only, nothing is shown to the agent"},
           "guard": "Runtime guard", "vetoed": ("{n} command vetoed in 7 days", "{n} commands vetoed in 7 days"), "attention": "Needs attention",
           "auto_modes": {"auto": ("Auto mode", "acts by itself in every profile"),
                          "assist": ("Assisted", "on its own it only removes duplicate memory entries; everything "
                                                 "else waits for you"),
                          "manual": ("Manual", "only measures and proposes")},
           "env": "Every profile", "env_profiles": ("{n} profile", "{n} profiles"), "env_used": "{n} used in 7 days",
           "env_on": ("{n} plugin on", "{n} plugins on"), "env_idle": "{n} never loaded",
           "env_skills": ("{n} skill", "{n} skills"),
           "env_todo": ("1 thing to fix: `hermes evolution environment`", "{n} things to fix: `hermes evolution environment`"),
           "env_clean": "nothing to fix",
           "auto_done": ("{n} automatic action in 7 days", "{n} automatic actions in 7 days"),
           "guard_covers": "stops commands that would stop the gateway or change this plugin's settings",
           "doctor": "Hermes doctor", "doctor_line": "{issues} · {checks} · {when}",
           "doctor_checks": ("{n} check", "{n} checks"),
           "doctor_fail": ("{n} problem", "{n} problems"), "doctor_warn": ("{n} warning", "{n} warnings"),
           "doctor_optional": ("{n} optional item not set up", "{n} optional items not set up"),
           "doctor_error": "could not run ({reason}) · {when}",
           "doctor_reasons": {"disabled": "turned off here", "not_a_profile": "not a Hermes profile",
                              "timeout": "took too long", "unavailable": "this Hermes has no `hermes doctor`",
                              "no_answer": "no answer"},
           "doctor_more": "Details and what to do: `hermes evolution environment`.",
           "guard_host": "off: plugins run in a plugin host here (`plugins.isolation: host`), where a crashed host would "
                         "block every tool",
           "no_events_today": "No event reached the plugin today. If the profile was used today, check that Hermes "
                              "loaded the plugin in this profile (`hermes plugins list`).",
           "autopilot_off": "The automatic learning pass is off: learning only runs when you call `hermes evolution learn`.",
           "no_autopilot_pass": "The automatic learning pass has not run in this profile yet; `hermes evolution learn` "
                                "runs one now.",
           "autopilot_failing": "The last automatic learning pass failed; it is retried in 10 minutes. To see the error "
                                "now, run `hermes evolution learn`.",
           "lessons_waiting": "Lessons are waiting for your approval: decide in the menu (`hermes evolution`) or with `hermes evolution lessons review`, in your own terminal.",
           "profiles_without_plugin": "Profiles used in the last 7 days that do not have the plugin (each profile loads its own plugins):",
           "owner_unknown": "These chat channels count as someone else's, so they get built-in template lessons only and "
                            "their failures wait for your approval. If one of them is you, run `hermes evolution owner` "
                            "in your own terminal:",
           "chat_item": "`{origin}`: {sessions} in 7 days {senders}",
           "chat_senders": ("from 1 sender", "from {n} senders"),
           "hermes_gateway_crashed": "The gateway's state file says it runs, but its process is gone: it crashed. "
                                     "Start it again (`hermes gateway restart`, from your terminal) and look at its log: "
                                     "`tail -n 50 ~/.hermes/logs/gateway.log`.",
           "os_held_by_plugin": "A system update of this plugin was stopped before it released the packages it left out "
                                "({held}): they get no update, not even security ones, until you repair them with "
                                "`hermes evolution system repair`.",
           "hermes_gateway_heartbeat_stale": "The gateway says it is running but has not written its heartbeat for {age} s: it may be stuck. Look at `hermes gateway status`.",
           "hermes_gateway_memory_growth": "The gateway uses {rss} of memory, more than twice its usual: a restart frees it (`hermes gateway restart`, from your terminal).",
           "hermes_state_db_unreadable": "Hermes' session database could not be read. Run the Hermes doctor: "
                                         "`hermes evolution care doctor`.",
           "hermes_state_db_damaged": "Hermes' session database failed its integrity check. `hermes evolution care fix` "
                                      "makes a backup and then runs `hermes doctor --fix`.",
           "hermes_state_db_wal_large": "The session database's write-ahead log is {wal}: Hermes folds it back by itself "
                                        "when nothing writes; `hermes evolution care fix` does it now (after a backup).",
           "hermes_disk_critical": "Only {free} free where Hermes writes: below Hermes' own floor. Free room now; see "
                                   "what takes the most with `du -sh ~/.hermes/* | sort -h | tail`.",
           "hermes_disk_low": "Only {free} free where Hermes writes. See what takes the most with "
                              "`du -sh ~/.hermes/* | sort -h | tail`; `hermes evolution care gc` removes tool versions "
                              "nothing uses.",
           "hermes_disk_filling": "At this pace the disk Hermes writes to is full in about {days_en}. See what grows "
                                  "with `du -sh ~/.hermes/* | sort -h | tail`.",
           "hermes_storage_growth": "Hermes' folders grew fast: {grown}. See what takes the most with "
                                    "`du -sh ~/.hermes/* | sort -h | tail`; `hermes evolution care gc` removes old tool "
                                    "versions.",
           "hermes_memory_low": "The machine has little memory left ({available} of {total}). See what uses the most "
                                "memory: `ps -eo rss,comm --sort=-rss | head`; if it is the gateway, "
                                "`hermes gateway restart` frees it.",
           "hermes_swap_full": "The swap is almost full ({used} of {swap}): the machine is short of memory and "
                               "everything slows down. See what uses the most memory: "
                               "`ps -eo rss,comm --sort=-rss | head`; if it is the gateway, `hermes gateway restart` "
                               "frees it.",
           "hermes_cpu_saturated": "The processors are saturated (load {load} on {cpus} CPUs over 15 minutes). See "
                                   "what uses them the most: `ps -eo pcpu,comm --sort=-pcpu | head`.",
           "hermes_unused_tools": "Old versions of Hermes' tools take {size} and nothing uses them: "
                                  "`hermes evolution care gc` removes them (it asks first).",
           "hermes_gateway_not_at_boot": "The gateway runs as a user service without linger: after a reboot it does not start. Fix: `loginctl enable-linger {user}`.",
           "os_package_manager_stuck": "A package manager has been working for {busy} h (an apt left waiting at its "
                                       "question?): while it runs, nothing else can update the system, and a half-way "
                                       "update would not show. See which one with "
                                       "`ps -C apt,apt-get,dpkg -o pid,etime,args`, then finish or close it.",
           "os_dpkg_interrupted": "An update of the system stopped half-way (dpkg). Finish it before anything else: "
                                  "`hermes evolution system repair`.",
           "os_apt_broken": "The system's package manager reports broken dependencies: `hermes evolution system repair` "
                            "fixes them.",
           "os_reboot_required": "The system waits for a reboot ({packages}). Reboot when you can (`sudo reboot`); "
                                 "Hermes' services come back by themselves when they are enabled.",
           "os_security_updates": "{security_en}: run `hermes evolution system upgrade` (it shows what changes and asks "
                                  "first).",
           "os_unattended_off": "Automatic security updates of the system are off (unattended-upgrades). Turn them on "
                                "with `sudo dpkg-reconfigure -plow unattended-upgrades`.",
           "os_lists_stale": "The system's package lists were not refreshed for more than 7 days: "
                             "`hermes evolution system plan` refreshes them.",
           "os_unit_not_enabled": "A Hermes service is not enabled: after a reboot it does not start ({units}). Enable "
                                  "it: {enable}.",
           "health": "Hermes and the system", "health_gateway": "Gateway", "health_db": "Session database",
           "health_disk": "Disk", "health_memory": "Memory", "health_os": "System",
           "updates": ("1 update waiting", "{n} updates waiting"), "security": "{n} security",
           "reboot": "reboot waiting",
           "digest_unscheduled": "Decisions wait for you and nothing reminds you of them (Hermes 0.23.1 drops "
                                 "end-of-session notices). Schedule the weekly digest to your chat with "
                                 "`hermes evolution schedule-digest --deliver <channel>`: it becomes a Hermes scheduled "
                                 "task (cron), no agent.",
           "settings_reload": "These settings changed and apply after `hermes gateway restart` (in the terminal, at the "
                              "next session): {keys}.",
           "settings_ignored": "These values in `settings.json` are ignored: {keys}. They would widen whom the plugin trusts or turn a protection off, and the agent's own file tools can write that file. Set the ones you want in the Hermes settings form, with `hermes evolution owner` (sender ids and platforms), or with `hermes config set plugins.entries.hermes-atlas-evolution.settings.<key> <value>` in your own terminal.",
           "sessions": ("{n} session", "{n} sessions"), "none": "Nothing needs attention.",
           "stale_runtime": "A running Hermes process still runs an older copy of the plugin than the one on disk (`hermes plugins update` does not reload a running gateway). Run `hermes gateway restart`; it waits for the running turns.",
           "stale_item": "pid {pid} ({kind}) loaded {version}",
           "legacy_install": "These profiles still have the plugin under its old name hermes-evolution, which keeps running while a new copy there stays inactive. Run the command shown for each; the data it learned stays:",
           "busier_profile": "This profile is barely used. `{profile}` had {sessions} sessions in 7 days; see it with `{status}`.",
           "profiles": "Profiles of this Hermes", "col_profile": "profile", "col_sessions": "sessions (7 d)",
           "col_plugin": "plugin", "col_last": "last learning pass", "col_lessons": "lessons active / waiting",
           "states": {"installed": "installed", "legacy": "old name hermes-evolution", "both": "both copies: the old one runs",
                      "missing": "not installed"},
           "first_pass": "First learning pass done now: {sessions} sessions, {tool_calls} tool calls of history read.",
           "statuses": {"done": "read", "running": "being read", "waiting": "waiting for the first learning pass",
                        "no_history": "no history", "unreadable": "unreadable", "unsupported_history": "unsupported format"}},
    "pt": {"title": "Atlas Evolution {version} · perfil `{profile}`", "since": "Observando desde",
           "tool_calls": ("{n} chamada de ferramenta", "{n} chamadas de ferramentas"),
           "turns": ("{n} turno", "{n} turnos"), "model_calls": ("{n} chamada ao modelo", "{n} chamadas ao modelo"),
           "autopilot": "Rodada de aprendizado", "every": "a cada {minutes} min",
           "off": "desligado", "pass_off": "desligada (rode `hermes evolution learn` você mesmo)",
           "last": "última", "ok": "ok", "failed": "falhou",
           "never": "ainda não rodou neste perfil; `hermes evolution learn` roda uma agora",
           "backfill": "Histórico do Hermes", "reflector": "Refletor", "on": "ligado", "guard_on": "ligada",
           "guard_off": "desligada", "calls": "chamadas hoje",
           "drafts": ("{n} rascunho aguardando", "{n} rascunhos aguardando"), "lessons": "Lições",
           "active": ("{n} ativa", "{n} ativas"),
           "awaiting": ("{n} aguardando aprovação", "{n} aguardando aprovação"),
           "retired": ("{n} aposentada", "{n} aposentadas"),
           "withdrawn": ("{n} descartada sem decisão (fila limpa)", "{n} descartadas sem decisão (fila limpa)"),
           "proposals": "Propostas abertas",
           "lesson_modes": {"auto": "automáticas: padrões fixos valem sozinhos, o resto espera você",
                            "suggest": "com aprovação: toda lição espera você",
                            "off": "desligadas: só medem, nada é mostrado ao agente"},
           "guard": "Proteção do Hermes", "vetoed": ("{n} comando barrado em 7 dias", "{n} comandos barrados em 7 dias"), "attention": "Precisa de atenção",
           "auto_modes": {"auto": ("Modo Auto", "age sozinho em todos os perfis"),
                          "assist": ("Modo Assistido", "sozinho, só remove entradas repetidas da memória; o resto "
                                                       "espera você"),
                          "manual": ("Modo Manual", "só mede e propõe")},
           "env": "Todos os perfis", "env_profiles": ("{n} perfil", "{n} perfis"), "env_used": "{n} em uso nos últimos 7 dias",
           "env_on": ("{n} plugin ligado", "{n} plugins ligados"),
           "env_idle": ("{n} nunca carregado", "{n} nunca carregados"),
           "env_skills": ("{n} skill", "{n} skills"),
           "env_todo": ("1 coisa para arrumar: `hermes evolution environment`",
                        "{n} coisas para arrumar: `hermes evolution environment`"),
           "env_clean": "nada para arrumar",
           "auto_done": ("{n} ação automática em 7 dias", "{n} ações automáticas em 7 dias"),
           "guard_covers": "barra comandos que parariam o gateway ou mudariam as configurações deste plugin",
           "doctor": "Checagem do Hermes (`hermes doctor`)", "doctor_line": "{issues} · {checks} · {when}",
           "doctor_checks": ("{n} checagem", "{n} checagens"),
           "doctor_fail": ("{n} problema", "{n} problemas"), "doctor_warn": ("{n} aviso", "{n} avisos"),
           "doctor_optional": ("{n} item opcional não configurado", "{n} itens opcionais não configurados"),
           "doctor_error": "não rodou ({reason}) · {when}",
           "doctor_reasons": {"disabled": "desligada aqui", "not_a_profile": "não é um perfil do Hermes",
                              "timeout": "demorou demais", "unavailable": "este Hermes não tem o `hermes doctor`",
                              "no_answer": "sem resposta"},
           "doctor_more": "Detalhes e o que fazer: `hermes evolution environment`.",
           "guard_host": "desligada: aqui os plugins rodam num host de plugins (`plugins.isolation: host`), onde um "
                         "host de plugins que caísse bloquearia todas as ferramentas",
           "no_events_today": "Nenhum evento chegou ao plugin hoje. Se o perfil foi usado hoje, confira se o Hermes "
                              "carregou o plugin neste perfil (`hermes plugins list`).",
           "autopilot_off": "A rodada automática de aprendizado está desligada: o aprendizado só roda quando você chama "
                            "`hermes evolution learn`.",
           "no_autopilot_pass": "A rodada automática ainda não rodou neste perfil; `hermes evolution learn` roda uma "
                                "agora.",
           "autopilot_failing": "A última rodada automática de aprendizado falhou; ela se repete em 10 minutos. Para ver "
                                "o erro agora, rode `hermes evolution learn`.",
           "lessons_waiting": "Há lições aguardando a sua aprovação: decida pelo menu (`hermes evolution`) ou com `hermes evolution lessons review`, no seu próprio terminal.",
           "profiles_without_plugin": "Perfis usados nos últimos 7 dias que não têm o plugin (cada perfil carrega os próprios plugins):",
           "owner_unknown": "Estes canais de chat contam como de outra pessoa, então recebem só lições de padrão fixo e "
                            "as falhas deles esperam a sua aprovação. Se um deles é você, rode `hermes evolution owner` "
                            "no seu próprio terminal:",
           "chat_item": "`{origin}`: {sessions} em 7 dias {senders}",
           "chat_senders": ("de 1 remetente", "de {n} remetentes"),
           "hermes_gateway_crashed": "O arquivo de estado do gateway diz que ele roda, mas o processo sumiu: ele caiu. "
                                     "Suba-o de novo (`hermes gateway restart`, no seu terminal) e veja o registro dele: "
                                     "`tail -n 50 ~/.hermes/logs/gateway.log`.",
           "os_held_by_plugin": "Uma atualização do sistema feita por este plugin parou antes de liberar os pacotes que "
                                "deixou de fora ({held}): eles não recebem atualização, nem de segurança, até você "
                                "repará-los com `hermes evolution system repair`.",
           "hermes_gateway_heartbeat_stale": "O gateway diz que está rodando, mas não grava o sinal de vida há {age} s: pode estar travado. Veja `hermes gateway status`.",
           "hermes_gateway_memory_growth": "O gateway usa {rss} de memória, mais que o dobro do normal: reiniciar libera (`hermes gateway restart`, no seu terminal).",
           "hermes_state_db_unreadable": "Não foi possível ler o banco de sessões do Hermes. Rode a checagem do Hermes "
                                         "(`hermes doctor`): `hermes evolution care doctor`.",
           "hermes_state_db_damaged": "O banco de sessões do Hermes falhou na checagem de integridade. "
                                      "`hermes evolution care fix` faz um backup e depois roda `hermes doctor --fix`.",
           "hermes_state_db_wal_large": "O registro de escrita do banco de sessões está com {wal}: o Hermes o recolhe "
                                        "sozinho quando nada escreve; `hermes evolution care fix` faz isso agora (com um "
                                        "backup antes).",
           "hermes_disk_critical": "Só {free} livres onde o Hermes grava: abaixo do mínimo do próprio Hermes. Libere "
                                   "espaço agora; veja o que mais ocupa com `du -sh ~/.hermes/* | sort -h | tail`.",
           "hermes_disk_low": "Só {free} livres onde o Hermes grava. Veja o que mais ocupa com "
                              "`du -sh ~/.hermes/* | sort -h | tail`; `hermes evolution care gc` remove versões de "
                              "ferramentas que nada usa.",
           "hermes_disk_filling": "Neste ritmo, o disco onde o Hermes grava enche em cerca de {days_pt}. Veja o que "
                                  "cresce com `du -sh ~/.hermes/* | sort -h | tail`.",
           "hermes_storage_growth": "Pastas do Hermes cresceram rápido: {grown}. Veja o que mais ocupa com "
                                    "`du -sh ~/.hermes/* | sort -h | tail`; `hermes evolution care gc` remove versões "
                                    "antigas de ferramentas.",
           "hermes_memory_low": "A máquina tem pouca memória livre ({available} de {total}). Veja quem usa mais memória: "
                                "`ps -eo rss,comm --sort=-rss | head`; se for o gateway, `hermes gateway restart` libera.",
           "hermes_swap_full": "O swap está quase cheio ({used} de {swap}): falta memória na máquina e tudo fica lento. "
                               "Veja quem usa mais memória: `ps -eo rss,comm --sort=-rss | head`; se for o gateway, "
                               "`hermes gateway restart` libera.",
           "hermes_cpu_saturated": "Os processadores estão saturados (carga {load_pt} em {cpus} CPUs nos últimos 15 "
                                   "minutos). Veja quem mais os usa: `ps -eo pcpu,comm --sort=-pcpu | head`.",
           "hermes_unused_tools": "Versões antigas das ferramentas do Hermes ocupam {size} e nada as usa: "
                                  "`hermes evolution care gc` as remove (pergunta antes).",
           "hermes_gateway_not_at_boot": "O gateway roda como serviço do usuário sem o `linger` ligado: depois de um "
                                         "reinício ele não sobe. Correção: `loginctl enable-linger {user}`.",
           "os_package_manager_stuck": "Um gerenciador de pacotes está trabalhando há {busy_pt} h (um apt esquecido "
                                       "esperando a resposta?): enquanto ele roda, nada mais atualiza o sistema e uma "
                                       "atualização pela metade não apareceria. Veja qual é com "
                                       "`ps -C apt,apt-get,dpkg -o pid,etime,args` e termine-o ou feche-o.",
           "os_dpkg_interrupted": "Uma atualização do sistema parou no meio (dpkg). Termine-a antes de qualquer coisa: "
                                  "`hermes evolution system repair`.",
           "os_apt_broken": "O gerenciador de pacotes do sistema relata dependências quebradas: "
                            "`hermes evolution system repair` as conserta.",
           "os_reboot_required": "O sistema espera um reinício ({packages}). Reinicie quando puder (`sudo reboot`); os "
                                 "serviços do Hermes voltam sozinhos quando estão habilitados.",
           "os_security_updates": "{security_pt}: rode `hermes evolution system upgrade` (ele mostra o que muda e "
                                  "pergunta antes).",
           "os_unattended_off": "As atualizações automáticas de segurança do sistema estão desligadas "
                                "(unattended-upgrades). Ligue-as com `sudo dpkg-reconfigure -plow unattended-upgrades`.",
           "os_lists_stale": "As listas de pacotes do sistema não são atualizadas há mais de 7 dias: "
                             "`hermes evolution system plan` as atualiza.",
           "os_unit_not_enabled": "Um serviço do Hermes não está habilitado: depois de um reinício ele não sobe "
                                  "({units}). Habilite-o: {enable}.",
           "health": "Hermes e sistema", "health_gateway": "Gateway", "health_db": "Banco de sessões",
           "health_disk": "Disco", "health_memory": "Memória", "health_os": "Sistema",
           "updates": ("1 atualização esperando", "{n} atualizações esperando"), "security": "{n} de segurança",
           "reboot": "reinício pendente",
           "digest_unscheduled": "Há decisões esperando por você e nada as lembra (o Hermes 0.23.1 descarta os avisos de "
                                 "fim de sessão). Agende o resumo semanal no seu chat com "
                                 "`hermes evolution schedule-digest --deliver <canal>`: ele vira uma tarefa agendada do "
                                 "Hermes (cron), sem agente.",
           "settings_reload": "Estas configurações mudaram e valem depois de `hermes gateway restart` (no terminal, na "
                              "próxima sessão): {keys}.",
           "settings_ignored": "Estes valores do `settings.json` são ignorados: {keys}. Eles ampliariam em quem o plugin confia ou desligariam uma proteção, e as ferramentas de arquivo do próprio agente conseguem gravar esse arquivo. Defina os que você quer no formulário de configurações do Hermes, com o `hermes evolution owner` (ids de remetente e plataformas) ou com `hermes config set plugins.entries.hermes-atlas-evolution.settings.<chave> <valor>` no seu próprio terminal.",
           "sessions": ("{n} sessão", "{n} sessões"), "none": "Nada precisa de atenção.",
           "stale_runtime": "Um processo do Hermes em execução ainda roda uma cópia do plugin mais antiga que a do disco (o `hermes plugins update` não recarrega um gateway em execução). Rode `hermes gateway restart`; ele espera os turnos em andamento.",
           "stale_item": "pid {pid} ({kind}) carregou a {version}",
           "legacy_install": "Estes perfis ainda têm o plugin com o nome antigo hermes-evolution, que continua rodando enquanto uma cópia nova ali fica inativa. Rode o comando de cada um; os dados aprendidos ficam:",
           "busier_profile": "Este perfil quase não é usado. O `{profile}` teve {sessions} sessões em 7 dias; veja com `{status}`.",
           "profiles": "Perfis deste Hermes", "col_profile": "perfil", "col_sessions": "sessões (7 d)",
           "col_plugin": "plugin", "col_last": "última rodada", "col_lessons": "lições ativas / aguardando",
           "states": {"installed": "instalado", "legacy": "nome antigo hermes-evolution", "both": "duas cópias: a antiga roda",
                      "missing": "não instalado"},
           "first_pass": "Primeira rodada de aprendizado feita agora: {sessions} sessões e {tool_calls} chamadas de "
                         "ferramentas do histórico lidas.",
           "statuses": {"done": "lido", "running": "em leitura", "waiting": "aguardando a primeira rodada",
                        "no_history": "sem histórico", "unreadable": "ilegível", "unsupported_history": "formato não suportado"}},
}

# The words for the gateway, the session database and the memory, the same in `status` and `care`.
_WORDS = {
    "en": {"running": "running", "stopped": "not running", "crashed": "stopped unexpectedly (its process is gone)",
           "version": "version {v}", "rss": "{size} of memory", "beat": "last sign of life {s} s ago",
           "wal": "write-ahead log {size}", "no_wal": "no write-ahead log", "integrity_ok": "integrity ok",
           "integrity_bad": "integrity check failed ({value})", "db_missing": "not created yet",
           "db_error": "could not be read", "free": "{free} free of {total}", "swap": "swap: {used} of {total} in use"},
    "pt": {"running": "rodando", "stopped": "parado", "crashed": "caiu (o processo dele sumiu)",
           "version": "versão {v}", "rss": "{size} de memória", "beat": "último sinal de vida há {s} s",
           "wal": "registro de escrita {size}", "no_wal": "sem registro de escrita", "integrity_ok": "integridade ok",
           "integrity_bad": "a checagem de integridade falhou ({value})", "db_missing": "ainda não criado",
           "db_error": "não pôde ser lido", "free": "{free} livres de {total}", "swap": "swap: {used} de {total} em uso"},
}


def gateway_words(gate: dict, lang: str, *, beat: bool = False) -> str:
    """rodando · versão 0.23.1 · 0,6 GB de memória (· último sinal de vida há 12 s)."""
    w = _WORDS.get(lang, _WORDS["en"])
    gate = gate if isinstance(gate, dict) else {}
    parts = [w["crashed"] if gate.get("crashed") else w["running"] if gate.get("running") else w["stopped"]]
    if gate.get("version"):
        parts.append(w["version"].format(v=gate["version"]))
    if gate.get("rss"):
        parts.append(w["rss"].format(size=human_size(gate["rss"])))
    if beat and gate.get("running") and gate.get("heartbeat_age") is not None:
        parts.append(w["beat"].format(s=gate["heartbeat_age"]))
    return " · ".join(parts)


def database_words(db: dict, lang: str) -> str:
    """registro de escrita 878,9 KB · integridade ok (or why it cannot say)."""
    w = _WORDS.get(lang, _WORDS["en"])
    db = db if isinstance(db, dict) else {}
    if not db.get("present"):
        return w["db_missing"]
    if db.get("error"):
        return w["db_error"]
    wal = db.get("wal") or 0
    parts = [w["wal"].format(size=human_size(wal)) if wal or str(db.get("journal") or "").lower() == "wal"
             else w["no_wal"]]
    if db.get("quick_check") == "ok":
        parts.append(w["integrity_ok"])
    elif db.get("quick_check"):
        parts.append(w["integrity_bad"].format(value=db["quick_check"]))
    return " · ".join(parts)


def memory_words(memory: dict, lang: str) -> str:
    """7,0 GB livres de 12,0 GB · swap: 11,0 GB de 12,0 GB em uso."""
    w = _WORDS.get(lang, _WORDS["en"])
    line = w["free"].format(free=human_size(memory.get("available")), total=human_size(memory.get("total")))
    if memory.get("swap_total"):
        line += " · " + w["swap"].format(used=human_size(memory.get("swap_used")), total=human_size(memory["swap_total"]))
    return line


def _comma(value) -> str:
    """A number with a decimal comma, for the Portuguese texts (1,1)."""
    return "?" if value is None else str(value).replace(".", ",")


def _care_values(report: dict) -> dict:
    """The values the attention texts of Hermes' health and the system take. Values that depend on the language
    come in both (`days_en`, `days_pt`...), so every caller formats any text of `_TEXT` with the same mapping."""
    care, system = report.get("care") or {}, report.get("system") or {}
    gate, memory = care.get("gateway") or {}, care.get("memory") or {}
    load = care.get("load") or {}
    grown = ", ".join(f"`{item['folder']}` {_gb(item['before'])} → {_gb(item['now'])}" for item in care.get("growth") or ())
    off = [unit for unit in system.get("units") or () if isinstance(unit, dict) and not unit.get("enabled")]
    units = ", ".join(f"`{unit['unit']}`" for unit in off)
    enable = "; ".join(f"`systemctl --user enable --now {unit['unit']}`" if unit.get("scope") == "user"
                       else f"`sudo systemctl enable --now {unit['unit']}`" for unit in off)
    security = system.get("security") or 0
    days = care.get("days_to_full")
    days = round(days) if isinstance(days, (int, float)) and not isinstance(days, bool) else None
    busy = round((system.get("busy_seconds") or 0) / 3600, 1)
    return {"age": gate.get("heartbeat_age") if gate.get("heartbeat_age") is not None else "?", "rss": _gb(gate.get("rss")), "wal": _gb((care.get("database") or {}).get("wal")),
            "free": _gb(care.get("free")), "days": care.get("days_to_full"), "grown": grown or "?",
            "days_en": "?" if days is None else plural(days, "{n} day", "{n} days"),
            "days_pt": "?" if days is None else plural(days, "{n} dia", "{n} dias"),
            "available": _gb(memory.get("available")), "total": _gb(memory.get("total")),
            "used": _gb(memory.get("swap_used")), "swap": _gb(memory.get("swap_total")),
            "load": load.get("15m"), "load_pt": _comma(load.get("15m")), "cpus": load.get("cpus"),
            "size": _gb(care.get("reclaimable")),
            "user": care.get("owner") or system.get("owner") or "root", "packages": ", ".join(system.get("reboot_packages") or ()) or "?",
            "count": security, "units": units or "?", "enable": enable or "?",
            "security_en": plural(security, "1 security update of the system is waiting",
                                  "{n} security updates of the system are waiting"),
            "security_pt": plural(security, "1 atualização de segurança do sistema espera",
                                  "{n} atualizações de segurança do sistema esperam"),
            "held": ", ".join(system.get("plugin_holds") or ()) or "?",
            "busy": busy, "busy_pt": _comma(busy)}


def _health_lines(report: dict, t: dict, lang: str = "en") -> list[str]:
    care, system = report.get("care"), report.get("system")
    if not care and not system:
        return []
    lines = ["", f"## {t['health']}", ""]
    if care:
        memory, db = care.get("memory") or {}, care.get("database") or {}
        lines.append(f"- **{t['health_gateway']}:** {gateway_words(care.get('gateway') or {}, lang)}")
        if db.get("present"):
            lines.append(f"- **{t['health_db']}:** {database_words(db, lang)}")
        if care.get("free") is not None:
            lines.append(f"- **{t['health_disk']}:** " + _WORDS.get(lang, _WORDS["en"])["free"].format(
                free=_gb(care["free"]), total=_gb(care.get("total"))))
        if memory.get("total"):
            lines.append(f"- **{t['health_memory']}:** {memory_words(memory, lang)}")
    if system and system.get("supported"):
        pending = system.get("upgradable")
        count = len(pending) if isinstance(pending, list) else pending if isinstance(pending, int) else 0
        parts = [str((system.get("os") or {}).get("name") or "?"), _p(t["updates"], count)
                 + (f" ({_p(t['security'], system['security'])})" if system.get("security") else "")]
        if system.get("reboot_required"):
            parts.append(t["reboot"])
        lines.append(f"- **{t['health_os']}:** " + " · ".join(parts))
    return lines


def _p(entry, count) -> str:
    """A text of the tables with its count: a (singular, plural) pair, or one form for both."""
    if isinstance(entry, tuple):
        return plural(count, *entry)
    return entry.format(n=count)


def _doctor_lines(report: dict, t: dict, lang: str) -> list[str]:
    """One short line per profile from the last run of Hermes' own doctor checks, when there is one."""
    runs = report.get("doctor") or {}
    if not runs:
        return []
    lines = ["", f"## {t['doctor']}", ""]
    for name in sorted(runs, key=lambda item: (item != report.get("profile"), item))[:40]:
        item = runs[name]
        when = local_time(item.get("at"), lang) if item.get("at") else "?"
        if not item.get("ok"):
            reason = t["doctor_reasons"].get(item.get("error"), str(item.get("error") or "?"))
            lines.append(f"- `{name}`: " + t["doctor_error"].format(reason=reason, when=when))
            continue
        issues = f"{_p(t['doctor_fail'], item.get('fail') or 0)}, {_p(t['doctor_warn'], item.get('warn') or 0)}"
        if item.get("optional"):
            issues += f" · {_p(t['doctor_optional'], item['optional'])}"
        lines.append(f"- `{name}`: " + t["doctor_line"].format(
            issues=issues, checks=_p(t["doctor_checks"], item.get("checks") or 0), when=when))
    lines += ["", t["doctor_more"]]
    return lines


def markdown(report: dict, lang: str = "en") -> str:
    lang = lang if lang in _TEXT else "en"
    t = _TEXT[lang]
    today, pilot, history = report["today"], report["autopilot"], report["backfill"]
    lines = ["# " + t["title"].format(version=report["plugin_version"], profile=report["profile"]), ""]
    if report["observing_since"]:
        lines.append(f"- **{t['since']}:** {local_time(report['observing_since'], lang)}")
    lines.append(f"- **{_today_label(report.get('now') or time.time(), lang)}:** {_p(t['turns'], today['turns'])} · "
                 f"{_p(t['tool_calls'], today['tool_calls'])} · {_p(t['model_calls'], today['model_calls'])}")
    if pilot["minutes"]:
        last = pilot["last_pass"]
        state = (f"{t['last']} {local_time(last['at'], lang)} · {t['ok'] if last.get('ok') else t['failed']}"
                 if last else t["never"])
        lines.append(f"- **{t['autopilot']}:** {t['every'].format(minutes=pilot['minutes'])} · {state}")
    else:
        lines.append(f"- **{t['autopilot']}:** {t['pass_off']}")
    counts = ", ".join(_p(t[key], history[key]) for key in ("turns", "tool_calls") if key in history)
    lines.append(f"- **{t['backfill']}:** {t['statuses'].get(history['status'], history['status'])}"
                 + (f" · {_p(t['sessions'], history.get('sessions', 0))}, {counts}" if counts else ""))
    reflector = report["reflector"]
    lines.append(f"- **{t['reflector']}:** " + (f"{t['on']} · {reflector['calls_today']}/{reflector['daily_limit']} "
                 f"{t['calls']} · {_p(t['drafts'], reflector['drafts_waiting'])}" if reflector["enabled"] else t["off"]))
    playbook = report["lessons"]
    lines.append(f"- **{t['lessons']}** ({t['lesson_modes'].get(playbook['mode'], playbook['mode'])}): "
                 f"{_p(t['active'], playbook['active'])} · {_p(t['awaiting'], playbook['awaiting_approval'])} · "
                 f"{_p(t['retired'], playbook['retired'])}"
                 + (f" · {_p(t['withdrawn'], playbook['withdrawn'])}" if playbook.get("withdrawn") else ""))
    lines.append(f"- **{t['proposals']}:** {report['proposals_open']}")
    guard = report["runtime_guard"]
    if guard.get("state") == "host":
        state = t["guard_host"]
    elif guard["enabled"]:
        state = f"{t['guard_on']} · {_p(t['vetoed'], guard['vetoed_7d'])} · {t['guard_covers']}"
    else:
        state = t["guard_off"]
    lines.append(f"- **{t['guard']}:** {state}")
    automatic = report.get("auto_actions") or {}
    if automatic:
        name, does = t["auto_modes"].get(automatic.get("mode"), (str(automatic.get("mode")), ""))
        lines.append(f"- **{name}:** " + (f"{does} · " if does else "") + _p(t["auto_done"], automatic.get("last_7d", 0)))
    if report.get("first_pass"):
        lines += ["", "> " + t["first_pass"].format(**report["first_pass"])]
    lines += ["", f"## {t['attention']}", ""]
    for code in report["attention"]:
        if code == "busier_profile":
            lines.append("- " + t[code].format(**{**report["busier_profile"], "profile": report["busier_profile"]["profile"],
                                                 "sessions": report["busier_profile"]["sessions_7d"]}))
            continue
        if code == "settings_ignored":
            lines.append("- " + t[code].format(keys=", ".join(f"`{key}`" for key in report["settings_ignored"])))
            continue
        if code.startswith(("hermes_", "os_")):
            lines.append("- " + t[code].format(**_care_values(report)))
            continue
        if code == "settings_reload":
            lines.append("- " + t[code].format(keys=", ".join(f"`{key}`" for key in report["settings_reload"])))
            continue
        lines.append(f"- {t[code]}")
        if code == "legacy_install":
            for item in report["legacy_installs"]:
                lines.append(f"  - `{item['profile']}`: `{item['migrate']}`")
        if code == "stale_runtime":
            for item in report["stale_runtimes"]:
                lines.append("  - " + t["stale_item"].format(**item))
        if code == "profiles_without_plugin":
            for item in report["profiles_without_plugin"]:
                lines.append(f"  - `{item['profile']}` ({_p(t['sessions'], item['sessions_7d'])}): `{item['install']}`")
        if code == "owner_unknown":
            for item in report["chats"]:
                if not item["covered"]:
                    lines.append("  - " + t["chat_item"].format(origin=item["origin"], sessions=_p(t["sessions"], item["sessions"]),
                                                                senders=_p(t["chat_senders"], item["senders"])))
    if not report["attention"]:
        lines.append(t["none"])
    lines += _health_lines(report, t, lang)
    rows = report.get("profiles", [])
    env = report.get("environment") or {}
    if env.get("summary"):
        # the profiles counted here are the ones the table below lists (the environment's look may be older)
        counts, todo = env["summary"], len(env.get("findings") or ())
        parts = [_p(t["env_profiles"], len(rows) or counts.get("profiles") or 0),
                 _p(t["env_used"], sum(1 for row in rows if row["sessions_7d"]) if rows else counts.get("used_7d") or 0),
                 _p(t["env_on"], counts.get("plugins_on") or 0), _p(t["env_idle"], counts.get("plugins_idle") or 0),
                 _p(t["env_skills"], counts.get("skills") or 0), _p(t["env_todo"], todo) if todo else t["env_clean"]]
        lines += ["", f"## {t['env']}", "", " · ".join(parts)]
    lines += _doctor_lines(report, t, lang)
    if rows:
        lines += ["", f"## {t['profiles']}", "",
                  f"| {t['col_profile']} | {t['col_sessions']} | {t['col_plugin']} | {t['col_last']} | {t['col_lessons']} |",
                  "|---|---:|---|---|---:|"]
        for row in sorted(rows, key=lambda item: (not item["current"], -item["sessions_7d"], item["profile"]))[:40]:
            name = f"**{row['profile']}**" if row["current"] else row["profile"]
            lessons = f"{row['lessons_active']} / {row['lessons_waiting']}" if row["plugin"] != "missing" else "—"
            last = local_time(row["last_pass"], lang) if row["last_pass"] else "—"
            lines.append(f"| {name} | {row['sessions_7d']} | {t['states'][row['plugin']]} | {last} | {lessons} |")
        if len(rows) > 40:
            lines.append(f"| … +{len(rows) - 40} | | | | |")
    return decimal_comma("\n".join(lines) + "\n", lang)


_NOTICE = {
    "en": {"lessons": ("{n} lesson awaiting your approval", "{n} lessons awaiting your approval"),
           "drafts": ("{n} reflector draft to read", "{n} reflector drafts to read"),
           "risky": ("{n} high-risk proposal", "{n} high-risk proposals"), "failing": "the last learning pass failed",
           "lead": "Atlas Evolution", "tail": "See `hermes evolution status`."},
    "pt": {"lessons": ("{n} lição aguardando a sua aprovação", "{n} lições aguardando a sua aprovação"),
           "drafts": ("{n} rascunho do refletor para ler", "{n} rascunhos do refletor para ler"),
           "risky": ("{n} proposta de risco alto", "{n} propostas de risco alto"),
           "failing": "a última rodada de aprendizado falhou",
           "lead": "Atlas Evolution", "tail": "Veja `hermes evolution status`."},
}


def _guard_state(enabled: bool) -> str:
    """on, off, or host: Hermes runs this profile's plugins in a plugin host, where the guard stays off."""
    module = sys.modules.get("hermes_cli.plugin_isolation")
    mode = getattr(module, "isolation_mode", None)
    try:
        isolation = mode() if callable(mode) else None
    except Exception:
        isolation = None
    if isolation == "host":
        return "host"
    return "on" if enabled else "off"


def pending_notice(home: Path, *, now: float, lang: str = "en") -> dict | None:
    """At most one short notice per UTC day about decisions waiting for the owner, or None.

    The day is marked in the same transaction that reads it, on the hooks' shared connection (it never waits
    long): a session end that cannot mark it, because the database is busy, shows nothing and leaves the notice
    to a later one, so two sessions never both show it."""
    today = utc_day(now)
    t = _NOTICE[lang if lang in _NOTICE else "en"]
    parts: list[str] = []
    try:
        with experience(home, observer=True) as conn:
            if (conn.execute("SELECT value FROM cursor WHERE key='notice_day'").fetchone() or [None])[0] == today:
                return None
            waiting = conn.execute("SELECT COUNT(*) FROM lesson WHERE status IN ('candidate','quarantined')").fetchone()[0]
            drafts = conn.execute("SELECT COUNT(*) FROM lesson WHERE status='quarantined' AND origin='reflector'").fetchone()[0]
            risky = conn.execute("SELECT COUNT(*) FROM proposal WHERE status='open' AND risk='high'").fetchone()[0]
            saved = conn.execute("SELECT value FROM cursor WHERE key='loop_status'").fetchone()
            try:
                failing = bool(saved) and json.loads(saved[0]).get("ok") is False
            except (ValueError, AttributeError):
                failing = False
            parts = [_p(text, count) for count, text in ((waiting, t["lessons"]), (drafts, t["drafts"]),
                                                         (risky, t["risky"])) if count]
            if failing:
                parts.append(t["failing"])
            if not parts:
                return None
            # Compare and set: only the session that moves the mark to today shows the notice.
            marked = conn.execute("""INSERT INTO cursor VALUES ('notice_day', ?) ON CONFLICT(key)
                DO UPDATE SET value=excluded.value WHERE cursor.value IS NOT excluded.value""", (today,)).rowcount
    except (sqlite3.Error, OSError, ValueError):   # busy or unreadable: nothing is shown, a later session tries again
        return None
    if not marked:
        return None
    return {"message": f"{t['lead']}: {'; '.join(parts)}. {t['tail']}"}



_ENV_TEXT = {
    "en": {"none": "No profile has Atlas Evolution on, so nobody looks after the whole Hermes yet. Turn it on in a "
                   "profile: `hermes -p <profile> plugins enable hermes-atlas-evolution`.",
           "not_yet": "Not checked yet. The first check happens in the next learning pass of profile `{lead}`, or now "
                      "with `hermes evolution environment scan`.",
           "head": "Looked after by profile `{lead}` · last check {when} · its mode: {mode} (it decides for profiles without the plugin; each other profile follows its own mode).",
           "catalog": "Hermes plugin catalog: {plugins} ({tiers}), {removed}.", "plugins": ("1 plugin", "{n} plugins"),
           "removed": ("1 removed from it", "{n} removed from it"),
           "cols": "| Profile | Sessions 7 d | Plugins on | Never loaded | Skills | Atlas Evolution |",
           "partial": "A partial check: the limit of plugin code read in one check was reached, so a conflict may be missing.",
           "todo": "What to do ({n})", "clean": "Nothing to fix: no conflict, duplicate, removed plugin or leftover.",
           "seen": "Seen", "advice": "What to do", "run": "Command", "risk": "{risk} risk",
           "where": "Decide in profile `{lead}`: `hermes{p} evolution proposals list` shows each one's number; then run "
                    "`hermes{p} evolution proposals approve <id>` or `hermes{p} evolution proposals reject <id>`.",
           "yes": "yes", "no": "no"},
    "pt": {"none": "Nenhum perfil tem o Atlas Evolution ligado, então ninguém cuida do Hermes inteiro ainda. Ligue-o "
                   "num perfil: `hermes -p <perfil> plugins enable hermes-atlas-evolution`.",
           "not_yet": "Ainda não verificado. A primeira verificação acontece na próxima rodada de aprendizado do perfil "
                      "`{lead}`, ou agora com `hermes evolution environment scan`.",
           "head": "Quem cuida: perfil `{lead}` · última verificação {when} · modo dele: {mode} (vale para os perfis sem o plugin; cada um dos outros segue o próprio modo).",
           "catalog": "Catálogo de plugins do Hermes: {plugins} ({tiers}), {removed}.", "plugins": ("1 plugin", "{n} plugins"),
           "removed": ("1 removido", "{n} removidos"),
           "cols": "| Perfil | Sessões 7 d | Plugins ligados | Nunca carregados | Skills | Atlas Evolution |",
           "partial": "Uma verificação parcial: o limite de código de plugins lido numa verificação foi atingido, então pode faltar um conflito.",
           "todo": "O que fazer ({n})", "clean": "Nada para arrumar: nenhum conflito, duplicidade, plugin removido ou resto de instalação.",
           "seen": "Visto", "advice": "O que fazer", "run": "Comando", "risk": "risco {risk}",
           "where": "Decida no perfil `{lead}`: `hermes{p} evolution proposals list` mostra o número de cada uma; depois "
                    "rode `hermes{p} evolution proposals approve <id>` ou `hermes{p} evolution proposals reject <id>`.",
           "yes": "sim", "no": "não"},
}


def environment_markdown(home: Path, lang: str = "en", *, mode: str = "auto") -> str:
    """The Environment tab and screen: every profile, the catalog, and what to do, from the lead profile. Never
    raises: a damaged record shows as not looked at yet."""
    try:
        return decimal_comma(_environment_markdown(home, lang, mode=mode), lang)
    except Exception:
        return _ENV_TEXT.get(lang, _ENV_TEXT["en"])["not_yet"].format(lead="?") + "\n"


def _environment_markdown(home: Path, lang: str = "en", *, mode: str = "auto") -> str:
    from . import environment, explain
    from .report import subject_words

    t = _ENV_TEXT.get(lang, _ENV_TEXT["en"])
    lead = environment.lead_home(home)
    if lead is None:
        return t["none"] + "\n"
    name = "default" if lead.parent.name != "profiles" else lead.name
    found = _environment(home)
    if not found:
        return t["not_yet"].format(lead=name) + "\n"
    stamp = local_time(found.get("at"), lang) if found.get("at") else "?"
    mode = found.get("lead_mode") or mode   # the lead's mode decides for profiles without the plugin
    shown = {"auto": "Auto", "assist": "Assistido" if lang == "pt" else "Assisted", "manual": "Manual"}.get(mode, mode)
    lines = [t["head"].format(lead=name, when=stamp, mode=shown), ""]
    catalog = found.get("catalog") or {}
    if catalog.get("plugins"):
        names = {"official": "oficiais", "verified": "verificados", "community": "da comunidade"} if lang == "pt" else {}
        tiers = ", ".join(f"{count} {names.get(tier, tier)}" for tier, count in sorted((catalog.get("tiers") or {}).items()))
        lines += [t["catalog"].format(plugins=_p(t["plugins"], catalog["plugins"]), tiers=tiers,
                                      removed=plural(catalog.get("removed", 0), *t["removed"])), ""]
    if found.get("complete") is False:
        lines += [t["partial"], ""]
    lines += [t["cols"], "|---|---:|---:|---:|---:|---|"]
    for row in found.get("profiles") or ():
        lines.append(f"| {row['profile']} | {row['sessions_7d']} | {row['plugins_on']} | {row['plugins_idle']} | "
                     f"{row['skills']} | {t['yes'] if row['self'] else t['no']} |")
    items = found.get("findings") or []
    lines += ["", f"### {t['todo'].format(n=len(items))}", ""]
    if not items:
        lines.append(t["clean"])
    for item in items[:40]:
        told = explain.describe(item, lang)
        lines.append(f"- **{told['title']}** · {subject_words(item, lang)} · " + t["risk"].format(risk=told["risk"]))
        if told["what"]:
            lines.append(f"  - {t['seen']}: {told['what']}")
        if told.get("advice"):
            lines.append(f"  - {t['advice']}: {told['advice']}")
        if item.get("command"):
            lines.append(f"  - {t['run']}: `{item['command']}`")
    lines += ["", t["where"].format(lead=name, p="" if name == "default" else f" -p {name}")]
    return "\n".join(lines) + "\n"
