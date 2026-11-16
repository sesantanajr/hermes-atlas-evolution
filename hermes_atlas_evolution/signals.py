"""Experience signals from Hermes hooks: tool failures, turn outcomes, owner feedback and skill use.

Text supplied by Hermes (error messages, tool results, user messages) is classified in memory and then
discarded. Only the class, an identifier-like hint and counters are stored.
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
import threading
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from .experience import day_offset, label, record, utc_day

SURFACES = ("local", "gateway", "cron", "subagent")
_LOCAL_PLATFORMS = frozenset({"cli", "tui", "desktop", "gui", "acp", "local", "oneshot"})
# Work nobody reads as it runs: scheduled jobs, kanban workers, the Curator's background fork. Counted as "cron".
_AUTOMATED_SOURCES = frozenset({"cron", "kanban", "curator", "batch"})
# Hermes' synthetic user turns: /goal continuations. Never a person correcting or thanking anyone.
_SYNTHETIC = re.compile(r"\s*\[Continuing toward your standing goal")
LATENCY_BUCKETS = ((15_000, "b15"), (30_000, "b30"), (60_000, "b60"), (120_000, "b120"), (300_000, "b300"))
DEFAULT_SLOW_TURN_MS = 60_000
_RESULT_LIMIT = 262_144
_HINT = re.compile(r"[A-Za-z_][A-Za-z0-9_.+-]{0,47}\Z")
_SKILL_ACTIONS = frozenset({"loaded", "patched", "edited", "created", "installed", "archived", "stale", "restored"})

# Ordered: the first matching rule names the class. Each rule may capture an identifier-like hint.
_RULES = (
    ("missing_argument", re.compile(
        r"missing required argument(?:\(s\)|s)?:[ \t]*`?'?(?P<hint>[A-Za-z_][\w.-]{0,47})"
        r"|requires an? '(?P<hint2>[A-Za-z_][\w.-]{0,47})'"
        r"|the following arguments are required:[ \t]*-{0,2}(?P<hint3>[A-Za-z_][\w-]{0,47})", re.I)),
    ("missing_argument", re.compile(r"missing required argument", re.I)),   # the argument was not named
    # Hermes' approval gate: the user denied, nobody answered, no approver can be reached, or the context
    # cannot approve. Hermes tells the agent not to reach the same outcome another way; no lesson may differ.
    ("approval_refused", re.compile(
        r"User denied|has NOT consented|Silence is not consent|timed out without user response|"
        r"Failed to send (?:\w+ )?approval request|set approvals\.\w+: approve|nobody (?:can|could) approve", re.I)),
    ("locked_resource", re.compile(r"is locked by the user|locked by (?:the )?operator", re.I)),
    ("review_precondition", re.compile(r"Refusing background curator patch|has not been loaded in this review", re.I)),
    ("edit_precondition", re.compile(
        r"Refusing to overwrite|has not seen its|must (?:be )?(?:read|view(?:ed)?)\b.{0,40}\bbefore", re.I)),
    ("edit_mismatch", re.compile(
        r"Could not find a match for old_string|Found \d+ matches for old_string|No edit was applied", re.I)),
    ("blocked_by_policy", re.compile(r"\bBLOCKED:|flagged as dangerous|blocked by (?:policy|security)|not permitted by policy", re.I)),
    ("rate_limited", re.compile(r"rate.?limit|too many requests|\b429\b|quota exceeded", re.I)),
    ("timeout", re.compile(r"timed out|\btimeout\b|deadline exceeded", re.I)),
    ("missing_dependency", re.compile(
        r"(?P<hint>[A-Za-z_][\w.+-]{0,40}): command not found"
        r"|No module named '?(?P<hint2>[A-Za-z_][\w.]{0,47})"
        r"|Unable to locate package (?P<hint3>[A-Za-z_0-9][\w.+-]{0,47})"
        r"|is not recognized as an internal or external command", re.I)),
    ("permission_denied", re.compile(r"permission denied|\bEACCES\b|operation not permitted", re.I)),
    ("auth", re.compile(r"\b401\b|\bunauthorized\b|invalid api key|authentication failed|\b403\b|\bforbidden\b", re.I)),
    ("backend_unavailable", re.compile(
        r"not initialized|backend\b.{0,40}\bunavailable|connection refused|\bECONNREFUSED\b|is not running|not reachable", re.I)),
    ("not_found", re.compile(r"\bnot found\b|no such file|does not exist|\bENOENT\b|unknown (?:tool|skill|command)", re.I)),
    ("limit_exceeded", re.compile(r"\bis \d+ chars\b|\bexceeds\b|too (?:long|large)|limit of \d+|\bmaximum\b.{0,30}\b(?:size|length|chars)", re.I)),
    ("invalid_argument", re.compile(
        r"^usage: |unrecognized arguments|Unsupported\b.{0,40}\boptions|invalid choice|unexpected keyword"
        r"|invalid (?:argument|parameter|value|skill name)|must be (?:one of|an? )", re.I | re.M)),
    ("code_exception", re.compile(r"Traceback \(most recent call last\)|\bSyntaxError\b", re.I)),
    ("network", re.compile(r"connection reset|network is unreachable|name or service not known|\bSSL\b.{0,20}\berror", re.I)),
)
_EXCEPTION = re.compile(r"^(?P<hint>[A-Za-z_][A-Za-z0-9_.]{2,47}(?:Error|Exception))\b", re.M)
_TARGET_TOOL = re.compile(r"tool_call to '(?P<tool>[A-Za-z0-9_][\w.:-]{0,95})'")
# plugin_fault: Hermes blocked the call because another plugin's pre_tool_call hung or raised (fail-closed).
# rewritten_command: the call failed after another plugin rewrote the command the agent wrote.
# Neither is the agent's doing, so neither ever becomes a lesson.
CLASSES = tuple(dict.fromkeys(name for name, _ in _RULES)) + ("command_failed", "other", "plugin_fault", "rewritten_command")
_PLUGIN_FAULT = re.compile(r"^\s*pre_tool_call plugin callback\b", re.M)


def reclassify(failure: dict, klass: str) -> dict:
    """The same failure under another class, with its signature recomputed."""
    signature = hashlib.sha256("\0".join((failure["tool"], klass, failure["hint"])).encode()).hexdigest()[:20]
    return {**failure, "klass": klass, "signature": signature}

_CORRECTION = re.compile(
    r"^\W{0,3}(?:n[ãa]o[,.!]|n[ãa]o era isso|n[ãa]o foi isso|n[ãa]o [ée] isso|n[ãa]o funcionou|errado|incorreto|"
    r"est[áa] errado|ainda (?:est[áa] )?errado|de novo[,.!]|"
    r"novamente\b|refa[çc]a|corrija|voc[êe] (?:errou|n[ãa]o entendeu)|no[,.!] |nope\b|wrong\b|that'?s (?:not|wrong)|"
    r"not what i|incorrect\b|try again|redo\b|you (?:missed|misunderstood)|still (?:wrong|not|broken))", re.I)
_POSITIVE = re.compile(
    r"^\W{0,3}(?:obrigad[oa]\b|valeu\b|perfeito\b|[óo]timo\b|excelente\b|funcionou\b|thanks\b|thank you\b|perfect\b|"
    r"great\b|awesome\b|that worked\b)", re.I)
_WORD = re.compile(r"[^\W_]{3,}", re.UNICODE)


@functools.lru_cache(maxsize=1)
def _native_redactor():
    """Hermes' own secret redactor when running inside Hermes; None in the standalone runner."""
    try:
        from agent.redact import redact_sensitive_text
    except ImportError:
        return None
    return redact_sensitive_text


