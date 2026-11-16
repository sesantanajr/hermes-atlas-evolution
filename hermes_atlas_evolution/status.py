"""`hermes evolution status`: is the plugin observing, learning and protecting this profile right now?

It reads only the plugin's own store and, for the other profiles of the same Hermes root, how many sessions
they had (read-only), so it can point out a busy profile that does not have the plugin: one gateway serves
many profiles, but each profile loads its own plugins.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import quote

from .experience import experience, utc_day

REPOSITORY = "sesantanajr/hermes-atlas-evolution"
PLUGIN_DIR = "hermes-atlas-evolution"


def plugin_version() -> str:
    manifest = Path(__file__).resolve().parent.parent / "plugin.yaml"
    try:
        match = re.search(r'^version:\s*"?([\d.]+)"?', manifest.read_text(encoding="utf-8"), re.M)
    except OSError:
        match = None
    return match.group(1) if match else "unknown"


def process_kind() -> str:
    """gateway, dashboard or cli: long-running hosts are the ones that can keep old code loaded."""
    words = [os.path.basename(str(item)) for item in sys.argv[:6]]
    if "gateway" in words and "run" in words:
        return "gateway"
    if "dashboard" in words:
        return "dashboard"
    return "cli"


def record_runtime(home: Path, *, now: float, version: str | None = None, pid: int | None = None) -> None:
    """Remember which plugin version a long-running host loaded, so `status` can tell when the code on disk
    is newer than what the gateway runs (`hermes plugins update` does not reload a running gateway)."""
    from .experience import record

    kind = process_kind() if pid is None else "gateway"
    if kind == "cli":
        return
    value = json.dumps({"version": version or plugin_version(), "kind": kind, "since": now})
    key = f"runtime:{os.getpid() if pid is None else int(pid)}"
    record(home, lambda conn: conn.execute("INSERT OR REPLACE INTO cursor VALUES (?, ?)", (key, value)), now=now)


def _alive(pid: int) -> bool | None:
    """True or False on POSIX; None where a liveness probe is not safe (on Windows os.kill would end it)."""
    if os.name != "posix":
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _runtimes(conn, disk: str) -> tuple[list[dict], list[str]]:
    """(live hosts that loaded another version, keys of hosts that are gone)."""
    stale, gone = [], []
    for key, value in conn.execute("SELECT key, value FROM cursor WHERE key LIKE 'runtime:%'").fetchall():
        try:
            pid, data = int(key.split(":", 1)[1]), json.loads(value)
        except (ValueError, TypeError):
            gone.append(key)
            continue
        alive = _alive(pid)
        if alive is False:
            gone.append(key)
        elif alive and isinstance(data, dict) and data.get("version") != disk:
            stale.append({"pid": pid, "kind": data.get("kind"), "version": data.get("version"), "since": _iso(data.get("since"))})
    return stale, gone


def _iso(stamp) -> str | None:
    try:
        from .experience import utc_datetime

        return utc_datetime(float(stamp)).isoformat(timespec="seconds")
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


def chat_senders(home: Path, *, since: float, owners=(), platforms=()) -> list[dict]:
    """Chat channels of this profile since `since`, read-only from its state.db: per origin (telegram,
    api_server, ...) the sessions, their distinct senders with their session counts, and whether the owner
    settings already cover the channel. Local surfaces, automations and subagents are left out."""
    from .signals import surface_of

    path = Path(home) / "state.db"
    if not path.is_file():
        return []
    try:
        source = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=1)
        try:
            rows = source.execute("""SELECT source, user_id, COUNT(*) FROM sessions WHERE started_at >= ?
                GROUP BY source, user_id ORDER BY COUNT(*) DESC LIMIT 400""", (since,)).fetchall()
        finally:
            source.close()
    except sqlite3.Error:
        return []
    owners, platforms = {str(item) for item in owners}, set(platforms)
    channels: dict[str, dict] = {}
    for origin, sender, count in rows:
        name = str(origin or "unknown").lower()[:48]
        if surface_of(name) != "gateway":
            continue
        channel = channels.setdefault(name, {"origin": name, "sessions": 0, "senders": [], "covered": name in platforms})
        channel["sessions"] += count
        listed = sender is not None and str(sender) in owners
        channel["covered"] = channel["covered"] or listed
        channel["senders"].append({"id": None if sender is None else str(sender)[:200], "sessions": count, "owner": listed})
    return sorted(channels.values(), key=lambda item: -item["sessions"])


def _digest_scheduled(home: Path) -> bool:
    """Whether Hermes' scheduler holds an enabled weekly digest of this plugin for the profile."""
    from .workloads import _load_json

    status, data = _load_json(Path(home) / "cron" / "jobs.json", 4 * 1024 * 1024)
    jobs = data.get("jobs") if status == "ok" and isinstance(data, dict) else None
    return any(isinstance(job, dict) and job.get("enabled") is not False
               and job.get("name") in ("hermes-atlas-evolution-digest", "hermes-evolution-digest")
               for job in jobs or ())


