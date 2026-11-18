"""Insights (0.11.0): why the Evolution index moved, what is failing far more than usual today, and what to do first.

Three readings, from tables the plugin already keeps, with no model call and no new data:

- `moved`: the part of the index that changed the most against the stored day at least a week back, with what drove
  it (the tools whose errors grew or fell, failed or slow turns, your corrections, the health problems).
- `spikes`: a failure that, in the UTC day so far, already reached SPIKE_RATIO times its daily average of the
  BASELINE_DAYS before (and at least SPIKE_MIN times): a dependency, a key, an API or a deploy that just broke, said
  before the weekly numbers can show it.
- `next_actions`: the waiting lessons, the recurring failures that have no lesson and the health problems, ranked by
  the points each could add to the index, estimated with the index's own formula. A lesson that works halves its
  failure (the rule `lessons.evaluate` calls effective), so it may win back half of what that failure costs the
  "tools" part; a health problem fixed gives back what it takes off the "health" part.

Each reading is bounded (a few indexed queries over 7 to 14 days), returns plain numbers and is worded here for
`status`, the digest and the panel. `read` never raises for missing or odd data; database errors reach the caller.
"""

from __future__ import annotations

import json

from .experience import day_offset, utc_day

SPIKE_MIN = 5            # a failure is a spike only from this many occurrences in the day
SPIKE_RATIO = 4.0        # ... and this many times its usual day
BASELINE_DAYS = 14
MOVED_MIN = 5            # a part that moved less than this many points is not a story
TOP = 3
UNTOLD_KEY = "insight_spikes"   # the spikes a scheduled notification already told today


def _window(now: float, offset: int, days: int = 7) -> tuple[str, str]:
    """The `days` UTC days ending `offset` days before today: offset 0 is the index's own window."""
    return day_offset(now, offset + days - 1), day_offset(now, offset)


def _bounded(value: float) -> float:
    return max(0.0, min(100.0, value))


def spikes(conn, *, now: float) -> list[dict]:
    """Failures far above their usual day, today (UTC so far), most occurrences first."""
    today = utc_day(now)
    start, end = day_offset(now, BASELINE_DAYS), day_offset(now, 1)
    rows = conn.execute("""SELECT f.signature, f.tool, f.klass, f.hint, t.count AS today,
          COALESCE((SELECT SUM(d.count) FROM failure_daily d WHERE d.signature=f.signature AND d.day BETWEEN ? AND ?), 0)
            AS before
        FROM failure_daily t JOIN failure f ON f.signature=t.signature
        WHERE t.day=? AND t.count >= ? ORDER BY t.count DESC LIMIT 50""", (start, end, today, SPIKE_MIN)).fetchall()
    found = []
    for row in rows:
        usual = (row["before"] or 0) / BASELINE_DAYS
        if row["today"] >= SPIKE_RATIO * max(usual, 0.5):
            found.append({"tool": row["tool"], "class": row["klass"], "hint": row["hint"], "today": row["today"],
                          "usual_per_day": round(usual, 1), "signature": row["signature"]})
    return found[:TOP]


def untold_spikes(conn, *, now: float) -> list[dict]:
    """Today's spikes that no scheduled notification told yet; marks them told (the caller commits)."""
    found = spikes(conn, now=now)
    today = utc_day(now)
    row = conn.execute("SELECT value FROM cursor WHERE key=?", (UNTOLD_KEY,)).fetchone()
    try:
        told = json.loads(row[0]) if row else {}
    except (TypeError, ValueError):
        told = {}
    told = told if isinstance(told, dict) and told.get("day") == today and isinstance(told.get("signatures"), list) \
        else {"day": today, "signatures": []}
    new = [item for item in found if item["signature"] not in told["signatures"]]
    if new:
        told["signatures"] = (told["signatures"] + [item["signature"] for item in new])[-50:]
        conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES (?, ?)", (UNTOLD_KEY, json.dumps(told)))
    return new


def _health(conn) -> list[dict]:
    """The health problems the index counts, from the stored checks: [{code, level, source}]."""
    from . import evolution_index

    found = []
    for source, check in zip(("hermes", "system"), evolution_index._checks(conn)):
        codes = check.get("findings") if isinstance(check, dict) else None
        for code in codes if isinstance(codes, list) else ():
            if code in evolution_index._HEALTH_HIGH:
                found.append({"code": code, "level": "high", "source": source})
            elif code in evolution_index._HEALTH_LOW:
                found.append({"code": code, "level": "low", "source": source})
    return found


