"""What the owner administers, the same way from the dashboard panel and from the terminal menu (0.8.2).

- The settings the owner may change: those of the plugin's settings form (plugin.yaml config_schema), checked
  with the plugin's own rules before anything is written, and written with Hermes' public command
  `hermes --profile <name> config set plugins.entries.hermes-atlas-evolution.settings.<key> <value>`.
- Every decision (a lesson, approve all, a proposal, a setting, a memory entry) and its undo, logged in one
  history (decisions.decision_log) with who decided where.
- The recommended setup: lessons on auto, the reflector on, and the chat channels that are the owner's by
  construction (status.owner_suggestions).
- Which profile a panel request acts on: the dashboard's own, or another profile of the same Hermes named by the
  panel (`profile_home`), where this plugin is installed and enabled. Decisions are taken only in the dashboard's
  own process, where Hermes' login gate leaves the session it verified; a secondary profile's plugin host gets no
  credential from Hermes, so the panel never decides there (see dashboard/plugin_api.py).

Callers check that the owner is the one deciding (cli._operator_only in a terminal, the panel's login rule).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

PLUGIN_ID = "hermes-atlas-evolution"
SETTINGS = ("lessons", "language", "tool_hints", "feedback_signals", "agent_tools", "slow_turn_seconds",
            "owner_sender_ids", "owner_platforms", "reflector", "reflector_daily_limit", "autopilot_minutes",
            "runtime_guard", "mode")
BOOLEANS = ("tool_hints", "feedback_signals", "agent_tools", "reflector", "runtime_guard")
CHOICES = {"lessons": ("auto", "suggest", "off"), "language": ("auto", "en", "pt"), "mode": ("auto", "assist", "manual")}
LISTS = ("owner_sender_ids", "owner_platforms")
NUMBERS = ("slow_turn_seconds", "reflector_daily_limit", "autopilot_minutes")
RELOAD_KEYS = ("runtime_guard", "tool_hints")   # applied at the next load of the plugin (integration.RELOAD_KEYS)


def current_settings(home: Path, runtime_config: dict) -> dict:
    """The value of each setting as the plugin uses it now, by its key in the settings form."""
    from .cli import setting_values

    return setting_values(home, runtime_config if isinstance(runtime_config, dict) else {})


def validate_settings(values: dict) -> None:
    """Refuse a value the plugin would not use as given, with the plugin's own rules (evolve.options, strict), before
    anything is written: Hermes' `config set` checks the key, not the plugin's ranges."""
    from .cli import NATIVE_KEYS
    from .evolve import options

    if not isinstance(values, dict) or not values or any(key not in SETTINGS for key in values):
        raise ValueError("Unknown setting")
    candidate: dict = {"evolution": {}}
    for key, value in values.items():
        path = NATIVE_KEYS[key]
        if len(path) == 1:
            candidate[path[0]] = value
        else:
            candidate.setdefault(path[0], {})[path[1]] = value
        if key in BOOLEANS and type(value) is not bool:
            raise ValueError(f"{key} must be true or false")
    if "language" in values and values["language"] not in CHOICES["language"]:
        raise ValueError("language must be auto, en or pt")
    options(candidate, {}, strict=True)


def hermes_executable() -> str | None:
    """The `hermes` command that runs this profile's Hermes (a .bat or .cmd shim is never run with a list argv)."""
    import shutil

    if os.environ.get("ATLAS_EVOLUTION_NO_HERMES") == "1":   # the test suite: never the machine's real Hermes
        return None
    import sys

    found = shutil.which("hermes")
    candidates = [found]
    # A service (the gateway, the dashboard) may run with a PATH that lacks it: the launcher this process came from,
    # then the launchers inside Hermes' own install, then the usual places.
    if Path(sys.argv[0] if sys.argv else "").name == "hermes":
        candidates.append(sys.argv[0])
    module = sys.modules.get("hermes_cli")
    if getattr(module, "__file__", None):
        root = Path(module.__file__).resolve().parent.parent
        candidates += [str(root / ".hermes" / "bin" / "hermes"), str(root / "venv" / "bin" / "hermes")]
    candidates += ["/usr/local/bin/hermes", str(Path.home() / ".local" / "bin" / "hermes")]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and Path(candidate).suffix.lower() not in (".bat", ".cmd"):
            return os.path.abspath(candidate)   # absolute: a script or a job run from another folder finds it too
    return None


