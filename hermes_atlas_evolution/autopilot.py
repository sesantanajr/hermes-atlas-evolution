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
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

CHECK_SECONDS = 60          # how often a hook may start a check
CLOCK_FIRST_SECONDS = 120   # the clock's first nudge after the plugin loads
CLOCK_PERIOD_SECONDS = 600  # then every 10 minutes, so an idle profile still gets its passes
LEASE_SECONDS = 15 * 60     # a pass that has not finished by then is presumed dead
RETRY_SECONDS = 10 * 60     # a failed pass is tried again after this, not after a full interval

logger = logging.getLogger(__name__)


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
        holder, until = _lease(conn)
        if holder and until > now:
            return False
        conn.execute("INSERT OR REPLACE INTO cursor VALUES ('loop_lease', ?)", (f"{owner}|{now + LEASE_SECONDS!r}",))
        return True


def finish(home: Path, *, now: float, owner: str, ok: bool, interval_s: float, started: float | None = None,
           summary=None, error: str = "") -> None:
    """Record the end of a pass. A failure moves the next attempt to RETRY_SECONDS from now."""
    from .experience import experience

    with experience(home) as conn:
        conn.execute("BEGIN IMMEDIATE")
        holder, _ = _lease(conn)
        if holder and holder != owner:
            return   # the lease expired and another process owns the pass now
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
        result = run()
    except BaseException as problem:
        finish(home, now=now(), owner=owner, ok=False, interval_s=0, started=started, error=type(problem).__name__)
        raise
    finish(home, now=now(), owner=owner, ok=True, interval_s=0, started=started)
    return result


@contextmanager
def pinned_profile(home: Path):
    """Make Hermes resolve this plugin's profile in the current thread."""
    try:
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    except ImportError:   # outside Hermes: nothing to pin
        yield
        return
    token = set_hermes_home_override(str(home))
    try:
        yield
    finally:
        reset_hermes_home_override(token)


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

    def start_clock(self, *, first: float = CLOCK_FIRST_SECONDS, period: float = CLOCK_PERIOD_SECONDS) -> bool:
        """A daemon timer that nudges the pass while the process is idle.

        Turns nudge the pass too, but a gateway that serves several profiles delivers its kanban tick only to
        its own profile, so another profile with no turns would otherwise never run a pass. The clock only
        calls `poke`, which is O(1) until a check is due. `stop` ends it (the plugin's on_unload)."""
        if not self.interval_s or self._clock is not None or self._stop.is_set():
            return False

        def loop():
            delay = first
            while not self._stop.wait(delay):
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
        """Stop the clock and refuse new passes. A pass already running finishes on its own."""
        self._stop.set()

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
        if not self.interval_s or self._stop.is_set():
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
        ok, summary, error = False, None, ""
        self._running = True
        if not self._exit_hooked:   # a short-lived process (hermes -z) may exit in the middle of the pass
            self._exit_hooked = True
            atexit.register(self._at_exit)
        try:
            with pinned_profile(self.home):
                summary = self.run()
            ok = True
        except Exception as problem:   # never let a pass escape into the host process
            error = type(problem).__name__
            if not self._warned:
                self._warned = True
                logger.warning("Atlas Evolution autopilot pass failed (%s); it will try again in %d minutes",
                               error, RETRY_SECONDS // 60)
        finally:
            self._running = False
            try:
                finish(self.home, now=self.wall(), owner=self._owner, ok=ok, interval_s=self.interval_s,
                       started=started, summary=summary, error=error)
            except (sqlite3.Error, OSError, ValueError):
                pass
