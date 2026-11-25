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
            "runtime_guard", "mode", "fleet_managed", "overlay_pins")
BOOLEANS = ("tool_hints", "feedback_signals", "agent_tools", "reflector", "runtime_guard", "fleet_managed")
CHOICES = {"lessons": ("auto", "suggest", "off"), "language": ("auto", "en", "pt"), "mode": ("auto", "assist", "manual")}
NUMBERS = ("slow_turn_seconds", "reflector_daily_limit", "autopilot_minutes")
RELOAD_KEYS = ("runtime_guard", "tool_hints")   # applied at the next load of the plugin (integration.RELOAD_KEYS)
_WINDOWS = os.name == "nt"


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
    """The `hermes` command that runs this profile's Hermes. A real program comes first everywhere: on Windows
    `hermes.exe` (beside the `hermes.cmd` shim PATH names, in Hermes' own `.hermes/bin`, or in the older
    `venv/Scripts`); the published `hermes.cmd` shim is used only when there is no program, and run_command then
    quotes every argument for cmd.exe (refusing one it would read as something else). A .bat is never run."""
    import shutil

    import sys

    windows = _WINDOWS
    found = shutil.which("hermes")
    candidates = [found]
    if found and Path(found).suffix.lower() in (".cmd", ".bat"):
        candidates.insert(0, str(Path(found).with_suffix(".exe")))   # the program the shim starts
    # A service (the gateway, the dashboard) may run with a PATH that lacks it: the launcher this process came from,
    # then the launchers inside Hermes' own install, then the usual places.
    if Path(sys.argv[0] if sys.argv else "").stem.lower() == "hermes":
        candidates.append(sys.argv[0])
    module = sys.modules.get("hermes_cli")
    if getattr(module, "__file__", None):
        root = Path(module.__file__).resolve().parent.parent
        if windows:
            candidates += [str(root / ".hermes" / "bin" / "hermes.exe"), str(root / "venv" / "Scripts" / "hermes.exe"),
                           str(root / ".hermes" / "bin" / "hermes.cmd")]
        else:
            candidates += [str(root / ".hermes" / "bin" / "hermes"), str(root / "venv" / "bin" / "hermes")]
    if not windows:
        candidates += ["/usr/local/bin/hermes", str(Path.home() / ".local" / "bin" / "hermes")]
    existing = [item for item in candidates if item and Path(item).is_file()]
    program = next((item for item in existing if Path(item).suffix.lower() not in (".bat", ".cmd")), None)
    if program is None and windows:
        program = next((item for item in existing if Path(item).suffix.lower() == ".cmd"), None)
    return os.path.abspath(program) if program else None   # absolute: a job run from another folder finds it too


# A batch file runs under cmd.exe, which reads its command line again: only arguments without its special characters
# are passed to one (each then quoted), anything else is refused rather than escaped.
_BATCH_SAFE = re.compile(r'[^"%!^&|<>()\r\n\x00]*')


def _batch_command_line(argv: list) -> str:
    """The command line for a .cmd launcher, every argument quoted; ValueError for an argument cmd.exe would read as
    something else."""
    parts = []
    for item in (str(value) for value in argv):
        if not _BATCH_SAFE.fullmatch(item) or item.endswith("\\"):
            raise ValueError("This value cannot be passed to Hermes' hermes.cmd launcher; run the command by hand")
        parts.append(f'"{item}"')
    return " ".join(parts)


def _kill_tree(process) -> None:
    """End a command and every process it started: Hermes' launcher hands the work to a child Python, so ending the
    launcher alone left the child running after a timeout. Its own process group on POSIX (a polite TERM, then KILL),
    `taskkill /T /F` on Windows."""
    import signal
    import time

    if _WINDOWS:
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            process.kill()
        except OSError:
            pass
        return
    for sig, grace in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 0.0)):
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        give_up = time.monotonic() + grace
        while time.monotonic() < give_up:
            if process.poll() is not None:
                try:
                    os.killpg(process.pid, 0)   # the launcher ended: anything left of its group?
                except (ProcessLookupError, PermissionError, OSError):
                    return
            time.sleep(0.1)


