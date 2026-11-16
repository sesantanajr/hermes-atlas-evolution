"""Atlas Evolution panel for the Hermes dashboard, mounted at /api/plugins/hermes-atlas-evolution/.

It shows what `hermes evolution status`, `report`, `lessons list`, `proposals list` and `decisions list` show,
for the profile the dashboard is looking at (Hermes scopes every plugin request to that profile and refuses it
where the plugin is disabled), and since 0.8.1 it lets the owner decide there too: approve, reject, retire or
reactivate a lesson, approve every waiting lesson at once, decide a proposal, change the settings, choose who
counts as the owner, and undo any of it.

Who may decide. Every request here is already authenticated by the dashboard. A decision is accepted only when
the request carries no dashboard session token: with a dashboard login (password or OAuth) the browser sends
the session cookie and no token, and the Desktop app sends its own grant. Without a login, the dashboard hands
its token to anyone who loads the page, the agent's terminal included, so there the panel stays read-only and
says why. A decision also needs the `X-Atlas-Evolution: 1` header, which a page of another site cannot send
without the browser asking first. The runtime guard vetoes the agent's own commands that call these routes.

Settings are written with Hermes' own public command, `hermes --profile <name> config set
plugins.entries.hermes-atlas-evolution.settings.<key> <value>` (the one the documentation and `hermes evolution
owner` give the owner), into the profile's config.yaml, and the running gateway picks them up within a minute
(integration.refresh_settings); the runtime guard and tool hints apply at the next load of the plugin.

The plugin's code is loaded from the folder next to this one under a private module name, so the panel never
shares state with a copy of the plugin the dashboard process may have loaded for its hooks.
"""

# No `from __future__ import annotations`: FastAPI reads the route parameters' annotations as objects, and the
# routes import Request inside _router(), where a string annotation could not be resolved.
import importlib
import importlib.util
import re
import sys
import threading
import time
from pathlib import Path

PACKAGE = "hermes_atlas_evolution_panel"
PLUGIN_ID = "hermes-atlas-evolution"
_LOCK = threading.Lock()
# Settings the panel may change: those of the plugin's settings form (plugin.yaml config_schema).
SETTINGS = ("lessons", "language", "tool_hints", "feedback_signals", "agent_tools", "slow_turn_seconds",
            "owner_sender_ids", "owner_platforms", "reflector", "reflector_daily_limit", "autopilot_minutes",
            "runtime_guard")
# Where the form's key and the plugin's option differ (evolve.options names the agent tools agent_advice).
_OPTION = {"agent_tools": "agent_advice"}


def _module(name: str):
    """A module of the plugin's package, imported once under PACKAGE."""
    with _LOCK:
        if PACKAGE not in sys.modules:
            folder = Path(__file__).resolve().parent.parent / "hermes_atlas_evolution"
            spec = importlib.util.spec_from_file_location(PACKAGE, folder / "__init__.py",
                                                          submodule_search_locations=[str(folder)])
            package = importlib.util.module_from_spec(spec)
            sys.modules[PACKAGE] = package
            try:
                spec.loader.exec_module(package)
            except Exception:
                sys.modules.pop(PACKAGE, None)
                raise
    return importlib.import_module(f"{PACKAGE}.{name}")


def _prefix(profile: str) -> str:
    return "hermes" if profile == "default" else f"hermes -p {profile}"


_LOGIN_COOKIE = re.compile(r"(?:^|;)\s*hermes_session_at(?:_[\w-]+)?=[^;\s]")


