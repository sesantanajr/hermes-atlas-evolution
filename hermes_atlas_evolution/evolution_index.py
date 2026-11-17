"""The Evolution Index (0.9.2): is this agent getting better? One number from 0 to 100, per profile, per day.

It is built only from what the plugin already measures, over the last WINDOW_DAYS days, as five parts (each 0-100,
higher is better), each counted only with enough data behind it:

- tools: the share of tool calls that did not fail;
- turns: interactive turns that neither failed nor ran slow;
- you: how rarely the owner had to correct or ask again (corrections per 100 turns);
- lessons: of the active lessons the plugin could judge, the share that cut their failure by half or more;
- health: Hermes' own health and the server's, from the latest checks (each problem takes points off).

The index is their weighted mean (WEIGHTS), renormalized over the parts that have data; with fewer than two parts
there is no index yet. Each learning pass stores today's value (one entry per UTC day, HISTORY_DAYS kept), so the
trend is today against a week ago: the number to watch after a lesson, a skill or a setting changed.
"""

from __future__ import annotations

import json

from .experience import day_offset, utc_day

WINDOW_DAYS = 7
HISTORY_DAYS = 90
WEIGHTS = {"tools": 0.30, "turns": 0.25, "you": 0.20, "lessons": 0.10, "health": 0.15}
MIN_TOOL_CALLS, MIN_TURNS, MIN_JUDGED = 40, 20, 1
# What stops or endangers the agent costs 25 points; a problem worth fixing soon 8; housekeeping (old tool versions to
# free, lists to refresh, an update waiting) nothing: the index is about how the agent works, not about chores.
_HEALTH_HIGH = frozenset({"gateway_exec_missing", "gateway_crashed", "gateway_restart_loop", "state_db_damaged",
                          "state_db_unreadable", "disk_critical", "tools_in_use_unreferenced", "unit_exec_missing",
                          "unit_restart_loop", "dpkg_interrupted", "apt_broken", "package_manager_stuck"})
_HEALTH_LOW = frozenset({"gateway_heartbeat_stale", "gateway_memory_growth", "disk_low", "disk_filling", "memory_low",
                         "swap_full", "cpu_saturated", "state_db_wal_large", "security_updates", "reboot_required",
                         "gateway_not_at_boot", "unit_not_enabled", "user_units_without_linger"})
KEY = "evolution_index"


def _clamp(value: float) -> int:
    return int(round(max(0.0, min(100.0, value))))


def parts(conn, *, now: float, care: dict | None = None, system: dict | None = None) -> dict:
    """{part: score or None} over the last WINDOW_DAYS days, with the sample behind each part."""
    from .experiments import _INTERACTIVE

    start, end = day_offset(now, WINDOW_DAYS - 1), utc_day(now)
    marks = ",".join("?" * len(_INTERACTIVE))
    turns, failed, slow = conn.execute(f"""SELECT COALESCE(SUM(turns),0), COALESCE(SUM(failed),0), COALESCE(SUM(slow),0)
        FROM turn_daily WHERE day BETWEEN ? AND ? AND surface IN ({marks})""", (start, end, *_INTERACTIVE)).fetchone()
    calls, errors = conn.execute("""SELECT COALESCE(SUM(calls),0), COALESCE(SUM(errors),0) FROM tool_daily
        WHERE day BETWEEN ? AND ?""", (start, end)).fetchone()
    corrections = conn.execute("""SELECT COALESCE(SUM(count),0) FROM feedback_daily WHERE day BETWEEN ? AND ?
        AND kind IN ('correction','reask')""", (start, end)).fetchone()[0]
    judged = dict(conn.execute("""SELECT verdict, COUNT(*) FROM lesson WHERE status='active'
        AND verdict IN ('effective','ineffective') GROUP BY verdict""").fetchall())
    found = {"tools": None, "turns": None, "you": None, "lessons": None, "health": None,
             "sample": {"tool_calls": calls, "turns": turns, "lessons_judged": sum(judged.values())}}
    if calls >= MIN_TOOL_CALLS:
        # 10% of calls failing is 75, 40% is 0: some failures are normal work (a search with no match, a path
        # tried), so the scale is about the agent's waste, not perfection
        found["tools"] = _clamp(100 - 2.5 * (100.0 * errors / calls))
    if turns >= MIN_TURNS:
        found["turns"] = _clamp(100 - 2.5 * (100.0 * failed / turns) - 1.0 * (100.0 * slow / turns))
        found["you"] = _clamp(100 - 3 * (100.0 * corrections / turns))   # 10 per 100 turns is 70
    if sum(judged.values()) >= MIN_JUDGED:
        found["lessons"] = _clamp(100.0 * judged.get("effective", 0) / sum(judged.values()))
    codes = []
    for check in (care, system):
        if isinstance(check, dict) and isinstance(check.get("findings"), list):
            codes += [str(code) for code in check["findings"]]
    if care is not None or system is not None:
        high = sum(1 for code in codes if code in _HEALTH_HIGH)
        low = sum(1 for code in codes if code in _HEALTH_LOW)
        found["health"] = _clamp(100 - 25 * high - 8 * low)
    return found


