"""What exists around Hermes (0.8.5): Hermes' own plugin catalog and the plugin's knowledge base, in its database.

- Hermes' plugin catalog: the curated list Hermes ships and refreshes by itself (the copy Hermes caches in
  `<profile>/cache/plugin-catalog.json`, else the one in its source, `plugin-catalog/*.yaml`), with three tiers
  (official, verified, community) and a kill list of plugins removed for security reasons. Read only: the plugin
  opens no connection; Hermes refreshes the copy when its plugin hub is used.
- The knowledge base shipped with the plugin (`knowledge/plugins.json`, `knowledge/playbook.json`): roles that only
  one plugin of a profile should fill, what well-known plugins change, and the playbook: every situation the plugin
  recognises, how much it may do by itself in each mode, and the action.

`sync` copies both into the tables catalog_plugin, catalog_removed, knowledge_plugin and playbook of the database of
the profile that looks after the whole Hermes (environment.lead), so the menu, the panel and any sqlite reader see
the same data.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

TIERS = ("official", "verified", "community")
CACHE_LIMIT = 32 * 1024 * 1024
CATALOG_MAX = 10_000
IN_TREE_MAX = 4_000
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,95}")
_SHA = re.compile(r"[0-9a-f]{40}")
KNOWLEDGE = Path(__file__).resolve().parent / "knowledge"


def _text(value, limit: int) -> str:
    return " ".join(str(value).split())[:limit] if isinstance(value, (str, int, float)) else ""


def _strings(value, limit: int = 64) -> list[str]:
    return [item[:96] for item in value if isinstance(item, str) and item][:limit] if isinstance(value, list) else []


def normalize_repo(url) -> str:
    """A git URL as one comparable text: scheme, user, `.git` and case dropped (`github.com/owner/name`)."""
    if not isinstance(url, str):
        return ""
    text = url.strip().lower()
    text = re.sub(r"^(git\+)?(https?|ssh|git)://", "", text)
    text = re.sub(r"^[^@/]+@", "", text).replace(":", "/", 1) if "@" in text.split("/", 1)[0] else text
    text = text.split("#", 1)[0].rstrip("/")
    return text[:-4] if text.endswith(".git") else text


def _entry(raw) -> dict | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str) or not _NAME.fullmatch(raw["name"]):
        return None
    caps = raw.get("capabilities") if isinstance(raw.get("capabilities"), dict) else {}
    tier = raw.get("tier") if raw.get("tier") in TIERS else "community"
    sha = raw.get("sha") if isinstance(raw.get("sha"), str) and _SHA.fullmatch(raw["sha"]) else ""
    return {"name": raw["name"], "tier": tier, "category": _text(raw.get("category"), 40) or "general",
            "repo": _text(raw.get("repo"), 300), "sha": sha, "version": _text(raw.get("version"), 40),
            "maintainer": _text(raw.get("maintainer"), 80), "description": _text(raw.get("description"), 600),
            "hooks": _strings(caps.get("provides_hooks")), "tools": _strings(caps.get("provides_tools")),
            "known_issues": [_text(item, 300) for item in (raw.get("known_issues") if isinstance(raw.get("known_issues"), list) else [])[:5]
                             if _text(item, 300)]}


def _removed(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    name, repo = _text(raw.get("name"), 96), _text(raw.get("repo"), 300)
    if not name and not repo:
        return None
    return {"name": name, "repo": repo, "reason": _text(raw.get("reason"), 300)}


def hermes_source() -> Path | None:
    """The folder of Hermes' source (where its catalog lives): the loaded `hermes_cli` package, or a usual place."""
    module = sys.modules.get("hermes_cli")
    candidates = [Path(module.__file__).resolve().parent.parent] if getattr(module, "__file__", None) else []
    candidates += [Path(item) for item in (os.environ.get("HERMES_AGENT_ROOT"), "/usr/local/lib/hermes-agent",
                                           str(Path.home() / ".hermes" / "hermes-agent")) if item]
    for candidate in candidates:
        if (candidate / "plugin-catalog").is_dir():
            return candidate
    return None


def _yaml():
    try:
        import yaml   # Hermes' own environment has it; the standalone runner may not

        return yaml
    except ImportError:
        return None


_CACHE_MEMO: dict = {}


def _cache(home: Path) -> dict | None:
    """The catalog copy Hermes keeps in a profile (`cache/plugin-catalog.json`), read as a regular file only; parsed
    once per version of the file (a scan asks for it once per plugin)."""
    import os

    from .environment import read_text

    path = Path(home) / "cache" / "plugin-catalog.json"
    try:
        info = os.lstat(path)
        key = (str(path), info.st_mtime_ns, info.st_size)
    except OSError:
        return None
    if key in _CACHE_MEMO:
        return _CACHE_MEMO[key]
    text = read_text(path, CACHE_LIMIT)
    try:
        data = json.loads(text) if text else None
    except (ValueError, RecursionError):
        data = None
    data = data if isinstance(data, dict) and isinstance(data.get("entries"), list) else None
    if len(_CACHE_MEMO) > 64:
        _CACHE_MEMO.clear()
    _CACHE_MEMO[key] = data
    return data


