"""The whole Hermes at once: every profile, its plugins and skills, how much it is used, and what to do.

One profile looks after the whole Hermes (`lead`): the default one when the plugin is on there and has run a pass in
the last two days, else the first such profile in name order, else the first profile with the plugin on. The work is
then done once however many profiles load the plugin. `scan` reads and never writes. It follows a link only where
Hermes itself does (a plugin folder that is a link to a checkout); anywhere else a link is skipped. It reads each profile's config.yaml, each plugin's manifest, install record, git checkout and Python
source (parsed with `ast`, never imported or run), each skill's SKILL.md and Hermes' skill usage records. Every file
is opened as a regular file only (a named pipe, a device or a link is skipped) and read up to a size limit.

`findings` turns that, Hermes' plugin catalog and the knowledge base (ecosystem.py) into situations of the playbook,
each with its reason, its numbers and its action. What the plugin reads in a plugin's source:
- which tools a transform hook acts on: only some (`if tool_name != "web_search": return`, `_TOOLS.get(tool_name)`),
  all but some (`if tool_name in SKIP: return`), or any; Hermes keeps only the first transform result of each call, in
  its load order (requires_plugins first, then alphabetical by manifest name), so two plugins conflict only when they
  can transform the same tool;
- whether a `pre_tool_call` hook rewrites the terminal's command (it writes its `args["command"]`, or answers
  Hermes' {"action": "modify", "args": {"command": ...}} directive).

Nothing found here is trusted later as it was stored: before the plugin changes anything in a profile, `verify`
reads that profile again from disk and the change runs only if the same situation is still there.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path

PLUGIN_ID = "hermes-atlas-evolution"
REPOSITORY = "sesantanajr/hermes-atlas-evolution"
PROFILE_MAX = 100
PLUGIN_MAX = 200
SKILL_MAX = 3000
SKILL_WALK_MAX = 20_000          # entries looked at under one profile's skills/
SOURCE_FILES_MAX = 400           # Python files read per plugin
SOURCE_FILE_BYTES = 128 * 1024   # a larger file is skipped (its hooks count as acting on any tool)
SOURCE_PLUGIN_BYTES = 384 * 1024    # parsed per plugin (only the files that register hooks and define their callbacks)
SOURCE_READ_BYTES = 4 * 1024 * 1024  # read per plugin to find those files
SOURCE_SCAN_BYTES = 8 * 1024 * 1024   # all plugins of one scan; past it, hooks are not read (no finding from them)
UNUSED_SKILL_DAYS = 30   # Hermes' own curator.archive_after_days default
IDLE_PLUGIN_DAYS = 1     # installed and not loaded for longer than this (a plugin just installed is left alone)
LEFTOVER_DAYS = 1
IDLE_PROFILE_DAYS = 30
LEAD_PASS_SECONDS = 2 * 86_400
LOCK_WAIT_SECONDS = 10   # the shared state's lock is waited for this long at most
SIMILAR = 0.6
FIRST_WINS = ("transform_tool_result", "transform_terminal_output", "transform_llm_output")
KINDS = ("plugin_removed", "plugin_transform_overlap", "plugin_transform_protected", "plugin_transform_unclear",
         "plugin_hints_overlap",
         "plugin_duplicate", "plugin_unused", "plugin_leftover", "plugin_update", "plugin_obsolete", "self_missing",
         "self_off", "profile_idle", "skills_unused", "skills_duplicate", "skills_similar", "skills_shared",
         "doctor_cache", "doctor_checkpoints", "doctor_security", "doctor_issue")
_TOOLISH = {"tool_name", "tool", "name", "function_name", "tool_id"}
_ARGSISH = {"args", "arguments", "tool_args", "tool_input", "params", "kwargs_args"}
_PROFILE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")   # Hermes' own rule for a profile name (hermes_constants)
_PLUGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}")
_LEFTOVER = re.compile(r"\.install-[A-Za-z0-9_-]{1,64}")
_SHA = re.compile(r"[0-9a-f]{40}")
_WORD = re.compile(r"[a-zà-ú0-9]{4,}")
_HOOK_NAMES = re.compile(r"(pre|post|on|transform)_[a-z_]{2,60}")


class ScanTimeout(Exception):
    """The scan ran past its deadline; nothing it found is used."""


# ── reading, safely ──────────────────────────────────────────────────────────────────────────────────────────────
def _read_up_to(handle: int, size: int) -> bytes:
    chunks, left = [], size
    while left > 0:
        chunk = os.read(handle, min(left, 1 << 20))
        if not chunk:
            break
        chunks.append(chunk)
        left -= len(chunk)
    return b"".join(chunks)


def read_bytes(path: Path, limit: int, *, part: str = "") -> bytes | None:
    """The one reader of a file the plugin did not write (a memory file, a SKILL.md, a manifest, Hermes' records):
    a regular file's bytes, or None. A link, a named pipe, a device or an unreadable file is never read (opening a
    pipe would block the pass for good, a device could stream for ever): the path is looked at without following a
    link, opened with O_NOFOLLOW and O_NONBLOCK, and checked again on the open file. A file larger than `limit` is
    refused; with `part="head"` its first `limit` bytes are read instead, with `part="tail"` its last ones from the
    first whole line."""
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_BINARY", 0))
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        handle = os.open(path, flags)
    except (OSError, ValueError):
        return None
    try:
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode):
            return None
        if info.st_size > limit and part == "tail":
            os.lseek(handle, info.st_size - limit, os.SEEK_SET)
            data = _read_up_to(handle, limit)
            return data.split(b"\n", 1)[1] if b"\n" in data else b""
        if info.st_size > limit and part != "head":
            return None
        data = _read_up_to(handle, limit + 1)
        if len(data) > limit:   # it grew while it was read
            return data[:limit] if part == "head" else None
        return data
    except (OSError, MemoryError, OverflowError):
        return None
    finally:
        os.close(handle)


def read_text(path: Path, limit: int, *, part: str = "") -> str | None:
    """read_bytes as text (UTF-8, a byte order mark left out, an invalid byte replaced)."""
    data = read_bytes(path, limit, part=part)
    if data is None:
        return None
    try:
        return data.decode("utf-8-sig", errors="replace")
    except MemoryError:
        return None


def _is_dir(path: Path) -> bool:
    """A real folder (not a link to one)."""
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except (OSError, ValueError):
        return False


def _is_plugin_dir(path: Path) -> bool:
    """A plugin's folder as Hermes loads it: a real folder or a link to one (a development checkout)."""
    try:
        return stat.S_ISDIR(os.stat(path).st_mode)
    except (OSError, ValueError):
        return False


_UNSAFE = re.compile(r"[\x00-\x1f\x7f`]")


def safe_name(name: str) -> bool:
    """A folder name that can be stored, shown and put in a command: valid UTF-8, no control character, no backtick."""
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:   # a name that is not UTF-8 on disk (os.scandir keeps it as surrogates)
        return False
    return not _UNSAFE.search(name)


def _entries(folder: Path, limit: int) -> list[os.DirEntry]:
    try:
        with os.scandir(folder) as found:
            items = []
            for entry in found:
                if not safe_name(entry.name):
                    continue
                items.append(entry)
                if len(items) >= limit:
                    break
    except OSError:
        return []
    return sorted(items, key=lambda entry: entry.name)


# ── profiles and the lead ────────────────────────────────────────────────────────────────────────────────────────
_HOME_MARKERS = ("config.yaml", ".env", "state.db")   # hermes_constants._HERMES_HOME_MARKERS
# hermes_constants._PROFILE_IDENTITY_MARKERS: a folder under profiles/ is a profile only with one of these
_IDENTITY_MARKERS = ("config.yaml", ".env", "SOUL.md", "profile.yaml", "auth.json", "state.db")
_TOMBSTONES = ".deleted"   # profiles/.deleted/<name>: `hermes profile delete` marked the profile gone


def _real_profiles_folder(folder: Path) -> bool:
    """Hermes' _is_hermes_profiles_root: `folder` is <home>/profiles of a real Hermes home (one named .hermes, one
    with Hermes' own files, or a profiles folder with tombstones), not any folder that happens to be named so."""
    root = folder.parent
    if root.name == ".hermes":
        return True
    try:
        return _is_dir(folder / _TOMBSTONES) or any(os.path.exists(root / marker) for marker in _HOME_MARKERS)
    except (OSError, ValueError):
        return False


def home_parts(home: Path) -> tuple[Path, str]:
    """(root, profile name) of a Hermes home, the way Hermes reads them (hermes_constants.named_profile_home and
    hermes_cli.profiles.profile_root_for_env_home):
    - `<root>/profiles/<name>` as written, before links are followed: a profile folder that links elsewhere is still
      profile <name> of <root>, and `hermes --profile <name>` with HERMES_HOME so spelled finds it;
    - a default home (`~/.hermes`) is a root and the default profile, wherever it links to;
    - any other path is followed to where it leads, and is a named profile only under the profiles folder of a real
      Hermes home: a link to /srv/profiles/alice never makes /srv a root whose other folders are profiles."""
    plain = Path(os.path.abspath(home))
    if plain.parent.name == "profiles" and not plain.name.startswith("."):
        return plain.parent.parent, plain.name
    if plain.name == ".hermes":
        return plain, "default"
    try:
        real = plain.resolve()
    except (OSError, RuntimeError, ValueError):
        return plain, "default"
    if real != plain and real.parent.name == "profiles" and not real.name.startswith(".") \
            and _real_profiles_folder(real.parent):
        return real.parent.parent, real.name
    return plain, "default"


def root_of(home: Path) -> Path:
    """The root of the Hermes a home belongs to (home_parts), spelled as given: links are not followed in it, so the
    homes of its profiles, and the HERMES_HOME the plugin gives Hermes' commands, keep the spelling Hermes reads."""
    return home_parts(home)[0]


