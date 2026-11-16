"""Runtime guard: the agent must not take down the Hermes it runs in.

On a real server the agent restarted its own gateway with systemctl a dozen times in one day, cutting
turns in progress, and once deleted the Python runtime the gateway unit used, which kept Hermes down for
an hour. Hermes has a safe path for the first (`hermes gateway restart` waits for running turns) and
nothing good comes from the second. The guard vetoes exactly those terminal commands through
`pre_tool_call` and tells the agent the safe alternative.

`check` is a pure function over the command: no I/O, a bounded input, and any doubt (an unparsable
command, a relative path, an unknown tool) lets the command through. Hermes blocks a tool when a
`pre_tool_call` callback raises, so the hook wrapper never raises.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
import sys
from pathlib import Path, PurePosixPath

TERMINAL_TOOLS = frozenset({"terminal", "shell", "bash", "run_command", "execute_command"})
_COMMAND_LIMIT = 20_000   # longer commands (big inline files) are let through unparsed
_UNIT = re.compile(r"hermes-gateway(?:@[\w.-]+)?(?:\.service)?\Z", re.I)
_STOP_ACTIONS = frozenset({"restart", "stop", "kill", "try-restart", "reload-or-restart", "try-reload-or-restart",
                           "condrestart", "force-reload"})
_REMOVERS = frozenset({"rm", "rmdir", "unlink", "shred", "mv"})
_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
# Wrappers that run the command after their own options: name -> options that take a separate value.
_WRAPPERS = {"sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t"}, "env": {"-u", "-C", "-S"}, "nohup": set(),
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
                         "from their own terminal, which waits for the running turns to finish."),
        "runtime_removal": ("Atlas Evolution runtime guard: this command would delete or move {path}, which the running "
                            "Hermes needs; Hermes would stop working until someone repairs it by hand. Leave it in place. "
                            "Removing it is for the owner to do from their own terminal, with Hermes stopped."),
    },
    "pt": {
        "gateway_stop": ("Guarda de runtime do Atlas Evolution: este comando pararia ou reiniciaria o gateway do Hermes em "
                         "que esta conversa roda, cortando os turnos em andamento, inclusive este. Não tente de outra forma. "
                         "Diga ao dono que é preciso reiniciar e por quê; o dono roda `hermes gateway restart` no próprio "
                         "terminal, e esse comando espera os turnos em andamento terminarem."),
        "runtime_removal": ("Guarda de runtime do Atlas Evolution: este comando apagaria ou moveria {path}, de que o Hermes "
                            "em execução precisa; o Hermes pararia de funcionar até alguém consertar à mão. Deixe como está. "
                            "Remover isso fica para o dono, no próprio terminal, com o Hermes parado."),
    },
}


def protected_paths(*, hermes_root=None, profile_home=None) -> tuple[Path, ...]:
    """What the running Hermes needs: its interpreter and environment, its source, its root and this profile."""
    found = set()
    for value in (sys.base_prefix, sys.prefix, sys.exec_prefix):
        try:
            found.add(Path(value).resolve())
        except (OSError, RuntimeError, ValueError):
            pass
    module = sys.modules.get("hermes_cli")
    location = getattr(module, "__file__", None) if module is not None else None
    if isinstance(location, str):
        found.add(Path(location).resolve().parent.parent)
    for value in (hermes_root, profile_home):
        if value is not None:
            found.add(Path(value).resolve())
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


def _strip_prefix(tokens: list[str]) -> list[str]:
    """Drop variable assignments and wrappers such as sudo, env, nohup, timeout, setsid, systemd-run, flock,
    watch, xargs and rtk run before the real command."""
    index = 0
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
        index += 1
        with_value = _WRAPPERS[name]
        while index < len(tokens) and tokens[index].startswith("-"):
            if tokens[index] == "--":
                index += 1
                break
            index += 2 if tokens[index] in with_value else 1
        index += _TAKES_POSITIONAL.get(name, 0)
    return tokens[index:]


def _command_string(verb: str, rest: list[str]) -> str | None:
    """The command a shell (-c, -lc, -ec, ...) or su, runuser, script or flock (-c, --command) gets as one string."""
    for position, token in enumerate(rest):
        if verb in _SHELLS and re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", token):
            return rest[position + 1] if position + 1 < len(rest) else None
        if verb in _STRING_RUNNERS and token in ("-c", "--command"):
            return rest[position + 1] if position + 1 < len(rest) else None
        if verb in _STRING_RUNNERS and re.fullmatch(r"-[A-Za-z]*c", token) and verb == "script":
            return rest[position + 1] if position + 1 < len(rest) else None   # script -qc '<command>'
    return None


def _expand(token: str, home: Path) -> PurePosixPath | None:
    for prefix in ("~/", "$HOME/", "${HOME}/"):
        if token.startswith(prefix):
            token = f"{home}/{token[len(prefix):]}"
    if token in ("~", "$HOME", "${HOME}"):
        token = str(home)
    if not token.startswith("/"):
        return None   # relative to an unknown directory
    return PurePosixPath(os.path.normpath(token.rstrip("/") or "/"))


def _hits(pattern: PurePosixPath, protected: Path) -> bool:
    """True when removing `pattern` would remove `protected`: the same path, an ancestor, or a glob of either."""
    target = PurePosixPath(protected)
    chain = [target, *target.parents]
    if any(char in str(pattern) for char in "*?["):
        return any(fnmatch.fnmatchcase(str(item), str(pattern)) for item in chain)
    return pattern in chain


def _stops_gateway(verb: str, rest: list[str], words: list[str]) -> bool:
    if verb == "systemctl":
        action = words[0] if words else ""
        stops = action in _STOP_ACTIONS or (action == "disable" and "--now" in rest)
        return stops and any(_UNIT.match(word) for word in words[1:])
    if verb == "service":
        return len(words) >= 2 and bool(_UNIT.match(words[0])) and words[1] in ("restart", "stop")
    if verb in ("pkill", "killall"):
        return any("hermes" in word.lower() for word in words)
    if verb == "hermes" and "gateway" in words:   # stop leaves Hermes down; restart is the owner's call
        position = words.index("gateway") + 1
        return position < len(words) and words[position] in ("stop", "restart")
    return False


def _removed_paths(verb: str, rest: list[str], words: list[str]) -> list[str]:
    """The paths a removing command would delete or move away."""
    if verb in _REMOVERS:
        return words[:-1] if verb == "mv" and len(words) > 1 else words
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


def _hit(paths: list[str], *, protected, home: Path) -> str | None:
    for token in paths:
        pattern = _expand(token, home)
        if pattern is None:
            continue
        for item in protected:
            if _hits(pattern, item):
                return str(item)
    return None


def check(tool_name, args, *, protected, home: Path) -> dict | None:
    """{"kind", "path"?} when the command would take Hermes down; None otherwise."""
    if tool_name not in TERMINAL_TOOLS or not isinstance(args, dict):
        return None
    command = args.get("command")
    if not isinstance(command, str) or not command.strip() or len(command) > _COMMAND_LIMIT:
        return None
    return _check_command(command, protected=protected, home=home, nested=False)


def _check_command(command: str, *, protected, home: Path, nested: bool) -> dict | None:
    for segment in _segments(command) or ():
        tokens = _strip_prefix(segment)
        if not tokens:
            continue
        verb, rest = os.path.basename(tokens[0]), tokens[1:]
        inner = _command_string(verb, rest)
        if inner is not None:   # one level: bash -lc '<command>', su -c '<command>', flock FILE -c '<command>'
            found = None if nested else _check_command(inner, protected=protected, home=home, nested=True)
            if found:
                return found
            continue
        words = [token for token in rest if not token.startswith("-")]
        if _stops_gateway(verb, rest, words):
            return {"kind": "gateway_stop"}
        path = _hit(_removed_paths(verb, rest, words), protected=protected, home=home)
        if path:
            return {"kind": "runtime_removal", "path": path}
    return None


class RuntimeGuard:
    """The `pre_tool_call` callback. Returns Hermes' block directive or None, and never raises."""

    def __init__(self, *, protected, home: Path, lang: str = "en", on_block=None):
        self.protected = tuple(protected)
        self.home = Path(home)
        self.lang = lang if lang in MESSAGES else "en"
        self.on_block = on_block

    def __call__(self, **payload):
        try:
            found = check(payload.get("tool_name"), payload.get("args"), protected=self.protected, home=self.home)
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
