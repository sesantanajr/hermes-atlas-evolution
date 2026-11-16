"""Runtime guard: the agent must not take down the Hermes it runs in.

On a real server the agent restarted its own gateway with systemctl a dozen times in one day, cutting
turns in progress, and once deleted the Python runtime the gateway unit used, which kept Hermes down for
an hour. A restart is the owner's call: `hermes gateway restart`, run in the owner's own terminal, lists
the turns still running and asks before interrupting them (Hermes 0.23.1). Nothing good comes from the
second. The guard vetoes exactly those terminal commands through `pre_tool_call` and tells the agent to
leave the restart to the owner. It knows how Hermes runs its gateway on each system: a systemd unit on
Linux, a LaunchAgent on macOS, a Windows service or scheduled task (the terminal there is Git Bash).

`check` is a pure function over the command: no I/O, a bounded input, and any doubt (an unparsable
command, a relative path, an unknown tool) lets the command through. Hermes blocks a tool when a
`pre_tool_call` callback raises, so the hook wrapper never raises.
"""

from __future__ import annotations

import fnmatch
import os
import posixpath
import re
import shlex
import sys
from pathlib import Path, PurePosixPath

TERMINAL_TOOLS = frozenset({"terminal", "shell", "bash", "run_command", "execute_command"})
# Hermes' process_manage `write` and `submit` send text to a background process, often a shell (Hermes 0.23.1).
PROCESS_TOOLS = frozenset({"process_manage", "process"})
_PROCESS_INPUT = frozenset({"write", "submit"})
_COMMAND_LIMIT = 20_000   # longer commands (big inline files) are let through unparsed
_WINDOWS = os.name == "nt"   # module-level so the tests can exercise the Windows rules on any system
# hermes-gateway, hermes-gateway-<profile>, hermes-gateway-<8 hex> (a HERMES_HOME of its own), templated @ units
_UNIT = re.compile(r"hermes-gateway(?:[-@][\w.-]+)?(?:\.service)?\Z", re.I)
# macOS LaunchAgent: ai.hermes.gateway or ai.hermes.gateway-<suffix>, bare, as gui/<uid>/<label> or as its plist
_LAUNCHD_LABEL = re.compile(r"ai\.hermes\.gateway(?:-[\w.-]+)?(?:\.plist)?\Z", re.I)
_LAUNCHCTL_STOPS = frozenset({"stop", "bootout", "unload", "remove", "kill"})
# Windows service or scheduled task: Hermes_Gateway or Hermes_Gateway_<suffix>
_WINDOWS_NAME = re.compile(r"hermes_gateway(?:_[\w.-]+)?\Z", re.I)
_SERVICE_CMDLETS = frozenset({"stop-service", "restart-service", "suspend-service", "stop-scheduledtask"})
_STOP_ACTIONS = frozenset({"restart", "stop", "kill", "try-restart", "reload-or-restart", "try-reload-or-restart",
                           "condrestart", "condstop", "force-reload", "freeze"})
_STOP_WITH_NOW = frozenset({"disable", "mask"})   # with --now they stop the unit as well
# systemctl options that take their value as the next word: that word is not the action or a unit.
_SYSTEMCTL_VALUES = frozenset({"-s", "--signal", "-t", "--type", "-p", "--property", "-M", "--machine", "-H", "--host",
                               "--job-mode", "--kill-whom", "--kill-value", "--state", "-o", "--output", "-n", "--lines",
                               "--root", "--image", "--what", "--timestamp", "--preset-mode", "--message",
                               "--boot-loader-entry", "--reboot-argument", "--drop-in", "--when"})
_REMOVERS = frozenset({"rm", "rmdir", "unlink", "shred", "mv", "del", "erase", "rd", "remove-item", "ri", "move",
                       "move-item", "mi"})
_MOVERS = frozenset({"mv", "move", "move-item", "mi"})   # the last word is where things go, not what goes
_CMD_SWITCH = re.compile(r"/[A-Za-z?]{1,2}\Z")          # del /s /q: switches of cmd's own commands
# Variables expanded at the start of a path: the homes, and on Windows the profile folders Git Bash exports.
_ENV_PREFIX = re.compile(r"(?:\$\{(\w+)(?::?[-?=+][^}]*)?\}|\$env:(\w+)|\$(\w+)|%(\w+)%)(?=[\\/]|\Z)", re.I)
_EXPANDED = frozenset({"USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA"})
_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
# Wrappers that run the command after their own options: name -> options that take a separate value.
_WRAPPERS = {"sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "-T", "--user", "--group", "--host", "--prompt",
                      "--chdir", "--close-from", "--role", "--type", "--other-user", "--command-timeout"},
             "env": {"-u", "-C", "--unset", "--chdir"}, "nohup": set(),
             "timeout": {"-s", "-k", "--signal", "--kill-after"}, "nice": {"-n", "--adjustment"},
             "ionice": {"-c", "-n", "-p"}, "stdbuf": {"-i", "-o", "-e"}, "exec": set(), "command": set(),
             "builtin": set(), "time": set(), "chrt": set(), "doas": {"-u"}, "setsid": set(),
             "systemd-run": {"-u", "--unit", "-p", "--property", "-E", "--setenv", "-M", "--machine", "-H", "--host",
                             "--description", "--slice", "--uid", "--gid", "-d", "--working-directory", "--nice",
                             "--service-type", "--on-active", "--on-boot", "--on-startup", "--on-unit-active",
                             "--on-unit-inactive", "--on-calendar", "--on-clock-change", "--timer-property",
                             "--path-property", "--socket-property", "--expand-environment"},
             "runuser": {"-u", "-g", "-G", "--user", "--group", "--supp-group"}, "watch": {"-n", "--interval"}, "flock": {"-w", "--timeout", "-E"},
             "xargs": {"-n", "-I", "-P", "-d", "-L", "-s", "-a", "-E"}, "uvx": {"--from", "--with", "-p", "--python"},
             "taskset": set(), "unshare": set(), "nsenter": {"-t", "--target", "-S", "-G"}, "chroot": set(),
             "strace": {"-o", "-e", "-p", "-s", "-u"}, "ltrace": {"-o", "-e", "-p", "-s", "-u"}, "nocache": set(),
             "caffeinate": set(), "npx": {"-p", "--package"}}