def deleted_profile(folder: Path) -> bool:
    """Whether `hermes profile delete` marked this profile folder gone (a tombstone in profiles/.deleted/), even while
    the folder is still there."""
    try:
        return os.path.lexists(Path(folder).parent / _TOMBSTONES / Path(folder).name)
    except (OSError, ValueError):
        return True


def is_profile(folder: Path) -> bool:
    """A named profile as Hermes lists one (hermes_cli.profiles._iter_named_profile_dirs): a real folder (here never
    a link) that holds one of Hermes' identity files and has no tombstone. What cron or a log rotation leaves under
    profiles/, or a profile `hermes profile delete` marked gone, is not one."""
    folder = Path(folder)
    if not _is_dir(folder) or deleted_profile(folder):
        return False
    try:
        return any(os.path.isfile(folder / marker) or os.path.islink(folder / marker) for marker in _IDENTITY_MARKERS)
    except (OSError, ValueError):
        return False


def profiles(root: Path) -> list[tuple[str, Path]]:
    """(name, home) of every profile of this Hermes: the default one (the root) first, then the ones Hermes itself
    lists (is_profile), by name. A profile folder that is a link, a folder named `default`, or one whose name is not
    a profile name, is not one."""
    found = [("default", Path(root))]
    for entry in _entries(Path(root, "profiles"), PROFILE_MAX * 4):
        if len(found) > PROFILE_MAX:
            break
        if entry.name != "default" and _PROFILE.fullmatch(entry.name) and is_profile(Path(entry.path)):
            found.append((entry.name, Path(entry.path)))
    return found


_OFF_WORDS = ("false", "off", "no", "0")


def fleet_managed(home: Path, *, read_config=None) -> bool:
    """Whether the profile that looks after the whole Hermes may install this plugin, update it or move a folder to
    the trash in this profile by itself (the Mode auto). A profile opts out with
    `hermes -p <profile> config set plugins.entries.hermes-atlas-evolution.settings.fleet_managed false`, or with
    {"evolution": {"fleet_managed": false}} in its evolution/settings.json (which can only narrow it). Both are read
    with the plugin's safe readers. A config.yaml that cannot be read now (missing, invalid while it is edited, too
    large) counts as not managed: an opt-out it may hold is never missed; an unreadable settings.json narrows nothing."""
    def off(value) -> bool:
        return value is False or (isinstance(value, str) and value.strip().lower() in _OFF_WORDS) \
            or (type(value) is int and value == 0)

    config = (read_config or _read_config)(Path(home))
    if not isinstance(config, dict):
        return False
    section = config.get("plugins")
    entries = section.get("entries") if isinstance(section, dict) else None
    entry = entries.get(PLUGIN_ID) if isinstance(entries, dict) else None
    settings = entry.get("settings") if isinstance(entry, dict) else None
    if isinstance(settings, dict) and off(settings.get("fleet_managed")):
        return False
    text = read_text(Path(home) / "evolution" / "settings.json", 65_536)
    try:
        own = json.loads(text) if text else {}
    except (ValueError, RecursionError):
        own = {}
    own = own.get("evolution") if isinstance(own, dict) else None
    return not (isinstance(own, dict) and off(own.get("fleet_managed")))


def _read_config(home: Path):
    """A profile's config.yaml, read-only with yaml.safe_load (Hermes' own loader may migrate and write the file);
    None when it cannot be read or PyYAML is missing (the standalone runner)."""
    text = read_text(Path(home) / "config.yaml", 8 * 1024 * 1024)
    if text is None:
        return None
    try:
        import yaml
    except ImportError:
        return None
    try:
        data = yaml.load(text, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader)) if text.strip() else {}
    except (yaml.YAMLError, ValueError, RecursionError, MemoryError):
        return None
    return data if isinstance(data, dict) else None


def _plugin_lists(config) -> tuple[list | None, list]:
    plugins = config.get("plugins") if isinstance(config, dict) else None
    if not isinstance(plugins, dict):
        return None, []
    enabled = plugins.get("enabled")
    disabled = plugins.get("disabled")
    return ([str(item) for item in enabled] if isinstance(enabled, list) else None,
            [str(item) for item in disabled] if isinstance(disabled, list) else [])


def has_plugin(home: Path, config) -> bool:
    enabled, disabled = _plugin_lists(config)
    return _is_plugin_dir(Path(home) / "plugins" / PLUGIN_ID) and enabled is not None \
        and PLUGIN_ID in enabled and PLUGIN_ID not in disabled


def _last_pass(home: Path) -> float:
    """When this profile's plugin last finished a pass (its own database, read-only), 0 when never."""
    path = Path(home) / "evolution" / "evolution.sqlite3"
    if not path.is_file():
        return 0.0
    import sqlite3
    from urllib.parse import quote

    try:
        source = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=1)
        try:
            row = source.execute("SELECT value FROM cursor WHERE key='loop_status'").fetchone()
        finally:
            source.close()
        found = json.loads(row[0]) if row else {}
        return float(found.get("at") or 0) if isinstance(found, dict) else 0.0
    except (sqlite3.Error, ValueError, TypeError, OSError):
        return 0.0


_LEAD_MEMO: dict = {}
_READ_CONFIG = _read_config   # the memo is used only with Hermes' own reader (a test's reader is never memoized)


