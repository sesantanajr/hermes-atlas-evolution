"""The whole Hermes at once (0.8.5): every profile, its plugins and skills, how much it is used, and what to do.

One profile looks after the whole Hermes (`lead`: the default profile when the plugin is on there, else the first
profile that has it), so the work is done once however many profiles load the plugin. `scan` reads, never writes:
the profiles' folders, their config.yaml, each plugin's manifest, git checkout and Python source (parsed, never run),
each skill's SKILL.md and Hermes' skill usage records. `findings` turns that, Hermes' plugin catalog and the
knowledge base (ecosystem.py) into situations of the playbook, each with its reason, its numbers and its action.

What the plugin reads in a plugin's source: which tools a transform hook acts on (`if tool_name != "web_search"`,
`_TOOLS.get(tool_name)`, `tool_name in {...}`), and whether a `pre_tool_call` hook rewrites the terminal's command.
Hermes keeps only the first transform result of each call, so two plugins conflict only when they transform the same
tool; plugins that act on different tools do not, and nothing is reported then.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import time
from pathlib import Path

PLUGIN_ID = "hermes-atlas-evolution"
REPOSITORY = "sesantanajr/hermes-atlas-evolution"
PROFILE_MAX = 100
PLUGIN_MAX = 200
SKILL_MAX = 3000
SOURCE_FILES_MAX = 120
SOURCE_BYTES_MAX = 768 * 1024
UNUSED_SKILL_DAYS = 30   # Hermes' own curator.archive_after_days default
IDLE_PLUGIN_DAYS = 1     # installed and not loaded for longer than this (a plugin just installed is left alone)
LEFTOVER_DAYS = 1
IDLE_PROFILE_DAYS = 30
SIMILAR = 0.6
FIRST_WINS = ("transform_tool_result", "transform_terminal_output", "transform_llm_output")
KINDS = ("plugin_removed", "plugin_removed_off", "plugin_transform_overlap", "plugin_duplicate", "plugin_unused",
         "plugin_leftover", "plugin_update", "plugin_obsolete", "self_missing", "profile_idle", "skills_unused",
         "skills_duplicate", "skills_similar", "skills_shared")   # what findings() reports
_TOOLISH = {"tool_name", "tool", "name", "function_name", "tool_id"}
_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_PLUGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}")
_WORD = re.compile(r"[a-zà-ú0-9]{4,}")


# ── profiles ─────────────────────────────────────────────────────────────────────────────────────────────────────
def root_of(home: Path) -> Path:
    home = Path(home).resolve()
    return home.parent.parent if home.parent.name == "profiles" else home


def profiles(root: Path) -> list[tuple[str, Path]]:
    """(name, home) of every profile of this Hermes: the default one (the root) first."""
    found = [("default", Path(root))]
    try:
        entries = sorted(Path(root, "profiles").iterdir())
    except OSError:
        entries = []
    for entry in entries:
        if len(found) > PROFILE_MAX:
            break
        if _PROFILE.fullmatch(entry.name) and not entry.name.startswith(".") and entry.is_dir() and not entry.is_symlink():
            found.append((entry.name, entry))
    return found


def _read_config(home: Path):
    from .integration import _read_hermes_config

    return _read_hermes_config(home)


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
    return (Path(home) / "plugins" / PLUGIN_ID / "plugin.yaml").is_file() and enabled is not None \
        and PLUGIN_ID in enabled and PLUGIN_ID not in disabled


def lead(root: Path, *, read_config=None) -> str | None:
    """The profile that looks after the whole Hermes: the default one when the plugin is on there, else the first."""
    read_config = read_config or _read_config
    for name, home in profiles(root):
        if has_plugin(home, read_config(home)):
            return name
    return None


# ── plugins ──────────────────────────────────────────────────────────────────────────────────────────────────────
def manifest(folder: Path) -> dict | None:
    """name, version, description and the hooks and tools a plugin.yaml declares (a small reader: no PyYAML)."""
    path = Path(folder) / "plugin.yaml"
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65_536:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    found: dict = {"name": Path(folder).name, "version": "", "description": "", "provides_hooks": [], "provides_tools": []}
    current, folded = None, []
    for line in text.splitlines():
        if line and not line.startswith((" ", "#", "-", "\t")):
            if current == "description" and folded:
                found["description"] = " ".join(folded)[:400]
            key, _, value = line.partition(":")
            current, folded = key.strip(), []
            value = value.strip()
            if current in ("name", "version") and value:
                found[current] = value.strip("'\"")[:96]
            elif current == "description" and value and value not in (">-", ">", "|", "|-"):
                found["description"] = value.strip("'\"")[:400]
                current = None
        elif current == "description" and line.strip():
            folded.append(line.strip())
        elif current in ("provides_hooks", "provides_tools") and line.strip().startswith("- "):
            found[current].append(line.strip()[2:].strip().strip("'\"")[:96])
    if current == "description" and folded:
        found["description"] = " ".join(folded)[:400]
    return found


def git_origin(folder: Path) -> tuple[str, str]:
    """(origin URL, HEAD commit) of a plugin's git checkout, read from its .git files; ("", "") when not one."""
    git = Path(folder) / ".git"
    repo = sha = ""
    try:
        config = (git / "config").read_text(encoding="utf-8", errors="replace")[:65_536]
        section = re.search(r'\[remote "origin"\]([^\[]*)', config)
        url = re.search(r"^\s*url\s*=\s*(\S+)", section.group(1), re.M) if section else None
        repo = url.group(1)[:300] if url else ""
        head = (git / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
        if head.startswith("ref: "):
            ref = head[5:].strip()
            if re.fullmatch(r"refs/[A-Za-z0-9._/-]{1,200}", ref) and ".." not in ref:
                try:
                    sha = (git / ref).read_text(encoding="utf-8").strip()
                except OSError:
                    packed = (git / "packed-refs").read_text(encoding="utf-8", errors="replace")[:1_000_000]
                    match = re.search(rf"^([0-9a-f]{{40}}) {re.escape(ref)}$", packed, re.M)
                    sha = match.group(1) if match else ""
        else:
            sha = head
    except (OSError, AttributeError):
        pass
    return repo, sha if re.fullmatch(r"[0-9a-f]{40}", sha or "") else ""


def _size(folder: Path, limit: int = 4000) -> tuple[int, float]:
    """Bytes of a folder (bounded walk, links not followed) and its newest change."""
    total, newest, seen = 0, 0.0, 0
    stack = [Path(folder)]
    while stack and seen < limit:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            seen += 1
            try:
                info = entry.lstat()
            except OSError:
                continue
            newest = max(newest, info.st_mtime)
            if entry.is_symlink():
                continue
            if entry.is_dir():
                stack.append(entry)
            else:
                total += info.st_size
    return total, newest


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


def _tools_in(function, functions, constants, depth: int = 0) -> set[str] | None:
    """The tool names a hook callback limits itself to, or None when it may act on any tool."""
    found: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Compare) and (_toolish(node.left) or any(_toolish(item) for item in node.comparators)):
            for side in (node.left, *node.comparators):
                if isinstance(side, ast.Constant) and isinstance(side.value, str):
                    found.add(side.value)
                elif isinstance(side, ast.Name) and side.id in constants:
                    found |= constants[side.id]
                else:
                    found |= _literal_strings(side) or set()
        elif isinstance(node, ast.Call):
            callee = node.func
            if isinstance(callee, ast.Attribute) and callee.attr == "get" and isinstance(callee.value, ast.Name) \
                    and callee.value.id in constants and node.args and _toolish(node.args[0]):
                found |= constants[callee.value.id]
            elif depth < 2 and isinstance(callee, ast.Name) and callee.id in functions \
                    and any(_toolish(argument) for argument in node.args):
                inner = _tools_in(functions[callee.id], functions, constants, depth + 1)
                found |= inner or set()
    return found or None


