"""Hermes Agent v0.23.1 adapters: hooks, tools, the playbook prompt section and the operator CLI.

Core operations are usable without Hermes. Every hook callback fails open: a storage problem is logged
once and the turn continues.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path

from .cli import execute, safe_settings, setup
from .operations import observe
from .telemetry import ResearchTracker

# Hermes' own events the plugin follows, beyond the turns and tool calls: a delegated child names its parent
# (subagent_start), a compaction carries a session to a new id and says how much context it kept (post_compaction).
NATIVE_HOOKS = ("subagent_start", "post_compaction")


# The one line Hermes shows for `hermes evolution` (its help and command list), in both languages.
CLI_HELP = "Atlas Evolution: lessons, health and care of every profile · Lições, saúde e cuidado de todos os perfis"
SETTINGS_CHECK_SECONDS = 30   # how often a hook or the clock may look for a change of the settings
RELOAD_KEYS = ("runtime_guard", "tool_hints")   # these decide which hooks are registered: next load of the plugin
PLAYBOOK_TTL_SECONDS = 30   # the playbook as rendered is reused this long
PLAYBOOK_BUSY_SECONDS = 0.25   # the lessons section reads on the prompt path: a busy database leaves it empty


def _managed_files() -> list:
    """The administrator's managed configuration files, when Hermes has a managed scope (read-only lookup)."""
    module = sys.modules.get("hermes_cli.managed_scope")
    try:
        folder = module.get_managed_dir() if module is not None else None
    except Exception:
        return []
    if folder is None:
        return []
    try:
        return sorted(Path(folder).glob("*.yaml"))[:8] + sorted(Path(folder).glob("*.env"))[:4]
    except OSError:
        return []


def _settings_stamp(home: Path) -> tuple:
    """Modification times of the files the settings come from: Hermes' config.yaml, the administrator's managed
    configuration and the plugin's settings.json."""
    stamp = []
    for path in (Path(home) / "config.yaml", Path(home) / "evolution" / "settings.json", *_managed_files()):
        try:
            info = path.stat()
            stamp.append((info.st_mtime_ns, info.st_size))
        except OSError:
            stamp.append(None)
    return tuple(stamp)


