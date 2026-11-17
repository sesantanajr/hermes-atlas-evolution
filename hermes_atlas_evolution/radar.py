"""Radar: what slows Hermes down or wastes its context, read from Hermes' own records.

Every reading is read-only and bounded, and every finding names the native command to inspect it, so the
operator decides what to change; a change to a tracked setting is then measured before and after by the
experiments module. Readings:

- sessions that end as orphans (reaped after a disconnect or a restart) instead of being closed, so the
  end-of-session work of Hermes and its memory never runs for them;
- the context the agent sends per model call, and how much of it the provider served from cache;
- compactions that keep most of the context (they cost a model call and buy little room);
- plugins of this process that failed to load, register hooks they do not declare, or compete for a
  transform hook where only the first plugin's result counts.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

RADAR_DAYS = 7
ORPHAN_ENDS = ("ws_orphan_reap", "startup_orphan_reap")
MIN_ENDED_SESSIONS = 10
ORPHAN_SHARE = 0.3
MIN_CALLS = 50
HEAVY_PROMPT_TOKENS = 60_000
MIN_COMPACTIONS = 5
WEAK_COMPACTION_KEPT = 0.75
FIRST_WINS_HOOKS = ("transform_tool_result", "transform_terminal_output", "transform_llm_output")
_ERROR_NAME = re.compile(r"[A-Za-z_][\w.]{0,60}")


def _history(home: Path) -> sqlite3.Connection | None:
    path = Path(home) / "state.db"
    if not path.is_file():
        return None
    try:
        source = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return None
    try:
        source.execute("SELECT 1 FROM sqlite_master LIMIT 1")
    except sqlite3.Error:   # opens, but is not a database it can read: closed here, not left to the collector
        source.close()
        return None
    return source


def _columns(source, table: str) -> set[str]:
    return {row[1] for row in source.execute(f"PRAGMA table_info({table})")}


def sessions(source, *, now: float, days: int = RADAR_DAYS) -> dict | None:
    """How the owner's sessions of the window ended. Subagent sessions are left out."""
    columns = _columns(source, "sessions")
    if not {"started_at", "end_reason"} <= columns:
        return None
    parent = "AND (parent_session_id IS NULL OR parent_session_id = '')" if "parent_session_id" in columns else ""
    rows = source.execute(f"""SELECT end_reason, COUNT(*) FROM sessions WHERE started_at >= ? {parent}
        GROUP BY end_reason""", (now - days * 86_400,)).fetchall()
    from .experience import label

    by_reason: dict = {}
    for reason, count in rows:
        if reason:   # Hermes' own reason codes; anything unexpected is stored as a safe label
            key = label(str(reason)[:96], fallback="other")
            by_reason[key] = by_reason.get(key, 0) + count
    ended = sum(by_reason.values())
    orphaned = sum(by_reason.get(reason, 0) for reason in ORPHAN_ENDS)
    return {"ended": ended, "orphaned": orphaned, "open": sum(count for reason, count in rows if not reason),
            "orphan_pct": round(100 * orphaned / ended, 1) if ended else None, "by_reason": by_reason}


def context(source, *, now: float, days: int = RADAR_DAYS) -> dict | None:
    """Prompt tokens per model call of the agent itself (auxiliary tasks excluded), and the cached share."""
    columns = _columns(source, "session_model_usage")
    need = {"task", "api_call_count", "input_tokens", "cache_read_tokens", "last_seen"}
    if not need <= columns:
        return None
    write = "cache_write_tokens" if "cache_write_tokens" in columns else "0"
    row = source.execute(f"""SELECT SUM(api_call_count), SUM(input_tokens), SUM(cache_read_tokens), SUM({write}),
          COUNT(DISTINCT session_id) FROM session_model_usage
        WHERE (task IS NULL OR task IN ('', 'main')) AND last_seen >= ?""", (now - days * 86_400,)).fetchone()
    calls, fresh, cached, written, count = (value or 0 for value in row)
    prompt = fresh + cached + written
    if not calls:
        return {"calls": 0, "sessions": 0, "avg_prompt_tokens": None, "cache_pct": None}
    return {"calls": calls, "sessions": count, "avg_prompt_tokens": round(prompt / calls),
            "cache_pct": round(100 * cached / prompt, 1) if prompt else None}


