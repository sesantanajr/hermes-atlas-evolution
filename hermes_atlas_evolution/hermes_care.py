"""Care of the Hermes installation itself, without the agent and without tokens.

`check` reads only what Hermes already writes, with no network, no model and no `hermes` command (any `hermes`
command may start Hermes' own maintenance): the gateway (alive by its process and start time, heartbeat, pid file,
lock or host record, whatever state it says: degraded and starting gateways run; its memory, the machine's memory
and swap), the session database (journal mode, a write-ahead log that grew, a quick integrity check once a week),
room on the disk Hermes writes to and how fast each of its folders grows (with the day the disk would be full at that
pace), the tool folders `hermes pm gc` would delete (every one facts.json does not name; one a service's command or a
running program uses holds gc back: gc_blocked), and whether the gateway comes back after a reboot. The learning
pass runs it at most every 6 hours and keeps a short history to see trends.

Before anything starts a gateway (the Mode auto's repair, `care fix-gateway`), start_blockers says why it must not:
a gateway of this Hermes runs, a gateway unit of either scope runs, the unit is not the one Hermes names for this
home, or it runs another HERMES_HOME. Every repair goes to a journal of the whole Hermes, which `care undo-gateway`
walks back from any profile.

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


def _proc_stat(pid: int) -> tuple[str, int] | None:
    """(state, start time) of a process as /proc/<pid>/stat says (field 3 and field 22, the start time Hermes
    records), None where unknown (no /proc: macOS, Windows)."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as stream:
            fields = stream.read().rsplit(")", 1)[1].split()
        return fields[0], int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def _started(pid: int) -> int | None:
    """The process' start time as Hermes records it (field 22 of /proc/<pid>/stat), None where unknown."""
    found = _proc_stat(pid)
    return found[1] if found else None


def _cmdline(pid: int) -> str | None:
    """The process' command line, None when it cannot be read (another user's, no /proc)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as stream:
            raw = stream.read(8192)
    except OSError:
        return None
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip() or None


def _hermes_like(command: str) -> bool:
    """A command line that may be a Hermes gateway: `hermes gateway run`, `-m hermes_cli.main gateway run`, a
    launcher of its own (a `...gateway.py`). Broad on purpose: only a program that is plainly something else is
    taken for a pid now used by another program."""
    lowered = command.lower()
    return "gateway" in lowered or "hermes" in lowered


def process_state(pid, start_time=None) -> str:
    """"alive", "gone" (no such process, a zombie, or the pid now another program's) or "unknown" (it cannot be told
    from here: no /proc, or Windows, where os.kill(pid, 0) sends CTRL_C_EVENT instead of probing). The start time
    decides when both are known (Hermes records it with the pid); without it, a command line that is plainly not
    Hermes' says the pid was reused."""
    if type(pid) is not int or pid <= 0 or os.name != "posix":
        return "unknown"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "gone"
    except PermissionError:
        pass   # alive, another user's
    except OSError:
        return "unknown"
    found = _proc_stat(pid)
    if found and found[0] in ("Z", "X"):
        return "gone"   # dead, not reaped yet: it runs nothing
    if type(start_time) is int and found:
        return "alive" if found[1] == start_time else "gone"
    command = _cmdline(pid)
    if command is None:
        return "alive"   # unreadable: never a guess of death
    return "alive" if _hermes_like(command) else "gone"


def _lock_held(path: Path, *, table: str | None = None) -> bool | None:
    """Whether some process holds a lock on the file at `path` now, from /proc/locks (nothing is taken to find out;
    Hermes' gateway.lock is an flock its gateway holds for its whole life). None where it cannot be told."""
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return None
    text = table
    if text is None:   # `table`: what /proc/locks says, when already read
        try:
            with open("/proc/locks", encoding="utf-8", errors="replace") as stream:
                text = stream.read(4_000_000)
        except OSError:
            return None
    wanted = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
    other_device = False
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 6 or parts[1] == "->":
            continue   # a process waiting for the lock does not hold it
        try:
            major, minor, inode = parts[5].split(":")
            found = (int(major, 16), int(minor, 16), int(inode))
        except ValueError:
            continue
        if found == wanted:
            return True
        if found[2] == info.st_ino:
            # the same inode on a device that does not match st_dev: btrfs subvolumes and overlayfs report another
            # device in /proc/locks than stat() does, so this may be our file held: it cannot be told
            other_device = True
    return None if other_device else False


def _stamp_age(value, now: float) -> float | None:
    """Seconds since an ISO time Hermes wrote (`updated_at` of gateway_state.json), None when unreadable."""
    if not isinstance(value, str) or not value.strip():
        return None
    from datetime import datetime, timezone

    raw = value.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return now - moment.timestamp()


LIVE_STATES = ("running", "degraded", "starting", "draining")   # what a gateway that runs writes (degraded: no
                                                                 # platform connected yet; it still runs cron)
ENDED_STATES = ("stopped", "startup_failed")


def _record_alive(path: Path) -> bool | None:
    """Whether the process a Hermes gateway.pid names (a JSON record with its start time, or a bare pid) runs."""
    record = _read_json(path, 65_536)
    if type(record) is int:
        record = {"pid": record}
    if not isinstance(record, dict) or type(record.get("pid")) is not int:
        return None
    found = process_state(record["pid"], record.get("start_time"))
    return None if found == "unknown" else found == "alive"


def own_gateway(where: Path, *, now: float) -> dict:
    """The gateway that runs from the Hermes home `where`, from what Hermes writes there: gateway_state.json (its
    state, pid and start time, `updated_at` re-stamped every minute), state/gateway.heartbeat, gateway.pid and the
    gateway.lock it holds while it runs. {"state", "live", "crashed", "evidence": [...], "process", ...}. Live: its
    process runs (pid and start time), or a fresh heartbeat or stamp says so (a gateway in another PID namespace,
    a container on the same folder), or its pid file or lock does, or its state says it runs and nothing here can
    prove otherwise. Crashed: its state says it runs and its process is gone, with nothing fresh."""
    where = Path(where)
    state = _read_json(where / "gateway_state.json")
    beat = _read_json(where / "state" / "gateway.heartbeat")
    state, beat = (state if isinstance(state, dict) else {}), (beat if isinstance(beat, dict) else {})
    updated = None
    try:
        updated = (where / "state" / "gateway.heartbeat").stat().st_mtime
    except OSError:
        pass
    name = state.get("gateway_state")
    process = process_state(state["pid"], state.get("start_time")) if "pid" in state else None
    evidence = []
    if process == "alive":
        evidence.append("process")
    ended = name in ENDED_STATES and process == "gone"   # what it wrote before it stopped says nothing now
    if not ended:
        if updated is not None and now - updated <= HEARTBEAT_STALE_SECONDS:
            evidence.append("heartbeat")
        age = _stamp_age(state.get("updated_at"), now)
        if age is not None and -300 <= age <= HEARTBEAT_STALE_SECONDS:
            evidence.append("state_updated")
    if _record_alive(where / "gateway.pid"):
        evidence.append("pid_file")
    if _lock_held(where / "gateway.lock"):
        evidence.append("lock")
    claimed = name in LIVE_STATES
    live = bool(evidence) or claimed and process in (None, "unknown")
    mem = beat.get("mem") if isinstance(beat.get("mem"), dict) else {}
    return {"state": name, "live": live, "crashed": bool(claimed and not live), "evidence": evidence,
            "process": process, "pid": state.get("pid") if type(state.get("pid")) is int else None,
            "updated": updated, "version": state.get("code_version"), "active_agents": state.get("active_agents"),
            "rss": mem.get("rss_kib") * 1024 if isinstance(mem.get("rss_kib"), int) else None}


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
    """This profile's gateway (the root's, when its gateway serves this profile), as own_gateway reads it.
    `running`: it is live, whatever state it says (running, degraded, starting, draining)."""
    root = _served_by_root(Path(home))
    found = own_gateway(root or Path(home), now=now)
    updated, running = found["updated"], found["live"]
    return {"state": found["state"], "version": found["version"], "running": running, "live": running,
            "crashed": found["crashed"], "served_by_root": root is not None, "evidence": found["evidence"],
            "active_agents": found["active_agents"],
            "heartbeat_age": None if updated is None else round(now - updated),
            # a gateway still starting has not written its first heartbeat yet
            "stale": bool(running and found["state"] != "starting"
                          and (updated is None or now - updated > HEARTBEAT_STALE_SECONDS)),
            "rss": found["rss"]}