def load_catalog(homes, *, source: Path | None = None) -> dict:
    """Hermes' plugin catalog: the copy Hermes cached in the first of `homes` that has one (the lead's own first),
    else the one in its source. Used for what it says about versions and tiers; what Hermes removed for safety is read
    per profile by removed_reason, as Hermes itself does.
    {"source": "cache" | "hermes" | "none", "generated_at", "entries": {name: entry}, "removed": [...]}"""
    best = None
    for home in homes:
        data = _cache(Path(home))
        if data is not None:
            best = (0, data)
            break
    entries, removed, origin, generated = {}, [], "none", None
    if best is not None:
        origin, generated = "cache", _text(best[1].get("generated_at"), 40) or None
        raw_entries, raw_removed = best[1]["entries"], best[1].get("removed") or []
    else:
        raw_entries, raw_removed = [], []
        folder = (source or hermes_source() or Path("/nonexistent")) / "plugin-catalog"
        yaml = _yaml()
        if yaml is not None and folder.is_dir():
            origin = "hermes"
            try:
                files = sorted(folder.glob("*.yaml"))[:IN_TREE_MAX]
            except OSError:
                files = []
            for item in files:
                try:
                    data = yaml.safe_load(item.read_text(encoding="utf-8")[:65_536])
                except (OSError, ValueError, yaml.YAMLError):
                    continue
                if item.name == "removed.yaml":
                    raw_removed = data.get("removed", data) if isinstance(data, dict) else (data or [])
                else:
                    raw_entries.append(data)
    for raw in raw_entries[:CATALOG_MAX]:
        entry = _entry(raw)
        if entry is not None:
            entries[entry["name"]] = entry
    for raw in raw_removed[:CATALOG_MAX] if isinstance(raw_removed, list) else []:
        item = _removed(raw)
        if item is not None:
            removed.append(item)
    return {"source": origin, "generated_at": generated, "entries": entries, "removed": removed}


def knowledge() -> dict:
    """The knowledge base shipped with the plugin: {"plugins": {name: note}, "playbook": {situation: entry}}."""
    found = {"plugins": {}, "playbook": {}, "obsolete": {}}
    for name, key in (("plugins.json", "plugins"), ("playbook.json", "playbook"), ("obsolete.json", "obsolete")):
        try:
            data = json.loads((KNOWLEDGE / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        items = data.get("plugins" if key == "obsolete" else key) if isinstance(data, dict) else None
        if isinstance(items, dict):
            found[key] = {str(k): v for k, v in items.items() if isinstance(v, dict)}
    return found


def lookup(plugin: dict, catalog: dict) -> dict | None:
    """The catalog entry of an installed plugin: by its name, its folder, or its git repository."""
    entries = catalog.get("entries") or {}
    for name in (plugin.get("name"), plugin.get("folder")):
        if isinstance(name, str) and name in entries:
            return entries[name]
    repo = normalize_repo(plugin.get("repo"))
    if repo:
        for entry in entries.values():
            if normalize_repo(entry.get("repo")) == repo:
                return entry
    return None


def _in_tree_removed(source: Path | None) -> list[dict]:
    """`plugin-catalog/removed.yaml` of Hermes' source (`removed:` list of name/repo/reason), with a small reader."""
    from .environment import read_text

    folder = source or hermes_source()
    text = read_text(Path(folder) / "plugin-catalog" / "removed.yaml", 1_000_000) if folder else None
    found, current, inside = [], None, False
    for line in (text or "").splitlines():
        if line.startswith("removed:"):
            inside = True
            continue
        if not inside or not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "-")):
            break
        body = line.strip()
        if body.startswith("- "):
            current = {"name": "", "repo": "", "reason": ""}
            found.append(current)
            body = body[2:].strip()
        key, _, value = body.partition(":")
        if current is not None and key.strip() in ("name", "repo", "reason"):
            current[key.strip()] = _text(value.strip().strip("'\""), 300)
    return [item for item in found if item["name"] or item["repo"]]


def removed_entries(home: Path | None, *, source: Path | None = None) -> list[dict]:
    """What Hermes refuses to load in a profile: the kill list of its source plus the copy cached in that profile
    (the same union Hermes' own `cached_removed_entries` uses there)."""
    entries = list(_in_tree_removed(source))
    data = _cache(Path(home)) if home is not None else None
    for raw in (data or {}).get("removed") or ():
        item = _removed(raw)
        if item is not None:
            entries.append(item)
    return entries