def compaction(conn, *, now: float, days: int = RADAR_DAYS) -> dict:
    from .experience import day_offset, utc_day

    row = conn.execute("""SELECT SUM(measured), SUM(tokens_before), SUM(tokens_after) FROM native_activity
        WHERE hook='post_compaction' AND day BETWEEN ? AND ?""", (day_offset(now, days - 1), utc_day(now))).fetchone()
    measured, before, after = (value or 0 for value in row)
    return {"compactions": measured, "tokens_before": before, "tokens_after": after,
            "kept_pct": round(100 * after / before, 1) if before else None}


def _declared_hooks(folders) -> dict[str, list[str]]:
    """provides_hooks of each plugin manifest found in `folders`, by plugin name. Bounded reads."""
    found = {}
    for folder in folders:
        try:
            entries = sorted(Path(folder).iterdir()) if folder else []
        except OSError:
            continue
        for entry in entries[:500]:
            manifest = entry / "plugin.yaml"
            try:
                if not manifest.is_file() or manifest.stat().st_size > 65_536:
                    continue
                text = manifest.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            name, hooks, current = entry.name, [], None
            for line in text.splitlines():
                if line.startswith("name:"):
                    name = line.split(":", 1)[1].strip().strip("'\"") or name
                if line and not line.startswith((" ", "#", "-")):
                    current = line.split(":", 1)[0].strip()
                elif current == "provides_hooks" and line.strip().startswith("- "):
                    hooks.append(line.strip()[2:].strip().strip("'\""))
            found[name] = hooks
    return found


_SELF = "hermes-atlas-evolution"


def _own_home(home: Path) -> bool:
    """Whether `home` is the profile Hermes resolves in this thread (True outside Hermes)."""
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        return True
    try:
        return Path(get_hermes_home()).resolve() == Path(home).resolve()
    except Exception:
        return True


def plugins(home: Path, *, own_transform: bool = True) -> dict | None:
    """Plugins loaded by this process for the profile. None when Hermes' plugin manager is not loaded here.

    `own_transform` False: this plugin's transform_tool_result returns nothing (tool_hints off), so it never
    competes with another plugin's transform and is left out of the overlaps."""
    module = sys.modules.get("hermes_cli.plugins")
    manager = getattr(module, "get_plugin_manager", None) if module is not None else None
    if not callable(manager):
        return None
    try:
        rows = [row for row in manager().list_plugins() if isinstance(row, dict)]
    except Exception:   # a manager in the middle of a reload: try again on the next pass
        return None
    try:
        bundled = module.get_bundled_plugins_dir()
    except Exception:
        bundled = None
    declared = _declared_hooks([bundled, Path(home) / "plugins"])
    from .experience import label

    enabled = sorted((row for row in rows if row.get("enabled") is True), key=lambda row: str(row.get("key") or ""))
    errors, undeclared, overlaps = [], [], {}
    for row in enabled:
        key = label(str(row.get("key") or row.get("name") or "")[:96], fallback="plugin")
        if row.get("error"):
            match = _ERROR_NAME.match(str(row["error"]))
            errors.append({"plugin": key, "error": label(match.group(0), fallback="error") if match else "error"})
        hooks = declared.get(str(row.get("name") or key))
        registered = row.get("hooks")
        if hooks is not None and type(registered) is int and registered > len(hooks):
            undeclared.append({"plugin": key, "registered": registered, "declared": len(hooks)})
        for hook in FIRST_WINS_HOOKS:
            if hooks and hook in hooks and not (hook == "transform_tool_result" and not own_transform
                                                and str(row.get("name") or key) == _SELF):
                overlaps.setdefault(hook, []).append(key)
    return {"enabled": len(enabled), "errors": errors, "undeclared": undeclared,
            "overlaps": {hook: names for hook, names in overlaps.items() if len(names) > 1}}


def external(conn, *, now: float, days: int = RADAR_DAYS) -> dict:
    """Tool failures that were not the agent's doing: Hermes' fail-closed veto after another plugin's
    pre_tool_call hung or raised, and commands another plugin rewrote before they failed."""
    from .experience import day_offset, utc_day

    rows = dict(conn.execute("""SELECT f.klass, SUM(d.count) FROM failure_daily d JOIN failure f ON f.signature=d.signature
        WHERE f.klass IN ('plugin_fault', 'rewritten_command') AND d.day BETWEEN ? AND ? GROUP BY f.klass""",
                             (day_offset(now, days - 1), utc_day(now))).fetchall())
    return {"plugin_fault": rows.get("plugin_fault", 0), "rewritten_command": rows.get("rewritten_command", 0)}