_TAKES_POSITIONAL = {"timeout": 1, "chrt": 1, "flock": 1, "taskset": 1, "chroot": 1}   # duration, priority, lock, mask, root
# `uv run ...` and `pipx run ...`: the command follows the subcommand and its options.
_RUNNERS = {("uv", "run"): {"--with", "--python", "-p", "--project", "--directory", "--env-file", "--from", "--package",
                            "--group", "--extra", "--index", "--index-url"},
            ("pipx", "run"): {"--spec", "--python", "--index-url", "--pip-args"}}
# Interpreters whose inline program (-c / -e) may run a command given as string literals.
_INLINE_CODE = {"python": ("-c",), "perl": ("-e", "-E"), "ruby": ("-e",), "node": ("-e", "--eval"), "php": ("-r",)}
_LITERAL = re.compile(r"'([^'\\]{1,2000})'|\"([^\"\\]{1,2000})\"")
_DBUS = frozenset({"busctl", "dbus-send", "gdbus", "qdbus"})
_DBUS_STOP = re.compile(r"(?:Restart|Stop|Kill|TryRestart|ReloadOrRestart|TryReloadOrRestart)Unit\b")
_LOCALHOST = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})
# Hermes settings files: writing them can switch plugins off or widen whom this plugin trusts.
_CONFIG_FILES = ("config.yaml", "config.yml")
_PLUGIN_WORDS = re.compile(r"plugins|hermes-atlas-evolution|hermes-evolution|runtime_guard|owner_sender_ids|owner_platforms"
                           r"|agent_advice|agent_tools", re.I)

# Programs that run a command given as one string after -c (shells also accept it inside -lc, -ec, ...).
_STRING_RUNNERS = frozenset({"su", "runuser", "script", "flock"})
_RTK_SUBCOMMANDS = frozenset({"run", "err", "summary", "test", "bypass", "proxy"})
_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")", "\n", ";;", "|&"})
# Shell words that open a compound command or negate one: the command that follows them is the one that runs
# (`if ! systemctl ...; then systemctl restart ...; fi`, `for ...; do ...; done`, `{ ...; }`, `! cmd`).
_RESERVED = frozenset({"if", "then", "elif", "else", "while", "until", "do", "!", "{", "coproc"})
# $(...) and `...`: run when the command runs, also inside double quotes, where the shell lexer keeps them whole.
_SUBSTITUTION = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
# `python -m hermes_cli.main ...` is the gateway unit's own ExecStart form: the hermes command under another name.
_PYTHON = re.compile(r"(?:python(?:\d+(?:\.\d+)*)?w?|py)\Z")
_PYTHON_VALUES = frozenset({"-X", "-W", "-Q"})
_HERMES_MODULES = frozenset({"hermes_cli.main", "hermes_cli"})
# What a pid of the gateway is looked up with, for `kill $(pgrep -f hermes)` and its forms.
_PID_LOOKUPS = frozenset({"pgrep", "pidof", "grep", "egrep", "fgrep", "rg", "awk"})
_PID_FILES = ("gateway.pid", "gateway.lock")
_BRACE = re.compile(r"\{([^{}]*,[^{}]*)\}")

MESSAGES = {
    "en": {
        "gateway_stop": ("Atlas Evolution runtime guard: this command would stop or restart the Hermes gateway that this "
                         "conversation runs on, cutting off the turns in progress, including this one. Do not retry it in "
                         "another form. Tell the owner a restart is needed and why; the owner runs `hermes gateway restart` "
                         "from their own terminal, which lists the turns still running and asks before interrupting them."),
        "runtime_removal": ("Atlas Evolution runtime guard: this command would delete or move {path}, which the running "
                            "Hermes needs; Hermes would stop working until someone repairs it by hand. Leave it in place. "
                            "Removing it is for the owner to do from their own terminal, with Hermes stopped."),
        "plugin_settings": ("Atlas Evolution runtime guard: this command would change the settings of Atlas Evolution "
                            "(who counts as the owner, its protections) or switch the plugin off. Only the owner decides "
                            "that, from their own terminal. Do not retry it in another form; tell the owner what you "
                            "wanted to change and why."),
    },
    "pt": {
        "gateway_stop": ("Guarda de runtime do Atlas Evolution: este comando pararia ou reiniciaria o gateway do Hermes em "
                         "que esta conversa roda, cortando os turnos em andamento, inclusive este. Não tente de outra forma. "
                         "Diga ao dono que é preciso reiniciar e por quê; o dono roda `hermes gateway restart` no próprio "
                         "terminal, e esse comando mostra os turnos ainda em andamento e pergunta antes de interrompê-los."),
        "runtime_removal": ("Guarda de runtime do Atlas Evolution: este comando apagaria ou moveria {path}, de que o Hermes "
                            "em execução precisa; o Hermes pararia de funcionar até alguém consertar à mão. Deixe como está. "
                            "Remover isso fica para o dono, no próprio terminal, com o Hermes parado."),
        "plugin_settings": ("Guarda de runtime do Atlas Evolution: este comando mudaria as configurações do Atlas Evolution "
                            "(quem conta como o dono, as proteções dele) ou desligaria o plugin. Só o dono decide isso, no "
                            "próprio terminal. Não tente de outra forma; diga ao dono o que você queria mudar e por quê."),
    },
}


