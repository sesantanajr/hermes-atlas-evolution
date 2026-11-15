"""Bounded, passive inventory of Hermes Agent 0.23.1 profile components."""

from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import unicodedata

from .diagnostics import _checkpoint_unchanged


_MIB = 1024 * 1024
_PLUGIN_ROOT_LIMIT = 1024
_PLUGIN_FILES_LIMIT = 2500
_PLUGIN_TOTAL_LIMIT = 18000
_SKILL_TREE_LIMIT = 9000
_SKILL_TEXT_LIMIT = 96 * 1024
_SKILL_TOTAL_BYTES = 12 * _MIB
_USAGE_ROWS = 2500
_USAGE_DAYS = 14
_LABEL = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}\Z")
_TOOL = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9_.:-]{0,95}\Z")
_SPECIALIZED = frozenset({"memory", "context_engine", "image_gen", "video_gen", "tts", "stt", "model_providers",
                          "providers", "cron_providers", "config_backends", "dashboard_auth", "platforms", "browser"})
_PROVIDER_MARKERS = (b"register_memory_provider", b"MemoryProvider", b"register_cron_scheduler", b"CronScheduler",
                     b"register_computer_use_provider", b"ComputerUseProvider")
_SKILL_EXCLUDED = frozenset({"venv", "node_modules", "site-packages", "__pycache__"})
_SKILL_SUPPORT = frozenset({"references", "templates", "assets", "scripts"})


def _stat(path: Path):
    try:
        info = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        return None  # Includes Windows junction/reparse points.
    return info


def _directory(path: Path) -> bool:
    info = _stat(path)
    return info is not None and stat.S_ISDIR(info.st_mode)


def _read(path: Path, limit: int) -> tuple[str, bytes | None]:
    info = _stat(path)
    if info is None or not stat.S_ISREG(info.st_mode):
        return "missing_or_nonregular", None
    if info.st_size > limit:
        return "limit_exceeded", None
    flags = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                return "changed", None
            data = stream.read(limit + 1)
    except OSError:
        return "unreadable", None
    return ("ok", data) if len(data) <= limit else ("limit_exceeded", None)


def _walk(root: Path, depth: int, limit: int, scan: dict, *, hidden: bool = False,
          excluded=frozenset()):
    if not _directory(root):
        return
    current = [root]
    next_depth = []
    while current:
        for folder in current:
            if not _directory(folder):
                scan["unreadable"] += 1
                continue
            try:
                with os.scandir(folder) as entries:
                    for entry in entries:
                        if scan["entries"] >= limit:
                            scan["truncated"] = True
                            return
                        scan["entries"] += 1
                        if entry.name in excluded or not hidden and entry.name.startswith("."):
                            continue
                        path = Path(entry.path)
                        info = _stat(path)
                        if info is None:
                            continue
                        is_dir = stat.S_ISDIR(info.st_mode)
                        if is_dir and depth:
                            next_depth.append(path)
                        elif is_dir and hidden:
                            scan["truncated"] = True
                        if is_dir or stat.S_ISREG(info.st_mode):
                            yield path, info
            except OSError:
                scan["unreadable"] += 1
        current, next_depth = next_depth, []
        depth -= 1


def _metric() -> dict:
    return {"entries": 0, "truncated": False, "unreadable": 0}


def _name(value) -> str | None:
    return value if isinstance(value, str) and _LABEL.fullmatch(value) else None


def _scalar(value: str) -> str:
    return value.partition(" #")[0].strip().strip('"\'')


def _yaml_metadata(text: str) -> dict:
    """Read common literal fields only, without resolving YAML tags, aliases or includes."""
    result = {}
    tools = []
    in_tools = False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if in_tools and line.lstrip().startswith("- "):
            tools.append(_scalar(line.lstrip()[2:]))
            continue
        if line[0].isspace():
            continue
        key, _, value = line.partition(":")
        in_tools = key == "provides_tools"
        if key in {"name", "kind"}:
            result[key] = _scalar(value)
        if in_tools and value.strip().startswith("[") and "]" in value:
            tools.extend(_scalar(part) for part in value.split("[", 1)[1].split("]", 1)[0].split(","))
    result["provides_tools"] = tools
    return result