def _lock_dirs(root: Path) -> list[Path]:
    """Where Hermes keeps its host records (gateway.status._get_lock_dir): HERMES_GATEWAY_LOCK_DIR, else
    $XDG_STATE_HOME/hermes/gateway-locks, else ~/.local/state/hermes/gateway-locks (and the one of the account a
    `~/.hermes` root sits in)."""
    found = []
    override = os.environ.get("HERMES_GATEWAY_LOCK_DIR", "").strip()
    if override:
        found.append(Path(override))
    state_home = os.environ.get("XDG_STATE_HOME", "")
    if os.path.isabs(state_home):
        found.append(Path(state_home) / "hermes" / "gateway-locks")
    found.append(Path.home() / ".local" / "state" / "hermes" / "gateway-locks")
    if Path(root).name.startswith(".hermes"):
        found.append(Path(root).parent / ".local" / "state" / "hermes" / "gateway-locks")
    unique = []
    for folder in found:
        if folder not in unique:
            unique.append(folder)
    return unique


def _boot_time() -> float | None:
    try:
        with open("/proc/stat", encoding="ascii", errors="replace") as stream:
            for line in stream:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _host_owner_alive(record: dict) -> bool:
    """Whether the process a host record names runs: its start ticks (`startTime`) within Hermes' tolerance of 200
    (2 s), else its creation time (`createTime`, seconds) within 2 s, else a command line that may be Hermes'."""
    pid, start, created = record.get("pid"), record.get("startTime"), record.get("createTime")
    state = process_state(pid, None)
    if state == "unknown" or type(pid) is not int:
        return state == "unknown"   # nothing here can prove it gone (no /proc, Windows)
    if type(start) is not int and not isinstance(created, (int, float)):
        return state == "alive"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        pass
    found = _proc_stat(pid)
    if found is None:
        return True   # alive, and its start cannot be read here
    if found[0] in ("Z", "X"):
        return False
    if type(start) is int:
        return abs(found[1] - start) <= 200
    boot = _boot_time()
    try:
        ticks = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError, AttributeError):
        ticks = None
    if boot is None or not ticks:
        return True
    return abs(boot + found[1] / ticks - float(created)) <= 2.0


def _host_gateway(root: Path, *, now: float) -> list[dict]:
    """The host gateway Hermes publishes (host-gateway.json: one gateway for every profile of the account) when it
    was launched from this Hermes and its process runs: its pid and start time agree, and its host lock is not free."""
    from .system_update import same_home

    found = []
    for folder in _lock_dirs(root):
        record = _read_json(folder / "host-gateway.json", 262_144)
        if not isinstance(record, dict) or record.get("role") != "gateway" or type(record.get("pid")) is not int:
            continue
        launched = str(record.get("home") or "").strip()
        if launched:
            path = Path(launched)
            mine = same_home(path, root) or path.parent.name == "profiles" and same_home(path.parent.parent, root)
        else:   # a record without its home names the account's default root
            mine = any(same_home(root, owner) for owner in _bare_owners())
        if not mine:
            continue
        # its process decides; a host lock seen held adds to it, and one that reads free proves nothing here (a
        # device /proc/locks names differently, a lock taken by another mount namespace)
        lock = _lock_held(folder / "host-gateway.lock")
        if not _host_owner_alive(record):
            continue
        found.append({"home": launched or str(root), "evidence": ["host_record"] + (["host_lock"] if lock else []),
                      "pid": record["pid"]})
    return found


def live_gateways(home: Path, *, now: float) -> list[dict]:
    """Every gateway that runs for this Hermes now (own_gateway of the root and of each profile, a legacy
    per-profile one included, and the host gateway Hermes published from it): [{"home", "evidence"}]. Nothing may
    start a gateway while this is not empty: two gateways would answer the same bots."""
    from .environment import root_of

    root = root_of(Path(home))
    homes = [root]
    try:
        homes += sorted(entry for entry in (root / "profiles").iterdir() if entry.is_dir())[:200]
    except OSError:
        pass
    found = []
    for where in homes:
        gate = own_gateway(where, now=now)
        if gate["live"]:
            found.append({"home": str(where), "evidence": gate["evidence"] or ["state"], "state": gate["state"]})
    found += _host_gateway(root, now=now)
    return found


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
# pm.defaults.RETIRED_DEFAULTS: `hermes pm gc` forgets these packages' facts first, so their folders go too
RETIRED_DEFAULTS = ("agent-browser",)
GC_MODEL = 2   # how `hermes pm gc` is read (a check stored by an older version proves nothing: gc_blocked)
PROCESS_SCAN = 6000


def _in_use_by(store: Path, sources, *, exact=()) -> dict[str, list[str]]:
    """{folder: [who]}: the folders of the tool store that a service's commands (ExecStart, ExecStop...) or a
    running program point into (`sources`, `exact`: paths, or (who, path) pairs). Never unused, whatever facts.json
    says (removing one would delete the interpreter a service starts). A source also counts by any `.../tools/
    <folder>/...` in its path (a unit names the root's store by another spelling); an `exact` one (a running
    process' own program, already resolved) only inside this store."""
    found: dict[str, list[str]] = {}
    try:
        base = os.path.normcase(os.path.realpath(store)).replace("\\", "/").rstrip("/") + "/"
    except (OSError, ValueError):
        return found
    for strict, group in ((False, sources), (True, exact)):
        for source in group:
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
                if strict:
                    continue
                parts = str(candidate).replace("\\", "/").split("/")
                for index in range(1, len(parts)):   # any .../tools/<folder>/... (a unit names the root's own store)
                    if parts[index - 1] == "tools" and parts[index]:
                        names.add(parts[index])
            for name in names:
                users = found.setdefault(name, [])
                if who and who not in users:
                    users.append(str(who)[:120])
    return found


def running_programs(root: Path = Path("/"), *, budget: float = 2.0) -> list[tuple[str, str]]:
    """("pid <n>", program) of the processes running now, from <root>/proc/<pid>/exe (Linux; nothing elsewhere,
    and nothing for a process this user may not look at): a program running from a tools folder keeps it in use."""
    proc = Path(root) / "proc"
    found = []
    deadline = time.monotonic() + budget
    try:
        with os.scandir(proc) as entries:
            pids = [entry.name for entry in entries if entry.name.isdigit()][:PROCESS_SCAN]
    except OSError:
        return found
    for pid in pids:
        if time.monotonic() > deadline:
            break
        try:
            target = os.readlink(proc / pid / "exe")
        except OSError:
            continue
        if target.endswith(" (deleted)"):
            target = target[:-len(" (deleted)")]
        found.append((f"pid {pid}", target))
    return found