def _forms(value) -> set[Path]:
    """A path as written (absolute, links kept) and as resolved: a command may name either."""
    found = set()
    try:
        found.add(Path(os.path.abspath(os.fspath(value))))
    except (OSError, TypeError, ValueError):
        pass
    try:
        found.add(Path(value).resolve())
    except (OSError, RuntimeError, TypeError, ValueError):
        pass
    return found


def _base_interpreter() -> set[Path]:
    """The interpreter a virtual environment points at, under the name it uses: `home` in pyvenv.cfg (often a
    link, such as uv's cpython-3.11-... pointing at cpython-3.11.16-...) and sys._base_executable."""
    found = set()
    try:
        config = (Path(sys.prefix) / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")[:65_536]
    except OSError:
        config = ""
    match = re.search(r"^\s*home\s*=\s*(.+?)\s*$", config, re.M)
    if match:   # the folder of the executable: bin/ on POSIX, the installation itself on Windows
        home = Path(match.group(1))
        found |= _forms(home if _WINDOWS else home.parent)
    base = getattr(sys, "_base_executable", None)
    if isinstance(base, str) and base:
        found |= _forms(Path(base).parent if _WINDOWS else Path(base).parent.parent)
    return found


def protected_paths(*, hermes_root=None, profile_home=None) -> tuple[Path, ...]:
    """What the running Hermes needs: its interpreter and environment, its source, its root and this profile,
    each both as written and resolved."""
    found = set()
    for value in (sys.base_prefix, sys.prefix, sys.exec_prefix):
        found |= _forms(value)
    found |= _base_interpreter()
    module = sys.modules.get("hermes_cli")
    location = getattr(module, "__file__", None) if module is not None else None
    if isinstance(location, str):
        found |= _forms(Path(os.path.abspath(location)).parent.parent)
        found |= _forms(Path(location).resolve().parent.parent)
    for value in (hermes_root, profile_home):
        if value is not None:
            found |= _forms(value)
    # Never a system directory such as / or /usr: those are not Hermes' to protect.
    return tuple(sorted(path for path in found if path.is_absolute() and len(path.parts) > 2))


def protected_trees() -> tuple[Path, ...]:
    """Folders whose inside the running Hermes needs as well: its virtual environment, a Python runtime of its
    own (such as ~/.hermes/tools/python-...), the standard library, and the Python packages and environments of
    Hermes' source. A shared prefix such as /usr/local is never one: only the standard library inside it is."""
    import sysconfig

    found = set()
    if sys.prefix != sys.base_prefix:
        found |= _forms(sys.prefix)
    for value in (sys.base_prefix, sys.exec_prefix):
        if "python" in Path(value).name.lower():   # a folder that is nothing but this Python
            found |= _forms(value)
    for key in ("stdlib", "platstdlib"):
        try:
            found |= _forms(sysconfig.get_paths()[key])
        except (KeyError, OSError, ValueError):
            pass
    module = sys.modules.get("hermes_cli")
    location = getattr(module, "__file__", None) if module is not None else None
    if isinstance(location, str):
        source = Path(location).resolve().parent.parent
        try:
            children = sorted(source.iterdir())[:400]
        except OSError:
            children = []
        for child in children:
            try:
                if child.is_dir() and ((child / "__init__.py").is_file() or (child / "pyvenv.cfg").is_file()):
                    found |= _forms(child)
            except OSError:
                continue
    return tuple(sorted(path for path in found if path.is_absolute() and len(path.parts) > 2))


_HEREDOC = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_]\w*)\2")


def _strip_heredocs(command: str) -> str:
    """Drop here-document bodies: they are data written somewhere (a script, a note), not commands run now."""
    lines, kept, index = command.split("\n"), [], 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        index += 1
        for match in _HEREDOC.finditer(line):
            tabs, delimiter = match.group(1) == "-", match.group(3)
            while index < len(lines):
                body = lines[index].lstrip("\t") if tabs else lines[index]
                index += 1
                if body == delimiter:
                    break
    return "\n".join(kept)


def _segments(command: str) -> list[list[str]] | None:
    command = _strip_heredocs(command)
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()\n")
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r"
    try:
        tokens = list(lexer)
    except ValueError:   # unbalanced quotes: let it through
        return None
    segments, current = [], []
    for token in tokens:
        if token in _SEPARATORS or set(token) <= set(";&|()\n"):
            if current:
                segments.append(current)
            current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


# Stands for the words xargs adds from its input at the end of the command: unknown here, like a shell variable.
XARGS_WORDS = "$XARGS"


def _strip_prefix(tokens: list[str]) -> list[str]:
    """Drop variable assignments and wrappers such as sudo, env, nohup, timeout, setsid, systemd-run, flock,
    watch, xargs and rtk run before the real command. Under xargs, a last word XARGS_WORDS stands for the words
    xargs adds from its input."""
    index, xargs = 0, False
    while index < len(tokens):
        token = tokens[index]
        if _ASSIGNMENT.match(token) or token in _RESERVED:
            index += 1
            continue
        name = os.path.basename(token)
        runner = _RUNNERS.get((name, tokens[index + 1] if index + 1 < len(tokens) else ""))
        if runner is not None:   # uv run [options] <command>, pipx run [options] <command>
            index += 2
            while index < len(tokens) and tokens[index].startswith("-"):
                if tokens[index] == "--":
                    index += 1
                    break
                index += 2 if tokens[index] in runner else 1
            continue
        if name == "rtk" and index + 1 < len(tokens) and tokens[index + 1] in _RTK_SUBCOMMANDS:
            index += 2   # rtk run|err|...: what follows is the command
            if index < len(tokens) and tokens[index] == "--":
                index += 1
            continue
        if name not in _WRAPPERS or _command_string(name, tokens[index + 1:]) is not None:
            break
        xargs = xargs or name == "xargs"
        index += 1
        with_value = _WRAPPERS[name]
        while index < len(tokens) and tokens[index].startswith("-"):
            if tokens[index] == "--":
                index += 1
                break
            index += 2 if tokens[index] in with_value else 1
        index += _TAKES_POSITIONAL.get(name, 0)
    rest = tokens[index:]
    return [*rest, XARGS_WORDS] if xargs and rest else rest


