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
_COMMAND_LIMIT = 20_000   # the real command is read up to here; trailing padding (comments, heredoc bodies) is cut first
_SCAN_LIMIT = 100_000     # never strip/scan more than this much raw text, so a huge input cannot cost much
_MAX_SUBCHECKS = 2_000    # how many commands-inside-commands (substitutions, nesting, piped-into shells) are looked at
_MAX_HEREDOC_BODIES = 64  # here-document bodies kept for a shell reading them on stdin; the rest is padding
_MAX_FED = 64             # paths carried along a pipeline for a later `xargs rm`; past this it is not a tidy removal
_MAX_FINDS = 32           # pending `find` producers carried along a pipeline for a later `xargs rm`
_WINDOWS = os.name == "nt"   # the Windows rules (drive letters, case-insensitive paths)
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
# Taking the machine (or the owner's user manager) down takes the gateway with it.
_POWER = frozenset({"reboot", "shutdown", "poweroff", "halt", "restart-computer", "stop-computer"})
_SYSTEMCTL_POWER = frozenset({"reboot", "poweroff", "halt", "kexec", "soft-reboot", "exit"})
_MAX_DEPTH = 8   # commands inside commands (bash -c "bash -c '...'", $(...), find -exec) looked into this deep
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
             "busybox": set(), "toybox": set(),   # applet multiplexers: `busybox rm ...` runs the applet after them
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
# Programs `_command_string` reads a command string from, and the wrappers among them (watch, env -S, flock -c).
_STRING_VERBS = _SHELLS | _STRING_RUNNERS | {"powershell", "pwsh", "cmd", "env", "eval", "watch"}
_STRING_WRAPPERS = frozenset({"watch", "env", "flock", "runuser"})
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
# The dashboard panel's decision route (0.8.1): only the owner, logged in to the dashboard, decides there.
_PANEL_DECISIONS = re.compile(r"hermes-(?:atlas-)?evolution/decide")
_ANY_PLUGIN_DECIDE = re.compile(r"api/plugins/[^\s]*/decide")


def _calls_panel_decisions(command: str) -> bool:
    """A command that calls the panel's decision route, however the URL is spelled: %-escapes (also twice), quotes
    or backslashes inside words, `/./`, `//` and `/x/../`, or the plugin's name filled in by the shell."""
    from urllib.parse import unquote

    text = command
    for _ in range(3):
        text = unquote(text)
    text = re.sub(r"[\"'\\]", "", text).lower()
    text = re.sub(r"/(?:\./)+", "/", re.sub(r"/{2,}", "/", text))
    previous = None
    while previous != text:
        previous, text = text, re.sub(r"/[^/\s]+/\.\.(?=/)", "", text)
    if _PANEL_DECISIONS.search(text):
        return True
    return bool(_ANY_PLUGIN_DECIDE.search(text)) and ("evolution" in text or "$" in text or "`" in text)