def _manifest(folder: Path) -> dict | None:
    for name in ("plugin.yaml", "plugin.yml", "plugin.json"):
        status, data = _read(folder / name, 48 * 1024)
        if status == "missing_or_nonregular":
            continue
        if data is None:
            return None
        try:
            text = data.decode("utf-8-sig")
            result = json.loads(text) if name.endswith("json") else _yaml_metadata(text)
        except (UnicodeError, ValueError, RecursionError):
            return None
        return result if isinstance(result, dict) else None
    return None


def _footprint(folder: Path, max_entries: int) -> tuple[dict, int]:
    scan = _metric()
    apparent_bytes = 0
    allocated_bytes = 0
    allocation_known = True
    seen = set()
    for _, info in _walk(folder, 12, max_entries, scan, hidden=True):
        if not stat.S_ISREG(info.st_mode):
            continue
        identity = (info.st_dev, info.st_ino)
        if info.st_ino and identity in seen:
            continue
        if info.st_ino:
            seen.add(identity)
        apparent_bytes += info.st_size
        blocks = getattr(info, "st_blocks", None)
        allocation_known = allocation_known and blocks is not None
        if blocks is not None:
            allocated_bytes += blocks * 512
    return ({"bytes": apparent_bytes, "allocated_bytes": allocated_bytes if allocation_known else None,
             "footprint_complete": not scan["truncated"] and not scan["unreadable"]}, scan["entries"])


def _separate_loader(folder: Path, relative: Path, kind) -> bool:
    if relative.parts[0] in _SPECIALIZED or kind not in (None, "standalone"):
        return True
    if kind == "standalone":
        return False
    status, source = _read(folder / "__init__.py", 64 * 1024)
    if source is None:
        return status != "missing_or_nonregular"
    prefix = source[:8192]
    return any(marker in prefix for marker in _PROVIDER_MARKERS) or (
        b"register_provider" in prefix and b"ProviderProfile" in prefix
    )


def _plugins(roots, enabled, disabled):
    inventory = []
    scan = _metric()
    files_checked = 0
    invalid = 0
    for origin, root in roots:
        manifests_at = set()
        for folder, info in _walk(root, 1, _PLUGIN_ROOT_LIMIT, scan):
            if not stat.S_ISDIR(info.st_mode) or folder.parent in manifests_at:
                continue
            relative = folder.relative_to(root)
            if not all(_name(part) for part in relative.parts):
                continue
            metadata = _manifest(folder)
            if metadata is None:
                continue
            manifests_at.add(folder)
            name = _name(metadata.get("name", folder.name))
            if name is None:
                invalid += 1
                continue
            key = relative.as_posix() if len(relative.parts) > 1 else name
            kind = metadata.get("kind")
            if _separate_loader(folder, relative, kind):
                state = "separate_loader"
            elif {key, name} & disabled:
                state = "explicitly_disabled"
            elif enabled is None:
                state = "unknown"
            elif {key, name} & enabled:
                state = "explicitly_enabled"
            else:
                state = "not_allowlisted" if origin == "profile" else "unknown"
            tools = metadata.get("provides_tools")
            tools = sorted({tool for tool in tools if isinstance(tool, str) and _TOOL.fullmatch(tool)})[:64] if isinstance(tools, list) else []
            budget = min(_PLUGIN_FILES_LIMIT, _PLUGIN_TOTAL_LIMIT - files_checked)
            size, inspected = _footprint(folder, budget)
            files_checked += inspected
            inventory.append({"name": key, "origin": origin, "state": state, "tools": tools,
                              "recent_matches": None, **size})
    inventory.sort(key=lambda item: (item["origin"] != "profile", -item["bytes"], item["name"]))
    return {"entries": inventory, "scan": scan, "footprint_entries": files_checked, "invalid_manifest_names": invalid}