LEGACY_DIRS = ("hermes-evolution",)   # the plugin id used up to 0.5.0


def _command(name: str, rest: str) -> str:
    return f"hermes {'' if name == 'default' else f'-p {name} '}{rest}"


def _profile_state(folder: Path) -> tuple[str, str | None, int, int]:
    """(plugin state, last pass, active lessons, lessons waiting) of a profile, read-only from its own database.

    "both": the new copy is there but so is the old one, which keeps running while the new one stays inactive."""
    new = (folder / "plugins" / PLUGIN_DIR).is_dir()
    old = any((folder / "plugins" / legacy / "plugin.yaml").is_file() and not _is_this_plugin(folder / "plugins" / legacy)
              for legacy in LEGACY_DIRS)
    if new and old:
        state = "both"
    elif new or (folder / "plugins" / LEGACY_DIRS[0]).is_dir() and not old:
        state = "installed"
    elif old:
        state = "legacy"
    else:
        return "missing", None, 0, 0
    path = folder / "evolution" / "evolution.sqlite3"
    if not path.is_file():
        return state, None, 0, 0
    try:
        source = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=1)
        try:
            row = source.execute("SELECT value FROM cursor WHERE key='loop_status'").fetchone()
            counts = dict(source.execute("SELECT status, COUNT(*) FROM lesson GROUP BY status").fetchall())
        finally:
            source.close()
    except sqlite3.Error:
        return state, None, 0, 0
    try:
        last = _iso(json.loads(row[0]).get("at")) if row else None
    except (ValueError, AttributeError):
        last = None
    return state, last, counts.get("active", 0), counts.get("candidate", 0) + counts.get("quarantined", 0)


def _is_this_plugin(folder: Path) -> bool:
    """True when a folder named after the old id holds this version (a clone updated in place)."""
    try:
        manifest = (folder / "plugin.yaml").read_text(encoding="utf-8", errors="replace")[:65_536]
    except OSError:
        return False
    return re.search(rf"^name:\s*[\"']?{re.escape(PLUGIN_DIR)}[\"']?\s*$", manifest, re.M) is not None


def _profiles(home: Path, *, now: float) -> list[dict]:
    """Every profile of the same Hermes root: sessions in 7 days and the plugin's state there.

    One gateway serves many profiles, but each profile loads its own plugins, so the profile you happen to be
    in may not be the one doing the work."""
    home = Path(home).resolve()
    root = home.parent.parent if home.parent.name == "profiles" else home
    candidates = [("default", root)]
    try:
        candidates += [(entry.name, entry) for entry in sorted((root / "profiles").iterdir()) if entry.is_dir()][:200]
    except OSError:
        pass
    rows = []
    for name, folder in candidates:
        state, last, active, waiting = _profile_state(folder)
        rows.append({"profile": name, "current": folder.resolve() == home,
                     "sessions_7d": _sessions(folder, since=now - 7 * 86_400) or 0, "plugin": state,
                     "last_pass": last, "lessons_active": active, "lessons_waiting": waiting,
                     "install": _command(name, f"plugins install {REPOSITORY} --enable"),
                     "migrate": f"{_command(name, 'plugins remove ' + LEGACY_DIRS[0])} && " + (
                         "hermes gateway restart" if state == "both"
                         else _command(name, f"plugins install {REPOSITORY} --enable")),
                     "status": _command(name, "evolution status")})
    return rows


