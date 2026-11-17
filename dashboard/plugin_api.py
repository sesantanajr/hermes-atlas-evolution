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
            "runtime_guard", "mode")


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


# What the panel says when a request is refused before the plugin's own rules run (the panel maps the same codes).
_MESSAGES = {
    "en": {"busy": "The Evolution store is busy right now. Try again in a moment.",
           "open_dashboard": "Read-only: this dashboard has no login, so decisions are taken in the terminal.",
           "shared_token": "Read-only: this session uses the dashboard's shared token, not a login.",
           "other_profile": "This profile is decided from the dashboard's own profile, with a login.",
           "missing_header": "The request did not come from the panel; reload the page and try again.",
           "managed": "Your administrator manages this, so it cannot be changed here.",
           "refused": "It was not done"},
    "pt": {"busy": "O banco do Evolution está ocupado agora. Tente de novo em instantes.",
           "open_dashboard": "Só leitura: este dashboard não tem login, então as decisões ficam no terminal.",
           "shared_token": "Só leitura: esta sessão usa o token compartilhado do dashboard, não um login.",
           "other_profile": "Este perfil se decide pelo perfil do próprio dashboard, com login.",
           "missing_header": "O pedido não veio do painel; recarregue a página e tente de novo.",
           "managed": "O seu administrador gerencia isto, então não dá para mudar aqui.",
           "refused": "Não foi feito"},
}


def message(code: str, lang: str) -> str:
    """The sentence for a refusal code, in `lang` (English for an unknown language)."""
    texts = _MESSAGES.get(lang, _MESSAGES["en"])
    return texts.get(code) or _MESSAGES["en"].get(code) or code


def panel_language(setting, hint=None, profile=None, chosen=None) -> str:
    """The language the panel speaks for a profile: the plugin's own `language` setting when it is `en` or `pt`;
    with `auto` (or nothing set), the language chosen for the profile in Hermes (`chosen`: HERMES_LANGUAGE or
    display.language), then the dashboard's language (`hint`, what the panel sends as `lang`; the dashboard says
    English until someone picks another), then the profile's language as Hermes resolves it with the machine's locale
    (`profile`), then English."""
    i18n = _module("i18n")
    return (i18n.normalize(setting) or i18n.normalize(chosen) or i18n.normalize(hint) or i18n.normalize(profile)
            or "en")


def profile_language(home: Path, runtime_config: dict, hint=None) -> str:
    """panel_language for the profile at `home`, read from its settings. Never raises."""
    i18n = _module("i18n")
    config = runtime_config if isinstance(runtime_config, dict) else {}
    try:
        setting = current_settings(home, config).get("language")
    except Exception:   # a broken settings file still gets an answer in a language
        setting = None
    try:
        resolved = i18n.resolve(None, config)
        chosen = i18n.normalize(i18n._hermes_env_language()) or i18n.normalize(i18n._display_language(config))
    except Exception:
        resolved = chosen = None
    return panel_language(setting, hint, resolved, chosen)


def localized_result(result, lang: str):
    """A decision's result with its sentences in `lang`: a `reason` (or a skipped item's) that is a sentence, as the
    plugin's own refusals are, is translated; a reason that is a code stays a code (the panel words it)."""
    if not isinstance(result, dict):
        return result
    i18n = _module("i18n")

    def sentence(value):
        return i18n.text(value, lang) if isinstance(value, str) and " " in value else value

    result = dict(result)
    if "reason" in result:
        result["reason"] = sentence(result["reason"])
    if isinstance(result.get("skipped"), list):
        result["skipped"] = [{**item, "reason": sentence(item.get("reason"))} if isinstance(item, dict) else item
                             for item in result["skipped"]]
    return result


def decide_reply(home: Path, runtime_config: dict, body, *, now: float, hint=None, writer=None) -> tuple[int, dict]:
    """(HTTP status, JSON body) of one decision the route already allowed: the plugin's refusals and errors in the
    profile's language (panel_language), never as raw text of another language."""
    import sqlite3

    i18n = _module("i18n")
    lang = profile_language(home, runtime_config, hint)
    try:
        return 200, localized_result(decide(home, runtime_config, body, now=now, lang=lang, writer=writer), lang)
    except PermissionError as error:   # an administrator-managed key or a managed install
        detail = i18n.text(" ".join(str(error).split())[:200], lang)
        text = message("managed", lang) + (f" ({detail})" if detail else "")
        return 403, {"error": "managed", "message": text}
    except (KeyError, TypeError, ValueError) as error:
        detail = str(error.args[0]) if isinstance(error, KeyError) and error.args else str(error)
        if " " not in detail:   # a missing key or a bare value: the body was not a decision
            text = i18n.text("Invalid decision", lang)
        else:   # the plugin's sentences are catalogued; anything else (Hermes' own output) is quoted as it came
            text = i18n.text(detail, lang)
            if text == detail and lang != "en":
                text = f"{message('refused', lang)}: {detail}"
        return 400, {"error": "invalid", "message": text}
    except (OSError, sqlite3.Error):
        return 503, {"error": "busy", "message": message("busy", lang)}


def environment_extra(home: Path) -> dict:
    """What the Environment tab shows beside its text: Hermes' own doctor per profile (0.9.0), when the lead
    profile's last look recorded it. Never raises; empty when there is nothing."""
    try:
        found = _module("status")._environment(home)
    except Exception:
        return {}
    doctor = found.get("doctor") if isinstance(found, dict) else None
    if not isinstance(doctor, dict):
        return {}
    return {"doctor": {str(name): entry for name, entry in doctor.items() if isinstance(entry, dict)}}


