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


def plugin_version(home: Path | None = None) -> str:
    """The plugin's version: of the copy installed in the profile at `home` when there is one (the dashboard reads
    other profiles, whose copies may be older or newer than its own), else of the code running here."""
    manifest = Path(__file__).resolve().parent.parent / "plugin.yaml"
    if home is not None:
        installed = Path(home) / "plugins" / "hermes-atlas-evolution" / "plugin.yaml"
        if installed.is_file():
            manifest = installed
    try:
        match = re.search(r'^version:\s*"?([\d.]+)"?', manifest.read_text(encoding="utf-8"), re.M)
    except OSError:
        match = None
    return match.group(1) if match else "unknown"


def process_kind() -> str:
    """gateway, dashboard, plugin_host or cli: long-running hosts are the ones that can keep old code loaded. The
    plugin host (`plugins.isolation: host`) lives as long as the Hermes that started it."""
    if os.environ.get("HERMES_PLUGIN_HOST_PROCESS") == "1" or any(
            "plugin_host_child" in str(item) for item in sys.argv[:3]):
        return "plugin_host"
    words = [os.path.basename(str(item)) for item in sys.argv[:6]]
    if "gateway" in words and "run" in words:
        return "gateway"
    if "dashboard" in words or "serve" in words:   # `hermes serve`: the same server, for the Desktop and remote clients
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


def _load_json(path: Path, limit: int) -> tuple[str, object]:
    """(status, value) of a JSON file of Hermes, read in a bounded way: a regular file only (no link or reparse
    point), at most `limit` bytes, the same file that was checked."""
    import stat

    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) \
                or not stat.S_ISREG(info.st_mode):
            return "unreadable", None
        if info.st_size > limit:
            return "file_limit", None
        with path.open("rb") as stream:
            if not os.path.samestat(info, os.fstat(stream.fileno())):
                return "snapshot_changed", None
            raw = stream.read(limit + 1)
    except FileNotFoundError:
        return "not_found", None
    except OSError:
        return "unreadable", None
    if len(raw) > limit:
        return "file_limit", None
    try:
        return "ok", json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError, RecursionError):
        return "invalid_format", None


def legacy_notes_command(home: Path) -> str:
    """A command that prints the notes of version 0.1 still in the plugin's database (read-only: the file is opened
    with mode=ro), one per line: id, category, scope, when it was written and its text."""
    import shlex

    database = Path(home).resolve() / "evolution" / "evolution.sqlite3"
    code = ("import pathlib,sqlite3,sys; db=sqlite3.connect(pathlib.Path(sys.argv[1]).as_uri()+'?mode=ro',uri=True); "
            "[print(row) for row in db.execute('SELECT id,category,scope,created_at,content FROM evolution_memories "
            "ORDER BY id')]")
    return f'python3 -c "{code}" {shlex.quote(str(database))}'


def legacy_notes_forget_command(home: Path) -> str:
    """A command that saves the notes of version 0.1 to evolution/notes-0.1.json, next to the database, and then
    removes them from it (the table evolution_memories, which nothing reads since 0.11.0). Run by the owner only."""
    import shlex

    database = Path(home).resolve() / "evolution" / "evolution.sqlite3"
    code = ("import json,pathlib,sqlite3,sys; p=pathlib.Path(sys.argv[1]); db=sqlite3.connect(p); "
            "keys=('id','category','scope','created_at','content'); "
            "rows=[dict(zip(keys,row)) for row in db.execute('SELECT id,category,scope,created_at,content FROM "
            "evolution_memories ORDER BY id')]; out=p.with_name('notes-0.1.json'); "
            "out.write_text(json.dumps(rows,ensure_ascii=False,indent=1),encoding='utf-8'); "
            "db.execute('DROP TABLE evolution_memories'); db.commit(); print(len(rows),'->',out)")
    return f'python3 -c "{code}" {shlex.quote(str(database))}'


def _legacy_notes(home: Path) -> dict | None:
    """The notes of version 0.1 (table evolution_memories) left in the plugin's database: nothing reads or writes
    them since 0.11.0, and they are not deleted, so the owner can keep what they need. None when there are none."""
    try:
        with experience(home) as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='evolution_memories'").fetchone() is None:
                return None
            count = conn.execute("SELECT COUNT(*) FROM evolution_memories").fetchone()[0]
    except (OSError, ValueError, sqlite3.Error):
        return None
    return {"count": count, "command": legacy_notes_command(home), "forget": legacy_notes_forget_command(home)} \
        if count else None


def _digest_scheduled(home: Path, *, paused_too: bool = True) -> bool:
    """Whether Hermes' scheduler holds the weekly digest of this plugin for the profile (a job the owner paused
    counts: it is theirs to resume, and a second one would deliver twice)."""
    status, data = _load_json(Path(home) / "cron" / "jobs.json", 4 * 1024 * 1024)
    jobs = data.get("jobs") if status == "ok" and isinstance(data, dict) else None
    return any(isinstance(job, dict) and (paused_too or job.get("enabled") is not False)
               and job.get("name") in ("hermes-atlas-evolution-digest", "hermes-evolution-digest")
               for job in jobs or ())


_OLD_TICK = re.compile(r"\bevolution\s+(?:ops\s+)?tick\b")
_OLD_TICK_NAMES = ("hermes-atlas-evolution-watchdog", "hermes-evolution-watchdog")   # `ops schedule-spec` up to 0.10


def stale_tick_jobs(home: Path) -> list[dict]:
    """Jobs of Hermes' scheduler that still run `evolution ops tick` or `evolution tick`, removed in 0.11.0: each
    fails at every run now. Found by the name the old recipe gave them or by the command in the job."""
    status, data = _load_json(Path(home) / "cron" / "jobs.json", 4 * 1024 * 1024)
    jobs = data.get("jobs") if status == "ok" and isinstance(data, dict) else None
    found = []
    for job in jobs or ():
        if not isinstance(job, dict):
            continue
        name, ident = str(job.get("name") or ""), str(job.get("id") or "")
        said = " ".join(str(job.get(key) or "") for key in ("prompt", "script", "command", "args"))
        if (ident or name) and (name in _OLD_TICK_NAMES or _OLD_TICK.search(said)
                                or "atlas-evolution-watchdog" in str(job.get("script") or "")):
            found.append({"id": ident or name, "name": name or ident})
    return found[:5]


LEGACY_DIRS = ("hermes-evolution",)   # the plugin id used up to 0.5.0 (the same as cli.LEGACY_PLUGIN_IDS)


def _command(name: str, rest: str) -> str:
    return f"hermes -p {name} {rest}"   # explicit even for default: `hermes profile use` makes another one sticky


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


def _gateway_of(folder: Path, name: str) -> str:
    """The profile whose gateway serves the profile `name` (cli.gateway_profile)."""
    from .cli import gateway_profile

    return gateway_profile(folder) if name != "default" else "default"