def _clean_hint(value) -> str:
    """Keep a hint only when it reads as a short name (argument, command, module or exception class).

    Hints come from fixed positions of an error message, but the text there is still untrusted: anything
    long, mostly upper case, digit-heavy or credential-shaped is dropped rather than stored.
    """
    if not isinstance(value, str):
        return ""
    value = value.rstrip(".-+")
    if not _HINT.fullmatch(value) or len(value) > 32 or label(value, fallback="") != value:
        return ""
    letters = [char for char in value if char.isalpha()]
    if sum(char.isdigit() for char in value) > 4 or not letters:
        return ""
    if len(value) > 8 and sum(char.isupper() for char in letters) > 0.6 * len(letters):
        return ""
    redact = _native_redactor()
    if redact is not None:
        try:
            if redact(value, force=True) != value:   # force: redact even when the profile turned redaction off
                return ""
        except Exception:   # a redactor failure must not let the value through
            return ""
    return value


def _error_parts(error_message, result) -> tuple[str, str, int | None]:
    """(text Hermes or the tool wrote about the error, command output, exit code)."""
    errors, output, exit_code = [], "", None
    if isinstance(error_message, str):
        errors.append(error_message[:2000])
    if isinstance(result, str) and len(result) <= _RESULT_LIMIT and result.lstrip()[:1] == "{":
        try:
            data = json.loads(result)
        except (ValueError, RecursionError):   # nested thousands deep: still a result, never a hook crash
            data = None
        if isinstance(data, dict):
            for key in ("error", "hint", "approval", "message"):
                value = data.get(key)
                if isinstance(value, str):
                    errors.append(value[:2000])
            value = data.get("output")
            if isinstance(value, str):
                output = value[-1500:]
            code = data.get("exit_code")
            if type(code) is int and -255 <= code <= 255:
                exit_code = code
    elif isinstance(result, str):
        output = result[:1500]
    return "\n".join(errors), output, exit_code


def _error_text(error_message, result) -> tuple[str, int | None]:
    errors, output, exit_code = _error_parts(error_message, result)
    return "\n".join(part for part in (errors, output) if part), exit_code


_OUTPUT_HINT = re.compile(r"[a-z_][a-z0-9_+-]{0,31}\Z")
_MODULE_HINT = re.compile(r"[a-z_][a-z0-9_]{0,31}(?:\.[a-z_][a-z0-9_]{0,31}){0,3}\Z")