def snapshot(home: Path, *, now: float, runtime_config: dict | None = None, lang: str | None = None,
             days: int = 7, can_decide: bool = False, decide_reason: str = "open_dashboard",
             ui_lang: str | None = None) -> dict:
    """Everything the panel shows, for one profile. Raises OSError or sqlite3.Error when the store is busy.
    The sender ids of the chat channels are included only for a request that may decide (the owner's login).
    `lang` forces a language; otherwise the profile's own setting decides and, with `auto`, `ui_lang` (the
    dashboard's language), then the profile's (panel_language)."""
    cli, evolve, status, report = _module("cli"), _module("evolve"), _module("status"), _module("report")
    lessons, proposals, experience = _module("lessons"), _module("proposals"), _module("experience")
    decisions = _module("decisions")
    runtime_config = runtime_config if isinstance(runtime_config, dict) else {}
    days = days if isinstance(days, int) and 1 <= days <= 35 else 7
    config, settings_problems = cli.safe_settings(home, runtime_config)
    opts = evolve.options(config, runtime_config, strict=False, problems=settings_problems)
    values = current_settings(home, runtime_config)
    lang = lang if lang in ("en", "pt") else panel_language(values.get("language"), ui_lang, opts["language"])
    state = status.build(home, now=now, config=config, runtime_config=runtime_config)
    digest = report.build(home, now=now, days=days, lang=lang, rates=config.get("rates_usd_per_million", {}),
                          own_transform=opts["tool_hints"] and opts["lessons"] != "off")
    with experience.experience(home) as conn:
        playbook = lessons.listing(conn, lang=lang)
        explain = _module("explain")
        queue = [{**item, "explain": explain.describe(item, lang)} for item in proposals.listing(conn, status="open")]
        recent = _module("actions").recent(conn, now=now)
        pending = decisions.waiting(conn, lang=lang)
        history = decisions.history(conn, lang=lang, limit=40)
        reload_row = conn.execute("SELECT value FROM cursor WHERE key='settings_reload'").fetchone()
    channels = status.chat_senders(home, since=now - 30 * 86_400, owners=opts["owner_sender_ids"],
                                   platforms=opts["owner_platforms"])
    suggestions = status.owner_suggestions(channels, owners=opts["owner_sender_ids"], platforms=opts["owner_platforms"])
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
    texts, care_values = status._TEXT.get(lang, status._TEXT["en"]), status._care_values(state)
    health_md = "\n".join("- " + texts[code].format(**care_values) for code in state.get("attention") or ()
                           if code.startswith(("hermes_", "os_")) and code in texts)
    return {
        "lang": lang, "days": days, "generated": now, "profile": state["profile"], "version": state["plugin_version"],
        "mode": opts["lessons"], "status": state, "report": digest, "lessons": playbook, "proposals": queue,
        "automatic": {"mode": opts["mode"], "recent": recent},
        "pending": pending, "history": history, "can_decide": can_decide, "decide_reason": decide_reason,
        "settings": {"values": values, "reload": reload_keys, "recommended": recommended,
                     "reload_keys": ["runtime_guard", "tool_hints"], "problems": sorted(set(settings_problems))},
        "owner": {"channels": channels, "suggestions": suggestions},
        "status_md": status.markdown(state, lang), "report_md": report.markdown(digest, lang), "health_md": health_md,
        "environment_md": status.environment_markdown(home, lang, mode=opts["mode"]),
        "environment": environment_extra(home),
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
    def get_snapshot(request: Request, days: int = Query(7, ge=1, le=35), lang: str = Query("", max_length=8)):
        # A plain def: FastAPI runs it in a worker thread, so reading the store never blocks the event loop.
        # `lang` is the dashboard's language: it counts only where the profile's own language setting is `auto`.
        import sqlite3

        i18n = _module("i18n")
        try:
            home, config = _current(_target(request))
        except ValueError as error:
            told = i18n.normalize(lang) or "en"
            return JSONResponse({"error": "invalid", "message": i18n.text(str(error), told)}, status_code=400)
        allowed, why = _policy(request)
        if allowed and not _ticket_allows(request, home):
            allowed, why = False, "other_profile"
        try:
            return snapshot(home, now=time.time(), runtime_config=config, ui_lang=lang or None, days=days,
                            can_decide=allowed, decide_reason=why)
        except (OSError, sqlite3.Error):
            told = profile_language(home, config, lang)
            return JSONResponse({"error": "busy", "message": message("busy", told)}, status_code=503)

    @router.post("/decide")
    def post_decide(request: Request, body: dict = Body(...), lang: str = Query("", max_length=8)):
        i18n = _module("i18n")
        allowed, why = _policy(request)
        told = i18n.normalize(lang) or "en"   # until the profile is known
        if not allowed:
            return JSONResponse({"error": why, "message": message(why, told)}, status_code=403)
        if request.headers.get("x-atlas-evolution") != "1":
            return JSONResponse({"error": "missing_header", "message": message("missing_header", told)}, status_code=403)
        try:
            home, config = _current(_target(request))
        except ValueError as error:
            return JSONResponse({"error": "invalid", "message": i18n.text(str(error), told)}, status_code=400)
        except OSError:
            return JSONResponse({"error": "busy", "message": message("busy", told)}, status_code=503)
        if not _ticket_allows(request, home):
            told = profile_language(home, config, lang)
            return JSONResponse({"error": "other_profile", "message": message("other_profile", told)}, status_code=403)
        code, reply = decide_reply(home, config, body, now=time.time(), hint=lang or None)
        return reply if code == 200 else JSONResponse(reply, status_code=code)

    return router


router = _router()