def _skill_document(data: bytes) -> tuple[str | None, bytes]:
    lines = data.decode("utf-8-sig").splitlines()
    if not lines or lines[0].strip() != "---":
        return None, data.strip()
    stop = next((i for i, line in enumerate(lines[1:80], 1) if line.strip() == "---"), None)
    if stop is None:
        return None, data.strip()
    title = next((_name(_scalar(line.partition(":")[2])) for line in lines[1:stop] if line.startswith("name:")), None)
    return title, "\n".join(lines[stop + 1:]).strip().encode("utf-8")


def _grouped(groups, *, different_versions: bool = False) -> list[dict]:
    matches = []
    for items in groups.values():
        versions = {item[0] for item in items}
        if len(items) > 1 and (not different_versions or len(versions) > 1):
            skill_names = sorted({item[2] for item in items})
            matches.append({"name": skill_names[0], "names": skill_names[:12], "locations": sorted(item[1] for item in items)[:12],
                            "copies": len(items), "versions": len(versions)})
    return sorted(matches, key=lambda item: (-item["copies"], item["name"]))


def _skills(roots):
    exact = defaultdict(list)
    body_copies = defaultdict(list)
    names = defaultdict(list)
    scan = _metric()
    bytes_read = inspected = invalid = 0
    for origin, root in roots:
        for path, info in _walk(root, 7, _SKILL_TREE_LIMIT, scan, excluded=_SKILL_EXCLUDED):
            if not stat.S_ISREG(info.st_mode) or path.name != "SKILL.md":
                continue
            relative = path.relative_to(root)
            if not all(_name(part) for part in relative.parts):
                continue
            if any(part in _SKILL_SUPPORT and (root.joinpath(*relative.parts[:index]) / "SKILL.md").is_file()
                   for index, part in enumerate(relative.parts[:-1])):
                continue
            if info.st_size > _SKILL_TEXT_LIMIT:
                invalid += 1
                continue
            if bytes_read + info.st_size > _SKILL_TOTAL_BYTES:
                scan["truncated"] = True
                break
            status, data = _read(path, min(_SKILL_TEXT_LIMIT, _SKILL_TOTAL_BYTES - bytes_read))
            if status != "ok":
                invalid += 1
                continue
            bytes_read += len(data)
            try:
                title, body = _skill_document(data)
            except UnicodeError:
                invalid += 1
                continue
            inspected += 1
            version = hashlib.sha256(data).digest()
            location = origin + ":" + relative.as_posix()
            name = (title or path.parent.name).casefold()
            record = (version, location, name)
            exact[version].append(record)
            names[name].append(record)
            if len(body) >= 160:
                body_copies[hashlib.sha256(body).digest()].append(record)
    exact_matches = _grouped(exact)
    body_matches = _grouped(body_copies, different_versions=True)
    name_matches = _grouped(names, different_versions=True)
    return {"inspected": inspected, "unreadable_or_oversize": invalid, "scan": scan,
            "identical_groups": exact_matches[:30], "identical_group_count": len(exact_matches),
            "body_duplicates": body_matches[:30], "body_duplicate_count": len(body_matches),
            "name_collisions": name_matches[:30], "name_collision_count": len(name_matches)}


def _skill_usage(home: Path, now: float) -> dict:
    base = home / "skills"
    status, data = _read(base / ".usage.json", 2 * _MIB) if _directory(base) else ("missing_or_nonregular", None)
    report = {"status": status}
    if data is None:
        return report
    try:
        records = json.loads(data.decode("utf-8-sig"))
    except (UnicodeError, ValueError, RecursionError):
        return {"status": "invalid"}
    if not isinstance(records, dict):
        return {"status": "invalid"}
    states = Counter()
    recent = protected = invalid = 0
    window_days = 30
    for rec in records.values():
        if not isinstance(rec, dict):
            invalid += 1
            continue
        state = rec.get("state")
        states[state if state in ("active", "stale", "archived") else "unknown"] += 1
        protected += rec.get("pinned") is True
        try:
            stamp = datetime.fromisoformat(rec.get("last_used_at") or "")
            epoch = (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).timestamp()
        except (ValueError, TypeError, OverflowError, OSError):
            continue
        recent += now - window_days * 86400 <= epoch <= now
    return {**report, "records": len(records), "states": dict(states), "recent_usage_records": recent,
            "pinned_records": protected, "unknown_record_shapes": invalid, "window_days": window_days}