def _output_hint(value, *, module: bool = False) -> str:
    """A hint read from command output is anyone's text: keep only lower-case command or module names."""
    value = _clean_hint(value)
    return value if (_MODULE_HINT if module else _OUTPUT_HINT).fullmatch(value) else ""


def effective_tool(tool_name, args=None) -> str:
    """Name the tool that actually ran. `tool_call` is Hermes' wrapper for deferred tools."""
    tool = label(tool_name)
    if tool == "tool_call" and isinstance(args, dict):
        calls = args.get("calls")
        if isinstance(calls, str) and len(calls) <= 65_536:
            try:
                calls = json.loads(calls)
            except (ValueError, RecursionError):
                calls = None
        if calls is None and args.get("name"):
            calls = [args]
        if isinstance(calls, list) and len(calls) == 1 and isinstance(calls[0], dict):
            return label(calls[0].get("name"), fallback=tool)
    return tool


def classify(tool_name, status, error_message=None, result=None, args=None) -> dict | None:
    """Classify one failed tool call. Returns tool, class, hint and signature, or None for a success."""
    if status not in ("error", "blocked"):
        return None
    tool = effective_tool(tool_name, args)
    errors, output, exit_code = _error_parts(error_message, result)
    text = "\n".join(part for part in (errors, output) if part)
    if tool == "tool_call":   # the wrapper did not say which tool; Hermes names it in its own error text
        target = _TARGET_TOOL.search(errors)
        if target and len(target.group("tool")) <= 96:
            named = _clean_hint(target.group("tool")) if len(target.group("tool")) <= 32 else label(target.group("tool"))
            tool = named or tool
    klass, hint = "", ""
    if _PLUGIN_FAULT.search(errors):   # Hermes' own fail-closed veto after another plugin's guard hung or raised
        signature = hashlib.sha256("\0".join((tool, "plugin_fault", "")).encode()).hexdigest()[:20]
        return {"tool": tool, "klass": "plugin_fault", "hint": "", "signature": signature}
    for source, from_output in ((errors, False), (output, True)):
        for name, pattern in _RULES:
            match = pattern.search(source) if source else None
            if match:
                klass = name
                groups = match.groupdict()
                key = next((key for key in ("hint", "hint2", "hint3") if groups.get(key)), None)
                if key and from_output:
                    hint = _output_hint(groups[key], module=name == "missing_dependency" and key == "hint2")
                elif key:
                    hint = _clean_hint(groups[key])
                break
        if klass:
            break
    if klass == "code_exception":
        found = _EXCEPTION.findall(text)
        hint = _clean_hint(found[-1]) if found else ""
    if klass == "blocked_by_policy" and re.search(r"-e/-c|inline (?:script|code)|script execution", text, re.I):
        hint = "inline_script"
    if klass == "not_found":
        hint = "skill" if re.search(r"\bskill\b", text, re.I) else "file" if re.search(r"\bfile\b|no such file|ENOENT", text, re.I) else ""
    if not klass:
        if status == "blocked":
            klass = "blocked_by_policy"
        elif exit_code not in (None, 0):
            klass, hint = "command_failed", f"exit_{exit_code}" if exit_code > 0 else "exit_signal"
        else:
            klass = "other"
    signature = hashlib.sha256("\0".join((tool, klass, hint)).encode()).hexdigest()[:20]
    return {"tool": tool, "klass": klass, "hint": hint, "signature": signature}


def _session_tag(session_id) -> str:
    if isinstance(session_id, str) and 0 < len(session_id) <= 400:
        return hashlib.sha256(session_id.encode()).hexdigest()[:16]
    return ""


