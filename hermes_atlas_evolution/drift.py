"""Drift after a Hermes update (0.14.0): what an update leaves behind that Hermes does not say in one place.

A `hermes update` replaces Hermes' files, keeps local changes in a git stash and tries to put them back, rebuilds what
it builds and restarts the services. What it cannot see is what lives around it: a file loaded in Hermes' place (an
overlay), local changes it parked because they no longer apply, plugins its new dependency set turned off, a profile
whose configuration is behind the new schema. Each of those stays silent until something breaks. This module reads,
and never changes anything:

- Hermes' own update receipts (`logs/update_receipts/*.json`): from which version to which, whether the local changes
  came back ("restored") or were parked in a stash ("parked: … restore hit conflicts"), the plugins the update turned
  off, the services that did not come back;
- the modules a running gateway loaded in place of Hermes' own files (`runtime_overlays`, taken inside the gateway and
  kept in `<root>/evolution/runtime-overlays.json`), and for each one whether Hermes' file changed after the overlay
  was written (the overlay then hides the new upstream code: its fixes stay off) or the overlay changed after the
  gateway loaded it (a restart is needed to use it);
- Hermes' checkout: files changed locally (`git status`), and every stash an update left, with how much of what it
  holds is back in place now (each added line looked up in the file as it is): back, partly back, or not there;
- each profile's configuration schema stamp (`_config_version`) against the one this Hermes writes.

Everything is bounded (time, files, bytes) and read-only: git runs `status`, `stash list` and `stash show`, never a
command that writes. Nothing here leaves the machine.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

CHECK_EVERY_SECONDS = 6 * 3600
SNAPSHOT_FILE = "runtime-overlays.json"
SNAPSHOT_EVERY_SECONDS = 3600
RECEIPTS_KEPT = 6
STASHES_READ = 8
STASH_FILES = 40
STASH_OLD_DAYS = 7          # Hermes' own update warns about autostash entries older than this
BACK_SHARE = 0.9            # this share of a stash's added lines found in the file: its change is back in place
PARTLY_SHARE = 0.3
MAX_STASH_PATCH = 4 * 2**20
FILE_READ_LIMIT = 4 * 2**20
GIT_SECONDS = 15
# Hermes' own top-level packages (a module of one of them loaded from elsewhere is an overlay). A checkout's own
# top-level modules (hermes_state.py, run_agent.py...) are added when the install is a checkout.
HERMES_PACKAGES = ("acp_adapter", "agent", "cron", "gateway", "hermes_agent", "hermes_cli", "hermes_platform",
                   "persistence", "plugins", "pm", "providers", "session_rules", "tools", "tui_gateway", "utils")
LOADED_AT = time.time()     # about when this process loaded the plugin (a gateway loads it while it starts)
_AUTOSTASH = re.compile(r"hermes-update-autostash-(\d{8})-(\d{6})")
_DISABLED = re.compile(r"Disabled plugin '([A-Za-z0-9][A-Za-z0-9._-]{0,95})' in (/[^:\n]{1,400}?):")
_PARKED = re.compile(r"parked:\s*([0-9a-f]{7,64})")
_RESTORED = re.compile(r"restored:\s*([0-9a-f]{7,64})")
_STAMP = re.compile(rb"^_config_version:\s*['\"]?(\d{1,4})['\"]?\s*$", re.M)
_LATEST = re.compile(r"[\"']_config_version[\"']\s*:\s*(\d{1,4})")
_VERSION = re.compile(r"^\d+(?:\.\d+){1,3}(?:[-+.][0-9A-Za-z.]{1,40})?$")


def _q(count, one: str, many: str) -> str:
    """A count with its word in the right number: 1 vez, 2 vezes."""
    return f"{count} {one if count == 1 else many}"


# ── where Hermes is ──────────────────────────────────────────────────────────────────────────────────────────────
def install_root() -> Path | None:
    """The folder of Hermes' code (where `hermes_cli/` and `gateway/` live), when Hermes is loaded in this process;
    None outside Hermes (the standalone runner)."""
    for name in ("hermes_constants", "hermes_cli"):
        module = sys.modules.get(name)
        location = getattr(module, "__file__", None) if module is not None else None
        if not isinstance(location, str):
            continue
        path = Path(location).resolve().parent
        if name == "hermes_cli":
            path = path.parent
        if (path / "hermes_cli").is_dir() and (path / "gateway").is_dir():
            return path
    return None


def _top_level_names(install: Path) -> frozenset:
    names = {name for name in HERMES_PACKAGES if (install / name / "__init__.py").is_file()}
    if (install / ".git").exists() or (install / "pyproject.toml").is_file():   # a checkout: its own modules too
        try:
            names |= {entry.name[:-3] for entry in os.scandir(install)
                      if entry.name.endswith(".py") and entry.is_file() and entry.name != "setup.py"}
        except OSError:
            pass
    return frozenset(names)


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.relative_to(folder)
        return True
    except ValueError:
        return False


def _digest(path: Path) -> str | None:
    try:
        with open(path, "rb") as stream:
            data = stream.read(FILE_READ_LIMIT + 1)
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()[:16] if len(data) <= FILE_READ_LIMIT else None


def _mtime(path: Path) -> float | None:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


# ── overlays, read inside the process that loaded them ──────────────────────────────────────────────────────────
def runtime_overlays(install: Path | None = None, *, modules=None, now: float | None = None) -> dict | None:
    """The modules of Hermes' own packages this process loaded from outside Hermes' folder (an overlay put in
    Hermes' place), and Hermes' packages whose search path has folders outside it. None outside Hermes."""
    install = install or install_root()
    if install is None:
        return None
    install = Path(install).resolve()
    names = _top_level_names(install)
    found, extended = [], []
    for name, module in list((sys.modules if modules is None else modules).items())[:50_000]:
        top = name.split(".", 1)[0]
        if top not in names or module is None:
            continue
        paths = getattr(module, "__path__", None)
        if paths is not None and "." not in name:
            try:
                outside = [str(Path(item).resolve()) for item in list(paths)[:16]
                           if isinstance(item, str) and not _inside(Path(item).resolve(), install)]
            except (OSError, RuntimeError, TypeError):
                outside = []
            if outside:
                extended.append({"package": name, "folders": outside[:8]})
        location = getattr(module, "__file__", None)
        if not isinstance(location, str) or not location.endswith(".py"):
            continue
        try:
            path = Path(location).resolve()
        except (OSError, RuntimeError):
            continue
        if _inside(path, install):
            continue
        relative = Path(*name.split("."))
        upstream = install / (relative / "__init__.py" if path.name == "__init__.py" else relative.with_suffix(".py"))
        found.append({"module": name, "overlay": str(path), "upstream": str(upstream),
                      "upstream_exists": upstream.is_file(), "overlay_mtime": _mtime(path),
                      "overlay_sha": _digest(path), "upstream_mtime": _mtime(upstream),
                      "upstream_sha": _digest(upstream) if upstream.is_file() else None})
        if len(found) >= 64:
            break
    found.sort(key=lambda item: item["module"])
    return {"at": time.time() if now is None else now, "pid": os.getpid(), "loaded_at": LOADED_AT,
            "install": str(install), "overlays": found, "paths": sorted(extended, key=lambda item: item["package"])}


def _snapshot_path(root: Path) -> Path:
    return Path(root) / "evolution" / SNAPSHOT_FILE


def read_snapshot(root: Path) -> dict | None:
    try:
        with open(_snapshot_path(root), "rb") as stream:
            data = json.loads(stream.read(1_000_000) or b"{}")
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("overlays"), list) else None


def write_snapshot(root: Path, *, now: float | None = None, force: bool = False) -> dict | None:
    """Keep what this gateway loaded in Hermes' place, for `status`, the panel and the learning pass (they run in other
    processes). Written by the gateway only, at most every SNAPSHOT_EVERY_SECONDS (or when its pid changed). The
    previous snapshot's Hermes file hashes are kept, so a file Hermes replaced under an overlay is told apart."""
    now = time.time() if now is None else now
    previous = read_snapshot(root)
    if not force and previous and previous.get("pid") == os.getpid() and \
            0 <= now - float(previous.get("at") or 0) < SNAPSHOT_EVERY_SECONDS:
        return previous
    taken = runtime_overlays(now=now)
    if taken is None:
        return None
    taken["uid"] = os.geteuid() if hasattr(os, "geteuid") else None
    if previous and previous.get("pid") == os.getpid() and isinstance(previous.get("folders"), dict):
        taken["folders"] = previous["folders"]   # as the gateway found them at its start, not as they are now
    else:
        taken["folders"] = folder_listing(taken)
    earlier = {item.get("module"): item for item in (previous or {}).get("overlays") or () if isinstance(item, dict)}
    for item in taken["overlays"]:   # what Hermes' file was when this overlay was first seen as it is now
        before = earlier.get(item["module"])
        if before and before.get("overlay_sha") == item["overlay_sha"]:
            item["first_upstream_sha"] = before.get("first_upstream_sha") or before.get("upstream_sha")
            item["first_seen"] = before.get("first_seen") or before.get("at") or previous.get("at")
        else:
            item["first_upstream_sha"], item["first_seen"] = item["upstream_sha"], now
    folder = _snapshot_path(root).parent
    try:
        folder.mkdir(mode=0o700, exist_ok=True)
        temporary = folder / f".{SNAPSHOT_FILE}.{os.getpid()}.tmp"
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(taken, stream)
        os.replace(temporary, _snapshot_path(root))
    except OSError:
        return taken
    return taken