def _rewrites_command(function) -> bool:
    """Whether a callback changes a `command` argument: it writes args["command"], or answers Hermes' pre_tool_call
    directive {"action": "modify", "args": {"command": ...}}."""
    modify = False
    for node in ast.walk(function):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AugAssign) else []
        for target in targets:
            if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant) and target.slice.value == "command":
                return True
        if isinstance(node, ast.Constant) and node.value == "modify":
            modify = True
    return modify and any(isinstance(node, ast.Dict) and any(isinstance(key, ast.Constant) and key.value == "command"
                                                                for key in node.keys) for node in ast.walk(function))


_HOOK_NAMES = re.compile(r"(pre|post|on|transform)_[a-z_]{2,60}")


def code_hooks(folder: Path) -> dict:
    """What a plugin's Python source registers, without running it: {hook: {"tools": [...] | None, "rewrites": bool}}.
    `ctx.register_hook("hook", f)` and helpers called with ("hook", f) are both read; `f` is looked up in every file
    of the plugin (an imported function too). "tools" None: the callback may act on any tool."""
    trees, budget = [], SOURCE_BYTES_MAX
    try:
        files = sorted(Path(folder).glob("*.py"))[:SOURCE_FILES_MAX] + sorted(Path(folder).glob("*/*.py"))[:SOURCE_FILES_MAX]
    except OSError:
        files = []
    for path in files:
        try:
            if path.is_symlink() or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        budget -= len(text)
        if budget < 0:
            break
        try:
            trees.append(ast.parse(text))
        except (SyntaxError, ValueError, RecursionError):
            continue
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
            entry = hooks.setdefault(hook, {"tools": set(), "rewrites": False, "known": True})
            if function is None:
                entry["known"] = False
                continue
            tools = _tools_in(function, functions, constants)
            if tools is None:
                entry["known"] = False
            else:
                entry["tools"] |= tools
            entry["rewrites"] = entry["rewrites"] or _rewrites_command(function)
    return {hook: {"tools": sorted(item["tools"]) if item["known"] else None, "rewrites": item["rewrites"]}
            for hook, item in hooks.items()}