def record_tool(home: Path, payload: dict, *, now: float | None = None, failure: dict | None = None,
                sample: bool = False, owner: bool = True, origin: str | None = None) -> dict | None:
    """Count one finished tool call and, for a failure, its signature. Returns the classification.

    `owner`: the call ran in a turn of the owner's own (CLI, TUI, Desktop, cron, a subagent, or a chat with
    a sender listed in owner_sender_ids). Every failure is counted; only the owner's are counted again in
    failure_owner, which decides whether a lesson may activate by itself, and only theirs keep an excerpt
    for the reflector: text a third party controls never reaches the model that drafts lessons.

    `origin`: the session's real origin label. A refused approval is also counted per origin and tool, so the
    radar can tell refusals nobody could have answered (a one-shot run, a cron job, a subagent) from the rest."""
    status = payload.get("status")
    if status not in ("ok", "error", "blocked"):
        return None
    now = time.time() if now is None else now
    tool = effective_tool(payload.get("tool_name"), payload.get("args"))
    if failure is None:
        failure = classify(payload.get("tool_name"), status, payload.get("error_message"), payload.get("result"),
                           payload.get("args"))
    duration = payload.get("duration_ms")
    duration = int(min(duration, 86_400_000)) if type(duration) in (int, float) and 0 <= duration < float("inf") else 0
    today = utc_day(now)
    row = (today, failure["tool"] if failure else tool, int(status == "error"), int(status == "blocked"), duration)
    seen = ((failure["signature"], failure["tool"], failure["klass"], failure["hint"], now, now,
             _session_tag(payload.get("session_id"))) if failure else None)
    origin_name = label(origin, fallback="unknown") if isinstance(origin, str) else "unknown"
    excerpt = ""
    if sample and owner and failure:
        from .reflector import UNTEMPLATED, scrub

        if failure["klass"] in UNTEMPLATED:   # only the reflector reads it, and only when it is switched on
            excerpt = scrub(_error_text(payload.get("error_message"), payload.get("result"))[0])
            program = command_program(payload.get("args"))
            if excerpt and program:   # which program failed makes a draft specific; only an identifier-like name
                excerpt = f"[{program}] {excerpt}"

    def write(conn):
        conn.execute("""INSERT INTO tool_daily VALUES (?, ?, 1, ?, ?, ?)
            ON CONFLICT DO UPDATE SET calls=calls+1, errors=errors+excluded.errors,
              blocked=blocked+excluded.blocked, duration_ms=duration_ms+excluded.duration_ms""", row)
        if seen:
            conn.execute("""INSERT INTO failure VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?)
                ON CONFLICT(signature) DO UPDATE SET last_seen=MAX(last_seen, excluded.last_seen), total=total+1,
                  sessions=sessions + (last_session != excluded.last_session), last_session=excluded.last_session""", seen)
            conn.execute("""INSERT INTO failure_daily VALUES (?, ?, 1)
                ON CONFLICT DO UPDATE SET count=count+1""", (today, seen[0]))
            if owner:
                conn.execute("""INSERT INTO failure_owner_daily VALUES (?, ?, 1)
                    ON CONFLICT DO UPDATE SET count=count+1""", (today, seen[0]))
            if seen[2] == "approval_refused":
                conn.execute("""INSERT INTO approval_daily VALUES (?, ?, ?, 1)
                    ON CONFLICT DO UPDATE SET count=count+1""", (today, origin_name, seen[1]))
            if excerpt:
                from .reflector import keep_sample

                keep_sample(conn, seen[0], seen[2], excerpt, now)

    record(home, write, now=now)
    return failure


_PROGRAM = re.compile(r"[A-Za-z_][\w.+-]{0,39}\Z")
_SHELL_TRIVIA = frozenset({"cd", "export", "set", "unset", "source", ".", "echo", "printf", "true", "false", "test",
                           "sleep", "pwd", "exit", "local", "read"})


def command_program(args) -> str:
    """The program a terminal command runs (its first word after variable assignments and wrappers such as sudo,
    env, timeout or systemd-run, read the way the runtime guard reads them), when it looks like a program name;
    '' otherwise. Never arguments, paths or other text of the command."""
    from .guard import _segments, _strip_prefix

    command = args.get("command") if isinstance(args, dict) else None
    if not isinstance(command, str) or not command.strip():
        return ""
    found = ""
    for segment in (_segments(command[:4000]) or [])[:16]:   # the last real program of a chain is the likeliest to fail
        tokens = _strip_prefix(segment)
        name = tokens[0].rsplit("/", 1)[-1] if tokens else ""
        if name and name not in _SHELL_TRIVIA and _PROGRAM.match(name) and label(name, fallback="") == name:
            found = name
    return found


def surface_of(platform, source=None) -> str:
    """Where a turn comes from. `source` is Hermes' real origin of the session when known (a kanban worker
    and `hermes chat --source tool` run with platform "cli"): local (the owner's CLI, TUI, Desktop, ACP, a
    one-shot run), cron (scheduled and automated work: cron jobs, kanban workers, the Curator), subagent (a
    delegated child) or gateway (a messaging chat, an integration, an A2A peer: someone else may be there)."""
    kind = source if isinstance(source, str) and source.strip() else platform
    if not isinstance(kind, str):
        return "gateway"
    lower = kind.strip().lower()
    if lower == "subagent":
        return "subagent"
    if lower in _AUTOMATED_SOURCES:
        return "cron"
    return "local" if lower in _LOCAL_PLATFORMS else "gateway"


def session_source(platform) -> str | None:
    """Hermes' real origin of the running session (kanban, tool, a2a, oneshot, ...), read from its session
    environment during a turn. None when Hermes cannot say; the platform then decides."""
    module = sys.modules.get("agent.session_source")
    resolve = getattr(module, "session_source_for", None)
    if not callable(resolve):
        return None
    try:
        value = resolve(platform if isinstance(platform, str) and platform else None)
    except Exception:
        return None
    return value if isinstance(value, str) and value.strip() else None


def turn_surface(payload: dict) -> str:
    """The surface of a turn from its pre_llm_call payload. Only a delegated child is a subagent: a session
    continued after /new, /branch or a compression has a parent too, and stays where it is."""
    if isinstance(payload.get("surface"), str) and payload["surface"] in SURFACES:
        return payload["surface"]
    platform = payload.get("platform")
    if not platform and payload.get("parent_session_id"):
        return "subagent"
    return surface_of(platform, session_source(platform))