def lead(root: Path, *, read_config=None, now: float | None = None) -> str | None:
    """The profile that looks after the whole Hermes: of the profiles with the plugin on, the default one when it ran a
    pass in the last two days, else the first one (by name) that did, else the default one or the first one. A profile
    nobody uses never keeps the others waiting, and the choice does not move between passes."""
    read_config = read_config or _read_config
    now = time.time() if now is None else now
    found = profiles(root)
    stamps = []
    for _name, home in found:
        try:
            info = os.stat(Path(home) / "config.yaml")
            stamps.append((info.st_mtime_ns, info.st_size))
        except OSError:
            stamps.append(None)
    key = (str(root), tuple(stamps), int(now // 3600))
    memo = read_config is _READ_CONFIG
    if memo and key in _LEAD_MEMO:
        return _LEAD_MEMO[key]
    candidates = [(name, home) for name, home in found if has_plugin(home, read_config(home))]
    leader = None
    for name, home in candidates:
        if now - _last_pass(home) < LEAD_PASS_SECONDS:
            leader = name
            break
    if leader is None and candidates:
        leader = candidates[0][0]
    if memo:
        if len(_LEAD_MEMO) > 32:
            _LEAD_MEMO.clear()
        _LEAD_MEMO[key] = leader
    return leader


def lead_home(home: Path, *, read_config=None) -> Path | None:
    """The home of the profile that looks after the whole Hermes, None when no profile has the plugin on."""
    root = root_of(home)
    leader = lead(root, read_config=read_config)
    for name, folder in profiles(root):
        if name == leader:
            return Path(folder)
    return None


# ── shared state of the whole Hermes (what was undone, where the plugin has been) ───────────────────────────────
def _state_path(root: Path) -> Path:
    return Path(root) / "evolution" / "fleet-state.json"


def state(root: Path) -> dict:
    """{"seen": [profiles where the plugin was ever found], "skip": [situations the owner undid]}. Kept in the
    default profile's data folder of the plugin, so a change of lead keeps it."""
    text = read_text(_state_path(root), 1_000_000)
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        data = {}
    data = data if isinstance(data, dict) else {}
    return {"seen": sorted({str(item) for item in data.get("seen") or () if isinstance(item, str)}),
            "skip": sorted({str(item) for item in data.get("skip") or () if isinstance(item, str)})}


def remember(root: Path, *, seen=(), skip=()) -> None:
    """Add to the shared state (written whole, owner-only, never through a link)."""
    folder = Path(root) / "evolution"
    if folder.is_symlink() or (folder.exists() and not _is_dir(folder)):
        return
    with _locked(folder):
        _remember(root, folder, seen, skip)


def exclusive_lock(lock_path: Path, *, wait_s: float):
    """An exclusive lock on the file `lock_path` across processes, the way Hermes takes its own (flock on POSIX,
    msvcrt.locking of its first byte on Windows), never through a link. It is waited for `wait_s` at most: past it,
    TimeoutError (a process stuck while it holds the lock must not hang every pass and every menu). OSError when the
    lock file cannot be opened. Where the system has neither (no known platform), nothing is locked."""
    import contextlib

    try:
        import fcntl
    except ImportError:
        fcntl = None
    try:
        import msvcrt
    except ImportError:
        msvcrt = None

    @contextlib.contextmanager
    def held():
        handle = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
                         0o600)
        locked = False
        try:
            give_up = time.monotonic() + wait_s
            while fcntl is not None or msvcrt is not None:
                try:
                    if fcntl is not None:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    else:
                        os.lseek(handle, 0, os.SEEK_SET)
                        msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    if time.monotonic() >= give_up:
                        raise TimeoutError(str(lock_path)) from None
                    time.sleep(0.1)
            yield
        finally:
            if locked:
                with contextlib.suppress(OSError):
                    if fcntl is not None:
                        fcntl.flock(handle, fcntl.LOCK_UN)
                    else:
                        os.lseek(handle, 0, os.SEEK_SET)
                        msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
            os.close(handle)
    return held()


def _locked(folder: Path):
    """An exclusive lock on the shared state across processes (exclusive_lock: POSIX and Windows); when the lock file
    cannot be made at all, the write goes on unlocked, as before."""
    import contextlib

    @contextlib.contextmanager
    def held():
        try:
            folder.mkdir(mode=0o700, exist_ok=True)
            lock = exclusive_lock(folder / ".fleet-state.lock", wait_s=LOCK_WAIT_SECONDS)
            lock.__enter__()
        except TimeoutError:
            raise TimeoutError(f"The shared state of this Hermes ({folder / '.fleet-state.lock'}) stayed locked by "
                               f"another process for {LOCK_WAIT_SECONDS} s; nothing was written. Try again; if it "
                               "lasts, look for a stuck `hermes` process.") from None
        except OSError:
            yield
            return
        try:
            yield
        finally:
            lock.__exit__(None, None, None)
    return held()


def _ordered(values, limit: int) -> list[str]:
    """The strings of `values` once each, in their order (oldest first), keeping the newest `limit`."""
    seen: dict[str, None] = {}
    for item in values or ():
        if isinstance(item, str):
            seen.pop(item, None)
            seen[item] = None
    return list(seen)[-limit:]


def _raw_state(root: Path) -> dict:
    """The shared state as written, oldest entry first (state() sorts it for its readers)."""
    text = read_text(_state_path(root), 1_000_000)
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        data = {}
    data = data if isinstance(data, dict) else {}
    return {"seen": _ordered(data.get("seen"), 1000), "skip": _ordered(data.get("skip"), 5000)}


def forget_skip(root: Path, code: str) -> None:
    """Take a situation out of the shared skip list (the owner took back the "no" that put it there)."""
    folder = Path(root) / "evolution"
    if folder.is_symlink() or not _is_dir(folder):
        return
    with _locked(folder):
        _remember(root, folder, (), (), drop={str(code)})


def _remember(root: Path, folder: Path, seen, skip, drop=frozenset()) -> None:
    current = _raw_state(root)
    # added at the end, and past the limit the oldest go first: a new entry is never the one dropped (a sorted list
    # cut at its limit dropped whatever sorted last, often what was just added)
    merged = {"seen": _ordered([*current["seen"], *[str(item) for item in seen]], 1000),
              "skip": [item for item in _ordered([*current["skip"], *[str(item) for item in skip]], 5000)
                       if item not in drop]}
    if merged == current:
        return
    try:
        folder.mkdir(mode=0o700, exist_ok=True)
        temporary = folder / f".fleet-state.{os.getpid()}.tmp"
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(merged, stream)
        os.replace(temporary, _state_path(root))
    except OSError:
        pass


# ── plugins ──────────────────────────────────────────────────────────────────────────────────────────────────────
def manifest(folder: Path) -> dict | None:
    """name, version, kind, description, requires_plugins and the hooks and tools a plugin.yaml (or plugin.yml)
    declares. A small reader: no PyYAML, which the standalone runner may not have."""
    text = None
    for name in ("plugin.yaml", "plugin.yml"):
        text = read_text(Path(folder) / name, 65_536)
        if text is not None:
            break
    if text is None:
        return None
    found: dict = {"name": Path(folder).name, "version": "", "kind": "", "description": "", "provides_hooks": [],
                   "provides_tools": [], "requires_plugins": []}
    current, folded = None, []
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.replace("\t", "    ")
        if line and not line.startswith((" ", "#", "-")):
            if current == "description" and folded:
                found["description"] = " ".join(folded)[:400]
            key, _, value = line.partition(":")
            current, folded = key.strip().strip("'\""), []
            value = value.split(" #", 1)[0].strip()
            if current in ("name", "version", "kind") and value:
                found[current] = value.strip("'\"")[:96]
            elif current == "description" and value and value not in (">-", ">", "|", "|-", ">+", "|+"):
                found["description"] = value.strip("'\"")[:400]
                current = None
            elif current in ("provides_hooks", "provides_tools", "requires_plugins") and value.startswith("["):
                found[current] = [item.strip().strip("'\"")[:96] for item in value.strip("[]").split(",") if item.strip()]
        elif current == "description" and line.strip():
            folded.append(line.strip())
        elif current in ("provides_hooks", "provides_tools", "requires_plugins") and line.strip().startswith("- "):
            item = line.strip()[2:].strip()
            if current == "requires_plugins" and item.startswith("id:"):
                item = item[3:].strip()
            found[current].append(item.strip("'\"")[:96])
        elif current == "requires_plugins" and line.strip().startswith("id:") and found[current] == []:
            found[current].append(line.strip()[3:].strip().strip("'\"")[:96])
    if current == "description" and folded:
        found["description"] = " ".join(folded)[:400]
    if not _PLUGIN.fullmatch(found["name"]):
        found["name"] = Path(folder).name
    return found


def git_origin(folder: Path) -> tuple[str, str]:
    """(origin URL, HEAD commit) of a plugin's git checkout, read from its .git files; ("", "") when not one."""
    git = Path(folder) / ".git"
    if not _is_dir(git):
        return "", ""
    repo = sha = ""
    config = read_text(git / "config", 65_536) or ""
    section = re.search(r'\[remote "origin"\]([^\[]*)', config)
    url = re.search(r"^\s*url\s*=\s*(\S+)", section.group(1), re.M) if section else None
    repo = url.group(1)[:300] if url else ""
    head = (read_text(git / "HEAD", 4096) or "").strip()
    if head.startswith("ref: "):
        ref = head[5:].strip()
        if re.fullmatch(r"refs/[A-Za-z0-9._/-]{1,200}", ref) and ".." not in ref:
            sha = (read_text(git / ref, 4096) or "").strip()
            if not _SHA.fullmatch(sha):
                packed = read_text(git / "packed-refs", 1_000_000) or ""
                match = re.search(rf"^([0-9a-f]{{40}}) {re.escape(ref)}$", packed, re.M)
                sha = match.group(1) if match else ""
    else:
        sha = head
    return repo, sha if _SHA.fullmatch(sha or "") else ""


def install_record(home: Path, name: str, folder: str) -> dict:
    """Hermes' install record of a plugin (`plugins/.install-metadata.json`, or the plugin folder's own), {} when
    none. `pinned`: the commit Hermes pinned the install to (an install made with `--ref`), "" when not pinned; a
    pinned plugin refuses `hermes plugins update` ("is pinned to <sha>")."""
    record: dict = {}
    text = read_text(Path(home) / "plugins" / ".install-metadata.json", 1_000_000)
    try:
        data = json.loads(text) if text else {}
    except (ValueError, RecursionError):
        data = {}
    if isinstance(data, dict):
        found = data.get(name) or data.get(folder)
        record = dict(found) if isinstance(found, dict) else {}
    own = read_text(Path(home) / "plugins" / folder / ".install-metadata.json", 65_536) if _PLUGIN.fullmatch(folder) else None
    try:
        local = json.loads(own) if own else {}
    except (ValueError, RecursionError):
        local = {}
    if isinstance(local, dict):
        record = {**local, **record}
    record["pinned"] = _pin_of(record) or _pin_of(local if isinstance(local, dict) else {})
    return record


def _pin_of(record: dict) -> str:
    """The commit an install record pins to, whatever Hermes names the key; "" when it pins nothing."""
    if not isinstance(record, dict) or record.get("pinned") is False:
        return ""
    # Hermes' own record: {"pinned": true, "revision": "<sha>"} (the revision is there unpinned too: only with the flag)
    keys = ("revision",) if record.get("pinned") is True else ()
    for key in (*keys, "pinned", "pin", "pinned_ref", "pinned_sha", "ref", "commit"):
        value = record.get(key)
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{7,40}", value.strip().lower()):
            return value.strip().lower()
    return "pinned" if record.get("pinned") is True else ""


def _size(folder: Path, limit: int = 4000) -> tuple[int, float]:
    """Bytes of a folder (bounded walk, links not followed) and its newest change."""
    total, newest, seen = 0, 0.0, 0
    stack = [Path(folder)]
    while stack and seen < limit:
        current = stack.pop()
        for entry in _entries(current, limit):
            seen += 1
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            newest = max(newest, info.st_mtime)
            if stat.S_ISDIR(info.st_mode):
                stack.append(Path(entry.path))
            elif stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total, newest


# ── reading a plugin's source ────────────────────────────────────────────────────────────────────────────────────
def _constants(tree) -> dict[str, set[str]]:
    """Module-level names bound to literal collections of strings (keys of a dict included)."""
    found: dict[str, set[str]] = {}
    for node in tree.body:
        targets, value = ([node.target], node.value) if isinstance(node, ast.AnnAssign) else (
            (node.targets, node.value) if isinstance(node, ast.Assign) else ((), None))
        strings = _literal_strings(value)
        if strings:
            for target in targets:
                if isinstance(target, ast.Name):
                    found[target.id] = strings
    return found


def _literal_strings(value) -> set[str] | None:
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in ("frozenset", "set", "tuple") \
            and value.args:
        value = value.args[0]
    items = value.keys if isinstance(value, ast.Dict) else value.elts if isinstance(value, (ast.Set, ast.List, ast.Tuple)) \
        else None
    if not items:
        return None
    strings = {item.value for item in items if isinstance(item, ast.Constant) and isinstance(item.value, str)}
    return strings if strings and len(strings) == len(items) else None


def _toolish(node) -> bool:
    return isinstance(node, ast.Name) and node.id in _TOOLISH


def _strings_of(node, constants) -> set[str] | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name) and node.id in constants:
        return set(constants[node.id])
    return _literal_strings(node)