# Approval bypasses no command this plugin starts inherits (catalog rule 12): Hermes' --yolo (HERMES_YOLO_MODE, and
# the HERMES_YOLO a TUI forwards), ACP's auto-approve, shell hooks accepted without asking, the TUI's skipped
# confirmation and the computer-use driver's bypass.
UNSAFE_ENV = ("HERMES_YOLO_MODE", "HERMES_YOLO", "HERMES_ACP_AUTO_APPROVE", "HERMES_ACCEPT_HOOKS",
              "HERMES_TUI_NO_CONFIRM", "CUA_DRIVER_DANGEROUSLY_BYPASS_APPROVALS")
# What is kept of each stream a command prints (characters): its start and its end, so a command that prints without
# end never fills the memory (its last lines say why it failed).
OUTPUT_HEAD, OUTPUT_TAIL = 64 * 1024, 192 * 1024
EOF_GRACE_SECONDS = 2.0   # once a command ended, how long its pipes get to close (a helper it left may hold them)


def child_environment(base=None, **values) -> dict:
    """The environment of a command this plugin starts: `base` (this process' own by default) with `values`, without
    any approval bypass of UNSAFE_ENV (in any case: Windows' names are not case-sensitive)."""
    env = dict(os.environ if base is None else base, **values)
    for key in [key for key in env if str(key).upper() in UNSAFE_ENV]:
        del env[key]
    return env


class _Kept:
    """The first `head` and the last `tail` characters of a stream; where anything was left out, an ellipsis line
    stands for it and neither side keeps half a line (a parser would read a cut line as a whole one)."""

    def __init__(self, head: int, tail: int):
        import collections
        import threading

        self.head, self.tail = max(0, head), max(0, tail)
        self.start: list[str] = []
        self.start_size = 0
        self.end: collections.deque = collections.deque()
        self.end_size = 0
        self.dropped = 0
        self.lock = threading.Lock()

    def add(self, chunk: str) -> None:
        with self.lock:
            if self.start_size < self.head:
                first = chunk[:self.head - self.start_size]
                self.start.append(first)
                self.start_size += len(first)
                chunk = chunk[len(first):]
            if not chunk:
                return
            self.end.append(chunk)
            self.end_size += len(chunk)
            while self.end and self.end_size - len(self.end[0]) >= self.tail:
                self.end_size -= len(self.end[0])
                self.dropped += len(self.end.popleft())

    def text(self) -> str:
        with self.lock:
            start, end, dropped = "".join(self.start), "".join(self.end), self.dropped
        if len(end) > self.tail:
            dropped += len(end) - self.tail
            end = end[len(end) - self.tail:] if self.tail else ""
        if not dropped:
            return start + end
        cut = start.rfind("\n")   # a line longer than what is kept stays cut: better than nothing
        start = start[:cut + 1] if cut >= 0 else start + "\n" if start else ""
        cut = end.find("\n")
        end = end[cut + 1:] if 0 <= cut < len(end) - 1 else end
        return start + "…\n" + end


def _reader(stream, kept: _Kept):
    """A daemon thread reading `stream` (a binary pipe) to its end into `kept`, decoded as text mode would (UTF-8,
    errors replaced, universal newlines). Once `abandoned` it only drains: a helper left holding the pipe never
    blocks on a full pipe nor dies of a broken one."""
    import codecs
    import io
    import threading

    def run():
        decoder = io.IncrementalNewlineDecoder(codecs.getincrementaldecoder("utf-8")("replace"), translate=True)
        read = getattr(stream, "read1", stream.read)   # what is there now, never a wait for a full buffer
        try:
            while True:
                data = read(65_536)
                if not data:
                    break
                if not thread.abandoned:
                    kept.add(decoder.decode(data))
            if not thread.abandoned:
                kept.add(decoder.decode(b"", final=True))
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    thread = threading.Thread(target=run, name="atlas-evolution-output", daemon=True)
    thread.abandoned = False
    thread.start()
    return thread