def _launcher_target(path: Path) -> tuple[Path | None, Path | None]:
    """(the checkout, the interpreter) a Hermes launcher names: `<checkout>/.hermes/bin/hermes` (or venv/bin), a
    `sys.path.insert(0, '<checkout>')` and `exec <python> -I` in its text, following a small `exec <launcher>`
    wrapper (/usr/local/bin/hermes) at most twice."""
    current: Path | None = Path(path)
    for _ in range(3):
        if current is None:
            break
        try:
            resolved = current.resolve()
            with open(resolved, "rb") as stream:
                text = stream.read(65_536).decode("utf-8", "replace")
        except OSError:
            return None, None
        checkout = None
        if resolved.parent.name in ("bin", "Scripts") and resolved.parent.parent.name in (".hermes", "venv"):
            checkout = resolved.parents[2]
        inserted = re.search(r"sys\.path\.insert\(0,\s*(?:'\"'\"')?['\"]?([^'\"\)]+?)['\"]?(?:'\"'\"')?\)", text)
        if inserted:
            checkout = Path(inserted.group(1))
        python = re.search(r"^exec\s+(\S*python[\w.]*)\s+-I\b", text, re.M)
        if checkout is not None or python:
            return checkout, Path(python.group(1)) if python else None
        wrapper = re.search(r"^exec\s+(\S+)", text, re.M)
        current = Path(wrapper.group(1)) if wrapper and len(text) < 4096 else None
    return None, None


def hermes_checkout() -> tuple[Path | None, Path | None]:
    """(the Hermes checkout `hermes pm gc` runs from, its launcher's interpreter): the one the `hermes` command this
    plugin runs (admin.hermes_executable) launches, else the one this process runs inside. (None, None) unknown."""
    import sys

    from .admin import hermes_executable

    try:
        executable = hermes_executable()
    except Exception:
        executable = None
    if executable:
        checkout, python = _launcher_target(Path(executable))
        if checkout is not None:
            return checkout, python
    for name in ("pm", "hermes_cli"):
        origin = getattr(sys.modules.get(name), "__file__", None)
        if origin:
            return Path(origin).resolve().parent.parent, None
    return None, None


def _install_key(checkout: Path) -> str:
    import hashlib

    return hashlib.sha256(str(Path(checkout).resolve()).encode("utf-8")).hexdigest()[:16]


def gc_store(home: Path) -> tuple[Path | None, str]:
    """The tool store `hermes pm gc`, run for this profile, sweeps (pm.paths.writable_store_root): HERMES_RUNTIME_DIR;
    else the `runtimeDir` of the checkout's install stamp; else the owner's store when this Hermes only borrows the
    checkout (pm.environments._unstamped_store: a staging or temporary home sweeps the store of the root that
    installed it, often the production one); else this root's tools/. A store beside a sealed payload's
    manifest.json is never swept: this root's is. (store, "ok") or (None, why it cannot be told)."""
    from .environment import root_of

    root = root_of(Path(home))

    def writable(store: Path) -> Path:
        return root / "tools" if (store.parent / "manifest.json").is_file() else store

    override = os.environ.get("HERMES_RUNTIME_DIR", "").strip()
    if override:
        return writable(Path(override).resolve()), "ok"
    checkout, python = hermes_checkout()
    if checkout is None:
        return None, "checkout_unknown"
    checkout = checkout.resolve()
    for manifest in (checkout.parent / "manifest.json", checkout / "manifest.json"):
        data = _read_json(manifest, 1_000_000)
        if isinstance(data, dict) and data.get("store"):
            return None, "payload"   # a sealed payload's own store: never guessed
    for folder in (checkout, *checkout.parents):
        stamp = folder / "install-stamp.json"
        if stamp.is_file():
            data = _read_json(stamp, 262_144)
            if not isinstance(data, dict):
                break   # an unreadable stamp: Hermes falls back to the unstamped store
            if isinstance(data.get("runtimeDir"), str) and data["runtimeDir"].strip():
                return writable(Path(data["runtimeDir"]).resolve()), "ok"
            break
    key = _install_key(checkout)
    native = Path.home() / (".hermes" + os.environ.get("HERMES_DATA_DIR_SUFFIX", ""))
    candidates = []
    for candidate in (checkout.parent, native):
        if candidate not in candidates and (candidate / "installs" / key / "facts.json").is_file():
            candidates.append(candidate)
    owner = None
    if candidates:   # pm.environments.owning_home_root
        owner = candidates[0]
        if python is not None:
            for candidate in candidates:
                try:
                    if (candidate / "tools").resolve() in python.resolve().parents:
                        owner = candidate
                        break
                except (OSError, RuntimeError):
                    continue
        try:
            if (owner / "installs" / key).resolve() == (root / "installs" / key).resolve():
                owner = None
        except OSError:
            pass
    return writable((owner or root) / "tools"), "ok"


def _installs_plan(root: Path) -> list[Path]:
    """What the rest of `hermes pm gc` removes under <root>/installs (hermes_cli.runtime_state.collect_generations,
    pm.runtime.collect_runtime_generations, pm.install_states.collect_orphan_install_states): the install state of a
    checkout that is gone, every dependency environment that is not the selected one (lease-managed), every PM
    runtime generation that is not the selected one (unpublished, or lease-managed). Counted wide (leases and ages
    are Hermes' to weigh): what a unit names inside one of these must hold gc back."""
    removable: list[Path] = []
    try:
        states = [entry for entry in (Path(root) / "installs").iterdir() if entry.is_dir() and not entry.is_symlink()]
    except OSError:
        return removable
    for state in states[:50]:
        try:
            project = (state / "inputs" / ".project-root").read_text(encoding="utf-8-sig").strip()
        except OSError:
            project = ""
        if project and not Path(project).exists():
            removable.append(state)
            continue
        facts = _read_json(state / "facts.json")
        venv = ((facts.get("packages") or {}).get("venv") or {}).get("environment") if isinstance(facts, dict) else None
        selected = Path(venv).resolve().parent if isinstance(venv, str) and venv else None
        try:
            generations = list((state / "environments").iterdir())
        except OSError:
            generations = []
        for generation in generations:
            if generation.is_dir() and not generation.is_symlink() and (generation / ".lease-managed").is_file() \
                    and generation.resolve() != selected:
                removable.append(generation)
        runtime = state / "pm-runtime"
        chosen = _read_json(runtime / "selected.json", 262_144)
        chosen = runtime / chosen["generation"] if isinstance(chosen, dict) and isinstance(chosen.get("generation"), str) \
            else None
        try:
            generations = list((runtime / "generations").iterdir())
        except OSError:
            generations = []
        for generation in generations:
            if not generation.is_dir() or generation.is_symlink() or generation == chosen:
                continue
            published = (generation / "pm-runtime.json").is_file()
            if not published or (generation / ".lease-managed").is_file():
                removable.append(generation)
    return removable


def _inside(path: str, folders: list[Path]) -> Path | None:
    """The folder of `folders` that holds `path` (links followed), None when none does."""
    try:
        target = os.path.normcase(os.path.realpath(str(path)))
    except (OSError, ValueError):
        return None
    for folder in folders:
        try:
            base = os.path.normcase(os.path.realpath(folder)).rstrip(os.sep) + os.sep
        except (OSError, ValueError):
            continue
        if target.startswith(base) or target == base.rstrip(os.sep):
            return folder
    return None