def _memory(home: Path, limits) -> dict:
    files = {}
    entries_by_file = {}
    base = home / "memories"
    for filename, limit_key, default in (("MEMORY.md", "memory_char_limit", 2200), ("USER.md", "user_char_limit", 1375)):
        path = base / filename
        status, data = _read(path, 96 * 1024) if _directory(base) else ("missing_or_nonregular", None)
        configured = (limits or {}).get(limit_key)
        limit = configured if type(configured) is int and configured > 0 else default
        report = {"status": status, "char_limit": limit,
                  "limit_source": "configured" if type(configured) is int and configured > 0 else "upstream_default"}
        files[filename] = report
        if data is None:
            continue
        try:
            text = data.decode("utf-8").replace("\r\n", "\n")
        except UnicodeError:
            report["status"] = "invalid_utf8"
            continue
        raw = [part.strip() for part in text.split("\n§\n")]
        entries = [part for part in raw if part]
        normalized = [" ".join(unicodedata.normalize("NFKC", part).casefold().split()) for part in entries]
        entries_by_file[filename] = set(normalized)
        longest = max(map(len, entries), default=0)
        canonical_length = sum(map(len, entries)) + max(0, len(entries) - 1) * 3
        report.update({"status": "ok" if entries else "empty", "bytes": len(data), "characters": canonical_length,
                       "entries": len(entries), "empty_sections": sum(not part for part in raw) if len(raw) > 1 else 0,
                       "duplicates": len(normalized) - len(set(normalized)), "largest_entry_characters": longest,
                       "oversized_entries": sum(len(entry) > limit for entry in entries),
                       "at_capacity": canonical_length >= limit})
    overlap = len(entries_by_file.get("MEMORY.md", set()) & entries_by_file.get("USER.md", set()))
    return {"files": files, "cross_file_duplicates": overlap}


def _tool_counts(connection, names, now):
    columns = {row[1]: row for row in connection.execute("PRAGMA table_info('messages')")}
    expected = {"id", "tool_name", "role", "timestamp", "active"}
    if not expected <= columns.keys() or columns["id"][2].upper() != "INTEGER" or not columns["id"][5]:
        return {"status": "unsupported_schema"}, {}
    cutoff = now - _USAGE_DAYS * 86400
    rows = connection.execute("SELECT COUNT(*) FROM (SELECT id FROM messages ORDER BY id DESC LIMIT ?)",
                              (_USAGE_ROWS,)).fetchone()[0]
    selections = ",".join("?" for _ in names)
    sql = f"""SELECT tool_name, COUNT(*) FROM
        (SELECT tool_name, role, active, timestamp FROM messages ORDER BY id DESC LIMIT ?)
        WHERE role='tool' AND active=1 AND timestamp BETWEEN ? AND ? AND tool_name IN ({selections})
        GROUP BY tool_name"""
    counts = dict(connection.execute(sql, (_USAGE_ROWS, cutoff, now, *names)))
    return ({"status": "sampled", "window_days": _USAGE_DAYS, "rows_sampled": rows,
             "row_limit_reached": rows == _USAGE_ROWS,
             "basis": "result_names_matching_unique_manifest_declarations_in_scanned_roots"}, counts)


def _usage_sample(home: Path, connection, names, now):
    if not names:
        return {"status": "no_declared_unambiguous_tools"}, {}
    try:
        if connection is not None:
            return _tool_counts(connection, names, now)
        path = home / "state.db"
        before = _stat(path)
        if before is None or not stat.S_ISREG(before.st_mode):
            return {"status": "unavailable"}, {}
        sidecars = [path.with_name(path.name + suffix) for suffix in ("-wal", "-shm", "-journal")]
        if any(file.is_symlink() for file in sidecars):
            return {"status": "unavailable"}, {}
        checkpoint = not any(file.exists() for file in sidecars)
        companions = [_stat(file) for file in sidecars[:2]]
        if not checkpoint and (not all(info and stat.S_ISREG(info.st_mode) for info in companions) or sidecars[2].exists()):
            return {"status": "unavailable"}, {}
        query = "?mode=ro&immutable=1" if checkpoint else "?mode=ro&readonly_shm=1"
        deadline = time.monotonic() + 0.2
        with closing(sqlite3.connect(path.absolute().as_uri() + query, uri=True, timeout=0.05)) as db:
            db.execute("PRAGMA trusted_schema=OFF")
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 500)
            db.execute("BEGIN")
            result = _tool_counts(db, names, now)
        if checkpoint and not _checkpoint_unchanged(path, before, sidecars):
            return {"status": "snapshot_changed"}, {}
        return result
    except (OSError, sqlite3.Error):
        return {"status": "unavailable"}, {}