def _decision_id(body: dict) -> int:
    """The id a decision names; ValueError (never KeyError or TypeError) for one that is missing or not a number."""
    value = body.get("id")
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("Invalid decision")
    try:
        return int(value)
    except ValueError:
        raise ValueError("Invalid decision") from None


def profile_name(home: Path) -> str:
    home = Path(home).resolve()
    return home.name if home.parent.name == "profiles" else "default"


def launch_home(home: Path) -> Path:
    """The root Hermes home of a profile (<root>/profiles/<name> belongs to <root>)."""
    home = Path(home).resolve()
    return home.parent.parent if home.parent.name == "profiles" else home


def run_hermes(home: Path, args: list, *, runner=None, executable=None, timeout: int = 60, what: str = "setting") -> None:
    """Run `hermes --profile <name> <args...>` for this profile, Hermes' public command line, with an argument list
    (no shell). Raises ValueError with Hermes' own message when it refuses, fails or does not answer in time."""
    profile = Path(home).resolve()
    command_path = executable() if callable(executable) else (executable or hermes_executable())
    if command_path is None:
        raise ValueError("The hermes command was not found: run this where `hermes --version` works")
    if runner is None and not (profile / "config.yaml").is_file():
        # Hermes bootstraps a home it has never seen (and its shared install with it): only real profiles.
        raise ValueError("Not a Hermes profile (no config.yaml)")
    env = dict(os.environ, HERMES_HOME=str(profile))
    env.pop("HERMES_PROFILE", None)   # the launch profile's own marker; --profile names the one changed
    run = subprocess.run if runner is None else runner
    try:
        result = run([command_path, "--profile", profile_name(profile), *[str(item) for item in args]], env=env,
                     cwd=str(profile), stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8",
                     errors="replace", timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        if what == "setting":
            raise ValueError("Hermes did not answer in time; that setting may not be saved: look at the settings "
                             "again") from None
        raise ValueError("Hermes did not answer in time; check the result before trying again") from None
    except (OSError, subprocess.SubprocessError):
        raise ValueError("Hermes refused the setting" if what == "setting" else "Hermes refused the command") from None
    if result.returncode != 0:
        detail = " ".join((result.stderr or result.stdout or "").split())[:200]
        raise ValueError(detail or ("Hermes refused the setting" if what == "setting" else "Hermes refused the command"))


def hermes_output(home: Path, args: list, *, runner=None, executable=None, timeout: int = 300) -> tuple[int, str]:
    """Run `hermes --profile <name> <args...>` and return (exit code, what it printed), never raising for its exit
    code: for Hermes' own checks, whose exit code says "issues found" (124 = it did not answer in time)."""
    profile = Path(home).resolve()
    command_path = executable() if callable(executable) else (executable or hermes_executable())
    if command_path is None:
        return 127, "The hermes command was not found: run this where `hermes --version` works"
    if runner is None and not (profile / "config.yaml").is_file():
        return 127, "Not a Hermes profile (no config.yaml)"
    env = dict(os.environ, HERMES_HOME=str(profile), NO_COLOR="1")
    env.pop("HERMES_PROFILE", None)
    run = subprocess.run if runner is None else runner
    try:
        result = run([command_path, "--profile", profile_name(profile), *[str(item) for item in args]], env=env,
                     cwd=str(profile), stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8",
                     errors="replace", timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return 124, "Hermes did not answer in time; check the result before trying again"
    except (OSError, subprocess.SubprocessError) as error:
        return 126, type(error).__name__
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def cli_writer(home: Path, *, runner=None, executable=None):
    """A writer that sets each value with `hermes --profile <name> config set ...`, Hermes' public command, which
    checks the key, takes Hermes' config lock and writes atomically. Raises ValueError with Hermes' own message."""

    def write(values: dict) -> list:
        written = []
        for key, value in values.items():
            literal = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, tuple, bool)) else str(value)
            run_hermes(home, ["config", "set", f"plugins.entries.{PLUGIN_ID}.settings.{key}", literal], runner=runner,
                       executable=executable)
            written.append(key)
        return written

    return write