def _profiles(home: Path, *, now: float) -> list[dict]:
    """Every profile of the same Hermes root: sessions in 7 days and the plugin's state there.

    One gateway serves many profiles, but each profile loads its own plugins, so the profile you happen to be
    in may not be the one doing the work."""
    from .environment import profiles, root_of

    # the same profiles the Mode auto looks after (environment.profiles): never a link or a name Hermes refuses
    candidates = profiles(root_of(home))
    home = Path(home).resolve()
    rows = []
    for name, folder in candidates:
        state, last, active, waiting = _profile_state(folder)
        rows.append({"profile": name, "current": folder.resolve() == home,
                     "sessions_7d": _sessions(folder, since=now - 7 * 86_400) or 0, "plugin": state,
                     "last_pass": last, "lessons_active": active, "lessons_waiting": waiting,
                     # Hermes' own command with the plugin's name (the catalog's reviewed pin): never a URL or a
                     # commit the plugin picks
                     "install": _command(name, f"plugins install {PLUGIN_DIR} --enable"),
                     "migrate": f"{_command(name, 'plugins remove ' + LEGACY_DIRS[0])} && " + (
                         _command(_gateway_of(folder, name), "gateway restart") if state == "both"
                         else _command(name, f"plugins install {PLUGIN_DIR} --enable")),
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
                  "hermes_cpu_saturated", "hermes_unused_tools", "hermes_tools_in_use_unreferenced",
                  "hermes_gateway_not_at_boot", "hermes_gateway_program_deleted",
                  "os_dpkg_interrupted", "os_package_manager_stuck", "os_apt_broken", "os_reboot_required", "os_security_updates", "os_unattended_off",
                  "os_lists_stale", "os_unit_not_enabled", "hermes_gateway_exec_missing", "hermes_gateway_restart_loop",
                  "os_unit_exec_missing", "os_unit_restart_loop")
# The attention codes that come from the gateway's systemd unit: a look now (live_units) overrides what was stored.
UNIT_CODES = ("hermes_gateway_exec_missing", "hermes_gateway_restart_loop", "hermes_gateway_crashed",
              "hermes_gateway_program_deleted")
CARE_REFRESH_SECONDS, SYSTEM_REFRESH_SECONDS = 6 * 3600, 86_400   # hermes_care's and system_update's own pace


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
    zone = short_zone(moment)
    return moment.strftime("%d/%m/%Y %H:%M" if lang == "pt" else "%Y-%m-%d %H:%M") + (f" {zone}" if zone else "")


def short_zone(moment) -> str:
    """The zone of an aware time in one short word on every system: its abbreviation when the system has one (BRT,
    -03, UTC), else the offset (-03, +05:30). Windows names zones in long words ("E. South America Standard Time")."""
    name = moment.strftime("%Z")
    if name and len(name) <= 6 and " " not in name:
        return name
    offset = moment.utcoffset()
    if offset is None:
        return ""
    minutes = int(offset.total_seconds() // 60)
    if minutes == 0:
        return "UTC"
    sign, minutes = ("+" if minutes > 0 else "-"), abs(minutes)
    return f"{sign}{minutes // 60:02d}" + (f":{minutes % 60:02d}" if minutes % 60 else "")


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


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _probe_names(care: dict | None, system: dict | None, home: Path | None = None) -> dict:
    """The Hermes units a look now asks systemd about: the ones the last checks saw, and the gateway's usual one
    (this profile's name for it)."""
    from .hermes_care import expected_gateway_units

    served = bool(((care or {}).get("gateway") or {}).get("served_by_root"))
    names: dict = {"user": list(expected_gateway_units(home, served=served)), "system": []}
    for unit in [*((system or {}).get("units") or ()), (care or {}).get("gateway_unit")]:
        if isinstance(unit, dict) and isinstance(unit.get("unit"), str) and unit.get("scope") in names \
                and unit["unit"] not in names[unit["scope"]]:
            names[unit["scope"]].append(unit["unit"])
    return names


def live_units(care: dict | None, system: dict | None, *, host=None, home: Path | None = None) -> dict | None:
    """One `systemctl show` per scope, 4 s at most, on Linux with systemd (or the `host` given): what Hermes'
    services do now. None where it cannot be asked."""
    if host is None and (os.name != "posix" or not Path("/run/systemd/system").is_dir()):
        return None
    try:
        from .system_update import probe_units

        return probe_units(_probe_names(care, system, home), host=host, timeout=4)
    except Exception:   # a look now is a bonus: status never fails for it
        return None


def _key(unit: dict) -> tuple:
    return unit.get("scope"), unit.get("unit")


def _unit_trouble(unit: dict) -> bool:
    """A unit that cannot start or that systemd keeps restarting."""
    return bool(unit.get("exec_missing") or unit.get("restart_loop")
                or unit.get("exec_main_status") == 203 and unit.get("active_state") != "active")


def _stored_units(care: dict | None, system: dict | None) -> list[dict]:
    return [unit for unit in [(care or {}).get("gateway_unit"), *((system or {}).get("units") or ())]
            if isinstance(unit, dict) and isinstance(unit.get("unit"), str)]


def _current_units(care: dict | None, system: dict | None, live: dict | None) -> list[dict]:
    """Hermes' units as known now: what a look now saw, then what the stored checks saw of the units it did not."""
    seen = [unit for unit in (live or {}).get("units") or () if isinstance(unit, dict) and unit.get("unit")]
    keys = {_key(unit) for unit in seen}
    return seen + [unit for unit in _stored_units(care, system) if _key(unit) not in keys]


def _apply_live(attention: list, live: dict, care: dict | None = None, system: dict | None = None,
                home: Path | None = None) -> list:
    """The unit-derived attention codes, as systemd says now: a gateway that cannot start is said even when the
    stored checks are old, and one that runs again is no longer said to be down. When every unit the stored checks
    found in trouble was looked at now and is fine (the owner repaired it), what they said of it is dropped."""
    from .hermes_care import pick_gateway_unit

    stored = _stored_units(care, system)
    enabled = {_key(unit): unit["enabled"] for unit in stored if isinstance(unit.get("enabled"), bool)}
    # a `show` does not say whether a unit comes back after a reboot: the stored checks' list-unit-files does
    units = [{**unit, **({"enabled": enabled[_key(unit)]} if "enabled" not in unit and _key(unit) in enabled else {})}
             for unit in live.get("units") or () if isinstance(unit, dict)]
    picked = (care or {}).get("gateway_unit")
    gate = (care or {}).get("gateway") or {}
    gateway = pick_gateway_unit(units, home=home, served=bool(gate.get("served_by_root")),
                                prefer=[_key(picked)] if isinstance(picked, dict) else ())
    codes = [code for code in attention if code not in UNIT_CODES] if gateway else list(attention)
    now = {_key(unit): unit for unit in units}
    troubled = [unit for unit in stored if _unit_trouble(unit)]
    if troubled and all(_key(unit) in now and not _unit_trouble(now[_key(unit)]) for unit in troubled):
        codes = [code for code in codes if code not in ("os_unit_exec_missing", "os_unit_restart_loop",
                                                        "hermes_gateway_exec_missing", "hermes_gateway_restart_loop",
                                                        "hermes_gateway_program_deleted")]
    if gateway:
        running = gateway.get("active_state") == "active" and gateway.get("sub_state") == "running"
        # a leftover unit that is off, while the gateway runs from another one, is not the gateway failing to start
        leftover = gate.get("running") is True and gateway.get("enabled") is False
        if not running and not leftover and (gateway.get("exec_missing") or gateway.get("exec_main_status") == 203):
            codes.append("hermes_gateway_exec_missing")
        elif not running and gateway.get("restart_loop"):
            codes.append("hermes_gateway_restart_loop")
        elif not running and "hermes_gateway_crashed" in attention:
            codes.append("hermes_gateway_crashed")
        elif running and gateway.get("exec_missing"):   # it runs, but the next restart fails (hermes_care.check)
            codes.append("hermes_gateway_program_deleted")
    others = [unit for unit in units if unit is not gateway]
    if any(unit.get("exec_missing") for unit in others) and "os_unit_exec_missing" not in codes:
        codes.append("os_unit_exec_missing")
    if any(unit.get("restart_loop") for unit in others) and "os_unit_restart_loop" not in codes:
        codes.append("os_unit_restart_loop")
    return codes


def build(home: Path, *, now: float | None = None, config=None, runtime_config=None, live: bool | None = None,
          host=None, problems=None) -> dict:
    """The status of this profile. `live`: also ask systemd now about Hermes' services (one `systemctl show`, 4 s
    at most); by default when run from the command line, or when the stored health checks are old or the learning
    pass stopped (a gateway that died stops the passes that would see it). `host` replaces the machine's commands and files.
    `problems`: what cli.safe_settings could not use (["settings.json"]); the settings evolve.options refuses here are
    added, and both are listed under the attention heading instead of failing."""
    from .evolve import CONFIG_UNREAD, options
    from .signals import guard_summary

    now = time.time() if now is None else now
    refused: list = []
    opts = options(config or {}, runtime_config, strict=False, problems=refused)
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
        stale, gone = _runtimes(conn, plugin_version(home))
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
        "plugin_version": plugin_version(home), "observing_since": _iso(cursor.get("observed_since")),
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
    report["stale_tick_jobs"] = stale_tick_jobs(home)
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
    if report["stale_tick_jobs"]:
        attention.append("stale_tick_job")
    from .cli import ignored_settings, settings as stored_settings

    from .cli import TRUSTED_ONLY

    try:   # values the agent could have written there are not used (cli.TRUSTED_ONLY); say so, by their name there
        report["settings_ignored"] = sorted(TRUSTED_ONLY[key][0][-1] for key in ignored_settings(stored_settings(home)))
    except (OSError, ValueError, RecursionError):
        report["settings_ignored"] = []
    if report["settings_ignored"]:
        attention.append("settings_ignored")
    report["settings_problems"] = sorted({str(item) for item in [*(problems or ()), *refused] if item})
    if report["settings_problems"]:
        attention.append("settings_problems")
    # config.yaml could not be read here (no YAML parser, or a damaged file): every value shown is a default. Also
    # read from evolve.options, should it say so itself.
    report["config_unread"] = bool(isinstance(runtime_config, dict) and runtime_config.get(CONFIG_UNREAD)
                                   or opts.get("config_unread"))
    if report["config_unread"]:
        attention.insert(0, "config_unread")
    try:   # changes the running gateway applies only at the next load of the plugin (integration.RELOAD_KEYS)
        with experience(home) as conn:
            row = conn.execute("SELECT value FROM cursor WHERE key='settings_reload'").fetchone()
        waiting = json.loads(row[0]) if row else []
        report["settings_reload"] = [key for key in waiting if isinstance(key, str) and key.isidentifier()][:8]
    except (OSError, ValueError, sqlite3.Error):
        report["settings_reload"] = []
    if report["settings_reload"]:
        attention.append("settings_reload")
    from .operations import recovered

    report["db_recovered"] = recovered(home, now=time.time() if now is None else now)
    if report["db_recovered"]:
        attention.append("db_recovered")
    report["legacy_notes"] = _legacy_notes(home)
    if report["legacy_notes"]:
        attention.append("legacy_notes")
    try:   # is the agent getting better (evolution_index), and what the plugin itself costs it (selfcost)
        from . import evolution_index, selfcost

        with experience(home) as conn:
            report["evolution_index"] = evolution_index.current(conn, now=now)
            report["self_cost"] = selfcost.summary(conn, now=now)
    except (OSError, ValueError, sqlite3.Error):
        report["evolution_index"] = report["self_cost"] = None
    try:   # why the index moved, a failure far above its usual day, and what to do first (insights)
        from . import insights

        with experience(home) as conn:
            report["insights"] = insights.read(conn, now=now, view=report["evolution_index"])
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
        from logging import getLogger

        getLogger(__name__).info("Atlas Evolution could not read the insights: %s", type(error).__name__)
        report["insights"] = None
    if (report.get("insights") or {}).get("spikes"):
        attention.append("failure_spike")
    if (report.get("self_cost") or {}).get("slow"):
        attention.append("self_slow")
    # Hermes' own health and the operating system's, as the last learning pass read them (hermes_care, system_update)
    care = system = None
    try:
        with experience(home) as conn:
            rows = dict(conn.execute("SELECT key, value FROM cursor WHERE key IN ('care_check', 'system_check')").fetchall())
        care = json.loads(rows["care_check"]) if "care_check" in rows else None
        system = json.loads(rows["system_check"]) if "system_check" in rows else None
    except (OSError, ValueError, sqlite3.Error):
        pass
    care = care if isinstance(care, dict) else None
    system = system if isinstance(system, dict) else None
    report["care"], report["system"] = care, system
    health = []
    for code in (care or {}).get("findings") or ():
        if f"hermes_{code}" in CARE_ATTENTION:
            health.append(f"hermes_{code}")
    for code in (system or {}).get("findings") or ():
        if f"os_{code}" in CARE_ATTENTION:
            health.append(f"os_{code}")
    # How old the stored health checks are, and whether the learning pass that refreshes them still runs.
    interval = (opts["autopilot_minutes"] or 0) * 60
    last_at = _number(last.get("at")) if isinstance(last, dict) else None
    # the passes ran and stopped (a gateway that died stops them): what they stored is no longer the present
    loop_stopped = bool(interval and (care or system) and last_at is not None and now - last_at > 2 * interval)
    ages = [now - at for at in (_number((care or {}).get("at")), _number((system or {}).get("at"))) if at is not None]
    care_at, system_at = _number((care or {}).get("at")), _number((system or {}).get("at"))
    old = bool(care_at is not None and now - care_at > 2 * max(interval, CARE_REFRESH_SECONDS)
               or system_at is not None and now - system_at > 2 * max(interval, SYSTEM_REFRESH_SECONDS))
    report["health_stale"] = loop_stopped or old
    report["health_age"] = round(max(ages)) if ages else None
    if live is None:
        live = process_kind() == "cli" or report["health_stale"]
    report["live"] = live_units(care, system, host=host, home=home) if live else None
    if report["live"] is not None:
        health = _apply_live(health, report["live"], care, system, home)
    if {"hermes_gateway_exec_missing", "hermes_gateway_restart_loop", "hermes_gateway_program_deleted"} & set(health):
        # the gateway's own line already says it, with its fix: the system's line only when another unit is concerned
        # (as known now: a unit the look now found fine is not one, whatever the stored checks said)
        problems = [unit for unit in _current_units(None, system, report["live"])
                    if unit.get("exec_missing") or unit.get("restart_loop")]
        if all("gateway" in str(unit.get("unit")) for unit in problems):
            health = [code for code in health if code not in ("os_unit_exec_missing", "os_unit_restart_loop")]
    if report["health_stale"] and (care or system):
        health.insert(0, "hermes_health_stale")
    attention += list(dict.fromkeys(health))
    report["attention"] = attention
    return report


_TEXT = {
    "en": {"title": "Atlas Evolution {version} · profile `{profile}`", "since": "Observing since",
           "tool_calls": ("{n} tool call", "{n} tool calls"), "turns": ("{n} turn", "{n} turns"),
           "model_calls": ("{n} model call", "{n} model calls"),
           "autopilot": "Learning pass", "every": "every {minutes} min",
           "off": "off", "pass_off": "off (run `hermes evolution learn` yourself)",
           "last": "last one", "ok": "ok", "failed": "failed",
           "never": "none yet in this profile",
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
                              "no_answer": "no answer",
                              "runtime_moved": "Hermes' Python was replaced under the running process (an update "
                                               "moved it): restart Hermes, and check that the gateway service's "
                                               "ExecStart (`systemctl --user cat hermes-gateway`) points at the new "
                                               "one"},
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
           "hermes_tools_in_use_unreferenced": "{in_use_advice_en}",
           "hermes_gateway_not_at_boot": "The gateway runs as a user service without linger: after a reboot it does not start. Fix: `loginctl enable-linger {user}`.",
           "hermes_gateway_exec_missing": "The gateway's service `{unit}` cannot start: its ExecStart runs `{exec_path}`, "
                                          "which no longer exists (Hermes replaced its bundled Python{replacement_en}). "
                                          "systemd keeps failing (status 203/EXEC, {restarts} restarts) and "
                                          "`hermes gateway restart` only waits. {fix_en} Then run `{restart_cmd}` and "
                                          "check it with `{journal_cmd}`.",
           "hermes_gateway_program_deleted": "The gateway runs, but the program its service `{unit}` starts "
                                             "(`{exec_path}`) was deleted under it (Hermes replaced its Python): the "
                                             "next restart fails. Fix the service now: "
                                             "`hermes -p {profile} evolution care fix-gateway`.",
           "hermes_gateway_restart_loop": "systemd keeps restarting the gateway's service `{unit}` ({restarts} restarts, "
                                          "now {sub_state}): it fails right after it starts. See why with "
                                          "`{journal_cmd}` before starting it again.",
           "hermes_health_stale": "Hermes' health below was read {stale_en} ago and the learning pass has not run since: "
                                  "the gateway may be down. `hermes gateway status` and "
                                  "`journalctl --user -u hermes-gateway -n 30` show what it does now.",
           "os_unit_exec_missing": "A Hermes service cannot start: `{unit}` runs `{exec_path}`, which no longer exists"
                                   "{replacement_en}. {fix_en} Then run `{restart_cmd}` and check it with "
                                   "`{journal_cmd}`.",
           "os_unit_restart_loop": "systemd keeps restarting Hermes' service(s) {loop_units}: they fail right after "
                                   "they start. See why with `{journal_cmd}`.",
           "os_package_manager_stuck": "A package manager has been working for {busy} h (an apt left waiting at its "
                                       "question?): while it runs, nothing else can update the system, and a half-way "
                                       "update would not show. See which one with "
                                       "`ps -C apt,apt-get,dpkg -o pid,etime,args`, then finish or close it.",
           "os_dpkg_interrupted": "An update of the system stopped half-way (dpkg). Finish it before anything else: "
                                  "`hermes evolution system repair`.",
           "os_apt_broken": "The system's package manager reports broken dependencies: `hermes evolution system repair` "
                            "fixes them.",
           "os_reboot_required": "The system waits for a reboot ({packages}). Reboot when you can (`sudo systemctl reboot`); "
                                 "Hermes' services come back by themselves when they are enabled.",
           "os_security_updates": "{security_en}: run `hermes evolution system upgrade` (it shows what changes and asks "
                                  "first).",
           "os_unattended_off": "Automatic security updates of the system are off (unattended-upgrades). Turn them on "
                                "as root with `dpkg-reconfigure -plow unattended-upgrades`.",
           "os_lists_stale": "The system's package lists were not refreshed for more than 7 days: "
                             "`hermes evolution system plan` refreshes them.",
           "os_unit_not_enabled": "A Hermes service is not enabled: after a reboot it does not start ({units}). Enable "
                                  "it: {enable}.",
           "health": "Hermes and the system", "health_gateway": "Gateway", "health_db": "Session database",
           "health_disk": "Disk", "health_memory": "Memory", "health_os": "System",
           "updates": ("1 update waiting", "{n} updates waiting"), "security": "{n} security",
           "reboot": "reboot waiting",
           "stale_tick_job": "A scheduled job still runs the `tick` that 0.11.0 removed, so it fails at every run. "
                             "Nothing replaces it: the learning pass runs by itself. Remove it:",
           "digest_unscheduled": "Decisions wait for you and nothing reminds you of them (Hermes drops "
                                 "end-of-session notices). Schedule the weekly digest to your chat with "
                                 "`hermes evolution schedule-digest --deliver <channel>`: it becomes a Hermes scheduled "
                                 "task (cron), no agent.",
           "index": "Evolution index", "index_week": "in 7 days",
           "index_wait": "not yet: it needs a few days of turns and tool calls",
           "index_parts": {"tools": "tools", "turns": "turns", "you": "you", "lessons": "lessons", "health": "health"},
           "cost": "What the plugin costs the agent", "cost_item": "{hook} {mean} (p95 {p95})",
           "cost_hooks": {"post_tool_call": "per tool call", "pre_tool_call": "guard", "prompt_section": "per prompt",
                          "pre_llm_call": "per turn"},
           "self_slow": "Atlas Evolution is adding {p95} to {hook} in its slow tail (p95). A slow disk or a database "
                        "kept busy by another process is the usual cause: see `hermes evolution care check`.",
           "db_recovered": "The plugin's database was damaged and was started again ({when}); the damaged copy is "
                           "kept as `evolution/{copy}`. Lessons, proposals and the history start over; Hermes itself "
                           "was not affected.",
           "legacy_notes": ("{n} note of version 0.1 is still in the plugin's database. Since 0.11.0 nothing reads it "
                            "and the agent no longer gets it; it was kept so you can copy what you need. To print it "
                            "(read-only): `{command}`. To save it to `evolution/notes-0.1.json` and remove it from the "
                            "database, which closes this item: `{forget}`",
                            "{n} notes of version 0.1 are still in the plugin's database. Since 0.11.0 nothing reads "
                            "them and the agent no longer gets them; they were kept so you can copy what you need. To "
                            "print them (read-only): `{command}`. To save them to `evolution/notes-0.1.json` and remove "
                            "them from the database, which closes this item: `{forget}`"),
           "settings_reload": "These settings changed and apply after `hermes gateway restart` (in the terminal, at the "
                              "next session): {keys}.",
           "settings_ignored": "These values in `settings.json` are ignored: {keys}. They would widen whom the plugin trusts or what it does by itself, or turn a protection off, and the agent's own file tools can write that file. Set the ones you want in the Hermes settings form, with `hermes evolution owner` (sender ids and platforms), or with `hermes config set plugins.entries.hermes-atlas-evolution.settings.<key> <value>` in your own terminal.",
           "settings_problems": "These settings could not be used, so a safe value applies until they are fixed: {keys}. Correct them in the Hermes settings form, with `hermes config set plugins.entries.hermes-atlas-evolution.settings.<key> <value>`, or in `evolution/settings.json`; a `settings.json` that cannot be read also keeps lessons off.",
           "config_unread": "config.yaml was not read: install PyYAML or run with Hermes' Python; the values shown are defaults.",
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
                        "no_history": "no history", "unreadable": "unreadable", "unsupported_history": "unsupported format",
                        "damaged_history": "Hermes' history is damaged; replay ended"}},
    "pt": {"title": "Atlas Evolution {version} · perfil `{profile}`", "since": "Observando desde",
           "tool_calls": ("{n} chamada de ferramenta", "{n} chamadas de ferramentas"),
           "turns": ("{n} turno", "{n} turnos"), "model_calls": ("{n} chamada ao modelo", "{n} chamadas ao modelo"),
           "autopilot": "Rodada de aprendizado", "every": "a cada {minutes} min",
           "off": "desligado", "pass_off": "desligada (rode `hermes evolution learn` você mesmo)",
           "last": "última", "ok": "ok", "failed": "falhou",
           "never": "nenhuma ainda neste perfil",
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
           "guard": "Proteção do Hermes", "vetoed": ("{n} comando barrado em 7 dias", "{n} comandos barrados em 7 dias"), "attention": "Pede atenção",
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
                              "no_answer": "sem resposta",
                              "runtime_moved": "o Python do Hermes foi trocado com o processo rodando (uma "
                                               "atualização o moveu): reinicie o Hermes e confira se o ExecStart do "
                                               "serviço do gateway (`systemctl --user cat hermes-gateway`) aponta "
                                               "para o novo"},
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
           "hermes_tools_in_use_unreferenced": "{in_use_advice_pt}",
           "hermes_gateway_not_at_boot": "O gateway roda como serviço do usuário sem o `linger` ligado: depois de um "
                                         "reinício ele não sobe. Correção: `loginctl enable-linger {user}`.",
           "hermes_gateway_exec_missing": "O serviço do gateway `{unit}` não consegue subir: o ExecStart dele roda "
                                          "`{exec_path}`, que não existe mais (o Hermes trocou o Python que traz"
                                          "{replacement_pt}). O systemd segue falhando (status 203/EXEC, {restarts} "
                                          "reinícios) e o `hermes gateway restart` só fica esperando. {fix_pt} Depois "
                                          "rode `{restart_cmd}` e confira com `{journal_cmd}`.",
           "hermes_gateway_program_deleted": "O gateway está rodando, mas o programa que o serviço `{unit}` inicia "
                                             "(`{exec_path}`) foi apagado por baixo dele (o Hermes trocou o Python): o "
                                             "próximo reinício falha. Corrija o serviço agora: "
                                             "`hermes -p {profile} evolution care fix-gateway`.",
           "hermes_gateway_restart_loop": "O systemd segue reiniciando o serviço do gateway `{unit}` ({restarts} "
                                          "reinícios, agora {sub_state}): ele cai logo depois de subir. Veja o motivo com "
                                          "`{journal_cmd}` antes de subi-lo de novo.",
           "hermes_health_stale": "A saúde do Hermes abaixo foi lida há {stale_pt} e a rodada de aprendizado não rodou "
                                  "desde então: o gateway pode estar parado. `hermes gateway status` e "
                                  "`journalctl --user -u hermes-gateway -n 30` mostram o que ele faz agora.",
           "os_unit_exec_missing": "Um serviço do Hermes não consegue subir: `{unit}` roda `{exec_path}`, que não existe "
                                   "mais{replacement_pt}. {fix_pt} Depois rode `{restart_cmd}` e confira com "
                                   "`{journal_cmd}`.",
           "os_unit_restart_loop": "O systemd segue reiniciando serviço(s) do Hermes {loop_units}: eles caem logo depois "
                                   "de subir. Veja o motivo com `{journal_cmd}`.",
           "os_package_manager_stuck": "Um gerenciador de pacotes está trabalhando há {busy_pt} h (um apt esquecido "
                                       "esperando a resposta?): enquanto ele roda, nada mais atualiza o sistema e uma "
                                       "atualização pela metade não apareceria. Veja qual é com "
                                       "`ps -C apt,apt-get,dpkg -o pid,etime,args` e termine-o ou feche-o.",
           "os_dpkg_interrupted": "Uma atualização do sistema parou no meio (dpkg). Termine-a antes de qualquer coisa: "
                                  "`hermes evolution system repair`.",
           "os_apt_broken": "O gerenciador de pacotes do sistema relata dependências quebradas: "
                            "`hermes evolution system repair` as conserta.",
           "os_reboot_required": "O sistema espera um reinício ({packages}). Reinicie quando puder (`sudo systemctl reboot`); os "
                                 "serviços do Hermes voltam sozinhos quando estão habilitados.",
           "os_security_updates": "{security_pt}: rode `hermes evolution system upgrade` (ele mostra o que muda e "
                                  "pergunta antes).",
           "os_unattended_off": "As atualizações automáticas de segurança do sistema estão desligadas "
                                "(unattended-upgrades). Ligue-as, como root, com `dpkg-reconfigure -plow unattended-upgrades`.",
           "os_lists_stale": "As listas de pacotes do sistema não são atualizadas há mais de 7 dias: "
                             "`hermes evolution system plan` as atualiza.",
           "os_unit_not_enabled": "Um serviço do Hermes não está habilitado: depois de um reinício ele não sobe "
                                  "({units}). Habilite-o: {enable}.",
           "health": "Hermes e sistema", "health_gateway": "Gateway", "health_db": "Banco de sessões",
           "health_disk": "Disco", "health_memory": "Memória", "health_os": "Sistema",
           "updates": ("1 atualização esperando", "{n} atualizações esperando"), "security": "{n} de segurança",
           "reboot": "reinício pendente",
           "stale_tick_job": "Uma tarefa agendada ainda roda o `tick` que a 0.11.0 removeu, então falha em toda "
                             "execução. Nada a substitui: a rodada de aprendizado roda sozinha. Remova-a:",
           "digest_unscheduled": "Há decisões esperando por você e nada as lembra (o Hermes descarta os avisos de "
                                 "fim de sessão). Agende o resumo semanal no seu chat com "
                                 "`hermes evolution schedule-digest --deliver <canal>`: ele vira uma tarefa agendada do "
                                 "Hermes (cron), sem agente.",
           "index": "Índice de evolução", "index_week": "em 7 dias",
           "index_wait": "ainda não: precisa de alguns dias de turnos e chamadas de ferramentas",
           "index_parts": {"tools": "ferramentas", "turns": "turnos", "you": "você", "lessons": "lições",
                           "health": "saúde"},
           "cost": "Quanto o plugin custa ao agente", "cost_item": "{hook} {mean} (p95 {p95})",
           "cost_hooks": {"post_tool_call": "por chamada de ferramenta", "pre_tool_call": "proteção",
                          "prompt_section": "por prompt", "pre_llm_call": "por turno"},
           "self_slow": "O Atlas Evolution está somando {p95} a {hook} na sua cauda lenta (p95). Um disco lento ou um "
                        "banco ocupado por outro processo é a causa comum: veja `hermes evolution care check`.",
           "db_recovered": "O banco do plugin estava danificado e foi recomeçado ({when}); a cópia danificada fica em "
                           "`evolution/{copy}`. Lições, propostas e o histórico recomeçam; o próprio Hermes não foi "
                           "afetado.",
           "legacy_notes": ("{n} nota da versão 0.1 continua no banco do plugin. Desde a 0.11.0 nada a lê e o agente "
                            "não a recebe mais; ela foi mantida para você copiar o que precisar. Para mostrá-la (só "
                            "leitura): `{command}`. Para salvá-la em `evolution/notes-0.1.json` e tirá-la do banco, o "
                            "que encerra este item: `{forget}`",
                            "{n} notas da versão 0.1 continuam no banco do plugin. Desde a 0.11.0 nada as lê e o "
                            "agente não as recebe mais; elas foram mantidas para você copiar o que precisar. Para "
                            "mostrá-las (só leitura): `{command}`. Para salvá-las em `evolution/notes-0.1.json` e "
                            "tirá-las do banco, o que encerra este item: `{forget}`"),
           "settings_reload": "Estas configurações mudaram e valem depois de `hermes gateway restart` (no terminal, na "
                              "próxima sessão): {keys}.",
           "settings_ignored": "Estes valores do `settings.json` são ignorados: {keys}. Eles ampliariam em quem o plugin confia ou o que ele faz sozinho, ou desligariam uma proteção, e as ferramentas de arquivo do próprio agente conseguem gravar esse arquivo. Defina os que você quer no formulário de configurações do Hermes, com o `hermes evolution owner` (ids de remetente e plataformas) ou com `hermes config set plugins.entries.hermes-atlas-evolution.settings.<chave> <valor>` no seu próprio terminal.",
           "settings_problems": "Estas configurações não puderam ser usadas, então vale um valor seguro até serem corrigidas: {keys}. Corrija no formulário de configurações do Hermes, com `hermes config set plugins.entries.hermes-atlas-evolution.settings.<chave> <valor>` ou no `evolution/settings.json`; um `settings.json` que não pode ser lido também mantém as lições desligadas.",
           "config_unread": "O config.yaml não foi lido: instale o PyYAML ou rode com o Python do Hermes; os valores mostrados são os padrões.",
           "sessions": ("{n} sessão", "{n} sessões"), "none": "Nada pede atenção.",
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
                        "no_history": "sem histórico", "unreadable": "ilegível", "unsupported_history": "formato não suportado",
                        "damaged_history": "o histórico do Hermes está danificado; replay encerrado"}},
}