def origin_label(payload: dict) -> str:
    """The session's real origin as a short label (telegram, api_server, oneshot, kanban, ...), for owner
    platforms and approval counts. Never free text."""
    platform = payload.get("platform")
    source = payload.get("source") if isinstance(payload.get("source"), str) else session_source(platform)
    for value in (source, platform):
        if isinstance(value, str) and value.strip():
            return label(value.strip().lower()[:48], fallback="unknown")
    return "unknown"


def owner_turn(surface: str, sender_id, owners, *, origin: str | None = None, owner_platforms=frozenset()) -> bool:
    """True when the owner is the one being served: their own surfaces, their automations and subagents, a
    chat message from a sender listed in owner_sender_ids, or any chat of a platform listed in owner_platforms
    (a private bot or the owner's own UI, where nobody else writes)."""
    if surface in ("local", "cron", "subagent"):
        return True
    if surface != "gateway":
        return False
    if origin is not None and origin in owner_platforms:
        return True
    return sender_id is not None and str(sender_id) in owners


MAX_SKILLS_PER_TURN = 16
CORRECTION_WINDOW_SECONDS = 1800


@dataclass
class _Turn:
    started: float
    surface: str
    owners: tuple = ()
    calls: int = 0
    errors: int = 0
    seen: set[bytes] = field(default_factory=set)
    skills: set[str] = field(default_factory=set)


def _key(identifier) -> bytes | None:
    if isinstance(identifier, str) and 0 < len(identifier) <= 400:
        return hashlib.sha256(identifier.encode()).digest()
    return None


class TurnTracker:
    """Measure every turn from pre_llm_call to on_session_end. Holds no text; bounded to 512 open turns."""

    def __init__(self, home: Path, *, slow_ms: int = DEFAULT_SLOW_TURN_MS):
        self.home = home
        self.slow_ms = slow_ms
        self._turns: OrderedDict[bytes, _Turn] = OrderedDict()
        self._owner: OrderedDict[bytes, bytes] = OrderedDict()   # session or task key -> open turn key
        self._last: OrderedDict[bytes, tuple[float, tuple[str, ...]]] = OrderedDict()   # session -> finished turn skills
        self._lock = threading.Lock()

    def _forget(self, key: bytes) -> None:
        turn = self._turns.pop(key, None)
        for owner in turn.owners if turn else ():
            if self._owner.get(owner) == key:
                del self._owner[owner]

    def start(self, payload: dict, *, clock: float | None = None) -> None:
        key = _key(payload.get("turn_id"))
        if not key:
            return
        clock = time.monotonic() if clock is None else clock
        surface = turn_surface(payload)
        owners = tuple(owner for owner in (_key(payload.get("session_id")), _key(payload.get("task_id"))) if owner)
        with self._lock:
            if key in self._turns:
                return
            self._turns[key] = _Turn(clock, surface, owners)
            for owner in owners:
                self._owner[owner] = key
                self._owner.move_to_end(owner)
            while len(self._turns) > 512 or clock - next(iter(self._turns.values())).started > 6 * 3600:
                self._forget(next(iter(self._turns)))
            while len(self._owner) > 1024:
                self._owner.popitem(last=False)

    def skill(self, payload: dict) -> None:
        """Attach a loaded skill to the open turn of its session or task."""
        if payload.get("action") != "loaded":
            return
        skill = label(payload.get("skill_name"), fallback="")
        if not skill or len(skill) > 80:
            return
        with self._lock:
            for owner in (_key(payload.get("session_id")), _key(payload.get("task_id"))):
                turn = self._turns.get(self._owner.get(owner)) if owner else None
                if turn is not None:
                    if len(turn.skills) < MAX_SKILLS_PER_TURN:
                        turn.skills.add(skill)
                    return

    def recent_skills(self, session_id, *, clock: float | None = None) -> tuple[str, ...]:
        """Skills of the session's last finished turn, when it ended within the correction window."""
        session = _key(session_id)
        clock = time.monotonic() if clock is None else clock
        with self._lock:
            entry = self._last.get(session) if session else None
        if entry is None or clock - entry[0] > CORRECTION_WINDOW_SECONDS:
            return ()
        return entry[1]

    def tool(self, payload: dict) -> None:
        if payload.get("status") not in ("ok", "error", "blocked"):
            return
        key = _key(payload.get("turn_id"))
        call = _key(payload.get("tool_call_id"))
        with self._lock:
            turn = self._turns.get(key) if key else None
            if turn is None or turn.calls >= 4096 or call and call in turn.seen:
                return
            if call:
                turn.seen.add(call)
            turn.calls += 1
            turn.errors += int(payload.get("status") == "error")

    def finish(self, payload: dict, *, clock: float | None = None, now: float | None = None) -> bool:
        key = _key(payload.get("turn_id"))
        if not key:
            return False
        clock = time.monotonic() if clock is None else clock
        with self._lock:
            turn = self._turns.get(key)
            self._forget(key)
            session = turn.owners[0] if turn is not None and turn.owners else None
            if session is not None and turn.surface in ("local", "gateway"):
                self._last[session] = (clock, tuple(sorted(turn.skills)))
                self._last.move_to_end(session)
                while len(self._last) > 256:
                    self._last.popitem(last=False)
        if turn is None or clock < turn.started:
            return False
        duration = min(14_400_000, int((clock - turn.started) * 1000))
        failed = payload.get("failed") is True
        interrupted = payload.get("interrupted") is True and not failed
        completed = payload.get("completed") is True and not failed and not interrupted
        bucket = next((name for limit, name in LATENCY_BUCKETS if duration <= limit), "b_more")
        buckets = [int(bucket == name) for name in ("b15", "b30", "b60", "b120", "b300", "b_more")]
        stamp = time.time() if now is None else now
        row = (utc_day(stamp), turn.surface, int(completed), int(failed), int(interrupted),
               int(duration >= self.slow_ms), int(turn.calls > 0), turn.errors, duration, duration, *buckets)

        skill_rows = [(utc_day(stamp), skill, int(failed), int(interrupted), int(duration >= self.slow_ms),
                       int(turn.errors > 0), duration) for skill in sorted(turn.skills)] if turn.surface in ("local", "gateway") else []

        def write(conn):
            conn.executemany("""INSERT INTO skill_outcome_daily VALUES (?, ?, 1, ?, ?, ?, ?, 0, ?)
                ON CONFLICT DO UPDATE SET turns=turns+1, failed=failed+excluded.failed,
                  interrupted=interrupted+excluded.interrupted, slow=slow+excluded.slow,
                  tool_error_turns=tool_error_turns+excluded.tool_error_turns,
                  duration_ms=duration_ms+excluded.duration_ms""", skill_rows)
            conn.execute("""INSERT INTO turn_daily VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO UPDATE SET turns=turns+1, completed=completed+excluded.completed,
                  failed=failed+excluded.failed, interrupted=interrupted+excluded.interrupted,
                  slow=slow+excluded.slow, with_tools=with_tools+excluded.with_tools,
                  tool_errors=tool_errors+excluded.tool_errors, duration_ms=duration_ms+excluded.duration_ms,
                  max_ms=MAX(max_ms, excluded.max_ms), b15=b15+excluded.b15, b30=b30+excluded.b30,
                  b60=b60+excluded.b60, b120=b120+excluded.b120, b300=b300+excluded.b300,
                  b_more=b_more+excluded.b_more""", row)

        record(self.home, write, now=stamp)
        return True