def _gc_plan(store: Path) -> tuple[str, list[Path], dict]:
    """What `hermes pm gc` (pm.cli._gc_store) removes from `store`: every folder whose name is not the entry of a
    package facts.json records (after forgetting RETIRED_DEFAULTS), whatever its name, except dot folders (a
    `.staging-*` one goes too); a link is left (rmtree refuses it). ("ok" | "missing" (no facts.json: gc removes
    every folder) | "unreadable" (gc refuses to run) | "absent" (no store), [folders], packages)."""
    if not store.is_dir():
        return "absent", [], {}
    facts_path = store / "facts.json"
    if not facts_path.exists():
        packages, status = {}, "missing"
    else:
        facts = _read_json(facts_path)
        packages = facts.get("packages") if isinstance(facts, dict) else None
        if not isinstance(packages, dict):
            return "unreadable", [], {}
        status = "ok"
    keep = {fact.get("entry") for name, fact in packages.items()
            if name not in RETIRED_DEFAULTS and isinstance(fact, dict) and isinstance(fact.get("entry"), str)}
    try:
        children = sorted(store.iterdir())
    except OSError:
        return "unreadable", [], packages
    removed = []
    for child in children:
        if child.is_symlink() or not child.is_dir():
            continue
        if child.name.startswith(".") and not child.name.startswith(".staging-"):
            continue
        if child.name not in keep:
            removed.append(child)
    return status, removed, packages


def tool_store(home: Path, *, budget: float = 4.0, in_use=(), processes=None, root: Path = Path("/"),
               unverified=()) -> dict:
    """What `hermes pm gc` would do (gc_store, _gc_plan, _installs_plan): {"unused": [...] (unused_tools),
    "in_use_unreferenced": [folder, ...], "in_use_by": {folder: [who]}, "gc_unsafe": [why], "gc_model", "store"}.
    `in_use_unreferenced`: what gc deletes (a tools folder facts.json no longer names, whatever its name; a
    dependency environment or runtime generation that is not the selected one; a gone checkout's install state)
    that a service uses (`in_use`: (unit, path) of every Exec* command and PATH folder of Hermes' units), this process
    runs from, or a running process does (`processes`: (who, program); by default <root>/proc's): a gc now would
    leave that service failing at its next start (status=203/EXEC), or delete a running program's files.
    `gc_unsafe`: what makes the answer unknown, so gc must wait: a systemd scope whose units could not be read
    (`unverified`: a stopped gateway unit there may start a Python gc deletes), the store gc sweeps cannot be told,
    it has no facts.json (gc would delete every tool) or one gc cannot read."""
    import sys

    report: dict = {"unused": [], "in_use_unreferenced": [], "in_use_by": {}, "in_use_kind": {}, "gc_model": GC_MODEL,
                    "stores": [],
                    "gc_unsafe": [f"unverified:{scope}" for scope in sorted(set(unverified or ()))], "store": None}
    deadline = time.monotonic() + budget
    running = running_programs(root) if processes is None else list(processes)
    sources = list(in_use)
    # this process' own program is a real path, taken as it is: only that folder of this store is in use (a launcher
    # Python of another Hermes in a same-named tools folder elsewhere says nothing of this store)
    running = [*running, ("process", os.path.realpath(sys.executable))] if sys.executable else running
    store, why = gc_store(home)
    if store is None:
        report["gc_unsafe"].append(f"store_unknown:{why}")
        return report
    report["store"] = str(store)
    status, removed, packages = _gc_plan(store)
    if status != "absent":
        report["stores"].append(str(store))
    if status == "unreadable":
        report["gc_unsafe"].append(f"unreadable:{store}")
        removed = []
    if status == "missing" and removed:
        report["gc_unsafe"].append(f"no_facts:{store}")
    protected = _in_use_by(store, sources, exact=running)
    for child in removed:
        if child.name in protected:   # `pm gc` would remove it all the same: what starts from it must move first
            report["in_use_unreferenced"].append(child.name)
            report["in_use_by"][child.name] = protected[child.name][:5]
            report["in_use_kind"][child.name] = build_kind(
                child.name, [fact.get("entry") for fact in packages.values() if isinstance(fact, dict)])
            continue
        match = _VERSIONED.match(child.name)
        name = match["name"] if match else None
        package = next((key for key in packages if name and (name == key or name.startswith(key + "-"))), None)
        report["unused"].append({"folder": child.name, "package": package, "bytes": _size(child, deadline)})
    from .environment import root_of

    base = root_of(Path(home))   # gc's dependency state is this root's (pm.environments.dependency_home_root)
    collected = _installs_plan(base)
    if collected:
        for source in [*sources, *running]:
            who, raw = source if isinstance(source, tuple) and len(source) == 2 else (None, source)
            folder = _inside(raw, collected) if raw else None
            if folder is None:
                continue
            label = "installs/" + os.path.relpath(folder, Path(base) / "installs").replace(os.sep, "/")
            if label not in report["in_use_unreferenced"]:
                report["in_use_unreferenced"].append(label)
                report["in_use_kind"][label] = "environment"
            users = report["in_use_by"].setdefault(label, [])
            if who and str(who)[:120] not in users and len(users) < 5:
                users.append(str(who)[:120])
    return report


# Hermes' own naming of a tool version: `python-3.14.9`, `python-3.14.9+20261021-linux-x64`, `node-26.12.1-linux-x64`
_HERMES_BUILD = re.compile(r"^(?P<name>[a-z][a-z0-9-]*?)-(?P<version>\d+(?:\.\d+)*)(?:\+\d{8})?(?:-[a-z0-9_]+-[a-z0-9_]+)?$")


def _numbers(text: str) -> tuple:
    found = re.match(r"\d+(?:\.\d+)*", text or "")
    return tuple(int(part) for part in found.group(0).split(".")) if found else ()


def build_kind(folder: str, entries) -> str:
    """What a tools folder in use that facts.json no longer names is: "older", an older version of a tool Hermes
    manages, in Hermes' own naming (Hermes moved on and the service was left behind: point it at the current one);
    "custom" otherwise (a build Hermes does not manage, a newer one, another naming: the service may run it on
    purpose, and pointing it at Hermes' Python would drop that build); "environment" for a dependency environment
    or runtime generation under installs/."""
    folder = str(folder)
    if folder.startswith("installs/"):
        return "environment"
    match = _HERMES_BUILD.match(folder)
    if not match:
        return "custom"
    for entry in entries or ():
        entry = str(entry)
        if entry == folder or not entry.startswith(match["name"] + "-"):
            continue
        current = _numbers(entry[len(match["name"]) + 1:])
        if current and _numbers(match["version"]) < current:
            return "older"
    return "custom"


