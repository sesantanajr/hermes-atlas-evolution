"""Care of the Hermes installation itself, without the agent and without tokens.

`check` reads only what Hermes already writes, with no network, no model and no `hermes` command (any `hermes`
command may start Hermes' own maintenance): the gateway's state and heartbeat (alive, its memory, the machine's
memory and swap), the session database (journal mode, a write-ahead log that grew, a quick integrity check once a
week), room on the disk Hermes writes to and how fast each of its folders grows (with the day the disk would be full
at that pace), old versions of Hermes' tools nothing uses anymore (and the ones facts.json dropped that a service
still starts, which hold back `hermes pm gc`: gc_blocked), and whether the gateway comes back after a reboot. The
learning pass runs it at most every 6 hours and keeps a short history to see trends.

What fixes things stays with the owner, in their terminal, with Hermes' own commands, each explained first:
`hermes pm doctor` (integrity of Hermes' installed files; offline), `hermes doctor` (Hermes' full check; it calls
the configured providers' APIs and, for an endpoint ending in /anthropic, sends a one-token ping), `hermes doctor
--fix` after `hermes backup --quick` (a snapshot Hermes can restore), and `hermes pm gc` (removes tool versions
nothing references). None of them involves the agent.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

CHECK_EVERY_SECONDS = 6 * 3600
QUICK_CHECK_EVERY_SECONDS = 7 * 86_400
HISTORY_DAYS = 21
SIZE_BUDGET_SECONDS = 8.0
HEARTBEAT_STALE_SECONDS = 180
WAL_LARGE = 64 * 1024 * 1024
DISK_CRITICAL, DISK_LOW = 256 * 1024 * 1024, 1024 * 1024 * 1024   # Hermes' own floor, and a day's margin
GROWTH_BYTES, GROWTH_RATIO = 1024 * 1024 * 1024, 0.5
FOLDERS = ("sessions", "logs", "checkpoints", "cache", "image_cache", "audio_cache", "tools", "backups",
           "state-snapshots", "plugin-data", "evolution", "skills", "memories", "cron", "kanban", "browser",
           "workspace")


def _read_json(path: Path, limit: int = 2_000_000):
    try:
        with path.open("rb") as stream:
            body = stream.read(limit + 1)
        return json.loads(body) if len(body) <= limit else None
    except (OSError, ValueError, RecursionError):
        return None


def _size(path: Path, deadline: float) -> int | None:
    """Bytes under `path` (no symlink followed), or None when the time budget ran out."""
    total = 0
    stack = [path]
    while stack:
        if time.monotonic() > deadline:
            return None
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def sizes(home: Path, *, budget: float = SIZE_BUDGET_SECONDS) -> dict:
    home = Path(home)
    deadline = time.monotonic() + budget
    found = {}
    for name in FOLDERS:
        folder = home / name
        if folder.is_dir() and not folder.is_symlink():
            found[name] = _size(folder, deadline)
    for name in ("state.db", "state.db-wal"):
        try:
            found[name] = (home / name).stat().st_size
        except OSError:
            pass
    return found


def _meminfo(root: Path) -> dict:
    values = {}
    try:
        for line in (root / "proc" / "meminfo").read_text(encoding="utf-8").splitlines():
            key, _, rest = line.partition(":")
            number = rest.strip().split()
            if number and number[0].isdigit():
                values[key] = int(number[0]) * 1024
    except OSError:
        return {}
    return values


def _started(pid: int) -> int | None:
    """The process' start time as Hermes records it (field 22 of /proc/<pid>/stat), None where unknown."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as stream:
            return int(stream.read().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _served_by_root(home: Path) -> Path | None:
    """The root of the Hermes when its gateway serves this named profile: Hermes 0.23.1 runs one gateway for every
    profile, and a profile it serves has no gateway_state.json or gateway.pid of its own (both stay in the root, whose
    state lists it in `served_profiles`). None otherwise."""
    home = Path(home)
    if home.parent.name != "profiles" or (home / "gateway_state.json").exists() or (home / "gateway.pid").exists():
        return None
    root = home.parent.parent
    marker = _read_json(root / "gateway_state.json")
    served = marker.get("served_profiles") if isinstance(marker, dict) else None
    return root if isinstance(served, list) and home.name in served else None


def gateway(home: Path, *, now: float) -> dict:
    root = _served_by_root(Path(home))
    where = root or Path(home)   # a served profile's gateway is the root's: its state and heartbeat are there
    state = _read_json(where / "gateway_state.json")
    beat = _read_json(where / "state" / "gateway.heartbeat")
    state, beat = (state if isinstance(state, dict) else {}), (beat if isinstance(beat, dict) else {})
    updated = None
    try:
        updated = (where / "state" / "gateway.heartbeat").stat().st_mtime
    except OSError:
        pass
    mem = beat.get("mem") if isinstance(beat.get("mem"), dict) else {}
    running = state.get("gateway_state") == "running"
    crashed = False
    fresh = updated is not None and now - updated <= HEARTBEAT_STALE_SECONDS
    if running and not fresh and os.name == "posix" and isinstance(state.get("pid"), int) and state["pid"] > 0:
        # (on Windows, os.kill(pid, 0) sends CTRL_C_EVENT instead of probing: no crash is guessed there)
        # A fresh heartbeat means it runs, maybe in another PID namespace (a container on the same folder), where
        # its pid means nothing here. Without one: its process gone, or the pid now someone else's, is a crash.
        try:
            os.kill(state["pid"], 0)
            # the pid now someone else's only when both start times are known and differ: a missing or odd
            # start_time in the state file proves nothing, and a live gateway is never called crashed for it
            expected, started = state.get("start_time"), _started(state["pid"])
            gone = type(expected) is int and type(started) is int and started != expected
        except ProcessLookupError:
            gone = True
        except OSError:
            gone = False
        if gone:
            running, crashed = False, True
    return {"state": state.get("gateway_state"), "version": state.get("code_version"), "running": running,
            "crashed": crashed, "served_by_root": root is not None,
            "active_agents": state.get("active_agents"),
            "heartbeat_age": None if updated is None else round(now - updated),
            "stale": bool(running and (updated is None or now - updated > HEARTBEAT_STALE_SECONDS)),
            "rss": mem.get("rss_kib") * 1024 if isinstance(mem.get("rss_kib"), int) else None}


QUICK_CHECK_SECONDS = 5.0


def database(home: Path, *, quick: bool) -> dict:
    path = Path(home) / "state.db"
    report: dict = {"present": path.exists()}
    if not report["present"]:
        return report
    try:
        report["wal"] = (Path(home) / "state.db-wal").stat().st_size
    except OSError:
        report["wal"] = 0
    try:
        from urllib.parse import quote

        conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=2)
        try:
            report["journal"] = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if quick:   # read-only and bounded: a large database is checked for at most QUICK_CHECK_SECONDS
                limit = time.monotonic() + QUICK_CHECK_SECONDS
                conn.set_progress_handler(lambda: 1 if time.monotonic() > limit else 0, 10_000)
                try:
                    report["quick_check"] = conn.execute("PRAGMA quick_check(3)").fetchone()[0]
                except sqlite3.OperationalError as error:
                    if "interrupt" not in str(error).lower():
                        raise
                    report["quick_check"] = None   # not finished in time: unknown, never "damaged"
                finally:
                    conn.set_progress_handler(None, 0)
        finally:
            conn.close()
    except sqlite3.Error as error:
        report["error"] = type(error).__name__
    return report


