"""`hermes evolution status`: is the plugin observing, learning and protecting this profile right now?

It reads only the plugin's own store and, for the other profiles of the same Hermes root, how many sessions
they had (read-only), so it can point out a busy profile that does not have the plugin: one gateway serves
many profiles, but each profile loads its own plugins.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .experience import experience, utc_day

REPOSITORY = "sesantanajr/hermes-atlas-evolution"
PLUGIN_DIR = "hermes-evolution"


def plugin_version() -> str:
    manifest = Path(__file__).resolve().parent.parent / "plugin.yaml"
    try:
        match = re.search(r'^version:\s*"?([\d.]+)"?', manifest.read_text(encoding="utf-8"), re.M)
    except OSError:
        match = None
    return match.group(1) if match else "unknown"


def _iso(stamp) -> str | None:
    try:
        return datetime.fromtimestamp(float(stamp), timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _sessions(home: Path, *, since: float) -> int | None:
    path = home / "state.db"
    if not path.is_file():
        return None
    try:
        source = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=1)
        try:
            return source.execute("SELECT COUNT(*) FROM sessions WHERE started_at >= ?", (since,)).fetchone()[0]
        finally:
            source.close()
    except sqlite3.Error:
        return None


def _profiles(home: Path, *, now: float) -> list[dict]:
    """Every other profile of the same Hermes root that had sessions in 7 days and lacks the plugin."""
    home = Path(home).resolve()
    root = home.parent.parent if home.parent.name == "profiles" else home
    candidates = [("default", root)]
    try:
        candidates += [(entry.name, entry) for entry in sorted((root / "profiles").iterdir()) if entry.is_dir()][:200]
    except OSError:
        pass
    found = []
    for name, folder in candidates:
        if folder.resolve() == home or (folder / "plugins" / PLUGIN_DIR).is_dir():
            continue
        count = _sessions(folder, since=now - 7 * 86_400)
        if count:
            found.append({"profile": name, "sessions_7d": count,
                          "install": f"hermes {'' if name == 'default' else f'-p {name} '}plugins install {REPOSITORY} --enable"})
    return sorted(found, key=lambda item: -item["sessions_7d"])


def build(home: Path, *, now: float | None = None, config=None, runtime_config=None) -> dict:
    from .evolve import options
    from .signals import guard_summary

    now = time.time() if now is None else now
    opts = options(config or {}, runtime_config, strict=False)
    today = utc_day(now)
    with experience(home) as conn:
        cursor = dict(conn.execute("SELECT key, value FROM cursor WHERE key IN "
                                   "('observed_since','loop_status','backfill_done','backfill_totals',?)",
                                   (f"reflector:{today}",)).fetchall())
        tools = conn.execute("SELECT COALESCE(SUM(calls),0) FROM tool_daily WHERE day=?", (today,)).fetchone()[0]
        turns = conn.execute("SELECT COALESCE(SUM(turns),0) FROM turn_daily WHERE day=?", (today,)).fetchone()[0]
        calls = conn.execute("SELECT COALESCE(SUM(requests),0) FROM metric WHERE day=? AND lane='main'", (today,)).fetchone()[0]
        lessons = dict(conn.execute("SELECT status, COUNT(*) FROM lesson GROUP BY status").fetchall())
        drafts = conn.execute("SELECT COUNT(*) FROM lesson WHERE origin='reflector' AND status='quarantined'").fetchone()[0]
        proposals = conn.execute("SELECT COUNT(*) FROM proposal WHERE status='open'").fetchone()[0]
        vetoed = guard_summary(conn, now=now, days=7)
    try:
        last = json.loads(cursor["loop_status"]) if "loop_status" in cursor else None
    except ValueError:
        last = None
    try:
        totals = json.loads(cursor.get("backfill_totals") or "{}")
    except ValueError:
        totals = {}
    report = {
        "profile": "default" if Path(home).resolve().parent.name != "profiles" else Path(home).resolve().name,
        "plugin_version": plugin_version(), "observing_since": _iso(cursor.get("observed_since")),
        "today": {"tool_calls": tools, "turns": turns, "model_calls": calls},
        "autopilot": {"minutes": opts["autopilot_minutes"],
                      "last_pass": {**last, "at": _iso(last.get("at"))} if isinstance(last, dict) else None},
        "backfill": {"status": cursor.get("backfill_done") or ("running" if totals else "waiting"), **totals},
        "reflector": {"enabled": opts["reflector"], "calls_today": int(cursor.get(f"reflector:{today}") or 0),
                      "daily_limit": opts["reflector_daily_limit"], "drafts_waiting": drafts},
        "lessons": {"mode": opts["lessons"], "active": lessons.get("active", 0),
                    "awaiting_approval": lessons.get("candidate", 0) + lessons.get("quarantined", 0),
                    "retired": lessons.get("retired", 0)},
        "proposals_open": proposals,
        "runtime_guard": {"enabled": opts["runtime_guard"], "vetoed_7d": sum(vetoed.values())},
        "profiles_without_plugin": _profiles(Path(home), now=now),
    }
    attention = []
    if not (tools or turns or calls):
        attention.append("no_events_today")
    if opts["autopilot_minutes"] == 0:
        attention.append("autopilot_off")
    elif report["autopilot"]["last_pass"] is None:
        attention.append("no_autopilot_pass")
    elif report["autopilot"]["last_pass"].get("ok") is False:
        attention.append("autopilot_failing")
    if report["lessons"]["awaiting_approval"]:
        attention.append("lessons_waiting")
    if report["profiles_without_plugin"]:
        attention.append("profiles_without_plugin")
    report["attention"] = attention
    return report


_TEXT = {
    "en": {"title": "Atlas Evolution {version} · profile `{profile}`", "since": "Observing since",
           "today": "Today (UTC)", "tool_calls": "tool calls", "turns": "turns", "model_calls": "model calls",
           "autopilot": "Autopilot", "every": "every {minutes} min", "off": "off (run `hermes evolution learn` yourself)",
           "last": "last pass", "ok": "ok", "failed": "failed", "never": "has not run yet; it starts after the next turn",
           "backfill": "Hermes history", "reflector": "Reflector", "on": "on", "calls": "calls today",
           "drafts": "drafts waiting", "lessons": "Lessons", "mode": "mode", "active": "active",
           "awaiting": "awaiting approval", "retired": "retired", "proposals": "Open proposals",
           "guard": "Runtime guard", "vetoed": "commands vetoed in 7 days", "attention": "Needs attention",
           "no_events_today": "No event from the hooks today. If the profile was used today, check that Hermes loaded the plugin in this profile (`hermes plugins list`).",
           "autopilot_off": "The autopilot is off: the loop only runs when you call `hermes evolution learn`.",
           "no_autopilot_pass": "The autopilot has not run a pass in this profile yet.",
           "autopilot_failing": "The last autopilot pass failed; it is retried in 10 minutes. See Hermes' agent.log.",
           "lessons_waiting": "Lessons are waiting for your approval: `hermes evolution lessons list`.",
           "profiles_without_plugin": "Profiles used in the last 7 days that do not have the plugin (each profile loads its own plugins):",
           "sessions": "sessions", "none": "Nothing needs attention.",
           "statuses": {"done": "read", "running": "being read", "waiting": "waiting for the first pass",
                        "no_history": "no history", "unreadable": "unreadable", "unsupported_history": "unsupported format"}},
    "pt": {"title": "Atlas Evolution {version} · perfil `{profile}`", "since": "Observando desde",
           "today": "Hoje (UTC)", "tool_calls": "chamadas de ferramentas", "turns": "turnos", "model_calls": "chamadas ao modelo",
           "autopilot": "Autopilot", "every": "a cada {minutes} min", "off": "desligado (rode `hermes evolution learn` você mesmo)",
           "last": "última passada", "ok": "ok", "failed": "falhou", "never": "ainda não rodou; começa depois do próximo turno",
           "backfill": "Histórico do Hermes", "reflector": "Refletor", "on": "ligado", "calls": "chamadas hoje",
           "drafts": "rascunhos aguardando", "lessons": "Lições", "mode": "modo", "active": "ativas",
           "awaiting": "aguardando aprovação", "retired": "aposentadas", "proposals": "Propostas abertas",
           "guard": "Guarda de runtime", "vetoed": "comandos barrados em 7 dias", "attention": "Precisa de atenção",
           "no_events_today": "Nenhum evento dos hooks hoje. Se o perfil foi usado hoje, confira se o Hermes carregou o plugin neste perfil (`hermes plugins list`).",
           "autopilot_off": "O autopilot está desligado: o ciclo só roda quando você chama `hermes evolution learn`.",
           "no_autopilot_pass": "O autopilot ainda não fez nenhuma passada neste perfil.",
           "autopilot_failing": "A última passada do autopilot falhou; ela é repetida em 10 minutos. Veja o agent.log do Hermes.",
           "lessons_waiting": "Há lições aguardando a sua aprovação: `hermes evolution lessons list`.",
           "profiles_without_plugin": "Perfis usados nos últimos 7 dias que não têm o plugin (cada perfil carrega os próprios plugins):",
           "sessions": "sessões", "none": "Nada precisa de atenção.",
           "statuses": {"done": "lido", "running": "em leitura", "waiting": "aguardando a primeira passada",
                        "no_history": "sem histórico", "unreadable": "ilegível", "unsupported_history": "formato não suportado"}},
}


def markdown(report: dict, lang: str = "en") -> str:
    t = _TEXT[lang if lang in _TEXT else "en"]
    today, pilot, history = report["today"], report["autopilot"], report["backfill"]
    lines = ["# " + t["title"].format(version=report["plugin_version"], profile=report["profile"]), ""]
    if report["observing_since"]:
        lines.append(f"- **{t['since']}:** {report['observing_since']}")
    lines.append(f"- **{t['today']}:** {today['turns']} {t['turns']} · {today['tool_calls']} {t['tool_calls']} · "
                 f"{today['model_calls']} {t['model_calls']}")
    if pilot["minutes"]:
        last = pilot["last_pass"]
        state = (f"{t['last']} {last['at']} · {t['ok'] if last.get('ok') else t['failed']}" if last else t["never"])
        lines.append(f"- **{t['autopilot']}:** {t['every'].format(minutes=pilot['minutes'])} · {state}")
    else:
        lines.append(f"- **{t['autopilot']}:** {t['off']}")
    counts = ", ".join(f"{history[key]} {t[key] if key in t else key}" for key in ("turns", "tool_calls") if key in history)
    lines.append(f"- **{t['backfill']}:** {t['statuses'].get(history['status'], history['status'])}"
                 + (f" · {history.get('sessions', 0)} {t['sessions']}, {counts}" if counts else ""))
    reflector = report["reflector"]
    lines.append(f"- **{t['reflector']}:** " + (f"{t['on']} · {reflector['calls_today']}/{reflector['daily_limit']} "
                 f"{t['calls']} · {reflector['drafts_waiting']} {t['drafts']}" if reflector["enabled"] else t["off"].split(" (")[0]))
    playbook = report["lessons"]
    lines.append(f"- **{t['lessons']}:** {t['mode']} `{playbook['mode']}` · {playbook['active']} {t['active']} · "
                 f"{playbook['awaiting_approval']} {t['awaiting']} · {playbook['retired']} {t['retired']}")
    lines.append(f"- **{t['proposals']}:** {report['proposals_open']}")
    guard = report["runtime_guard"]
    lines.append(f"- **{t['guard']}:** " + (f"{t['on']} · {guard['vetoed_7d']} {t['vetoed']}" if guard["enabled"]
                                             else t["off"].split(" (")[0]))
    lines += ["", f"## {t['attention']}", ""]
    for code in report["attention"]:
        lines.append(f"- {t[code]}")
        if code == "profiles_without_plugin":
            for item in report["profiles_without_plugin"]:
                lines.append(f"  - `{item['profile']}` ({item['sessions_7d']} {t['sessions']}): `{item['install']}`")
    if not report["attention"]:
        lines.append(t["none"])
    return "\n".join(lines) + "\n"


_NOTICE = {
    "en": {"lessons": "{n} lesson(s) awaiting your approval", "drafts": "{n} reflector draft(s) to read",
           "risky": "{n} high-risk proposal(s)", "failing": "the last learning pass failed",
           "lead": "Atlas Evolution", "tail": "See `hermes evolution status`."},
    "pt": {"lessons": "{n} lição(ões) aguardando a sua aprovação", "drafts": "{n} rascunho(s) do refletor para ler",
           "risky": "{n} proposta(s) de risco alto", "failing": "a última passada de aprendizado falhou",
           "lead": "Atlas Evolution", "tail": "Veja `hermes evolution status`."},
}


def pending_notice(home: Path, *, now: float, lang: str = "en") -> dict | None:
    """At most one short notice per UTC day about decisions waiting for the owner, or None."""
    from .experience import record

    today = utc_day(now)
    with experience(home) as conn:
        if (conn.execute("SELECT value FROM cursor WHERE key='notice_day'").fetchone() or [None])[0] == today:
            return None
        waiting = conn.execute("SELECT COUNT(*) FROM lesson WHERE status IN ('candidate','quarantined')").fetchone()[0]
        drafts = conn.execute("SELECT COUNT(*) FROM lesson WHERE status='quarantined' AND origin='reflector'").fetchone()[0]
        risky = conn.execute("SELECT COUNT(*) FROM proposal WHERE status='open' AND risk='high'").fetchone()[0]
        saved = conn.execute("SELECT value FROM cursor WHERE key='loop_status'").fetchone()
    try:
        failing = bool(saved) and json.loads(saved[0]).get("ok") is False
    except (ValueError, AttributeError):
        failing = False
    t = _NOTICE[lang if lang in _NOTICE else "en"]
    parts = [text.format(n=count) for count, text in ((waiting, t["lessons"]), (drafts, t["drafts"]), (risky, t["risky"]))
             if count]
    if failing:
        parts.append(t["failing"])
    if not parts:
        return None
    record(home, lambda conn: conn.execute("INSERT OR REPLACE INTO cursor VALUES ('notice_day', ?)", (today,)), now=now)
    return {"message": f"{t['lead']}: {'; '.join(parts)}. {t['tail']}"}

