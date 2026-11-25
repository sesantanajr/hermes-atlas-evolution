"""Every profile of this Hermes in one view, and Auto for all of them at once (0.14.0).

One gateway often serves a dozen profiles, and each profile loads its own copy of the plugin, learns on its own and
keeps its own database. Up to 0.13.0 the owner had to open each profile to see whether it learned anything, and
nothing said when the passes of most of them had stopped. This module reads every profile's database (read-only)
and puts the whole Hermes in one table: is the plugin on, which version runs, when its last learning pass ran and
whether it is late, what it learned, what waits, what failed; with the gateway, its memory, what the last Hermes
update left behind (drift) and Hermes' own warnings (logsignals).

Auto for the whole Hermes (`hermes evolution fleet auto on`) is one switch, in the owner's own terminal: each profile
with the plugin on gets `mode: auto` and `lessons: auto` (and a learning pass every hour where it was off) through
Hermes' own `hermes config set`, each change kept in that profile's history with its undo, and the values it had
before kept here, so `fleet auto off` puts every profile back as it was. Optionally (`--owner suggested`) the chat
channels that are the owner's by construction (one sender, the owner's own interface) count as the owner, so lessons
can activate there. What Auto does by itself is the list in `SAFE_ACTIONS`, every item with its undo; it never updates
anything, never touches Hermes' code, a stash, a service or the gateway, and needs no network.

Lessons proven in one profile (a template lesson whose measurement says it helped, see lessons.judge) are offered to
the other profiles in Auto that use the same tool: they arrive waiting, like a pack the owner imports, and activate
there only once that profile's own turns hit the failure (lessons.promote). Nothing written by a model travels.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote

AUTO_KEY = "fleet_auto"
HEALTH_KEY = "fleet_health"
JOB_NAME = "hermes-atlas-evolution-fleet"
SCRIPT_NAME = "hermes-atlas-evolution-fleet.py"
LATE_FACTOR = 3              # a pass this many intervals late is overdue
LATE_FLOOR_SECONDS = 6 * 3600
SHARE_PER_PASS = 5
SHARE_TOOL_DAYS = 30
CLAIM_SECONDS = 600
# What Auto does by itself, each with its undo (the menu, the panel and `fleet auto status` list them):
# (code, English, Portuguese, undo in English, undo in Portuguese). An undo between backticks is a command.
SAFE_ACTIONS = (
    ("memory_duplicates", "Takes out of MEMORY.md an entry repeated word for word in USER.md (a copy of the file is kept)",
     "Tira do MEMORY.md uma entrada repetida palavra por palavra no USER.md (guarda uma cópia do arquivo)",
     "`hermes evolution decisions undo <id>`", "`hermes evolution decisions undo <id>`"),
    ("lessons", "Activates template lessons once your own turns hit their failure twice; retires lessons measured as "
                "not helping or whose tool nobody uses any more",
     "Ativa lições de padrão fixo quando os seus próprios turnos batem na falha duas vezes; aposenta as que a medição "
     "diz que não ajudam ou cuja ferramenta ninguém usa mais",
     "`hermes evolution lessons retire <id>` or `approve <id>`", "`hermes evolution lessons retire <id>` ou `approve <id>`"),
    ("skill_lock", "Locks a skill the curator keeps rewriting", "Trava uma skill que o curador reescreve sem parar",
     "`hermes curator unlock <skill>`", "`hermes curator unlock <skill>`"),
    ("self_missing", "Installs Atlas Evolution, from Hermes' catalog, in a profile in use that never had it",
     "Instala o Atlas Evolution, pelo catálogo do Hermes, num perfil em uso que nunca o teve",
     "`hermes evolution decisions undo <id>`", "`hermes evolution decisions undo <id>`"),
    ("plugin_leftover", "Moves a failed plugin install's folder to the trash (kept 30 days)",
     "Move para a lixeira (guardada 30 dias) a pasta de uma instalação de plugin que falhou",
     "`hermes evolution decisions undo <id>`", "`hermes evolution decisions undo <id>`"),
    ("gateway_repair", "Points the gateway's service at Hermes' current Python when Hermes removed the one it started",
     "Aponta o serviço do gateway para o Python atual do Hermes quando o Hermes removeu o que ele iniciava",
     "`hermes evolution care undo-gateway`", "`hermes evolution care undo-gateway`"),
    ("pm_gc", "Frees old tool versions nothing uses (`hermes pm gc`, only when nothing in use would go)",
     "Libera versões antigas de ferramentas que nada usa (`hermes pm gc`, só quando nada em uso sairia)",
     "nothing to undo: Hermes downloads a version again when something asks for it",
     "nada a desfazer: o Hermes baixa a versão de novo quando algo pede"),
    ("lesson_share", "Offers a lesson proven in one profile to the others in Auto that use the same tool (it arrives "
                     "waiting)",
     "Oferece uma lição comprovada num perfil aos outros em Auto que usam a mesma ferramenta (ela chega esperando)",
     "`hermes evolution lessons retire <id>` in that profile", "`hermes evolution lessons retire <id>` nesse perfil"),
)
NEVER = ("updates Hermes or a plugin", "changes Hermes' code, an overlay or a stash", "edits a service or restarts "
         "the gateway (except pointing it at Hermes' current Python when the one it started is gone)",
         "uses the network or sends telemetry")
NEVER_PT = ("atualiza o Hermes ou um plugin", "muda o código do Hermes, uma sobreposição ou um stash",
            "edita um serviço ou reinicia o gateway (fora apontar o serviço para o Python atual do Hermes quando o que "
            "ele iniciava sumiu)", "usa a rede ou manda telemetria")


def _q(count, one: str, many: str) -> str:
    """A count with its word in the right number: 1 vez, 2 vezes."""
    return f"{count} {one if count == 1 else many}"


# ── reading every profile ───────────────────────────────────────────────────────────────────────────────────────
def _readonly(path: Path):
    if not path.is_file():
        return None
    try:
        source = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=1)
        source.row_factory = sqlite3.Row
        source.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        return source
    except sqlite3.Error:
        return None


def _scalar(conn, query: str, args=()) -> float:
    try:
        row = conn.execute(query, args).fetchone()
    except sqlite3.Error:
        return 0
    return (row[0] or 0) if row else 0


def profile_row(name: str, home: Path, *, now: float, read_config=None) -> dict:
    """One profile as the fleet view shows it. Read-only; a database that cannot be read leaves its numbers out."""
    from .environment import _read_config, fleet_managed, has_plugin, profile_mode
    from .experience import day_offset
    from .status import _sessions, plugin_version

    read_config = read_config or _read_config
    config = read_config(Path(home))
    enabled = isinstance(config, dict) and has_plugin(Path(home), config)
    row = {"profile": name, "home": str(home), "enabled": enabled, "version": plugin_version(Path(home)),
           "mode": profile_mode(Path(home), read_config=lambda _home: config) if enabled else None,
           "managed": fleet_managed(Path(home), read_config=lambda _home: config) if isinstance(config, dict) else False,
           "sessions_7d": _sessions(Path(home), since=now - 7 * 86_400)}
    interval = 60
    try:
        from .cli import effective_settings
        from .evolve import options

        if enabled:
            interval = options(effective_settings(Path(home), config), config, strict=False)["autopilot_minutes"]
    except Exception:
        pass
    row["interval_minutes"] = interval
    conn = _readonly(Path(home) / "evolution" / "evolution.sqlite3")
    if conn is None:
        row.update(last_pass=None, pass_ok=None, overdue=bool(enabled and interval and row["sessions_7d"]))
        return row
    try:
        cursor = dict(conn.execute("SELECT key, value FROM cursor WHERE key IN ('last_cycle', 'loop_status')").fetchall())
        try:
            status = json.loads(cursor.get("loop_status") or "{}")
        except ValueError:
            status = {}
        try:
            last = float(cursor.get("last_cycle")) if cursor.get("last_cycle") else None
        except ValueError:
            last = None
        since = day_offset(now, 6)
        lessons = dict(conn.execute("SELECT status, COUNT(*) FROM lesson GROUP BY status").fetchall())
        row.update(
            last_pass=last, pass_ok=status.get("ok") if isinstance(status, dict) else None,
            pass_seconds=status.get("seconds") if isinstance(status, dict) else None,
            lessons_active=lessons.get("active", 0),
            lessons_waiting=lessons.get("candidate", 0) + lessons.get("quarantined", 0),
            lessons_new_7d=int(_scalar(conn, "SELECT COUNT(*) FROM lesson WHERE created >= ?", (now - 7 * 86_400,))),
            proposals_open=int(_scalar(conn, "SELECT COUNT(*) FROM proposal WHERE status='open'")),
            tool_calls_7d=int(_scalar(conn, "SELECT SUM(calls) FROM tool_daily WHERE day >= ?", (since,))),
            tool_errors_7d=int(_scalar(conn, "SELECT SUM(errors) FROM tool_daily WHERE day >= ?", (since,))),
            turns_7d=int(_scalar(conn, "SELECT SUM(turns) FROM turn_daily WHERE day >= ?", (since,))),
            slow_7d=int(_scalar(conn, "SELECT SUM(slow) FROM turn_daily WHERE day >= ?", (since,))),
            failures_7d=int(_scalar(conn, "SELECT SUM(count) FROM failure_daily WHERE day >= ?", (since,))),
            automatic_7d=int(_scalar(conn, "SELECT COUNT(*) FROM decision_log WHERE actor='automatic' AND at >= ?",
                                     (now - 7 * 86_400,))))
        loaded = []
        for key, value in conn.execute("SELECT key, value FROM cursor WHERE key LIKE 'runtime:%'").fetchall():
            try:
                entry = json.loads(value)
            except ValueError:
                continue
            if isinstance(entry, dict) and entry.get("kind") == "gateway":
                loaded.append((float(entry.get("since") or 0), str(entry.get("version") or "")))
        row["gateway_version"] = max(loaded)[1] if loaded else None
    except sqlite3.Error:
        row.setdefault("last_pass", None)
    finally:
        conn.close()
    used = bool(row.get("sessions_7d") or row.get("turns_7d") or row.get("tool_calls_7d"))
    late = max(LATE_FLOOR_SECONDS, LATE_FACTOR * interval * 60) if interval else None
    row["used"] = used
    row["overdue"] = bool(enabled and interval and used and (row.get("last_pass") is None
                                                            or now - row["last_pass"] > late))
    return row


def gateway_state(root: Path) -> dict:
    """The gateway as Hermes' own runtime record says (gateway_state.json): pid, state, the Hermes version it runs."""
    try:
        with open(Path(root) / "gateway_state.json", "rb") as stream:
            data = json.loads(stream.read(2_000_000) or b"{}")
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    version = data.get("code_version")
    return {"pid": data.get("pid") if type(data.get("pid")) is int else None,
            "state": str(data.get("gateway_state") or "")[:20] or None,
            "version": version if isinstance(version, str) and re.fullmatch(r"\d+(?:\.\d+){1,3}", version) else None,
            "active_agents": data.get("active_agents") if type(data.get("active_agents")) is int else None}


