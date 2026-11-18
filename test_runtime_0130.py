"""0.13.0: what the plugin costs the Hermes it runs in, and what it keeps. Each test below failed on 0.12.0: a guard
pattern that froze the gateway, padding that switched the guard off, connections reopened on every hook write, a
learning pass that lost its race with the hooks, a table that was 99% of the database, a plugin host with no pass, a
lease another process could take mid-pass, checkpoints paid inside tool calls, and lessons that were never shown."""

import os
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from hermes_atlas_evolution import autopilot, experience as experience_module, guard, lessons, operations, proposals
from hermes_atlas_evolution.autopilot import Autopilot, runs_passes
from hermes_atlas_evolution.experience import experience
from hermes_atlas_evolution.signals import record_tool
from test_plugin_loop import FakeContext, PluginBase

os.environ.setdefault("ATLAS_EVOLUTION_NO_HERMES", "1")   # never the real Hermes of this machine

NOW = datetime(2026, 11, 18, 12, tzinfo=timezone.utc).timestamp()
DAY = 86_400
HOME = PurePosixPath("/home/owner")
PROFILE = HOME / ".hermes"
SOURCE = PurePosixPath("/usr/local/lib/hermes-agent")
PROTECTED = (SOURCE, SOURCE / "venv", PROFILE)
TREES = (SOURCE / "venv", SOURCE / "hermes_cli")
# As long as a gateway's command line can be (the production one is 363 characters), placeholder paths.
GATEWAY = ("/root/.hermes/tools/python-3.14.9+20261021-linux-x64/bin/python3 -B -I -c import sys, runpy; "
           "sys.path.insert(0, '/usr/local/lib/hermes-agent'); sys.argv = ['/opt/example/native_gateway.py', 'gateway', "
           "'run']; runpy.run_path('/opt/example/native_gateway.py', run_name='__main__') ")
PROCESS = ("python3", (GATEWAY * 2)[:500])