def _program(token: str) -> str:
    """The program a token names: its file name on either kind of path, lower case, without .exe."""
    name = re.split(r"[\\/]", token)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _command_string(verb: str, rest: list[str]) -> str | None:
    """The command a shell (-c, -lc, -ec, ...), su, runuser, script or flock (-c, --command), PowerShell
    (-Command, -c) or cmd (/c) gets as one string."""
    if verb in ("powershell", "pwsh", "cmd"):
        switches = ("/c", "//c", "/k", "//k") if verb == "cmd" else ("-command", "-c", "/command", "/c")
        for position, token in enumerate(rest):
            if token.lower() in switches:
                return " ".join(rest[position + 1:]) or None   # everything after the switch is the command
        return None
    if verb == "env":   # env -S '<command>' splits one string into the command and its arguments
        for position, token in enumerate(rest):
            if token in ("-S", "--split-string"):
                return " ".join(rest[position + 1:]) or None
            if token.startswith("--split-string="):
                return " ".join([token.split("=", 1)[1], *rest[position + 1:]]) or None
            if token.startswith("-S") and len(token) > 2:
                return " ".join([token[2:], *rest[position + 1:]])
            if not token.startswith("-") and not _ASSIGNMENT.match(token):
                break
        return None
    if verb == "eval":   # eval '<command>': its words, joined, are the command
        return " ".join(rest) or None
    for position, token in enumerate(rest):
        if verb in _STRING_RUNNERS and token.startswith("--command="):
            return " ".join([token.split("=", 1)[1], *rest[position + 1:]]) or None
        if verb in _SHELLS and re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", token):
            return rest[position + 1] if position + 1 < len(rest) else None
        if verb in _STRING_RUNNERS and token in ("-c", "--command"):
            return rest[position + 1] if position + 1 < len(rest) else None
        if verb in _STRING_RUNNERS and re.fullmatch(r"-[A-Za-z]*c", token) and verb == "script":
            return rest[position + 1] if position + 1 < len(rest) else None   # script -qc '<command>'
    return None


_DRIVE = re.compile(r"([A-Za-z]):(?:/|\Z)")
_GIT_BASH_DRIVE = re.compile(r"/[A-Za-z](?:/|\Z)")


def _canonical(text: str) -> PurePosixPath | None:
    """An absolute path in one comparable form, or None for a relative one. POSIX paths stay as they are. On
    Windows `C:\\x`, `C:/x` and Git Bash's `/c/x` all become `/c/x`, compared without case."""
    if not _WINDOWS:
        if not text.startswith("/"):
            return None   # relative to an unknown directory
        return PurePosixPath(posixpath.normpath(text.rstrip("/") or "/"))
    text = text.replace("\\", "/")
    drive = _DRIVE.match(text)
    if drive:
        text = f"/{drive.group(1)}/{text[drive.end():]}"
    elif not _GIT_BASH_DRIVE.match(text):
        return None   # not anchored to a drive
    return PurePosixPath(posixpath.normpath(text.lower().rstrip("/") or "/"))


def _braces(word: str, limit: int = 64) -> list[str]:
    """The words bash makes of `{a,b}` (`mv x{,.bak}` is `mv x x.bak`), at most `limit` of them."""
    match = _BRACE.search(word)
    if not match:
        return [word]
    found = []
    for choice in match.group(1).split(","):
        for item in _braces(word[:match.start()] + choice + word[match.end():], limit):
            if len(found) >= limit:
                return found
            found.append(item)
    return found


def _expand(token: str, home: Path, profile: Path | None = None, cwd: PurePosixPath | None = None) -> PurePosixPath | None:
    """An absolute, comparable path for a word of the command, or None when it cannot be known here. A relative
    word is taken from `cwd` (the terminal tool's `workdir`, or a `cd` earlier in the command) when it is known."""
    if token == "~" or token.startswith(("~/", "~\\")):
        token = str(home) + token[1:]
    elif token.startswith("~") and re.match(r"~([\w.-]+)(?=[\\/]|\Z)", token):
        user = re.match(r"~([\w.-]+)", token).group(1)
        if user != Path(home).name:
            return None   # another user's home: unknown here
        token = str(home) + token[1 + len(user):]
    else:
        variable = _ENV_PREFIX.match(token)
        if variable:
            name = next(group for group in variable.groups() if group).upper()
            if name == "HOME":
                value = str(home)
            elif name == "HERMES_HOME":
                value = str(profile) if profile is not None else os.environ.get("HERMES_HOME")
            else:
                value = os.environ.get(name) if name in _EXPANDED else None
            if not value:
                return None   # an unknown variable: let it through
            token = value + token[variable.end():]
    found = _canonical(token)
    if found is None and cwd is not None and token and not _dynamic(token) and not token.startswith("~"):
        relative = token.replace("\\", "/") if _WINDOWS else token
        found = PurePosixPath(posixpath.normpath(f"{cwd}/{relative.lower() if _WINDOWS else relative}"))
    return found


def _hits(pattern: PurePosixPath, protected, *, tree: bool = False) -> bool:
    """True when removing `pattern` would remove `protected`: the same path, an ancestor, or a glob of either.
    With `tree`, anything inside `protected` counts too (`rm -rf venv/lib`, `rm -rf venv/*`)."""
    target = _canonical(str(protected))
    if target is None:
        return False
    chain = [target, *target.parents]
    globbed = any(char in str(pattern) for char in "*?[")
    if globbed and any(fnmatch.fnmatchcase(str(item), str(pattern)) for item in chain):
        return True
    if not globbed and pattern in chain:
        return True
    if tree:   # the part of the pattern before its first glob is the folder it reaches into
        parts = []
        for part in pattern.parts:
            if any(char in part for char in "*?["):
                break
            parts.append(part)
        base = PurePosixPath(*parts) if parts else None
        return base is not None and (base == target or target in base.parents)
    return False