def _compare(node, constants) -> tuple[bool, set[str]] | None:
    """(positive, names) of a test on the tool's name: positive when the test is true for exactly those names
    (`==`, `in`, `.get()`), negative when it is true for every other name (`!=`, `not in`); None when it is no such
    test. `not <test>` flips it."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        inner = _compare(node.operand, constants)
        return None if inner is None else (not inner[0], inner[1])
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        left, right, op = node.left, node.comparators[0], node.ops[0]
        if _toolish(right) and not _toolish(left):
            left, right = right, left
        if not _toolish(left):
            return None
        names = _strings_of(right, constants)
        if names is None:
            return None
        if isinstance(op, (ast.Eq, ast.In, ast.Is)):
            return True, names
        if isinstance(op, (ast.NotEq, ast.NotIn, ast.IsNot)):
            return False, names
    return None


def _exits(body) -> bool:
    """A branch that ends the callback with nothing changed: `return`, `return None` or `return False`. Returning a
    name (`return result`) is a change: Hermes keeps the first string any callback returns, even the original one."""
    if not body:
        return False
    last = body[-1]
    return isinstance(last, ast.Return) and (
        last.value is None or isinstance(last.value, ast.Constant) and last.value.value in (None, False))


def _scope(function, functions, constants, depth: int = 0) -> tuple[str, set[str]]:
    """Which tools a hook callback can change: ("only", names), ("except", names) or ("any", set())."""
    only: set[str] = set()
    excluded: set[str] = set()
    unknown = False
    for node in ast.walk(function):
        if isinstance(node, ast.If):
            tests = [node.test]
            joined = None
            if isinstance(node.test, ast.BoolOp):
                tests, joined = node.test.values, type(node.test.op)
            for test in tests:
                found = _compare(test, constants)
                if found is None:
                    continue
                positive, names = found
                leaves = _exits(node.body)
                if leaves:   # a guard: the callback stops for these names
                    if joined is ast.And and len(tests) > 1:
                        unknown = True   # it stops only when another condition holds too
                    elif positive:
                        excluded |= names
                    else:
                        only |= names
                elif joined is ast.Or and len(tests) > 1:
                    unknown = True   # it acts when another condition holds too
                elif positive:
                    only |= names
                else:
                    excluded |= names
        elif isinstance(node, ast.Call):
            callee = node.func
            if isinstance(callee, ast.Attribute) and callee.attr == "get" and isinstance(callee.value, ast.Name) \
                    and callee.value.id in constants and node.args and _toolish(node.args[0]):
                only |= constants[callee.value.id]
            elif depth < 2 and isinstance(callee, ast.Name) and callee.id in functions \
                    and any(_toolish(argument) for argument in node.args):
                kind, names = _scope(functions[callee.id], functions, constants, depth + 1)
                if kind == "only":
                    only |= names
                elif kind == "except":
                    excluded |= names
    if unknown or (only and excluded):
        return "any", set()
    if only:
        return "only", only
    if excluded:
        return "except", excluded
    return "any", set()


def _params(function) -> set[str]:
    arguments = getattr(function, "args", None)
    if arguments is None:
        return set()
    names = [item.arg for item in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)]
    if names and names[0] in ("self", "cls"):
        names = names[1:]
    found = {name for name in names if name in _ARGSISH}
    if len(names) >= 2 and not found:
        found.add(names[1])   # (tool_name, args, ...) is Hermes' calling convention
    return found


def _rewrites_command(function) -> bool:
    """Whether a pre_tool_call callback changes the tool's `command` argument: it writes `<args>["command"]` (or
    `<args>.update(command=...)`) on its own args parameter, or returns {"action": "modify", "args": {"command": ...}}.
    Writing a `command` key of any other dictionary (a log record) does not count."""
    params = _params(function)
    for node in ast.walk(function):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, (ast.AugAssign, ast.AnnAssign)) else []
        for target in targets:
            if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name) and target.value.id in params \
                    and isinstance(target.slice, ast.Constant) and target.slice.value == "command":
                return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "update" \
                and isinstance(node.func.value, ast.Name) and node.func.value.id in params:
            if any(keyword.arg == "command" for keyword in node.keywords) or any(
                    isinstance(item, ast.Dict) and any(isinstance(key, ast.Constant) and key.value == "command"
                                                      for key in item.keys) for item in node.args):
                return True
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            pairs = {key.value: value for key, value in zip(node.value.keys, node.value.values)
                     if isinstance(key, ast.Constant) and isinstance(key.value, str)}
            action, arguments = pairs.get("action"), pairs.get("args")
            if isinstance(action, ast.Constant) and action.value == "modify" and isinstance(arguments, ast.Dict) \
                    and any(isinstance(key, ast.Constant) and key.value == "command" for key in arguments.keys):
                return True
    return False


def _python_files(folder: Path) -> list[Path]:
    found = []
    for entry in _entries(folder, 2000):
        if entry.name.startswith((".", "__pycache__")):
            continue
        path = Path(entry.path)
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and entry.name.endswith(".py"):
            found.append(path)
        elif stat.S_ISDIR(info.st_mode) and entry.name not in ("tests", "test", "node_modules", "venv", ".venv"):
            for inner in _entries(path, 2000):
                if inner.name.startswith((".", "__pycache__")):
                    continue
                try:
                    kind = inner.stat(follow_symlinks=False).st_mode
                except OSError:
                    continue
                if stat.S_ISREG(kind) and inner.name.endswith(".py"):
                    found.append(Path(inner.path))
                elif stat.S_ISDIR(kind) and inner.name not in ("tests", "test", "node_modules", "venv"):
                    for deeper in _entries(Path(inner.path), 500):   # hooks/result_transforms.py and the like
                        try:
                            if deeper.name.endswith(".py") and stat.S_ISREG(deeper.stat(follow_symlinks=False).st_mode):
                                found.append(Path(deeper.path))
                        except OSError:
                            continue
        if len(found) >= SOURCE_FILES_MAX:
            break
    return found[:SOURCE_FILES_MAX]


def code_hooks(folder: Path, budget: dict | None = None) -> dict | None:
    """What a plugin's Python source registers, without running it:
    {hook: {"scope": "only" | "except" | "any", "tools": [...], "rewrites": bool}}. `ctx.register_hook("hook", f)`
    and helpers called with ("hook", f) are both read; `f` is looked up in every file of the plugin (an imported
    function too). None when the scan's source budget is spent: nothing is concluded from this plugin's code."""
    budget = budget if budget is not None else {"left": SOURCE_SCAN_BYTES}
    texts, read, skipped = [], 0, False
    for path in _python_files(folder):
        text = read_text(path, SOURCE_FILE_BYTES)
        if text is None:
            skipped = True   # too large or not a regular file: what it registers is unknown
            continue
        read += len(text)
        if read > SOURCE_READ_BYTES:
            skipped = True
            break
        texts.append((path.relative_to(folder).as_posix(), text))
    key = hashlib.sha256(repr((skipped, texts)).encode("utf-8", "surrogatepass")).hexdigest()
    memo = budget.setdefault("memo", {})
    if key in memo:   # the same plugin in another profile: read once, and no budget spent again
        return memo[key]
    if budget["left"] <= 0:
        return None
    # Parse only what matters: the files that register hooks, then the files that define the functions they register
    # (and the module-level constants beside them). Text search first; `ast` only on those files.
    registering = [(name, text) for name, text in texts if "hook" in text and "register" in text]
    wanted = set(re.findall(r"_?(?:register\w*hook\w*)\s*\([^)]*?[\"'](?:pre|post|on|transform)_[a-z_]+[\"']\s*,\s*"
                            r"(?:[\w.]+\.)?(\w+)", "\n".join(text for _name, text in registering)))
    chosen = {name for name, _text in registering}
    for name, text in texts:
        if name not in chosen and any(re.search(rf"^\s*(?:async\s+)?def\s+{re.escape(function)}\s*\(", text, re.M)
                                      for function in wanted):
            chosen.add(name)
    trees, used = [], 0
    for name, text in texts:
        if name not in chosen:
            continue
        used += len(text)
        if used > SOURCE_PLUGIN_BYTES:
            skipped = True
            break
        try:
            trees.append(ast.parse(text))
        except Exception:   # SyntaxError, RecursionError, MemoryError (deep nesting): skip the file, never fail
            skipped = True
            continue
    budget["left"] -= used
    functions, constants = {}, {}
    for tree in trees:
        constants.update(_constants(tree))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.setdefault(node.name, node)
    hooks: dict = {}
    for tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id if isinstance(node.func, ast.Name) else ""
            if "hook" not in name.lower():
                continue
            for index, argument in enumerate(node.args[:-1]):
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str) \
                        and _HOOK_NAMES.fullmatch(argument.value):
                    callback = node.args[index + 1]
                    break
            else:
                continue
            hook = argument.value
            function = (callback if isinstance(callback, ast.Lambda)
                        else functions.get(callback.id) if isinstance(callback, ast.Name)
                        else functions.get(callback.attr) if isinstance(callback, ast.Attribute) else None)
            entry = hooks.setdefault(hook, {"scopes": [], "rewrites": False})
            if function is None:
                entry["scopes"].append(("unknown", set()))   # registered with a callback the source does not show
                continue
            try:
                entry["scopes"].append(_scope(function, functions, constants))
                entry["rewrites"] = entry["rewrites"] or (hook == "pre_tool_call" and _rewrites_command(function))
            except RecursionError:
                entry["scopes"].append(("unknown", set()))
    result = {}
    for hook, entry in hooks.items():
        kinds = {kind for kind, _names in entry["scopes"]}
        if "unknown" in kinds:
            scope, tools = "unknown", set()
        elif kinds == {"only"}:
            scope, tools = "only", set().union(*(names for _kind, names in entry["scopes"]))
        elif kinds == {"except"} and len(entry["scopes"]) == 1:
            scope, tools = "except", entry["scopes"][0][1]
        else:
            scope, tools = "any", set()
        result[hook] = {"scope": scope, "tools": sorted(tools), "rewrites": entry["rewrites"]}
    if skipped and not result:
        result = {"_unread": {"scope": "unknown", "tools": [], "rewrites": False}}
    memo[key] = result
    return result


def overlap(first: dict | None, later: dict | None) -> tuple[str, list[str]]:
    """Whether two transform callbacks can change the same tool: ("none", []), ("tools", names) or ("any", [])."""
    if not first or not later or "unknown" in (first.get("scope"), later.get("scope")):
        return "unknown", []
    a, b = first.get("scope"), later.get("scope")
    x, y = set(first.get("tools") or ()), set(later.get("tools") or ())
    if a == "only" and b == "only":
        shared = x & y
    elif a == "only" and b == "except":
        shared = x - y
    elif a == "except" and b == "only":
        shared = y - x
    elif a == "only":
        shared = x
    elif b == "only":
        shared = y
    else:
        return "any", []
    return ("tools", sorted(shared)) if shared else ("none", [])


def load_order(items: list[dict]) -> list[dict]:
    """Hermes 0.23.1's load order of enabled plugins (plugins_manifest.resolve_plugin_load_order): waves of
    requires_plugins, alphabetical by manifest name within each; a cycle falls back to alphabetical for all."""
    import graphlib

    by_name = {item["name"]: item for item in items}
    sorter = graphlib.TopologicalSorter({name: {dep for dep in item.get("requires") or () if dep in by_name and dep != name}
                                         for name, item in by_name.items()})
    try:
        sorter.prepare()
        order = []
        while sorter.is_active():
            ready = sorted(sorter.get_ready())
            order.extend(ready)
            sorter.done(*ready)
    except graphlib.CycleError:
        order = sorted(by_name)
    return [by_name[name] for name in order]


def _plugins_of(home: Path, enabled, disabled, bundled: Path | None, *, now: float, budget: dict) -> tuple[list, list]:
    """The plugins a profile has (its own folder, and the bundled ones it enables) and leftover install folders."""
    found, leftovers = [], []
    sources = [("profile", Path(home) / "plugins")]
    if bundled is not None:
        sources.append(("bundled", Path(bundled)))
    for origin, base in sources:
        if origin == "profile" and not _is_dir(base):
            continue
        for entry in _entries(base, PLUGIN_MAX * 3):
            path = Path(entry.path)
            if origin == "profile" and _LEFTOVER.fullmatch(entry.name):
                if not _is_dir(path):
                    continue
                size, newest = _size(path, 2000)
                try:
                    newest = max(newest, os.lstat(path).st_mtime)
                except OSError:
                    pass
                if now - newest > LEFTOVER_DAYS * 86_400:
                    leftovers.append({"folder": entry.name, "bytes": size, "age_days": round((now - newest) / 86_400)})
                continue
            if entry.name.startswith((".", "_")) or not _PLUGIN.fullmatch(entry.name):
                continue
            linked = entry.is_symlink()
            if not (linked and origin == "profile" and path.is_dir()) and not _is_dir(path):
                continue
            meta = manifest(path)
            if meta is None:
                continue
            keys = {entry.name, meta["name"]}
            if keys & set(disabled):
                state = "disabled"
            elif enabled is not None and keys & set(enabled):
                state = "enabled"
            else:
                state = "not_loaded"
            if origin == "bundled" and state != "enabled":
                continue   # bundled plugins the profile does not turn on are part of Hermes, not of the profile
            repo, sha = git_origin(path) if origin == "profile" and not linked else ("", "")
            size, newest = _size(path) if origin == "profile" and not linked else (0, 0.0)
            record = install_record(home, meta["name"], entry.name) if origin == "profile" else {}
            found.append({"folder": entry.name, "name": meta["name"], "version": meta["version"], "kind": meta["kind"],
                          "description": meta["description"], "declared_hooks": meta["provides_hooks"],
                          "requires": meta["requires_plugins"], "origin": origin, "state": state, "repo": repo,
                          "sha": sha, "bytes": size, "changed": newest, "linked": linked,
                          "source": str(record.get("source") or "")[:300], "allow_removed": record.get("allow_removed") is True,
                          "pinned": str(record.get("pinned") or "")[:40],
                          "hooks": code_hooks(path, budget) if state == "enabled" else {}})
            if len(found) >= PLUGIN_MAX:
                break
    return found, leftovers


# ── skills ───────────────────────────────────────────────────────────────────────────────────────────────────────
def _front(text: str) -> tuple[str, str, str]:
    """(name, description, body) of a SKILL.md."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return "", "", text
    stop = next((index for index, line in enumerate(lines[1:80], 1) if line.strip() == "---"), None)
    if stop is None:
        return "", "", text
    name = description = ""
    for line in lines[1:stop]:
        if line.startswith("name:"):
            name = line.split(":", 1)[1].strip().strip("'\"")[:96]
        elif line.startswith("description:"):
            description = line.split(":", 1)[1].strip().strip("'\"")[:400]
    return name, description, "\n".join(lines[stop + 1:])