def overview(home: Path, *, now: float | None = None, read_config=None) -> dict:
    """The whole Hermes: every profile (profile_row), the gateway, and what the lead profile's last pass stored about
    drift, the gateway's memory and Hermes' warnings (fleet_health), plus Auto for all of them."""
    from .environment import lead, profiles, root_of

    now = time.time() if now is None else now
    root = root_of(Path(home))
    rows = [profile_row(name, Path(folder), now=now, read_config=read_config) for name, folder in profiles(root)]
    leader = lead(root, read_config=read_config, now=now)
    health, auto = {}, {}
    lead_home = next((Path(row["home"]) for row in rows if row["profile"] == leader), None)
    if lead_home is not None:
        conn = _readonly(lead_home / "evolution" / "evolution.sqlite3")
        if conn is not None:
            try:
                stored = dict(conn.execute("SELECT key, value FROM cursor WHERE key IN (?, ?)",
                                           (HEALTH_KEY, AUTO_KEY)).fetchall())
                health = json.loads(stored.get(HEALTH_KEY) or "{}")
                auto = json.loads(stored.get(AUTO_KEY) or "{}")
            except (sqlite3.Error, ValueError):
                pass
            finally:
                conn.close()
    gateway = gateway_state(root)
    view = {"at": now, "root": str(root), "lead": leader, "profiles": rows, "gateway": gateway,
            "health": health if isinstance(health, dict) else {}, "auto": auto if isinstance(auto, dict) else {}}
    view["attention"] = attention(view)
    return view