class FeedbackSensor:
    """Detect owner corrections, repeated questions and thanks at turn start. Stores counters only.

    The previous message of a session is kept in memory as a set of hashed words, never as text.
    """

    def __init__(self, home: Path, *, owner_sender_ids=(), owner_platforms=(), reask_seconds: int = 180):
        self.home = home
        self.owners = frozenset(str(item) for item in owner_sender_ids
                                if isinstance(item, str) and item or type(item) is int)
        self.platforms = frozenset(item for item in owner_platforms if isinstance(item, str) and item)
        self.reask_seconds = reask_seconds
        self._last: OrderedDict[bytes, tuple[float, frozenset]] = OrderedDict()
        self._lock = threading.Lock()

    def _from_owner(self, payload: dict) -> bool:
        """A message the owner wrote: on their own surfaces, or in a chat from a listed sender. Automations,
        kanban workers and subagents get prompts written by software, never feedback."""
        surface = turn_surface(payload)
        if surface == "local":
            return True
        origin = payload.get("origin") if isinstance(payload.get("origin"), str) else origin_label(payload)
        return surface == "gateway" and owner_turn(surface, payload.get("sender_id"), self.owners, origin=origin,
                                                   owner_platforms=self.platforms)

    def observe(self, payload: dict, *, clock: float | None = None, now: float | None = None) -> list[str]:
        message = payload.get("user_message")
        session = _key(payload.get("session_id"))
        if not isinstance(message, str) or not message.strip() or session is None or not self._from_owner(payload):
            return []
        if _SYNTHETIC.match(message):
            return []   # Hermes continuing a /goal: nobody wrote it
        clock = time.monotonic() if clock is None else clock
        head = message.strip()[:240]
        words = frozenset(hashlib.blake2b(word.lower().encode(), digest_size=6).digest()
                          for word in _WORD.findall(message[:2000]))
        same = hashlib.blake2b(" ".join(message.split()).lower().encode("utf-8", "replace"), digest_size=8).digest()
        first = payload.get("is_first_turn") is True   # a new session has no earlier turn to correct or thank for
        kinds = []
        if first:
            pass
        elif _CORRECTION.search(head):
            kinds.append("correction")
        elif _POSITIVE.search(head):
            kinds.append("positive")
        with self._lock:
            previous = self._last.get(session)
            # The same text again is a /loop tick or a deliberate repeat, not the question asked again.
            if (previous and not first and previous[2] != same and clock - previous[0] <= self.reask_seconds
                    and len(words) >= 4 and len(words & previous[1]) / len(words | previous[1]) >= 0.6):
                kinds.append("reask")
            self._last[session] = (clock, words, same)
            self._last.move_to_end(session)
            while len(self._last) > 256:
                self._last.popitem(last=False)
        if kinds:
            record_feedback(self.home, kinds, now=now)
        return kinds