def _kill_targets(rest: list[str]) -> list[str]:
    """The process ids of `kill [-SIGNAL | -s SIGNAL | -n NUMBER] [--] pid...`: `kill -9 -1` sends 9 to -1."""
    targets, signal, skip, ended = [], False, False, False
    for token in rest:
        if skip:
            skip = False
        elif ended or not token.startswith("-") or token == "-":
            targets.append(token)
        elif token == "--":
            ended = True
        elif token in ("-s", "-n"):
            signal = skip = True
        elif not signal and not targets:
            signal = True   # the first option is the signal: -9, -TERM, -1 (HUP)
        else:
            targets.append(token)
    return targets


def own_process() -> tuple[str, str]:
    """This process as pkill and killall see it: its name (/proc/<pid>/comm, 15 characters) and its command line."""
    name = Path(sys.executable or "python").name[:15]
    return name, " ".join(getattr(sys, "orig_argv", None) or sys.argv)


def _own_process_match(verb: str, rest: list[str], words: list[str], process) -> bool:
    """Whether `pkill`/`killall` would select the gateway itself: its pattern matched against this process's name
    (`pkill python3`, `killall python3`) or, with -f, its whole command line (`pkill -f 'gateway run'`)."""
    if not process:
        return False
    name, command_line = process
    full = any(re.fullmatch(r"-[A-Za-z]*f[A-Za-z]*", token) or token == "--full" for token in rest)
    regex = verb == "pkill" or any(token in ("-r", "--regexp") for token in rest)
    for word in words:
        if not word or _dynamic(word):
            continue
        try:
            if verb == "pkill" or regex:
                if re.search(word, command_line if full else name):
                    return True
            elif word == name:
                return True
        except re.error:
            continue
    return False


def _stops_gateway(verb: str, rest: list[str], words: list[str], *, mentions: bool = False, process=None) -> bool:
    if verb in _DBUS:   # busctl/dbus-send/gdbus calling systemd's RestartUnit/StopUnit on the gateway
        return any(_DBUS_STOP.search(token) for token in rest) and any("hermes" in token.lower() for token in rest)
    if verb == "kill":   # the agent's shell is a child of Hermes: $PPID is Hermes; -1 is every process of the user
        return any(target in ("$PPID", "${PPID}", "-1") for target in _kill_targets(rest))
    if verb == "launchctl":   # macOS: kickstart -k restarts the agent, the others stop or unload it
        action = words[0].lower() if words else ""
        named = any(_LAUNCHD_LABEL.match(re.split(r"[\\/]", word)[-1]) for word in words[1:])
        if action == "kickstart":
            return named and any(re.fullmatch(r"-[A-Za-z]*k[A-Za-z]*", token) for token in rest)
        return named and action in _LAUNCHCTL_STOPS
    if verb in ("sc", "net"):   # Windows service control: sc stop <name>, net stop <name>
        return len(words) >= 2 and words[0].lower() in ("stop", "pause") and \
            any(_WINDOWS_NAME.match(word) for word in words[1:])
    if verb in _SERVICE_CMDLETS:   # PowerShell: Stop-Service -Name <name>, Stop-ScheduledTask -TaskName <name>
        return any(_WINDOWS_NAME.match(word) for word in words)
    if verb == "schtasks":
        return any(token.lower() == "/end" for token in rest) and any(_WINDOWS_NAME.match(token) for token in rest)
    if verb in ("taskkill", "stop-process"):
        return any("hermes" in token.lower() for token in rest)
    if verb == "systemctl":
        words = _without_values(rest, _SYSTEMCTL_VALUES)
        action = words[0] if words else ""
        stops = action in _STOP_ACTIONS or (action in _STOP_WITH_NOW and "--now" in rest) or _dynamic(action)
        # a unit the shell fills in ($U, xargs' words) counts when the command names Hermes somewhere
        return stops and any(_gateway_unit(word) or (mentions and _dynamic(word)) for word in words[1:])
    if verb == "service":
        return len(words) >= 2 and _gateway_unit(words[0]) and words[1] in ("restart", "stop")
    if verb in ("pkill", "killall"):
        return any("hermes" in word.lower() for word in words) or _own_process_match(verb, rest, words, process)
    if verb == "hermes":   # stop leaves Hermes down; restart is the owner's call
        words, options = _hermes_words(rest), {token.split("=", 1)[0] for token in rest if token.startswith("-")}
        for word, following in zip(words, words[1:]):
            if not (word == "gateway" or _dynamic(word)):
                continue
            if following in _GATEWAY_STOPS or _dynamic(following) or \
                    following in _GATEWAY_STOPS_WITH and _GATEWAY_STOPS_WITH[following] & options:
                return True
    return False


def _without_values(rest: list[str], takes_value) -> list[str]:
    """The words of a command line without its options and the values of the options that take one."""
    words, skip = [], False
    for token in rest:
        if skip:
            skip = False
        elif token in takes_value:
            skip = True
        elif not token.startswith("-"):
            words.append(token)
    return words


def _gateway_unit(word: str) -> bool:
    """A systemd unit of a Hermes gateway, also as a glob (`'hermes-gateway*'`, `*gateway*`) or a shell word."""
    if _UNIT.match(word):
        return True
    if any(char in word for char in "*?["):
        return any(fnmatch.fnmatchcase(name, word) for name in ("hermes-gateway", "hermes-gateway.service")) or \
            "hermes" in word.lower()
    return False