def _iso(value) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        from datetime import datetime

        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            from datetime import timezone

            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _bundled(base: Path) -> set[str]:
    """The skills Hermes ships and keeps in every profile (`skills/.bundled_manifest`, `name:hash` lines): Hermes'
    own, re-synced on update, so never the owner's to archive, merge or share."""
    text = read_text(base / ".bundled_manifest", 512 * 1024) or ""
    return {line.split(":", 1)[0].strip() for line in text.splitlines() if line.strip()}


def _skill_files(base: Path) -> list[Path]:
    """SKILL.md files under skills/, at most 5 folders deep, hidden folders (.archive, .hub) and links left out."""
    found, seen = [], 0
    stack = [(base, 0)]
    while stack and seen < SKILL_WALK_MAX and len(found) < SKILL_MAX:
        folder, depth = stack.pop()
        for entry in _entries(folder, 5000):
            seen += 1
            if entry.name.startswith("."):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode) and depth < 5 and entry.name not in ("node_modules", "venv", "__pycache__", "dist", "build"):
                stack.append((Path(entry.path), depth + 1))
            elif stat.S_ISREG(info.st_mode) and entry.name == "SKILL.md" and depth > 0:
                found.append(Path(entry.path))
    return sorted(found)


_SKILL_CACHE: dict[str, tuple] = {}
_SKILL_CACHE_MAX = 20_000