def attention(view: dict) -> list[str]:
    found = []
    rows = view.get("profiles") or ()
    if any(row.get("overdue") for row in rows):
        found.append("fleet_pass_overdue")
    versions = {row.get("version") for row in rows if row.get("enabled")}
    if len(versions) > 1:
        found.append("fleet_versions_differ")
    if any(row.get("gateway_version") and row.get("version") and row["gateway_version"] != row["version"]
           for row in rows if row.get("enabled")):
        found.append("fleet_gateway_behind")
    health = view.get("health") or {}
    found += [code for code in (health.get("drift") or {}).get("findings") or ()]
    found += [code for code in health.get("signals_findings") or ()]
    if (health.get("memory") or {}).get("suggestion"):   # said once, with what to change
        found = [code for code in found if code != "log_memory_pressure"] + ["fleet_memory_budget"]
    return list(dict.fromkeys(found))


# ── the lead's pass: what the whole Hermes needs once ───────────────────────────────────────────────────────────
def refresh(home: Path, conn, *, now: float, host=None, force: bool = False, pins=()) -> dict | None:
    """In the lead profile's learning pass: read Hermes' warnings (logsignals), what the last update left behind
    (drift, every 6 hours or after an update) and the gateway's memory, and keep them for the fleet view, `status` and
    the panel. None in any other profile. Never raises."""
    from . import drift, gateway_memory, logsignals
    from .admin import profile_name
    from .environment import lead, root_of

    root = root_of(Path(home))
    if lead(root, now=now) != profile_name(Path(home)):
        return None
    health: dict = {"at": now}
    try:
        logsignals.ingest(root, conn, now=now)
        conn.commit()
        signals = logsignals.summary(conn, now=now)
        health["signals"] = signals
        health["signals_findings"] = logsignals.findings(signals)
    except Exception as error:
        health["signals_error"] = type(error).__name__
        signals = {}
    try:
        health["drift"] = drift.cached(conn, Path(home), now=now, host=host, force=force, pins=pins)
        conn.commit()
    except Exception as error:
        health["drift_error"] = type(error).__name__
    try:
        health["memory"] = gateway_memory.reading(Path(home), host=host, signals=signals)
    except Exception as error:
        health["memory_error"] = type(error).__name__
    conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES (?, ?)", (HEALTH_KEY, json.dumps(health)))
    return health


def stored_health(conn) -> dict:
    row = conn.execute("SELECT value FROM cursor WHERE key=?", (HEALTH_KEY,)).fetchone()
    try:
        value = json.loads(row[0]) if row else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


# ── Auto for every profile ──────────────────────────────────────────────────────────────────────────────────────
def auto_state(home: Path) -> dict:
    """What `fleet auto on` recorded in the lead profile's database ({} when it was never used)."""
    from .environment import lead_home
    from .experience import experience

    target = lead_home(Path(home)) or Path(home)
    with experience(target) as conn:
        row = conn.execute("SELECT value FROM cursor WHERE key=?", (AUTO_KEY,)).fetchone()
    try:
        value = json.loads(row[0]) if row else {}
    except ValueError:
        value = {}
    return value if isinstance(value, dict) else {}