# The words for the gateway, the session database and the memory, the same in `status` and `care`.
_WORDS = {
    "en": {"running": "running", "stopped": "not running", "crashed": "stopped unexpectedly (its process is gone)",
           "degraded": "running (degraded: no chat platform connected yet; scheduled tasks still run)",
           "starting": "starting",
           "version": "version {v}", "rss": "{size} of memory", "beat": "last sign of life {s} s ago",
           "wal": "write-ahead log {size}", "no_wal": "no write-ahead log", "integrity_ok": "integrity ok",
           "integrity_bad": "integrity check failed ({value})", "db_missing": "not created yet",
           "db_error": "could not be read", "free": "{free} free of {total}", "swap": "swap: {used} of {total} in use"},
    "pt": {"running": "rodando", "stopped": "parado", "crashed": "caiu (o processo dele sumiu)",
           "degraded": "rodando (degradado: nenhuma plataforma de chat conectada ainda; as tarefas agendadas seguem "
                       "rodando)",
           "starting": "subindo",
           "version": "versão {v}", "rss": "{size} de memória", "beat": "último sinal de vida há {s} s",
           "wal": "registro de escrita {size}", "no_wal": "sem registro de escrita", "integrity_ok": "integridade ok",
           "integrity_bad": "a checagem de integridade falhou ({value})", "db_missing": "ainda não criado",
           "db_error": "não pôde ser lido", "free": "{free} livres de {total}", "swap": "swap: {used} de {total} em uso"},
}