# `hermes gateway <action>` that stops or replaces the running gateway, always or with one of these options:
# uninstall stops and disables the service, start --all kills every gateway process first, run --replace
# replaces the running one, install --force reinstalls the service under it.
_GATEWAY_STOPS = frozenset({"stop", "restart", "uninstall"})
_GATEWAY_STOPS_WITH = {"start": {"--all"}, "run": {"--replace"}, "install": {"--force"}}


def _deleted_profile(verb: str, rest: list[str]) -> str | None:
    """The profile `hermes profile delete <name>` would delete with everything Hermes keeps for it (stopping its
    gateway first), or the word the shell fills in there."""
    if verb != "hermes":
        return None
    words = _hermes_words(rest)
    for index, word in enumerate(words[:-1]):
        if (word == "profile" or _dynamic(word)) and words[index + 1] == "delete":
            return words[index + 2] if index + 2 < len(words) else "?"
    return None


PLUGIN_ID = "hermes-atlas-evolution"
_PLUGIN_IDS = frozenset({PLUGIN_ID, "hermes-evolution"})   # and the id used up to 0.5.0
# Options of the `hermes` command line that take a value, wherever they appear: Hermes reads the profile flag
# even after the subcommand (hermes_cli.main._scan_profile_flag), so `hermes config set -p work <key>` and
# `hermes gateway -p work restart` are the commands without it.
_HERMES_VALUE_FLAGS = frozenset({"-p", "--profile", "-z", "--oneshot", "-m", "--model", "--provider", "--reasoning",
                                 "-t", "--toolsets", "-r", "--resume", "-s", "--skills", "--usage-file", "--in",
                                 "--cpu-profile", "--config"})


def _hermes_words(rest: list[str]) -> list[str]:
    """The words of a `hermes` command line as Hermes reads them: options left out, with the value of each one
    that takes a value."""
    words, skip = [], False
    for token in rest:
        if skip:
            skip = False
        elif token in _HERMES_VALUE_FLAGS:
            skip = True
        elif not token.startswith("-"):
            words.append(token)
    return words


def _dynamic(token: str) -> bool:
    """A word filled in when the command runs ($VAR, $'...', `...`, xargs' {} or its added words): what it names
    cannot be read here."""
    return "$" in token or "`" in token or "{}" in token


def _key_parts(key: str) -> list[str]:
    """A `hermes config set` key split the way Hermes splits it: at dots, except a dot escaped with a backslash."""
    return [part.replace("\0", ".").casefold() for part in key.replace("\\.", "\0").split(".")]


def _owner_key(key: str) -> bool:
    """Whether a config key holds what only the owner decides about plugins: the whole plugins section, the
    plugins Hermes enables, disables or isolates, and this plugin's own settings. Other plugins' settings
    (plugins.entries.<their id>) stay open to the agent."""
    if _dynamic(key):
        return True
    parts = _key_parts(key)
    if parts[0] != "plugins":
        return False
    return len(parts) < 3 or parts[1] != "entries" or parts[2] in _PLUGIN_IDS


def _changes_plugin(verb: str, rest: list[str]) -> bool:
    """The agent's own change to who this plugin trusts or whether it runs: `hermes config set|unset` of the
    plugins section or of this plugin's settings, `hermes config edit`, and `hermes plugins disable|remove` of
    this plugin. A word filled in by the shell where a key, an action or a name goes counts as such a change."""
    if verb != "hermes":
        return False
    words = _hermes_words(rest)
    for index, word in enumerate(words):
        following = words[index + 1] if index + 1 < len(words) else None
        if following is None:
            break
        shell_word = _dynamic(word)   # `hermes $G restart`: the group itself is filled in by the shell
        if word == "config" or shell_word:
            if following == "edit" or (word == "config" and _dynamic(following)):
                return True
            if following in ("set", "unset") and index + 2 < len(words) and _owner_key(words[index + 2]):
                return True
        if word == "plugins" or shell_word:
            if word == "plugins" and _dynamic(following):
                return True
            if following in ("disable", "remove", "rm", "uninstall", "trust") and \
                    any(name in _PLUGIN_IDS or _dynamic(name) for name in words[index + 2:]):
                return True
            if following == "install" and any(_dynamic(name) or re.split(r"[\\/:]", name.rstrip("/"))[-1].removesuffix(".git")
                                              in _PLUGIN_IDS for name in words[index + 2:]):
                return True   # installing over this plugin replaces its code
    return False


def _removed_paths(verb: str, rest: list[str], words: list[str]) -> list[str]:
    """The paths a removing command would delete or move away, after the shell's brace expansion."""
    if verb in _REMOVERS:
        if _WINDOWS:
            words = [word for word in words if not _CMD_SWITCH.fullmatch(word)]
        if verb == "mv":   # mv -t DIR SOURCE...: every other word goes
            target, sources, skip = False, [], False
            for token in rest:
                if skip:
                    skip = False
                elif token in ("-t", "--target-directory"):
                    target = skip = True
                elif token.startswith("--target-directory=") or re.fullmatch(r"-[A-Za-z]*t.+", token):
                    target = True
                elif not token.startswith("-"):
                    sources.append(token)
            words = sources
        else:
            target = False
        words = [item for word in words for item in _braces(word)]
        return words[:-1] if verb in _MOVERS and not target and len(words) > 1 else words
    if verb != "find":
        return []
    if "-delete" not in rest and not ("-exec" in rest and any(os.path.basename(token) == "rm" for token in rest)):
        return []
    starts = []
    for token in rest:   # find's starting points come before the first expression token
        if token.startswith(("-", "(", "!")):
            break
        starts.append(token)
    return starts