def never_ran(home: Path) -> bool:
    """True when no pass ever finished in this profile (the autopilot has not had its first turn or tick)."""
    path = Path(home) / "evolution" / "evolution.sqlite3"
    if not path.is_file():
        return True
    try:
        source = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=1)
        try:
            return source.execute("SELECT 1 FROM cursor WHERE key='loop_status'").fetchone() is None
        finally:
            source.close()
    except sqlite3.Error:
        return False


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
        withdrawn = conn.execute("SELECT COUNT(*) FROM lesson WHERE status='retired' AND verdict='withdrawn'").fetchone()[0]
        drafts = conn.execute("SELECT COUNT(*) FROM lesson WHERE origin='reflector' AND status='quarantined'").fetchone()[0]
        proposals = conn.execute("SELECT COUNT(*) FROM proposal WHERE status='open'").fetchone()[0]
        vetoed = guard_summary(conn, now=now, days=7)
        stale, gone = _runtimes(conn, plugin_version())
    if gone:
        from .experience import record

        record(home, lambda conn: conn.executemany("DELETE FROM cursor WHERE key=?", [(key,) for key in gone]), now=now)
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
                    "retired": lessons.get("retired", 0) - withdrawn, "withdrawn": withdrawn},
        "proposals_open": proposals,
        "runtime_guard": {"enabled": opts["runtime_guard"], "state": _guard_state(opts["runtime_guard"]),
                          "vetoed_7d": sum(vetoed.values())},
        "profiles": _profiles(Path(home), now=now),
        "stale_runtimes": stale,
    }
    report["profiles_without_plugin"] = sorted(
        (row for row in report["profiles"] if row["plugin"] == "missing" and row["sessions_7d"] and not row["current"]),
        key=lambda row: -row["sessions_7d"])
    report["legacy_installs"] = [row for row in report["profiles"] if row["plugin"] in ("legacy", "both")]
    channels = chat_senders(home, since=now - 7 * 86_400, owners=opts["owner_sender_ids"], platforms=opts["owner_platforms"])
    report["chats"] = [{key: value for key, value in channel.items() if key != "senders"} | {"senders": len(channel["senders"])}
                       for channel in channels]   # counts only: the ids stay in `hermes evolution owner`
    report["digest_scheduled"] = _digest_scheduled(home)
    mine = next((row for row in report["profiles"] if row["current"]), None)
    busiest = max((row for row in report["profiles"] if row["plugin"] == "installed" and not row["current"]),
                  key=lambda row: row["sessions_7d"], default=None)
    report["busier_profile"] = (busiest if busiest and mine and busiest["sessions_7d"] >= 10
                                and busiest["sessions_7d"] > 4 * mine["sessions_7d"] else None)
    attention = []
    if stale:
        attention.append("stale_runtime")
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
    if report["legacy_installs"]:
        attention.append("legacy_install")
    if report["busier_profile"]:
        attention.append("busier_profile")
    if report["profiles_without_plugin"]:
        attention.append("profiles_without_plugin")
    if any(not channel["covered"] for channel in report["chats"]):
        attention.append("owner_unknown")
    if (report["lessons"]["awaiting_approval"] or report["proposals_open"]) and not report["digest_scheduled"]:
        attention.append("digest_unscheduled")
    from .cli import ignored_settings, settings as stored_settings

    try:   # values the agent could have written there are not used (cli.TRUSTED_ONLY); say so
        report["settings_ignored"] = sorted(ignored_settings(stored_settings(home)))
    except (OSError, ValueError, RecursionError):
        report["settings_ignored"] = []
    if report["settings_ignored"]:
        attention.append("settings_ignored")
    report["attention"] = attention
    return report


