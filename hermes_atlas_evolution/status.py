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


def _digest_scheduled(home: Path) -> bool:
    """Whether Hermes' scheduler holds an enabled weekly digest of this plugin for the profile."""
    from .workloads import _load_json

    status, data = _load_json(Path(home) / "cron" / "jobs.json", 4 * 1024 * 1024)
    jobs = data.get("jobs") if status == "ok" and isinstance(data, dict) else None
    return any(isinstance(job, dict) and job.get("enabled") is not False
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
CARE_ATTENTION = ("hermes_gateway_heartbeat_stale", "hermes_gateway_memory_growth", "hermes_state_db_unreadable",
                  "hermes_state_db_damaged", "hermes_state_db_wal_large", "hermes_disk_critical", "hermes_disk_low",
                  "hermes_disk_filling", "hermes_storage_growth", "hermes_memory_low", "hermes_swap_full",
                  "hermes_cpu_saturated", "hermes_unused_tools", "hermes_gateway_not_at_boot",
                  "os_dpkg_interrupted", "os_apt_broken", "os_reboot_required", "os_security_updates", "os_unattended_off",
                  "os_lists_stale", "os_unit_not_enabled")


def _gb(value) -> str:
    return f"{value / 2**30:.1f} GB" if isinstance(value, (int, float)) else "?"


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
        "auto_actions": {"mode": opts["auto_actions"], "last_7d": automatic},
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
    mine = next((row for row in report["profiles"] if row["current"]), None)
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
           "today": "Today (UTC)", "tool_calls": "tool calls", "turns": "turns", "model_calls": "model calls",
           "autopilot": "Autopilot", "every": "every {minutes} min", "off": "off (run `hermes evolution learn` yourself)",
           "last": "last pass", "ok": "ok", "failed": "failed",
           "never": "has not run yet; the clock starts it within 2 minutes of the plugin loading, or run `hermes evolution learn`",
           "backfill": "Hermes history", "reflector": "Reflector", "on": "on", "guard_on": "on", "guard_off": "off",
           "calls": "calls today",
           "drafts": "drafts waiting", "lessons": "Lessons", "mode": "mode", "active": "active",
           "awaiting": "awaiting approval", "retired": "retired", "withdrawn": "withdrawn", "proposals": "Open proposals",
           "guard": "Runtime guard", "vetoed": "commands vetoed in 7 days", "attention": "Needs attention",
           "auto": "Automatic actions", "auto_modes": {"off": "off", "safe": "safe (memory repeats)",
                                                       "full": "full (memory repeats and unstable skills)"},
           "auto_done": "{count} in 7 days",
           "guard_covers": "knows the gateway as a systemd unit (Linux), a LaunchAgent (macOS) and a Windows service or task, and keeps the agent from changing this plugin's settings",
           "guard_host": "off: plugins run in a plugin host here (plugins.isolation: host), where a crashed host would "
                         "block every tool",
           "no_events_today": "No event from the hooks today. If the profile was used today, check that Hermes loaded the plugin in this profile (`hermes plugins list`).",
           "autopilot_off": "The autopilot is off: the loop only runs when you call `hermes evolution learn`.",
           "no_autopilot_pass": "The autopilot has not run a pass in this profile yet.",
           "autopilot_failing": "The last autopilot pass failed; it is retried in 10 minutes. See Hermes' agent.log.",
           "lessons_waiting": "Lessons are waiting for your approval: decide in the menu (`hermes evolution`) or with `hermes evolution lessons review`, in your own terminal.",
           "profiles_without_plugin": "Profiles used in the last 7 days that do not have the plugin (each profile loads its own plugins):",
           "owner_unknown": "These chat channels count as someone else's, so they get template lessons only and their failures wait for your approval. If one of them is you, run `hermes evolution owner` in your own terminal:",
           "chat_item": "`{origin}`: {sessions} sessions in 7 days from {senders} sender(s)",
           "hermes_gateway_heartbeat_stale": "The gateway says it is running but has not written its heartbeat for {age} s: it may be stuck. Look at `hermes gateway status`.",
           "hermes_gateway_memory_growth": "The gateway uses {rss} of memory, more than twice its usual: a restart frees it (`hermes gateway restart`, from your terminal).",
           "hermes_state_db_unreadable": "Hermes' session database could not be read. Run Hermes' check: menu → Hermes health → `hermes doctor`.",
           "hermes_state_db_damaged": "Hermes' session database failed its integrity check. Back it up and repair it: menu → Hermes health → repair.",
           "hermes_state_db_wal_large": "The session database's write-ahead log is {wal}: Hermes folds it back by itself when nothing writes; `hermes doctor --fix` does it now.",
           "hermes_disk_critical": "Only {free} free where Hermes writes: below Hermes' own floor. Free room now (see Hermes health for what grew).",
           "hermes_disk_low": "Only {free} free where Hermes writes. See Hermes health for what grew and what can go.",
           "hermes_disk_filling": "At this pace the disk Hermes writes to is full in about {days} days.",
           "hermes_storage_growth": "Hermes' folders grew fast: {grown}.",
           "hermes_memory_low": "The machine has little memory left ({available} of {total}).",
           "hermes_swap_full": "The swap is almost full ({used} of {swap}): the machine is short of memory and everything slows down.",
           "hermes_cpu_saturated": "The processors are saturated (load {load} on {cpus} CPUs over 15 minutes).",
           "hermes_unused_tools": "Old versions of Hermes' tools take {size} and nothing uses them: menu → Hermes health → `hermes pm gc`.",
           "hermes_gateway_not_at_boot": "The gateway runs as a user service without linger: after a reboot it does not start. Fix: `loginctl enable-linger {user}`.",
           "os_dpkg_interrupted": "An update of the system stopped half-way (dpkg). Finish it before anything else: menu → Operating system → repair.",
           "os_apt_broken": "The system's package manager reports broken dependencies: menu → Operating system → repair.",
           "os_reboot_required": "The system waits for a reboot ({packages}). Plan it: Hermes' services come back by themselves when they are enabled.",
           "os_security_updates": "{count} security update(s) of the system are waiting: menu → Operating system.",
           "os_unattended_off": "Automatic security updates of the system are off (unattended-upgrades).",
           "os_lists_stale": "The system's package lists were not refreshed for more than 7 days.",
           "os_unit_not_enabled": "A Hermes service is not enabled: after a reboot it does not start ({units}).",
           "health": "Hermes and the system", "health_gateway": "Gateway", "health_db": "Session database",
           "health_disk": "Disk", "health_memory": "Memory", "health_os": "System", "running": "running",
           "stopped": "not running", "free_of": "free of", "swap": "swap", "updates": "updates waiting",
           "security": "security", "reboot": "reboot waiting",
           "digest_unscheduled": "Decisions wait for you and nothing reminds you of them (Hermes 0.23.1 drops end-of-session notices). Schedule the weekly digest to your chat: the menu's suggestion, or `hermes evolution schedule-digest --deliver <channel>` (a Hermes cron job with no agent).",
           "settings_reload": "These settings changed and apply at the next load of the plugin: {keys}. Hermes reloads plugins in a running gateway when one is enabled, disabled or updated; `hermes gateway restart` also does it.",
           "settings_ignored": "These values in `settings.json` are ignored: {keys}. They would widen whom the plugin trusts or turn a protection off, and the agent's own file tools can write that file. Set the ones you want in the Hermes settings form, with `hermes evolution owner` (sender ids and platforms), or with `hermes config set plugins.entries.hermes-atlas-evolution.settings.<key> <value>` in your own terminal.",
           "sessions": "sessions", "none": "Nothing needs attention.",
           "stale_runtime": "A running Hermes process still runs an older copy of the plugin than the one on disk (`hermes plugins update` does not reload a running gateway). Run `hermes gateway restart`; it waits for the running turns.",
           "stale_item": "pid {pid} ({kind}) loaded {version}",
           "legacy_install": "These profiles still have the plugin under its old id hermes-evolution (up to 0.5.0), which keeps running while a new copy there stays inactive. Run the command shown for each; the data it learned stays:",
           "busier_profile": "This profile is barely used. `{profile}` had {sessions} sessions in 7 days; see it with `{status}`.",
           "profiles": "Profiles of this Hermes", "col_profile": "profile", "col_sessions": "sessions (7 d)",
           "col_plugin": "plugin", "col_last": "last pass", "col_lessons": "lessons active / waiting",
           "states": {"installed": "installed", "legacy": "old id (0.5.0)", "both": "both copies: the old one runs",
                      "missing": "not installed"},
           "first_pass": "First pass done now: {sessions} sessions, {tool_calls} tool calls of history read.",
           "statuses": {"done": "read", "running": "being read", "waiting": "waiting for the first pass",
                        "no_history": "no history", "unreadable": "unreadable", "unsupported_history": "unsupported format"}},
    "pt": {"title": "Atlas Evolution {version} · perfil `{profile}`", "since": "Observando desde",
           "today": "Hoje (UTC)", "tool_calls": "chamadas de ferramentas", "turns": "turnos", "model_calls": "chamadas ao modelo",
           "autopilot": "Autopilot", "every": "a cada {minutes} min", "off": "desligado (rode `hermes evolution learn` você mesmo)",
           "last": "última passada", "ok": "ok", "failed": "falhou",
           "never": "ainda não rodou; o relógio a inicia em até 2 minutos depois de o plugin carregar, ou rode `hermes evolution learn`",
           "backfill": "Histórico do Hermes", "reflector": "Refletor", "on": "ligado", "guard_on": "ligada",
           "guard_off": "desligada", "calls": "chamadas hoje",
           "drafts": "rascunhos aguardando", "lessons": "Lições", "mode": "modo", "active": "ativas",
           "awaiting": "aguardando aprovação", "retired": "aposentadas", "withdrawn": "retiradas", "proposals": "Propostas abertas",
           "guard": "Guarda de runtime", "vetoed": "comandos barrados em 7 dias", "attention": "Precisa de atenção",
           "auto": "Ações automáticas", "auto_modes": {"off": "desligadas", "safe": "seguras (repetições da memória)",
                                                       "full": "completas (repetições da memória e skills instáveis)"},
           "auto_done": "{count} em 7 dias",
           "guard_covers": "reconhece o gateway como unidade systemd (Linux), LaunchAgent (macOS) e serviço ou tarefa "
                           "do Windows, e impede o agente de mudar as configurações deste plugin",
           "guard_host": "desligada: aqui os plugins rodam num host de plugins (plugins.isolation: host), onde um host "
                         "que caísse bloquearia todas as ferramentas",
           "no_events_today": "Nenhum evento dos hooks hoje. Se o perfil foi usado hoje, confira se o Hermes carregou o plugin neste perfil (`hermes plugins list`).",
           "autopilot_off": "O autopilot está desligado: o ciclo só roda quando você chama `hermes evolution learn`.",
           "no_autopilot_pass": "O autopilot ainda não fez nenhuma passada neste perfil.",
           "autopilot_failing": "A última passada do autopilot falhou; ela é repetida em 10 minutos. Veja o agent.log do Hermes.",
           "lessons_waiting": "Há lições aguardando a sua aprovação: decida pelo menu (`hermes evolution`) ou com `hermes evolution lessons review`, no seu próprio terminal.",
           "profiles_without_plugin": "Perfis usados nos últimos 7 dias que não têm o plugin (cada perfil carrega os próprios plugins):",
           "owner_unknown": "Estes canais de chat contam como de outra pessoa, então recebem só lições de modelo fixo e as falhas deles esperam a sua aprovação. Se um deles é você, rode `hermes evolution owner` no seu próprio terminal:",
           "chat_item": "`{origin}`: {sessions} sessões em 7 dias de {senders} remetente(s)",
           "hermes_gateway_heartbeat_stale": "O gateway diz que está rodando, mas não grava o sinal de vida há {age} s: pode estar travado. Veja `hermes gateway status`.",
           "hermes_gateway_memory_growth": "O gateway usa {rss} de memória, mais que o dobro do normal: reiniciar libera (`hermes gateway restart`, no seu terminal).",
           "hermes_state_db_unreadable": "Não foi possível ler o banco de sessões do Hermes. Rode a checagem do Hermes: menu → Saúde do Hermes → `hermes doctor`.",
           "hermes_state_db_damaged": "O banco de sessões do Hermes falhou na checagem de integridade. Faça um backup e repare: menu → Saúde do Hermes → reparar.",
           "hermes_state_db_wal_large": "O log de escrita do banco de sessões está com {wal}: o Hermes o recolhe sozinho quando nada escreve; o `hermes doctor --fix` faz isso agora.",
           "hermes_disk_critical": "Só {free} livres onde o Hermes grava: abaixo do mínimo do próprio Hermes. Libere espaço agora (veja em Saúde do Hermes o que cresceu).",
           "hermes_disk_low": "Só {free} livres onde o Hermes grava. Veja em Saúde do Hermes o que cresceu e o que pode sair.",
           "hermes_disk_filling": "Neste ritmo, o disco onde o Hermes grava enche em cerca de {days} dias.",
           "hermes_storage_growth": "Pastas do Hermes cresceram rápido: {grown}.",
           "hermes_memory_low": "A máquina tem pouca memória livre ({available} de {total}).",
           "hermes_swap_full": "O swap está quase cheio ({used} de {swap}): falta memória na máquina e tudo fica lento.",
           "hermes_cpu_saturated": "Os processadores estão saturados (carga {load} em {cpus} CPUs nos últimos 15 minutos).",
           "hermes_unused_tools": "Versões antigas das ferramentas do Hermes ocupam {size} e nada as usa: menu → Saúde do Hermes → `hermes pm gc`.",
           "hermes_gateway_not_at_boot": "O gateway roda como serviço do usuário sem linger: depois de um reinício ele não sobe. Correção: `loginctl enable-linger {user}`.",
           "os_dpkg_interrupted": "Uma atualização do sistema parou no meio (dpkg). Termine-a antes de qualquer coisa: menu → Sistema operacional → reparar.",
           "os_apt_broken": "O gerenciador de pacotes do sistema relata dependências quebradas: menu → Sistema operacional → reparar.",
           "os_reboot_required": "O sistema espera um reinício ({packages}). Planeje-o: os serviços do Hermes voltam sozinhos quando estão habilitados.",
           "os_security_updates": "{count} atualização(ões) de segurança do sistema esperam: menu → Sistema operacional.",
           "os_unattended_off": "As atualizações automáticas de segurança do sistema estão desligadas (unattended-upgrades).",
           "os_lists_stale": "As listas de pacotes do sistema não são atualizadas há mais de 7 dias.",
           "os_unit_not_enabled": "Um serviço do Hermes não está habilitado: depois de um reinício ele não sobe ({units}).",
           "health": "Hermes e sistema", "health_gateway": "Gateway", "health_db": "Banco de sessões",
           "health_disk": "Disco", "health_memory": "Memória", "health_os": "Sistema", "running": "rodando",
           "stopped": "parado", "free_of": "livres de", "swap": "swap", "updates": "atualizações esperando",
           "security": "de segurança", "reboot": "reinício pendente",
           "digest_unscheduled": "Há decisões esperando por você e nada as lembra (o Hermes 0.23.1 descarta os avisos de fim de sessão). Agende o resumo semanal no seu chat: a sugestão do menu, ou `hermes evolution schedule-digest --deliver <canal>` (um job de cron do Hermes sem agente).",
           "settings_reload": "Estas configurações mudaram e valem na próxima carga do plugin: {keys}. O Hermes recarrega os plugins num gateway em execução quando um é ligado, desligado ou atualizado; `hermes gateway restart` também faz isso.",
           "settings_ignored": "Estes valores do `settings.json` são ignorados: {keys}. Eles ampliariam em quem o plugin confia ou desligariam uma proteção, e as ferramentas de arquivo do próprio agente conseguem gravar esse arquivo. Defina os que você quer no formulário de configurações do Hermes, com o `hermes evolution owner` (ids de remetente e plataformas) ou com `hermes config set plugins.entries.hermes-atlas-evolution.settings.<chave> <valor>` no seu próprio terminal.",
           "sessions": "sessões", "none": "Nada precisa de atenção.",
           "stale_runtime": "Um processo do Hermes em execução ainda roda uma cópia do plugin mais antiga que a do disco (o `hermes plugins update` não recarrega um gateway em execução). Rode `hermes gateway restart`; ele espera os turnos em andamento.",
           "stale_item": "pid {pid} ({kind}) carregou a {version}",
           "legacy_install": "Estes perfis ainda têm o plugin com o nome antigo hermes-evolution (até a 0.5.0), que continua rodando enquanto uma cópia nova ali fica inativa. Rode o comando de cada um; os dados aprendidos ficam:",
           "busier_profile": "Este perfil quase não é usado. O `{profile}` teve {sessions} sessões em 7 dias; veja com `{status}`.",
           "profiles": "Perfis deste Hermes", "col_profile": "perfil", "col_sessions": "sessões (7 d)",
           "col_plugin": "plugin", "col_last": "última passada", "col_lessons": "lições ativas / aguardando",
           "states": {"installed": "instalado", "legacy": "nome antigo (0.5.0)", "both": "duas cópias: a antiga roda",
                      "missing": "não instalado"},
           "first_pass": "Primeira passada feita agora: {sessions} sessões e {tool_calls} chamadas de ferramentas do histórico lidas.",
           "statuses": {"done": "lido", "running": "em leitura", "waiting": "aguardando a primeira passada",
                        "no_history": "sem histórico", "unreadable": "ilegível", "unsupported_history": "formato não suportado"}},
}