def _hit(paths: list[str], *, protected, home: Path, profile: Path | None = None, trees=(),
         cwd: PurePosixPath | None = None) -> str | None:
    for token in paths:
        pattern = _expand(token, home, profile, cwd)
        if pattern is None:
            continue
        for item in protected:
            if _hits(pattern, item):
                return str(item)
        for item in trees:
            if _hits(pattern, item, tree=True):
                return str(item)
    return None


def _nested_command(verb: str, rest: list[str]) -> list[str] | None:
    """Commands a program runs on the agent's behalf: `find ... -exec <command> ;`, `ssh localhost <command>`, and
    for an interpreter's inline program (`python -c`, `perl -e`, ...) each string literal and all of them joined
    (`subprocess.run(['systemctl', '--user', 'restart', 'hermes-gateway'])`)."""
    if verb == "find":
        for index, token in enumerate(rest):
            if token in ("-exec", "-execdir", "-ok", "-okdir"):
                tail = []
                for item in rest[index + 1:]:
                    if item in (";", "\\;", "+"):
                        break
                    tail.append(item)
                return [shlex.join(tail)] if tail else None
        return None
    if verb == "ssh":
        words = _without_values(rest, {"-p", "-l", "-i", "-o", "-F", "-J", "-L", "-R", "-D", "-W", "-b", "-c", "-E", "-m",
                                       "-O", "-Q", "-S", "-w", "-B", "-e", "-I"})
        host = words[0].rsplit("@", 1)[-1] if words else ""
        return [shlex.join(words[1:])] if host in _LOCALHOST and len(words) > 1 else None
    family = re.sub(r"[\d.]+\Z", "", verb)
    switches = _INLINE_CODE.get(family)
    if switches is None:
        return None
    for index, token in enumerate(rest):
        if token in switches and index + 1 < len(rest):
            literals = [a or b for a, b in _LITERAL.findall(rest[index + 1])]
            return [*literals, " ".join(literals)] if literals else None
    return None


def _hermes_config(token: str, *, home: Path, profile: Path | None, cwd: PurePosixPath | None) -> bool:
    """A word naming a Hermes config.yaml: the profile's, the root's, or one under a folder named for Hermes."""
    for part in re.split(r"[\s'\"=,;()\[\]]", token):
        if not part.lower().endswith(_CONFIG_FILES):
            continue
        found = _expand(part, home, profile, cwd)
        if found is None:
            if "hermes" in part.lower():
                return True
            continue
        places = {_canonical(str(item)) for item in (profile, Path(home) / ".hermes") if item is not None}
        if found.parent in places or "hermes" in str(found).lower():
            return True
    return False


def _writes_plugin_config(command: str, *, home: Path, profile: Path | None, cwd: PurePosixPath | None) -> bool:
    """A command that writes a Hermes config.yaml (`sed -i`, `yq -i`, a redirect, `cp`, `tee`, a Python one-liner
    that dumps or writes) and names the plugins section, this plugin or one of its trust settings: it can switch
    the plugin off or widen whom it trusts, which `hermes config set` of those keys is refused for."""
    lowered = command.lower()
    if not any(name in lowered for name in _CONFIG_FILES) or not _PLUGIN_WORDS.search(command):
        return False
    where = {"home": home, "profile": profile, "cwd": cwd}
    for target in re.findall(r">{1,2}\s*([^\s;&|]+)", command):   # a redirect into the file
        if _hermes_config(target, **where):
            return True
    for segment in _segments(command) or ():
        tokens = _strip_prefix(segment)
        if not tokens:
            continue
        verb, rest = _program(tokens[0]), tokens[1:]
        named = any(_hermes_config(token, **where) for token in rest)
        if not named:
            continue
        family = re.sub(r"[\d.]+\Z", "", verb)
        if family in ("sed", "perl", "yq", "ruby") and any(re.fullmatch(r"-[A-Za-z]*i\S*|--in-place\S*", token)
                                                          for token in rest):
            return True
        if verb in ("cp", "mv", "install", "tee", "truncate", "dd", "ln", "rsync"):
            return True
        if family in ("python", "node", "ruby", "perl") and re.search(r"dump|write|['\"]w['\"]", " ".join(rest)):
            return True
    return False


def _as_hermes(verb: str, rest: list[str]) -> tuple[str, list[str]]:
    """`hermes-agent ...`, `python -m hermes_cli.main ...` and `python /path/to/hermes ...` are the hermes command."""
    if verb == "hermes-agent":
        return "hermes", rest
    if not _PYTHON.fullmatch(verb):
        return verb, rest
    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "-m" or (token.startswith("-m") and len(token) > 2 and not token.startswith("--")):
            module, tail = (rest[index + 1] if index + 1 < len(rest) else "", rest[index + 2:]) if token == "-m" \
                else (token[2:], rest[index + 1:])
            return ("hermes", tail) if module in _HERMES_MODULES else (verb, rest)
        if token in _PYTHON_VALUES:
            index += 2
            continue
        if token == "-c" or not token.startswith("-") or token == "-":
            # inline code, or a script: the launcher script under a virtual environment's python
            return ("hermes", rest[index + 1:]) if _program(token) in ("hermes", "hermes-agent") else (verb, rest)
        index += 1
    return verb, rest


def _kills_by_lookup(command: str) -> bool:
    """`kill $(pgrep -f hermes)`, `pgrep -f hermes_cli | xargs kill`, ``kill `pidof hermes` ``, `kill $(cat
    ~/.hermes/gateway.pid)`: a kill whose process ids the shell fills in, in a command that looks up Hermes or its
    pid file."""
    kills = False
    for segment in _segments(command) or ():
        tokens = _strip_prefix(segment)
        if tokens and _program(tokens[0]) in ("kill", "tkill") and any(_dynamic(token) for token in tokens[1:]):
            kills = True
    if not kills:
        return False
    looked_up = False
    flat = command.replace('"', " ").replace("`", "\n").replace("$(", "\n").replace(")", "\n")
    for segment in _segments(flat) or ():
        tokens = _strip_prefix(segment)
        if not tokens:
            continue
        verb, rest = _program(tokens[0]), tokens[1:]
        if verb in _PID_LOOKUPS and any("hermes" in token.lower() for token in rest):
            looked_up = True
        elif any(name in token.lower() for token in tokens for name in _PID_FILES):
            looked_up = True
        elif verb == "systemctl" and "show" in rest and any(_gateway_unit(token) for token in rest):
            looked_up = True
    return looked_up


