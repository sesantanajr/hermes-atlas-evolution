"""The evolution pass on its own: hooks nudge it, a background thread runs it at most once per interval.

Nothing here runs on the turn path. `Autopilot.poke` compares two clocks until a check is due (once a
minute at most). The check runs in a daemon thread that claims the pass with a compare-and-set shared by
every process of the profile, so a gateway, a CLI and a cron runner never run it twice. A process that
dies in the middle of a pass leaves a lease that expires, and the pass is tried again.

A gateway that serves several profiles keeps the active profile in a context variable that new threads
do not inherit, so the thread pins the plugin's own profile before anything reads Hermes' configuration.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

CHECK_SECONDS = 60          # how often a hook may start a check
CLOCK_FIRST_SECONDS = 120   # the clock's first nudge after the plugin loads
CLOCK_PERIOD_SECONDS = 600  # then every 10 minutes, so an idle profile still gets its passes
LEASE_SECONDS = 15 * 60     # a pass that has not renewed its lease by then is presumed dead
PASS_RENEW_LIMIT = 3        # renewals of a running pass's lease on a timer: past ~2 leases a stuck pass lets another run
RETRY_SECONDS = 10 * 60     # a failed pass is tried again after this, not after a full interval
SKEW_SECONDS = 5 * 60       # a last pass up to this far "in the future" finished after the caller read its clock

logger = logging.getLogger(__name__)


# Hermes' top-level flags that take a value (hermes_cli/_parser.py _VALUE_FLAGS_FALLBACK, and -p/--profile, which
# main strips before argparse): the word after one is that value, not the subcommand.
_VALUE_FLAGS = frozenset({"-z", "--oneshot", "-m", "--model", "--provider", "--reasoning", "-t", "--toolsets", "-r",
                          "--resume", "-s", "--skills", "--usage-file", "--in", "--cpu-profile", "-p", "--profile"})
# A run that answers and exits: `hermes -z PROMPT`, `hermes chat -q ... --oneshot`, `-Q`, `--query-file`.
_ONE_SHOT = frozenset({"-z", "--oneshot", "-Q", "--quiet", "--query-file"})
# Subcommands that serve sessions until they are stopped: an editor's agent server, the backend of the Desktop app and
# of remote clients (`hermes serve`, which runs their chats in this process).
_SERVING = frozenset({"acp", "serve"})


def _terminal() -> bool:
    try:
        return sys.stdin is not None and sys.stdout is not None and sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError, OSError):   # a closed or replaced stream
        return False


def runs_passes(argv=None, *, terminal: bool | None = None) -> bool:
    """Whether this process may claim the learning pass: one that stays up (a gateway, the dashboard, the backend of
    the TUI or the Desktop, an editor's ACP server, an interactive CLI in a terminal). A run that ends in seconds
    (`hermes -z`, a scripted or piped `hermes chat -q`, a kanban worker, any other subcommand) would claim the pass,
    exit in its middle and push the next attempt RETRY_SECONDS away, again and again: it only reads the settings.

    A plugin host (`plugins.isolation: host`: `python -m hermes_cli.plugin_host_child`, HERMES_PLUGIN_HOST_PROCESS=1)
    lives as long as the Hermes that started it and is where the plugin runs then: without it no pass, and no
    retention, ever ran in such a profile."""
    from .status import process_kind

    if process_kind() != "cli":
        return True
    argv = [str(item) for item in (sys.argv if argv is None else argv)[:64]]
    if os.environ.get("HERMES_PLUGIN_HOST_PROCESS") == "1" or argv and "plugin_host_child" in argv[0]:
        return True
    if argv and "tui_gateway" in argv[0].replace("\\", "/"):
        return True   # python -m tui_gateway.entry: the TUI's and the Desktop's backend, up while they are open
    subcommand, value, previous = None, False, ""
    for word in argv[1:]:
        if word in _ONE_SHOT or word.startswith(("--oneshot=", "--query-file=")) or \
                word.startswith("-z") and not word.startswith("--") or word == "--format=stream-json" or \
                previous == "--format" and word == "stream-json":   # stream-json implies --quiet: it answers and exits
            return False
        previous = word
        if value:
            value = False
        elif word.startswith("-"):
            value = word in _VALUE_FLAGS   # `--flag=value` carries its own
        elif subcommand is None:
            subcommand = word
    if subcommand in _SERVING:
        return True
    if subcommand not in (None, "chat"):
        return False
    return _terminal() if terminal is None else terminal


def _number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _lease(conn) -> tuple[str, float]:
    row = conn.execute("SELECT value FROM cursor WHERE key='loop_lease'").fetchone()
    owner, _, until = (row[0] if row else "").partition("|")
    return owner, _number(until)


def claim(home: Path, *, now: float, interval_s: float, owner: str) -> bool:
    """Claim the next pass of this profile. True when the caller should run it now."""
    from .experience import experience

    with experience(home) as conn:
        conn.execute("BEGIN IMMEDIATE")   # read, check and write as one step for every process
        row = conn.execute("SELECT value FROM cursor WHERE key='last_cycle'").fetchone()
        last = _number(row[0]) if row else 0.0
        if 0 <= now - last < interval_s:
            return False
        if interval_s and 0 < last - now <= SKEW_SECONDS:
            return False   # another process finished a pass after this caller read its clock
        holder, until = _lease(conn)
        if holder and until > now:
            return False
        conn.execute("INSERT OR REPLACE INTO cursor VALUES ('loop_lease', ?)", (f"{owner}|{now + LEASE_SECONDS!r}",))
        return True


def renew(home: Path, *, now: float, owner: str) -> bool:
    """Extend the lease of the pass `owner` runs to LEASE_SECONDS from `now`. False when it is not its own any more
    (it expired and another process took the pass)."""
    from .experience import experience

    with experience(home) as conn:
        conn.execute("BEGIN IMMEDIATE")
        holder, _ = _lease(conn)
        if holder != owner:
            return False
        conn.execute("INSERT OR REPLACE INTO cursor VALUES ('loop_lease', ?)", (f"{owner}|{now + LEASE_SECONDS!r}",))
        return True


class _Lease:
    """Keeps the lease of a running pass: renewed every LEASE_SECONDS / 3 on a thread of its own, at most
    PASS_RENEW_LIMIT times, so a pass that outlives one lease (a slow systemd, a big quick_check) is not run a second
    time by another process, and one stuck for good still lets another process take over. `lost` turns True when
    another process holds the pass after all; the pass's `cancelled` check then stops it."""

    def __init__(self, home: Path, owner: str, *, wall=time.time):
        self.home, self.owner, self.wall = home, owner, wall
        self.lost = False
        self.renewed = wall()   # the claim wrote the first lease
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    def renew(self) -> bool:
        try:
            held = renew(self.home, now=self.wall(), owner=self.owner)
        except (sqlite3.Error, OSError, ValueError):
            return not self.lost   # busy now: the lease still has two thirds of its time
        if held:
            self.renewed = self.wall()
        else:
            self.lost = True
        return held

    def __enter__(self):
        def keep():
            for _ in range(PASS_RENEW_LIMIT):
                if self._done.wait(LEASE_SECONDS / 3) or not self.renew():
                    return

        self._thread = threading.Thread(target=keep, name="hermes-atlas-evolution-lease", daemon=True)
        try:
            self._thread.start()
        except RuntimeError:   # the interpreter is shutting down
            self._thread = None
        return self

    def __exit__(self, *_exc):
        self._done.set()
        return False


def finish(home: Path, *, now: float, owner: str, ok: bool, interval_s: float, started: float | None = None,
           summary=None, error: str = "") -> None:
    """Record the end of a pass. A failure moves the next attempt to RETRY_SECONDS from now."""
    from .experience import experience

    with experience(home) as conn:
        conn.execute("BEGIN IMMEDIATE")
        holder, _ = _lease(conn)
        if holder != owner:
            return   # not ours any more: released at exit, or expired and taken by another process
        conn.execute("DELETE FROM cursor WHERE key='loop_lease'")
        if ok or interval_s > RETRY_SECONDS:   # a failed manual pass leaves the schedule as it was
            stamp = now if ok else now - interval_s + RETRY_SECONDS
            conn.execute("INSERT OR REPLACE INTO cursor VALUES ('last_cycle', ?)", (repr(stamp),))
        status = {"at": now, "ok": ok, "seconds": round(max(0.0, now - (started or now)), 3),
                  "summary": summary if isinstance(summary, dict) else None, "error": error[:80]}
        conn.execute("INSERT OR REPLACE INTO cursor VALUES ('loop_status', ?)", (json.dumps(status),))


def run_exclusive(home: Path, run, *, wait_s: float = 30.0, now=time.time):
    """Run a pass the operator asked for, never alongside another pass of the profile.

    Waits up to `wait_s` for a running pass to finish. The manual pass then counts as the latest one, so the
    autopilot does not repeat it right away."""
    owner = f"manual-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    deadline = time.monotonic() + wait_s
    while not claim(home, now=now(), interval_s=0, owner=owner):
        if time.monotonic() >= deadline:
            raise ValueError("Another learning pass is running in this profile. Try again in a minute.")
        time.sleep(0.5)
    started = now()
    try:
        with _Lease(home, owner, wall=now):
            result = run()
    except BaseException as problem:
        finish(home, now=now(), owner=owner, ok=False, interval_s=0, started=started, error=type(problem).__name__)
        raise
    finish(home, now=now(), owner=owner, ok=True, interval_s=0, started=started)
    return result


@contextmanager
def pinned_profile(home: Path):
    """Make Hermes resolve this plugin's profile in the current thread: its home and its secrets.

    A gateway that serves several profiles keeps both in context variables that a new thread does not
    inherit. Without the secret scope, a model call of the reflector would use the gateway's own profile
    keys (or fail), not this profile's."""
    try:
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    except ImportError:   # outside Hermes: nothing to pin
        yield
        return
    refused = object()
    try:
        home_token = set_hermes_home_override(str(home))
    except Exception:   # a hosted (pooled) scope refuses a switch (ScopeSwitchRefused): it is already pinned
        home_token = refused
    if home_token is refused:
        yield
        return
    secret_token = reset_scope = None
    try:
        from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope

        secret_token = set_secret_scope(build_profile_secret_scope(Path(home)), profile_home=str(home))
        reset_scope = reset_secret_scope
    except Exception:   # an older Hermes without secret scopes: the home override is all there is
        pass
    try:
        yield
    finally:
        if reset_scope is not None:
            reset_scope(secret_token)
        reset_hermes_home_override(home_token)


class Autopilot:
    """Run `run()` at most once per interval for the profile, in a daemon thread started by `poke()`."""

    def __init__(self, home: Path, run, *, interval_minutes: int, clock=time.monotonic, wall=time.time):
        self.home = Path(home)
        self.run = run
        self.interval_s = max(0, int(interval_minutes)) * 60
        self.clock, self.wall = clock, wall
        self._due = 0.0
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._owner = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._warned = False
        self._running = False
        self._stop = threading.Event()
        self._clock: threading.Thread | None = None
        self._exit_hooked = False
        self.on_tick = None   # called by the clock before each nudge (the plugin's settings refresh)
        self.claims = True    # False in a short-lived process (runs_passes): it never claims the pass
        self._lease: _Lease | None = None   # the lease of the pass running now

    def start_clock(self, *, first: float = CLOCK_FIRST_SECONDS, period: float = CLOCK_PERIOD_SECONDS) -> bool:
        """A daemon timer that nudges the pass while the process is idle.

        Turns nudge the pass too, but a gateway that serves several profiles delivers its kanban tick only to
        its own profile, so another profile with no turns would otherwise never run a pass. The clock only
        calls `poke`, which is O(1) until a check is due. `stop` ends it (the plugin's on_unload)."""
        # The clock runs with the pass on "by hand only" too (interval 0): its tick still re-reads the settings and
        # keeps the database's retention (integration), and poke() stays a no-op until an interval is set.
        if self._clock is not None or self._stop.is_set():
            return False

        def loop():
            delay = first
            while not self._stop.wait(delay):
                if self.on_tick is not None:
                    try:
                        self.on_tick()
                    except Exception:   # a settings refresh must never stop the clock
                        pass
                self.poke()
                delay = period

        thread = threading.Thread(target=loop, name="hermes-atlas-evolution-clock", daemon=True)
        self._clock = thread
        try:
            thread.start()
        except RuntimeError:   # the interpreter is shutting down
            self._clock = None
            return False
        return True

    def stop(self) -> None:
        """Stop the clock and refuse new passes. A pass already running stops at its next safe point."""
        self._stop.set()
        if not self._running:
            self._unhook_exit()   # a running pass unhooks as it ends: until then an exit still frees its lease

    def _unhook_exit(self) -> None:
        # atexit keeps a reference to this Autopilot (and its plugin) until the process ends: one per reload.
        with self._lock:
            hooked, self._exit_hooked = self._exit_hooked, False
        if hooked:
            atexit.unregister(self._at_exit)

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def cancelled(self) -> bool:
        """The running pass's check at each safe point (`cancelled` of evolve.cycle): stop when the plugin was unloaded,
        or when another process holds the pass now (its lease ran out). A pass that reaches a safe point is making
        progress, so its lease is renewed there too, without the timer's limit, once a third of it is gone."""
        if self._stop.is_set():
            return True
        lease = self._lease
        if lease is None:
            return False
        if not lease.lost and self.wall() - lease.renewed >= LEASE_SECONDS / 3:
            lease.renew()
        return lease.lost

    def _at_exit(self) -> None:
        """At interpreter exit, free the lease of a pass this process cannot finish, so another retries soon."""
        if not self._running:
            return
        try:
            finish(self.home, now=self.wall(), owner=self._owner, ok=False, interval_s=self.interval_s,
                   error="ProcessExit")
        except Exception:   # the database may already be gone; the lease then expires on its own
            pass

    def poke(self) -> bool:
        """Start a background check when one is due. Never blocks and never raises."""
        if not self.interval_s or not self.claims or self._stop.is_set():
            return False
        moment = self.clock()
        if moment < self._due or not self._lock.acquire(blocking=False):
            return False
        try:
            if moment < self._due or (self._thread is not None and self._thread.is_alive()):
                return False
            self._due = moment + CHECK_SECONDS
            thread = threading.Thread(target=self._work, name="hermes-atlas-evolution-autopilot", daemon=True)
            self._thread = thread
        finally:
            self._lock.release()
        try:
            thread.start()
        except RuntimeError:   # the interpreter is shutting down
            return False
        return True

    def _work(self) -> None:
        started = self.wall()
        try:
            if not claim(self.home, now=started, interval_s=self.interval_s, owner=self._owner):
                return
        except (sqlite3.Error, OSError, ValueError):
            return   # busy or unavailable now; the next check tries again
        if self._stop.is_set():   # unloaded while this check was on its way: give the pass back
            try:
                finish(self.home, now=self.wall(), owner=self._owner, ok=False, interval_s=self.interval_s, error="Unloaded")
            except (sqlite3.Error, OSError, ValueError):
                pass
            return
        ok, summary, error = False, None, ""
        self._running = True
        with self._lock:
            hook = not self._exit_hooked and not self._stop.is_set()
            self._exit_hooked = self._exit_hooked or hook
        if hook:   # a process may still end in the middle of the pass (its user quits): the lease is freed at exit
            atexit.register(self._at_exit)
        try:
            with pinned_profile(self.home), _Lease(self.home, self._owner, wall=self.wall) as lease:
                self._lease = lease
                summary = self.run()
            ok = True
        except Exception as problem:   # never let a pass escape into the host process
            error = type(problem).__name__
            if not self._warned and error != "Cancelled":   # told to stop (evolve.Cancelled) is not a failure
                self._warned = True
                logger.warning("Atlas Evolution autopilot pass failed (%s); it will try again in %d minutes",
                               error, RETRY_SECONDS // 60)
        finally:
            self._running, self._lease = False, None
            if self._stop.is_set():   # unloaded during the pass: nothing is left for an exit to free
                self._unhook_exit()
            try:
                finish(self.home, now=self.wall(), owner=self._owner, ok=ok, interval_s=self.interval_s,
                       started=started, summary=summary, error=error)
            except (sqlite3.Error, OSError, ValueError):
                pass