def write_settings(home: Path, runtime_config: dict, values: dict, *, now: float, writer=None,
                   actor: str = "dashboard") -> dict:
    """Write settings with Hermes' public command and log each change (so it can be undone). Unknown keys and values
    the plugin or Hermes refuses raise ValueError; nothing is written then."""
    from . import decisions
    from .experience import experience

    validate_settings(values)
    before = current_settings(home, runtime_config)
    changed = {key: value for key, value in values.items() if before.get(key) != value}
    if not changed:
        return {"written": [], "reload": []}
    write, written = writer or cli_writer(home), []
    for key, value in changed.items():   # one at a time, each logged once Hermes took it: a later failure keeps the
        for done in write({key: value}):  # ones written before in the history, where they can be undone
            with experience(home) as conn:
                decisions.record_setting(conn, done, before.get(done), changed[done], now=now, actor=actor)
            written.append(done)
    # agent tools are registered at load: turning them on waits for the next load, turning them off is immediate
    return {"written": written, "reload": [key for key in written if key in RELOAD_KEYS
                                           or key == "agent_tools" and changed.get(key) is True]}


def recommended_settings(current: dict, suggestions: list[dict]) -> dict:
    """The changes the recommended setup makes to `current`: lessons on auto, the reflector on, the automatic actions
    complete, and every channel that is the owner's by construction (status.owner_suggestions) counted as the owner.
    Empty when nothing changes."""
    wanted = {"lessons": "auto", "reflector": True, "mode": "auto"}
    platforms = list(current.get("owner_platforms") or [])
    senders = list(current.get("owner_sender_ids") or [])
    for item in suggestions or ():
        target = platforms if item.get("kind") == "platform" else senders
        value = item.get("value")
        if isinstance(value, str) and value and value not in target:
            target.append(value)
    wanted["owner_platforms"], wanted["owner_sender_ids"] = platforms, senders
    return {key: value for key, value in wanted.items() if current.get(key) != value}