def _plugins_of(home: Path, enabled, disabled, bundled: Path | None, *, now: float) -> tuple[list, list]:
    """The plugins a profile has (its own folder, and the bundled ones it enables) and leftover install folders."""
    found, leftovers = [], []
    sources = [("profile", Path(home) / "plugins")]
    if bundled is not None:
        sources.append(("bundled", bundled))
    for origin, base in sources:
        try:
            entries = sorted(base.iterdir())[:PLUGIN_MAX]
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink() or not entry.is_dir():
                continue
            if origin == "profile" and entry.name.startswith(".install-"):
                size, newest = _size(entry, 2000)
                try:
                    newest = max(newest, entry.lstat().st_mtime)
                except OSError:
                    pass
                if now - newest > LEFTOVER_DAYS * 86_400:
                    leftovers.append({"folder": entry.name, "bytes": size, "age_days": round((now - newest) / 86_400)})
                continue
            if entry.name.startswith((".", "_")) or not _PLUGIN.fullmatch(entry.name):
                continue
            meta = manifest(entry)
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
            repo, sha = git_origin(entry) if origin == "profile" else ("", "")
            size, newest = _size(entry) if origin == "profile" else (0, 0.0)
            found.append({"folder": entry.name, "name": meta["name"], "version": meta["version"],
                          "description": meta["description"], "declared_hooks": meta["provides_hooks"],
                          "origin": origin, "state": state, "repo": repo, "sha": sha, "bytes": size,
                          "changed": newest, "hooks": code_hooks(entry) if state == "enabled" else {}})
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

        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _skills_of(home: Path) -> list[dict]:
    base = Path(home) / "skills"
    try:
        usage = json.loads((base / ".usage.json").read_text(encoding="utf-8"))
        usage = usage if isinstance(usage, dict) else {}
    except (OSError, ValueError):
        usage = {}
    found = []
    try:
        paths = sorted(base.rglob("SKILL.md"))
    except OSError:
        paths = []
    for path in paths[:SKILL_MAX]:
        try:
            relative = path.relative_to(base)
        except ValueError:
            continue
        if any(part.startswith(".") for part in relative.parts) or len(relative.parts) > 5 or path.is_symlink():
            continue
        try:
            if path.stat().st_size > 256 * 1024:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        name, description, body = _front(text)
        folder = path.parent.name
        record = usage.get(folder) if isinstance(usage.get(folder), dict) else {}
        found.append({"folder": folder, "path": relative.parent.as_posix(), "name": name or folder,
                      "description": description, "sha": hashlib.sha256(text.encode()).hexdigest()[:16],
                      "body_sha": hashlib.sha256(body.strip().encode()).hexdigest()[:16],
                      "last_used": _iso(record.get("last_used_at")), "created": _iso(record.get("created_at")),
                      "uses": record.get("use_count") if type(record.get("use_count")) is int else 0,
                      "pinned": record.get("pinned") is True, "locked": record.get("locked") is True,
                      "state": record.get("state") if isinstance(record.get("state"), str) else "active"})
    return found