_VERSIONED = re.compile(r"^(?P<name>[a-z][a-z0-9-]*?)-(?P<version>\d[\w.+-]*)$")


def _in_use_by(store: Path, sources) -> dict[str, list[str]]:
    """{folder: [who]}: the folders of the tool store that a running program or a service's ExecStart points into
    (`sources`: paths, or (who, path) pairs). Never unused, whatever facts.json says (removing one would delete the
    interpreter a service starts)."""
    found: dict[str, list[str]] = {}
    try:
        base = os.path.normcase(os.path.realpath(store)).replace("\\", "/").rstrip("/") + "/"
    except (OSError, ValueError):
        return found
    for source in sources:
        who, raw = source if isinstance(source, tuple) and len(source) == 2 else (None, source)
        if not raw:
            continue
        try:
            candidates = {str(raw), os.path.realpath(str(raw))}
        except (OSError, ValueError):
            candidates = {str(raw)}
        names = set()
        for candidate in candidates:
            folded = os.path.normcase(candidate).replace("\\", "/")
            if folded.startswith(base):
                names.add(folded[len(base):].split("/", 1)[0])
            parts = str(candidate).replace("\\", "/").split("/")
            for index in range(1, len(parts)):   # any .../tools/<folder>/... (a unit names the root's own store)
                if parts[index - 1] == "tools" and parts[index]:
                    names.add(parts[index])
        for name in names:
            users = found.setdefault(name, [])
            if who and who not in users:
                users.append(str(who)[:120])
    return found