def check(tool_name, args, *, protected, home: Path, profile: Path | None = None, trees=(), own=(),
          process=None) -> dict | None:
    """{"kind", "path"?} when the command would take Hermes down; None otherwise. `home` expands ~ and $HOME,
    `profile` (the profile's HERMES_HOME) expands $HERMES_HOME. `trees` are folders whose inside is protected too
    (the interpreter, the environment, Hermes' packages); removing anything in `own` (this plugin's folder) switches
    the plugin off, which only the owner decides."""
    if not isinstance(args, dict):
        return None
    if tool_name in PROCESS_TOOLS:   # text written to a background process, often a shell
        command = args.get("data") if args.get("action") in _PROCESS_INPUT else None
    elif tool_name in TERMINAL_TOOLS:
        command = args.get("command")
    else:
        return None
    if not isinstance(command, str) or not command.strip() or len(command) > _COMMAND_LIMIT:
        return None
    workdir = args.get("workdir") if tool_name in TERMINAL_TOOLS else None
    cwd = _expand(workdir, home, profile) if isinstance(workdir, str) and workdir.strip() else None
    return _check_command(command, protected=protected, home=home, profile=profile, nested=False, trees=trees, own=own,
                          process=process, cwd=cwd)


def _check_command(command: str, *, protected, home: Path, profile: Path | None = None, nested: bool, trees=(), own=(),
                   process=None, cwd: PurePosixPath | None = None) -> dict | None:
    options = {"protected": protected, "home": home, "profile": profile, "trees": trees, "own": own, "process": process}
    if _kills_by_lookup(command):
        return {"kind": "gateway_stop"}
    if _writes_plugin_config(command, home=home, profile=profile, cwd=cwd):
        return {"kind": "plugin_settings"}
    mentions = "hermes" in command.lower()
    segments = _segments(command) or []
    fed = [token for segment in segments for token in segment]   # what xargs may be given: every word of the command
    if not nested:   # $(...) and `...` run too, also inside double quotes
        for groups in _SUBSTITUTION.findall(command):
            found = _check_command(groups[0] or groups[1], nested=True, cwd=cwd, **options)
            if found:
                return found
    for segment in segments:
        tokens = _strip_prefix(segment)
        if not tokens:
            continue
        verb, rest = _as_hermes(_program(tokens[0]), tokens[1:])
        nested_command = _nested_command(verb, rest) if not nested else None
        if nested_command is not None:   # find -exec ..., ssh localhost ..., the strings of python -c / perl -e
            for item in nested_command:
                found = _check_command(item, nested=True, cwd=cwd, **options)
                if found:
                    return found
        if verb in ("cd", "pushd", "chdir", "set-location", "sl"):   # later relative paths start there
            target = [token for token in rest if not token.startswith("-")]
            cwd = _expand(target[0], home, profile, cwd) if target else _canonical(str(home)) if not rest else None
            continue
        inner = _command_string(verb, rest)
        if inner is not None:   # one level: bash -lc '<command>', su -c '<command>', pwsh -c '<command>', cmd /c ...
            found = None if nested else _check_command(inner, nested=True, cwd=cwd, **options)
            if found:
                return found
            continue
        words = [token for token in rest if not token.startswith("-")]
        if _stops_gateway(verb, rest, words, mentions=mentions, process=process):
            return {"kind": "gateway_stop"}
        if _changes_plugin(verb, rest):
            return {"kind": "plugin_settings"}
        deleted = _deleted_profile(verb, rest)
        if deleted is not None:
            return {"kind": "runtime_removal", "path": f"`{deleted}`"}
        removed = _removed_paths(verb, rest, words)
        if XARGS_WORDS in removed:   # `ls -d <path> | xargs rm -rf`: the paths come from the rest of the command
            removed = [item for item in removed if item != XARGS_WORDS] + [token for token in fed if token not in tokens]
        path = _hit(removed, protected=protected, home=home, profile=profile, trees=trees, cwd=cwd)
        if path:
            return {"kind": "runtime_removal", "path": path}
        if own and _hit(removed, protected=(), home=home, profile=profile, trees=own, cwd=cwd):
            return {"kind": "plugin_settings"}
    return None


class RuntimeGuard:
    """The `pre_tool_call` callback. Returns Hermes' block directive or None, and never raises."""

    def __init__(self, *, protected, home: Path, lang: str = "en", on_block=None, profile: Path | None = None,
                 trees=(), own=(), process=None):
        self.protected = tuple(protected)
        self.trees, self.own, self.process = tuple(trees), tuple(own), process
        self.home = Path(home)
        self.profile = Path(profile) if profile is not None else None
        self.lang = lang if lang in MESSAGES else "en"
        self.on_block = on_block

    def __call__(self, **payload):
        try:
            found = check(payload.get("tool_name"), payload.get("args"), protected=self.protected, home=self.home,
                          profile=self.profile, trees=self.trees, own=self.own, process=self.process)
            if not found:
                return None
            if self.on_block is not None:
                try:
                    self.on_block(found["kind"])
                except Exception:   # counting must never decide whether the command runs
                    pass
            return {"action": "block", "message": MESSAGES[self.lang][found["kind"]].format(path=found.get("path", ""))}
        except Exception:   # Hermes blocks a tool whose pre_tool_call raises: any doubt lets the command through
            return None