def folder_listing(snapshot: dict) -> dict:
    """{folder: [file names]} of the folders the overlays of a snapshot live in (at most 8 folders, 200 names each)."""
    folders: dict = {}
    for item in snapshot.get("overlays") or ():
        folder = str(Path(str(item.get("overlay") or "")).parent)
        if folder in folders or len(folders) >= 8 or not folder or folder == ".":
            continue
        try:
            folders[folder] = sorted(entry.name for entry in os.scandir(folder) if entry.name != "__pycache__")[:200]
        except OSError:
            continue
    return folders


def folders_now(snapshot: dict | None) -> list[dict]:
    """Each overlay folder of a gateway's snapshot as it is now: names that came or went since the gateway loaded it
    (a launcher that checks the folder's contents may refuse the next start), and files that someone else could change
    (written by group or others, owned by another user, a link, or with another name: the gateway runs that code)."""
    import stat

    if not isinstance(snapshot, dict):
        return []
    uid = snapshot.get("uid")
    found = []
    for folder, names in sorted((snapshot.get("folders") or {}).items()):
        try:
            entries = {entry.name: entry for entry in os.scandir(folder) if entry.name != "__pycache__"}
        except OSError:
            found.append({"folder": folder, "missing": True})
            continue
        unsafe = []
        for name, entry in sorted(entries.items())[:200]:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            reasons = []
            if stat.S_ISLNK(info.st_mode):
                reasons.append("link")
            elif not stat.S_ISREG(info.st_mode):
                reasons.append("not_file")
            if info.st_mode & 0o022:
                reasons.append("writable")
            if type(uid) is int and info.st_uid != uid:
                reasons.append("owner")
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                reasons.append("hardlink")
            if reasons:
                unsafe.append({"name": name, "reasons": reasons})
        added = sorted(set(entries) - set(names or ()))
        removed = sorted(set(names or ()) - set(entries))
        if added or removed or unsafe:
            found.append({"folder": folder, "added": added[:20], "removed": removed[:20], "unsafe": unsafe[:20]})
    return found