def _sum(conn, sql: str, *values) -> int:
    row = conn.execute(sql, values).fetchone()
    return int(row[0] or 0) if row else 0


def moved(conn, *, now: float, view: dict, history: list[dict], health: list[dict] = ()) -> dict | None:
    """The part that moved the most against the stored day at least 7 days back, with what drove it; None when no
    part moved MOVED_MIN points or more, or when there is nothing to compare with."""
    from .experiments import _INTERACTIVE

    week_ago = day_offset(now, 7)
    before = [item for item in history if isinstance(item, dict) and str(item.get("day", "")) <= week_ago
              and isinstance(item.get("parts"), dict)]
    if not before:
        return None
    old, new = before[-1]["parts"], view.get("parts") or {}
    deltas = {name: new[name] - old[name] for name in new
              if type(new.get(name)) is int and type(old.get(name)) is int}
    if not deltas:
        return None
    part = max(deltas, key=lambda name: (abs(deltas[name]), name))
    if abs(deltas[part]) < MOVED_MIN:
        return None
    worse = deltas[part] < 0
    this, last = _window(now, 0), _window(now, 7)
    drivers: list[dict] = []
    if part == "tools":
        sql = "SELECT tool, SUM(errors) FROM tool_daily WHERE day BETWEEN ? AND ? GROUP BY tool"
        now_errors = {row[0]: int(row[1] or 0) for row in conn.execute(sql, this)}
        then_errors = {row[0]: int(row[1] or 0) for row in conn.execute(sql, last)}
        changes = {tool: now_errors.get(tool, 0) - then_errors.get(tool, 0) for tool in {*now_errors, *then_errors}}
        ranked = sorted((pair for pair in changes.items() if (pair[1] > 0 if worse else pair[1] < 0)),
                        key=lambda pair: (-abs(pair[1]), pair[0]))
        drivers = [{"what": "tool_errors", "name": tool, "change": change} for tool, change in ranked[:TOP]]
    elif part == "turns":
        marks = ",".join("?" * len(_INTERACTIVE))
        for column, what in (("failed", "failed_turns"), ("slow", "slow_turns")):
            sql = f"SELECT SUM({column}) FROM turn_daily WHERE day BETWEEN ? AND ? AND surface IN ({marks})"
            values = {"now": _sum(conn, sql, *this, *_INTERACTIVE), "before": _sum(conn, sql, *last, *_INTERACTIVE)}
            if values["now"] != values["before"]:
                drivers.append({"what": what, **values})
    elif part == "you":
        sql = "SELECT SUM(count) FROM feedback_daily WHERE day BETWEEN ? AND ? AND kind IN ('correction','reask')"
        drivers.append({"what": "corrections", "now": _sum(conn, sql, *this), "before": _sum(conn, sql, *last)})
    elif part == "lessons" and worse:
        count = _sum(conn, "SELECT COUNT(*) FROM lesson WHERE status='active' AND verdict='ineffective'")
        if count:
            drivers.append({"what": "ineffective_lessons", "now": count})
    elif part == "health" and worse:   # what the checks find now; an improvement has nothing left to name
        drivers = [{"what": "health", "name": item["code"]} for item in health][:TOP]
    return {"part": part, "from": old[part], "to": new[part], "delta": deltas[part], "since": before[-1]["day"],
            "drivers": drivers}