def decision_policy(headers, query, *, state=None, app_state=None) -> tuple[bool, str]:
    """(may decide, why) for a request the dashboard already authenticated. `headers` and `query` are the request's
    case-insensitive mappings; `state` and `app_state` are request.state and request.app.state when this router runs
    in the dashboard process (absent when Hermes forwards the request to a plugin host).

    Only the owner's login decides: the session cookie a dashboard login (password or OAuth) sets, or the grant
    Hermes Desktop sends (verified by Hermes before this router runs). The dashboard's session token, in its header,
    in `?token=` or as `Authorization: Bearer` (Hermes' legacy form), is what a dashboard without a login hands to
    any program on the machine, the agent's terminal included: with it, nothing is decided here."""
    if headers.get("x-hermes-session-token") or query.get("token") or headers.get("authorization"):
        return False, "open_dashboard"
    if state is not None and getattr(state, "native_http_principal", None):
        return True, "desktop"
    if app_state is not None and getattr(app_state, "auth_required", None) is False:
        return False, "open_dashboard"   # the dashboard process itself says it has no login
    if headers.get("x-hermes-gateway-ticket"):
        return True, "desktop"   # Hermes verified the grant before forwarding (a forged one is refused there)
    if _LOGIN_COOKIE.search(headers.get("cookie") or ""):
        return True, "login"
    return False, "open_dashboard"


def current_settings(home: Path, runtime_config: dict) -> dict:
    """The value of each setting as the plugin uses it now, by its key in the settings form."""
    return _module("cli").setting_values(home, runtime_config)


def snapshot(home: Path, *, now: float, runtime_config: dict | None = None, lang: str | None = None,
             days: int = 7, can_decide: bool = False, decide_reason: str = "open_dashboard") -> dict:
    """Everything the panel shows, for one profile. Raises OSError or sqlite3.Error when the store is busy.
    The sender ids of the chat channels are included only for a request that may decide (the owner's login)."""
    cli, evolve, status, report = _module("cli"), _module("evolve"), _module("status"), _module("report")
    lessons, proposals, experience = _module("lessons"), _module("proposals"), _module("experience")
    decisions = _module("decisions")
    runtime_config = runtime_config if isinstance(runtime_config, dict) else {}
    days = days if isinstance(days, int) and 1 <= days <= 35 else 7
    config, settings_problems = cli.safe_settings(home, runtime_config)
    opts = evolve.options(config, runtime_config, strict=False, problems=settings_problems)
    lang = lang if lang in ("en", "pt") else opts["language"]
    state = status.build(home, now=now, config=config, runtime_config=runtime_config)
    digest = report.build(home, now=now, days=days, lang=lang, rates=config.get("rates_usd_per_million", {}),
                          own_transform=opts["tool_hints"] and opts["lessons"] != "off")
    with experience.experience(home) as conn:
        playbook = lessons.listing(conn, lang=lang)
        queue = proposals.listing(conn, status="open")
        pending = decisions.waiting(conn, lang=lang)
        history = decisions.history(conn, lang=lang, limit=40)
        reload_row = conn.execute("SELECT value FROM cursor WHERE key='settings_reload'").fetchone()
    channels = status.chat_senders(home, since=now - 30 * 86_400, owners=opts["owner_sender_ids"],
                                   platforms=opts["owner_platforms"])
    suggestions = status.owner_suggestions(channels, owners=opts["owner_sender_ids"], platforms=opts["owner_platforms"])
    if not can_decide:   # counts only, as `status` shows them
        channels = [{**channel, "senders": [{"id": None, "sessions": item["sessions"], "owner": item["owner"]}
                                            for item in channel["senders"]]} for channel in channels]
        suggestions = [item for item in suggestions if item["kind"] == "platform"]
        history = decisions.masked(history)
    try:
        import json

        reload_keys = [key for key in json.loads(reload_row[0]) if key in SETTINGS] if reload_row else []
    except (TypeError, ValueError):
        reload_keys = []
    hermes = _prefix(state["profile"])
    return {
        "lang": lang, "days": days, "generated": now, "profile": state["profile"], "version": state["plugin_version"],
        "mode": opts["lessons"], "status": state, "report": digest, "lessons": playbook, "proposals": queue,
        "pending": pending, "history": history, "can_decide": can_decide, "decide_reason": decide_reason,
        "settings": {"values": current_settings(home, runtime_config), "reload": reload_keys,
                     "reload_keys": ["runtime_guard", "tool_hints"], "problems": sorted(set(settings_problems))},
        "owner": {"channels": channels, "suggestions": suggestions},
        "status_md": status.markdown(state, lang), "report_md": report.markdown(digest, lang),
        "commands": {"review": f"{hermes} evolution lessons review", "learn": f"{hermes} evolution learn",
                     "approve_lesson": f"{hermes} evolution lessons approve {{id}}",
                     "retire_lesson": f"{hermes} evolution lessons retire {{id}}",
                     "approve_proposal": f"{hermes} evolution proposals approve {{id}}",
                     "reject_proposal": f"{hermes} evolution proposals reject {{id}}",
                     "export": f"{hermes} evolution lessons export --out lessons.json",
                     "import": f"{hermes} evolution lessons import --from <profile> --dry-run"},
    }