def tool_store(home: Path, *, budget: float = 4.0, in_use=()) -> dict:
    """What `hermes pm gc` would do to the tool store: {"unused": [...] (unused_tools), "in_use_unreferenced":
    [folder, ...], "in_use_by": {folder: [who]}}. `in_use_unreferenced`: folders facts.json no longer points at, so
    `pm gc` deletes them, that a Hermes service starts a program from or this process runs from: a gc now would
    leave that service failing at its next start (status=203/EXEC)."""
    import sys

    from .environment import root_of

    report: dict = {"unused": [], "in_use_unreferenced": [], "in_use_by": {}}
    store = root_of(Path(home)) / "tools"   # one store for every profile of a Hermes, at its root
    protected = _in_use_by(store, [*in_use, ("process", sys.executable)])
    facts = _read_json(store / "facts.json")
    packages = facts.get("packages") if isinstance(facts, dict) else None
    if not isinstance(packages, dict):
        return report
    used = {entry.get("entry") for entry in packages.values() if isinstance(entry, dict)}
    deadline = time.monotonic() + budget
    try:
        children = sorted(store.iterdir())
    except OSError:
        return report
    for child in children:
        if not child.is_dir() or child.is_symlink() or child.name in used:
            continue
        match = _VERSIONED.match(child.name)
        name = match["name"] if match else None
        base = next((key for key in packages if name and (name == key or name.startswith(key + "-"))), None)
        if base is None:
            continue   # not a version Hermes manages: `pm gc` leaves it
        if child.name in protected:   # `pm gc` would remove it all the same: what starts from it must move first
            report["in_use_unreferenced"].append(child.name)
            report["in_use_by"][child.name] = protected[child.name][:5]
            continue
        report["unused"].append({"folder": child.name, "package": base, "bytes": _size(child, deadline)})
    return report


def unused_tools(home: Path, *, budget: float = 4.0, in_use=()) -> list[dict]:
    """Versions of Hermes' managed tools that `tools/facts.json` no longer points at (what `hermes pm gc` removes).
    Only names Hermes manages count; anything else in the folder is left out, and so is any folder a Hermes service
    starts a program from (`in_use`: the ExecStart paths of the hermes* units) or this process runs from: those are
    tool_store's `in_use_unreferenced`, which stop `pm gc` altogether (gc_blocked)."""
    return tool_store(home, budget=budget, in_use=in_use)["unused"]


def gc_blocked(care) -> bool:
    """Whether `hermes pm gc` must not run now (the Auto mode's reclaim, `care gc`, the menu): by the health check
    `care` (check or cached), a tools folder facts.json no longer points at is one a Hermes service or a running
    process still starts from, and gc would delete it. A check without that answer (none yet, or stored by an
    older version) proves nothing: blocked until a new one."""
    if not isinstance(care, dict) or not isinstance(care.get("in_use_unreferenced"), list):
        return True
    return bool(care["in_use_unreferenced"])