def _skill_text(path: Path) -> tuple[str, str, str, str] | None:
    """(name, description, sha, body sha) of a SKILL.md, from the cache while its size and change time are the same."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    key, stamp = os.fspath(path), (info.st_size, info.st_mtime_ns, getattr(info, "st_ino", 0))
    cached = _SKILL_CACHE.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    text = read_text(path, 256 * 1024)
    if text is None:
        _SKILL_CACHE.pop(key, None)
        return None
    name, description, body = _front(text)
    parsed = (name, description, hashlib.sha256(text.encode()).hexdigest()[:16],
              hashlib.sha256(body.strip().encode()).hexdigest()[:16])
    if len(_SKILL_CACHE) >= _SKILL_CACHE_MAX:
        _SKILL_CACHE.clear()   # bounded: a gateway that looked at a huge Hermes once does not keep it all
    _SKILL_CACHE[key] = (stamp, parsed)
    return parsed


def _skills_of(home: Path) -> list[dict]:
    base = Path(home) / "skills"
    if not _is_dir(base):
        return []
    bundled = _bundled(base)
    try:
        usage = json.loads(read_text(base / ".usage.json", 8 * 1024 * 1024) or "{}")
    except ValueError:
        usage = {}
    usage = usage if isinstance(usage, dict) else {}
    found = []
    for path in _skill_files(base):
        parsed = _skill_text(path)
        if parsed is None:
            continue
        name, description, sha, body_sha = parsed
        folder = path.parent.name
        record = usage.get(folder) if isinstance(usage.get(folder), dict) else {}
        found.append({"folder": folder, "path": path.parent.relative_to(base).as_posix(), "name": name or folder,
                      "description": description, "sha": sha, "body_sha": body_sha,
                      "last_used": _iso(record.get("last_used_at")), "created": _iso(record.get("created_at")),
                      "uses": record.get("use_count") if type(record.get("use_count")) is int else 0,
                      "pinned": record.get("pinned") is True, "locked": record.get("locked") is True,
                      "bundled": folder in bundled or (name or folder) in bundled,
                      "state": record.get("state") if isinstance(record.get("state"), str) else "active"})
    return found


# ── the scan ─────────────────────────────────────────────────────────────────────────────────────────────────────
def _own_transform(home: Path, config) -> bool:
    """Whether this plugin's own transform hook returns text in that profile (tool_hints on, lessons not off), read
    as the plugin there would read it: its settings in config.yaml over its settings.json."""
    try:
        from .cli import effective_settings
        from .evolve import options

        opts = options(effective_settings(home, config if isinstance(config, dict) else {}),
                       config if isinstance(config, dict) else {}, strict=False)
        return bool(opts["tool_hints"] and opts["lessons"] != "off")
    except Exception:
        return False


def _created(home: Path) -> float | None:
    """When a profile folder was made, as near as the system tells (Hermes records no creation date): its birth time
    where the file system keeps one (macOS, Windows, BSD), else the oldest change time (ctime) of the folder and of
    what lies directly in it. A copy keeps a file's modification time (`hermes profile create --clone` copies
    config.yaml, .env and SOUL.md) but never its change time, so a profile made minutes ago never looks old. None when
    the folder cannot be read."""
    try:
        info = os.lstat(home)
    except (OSError, ValueError):
        return None
    birth = getattr(info, "st_birthtime", None)
    if isinstance(birth, (int, float)) and birth > 0:
        return float(birth)
    oldest = info.st_ctime
    for entry in _entries(Path(home), 2000):
        try:
            oldest = min(oldest, entry.stat(follow_symlinks=False).st_ctime)
        except OSError:
            continue
    return float(oldest)


def scan_profile(name: str, home: Path, *, now: float, read_config, bundled: Path | None, sessions,
                 budget: dict | None = None) -> dict:
    config = read_config(home)
    enabled, disabled = _plugin_lists(config)
    plugins, leftovers = _plugins_of(home, enabled, disabled, bundled, now=now,
                                     budget=budget if budget is not None else {"left": SOURCE_SCAN_BYTES})
    section = config.get("plugins") if isinstance(config, dict) else None
    entries = section.get("entries") if isinstance(section, dict) else None
    entries = entries if isinstance(entries, dict) else {}
    return {"profile": name, "home": str(home),
            # a profile Hermes never configured has no config.yaml: nothing is proposed or run for it
            "config_read": isinstance(config, dict) and (Path(home) / "config.yaml").is_file(),
            "sessions_7d": sessions(home, since=now - 7 * 86_400) or 0,
            "sessions_30d": sessions(home, since=now - IDLE_PROFILE_DAYS * 86_400) or 0,
            "created": _created(home),
            "plugins": plugins, "leftovers": leftovers, "skills": _skills_of(home),
            "self": has_plugin(home, config),
            "self_installed": _is_plugin_dir(Path(home) / "plugins" / PLUGIN_ID),
            # the plugin's data folder outlives `hermes plugins remove`: the plugin was there and the owner removed it
            "self_data": os.path.lexists(Path(home) / "evolution" / "evolution.sqlite3"),
            "self_disabled": PLUGIN_ID in disabled,
            "self_configured": isinstance(entries, dict) and PLUGIN_ID in entries,
            "own_transform": _own_transform(home, config) if has_plugin(home, config) else False}


def scan(root: Path, *, now: float | None = None, read_config=None, bundled: Path | None = None,
         sessions=None, deadline: float | None = None, only: str | None = None) -> dict:
    """Every profile of the Hermes at `root` (or only the profile `only`): its plugins, leftover installs, skills
    and use. Read only. ScanTimeout past `deadline` (time.monotonic)."""
    from .ecosystem import hermes_source

    now = time.time() if now is None else now
    read_config = read_config or _read_config
    if bundled is None:
        source = hermes_source()
        bundled = source / "plugins" if source is not None else None
    if sessions is None:
        from .status import _sessions as sessions
    rows, budget = [], {"left": SOURCE_SCAN_BYTES}
    for name, home in profiles(root):
        if only is not None and name != only:
            continue
        if deadline is not None and time.monotonic() > deadline:
            raise ScanTimeout(name)
        rows.append(scan_profile(name, home, now=now, read_config=read_config, bundled=bundled, sessions=sessions,
                                 budget=budget))
    return {"at": now, "root": str(root), "profiles": rows, "complete": budget["left"] > 0}


# ── findings ─────────────────────────────────────────────────────────────────────────────────────────────────────
def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.casefold()))


def similar_pairs(word_sets: list[set[str]], bodies: list[str] | None = None,
                  threshold: float | None = None) -> list[tuple[int, int]]:
    """(i, j) with i < j for every two word sets whose Jaccard similarity is at least `threshold` (SIMILAR), leaving
    out pairs with the same body. Each set becomes an int bitmask over the words seen, so a pair costs an AND, an OR
    and a popcount instead of two set operations: the same pairs, several times faster on a few hundred skills."""
    threshold = SIMILAR if threshold is None else threshold
    positions: dict[str, int] = {}
    masks, sizes = [], []
    for words in word_sets:
        mask = 0
        for word in words:
            mask |= 1 << positions.setdefault(word, len(positions))
        masks.append(mask)
        sizes.append(len(words))
    # By size, smallest first: a set this size can only reach the threshold with sets up to size / threshold, so the
    # inner loop stops there instead of looking at every later set.
    order = sorted((index for index in range(len(masks)) if sizes[index]), key=sizes.__getitem__)
    found = []
    for position, left in enumerate(order):
        a, size_a = masks[left], sizes[left]
        widest = size_a / threshold
        for right in order[position + 1:]:
            size_b = sizes[right]
            if size_b > widest:
                break
            shared = (a & masks[right]).bit_count()
            if shared and shared >= threshold * (size_a + size_b - shared) \
                    and not (bodies is not None and bodies[left] == bodies[right]):
                found.append((left, right) if left < right else (right, left))
    found.sort()
    return found



def _finding(kind: str, profile: str, subject: str, *, risk: str, evidence: dict, command: str = "", key: str = "") -> dict:
    return {"code": (f"env:{kind}:{profile}:{subject}" + (f":{key}" if key else ""))[:240], "kind": kind, "risk": risk,
            "subject": f"{profile}/{subject}" if subject else profile, "evidence": {"profile": profile, **evidence},
            "command": command}


def _cmd(profile: str, args: str) -> str:
    return f"hermes -p {profile} {args}"   # always the profile by name: a pinned active profile never changes it


def _q(name: str) -> str:
    import shlex

    return shlex.quote(name)   # a skill folder may hold spaces or shell characters: safe to paste


def _protected(item: dict, notes: dict) -> bool:
    note = notes.get(item["name"]) or notes.get(item["folder"]) or {}
    return item["name"] == PLUGIN_ID or isinstance(note, dict) and note.get("protect") is True


def _newer(candidate: str, current: str) -> bool:
    def parts(text):
        return [int(piece) if piece.isdigit() else 0 for piece in re.split(r"[.\-+]", text.lstrip("v"))[:4]]
    try:
        return parts(candidate) > parts(current)
    except ValueError:
        return False


def profile_findings(row: dict, catalog: dict, known: dict, *, now: float, fleet: dict | None = None,
                     self_sha: str = "") -> list[dict]:
    """The situations of one profile."""
    from .ecosystem import obsolete, removed_reason

    notes = known.get("plugins") or {}
    fleet = fleet or {"seen": [], "skip": []}
    profile, found = row["profile"], []
    on = load_order([item for item in row["plugins"] if item["state"] == "enabled"])
    # On Hermes' kill list: Hermes already refuses to load it (unless it was installed with --allow-removed); the
    # removal is the owner's.
    for item in row["plugins"]:
        if item["origin"] != "profile":
            continue
        reason = removed_reason(item, catalog, home=Path(row["home"]))
        if reason:
            found.append(_finding("plugin_removed", profile, item["name"], risk="high",
                                  evidence={"reason": reason, "state": item["state"], "allowed": item["allow_removed"],
                                            "folder": item["folder"]},
                                  command=_cmd(profile, f"plugins remove {item['name']}")))
    removed_names = {item["subject"].split("/", 1)[1] for item in found}
    for item in row["plugins"]:
        if item["origin"] != "profile" or item["name"] in removed_names:
            continue   # a plugin on the kill list is said once, as such
        note = obsolete(item, known)
        if note:
            found.append(_finding("plugin_obsolete", profile, item["name"],
                                  risk="low" if note.get("state") == "unmaintained" else "medium",
                                  evidence={"state": note.get("state"), "basis": note.get("basis"),
                                            "replacement": note.get("superseded_by") or [], "loaded": item["state"]},
                                  command=_cmd(profile, f"plugins remove {item['name']}")))
    # Transform hooks: Hermes keeps the first result of each call, in its load order; a conflict is two plugins that
    # can change the same tool's result.
    for hook in FIRST_WINS:
        users = []
        for item in on:
            hooks = item["hooks"]
            if hooks is None:
                continue   # not read (the scan's source budget): nothing is concluded
            if hook in hooks or hook in item["declared_hooks"] or "_unread" in hooks:
                if item["name"] == PLUGIN_ID and not row.get("own_transform"):
                    continue   # its transform returns nothing here (tool_hints off)
                users.append(item)
        for index, first in enumerate(users):
            for later in users[index + 1:]:
                verdict, shared = overlap(first["hooks"].get(hook) or first["hooks"].get("_unread"),
                                          later["hooks"].get(hook) or later["hooks"].get("_unread"))
                if verdict == "none":
                    continue   # different tools: both changes apply
                evidence = {"hook": hook, "first": first["name"], "later": later["name"], "tools": shared,
                            "first_scope": (first["hooks"].get(hook) or {}).get("scope", "any"),
                            "first_tools": (first["hooks"].get(hook) or {}).get("tools", []),
                            "later_scope": (later["hooks"].get(hook) or {}).get("scope", "any"),
                            "later_tools": (later["hooks"].get(hook) or {}).get("tools", [])}
                key = f"{hook}:{first['name']}:{later['name']}"
                if verdict == "unknown":
                    found.append(_finding("plugin_transform_unclear", profile, later["name"], key=key, risk="low",
                                          evidence=evidence, command=_cmd(profile, "plugins list")))
                elif PLUGIN_ID in (first["name"], later["name"]):
                    other = later if first["name"] == PLUGIN_ID else first
                    found.append(_finding("plugin_hints_overlap", profile, other["name"], key=key, risk="medium",
                                          evidence={**evidence, "other": other["name"]},
                                          command=_cmd(profile, f"config set plugins.entries.{PLUGIN_ID}.settings.tool_hints false")))
                elif _protected(first, notes) and _protected(later, notes):
                    found.append(_finding("plugin_transform_protected", profile, later["name"], key=key, risk="medium",
                                          evidence=evidence, command=_cmd(profile, "plugins list")))
                else:
                    off = first if _protected(later, notes) else later
                    found.append(_finding("plugin_transform_overlap", profile, off["name"], key=key,
                                          risk="high" if _protected(later, notes) else "medium",
                                          evidence={**evidence, "off": off["name"],
                                                    "protected": _protected(later, notes)},
                                          command=_cmd(profile, f"plugins disable {off['name']}")))
    # Two plugins doing one job: rewriting the terminal's commands, or a role only one should fill.
    rewriters = [item for item in on if item["hooks"] and (item["hooks"].get("pre_tool_call") or {}).get("rewrites")
                 and ((item["hooks"]["pre_tool_call"].get("scope") == "only" and "terminal" in item["hooks"]["pre_tool_call"]["tools"])
                      or item["hooks"]["pre_tool_call"].get("scope") != "only")]
    groups = [("terminal_rewrite", rewriters)]
    roles: dict[str, list] = {}
    for item in on:
        note = notes.get(item["name"]) or notes.get(item["folder"])
        if isinstance(note, dict) and note.get("exclusive") and isinstance(note.get("role"), str) \
                and note["role"] != "terminal_rewrite":
            roles.setdefault(note["role"], []).append(item)
    groups += sorted(roles.items())
    for role, items in groups:
        if len(items) < 2:
            continue
        keep = next((item for item in items if _protected(item, notes)), items[0])
        for item in items:
            if item is keep or _protected(item, notes):
                continue
            found.append(_finding("plugin_duplicate", profile, item["name"], risk="medium",
                                  evidence={"role": role, "keep": keep["name"], "plugins": [entry["name"] for entry in items]},
                                  command=_cmd(profile, f"plugins disable {item['name']}")))
    # Installed and never loaded: disk and confusion for nothing.
    idle = [item for item in row["plugins"] if item["origin"] == "profile" and item["state"] != "enabled"
            and item["name"] != PLUGIN_ID and now - (item["changed"] or now) > IDLE_PLUGIN_DAYS * 86_400]
    if idle:
        found.append(_finding("plugin_unused", profile, "", risk="low",
                              evidence={"plugins": [{"name": item["name"], "state": item["state"],
                                                     "mb": round(item["bytes"] / 2**20, 1)} for item in idle][:30],
                                        "count": len(idle), "mb": round(sum(item["bytes"] for item in idle) / 2**20, 1),
                                        "commands": [_cmd(profile, f"plugins remove {item['name']}") for item in idle][:30]},
                              command=_cmd(profile, f"plugins remove {idle[0]['name']}")))
    for item in row["leftovers"]:
        found.append(_finding("plugin_leftover", profile, item["folder"], risk="low",
                              evidence={"mb": round(item["bytes"] / 2**20, 1), "age_days": item["age_days"]}))
    # This plugin where the profile is used: installed by the Mode auto only where it never was (never found there,
    # not turned off, never configured); anywhere else the owner decided, and it is only said.
    if not row["self"] and row["sessions_7d"] > 0 and row["config_read"]:
        decided = row["self_disabled"] or row["self_installed"] or row["self_configured"] or row.get("self_data", True) \
            or profile in fleet["seen"]
        if decided:
            found.append(_finding("self_off", profile, PLUGIN_ID, risk="low",
                                  evidence={"sessions_7d": row["sessions_7d"], "disabled": row["self_disabled"],
                                            "installed": row["self_installed"]},
                                  command=_cmd(profile, f"plugins enable {PLUGIN_ID}" if row["self_installed"]
                                               else f"plugins install {REPOSITORY} --enable")))
        else:
            # never `--ref`: Hermes pins an install made with one, and every later `plugins update` there fails
            found.append(_finding("self_missing", profile, PLUGIN_ID, risk="low",
                                  evidence={"sessions_7d": row["sessions_7d"]},
                                  command=_cmd(profile, f"plugins install {REPOSITORY} --enable")))
    # unused for 30 days only when it is at least that old: a profile made minutes ago has had no session yet (a row
    # stored by an older version has no date: as before); a date that cannot be read says nothing
    created = row.get("created", 0)
    if row["sessions_30d"] == 0 and profile != "default" and row["config_read"] \
            and created is not None and now - created >= IDLE_PROFILE_DAYS * 86_400:
        found.append(_finding("profile_idle", profile, "", risk="low",
                              evidence={"days": IDLE_PROFILE_DAYS, "plugins": len(on), "skills": len(row["skills"])},
                              command="hermes profile list"))
    # Skills (Hermes' bundled ones left out): unused, the same text twice, near twins.
    skills = [item for item in row["skills"] if item["state"] == "active" and not item.get("bundled")]
    unused = [item for item in skills if not (item["pinned"] or item["locked"])
              and now - (item["last_used"] or item["created"] or now) > UNUSED_SKILL_DAYS * 86_400]
    if unused:
        unused.sort(key=lambda item: item["last_used"] or item["created"] or 0)
        found.append(_finding("skills_unused", profile, "", risk="low",
                              evidence={"count": len(unused), "days": UNUSED_SKILL_DAYS, "total": len(skills),
                                        "skills": [item["folder"] for item in unused][:40]},
                              command=_cmd(profile, f"curator archive {_q(unused[0]['folder'])}")))
    by_text: dict[str, list] = {}
    for item in skills:
        by_text.setdefault(item["body_sha"], []).append(item)
    for group in by_text.values():
        if len(group) > 1:
            group.sort(key=lambda item: (-(item["uses"] or 0), item["folder"]))
            found.append(_finding("skills_duplicate", profile, group[1]["folder"], risk="low",
                                  evidence={"keep": group[0]["folder"], "copies": [item["folder"] for item in group],
                                            "used": bool(group[0]["uses"])},
                                  command=_cmd(profile, f"curator archive {_q(group[1]['folder'])}")))
    ordered = sorted(skills, key=lambda item: item["folder"])[:400]
    pairs = similar_pairs([_words(item["description"] or item["name"]) for item in ordered],
                          [item["body_sha"] for item in ordered])
    pairs = [(ordered[left]["folder"], ordered[right]["folder"]) for left, right in pairs]
    if pairs:
        found.append(_finding("skills_similar", profile, "", risk="low",
                              evidence={"pairs": [list(pair) for pair in pairs[:15]], "count": len(pairs)},
                              command=_cmd(profile, "curator run --consolidate --dry-run")))
    return found


SELF_URL = f"https://github.com/{REPOSITORY}.git"


def _bring_current(profile: str, pinned: bool, sha: str = "") -> str:
    """The command that brings this plugin's copy in `profile` to current code. Hermes keeps a pin when a plugin is
    installed again without --ref (it reinstalls the pinned commit) and `plugins update` refuses a pinned copy, so a
    pin moves with `--ref <commit>`: the lead's own commit when known, else the plugin's own update, which reads the
    repository's latest one."""
    if pinned:
        if _SHA.fullmatch(sha or ""):
            return _cmd(profile, f"plugins install {SELF_URL} --force --ref {sha}")
        return f"hermes evolution update --profiles {profile}"
    return _cmd(profile, f"plugins update {PLUGIN_ID} --force")