def _care_values(report: dict) -> dict:
    care, system = report.get("care") or {}, report.get("system") or {}
    gate, memory = care.get("gateway") or {}, care.get("memory") or {}
    load = care.get("load") or {}
    grown = ", ".join(f"`{item['folder']}` {_gb(item['before'])} → {_gb(item['now'])}" for item in care.get("growth") or ())
    units = ", ".join(f"`{unit['unit']}`" for unit in system.get("units") or () if not unit.get("enabled"))
    return {"age": gate.get("heartbeat_age"), "rss": _gb(gate.get("rss")), "wal": _gb((care.get("database") or {}).get("wal")),
            "free": _gb(care.get("free")), "days": care.get("days_to_full"), "grown": grown or "?",
            "available": _gb(memory.get("available")), "total": _gb(memory.get("total")),
            "used": _gb(memory.get("swap_used")), "swap": _gb(memory.get("swap_total")),
            "load": load.get("15m"), "cpus": load.get("cpus"), "size": _gb(care.get("reclaimable")),
            "user": os.environ.get("USER") or "root", "packages": ", ".join(system.get("reboot_packages") or ()) or "?",
            "count": system.get("security") or 0, "units": units or "?"}


def _health_lines(report: dict, t: dict) -> list[str]:
    care, system = report.get("care"), report.get("system")
    if not care and not system:
        return []
    lines = ["", f"## {t['health']}", ""]
    if care:
        gate, memory, db = care.get("gateway") or {}, care.get("memory") or {}, care.get("database") or {}
        state = t["running"] if gate.get("running") else t["stopped"]
        lines.append(f"- **{t['health_gateway']}:** {state}" + (f" · {gate.get('version')}" if gate.get("version") else "")
                     + (f" · RSS {_gb(gate.get('rss'))}" if gate.get("rss") else ""))
        if db.get("present"):
            lines.append(f"- **{t['health_db']}:** {db.get('journal') or '?'} · WAL {_gb(db.get('wal'))}"
                         + (f" · quick_check {db['quick_check']}" if db.get("quick_check") else ""))
        if care.get("free") is not None:
            lines.append(f"- **{t['health_disk']}:** {_gb(care['free'])} {t['free_of']} {_gb(care.get('total'))}")
        if memory.get("total"):
            lines.append(f"- **{t['health_memory']}:** {_gb(memory.get('available'))} {t['free_of']} {_gb(memory['total'])}"
                         + (f" · {t['swap']} {_gb(memory.get('swap_used'))}/{_gb(memory.get('swap_total'))}"
                            if memory.get("swap_total") else ""))
    if system and system.get("supported"):
        parts = [str((system.get("os") or {}).get("name") or "?")]
        parts.append(f"{system.get('upgradable', 0)} {t['updates']} ({system.get('security', 0)} {t['security']})")
        if system.get("reboot_required"):
            parts.append(t["reboot"])
        lines.append(f"- **{t['health_os']}:** " + " · ".join(parts))
    return lines