def record_feedback(home: Path, kinds, *, now: float | None = None) -> None:
    stamp = time.time() if now is None else now
    rows = [(utc_day(stamp), kind) for kind in kinds if kind in ("correction", "reask", "positive")]
    if rows:
        record(home, lambda conn: conn.executemany("""INSERT INTO feedback_daily VALUES (?, ?, 1)
            ON CONFLICT DO UPDATE SET count=count+1""", rows), now=stamp)


def record_skill_correction(home: Path, skills, *, now: float | None = None) -> None:
    """Count an owner correction against the skills of the turn being corrected."""
    stamp = time.time() if now is None else now
    rows = [(utc_day(stamp), skill) for skill in skills if isinstance(skill, str) and skill]
    if rows:
        record(home, lambda conn: conn.executemany("""INSERT INTO skill_outcome_daily VALUES (?, ?, 0, 0, 0, 0, 0, 1, 0)
            ON CONFLICT DO UPDATE SET corrected=corrected+1""", rows), now=stamp)


def record_skill_event(home: Path, payload: dict, *, now: float | None = None) -> None:
    action = payload.get("action")
    skill = label(payload.get("skill_name"), fallback="")
    if action not in _SKILL_ACTIONS or not skill or len(skill) > 80:
        return
    stamp = time.time() if now is None else now
    today = utc_day(stamp)
    rows = [(today, skill, action)]
    if action == "loaded" and payload.get("reuse_after_patch") is True:
        rows.append((today, skill, "reused_after_patch"))
    record(home, lambda conn: conn.executemany("""INSERT INTO skill_daily VALUES (?, ?, ?, 1)
        ON CONFLICT DO UPDATE SET count=count+1""", rows), now=stamp)


def record_guard(home: Path, kind: str, *, now: float | None = None) -> None:
    """Count a command the runtime guard vetoed. Only the kind is kept, never the command."""
    if kind not in ("gateway_stop", "runtime_removal"):
        return
    stamp = time.time() if now is None else now
    record(home, lambda conn: conn.execute("""INSERT INTO guard_daily VALUES (?, ?, 1)
        ON CONFLICT DO UPDATE SET count=count+1""", (utc_day(stamp), kind)), now=stamp)


def guard_summary(conn, *, now: float, days: int) -> dict:
    start, end = _window(now, days)
    return dict(conn.execute("SELECT kind, SUM(count) FROM guard_daily WHERE day BETWEEN ? AND ? GROUP BY kind",
                             (start, end)).fetchall())


# ── summaries ────────────────────────────────────────────────────────────────────────────────────

def _window(now: float, days: int) -> tuple[str, str]:
    return day_offset(now, days - 1), utc_day(now)