def score(found: dict) -> int | None:
    """The weighted mean of the parts that have data; None with fewer than two."""
    present = {name: value for name, value in found.items() if name in WEIGHTS and value is not None}
    if len(present) < 2:
        return None
    weight = sum(WEIGHTS[name] for name in present)
    return _clamp(sum(WEIGHTS[name] * value for name, value in present.items()) / weight)


def history(conn) -> list[dict]:
    row = conn.execute("SELECT value FROM cursor WHERE key=?", (KEY,)).fetchone()
    try:
        found = json.loads(row[0]) if row else []
    except (TypeError, ValueError):
        return []
    return [item for item in found if isinstance(item, dict) and isinstance(item.get("day"), str)] \
        if isinstance(found, list) else []


def _checks(conn) -> tuple[dict | None, dict | None]:
    rows = dict(conn.execute("SELECT key, value FROM cursor WHERE key IN ('care_check', 'system_check')").fetchall())
    found = []
    for key in ("care_check", "system_check"):
        try:
            value = json.loads(rows[key]) if key in rows else None
        except (TypeError, ValueError):
            value = None
        found.append(value if isinstance(value, dict) else None)
    return found[0], found[1]


def update(conn, *, now: float) -> dict:
    """Today's index, stored (one entry per UTC day, the latest of the day wins), with its trend."""
    care, system = _checks(conn)
    found = parts(conn, now=now, care=care, system=system)
    value = score(found)
    today = utc_day(now)
    kept = [item for item in history(conn) if item["day"] != today and item["day"] >= day_offset(now, HISTORY_DAYS - 1)]
    if value is not None:
        kept.append({"day": today, "score": value, "parts": {name: found[name] for name in WEIGHTS}})
    kept.sort(key=lambda item: item["day"])
    conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES (?, ?)", (KEY, json.dumps(kept[-HISTORY_DAYS:])))
    return view(kept, now=now, current=found, value=value)


def current(conn, *, now: float) -> dict:
    """The index as stored, with today's parts computed again (read only: `status` and the panel)."""
    care, system = _checks(conn)
    found = parts(conn, now=now, care=care, system=system)
    return view(history(conn), now=now, current=found, value=score(found))


def view(kept: list[dict], *, now: float, current: dict, value: int | None) -> dict:
    """{score, parts, trend_7d, series}: the trend is today against the latest stored day at least 7 days back."""
    week_ago = day_offset(now, 7)
    before = [item for item in kept if item["day"] <= week_ago and isinstance(item.get("score"), int)]
    trend = value - before[-1]["score"] if value is not None and before else None
    return {"score": value, "parts": {name: current.get(name) for name in WEIGHTS}, "trend_7d": trend,
            "sample": current.get("sample") or {},
            "series": [{"day": item["day"], "score": item["score"]} for item in kept[-30:]
                       if isinstance(item.get("score"), int)]}


def sparkline(series: list[dict]) -> str:
    """The last scores as a line of block characters (▁▂▃▄▅▆▇█), oldest first."""
    blocks = "▁▂▃▄▅▆▇█"
    return "".join(blocks[min(7, int(item["score"] / 12.5))] for item in series[-14:])