def gc_block_advice(care, lang: str = "en", *, fix: str = "hermes evolution care fix-gateway") -> list[str]:
    """What to tell the owner when `hermes pm gc` is held back because a folder it would delete is in use, one
    sentence per folder (en, pt), honest about the remedy: for an older Python Hermes left behind, point the service
    at the current one (`fix`); for a build Hermes does not manage, which the service may run on purpose, gc stays
    off while it is in use, and `fix` would drop that build; for a running program, restart it once it should use
    Hermes' current Python."""
    care = care if isinstance(care, dict) else {}
    users = care.get("in_use_by") if isinstance(care.get("in_use_by"), dict) else {}
    kinds = care.get("in_use_kind") if isinstance(care.get("in_use_kind"), dict) else {}
    pt = lang == "pt"
    lines = []
    for folder in [str(item) for item in care.get("in_use_unreferenced") or () if item][:8]:
        who = [str(item) for item in users.get(folder) or () if isinstance(item, str)]
        services = [item for item in who if item != "process" and not item.startswith("pid ")][:3]
        named = ", ".join(f"`{item}`" for item in services)
        kind = kinds.get(folder) or "custom"
        if not services:   # only a running program (this one, or another process) uses it
            lines.append(f"o `hermes pm gc` apagaria `{folder}`, que um programa em execução usa: o gc fica desligado "
                         "enquanto ele roda; reinicie-o (`hermes gateway restart`) quando ele dever usar o Python atual "
                         "do Hermes." if pt else
                         f"`hermes pm gc` would delete `{folder}`, which a running program uses: gc stays off while it "
                         "runs; restart it (`hermes gateway restart`) once it should use Hermes' current Python.")
        elif kind == "older":
            lines.append(f"o `hermes pm gc` apagaria `{folder}`, um Python antigo do Hermes de onde o serviço {named} "
                         f"ainda inicia. Aponte o serviço para o Python atual antes: `{fix}`." if pt else
                         f"`hermes pm gc` would delete `{folder}`, an older Python of Hermes the service {named} still "
                         f"starts from. Point the service at the current Python first: `{fix}`.")
        elif kind == "environment":
            lines.append(f"o `hermes pm gc` apagaria `{folder}`, que o serviço {named} usa: o gc fica desligado "
                         "enquanto ele estiver em uso." if pt else
                         f"`hermes pm gc` would delete `{folder}`, which the service {named} uses: gc stays off while "
                         "it is in use.")
        else:
            lines.append(f"o `hermes pm gc` apagaria `{folder}`, que o serviço {named} roda de propósito (uma versão "
                         "que o Hermes não gerencia). O gc fica desligado enquanto ela estiver em uso; trocar o serviço "
                         f"para o Python do próprio Hermes (`{fix}`) descartaria essa versão — só faça isso se quiser."
                         if pt else
                         f"`hermes pm gc` would delete `{folder}`, which the service {named} runs on purpose (a build "
                         "Hermes does not manage). gc stays off while it is in use; switching the service to Hermes' "
                         f"own Python (`{fix}`) would drop that build — only do it if you want that.")
    return lines


def gc_unsafe_words(items, lang: str = "en") -> list[str]:
    """Each reason of tool_store's `gc_unsafe` in plain words (en, pt)."""
    pt = lang == "pt"
    words = []
    for item in items or ():
        kind, _, rest = str(item).partition(":")
        if kind == "unverified":
            scope = {"user": ("user", "do usuário"), "system": ("system", "do sistema")}.get(rest, (rest, rest))
            words.append(f"os serviços {scope[1]} do systemd não puderam ser lidos aqui (um serviço parado pode iniciar "
                         "um Python que o gc apagaria)" if pt else
                         f"systemd's {scope[0]} services could not be read here (a stopped service may start a Python "
                         "gc would delete)")
        elif kind == "store_unknown":
            words.append("não dá para saber qual depósito de ferramentas o `hermes pm gc` limparia" if pt else
                         "which tool store `hermes pm gc` would sweep cannot be told")
        elif kind == "no_facts":
            words.append(f"o depósito {rest} não tem facts.json: o `hermes pm gc` apagaria todas as ferramentas dele"
                         if pt else f"the store {rest} has no facts.json: `hermes pm gc` would delete every tool in it")
        elif kind == "unreadable":
            words.append(f"o facts.json do depósito {rest} não pôde ser lido" if pt else
                         f"the facts.json of the store {rest} cannot be read")
    return words


def unused_tools(home: Path, *, budget: float = 4.0, in_use=()) -> list[dict]:
    """The folders of Hermes' tool store that `hermes pm gc` removes (tool_store): every one `tools/facts.json` no
    longer points at, whatever its name (an old version, a download cache, a folder put there by hand), except the
    ones a Hermes service starts a program from (`in_use`: the paths of the hermes* units' commands) or a running
    program runs from: those are tool_store's `in_use_unreferenced`, which stop `pm gc` altogether (gc_blocked)."""
    return tool_store(home, budget=budget, in_use=in_use)["unused"]


def gc_blocked(care) -> bool:
    """Whether `hermes pm gc` must not run now (the Auto mode's reclaim, `care gc`, the menu): by the health check
    `care` (check or cached), a folder gc would delete is one a Hermes service or a running process still starts
    from, or a store has no facts.json (gc would delete every tool) or cannot be read. A check without that answer
    (none yet, or stored by an older version) proves nothing: blocked until a new one."""
    if not isinstance(care, dict) or not isinstance(care.get("in_use_unreferenced"), list) \
            or care.get("gc_model") != GC_MODEL:
        return True
    return bool(care["in_use_unreferenced"] or care.get("gc_unsafe"))


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


_PROFILE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")   # Hermes' rule for a name a service may carry
BARE_UNIT = "hermes-gateway.service"


def _pinned_home(host=None) -> str | None:
    """The HERMES_HOME the installed bare `hermes-gateway.service` pins (Hermes' _bare_unit_pinned_home: root reads
    the system unit, any other user its own user unit), Linux only. `host`: the machine whose files are read."""
    import sys

    if not sys.platform.startswith("linux") or not hasattr(os, "geteuid"):
        return None
    if os.geteuid() == 0:
        name = f"/etc/systemd/system/{BARE_UNIT}"
    else:
        from .system_update import _account_home

        config = os.environ.get("XDG_CONFIG_HOME", "").strip()
        account = _account_home()
        if not config and account is None:
            return None
        name = str(Path(config) if config else account / ".config") + f"/systemd/user/{BARE_UNIT}"
    if host is not None:
        text = host.read(name, 262_144)
    else:
        try:
            with open(name, encoding="utf-8-sig") as stream:
                text = stream.read(262_144)
        except OSError:
            text = None
    for line in (text or "").splitlines():   # hermes_cli.gateway._unit_environment_value
        body = line.strip()
        if not body.startswith("Environment="):
            continue
        body = body[len("Environment="):].strip()
        if body.startswith('"') and body.endswith('"'):
            body = body[1:-1].replace('\\"', '"').replace("\\\\", "\\").replace("%%", "%")
        if body.startswith("HERMES_HOME="):
            return body.split("=", 1)[1].strip() or None
    return None


def _bare_owners(host=None) -> list[str]:
    """The homes Hermes gives the bare unit name (hermes_cli.gateway._home_owns_bare_service_name): the account's
    own `~/.hermes` (the account's, never a process HOME a harness moved), under sudo the invoking user's too, and
    the home the installed bare unit pins."""
    from .system_update import _account_home

    owners = []
    account = _account_home()
    if account is not None:
        owners.append(str(account / (".hermes" + os.environ.get("HERMES_DATA_DIR_SUFFIX", ""))))
    invoking = os.environ.get("SUDO_USER", "").strip()   # the account that raised this process to root
    if hasattr(os, "geteuid") and os.geteuid() == 0 and invoking and invoking != "root":
        invoker = _account_home(invoking)
        if invoker is not None:
            owners.append(str(invoker / ".hermes"))
    pinned = _pinned_home(host)
    if pinned:
        owners.append(os.path.expanduser(pinned))
    return owners


def gateway_unit_name(home: Path, *, owners=None, host=None) -> str:
    """The name Hermes gives the gateway's service of the Hermes home `home` (hermes_cli.gateway._profile_suffix):
    `hermes-gateway.service` only for a home that owns the bare name (_bare_owners), `hermes-gateway-<profile>`
    for `<root>/profiles/<profile>`, else `hermes-gateway-<first 8 of the sha256 of its path>`: a staging or test
    home never takes the production gateway's name."""
    import hashlib

    from .system_update import same_home

    owners = _bare_owners(host) if owners is None else owners
    try:
        resolved = Path(os.path.realpath(home))
    except (OSError, ValueError):
        resolved = Path(home)
    if any(same_home(resolved, owner) for owner in owners):
        return BARE_UNIT
    if resolved.parent.name == "profiles" and _PROFILE_NAME.fullmatch(resolved.name):
        return f"hermes-gateway-{resolved.name}.service"
    return f"hermes-gateway-{hashlib.sha256(str(resolved).encode()).hexdigest()[:8]}.service"