_BOOLEANS = ("tool_hints", "feedback_signals", "agent_tools", "reflector", "runtime_guard")


def validate_settings(values: dict) -> None:
    """Refuse a value the plugin would not use as given, with the plugin's own rules (evolve.options, strict), before
    anything is written: Hermes' `config set` checks the key, not the plugin's ranges."""
    cli, evolve = _module("cli"), _module("evolve")
    candidate: dict = {"evolution": {}}
    for key, value in values.items():
        path = cli.NATIVE_KEYS[key]
        if len(path) == 1:
            candidate[path[0]] = value
        else:
            candidate.setdefault(path[0], {})[path[1]] = value
        if key in _BOOLEANS and type(value) is not bool:
            raise ValueError(f"{key} must be true or false")
    if "language" in values and values["language"] not in ("auto", "en", "pt"):
        raise ValueError("language must be auto, en or pt")
    evolve.options(candidate, {}, strict=True)


def write_settings(home: Path, runtime_config: dict, values: dict, *, now: float, writer=None) -> dict:
    """Write settings through Hermes' plugin-settings writer and log each change (so it can be undone).
    Unknown keys and values Hermes' schema refuses raise ValueError; nothing is written then."""
    if not isinstance(values, dict) or not values or any(key not in SETTINGS for key in values):
        raise ValueError("Unknown setting")
    validate_settings(values)
    before = current_settings(home, runtime_config)
    changed = {key: value for key, value in values.items() if before.get(key) != value}
    if not changed:
        return {"written": [], "reload": []}
    written = (writer or cli_writer(home))(changed)
    decisions, experience = _module("decisions"), _module("experience")
    with experience.experience(home) as conn:
        for key in written:
            decisions.record_setting(conn, key, before.get(key), changed[key], now=now, actor="dashboard")
    return {"written": list(written), "reload": [key for key in written if key in ("runtime_guard", "tool_hints")]}


def _hermes() -> str | None:
    import shutil

    found = shutil.which("hermes")
    for candidate in (found, "/usr/local/bin/hermes", str(Path.home() / ".local" / "bin" / "hermes")):
        if candidate and Path(candidate).is_file() and Path(candidate).suffix.lower() not in (".bat", ".cmd"):
            return candidate
    return None


def cli_writer(home: Path, *, runner=None):
    """A writer that sets each value with `hermes --profile <name> config set ...`, Hermes' public command, which
    checks the key, takes Hermes' config lock and writes atomically. Raises ValueError with Hermes' own message."""
    import json
    import os
    import subprocess

    profile = Path(home).resolve()
    name = profile.name if profile.parent.name == "profiles" else "default"

    def write(values: dict) -> list:
        executable = _hermes()
        if executable is None:
            raise ValueError("Hermes command not found")
        written = []
        for key, value in values.items():
            literal = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, tuple, bool)) else str(value)
            command = [executable, "--profile", name, "config", "set", f"plugins.entries.{PLUGIN_ID}.settings.{key}", literal]
            run = subprocess.run if runner is None else runner
            result = run(command, env=dict(os.environ, HERMES_HOME=str(profile)), cwd=str(profile), stdin=subprocess.DEVNULL,
                         capture_output=True, encoding="utf-8", errors="replace", timeout=60, check=False)
            if result.returncode != 0:
                detail = " ".join((result.stderr or result.stdout or "").split())[:200]
                raise ValueError(detail or "Hermes refused the setting")
            written.append(key)
        return written

    return write