def _owner_platforms(home: Path, current: dict, *, now: float) -> list[str]:
    """The owner's platforms after adding the ones that are the owner's by construction (status.owner_suggestions)."""
    from .status import chat_senders, owner_suggestions

    owners = tuple(current.get("owner_sender_ids") or ())
    platforms = list(current.get("owner_platforms") or ())
    channels = chat_senders(Path(home), since=now - 30 * 86_400, owners=owners, platforms=tuple(platforms))
    for item in owner_suggestions(channels, owners=owners, platforms=tuple(platforms)):
        if item.get("kind") == "platform" and isinstance(item.get("value"), str) and item["value"] not in platforms:
            platforms.append(item["value"])
    return platforms


def enable(home: Path, *, now: float, profiles=None, owner: str = "keep", writer_for=None, actor: str = "terminal",
           read_config=None) -> dict:
    """Auto in every profile with the plugin on (or the ones named in `profiles`), through Hermes' own `hermes config
    set` (admin.write_settings, each change in that profile's history). `owner="suggested"` also counts the channels
    that are the owner's by construction as the owner. A profile that opted out of the fleet (`fleet_managed: false`)
    or whose configuration cannot be read is left as it is. Returns what changed, per profile."""
    from . import admin
    from .environment import _read_config, fleet_managed, has_plugin, lead_home, profiles as all_profiles, root_of
    from .experience import experience

    read_config = read_config or _read_config
    root = root_of(Path(home))
    wanted_names = set(profiles or ())
    results, before = [], {}
    for name, folder in all_profiles(root):
        if wanted_names and name not in wanted_names:
            continue
        folder = Path(folder)
        config = read_config(folder)
        if not isinstance(config, dict):
            results.append({"profile": name, "status": "unreadable"})
            continue
        if not has_plugin(folder, config):
            results.append({"profile": name, "status": "not_installed"})
            continue
        if not fleet_managed(folder, read_config=lambda _home: config):
            results.append({"profile": name, "status": "opted_out"})
            continue
        current = admin.current_settings(folder, config)
        wanted = {"mode": "auto", "lessons": "auto"}
        if not current.get("autopilot_minutes"):
            wanted["autopilot_minutes"] = 60
        if owner == "suggested":
            platforms = _owner_platforms(folder, current, now=now)
            if platforms != list(current.get("owner_platforms") or ()):
                wanted["owner_platforms"] = platforms
        changes = {key: value for key, value in wanted.items() if current.get(key) != value}
        if not changes:
            results.append({"profile": name, "status": "already"})
            continue
        writer = writer_for(folder) if writer_for is not None else None
        try:
            done = admin.write_settings(folder, config, changes, now=now, writer=writer, actor=actor)
        except ValueError as error:
            results.append({"profile": name, "status": "failed", "error": str(error)[:200]})
            continue
        written = list(done.get("written") or ())
        before[name] = {key: current.get(key) for key in written}
        results.append({"profile": name, "status": "changed", "written": written,
                        "values": {key: changes[key] for key in written}})
    target = lead_home(Path(home), read_config=read_config) or Path(home)
    with experience(target) as conn:
        row = conn.execute("SELECT value FROM cursor WHERE key=?", (AUTO_KEY,)).fetchone()
        try:
            state = json.loads(row[0]) if row else {}
        except ValueError:
            state = {}
        state = state if isinstance(state, dict) else {}
        kept = state.get("before") if isinstance(state.get("before"), dict) else {}
        for name, values in before.items():   # the first value seen before Auto is the one `off` puts back
            kept.setdefault(name, {})
            for key, value in values.items():
                kept[name].setdefault(key, value)
        state.update({"on": True, "at": now, "actor": actor, "before": kept,
                      "profiles": sorted({*state.get("profiles", ()), *[item["profile"] for item in results
                                                                        if item["status"] in ("changed", "already")]})})
        conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES (?, ?)", (AUTO_KEY, json.dumps(state)))
    return {"on": True, "results": results}


def disable(home: Path, *, now: float, writer_for=None, actor: str = "terminal", read_config=None) -> dict:
    """Put every profile `enable` changed back to the values it had before, where they are still what Auto set."""
    from . import admin
    from .environment import _read_config, lead_home, profiles as all_profiles, root_of
    from .experience import experience

    read_config = read_config or _read_config
    target = lead_home(Path(home), read_config=read_config) or Path(home)
    with experience(target) as conn:
        row = conn.execute("SELECT value FROM cursor WHERE key=?", (AUTO_KEY,)).fetchone()
    try:
        state = json.loads(row[0]) if row else {}
    except ValueError:
        state = {}
    kept = (state or {}).get("before") or {}
    homes = dict(all_profiles(root_of(Path(home))))
    results = []
    for name, values in sorted(kept.items()):
        folder = homes.get(name)
        if folder is None or not isinstance(values, dict):
            continue
        config = read_config(Path(folder))
        if not isinstance(config, dict):
            results.append({"profile": name, "status": "unreadable"})
            continue
        current = admin.current_settings(Path(folder), config)
        restore = {key: value for key, value in values.items() if value is not None and current.get(key) != value}
        defaults = {"mode": "assist", "lessons": "auto"}
        for key, value in values.items():   # a key that had no value of its own goes back to the default
            if value is None and key in defaults and current.get(key) != defaults[key]:
                restore[key] = defaults[key]
        if not restore:
            results.append({"profile": name, "status": "already"})
            continue
        writer = writer_for(Path(folder)) if writer_for is not None else None
        try:
            done = admin.write_settings(Path(folder), config, restore, now=now, writer=writer, actor=actor)
        except ValueError as error:
            results.append({"profile": name, "status": "failed", "error": str(error)[:200]})
            continue
        results.append({"profile": name, "status": "restored", "written": list(done.get("written") or ())})
    with experience(target) as conn:
        conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES (?, ?)",
                     (AUTO_KEY, json.dumps({"on": False, "at": now, "actor": actor, "before": {}, "profiles": []})))
    return {"on": False, "results": results}