# ── a builder's pin of Hermes' file (0.14.0) ────────────────────────────────────────────────────────────────────
# An overlay is often built from Hermes' own file by a script that pins that file's sha256 and refuses another one
# (`EXPECTED = '…'`). After an update, Hermes' file changes, the pin no longer matches, and the overlay the script built
# keeps running the old code until someone rebuilds it. `overlay_pins` names such scripts; each is read (never run):
# its 64-hex constants are the pins, and the existing files inside Hermes' folder it names are what they pin.
_HEX = re.compile(r"\b[0-9a-f]{64}\b")
_ABSOLUTE = re.compile(r"(?<![\w.])(/[A-Za-z0-9_.+@-]+(?:/[A-Za-z0-9_.+@-]+)+)")
PINS_KEPT = 8


def _full_digest(path: Path) -> str | None:
    try:
        with open(path, "rb") as stream:
            data = stream.read(FILE_READ_LIMIT + 1)
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest() if len(data) <= FILE_READ_LIMIT else None


def _builder_constants(text: str, python: bool) -> tuple[set, list]:
    """(64-hex pins, absolute paths) a builder names. In Python, only string constants (a comment with an older hash is
    not a pin); elsewhere, every line that is not a comment."""
    strings: list[str] = []
    if python:
        import ast

        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            tree = None
        if tree is not None:
            strings = [node.value for node in ast.walk(tree)
                       if isinstance(node, ast.Constant) and isinstance(node.value, str)][:5000]
    if not strings:
        strings = [line for line in text.splitlines()[:20_000] if not line.lstrip().startswith(("#", "//"))]
    pins = {match for value in strings for match in _HEX.findall(value) if len(value.strip()) == 64 or not python}
    paths = list(dict.fromkeys(match for value in strings for match in _ABSOLUTE.findall(value)))[:200]
    return pins, paths


def pin_check(builder: Path, *, install: Path | None, guide: str | None = None) -> dict:
    """Whether the files of Hermes a builder pins still have the pinned sha256: `ok`, `stale` (Hermes' file changed:
    rebuild the overlay, and the pin), `unpinned` (no pin or no Hermes file found in it), `unreadable`, or `no_hermes`
    (Hermes' folder is not known here). A guide next to the builder (README.md) is named for the rebuild."""
    from .environment import credential_file, read_text

    builder = Path(builder)
    result = {"builder": str(builder), "files": [], "state": "unreadable"}
    if guide:   # named by the owner (`builder|guide`): said as it is, never read
        result["guide"] = guide
    elif (builder.parent / "README.md").is_file():
        result["guide"] = str(builder.parent / "README.md")
    if credential_file(builder.name):
        return result
    text = read_text(builder, 1_000_000)
    if text is None:
        return result
    if install is None:
        result["state"] = "no_hermes"
        return result
    pins, paths = _builder_constants(text, builder.suffix == ".py")
    root = Path(install).resolve()
    for value in paths:
        try:
            path = Path(value).resolve()
        except (OSError, RuntimeError):
            continue
        if not _inside(path, root) or credential_file(path.name) or not path.is_file():
            continue
        digest = _full_digest(path)
        result["files"].append({"path": str(path), "sha256": digest, "pinned": digest in pins})
        if len(result["files"]) >= 8:
            break
    if not pins or not result["files"]:
        result["state"] = "unpinned"
    else:
        result["state"] = "ok" if all(item["pinned"] for item in result["files"]) else "stale"
    result["pins"] = sorted(pin[:12] for pin in pins)[:8]
    return result