def _read_hermes_config(home: Path) -> dict | None:
    """The profile's configuration as Hermes loads it at start (its own loader, under this profile: the managed
    overlay of an administrator and `${VAR}` expansion included), or None when it cannot be read now. Outside
    Hermes, config.yaml as written, when a YAML parser is available."""
    module = sys.modules.get("hermes_cli.config")
    loader = getattr(module, "load_config_readonly", None) if module is not None else None
    if callable(loader):
        from .autopilot import pinned_profile

        try:
            with pinned_profile(home):
                data = loader()
        except Exception:
            return None
        return data if isinstance(data, dict) else None
    try:
        import yaml
    except ImportError:
        return None
    try:
        body = (Path(home) / "config.yaml").read_text(encoding="utf-8", errors="replace")[:8_000_000]
        data = yaml.safe_load(body) if body.strip() else {}
    except (OSError, ValueError, RecursionError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _chat_type() -> str:
    """The chat type Hermes' gateway bound to this turn ("dm", "group", "channel", "thread"), or "" when unknown or
    outside a gateway. Read from the module the gateway already loaded, never imported here."""
    module = sys.modules.get("gateway.session_context")
    reader = getattr(module, "get_session_env", None) if module is not None else None
    if not callable(reader):
        return ""
    try:
        value = reader("HERMES_SESSION_CHAT_TYPE", "")
    except Exception:
        return ""
    return value.strip().lower() if isinstance(value, str) else ""


def _keep(kept: OrderedDict, key: str, value=None, limit: int = 2048) -> None:
    """Remember `key` as the most recent entry of a bounded memory (the caller holds the lock)."""
    kept[key] = value
    kept.move_to_end(key)
    while len(kept) > limit:
        kept.popitem(last=False)


def _warn_once(seen: set, event: str) -> None:
    if event not in seen:
        seen.add(event)
        from logging import getLogger

        getLogger(__name__).warning("Atlas Evolution could not record %s; the turn continues unaffected", event)


def autopilot_pass(home: Path, reflect, cancelled=None, *, reflector_reload: bool = False, events=None) -> dict:
    """One evolution pass from the autopilot thread. Settings are read again, so a change made in Hermes'
    settings form applies to the next pass, and an invalid value falls back instead of failing the pass.
    `events(result)`, when given, hears what the pass changed (the plugin's events on Hermes' bus); it never fails it."""
    from .autopilot import LEASE_SECONDS
    from .evolve import CONFIG_UNREAD, cycle

    try:
        from hermes_cli.config import load_config_readonly

        runtime = load_config_readonly() or {}
    except Exception:   # the pass still runs with the plugin's own settings; who the owner is stays unknown
        runtime = {CONFIG_UNREAD: True}
    current, _problems = safe_settings(home, runtime)
    # within the pass's lease, with room to spare: past it another process could start a second pass
    result = cycle(home, now=time.time(), runtime_config=runtime, config=current, reflect=reflect, strict=False,
                   cancelled=cancelled, deadline=time.monotonic() + LEASE_SECONDS - 180,
                   reflector_reload=reflector_reload)
    if events is not None:
        try:
            events(result)
        except Exception:   # an event nobody may be listening to never costs the pass
            pass
    reflection = result["reflector"]
    return {"new_lessons": len(result["new_lessons"]), "open_proposals": len(result["open_proposals"]),
            "reflector_status": reflection.get("status", "ran"), "reflector_drafts": len(reflection.get("drafted") or []),
            "reviews_ingested": result["reviews_ingested"]}


def _register_skills(ctx) -> None:
    """Expose the bundled skills as `hermes-atlas-evolution:<name>`. Optional: an older or stricter host skips them."""
    folder = Path(__file__).resolve().parent.parent / "skills"
    register_skill = getattr(ctx, "register_skill", None)
    if not callable(register_skill) or not folder.is_dir():
        return
    for child in sorted(folder.iterdir()):
        document = child / "SKILL.md"
        if document.is_file():
            try:
                register_skill(child.name, document)
            except Exception:   # a skill is a convenience; it must never stop the plugin from loading
                from logging import getLogger

                getLogger(__name__).warning("Atlas Evolution could not register the bundled skill %s", child.name)


# Session ends whose on_session_finalize text Hermes 0.23.1 shows: the classic CLI's exit ("shutdown") and its /new
# or /reset ("session_boundary"); the TUI and Desktop backend pass no reason and return the text with
# session.close. Every other reason (a viewer closing a gateway session, /resume, /branch, a reap) drops it, so the
# day's notice waits for an end that shows it. See cli.py, hermes_cli/cli_session_mixin.py, tui_gateway/.
_SHOWN_END_REASONS = frozenset({"shutdown", "session_boundary"})
_OWNER_ONLY_TOOL = ("Atlas Evolution's tools answer only in the owner's own sessions (CLI, TUI, Desktop, automations, "
                    "or a chat with a sender listed in owner_sender_ids).")


class _Plugin:
    """One load of the plugin in one profile: its settings, trackers and hook callbacks.

    Every callback fails open. A storage problem is logged once per kind and the turn goes on; a callback
    whose return value Hermes uses (a transform, the prompt section, a notice) returns None or "" on any doubt."""

    def __init__(self, home: Path, config: dict, evolution: dict):
        from .hints import HintIndex
        from .signals import FeedbackSensor, TurnTracker

        self.home, self.config, self.evolution = home, config, evolution
        self.advice = config.get("agent_advice") is True
        self.research = ResearchTracker(home)
        self.turns = TurnTracker(home, slow_ms=evolution["slow_turn_seconds"] * 1000)
        self.sensor = (FeedbackSensor(home, owner_sender_ids=evolution["owner_sender_ids"],
                                      owner_platforms=evolution["owner_platforms"])
                       if evolution["feedback_signals"] else None)
        self.hints = HintIndex(home, lang=evolution["language"])
        self.recent: dict[str, dict] = {}   # tool_call_id -> failure, for the transform that follows the tool
        self.commands: dict[str, str] = {}  # tool_call_id -> hash of the command the agent wrote (memory only)
        self.counted: OrderedDict[str, None] = OrderedDict()   # sessions whose playbook render was already counted
        # session id -> (whether its latest turn served the owner, its origin label); memory only
        self.sessions: OrderedDict[str, tuple[bool, str]] = OrderedDict()
        # delegated child session id -> its parent's session id, from subagent_start: a child serves whoever
        # its parent served, and its prompt is built before its first turn says who the parent is
        self.children: OrderedDict[str, str] = OrderedDict()
        self.owners = frozenset(str(item) for item in evolution["owner_sender_ids"] if item not in (None, ""))
        self.owner_platforms = frozenset(evolution["owner_platforms"])
        # The RELOAD_KEYS settings this load registered its hooks with: a change of these waits for the next load
        # (apply_settings). Their values, not the hooks: switching lessons off silences the tool hint at once.
        self.loaded = {key: evolution[key] for key in RELOAD_KEYS}
        self.reflector_possible = False   # the reflector, off at load, would get its model access at the next load
        # chat session id -> True when this process built its prompt with template lessons only (a stranger may be on
        # the other side); then, once its sender turns out to be the owner, the turn's context brings the rest
        self.prompted: OrderedDict[str, bool] = OrderedDict()
        self.given: OrderedDict[str, None] = OrderedDict()       # chat sessions that got the owner's lessons
        self.strangers: OrderedDict[str, None] = OrderedDict()   # chat sessions where someone else wrote
        self.last_local: OrderedDict[str, str] = OrderedDict()   # local surface (cli, tui...) -> its latest session
        self.emit = None   # Hermes' event bus for this plugin (ctx.emit), when the host has one
        self._spikes_told: tuple[str, frozenset] = ("", frozenset())   # (UTC day, signatures) emitted today
        self.guard = None
        self.recent_lock = threading.Lock()
        self.failed: set[str] = set()
        self.reflect = None
        self.autopilot = None
        from .agent_tools import LessonsTool

        self.lessons_tool = LessonsTool(home, lang=evolution["language"])   # one instance: it holds the hourly cap
        self.clock_allowed = False   # the host can stop the autopilot's clock (on_unload): it may be started later
        self.section_registered = False   # the playbook section exists only when lessons were on at load
        self._settings_checked = float("-inf")   # never: the monotonic clock may be under 30 s on a fresh machine
        self._settings_stamp = _settings_stamp(home)
        self._settings_lock = threading.Lock()
        # (lang, templates only) -> (monotonic time, text, lesson ids shown): the playbook as last rendered
        self._playbook: dict[tuple, tuple] = {}
        self._playbook_generation = 0   # bumped by forget_playbook: a render that started before is not cached
        from .selfcost import Meter

        self.meter = Meter()   # what each hook costs the turn, measured where it runs (selfcost)

    def forget_playbook(self) -> None:
        """Render the playbook again at the next prompt (lessons or settings changed in this process). A render
        already under way finishes with what it read, but is not kept: it may hold the lessons as they were."""
        with self.recent_lock:
            self._playbook.clear()
            self._playbook_generation += 1

    @property
    def lang(self) -> str:
        return self.evolution["language"]

    def warn(self, event: str) -> None:
        _warn_once(self.failed, event)

    # ── observation ──────────────────────────────────────────────────────────────────────────────────

    def observer(self, event: str):
        def callback(**payload):
            try:
                observe(self.home, event, payload)
            except Exception:
                self.warn(event)
        return callback

    def native(self, hook: str):
        def callback(**payload):
            if hook == "subagent_start":
                self._remember_child(payload.get("child_session_id"), payload.get("parent_session_id"))
            elif hook == "post_compaction":   # a compaction mid-turn continues the session under a new id
                self._renamed(payload.get("old_session_id"), payload.get("session_id"))
                try:   # how much context it kept: the radar's compaction reading (learning pass, digest)
                    from .radar import observe_compaction

                    observe_compaction(self.home, payload)
                except Exception:
                    self.warn(hook)
        return callback

    def _remember_child(self, child, parent) -> None:
        if isinstance(child, str) and isinstance(parent, str) and 0 < len(child) <= 400 and 0 < len(parent) <= 400:
            with self.recent_lock:
                self.children[child] = parent
                self.children.move_to_end(child)
                while len(self.children) > 2048:
                    self.children.popitem(last=False)

    def _renamed(self, old, new) -> None:
        """Carry what the plugin knows about a session to the id a compaction gave it: who it served, and the
        parent of a delegated child, so its children and its own next calls are attributed as before."""
        if not (isinstance(old, str) and isinstance(new, str) and old and new and old != new and len(new) <= 400):
            return
        with self.recent_lock:
            if old in self.sessions and new not in self.sessions:
                self.sessions[new] = self.sessions[old]
                while len(self.sessions) > 2048:
                    self.sessions.popitem(last=False)
            if old in self.children and new not in self.children:
                self.children[new] = self.children[old]
                while len(self.children) > 2048:
                    self.children.popitem(last=False)
            if old in self.strangers:   # the same conversation: someone else still wrote in it
                _keep(self.strangers, new)

    def session_reset(self, **payload):
        """on_session_reset: /new or /reset ended a session. Its session_id is the new one; the gateway names the one
        that ended (old_session_id), whose memory goes. The classic CLI names only the new one (reason "new_session"):
        there the session that ended is the latest one this process saw a turn of on that surface. The TUI and the
        Desktop fire it, with no reason, whenever a window starts or resumes a session, while another may still be
        open: nothing is forgotten then."""
        old = payload.get("old_session_id")
        if not (isinstance(old, str) and old):
            if payload.get("reason") != "new_session":
                return None
            platform, new = payload.get("platform"), payload.get("session_id")
            with self.recent_lock:
                old = self.last_local.get(platform[:64]) if isinstance(platform, str) else None
            old = old if old != new else None
        return self._forget_sessions([old])

    def session_deleted(self, **payload):
        """on_session_delete: a session deleted or archived (session_id; deleted_ids when several went)."""
        deleted = payload.get("deleted_ids")
        return self._forget_sessions([payload.get("session_id"), *(deleted[:1000] if isinstance(deleted, list) else ())])

    def _forget_sessions(self, gone: list):
        """Forget what this process kept in memory about these sessions: who they served, their delegated children,
        whether their prompt was counted, the owner's lessons given, the tool hints given, the last turn's skills and
        words. Memory only; never raises."""
        try:
            gone = [item for item in gone if isinstance(item, str) and 0 < len(item) <= 400]
            if not gone:
                return None
            with self.recent_lock:
                for session in gone:
                    for kept in (self.sessions, self.children, self.counted, self.prompted, self.given, self.strangers):
                        kept.pop(session, None)
            self.turns.forget_sessions(gone)
            sensor = self.sensor
            if sensor is not None:
                sensor.forget_sessions(gone)
            given = getattr(self.hints, "_given", None)   # the tool hints already given in each session
            lock = getattr(self.hints, "_lock", None)
            if isinstance(given, dict) and lock is not None:
                with lock:
                    for session in gone:
                        given.pop(session, None)
        except Exception:
            pass
        return None

    def parent_of(self, session) -> str | None:
        with self.recent_lock:
            return self.children.get(session) if isinstance(session, str) else None

    def pre_tool(self, **payload):
        """Remember a hash of the command the agent wrote (another plugin may rewrite it before it runs), then
        let the runtime guard decide. Hermes blocks a tool whose pre_tool_call raises, so this never raises."""
        try:
            self._remember_command(payload)
        except Exception:
            pass
        try:
            return self.guard(**payload) if self.guard is not None else None
        except Exception as error:   # fails open, but never silently
            self.guard_failed(error)
            return None

    def guard_failed(self, error) -> None:
        """The runtime guard failed and let the command through (it fails open; RuntimeGuard.errors counts each).
        Logged with its traceback the first time in this process, so a guard that quietly does nothing is seen
        without flooding the log on every tool call."""
        if "runtime_guard" in self.failed:
            return
        self.failed.add("runtime_guard")
        try:
            from logging import getLogger

            getLogger(__name__).warning("Atlas Evolution runtime guard failed internally; failing open", exc_info=error)
        except Exception:   # logging must never reach the tool call
            pass

    def _remember_command(self, payload: dict) -> None:
        from .guard import TERMINAL_TOOLS

        call, args = payload.get("tool_call_id"), payload.get("args")
        command = args.get("command") if isinstance(args, dict) else None
        if payload.get("tool_name") in TERMINAL_TOOLS and isinstance(call, str) and 0 < len(call) <= 400 \
                and isinstance(command, str):
            with self.recent_lock:
                self.commands[call] = hashlib.sha256(command.encode("utf-8", "replace")).hexdigest()
                while len(self.commands) > 512:
                    self.commands.pop(next(iter(self.commands)))

    def _rewritten(self, payload: dict) -> bool:
        """True when the call that ran differs from the one the agent wrote: Hermes' middleware trace names a
        rewrite, or the command differs from the one pre_tool_call saw (another plugin's `modify` directive)."""
        call, args = payload.get("tool_call_id"), payload.get("args")
        trace = payload.get("middleware_trace")
        if isinstance(trace, list) and any(isinstance(item, dict) for item in trace):
            with self.recent_lock:
                if isinstance(call, str):
                    self.commands.pop(call, None)
            return True
        with self.recent_lock:
            written = self.commands.pop(call, None) if isinstance(call, str) else None
        command = args.get("command") if isinstance(args, dict) else None
        return written is not None and isinstance(command, str) and \
            hashlib.sha256(command.encode("utf-8", "replace")).hexdigest() != written

    def _remember_owner(self, session, owner: bool, origin: str = "unknown") -> None:
        if isinstance(session, str) and 0 < len(session) <= 400:
            with self.recent_lock:
                self.sessions[session] = (owner, origin)
                self.sessions.move_to_end(session)
                while len(self.sessions) > 2048:
                    self.sessions.popitem(last=False)

    def served_owner(self, session) -> bool:
        """Whether the session's latest turn served the owner. Unknown (no turn seen since the plugin loaded)
        counts as someone else: what depends on it is held back, never given away."""
        with self.recent_lock:
            entry = self.sessions.get(session) if isinstance(session, str) else None
        return entry is not None and entry[0] is True

    def origin_of(self, session) -> str | None:
        with self.recent_lock:
            entry = self.sessions.get(session) if isinstance(session, str) else None
        return entry[1] if entry is not None else None

    def after_tool(self, **payload):
        from .operations import coalesced

        try:
            with coalesced(self.home):   # the call's usage counters and its outcome: one write transaction
                self._after_tool(payload)
        except Exception:   # the shared write at the end of the batch: an unusable store never fails the tool call
            self.warn("tool_outcome")

    def _after_tool(self, payload: dict) -> None:
        from .signals import classify, reclassify, record_tool

        try:
            self.research.tool(payload)
            self.turns.tool(payload)
            observe(self.home, "tool", payload)
        except Exception:
            self.warn("tool")
        try:
            failure = classify(payload.get("tool_name"), payload.get("status"), payload.get("error_message"),
                               payload.get("result"), payload.get("args"))
            if self._rewritten(payload) and failure and failure["klass"] != "plugin_fault":
                failure = reclassify(failure, "rewritten_command")   # the agent did not write what failed
            # An excerpt is kept only when the reflector could use it: on, and lessons not off.
            session = payload.get("session_id")
            record_tool(self.home, payload, failure=failure, owner=self.served_owner(session), origin=self.origin_of(session),
                        sample=self.evolution["reflector"] and self.evolution["lessons"] != "off")
            call = payload.get("tool_call_id")
            if failure and isinstance(call, str) and 0 < len(call) <= 400:
                with self.recent_lock:
                    self.recent[call] = failure
                    while len(self.recent) > 256:
                        self.recent.pop(next(iter(self.recent)))
        except Exception:
            self.warn("tool_outcome")

    def before_turn(self, **payload):
        from .signals import origin_label, owner_turn, record_skill_correction, turn_surface

        owner = None
        try:
            surface = turn_surface(payload)
            origin = origin_label(payload)
            payload = {**payload, "surface": surface, "origin": origin}
            parent = payload.get("parent_session_id") or self.parent_of(payload.get("session_id"))
            owner = owner_turn(surface, payload.get("sender_id"), self.owners, origin=origin,
                               owner_platforms=self.owner_platforms,
                               parent_owner=surface == "subagent" and self.served_owner(parent))
            self._remember_owner(payload.get("session_id"), owner, origin)
            platform, session = payload.get("platform"), payload.get("session_id")
            if surface == "local" and isinstance(platform, str) and isinstance(session, str) and 0 < len(session) <= 400:
                with self.recent_lock:   # the session a /new of this surface ends (session_reset)
                    _keep(self.last_local, platform[:64], session, limit=16)
        except Exception:
            pass
        sensor = self.sensor   # apply_settings may set it to None on another thread meanwhile
        try:
            if sensor is not None and {"correction", "reask"} & set(sensor.observe(payload)):
                # The owner is correcting the previous turn: count it against the skills that turn loaded.
                record_skill_correction(self.home, self.turns.recent_skills(payload.get("session_id")))
        except Exception:
            pass   # a feedback counter that cannot be written never costs the turn its measurement
        for tracker in (self.research, self.turns):
            try:
                tracker.start(payload)
            except Exception:
                pass
        return self._owner_context(payload.get("session_id"), owner)

    def _owner_context(self, session, owner):
        """The lessons a chat's prompt left out, once per chat session, as this turn's context, when its sender
        turned out to be the owner (owner_sender_ids).

        Hermes builds a chat's prompt before its first message says who is writing, so it gets template lessons only.
        A context returned by pre_llm_call is added to this turn's user message (never the system prompt), stored with
        it and sent again on every later turn, so once per session is enough, and only in a direct message: a group, a
        channel or a thread may be one session for every participant (Hermes' thread_sessions_per_user is off by
        default), and someone who joins later would find the lessons in the history. The chat type is the one Hermes
        bound to this turn (gateway.session_context); unknown means no. Never in a session someone else wrote in, never
        where this process did not build the prompt itself (it cannot tell what that prompt had), bounded
        (lessons.owner_lessons). A turn another plugin vetoes still counts as given: that is not told apart cheaply."""
        try:
            if not isinstance(session, str) or not session:
                return None
            with self.recent_lock:
                if session not in self.prompted:
                    return None   # not a chat this process built the prompt of
                if owner is not True:
                    _keep(self.strangers, session)   # from now on this chat never gets them
                    return None
                if not self.prompted[session] or session in self.strangers or session in self.given:
                    return None
            if _chat_type() != "dm":
                return None
            with self.recent_lock:
                if session in self.given:
                    return None
                _keep(self.given, session)
            if self.evolution["lessons"] == "off":
                return None
            from . import lessons as playbook
            from .experience import record
            from .operations import database

            chosen: list = []
            with database(self.home, timeout=PLAYBOOK_BUSY_SECONDS) as conn:
                text = playbook.owner_lessons(conn, lang=self.lang, chosen=chosen)
            if not text:
                return None
            stamp = time.time()
            record(self.home, lambda conn: playbook.shown(conn, chosen, now=stamp))
            return {"context": text}
        except Exception:   # a turn never waits for, or fails on, its lessons
            return None

    def after_turn(self, **payload):
        try:
            self.research.finish(payload)
        except Exception:
            return None
        finally:
            self.poke()

    def turn_ended(self, **payload):
        try:
            self.turns.finish(payload)
        except Exception:
            self.warn("turn")
        finally:
            self.poke()

    def skill_event(self, **payload):
        from .signals import record_skill_event

        try:
            self.turns.skill(payload)
            record_skill_event(self.home, payload)
        except Exception:
            self.warn("skill")

    def poke(self, **_payload):
        """Nudge the autopilot: O(1) unless a check is due. Also the kanban tick of the gateway's own profile.
        A change of the settings (the dashboard panel, the settings form, `hermes config set`) is picked up here."""
        try:
            self.refresh_settings()
        except Exception:
            pass
        try:
            if self.autopilot is not None:
                self.autopilot.poke()
        except Exception:
            pass
        return None

    # ── settings that apply while Hermes runs ────────────────────────────────────────────────────────────

    def refresh_settings(self, *, force: bool = False, read=None) -> bool:
        """Apply a change of the settings without restarting the gateway. At most every SETTINGS_CHECK_SECONDS it
        compares the modification times of config.yaml and settings.json (two stat calls); only a change reads
        them again. Returns True when new settings were applied.

        Who counts as the owner, the lessons mode, the reflector's limit, switching the reflector off, the language,
        owner signals, the slow-turn threshold and the autopilot interval apply at once. The runtime guard and tool
        hints decide which hooks Hermes calls, and the reflector's model access (its auxiliary task) exists only when
        the plugin loaded with it on, so a change of those (the reflector: switched on after a load with it off)
        waits for the next load of the plugin: it is recorded (`settings_reload`) for `status` and the panel to say
        so, against what this load registered."""
        clock = time.monotonic()
        if not force and clock - self._settings_checked < SETTINGS_CHECK_SECONDS:
            return False
        if not self._settings_lock.acquire(blocking=False):
            return False
        try:
            self._settings_checked = clock
            stamp = _settings_stamp(self.home)
            if not force and stamp == self._settings_stamp:
                return False
            runtime = (read or _read_hermes_config)(self.home)
            if runtime is None:   # unreadable right now (or no YAML parser): try again at the next change
                return False
            self._settings_stamp = stamp
            from .evolve import options

            config, problems = safe_settings(self.home, runtime)
            evolution = options(config, runtime, strict=False, problems=problems)
            self.apply_settings(config, evolution)
            return True
        finally:
            self._settings_lock.release()

    def tick(self) -> None:
        """The idle clock's nudge (every 10 minutes): settings first, this process's cost per hook written; then, with
        the learning pass by hand only (autopilot_minutes 0), the retention the pass would run, once a UTC day."""
        self.refresh_settings()
        self.save_cost()
        autopilot = getattr(self, "autopilot", None)
        if autopilot is None or autopilot.interval_s:
            return
        now = time.time()
        if getattr(self, "_retained_day", None) == int(now // 86_400):
            return
        self._retained_day = int(now // 86_400)
        try:
            from .operations import _prune, database
            from .reflector import prune_samples

            with database(self.home) as conn:
                _prune(conn, now)
                prune_samples(conn, now=now)
        except Exception:   # busy now, or the store is unusable: the next day tries again
            self.warn("retention")

    def save_cost(self) -> None:
        try:
            from .selfcost import persist
            from .status import process_kind

            persist(self.home, self.meter, kind=process_kind())
        except Exception:   # a measurement that cannot be written is simply not shown
            pass

    def apply_settings(self, config: dict, evolution: dict) -> None:
        from .signals import FeedbackSensor

        self.forget_playbook()   # the language, the lessons mode or who the owner is may have changed

        with self.recent_lock:
            self.config, self.evolution = config, evolution
            self.advice = config.get("agent_advice") is True
            self.owners = frozenset(str(item) for item in evolution["owner_sender_ids"] if item not in (None, ""))
            self.owner_platforms = frozenset(evolution["owner_platforms"])
        self.turns.slow_ms = evolution["slow_turn_seconds"] * 1000
        if not evolution["feedback_signals"]:
            self.sensor = None
        elif self.sensor is None:
            self.sensor = FeedbackSensor(self.home, owner_sender_ids=evolution["owner_sender_ids"],
                                         owner_platforms=evolution["owner_platforms"])
        else:
            self.sensor.owners = frozenset(evolution["owner_sender_ids"])
            self.sensor.platforms = frozenset(evolution["owner_platforms"])
        self.hints.lang = self.lessons_tool.lang = evolution["language"]
        if self.guard is not None:
            self.guard.lang = evolution["language"] if evolution["language"] in ("en", "pt") else "en"
        if self.autopilot is not None:
            self.autopilot.interval_s = evolution["autopilot_minutes"] * 60
            if self.autopilot.interval_s and self.clock_allowed:
                self.autopilot.start_clock()   # turned on while running: a no-op when the clock already runs
        # Against what this load registered, not the previous settings: a change of another setting meanwhile must not
        # hide one that still waits, and a value put back as it was loaded waits for nothing.
        waiting = sorted(key for key in RELOAD_KEYS if self.loaded.get(key) != evolution.get(key))
        if evolution["reflector"] and self.reflect is None and self.reflector_possible:
            waiting.append("reflector")   # loaded with it off: its auxiliary task is registered at the next load
        if evolution["lessons"] != "off" and not self.section_registered:
            waiting.append("lessons")   # loaded with lessons off: their prompt section appears at the next load
        if self.advice and not getattr(self, "tools_registered", True):
            waiting.append("agent_tools")   # the agent's tools are registered at load only
        try:
            from .experience import record

            body = json.dumps(waiting)
            record(self.home, lambda conn: conn.execute("INSERT OR REPLACE INTO cursor VALUES ('settings_reload', ?)",
                                                        (body,)))
        except Exception:
            self.warn("settings")

    # ── what reaches the agent and the owner ─────────────────────────────────────────────────────────

    def with_lesson(self, **payload):
        """Add the matching lesson to a failing tool result. Any doubt leaves the result untouched."""
        from .hints import attach
        from .signals import classify

        try:
            if not self.evolution["tool_hints"] or self.evolution["lessons"] == "off" or \
                    payload.get("status") not in ("error", "blocked"):
                return None   # off by default: the first string of all plugins wins, so a hint discards theirs
            call, session = payload.get("tool_call_id"), payload.get("session_id")
            if not isinstance(session, str) or not session.strip():
                return None   # without a session there is no "once per session"
            with self.recent_lock:
                failure = self.recent.pop(call, None) if isinstance(call, str) and call else None
            failure = failure or classify(payload.get("tool_name"), payload.get("status"), payload.get("error_message"),
                                          payload.get("result"), payload.get("args"))
            if not failure:
                return None
            entry = self.hints.lookup(failure["signature"], session, templates_only=not self.served_owner(session))
            if entry is None:
                return None
            changed = attach(payload.get("result"), entry[1], self.lang)
            if changed is not None:
                self.hints.count(entry[0])
            return changed
        except Exception:   # a transform hook must never break a tool result
            return None

    def playbook_section(self, session_info):
        """Read the playbook, then count the lessons shown through the queued writer: a busy database
        (a `learn` run, another process) must never leave a new session without its lessons. A session counts
        once, however often Hermes renders its prompt again (compaction, a fork, an inspection)."""
        from . import lessons as playbook
        from .experience import record
        from .operations import database
        from .signals import session_source, surface_of

        try:
            if self.evolution["lessons"] == "off":
                return ""   # switched off while running: the section stays registered, empty
            shown: list = []
            info = session_info if hasattr(session_info, "get") else {}
            platform = info.get("platform")
            # A chat where someone else may be on the other side gets template lessons only: the prompt is built
            # before the first message, so who is writing is not known yet. A delegated child gets what its
            # parent got: subagent_start named the parent before the child's prompt was built.
            origin = session_source(platform) or platform
            surface = surface_of(platform, origin)
            if surface == "subagent":
                others = not self.served_owner(self.parent_of(info.get("session_id")))
            else:
                others = surface == "gateway" and not self.served_owner(info.get("session_id")) \
                    and not (isinstance(origin, str) and origin.lower() in self.owner_platforms)
            # Rendered at most every PLAYBOOK_TTL_SECONDS per kind of session: a prompt was built on every turn,
            # each time opening the database. Lessons change in the learning pass or by the owner's decision, so a
            # change reaches new prompts within the TTL (at once when this process made it).
            key = (self.lang, bool(others))
            with self.recent_lock:
                cached = self._playbook.get(key)
                generation = self._playbook_generation
            if cached is not None and 0 <= time.monotonic() - cached[0] < PLAYBOOK_TTL_SECONDS:
                text, shown = cached[1], list(cached[2])
            else:
                # Its own connection: WAL readers never wait for a writer. The short busy wait bounds the rare
                # one-time schema step, so a prompt is never held up by this section; a busy database leaves it empty.
                with database(self.home, timeout=PLAYBOOK_BUSY_SECONDS) as conn:
                    text = playbook.playbook(conn, lang=self.lang, chosen=shown, templates_only=others)
                with self.recent_lock:
                    if generation == self._playbook_generation:   # nothing changed the lessons meanwhile
                        self._playbook[key] = (time.monotonic(), text, tuple(shown))
            session = info.get("session_id")
            if isinstance(session, str) and session:
                with self.recent_lock:
                    seen = session in self.counted
                    _keep(self.counted, session)
                    if surface == "gateway" and len(session) <= 400:
                        _keep(self.prompted, session, bool(others))   # what this chat's prompt holds (_owner_context)
                if seen:
                    shown = []
            if shown:
                stamp = time.time()
                record(self.home, lambda conn: playbook.shown(conn, shown, now=stamp))
            return text
        except Exception:
            return ""

    def session_finalized(self, **payload):
        """A short notice about decisions waiting for the owner: once a day, and only on the owner's own
        surfaces (CLI, TUI, Desktop). Never in a gateway chat, where the other side may be someone else."""
        from .signals import surface_of
        from .status import pending_notice

        try:
            if payload.get("parent_session_id") or surface_of(payload.get("platform")) != "local":
                return None
            reason = payload.get("reason")
            if reason is not None and reason not in _SHOWN_END_REASONS:
                return None   # Hermes drops this text (no viewer is left): the day's notice waits for one it shows
            return pending_notice(self.home, now=time.time(), lang=self.lang)
        except Exception:   # a notice is a convenience; it must never disturb the end of a session
            return None

    # ── agent tools (off unless the operator enables agent_tools) ────────────────────────────────────

    def in_language(self, payload: str) -> str:
        from .i18n import localize

        try:
            return json.dumps(localize(json.loads(payload), self.lang), ensure_ascii=False)
        except (TypeError, ValueError):
            return payload

    # ── events on Hermes' bus (ctx.emit): ids and numbers only, never text ─────────────────────────────

    def pass_events(self, result: dict) -> None:
        """After a learning pass: `hermes-atlas-evolution:lesson_activated`, `:lesson_retired` and `:failure_spike`
        (a failure far above its usual day, once per UTC day per failure in this process). Fail-silent."""
        emit = self.emit
        if emit is None or not isinstance(result, dict):
            return
        events = [("lesson_activated", {"lesson_id": item["id"], "class": item.get("class")})
                  for item in result.get("new_lessons") or () if isinstance(item, dict) and item.get("status") == "active"
                  and type(item.get("id")) is int]
        events += [("lesson_retired", {"lesson_id": item["id"], "verdict": item.get("verdict"), "basis": item.get("basis")})
                   for item in result.get("lesson_effects") or () if isinstance(item, dict) and item.get("retired")
                   and type(item.get("id")) is int]
        try:
            from .experience import experience, utc_day
            from .insights import spikes

            now = result.get("now") if isinstance(result.get("now"), (int, float)) else time.time()
            with experience(self.home) as conn:
                found = spikes(conn, now=now)
            day, told = self._spikes_told
            told = told if day == utc_day(now) else frozenset()
            fresh = [item for item in found if item["signature"] not in told]
            self._spikes_told = (utc_day(now), told | {item["signature"] for item in fresh})
            events += [("failure_spike", {"signature": item["signature"], "class": item["class"], "today": item["today"],
                                          "usual_per_day": item["usual_per_day"]}) for item in fresh]
        except Exception:   # the spikes are a reading of the database: busy now, they wait for the next pass
            pass
        for name, payload in events[:50]:
            try:
                emit(name, payload)
            except Exception:   # an older bus, a full queue: the event is simply not sent
                pass

    def playbook_tool(self, args, **context):
        if not self.advice:
            return self.in_language(json.dumps({"error": "Playbook access has not been enabled for this Hermes profile."}))
        if not self.served_owner(context.get("session_id")):
            return self.in_language(json.dumps({"error": _OWNER_ONLY_TOOL}))
        return self.in_language(self.lessons_tool.handle(args, **context))


def _runtime_config() -> dict:
    try:
        from hermes_cli.config import load_config_readonly

        return load_config_readonly() or {}
    except Exception:   # the plugin still loads with its own settings file
        return {}


def _repair_settings(ctx, runtime_config) -> None:
    """`hermes config set ... settings.lessons off`, and a bare `off` in config.yaml, store the boolean false, which
    Hermes then reports on every load ("lessons should be str") and its settings form shows as no choice. The plugin
    reads it as "off" already; here it writes the string back once, with Hermes' own writer for this plugin's
    settings (ctx.set_config), so the form and the schema check see a declared choice. Never fails the load."""
    from .cli import PLUGIN_ID

    setter = getattr(ctx, "set_config", None)
    plugins = runtime_config.get("plugins") if isinstance(runtime_config, dict) else None
    entries = plugins.get("entries") if isinstance(plugins, dict) else None
    entry = entries.get(PLUGIN_ID) if isinstance(entries, dict) else None
    values = entry.get("settings") if isinstance(entry, dict) else None
    if not callable(setter) or not isinstance(values, dict) or values.get("lessons") is not False:
        return
    try:
        setter("lessons", "off")
    except Exception as error:   # a managed install, a locked config: the plugin keeps reading false as "off"
        from logging import getLogger

        getLogger(__name__).info("Atlas Evolution could not store lessons=off as text: %s", type(error).__name__)


def _reflector(ctx, evolution: dict):
    """The model access of the reflector through Hermes, or None (reflector off, or an older host).

    Only with the reflector on at load: its auxiliary task shows in `hermes model` and the dashboard's model list
    as a task to configure, which it should not while the reflector is off. Switched on later, it waits for the next
    load (apply_settings records it), and each pass reads the setting again, so switching it off applies at once."""
    if not evolution["reflector"] or not callable(getattr(ctx, "register_auxiliary_task", None)):
        return None
    from logging import getLogger

    from .reflector import host_caller

    try:
        ctx.register_auxiliary_task(
            "evolution_reflector", display_name="Atlas Evolution reflector",
            description="Drafts lessons from recurring tool failures. Each draft waits for operator approval.",
            inherit_from="compression")
        return host_caller(ctx)
    except Exception:   # an older host without plugin LLM access: the loop runs without drafts
        getLogger(__name__).warning("Atlas Evolution reflector is unavailable on this Hermes build")
        return None


def _operator_cli(home: Path, reflect):
    def operator_cli(args):
        import hermes_cli.config as hermes_config

        execute(args, home, runtime_config=hermes_config.load_config_readonly() or {}, reflect=reflect)

    return operator_cli


def _start(plugin: _Plugin) -> None:
    """Mark when observation starts (the backfill reads Hermes' history only before it) and, in a gateway,
    which plugin version this process loaded."""
    from .backfill import mark_observed
    from .status import record_runtime

    for name, step in (("start", mark_observed), ("runtime", record_runtime)):
        try:
            step(plugin.home, now=time.time())
        except Exception:
            plugin.warn(name)


def _preload() -> None:
    """In a long-running host (a gateway, the dashboard), import every module of the plugin soon after it loads.

    Hermes replaces a plugin's files when it updates it (by hand, or in another profile by the Mode auto) while a
    running gateway keeps the code it loaded. Most modules are imported on first use, so one first used after an
    update would come from the new files and run next to old ones. Loaded at start, the process runs one version
    until it is restarted, and `status` says when that version is behind the files."""
    from .status import process_kind

    if process_kind() == "cli":
        return   # a short-lived process ends before an update could matter

    def load():
        import importlib
        import pkgutil

        package = sys.modules.get(__package__)
        for info in pkgutil.iter_modules(getattr(package, "__path__", ()) or ()):
            if info.name.startswith("_"):
                continue   # __main__ runs the standalone command line
            try:
                importlib.import_module(f"{__package__}.{info.name}")
            except Exception:   # a module that cannot load here is imported, and fails, where it is used
                pass

    try:
        threading.Thread(target=load, name="hermes-atlas-evolution-preload", daemon=True).start()
    except RuntimeError:   # the interpreter is shutting down
        pass


def _register_guard(ctx, plugin: _Plugin) -> None:
    """pre_tool_call, only with runtime_guard on: veto what would take Hermes down, and remember what the agent
    wrote (a rewrite by another plugin is then told from the agent's own command).

    Hermes blocks a tool whose pre_tool_call fails or stalls, so the hook is not registered at all when the guard
    is off. Not in a plugin host (plugins.isolation: host) either: there every pre_tool_call is a call into the
    host, and a host that crashed would block every tool of the profile."""
    import os

    if os.environ.get("HERMES_PLUGIN_HOST_PROCESS") == "1":
        from logging import getLogger

        getLogger(__name__).info("Atlas Evolution runs in a plugin host here: the runtime guard stays off so a crashed "
                                 "host cannot block every tool of this profile")
        return
    if not plugin.evolution["runtime_guard"]:
        return
    from hermes_constants import get_default_hermes_root

    from .guard import RuntimeGuard, own_process, protected_paths, protected_trees
    from .signals import record_guard

    home = plugin.home
    plugin.guard = RuntimeGuard(protected=protected_paths(hermes_root=get_default_hermes_root(home=home), profile_home=home),
                                trees=protected_trees(), own=(Path(__file__).resolve().parent.parent,),
                                process=own_process(), home=Path.home(), profile=home, lang=plugin.lang,
                                on_block=lambda kind: record_guard(home, kind), on_error=plugin.guard_failed)
    ctx.register_hook("pre_tool_call", plugin.meter.wrap("pre_tool_call", plugin.pre_tool))


def _register_session_marker(ctx) -> None:
    """Every shell an agent session spawns carries ATLAS_EVOLUTION_AGENT, so `hermes evolution` run by the
    agent can tell it is not the operator and refuses to approve or activate anything."""
    register_env = getattr(ctx, "register_session_env", None)
    if not callable(register_env):
        return
    try:
        register_env(["ATLAS_EVOLUTION_AGENT"], lambda session_id: {"ATLAS_EVOLUTION_AGENT": "1"})
    except Exception:   # the session id marker set by Hermes still guards the commands
        from logging import getLogger

        getLogger(__name__).warning("Atlas Evolution could not mark agent shells; the session id still guards decisions")


def register(ctx):
    from logging import getLogger

    from hermes_constants import get_hermes_home

    from .agent_tools import LESSONS_TOOL
    from .autopilot import Autopilot, runs_passes
    from .cli import legacy_install
    from .evolve import options

    home = get_hermes_home()
    runtime_config = _runtime_config()
    old = legacy_install(home, runtime_config)
    if old:   # the copy installed under the old id is still enabled: register nothing, never count twice
        getLogger(__name__).warning(
            "Atlas Evolution is installed twice in this profile, under its old name %s and hermes-atlas-evolution. "
            "The new copy stays inactive until the old one is removed: hermes plugins remove %s", old, old)
        return
    _repair_settings(ctx, runtime_config)
    # Unreadable settings.json: keep measuring with the values from Hermes' form, but show nothing to the
    # agent until the file is fixed.
    config, problems = safe_settings(home, runtime_config)
    evolution = options(config, runtime_config, strict=False, problems=problems)
    if problems:
        getLogger(__name__).warning("Atlas Evolution ignored invalid settings (%s); lessons=%s until they are fixed",
                                    ", ".join(problems), evolution["lessons"])
    plugin = _Plugin(home, config, evolution)
    plugin.reflect = _reflector(ctx, evolution)
    # Loaded with the reflector off where the host has auxiliary tasks: switched on, it works after the next load.
    plugin.reflector_possible = not evolution["reflector"] and callable(getattr(ctx, "register_auxiliary_task", None))
    emit = getattr(ctx, "emit", None)
    plugin.emit = emit if callable(emit) else None

    def learning_pass():
        try:
            return autopilot_pass(home, plugin.reflect, cancelled=lambda: plugin.autopilot.stopped,
                                  reflector_reload=plugin.reflector_possible,
                                  events=plugin.pass_events if plugin.emit is not None else None)
        finally:
            plugin.forget_playbook()   # lessons may have been activated or retired: new prompts show them now

    plugin.autopilot = Autopilot(home, learning_pass, interval_minutes=evolution["autopilot_minutes"])
    # Only a process that stays up claims the pass: a one-shot run would end in its middle (runs_passes).
    plugin.autopilot.claims = runs_passes()
    _start(plugin)
    _preload()

    measured = plugin.meter.wrap   # every hook is timed where it runs: `status` says what the plugin costs the turn
    for hook, event in (("post_api_request", "api"), ("api_request_error", "api_error"), ("post_auxiliary_call", "aux")):
        ctx.register_hook(hook, measured(hook, plugin.observer(event)))
    for hook in NATIVE_HOOKS:
        ctx.register_hook(hook, measured(hook, plugin.native(hook)))
    _register_guard(ctx, plugin)
    ctx.register_hook("on_session_finalize", measured("on_session_finalize", plugin.session_finalized))
    ctx.register_hook("on_kanban_dispatch_tick", plugin.poke)   # the gateway's own profile: a 60-second clock
    ctx.register_hook("post_tool_call", measured("post_tool_call", plugin.after_tool))
    ctx.register_hook("pre_llm_call", measured("pre_llm_call", plugin.before_turn))
    ctx.register_hook("post_llm_call", measured("post_llm_call", plugin.after_turn))
    ctx.register_hook("on_session_end", measured("on_session_end", plugin.turn_ended))
    ctx.register_hook("on_skill_lifecycle", measured("on_skill_lifecycle", plugin.skill_event))
    # /new, /reset and a deleted session: what the plugin keeps in memory about a session that ended goes with it
    ctx.register_hook("on_session_reset", measured("on_session_reset", plugin.session_reset))
    ctx.register_hook("on_session_delete", measured("on_session_delete", plugin.session_deleted))
    lessons_on = evolution["lessons"] != "off"
    if lessons_on and evolution["tool_hints"]:
        # Only with tool_hints on: while any plugin listens, Hermes reads every tool result for this hook and runs
        # it in a thread of its own. (`hermes plugins validate` notes it as declared but not registered.)
        ctx.register_hook("transform_tool_result", measured("transform_tool_result", plugin.with_lesson))
    if lessons_on:   # with lessons off at load there is no surface at all; switching them off later empties it
        ctx.register_system_prompt_section("hermes-atlas-evolution.playbook",
                                           measured("prompt_section", plugin.playbook_section), max_chars=1400)
        plugin.section_registered = True
    _register_skills(ctx)
    _register_session_marker(ctx)
    on_unload = getattr(ctx, "on_unload", None)
    if callable(on_unload):
        from .operations import close_observers

        on_unload(close_observers)            # write queued counters and release the shared connection
        on_unload(plugin.autopilot.stop)      # stop the idle clock of this load before a reload starts another
        on_unload(plugin.save_cost)           # what this load measured, before it goes
        plugin.clock_allowed = True
        plugin.autopilot.on_tick = plugin.tick
        plugin.autopilot.start_clock()        # only where it can be stopped: a host with on_unload
    plugin.tools_registered = plugin.advice
    if plugin.advice:
        # Only when the owner turned the agent's tools on (`agent_tools`): a toolset registered while off reads in
        # `hermes doctor` as "system dependency not met" on every Hermes. Turning them on later takes effect at the
        # next load of the plugin (`hermes gateway restart`); turning them off works at once (check_fn).
        ctx.register_tool(name=LESSONS_TOOL["name"], toolset="hermes-atlas-evolution", schema=LESSONS_TOOL,
                          handler=plugin.playbook_tool, check_fn=lambda: plugin.advice)
    import os

    # A plugin host (plugins.isolation: host) skips CLI commands and warns on every load: the `evolution` command is
    # registered where the terminal runs Hermes in process, and the standalone runner (python -m) serves the rest.
    if os.environ.get("HERMES_PLUGIN_HOST_PROCESS") != "1":
        ctx.register_cli_command("evolution", CLI_HELP, setup,
                                 _operator_cli(home, plugin.reflect))