# ── lessons proven in one profile, offered to the others ────────────────────────────────────────────────────────
def share_lessons(home: Path, *, now: float, read_config=None) -> list[dict]:
    """In the lead's pass, with Auto on for the fleet: each template lesson measured as effective in one profile in
    Auto is offered to every other profile in Auto that called the same tool in the last SHARE_TOOL_DAYS and has no
    lesson that says the same (in any state: one it turned down never comes back). It arrives waiting (packs.apply)
    and activates there only once that profile's own turns hit the failure. At most SHARE_PER_PASS per profile."""
    from . import packs
    from .environment import _read_config, has_plugin, profile_mode, profiles as all_profiles, root_of
    from .experience import day_offset, experience

    read_config = read_config or _read_config
    if not auto_state(Path(home)).get("on"):
        return []
    auto_homes = []
    for name, folder in all_profiles(root_of(Path(home))):
        config = read_config(Path(folder))
        if isinstance(config, dict) and has_plugin(Path(folder), config) and \
                profile_mode(Path(folder), read_config=lambda _home: config) == "auto":
            auto_homes.append((name, Path(folder)))
    offers: dict[str, list[dict]] = {}
    for name, folder in auto_homes:
        conn = _readonly(folder / "evolution" / "evolution.sqlite3")
        if conn is None:
            continue
        try:
            rows = conn.execute("""SELECT tool, klass, hint, rate_before, rate_after FROM lesson WHERE status='active'
                AND origin='derived' AND text IS NULL AND verdict='effective'""").fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()
        for row in rows:
            offers.setdefault(name, []).append({"kind": "template", "tool": row["tool"], "class": row["klass"],
                                                "hint": row["hint"], "verdict": "effective",
                                                "rate_before": row["rate_before"], "rate_after": row["rate_after"]})
    shared = []
    since = day_offset(now, SHARE_TOOL_DAYS - 1)
    for name, folder in auto_homes:
        lessons = [item for source, items in offers.items() if source != name for item in items]
        if not lessons:
            continue
        try:
            with experience(folder) as conn:
                used = {row[0] for row in conn.execute("SELECT DISTINCT tool FROM tool_daily WHERE day >= ? AND calls > 0",
                                                       (since,))}
                candidates = [item for item in lessons if item["tool"] in used]
                if not candidates:
                    continue
                pack = {"format": packs.FORMAT, "version": packs.PACK_VERSION, "profile": None,
                        "lessons": candidates[:packs.MAX_LESSONS]}
                planned = packs.plan(conn, pack)
                planned["add"] = planned["add"][:SHARE_PER_PASS]
                if not planned["add"]:
                    continue
                created = packs.apply(conn, planned, now=now, mode="auto", activate=False, source="fleet")
        except (sqlite3.Error, OSError, ValueError):
            continue
        shared += [{"profile": name, **item} for item in created]
    return shared


# ── the daily report and its schedule ───────────────────────────────────────────────────────────────────────────
def report_spec(home: Path, *, hour: int = 7, lang: str = "en", deliver: str = "local") -> dict:
    """A no-agent cron recipe that delivers the fleet report every day (written by `schedule`)."""
    import shlex

    from .admin import UNSAFE_ENV, _profile_args
    from .i18n import text

    if type(hour) is not int or not 0 <= hour <= 23:
        raise ValueError("Use an hour from 0 to 23")
    if lang not in ("en", "pt"):
        raise ValueError("Use the language en or pt")
    if not isinstance(deliver, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:@+-]{0,119}", deliver):
        raise ValueError("deliver must be a Hermes delivery target such as local or origin")
    profile = Path(home).expanduser().resolve()
    arguments = [*_profile_args(Path(home).expanduser()), "evolution", "fleet", "report", "--lang", lang]
    script = f'''"""Deliver the Atlas Evolution fleet report without an agent turn."""
import os
import shutil
import subprocess

launcher = shutil.which("hermes")
if not launcher:
    raise SystemExit({text("Hermes CLI is not on the scheduler PATH.", lang)!r})
env = {{key: value for key, value in os.environ.items() if key.upper() not in {tuple(UNSAFE_ENV)!r}}}
env["HERMES_HOME"] = {str(profile)!r}
try:
    result = subprocess.run([launcher, *{arguments!r}], capture_output=True, text=True, env=env,
                            encoding="utf-8", errors="replace", timeout=180, check=False)
    if result.returncode or len(result.stdout) > 65_536:
        raise SystemExit({text("Atlas Evolution fleet report failed. Run `hermes evolution fleet report` to inspect.", lang)!r})
except (OSError, subprocess.TimeoutExpired):
    raise SystemExit({text("Atlas Evolution fleet report could not be produced.", lang)!r}) from None

print(result.stdout.rstrip())
'''
    create_args = ["hermes", *_profile_args(Path(home).expanduser()), "cron", "create", f"0 {hour} * * *", "--name",
                   JOB_NAME, "--no-agent", "--script", SCRIPT_NAME, "--deliver", deliver]
    return {"script_name": SCRIPT_NAME, "script_path": str(profile / "scripts" / SCRIPT_NAME), "script_content": script,
            "create_args": create_args,
            "posix_command": f"HERMES_HOME={shlex.quote(str(profile))} {shlex.join(create_args)}"}