def decide(home: Path, runtime_config: dict, body: dict, *, now: float, lang: str = "en", writer=None,
           actor: str = "dashboard", runner=None) -> dict:
    """One decision. `body`: {"kind": lesson|all|proposal|apply|apply_all|schedule_digest|undo|settings|memory, ...}. `runner` replaces
    subprocess.run for the Hermes commands of "apply" and of its undo (tests)."""
    from . import decisions, memory_care
    from .experience import experience

    kind = body.get("kind") if isinstance(body, dict) else None
    if kind == "settings":
        return write_settings(home, runtime_config, body.get("values"), now=now, writer=writer, actor=actor)
    if kind == "apply":
        from . import actions

        return actions.apply_proposal(home, _decision_id(body), now=now, actor=actor, runner=runner)
    if kind == "schedule_digest":
        from . import actions

        return actions.schedule_digest(home, deliver=str(body.get("deliver") or "local"), lang=lang, now=now,
                                       actor=actor, runner=runner)
    if kind == "apply_all":
        from . import actions

        ids = body.get("ids")
        if not isinstance(ids, list) or not ids or len(ids) > 50:
            raise ValueError("Invalid decision")
        return actions.apply_many(home, ids, now=now, actor=actor, runner=runner)
    if kind == "memory":
        if body.get("action") != "remove":
            raise ValueError("Invalid decision")
        return memory_care.remove(home, str(body.get("file")), str(body.get("fingerprint")), now=now, actor=actor,
                                  runtime_config=runtime_config)
    with experience(home) as conn:
        if kind == "lesson":
            return decisions.decide_lesson(conn, _decision_id(body), str(body.get("action")), now=now, actor=actor,
                                           lang=lang, shown=body.get("shown") if isinstance(body.get("shown"), str) else None)
        if kind == "all":
            items = body.get("items")
            if not isinstance(items, dict) or len(items) > 500:
                raise ValueError("Invalid decision")
            return decisions.approve_all(conn, items, now=now, actor=actor, lang=lang)
        if kind == "proposal":
            return decisions.decide_proposal(conn, _decision_id(body), str(body.get("action")), now=now, actor=actor,
                                             lang=lang)
        if kind == "undo":
            decision_id = _decision_id(body)
            row = conn.execute("SELECT kind, target FROM decision_log WHERE id=?", (decision_id,)).fetchone()
            current = current_settings(home, runtime_config).get(row["target"]) \
                if row is not None and row["kind"] == "setting" else ...
            result = decisions.undo(conn, decision_id, now=now, actor=actor, current=current)
        else:
            raise ValueError("Invalid decision")
    if result.get("kind") == "setting" and result.get("restorable"):
        # written first; marked undone only once Hermes took the value back
        result.pop("restorable")
        result["settings"] = write_settings(home, runtime_config, {result["key"]: result["restore"]}, now=now,
                                            writer=writer, actor=actor)
        with experience(home) as conn:
            decisions.mark_undone(conn, result["id"], now=now)
        result["undone"] = True
    elif result.get("kind") == "cron_create" and result.get("restorable"):
        result.pop("restorable")
        code, out = hermes_output(home, ["cron", "remove", result["job"]], runner=runner, timeout=120)
        # Gone already (removed by hand) only when Hermes says so of this very job: "Job with ID or name '<job>' not
        # found". Any other failure (Hermes missing, a profile gone, a timeout) leaves the job, and the undo, as is.
        gone = code == 1 and re.search(rf"\bjob\b[^\n]*'{re.escape(result['job'])}'[^\n]*\bnot found\b", out, re.I)
        if code != 0 and not gone:
            raise ValueError(" ".join(out.split())[-200:] or "Hermes refused the command")
        with experience(home) as conn:   # removed now, or already gone (removed by hand)
            decisions.mark_undone(conn, result["id"], now=now)
        result["undone"] = True
    elif result.get("kind") == "proposal_apply" and result.get("restorable"):
        from . import actions

        result.pop("restorable")
        result = actions.undo_applied(home, result, now=now, runner=runner)
    elif result.get("kind") == "memory" and result.get("restorable"):
        result.pop("restorable")
        restored = memory_care.restore(home, result["file"], result["fingerprint"], result["backup"],
                                       position=result.get("position"), copies=result.get("copies"),
                                       runtime_config=runtime_config)
        if restored.get("restored"):
            from .actions import memory_skip

            with experience(home) as conn:
                decisions.mark_undone(conn, result["id"], now=now)
                memory_skip(conn, result["file"], result["fingerprint"], now)   # the owner wants it: never again by itself
            result["undone"] = True
        else:
            result["reason"] = restored.get("reason", "changed_since")
    return result


_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def plugin_enabled(config) -> bool:
    """Whether Hermes enables this plugin in a profile, by its configuration (`plugins.enabled`, `plugins.disabled`)."""
    plugins = config.get("plugins") if isinstance(config, dict) else None
    if not isinstance(plugins, dict):
        return False
    enabled, disabled = plugins.get("enabled"), plugins.get("disabled")
    return (isinstance(enabled, list) and PLUGIN_ID in enabled
            and not (isinstance(disabled, list) and PLUGIN_ID in disabled))


def profile_home(own_home: Path, name: str, *, read_config=None) -> tuple[Path, dict]:
    """The home and configuration of the profile `name` of the same Hermes as `own_home` ("default" is the root),
    when this plugin is installed and enabled there. ValueError for anything else: a name that is not a profile
    name, a folder that is not a profile of this Hermes (a link, a path outside it) or a profile without the plugin."""
    if not isinstance(name, str) or not _PROFILE_NAME.fullmatch(name):
        raise ValueError("Unknown profile")
    root = launch_home(own_home)
    if name == "default":
        home = root
    else:
        folder = root / "profiles" / name
        resolved = folder.resolve()   # resolved once: the checks and the result are the same path
        if folder.is_symlink() or resolved.parent != (root / "profiles").resolve() or not resolved.is_dir() \
                or resolved.is_symlink():
            raise ValueError("Unknown profile")
        home = resolved
    if not (home / "plugins" / PLUGIN_ID / "plugin.yaml").is_file():
        raise ValueError("Atlas Evolution is not installed in that profile")
    if read_config is None:
        from .integration import _read_hermes_config as read_config
    config = read_config(home)
    if not isinstance(config, dict) or not plugin_enabled(config):
        raise ValueError("Atlas Evolution is not enabled in that profile")
    return home, config