def removed_reason(plugin: dict, catalog: dict, *, home: Path | None = None, source: Path | None = None) -> str | None:
    """Why Hermes removed this plugin from its catalog, when it did: by its manifest name, folder, install source or
    git repository, against the kill list of that profile (`home`), else the one in `catalog`."""
    entries = removed_entries(home, source=source) if home is not None else list(catalog.get("removed") or ())
    names = {plugin.get("name"), plugin.get("folder")} - {None, ""}
    repos = {normalize_repo(plugin.get("repo")), normalize_repo(str(plugin.get("source") or "").split("#", 1)[0])} - {""}
    for item in entries:
        if (item["name"] and item["name"] in names) or (item["repo"] and normalize_repo(item["repo"]) in repos):
            return item["reason"] or "removed from the Hermes catalog"
    return None


def obsolete(plugin: dict, known: dict) -> dict | None:
    """What the knowledge base says of an archived, delisted, missing or unmaintained plugin, matched by name or
    repository."""
    items = known.get("obsolete") or {}
    repos = {normalize_repo(plugin.get("repo")), normalize_repo(str(plugin.get("source") or "").split("#", 1)[0])} - {""}
    for name, item in items.items():
        if not isinstance(item, dict):
            continue
        if item.get("repo"):   # a name can be reused by a maintained plugin: the repository decides
            if normalize_repo(item["repo"]) in repos:
                return {"name": name, **item}
        elif name in (plugin.get("name"), plugin.get("folder")):
            return {"name": name, **item}
    return None


def sync(conn, catalog: dict, known: dict) -> bool:
    """Copy the catalog and the knowledge base into the plugin's database when their source changed. True when it
    wrote."""
    import hashlib

    digest = hashlib.sha256(json.dumps([catalog.get("entries"), catalog.get("removed"), known], sort_keys=True,
                                       default=str).encode()).hexdigest()   # any changed entry or text rewrites
    stamp = json.dumps([digest, catalog.get("source"), catalog.get("generated_at"), len(catalog.get("entries") or {}),
                        len(catalog.get("removed") or ()), sorted(known.get("plugins") or {}),
                        len(known.get("obsolete") or {}),
                        sorted(known.get("playbook") or {}), (known.get("playbook") or {}).get("_version")])
    row = conn.execute("SELECT value FROM cursor WHERE key='ecosystem_sync'").fetchone()
    if row is not None and row[0] == stamp:
        return False
    conn.execute("DELETE FROM catalog_plugin")
    conn.executemany("INSERT OR REPLACE INTO catalog_plugin VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
        (e["name"], e["tier"], e["category"], e["repo"], e["sha"], e["version"], e["maintainer"], e["description"],
         json.dumps(e["hooks"]), json.dumps(e["tools"]), json.dumps(e["known_issues"]))
        for e in (catalog.get("entries") or {}).values()])
    conn.execute("DELETE FROM catalog_removed")
    conn.executemany("INSERT OR REPLACE INTO catalog_removed VALUES (?,?,?)",
                     [(item["name"], item["repo"], item["reason"]) for item in catalog.get("removed") or ()])
    conn.execute("DELETE FROM knowledge_plugin")
    conn.executemany("INSERT OR REPLACE INTO knowledge_plugin VALUES (?,?,?,?,?,?,?)", [
        (name, _text(note.get("role"), 60), 1 if note.get("exclusive") else 0, _text(note.get("summary_en"), 400),
         _text(note.get("summary_pt"), 400), json.dumps(_strings(note.get("transforms"))), _text(note.get("notes"), 600))
        for name, note in (known.get("plugins") or {}).items()])
    conn.execute("DELETE FROM knowledge_obsolete")
    conn.executemany("INSERT OR REPLACE INTO knowledge_obsolete VALUES (?,?,?,?,?)", [
        (name, _text(item.get("repo"), 300), _text(item.get("state"), 20), _text(item.get("basis"), 300),
         json.dumps(_strings(item.get("superseded_by"))))
        for name, item in (known.get("obsolete") or {}).items() if isinstance(item, dict)])
    conn.execute("DELETE FROM playbook")
    conn.executemany("INSERT OR REPLACE INTO playbook VALUES (?,?,?,?,?,?,?,?,?)", [
        (situation, _text(entry.get("level"), 20), _text(entry.get("risk"), 20), _text(entry.get("action"), 300),
         _text(entry.get("undo"), 300), _text(entry.get("title_en"), 200), _text(entry.get("title_pt"), 200),
         _text(entry.get("advice_en"), 800), _text(entry.get("advice_pt"), 800))
        for situation, entry in (known.get("playbook") or {}).items() if not situation.startswith("_")])
    conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('ecosystem_sync', ?)", (stamp,))
    return True