def collect(process, *, timeout, kill, head: int = OUTPUT_HEAD, tail: int = OUTPUT_TAIL,
            grace: float = EOF_GRACE_SECONDS) -> tuple[str, str]:
    """(stdout, stderr) of a started process with binary pipes ("" for a stream not piped), each read in a thread
    that keeps its first `head` and last `tail` characters only (_Kept). `timeout` is the process' own: once it
    ended, its pipes get `grace` seconds to close, and a helper it left running past that (a daemon it started)
    keeps them, is left alone and has its output read away. Past `timeout`, `kill(process)` ends it and what it
    started, and subprocess.TimeoutExpired is raised with what was read; any other exception (Ctrl-C) kills too."""
    import time

    streams = (process.stdout, process.stderr)
    kept = [_Kept(head, tail) for _stream in streams]
    readers = [_reader(stream, store) if stream is not None else None for stream, store in zip(streams, kept)]

    def finish(wait: float) -> tuple[str, str]:
        give_up = time.monotonic() + wait
        for reader in readers:
            if reader is not None:
                reader.join(max(0.0, give_up - time.monotonic()))
        for reader in readers:
            if reader is not None and reader.is_alive():
                reader.abandoned = True
        return kept[0].text(), kept[1].text()

    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill(process)
        out, err = finish(5.0)
        raise subprocess.TimeoutExpired(process.args, timeout, output=out, stderr=err) from None
    except BaseException:
        kill(process)
        finish(1.0)
        raise
    return finish(grace)