# ── the scan ─────────────────────────────────────────────────────────────────────────────────────────────────────
def scan(root: Path, *, now: float | None = None, read_config=None, bundled: Path | None = None,
         sessions=None) -> dict:
    """Every profile of the Hermes at `root`: its plugins, leftover installs, skills and use. Read only."""
    from .ecosystem import hermes_source

    now = time.time() if now is None else now
    read_config = read_config or _read_config
    if bundled is None:
        source = hermes_source()
        bundled = source / "plugins" if source is not None else None
    if sessions is None:
        from .status import _sessions as sessions
    rows = []
    for name, home in profiles(root):
        config = read_config(home)
        enabled, disabled = _plugin_lists(config)
        plugins, leftovers = _plugins_of(home, enabled, disabled, bundled, now=now)
        settings = (((config or {}).get("plugins") or {}).get("entries") or {}).get(PLUGIN_ID) if isinstance(config, dict) else None
        rows.append({"profile": name, "home": str(home), "config_read": isinstance(config, dict),
                     "sessions_7d": sessions(home, since=now - 7 * 86_400) or 0,
                     "sessions_30d": sessions(home, since=now - IDLE_PROFILE_DAYS * 86_400) or 0,
                     "plugins": plugins, "leftovers": leftovers, "skills": _skills_of(home),
                     "self": has_plugin(home, config),
                     "tool_hints": bool(isinstance(settings, dict) and isinstance(settings.get("settings"), dict)
                                        and settings["settings"].get("tool_hints") is True)})
    return {"at": now, "root": str(root), "profiles": rows}


# ── findings ─────────────────────────────────────────────────────────────────────────────────────────────────────
def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.casefold()))


def _similar(left: dict, right: dict) -> float:
    a, b = _words(left["description"] or left["name"]), _words(right["description"] or right["name"])
    return len(a & b) / len(a | b) if a and b else 0.0


def _finding(kind: str, profile: str, subject: str, *, risk: str, evidence: dict, command: str = "") -> dict:
    return {"code": f"env:{kind}:{profile}:{subject}"[:240], "kind": kind, "risk": risk,
            "subject": f"{profile}/{subject}" if subject else profile, "evidence": {"profile": profile, **evidence},
            "command": command}


def _cmd(profile: str, args: str) -> str:
    return f"hermes {'' if profile == 'default' else f'-p {profile} '}{args}"