def next_actions(conn, *, now: float, view: dict, health: list[dict] = ()) -> list[dict]:
    """Up to TOP things to do first, each with the index points it could add (an estimate, rounded to 0.1)."""
    from .evolution_index import WEIGHTS

    parts = view.get("parts") or {}
    present = [name for name in WEIGHTS if type(parts.get(name)) is int]
    if len(present) < 2:   # no index yet: there are no points to win
        return []
    share = {name: WEIGHTS[name] / sum(WEIGHTS[part] for part in present) for name in present}
    found = []
    if "tools" in present:
        start, end = _window(now, 0)
        calls, errors = conn.execute("""SELECT COALESCE(SUM(calls),0), COALESCE(SUM(errors),0) FROM tool_daily
            WHERE day BETWEEN ? AND ?""", (start, end)).fetchone()
        if calls:
            tools_now = _bounded(100 - 2.5 * (100.0 * errors / calls))
            seen = set()
            for row in conn.execute("""SELECT p.id, p.kind, p.subject, p.code, p.evidence FROM proposal p
                    WHERE p.status='open' AND p.kind IN ('lesson','investigate_failure') ORDER BY p.id LIMIT 200"""):
                signature = None
                if row["kind"] == "lesson":
                    try:
                        evidence = json.loads(row["evidence"]) if row["evidence"] else {}
                    except (TypeError, ValueError):
                        evidence = {}
                    lesson_id = evidence.get("lesson_id") if isinstance(evidence, dict) else None
                    if type(lesson_id) is int:
                        lesson = conn.execute("SELECT signature FROM lesson WHERE id=?", (lesson_id,)).fetchone()
                        signature = lesson[0] if lesson else None
                    command = "hermes evolution lessons review"
                elif str(row["code"]).startswith("investigate:"):
                    signature = str(row["code"]).split(":", 1)[1]
                    command = "hermes evolution proposals list"
                if not signature or signature in seen:
                    continue
                seen.add(signature)
                failures = min(errors, _sum(conn, """SELECT SUM(count) FROM failure_daily WHERE signature=?
                    AND day BETWEEN ? AND ?""", signature, start, end))
                tools_after = _bounded(100 - 2.5 * (100.0 * (errors - 0.5 * failures) / calls))
                gain = (tools_after - tools_now) * share["tools"]
                if gain >= 0.1:
                    found.append({"kind": row["kind"], "subject": row["subject"], "proposal": row["id"],
                                  "gain": round(gain, 1), "command": command})
    if "health" in present and health:
        high = sum(1 for item in health if item["level"] == "high")
        low = len(health) - high
        health_now = _bounded(100 - 25 * high - 8 * low)
        for item in health:
            fixed = _bounded(100 - 25 * (high - (item["level"] == "high")) - 8 * (low - (item["level"] == "low")))
            gain = (fixed - health_now) * share["health"]
            if gain >= 0.1:
                found.append({"kind": "health", "subject": item["code"], "gain": round(gain, 1),
                              "command": "hermes evolution care check" if item["source"] == "hermes"
                              else "hermes evolution system check"})
    found.sort(key=lambda item: (-item["gain"], item["kind"], str(item["subject"])))
    return found[:TOP]


def read(conn, *, now: float, view: dict | None = None) -> dict:
    """All three readings, for `status`, the digest and the panel."""
    from . import evolution_index

    view = view if isinstance(view, dict) else evolution_index.current(conn, now=now)
    health = _health(conn)
    return {"moved": moved(conn, now=now, view=view, history=evolution_index.history(conn), health=health),
            "spikes": spikes(conn, now=now),
            "next_actions": next_actions(conn, now=now, view=view, health=health)}


_TEXT = {
    "en": {"parts": {"tools": "tools", "turns": "turns", "you": "you", "lessons": "lessons", "health": "health"},
           "moved": "What moved it: {part} {old} → {new}",
           "tool_errors": "`{name}` {change} errors", "failed_turns": "failed turns {before} → {now}",
           "slow_turns": "slow turns {before} → {now}", "corrections": "corrections and re-asks {before} → {now}",
           "ineffective_lessons": ("{now} active lesson did not help", "{now} active lessons did not help"),
           "health": "`{name}`",
           "spike": "A failure jumped today: something the agent depends on may have just changed (an update, a key, "
                    "a service, a quota).",
           "spike_item": "`{tool}` · {words}: {today} times today, about {usual} a day in the {days} days before",
           "spike_title": "A failure jumped today",
           "do_first": "Do first",
           "do_first_note": "The points are an estimate of what each step could add to the Evolution index.",
           "lesson": "Decide on the lesson waiting for `{subject}`",
           "investigate_failure": "Write a lesson for the recurring failure of `{subject}`",
           "health_item": "Fix `{subject}` (see Needs attention)",
           "gain": "about +{gain} points"},
    "pt": {"parts": {"tools": "ferramentas", "turns": "turnos", "you": "você", "lessons": "lições", "health": "saúde"},
           "moved": "O que o moveu: {part} {old} → {new}",
           "tool_errors": "`{name}` {change} erros", "failed_turns": "turnos com falha {before} → {now}",
           "slow_turns": "turnos lentos {before} → {now}",
           "corrections": "correções e pedidos repetidos {before} → {now}",
           "ineffective_lessons": ("{now} lição ativa não ajudou", "{now} lições ativas não ajudaram"),
           "health": "`{name}`",
           "spike": "Uma falha disparou hoje: algo de que o agente depende pode ter acabado de mudar (uma atualização, "
                    "uma chave, um serviço, uma cota).",
           "spike_item": "`{tool}` · {words}: {today} vezes hoje, cerca de {usual} por dia nos {days} dias anteriores",
           "spike_title": "Uma falha disparou hoje",
           "do_first": "Faça primeiro",
           "do_first_note": "Os pontos são uma estimativa do que cada passo pode somar ao Índice de evolução.",
           "lesson": "Decida sobre a lição que aguarda para `{subject}`",
           "investigate_failure": "Escreva uma lição para a falha recorrente de `{subject}`",
           "health_item": "Corrija `{subject}` (veja Pede atenção)",
           "gain": "cerca de +{gain} pontos"},
}