MIN_UNATTENDED_REFUSALS = 5


def approvals(conn, *, now: float, days: int = RADAR_DAYS) -> dict:
    """Refused approvals per session origin and tool. A refusal nobody could have answered (a one-shot run, a cron
    job, a subagent, an unattended platform: see signals.approval_setting) is a bottleneck the owner can remove,
    and the Hermes setting that decided it is named."""
    from .experience import day_offset, utc_day

    rows = conn.execute("""SELECT source, tool, setting, SUM(count) FROM approval_daily WHERE day BETWEEN ? AND ?
        GROUP BY source, tool, setting""", (day_offset(now, days - 1), utc_day(now))).fetchall()
    by_origin, by_tool, by_setting = {}, {}, {}
    for source, tool, setting, count in rows:
        by_origin[source] = by_origin.get(source, 0) + count
        if setting:
            by_tool[tool] = by_tool.get(tool, 0) + count
            by_setting[setting] = by_setting.get(setting, 0) + count

    def ranked(counts):
        return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    return {"refused": sum(by_origin.values()), "unattended": sum(by_setting.values()), "by_origin": ranked(by_origin),
            "unattended_by_tool": ranked(by_tool), "by_setting": ranked(by_setting)}


# A tool slower than this on average, over at least this many calls in the window, is worth a look.
SLOW_TOOL_SECONDS = 20
MIN_SLOW_TOOL_CALLS = 5


def _slow_tools(conn, *, now: float, days: int) -> list[dict]:
    from .signals import slow_tools

    return slow_tools(conn, now=now, days=days, limit=5)


def _slow_calls(conn, *, now: float, days: int) -> list[dict]:
    from .signals import slow_calls

    return slow_calls(conn, now=now, days=days, min_calls=MIN_SLOW_TOOL_CALLS, min_mean_s=SLOW_TOOL_SECONDS)