def assess(snapshot: dict | None, *, now: float | None = None) -> list[dict]:
    """Each overlay of a gateway's snapshot as it stands now on disk: `stale` (Hermes' file changed after the overlay
    was written, or since it was first seen: the overlay hides the newer upstream code), `reload` (the overlay changed
    after the gateway loaded it: a restart uses it), `added` (a module Hermes does not have), or `ok`."""
    if not isinstance(snapshot, dict):
        return []
    loaded = float(snapshot.get("loaded_at") or 0)
    result = []
    for item in snapshot.get("overlays") or ():
        if not isinstance(item, dict) or not isinstance(item.get("overlay"), str):
            continue
        overlay, upstream = Path(item["overlay"]), Path(str(item.get("upstream") or ""))
        overlay_mtime, upstream_mtime = _mtime(overlay), _mtime(upstream) if item.get("upstream") else None
        upstream_sha = _digest(upstream) if upstream_mtime is not None else None
        state = "ok"
        if not item.get("upstream_exists") and upstream_mtime is None:
            state = "added"
        elif overlay_mtime is None:
            state = "gone"
        elif upstream_mtime is not None and upstream_mtime > overlay_mtime + 1:
            state = "stale"
        elif item.get("first_upstream_sha") and upstream_sha and upstream_sha != item["first_upstream_sha"] \
                and _digest(overlay) == item.get("overlay_sha"):
            state = "stale"
        if state in ("ok", "stale") and overlay_mtime is not None and loaded and overlay_mtime > loaded + 1:
            state = "reload"   # rebuilt after the gateway loaded it: what runs is the copy from before
        result.append({"module": item.get("module"), "overlay": str(overlay), "upstream": str(upstream),
                       "state": state, "overlay_mtime": overlay_mtime, "upstream_mtime": upstream_mtime})
    return result