def decide(home: Path, runtime_config: dict, body: dict, *, now: float, lang: str = "en", writer=None) -> dict:
    """One decision from the panel. `body`: {"kind": lesson|all|proposal|undo|settings, ...}."""
    decisions, experience = _module("decisions"), _module("experience")
    kind = body.get("kind") if isinstance(body, dict) else None
    if kind == "settings":
        return write_settings(home, runtime_config, body.get("values"), now=now, writer=writer)
    with experience.experience(home) as conn:
        if kind == "lesson":
            return decisions.decide_lesson(conn, int(body["id"]), str(body.get("action")), now=now, actor="dashboard",
                                           lang=lang, shown=body.get("shown") if isinstance(body.get("shown"), str) else None)
        if kind == "all":
            items = body.get("items")
            if not isinstance(items, dict) or len(items) > 500:
                raise ValueError("Invalid decision")
            return decisions.approve_all(conn, items, now=now, actor="dashboard", lang=lang)
        if kind == "proposal":
            return decisions.decide_proposal(conn, int(body["id"]), str(body.get("action")), now=now, actor="dashboard",
                                             lang=lang)
        if kind == "undo":
            decision_id = int(body["id"])
            row = conn.execute("SELECT kind, target FROM decision_log WHERE id=?", (decision_id,)).fetchone()
            current = current_settings(home, runtime_config).get(row["target"]) \
                if row is not None and row["kind"] == "setting" else None
            result = decisions.undo(conn, decision_id, now=now, actor="dashboard", current=current)
        else:
            raise ValueError("Invalid decision")
    if result.get("kind") == "setting" and result.get("restorable"):
        # written first; marked undone only once Hermes took the value back
        result["settings"] = write_settings(home, runtime_config, {result["key"]: result["restore"]}, now=now, writer=writer)
        with experience.experience(home) as conn:
            decisions.mark_undone(conn, result["id"], now=now)
        result["undone"] = True
    return result


def _current() -> tuple[Path, dict]:
    """The home and configuration of the profile this request is scoped to."""
    from hermes_constants import get_hermes_home

    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
    except Exception:   # the panel still shows the plugin's own data
        config = {}
    return Path(get_hermes_home()), config


def _router():
    try:
        from fastapi import APIRouter, Body, Query, Request
        from fastapi.responses import JSONResponse
    except ImportError:   # not the dashboard: nothing to mount
        return None
    router = APIRouter()

    @router.get("/snapshot")
    def get_snapshot(request: Request, days: int = Query(7, ge=1, le=35), lang: str = Query("", max_length=2)):
        # A plain def: FastAPI runs it in a worker thread, so reading the store never blocks the event loop.
        import sqlite3

        allowed, why = decision_policy(request.headers, request.query_params, state=request.state,
                                       app_state=request.app.state)
        try:
            home, config = _current()
            return snapshot(home, now=time.time(), runtime_config=config, lang=lang or None, days=days,
                            can_decide=allowed, decide_reason=why)
        except (OSError, sqlite3.Error):
            return JSONResponse({"error": "busy"}, status_code=503)

    @router.post("/decide")
    def post_decide(request: Request, body: dict = Body(...), lang: str = Query("", max_length=2)):
        import sqlite3

        allowed, why = decision_policy(request.headers, request.query_params, state=request.state,
                                       app_state=request.app.state)
        if not allowed:
            return JSONResponse({"error": why}, status_code=403)
        if request.headers.get("x-atlas-evolution") != "1":
            return JSONResponse({"error": "missing_header"}, status_code=403)
        i18n = _module("i18n")
        try:
            home, config = _current()
            return decide(home, config, body, now=time.time(), lang=lang if lang in ("en", "pt") else "en")
        except (KeyError, TypeError, ValueError) as error:
            return JSONResponse({"error": "invalid", "message": i18n.text(str(error), lang or "en")}, status_code=400)
        except PermissionError as error:   # an administrator-managed key or a managed install
            return JSONResponse({"error": "managed", "message": str(error)[:200]}, status_code=403)
        except (OSError, sqlite3.Error):
            return JSONResponse({"error": "busy"}, status_code=503)

    return router


router = _router()