def findings(reading: dict) -> list[dict]:
    """Proposals for the readings that cross their thresholds: {code, kind, risk, subject, evidence, command}."""
    found = []
    lifecycle = reading.get("sessions")
    if lifecycle and lifecycle["ended"] >= MIN_ENDED_SESSIONS and lifecycle["orphaned"] / lifecycle["ended"] >= ORPHAN_SHARE:
        found.append({"code": "sessions_orphaned", "kind": "session_lifecycle", "risk": "low", "subject": "sessions",
                      "evidence": {"ended": lifecycle["ended"], "orphaned": lifecycle["orphaned"],
                                   "orphan_pct": lifecycle["orphan_pct"], "by_reason": lifecycle["by_reason"]},
                      "command": "hermes sessions list"})
    weight = reading.get("context")
    if weight and weight["calls"] >= MIN_CALLS and (weight["avg_prompt_tokens"] or 0) >= HEAVY_PROMPT_TOKENS:
        found.append({"code": "context_heavy", "kind": "context_weight", "risk": "low", "subject": "context",
                      "evidence": {"calls": weight["calls"], "avg_prompt_tokens": weight["avg_prompt_tokens"],
                                   "cache_pct": weight["cache_pct"], "sessions": weight["sessions"],
                                   "candidates": ["skills.auto_load", "compression.proactive_prune_tokens",
                                                  "tool_budget.mcp_result_size_chars"]},
                      "command": "hermes prompt-size"})
    for item in reading.get("slow_calls") or ():   # already at least MIN_SLOW_TOOL_CALLS calls of SLOW_TOOL_SECONDS
        found.append({"code": f"tool_slow:{item['tool']}", "kind": "tool_latency", "risk": "low", "subject": item["tool"],
                      "evidence": {"calls": item["calls"], "mean_s": item["mean_s"], "total_min": item["total_min"]},
                      "command": "hermes evolution report --days 7"})
    refusals = reading.get("approvals") or {}
    if refusals.get("unattended", 0) >= MIN_UNATTENDED_REFUSALS:
        top = next(iter(refusals["by_setting"]), "approvals")
        found.append({"code": "approval_unattended", "kind": "approval_policy", "risk": "low", "subject": "approvals",
                      "evidence": {"refused": refusals["refused"], "unattended": refusals["unattended"],
                                   "by_origin": refusals["by_origin"], "unattended_by_tool": refusals["unattended_by_tool"],
                                   "by_setting": refusals["by_setting"]},
                      "command": f"hermes config get {top.split('.', 1)[0]}"})
    squeeze = reading.get("compaction") or {}
    if squeeze.get("compactions", 0) >= MIN_COMPACTIONS and (squeeze.get("kept_pct") or 0) >= 100 * WEAK_COMPACTION_KEPT:
        found.append({"code": "compaction_weak", "kind": "compaction", "risk": "low", "subject": "compression",
                      "evidence": {"compactions": squeeze["compactions"], "kept_pct": squeeze["kept_pct"],
                                   "tokens_before": squeeze["tokens_before"], "tokens_after": squeeze["tokens_after"]},
                      "command": "hermes config get compression"})
    outside = reading.get("external") or {}
    if outside.get("plugin_fault", 0) >= 3:
        found.append({"code": "plugin_guard_faults", "kind": "plugin_health", "risk": "medium", "subject": "pre_tool_call",
                      "evidence": {"blocked_calls": outside["plugin_fault"]}, "command": "hermes plugins list"})
    if outside.get("rewritten_command", 0) >= 3:
        found.append({"code": "rewritten_command_failures", "kind": "plugin_conflict", "risk": "medium",
                      "subject": "pre_tool_call", "evidence": {"failed_calls": outside["rewritten_command"]},
                      "command": "hermes plugins list"})
    loaded = reading.get("plugins") or {}
    for item in loaded.get("errors", ()):
        found.append({"code": f"plugin_error:{item['plugin']}", "kind": "plugin_health", "risk": "low",
                      "subject": item["plugin"], "evidence": {"error": item["error"]},
                      "command": f"hermes plugins doctor {item['plugin']}"})
    for item in loaded.get("undeclared", ()):
        found.append({"code": f"plugin_undeclared_hooks:{item['plugin']}", "kind": "plugin_health", "risk": "low",
                      "subject": item["plugin"], "evidence": {"registered": item["registered"], "declared": item["declared"]},
                      "command": f"hermes plugins doctor {item['plugin']}"})
    # Plugins sharing a transform hook are no finding by themselves (0.8.5): Hermes keeps the first result of each
    # call, so they conflict only on the same tool. environment.findings reads which tools each one changes, in every
    # profile, and reports only a real overlap, with who wins and what to turn off.
    return found


def scan(home: Path, conn, *, now: float, days: int = RADAR_DAYS, own_transform: bool = True) -> dict:
    """All readings and their findings. The plugin snapshot of the last process that could take one is kept."""
    reading = {"sessions": None, "context": None, "compaction": compaction(conn, now=now, days=days),
               "external": external(conn, now=now, days=days), "approvals": approvals(conn, now=now, days=days),
               "slow_tools": _slow_tools(conn, now=now, days=days), "slow_calls": _slow_calls(conn, now=now, days=days)}
    source = _history(home)
    if source is not None:
        try:
            reading["sessions"] = sessions(source, now=now, days=days)
            reading["context"] = context(source, now=now, days=days)
        except sqlite3.Error:
            pass
        finally:
            source.close()
    # The plugin manager loaded here is this process's own profile's: the dashboard reading another profile (or a
    # gateway serving several) must neither show it for that profile nor save it as that profile's snapshot.
    reading["plugins"] = plugins(home, own_transform=own_transform) if _own_home(home) else None
    if reading["plugins"] is not None:
        conn.execute("INSERT OR REPLACE INTO cursor VALUES ('radar_plugins', ?)", (json.dumps(reading["plugins"]),))
    else:
        saved = conn.execute("SELECT value FROM cursor WHERE key='radar_plugins'").fetchone()
        try:
            reading["plugins_snapshot"] = json.loads(saved[0]) if saved else None
        except ValueError:
            reading["plugins_snapshot"] = None
    reading["findings"] = findings({**reading, "plugins": reading["plugins"] or reading.get("plugins_snapshot")})
    return reading