MESSAGES = {
    "en": {
        "gateway_stop": ("Atlas Evolution runtime guard: this command would stop or restart the Hermes gateway that this "
                         "conversation runs on, cutting off the turns in progress, including this one. Do not retry it in "
                         "another form. Tell the owner a restart is needed and why; the owner runs `hermes gateway restart` "
                         "from their own terminal, which lists the turns still running and asks before interrupting them."),
        "runtime_removal": ("Atlas Evolution runtime guard: this command would delete or move {path}, which the running "
                            "Hermes needs; Hermes would stop working until someone repairs it by hand. Leave it in place. "
                            "Removing it is for the owner to do from their own terminal, with Hermes stopped."),
        "runtime_change": ("Atlas Evolution runtime guard: this command would change, overwrite or lock {path}, which the "
                           "running Hermes runs on; Hermes would stop working until someone repairs it by hand. Leave it "
                           "as it is. Changing it is for the owner to do from their own terminal, with Hermes stopped."),
        "plugin_settings": ("Atlas Evolution runtime guard: this command would change the settings of Atlas Evolution "
                            "(who counts as the owner, its protections) or switch the plugin off. Only the owner decides "
                            "that, from their own terminal. Do not retry it in another form; tell the owner what you "
                            "wanted to change and why."),
    },
    "pt": {
        "gateway_stop": ("Proteção do Hermes (Atlas Evolution): este comando pararia ou reiniciaria o gateway do Hermes em "
                         "que esta conversa roda, cortando os turnos em andamento, inclusive este. Não tente de outra forma. "
                         "Diga ao dono que é preciso reiniciar e por quê; o dono roda `hermes gateway restart` no próprio "
                         "terminal, e esse comando mostra os turnos ainda em andamento e pergunta antes de interrompê-los."),
        "runtime_removal": ("Proteção do Hermes (Atlas Evolution): este comando apagaria ou moveria {path}, de que o Hermes "
                            "em execução precisa; o Hermes pararia de funcionar até alguém consertar à mão. Deixe como está. "
                            "Remover isso fica para o dono, no próprio terminal, com o Hermes parado."),
        "runtime_change": ("Proteção do Hermes (Atlas Evolution): este comando mudaria, sobrescreveria ou travaria {path}, "
                           "em que o Hermes em execução roda; o Hermes pararia de funcionar até alguém consertar à mão. "
                           "Deixe como está. Mudar isso fica para o dono, no próprio terminal, com o Hermes parado."),
        "plugin_settings": ("Proteção do Hermes (Atlas Evolution): este comando mudaria as configurações do Atlas Evolution "
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


# `<<EOF`, `<<-'EOF'`; never the here-string `<<<word`, which feeds one word and has no body.
_HEREDOC = re.compile(r"(?<!<)<<(?!<)(-?)\s*(['\"]?)([A-Za-z_]\w*)\2")


def _heredoc_openers(line: str, single: bool, double: bool) -> tuple[list[tuple[bool, bool, str]], bool, bool]:
    """The here-document operators (`<<WORD`) opened on `line`, left to right, read only outside quotes and a
    comment, as (strip tabs, quoted, delimiter); and the quote state still open at the line's end (quotes carry
    across lines). A `<<<word` here-string is not one, and a quoted or in-word `<<` is ignored."""
    openers, index, length = [], 0, len(line)
    while index < length:
        char = line[index]
        if char == "\\" and not single and index + 1 < length:
            index += 2
            continue
        if char == "'" and not double:
            single = not single
        elif char == '"' and not single:
            double = not double
        elif char == "#" and not single and not double and (index == 0 or line[index - 1] in " \t\r;&|()"):
            break   # the rest of the line is a comment
        elif char == "<" and not single and not double:
            match = _HEREDOC.match(line, index)
            if match:
                openers.append((match.group(1) == "-", bool(match.group(2)), match.group(3)))
                index = match.end()
                continue
        index += 1
    return openers, single, double


def _heredocs(command: str) -> tuple[str, list[str]]:
    """(the command without its here-document bodies, the bodies of the unquoted ones): a body is data written
    somewhere (a script, a note), not a command run now, but the shell still expands $(...) in an unquoted one.
    A `<<` is read only outside quotes and comments; an unterminated heredoc's lines are kept as commands."""
    if "<<" not in command:
        return command, []   # no here-document: nothing to cut (the common case, and cheap)
    lines, kept, expanded, index = command.split("\n"), [], [], 0
    single = double = False
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        index += 1
        openers, single, double = _heredoc_openers(line, single, double)
        for tabs, quoted, delimiter in openers:
            body, found = [], False
            while index < len(lines):
                stripped = lines[index].lstrip("\t") if tabs else lines[index]
                index += 1
                if stripped == delimiter:
                    found = True
                    break
                body.append(stripped)
            if found:
                if not quoted:
                    expanded.extend(body)
            else:   # no delimiter line: those lines are commands, not a body, so keep them for inspection
                kept.extend(body)
                index = len(lines)
                break
    return "\n".join(kept), expanded


def _heredoc_body_map(command: str) -> dict[str, list[str]]:
    """delimiter -> the bodies fed with `<<DELIM` (quoted ones too), so a shell reading its script on stdin can
    be inspected. A body that never reaches its delimiter is left out (its lines are ordinary commands)."""
    if "<<" not in command:
        return {}
    lines, bodies, index, total = command.split("\n"), {}, 0, 0
    single = double = False
    while index < len(lines) and total < _MAX_HEREDOC_BODIES:   # past the cap the rest is padding, not a script
        line = lines[index]
        index += 1
        openers, single, double = _heredoc_openers(line, single, double)
        for tabs, _quoted, delimiter in openers:
            body, found = [], False
            while index < len(lines):
                stripped = lines[index].lstrip("\t") if tabs else lines[index]
                index += 1
                if stripped == delimiter:
                    found = True
                    break
                body.append(stripped)
            if found:
                kept = bodies.setdefault(delimiter, [])
                text = "\n".join(body)
                if text not in kept:   # identical bodies (padding that repeats one heredoc) are kept once
                    kept.append(text)
                    total += 1
            else:
                index = len(lines)
                break
    return bodies


def _strip_comments(command: str) -> str:
    """The command without its real shell comments: a `#` outside quotes that begins a word (at the start, or
    after whitespace or a separator) to its line's end. A `#` inside a word (`http://x/#/`, `$#`, `${#A[@]}`) is
    kept, so the lexer sees it as part of the word."""
    if "#" not in command:
        return command   # no `#` at all: no comment to cut
    out, index, single, double = [], 0, False, False
    while index < len(command):
        char = command[index]
        if char == "\\" and not single and index + 1 < len(command):
            out.append(command[index:index + 2])
            index += 2
            continue
        if char == "'" and not double:
            single = not single
        elif char == '"' and not single:
            double = not double
        elif char == "#" and not single and not double and (index == 0 or command[index - 1] in " \t\r\n;&|()"):
            newline = command.find("\n", index)
            if newline < 0:
                break
            index = newline   # keep the newline so the separators around the comment survive
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _strip_heredocs(command: str) -> str:
    """Drop here-document bodies: they are data written somewhere (a script, a note), not commands run now."""
    return _heredocs(command)[0]


def _without_single_quotes(command: str) -> str:
    """The command with its single-quoted spans ('...' and $'...') left out: the shell runs nothing inside them.
    Inside double quotes a single quote is an ordinary character."""
    if "'" not in command:
        return command   # no single quote: nothing to leave out
    out, index, double = [], 0, False
    while index < len(command):
        char = command[index]
        if char == "\\" and index + 1 < len(command):
            out.append(command[index:index + 2])
            index += 2
            continue
        if char == '"':
            double = not double
        elif char == "'" and not double:
            end = command.find("'", index + 1)
            if end < 0:
                return "".join(out) + command[index:]   # unbalanced: keep the rest, let the lexer decide
            if out and out[-1] == "$":
                out.pop()   # $'...': ANSI-C quoting, single-quoted as well
            index = end + 1
            out.append(" ")
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _substitutions(command: str) -> list[str]:
    """The commands inside $(...) and `...` that run when the command runs: also inside double quotes and in the
    body of an unquoted here-document, never inside single quotes or a quoted here-document."""
    stripped, expanded = _heredocs(command)
    found = [a or b for a, b in _SUBSTITUTION.findall(_without_single_quotes(stripped))]
    for body in expanded:
        found += [a or b for a, b in _SUBSTITUTION.findall(body)]
    return found


def _prepare(command: str) -> str:
    """The command as the lexer reads it: here-document bodies dropped, line continuations joined, comments cut."""
    command = _strip_heredocs(command)
    # A backslash-newline is a line continuation, not a separator; join it only after the heredoc step so a
    # quoted heredoc body ending in `\` cannot swallow its delimiter.
    command = re.sub(r"\\\r?\n", "", command)
    command = _strip_comments(command)   # a real `#` comment is not part of any command
    return _noclobber(command) if ">|" in command else command


def _noclobber(command: str) -> str:
    """`>|` (a redirect that overrides noclobber) as the plain `>` it is: the lexer would read its `|` as a pipe and
    lose the file it truncates. Only outside quotes; a quoted `>|` is text."""
    out, index, single, double, length = [], 0, False, False, len(command)
    while index < length:
        char = command[index]
        if char == "\\" and not single and index + 1 < length:
            out.append(command[index:index + 2])
            index += 2
            continue
        if char == "'" and not double:
            single = not single
        elif char == '"' and not single:
            double = not double
        elif char == ">" and not single and not double and command.startswith(">|", index):
            out.append("> ")
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _segments_with_ops(command: str) -> list[tuple[str, list[str]]] | None:
    """Each command segment with the separator token that preceded it ("" for the first): the `|` before a
    segment marks it as part of the same pipeline as what came before."""
    return _lex(_prepare(command))


# Put before each unquoted `>` (the first of a run) so the lexer's tokens show which `>` the shell reads as a
# redirect: a character no quoted text can carry through, and a word character, so token boundaries do not move.
_MARK = "\x01"


def _mark_redirects(prepared: str) -> str:
    """The prepared command with _MARK before each `>` the shell reads as a redirect operator: outside single and
    double quotes, not escaped, and not the second `>` of `>>`. A `>` inside quotes is text, never marked."""
    out, index, single, double, length = [], 0, False, False, len(prepared)
    while index < length:
        char = prepared[index]
        if char == "\\" and not single and index + 1 < length:
            out.append(prepared[index:index + 2])
            index += 2
            continue
        if char == "'" and not double:
            single = not single
        elif char == '"' and not single:
            double = not double
        elif char == ">" and not single and not double and (index == 0 or prepared[index - 1] != ">"):
            out.append(_MARK)
        out.append(char)
        index += 1
    return "".join(out)


def _segment_redirects(prepared: str, seg_ops) -> list[list[tuple[str, str]]] | None:
    """For each segment of `seg_ops` (lexed from `prepared`), the files its redirects write, as (word, "redirect" or
    "append"). None when the marked lexing does not line up with the plain one (then nothing is read per segment)."""
    if ">" not in prepared:
        return [[] for _ in seg_ops]
    marked = _lex(_mark_redirects(prepared))
    if marked is None or len(marked) != len(seg_ops) or \
            any(len(tokens) != len(plain) for (_op, tokens), (_op2, plain) in zip(marked, seg_ops)):
        return None
    found = []
    for _op, tokens in marked:
        targets = []
        for position, token in enumerate(tokens):
            if _MARK not in token:
                continue
            tail = token.rpartition(_MARK)[2]   # the last redirect in the word: `>file`, `>>file`, `2>&1`
            kind = "append" if tail.startswith(">>") else "redirect"
            target = tail.lstrip(">").lstrip("|")
            if not target and position + 1 < len(tokens):
                target = tokens[position + 1]   # `> file`: the next word is the file
            target = target.replace(_MARK, "")
            if target and not target.startswith("&"):   # `2>&1` duplicates a descriptor, it names no file
                targets.append((target, kind))
        found.append(targets)
    return found


def _lex(command: str) -> list[tuple[str, list[str]]] | None:
    """Split a prepared command into segments, each with the separator token that preceded it."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()\n")
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r"
    lexer.commenters = ""   # comments were already removed; a surviving `#` is inside a word (a URL fragment, $#)
    try:
        tokens = list(lexer)
    except ValueError:   # unbalanced quotes: let it through
        return None
    segments, current, pending_op = [], [], ""
    for token in tokens:
        if token in _SEPARATORS or set(token) <= set(";&|()\n"):
            if current:
                segments.append((pending_op, current))
                current = []
            pending_op = token
        else:
            current.append(token)
    if current:
        segments.append((pending_op, current))
    return segments


def _segments(command: str) -> list[list[str]] | None:
    result = _segments_with_ops(command)
    return None if result is None else [tokens for _op, tokens in result]


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
        # a switch that turns a wrapper into a string runner (`flock -c`, `env -S`, `watch '<cmd>'`) is read in
        # place from `index + 1`, never by copying the tail per wrapper, which made long wrapper chains quadratic
        if name not in _WRAPPERS or (name in _STRING_WRAPPERS and _command_string(name, tokens, index + 1) is not None):
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


# The command words the guard reasons about. A glob command word (`sys*ctl`, `/usr/bin/sys?ctl`) is matched
# against this small set, so a wildcard cannot hide a sensitive verb the shell would expand to.
_SENSITIVE_VERBS = frozenset({
    "systemctl", "service", "launchctl", "sc", "net", "schtasks", "taskkill", "stop-process", "loginctl",
    "init", "telinit", "kill", "tkill", "busctl", "dbus-send", "gdbus", "qdbus", "hermes", "hermes-agent",
    "find", "dd", "truncate", "tee", "chmod", "chown", "chgrp", "ln", "rsync", "cp", "install",
    "pkill", "killall", "pgrep", "pidof",
}) | _REMOVERS | _POWER | _SERVICE_CMDLETS


def _resolve_glob_verb(verb: str) -> str:
    """A command word with shell glob characters mapped to the sensitive verb it would expand to, if one matches
    (`sys*ctl`, `sys?ctl` -> `systemctl`); `?` is read as `*` so a single missing character still matches, which
    only ever widens the match within this small set. A word without a glob, or one that matches nothing, is left
    as it is."""
    if not any(char in verb for char in "*?["):
        return verb
    if len(re.sub(r"[*?\[\]]", "", verb)) < 2:
        return verb   # a word that is all wildcards (`*`, `?`) would match everything: not evidence of a verb
    pattern = verb.replace("?", "*")
    for candidate in sorted(_SENSITIVE_VERBS):
        if fnmatch.fnmatchcase(candidate, pattern):
            return candidate
    return verb


_SIMPLE_ASSIGNMENT = re.compile(r"([A-Za-z_]\w*)=([^$`\s]*)\Z")   # VAR=value with a literal value (no $, no `, no space)
_VARIABLE_REF = re.compile(r"\$\{(\w+)\}|\$(\w+)")
_ASSIGNMENT_KEYWORDS = frozenset({"export", "declare", "local", "typeset", "readonly"})


def _collect_assignments(tokens: list[str], assigned: dict[str, str]) -> None:
    """Record the simple literal assignments a segment begins with (`X=systemctl`, `export X=~/.hermes`), so a
    later `$X` can be read. Only a value the shell does not compute (no expansion, no command, no spaces) is
    recorded; anything else leaves the name unknown, and an unknown `$X` still lets the command through."""
    index = 1 if tokens and tokens[0] in _ASSIGNMENT_KEYWORDS else 0
    while index < len(tokens):
        match = _SIMPLE_ASSIGNMENT.fullmatch(tokens[index])
        if match is None:
            break
        assigned[match.group(1)] = match.group(2)
        index += 1


def _resolve_vars(tokens: list[str], assigned: dict[str, str]) -> list[str]:
    """The tokens with `$VAR` and `${VAR}` replaced by a value assigned earlier in the same command; an unknown
    variable is left as it is (still dynamic, so it lets the command through)."""
    if not assigned:
        return tokens
    def replace(match):
        name = match.group(1) or match.group(2)
        return assigned[name] if name in assigned else match.group(0)
    return [_VARIABLE_REF.sub(replace, token) for token in tokens]


# Options that take the next word as their value, per program, so that value is not read as the command or a file.
_STRING_VALUES = {
    "shell": frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}),
    "su": frozenset({"-s", "--shell", "-g", "--group", "-G", "--supp-group", "-w", "--whitelist-environment"}),
    "runuser": frozenset({"-u", "--user", "-s", "--shell", "-g", "--group", "-G", "--supp-group", "-w",
                          "--whitelist-environment"}),
    "flock": frozenset({"-w", "--wait", "--timeout", "-E", "--conflict-exit-code"}),
    "script": frozenset({"-T", "--log-timing", "-I", "--log-in", "-O", "--log-out", "-B", "--log-io", "-E", "--echo",
                         "-m", "--logging-format", "-o", "--output-limit"}),
    "powershell": frozenset({"-executionpolicy", "-ep", "-ex", "-workingdirectory", "-wd", "-outputformat", "-of",
                             "-inputformat", "-if", "-windowstyle", "-w", "-configurationname", "-version", "-v",
                             "-psconsolefile", "-settingsfile"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir"}),
    "watch": frozenset({"-n", "--interval"}),
}


def _command_string(verb: str, rest: list[str], start: int = 0) -> str | None:
    """The command a shell (-c, -lc, -ec, ...), su, runuser, script or flock (-c, --command), PowerShell
    (-Command, -c), cmd (/c), env (-S), eval or watch gets as one string, from `rest[start:]`. The options are read
    forward once, never past the first word that is not an option (su and runuser: the user name; flock: the lock
    file), so however many options come first, each word is looked at once and nothing is copied until a command
    string is found."""
    if verb not in _STRING_VERBS:
        return None
    length, index = len(rest), start
    if verb in ("powershell", "pwsh", "cmd"):
        switches = ("/c", "//c", "/k", "//k") if verb == "cmd" else ("-command", "-c", "/command", "/c")
        while index < length:
            token = rest[index].lower()
            if token in switches:
                inner = " ".join(rest[index + 1:]) or None   # everything after the switch is the command
                if inner is not None and _WINDOWS:
                    inner = inner.replace("\\", "/")   # the POSIX shlex that re-lexes it would eat these path separators
                return inner
            if not token.startswith(("-", "/")):
                return None
            index += 2 if token in _STRING_VALUES["powershell"] else 1
        return None
    if verb == "env":   # env -S '<command>' splits one string into the command and its arguments
        while index < length:
            token = rest[index]
            if token in ("-S", "--split-string"):
                return " ".join(rest[index + 1:]) or None
            if token.startswith("--split-string="):
                return " ".join([token.split("=", 1)[1], *rest[index + 1:]]) or None
            if token.startswith("-S") and len(token) > 2:
                return " ".join([token[2:], *rest[index + 1:]])
            if not token.startswith("-") and not _ASSIGNMENT.match(token):
                return None
            index += 2 if token in _STRING_VALUES["env"] else 1
        return None
    if verb == "eval":   # eval '<command>': its words, joined, are the command
        return " ".join(rest[start:]) or None
    if verb == "watch":   # watch [-n S] '<command>': it runs its operands, joined, as a shell string
        while index < length and rest[index].startswith("-"):
            index += 2 if rest[index] in _STRING_VALUES["watch"] else 1
        return " ".join(rest[index:]) or None
    shell, runner = verb in _SHELLS, verb in _STRING_RUNNERS
    values = _STRING_VALUES["shell" if shell else verb]
    positionals = 0
    while index < length:
        token = rest[index]
        if token.startswith("-") or (shell and token.startswith("+") and len(token) > 1):
            if runner and token.startswith("--command="):
                return " ".join([token.split("=", 1)[1], *rest[index + 1:]]) or None
            if shell and re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", token) or runner and token in ("-c", "--command") \
                    or verb == "script" and re.fullmatch(r"-[A-Za-z]*c", token):
                return rest[index + 1] if index + 1 < length else None   # script -qc '<command>' too
            if token == "--":
                return None   # what follows is a command and its words, not one string (runuser -u root -- cmd)
            index += 2 if token in values else 1
            continue
        positionals += 1
        # script reads its options anywhere; su and runuser take a user name, flock a lock file, before -c
        if verb == "script" or (verb in ("su", "runuser", "flock") and positionals == 1):
            index += 1
            continue
        return None   # a script file or a command: not a string handed over
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


def _braces(word: str, limit: int = 64, rounds: int = 16) -> list[str]:
    """The words bash makes of `{a,b}` (`mv x{,.bak}` is `mv x x.bak`): at most `limit` of them, expanded one
    brace group per round for at most `rounds` rounds, so a word packed with groups cannot cost exponentially."""
    found = [word]
    for _ in range(rounds):   # each round expands the first remaining group of every item, then caps the count
        if not any(_BRACE.search(item) for item in found):
            return found
        expanded = []
        for item in found:
            match = _BRACE.search(item)
            if match is None:
                expanded.append(item)
            else:
                for choice in match.group(1).split(","):
                    expanded.append(item[:match.start()] + choice + item[match.end():])
                    if len(expanded) >= limit:
                        break
            if len(expanded) >= limit:
                break
        found = expanded[:limit]
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
        # posixpath.join avoids the `//usr` that `f"{cwd}/{relative}"` builds when cwd is `/` (normpath keeps `//`)
        joined = posixpath.join(str(cwd), relative.lower() if _WINDOWS else relative)
        found = PurePosixPath(posixpath.normpath(joined))
    return found


def _glob_match(path: PurePosixPath, pattern: PurePosixPath) -> bool:
    """Whether the shell's glob `pattern` names `path`: one component at a time, since `*` never matches a `/`."""
    if len(path.parts) != len(pattern.parts):
        return False
    return all(fnmatch.fnmatchcase(part, glob) for part, glob in zip(path.parts, pattern.parts))


def _hits(pattern: PurePosixPath, protected, *, tree: bool = False) -> bool:
    """True when removing `pattern` would remove `protected`: the same path, an ancestor, or a glob of either.
    With `tree`, anything inside `protected` counts too (`rm -rf venv/lib`, `rm -rf venv/*`)."""
    target = _canonical(str(protected))
    if target is None:
        return False
    chain = [target, *target.parents]
    globbed = any(char in str(pattern) for char in "*?[")
    if globbed and any(_glob_match(item, pattern) for item in chain):
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


# Options of pkill/pgrep and killall that take a value: `pkill -u root -f node` selects by `node`, never by `root`.
_PKILL_VALUES = frozenset({"-u", "-U", "-g", "-G", "-P", "-s", "-t", "-F", "--euid", "--uid", "--group", "--pgroup",
                           "--parent", "--session", "--terminal", "--pidfile", "--signal", "--ns", "--nslist",
                           "--older", "--newer", "--env", "--cgroup"})
_KILLALL_VALUES = frozenset({"-u", "--user", "-s", "--signal", "-n", "--ns", "-o", "--older-than", "-y",
                             "--younger-than", "-Z", "--context"})
# Selections that, with no pattern, kill every process of a whole user or group, the gateway included.
_KILL_SELECTORS = frozenset({"-u", "-U", "-g", "-G", "--uid", "--euid", "--group", "--user"})
# Program names Hermes runs under: the launcher script, executed through its shebang, is a process of that name.
_HERMES_PROGRAMS = frozenset({"hermes", "hermes-agent"})


def _kill_patterns(verb: str, rest: list[str]) -> tuple[list[str], list[str]]:
    """(the options, the patterns or names) of `pkill`/`killall`: a signal (-9, -TERM) and the values of options
    that take one are never a pattern."""
    takes_value = _PKILL_VALUES if verb == "pkill" else _KILLALL_VALUES
    options, operands, index = [], [], 0
    while index < len(rest):
        token = rest[index]
        if token == "--":
            operands += rest[index + 1:]
            break
        if token.startswith("-") and token != "-":
            options.append(token)
            index += 2 if token in takes_value else 1
            continue
        operands.append(token)
        index += 1
    return options, operands


def _process_names(process) -> set[str]:
    """The names the gateway's process may go by (/proc/<pid>/comm, 15 characters): the interpreter's, a script
    run through its shebang (`/venv/bin/hermes gateway run`), and the names of Hermes' own launchers."""
    name, command_line = process
    names = {name, *_HERMES_PROGRAMS}
    words = command_line.split()
    for word in words[:2]:
        if not word.startswith("-"):
            names.add(_program(word)[:15])
    return {item for item in names if item}


def _own_process_match(verb: str, rest: list[str], words: list[str], process) -> bool:
    """Whether `pkill`/`killall` would select the gateway itself: its pattern matched against this process's name
    (`pkill python3`, `killall python3`) or, with -f, its whole command line (`pkill -f 'gateway run'`). Without
    the process to look at, a pattern naming Hermes counts."""
    if not process:
        return any("hermes" in word.lower() for word in words)
    _name, command_line = process
    names = _process_names(process)
    full = any(re.fullmatch(r"-[A-Za-z]*f[A-Za-z]*", token) or token == "--full" for token in rest)
    exact = any(re.fullmatch(r"-[A-Za-z]*x[A-Za-z]*", token) or token == "--exact" for token in rest) if verb == "pkill" \
        else any(token in ("-e", "--exact") for token in rest)
    regex = verb == "pkill" or any(token in ("-r", "--regexp") for token in rest)
    targets = [command_line] if full and verb == "pkill" else sorted(names)
    for word in words:
        if not word or _dynamic(word):
            continue
        if not regex:   # killall <name>: the process's name, or its path
            if word in names or (word.startswith("/") and word in command_line.split()[:2]):
                return True
            continue
        if any(_search(word, target, whole=exact) for target in targets):
            return True
    return False


def _stops_gateway(verb: str, rest: list[str], words: list[str], *, mentions: bool = False, process=None) -> bool:
    if verb in _DBUS:   # busctl/dbus-send/gdbus calling systemd's RestartUnit/StopUnit on the gateway
        return any(_DBUS_STOP.search(token) for token in rest) and any("hermes" in token.lower() for token in rest)
    if verb == "kill":   # the agent's shell is a child of Hermes: $PPID is Hermes; -1 is every process of the user
        # a leading `-` makes the target a process group: `-$PPID` is Hermes' group, `-1` every process of the user
        return any(target in ("$PPID", "${PPID}", "-1", "-$PPID", "-${PPID}") for target in _kill_targets(rest))
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
    if verb in ("taskkill", "stop-process"):   # Windows: match "hermes" or the gateway's own process names
        names = _process_names(process) if process else set()
        for token in rest:
            if "hermes" in token.lower():
                return True
            bare = token.lower()
            bare = bare[:-4] if bare.endswith(".exe") else bare
            if bare in names:
                return True
        return False
    if verb in _POWER:   # the machine goes down and the gateway with it; `shutdown -c` cancels, `-k` only warns
        return not any(token.lower() in ("-c", "/a", "-a", "-k", "--help") for token in rest)
    if verb in ("init", "telinit"):
        return bool(words) and words[0] in ("0", "6")
    if verb == "loginctl":   # ending the owner's session or user ends the user services, the gateway among them
        return bool(words) and words[0] in ("terminate-user", "kill-user", "terminate-session", "kill-session")
    if verb == "systemctl" and any(word in _SYSTEMCTL_POWER for word in _without_values(rest, _SYSTEMCTL_VALUES)[:1]):
        return True   # systemctl reboot|poweroff|halt|kexec, and `systemctl --user exit`, which stops every user unit
    if verb == "systemctl":
        words = _without_values(rest, _SYSTEMCTL_VALUES)
        action = words[0] if words else ""
        stops = action in _STOP_ACTIONS or (action in _STOP_WITH_NOW and "--now" in rest) or _dynamic(action)
        # a unit the shell fills in ($U, xargs' words) counts when the command names Hermes somewhere
        return stops and any(_gateway_unit(word) or (mentions and _dynamic(word)) for word in words[1:])
    if verb == "service":
        return len(words) >= 2 and _gateway_unit(words[0]) and words[1] in ("restart", "stop")
    if verb in ("pkill", "killall"):   # its patterns, never an option's value (`pkill -u root -f node`)
        options, operands = _kill_patterns(verb, rest)
        # no pattern, but a selection that sweeps up a whole user or group (`pkill -u u`, `killall -u u`): the gateway too
        if not [word for word in operands if not _dynamic(word)] and \
                any(option.split("=", 1)[0] in _KILL_SELECTORS for option in options):
            return True
        return _own_process_match(verb, rest, operands, process)
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
    found = _find_deletes(rest) if verb == "find" else None
    return found[0] if found is not None and found[1] is None else []


# find tests that select only some of what is under a starting point: with one, the starting point itself is not
# what goes (`find ~/.hermes -name '*.log' -delete`).
_FIND_FILTERS = frozenset({"-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename", "-regex", "-iregex",
                           "-lname", "-ilname", "-mtime", "-mmin", "-atime", "-amin", "-ctime", "-cmin", "-newer",
                           "-anewer", "-cnewer", "-newermt", "-type", "-xtype", "-size", "-empty", "-user", "-group",
                           "-uid", "-gid", "-perm", "-links", "-inum", "-samefile", "-used", "-nouser", "-nogroup",
                           "-mindepth"})
_FIND_PATH_TESTS = frozenset({"-path", "-ipath", "-wholename", "-iwholename", "-regex", "-iregex", "-lname", "-ilname",
                              "-inum", "-samefile"})


def _find_selection(rest: list[str]) -> tuple[list[str], list[tuple[str, bool]] | None]:
    """What a find expression names: (its starting points, None when a whole starting point goes, else the -name /
    -iname globs that select what goes under them, as (glob, ignore case), empty when any name may go)."""
    starts = []
    for token in rest:   # find's starting points come before the first expression token
        if token.startswith(("-", "(", "!")):
            break
        starts.append(token)
    # negation or alternation (`! -name`, `-not`, `-o`, `,`): which names survive is not read here, so all go
    if any(token in ("!", "-not", "-o", "-or", ",") for token in rest):
        return starts, None
    if not any(token in _FIND_FILTERS for token in rest):
        return starts, None
    globs = []
    for index, token in enumerate(rest[:-1]):
        if token in ("-name", "-iname"):
            globs.append((rest[index + 1], token == "-iname"))
        elif token in _FIND_PATH_TESTS:
            return starts, []   # -path, -regex, ...: which names go is not read here, so any may
    return starts, globs


def _find_deletes(rest: list[str]) -> tuple[list[str], list[tuple[str, bool]] | None] | None:
    """For `find ... -delete` or `-exec rm`: the find's selection (see `_find_selection`), or None when the find
    deletes nothing itself."""
    if "-delete" not in rest and not (any(token in ("-exec", "-execdir") for token in rest)
                                      and any(os.path.basename(token) == "rm" for token in rest)):
        return None
    return _find_selection(rest)


def _find_hit(starts: list[str], globs: list[tuple[str, bool]], *, protected, trees=(), home: Path,
              profile: Path | None = None, cwd: PurePosixPath | None = None) -> str | None:
    """A protected path strictly under a filtered find's starting point that the find may delete: one whose name, or
    the name of a folder between it and the starting point, matches a -name glob (any, without one)."""
    for token in starts:
        start = _expand(token, home, profile, cwd)
        if start is None:
            continue
        globbed = any(char in str(start) for char in "*?[")
        for item in (*protected, *trees):
            target = _canonical(str(item))
            if target is None:
                continue
            below = [parent for parent in target.parents
                     if (_glob_match(parent, start) if globbed else parent == start)]
            if not below:
                continue
            names = target.parts[len(below[0].parts):]
            if not globs or any(fnmatch.fnmatchcase(name.lower() if fold else name, glob.lower() if fold else glob)
                                for name in names for glob, fold in globs):
                return str(item)
    return None


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


# A Python/Node/Ruby/Perl one-liner that actually writes a file: `open(..., 'w'/'a')`, `write_text`, `.write(`,
# or `dump(obj, file)` (a comma after the opening paren) — not a bare `dump(...)` to a string.
_INTERPRETER_WRITE = re.compile(r"""open\([^)]*,\s*['"][wax]\+?b?['"]|write_text|\.write\(|dump\([^)]*,""")


# How a path is changed: "replace" (cp/mv/install over it), "edit" (sed -i and other in-place editors), "append"
# (`>>`, `tee -a`), "redirect" (`>`), "tee", "truncate", "dd" (of=), "attr" (chmod/chown/chgrp), "link" (ln), "rsync"
# and "rsync_delete". Only an "above" change reaches what lies under the folder it names (chmod -R, a symlink
# over a folder, rsync --delete); the rest change one file, so a plain copy into `~` or `/usr/local/lib` is fine.
_ABOVE_KINDS = frozenset({"attr", "link", "rsync_delete"})
_DESTRUCTIVE_KINDS = frozenset({"attr", "link", "truncate", "dd", "tee", "redirect", "rsync_delete"})
_CLOBBER_KINDS = frozenset({"attr", "link", "truncate", "dd", "tee", "redirect", "replace"})   # a config.yaml's whole content
_CHMOD_DASH_MODE = re.compile(r"-[rwxXst]+")   # `chmod -w file`: a mode the option scan would take for an option
# An interpreter one-liner that writes a file, and the forms of it that replace the whole content.
_INLINE_WRITE = re.compile(r"""open\([^)]*,\s*['"][wax]\+?b?['"]|write_text|write_bytes|\.write\(|dump\([^)]*,|writeFile""")
_INLINE_REPLACE = re.compile(r"""open\([^)]*,\s*['"][wx]\+?b?['"]|write_text|write_bytes|writeFileSync\(|writeFile\(""")


def _segment_mutations(tokens: list[str]) -> list[tuple[str, str]]:
    """The paths one segment (after its prefix) writes to or changes, each with how (see the kinds above): an
    in-place editor (`sed -i`, `perl -i`, `yq -i`), `tee`, `truncate`, `dd of=`, `chmod`/`chown`/`chgrp`, and the
    destination of `cp`/`mv`/`install`/`ln`/`rsync` (its `-t` value or last operand). A source only read is not one."""
    if not tokens:
        return []
    verb, rest = _program(tokens[0]), tokens[1:]
    family = re.sub(r"[\d.]+\Z", "", verb)
    operands = [token for token in rest if not token.startswith("-")]
    if family in ("sed", "perl", "yq", "ruby") and any(re.fullmatch(r"-[A-Za-z]*i\S*|--in-place\S*", token)
                                                      for token in rest):
        return [(token, "edit") for token in operands]   # in place: each file operand is rewritten
    if verb in ("chmod", "chown", "chgrp"):
        whole = any(token.startswith("--reference") for token in rest) or \
            (verb == "chmod" and any(_CHMOD_DASH_MODE.fullmatch(token) for token in rest))
        return [(token, "attr") for token in (operands if whole else operands[1:])]   # else operand 0 is the mode/owner
    if verb == "truncate":
        return [(token, "truncate") for token in _without_values(rest, {"-s", "--size", "-r", "--reference"})]
    if verb == "tee":
        appends = any(token == "--append" or re.fullmatch(r"-[A-Za-z]*a[A-Za-z]*", token) for token in rest)
        return [(token, "append" if appends else "tee") for token in operands]
    if verb == "dd":
        return [(token.split("=", 1)[1], "dd") for token in rest if token.startswith("of=")]
    if family in ("python", "node", "ruby", "perl") and _INLINE_WRITE.search(" ".join(rest)):
        # a one-liner that opens a config.yaml it names for writing: `open('~/.hermes/config.yaml', 'w')`
        code = " ".join(rest)
        kind = "replace" if _INLINE_REPLACE.search(code) else "edit"
        return [(part, kind) for token in rest for part in re.split(r"[\s'\"=,;()\[\]]", token)
                if part.lower().endswith(_CONFIG_FILES)]
    if verb in ("cp", "mv", "install", "ln", "rsync"):
        if verb == "ln":
            kind = "link"
        elif verb == "rsync":   # rsync --delete empties the destination of what the source lacks
            kind = "rsync_delete" if any(token == "--del" or token.startswith("--delete") for token in rest) else "rsync"
        else:
            kind = "replace"
        destination = None
        for index, token in enumerate(rest):
            if token in ("-t", "--target-directory") and index + 1 < len(rest):
                destination = rest[index + 1]
            elif token.startswith("--target-directory="):
                destination = token.split("=", 1)[1]
        if destination is None and len(operands) >= 2:
            destination = operands[-1]   # the destination is the last operand
        elif destination is None and verb == "ln" and operands:   # `ln -s TARGET`: a link named like it, right here
            destination = re.split(r"[\\/]", operands[0].rstrip("/\\"))[-1] or None
        return [(destination, kind)] if destination else []
    return []


def _mutating_targets(command: str, *, home: Path, profile: Path | None,
                      cwd: PurePosixPath | None) -> list[tuple[str, str]]:
    """Every file path a command writes to or changes, each with how, read from the start of the command (the
    per-segment check in `_check_command` follows `cd` and variables): its redirects and `_segment_mutations`."""
    prepared = _prepare(command)
    seg_ops = _lex(prepared) or []
    redirects = _segment_redirects(prepared, seg_ops)
    targets = [] if redirects is not None else _visible_redirects(prepared)
    for index, (_op, segment) in enumerate(seg_ops):
        if redirects is not None:
            targets += redirects[index]
        targets += _segment_mutations(_strip_prefix(segment))
    return targets


def _visible_redirects(prepared: str) -> list[tuple[str, str]]:
    """Redirect targets read from the text when the marked lexing cannot be lined up: outside single quotes only."""
    visible = _without_single_quotes(prepared)
    return [(path.replace('"', ""), "append" if len(op) == 2 else "redirect")
            for op, path in re.findall(r"(>>?)\s*([^\s;&|<>]+)", visible)]


def _write_targets(command: str, *, home: Path, profile: Path | None, cwd: PurePosixPath | None) -> list[str]:
    """The file paths a command writes to (just the paths; see `_mutating_targets` for how each is changed)."""
    return [token for token, _kind in _mutating_targets(command, home=home, profile=profile, cwd=cwd)]


def _at_or_inside(pattern: PurePosixPath, folder) -> bool:
    """Whether `pattern` (a path or a shell glob) names `folder` itself or something inside it; never a folder above."""
    base = _canonical(str(folder))
    if base is None:
        return False
    if not any(char in str(pattern) for char in "*?["):
        return pattern == base or base in pattern.parents
    if _glob_match(base, pattern):
        return True
    parts = []
    for part in pattern.parts:   # the part before the first glob is the folder the pattern reaches into
        if any(char in part for char in "*?["):
            break
        parts.append(part)
    prefix = PurePosixPath(*parts) if parts else None
    return prefix is not None and (prefix == base or base in prefix.parents)


def _is_hermes_config(target: PurePosixPath, *, home: Path, profile: Path | None, protected) -> bool:
    """A config.yaml Hermes reads: the profile's, the root's (`~/.hermes` or a protected Hermes folder), or another
    profile's under `<root>/profiles/<name>/`. A config.yaml anywhere else (a website, a project) is not one."""
    if target.name.lower() not in _CONFIG_FILES:
        return False
    places = {_canonical(str(item)) for item in (profile, Path(home) / ".hermes", *protected) if item is not None}
    parent = target.parent
    return parent in places or (parent.parent.name == "profiles" and parent.parent.parent in places)


def _mutation_verdict(targets, *, protected, trees, own, home: Path, profile: Path | None,
                      cwd: PurePosixPath | None, plugin_words: bool) -> dict | None:
    """Whether writing or changing `targets` ((word, kind) pairs, read from `cwd`) takes Hermes down, whatever the
    verb. The protected **code trees** (the interpreter, the environment, Hermes' packages) are protected at their
    root and inside, and against a chmod, a symlink or an `rsync --delete` from a folder above. This plugin's folder
    is protected at its root and inside (`plugin_settings`). The profile home and the Hermes root themselves (and the
    folders above them) are protected only against a destructive change; a plain write inside the profile home (a
    skill, a memory, a workspace file) or a copy into `~` is the agent's to make. Replacing a Hermes config.yaml
    wholesale wipes the plugin's settings; editing or appending to one counts when the command names them."""
    for token, kind in targets:
        target = _expand(token, home, profile, cwd)
        if target is None:
            continue
        for tree in trees:
            if _at_or_inside(target, tree) or (kind in _ABOVE_KINDS and _hits(target, tree)):
                return {"kind": "runtime_removal", "path": str(tree), "text": "runtime_change"}
        if any(_at_or_inside(target, folder) for folder in own):
            return {"kind": "plugin_settings"}
        if _is_hermes_config(target, home=home, profile=profile, protected=protected) and \
                (kind in _CLOBBER_KINDS or plugin_words):
            return {"kind": "plugin_settings"}
        if kind in _DESTRUCTIVE_KINDS:
            for item in protected:   # the profile home / Hermes root themselves, or a folder above them
                if _hits(target, item):
                    return {"kind": "runtime_removal", "path": str(item), "text": "runtime_change"}
    return None


def _writes_plugin_config(command: str, *, home: Path, profile: Path | None, cwd: PurePosixPath | None) -> bool:
    """A command that writes a Hermes config.yaml (`sed -i`, `yq -i`, a redirect, `cp`/`mv` into it, a Python
    one-liner that opens it for writing or dumps into a file) and names the plugins section, this plugin or one
    of its trust settings: it can switch the plugin off or widen whom it trusts, which `hermes config set` of
    those keys is refused for. Reading or copying it elsewhere (`cp config.yaml /tmp/x`) is not a write."""
    lowered = command.lower()
    if not any(name in lowered for name in _CONFIG_FILES) or not _PLUGIN_WORDS.search(command):
        return False
    where = {"home": home, "profile": profile, "cwd": cwd}
    for target in _write_targets(command, **where):
        if _hermes_config(target, **where):
            return True
    for segment in _segments(command) or ():   # an interpreter one-liner that writes a config.yaml it names
        tokens = _strip_prefix(segment)
        if not tokens:
            continue
        verb, rest = _program(tokens[0]), tokens[1:]
        family = re.sub(r"[\d.]+\Z", "", verb)
        if family in ("python", "node", "ruby", "perl") and any(_hermes_config(token, **where) for token in rest) \
                and _INTERPRETER_WRITE.search(" ".join(rest)):
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


# One value-option set per tool: a grep flag such as `-F`/`-v` is not a value option, so it must not swallow the
# pattern the way pgrep's `-u`/awk's `-F` would (mixing them let `grep -F hermes` lose its pattern).
_GREP_VALUES = frozenset({"-e", "-f", "-m", "-A", "-B", "-C", "-d", "-D", "--regexp", "--file", "--max-count"})
_AWK_VALUES = frozenset({"-v", "-f", "-F", "--assign", "--field-separator"})


def _lookup_value_options(verb: str) -> frozenset:
    if verb == "awk":
        return _AWK_VALUES
    if verb == "pgrep":
        return _PKILL_VALUES   # pgrep selects by the same options as pkill
    return _GREP_VALUES        # grep, egrep, fgrep, rg


def _operands(rest: list[str], verb: str) -> tuple[list[str], list[str], str | None]:
    """(the options, the operands, an -e/--regexp pattern) of a lookup command; an option's value is never an operand."""
    takes_value = _lookup_value_options(verb)
    options, operands, pattern = [], [], None
    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "--":
            operands += rest[index + 1:]
            break
        if token.startswith("-") and token != "-":
            options.append(token)
            name = token.split("=", 1)[0]
            if name in takes_value and "=" not in token:
                if name in ("-e", "--regexp") and index + 1 < len(rest):
                    pattern = rest[index + 1]
                index += 2
                continue
            index += 1
            continue
        operands.append(token)
        index += 1
    return options, operands, pattern


_PATTERN_LIMIT = 200   # longer agent-written patterns are not run as regular expressions


def _risky_pattern(pattern: str) -> bool:
    """A pattern Python's backtracking engine may take exponential time on: too long, or a group that holds a
    quantifier or an alternation and is itself repeated (`(a+)+`, `(\\w*)*`, `(a|aa)+`)."""
    if len(pattern) > _PATTERN_LIMIT:
        return True
    stack, index = [], 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            index += 2
            if stack:
                stack[-1] = stack[-1] or (index < len(pattern) and pattern[index] in "*+?{")
            continue
        if char == "[":   # a character class: its insides are not quantifiers
            end = index + 1
            if end < len(pattern) and pattern[end] == "^":
                end += 1
            if end < len(pattern) and pattern[end] == "]":
                end += 1
            end = pattern.find("]", end)
            index = len(pattern) if end < 0 else end + 1
            continue
        if char == "(":
            stack.append(False)
        elif char == ")" and stack:
            inner = stack.pop()
            repeated = index + 1 < len(pattern) and pattern[index + 1] in "*+{"
            if inner and repeated:
                return True
            if stack:
                stack[-1] = stack[-1] or inner or repeated
        elif char in "*+{|" or (char == "?" and index > 0 and pattern[index - 1] not in "(*+?}"):
            if stack:
                stack[-1] = True
        index += 1
    return False


def _search(pattern: str, text: str, *, whole: bool = False) -> bool:
    """`re.search` (or fullmatch) of an agent-written pattern, bounded: a risky or invalid pattern is matched as the
    literal text it names, its metacharacters left out."""
    if not _risky_pattern(pattern):
        try:
            return bool((re.fullmatch if whole else re.search)(pattern, text))
        except re.error:
            pass
    literal = re.sub(r"[\\^$.|?*+()\[\]{}]", "", pattern)
    return bool(literal) and (literal == text if whole else literal in text)


def _lookup_patterns(rest: list[str], verb: str) -> list[str]:
    """What a lookup command searches for: its operands and its -e pattern, never another option's value."""
    _options, operands, pattern = _operands(rest, verb)
    return [*operands, pattern] if pattern is not None else operands


def _matches(pattern: str, text: str, *, literal: bool = False) -> bool:
    if not pattern or _dynamic(pattern):
        return False
    if literal:
        return pattern in text
    if _search(pattern, ""):
        return False   # a pattern that matches anything is not a lookup of the gateway
    return _search(pattern, text)


def _looks_up_gateway(verb: str, rest: list[str], process) -> bool:
    """Whether a pid lookup selects the gateway itself: `pgrep -f 'gateway run'` (against its command line; without -f
    against its name), `pidof python3`, or a `grep`/`rg`/`awk '/pattern/{print $2}'` reading a process list from a pipe
    (one that reads files looks at those files, not at processes)."""
    if not process:
        return False
    name, command_line = process
    options, operands, pattern = _operands(rest, verb)
    if verb == "pidof":
        return any(token in _process_names(process) for token in operands)
    if verb == "pgrep":
        full = any(re.fullmatch(r"-[A-Za-z]*f[A-Za-z]*", token) or token == "--full" for token in options)
        exact = any(re.fullmatch(r"-[A-Za-z]*x[A-Za-z]*", token) or token == "--exact" for token in options)
        targets = [command_line] if full else sorted(_process_names(process))
        return bool(operands) and any(operands[0] == target if exact else _matches(operands[0], target)
                                      for target in targets)
    if verb == "awk":
        if any(token.startswith("-f") or token == "--file" for token in options) or len(operands) != 1:
            return False   # a program from a file, or input files: not a process list from a pipe
        return any(_matches(found, command_line) for found in re.findall(r"/((?:[^/\\]|\\.){2,200})/", operands[0]))
    # grep and friends: the pattern, and no file to read
    if pattern is None:
        if len(operands) != 1:
            return False
        pattern = operands[0]
    elif operands:
        return False
    literal = verb == "fgrep" or any(token in ("-F", "--fixed-strings") for token in options)
    return _matches(pattern, command_line, literal=literal)


def _kills_by_lookup(command: str, process=None, segments=None) -> bool:
    """`kill $(pgrep -f hermes)`, `pgrep -f hermes_cli | xargs kill`, ``kill `pidof hermes` ``, `kill $(cat
    ~/.hermes/gateway.pid)`: a kill whose process ids the shell fills in, in a command that looks up Hermes or its
    pid file."""
    kills = False
    for segment in (segments if segments is not None else _segments(command) or ()):
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
        if verb in _PID_LOOKUPS and (_looks_up_gateway(verb, rest, process) if process
                                     else any("hermes" in token.lower() for token in _lookup_patterns(rest, verb))):
            looked_up = True   # matched against the gateway's own command line; without it, a pattern naming Hermes
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
    if not isinstance(command, str) or not command.strip():
        return None
    if len(command) > _COMMAND_LIMIT:
        # The limit is on the real command, not on padding: a trailing comment or a huge here-document body cannot
        # disable the guard. Cut both first, bound the work, and scan at most the first _COMMAND_LIMIT characters.
        command = _strip_comments(_strip_heredocs(command[:_SCAN_LIMIT]))
        if len(command) > _COMMAND_LIMIT:
            command = command[:_COMMAND_LIMIT]
    workdir = args.get("workdir") if tool_name in TERMINAL_TOOLS else None
    cwd = _expand(workdir, home, profile) if isinstance(workdir, str) and workdir.strip() else None
    return _check_command(command, protected=protected, home=home, profile=profile, depth=0, trees=trees, own=own,
                          process=process, cwd=cwd, budget=[_MAX_SUBCHECKS, {}])


def _stdin_payloads(rest: list[str], seg_op: str, prior, body_map: dict) -> list[str]:
    """The code a shell reading from stdin runs: the bodies of its here-documents (`bash <<EOF`), its `<<<`
    here-string word, and the arguments of an `echo`/`printf` piped into it."""
    payloads, skip = [], False
    for position, token in enumerate(rest):
        if skip:
            skip = False
        elif token == "<<<":   # the next token is the here-string, the shell's whole stdin
            if position + 1 < len(rest):
                payloads.append(rest[position + 1])
            skip = True
        elif token.startswith("<<<"):   # `<<<word` with no space: the word after it is the stdin
            payloads.append(token[3:])
        elif token.startswith("<<"):
            payloads.extend(body_map.get(token[2:], []))   # quoted bodies too: the shell still runs them
    if seg_op in ("|", "|&") and prior is not None:
        prior_tokens = _strip_prefix(prior)
        if prior_tokens and _program(prior_tokens[0]) in ("echo", "printf"):
            payloads.append(" ".join(token for token in prior_tokens[1:] if not token.startswith("-")))
        # a producer that reads a here-document and writes it out pipes it to the shell: `cat <<EOF | bash`
        for token in prior_tokens:
            if token.startswith("<<") and not token.startswith("<<<"):
                payloads.extend(body_map.get(token[2:], []))
    return payloads


def _pipeline_contribution(verb: str, rest: list[str], fed: list[str], finds: list) -> None:
    """Add what a pipeline segment produces on stdout to the words a later `xargs rm` would delete: a `find` keeps
    its selection for a filtered read, any other producer its plain operands. A `cd` produces nothing. The lists
    are capped, since a pipeline longer than that is not a tidy removal the guard needs to follow word by word."""
    if verb in ("cd", "pushd", "chdir", "set-location", "sl"):
        return
    if verb == "find":
        starts, globs = _find_selection(rest)
        if globs is None:
            if len(fed) < _MAX_FED:
                fed += [token for token in starts if not _dynamic(token)][:_MAX_FED - len(fed)]
        elif len(finds) < _MAX_FINDS:
            finds.append((starts, globs))
        return
    if len(fed) < _MAX_FED:   # a word the shell fills in cannot be resolved here, so it is not carried
        fed += [token for token in rest if not token.startswith("-") and not _dynamic(token)][:_MAX_FED - len(fed)]


def _xargs_check(fed, finds, own_paths, *, protected, trees, own, home: Path, profile, cwd) -> dict | None:
    """`... | xargs rm`: the paths come only from what the earlier commands of the same pipeline produced, already
    gathered once as `fed` (plain operands) and `finds` (filtered `find` producers, read like `find ... -delete`).
    `own_paths` are the fixed paths the `xargs rm` itself carries before xargs appends its input."""
    where = {"home": home, "profile": profile, "cwd": cwd}
    for starts, globs in finds:
        hit = _find_hit(starts, globs, protected=protected, trees=trees, **where)
        if hit:
            return {"kind": "runtime_removal", "path": hit}
        if own and _find_hit(starts, globs, protected=own, **where):
            return {"kind": "plugin_settings"}
    paths = [*own_paths, *fed]
    path = _hit(paths, protected=protected, trees=trees, **where)
    if path:
        return {"kind": "runtime_removal", "path": path}
    if own and _hit(paths, protected=(), trees=own, **where):
        return {"kind": "plugin_settings"}
    return None


def _check_command(command: str, *, protected, home: Path, profile: Path | None = None, depth: int = 0, trees=(), own=(),
                   process=None, cwd: PurePosixPath | None = None, budget: list | None = None) -> dict | None:
    if budget is None:
        budget = [_MAX_SUBCHECKS, {}]   # the sub-checks left, and the (command, folder) already found harmless
    seen = budget[1].get((command, cwd))
    if seen is not None and seen <= depth:
        return None   # the same text from the same folder, read at least this deep already: it vetoed nothing
    if budget[0] <= 0:   # a command built to spawn countless sub-commands is let through once the work is bounded
        return None
    budget[0] -= 1
    if len(budget[1]) < _MAX_SUBCHECKS:
        budget[1][(command, cwd)] = depth   # recorded before the check; a veto ends the whole check anyway
    options = {"protected": protected, "home": home, "profile": profile, "trees": trees, "own": own,
               "process": process, "budget": budget}
    prepared = _prepare(command)
    seg_ops = _lex(prepared) or []
    if _kills_by_lookup(command, process, [tokens for _op, tokens in seg_ops]):
        return {"kind": "gateway_stop"}
    if _writes_plugin_config(command, home=home, profile=profile, cwd=cwd) or _calls_panel_decisions(command):
        return {"kind": "plugin_settings"}
    mentions = "hermes" in command.lower()
    # Any write, truncation or attribute change to what the running Hermes needs, whatever the verb, is judged per
    # segment below with that segment's folder (`cd`) and variables; its redirects are read here, quote-aware.
    judged = {"protected": protected, "trees": trees, "own": own, "home": home, "profile": profile,
             "plugin_words": bool(_PLUGIN_WORDS.search(command))}
    redirects = _segment_redirects(prepared, seg_ops) if seg_ops else []
    if redirects is None:   # the marked lexing did not line up: read the redirects from the text, from here
        found = _mutation_verdict(_visible_redirects(prepared), cwd=cwd, **judged)
        if found:
            return found
    deeper = depth < _MAX_DEPTH   # bash -c "bash -c '...'": commands inside commands, a few levels deep
    body_map = _heredoc_body_map(command) if deeper else {}
    if deeper:   # $(...) and `...` run too, also inside double quotes; never inside single quotes
        for inner_command in _substitutions(command):
            found = _check_command(inner_command, depth=depth + 1, cwd=cwd, **options)
            if found:
                return found
    assigned: dict[str, str] = {}   # simple literal assignments earlier in the command, for `$X` after them
    pipeline_fed, pipeline_finds, prior = [], [], None   # what earlier pipeline commands produce (for `| xargs`, `| sh`)
    checked = [0, 0]   # how much of pipeline_fed / pipeline_finds an earlier `xargs rm` already found harmless
    for position, (seg_op, segment) in enumerate(seg_ops):
        if seg_op not in ("|", "|&"):
            pipeline_fed, pipeline_finds, checked = [], [], [0, 0]   # a separator other than `|` starts a new pipeline
        _collect_assignments(segment, assigned)
        segment = _resolve_vars(segment, assigned)
        prior, prior_source = (prior if seg_op in ("|", "|&") else None), segment
        tokens = _strip_prefix(segment)
        written = [(_resolve_vars([word], assigned)[0], kind) for word, kind in redirects[position]] if redirects else []
        found = _mutation_verdict([*written, *_segment_mutations(tokens)], cwd=cwd, **judged)
        if found:   # judged before a `cd` here moves the folder: its own redirect is opened from the current one
            return found
        if not tokens:
            prior = prior_source
            continue
        verb, rest = _as_hermes(_resolve_glob_verb(_program(tokens[0])), tokens[1:])
        nested_command = _nested_command(verb, rest) if deeper else None
        if nested_command is not None:   # find -exec ..., ssh localhost ..., the strings of python -c / perl -e
            for item in nested_command:
                found = _check_command(item, depth=depth + 1, cwd=cwd, **options)
                if found:
                    return found
        if verb in ("cd", "pushd", "chdir", "set-location", "sl"):   # later relative paths start there
            target = [token for token in rest if not token.startswith("-")]
            cwd = _expand(target[0], home, profile, cwd) if target else _canonical(str(home)) if not rest else None
            prior = prior_source
            continue
        inner = _command_string(verb, rest)
        if inner is not None:   # bash -lc '<command>', su -c '<command>', pwsh -c '<command>', cmd /c, watch '<cmd>'
            found = _check_command(inner, depth=depth + 1, cwd=cwd, **options) if deeper else None
            if found:
                return found
            prior = prior_source
            continue
        if deeper and verb in _SHELLS:   # a shell reading its script on stdin, not from -c or a script file
            real_operands, skip = [], False
            for token in rest:
                if skip:
                    skip = False
                elif token == "<<<":
                    skip = True   # the here-string word is stdin, not a script operand
                elif not token.startswith("-") and not token.startswith("<<"):
                    real_operands.append(token)
            if not real_operands:
                for payload in _stdin_payloads(rest, seg_op, prior, body_map):
                    found = _check_command(payload, depth=depth + 1, cwd=cwd, **options)
                    if found:
                        return found
        prior = prior_source
        words = [token for token in rest if not token.startswith("-")]
        if _stops_gateway(verb, rest, words, mentions=mentions, process=process):
            return {"kind": "gateway_stop"}
        if _changes_plugin(verb, rest):
            return {"kind": "plugin_settings"}
        deleted = _deleted_profile(verb, rest)
        if deleted is not None:
            return {"kind": "runtime_removal", "path": f"`{deleted}`"}
        removed = _removed_paths(verb, rest, words)
        if XARGS_WORDS in removed:   # `... | xargs rm`: the paths come from the earlier commands of the pipeline
            # only what the pipeline produced since the last `xargs rm` is new: a long chain stays linear
            found = _xargs_check(pipeline_fed[checked[0]:], pipeline_finds[checked[1]:],
                                 [item for item in removed if item != XARGS_WORDS],
                                 protected=protected, trees=trees, own=own, home=home, profile=profile, cwd=cwd)
            checked = [len(pipeline_fed), len(pipeline_finds)]
            if found:
                return found
        else:
            path = _hit(removed, protected=protected, home=home, profile=profile, trees=trees, cwd=cwd)
            if path:
                return {"kind": "runtime_removal", "path": path}
            if own and _hit(removed, protected=(), home=home, profile=profile, trees=own, cwd=cwd):
                return {"kind": "plugin_settings"}
        filtered = _find_deletes(rest) if verb == "find" else None
        if filtered is not None and filtered[1] is not None:   # find <dir> -name X -delete: only what X selects goes
            find_where = {"home": home, "profile": profile, "cwd": cwd}   # never `judged`: that one has no cwd
            path = _find_hit(filtered[0], filtered[1], protected=protected, trees=trees, **find_where)
            if path:
                return {"kind": "runtime_removal", "path": path}
            if own and _find_hit(filtered[0], filtered[1], protected=own, **find_where):
                return {"kind": "plugin_settings"}
        # what this segment produces on stdout, carried once for a later `| xargs rm` (gathered, never re-stripped)
        _pipeline_contribution(verb, rest, pipeline_fed, pipeline_finds)
    return None


class RuntimeGuard:
    """The `pre_tool_call` callback. Returns Hermes' block directive or None, and never raises."""

    def __init__(self, *, protected, home: Path, lang: str = "en", on_block=None, profile: Path | None = None,
                 trees=(), own=(), process=None, on_error=None):
        self.protected = tuple(protected)
        self.trees, self.own, self.process = tuple(trees), tuple(own), process
        self.home = Path(home)
        self.profile = Path(profile) if profile is not None else None
        self.lang = lang if lang in MESSAGES else "en"
        self.on_block = on_block
        self.on_error = on_error
        self.errors = 0   # internal guard failures the caller can read; the guard still fails open on each

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
            # the counted kind stays one of the three; a change to the runtime has a sentence of its own
            text = MESSAGES[self.lang][found.get("text", found["kind"])]
            return {"action": "block", "message": text.format(path=found.get("path", ""))}
        except Exception as error:   # Hermes blocks a tool whose pre_tool_call raises: any doubt lets the command through
            # Still fail open, but do not swallow the failure silently: record it so the caller can see the guard
            # is erroring (a counter it reads, or an on_error callback), instead of a guard that quietly does nothing.
            self.errors += 1
            if self.on_error is not None:
                try:
                    self.on_error(error)
                except Exception:   # recording must never decide whether the command runs, nor raise into the turn
                    pass
            return None