def expected_gateway_units(home: Path | None, *, served: bool = False, host=None, owners=None) -> list[str]:
    """The names Hermes gives the gateway's unit of this profile (gateway_unit_name), best first: for a profile the
    root's gateway serves, the root's unit (that gateway is this profile's), then the profile's own legacy one."""
    if home is None:
        return [BARE_UNIT]
    from .environment import root_of

    owners = _bare_owners(host) if owners is None else owners
    own = gateway_unit_name(home, owners=owners)
    if Path(os.path.abspath(home)).parent.name != "profiles":
        return [own]
    if not served:
        return [own]
    root = gateway_unit_name(root_of(Path(home)), owners=owners)
    return [root, *([own] if own != root else [])]


def _other_profiles(home: Path | None, *, served: bool = False, owners=None) -> set[str]:
    """The gateway unit names of the other profiles of this Hermes: never this profile's gateway."""
    if home is None:
        return set()
    from .environment import root_of

    home = Path(home)
    named = Path(os.path.abspath(home)).parent.name == "profiles"
    root = root_of(home)
    owners = _bare_owners() if owners is None else owners
    try:
        entries = [entry for entry in (root / "profiles").iterdir() if entry.is_dir()][:200]
    except OSError:
        entries = []
    found = {gateway_unit_name(entry, owners=owners) for entry in entries if not named or entry.name != home.name}
    if named and not served:
        found.add(gateway_unit_name(root, owners=owners))   # the root profile's own
    return found - set(expected_gateway_units(home, served=served, owners=owners))


def pick_gateway_unit(units, *, home: Path | None = None, served: bool = False, prefer=(), host=None) -> dict | None:
    """The unit that runs this profile's gateway, among Hermes' units: one of `prefer` ((scope, unit) pairs, say the
    one the last check picked) first, then the name Hermes gives this profile's gateway (for a profile the root's
    gateway serves, the root's unit before its own legacy one), never another profile's while there is a better
    one, then one that runs, comes back after a reboot or fails (a leftover that is off says nothing), a user one
    before a system one (Hermes installs it there)."""
    found = [unit for unit in units or () if isinstance(unit, dict) and "gateway" in str(unit.get("unit") or "")]
    if not found:
        return None
    owners = _bare_owners(host) if home is not None else []
    expected = expected_gateway_units(home, served=served, owners=owners)
    others = _other_profiles(home, served=served, owners=owners)
    preferred = [tuple(item) for item in prefer or () if isinstance(item, (tuple, list)) and len(item) == 2]

    def rank(unit: dict):
        name, scope = str(unit.get("unit")), unit.get("scope")
        failing = bool(unit.get("exec_missing") or unit.get("restart_loop") or unit.get("active_state") == "failed"
                       or unit.get("exec_main_status") == 203 and unit.get("active_state") != "active")
        return ((scope, name) not in preferred, expected.index(name) if name in expected else len(expected),
                name in others, not unit.get("active"), unit.get("enabled") is not True, not failing, scope != "user",
                name)

    return min(found, key=rank)


def _gateway_unit(units: list[dict], home: Path | None = None, *, served: bool = False, host=None) -> dict | None:
    """The unit that runs the gateway of the profile at `home` (pick_gateway_unit)."""
    return pick_gateway_unit(units, home=home, served=served, host=host)


GATEWAY_UNIT_KEYS = ("unit", "scope", "enabled", "active", "active_state", "sub_state", "n_restarts", "exec_main_status",
                     "exec_path", "exec_exists", "exec_unreferenced", "exec_source", "replacement", "need_reload",
                     "exec_missing", "restart_loop", "main_pid")


def check(home: Path, *, now: float | None = None, history: list[dict] | None = None, quick: bool = False,
          root: Path = Path("/"), host=None, fresh: bool = False) -> dict:
    """Read-only. `history`: earlier records [{"at", "sizes", "free", "rss"}], oldest first. `host`: the machine
    (system_update.Host) whose systemd units are read; by default this one, on Linux with systemd only. `fresh`:
    the units as systemd says now, never the minute-old list another check of this pass kept."""
    from .system_update import Host, _linger, _user_units

    now = time.time() if now is None else now
    home = Path(home)
    report: dict = {"at": now, "gateway": gateway(home, now=now), "database": database(home, quick=quick)}
    units, unit_host = [], None
    if host is not None or (os.name == "posix" and Path("/run/systemd/system").is_dir()):
        unit_host = host or Host(root=root)
        units = _user_units(unit_host, fresh=fresh)
    # this profile's own unit (or the root's, when its gateway serves this profile), never another profile's
    unit = _gateway_unit(units, home, served=bool(report["gateway"].get("served_by_root")), host=unit_host)
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
    # every command of every Hermes unit (ExecStart, ExecStop...) and every program running now keeps its folder
    from .system_update import unit_paths

    store = tool_store(home, in_use=[(item.get("unit"), path) for item in units
                                     for path in unit_paths(unit_host, item)], root=root,
                       unverified=sorted(unit_host.unverified) if unit_host is not None else ())
    report["unused_tools"] = store["unused"]
    report["in_use_unreferenced"], report["in_use_by"] = store["in_use_unreferenced"], store["in_use_by"]
    report["gc_unsafe"], report["gc_model"] = store["gc_unsafe"], store["gc_model"]
    report["in_use_kind"] = store["in_use_kind"]
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
    elif gate.get("running") and service.get("exec_missing") and service.get("active_state") == "active":
        # it runs, but its program was deleted under it (Hermes replaced its Python): the next restart fails
        findings.append("gateway_program_deleted")
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
    elif report["reclaimable"] >= 300 * 2**20 and not report["gc_unsafe"]:
        findings.append("unused_tools")   # never suggested while gc could not be read (or would delete every tool)
    from .system_update import owner_of

    report["owner"] = owner_of(home)
    if unit_host is not None:
        if any(unit["scope"] == "user" and "gateway" in unit["unit"] for unit in units) \
                and _linger(unit_host, report["owner"]) is False:
            findings.append("gateway_not_at_boot")
    report["findings"] = findings
    return report


