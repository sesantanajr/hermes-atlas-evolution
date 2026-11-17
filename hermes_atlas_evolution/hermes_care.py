"""Care of the Hermes installation itself, without the agent and without tokens.

`check` reads only what Hermes already writes, with no network, no model and no `hermes` command (any `hermes`
command may start Hermes' own maintenance): the gateway's state and heartbeat (alive, its memory, the machine's
memory and swap), the session database (journal mode, a write-ahead log that grew, a quick integrity check once a
week), room on the disk Hermes writes to and how fast each of its folders grows (with the day the disk would be full
at that pace), old versions of Hermes' tools nothing uses anymore, and whether the gateway comes back after a
reboot. The learning pass runs it at most every 6 hours and keeps a short history to see trends.

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


def gateway(home: Path, *, now: float) -> dict:
    state = _read_json(Path(home) / "gateway_state.json") or {}
    beat = _read_json(Path(home) / "state" / "gateway.heartbeat") or {}
    updated = None
    try:
        updated = (Path(home) / "state" / "gateway.heartbeat").stat().st_mtime
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
            gone = _started(state["pid"]) not in (None, state.get("start_time"))
        except ProcessLookupError:
            gone = True
        except OSError:
            gone = False
        if gone:
            running, crashed = False, True
    return {"state": state.get("gateway_state"), "version": state.get("code_version"), "running": running,
            "crashed": crashed,
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


def unused_tools(home: Path, *, budget: float = 4.0) -> list[dict]:
    """Versions of Hermes' managed tools that `tools/facts.json` no longer points at (what `hermes pm gc` removes).
    Only names Hermes manages count; anything else in the folder is left out."""
    from .environment import root_of

    store = root_of(Path(home)) / "tools"   # one store for every profile of a Hermes, at its root
    facts = _read_json(store / "facts.json")
    packages = facts.get("packages") if isinstance(facts, dict) else None
    if not isinstance(packages, dict):
        return []
    used = {entry.get("entry") for entry in packages.values() if isinstance(entry, dict)}
    deadline = time.monotonic() + budget
    found = []
    try:
        children = sorted(store.iterdir())
    except OSError:
        return []
    for child in children:
        if not child.is_dir() or child.is_symlink() or child.name in used:
            continue
        match = _VERSIONED.match(child.name)
        name = match["name"] if match else None
        base = next((key for key in packages if name and (name == key or name.startswith(key + "-"))), None)
        if base is None:
            continue
        found.append({"folder": child.name, "package": base, "bytes": _size(child, deadline)})
    return found


def _growth(history: list[dict], current: dict) -> list[dict]:
    """Folders that grew by at least GROWTH_BYTES and GROWTH_RATIO since about a week ago (or the oldest record)."""
    if not history:
        return []
    old = history[0]["sizes"]
    grown = []
    for name, value in current.items():
        before = old.get(name)
        if isinstance(value, int) and isinstance(before, int) and value - before >= GROWTH_BYTES \
                and value >= before * (1 + GROWTH_RATIO):
            grown.append({"folder": name, "before": before, "now": value, "days": history[0].get("days_ago")})
    return grown


def check(home: Path, *, now: float | None = None, history: list[dict] | None = None, quick: bool = False,
          root: Path = Path("/")) -> dict:
    """Read-only. `history`: earlier records [{"at", "sizes", "free", "rss"}], oldest first."""
    from .system_update import Host, _linger, _user_units

    now = time.time() if now is None else now
    home = Path(home)
    report: dict = {"at": now, "gateway": gateway(home, now=now), "database": database(home, quick=quick)}
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
    report["unused_tools"] = unused_tools(home)
    report["reclaimable"] = sum(item["bytes"] or 0 for item in report["unused_tools"])
    past = [item for item in history or () if isinstance(item, dict) and now - float(item.get("at") or 0) <= 8 * 86_400]
    for item in past:
        item["days_ago"] = round((now - float(item["at"])) / 86_400, 1)
    report["growth"] = _growth(past, report["sizes"])
    if past and report["free"] is not None and isinstance(past[0].get("free"), int):
        days = max(0.5, (now - float(past[0]["at"])) / 86_400)
        per_day = (past[0]["free"] - report["free"]) / days
        report["disk_per_day"] = round(per_day)
        report["days_to_full"] = round(report["free"] / per_day, 1) if per_day > 0 else None
    rss_old = [item.get("rss") for item in past if isinstance(item.get("rss"), int)]
    findings = []
    gate = report["gateway"]
    if gate["stale"]:
        findings.append("gateway_heartbeat_stale")
    if gate.get("crashed"):
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
    if report["reclaimable"] >= 300 * 2**20:
        findings.append("unused_tools")
    from .system_update import owner_of

    report["owner"] = owner_of(home)
    if os.name == "posix" and Path("/run/systemd/system").is_dir():
        host = Host(root=root)
        units = _user_units(host)
        if any(unit["scope"] == "user" and "gateway" in unit["unit"] for unit in units) \
                and _linger(host, report["owner"]) is False:
            findings.append("gateway_not_at_boot")
    report["findings"] = findings
    return report


def cached(conn, home: Path, *, now: float, force: bool = False) -> dict | None:
    """The latest check, taken at most every CHECK_EVERY_SECONDS by the learning pass, with its history."""
    row = conn.execute("SELECT value FROM cursor WHERE key='care_check'").fetchone()
    try:
        last = json.loads(row[0]) if row else None
    except ValueError:
        last = None
    if not force and isinstance(last, dict) and now - float(last.get("at") or 0) < CHECK_EVERY_SECONDS:
        return last
    row = conn.execute("SELECT value FROM cursor WHERE key='care_history'").fetchone()
    try:
        history = json.loads(row[0]) if row else []
    except ValueError:
        history = []
    history = [item for item in history if isinstance(item, dict)]
    quick_row = conn.execute("SELECT value FROM cursor WHERE key='care_quick_check'").fetchone()
    quick = quick_row is None or now - float(quick_row[0] or 0) >= QUICK_CHECK_EVERY_SECONDS   # weekly, even when asked
    found = check(home, now=now, history=history, quick=quick)
    if quick:
        conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('care_quick_check', ?)", (str(now),))
    record = {"at": now, "sizes": found["sizes"], "free": found["free"], "rss": found["gateway"]["rss"]}
    keep = [item for item in history if now - float(item.get("at") or 0) <= HISTORY_DAYS * 86_400]
    if not keep or now - float(keep[-1].get("at") or 0) >= 20 * 3600:   # about one record a day
        keep.append(record)
    conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('care_history', ?)", (json.dumps(keep[-HISTORY_DAYS:]),))
    conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('care_check', ?)", (json.dumps(found),))
    return found


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


def run_action(home: Path, key: str, *, runner=None) -> dict:
    """Run one of ACTIONS for this profile and keep what it printed (a backup first where it says so)."""
    from .admin import hermes_output

    if key not in ACTIONS:
        raise ValueError("Unknown action")
    args, backup_first = ACTIONS[key]
    steps = []
    if backup_first:
        code, out = hermes_output(home, ACTIONS["backup"][0], runner=runner, timeout=900)
        steps.append({"command": "hermes " + " ".join(ACTIONS["backup"][0]), "exit": code, "tail": out.strip().splitlines()[-3:]})
        if code != 0:
            return {"ok": False, "steps": steps, "reason": "backup_failed"}
    code, out = hermes_output(home, args, runner=runner, timeout=900)
    steps.append({"command": "hermes " + " ".join(args), "exit": code, "tail": out.strip().splitlines()[-6:]})
    result = {"ok": code == 0, "steps": steps}
    if key.startswith("doctor"):
        result["doctor"] = summarize(out)
        # `hermes doctor` ends with 1 when it found problems: that is its answer, not a failure of the run
        if code == 1 and (result["doctor"]["summary"] or result["doctor"]["actions"]):
            result["ok"], result["problems_found"] = True, result["doctor"]["found"] or len(result["doctor"]["actions"])
    return result