def findings(reading: dict, catalog: dict, known: dict, *, now: float | None = None) -> list[dict]:
    """The situations of the playbook found in a scan, most important first."""
    from .ecosystem import lookup, obsolete, removed_reason

    now = time.time() if now is None else now
    notes = known.get("plugins") or {}
    found: list[dict] = []
    copies: dict[str, set[str]] = {}
    updates: dict[str, dict] = {}
    for row in reading.get("profiles") or ():
        profile = row["profile"]
        # Hermes loads plugins in alphabetical order of their manifest name (requires_plugins aside): that order
        # decides which transform result counts.
        on = sorted((item for item in row["plugins"] if item["state"] == "enabled"), key=lambda item: item["name"])
        # A plugin the Hermes catalog removed (its kill list): off at once, the removal is yours.
        for item in row["plugins"]:
            reason = removed_reason(item, catalog)
            if reason:
                found.append(_finding("plugin_removed" if item["state"] == "enabled" else "plugin_removed_off", profile,
                                      item["folder"], risk="high", evidence={"reason": reason, "state": item["state"]},
                                      command=_cmd(profile, f"plugins remove {item['folder']}")))
        # Archived, delisted, missing or unmaintained upstream (knowledge base, checked against GitHub).
        for item in row["plugins"]:
            if item["origin"] != "profile":
                continue
            note = obsolete(item, known)
            if note:
                found.append(_finding("plugin_obsolete", profile, item["folder"],
                                      risk="low" if note.get("state") == "unmaintained" else "medium",
                                      evidence={"state": note.get("state"), "basis": note.get("basis"),
                                                "replacement": note.get("superseded_by") or [], "loaded": item["state"]},
                                      command=_cmd(profile, f"plugins remove {item['folder']}")))
        # Transform hooks: Hermes keeps the first result of each call; a conflict is two plugins on the same tool.
        for hook in FIRST_WINS:
            users = [item for item in on if hook in item["hooks"] or hook in item["declared_hooks"]]
            users = [item for item in users if not (item["name"] == PLUGIN_ID and not row["tool_hints"])]
            for index, first in enumerate(users):
                for later in users[index + 1:]:
                    a = (first["hooks"].get(hook) or {}).get("tools", None) if hook in first["hooks"] else None
                    b = (later["hooks"].get(hook) or {}).get("tools", None) if hook in later["hooks"] else None
                    shared = sorted(set(a) & set(b)) if a is not None and b is not None else None
                    if shared == []:
                        continue   # different tools: both changes apply
                    protected = _protected(later, notes) and not _protected(first, notes)
                    off = first if protected else later
                    found.append(_finding("plugin_transform_overlap", profile, off["folder"],
                                          risk="high" if _protected(later, notes) else "medium",
                                          evidence={"hook": hook, "first": first["folder"], "later": later["folder"],
                                                    "tools": shared, "first_tools": a, "later_tools": b,
                                                    "off": off["folder"], "protected": _protected(later, notes)},
                                          command=_cmd(profile, f"plugins disable {off['folder']}")))
        # Two plugins doing one job: rewriting the terminal's commands, or a role only one should fill.
        rewriters = [item for item in on if (item["hooks"].get("pre_tool_call") or {}).get("rewrites")]
        if len(rewriters) > 1:
            keep = rewriters[0]
            for item in rewriters[1:]:
                found.append(_finding("plugin_duplicate", profile, item["folder"], risk="medium",
                                      evidence={"role": "terminal_rewrite", "keep": keep["folder"], "plugins":
                                                [entry["folder"] for entry in rewriters]},
                                      command=_cmd(profile, f"plugins disable {item['folder']}")))
        roles: dict[str, list] = {}
        for item in on:
            note = notes.get(item["name"]) or notes.get(item["folder"])
            if isinstance(note, dict) and note.get("exclusive") and isinstance(note.get("role"), str):
                roles.setdefault(note["role"], []).append(item)
        for role, items in roles.items():
            if len(items) > 1 and role != "terminal_rewrite":
                for item in items[1:]:
                    found.append(_finding("plugin_duplicate", profile, item["folder"], risk="medium",
                                          evidence={"role": role, "keep": items[0]["folder"],
                                                    "plugins": [entry["folder"] for entry in items]},
                                          command=_cmd(profile, f"plugins disable {item['folder']}")))
        # Installed and never loaded: disk and confusion for nothing.
        idle = [item for item in row["plugins"] if item["origin"] == "profile" and item["state"] != "enabled"
                and item["name"] != PLUGIN_ID and now - (item["changed"] or now) > IDLE_PLUGIN_DAYS * 86_400]
        if idle:
            found.append(_finding("plugin_unused", profile, "", risk="low",
                                  evidence={"plugins": [{"name": item["folder"], "state": item["state"],
                                                         "mb": round(item["bytes"] / 2**20, 1)} for item in idle][:30],
                                            "count": len(idle), "mb": round(sum(item["bytes"] for item in idle) / 2**20, 1)},
                                  command=_cmd(profile, f"plugins remove {idle[0]['folder']}")))
        for item in row["leftovers"]:
            found.append(_finding("plugin_leftover", profile, item["folder"], risk="low",
                                  evidence={"mb": round(item["bytes"] / 2**20, 1), "age_days": item["age_days"]}))
        # A newer version in the Hermes catalog than the one installed (one finding per plugin, every profile in it).
        for item in on:
            entry = lookup(item, catalog) if item["origin"] == "profile" else None
            if entry and entry["version"] and item["version"] and entry["version"] != item["version"] \
                    and _newer(entry["version"], item["version"]):
                group = updates.setdefault(item["folder"], {"catalog": entry["version"], "tier": entry["tier"],
                                                            "profiles": []})
                group["profiles"].append({"profile": profile, "installed": item["version"]})
        # This plugin missing where the profile is used.
        if not row["self"] and row["sessions_7d"] > 0 and row["config_read"]:
            installed = any(item["name"] == PLUGIN_ID for item in row["plugins"])
            found.append(_finding("self_missing", profile, PLUGIN_ID, risk="low",
                                  evidence={"sessions_7d": row["sessions_7d"], "installed": installed},
                                  command=_cmd(profile, f"plugins enable {PLUGIN_ID}" if installed
                                               else f"plugins install {REPOSITORY} --enable")))
        if row["sessions_30d"] == 0 and row["profile"] != "default" and row["config_read"]:
            found.append(_finding("profile_idle", profile, "", risk="low",
                                  evidence={"days": IDLE_PROFILE_DAYS, "plugins": len(on), "skills": len(row["skills"])},
                                  command=_cmd(profile, "evolution status")))
        # Skills: unused, the same text twice, near twins, and copies kept in every profile.
        skills = [item for item in row["skills"] if item["state"] == "active"]
        unused = [item for item in skills if not (item["pinned"] or item["locked"])
                  and now - (item["last_used"] or item["created"] or now) > UNUSED_SKILL_DAYS * 86_400]
        if unused:
            unused.sort(key=lambda item: item["last_used"] or item["created"] or 0)
            found.append(_finding("skills_unused", profile, "", risk="low",
                                  evidence={"count": len(unused), "days": UNUSED_SKILL_DAYS, "total": len(skills),
                                            "skills": [item["folder"] for item in unused][:40]},
                                  command=_cmd(profile, f"curator archive {unused[0]['folder']}")))
        by_text: dict[str, list] = {}
        for item in skills:
            by_text.setdefault(item["body_sha"], []).append(item)
            copies.setdefault(item["sha"], set()).add(profile)
        for group in by_text.values():
            if len(group) > 1:
                group.sort(key=lambda item: -(item["uses"] or 0))
                found.append(_finding("skills_duplicate", profile, group[1]["folder"], risk="low",
                                      evidence={"keep": group[0]["folder"], "copies": [item["folder"] for item in group]},
                                      command=_cmd(profile, f"curator archive {group[1]['folder']}")))
        pairs = []
        ordered = sorted(skills, key=lambda item: item["folder"])[:400]
        for index, left in enumerate(ordered):
            for right in ordered[index + 1:]:
                if left["body_sha"] != right["body_sha"] and _similar(left, right) >= SIMILAR:
                    pairs.append((left["folder"], right["folder"]))
        if pairs:
            found.append(_finding("skills_similar", profile, "", risk="low",
                                  evidence={"pairs": [list(pair) for pair in pairs[:15]], "count": len(pairs)},
                                  command=_cmd(profile, "curator run --consolidate --dry-run")))
    for folder, group in sorted(updates.items()):
        names = [item["profile"] for item in group["profiles"]]
        found.append(_finding("plugin_update", "all", folder, risk="low",
                              evidence={**group, "count": len(names),
                                        "commands": [_cmd(name, f"plugins update {folder}") for name in names][:20]},
                              command=_cmd(names[0], f"plugins update {folder}")))
    shared = sum(1 for where in copies.values() if len(where) >= 3)
    if shared:
        found.append(_finding("skills_shared", "all", "", risk="low",
                              evidence={"skills": shared, "profiles": len(reading.get("profiles") or ())},
                              command="hermes config get skills.external_dirs"))
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(found, key=lambda item: (order.get(item["risk"], 3), item["kind"], item["subject"]))


