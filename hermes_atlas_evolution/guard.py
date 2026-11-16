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
                           "condrestart", "force-reload"})
_REMOVERS = frozenset({"rm", "rmdir", "unlink", "shred", "mv", "del", "erase", "rd", "remove-item", "ri", "move",
                       "move-item", "mi"})
_MOVERS = frozenset({"mv", "move", "move-item", "mi"})   # the last word is where things go, not what goes
_CMD_SWITCH = re.compile(r"/[A-Za-z?]{1,2}\Z")          # del /s /q: switches of cmd's own commands
# Variables expanded at the start of a path: the homes, and on Windows the profile folders Git Bash exports.
_ENV_PREFIX = re.compile(r"(?:\$\{(\w+)\}|\$env:(\w+)|\$(\w+)|%(\w+)%)(?=[\\/]|\Z)", re.I)
_EXPANDED = frozenset({"USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA"})
_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
# Wrappers that run the command after their own options: name -> options that take a separate value.
_WRAPPERS = {"sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t"}, "env": {"-u", "-C"}, "nohup": set(),
             "timeout": {"-s", "-k", "--signal", "--kill-after"}, "nice": {"-n", "--adjustment"},
             "ionice": {"-c", "-n", "-p"}, "stdbuf": {"-i", "-o", "-e"}, "exec": set(), "command": set(),
             "builtin": set(), "time": set(), "chrt": set(), "doas": {"-u"}, "setsid": set(),
             "systemd-run": {"-u", "--unit", "-p", "--property", "-E", "--setenv", "-M", "--machine", "-H", "--host",
                             "--description", "--slice", "--uid", "--gid", "-d", "--working-directory"},
             "runuser": {"-u", "-g", "-G"}, "watch": {"-n", "--interval"}, "flock": {"-w", "--timeout", "-E"},
             "xargs": {"-n", "-I", "-P", "-d", "-L", "-s", "-a", "-E"}}
_TAKES_POSITIONAL = {"timeout": 1, "chrt": 1, "flock": 1}   # the duration, the priority, the lock file
# Programs that run a command given as one string after -c (shells also accept it inside -lc, -ec, ...).
_STRING_RUNNERS = frozenset({"su", "runuser", "script", "flock"})
_RTK_SUBCOMMANDS = frozenset({"run", "err", "summary", "test", "bypass", "proxy"})
_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")", "\n", ";;", "|&"})

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
        if _ASSIGNMENT.match(token):
            index += 1
            continue
        name = os.path.basename(token)
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
    for position, token in enumerate(rest):
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


def _expand(token: str, home: Path, profile: Path | None = None) -> PurePosixPath | None:
    if token == "~" or token.startswith(("~/", "~\\")):
        token = str(home) + token[1:]
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
    return _canonical(token)


def _hits(pattern: PurePosixPath, protected) -> bool:
    """True when removing `pattern` would remove `protected`: the same path, an ancestor, or a glob of either."""
    target = _canonical(str(protected))
    if target is None:
        return False
    chain = [target, *target.parents]
    if any(char in str(pattern) for char in "*?["):
        return any(fnmatch.fnmatchcase(str(item), str(pattern)) for item in chain)
    return pattern in chain


def _stops_gateway(verb: str, rest: list[str], words: list[str]) -> bool:
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
        action = words[0] if words else ""
        stops = action in _STOP_ACTIONS or (action == "disable" and "--now" in rest)
        return stops and any(_UNIT.match(word) for word in words[1:])
    if verb == "service":
        return len(words) >= 2 and bool(_UNIT.match(words[0])) and words[1] in ("restart", "stop")
    if verb in ("pkill", "killall"):
        return any("hermes" in word.lower() for word in words)
    if verb == "hermes":   # stop leaves Hermes down; restart is the owner's call
        words, options = _hermes_words(rest), {token.split("=", 1)[0] for token in rest if token.startswith("-")}
        for word, following in zip(words, words[1:]):
            if not (word == "gateway" or _dynamic(word)):
                continue
            if following in _GATEWAY_STOPS or _dynamic(following) or \
                    following in _GATEWAY_STOPS_WITH and _GATEWAY_STOPS_WITH[following] & options:
                return True
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
            if following in ("disable", "remove", "rm", "uninstall") and \
                    any(name in _PLUGIN_IDS or _dynamic(name) for name in words[index + 2:]):
                return True
    return False


def _removed_paths(verb: str, rest: list[str], words: list[str]) -> list[str]:
    """The paths a removing command would delete or move away."""
    if verb in _REMOVERS:
        if _WINDOWS:
            words = [word for word in words if not _CMD_SWITCH.fullmatch(word)]
        return words[:-1] if verb in _MOVERS and len(words) > 1 else words
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


def _hit(paths: list[str], *, protected, home: Path, profile: Path | None = None) -> str | None:
    for token in paths:
        pattern = _expand(token, home, profile)
        if pattern is None:
            continue
        for item in protected:
            if _hits(pattern, item):
                return str(item)
    return None


def check(tool_name, args, *, protected, home: Path, profile: Path | None = None) -> dict | None:
    """{"kind", "path"?} when the command would take Hermes down; None otherwise. `home` expands ~ and $HOME,
    `profile` (the profile's HERMES_HOME) expands $HERMES_HOME."""
    if tool_name not in TERMINAL_TOOLS or not isinstance(args, dict):
        return None
    command = args.get("command")
    if not isinstance(command, str) or not command.strip() or len(command) > _COMMAND_LIMIT:
        return None
    return _check_command(command, protected=protected, home=home, profile=profile, nested=False)


def _check_command(command: str, *, protected, home: Path, profile: Path | None = None, nested: bool) -> dict | None:
    for segment in _segments(command) or ():
        tokens = _strip_prefix(segment)
        if not tokens:
            continue
        verb, rest = _program(tokens[0]), tokens[1:]
        inner = _command_string(verb, rest)
        if inner is not None:   # one level: bash -lc '<command>', su -c '<command>', pwsh -c '<command>', cmd /c ...
            found = None if nested else _check_command(inner, protected=protected, home=home, profile=profile,
                                                       nested=True)
            if found:
                return found
            continue
        words = [token for token in rest if not token.startswith("-")]
        if _stops_gateway(verb, rest, words):
            return {"kind": "gateway_stop"}
        if _changes_plugin(verb, rest):
            return {"kind": "plugin_settings"}
        deleted = _deleted_profile(verb, rest)
        if deleted is not None:
            return {"kind": "runtime_removal", "path": f"`{deleted}`"}
        path = _hit(_removed_paths(verb, rest, words), protected=protected, home=home, profile=profile)
        if path:
            return {"kind": "runtime_removal", "path": path}
    return None


class RuntimeGuard:
    """The `pre_tool_call` callback. Returns Hermes' block directive or None, and never raises."""

    def __init__(self, *, protected, home: Path, lang: str = "en", on_block=None, profile: Path | None = None):
        self.protected = tuple(protected)
        self.home = Path(home)
        self.profile = Path(profile) if profile is not None else None
        self.lang = lang if lang in MESSAGES else "en"
        self.on_block = on_block

    def __call__(self, **payload):
        try:
            found = check(payload.get("tool_name"), payload.get("args"), protected=self.protected, home=self.home,
                          profile=self.profile)
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