def markdown(report: dict, lang: str = "en") -> str:
    t = _TEXT[lang if lang in _TEXT else "en"]
    today, pilot, history = report["today"], report["autopilot"], report["backfill"]
    lines = ["# " + t["title"].format(version=report["plugin_version"], profile=report["profile"]), ""]
    if report["observing_since"]:
        lines.append(f"- **{t['since']}:** {report['observing_since']}")
    lines.append(f"- **{t['today']}:** {today['turns']} {t['turns']} · {today['tool_calls']} {t['tool_calls']} · "
                 f"{today['model_calls']} {t['model_calls']}")
    if pilot["minutes"]:
        last = pilot["last_pass"]
        state = (f"{t['last']} {last['at']} · {t['ok'] if last.get('ok') else t['failed']}" if last else t["never"])
        lines.append(f"- **{t['autopilot']}:** {t['every'].format(minutes=pilot['minutes'])} · {state}")
    else:
        lines.append(f"- **{t['autopilot']}:** {t['off']}")
    counts = ", ".join(f"{history[key]} {t[key] if key in t else key}" for key in ("turns", "tool_calls") if key in history)
    lines.append(f"- **{t['backfill']}:** {t['statuses'].get(history['status'], history['status'])}"
                 + (f" · {history.get('sessions', 0)} {t['sessions']}, {counts}" if counts else ""))
    reflector = report["reflector"]
    lines.append(f"- **{t['reflector']}:** " + (f"{t['on']} · {reflector['calls_today']}/{reflector['daily_limit']} "
                 f"{t['calls']} · {reflector['drafts_waiting']} {t['drafts']}" if reflector["enabled"] else t["off"].split(" (")[0]))
    playbook = report["lessons"]
    lines.append(f"- **{t['lessons']}:** {t['mode']} `{playbook['mode']}` · {playbook['active']} {t['active']} · "
                 f"{playbook['awaiting_approval']} {t['awaiting']} · {playbook['retired']} {t['retired']}"
                 + (f" · {playbook['withdrawn']} {t['withdrawn']}" if playbook.get("withdrawn") else ""))
    lines.append(f"- **{t['proposals']}:** {report['proposals_open']}")
    guard = report["runtime_guard"]
    if guard.get("state") == "host":
        state = t["guard_host"]
    elif guard["enabled"]:
        state = f"{t['guard_on']} · {guard['vetoed_7d']} {t['vetoed']} · {t['guard_covers']}"
    else:
        state = t["guard_off"]
    lines.append(f"- **{t['guard']}:** {state}")
    automatic = report.get("auto_actions") or {}
    if automatic:
        lines.append(f"- **{t['auto']}:** {t['auto_modes'].get(automatic.get('mode'), automatic.get('mode'))} · "
                     + t["auto_done"].format(count=automatic.get("last_7d", 0)))
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
                lines.append(f"  - `{item['profile']}` ({item['sessions_7d']} {t['sessions']}): `{item['install']}`")
        if code == "owner_unknown":
            for item in report["chats"]:
                if not item["covered"]:
                    lines.append("  - " + t["chat_item"].format(**item))
    if not report["attention"]:
        lines.append(t["none"])
    lines += _health_lines(report, t)
    shown = [row for row in report.get("profiles", []) if row["sessions_7d"] or row["plugin"] != "missing" or row["current"]]
    if shown:
        lines += ["", f"## {t['profiles']}", "",
                  f"| {t['col_profile']} | {t['col_sessions']} | {t['col_plugin']} | {t['col_last']} | {t['col_lessons']} |",
                  "|---|---:|---|---|---:|"]
        for row in sorted(shown, key=lambda item: (-item["sessions_7d"], item["profile"])):
            name = f"**{row['profile']}**" if row["current"] else row["profile"]
            lessons = f"{row['lessons_active']} / {row['lessons_waiting']}" if row["plugin"] != "missing" else "—"
            lines.append(f"| {name} | {row['sessions_7d']} | {t['states'][row['plugin']]} | {row['last_pass'] or '—'} | {lessons} |")
    return "\n".join(lines) + "\n"


_NOTICE = {
    "en": {"lessons": "{n} lesson(s) awaiting your approval", "drafts": "{n} reflector draft(s) to read",
           "risky": "{n} high-risk proposal(s)", "failing": "the last learning pass failed",
           "lead": "Atlas Evolution", "tail": "See `hermes evolution status`."},
    "pt": {"lessons": "{n} lição(ões) aguardando a sua aprovação", "drafts": "{n} rascunho(s) do refletor para ler",
           "risky": "{n} proposta(s) de risco alto", "failing": "a última passada de aprendizado falhou",
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
            parts = [text.format(n=count) for count, text in ((waiting, t["lessons"]), (drafts, t["drafts"]),
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