def gateway_words(gate: dict, lang: str, *, beat: bool = False) -> str:
    """rodando · versão 0.23.1 · 0,6 GB de memória (· último sinal de vida há 12 s)."""
    w = _WORDS.get(lang, _WORDS["en"])
    gate = gate if isinstance(gate, dict) else {}
    # a degraded or starting gateway runs (hermes_care.gateway: running): said with its state
    running = w.get(gate.get("state"), w["running"]) if gate.get("state") in ("degraded", "starting") else w["running"]
    parts = [w["crashed"] if gate.get("crashed") else running if gate.get("running") else w["stopped"]]
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
    # the units the finding is about (system_update.check): system ones, running now, that a reboot would not start
    off = [unit for unit in system.get("units") or () if isinstance(unit, dict) and not unit.get("enabled")
           and unit.get("scope") == "system" and unit.get("active")]
    units = ", ".join(f"`{unit['unit']}`" for unit in off)
    enable = "; ".join(f"`systemctl --user enable --now {unit['unit']}`" if unit.get("scope") == "user"
                       else f"`sudo systemctl enable --now {unit['unit']}`" for unit in off)
    security = system.get("security") or 0
    days = care.get("days_to_full")
    days = round(days) if isinstance(days, (int, float)) and not isinstance(days, bool) else None
    busy = round((system.get("busy_seconds") or 0) / 3600, 1)
    return {"profile": report.get("profile") or "default", "age": gate.get("heartbeat_age") if gate.get("heartbeat_age") is not None else "?", "rss": _gb(gate.get("rss")), "wal": _gb((care.get("database") or {}).get("wal")),
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
            "busy": busy, "busy_pt": _comma(busy), **_in_use_values(care), **_unit_values(report)}