class Scratch(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.addCleanup(operations.close_observers)   # Windows cannot remove a database that is still open
        self.home = Path(folder.name)


class GuardTest(unittest.TestCase):
    def setUp(self):
        windows, guard._WINDOWS = guard._WINDOWS, False   # POSIX commands on every system
        self.addCleanup(setattr, guard, "_WINDOWS", windows)

    def verdict(self, command, process=PROCESS):
        found = guard.check("terminal", {"command": command}, protected=PROTECTED, home=HOME, profile=PROFILE,
                            trees=TREES, own=(), process=process)
        return found["kind"] if found else None

    def test_a_pattern_that_backtracks_is_decided_at_once(self):
        # 0.12.0 refused only repeated groups: `.*.*.*.*X` took 75 s against the gateway's own command line, holding the
        # GIL, and the gateway stood still. These took 4 s to minutes against this 500-character one.
        for command in ("pkill -f '.*.*.*X'", "pkill -f '.*.*.*.*X'", "pkill -f '" + ".{0,9}" * 6 + "X'",
                        "pkill -f '" + ".?" * 14 + ".*X'", "pkill -f '(?:.)*(?:.)*(?:.)*X'", "pkill -f '(?#[).*.*.*X]'",
                        "kill $(pgrep -f '[^X]*[^X]*[^X]*X')", "pkill -f '(?x). * . * . * X'"):
            with self.subTest(command=command):
                start = time.perf_counter()
                self.assertIsNone(self.verdict(command))
                self.assertLess(time.perf_counter() - start, 0.5)

    def test_patterns_that_name_the_gateway_still_stop_it(self):
        for command in ("pkill -f '.*.*.*.*'", "pkill -f '.*native_gateway.*gateway.*run.*'",
                        "pkill -f 'native_gateway.py.*gateway.*run.*run_path'",   # three wildcards: matched in one pass
                        "pkill -f '/root/.hermes/tools/python-3.14.9.20261021-linux-x64/bin/python3 -B -I -c import sys'",
                        "kill $(pgrep -f '^/root/.hermes/tools/python.*native_gateway')"):
            with self.subTest(command=command):
                self.assertEqual(self.verdict(command), "gateway_stop")
        self.assertIsNone(self.verdict("pkill -f 'native_gateway.*nothing.*here.*at.*all'"))

    def test_the_simpler_pattern_matches_the_same(self):
        for pattern, text, whole in ((".*.*X", "aXb", False), (".+.+", "a", False), (".+.+", "ab", False),
                                     ("^.*a.+b$", "xab", False), ("^.*a.+b$", "xacb", False), (".*.*", "", True),
                                     ("a.*b.+c", "abc", False), ("a.*b.+c", "ab-c", False)):
            with self.subTest(pattern=pattern, text=text):
                expected = bool((guard.re.fullmatch if whole else guard.re.search)(pattern, text))
                self.assertEqual(guard._search(pattern, text, whole=whole), expected)
                self.assertEqual(guard._in_order(pattern, text, whole=whole), expected)

    def test_padding_before_or_after_the_command_does_not_hide_it(self):
        stop = "\nhermes gateway stop"
        self.assertEqual(self.verdict("cat > notes.md <<'EOF'\n" + "lorem ipsum\n" * 8_500 + "EOF" + stop), "gateway_stop")
        self.assertEqual(self.verdict("true; " * 3_500 + "hermes gateway stop"), "gateway_stop")
        self.assertEqual(self.verdict('echo "' + "a;b " * 7_000 + '" ; hermes gateway stop'), "gateway_stop")
        self.assertEqual(self.verdict('systemctl --user restart hermes-gateway; echo "' + "a;b " * 7_000 + '"'),
                         "gateway_stop")
        self.assertEqual(self.verdict("rm -rf " + ("/tmp/" + "a" * 40 + " ") * 500 + "~/.hermes"), "runtime_removal")
        self.assertEqual(self.verdict("true; " * 100_000 + "hermes gateway stop"), "gateway_stop")   # past _SCAN_LIMIT
        self.assertIsNone(self.verdict("printf '%s' '" + "A" * 50_000 + "'"))
        self.assertIsNone(self.verdict("cat > f <<'EOF'\n" + "systemctl --user restart hermes-gateway\n" * 3_000 + "EOF"))

    def test_a_huge_command_stays_cheap(self):
        for command in ("'' " * 160_000 + "<<E\nx\nE\n#c\nhermes gateway stop", "a " * 240_000 + "; hermes gateway stop",
                        "".join(f"cat <<D{index}\nx\nD{index}\n" for index in range(20_000)) + "hermes gateway stop",
                        "cat <<'E'\n" + "x\n" * 600_000 + "E\nhermes gateway stop"):
            with self.subTest(command=command[:30]):
                start = time.perf_counter()
                self.assertEqual(self.verdict(command), "gateway_stop")
                self.assertLess(time.perf_counter() - start, 5.0)   # ~0.2-0.5 s here

    def test_a_costly_pattern_that_names_the_gateway_is_still_vetoed(self):
        # The first 0.13.0 build matched a pattern too costly to run by its text without metacharacters, which almost
        # never matches: these went through, 0.12.0 vetoed them at once. Their literal fragments, in order, decide now.
        launch = ("python3", "/usr/local/lib/hermes-agent/venv/bin/python3 /usr/local/lib/hermes-agent/venv/bin/hermes "
                             "gateway run --replace " + "--profile atlas-ai " * 12)
        for pattern in (r"(hermes|python3).*gateway.*run.*replace", r"hermes\S*.*gateway.*run",
                        r"(python|hermes).*gateway.*run", r"bin/hermes\s+gateway\s+run\s+--replace"):
            with self.subTest(pattern=pattern):
                self.assertEqual(self.verdict(f"pkill -f '{pattern}'", process=launch), "gateway_stop")
                self.assertEqual(self.verdict(f"kill $(pgrep -f '{pattern}')", process=launch), "gateway_stop")
        for pattern in (r"(celery|rq).*worker.*run.*now", r"node\s+server\.js\s+--port\s+8080",
                        r"(gunicorn|uvicorn)\s+app:main.*--reload", r"[m]y-?job\s+.*--daily"):
            with self.subTest(pattern=pattern):   # ordinary kills of other programs still run
                self.assertIsNone(self.verdict(f"pkill -f '{pattern}'", process=launch))
        self.assertEqual(guard._fragments(r"(a|b)x\.y+z[0-9]w|q"), ([["x.", "z", "w"], ["q"]], False))

    def test_many_patterns_in_one_command_share_one_budget(self):
        # 110 patterns of ~0.3 s each in one `pkill -f` took 34 s, past Hermes' 30 s veto timeout (each was under the
        # budget of one pattern)
        for command in ("pkill -f " + " ".join(".?" * 14 + "[^Z]{14}" + f"Z{index}" for index in range(40)),   # 0.3 s each
                        "pkill -f " + " ".join(".?" * 6 + "[^Z]*" + f"Z{index}" for index in range(200))):   # 10 ms each
            with self.subTest(command=command[:40]):
                start = time.perf_counter()
                self.assertIsNone(self.verdict(command))
                self.assertLess(time.perf_counter() - start, 0.5)

    def test_the_middle_of_a_long_command_is_read_as_before(self):
        # 0.12.0 read the first 20,000 characters; the first 0.13.0 build read 10,000 at each end and lost the middle
        pad = "true; " * 2_000
        self.assertEqual(self.verdict(pad + "hermes gateway stop; " + pad), "gateway_stop")
        self.assertEqual(self.verdict(pad + "rm -rf /usr/local/lib/hermes-agent; " + pad), "runtime_removal")
        self.assertEqual(self.verdict("rm -rf " + "/tmp/x " * 3_000 + "/usr/local/lib/hermes-agent; true"),
                         "runtime_removal")
        # a small script a shell reads, and an unquoted body's $(...), in a command padded past the limit
        self.assertEqual(self.verdict("bash <<'EOF'\nhermes gateway stop\nEOF\n" + "true; " * 4_000), "gateway_stop")
        self.assertEqual(self.verdict("true; " * 4_000 + "\ncat <<EOF\n$(hermes gateway stop)\nEOF\n"), "gateway_stop")
        start = time.perf_counter()   # thousands of distinct paths: 0.12.0 took 0.6 s on 20,000 characters
        self.assertEqual(self.verdict("rm -rf " + " ".join(f"/t/{index}" for index in range(4_000)) + " ~/.hermes"),
                         "runtime_removal")
        self.assertLess(time.perf_counter() - start, 2.0)


class ObserverTest(Scratch):
    def homes(self, count):
        homes = [self.home / f"p{index}" for index in range(count)]
        for home in homes:
            home.mkdir()
        return homes

    def test_one_process_serving_many_profiles_keeps_their_connections(self):
        homes = self.homes(12)   # 0.12.0 kept 8: each write closed one connection and opened another (60 us -> 1.1 ms)
        for home in homes:
            operations.observe(home, "tool", {"status": "ok"})
        first = {home: operations._observer(home).connection for home in homes}
        for _ in range(2):
            for home in homes:
                operations.observe(home, "tool", {"status": "ok"})
        self.assertEqual({home: operations._observer(home).connection for home in homes}, first)

    def test_an_idle_connection_is_closed(self):
        quiet, busy = self.homes(2)
        operations.observe(quiet, "tool", {"status": "ok"})
        operations.observe(busy, "tool", {"status": "ok"})
        operations._OBSERVERS[os.fspath(quiet)].used -= operations._OBSERVER_IDLE_SECONDS + 1
        operations.observe(busy, "tool", {"status": "ok"})
        self.assertNotIn(os.fspath(quiet), operations._OBSERVERS)
        self.assertIn(os.fspath(busy), operations._OBSERVERS)

    def test_a_repeated_delivery_counts_once_and_writes_no_table(self):
        payload = {"turn_id": "turn", "tool_call_id": "call", "status": "ok"}
        operations.observe(self.home, "tool", payload, now=NOW)
        operations.observe(self.home, "tool", payload, now=NOW)
        operations.observe(self.home, "tool", {**payload, "tool_call_id": "other"}, now=NOW)
        operations.close_observers()
        with experience(self.home) as conn:
            calls = conn.execute("SELECT SUM(requests) FROM metric WHERE lane='tool'").fetchone()[0]
            self.assertEqual((calls, conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]), (2, 0))
            conn.execute("INSERT INTO seen VALUES ('written-by-0.12.0', ?)", (experience_module.utc_day(NOW),))
            operations._prune(conn, NOW)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0], 0)

    @patch.object(operations, "_DELIVERY_LIMIT", 16)
    def test_the_delivery_memory_is_bounded(self):
        for index in range(40):
            operations.observe(self.home, "tool", {"turn_id": "turn", "tool_call_id": f"c{index}", "status": "ok"})
        self.assertLessEqual(len(operations._DELIVERIES), 16)

    def test_a_hook_builds_no_path_per_update(self):
        operations.observe(self.home, "tool", {"status": "ok"})   # the shared connection exists
        built = []

        def counting(*parts):
            built.append(parts)
            return Path(*parts)

        payload = {"tool_name": "terminal", "status": "ok", "result": "{}", "session_id": "s", "turn_id": "t",
                   "tool_call_id": "c"}
        with patch.object(operations, "Path", counting), patch.object(experience_module, "Path", counting):
            with operations.coalesced(self.home):
                operations.observe(self.home, "tool", payload)
                record_tool(self.home, payload, owner=True, origin="cli")
        self.assertEqual(built, [])

    def test_hook_writes_leave_the_checkpoint_to_the_clock(self):
        with patch.object(operations, "_CHECKPOINT_WRITES", 10 ** 9):
            for index in range(1_500):   # a frame or more each
                operations.observe(self.home, "tool", {"turn_id": "t", "tool_call_id": f"c{index}", "status": "ok"})
            connection = operations._observer(self.home).connection
            self.assertEqual(connection.execute("PRAGMA wal_autocheckpoint").fetchone()[0], operations._WAL_CEILING_PAGES)
            self.assertEqual(connection.execute("PRAGMA journal_size_limit").fetchone()[0], operations.JOURNAL_LIMIT_BYTES)
            probe = sqlite3.connect(self.home / "evolution" / "evolution.sqlite3")
            self.addCleanup(probe.close)
            _busy, frames, _done = probe.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            # 0.12.0 checkpointed at 1,000 frames inside a hook (about one write in 65); a PASSIVE one was done here
            self.assertGreater(frames, 1_000)
            self.assertTrue(operations.checkpoint(self.home))
            operations.observe(self.home, "tool", {"status": "ok"})   # the next write starts the log again, cut back
            wal = self.home / "evolution" / "evolution.sqlite3-wal"
            self.assertLessEqual(wal.stat().st_size, operations.JOURNAL_LIMIT_BYTES + 64 * 1024)

    def test_a_writer_that_never_stops_keeps_the_log_small(self):
        # A checkpoint from a connection of its own never let the log start over while hooks kept writing: the first
        # 0.13.0 build reached 23.5 MB for 6,000 writes (0.12.0: 4 MB); on the hooks' connection it stays near 1 MiB
        for index in range(3_000):
            operations.observe(self.home, "tool", {"turn_id": "t", "tool_call_id": f"c{index}", "status": "ok"})
        deadline = time.monotonic() + 5
        while operations._CHECKPOINTS and time.monotonic() < deadline:
            time.sleep(0.01)
        operations.observe(self.home, "tool", {"status": "ok"})
        wal = self.home / "evolution" / "evolution.sqlite3-wal"
        self.assertLess(wal.stat().st_size, 2 * operations.JOURNAL_LIMIT_BYTES)
        fresh = self.home / "never-written"
        fresh.mkdir()
        self.assertFalse(operations.checkpoint(fresh))   # nothing written here: nothing opened, nothing created
        self.assertEqual(list(fresh.iterdir()), [])

    def test_a_connection_just_handed_out_is_not_closed(self):
        mine, theirs = self.homes(2)
        operations.observe(mine, "tool", {"status": "ok"})
        operations.observe(theirs, "tool", {"status": "ok"})
        # another thread handed `theirs` out after this one read its clock: the first build saw a negative age as
        # idle and closed it under that thread
        operations._OBSERVERS[os.fspath(theirs)].used = time.monotonic() + 60
        operations.observe(mine, "tool", {"status": "ok"})
        self.assertIn(os.fspath(theirs), operations._OBSERVERS)
        self.assertFalse(operations._OBSERVERS[os.fspath(theirs)].closed)

    def test_updates_a_closed_connection_could_not_write_are_written_by_the_next(self):
        operations.observe(self.home, "tool", {"status": "ok"}, now=NOW)
        other = sqlite3.connect(self.home / "evolution" / "evolution.sqlite3", isolation_level=None)
        self.addCleanup(other.close)
        other.execute("BEGIN IMMEDIATE")   # busy: the hook keeps its update in the queue
        operations.observe(self.home, "tool", {"status": "ok"}, now=NOW)
        closing = operations._OBSERVERS.pop(os.fspath(self.home))
        self.assertEqual(len(closing.pending), 1)
        closing.close(wait=0.05)   # still busy: it cannot write it either
        other.execute("ROLLBACK")
        operations.observe(self.home, "tool", {"status": "ok"}, now=NOW)
        operations.close_observers()
        with experience(self.home) as conn:
            self.assertEqual(conn.execute("SELECT SUM(requests) FROM metric WHERE lane='tool'").fetchone()[0], 3)

    def test_a_busy_profile_checkpoints_on_a_thread_of_its_own(self):
        ran = []
        with patch.object(operations, "_CHECKPOINT_WRITES", 5), \
                patch.object(operations, "checkpoint", lambda home: ran.append(threading.current_thread().name)):
            for index in range(12):
                operations.observe(self.home, "tool", {"turn_id": "t", "tool_call_id": f"c{index}", "status": "ok"})
            deadline = time.monotonic() + 5
            while len(ran) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertGreaterEqual(len(ran), 2)
        self.assertEqual(set(ran), {"hermes-atlas-evolution-checkpoint"})

    def test_the_decision_log_keeps_what_the_history_shows(self):
        with experience(self.home) as conn:
            rows = [(NOW - 200 * DAY, "terminal", "lesson", "1", "approve", "{}", "{}")] * 1_100
            rows += [(NOW - DAY, "terminal", "lesson", "1", "approve", "{}", "{}")] * 5
            conn.executemany("INSERT INTO decision_log(at, actor, kind, target, action, before, after) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
            experience_module.prune(conn, now=NOW)
            kept = conn.execute("SELECT COUNT(*), MIN(id), MAX(id) FROM decision_log").fetchone()
        self.assertEqual(tuple(kept), (experience_module.DECISIONS_KEPT, 106, 1_105))


class ProposalRaceTest(Scratch):
    def test_the_rebuild_survives_a_hook_write_between_its_read_and_its_write(self):
        with experience(self.home) as conn:
            for signature in ("a", "b"):
                conn.execute("""INSERT INTO lesson(signature, tool, klass, hint, origin, status, created, updated)
                    VALUES (?, 'terminal', 'timeout', '', 'derived', 'candidate', ?, ?)""", (signature, NOW, NOW))
        original, hook = proposals._upsert, []

        def upsert(conn, *args, **kwargs):
            if not hook:   # a hook commits after the rebuild read the lessons and before it writes
                other = sqlite3.connect(self.home / "evolution" / "evolution.sqlite3", timeout=0, isolation_level=None)
                try:
                    other.execute("INSERT OR REPLACE INTO cursor VALUES ('probe', '1')")
                    hook.append("committed")
                except sqlite3.OperationalError:
                    hook.append("waits")   # the rebuild holds the write lock: the hook's queue keeps its event
                finally:
                    other.close()
            return original(conn, *args, **kwargs)

        with patch.object(proposals, "_upsert", upsert), experience(self.home) as conn:
            opened = proposals.refresh(conn, now=NOW)   # 0.12.0: SQLITE_BUSY_SNAPSHOT here, with no busy wait
        self.assertEqual(len(opened), 2)


class AutopilotTest(Scratch):
    def test_a_plugin_host_runs_the_learning_pass(self):
        with patch.object(sys, "argv", ["/opt/hermes/hermes_cli/plugin_host_child.py"]), \
                patch.dict(os.environ, {"HERMES_PLUGIN_HOST_PROCESS": "1"}):
            self.assertTrue(runs_passes(terminal=False))
        with patch.object(sys, "argv", ["/opt/hermes/venv/bin/hermes", "-z", "hi"]), \
                patch.dict(os.environ, {"HERMES_PLUGIN_HOST_PROCESS": ""}):
            self.assertFalse(runs_passes(terminal=False))   # a one-shot run still leaves it alone

    @patch.object(autopilot, "LEASE_SECONDS", 0.9)
    def test_a_pass_that_outlives_its_lease_keeps_it(self):
        release, ran = threading.Event(), []

        def first():
            ran.append("first")
            release.wait(10)
            return {"by": "first"}

        one = Autopilot(self.home, first, interval_minutes=15)
        other = Autopilot(self.home, lambda: ran.append("other") or {"by": "other"}, interval_minutes=15)
        self.assertTrue(one.poke())
        deadline = time.monotonic() + 5
        while not ran and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(1.3)   # past the lease the claim wrote
        self.assertTrue(other.poke())
        other._thread.join(5)
        release.set()
        one._thread.join(5)
        self.assertEqual(ran, ["first"])
        with experience(self.home) as conn:   # the first pass's end is recorded, not dropped
            status = conn.execute("SELECT value FROM cursor WHERE key='loop_status'").fetchone()[0]
        self.assertIn('"by": "first"', status)

    @patch.object(autopilot, "LEASE_SECONDS", 0.6)
    def test_a_pass_whose_lease_another_process_took_is_told_to_stop(self):
        told = []

        def run():
            with experience(self.home) as conn:
                conn.execute("INSERT OR REPLACE INTO cursor VALUES ('loop_lease', ?)", (f"elsewhere|{time.time() + 60!r}",))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if pilot.cancelled():
                    told.append(True)
                    break
                time.sleep(0.02)
            return {}

        pilot = Autopilot(self.home, run, interval_minutes=15)
        self.assertTrue(pilot.poke())
        pilot._thread.join(10)
        self.assertEqual(told, [True])


class PluginTest(PluginBase):
    def test_unloading_one_profile_keeps_the_others_connection(self):
        class Context(FakeContext):
            def __init__(self):
                super().__init__()
                self.unload = []

            def on_unload(self, callback):
                self.unload.append(callback)

        other = self.home / "other"
        other.mkdir()
        contexts = []
        for home in (self.home, other):
            sys.modules["hermes_constants"].get_hermes_home = lambda home=home: home
            context = Context()
            from hermes_atlas_evolution.integration import register

            register(context)
            contexts.append(context)
            self.addCleanup(lambda context=context: [callback() for callback in reversed(context.unload)])
            context.fire("post_tool_call", tool_name="terminal", status="ok", result="{}", session_id="s",
                         turn_id="t", tool_call_id=f"c-{home.name}")
        kept = operations._observer(other).connection
        for callback in reversed(contexts[0].unload):   # Hermes runs them in reverse order, the cost saved first
            callback()
        contexts[0].unload.clear()
        self.assertNotIn(os.fspath(self.home), operations._OBSERVERS)
        self.assertIs(operations._OBSERVERS[os.fspath(other)].connection, kept)

    def test_the_clock_folds_the_log_in(self):
        context = self.plugin()
        plugin = context.hooks["on_kanban_dispatch_tick"][0].__self__
        with patch.object(operations, "checkpoint") as folded:
            plugin.tick()
        folded.assert_called_once_with(self.home)

    def test_settings_are_watched_without_touching_credential_files(self):
        from hermes_atlas_evolution.integration import _managed_files

        managed = self.home / "managed"
        managed.mkdir()
        (managed / "config.yaml").write_text("x: 1\n")
        (managed / "secrets.env").write_text("NAME=value\n")
        scope = types.ModuleType("hermes_cli.managed_scope")
        scope.get_managed_dir = lambda: managed
        with patch.dict(sys.modules, {"hermes_cli.managed_scope": scope}):
            self.assertEqual(_managed_files(), [managed / "config.yaml"])


class LessonOrderTest(Scratch):
    def lesson(self, conn, signature, *, total, days_ago, recent=0, verdict=None, origin="derived", activated=None,
               last_seen=None, text=None):
        conn.execute("INSERT INTO failure VALUES (?, 'files_search', 'missing_argument', ?, ?, ?, ?, 3, 'x')",
                     (signature, signature, NOW - 90 * DAY, last_seen or NOW - days_ago * DAY, total))
        conn.execute("INSERT INTO failure_daily VALUES (?, ?, ?)",
                     (experience_module.day_offset(NOW, days_ago), signature, max(1, total - recent)))
        if recent:
            conn.execute("INSERT OR REPLACE INTO failure_daily VALUES (?, ?, ?)",
                         (experience_module.utc_day(NOW), signature, recent))
        activated = NOW - 60 * DAY if activated is None else activated
        return conn.execute("""INSERT INTO lesson(signature, tool, klass, hint, origin, status, text, created, updated,
              activated, verdict) VALUES (?, 'files_search', 'missing_argument', ?, ?, 'active', ?, ?, ?, ?, ?)""",
                            (signature, signature, origin, text, activated, activated, activated, verdict)).lastrowid

    def test_a_new_lesson_reaches_the_prompt(self):
        with experience(self.home) as conn:
            for index in range(9):   # old lessons, effective, with large all-time totals and nothing lately
                self.lesson(conn, f"old{index}", total=1_000, days_ago=60, verdict="effective")
            new = self.lesson(conn, "fresh", total=5, days_ago=0, recent=5)
            chosen = []
            lessons.playbook(conn, chosen=chosen)
        self.assertIn(new, chosen)   # 0.12.0: effective first, then all-time totals: never shown, never measured

    def test_an_unseen_lesson_auto_mode_activated_expires_when_its_failure_stopped(self):
        old = NOW - 40 * DAY
        with experience(self.home) as conn:
            # auto mode activated it the moment it was derived (activated = created): marked once, by the migration
            stale = self.lesson(conn, "stale", total=6, days_ago=35, activated=old)
            marked = self.lesson(conn, "marked", total=6, days_ago=35, activated=old + 5)
            conn.execute("INSERT INTO lesson_auto VALUES (?, ?)", (marked, old + 5))
            written = self.lesson(conn, "written", total=6, days_ago=35, activated=old, origin="operator",
                                  text="Pass the service name to files_search every time.")
            approved = self.lesson(conn, "approved", total=6, days_ago=35, activated=old)
            conn.execute("""INSERT INTO decision_log(at, actor, kind, target, action, before, after)
                VALUES (?, 'terminal', 'lesson', ?, 'approve', '{}', '{}')""", (old, str(approved)))
            seen = self.lesson(conn, "seen", total=6, days_ago=35, activated=old)
            conn.execute("INSERT INTO lesson_measure(lesson_id, activated, first_shown) VALUES (?, ?, ?)", (seen, old, old))
            recurring = self.lesson(conn, "recurring", total=6, days_ago=2, activated=old)
            young = self.lesson(conn, "young", total=6, days_ago=35, activated=NOW - 10 * DAY)
            lessons.tidy(conn, now=NOW)
            status = dict(conn.execute("SELECT id, status FROM lesson").fetchall())
            self.assertEqual({key for key, value in status.items() if value == "retired"}, {stale, marked})
            self.assertEqual({status[item] for item in (written, approved, seen, recurring, young)}, {"active"})
            self.assertEqual(conn.execute("SELECT verdict FROM lesson WHERE id=?", (stale,)).fetchone()[0],
                             lessons.WITHDRAWN)
            # it comes back only once its failure is seen again
            self.assertNotIn(stale, lessons.derive(conn, now=NOW + DAY, mode="auto"))
            conn.execute("UPDATE failure SET last_seen=?, total=total+1 WHERE signature='stale'", (NOW + 2 * DAY,))
            self.assertIn(stale, lessons.derive(conn, now=NOW + 2 * DAY, mode="auto"))

    def test_a_lesson_read_before_0120_is_not_taken_back(self):
        # a 0.11/0.12 database: the lesson was read 57 times, but lesson_measure had no row for it yet
        with experience(self.home) as conn:
            conn.execute("INSERT INTO cursor VALUES ('observed_since', ?)", (repr(NOW - 61 * DAY),))
            read = self.lesson(conn, "read", total=10, days_ago=40, activated=NOW - 60 * DAY, verdict="effective")
            conn.execute("UPDATE lesson SET shown=57, hints=3 WHERE id=?", (read,))
            lessons.tidy(conn, now=NOW)
            row = conn.execute("SELECT status, verdict FROM lesson WHERE id=?", (read,)).fetchone()
        self.assertEqual(tuple(row), ("active", "effective"))


class CancelTest(Scratch):
    def test_a_pass_told_to_stop_stops_between_phases(self):
        from hermes_atlas_evolution import evolve

        asked, ran = [], []

        def cancelled():
            asked.append(True)
            return len(asked) >= 2   # after the replay and the environment phase

        with patch.object(evolve.lessons, "derive", lambda *a, **k: ran.append("derive") or []), \
                patch.object(evolve, "_daily_checks", lambda *a, **k: ran.append("daily") or {}):
            with self.assertRaises(evolve.Cancelled):
                evolve.cycle(self.home, now=NOW, config={"evolution": {"lessons": "auto"}}, strict=False,
                             cancelled=cancelled)
        self.assertEqual(ran, [])   # the first build ran every phase beside the pass that held the lease


class PluginHostKindTest(unittest.TestCase):
    def test_the_plugin_host_records_the_version_it_loaded(self):
        from unittest.mock import patch as _patch

        from hermes_atlas_evolution import status as _status

        with _patch.dict(os.environ, {"HERMES_PLUGIN_HOST_PROCESS": "1"}), \
                _patch.object(_status.sys, "argv", ["/usr/bin/python3", "-m", "hermes_cli.plugin_host_child"]):
            self.assertEqual(_status.process_kind(), "plugin_host")
        with _patch.dict(os.environ, {"HERMES_PLUGIN_HOST_PROCESS": ""}), \
                _patch.object(_status.sys, "argv", ["hermes", "chat"]):
            self.assertEqual(_status.process_kind(), "cli")


if __name__ == "__main__":
    unittest.main()