def scheduled(home: Path) -> dict | None:
    """The daily fleet report job of this profile in Hermes' scheduler, if there is one."""
    from .status import _load_json

    status, data = _load_json(Path(home) / "cron" / "jobs.json", 4 * 1024 * 1024)
    jobs = data.get("jobs") if status == "ok" and isinstance(data, dict) else None
    for job in jobs or ():
        if isinstance(job, dict) and job.get("name") == JOB_NAME:
            return {"id": str(job.get("id") or "")[:64], "enabled": job.get("enabled") is not False,
                    "schedule": str(job.get("schedule") or job.get("cron") or "")[:40]}
    return None


def schedule(home: Path, *, now: float, hour: int = 7, lang: str = "en", deliver: str = "local", runner=None,
             actor: str = "terminal") -> dict:
    """Schedule the daily fleet report in the lead profile as a Hermes cron job that runs no agent and spends no token
    (`--no-agent`, a script under the profile's scripts/). Once only. Recorded with its undo (`hermes cron remove`)."""
    import os
    import secrets

    from . import decisions
    from .admin import hermes_output
    from .environment import lead_home
    from .experience import experience

    target = lead_home(Path(home)) or Path(home)
    real = time.time()
    with experience(target) as conn:
        conn.execute("DELETE FROM cursor WHERE key='fleet_schedule_claim' AND CAST(value AS REAL) < ?",
                     (real - CLAIM_SECONDS,))
        claimed = conn.execute("INSERT OR IGNORE INTO cursor(key, value) VALUES ('fleet_schedule_claim', ?)",
                               (str(real),)).rowcount
    if not claimed:
        return {"scheduled": False, "reason": "busy"}
    temporary = None
    try:
        existing = scheduled(target)
        if existing:
            return {"scheduled": False, "reason": "already", "job": existing.get("id")}
        spec = report_spec(target, hour=hour, lang=lang, deliver=deliver)
        script = Path(spec["script_path"])
        script.parent.mkdir(parents=True, exist_ok=True)
        temporary = script.with_name(f".{script.name}.{secrets.token_hex(4)}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(spec["script_content"])
        os.replace(temporary, script)
        temporary = None
        args = spec["create_args"]
        args = args[args.index("cron"):]
        code, out = hermes_output(target, args, runner=runner, timeout=180)
        found = re.search(r"Created job:?\s*([A-Za-z0-9_-]{1,64})", out)
        if code != 0 or not found:
            raise ValueError(" ".join(out.split())[-200:] or "Hermes refused the command")
        with experience(target) as conn:
            decision = decisions.record_cron(conn, found.group(1), name=JOB_NAME, schedule=args[2], deliver=deliver,
                                             now=now, actor=actor)
        return {"scheduled": True, "job": found.group(1), "schedule": args[2], "deliver": deliver,
                "decision": decision, "profile": target.name if target.parent.name == "profiles" else "default"}
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        try:
            with experience(target) as conn:
                conn.execute("DELETE FROM cursor WHERE key='fleet_schedule_claim' AND value=?", (str(real),))
        except Exception:
            pass


# ── in plain words ──────────────────────────────────────────────────────────────────────────────────────────────
def _when(stamp, lang: str) -> str:
    from .status import local_time

    return local_time(stamp, lang) if isinstance(stamp, (int, float)) else "—"


def _age(seconds, lang: str) -> str:
    if seconds is None:
        return "nunca" if lang == "pt" else "never"
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))} min"
    if seconds < 2 * 86_400:
        return f"{int(seconds // 3600)} h"
    return f"{int(seconds // 86_400)} d"


_ATTENTION = {
    "fleet_pass_overdue": ("The learning pass is late in a profile in use (see the table): nothing runs it there. Since "
                           "0.14.0 a gateway started by a launcher of its own runs them; restart it once after updating.",
                           "A rodada de aprendizado está atrasada num perfil em uso (veja a tabela): nada a roda ali. "
                           "Desde a 0.14.0 um gateway iniciado por um lançador próprio roda as rodadas; reinicie-o uma "
                           "vez depois de atualizar."),
    "fleet_versions_differ": ("The profiles do not all have the same version of Atlas Evolution: `hermes evolution update "
                              "--all` brings them to one.",
                              "Os perfis não têm todos a mesma versão do Atlas Evolution: `hermes evolution update --all` "
                              "deixa todos iguais."),
    "fleet_gateway_behind": ("The gateway still runs an older version of the plugin than the one on disk: restart it to "
                             "load the new code.",
                             "O gateway ainda roda uma versão do plugin mais antiga que a do disco: reinicie-o para "
                             "carregar o código novo."),
}