def _number(value, lang: str) -> str:
    text = f"{value:g}" if isinstance(value, float) else str(value)
    return text.replace(".", ",") if lang == "pt" else text


def moved_words(found: dict | None, lang: str = "en") -> str | None:
    """One line: the part that moved, from and to, and what drove it."""
    if not isinstance(found, dict):
        return None
    t = _TEXT.get(lang, _TEXT["en"])
    line = t["moved"].format(part=t["parts"].get(found["part"], found["part"]), old=found["from"], new=found["to"])
    said = []
    for item in found.get("drivers") or ():
        what = item.get("what")
        if what == "tool_errors":
            said.append(t[what].format(name=item["name"], change=f"{item['change']:+d}"))
        elif what in ("failed_turns", "slow_turns", "corrections"):
            said.append(t[what].format(before=item["before"], now=item["now"]))
        elif what == "ineffective_lessons":
            one, many = t[what]
            said.append((one if item["now"] == 1 else many).format(now=item["now"]))
        elif what == "health":
            said.append(t[what].format(name=item["name"]))
    return line + (" · " + " · ".join(said) if said else "")


def spike_items(found: list[dict], lang: str = "en") -> list[str]:
    """One sentence per spike: the tool, the failure in words, today against its usual day."""
    from .report import failure_words

    t = _TEXT.get(lang, _TEXT["en"])
    return [t["spike_item"].format(tool=item["tool"], words=failure_words(item["class"], item["hint"], lang),
                                   today=item["today"], usual=_number(item["usual_per_day"], lang), days=BASELINE_DAYS)
            for item in found or () if isinstance(item, dict)]


def spike_lines(found: list[dict], lang: str = "en") -> list[str]:
    """The heading sentence and one sub-item per spike, as Markdown list lines (empty with no spike)."""
    items = spike_items(found, lang)
    if not items:
        return []
    return ["- " + _TEXT.get(lang, _TEXT["en"])["spike"]] + [f"  - {item}" for item in items]


def action_lines(found: list[dict], lang: str = "en") -> list[str]:
    """The steps of "Do first", most points first, then the note about the points (empty with nothing to do)."""
    if not found:
        return []
    t = _TEXT.get(lang, _TEXT["en"])
    lines = []
    for item in found:
        key = "health_item" if item["kind"] == "health" else item["kind"]
        lines.append("- " + t[key].format(subject=item["subject"]) + " · "
                     + t["gain"].format(gain=_number(item["gain"], lang)) + f" · `{item['command']}`")
    return lines + ["", t["do_first_note"]]


def panel_markdown(found: dict | None, lang: str = "en") -> str:
    """The three readings as one Markdown block for the panel's overview; empty when there is nothing to say."""
    if not isinstance(found, dict):
        return ""
    blocks = []
    story = moved_words(found.get("moved"), lang)
    if story:
        blocks.append(story)
    if found.get("spikes"):
        blocks.append("\n".join(spike_lines(found["spikes"], lang)))
    if found.get("next_actions"):
        blocks.append("\n".join(action_lines(found["next_actions"], lang)))
    return "\n\n".join(blocks)