def cached(conn, home: Path, *, now: float, force: bool = False, host=None, fresh: bool = False) -> dict | None:
    """The latest check, taken at most every CHECK_EVERY_SECONDS by the learning pass, with its history. `host`:
    the machine whose units are read (check); `fresh`: never a cached list of units (what gc relies on)."""
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
    found = check(home, now=now, history=history, quick=quick, fresh=fresh,
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


def start_blockers(home: Path, target: dict, units, *, now: float, unverified=(), automatic: bool = False,
                   served: bool | None = None, host=None) -> list[dict]:
    """Why starting the gateway unit `target` now could run a second gateway, or touch a unit that is not this
    Hermes' gateway: [] when it is safe. The one rule for the Mode auto (`automatic`) and `care fix-gateway`.
    `units`: every Hermes gateway unit of both scopes as systemd says now (system_update.probe_gateway_units),
    `unverified`: the scopes it could not read. Refused while: a scope could not be read (a gateway may run there);
    another gateway unit runs, starts or stops (active, activating, reloading, deactivating); any gateway of this
    Hermes runs (live_gateways: its process, heartbeat, pid file, lock or host record), unless it is `target`'s own
    process (target itself runs: a start changes nothing); `target` is not the unit Hermes names for this home or
    the root that serves it (gateway_unit_name), or it runs another HERMES_HOME (or one that cannot be told:
    system_update.service_home). The Mode auto also never touches a unit that runs (its process would keep the old
    program anyway) nor, for a profile the root's gateway serves, the profile's own legacy unit."""
    from . import system_update
    from .environment import root_of

    target = target if isinstance(target, dict) else {}
    blockers: list[dict] = [{"why": "unverified", "scope": scope} for scope in sorted(set(unverified or ()))]
    key = (target.get("scope"), target.get("unit"))
    for unit in units or ():
        if isinstance(unit, dict) and (unit.get("scope"), unit.get("unit")) != key and system_update.unit_busy(unit):
            blockers.append({"why": "unit_running", "unit": unit.get("unit"), "scope": unit.get("scope"),
                             "state": unit.get("active_state")})
    target_busy = system_update.unit_busy(target)
    if automatic and target_busy:
        blockers.append({"why": "unit_running", "unit": target.get("unit"), "scope": target.get("scope"),
                         "state": target.get("active_state")})
    if not target_busy:
        live = live_gateways(home, now=now)
        if live:
            blockers.append({"why": "gateway_live", "where": [item["home"] for item in live][:5],
                             "evidence": sorted({word for item in live for word in item["evidence"]})})
    home, root = Path(home), root_of(Path(home))
    if served is None:
        served = bool(gateway(home, now=now).get("served_by_root"))
    owners = _bare_owners(host)
    name = str(target.get("unit") or "")
    expected = root if name == gateway_unit_name(root, owners=owners) else \
        home if name == gateway_unit_name(home, owners=owners) else None
    if expected is None:
        blockers.append({"why": "not_this_hermes", "unit": name,
                         "expected": expected_gateway_units(home, served=served, owners=owners)})
        return blockers
    if automatic and served and expected is home and not system_update.same_home(home, root):
        blockers.append({"why": "legacy_unit", "unit": name})   # the root's gateway serves this profile
    runs, _why = system_update.service_home(host or system_update.Host(), target)
    if runs is None:
        blockers.append({"why": "home_unknown", "unit": name})
    elif not system_update.same_home(runs, expected):
        blockers.append({"why": "other_home", "unit": name, "runs": runs, "expected": str(expected)})
    return blockers


BLOCKER_WORDS = {
    "unverified": ("systemd's {scope} services could not be read here: a gateway may run there",
                   "os serviços {scope} do systemd não puderam ser lidos aqui: um gateway pode estar rodando lá"),
    "unit_running": ("the service `{unit}` ({scope}) is {state}: a gateway runs, or is starting or stopping",
                     "o serviço `{unit}` ({scope}) está {state}: um gateway roda, ou está subindo ou parando"),
    "gateway_live": ("a gateway of this Hermes runs now ({where}; seen by: {evidence})",
                     "um gateway deste Hermes está rodando agora ({where}; visto por: {evidence})"),
    "not_this_hermes": ("`{unit}` is not the service Hermes names for this home (it would be {expected})",
                        "`{unit}` não é o serviço que o Hermes dá a esta pasta (seria {expected})"),
    "legacy_unit": ("`{unit}` is this profile's old per-profile gateway; the root's gateway serves this profile",
                    "`{unit}` é o gateway antigo, por perfil, deste perfil; o gateway da raiz atende este perfil"),
    "home_unknown": ("which Hermes `{unit}` runs (its HERMES_HOME) cannot be told from here",
                     "não dá para saber daqui qual Hermes `{unit}` roda (o HERMES_HOME dele)"),
    "other_home": ("`{unit}` runs another Hermes (HERMES_HOME={runs}), not {expected}",
                   "`{unit}` roda outro Hermes (HERMES_HOME={runs}), não {expected}"),
}


def blocker_words(blockers, lang: str = "en") -> list[str]:
    """Each reason of start_blockers in plain words (en, pt)."""
    pt = 1 if lang == "pt" else 0
    lines = []
    for item in blockers or ():
        if not isinstance(item, dict) or item.get("why") not in BLOCKER_WORDS:
            continue
        values = {key: (", ".join(str(part) for part in value) if isinstance(value, list) else value)
                  for key, value in item.items()}
        if item.get("scope") in ("user", "system"):
            values["scope"] = {"user": ("user", "do usuário"), "system": ("system", "do sistema")}[item["scope"]][pt]
        values.setdefault("state", "?")
        try:
            lines.append(BLOCKER_WORDS[item["why"]][pt].format(**values))
        except (KeyError, IndexError, ValueError):
            continue
    return lines


# ── the journal of gateway repairs: every repair and every undo, for the whole Hermes ─────────────────────────
JOURNAL_NAME = "gateway-repair.json"   # in <root>/evolution: one for every profile (0.9–0.11 kept the last one only)
JOURNAL_KEEP = 40


def _journal_path(home: Path) -> Path:
    from .environment import root_of

    return root_of(Path(home)) / "evolution" / JOURNAL_NAME


def _legacy_paths(home: Path) -> list[Path]:
    """Where 0.9–0.11 kept their single record: the profile that ran `care fix-gateway`, the lead (Mode auto)."""
    from .environment import lead_home

    paths = [Path(home) / "evolution" / JOURNAL_NAME]
    try:
        lead = lead_home(Path(home))
    except Exception:
        lead = None
    if lead is not None:
        paths.append(Path(lead) / "evolution" / JOURNAL_NAME)
    return paths


def _read_events(path: Path) -> list[dict]:
    import hashlib

    data = _read_json(path, 2_000_000)
    if isinstance(data, dict) and isinstance(data.get("events"), list):
        return [item for item in data["events"] if isinstance(item, dict)]
    if isinstance(data, dict) and data.get("unit") and data.get("backup"):   # 0.9–0.11: the last repair only
        ident = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]
        try:
            at = path.stat().st_mtime
        except OSError:
            at = 0.0
        return [{"event": "repair", "id": f"legacy-{ident}", "at": at, "legacy": str(path),
                 **{key: data.get(key) for key in ("unit", "scope", "file", "backup")}}]
    return []


def repair_events(home: Path) -> list[dict]:
    """Every repair and undo of the gateway's unit this Hermes recorded, oldest first."""
    journal = _journal_path(home)
    events = _read_events(journal)
    seen = {item.get("id") for item in events}
    for path in _legacy_paths(home):
        if path == journal:
            continue
        for item in _read_events(path):
            if item.get("id") not in seen:
                events.append(item)
                seen.add(item.get("id"))
    return sorted(events, key=lambda item: float(item.get("at") or 0) if isinstance(item.get("at"), (int, float))
                  else 0.0)


def pending_repairs(home: Path) -> list[dict]:
    """The repairs not undone yet, newest first: `care undo-gateway` undoes the first, and run again the next, back
    to the unit as it was before the first repair."""
    events = repair_events(home)
    # undone, or retired: a repair whose file changed since (Hermes reinstalled the service) can never be undone
    closed = {item.get("id") for item in events if item.get("event") in ("undo", "retired")}
    return [item for item in reversed(events) if item.get("event") == "repair" and item.get("id") not in closed]


def retire_repairs(home: Path, entries, *, now: float) -> list[dict]:
    """Take repairs that can no longer be undone (system_update.undoable: their file changed since, or their copy is
    gone) off the list `care undo-gateway` walks, each said once. Returns the ones retired. Never raises."""
    retired = []
    for entry, why in entries:
        try:
            record_repair_event(home, {"event": "retired", "id": entry.get("id"), "why": why, "at": now})
        except OSError:
            continue
        retired.append({"unit": entry.get("unit"), "file": entry.get("file"), "why": why})
    return retired