def _growth(history: list[dict], current: dict) -> list[dict]:
    """Folders that grew by at least GROWTH_BYTES and GROWTH_RATIO since about a week ago (or the oldest record)."""
    if not history:
        return []
    old = history[0].get("sizes") if isinstance(history[0], dict) else None
    if not isinstance(old, dict):
        return []
    grown = []
    for name, value in current.items():
        before = old.get(name)
        if isinstance(value, int) and isinstance(before, int) and value - before >= GROWTH_BYTES \
                and value >= before * (1 + GROWTH_RATIO):
            grown.append({"folder": name, "before": before, "now": value, "days": history[0].get("days_ago")})
    return grown


def _when(item) -> float | None:
    """A history record's time, None when the record is damaged."""
    try:
        value = float(item.get("at"))
    except (TypeError, ValueError, AttributeError, OverflowError):
        return None
    return value if value == value and abs(value) != float("inf") else None


def expected_gateway_units(home: Path | None, *, served: bool = False) -> list[str]:
    """The names Hermes gives the gateway's unit of this profile: `hermes-gateway.service` for the root profile,
    `hermes-gateway-<profile>.service` for a named one (and the root's, when its gateway serves this profile)."""
    if home is None or Path(home).parent.name != "profiles":
        return ["hermes-gateway.service"]
    return [f"hermes-gateway-{Path(home).name}.service", *(["hermes-gateway.service"] if served else [])]


def _other_profiles(home: Path | None, *, served: bool = False) -> set[str]:
    """The gateway unit names of the other profiles of this Hermes: never this profile's gateway."""
    if home is None:
        return set()
    home = Path(home)
    named = home.parent.name == "profiles"
    root = home.parent.parent if named else home
    try:
        names = [entry.name for entry in (root / "profiles").iterdir() if entry.is_dir()][:200]
    except OSError:
        names = []
    found = {f"hermes-gateway-{name}.service" for name in names if not named or name != home.name}
    if named and not served:
        found.add("hermes-gateway.service")   # the root profile's own
    return found


def pick_gateway_unit(units, *, home: Path | None = None, served: bool = False, prefer=()) -> dict | None:
    """The unit that runs this profile's gateway, among Hermes' units: one of `prefer` ((scope, unit) pairs, say the
    one the last check picked) first, then the name Hermes gives this profile's gateway, never another profile's
    while there is a better one, then one that runs, comes back after a reboot or fails (a leftover that is off says
    nothing), a user one before a system one (Hermes installs it there)."""
    found = [unit for unit in units or () if isinstance(unit, dict) and "gateway" in str(unit.get("unit") or "")]
    if not found:
        return None
    expected, others = expected_gateway_units(home, served=served), _other_profiles(home, served=served)
    preferred = [tuple(item) for item in prefer or () if isinstance(item, (tuple, list)) and len(item) == 2]

    def rank(unit: dict):
        name, scope = str(unit.get("unit")), unit.get("scope")
        failing = bool(unit.get("exec_missing") or unit.get("restart_loop") or unit.get("active_state") == "failed"
                       or unit.get("exec_main_status") == 203 and unit.get("active_state") != "active")
        return ((scope, name) not in preferred, expected.index(name) if name in expected else len(expected),
                name in others, not unit.get("active"), unit.get("enabled") is not True, not failing, scope != "user",
                name)

    return min(found, key=rank)


def _gateway_unit(units: list[dict], home: Path | None = None, *, served: bool = False) -> dict | None:
    """The unit that runs the gateway of the profile at `home` (pick_gateway_unit)."""
    return pick_gateway_unit(units, home=home, served=served)


GATEWAY_UNIT_KEYS = ("unit", "scope", "enabled", "active", "active_state", "sub_state", "n_restarts", "exec_main_status",
                     "exec_path", "exec_exists", "exec_unreferenced", "exec_source", "replacement", "need_reload",
                     "exec_missing", "restart_loop")