def _update_command(name: str, item: dict) -> str:
    if name == PLUGIN_ID and item.get("pinned"):
        return _bring_current(item["profile"], True)   # `plugins update` refuses a pinned install ("is pinned to")
    return _cmd(item["profile"], f"plugins update {name}")


def _self_drift(reading: dict, self_sha: str, *, already=()) -> list[dict]:
    """Profiles whose copy of this plugin is pinned to a commit, or runs another commit than the lead's (the same
    version string can hide different code): one finding, with the reinstall command for each."""
    rows, lead_version = [], ""
    for row in reading.get("profiles") or ():
        for item in row.get("plugins") or ():
            if item.get("origin") != "profile" or item.get("linked") or PLUGIN_ID not in (item.get("name"), item.get("folder")):
                continue
            if self_sha and item.get("sha") == self_sha:
                lead_version = lead_version or str(item.get("version") or "")
            reason = "pinned" if item.get("pinned") else (
                "self_skew" if self_sha and item.get("sha") and item["sha"] != self_sha else "")
            if reason and row["profile"] not in already:
                rows.append({"profile": row["profile"], "installed": item.get("version") or "?", "reason": reason,
                             "pinned": item.get("pinned") or "", "sha": (item.get("sha") or "")[:12]})
    if not rows:
        return []
    reasons = sorted({item["reason"] for item in rows})
    commands = [_bring_current(item["profile"], item["reason"] == "pinned", self_sha) for item in rows]
    return [_finding("plugin_update", "all", PLUGIN_ID, risk="medium" if "pinned" in reasons else "low", key="drift",
                     evidence={"catalog": lead_version or "?", "tier": "lead", "reason": reasons[0] if len(reasons) == 1
                               else "pinned", "reasons": reasons, "lead_sha": self_sha[:12], "profiles": rows[:20],
                               "count": len(rows), "commands": commands[:20]},
                     command=commands[0])]


def findings(reading: dict, catalog: dict, known: dict, *, now: float | None = None, fleet: dict | None = None,
             self_sha: str = "") -> list[dict]:
    """The situations of the playbook found in a scan, most important first."""
    from .ecosystem import lookup

    now = time.time() if now is None else now
    found: list[dict] = []
    copies: dict[str, set[str]] = {}
    updates: dict[str, dict] = {}
    for row in reading.get("profiles") or ():
        found += profile_findings(row, catalog, known, now=now, fleet=fleet, self_sha=self_sha)
        for item in row["skills"]:
            if item["state"] == "active" and not item.get("bundled"):
                copies.setdefault(item["sha"], set()).add(row["profile"])
        for item in row["plugins"]:
            if item["state"] != "enabled" or item["origin"] != "profile":
                continue
            entry = lookup(item, catalog)
            if entry and entry["version"] and item["version"] and entry["version"] != item["version"] \
                    and _newer(entry["version"], item["version"]):
                group = updates.setdefault(item["name"], {"catalog": entry["version"], "tier": entry["tier"], "profiles": []})
                group["profiles"].append({"profile": row["profile"], "installed": item["version"],
                                          **({"pinned": item["pinned"]} if item.get("pinned") else {})})
    for name, group in sorted(updates.items()):
        names = [item["profile"] for item in group["profiles"]]
        commands = [_update_command(name, item) for item in group["profiles"]]
        found.append(_finding("plugin_update", "all", name, risk="low",
                              evidence={**group, "count": len(names), "commands": commands[:20]},
                              command=commands[0]))
    found += _self_drift(reading, self_sha, already={item["profile"] for item in (updates.get(PLUGIN_ID) or {}).get("profiles") or ()})
    shared = sum(1 for where in copies.values() if len(where) >= 3)
    if shared:
        found.append(_finding("skills_shared", "all", "", risk="low",
                              evidence={"skills": shared, "profiles": len(reading.get("profiles") or ())},
                              command="hermes config get skills.external_dirs"))
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(found, key=lambda item: (order.get(item["risk"], 3), item["kind"], item["subject"]))