def _journal_lock(folder: Path):
    import contextlib

    @contextlib.contextmanager
    def held():
        try:
            import fcntl
        except ImportError:
            yield
            return
        try:
            handle = os.open(folder / f".{JOURNAL_NAME}.lock", os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                             0o600)
        except OSError:
            yield
            return
        try:
            give_up = time.monotonic() + 10
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= give_up:
                        raise OSError("the gateway repair journal stayed locked") from None
                    time.sleep(0.05)
            yield
        finally:
            os.close(handle)
    return held()


def record_repair_event(home: Path, event: dict) -> dict:
    """Add one event to the journal (written whole, owner-only, never through a link), keeping the newest
    JOURNAL_KEEP. A repair gets its id here. Raises OSError when it cannot be written."""
    import secrets

    path = _journal_path(home)
    folder = path.parent
    if folder.is_symlink() or path.is_symlink():
        raise OSError("the plugin's folder of this Hermes is a link")
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    event = {**event}
    event.setdefault("id", secrets.token_hex(6))
    with _journal_lock(folder):
        events = _read_events(path) + [event]
        text = json.dumps({"events": events[-JOURNAL_KEEP:]}, ensure_ascii=False, indent=1)
        temporary = folder / f".{path.name}.{secrets.token_hex(4)}.tmp"
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    return event


def repair_skip_code(unit) -> str:
    """The situation an undo of a repair stands for: this unit, back on this program. The Mode auto never repairs it
    again (the owner's "no"); a new situation (another program gone) is a new code."""
    unit = unit if isinstance(unit, dict) else {}
    path = unit.get("exec_path") if unit.get("exec_path") is not None else unit.get("from")
    return f"gateway_repair:{unit.get('scope')}:{unit.get('unit')}:{str(path or '')[:300]}"


def repair_skipped(home: Path, unit: dict) -> bool:
    """Whether the owner undid a repair of exactly this situation (the shared state of the Hermes, or this
    profile's database)."""
    from .environment import root_of, state
    from .experience import experience

    code = repair_skip_code(unit)
    try:
        if code in state(root_of(Path(home)))["skip"]:
            return True
    except Exception:
        pass
    try:
        with experience(home) as conn:
            return conn.execute("SELECT 1 FROM cursor WHERE key=?", (f"auto_skip:{code}",)).fetchone() is not None
    except Exception:
        return True   # cannot be told: never repaired on a guess


def remember_undo(home: Path, event: dict, *, now: float) -> None:
    """After `care undo-gateway`: the undo in the journal, the owner's "no" where the Mode auto reads it (the shared
    state of the Hermes and the lead's database), and the repair's line in the lead's history marked undone.
    Never raises."""
    from . import decisions
    from .environment import lead_home, profiles, remember, root_of
    from .experience import experience

    if not event.get("from") and event.get("backup"):   # a record of 0.9–0.11: the program is in the copy kept
        from .system_update import _exec_program

        try:
            event = {**event, "from": _exec_program(Path(str(event["backup"])).read_text(encoding="utf-8"))}
        except (OSError, UnicodeError):
            pass
    code = repair_skip_code(event)
    root = root_of(Path(home))
    try:
        record_repair_event(home, {"event": "undo", "id": event.get("id"), "at": now})
    except OSError:
        pass
    try:
        remember(root, skip=[code])
    except Exception:
        pass
    try:
        lead = lead_home(Path(home))
    except Exception:
        lead = None
    homes: dict[str, Path] = {}
    for candidate in (dict(profiles(root)).get(str(event.get("profile") or "")), lead, Path(home)):
        if candidate is not None:   # the profile that recorded the repair, the lead, this one: each once
            homes.setdefault(os.path.realpath(candidate), Path(candidate))
    for where in homes.values():
        try:
            with experience(where) as conn:
                conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES (?, ?)", (f"auto_skip:{code}", str(now)))
                rows = conn.execute("SELECT id, before, after FROM decision_log WHERE kind='maintenance' AND "
                                    "target='gateway_repair' AND undone IS NULL ORDER BY id DESC LIMIT 50").fetchall()
                for row in rows:
                    before, after = json.loads(row["before"] or "{}"), json.loads(row["after"] or "{}")
                    if before.get("unit") == event.get("unit") and after.get("path") == event.get("to"):
                        decisions.mark_undone(conn, row["id"], now=now)
                        break
        except Exception:
            continue
    legacy = event.get("legacy")
    if legacy and Path(legacy) != _journal_path(home):
        try:
            Path(legacy).unlink()
        except OSError:
            pass


def forget_undo(home: Path, unit: dict) -> None:
    """The owner repaired by hand what they had undone: the Mode auto may look after that situation again."""
    from .environment import forget_skip, root_of
    from .experience import experience

    code = repair_skip_code(unit)
    try:
        forget_skip(root_of(Path(home)), code)
    except Exception:
        pass
    try:
        with experience(home) as conn:
            conn.execute("DELETE FROM cursor WHERE key=?", (f"auto_skip:{code}",))
    except Exception:
        pass


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


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# what looks like a credential in what a Hermes command printed (as reflector._SCRUB names them): never kept
_SECRET = re.compile(r"\b(?:ya29\.|glpat-|rk_live_|rk_test_|sk_live_|sk_test_|pk_live_|xox[a-z]-|ghp_|gho_|ghs_|ghr_|"
                     r"ghu_|github_pat_|sk-|hf_|AKIA|AIza)[A-Za-z0-9_\-.]{6,}|\bnpm_[A-Za-z0-9]{16,}")
STEP_LINE_CHARS, STEP_PROBLEMS = 300, 12
_PASSED = ("✓", "✔")


def _step(command: str, code: int, out: str, *, tail: int) -> dict:
    """One Hermes command as kept: its exit code, its last `tail` lines and its `problems`, the lines that are not a
    passed check (✗ first, then ⚠, then the rest, STEP_PROBLEMS at most): what says why it failed, which the tail
    alone loses when a long check ends with passed lines. Each line without colour codes or anything that looks like
    a credential, STEP_LINE_CHARS at most."""
    lines = [_SECRET.sub("<secret>", _ANSI.sub("", line)).rstrip()[:STEP_LINE_CHARS]
             for line in (out or "").strip().splitlines()]
    failing = [line for line in lines if line.strip() and not line.lstrip().startswith(_PASSED)]
    ranked = sorted(failing, key=lambda line: (0 if line.lstrip().startswith("✗") else
                                                1 if line.lstrip().startswith("⚠") else 2))
    return {"command": command, "exit": code, "tail": lines[-tail:], "problems": ranked[:STEP_PROBLEMS]}


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
        steps.append(_step("hermes " + " ".join(ACTIONS["backup"][0]), code, out, tail=3))
        if code != 0:
            return {"ok": False, "steps": steps, "reason": "backup_failed"}
    code, out = hermes_output(home, args, runner=runner, timeout=timeout)
    steps.append(_step("hermes " + " ".join(args), code, out, tail=6))
    result = {"ok": code == 0, "steps": steps}
    if key.startswith("doctor"):
        result["doctor"] = summarize(out)
        # `hermes doctor` ends with 1 when it found problems: that is its answer, not a failure of the run
        if code == 1 and (result["doctor"]["summary"] or result["doctor"]["actions"]):
            result["ok"], result["problems_found"] = True, result["doctor"]["found"] or len(result["doctor"]["actions"])
    return result