def _findings(plugins, skills, memory):
    result = []
    for key, count, severity, recommendation in (
        ("duplicate_skills_exact", skills["identical_group_count"], "info", "Revise as cópias byte a byte entre raízes e preserve a precedência configurada."),
        ("duplicate_skill_bodies", skills["body_duplicate_count"], "info", "Compare versões com o mesmo corpo e metadados diferentes antes de consolidar."),
        ("duplicate_skill_names", skills["name_collision_count"], "warning", "Confira a precedência e a origem das skills com o mesmo nome e conteúdo diferente."),
        ("native_memory_redundancy", memory["cross_file_duplicates"] + sum(item.get("duplicates", 0) for item in memory["files"].values()),
         "info", "Revise os blocos repetidos pelo comando nativo de memória e mantenha a separação entre ambiente e perfil de usuário."),
        ("native_memory_review", sum(item.get("at_capacity", False) or item["status"] == "invalid_utf8" for item in memory["files"].values()),
         "warning", "Compare o tamanho e a codificação dos arquivos de memória com os limites do perfil."),
    ):
        if count:
            result.append({"id": key, "severity": severity, "recommendation": recommendation, "evidence": {"count": count}})
    candidates = [p for p in plugins["entries"] if p["origin"] == "profile" and p["state"] == "explicitly_disabled"
                  and (p["allocated_bytes"] if p["allocated_bytes"] is not None else p["bytes"]) >= 32 * _MIB]
    if candidates:
        result.append({"id": "large_disabled_plugins", "severity": "info",
                       "recommendation": "Revise as dependências e a possibilidade de arquivar os plugins explicitamente desabilitados.",
                       "evidence": {"count": len(candidates), "plugins": [p["name"] for p in candidates[:12]]}})
    return result


def scan_inventory(home: Path, *, source: Path | None = None, connection: sqlite3.Connection | None = None,
                   enabled_plugins=None, disabled_plugins=None, memory_limits=None, now: float | None = None) -> dict:
    """Inspect only explicit profile/bundled roots; return metadata and aggregate usage signals."""
    home = Path(home)
    now = time.time() if now is None else now
    roots = [("profile", home)]
    if source is not None and Path(source) != home:
        roots.append(("bundled", Path(source)))
    enabled = {name for name in enabled_plugins if isinstance(name, str)} if enabled_plugins is not None else None
    disabled = {name for name in (disabled_plugins or ()) if isinstance(name, str)}
    plugins = _plugins([(origin, base / "plugins") for origin, base in roots], enabled, disabled)
    tools = Counter(tool for plugin in plugins["entries"] for tool in plugin["tools"])
    declared_unique = sorted(tool for tool, count in tools.items() if count == 1)[:256]
    usage, counts = _usage_sample(home, connection, declared_unique, now)
    if usage["status"] == "sampled":
        selected = set(declared_unique)
        for plugin in plugins["entries"]:
            names = selected.intersection(plugin["tools"])
            if names:
                plugin["recent_matches"] = sum(counts.get(name, 0) for name in names)
    plugins["usage"] = usage
    skills = _skills([(origin, base / "skills") for origin, base in roots])
    skills["curator_usage"] = _skill_usage(home, now)
    memory = _memory(home, memory_limits)
    return {"plugins": plugins, "skills": skills, "native_memory": memory,
            "findings": _findings(plugins, skills, memory)}