def summary(reading: dict) -> dict:
    """Counts for the status page and the menu."""
    rows = reading.get("profiles") or ()
    return {"profiles": len(rows), "used_7d": sum(1 for row in rows if row["sessions_7d"] > 0),
            "with_plugin": sum(1 for row in rows if row["self"]),
            "plugins_on": sum(1 for row in rows for item in row["plugins"] if item["state"] == "enabled"),
            "plugins_idle": sum(1 for row in rows for item in row["plugins"] if item["origin"] == "profile"
                                and item["state"] != "enabled"),
            "skills": sum(len(row["skills"]) for row in rows)}


# ── the pass that looks after the whole Hermes ───────────────────────────────────────────────────────────────────
SCAN_EVERY_SECONDS = 6 * 3600


def _compact(row: dict) -> dict:
    return {"profile": row["profile"], "sessions_7d": row["sessions_7d"], "self": row["self"],
            "plugins_on": sum(1 for item in row["plugins"] if item["state"] == "enabled"),
            "plugins_idle": sum(1 for item in row["plugins"] if item["origin"] == "profile" and item["state"] != "enabled"),
            "skills": len(row["skills"]), "leftovers": len(row["leftovers"])}


def stored(conn) -> dict | None:
    row = conn.execute("SELECT value FROM cursor WHERE key='environment'").fetchone()
    try:
        found = json.loads(row[0]) if row else None
    except ValueError:
        return None
    if not isinstance(found, dict):
        return None
    try:
        found["at"] = float(found.get("at") or 0)
        if not 0 <= found["at"] < 4_102_444_800:   # before 2100
            found["at"] = 0.0
    except (TypeError, ValueError, OverflowError):
        found["at"] = 0.0
    return found


def self_sha(home: Path) -> str:
    """The commit the lead's own copy of the plugin runs, so a profile it installs runs the same code. Only from a
    checkout of the published repository: a fork's or a local commit is not on GitHub, and installing it would fail."""
    from .ecosystem import normalize_repo

    repo, sha = git_origin(Path(home) / "plugins" / PLUGIN_ID)
    return sha if normalize_repo(repo) == normalize_repo(f"https://github.com/{REPOSITORY}") else ""


def refresh(home: Path, *, now: float, force: bool = False, read_config=None, deadline: float | None = None) -> dict | None:
    """The whole Hermes, read by the lead profile at most every SCAN_EVERY_SECONDS (or now with `force`), kept in its
    database with Hermes' catalog and the knowledge base. None in any other profile: one profile does it for all."""
    from .admin import profile_name
    from .ecosystem import knowledge, load_catalog, sync
    from .experience import experience

    root = root_of(home)
    leader = lead(root, read_config=read_config, now=now)
    if leader is None or profile_name(home) != leader:
        return None
    with experience(home) as conn:
        cached = stored(conn)
    if not force and cached and 0 <= now - float(cached.get("at") or 0) < SCAN_EVERY_SECONDS:
        return cached
    if deadline is not None and deadline - time.monotonic() < 60:
        return cached
    try:
        reading = scan(root, now=now, read_config=read_config,
                       deadline=None if deadline is None else deadline - 30)
    except ScanTimeout:
        return cached
    seen = [row["profile"] for row in reading["profiles"]
            if row["self"] or row["self_installed"] or row["self_disabled"] or row["self_configured"]]
    remember(root, seen=seen)
    catalog = load_catalog([Path(row["home"]) for row in reading["profiles"]])
    known = knowledge()
    found = findings(reading, catalog, known, now=now, fleet=state(root), self_sha=self_sha(home))
    doctor_seen = _doctor(home, reading, found, now=now, deadline=deadline, force=force)
    tiers: dict[str, int] = {}
    for entry in catalog["entries"].values():
        tiers[entry["tier"]] = tiers.get(entry["tier"], 0) + 1
    keep = {"at": now, "lead": leader, "summary": summary(reading), "profiles": [_compact(row) for row in reading["profiles"]],
            "catalog": {"source": catalog["source"], "generated_at": catalog["generated_at"],
                        "plugins": len(catalog["entries"]), "tiers": tiers, "removed": len(catalog["removed"])},
            "complete": reading.get("complete", True), "findings": found, "doctor": doctor_seen}
    with experience(home) as conn:
        sync(conn, catalog, known)
        conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('environment', ?)", (json.dumps(keep),))
    return keep


def _doctor(home: Path, reading: dict, found: list, *, now: float, deadline: float | None, force: bool) -> dict:
    """Hermes' own doctor checks of the profiles in use (doctor.py), once a day each: their problems join `found`,
    and their counts per profile are returned."""
    from . import doctor

    rows = [row for row in reading["profiles"] if row["config_read"]]
    try:
        runs = doctor.collect(home, rows, now=now, deadline=deadline, force=force)
    except Exception:   # the doctor is an addition: the look at the plugins and skills stands without it
        return {}
    homes = {row["profile"]: Path(row["home"]) for row in rows}
    seen = {}
    machine: dict[tuple, dict] = {}   # a problem of the whole machine is said once, with every profile that saw it
    leader = lead(root_of(home), now=now)
    for profile, item in sorted(doctor.current(runs, now=now).items(), key=lambda pair: (pair[0] != leader, pair[0])):
        result = item.get("result") if isinstance(item, dict) else None
        if not isinstance(result, dict) or profile not in homes:
            continue
        for finding in doctor.findings(profile, homes[profile], result):
            if not finding["evidence"].get("host"):
                found.append(finding)
                continue
            shared = (finding["kind"], finding["subject"].split("/", 1)[1])
            if shared in machine:
                machine[shared]["evidence"].setdefault("profiles", [machine[shared]["evidence"]["profile"]]).append(profile)
            else:
                machine[shared] = finding
                found.append(finding)
        seen[profile] = {"at": item.get("at"), **doctor.summary(result)}
    order = {"high": 0, "medium": 1, "low": 2}
    found.sort(key=lambda item: (order.get(item["risk"], 3), item["kind"], item["subject"]))
    return seen


def forget(home: Path, code: str) -> None:
    """Take a finding out of the stored look (a change found stale on disk), so the next pass does not reopen it
    from a cached look before the next real one."""
    from .experience import experience

    parts = str(code).split(":")
    with experience(home) as conn:
        if len(parts) >= 3 and parts[1].startswith("doctor_"):
            from .doctor import forget as forget_run

            forget_run(conn, parts[2])   # the stored checks of that profile are stale too: run again at the next look
        found = stored(conn)
        if not found or not isinstance(found.get("findings"), list):
            return
        found["findings"] = [item for item in found["findings"] if item.get("code") != code]
        conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('environment', ?)", (json.dumps(found),))


def verify(home: Path, proposal: dict, *, now: float | None = None, read_config=None) -> dict | None:
    """The situation behind a proposal, read again from disk now: the fresh finding with the same code in the
    target profile, or None when it is not there any more. Nothing stored (the proposal, the cached scan, another
    profile's catalog copy) is trusted for a change."""
    from .ecosystem import knowledge

    now = time.time() if now is None else now
    if str(proposal.get("kind") or "").startswith("doctor_"):
        from .doctor import verify as doctor_verify

        return doctor_verify(home, proposal)
    subject = str(proposal.get("subject") or "")
    profile = subject.split("/", 1)[0] if "/" in subject else subject
    if not _PROFILE.fullmatch(profile or ""):
        return None
    root = root_of(home)
    reading = scan(root, now=now, read_config=read_config, only=profile)
    if not reading["profiles"]:
        return None
    row = reading["profiles"][0]
    # the kill list is read per profile by removed_reason; the catalog's entries matter only for update advice
    fresh = profile_findings(row, {"entries": {}, "removed": []}, knowledge(), now=now, fleet=state(root),
                             self_sha=self_sha(home))
    return next((item for item in fresh if item["code"] == proposal.get("code")), None)



def profile_mode(home: Path, *, read_config=None) -> str | None:
    """The mode the plugin in that profile uses (its settings in config.yaml over its settings.json), None when the
    plugin is not on there."""
    read_config = read_config or _read_config
    config = read_config(home)
    if not has_plugin(home, config):
        return None
    try:
        from .cli import effective_settings
        from .evolve import options

        return options(effective_settings(Path(home), config), config, strict=False)["mode"]
    except Exception:
        return "manual"


def publish(home: Path, found: dict | None, *, now: float) -> None:
    """Turn a look at every profile into the lead's proposals now (the learning pass does it too): new situations open,
    situations no longer there expire. Used after a look asked for by hand."""
    from . import proposals
    from .experience import experience

    items = list((found or {}).get("findings") or ())
    fresh = {item["code"] for item in items}
    with experience(home) as conn:
        for item in items:
            proposals._upsert(conn, set(), item["code"], item["kind"], item["risk"], item["subject"], item["evidence"],
                              item["command"], now)
        gone = [row[0] for row in conn.execute("SELECT code FROM proposal WHERE status='open' AND code LIKE 'env:%'")
                if row[0] not in fresh]
        conn.executemany("UPDATE proposal SET status='expired', updated=? WHERE code=? AND status='open'",
                         [(now, code) for code in gone])


def still_open(conn, found: dict | None) -> list[dict]:
    """The findings of a look that are still to do: not applied, rejected, dismissed or expired since."""
    closed = {row[0] for row in conn.execute("SELECT code FROM proposal WHERE code LIKE 'env:%' AND status NOT IN "
                                              "('open', 'approved')")}
    return [item for item in (found or {}).get("findings") or () if item.get("code") not in closed]