# ── Hermes' update receipts ─────────────────────────────────────────────────────────────────────────────────────
def _iso(value) -> float | None:
    if not isinstance(value, str):
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _side(value) -> dict:
    value = value if isinstance(value, dict) else {}
    version = value.get("version")
    sha = value.get("sha")
    return {"version": version if isinstance(version, str) and _VERSION.fullmatch(version) else None,
            "sha": sha[:12] if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{7,64}", sha) else None}


def _profile_of(root: Path, home: str) -> str | None:
    try:
        path = Path(home).resolve()
    except (OSError, RuntimeError):
        return None
    if path == Path(root).resolve():
        return "default"
    if path.parent == (Path(root) / "profiles").resolve():
        return path.name
    return None


def read_receipt(path: Path, root: Path) -> dict | None:
    """One receipt in plain fields: when, from and to which version, outcome, what happened to the local changes, the
    plugins it turned off (by profile), the services that did not come back. Only fixed fields are kept."""
    try:
        with open(path, "rb") as stream:
            data = json.loads(stream.read(2_000_000))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") not in (1, 2):
        return None
    steps = {str(step.get("name")): step for step in data.get("steps") or () if isinstance(step, dict)}
    local = steps.get("local_changes_stash") or {}
    detail = str(local.get("detail") or "")
    parked, restored = _PARKED.search(detail), _RESTORED.search(detail)
    disabled = []
    for warning in data.get("pm_warnings") or ():
        message = warning.get("message") if isinstance(warning, dict) else warning
        match = _DISABLED.search(str(message or ""))
        if match:
            profile = _profile_of(root, match.group(2))
            if profile:
                disabled.append({"plugin": match.group(1), "profile": profile})
    restart = data.get("gateway_restart") if isinstance(data.get("gateway_restart"), dict) else {}
    failed = [str(item)[:80] for item in restart.get("failed_units") or () if item]
    backup = steps.get("pre_update_backup") or {}
    outcome = str(data.get("outcome") or "")[:20]
    return {"started": _iso(data.get("started_at")), "finished": _iso(data.get("finished_at")),
            "outcome": outcome if re.fullmatch(r"[a-z_]{1,20}", outcome) else "unknown",
            "from": _side(data.get("pre_update")), "to": _side(data.get("post_update")),
            "local_changes": "parked" if parked else "restored" if restored else ("none" if not local else "other"),
            "stash": (parked or restored).group(1)[:12] if (parked or restored) else None,
            "plugins_disabled": disabled[:40], "failed_units": failed[:10],
            "restart_incomplete": restart.get("incomplete") is True, "backup_ok": backup.get("ok") is not False}


def receipts(root: Path, *, limit: int = RECEIPTS_KEPT) -> list[dict]:
    """The latest update receipts, newest first."""
    folder = Path(root) / "logs" / "update_receipts"
    try:
        names = sorted((entry.name for entry in os.scandir(folder)
                        if entry.name.startswith("update_") and entry.name.endswith(".json") and entry.is_file()),
                       reverse=True)[:limit]
    except OSError:
        return []
    found = []
    for name in names:
        item = read_receipt(folder / name, root)
        if item is not None:
            found.append(item)
    found.sort(key=lambda item: item.get("started") or 0, reverse=True)
    return found


# ── Hermes' checkout: local changes and the stashes updates left ─────────────────────────────────────────────────
def _git(host, install: Path, *args: str, timeout: int = GIT_SECONDS) -> tuple[int, str]:
    # --no-optional-locks: `git status` would otherwise refresh the index of Hermes' checkout (a write); read only
    code, out, _err = host.run(["git", "--no-optional-locks", "-C", str(install), "-c", "core.quotepath=off",
                                "--no-pager", *args], timeout=timeout)
    return code, out


def local_changes(install: Path, host) -> dict | None:
    """Files of Hermes' checkout that differ from the commit it runs (modified, deleted, added). None when the install
    is not a git checkout or git cannot read it."""
    if not (Path(install) / ".git").exists():
        return None
    code, out = _git(host, install, "status", "--porcelain=v1", "-z", "--untracked-files=normal")
    if code != 0:
        return None
    modified, deleted, added = [], [], []
    entries = out.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        state, name = entry[:2], entry[3:]
        if state[0] in "RC":   # a rename: its old name follows
            index += 1
        if state == "??":
            added.append(name)
        elif "D" in state:
            deleted.append(name)
        else:
            modified.append(name)
    return {"modified": modified[:60], "deleted": deleted[:60], "added": added[:60],
            "count": len(modified) + len(deleted) + len(added)}


def _stash_patch(text: str) -> dict[str, list[str]]:
    files: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("diff --git "):
            current = line.split(" b/", 1)[1] if " b/" in line else None
            if current is not None and len(files) < STASH_FILES:
                files.setdefault(current, [])
            elif current is not None and current not in files:
                current = None
        elif current is not None and line.startswith("+") and not line.startswith("+++"):
            added = line[1:].strip()
            if len(added) >= 4:
                files[current].append(added)
    return files


def _back(install: Path, name: str, added: list[str]) -> str:
    if not added:
        return "removal"
    try:
        with open(Path(install) / name, "rb") as stream:
            data = stream.read(FILE_READ_LIMIT)
    except OSError:
        return "gone"
    lines = {line.strip() for line in data.decode("utf-8", "replace").splitlines()}
    share = sum(1 for line in added if line in lines) / len(added)
    return "back" if share >= BACK_SHARE else "partly" if share >= PARTLY_SHARE else "missing"


def stashes(install: Path, host, *, now: float | None = None) -> list[dict] | None:
    """Every stash a Hermes update left (`hermes-update-autostash-<date>-<time>`), newest first, with how much of what
    it holds is in Hermes' files now: per file `back`, `partly`, `missing` (its change is not there), `gone` (the
    file is not there) or `removal` (it only removed lines). None when this is not a git checkout."""
    if not (Path(install) / ".git").exists():
        return None
    now = time.time() if now is None else now
    code, out = _git(host, install, "stash", "list", "--format=%gd%x1f%gs%x1f%ct")
    if code != 0:
        return None
    found = []
    for line in out.splitlines()[:50]:
        parts = line.split("\x1f")
        if len(parts) != 3 or not re.fullmatch(r"stash@\{\d{1,3}\}", parts[0]):
            continue
        match = _AUTOSTASH.search(parts[1])
        try:
            stamp = float(parts[2])
        except ValueError:
            stamp = None
        found.append({"ref": parts[0], "update": bool(match), "at": stamp,
                      "label": match.group(0) if match else None})
    for item in [entry for entry in found if entry["update"]][:STASHES_READ]:
        code, out = _git(host, install, "stash", "show", "-p", "--no-color", "--no-ext-diff", item["ref"])
        if code != 0:
            continue
        files = _stash_patch(out[:MAX_STASH_PATCH])
        states = {name: _back(install, name, added) for name, added in files.items()}
        item["files"] = len(states)
        item["back"] = sorted(name for name, state in states.items() if state in ("back", "removal"))
        item["partly"] = sorted(name for name, state in states.items() if state == "partly")
        item["missing"] = sorted(name for name, state in states.items() if state in ("missing", "gone"))
        item["old"] = item["at"] is not None and now - item["at"] > STASH_OLD_DAYS * 86_400
    return found


# ── configuration schema of every profile ───────────────────────────────────────────────────────────────────────
def latest_config_version(install: Path | None) -> int | None:
    """The `_config_version` this Hermes writes (hermes_cli/config_defaults.py), read as text."""
    module = sys.modules.get("hermes_cli.config_defaults")
    defaults = getattr(module, "DEFAULT_CONFIG", None) if module is not None else None
    if isinstance(defaults, dict) and type(defaults.get("_config_version")) is int:
        return defaults["_config_version"]
    if install is None:
        return None
    try:
        with open(Path(install) / "hermes_cli" / "config_defaults.py", encoding="utf-8", errors="replace") as stream:
            match = _LATEST.search(stream.read(FILE_READ_LIMIT))
    except OSError:
        return None
    return int(match.group(1)) if match else None


def config_versions(root: Path, latest: int | None) -> list[dict]:
    """Each profile whose config.yaml is stamped with an older schema than this Hermes writes (or has no stamp)."""
    from .environment import profiles, read_bytes

    behind = []
    if latest is None:
        return behind
    for name, home in profiles(Path(root)):
        body = read_bytes(Path(home) / "config.yaml", 8_000_000, part="head")
        if body is None:
            continue
        match = _STAMP.search(body)
        stamp = int(match.group(1)) if match else None
        if stamp is None or stamp < latest:
            behind.append({"profile": name, "stamp": stamp, "latest": latest})
    return behind


def plugins_still_off(root: Path, update: dict | None) -> list[dict]:
    """The plugins the last update turned off that are still off in their profile now."""
    from .environment import _plugin_lists, _read_config, profiles

    if not update or not update.get("plugins_disabled"):
        return []
    homes = dict(profiles(Path(root)))
    still = []
    for item in update["plugins_disabled"]:
        home = homes.get(item["profile"])
        if home is None:
            continue
        config = _read_config(Path(home))
        if not isinstance(config, dict):
            continue
        _enabled, disabled = _plugin_lists(config)
        if item["plugin"] in disabled:
            still.append(item)
    return still


# ── the whole reading ───────────────────────────────────────────────────────────────────────────────────────────
def check(home: Path, *, now: float | None = None, host=None, install: Path | None = None, pins=()) -> dict:
    """Everything above for the Hermes `home` belongs to; `pins`, the builders whose pin of Hermes' files to watch
    (the `overlay_pins` setting). Never raises: a part that cannot be read is left out."""
    from .environment import root_of

    now = time.time() if now is None else now
    if host is None:
        from .system_update import Host

        host = Host()
    root = root_of(Path(home))
    install = install or install_root()
    reading: dict = {"at": now, "install": str(install) if install else None}
    try:
        reading["updates"] = receipts(root)
    except Exception:
        reading["updates"] = []
    last = reading["updates"][0] if reading["updates"] else None
    reading["update"] = last
    snapshot = read_snapshot(root)
    reading["snapshot_at"] = (snapshot or {}).get("at")
    reading["overlays"] = assess(snapshot, now=now)
    reading["paths"] = list((snapshot or {}).get("paths") or ())[:16]
    reading["folders"] = folders_now(snapshot)
    reading["pins_config"] = [str(item) for item in list(pins or ())[:PINS_KEPT]]
    reading["pins"] = []
    for item in reading["pins_config"]:
        builder, _, guide = item.partition("|")   # "<builder>|<guide to rebuild it>"
        try:
            reading["pins"].append(pin_check(Path(builder.strip()), install=install, guide=guide.strip() or None))
        except Exception:
            reading["pins"].append({"builder": builder.strip(), "files": [], "state": "unreadable"})
    for key, reader in (("local", lambda: local_changes(install, host) if install else None),
                        ("stashes", lambda: stashes(install, host, now=now) if install else None),
                        ("config", lambda: config_versions(root, latest_config_version(install))),
                        ("plugins_off", lambda: plugins_still_off(root, last))):
        try:
            reading[key] = reader()
        except Exception:
            reading[key] = None
    reading["findings"] = findings(reading)
    return reading


def findings(reading: dict) -> list[str]:
    """Attention codes of a reading, the most urgent first."""
    found = []
    overlays = reading.get("overlays") or ()
    if any(item.get("state") == "stale" for item in overlays):
        found.append("drift_overlay_stale")
    if any(item.get("state") == "stale" for item in reading.get("pins") or ()):
        found.append("drift_pin_stale")
    if any(item.get("state") == "reload" for item in overlays):
        found.append("drift_overlay_reload")
    folders = reading.get("folders") or ()
    if any(item.get("added") or item.get("removed") or item.get("missing") for item in folders):
        found.append("drift_overlay_folder")
    if any(item.get("unsafe") for item in folders):
        found.append("drift_overlay_unsafe")
    update = reading.get("update") or {}
    stashes_ = [item for item in reading.get("stashes") or () if item.get("update") and "files" in item]
    if update.get("local_changes") == "parked":
        found.append("drift_update_parked")
    if any(item.get("missing") for item in stashes_):
        found.append("drift_stash_missing")
    elif any(item.get("old") for item in stashes_):
        found.append("drift_stash_old")
    if reading.get("plugins_off"):
        found.append("drift_plugins_off")
    if update.get("failed_units") or update.get("restart_incomplete"):
        found.append("drift_restart_failed")
    if reading.get("config"):
        found.append("drift_config_behind")
    return found


def cached(conn, home: Path, *, now: float, force: bool = False, host=None, install: Path | None = None,
           pins=()) -> dict | None:
    """The reading kept in the lead's database ('drift_check'), taken again every CHECK_EVERY_SECONDS, when a newer
    update receipt exists than the one it read, or with `force`."""
    row = conn.execute("SELECT value FROM cursor WHERE key='drift_check'").fetchone()
    try:
        stored = json.loads(row[0]) if row else None
    except ValueError:
        stored = None
    stored = stored if isinstance(stored, dict) else None
    wanted = [str(item) for item in list(pins or ())[:PINS_KEPT]]
    if stored and not force:
        age = now - float(stored.get("at") or 0)
        if 0 <= age < CHECK_EVERY_SECONDS and _latest_receipt_name(home) == stored.get("receipt") \
                and stored.get("pins_config", []) == wanted:
            return stored
    reading = check(home, now=now, host=host, install=install, pins=wanted)
    reading["receipt"] = _latest_receipt_name(home)
    conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('drift_check', ?)", (json.dumps(reading),))
    return reading


def _latest_receipt_name(home: Path) -> str | None:
    from .environment import root_of

    folder = root_of(Path(home)) / "logs" / "update_receipts"
    try:
        names = [entry.name for entry in os.scandir(folder) if entry.name.startswith("update_")]
    except OSError:
        return None
    return max(names) if names else None


def stored(conn) -> dict | None:
    row = conn.execute("SELECT value FROM cursor WHERE key='drift_check'").fetchone()
    try:
        value = json.loads(row[0]) if row else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


# ── in plain words ──────────────────────────────────────────────────────────────────────────────────────────────
def _when(stamp, lang: str) -> str:
    from .status import local_time

    return local_time(stamp, lang) if isinstance(stamp, (int, float)) else "?"


def _files(names, lang: str, limit: int = 4) -> str:
    names = list(names or ())
    shown = ", ".join(f"`{name}`" for name in names[:limit])
    rest = len(names) - limit
    if rest > 0:
        shown += f" e mais {rest}" if lang == "pt" else f" and {rest} more"
    return shown


def lines(reading: dict | None, lang: str = "en") -> list[str]:
    """The reading as Markdown bullets: what changed, what it means and what to do, most urgent first."""
    if not isinstance(reading, dict):
        return []
    pt = lang == "pt"
    out = []
    update = reading.get("update") or {}
    if update:
        source, target = (update.get("from") or {}).get("version"), (update.get("to") or {}).get("version")
        outcome = {"success": ("concluída", "succeeded"), "partial": ("parcial", "partial")}.get(
            update.get("outcome"), (update.get("outcome") or "?", update.get("outcome") or "?"))
        out.append(f"- **Última atualização do Hermes:** {source or '?'} → {target or '?'} em "
                   f"{_when(update.get('finished') or update.get('started'), lang)} ({outcome[0]})." if pt else
                   f"- **Last Hermes update:** {source or '?'} → {target or '?'} on "
                   f"{_when(update.get('finished') or update.get('started'), lang)} ({outcome[1]}).")
    for item in reading.get("overlays") or ():
        state, module = item.get("state"), item.get("module")
        if state == "stale":
            out.append(
                f"- ⚠️ **Sobreposição desatualizada:** `{module}` roda a partir de `{item.get('overlay')}`, no lugar do "
                f"arquivo do Hermes `{item.get('upstream')}`, que mudou depois ({_when(item.get('upstream_mtime'), lang)}; "
                f"a sobreposição é de {_when(item.get('overlay_mtime'), lang)}). As correções da versão nova desse "
                "arquivo ficam desligadas até a sobreposição ser refeita a partir dele (e qualquer fixação de hash no "
                "script que a gera, atualizada); depois, reinicie o gateway." if pt else
                f"- ⚠️ **Stale overlay:** `{module}` runs from `{item.get('overlay')}` instead of Hermes' file "
                f"`{item.get('upstream')}`, which changed after it ({_when(item.get('upstream_mtime'), lang)}; the "
                f"overlay is from {_when(item.get('overlay_mtime'), lang)}). The new version's fixes in that file stay "
                "off until the overlay is rebuilt from it (and any hash pinned by the script that builds it, updated); "
                "then restart the gateway.")
        elif state == "reload":
            out.append(f"- **Sobreposição refeita, ainda não carregada:** `{item.get('overlay')}` mudou depois que o "
                       f"gateway subiu; ele continua rodando a cópia anterior de `{module}` até reiniciar." if pt else
                       f"- **Overlay rebuilt, not loaded yet:** `{item.get('overlay')}` changed after the gateway "
                       f"started; it keeps running the previous copy of `{module}` until it restarts.")
    for item in reading.get("pins") or ():
        builder, guide = item.get("builder"), item.get("guide")
        if item.get("state") == "stale":
            changed = [entry for entry in item.get("files") or () if not entry.get("pinned")]
            names = ", ".join(f"`{entry['path']}` (sha256 {str(entry.get('sha256') or '?')[:12]}…)" for entry in changed[:3])
            pinned = ", ".join(f"{pin}…" for pin in item.get("pins") or ()) or "?"
            out.append(f"- ⚠️ **Pino desatualizado:** {names} não bate mais com o pino de `{builder}` ({pinned}): a "
                       "sobreposição gerada a partir dele roda o código de antes da atualização. Refaça a sobreposição "
                       "e o pino" + (f" pelo passo a passo em `{guide}`" if guide else "") + "; nada é refeito sozinho." if pt else
                       f"- ⚠️ **Stale pin:** {names} no longer matches the pin in `{builder}` ({pinned}): the overlay built "
                       "from it runs the code from before the update. Rebuild the overlay and the pin"
                       + (f" following `{guide}`" if guide else "") + "; nothing is rebuilt by itself.")
        elif item.get("state") == "ok":
            files = ", ".join(f"`{Path(entry['path']).name}`" for entry in item.get("files") or ())
            out.append(f"- O pino de `{builder}` bate com {files} ({', '.join(item.get('pins') or ())[:40]}…)." if pt else
                       f"- The pin in `{builder}` matches {files} ({', '.join(item.get('pins') or ())[:40]}…).")
        elif item.get("state") in ("unreadable", "unpinned"):
            out.append(f"- O pino de `{builder}` não pôde ser conferido (arquivo ilegível, ou sem um sha256 e um arquivo do "
                       "Hermes nele)." if pt else
                       f"- The pin in `{builder}` could not be checked (unreadable, or no sha256 and Hermes file in it).")
    words = {"link": ("é um link", "is a link"), "not_file": ("não é um arquivo comum", "is not a regular file"),
             "writable": ("pode ser alterado por grupo ou outros", "can be changed by group or others"),
             "owner": ("é de outro usuário", "belongs to another user"),
             "hardlink": ("tem outro nome no disco (nlink > 1)", "has another name on disk (nlink > 1)")}
    for item in reading.get("folders") or ():
        folder = item.get("folder")
        if item.get("missing"):
            out.append(f"- ⚠️ A pasta de sobreposição `{folder}` sumiu desde que o gateway subiu: a próxima partida pode "
                       "falhar." if pt else
                       f"- ⚠️ The overlay folder `{folder}` is gone since the gateway started: its next start may fail.")
            continue
        if item.get("added") or item.get("removed"):
            change = ", ".join([*(f"+`{name}`" for name in item.get("added") or ()),
                                *(f"-`{name}`" for name in item.get("removed") or ())])
            out.append(f"- ⚠️ A pasta de sobreposição `{folder}` mudou desde que o gateway subiu ({change}): um lançador "
                       "que confere o conteúdo dela pode recusar a próxima partida do gateway. Confira antes de "
                       "reiniciar." if pt else
                       f"- ⚠️ The overlay folder `{folder}` changed since the gateway started ({change}): a launcher that "
                       "checks its contents may refuse the gateway's next start. Check it before a restart.")
        for entry in item.get("unsafe") or ():
            reasons = "; ".join(words.get(reason, (reason, reason))[0 if pt else 1] for reason in entry.get("reasons") or ())
            out.append(f"- ⚠️ `{folder}/{entry['name']}` {reasons}: o gateway roda esse código." if pt else
                       f"- ⚠️ `{folder}/{entry['name']}` {reasons}: the gateway runs that code.")
    overlays = [item for item in reading.get("overlays") or () if item.get("state") == "ok"]
    if overlays:
        names = ", ".join(f"`{item.get('module')}`" for item in overlays[:4])
        out.append(f"- Sobreposições em dia com o Hermes: {names}." if pt else
                   f"- Overlays up to date with Hermes: {names}.")
    if update.get("local_changes") == "parked":
        out.append(f"- ⚠️ **A última atualização guardou as suas mudanças locais num stash** (`{update.get('stash')}`): "
                   "elas não voltaram sozinhas porque não se aplicavam mais à versão nova." if pt else
                   f"- ⚠️ **The last update parked your local changes in a stash** (`{update.get('stash')}`): they did "
                   "not come back by themselves because they no longer applied to the new version.")
    local = reading.get("local") or {}
    if local.get("count"):
        out.append(f"- {_q(local['count'], 'arquivo do Hermes difere', 'arquivos do Hermes diferem')} da versão oficial (mudanças locais): "
                   f"{_files([*local.get('modified', ()), *local.get('deleted', ()), *local.get('added', ())], lang)}."
                   if pt else
                   f"- {_q(local['count'], 'Hermes file differs', 'Hermes files differ')} from the official version (local changes): "
                   f"{_files([*local.get('modified', ()), *local.get('deleted', ()), *local.get('added', ())], lang)}.")
    install = reading.get("install") or "<hermes>"
    for item in reading.get("stashes") or ():
        if not item.get("update") or "files" not in item:
            continue
        missing, partly = item.get("missing") or [], item.get("partly") or []
        if not missing and not partly and not item.get("old"):
            continue
        when = _when(item.get("at"), lang)
        if missing:
            out.append(f"- ⚠️ `{item['ref']}` ({when}): {len(missing)} de {_q(item['files'], 'arquivo guardado', 'arquivos guardados')} por uma "
                       f"atualização não {'tem' if len(missing) == 1 else 'têm'} a mudança hoje ({_files(missing, lang)}). Ou o Hermes já corrigiu isso, ou "
                       f"ela se perdeu: veja com `git -C {install} stash show -p {item['ref']}`." if pt else
                       f"- ⚠️ `{item['ref']}` ({when}): {len(missing)} of {_q(item['files'], 'file', 'files')} an update stashed "
                       f"{'does' if len(missing) == 1 else 'do'} not have the change now ({_files(missing, lang)}). Either Hermes fixed it upstream, or it "
                       f"was lost: look with `git -C {install} stash show -p {item['ref']}`.")
        elif item.get("old"):
            out.append(f"- `{item['ref']}` ({when}): as mudanças que ele guarda estão nos arquivos do Hermes"
                       + (f", algumas só em parte ({_files(partly, lang)})" if partly else "") +
                       f". Se não precisar mais dele: `git -C {install} stash drop {item['ref']}`." if pt else
                       f"- `{item['ref']}` ({when}): the changes it holds are in Hermes' files"
                       + (f", some only in part ({_files(partly, lang)})" if partly else "") +
                       f". When you no longer need it: `git -C {install} stash drop {item['ref']}`.")
    for item in reading.get("plugins_off") or ():
        out.append(f"- ⚠️ O plugin `{item['plugin']}` foi desligado pela atualização no perfil `{item['profile']}` e "
                   f"continua desligado: `hermes -p {item['profile']} plugins enable {item['plugin']}` quando as "
                   "dependências dele couberem de novo." if pt else
                   f"- ⚠️ The update turned the plugin `{item['plugin']}` off in profile `{item['profile']}` and it is "
                   f"still off: `hermes -p {item['profile']} plugins enable {item['plugin']}` once its dependencies "
                   "fit again.")
    for unit in update.get("failed_units") or ():
        out.append(f"- ⚠️ `{unit}` não voltou depois da atualização." if pt else
                   f"- ⚠️ `{unit}` did not come back after the update.")
    for item in reading.get("config") or ():
        stamp = item.get("stamp")
        out.append(f"- A configuração do perfil `{item['profile']}` está no esquema {stamp if stamp is not None else '—'} "
                   f"e este Hermes escreve o {item['latest']}: `hermes -p {item['profile']} config migrate`." if pt else
                   f"- Profile `{item['profile']}`'s configuration is on schema {stamp if stamp is not None else '—'} "
                   f"and this Hermes writes {item['latest']}: `hermes -p {item['profile']} config migrate`.")
    return out