def _protected(item: dict, notes: dict) -> bool:
    note = notes.get(item["name"]) or notes.get(item["folder"]) or {}
    return isinstance(note, dict) and note.get("protect") is True


def _newer(candidate: str, current: str) -> bool:
    def parts(text):
        return [int(piece) if piece.isdigit() else 0 for piece in re.split(r"[.\-+]", text.lstrip("v"))[:4]]
    try:
        return parts(candidate) > parts(current)
    except ValueError:
        return False


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
    return found if isinstance(found, dict) else None


def refresh(home: Path, *, now: float, force: bool = False, read_config=None, deadline: float | None = None) -> dict | None:
    """The whole Hermes, read by the lead profile at most every SCAN_EVERY_SECONDS (or now with `force`), kept in its
    database with Hermes' catalog and the knowledge base. None in any other profile: one profile does it for all."""
    from .admin import profile_name
    from .ecosystem import knowledge, load_catalog, sync
    from .experience import experience

    root = root_of(home)
    leader = lead(root, read_config=read_config)
    if leader is None or profile_name(home) != leader:
        return None
    with experience(home) as conn:
        cached = stored(conn)
    if not force and cached and now - float(cached.get("at") or 0) < SCAN_EVERY_SECONDS:
        return cached
    if deadline is not None and deadline - time.monotonic() < 60:
        return cached
    reading = scan(root, now=now, read_config=read_config)
    catalog = load_catalog([Path(row["home"]) for row in reading["profiles"]])
    known = knowledge()
    found = findings(reading, catalog, known, now=now)
    tiers: dict[str, int] = {}
    for entry in catalog["entries"].values():
        tiers[entry["tier"]] = tiers.get(entry["tier"], 0) + 1
    keep = {"at": now, "lead": leader, "summary": summary(reading), "profiles": [_compact(row) for row in reading["profiles"]],
            "catalog": {"source": catalog["source"], "generated_at": catalog["generated_at"],
                        "plugins": len(catalog["entries"]), "tiers": tiers, "removed": len(catalog["removed"])},
            "findings": found}
    with experience(home) as conn:
        sync(conn, catalog, known)
        conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('environment', ?)", (json.dumps(keep),))
    return keep


def lead_home(home: Path, *, read_config=None) -> Path | None:
    """The home of the profile that looks after the whole Hermes, None when no profile has the plugin on."""
    root = root_of(home)
    leader = lead(root, read_config=read_config)
    for name, folder in profiles(root):
        if name == leader:
            return Path(folder)
    return None