def check(home: Path, *, now: float | None = None, history: list[dict] | None = None, quick: bool = False,
          root: Path = Path("/"), host=None) -> dict:
    """Read-only. `history`: earlier records [{"at", "sizes", "free", "rss"}], oldest first. `host`: the machine
    (system_update.Host) whose systemd units are read; by default this one, on Linux with systemd only."""
    from .system_update import Host, _linger, _user_units

    now = time.time() if now is None else now
    home = Path(home)
    report: dict = {"at": now, "gateway": gateway(home, now=now), "database": database(home, quick=quick)}
    units, unit_host = [], None
    if host is not None or (os.name == "posix" and Path("/run/systemd/system").is_dir()):
        unit_host = host or Host(root=root)
        units = _user_units(unit_host)
    # this profile's own unit (or the root's, when its gateway serves this profile), never another profile's
    unit = _gateway_unit(units, home, served=bool(report["gateway"].get("served_by_root")))
    report["gateway_unit"] = {key: unit.get(key) for key in GATEWAY_UNIT_KEYS} if unit else None
    try:
        stat = os.statvfs(home)
        report["free"] = stat.f_bavail * stat.f_frsize
        report["total"] = stat.f_blocks * stat.f_frsize
    except (OSError, AttributeError):
        report["free"] = report["total"] = None
    memory = _meminfo(root)
    report["memory"] = {"total": memory.get("MemTotal"), "available": memory.get("MemAvailable"),
                        "swap_total": memory.get("SwapTotal"),
                        "swap_used": (memory.get("SwapTotal") or 0) - (memory.get("SwapFree") or 0) if memory else None}
    try:
        load = os.getloadavg()
        report["load"] = {"1m": round(load[0], 2), "15m": round(load[2], 2), "cpus": os.cpu_count()}
    except (OSError, AttributeError):
        report["load"] = None
    report["sizes"] = sizes(home)
    store = tool_store(home, in_use=[(item.get("unit"), item.get("exec_path")) for item in units])
    report["unused_tools"] = store["unused"]
    report["in_use_unreferenced"], report["in_use_by"] = store["in_use_unreferenced"], store["in_use_by"]
    report["reclaimable"] = sum(item["bytes"] or 0 for item in report["unused_tools"])
    past = [item for item in history or () if isinstance(item, dict) and _when(item) is not None
            and now - _when(item) <= 8 * 86_400]
    for item in past:
        item["days_ago"] = round((now - _when(item)) / 86_400, 1)
    report["growth"] = _growth(past, report["sizes"])
    if past and report["free"] is not None and isinstance(past[0].get("free"), int):
        days = max(0.5, (now - _when(past[0])) / 86_400)
        per_day = (past[0]["free"] - report["free"]) / days
        report["disk_per_day"] = round(per_day)
        report["days_to_full"] = round(report["free"] / per_day, 1) if per_day > 0 else None
    rss_old = [item.get("rss") for item in past if isinstance(item.get("rss"), int)]
    findings = []
    gate = report["gateway"]
    if gate["stale"]:
        findings.append("gateway_heartbeat_stale")
    service = report["gateway_unit"] or {}
    if not gate.get("running") and (service.get("exec_missing") or service.get("exec_main_status") == 203
                                    and service.get("active_state") != "active"):
        # systemd cannot start it (its program is gone, status=203/EXEC): a restart only waits; the unit needs fixing
        findings.append("gateway_exec_missing")
    elif not gate.get("running") and service.get("restart_loop"):
        findings.append("gateway_restart_loop")
    elif gate.get("crashed"):
        findings.append("gateway_crashed")
    if gate["rss"] and rss_old and gate["rss"] > 2 * sorted(rss_old)[len(rss_old) // 2] and gate["rss"] > 512 * 2**20:
        findings.append("gateway_memory_growth")
    db = report["database"]
    if db.get("error"):
        findings.append("state_db_unreadable")
    if db.get("quick_check") not in (None, "ok"):
        findings.append("state_db_damaged")
    if (db.get("wal") or 0) >= WAL_LARGE:
        findings.append("state_db_wal_large")
    if report["free"] is not None:
        if report["free"] < DISK_CRITICAL:
            findings.append("disk_critical")
        elif report["free"] < DISK_LOW:
            findings.append("disk_low")
    if report.get("days_to_full") is not None and report["days_to_full"] < 14:
        findings.append("disk_filling")
    if report["growth"]:
        findings.append("storage_growth")
    mem = report["memory"]
    if mem.get("total") and mem.get("available") is not None and mem["available"] < 0.10 * mem["total"]:
        findings.append("memory_low")
    if mem.get("swap_total") and (mem.get("swap_used") or 0) > 0.8 * mem["swap_total"]:
        findings.append("swap_full")
    if report["load"] and report["load"]["cpus"] and report["load"]["15m"] > 2 * report["load"]["cpus"]:
        findings.append("cpu_saturated")
    if report["in_use_unreferenced"]:
        # `hermes pm gc` would delete a Python a service still starts: no gc (nor its suggestion) until it moved
        findings.append("tools_in_use_unreferenced")
    elif report["reclaimable"] >= 300 * 2**20:
        findings.append("unused_tools")
    from .system_update import owner_of

    report["owner"] = owner_of(home)
    if unit_host is not None:
        if any(unit["scope"] == "user" and "gateway" in unit["unit"] for unit in units) \
                and _linger(unit_host, report["owner"]) is False:
            findings.append("gateway_not_at_boot")
    report["findings"] = findings
    return report


def cached(conn, home: Path, *, now: float, force: bool = False, host=None) -> dict | None:
    """The latest check, taken at most every CHECK_EVERY_SECONDS by the learning pass, with its history. `host`:
    the machine whose units are read (check)."""
    row = conn.execute("SELECT value FROM cursor WHERE key='care_check'").fetchone()
    try:
        last = json.loads(row[0]) if row else None
    except ValueError:
        last = None
    if not force and isinstance(last, dict) and now - (_when(last) or 0) < CHECK_EVERY_SECONDS:
        return last
    row = conn.execute("SELECT value FROM cursor WHERE key='care_history'").fetchone()
    try:
        history = json.loads(row[0]) if row else []
    except ValueError:
        history = []
    history = [item for item in history if isinstance(item, dict) and _when(item) is not None] \
        if isinstance(history, list) else []
    quick_row = conn.execute("SELECT value FROM cursor WHERE key='care_quick_check'").fetchone()
    try:
        quick = quick_row is None or now - float(quick_row[0] or 0) >= QUICK_CHECK_EVERY_SECONDS   # weekly, even when asked
    except (TypeError, ValueError):
        quick = True
    found = check(home, now=now, history=history, quick=quick,
                  **({} if host is None else {"host": host, "root": Path(host.root)}))
    if quick:
        conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('care_quick_check', ?)", (str(now),))
    record = {"at": now, "sizes": found["sizes"], "free": found["free"], "rss": found["gateway"]["rss"]}
    keep = [item for item in history if now - _when(item) <= HISTORY_DAYS * 86_400]
    if not keep or now - _when(keep[-1]) >= 20 * 3600:   # about one record a day
        keep.append(record)
    conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('care_history', ?)", (json.dumps(keep[-HISTORY_DAYS:]),))
    conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('care_check', ?)", (json.dumps(found),))
    return found


def refresh_after_repair(home: Path, now: float | None = None, *, host=None) -> dict:
    """After the owner's `care fix-gateway` or `care undo-gateway`: a new health check now (not up to 6 hours later)
    and the stored system check's units as systemd says they are now (system_update.refresh_units), so `status`, the
    menu and the panel stop saying the gateway cannot start once it runs, and say it again after an undo. Never
    raises: {"care": the new check or None, "system": the updated system check or None}."""
    from . import system_update
    from .experience import experience

    now = time.time() if now is None else now
    system_update._UNIT_CACHE.clear()   # what the units were a minute ago is what the repair changed
    result: dict = {"care": None, "system": None}
    try:
        with experience(home) as conn:
            result["care"] = cached(conn, home, now=now, force=True, host=host)
    except Exception:   # the repair is done: a check that could not be taken now is taken by the next pass
        pass
    try:
        with experience(home) as conn:
            row = conn.execute("SELECT value FROM cursor WHERE key='system_check'").fetchone()
            stored = json.loads(row[0]) if row else None
            if isinstance(stored, dict) and isinstance(stored.get("units"), list):
                updated = system_update.refresh_units(stored, host=host)
                conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('system_check', ?)",
                             (json.dumps(updated),))
                result["system"] = updated
    except Exception:
        pass
    return result