def markdown(view: dict, lang: str = "en") -> str:
    """The fleet view as Markdown: what needs attention, the table, then the gateway, its memory, the update's drift
    and Hermes' warnings."""
    from . import drift, gateway_memory, logsignals

    pt = lang == "pt"
    now = view.get("at") or time.time()
    out = [f"# Atlas Evolution · {'todos os perfis' if pt else 'every profile'}", ""]
    auto = view.get("auto") or {}
    rows = view.get("profiles") or []
    enabled = [row for row in rows if row.get("enabled")]
    in_auto = [row for row in enabled if row.get("mode") == "auto"]
    late = [row for row in rows if row.get("overdue")]
    out.append(("- **Perfis:** " if pt else "- **Profiles:** ") +
               (f"{len(rows)} · plugin ligado em {len(enabled)} · Modo Auto em {len(in_auto)} · rodada atrasada em "
                f"{len(late)}" if pt else f"{len(rows)} · plugin on in {len(enabled)} · Auto mode in {len(in_auto)} · "
                f"pass late in {len(late)}"))
    if auto.get("on"):
        out.append(f"- **Auto para todos:** ligado desde {_when(auto.get('at'), lang)} (`hermes evolution fleet auto off` "
                   "desfaz)" if pt else
                   f"- **Auto for all:** on since {_when(auto.get('at'), lang)} (`hermes evolution fleet auto off` undoes it)")
    gateway = view.get("gateway") or {}
    if gateway:
        states = {"running": "rodando", "starting": "subindo", "draining": "esvaziando", "stopped": "parado",
                  "degraded": "degradado"}
        state = gateway.get("state") or "?"
        out.append(f"- **Gateway:** {states.get(state, state) if pt else state} · Hermes {gateway.get('version') or '?'}"
                   f" · pid {gateway.get('pid') or '?'}")
    from .status import _TEXT as status_text

    said = status_text.get(lang, status_text["en"])
    texts = [_ATTENTION[code][1 if pt else 0] if code in _ATTENTION else said[code]
             for code in view.get("attention") or () if code in _ATTENTION or code in said]
    if texts:
        out += ["", "## " + ("Pede atenção" if pt else "Needs attention"), ""] + [f"- {item}" for item in texts]
    header = ("| perfil | plugin | modo | última rodada | lições ativas / esperando | falhas 7 d | turnos 7 d (lentos) "
              "| ações auto 7 d |" if pt else
              "| profile | plugin | mode | last pass | lessons active / waiting | failures 7 d | turns 7 d (slow) "
              "| automatic 7 d |")
    out += ["", "## " + ("Perfis" if pt else "Profiles"), "", header, "|---|---|---|---|---:|---:|---:|---:|"]
    modes = {"auto": "Auto", "assist": "Assistido" if pt else "Assist", "manual": "Manual"}
    for row in sorted(rows, key=lambda item: (not item.get("enabled"), -(item.get("sessions_7d") or 0), item["profile"])):
        name = f"**{row['profile']}**" if row["profile"] == view.get("lead") else row["profile"]
        if not row.get("enabled"):
            out.append(f"| {name} | {'desligado' if pt else 'off'} | — | — | — | — | — | — |")
            continue
        last = row.get("last_pass")
        age = _age(now - last if last else None, lang)
        mark = " ⚠️" if row.get("overdue") else (" ✗" if row.get("pass_ok") is False else "")
        version = row.get("version") or "?"
        if row.get("gateway_version") and row["gateway_version"] != version:
            version += f" (gateway: {row['gateway_version']})"
        turns = f"{row.get('turns_7d', 0)} ({row.get('slow_7d', 0)})"
        out.append(f"| {name} | {version} | {modes.get(row.get('mode'), row.get('mode') or '?')} | {age}{mark} | "
                   f"{row.get('lessons_active', 0)} / {row.get('lessons_waiting', 0)} | {row.get('failures_7d', 0)} | "
                   f"{turns} | {row.get('automatic_7d', 0)} |")
    out.append("")
    out.append("Em **negrito**, o perfil que cuida de todos (faz as checagens da máquina uma vez só)." if pt else
               "In **bold**, the profile that looks after all of them (it runs the machine-wide checks once).")
    health = view.get("health") or {}
    if health:
        memory_lines = gateway_memory.lines(health.get("memory"), lang)
        if memory_lines:
            out += ["", "## " + ("Memória do gateway" if pt else "Gateway memory"), ""] + memory_lines
        drift_lines = drift.lines(health.get("drift"), lang)
        if drift_lines:
            out += ["", "## " + ("Depois da atualização do Hermes" if pt else "After the Hermes update"), ""] + drift_lines
        signal_lines = logsignals.lines(health.get("signals"), lang)
        if signal_lines:
            out += ["", "## " + ("Avisos do Hermes nos logs (7 dias)" if pt else "Hermes' warnings in its logs (7 days)"),
                    ""] + signal_lines
        out += ["", (f"Lido pela rodada do perfil `{view.get('lead')}` em {_when(health.get('at'), lang)}." if pt else
                     f"Read by profile `{view.get('lead')}`'s pass on {_when(health.get('at'), lang)}.")]
    return "\n".join(out).rstrip() + "\n"