def _in_use_values(care: dict) -> dict:
    """The tool folders `hermes pm gc` would delete while something starts from them, and what does, in both
    languages: `python-3.14.9` (used by `hermes-gateway.service`)."""
    users = care.get("in_use_by") if isinstance(care.get("in_use_by"), dict) else {}
    said = {"en": [], "pt": []}
    for folder in [str(item) for item in care.get("in_use_unreferenced") or () if item][:8]:
        who = [item for item in users.get(folder) or () if isinstance(item, str)][:3]
        for lang, by, process, joined in (("en", "used by", "the running Hermes process", " and "),
                                          ("pt", "usada por", "o processo do Hermes em execução", " e ")):
            names = joined.join(process if item == "process" else f"`{item}`" for item in who)
            said[lang].append(f"`{folder}`" + (f" ({by} {names})" if names else ""))
    values = {"in_use_en": ", ".join(said["en"]) or "?", "in_use_pt": ", ".join(said["pt"]) or "?"}
    kinds = care.get("in_use_kind") if isinstance(care.get("in_use_kind"), dict) else {}
    if any(kinds.get(folder) != "older" for folder in care.get("in_use_unreferenced") or ()) and kinds:
        # a build the service may run on purpose: never presented as fixed by pointing it at Hermes' Python
        from .hermes_care import gc_block_advice

        for lang in ("en", "pt"):
            values[f"in_use_advice_{lang}"] = " ".join(gc_block_advice(care, lang)) + (
                " Old tool versions are not removed until then." if lang == "en" else
                " Versões antigas de ferramentas não são removidas até lá.")
        return values
    values["in_use_advice_en"] = (
        f"Hermes no longer points at {values['in_use_en']} in its tools (`tools/facts.json`), but something still "
        "starts from there: `hermes pm gc` would delete it and that service would fail at its next start (status "
        "203/EXEC). Old tool versions are not removed until then. First point the service at the current Python "
        "(`hermes evolution care fix-gateway` does it for the gateway's service) or, for the running Hermes process, "
        "restart it (`hermes gateway restart`); then `hermes evolution care gc`.")
    values["in_use_advice_pt"] = (
        f"O Hermes não aponta mais para {values['in_use_pt']} nas ferramentas dele (`tools/facts.json`), mas algo "
        "ainda sobe a partir dali: o `hermes pm gc` apagaria isso e esse serviço falharia na próxima vez que subisse "
        "(status 203/EXEC). Versões antigas de ferramentas não são removidas até lá. Primeiro aponte o serviço para o "
        "Python atual (o `hermes evolution care fix-gateway` faz isso no serviço do gateway) ou, se for o processo do "
        "Hermes em execução, reinicie-o (`hermes gateway restart`); depois, `hermes evolution care gc`.")
    return values


