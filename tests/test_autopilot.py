"""The evolution pass runs by itself: claimed once per profile across processes, never on the turn path."""

import contextvars
import json
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_atlas_evolution.autopilot import (CHECK_SECONDS, LEASE_SECONDS, RETRY_SECONDS, Autopilot, claim, finish,
                                        pinned_profile, run_exclusive)
from hermes_atlas_evolution.experience import experience
from hermes_atlas_evolution.operations import close_observers

NOW = 1_794_700_000.0
HOUR = 3600


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.addCleanup(close_observers)
        self.home = Path(folder.name)


class ClaimTest(Base):
    def test_one_claim_at_a_time_and_one_pass_per_interval(self):
        self.assertTrue(claim(self.home, now=NOW, interval_s=HOUR, owner="a"))
        self.assertFalse(claim(self.home, now=NOW + 1, interval_s=HOUR, owner="b"))    # a pass is running
        finish(self.home, now=NOW + 5, owner="a", ok=True, interval_s=HOUR)
        self.assertFalse(claim(self.home, now=NOW + 10, interval_s=HOUR, owner="b"))   # it ran a moment ago
        self.assertTrue(claim(self.home, now=NOW + 5 + HOUR, interval_s=HOUR, owner="b"))

    def test_a_dead_owner_leaves_a_lease_that_expires(self):
        self.assertTrue(claim(self.home, now=NOW, interval_s=HOUR, owner="dead"))
        self.assertFalse(claim(self.home, now=NOW + LEASE_SECONDS - 1, interval_s=HOUR, owner="b"))
        self.assertTrue(claim(self.home, now=NOW + LEASE_SECONDS + 1, interval_s=HOUR, owner="b"))
        finish(self.home, now=NOW + LEASE_SECONDS + 2, owner="dead", ok=True, interval_s=HOUR)   # too late: not its lease
        with experience(self.home) as conn:
            lease = conn.execute("SELECT value FROM cursor WHERE key='loop_lease'").fetchone()[0]
        self.assertTrue(lease.startswith("b|"))

    def test_a_failed_pass_is_retried_sooner_than_a_full_interval(self):
        self.assertTrue(claim(self.home, now=NOW, interval_s=HOUR, owner="a"))
        finish(self.home, now=NOW, owner="a", ok=False, interval_s=HOUR, error="OperationalError")
        self.assertFalse(claim(self.home, now=NOW + RETRY_SECONDS - 1, interval_s=HOUR, owner="a"))
        self.assertTrue(claim(self.home, now=NOW + RETRY_SECONDS + 1, interval_s=HOUR, owner="a"))
        with experience(self.home) as conn:
            status = json.loads(conn.execute("SELECT value FROM cursor WHERE key='loop_status'").fetchone()[0])
        self.assertEqual((status["ok"], status["error"]), (False, "OperationalError"))

    def test_only_one_of_many_concurrent_claims_wins(self):
        claim(self.home, now=NOW - 2 * HOUR, interval_s=HOUR, owner="setup")   # create the database first
        finish(self.home, now=NOW - 2 * HOUR, owner="setup", ok=True, interval_s=HOUR)
        wins, barrier = [], threading.Barrier(8)

        def contender(index):
            barrier.wait()
            try:
                if claim(self.home, now=NOW, interval_s=HOUR, owner=f"t{index}"):
                    wins.append(index)
            except Exception:   # a busy database is a lost race, not a second winner
                pass

        threads = [threading.Thread(target=contender, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(wins), 1)


class FencingTest(Base):
    def test_only_the_lease_holder_can_record_the_end_of_a_pass(self):
        self.assertTrue(claim(self.home, now=NOW, interval_s=HOUR, owner="a"))
        finish(self.home, now=NOW + 1, owner="a", ok=False, interval_s=HOUR, error="ProcessExit")   # released at exit
        finish(self.home, now=NOW + 2, owner="a", ok=True, interval_s=HOUR)                         # the thread, too late
        with experience(self.home) as conn:
            status = json.loads(conn.execute("SELECT value FROM cursor WHERE key='loop_status'").fetchone()[0])
        self.assertEqual(status["error"], "ProcessExit")    # the late finish changed nothing

    def test_a_pass_that_finished_after_the_caller_read_its_clock_is_not_repeated(self):
        self.assertTrue(claim(self.home, now=NOW, interval_s=HOUR, owner="gateway"))
        finish(self.home, now=NOW + 3, owner="gateway", ok=True, interval_s=HOUR)
        self.assertFalse(claim(self.home, now=NOW, interval_s=HOUR, owner="tick"))         # read its clock at NOW
        self.assertTrue(claim(self.home, now=NOW - 2 * HOUR, interval_s=HOUR, owner="x"))  # the clock went back hours


class ExclusiveTest(Base):
    def test_a_manual_pass_waits_for_a_running_one_and_then_counts_as_the_latest(self):
        self.assertTrue(claim(self.home, now=NOW, interval_s=HOUR, owner="autopilot"))
        with self.assertRaises(ValueError):   # the autopilot is in the middle of a pass
            run_exclusive(self.home, lambda: "manual", wait_s=0.2, now=lambda: NOW + 1)
        finish(self.home, now=NOW + 2, owner="autopilot", ok=True, interval_s=HOUR)
        self.assertEqual(run_exclusive(self.home, lambda: "manual", wait_s=0.2, now=lambda: NOW + 3), "manual")
        self.assertFalse(claim(self.home, now=NOW + 4, interval_s=HOUR, owner="autopilot"))   # no second pass right away

    def test_a_failing_manual_pass_releases_the_lease(self):
        def broken():
            raise KeyError("x")

        claim(self.home, now=NOW - 700, interval_s=HOUR, owner="autopilot-0")
        finish(self.home, now=NOW - 600, owner="autopilot-0", ok=True, interval_s=HOUR)   # it ran 10 minutes ago
        with self.assertRaises(KeyError):
            run_exclusive(self.home, broken, wait_s=0.2, now=lambda: NOW)
        self.assertFalse(claim(self.home, now=NOW + 1, interval_s=HOUR, owner="autopilot"))   # the schedule is unchanged
        self.assertEqual(run_exclusive(self.home, lambda: "again", wait_s=0.2, now=lambda: NOW + 3), "again")


class AutopilotTest(Base):
    def make(self, run, *, minutes=60):
        clock = {"t": 1000.0}
        pilot = Autopilot(self.home, run, interval_minutes=minutes, clock=lambda: clock["t"], wall=lambda: NOW + clock["t"])
        return pilot, clock

    def wait(self, pilot):
        thread = pilot._thread
        if thread is not None:
            thread.join(5)

    def test_poke_runs_the_pass_in_the_background_and_never_blocks(self):
        started, release = threading.Event(), threading.Event()

        def run():
            started.set()
            release.wait(5)
            return {"new_lessons": 1}

        pilot, clock = self.make(run)
        before = time.perf_counter()
        self.assertTrue(pilot.poke())
        self.assertLess(time.perf_counter() - before, 0.05)   # the hook returns at once
        self.assertTrue(started.wait(5))
        self.assertFalse(pilot.poke())                         # within the check interval: nothing to do
        clock["t"] += CHECK_SECONDS + 1
        self.assertFalse(pilot.poke())                         # the pass is still running
        release.set()
        self.wait(pilot)
        with experience(self.home) as conn:
            status = json.loads(conn.execute("SELECT value FROM cursor WHERE key='loop_status'").fetchone()[0])
        self.assertEqual((status["ok"], status["summary"]), (True, {"new_lessons": 1}))

    def test_the_pass_runs_once_per_interval_and_never_when_disabled(self):
        calls = []
        pilot, clock = self.make(lambda: calls.append(1))
        pilot.poke()
        self.wait(pilot)
        clock["t"] += CHECK_SECONDS + 1
        pilot.poke()
        self.wait(pilot)
        self.assertEqual(len(calls), 1)                        # checked again, but an hour has not passed
        clock["t"] += HOUR
        pilot.poke()
        self.wait(pilot)
        self.assertEqual(len(calls), 2)
        off, _ = self.make(lambda: calls.append(1), minutes=0)
        self.assertFalse(off.poke())
        self.assertEqual(len(calls), 2)

    def test_a_failing_pass_is_logged_once_and_retried_later(self):
        def broken():
            raise RuntimeError("boom")

        pilot, clock = self.make(broken)
        with self.assertLogs("hermes_atlas_evolution.autopilot", level="WARNING") as logged:
            pilot.poke()
            self.wait(pilot)
            clock["t"] += RETRY_SECONDS + 1
            pilot.poke()
            self.wait(pilot)
        self.assertEqual(len(logged.output), 1)
        self.assertNotIn("boom", logged.output[0])             # the exception type only, never its text

    def test_the_clock_nudges_an_idle_profile_and_stops_on_unload(self):
        calls = []
        pilot, clock = self.make(lambda: calls.append(1))
        self.assertTrue(pilot.start_clock(first=0.05, period=0.05))
        self.assertFalse(pilot.start_clock(first=0.05, period=0.05))   # one clock per autopilot
        deadline = time.monotonic() + 5
        while not calls and time.monotonic() < deadline:
            time.sleep(0.02)
        self.wait(pilot)
        self.assertEqual(len(calls), 1)                                  # no turn needed for the first pass
        pilot.stop()
        pilot._clock.join(2)
        self.assertFalse(pilot._clock.is_alive())
        clock["t"] += HOUR * 2
        self.assertFalse(pilot.poke())                                   # after unload nothing new starts

    def test_a_pass_cut_by_process_exit_frees_the_lease_for_a_retry(self):
        started, release = threading.Event(), threading.Event()

        def run():
            started.set()
            release.wait(5)

        pilot, clock = self.make(run)
        pilot.poke()
        self.assertTrue(started.wait(5))
        pilot._at_exit()                                                 # what atexit does at interpreter shutdown
        with experience(self.home) as conn:
            self.assertIsNone(conn.execute("SELECT value FROM cursor WHERE key='loop_lease'").fetchone())
            status = json.loads(conn.execute("SELECT value FROM cursor WHERE key='loop_status'").fetchone()[0])
        self.assertEqual((status["ok"], status["error"]), (False, "ProcessExit"))
        release.set()
        self.wait(pilot)

    def test_an_unloaded_autopilot_does_not_run_a_pass_it_claimed(self):
        calls = []
        pilot, _ = self.make(lambda: calls.append(1))
        pilot.stop()
        pilot._work()                                     # a check that was already on its way
        self.assertEqual(calls, [])
        with experience(self.home) as conn:
            self.assertIsNone(conn.execute("SELECT value FROM cursor WHERE key='loop_lease'").fetchone())

    def test_the_thread_pins_the_plugin_profile(self):
        override = contextvars.ContextVar("override", default=None)
        constants = types.ModuleType("hermes_constants")
        constants.set_hermes_home_override = lambda path: override.set(str(path))
        constants.reset_hermes_home_override = lambda token: override.reset(token)
        scope = contextvars.ContextVar("scope", default=None)
        secrets = types.ModuleType("agent.secret_scope")
        secrets.build_profile_secret_scope = lambda home: {"KEY": f"key-of-{Path(home).name}"}
        secrets.set_secret_scope = lambda mapping, profile_home=None: scope.set((mapping, profile_home))
        secrets.reset_secret_scope = lambda token: scope.reset(token)
        agent = types.ModuleType("agent")
        agent.secret_scope = secrets
        seen = []
        with patch.dict(sys.modules, {"hermes_constants": constants, "agent": agent, "agent.secret_scope": secrets}):
            pilot, _ = self.make(lambda: seen.append((override.get(), scope.get())))
            pilot.poke()
            self.wait(pilot)
            with pinned_profile(self.home):
                self.assertEqual(override.get(), str(self.home))
            self.assertIsNone(override.get())
        # the profile's own API keys, never the gateway's: a multi-profile gateway scopes secrets per profile
        self.assertEqual(seen, [(str(self.home), ({"KEY": f"key-of-{self.home.name}"}, str(self.home)))])


if __name__ == "__main__":
    unittest.main()
