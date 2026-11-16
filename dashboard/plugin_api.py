"""Atlas Evolution panel for the Hermes dashboard, mounted at /api/plugins/hermes-atlas-evolution/.

It shows what `hermes evolution status`, `report`, `lessons list`, `proposals list` and `decisions list` show,
for the profile the dashboard is looking at (Hermes scopes every plugin request to that profile and refuses it
where the plugin is disabled), and lets the owner decide there too: approve, reject, retire or reactivate a lesson,
approve every waiting lesson at once, decide a proposal, change the settings, choose who counts as the owner, and
undo any of it. The rules are the terminal menu's (admin.py), so both places decide the same way.

Who may decide (decision_policy). Every request here is already authenticated by the dashboard. A decision is
accepted only from the owner's login: a dashboard login (password or OAuth) that Hermes verified, or the Desktop
app's grant, and only in the dashboard's own process, where Hermes' login gate leaves the session it verified.
Without a login, the dashboard hands its token to anyone who loads the page, the agent's terminal included, so
there the panel stays read-only and says why. A decision also needs the `X-Atlas-Evolution: 1` header, which a
page of another site cannot send without the browser asking first. The runtime guard vetoes the agent's own
commands that call these routes.

Other profiles. Hermes serves the panel of a profile other than the dashboard's own from that profile's plugin
host, with no credential and a verdict that is the same for a login and for the open dashboard's token, so the
plugin host never decides. The panel then asks the dashboard's own process instead (`?profile=current`, the
dashboard's profile for Hermes), naming the profile on screen in `target`: that profile's data and settings are
read and decided there, under the same login rule, when Atlas Evolution is installed and enabled in it.

Settings are written with Hermes' own public command, `hermes --profile <name> config set
plugins.entries.hermes-atlas-evolution.settings.<key> <value>`, into the profile's config.yaml, and the running
gateway picks them up within a minute (integration.refresh_settings); the runtime guard and tool hints apply at the
next load of the plugin.

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
# Settings the panel may change: those of the plugin's settings form (plugin.yaml config_schema); admin.SETTINGS.
SETTINGS = ("lessons", "language", "tool_hints", "feedback_signals", "agent_tools", "slow_turn_seconds",
            "owner_sender_ids", "owner_platforms", "reflector", "reflector_daily_limit", "autopilot_minutes",
            "runtime_guard")


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


# Hermes names the login cookie by the request's shape (hermes_cli/dashboard_auth/cookies.py): bare over HTTP,
# `__Host-` on HTTPS (a public_url behind a proxy, as most logins are), `__Secure-` under a path prefix, with
# `_<gateway>` on the gateway's Webapp. Read only where Hermes publishes no verified session (older hosts).
_LOGIN_COOKIE = re.compile(r"(?:^|;)\s*(?:__Host-|__Secure-)?hermes_session_at(?:_[\w-]+)?=[^;\s]")
_SHARED_TOKEN_PROVIDER = "session-token"   # Hermes' deployment session token, as a session provider


def decision_policy(headers, query, *, state=None, app_state=None) -> tuple[bool, str]:
    """(may decide, why) for a request the dashboard already authenticated. `headers` and `query` are the request's
    case-insensitive mappings; `state` and `app_state` are request.state and request.app.state.

    Only the owner's login decides: a dashboard login Hermes verified (password or OAuth), or the grant Hermes
    Desktop sends. The dashboard's session token, in its header, in `?token=` or as `Authorization: Bearer`
    (Hermes' legacy form), is what a dashboard without a login hands to any program on the machine, the agent's
    terminal included: with it, nothing is decided here.

    Where this runs decides what it can see:
    - in the dashboard process (the profile that launched it), Hermes' login gate leaves the verified session on
      request.state.session, with the provider that verified it;
    - in a secondary profile's plugin host, Hermes forwards no credential (no cookie, no token) and only its own
      verdict, request.state.hosted_http_authenticated, which is True for a login and for the open dashboard's
      token alike, and the plugin host's environment hides how the dashboard admits people: nothing is decided
      there ("other_profile"), and the panel asks the dashboard's own process (see the module's docstring)."""
    if headers.get("x-hermes-session-token") or query.get("token") or headers.get("authorization"):
        return False, "open_dashboard"
    if state is not None and getattr(state, "native_http_principal", None):
        return True, "desktop"
    if state is not None and getattr(state, "hosted_http_authenticated", None) is not None:
        return False, "other_profile"
    required = getattr(app_state, "auth_required", None) if app_state is not None else None
    if required is False:
        return False, "open_dashboard"   # the dashboard process itself says it has no login
    if required is True:   # Hermes' gate ran: only the session it verified counts
        session = getattr(state, "session", None) if state is not None else None
        if session is None:
            return False, "open_dashboard"
        if getattr(session, "provider", None) == _SHARED_TOKEN_PROVIDER:
            return False, "shared_token"
        return True, "login"
    if headers.get("x-hermes-gateway-ticket"):
        return True, "desktop"   # Hermes verified the grant before forwarding (a forged one is refused there)
    if _LOGIN_COOKIE.search(headers.get("cookie") or ""):
        return True, "login"
    return False, "open_dashboard"


def current_settings(home: Path, runtime_config: dict) -> dict:
    """The value of each setting as the plugin uses it now, by its key in the settings form."""
    return _module("admin").current_settings(home, runtime_config)


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
    values = current_settings(home, runtime_config)
    recommended = _module("admin").recommended_settings(values, suggestions) if can_decide else None
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
        "settings": {"values": values, "reload": reload_keys, "recommended": recommended,
                     "reload_keys": ["runtime_guard", "tool_hints"], "problems": sorted(set(settings_problems))},
        "owner": {"channels": channels, "suggestions": suggestions},
        "status_md": status.markdown(state, lang), "report_md": report.markdown(digest, lang),
        "commands": {"menu": f"{hermes} evolution", "review": f"{hermes} evolution lessons review",
                     "learn": f"{hermes} evolution learn",
                     "approve_lesson": f"{hermes} evolution lessons approve {{id}}",
                     "retire_lesson": f"{hermes} evolution lessons retire {{id}}",
                     "approve_proposal": f"{hermes} evolution proposals approve {{id}}",
                     "reject_proposal": f"{hermes} evolution proposals reject {{id}}",
                     "export": f"{hermes} evolution lessons export --out lessons.json",
                     "import": f"{hermes} evolution lessons import --from <profile> --dry-run"},
    }


def validate_settings(values: dict) -> None:
    _module("admin").validate_settings(values)


def write_settings(home: Path, runtime_config: dict, values: dict, *, now: float, writer=None) -> dict:
    return _module("admin").write_settings(home, runtime_config, values, now=now, writer=writer, actor="dashboard")


def cli_writer(home: Path, *, runner=None):
    return _module("admin").cli_writer(home, runner=runner)


def decide(home: Path, runtime_config: dict, body: dict, *, now: float, lang: str = "en", writer=None) -> dict:
    """One decision from the panel. `body`: {"kind": lesson|all|proposal|undo|settings|memory, ...}."""
    return _module("admin").decide(home, runtime_config, body, now=now, lang=lang, writer=writer, actor="dashboard")


def _hosted(request) -> bool:
    return getattr(request.state, "hosted_http_authenticated", None) is not None


def _policy(request) -> tuple[bool, str]:
    return decision_policy(request.headers, request.query_params, state=request.state, app_state=request.app.state)


def _current(target: str | None = None) -> tuple[Path, dict]:
    """The home and configuration of the profile this request acts on: the one Hermes scoped it to, or `target`, a
    profile of the same Hermes where the plugin is installed and enabled (admin.profile_home)."""
    from hermes_constants import get_hermes_home

    home = Path(get_hermes_home())
    if target:
        return _module("admin").profile_home(home, target)
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
    except Exception:   # the panel still shows the plugin's own data
        config = {}
    return home, config


def _ticket_allows(request, home: Path) -> bool:
    """A Hermes Desktop grant is bound to one profile (Hermes refuses its `?profile=` elsewhere): `target` may name
    only that profile for it. A login is not bound to a profile."""
    principal = getattr(request.state, "native_http_principal", None)
    if not principal:
        return True
    bound = principal.get("profile_id") if isinstance(principal, dict) else getattr(principal, "profile_id", None)
    try:
        return bool(bound) and Path(str(bound)).resolve() == Path(home).resolve()
    except (OSError, ValueError):
        return False


def _target(request) -> str | None:
    """The profile named by the panel, honored only in the dashboard's own process and scope: a plugin host or a
    request Hermes scoped to another profile acts on that profile alone."""
    target = request.query_params.get("target") or None
    if target is None:
        return None
    if _hosted(request) or (request.query_params.get("profile") or "current").strip().lower() != "current":
        raise ValueError("Unknown profile")
    return target


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

        i18n = _module("i18n")
        try:
            home, config = _current(_target(request))
        except ValueError as error:
            return JSONResponse({"error": "invalid", "message": i18n.text(str(error), lang or "en")}, status_code=400)
        allowed, why = _policy(request)
        if allowed and not _ticket_allows(request, home):
            allowed, why = False, "other_profile"
        try:
            return snapshot(home, now=time.time(), runtime_config=config, lang=lang or None, days=days,
                            can_decide=allowed, decide_reason=why)
        except (OSError, sqlite3.Error):
            return JSONResponse({"error": "busy"}, status_code=503)

    @router.post("/decide")
    def post_decide(request: Request, body: dict = Body(...), lang: str = Query("", max_length=2)):
        import sqlite3

        allowed, why = _policy(request)
        if not allowed:
            return JSONResponse({"error": why}, status_code=403)
        if request.headers.get("x-atlas-evolution") != "1":
            return JSONResponse({"error": "missing_header"}, status_code=403)
        i18n = _module("i18n")
        try:
            home, config = _current(_target(request))
            if not _ticket_allows(request, home):
                return JSONResponse({"error": "other_profile"}, status_code=403)
            return decide(home, config, body, now=time.time(), lang=lang if lang in ("en", "pt") else "en")
        except (KeyError, TypeError, ValueError) as error:
            return JSONResponse({"error": "invalid", "message": i18n.text(str(error), lang or "en")}, status_code=400)
        except PermissionError as error:   # an administrator-managed key or a managed install
            return JSONResponse({"error": "managed", "message": str(error)[:200]}, status_code=403)
        except (OSError, sqlite3.Error):
            return JSONResponse({"error": "busy"}, status_code=503)

    return router


router = _router()