def run_command(argv: list, *, env=None, cwd=None, timeout=None, **_ignored) -> subprocess.CompletedProcess:
    """subprocess.run(argv, capture_output=True, text=True) for Hermes' command line, in its own process group, so a
    timeout (or the owner's Ctrl-C) ends everything it started (_kill_tree) before subprocess.TimeoutExpired is
    raised. The timeout is the command's own: one that ended is never a timeout, whatever helper it left holding its
    output (collect). Its output is kept to its start and end (OUTPUT_HEAD, OUTPUT_TAIL) and its environment never
    carries an approval bypass (child_environment). A .cmd launcher runs with every argument quoted
    (_batch_command_line)."""
    options: dict = {"start_new_session": True} if not _WINDOWS else {
        "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    command = argv
    if _WINDOWS and str(argv[0]).lower().endswith((".cmd", ".bat")):
        command = _batch_command_line(argv)
    process = subprocess.Popen(command, env=child_environment(env), cwd=cwd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
    out, err = collect(process, timeout=timeout, kill=_kill_tree)
    return subprocess.CompletedProcess(argv, process.returncode, out, err)


def _decision_id(body: dict) -> int:
    """The id a decision names; ValueError (never KeyError or TypeError) for one that is missing or not a number."""
    value = body.get("id")
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("Invalid decision")
    try:
        number = int(value)
    except ValueError:
        raise ValueError("Invalid decision") from None
    if not 0 < number < 2**63:
        raise ValueError("Invalid decision")
    return number


def profile_name(home: Path) -> str:
    """The profile a home is, as Hermes names it (environment.home_parts): read before links are followed, so a
    linked profile folder keeps its own name and a default home that links into another Hermes' profiles folder is
    still the default one."""
    from .environment import home_parts

    return home_parts(home)[1]


_PROFILE_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")


def _profile_args(profile: Path) -> list[str]:
    """`--profile <name>` for Hermes' command line. A root HERMES_HOME alone can still follow Hermes' sticky
    active_profile, so the name is always explicit. It is read before links are followed (profile_name): a linked
    profile folder is still that profile, never the default one."""
    name = profile_name(Path(profile))
    if not _PROFILE_SLUG.fullmatch(name):
        raise ValueError("Invalid Hermes profile directory name")
    return ["--profile", name]


def launch_spelling(home: Path) -> tuple[Path, str]:
    """(HERMES_HOME, profile name) for Hermes' command line in this home. Hermes finds a profile from HERMES_HOME's
    spelling (`<root>/profiles/<name>` names <root>; anything else is a root), so the home is spelled the way
    environment.home_parts read it, never as the place its links lead to: a resolved profile folder outside its
    Hermes would make `--profile <name>` look for it in the wrong place, and `--profile default` run another Hermes."""
    from .environment import home_parts

    root, name = home_parts(home)
    return (root if name == "default" else root / "profiles" / name), name


def launch_home(home: Path) -> Path:
    """The root Hermes home of a profile (<root>/profiles/<name> belongs to <root>): environment.root_of."""
    from .environment import root_of

    return root_of(home)


def run_hermes(home: Path, args: list, *, runner=None, executable=None, timeout: int = 60, what: str = "setting") -> None:
    """Run `hermes --profile <name> <args...>` for this profile, Hermes' public command line, with an argument list
    (no shell). Raises ValueError with Hermes' own message when it refuses, fails or does not answer in time."""
    profile, name = launch_spelling(home)
    command_path = executable() if callable(executable) else (executable or hermes_executable())
    if command_path is None:
        raise ValueError("The hermes command was not found: run this where `hermes --version` works")
    if runner is None and not (profile / "config.yaml").is_file():
        # Hermes bootstraps a home it has never seen (and its shared install with it): only real profiles.
        raise ValueError("Not a Hermes profile (no config.yaml)")
    env = child_environment(HERMES_HOME=str(profile))   # never an approval bypass (UNSAFE_ENV)
    env.pop("HERMES_PROFILE", None)   # the launch profile's own marker; --profile names the one changed
    run = run_command if runner is None else runner
    try:
        result = run([command_path, "--profile", name, *[str(item) for item in args]], env=env,
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
    profile, name = launch_spelling(home)
    command_path = executable() if callable(executable) else (executable or hermes_executable())
    if command_path is None:
        return 127, "The hermes command was not found: run this where `hermes --version` works"
    if runner is None and not (profile / "config.yaml").is_file():
        return 127, "Not a Hermes profile (no config.yaml)"
    env = child_environment(HERMES_HOME=str(profile), NO_COLOR="1")
    env.pop("HERMES_PROFILE", None)
    run = run_command if runner is None else runner
    try:
        result = run([command_path, "--profile", name, *[str(item) for item in args]], env=env,
                     cwd=str(profile), stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8",
                     errors="replace", timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return 124, "Hermes did not answer in time; check the result before trying again"
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        return 126, str(error)[:200] if isinstance(error, ValueError) else type(error).__name__
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
    subprocess.run for the Hermes commands of "apply" and of its undo."""
    from . import decisions, memory_care
    from .experience import experience

    kind = body.get("kind") if isinstance(body, dict) else None
    if kind == "settings":
        return write_settings(home, runtime_config, body.get("values"), now=now, writer=writer, actor=actor)
    if kind == "fleet_auto":   # 0.14.0: Auto for every profile, and back (fleet.enable / fleet.disable)
        from . import fleet

        if body.get("on") is True:
            owner = body.get("owner") if body.get("owner") in ("keep", "suggested") else "keep"
            return fleet.enable(home, now=now, owner=owner, actor=actor,
                                writer_for=(lambda _home: writer) if writer is not None else None)
        if body.get("on") is False:
            return fleet.disable(home, now=now, actor=actor,
                                 writer_for=(lambda _home: writer) if writer is not None else None)
        raise ValueError("Say on or off")
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
        # the copy the owner keeps is named too: nothing is taken out unless it is still there (memory_care.remove)
        return memory_care.remove(home, str(body.get("file")), str(body.get("fingerprint")), keep=body.get("keep"),
                                  now=now, actor=actor, runtime_config=runtime_config)
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
        from .environment import deleted_profile

        folder = root / "profiles" / name
        resolved = folder.resolve()   # resolved once: the checks and the result are the same path
        if folder.is_symlink() or resolved.parent != (root / "profiles").resolve() or not resolved.is_dir() \
                or resolved.is_symlink() or deleted_profile(folder):
            raise ValueError("Unknown profile")   # `hermes profile delete` marked it gone: never served again
        home = resolved
    if not (home / "plugins" / PLUGIN_ID / "plugin.yaml").is_file():
        raise ValueError("Atlas Evolution is not installed in that profile")
    if read_config is None:
        from .integration import _read_hermes_config as read_config
    config = read_config(home)
    if not isinstance(config, dict) or not plugin_enabled(config):
        raise ValueError("Atlas Evolution is not enabled in that profile")
    return home, config