def auto_markdown(home: Path, lang: str = "en", *, result: dict | None = None) -> str:
    """`fleet auto status|on|off` as text: the state, what changed, what Auto does by itself and what it never does."""
    pt = lang == "pt"
    state = auto_state(Path(home))
    out = []
    if result is not None:
        labels = {"changed": ("mudou", "changed"), "already": ("já estava", "already"),
                  "restored": ("voltou ao que era", "restored"), "not_installed": ("sem o plugin", "no plugin"),
                  "opted_out": ("não deixa a frota agir (fleet_managed: false)", "opted out (fleet_managed: false)"),
                  "unreadable": ("configuração ilegível agora", "configuration unreadable now"),
                  "failed": ("o Hermes recusou", "Hermes refused")}
        out += [f"# Atlas Evolution · {'Auto para todos os perfis' if pt else 'Auto for every profile'}: "
                f"{('ligado' if result.get('on') else 'desligado') if pt else ('on' if result.get('on') else 'off')}", ""]
        for item in result.get("results") or ():
            label = labels.get(item.get("status"), (item.get("status"), item.get("status")))[0 if pt else 1]
            values = ", ".join(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in
                               (item.get("values") or {}).items())
            extra = f" ({values})" if values else (f" ({item['error']})" if item.get("error") else "")
            out.append(f"- `{item['profile']}`: {label}{extra}")
        out.append("")
    else:
        on = state.get("on")
        out += [f"# Atlas Evolution · {'Auto para todos os perfis' if pt else 'Auto for every profile'}", "",
                (f"- **Situação:** {'ligado desde ' + _when(state.get('at'), lang) if on else 'desligado'}" if pt else
                 f"- **State:** {'on since ' + _when(state.get('at'), lang) if on else 'off'}"), ""]
    out.append("## " + ("O que o Auto faz sozinho (tudo com desfazer)" if pt else "What Auto does by itself (all with undo)"))
    out.append("")
    for _code, english, portuguese, undo_en, undo_pt in SAFE_ACTIONS:
        out.append(f"- {portuguese if pt else english} · {'desfazer' if pt else 'undo'}: {undo_pt if pt else undo_en}")
    out += ["", "## " + ("O que ele nunca faz" if pt else "What it never does"), ""]
    out += [f"- {item}" for item in (NEVER_PT if pt else NEVER)]
    out += ["", ("Desligar para todos e voltar cada perfil ao que era: `hermes evolution fleet auto off`." if pt else
                 "Turn it off for all and put each profile back as it was: `hermes evolution fleet auto off`.")]
    return "\n".join(out).rstrip() + "\n"


def report_markdown(home: Path, *, now: float | None = None, lang: str = "en", hours: int = 24) -> str:
    """The daily report: what the plugin did and learned in every profile in the last `hours`, and what needs the
    owner, then the fleet view."""
    now = time.time() if now is None else now
    pt = lang == "pt"
    view = overview(Path(home), now=now)
    since = now - hours * 3600
    learned, acted, waiting = [], [], []
    for row in view.get("profiles") or ():
        if not row.get("enabled"):
            continue
        conn = _readonly(Path(row["home"]) / "evolution" / "evolution.sqlite3")
        if conn is None:
            continue
        try:
            new = int(_scalar(conn, "SELECT COUNT(*) FROM lesson WHERE created >= ?", (since,)))
            retired = int(_scalar(conn, "SELECT COUNT(*) FROM lesson WHERE status='retired' AND updated >= ?", (since,)))
            actions = conn.execute("SELECT kind, target, action, after FROM decision_log WHERE actor='automatic' "
                                   "AND at >= ? ORDER BY id DESC LIMIT 10", (since,)).fetchall()
        except sqlite3.Error:
            new, retired, actions = 0, 0, []
        finally:
            conn.close()
        if new or retired:
            learned.append(f"- `{row['profile']}`: {_q(new, 'lição nova', 'lições novas')}, {_q(retired, 'aposentada', 'aposentadas')}" if pt else
                           f"- `{row['profile']}`: {_q(new, 'new lesson', 'new lessons')}, {retired} retired")
        for action in actions:
            try:
                after = json.loads(action["after"] or "{}")
            except ValueError:
                after = {}
            what = after.get("code") if isinstance(after, dict) and isinstance(after.get("code"), str) else \
                f"{action['kind']} {action['target']}"
            acted.append(f"- `{row['profile']}`: `{what}` ({action['action']}) · "
                         + ("desfazer: `hermes -p " if pt else "undo: `hermes -p ") + f"{row['profile']} evolution decisions list`")
        if row.get("lessons_waiting"):
            waiting.append(f"- `{row['profile']}`: {_q(row['lessons_waiting'], 'lição esperando', 'lições esperando')} você "
                           f"(`hermes -p {row['profile']} evolution lessons review`)" if pt else
                           f"- `{row['profile']}`: {_q(row['lessons_waiting'], 'lesson waiting', 'lessons waiting')} for you "
                           f"(`hermes -p {row['profile']} evolution lessons review`)")
    title = (f"# Atlas Evolution · relatório das últimas {hours} h · {_when(now, lang)}" if pt else
             f"# Atlas Evolution · report of the last {hours} h · {_when(now, lang)}")
    out = [title, ""]
    out += ["## " + ("O que aprendeu" if pt else "What it learned"), ""] + (learned or [
        "- Nada novo." if pt else "- Nothing new."])
    out += ["", "## " + ("O que fez sozinho" if pt else "What it did by itself"), ""] + (acted or [
        "- Nada." if pt else "- Nothing."])
    if waiting:
        out += ["", "## " + ("Esperando você" if pt else "Waiting for you"), ""] + waiting
    body = markdown(view, lang)
    out += ["", body.split("\n", 2)[2] if body.startswith("# ") else body]
    return "\n".join(out).rstrip() + "\n"