def tool_summary(conn, *, now: float, days: int, limit: int = 15) -> dict:
    start, end = _window(now, days)
    rows = conn.execute("""SELECT tool, SUM(calls) AS calls, SUM(errors) AS errors, SUM(blocked) AS blocked,
          SUM(duration_ms) AS duration_ms FROM tool_daily WHERE day BETWEEN ? AND ?
          GROUP BY tool ORDER BY SUM(errors)+SUM(blocked) DESC, SUM(calls) DESC, tool""", (start, end)).fetchall()
    calls = sum(row["calls"] for row in rows)
    errors = sum(row["errors"] for row in rows)
    return {"calls": calls, "errors": errors, "blocked": sum(row["blocked"] for row in rows),
            "error_pct": round(100 * errors / calls, 2) if calls else None, "tools_seen": len(rows),
            "by_tool": [{"tool": row["tool"], "calls": row["calls"], "errors": row["errors"], "blocked": row["blocked"],
                         "error_pct": round(100 * row["errors"] / row["calls"], 1),
                         "mean_ms": row["duration_ms"] // row["calls"]} for row in rows[:limit]]}


# Tools whose time is not latency: they wait for a person (clarify) or run a whole subagent (delegate_task).
NOT_LATENCY = frozenset({"clarify", "delegate_task", "ask_user", "request_user_input"})


def slow_tools(conn, *, now: float, days: int, limit: int = 3, min_calls: int = 3) -> list[dict]:
    """The tools that took the most time in the window: calls, mean and total seconds. Durations come from the
    hooks and, for replayed history, from the time between a tool request and its result (approximate). Tools
    that wait for a person or run a subagent are left out: their time is not a bottleneck of the tool."""
    start, end = _window(now, days)
    marks = ",".join("?" * len(NOT_LATENCY))
    rows = conn.execute(f"""SELECT tool, SUM(calls) AS calls, SUM(duration_ms) AS duration_ms FROM tool_daily
        WHERE day BETWEEN ? AND ? AND tool NOT IN ({marks}) GROUP BY tool HAVING SUM(calls) >= ? AND SUM(duration_ms) > 0
        ORDER BY SUM(duration_ms) DESC, tool LIMIT ?""", (start, end, *sorted(NOT_LATENCY), min_calls, limit)).fetchall()
    return [{"tool": row["tool"], "calls": row["calls"], "mean_s": round(row["duration_ms"] / row["calls"] / 1000, 1),
             "total_min": round(row["duration_ms"] / 60_000, 1)} for row in rows]


def failure_summary(conn, *, now: float, days: int, limit: int = 12) -> list[dict]:
    start, end = _window(now, days)
    rows = conn.execute("""SELECT f.signature, f.tool, f.klass, f.hint, f.total, f.sessions, f.first_seen, f.last_seen,
          SUM(d.count) AS recent, COUNT(DISTINCT d.day) AS recent_days,
          (SELECT status FROM lesson l WHERE l.signature=f.signature) AS lesson_status
        FROM failure f JOIN failure_daily d ON d.signature=f.signature
        WHERE d.day BETWEEN ? AND ? GROUP BY f.signature
        ORDER BY recent DESC, recent_days DESC, f.signature LIMIT ?""", (start, end, limit)).fetchall()
    return [{"signature": row["signature"], "tool": row["tool"], "class": row["klass"], "hint": row["hint"],
             "count": row["recent"], "days": row["recent_days"], "total": row["total"], "sessions": row["sessions"],
             "lesson": row["lesson_status"]} for row in rows]


def _percentile(counts: list[int], share: float, maximum: int) -> int | None:
    total = sum(counts)
    if not total:
        return None
    limits = [limit for limit, _ in LATENCY_BUCKETS] + [maximum]
    seen = 0
    for count, limit in zip(counts, limits):
        seen += count
        if seen >= total * share:
            return min(limit, maximum) if maximum else limit
    return maximum


def turn_summary(conn, *, now: float, days: int, surfaces=SURFACES) -> dict:
    start, end = _window(now, days)
    marks = ",".join("?" * len(surfaces))
    row = conn.execute(f"""SELECT COALESCE(SUM(turns),0) AS turns, COALESCE(SUM(completed),0) AS completed,
          COALESCE(SUM(failed),0) AS failed, COALESCE(SUM(interrupted),0) AS interrupted,
          COALESCE(SUM(slow),0) AS slow, COALESCE(SUM(with_tools),0) AS with_tools,
          COALESCE(SUM(tool_errors),0) AS tool_errors, COALESCE(SUM(duration_ms),0) AS duration_ms,
          COALESCE(MAX(max_ms),0) AS max_ms, COALESCE(SUM(b15),0) AS b15, COALESCE(SUM(b30),0) AS b30,
          COALESCE(SUM(b60),0) AS b60, COALESCE(SUM(b120),0) AS b120, COALESCE(SUM(b300),0) AS b300,
          COALESCE(SUM(b_more),0) AS b_more FROM turn_daily
          WHERE day BETWEEN ? AND ? AND surface IN ({marks})""", (start, end, *surfaces)).fetchone()
    turns = row["turns"]
    counts = [row[name] for name in ("b15", "b30", "b60", "b120", "b300", "b_more")]

    def pct(value):
        return round(100 * value / turns, 2) if turns else None

    return {"turns": turns, "completed": row["completed"], "failed": row["failed"], "interrupted": row["interrupted"],
            "slow": row["slow"], "with_tools": row["with_tools"], "tool_errors": row["tool_errors"],
            "failed_pct": pct(row["failed"]), "interrupted_pct": pct(row["interrupted"]), "slow_pct": pct(row["slow"]),
            "mean_ms": row["duration_ms"] // turns if turns else None, "max_ms": row["max_ms"] if turns else None,
            "p50_ms": _percentile(counts, 0.5, row["max_ms"]), "p95_ms": _percentile(counts, 0.95, row["max_ms"]),
            "basis": "turns_measured_by_this_plugin_between_pre_llm_call_and_on_session_end",
            "percentiles": "upper_bound_of_latency_bucket"}


def feedback_summary(conn, *, now: float, days: int) -> dict:
    start, end = _window(now, days)
    rows = dict(conn.execute("SELECT kind, SUM(count) FROM feedback_daily WHERE day BETWEEN ? AND ? GROUP BY kind",
                             (start, end)).fetchall())
    return {"correction": rows.get("correction", 0), "reask": rows.get("reask", 0), "positive": rows.get("positive", 0),
            "basis": "owner_messages_classified_in_memory_counters_only"}


def skill_event_summary(conn, *, now: float, days: int) -> dict:
    start, end = _window(now, days)
    result: dict[str, dict] = {}
    for skill, action, count in conn.execute(
            "SELECT skill, action, SUM(count) FROM skill_daily WHERE day BETWEEN ? AND ? GROUP BY skill, action",
            (start, end)):
        result.setdefault(skill, {})[action] = count
    return result