_TEXT = {
    "en": {"title": "Atlas Evolution {version} · profile `{profile}`", "since": "Observing since",
           "today": "Today (UTC)", "tool_calls": "tool calls", "turns": "turns", "model_calls": "model calls",
           "autopilot": "Autopilot", "every": "every {minutes} min", "off": "off (run `hermes evolution learn` yourself)",
           "last": "last pass", "ok": "ok", "failed": "failed",
           "never": "has not run yet; the clock starts it within 2 minutes of the plugin loading, or run `hermes evolution learn`",
           "backfill": "Hermes history", "reflector": "Reflector", "on": "on", "calls": "calls today",
           "drafts": "drafts waiting", "lessons": "Lessons", "mode": "mode", "active": "active",
           "awaiting": "awaiting approval", "retired": "retired", "withdrawn": "withdrawn", "proposals": "Open proposals",
           "guard": "Runtime guard", "vetoed": "commands vetoed in 7 days", "attention": "Needs attention",
           "guard_covers": "knows the gateway as a systemd unit (Linux), a LaunchAgent (macOS) and a Windows service or task, and keeps the agent from changing this plugin's settings",
           "guard_host": "off: plugins run in a plugin host here (plugins.isolation: host), where a crashed host would "
                         "block every tool",
           "no_events_today": "No event from the hooks today. If the profile was used today, check that Hermes loaded the plugin in this profile (`hermes plugins list`).",
           "autopilot_off": "The autopilot is off: the loop only runs when you call `hermes evolution learn`.",
           "no_autopilot_pass": "The autopilot has not run a pass in this profile yet.",
           "autopilot_failing": "The last autopilot pass failed; it is retried in 10 minutes. See Hermes' agent.log.",
           "lessons_waiting": "Lessons are waiting for your approval: decide on each in your own terminal with `hermes evolution lessons review`.",
           "profiles_without_plugin": "Profiles used in the last 7 days that do not have the plugin (each profile loads its own plugins):",
           "owner_unknown": "These chat channels count as someone else's, so they get template lessons only and their failures wait for your approval. If one of them is you, run `hermes evolution owner` in your own terminal:",
           "chat_item": "`{origin}`: {sessions} sessions in 7 days from {senders} sender(s)",
           "digest_unscheduled": "Decisions wait for you and nothing reminds you of them (Hermes 0.23.1 drops end-of-session notices). Schedule the weekly digest to your chat with `hermes evolution report --schedule-spec --deliver <channel>` and run the command it prints.",
           "settings_ignored": "These values in `settings.json` are ignored: {keys}. They would widen whom the plugin trusts or turn a protection off, and the agent's own file tools can write that file. Set the ones you want in the Hermes settings form, with `hermes evolution owner` (sender ids and platforms), or with `hermes config set plugins.entries.hermes-atlas-evolution.settings.<key> <value>` in your own terminal.",
           "sessions": "sessions", "none": "Nothing needs attention.",
           "stale_runtime": "A running Hermes process still runs an older copy of the plugin than the one on disk (`hermes plugins update` does not reload a running gateway). Run `hermes gateway restart`; it waits for the running turns.",
           "stale_item": "pid {pid} ({kind}) loaded {version}",
           "legacy_install": "These profiles still have the plugin under its old id hermes-evolution (up to 0.5.0), which keeps running while a new copy there stays inactive. Run the command shown for each; the data it learned stays:",
           "busier_profile": "This profile is barely used. `{profile}` had {sessions} sessions in 7 days; see it with `{status}`.",
           "profiles": "Profiles of this Hermes", "col_profile": "profile", "col_sessions": "sessions (7 d)",
           "col_plugin": "plugin", "col_last": "last pass", "col_lessons": "lessons active / waiting",
           "states": {"installed": "installed", "legacy": "old id (0.5.0)", "both": "both copies: the old one runs",
                      "missing": "not installed"},
           "first_pass": "First pass done now: {sessions} sessions, {tool_calls} tool calls of history read.",
           "statuses": {"done": "read", "running": "being read", "waiting": "waiting for the first pass",
                        "no_history": "no history", "unreadable": "unreadable", "unsupported_history": "unsupported format"}},
    "pt": {"title": "Atlas Evolution {version} · perfil `{profile}`", "since": "Observando desde",
           "today": "Hoje (UTC)", "tool_calls": "chamadas de ferramentas", "turns": "turnos", "model_calls": "chamadas ao modelo",
           "autopilot": "Autopilot", "every": "a cada {minutes} min", "off": "desligado (rode `hermes evolution learn` você mesmo)",
           "last": "última passada", "ok": "ok", "failed": "falhou",
           "never": "ainda não rodou; o relógio a inicia em até 2 minutos depois de o plugin carregar, ou rode `hermes evolution learn`",
           "backfill": "Histórico do Hermes", "reflector": "Refletor", "on": "ligado", "calls": "chamadas hoje",
           "drafts": "rascunhos aguardando", "lessons": "Lições", "mode": "modo", "active": "ativas",
           "awaiting": "aguardando aprovação", "retired": "aposentadas", "withdrawn": "retiradas", "proposals": "Propostas abertas",
           "guard": "Guarda de runtime", "vetoed": "comandos barrados em 7 dias", "attention": "Precisa de atenção",
           "guard_covers": "reconhece o gateway como unidade systemd (Linux), LaunchAgent (macOS) e serviço ou tarefa "
                           "do Windows, e impede o agente de mudar as configurações deste plugin",
           "guard_host": "desligada: aqui os plugins rodam num host de plugins (plugins.isolation: host), onde um host "
                         "que caísse bloquearia todas as ferramentas",
           "no_events_today": "Nenhum evento dos hooks hoje. Se o perfil foi usado hoje, confira se o Hermes carregou o plugin neste perfil (`hermes plugins list`).",
           "autopilot_off": "O autopilot está desligado: o ciclo só roda quando você chama `hermes evolution learn`.",
           "no_autopilot_pass": "O autopilot ainda não fez nenhuma passada neste perfil.",
           "autopilot_failing": "A última passada do autopilot falhou; ela é repetida em 10 minutos. Veja o agent.log do Hermes.",
           "lessons_waiting": "Há lições aguardando a sua aprovação: decida cada uma no seu próprio terminal com `hermes evolution lessons review`.",
           "profiles_without_plugin": "Perfis usados nos últimos 7 dias que não têm o plugin (cada perfil carrega os próprios plugins):",
           "owner_unknown": "Estes canais de chat contam como de outra pessoa, então recebem só lições de modelo fixo e as falhas deles esperam a sua aprovação. Se um deles é você, rode `hermes evolution owner` no seu próprio terminal:",
           "chat_item": "`{origin}`: {sessions} sessões em 7 dias de {senders} remetente(s)",
           "digest_unscheduled": "Há decisões esperando por você e nada as lembra (o Hermes 0.23.1 descarta os avisos de fim de sessão). Agende o resumo semanal no seu chat com `hermes evolution report --schedule-spec --deliver <canal>` e rode o comando que ele imprime.",
           "settings_ignored": "Estes valores do `settings.json` são ignorados: {keys}. Eles ampliariam em quem o plugin confia ou desligariam uma proteção, e as ferramentas de arquivo do próprio agente conseguem gravar esse arquivo. Defina os que você quer no formulário de configurações do Hermes, com o `hermes evolution owner` (ids de remetente e plataformas) ou com `hermes config set plugins.entries.hermes-atlas-evolution.settings.<chave> <valor>` no seu próprio terminal.",
           "sessions": "sessões", "none": "Nada precisa de atenção.",
           "stale_runtime": "Um processo do Hermes em execução ainda roda uma cópia do plugin mais antiga que a do disco (o `hermes plugins update` não recarrega um gateway em execução). Rode `hermes gateway restart`; ele espera os turnos em andamento.",
           "stale_item": "pid {pid} ({kind}) carregou a {version}",
           "legacy_install": "Estes perfis ainda têm o plugin com o nome antigo hermes-evolution (até a 0.5.0), que continua rodando enquanto uma cópia nova ali fica inativa. Rode o comando de cada um; os dados aprendidos ficam:",
           "busier_profile": "Este perfil quase não é usado. O `{profile}` teve {sessions} sessões em 7 dias; veja com `{status}`.",
           "profiles": "Perfis deste Hermes", "col_profile": "perfil", "col_sessions": "sessões (7 d)",
           "col_plugin": "plugin", "col_last": "última passada", "col_lessons": "lições ativas / aguardando",
           "states": {"installed": "instalado", "legacy": "nome antigo (0.5.0)", "both": "duas cópias: a antiga roda",
                      "missing": "não instalado"},
           "first_pass": "Primeira passada feita agora: {sessions} sessões e {tool_calls} chamadas de ferramentas do histórico lidas.",
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
                 f"{playbook['awaiting_approval']} {t['awaiting']} · {playbook['retired']} {t['retired']}"
                 + (f" · {playbook['withdrawn']} {t['withdrawn']}" if playbook.get("withdrawn") else ""))
    lines.append(f"- **{t['proposals']}:** {report['proposals_open']}")
    guard = report["runtime_guard"]
    if guard.get("state") == "host":
        state = t["guard_host"]
    elif guard["enabled"]:
        state = f"{t['on']} · {guard['vetoed_7d']} {t['vetoed']} · {t['guard_covers']}"
    else:
        state = t["off"].split(" (")[0]
    lines.append(f"- **{t['guard']}:** {state}")
    if report.get("first_pass"):
        lines += ["", "> " + t["first_pass"].format(**report["first_pass"])]
    lines += ["", f"## {t['attention']}", ""]
    for code in report["attention"]:
        if code == "busier_profile":
            lines.append("- " + t[code].format(**{**report["busier_profile"], "profile": report["busier_profile"]["profile"],
                                                 "sessions": report["busier_profile"]["sessions_7d"]}))
            continue
        if code == "settings_ignored":
            lines.append("- " + t[code].format(keys=", ".join(f"`{key}`" for key in report["settings_ignored"])))
            continue
        lines.append(f"- {t[code]}")
        if code == "legacy_install":
            for item in report["legacy_installs"]:
                lines.append(f"  - `{item['profile']}`: `{item['migrate']}`")
        if code == "stale_runtime":
            for item in report["stale_runtimes"]:
                lines.append("  - " + t["stale_item"].format(**item))
        if code == "profiles_without_plugin":
            for item in report["profiles_without_plugin"]:
                lines.append(f"  - `{item['profile']}` ({item['sessions_7d']} {t['sessions']}): `{item['install']}`")
        if code == "owner_unknown":
            for item in report["chats"]:
                if not item["covered"]:
                    lines.append("  - " + t["chat_item"].format(**item))
    if not report["attention"]:
        lines.append(t["none"])
    shown = [row for row in report.get("profiles", []) if row["sessions_7d"] or row["plugin"] != "missing" or row["current"]]
    if shown:
        lines += ["", f"## {t['profiles']}", "",
                  f"| {t['col_profile']} | {t['col_sessions']} | {t['col_plugin']} | {t['col_last']} | {t['col_lessons']} |",
                  "|---|---:|---|---|---:|"]
        for row in sorted(shown, key=lambda item: (-item["sessions_7d"], item["profile"])):
            name = f"**{row['profile']}**" if row["current"] else row["profile"]
            lessons = f"{row['lessons_active']} / {row['lessons_waiting']}" if row["plugin"] != "missing" else "—"
            lines.append(f"| {name} | {row['sessions_7d']} | {t['states'][row['plugin']]} | {row['last_pass'] or '—'} | {lessons} |")
    return "\n".join(lines) + "\n"


_NOTICE = {
    "en": {"lessons": "{n} lesson(s) awaiting your approval", "drafts": "{n} reflector draft(s) to read",
           "risky": "{n} high-risk proposal(s)", "failing": "the last learning pass failed",
           "lead": "Atlas Evolution", "tail": "See `hermes evolution status`."},
    "pt": {"lessons": "{n} lição(ões) aguardando a sua aprovação", "drafts": "{n} rascunho(s) do refletor para ler",
           "risky": "{n} proposta(s) de risco alto", "failing": "a última passada de aprendizado falhou",
           "lead": "Atlas Evolution", "tail": "Veja `hermes evolution status`."},
}


def _guard_state(enabled: bool) -> str:
    """on, off, or host: Hermes runs this profile's plugins in a plugin host, where the guard stays off."""
    module = sys.modules.get("hermes_cli.plugin_isolation")
    mode = getattr(module, "isolation_mode", None)
    try:
        isolation = mode() if callable(mode) else None
    except Exception:
        isolation = None
    if isolation == "host":
        return "host"
    return "on" if enabled else "off"


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