# What the owner can run from the menu, each with Hermes' own command. (key, arguments, needs a backup first)
ACTIONS = {
    "pm_doctor": (["pm", "doctor"], False),
    "doctor": (["doctor"], False),
    "doctor_fix": (["doctor", "--fix"], True),
    "pm_gc": (["pm", "gc"], False),
    "backup": (["backup", "--quick", "--label", "atlas-evolution"], False),
}


def summarize(output: str) -> dict:
    """The lines of a `hermes doctor` run that matter: problems, warnings, its summary line and the numbered list of
    what to do that follows it ("actions"), and how many problems it counted ("found")."""
    lines = [re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", line).rstrip() for line in output.splitlines()]
    problems = [line.strip() for line in lines if line.strip().startswith("✗")]
    warnings = [line.strip() for line in lines if line.strip().startswith("⚠")]
    tail = [line.strip() for line in lines if re.search(r"(Found \d+ issue|All checks passed|Fixed \d+ issue|"
                                                        r"require manual intervention)", line)]
    actions: list[str] = []
    found = 0
    for index, line in enumerate(lines):
        head = re.search(r"Found (\d+) issue|Fixed \d+ issue\(s\)\.(?: (\d+) issue)?", line)
        if not head:
            continue
        found = int(head.group(1) or head.group(2) or 0)
        actions = []
        for following in lines[index + 1:]:
            item = re.match(r"^\s+\d+\. (.+)$", following)
            if item:
                actions.append(item.group(1).strip()[:600])
            elif following.strip() and actions:
                break
    return {"problems": problems[:20], "warnings": warnings[:20], "summary": tail[-3:], "actions": actions[:20],
            "found": found}


def run_action(home: Path, key: str, *, runner=None, timeout: int = 900) -> dict:
    """Run one of ACTIONS for this profile and keep what it printed (a backup first where it says so). `timeout`
    bounds each Hermes command (a learning pass passes what is left of its own time)."""
    from .admin import hermes_output

    if key not in ACTIONS:
        raise ValueError("Unknown action")
    args, backup_first = ACTIONS[key]
    steps = []
    if backup_first:
        code, out = hermes_output(home, ACTIONS["backup"][0], runner=runner, timeout=timeout)
        steps.append({"command": "hermes " + " ".join(ACTIONS["backup"][0]), "exit": code, "tail": out.strip().splitlines()[-3:]})
        if code != 0:
            return {"ok": False, "steps": steps, "reason": "backup_failed"}
    code, out = hermes_output(home, args, runner=runner, timeout=timeout)
    steps.append({"command": "hermes " + " ".join(args), "exit": code, "tail": out.strip().splitlines()[-6:]})
    result = {"ok": code == 0, "steps": steps}
    if key.startswith("doctor"):
        result["doctor"] = summarize(out)
        # `hermes doctor` ends with 1 when it found problems: that is its answer, not a failure of the run
        if code == 1 and (result["doctor"]["summary"] or result["doctor"]["actions"]):
            result["ok"], result["problems_found"] = True, result["doctor"]["found"] or len(result["doctor"]["actions"])
    return result