def _trouble(report: dict) -> tuple[dict, list[dict]]:
    """The unit that cannot start (or loops), and every looping unit, from a look now first, then the stored
    checks: the gateway's own unit before any other."""
    # a unit the look now saw is described as it is now, never as the stored checks saw it before a repair
    units = _current_units(report.get("care"), report.get("system"), report.get("live"))
    units.sort(key=lambda unit: "gateway" not in str(unit.get("unit")))   # stable: the live look stays first
    missing = [unit for unit in units if unit.get("exec_missing")
               or unit.get("exec_main_status") == 203 and unit.get("active_state") != "active"]
    looping = [unit for unit in units if unit.get("restart_loop")]
    return (missing or looping or [{}])[0], looping


def _unit_values(report: dict) -> dict:
    unit, looping = _trouble(report)
    name = str(unit.get("unit") or "hermes-gateway.service")
    user = unit.get("scope", "user") == "user"
    systemctl = "systemctl --user" if user else "sudo systemctl"
    source, replacement = unit.get("exec_source"), unit.get("replacement")
    if source and str(source).endswith(".conf"):
        fix_en = (f"Edit `{source}` and correct (to the new path) or remove its stale `ExecStart=` line, or re-run "
                  "Hermes' own service install.")
        fix_pt = (f"Edite `{source}` e corrija (para o caminho novo) ou apague a linha `ExecStart=` antiga, ou rode de "
                  "novo a instalação do serviço do próprio Hermes.")
    elif source:
        fix_en = f"Correct the `ExecStart=` line of `{source}`, or re-run Hermes' own service install."
        fix_pt = f"Corrija a linha `ExecStart=` de `{source}`, ou rode de novo a instalação do serviço do próprio Hermes."
    else:
        fix_en = (f"Correct the unit's `ExecStart=` line (`{systemctl} cat {name}` shows which file sets it), or re-run "
                  "Hermes' own service install.")
        fix_pt = (f"Corrija a linha `ExecStart=` do serviço (`{systemctl} cat {name}` mostra qual arquivo a define), ou "
                  "rode de novo a instalação do serviço do próprio Hermes.")
    age = report.get("health_age")
    hours = round(age / 3600, 1) if isinstance(age, (int, float)) and not isinstance(age, bool) else None
    return {"unit": name, "exec_path": unit.get("exec_path") or "?",
            "replacement_en": f"; the current one is `{replacement}`" if replacement else "",
            "replacement_pt": f"; o atual é `{replacement}`" if replacement else "",
            "restarts": unit.get("n_restarts") if unit.get("n_restarts") is not None else "?",
            "sub_state": unit.get("sub_state") or "?", "fix_en": fix_en, "fix_pt": fix_pt,
            "restart_cmd": f"{systemctl} daemon-reload && {systemctl} reset-failed {name} && {systemctl} start {name}",
            "journal_cmd": (f"journalctl --user -u {name} -n 30" if user else f"sudo systemctl status {name} -n 30 --no-pager"),
            "loop_units": ", ".join(f"`{item['unit']}`" for item in looping) or f"`{name}`",
            "stale_en": "?" if hours is None else (plural(round(age / 60), "{n} minute", "{n} minutes") if hours < 1
                                                   else f"{hours} h"),
            "stale_pt": "?" if hours is None else (plural(round(age / 60), "{n} minuto", "{n} minutos") if hours < 1
                                                   else f"{_comma(hours)} h")}


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


def _ms(value, lang: str) -> str:
    """A duration in milliseconds for a person: 0.04 ms, 1.2 ms, 38 ms (decimal comma in Portuguese)."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "?"
    text = f"{value:.2f}" if value < 1 else f"{value:.1f}" if value < 10 else f"{value:.0f}"
    return (text.replace(".", ",") if lang == "pt" else text) + " ms"


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
    index = report.get("evolution_index") or {}
    if index.get("score") is not None:
        from .evolution_index import sparkline

        trend = index.get("trend_7d")
        moved = "" if trend is None else f" ({'+' if trend > 0 else ''}{trend} {t['index_week']})"
        named = " · ".join(f"{t['index_parts'][name]} {value}" for name, value in (index.get("parts") or {}).items()
                           if value is not None)
        line = sparkline(index.get("series") or [])
        lines.append(f"- **{t['index']}:** {index['score']}/100{moved}" + (f" `{line}`" if len(line) > 1 else "")
                     + (f" · {named}" if named else ""))
        from .insights import moved_words

        story = moved_words((report.get("insights") or {}).get("moved"), lang)
        if story:
            lines.append(f"  - {story}")
    elif index:
        lines.append(f"- **{t['index']}:** {t['index_wait']}")
    cost = (report.get("self_cost") or {}).get("hooks") or {}
    shown = [(hook, cost[hook]) for hook in ("post_tool_call", "pre_tool_call", "prompt_section", "pre_llm_call")
             if hook in cost and cost[hook]["calls"]]
    if shown:
        lines.append(f"- **{t['cost']}:** " + " · ".join(
            t["cost_item"].format(hook=t["cost_hooks"].get(hook, hook), mean=_ms(item.get("p50_ms", item["mean_ms"]), lang),
                                  p95=_ms(item["p95_ms"], lang)) for hook, item in shown))
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
        if code in ("settings_ignored", "settings_problems"):
            lines.append("- " + t[code].format(keys=", ".join(f"`{key}`" for key in report[code])))
            continue
        if code.startswith(("hermes_", "os_")):
            lines.append("- " + t[code].format(**_care_values(report)))
            continue
        if code == "settings_reload":
            lines.append("- " + t[code].format(keys=", ".join(f"`{key}`" for key in report["settings_reload"])))
            continue
        if code == "self_slow":
            cost = report["self_cost"]
            hook = cost["slow"][0]
            lines.append("- " + t[code].format(hook=hook, p95=_ms(cost["hooks"][hook]["p95_ms"], lang)))
            continue
        if code == "db_recovered":
            found = report.get("db_recovered") or {}
            lines.append("- " + t[code].format(copy=found.get("copy") or "?", when=_iso(found.get("at")) or "?"))
            continue
        if code == "legacy_notes":
            notes = report["legacy_notes"]
            one, many = t[code]
            lines.append("- " + (one if notes["count"] == 1 else many).format(
                n=notes["count"], command=notes["command"], forget=notes.get("forget", "")))
            continue
        if code == "failure_spike":
            from .insights import spike_lines

            lines += spike_lines(report["insights"]["spikes"], lang)
            continue
        lines.append(f"- {t[code]}")
        if code == "stale_tick_job":
            import shlex

            for job in report["stale_tick_jobs"]:
                lines.append(f"  - `{job['name']}`: `{_command(report['profile'], 'cron remove ' + shlex.quote(job['id']))}`")
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
    from .insights import _TEXT as insight_text, action_lines

    steps = action_lines((report.get("insights") or {}).get("next_actions") or [], lang)
    if steps:
        lines += ["", f"## {insight_text.get(lang, insight_text['en'])['do_first']}", "", *steps]
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
    from .cli import profile_commands

    # every command as the owner types it in this profile: `hermes -p <profile> evolution …` (cli._hermes_cmd), and
    # Hermes' own commands for this profile, `gateway …` for the gateway that serves it (the root's, when it does)
    served = bool(((report.get("care") or {}).get("gateway") or {}).get("served_by_root"))
    profile = report.get("profile")
    return decimal_comma(profile_commands("\n".join(lines) + "\n", profile,
                                          gateway="default" if served else profile), lang)


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
    if os.environ.get("HERMES_PLUGIN_HOST_PROCESS") == "1":   # read inside the host: isolation_mode() says in_process
        return "host"
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
    from .admin import profile_name
    from .cli import profile_commands

    return {"message": profile_commands(f"{t['lead']}: {'; '.join(parts)}. {t['tail']}", profile_name(home))}



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
           "pinned": "Pinned to a commit in {profiles}: `hermes plugins update` refuses it there (\"is pinned to "
                     "<sha>\"). The command for each profile moves the pin to the commit profile `{lead}` runs; "
                     "then `hermes gateway restart`.",
           "self_skew": "{profiles} run(s) another commit of Atlas Evolution than profile `{lead}` (the version "
                        "number may be the same). Update it in each with Hermes' own "
                        "`hermes -p <profile> plugins update hermes-atlas-evolution`, then `hermes gateway restart`.",
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
           "pinned": "Fixado num commit em {profiles}: ali o `hermes plugins update` recusa (\"is pinned to <sha>\"). "
                     "O comando de cada perfil leva a fixação para o commit que o perfil `{lead}` roda; depois "
                     "`hermes gateway restart`.",
           "self_skew": "{profiles} roda(m) outro commit do Atlas Evolution que o perfil `{lead}` (o número da versão "
                        "pode ser o mesmo). Atualize em cada um com o comando do próprio Hermes, "
                        "`hermes -p <perfil> plugins update hermes-atlas-evolution`, e depois `hermes gateway restart`.",
           "yes": "sim", "no": "não"},
}


def environment_markdown(home: Path, lang: str = "en", *, mode: str = "auto") -> str:
    """The Environment tab and screen: every profile, the catalog, and what to do, from the lead profile. Never
    raises: a damaged record shows as not looked at yet."""
    from .admin import profile_name
    from .cli import profile_commands

    try:
        return profile_commands(decimal_comma(_environment_markdown(home, lang, mode=mode), lang), profile_name(home))
    except Exception:
        return profile_commands(_ENV_TEXT.get(lang, _ENV_TEXT["en"])["not_yet"].format(lead="?") + "\n",
                                profile_name(home))


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
        # the lead profile wrote these commands for itself: they keep its `-p`, whatever profile reads them
        from .cli import profile_commands

        told = {key: profile_commands(value, name) for key, value in explain.describe(item, lang).items()}
        item = {**item, "command": profile_commands(item.get("command"), name)}
        lines.append(f"- **{told['title']}** · {subject_words(item, lang)} · " + t["risk"].format(risk=told["risk"]))
        if told["what"]:
            lines.append(f"  - {t['seen']}: {told['what']}")
        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        if item.get("kind") == "plugin_update" and evidence.get("reasons"):
            for reason in ("pinned", "self_skew"):
                where = [entry.get("profile") for entry in evidence.get("profiles") or ()
                         if isinstance(entry, dict) and entry.get("reason") == reason]
                if where:
                    lines.append(f"  - {t[reason].format(profiles=', '.join(f'`{who}`' for who in where), lead=name)}")
        if told.get("advice"):
            lines.append(f"  - {t['advice']}: {told['advice']}")
        if item.get("command"):
            lines.append(f"  - {t['run']}: `{item['command']}`")
    lines += ["", t["where"].format(lead=name, p=f" -p {name}")]
    return "\n".join(lines) + "\n"
