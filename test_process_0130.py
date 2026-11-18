"""0.13.0: the plugin's own processes, each confirmed finding of the background audit as a regression: the daily checks
of a learning pass end by its deadline (and Hermes' check has a time budget of its own), the session database's
quick check keeps to its budget, a command's output is kept to its start and end, a command that ended is never a
timeout because a helper it left holds its output, the doctor's checks end with the process that asked for them,
`hermes pm gc` waits when the running programs could not all be read, and no command the plugin starts inherits an
approval bypass (--yolo, auto-approve)."""

import contextlib
import io
import json
import os
import runpy
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_atlas_evolution import admin, doctor, evolve, hermes_care, system_update
from hermes_atlas_evolution.experience import experience

os.environ.setdefault("ATLAS_EVOLUTION_NO_HERMES", "1")   # never the real Hermes of this machine
POSIX = unittest.skipUnless(os.name == "posix", "POSIX only: process groups, orphans adopted by another parent")
BYPASS = {"HERMES_YOLO_MODE": "1", "HERMES_YOLO": "1", "HERMES_ACP_AUTO_APPROVE": "1", "HERMES_ACCEPT_HOOKS": "1"}
# prints the approval bypasses this process inherited, as JSON
SHOW_BYPASS = ("import json, os; print(json.dumps(sorted(k for k in os.environ if k.upper() in "
               + repr(tuple(BYPASS)) + ")))")


def alive(pid: int) -> bool:
    """Whether a process still runs (POSIX). A zombie (ended, not yet reaped by whoever adopted it) has ended."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as stream:
            return stream.read().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
    except (OSError, IndexError):
        return True


def end(pid: int) -> None:
    """Kill a process a test started (cleanup)."""
    try:
        os.kill(pid, signal.SIGKILL if os.name == "posix" else signal.SIGTERM)
    except OSError:
        pass


def wait_for(predicate, seconds: float) -> bool:
    give_up = time.monotonic() + seconds
    while time.monotonic() < give_up:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class Scratch(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.home = self.base / "hermes"
        self.home.mkdir()


# ── 3. a command's output is kept to its start and its end ──────────────────────────────────────────────────────
class OutputTest(Scratch):
    LINES = ("import sys\nsys.stdout.write('first\\n')\nline = 'x' * 1023 + '\\n'\n"
             "for _ in range({count}): sys.stdout.write(line)\nsys.stdout.write('last\\n')\n")

    def test_run_command_keeps_the_start_and_the_end_in_bounded_memory(self):
        """sub_tests.py: 400 MB of output took 1.2 GB of memory. 32 MB now: what is kept and the memory are bounded."""
        tracemalloc.start()
        try:
            done = admin.run_command([sys.executable, "-c", self.LINES.format(count=32 * 1024)], timeout=120)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(done.returncode, 0)
        self.assertLessEqual(len(done.stdout), admin.OUTPUT_HEAD + admin.OUTPUT_TAIL + 2)
        self.assertTrue(done.stdout.startswith("first\nxxx"))
        self.assertTrue(done.stdout.endswith("x\nlast\n"))
        self.assertIn("\n…\n", done.stdout)
        self.assertTrue(all(line in ("first", "…", "last") or line == "x" * 1023
                            for line in done.stdout.splitlines()))   # never half a line either side of the cut
        self.assertLess(peak, 12 * 2**20)

    def test_a_short_output_is_kept_whole_and_decoded_as_text_mode_would(self):
        done = admin.run_command([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'a\\r\\nb\\rc\\n'); "
                                  "sys.stderr.buffer.write('é\\n'.encode()); sys.exit(3)"], timeout=60)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (3, "a\nb\nc\n", "é\n"))

    def test_the_system_checks_keep_a_bounded_output_and_a_package_list_whole(self):
        host = system_update.Host()
        code, out, _err = host.run([sys.executable, "-c", self.LINES.format(count=12 * 1024)], timeout=120)
        self.assertEqual(code, 0)
        self.assertLessEqual(len(out), system_update.OUTPUT_HEAD + system_update.OUTPUT_TAIL + 2)
        self.assertTrue(out.endswith("last\n"))
        line = "Inst package{} [1.0] (2.0 Ubuntu:26.04/resolute-updates [amd64])\n"
        code, out, _err = host.run([sys.executable, "-c", f"import sys\nfor n in range(6000): "
                                    f"sys.stdout.write({line!r}.format(n))"], timeout=120)
        self.assertEqual((code, out), (0, "".join(line.format(n) for n in range(6000))))   # an apt simulation or a
        # package list (hundreds of KB) is never cut

    def test_the_doctor_keeps_its_answer_the_last_line(self):
        script = (f"import sys\nfor _ in range(5000): sys.stdout.write('y' * 1023 + '\\n')\n"
                  f"sys.stdout.write({doctor.MARKER!r} + '{{\"checks\": []}}\\n')\n")
        out = doctor._spawn([sys.executable, "-c", script], env=dict(os.environ), cwd=str(self.base), timeout=120)
        self.assertLessEqual(len(out), doctor.OUTPUT_MAX + 2)
        self.assertEqual(out.splitlines()[-1], doctor.MARKER + '{"checks": []}')


# ── 4. a command that ended is never a timeout ──────────────────────────────────────────────────────────────────
class EndedCommandTest(Scratch):
    # exits 0 at once, leaving a helper that holds its output (a daemon it started)
    LEADER = ("import subprocess, sys\n"
              "helper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'], stdout=sys.stdout,"
              " stderr=sys.stderr, stdin=subprocess.DEVNULL)\n"
              "print(helper.pid)\nprint('started')\n")

    def helper(self, text: str) -> int:
        pid = int(text.split()[0])
        self.addCleanup(end, pid)
        return pid

    def test_a_helper_holding_the_output_is_left_alone(self):
        """sub_tests.py: `sh -c "sleep 30 & echo; exit 0"` with timeout 6 was TimeoutExpired at 6.1 s, the helper
        killed."""
        started = time.monotonic()
        done = admin.run_command([sys.executable, "-c", self.LEADER], timeout=10)
        seconds = time.monotonic() - started
        pid = self.helper(done.stdout)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout.split()[1:], ["started"])
        self.assertLess(seconds, admin.EOF_GRACE_SECONDS + 5)
        if os.name == "posix":
            self.assertTrue(alive(pid))   # not ours to end: the command succeeded

    def test_hermes_output_says_what_it_printed_not_did_not_answer(self):
        (self.home / "config.yaml").write_text("{}\n")
        code, out = admin.hermes_output(self.home, ["x"], executable=sys.executable, timeout=10,
                                        runner=lambda argv, **kw: admin.run_command(
                                            [sys.executable, "-c", self.LEADER], **kw))
        self.helper(out)
        self.assertEqual(code, 0)
        self.assertIn("started", out)

    def test_a_timeout_still_ends_the_command_and_keeps_what_it_printed(self):
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired) as raised:
            admin.run_command([sys.executable, "-c", "import time; print('a', flush=True); time.sleep(30)"], timeout=1)
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(raised.exception.output, "a\n")


# ── 5. the doctor's checks end with the process that asked for them ─────────────────────────────────────────────
class DoctorWatchdogTest(Scratch):
    def fake_hermes(self) -> Path:
        """A hermes_cli whose import never ends, after starting a helper; both pids go to <base>/pids."""
        fake = self.base / "fake" / "hermes_cli"
        fake.mkdir(parents=True)
        (fake / "__init__.py").write_text(
            "import os, subprocess, sys, time\n"
            "folder = os.environ['FAKE_PIDS']\n"
            "helper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], stdin=subprocess.DEVNULL,"
            " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "with open(os.path.join(folder, 'pids.tmp'), 'w') as stream:\n"
            "    stream.write(f'{os.getpid()} {helper.pid}')\n"
            "os.replace(os.path.join(folder, 'pids.tmp'), os.path.join(folder, 'pids'))\n"
            "time.sleep(60)\n")
        return fake.parent

    def environment(self, **more) -> dict:
        env = doctor._environment(self.home)
        env.update({"ATLAS_EVOLUTION_PATH": json.dumps([str(self.fake_hermes())]), "FAKE_PIDS": str(self.base), **more})
        return env

    def pids(self) -> list[int]:
        pids = [int(item) for item in (self.base / "pids").read_text().split()]
        for pid in pids:
            self.addCleanup(end, pid)
        return pids

    @POSIX
    def test_the_checks_end_when_the_plugin_process_dies(self):
        """orphan.py: the parent SIGKILLed, the child kept running with PPID 1."""
        env = self.environment()
        repo = str(Path(doctor.__file__).resolve().parent.parent)
        code = ("import json, sys\nsys.path.insert(0, %r)\nfrom hermes_atlas_evolution import doctor\n"
                "doctor._spawn([sys.executable, '-I', '-c', doctor.BOOT], env=json.loads(%r), cwd=%r, timeout=120)\n"
                % (repo, json.dumps(env), str(self.home)))
        parent = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL)
        self.addCleanup(lambda: (end(parent.pid), parent.wait()))
        self.assertTrue(wait_for((self.base / "pids").exists, 30), "the checks never started")
        checks, helper = self.pids()
        self.assertTrue(alive(checks) and alive(helper))
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait()
        self.assertTrue(wait_for(lambda: not alive(checks) and not alive(helper), 10),
                        "the doctor's checks outlived the plugin process")

    def test_the_checks_end_by_their_own_limit(self):
        """The bound that holds on Windows too, where nothing shows a parent's death."""
        started = time.monotonic()
        out = doctor._spawn([sys.executable, "-I", "-c", doctor.BOOT], env=self.environment(ATLAS_EVOLUTION_LIMIT="1"),
                            cwd=str(self.home), timeout=30)
        self.assertLess(time.monotonic() - started, 20)
        self.assertNotIn(doctor.MARKER, out)
        checks, helper = self.pids()
        if os.name == "posix":   # its whole process group went with it
            self.assertTrue(wait_for(lambda: not alive(helper), 5))

    def test_a_run_tells_the_checks_their_limit(self):
        seen = []
        doctor.run(self.home, runner=lambda argv, **kw: seen.append(kw["env"]) or SimpleNamespace(stdout=""),
                   timeout=40)
        self.assertEqual(seen[0]["ATLAS_EVOLUTION_LIMIT"], "55")
        self.assertTrue(doctor.BOOT.startswith(doctor.WATCHDOG))


# ── 7. no command the plugin starts inherits an approval bypass ─────────────────────────────────────────────────
class EnvironmentTest(Scratch):
    def setUp(self):
        super().setUp()
        patcher = patch.dict(os.environ, BYPASS)
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.home / "config.yaml").write_text("{}\n")

    def test_hermes_commands_never_carry_yolo_or_auto_approve(self):
        seen = []

        def runner(argv, **kw):
            seen.append(kw["env"])
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        admin.run_hermes(self.home, ["config", "set", "x", "y"], runner=runner, executable="/usr/bin/hermes")
        admin.hermes_output(self.home, ["doctor"], runner=runner, executable="/usr/bin/hermes")
        for env in seen:
            self.assertFalse(set(BYPASS) & set(env))
            self.assertEqual(env["HERMES_HOME"], str(self.home))

    def test_every_child_the_plugin_starts_is_without_them(self):
        done = admin.run_command([sys.executable, "-c", SHOW_BYPASS], env=dict(os.environ), timeout=60)
        self.assertEqual(json.loads(done.stdout), [])
        done = admin.run_command([sys.executable, "-c", SHOW_BYPASS], timeout=60)   # this process' environment
        self.assertEqual(json.loads(done.stdout), [])
        code, out, _err = system_update.Host().run([sys.executable, "-c", SHOW_BYPASS], timeout=60)
        self.assertEqual((code, json.loads(out)), (0, []))
        self.assertFalse(set(BYPASS) & set(doctor._environment(self.home)))
        self.assertEqual(admin.child_environment({"hermes_yolo_mode": "1", "PATH": "/bin"}), {"PATH": "/bin"})

    def test_the_digest_script_drops_them_from_the_schedulers_environment(self):
        spec = evolve.digest_schedule_spec(self.home)
        seen = []

        def run(argv, **kw):
            seen.append(kw["env"])
            return SimpleNamespace(returncode=0, stdout="digest")

        script = self.base / spec["script_name"]
        script.write_text(spec["script_content"], encoding="utf-8")
        with patch("shutil.which", return_value="/usr/bin/hermes"), patch("subprocess.run", run), \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            runpy.run_path(str(script), run_name="__main__")   # as the scheduler runs it, with nothing started
        self.assertEqual(printed.getvalue().strip(), "digest")
        self.assertEqual(len(seen), 2)
        for env in seen:
            self.assertFalse(set(BYPASS) & set(env))
            self.assertEqual(env["HERMES_HOME"], str(self.home.resolve()))


# ── 1. the daily checks end by the pass' deadline; 2. the quick check keeps to its budget ──────────────────────
class Slow:
    """A machine whose every command runs to its timeout (a wedged systemd bus)."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        time.sleep(timeout)
        return 124, "", f"{argv[0]}: timed out"


class DeadlineTest(Scratch):
    def setUp(self):
        super().setUp()
        (self.home / "config.yaml").write_text("{}\n")
        (self.base / "root" / "proc").mkdir(parents=True)
        checkout = self.base / "checkout"   # `hermes pm gc`'s store: a checkout of nothing, never this machine's
        checkout.mkdir()
        patcher = patch.object(hermes_care, "hermes_checkout", lambda: (checkout, None))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(system_update._UNIT_CACHE.clear)

    def test_the_daily_checks_wait_for_a_pass_with_time_left(self):
        """overrun.py: started with 31 s left, the checks returned 14.4 s past the pass' deadline."""
        calls = []

        def care(conn, home, **kw):
            calls.append(("hermes", kw.get("deadline")))
            return {"findings": []}

        def system(conn, home, **kw):
            calls.append(("system", kw.get("deadline")))
            return {"findings": []}

        with patch.object(hermes_care, "cached", care), patch.object(system_update, "cached", system):
            self.assertEqual(evolve._daily_checks(self.home, now=time.time(), deadline=time.monotonic() + 299), {})
            self.assertEqual(calls, [])
            deadline = time.monotonic() + 301
            evolve._daily_checks(self.home, now=time.time(), deadline=deadline)
        self.assertEqual(calls[0], ("hermes", deadline))
        if Path("/etc/os-release").exists():
            self.assertEqual(calls[1], ("system", deadline))

    def test_the_daily_checks_end_by_the_deadline_on_a_wedged_machine(self):
        slow = Slow()

        def run(host, argv, *, timeout=60):   # Host.run's own time budget, then the wedged command
            if host.deadline is not None and host.deadline - time.monotonic() < 1:
                host.skipped += 1
                return 124, "", "time budget spent"
            if host.deadline is not None:
                timeout = max(1, min(timeout, int(host.deadline - time.monotonic())))
            return slow(argv, timeout)

        with patch.object(evolve, "DAILY_CHECKS_SECONDS", 2), patch.object(system_update.Host, "run", run):
            deadline = time.monotonic() + 4
            evolve._daily_checks(self.home, now=time.time(), deadline=deadline)
        self.assertLess(time.monotonic() - deadline, 1.0)

    def test_hermes_check_ends_by_its_deadline_and_says_what_it_could_not_read(self):
        host = system_update.Host(root=self.base / "root", runner=Slow())
        started = time.monotonic()
        report = hermes_care.check(self.home, root=self.base / "root", host=host, deadline=started + 3)
        self.assertLess(time.monotonic() - started, 4.0)
        self.assertIn("unverified:system", report["gc_unsafe"])
        self.assertTrue(hermes_care.gc_blocked(report))
        self.assertIsNone(host.deadline)   # the host's own budget is given back
        self.assertGreater(host.skipped, 0)

    def test_a_quick_check_left_out_for_lack_of_time_is_taken_next_time(self):
        self.make_database()
        quiet = system_update.Host(root=self.base / "root", runner=lambda argv, timeout: (1, "", ""))
        with experience(self.home) as conn:
            found = hermes_care.cached(conn, self.home, now=1000.0, host=quiet, deadline=time.monotonic() + 0.5)
            self.assertNotIn("quick_check", found["database"])
            self.assertIsNone(conn.execute("SELECT value FROM cursor WHERE key='care_quick_check'").fetchone())
            found = hermes_care.cached(conn, self.home, now=1000.0, host=quiet, force=True)
            self.assertEqual(found["database"]["quick_check"], "ok")
            self.assertIsNotNone(conn.execute("SELECT value FROM cursor WHERE key='care_quick_check'").fetchone())

    def make_database(self):
        conn = sqlite3.connect(self.home / "state.db")
        conn.execute("CREATE TABLE t(x)")
        conn.executemany("INSERT INTO t VALUES (?)", [("x" * 500,)] * 2000)
        conn.commit()
        conn.close()

    def test_the_quick_check_asks_for_the_time_often(self):
        """At every 10,000 steps a 5 s budget became 9.9 s on a 441 MB state.db (a 1 s one: 3.42 s; at 100: 1.02)."""
        self.make_database()
        connect, steps = sqlite3.connect, []

        class Connection:
            def __init__(self, conn):
                self.conn = conn

            def set_progress_handler(self, handler, n):
                steps.append(n)
                return self.conn.set_progress_handler(handler, n)

            def __getattr__(self, name):
                return getattr(self.conn, name)

        with patch.object(hermes_care.sqlite3, "connect", lambda *a, **kw: Connection(connect(*a, **kw))):
            report = hermes_care.database(self.home, quick=True)
        self.assertEqual(report["quick_check"], "ok")
        self.assertLessEqual(steps[0], 1000)
        self.assertNotIn("quick_check", hermes_care.database(self.home, quick=True, budget=0.5))


# ── 6. gc waits when the running programs could not all be read ─────────────────────────────────────────────────
class ProcessesUnknownTest(Scratch):
    def setUp(self):
        super().setUp()
        tools = self.home / "tools"
        (tools / "python-3.14.10").mkdir(parents=True)
        (tools / "python-3.14.9").mkdir()
        (tools / "facts.json").write_text(json.dumps({"schema": 1, "packages": {"python": {"entry": "python-3.14.10"}}}))
        checkout = self.base / "checkout"
        checkout.mkdir()
        patcher = patch.object(hermes_care, "hermes_checkout", lambda: (checkout, None))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = self.base / "machine"
        (self.root / "proc" / "4242").mkdir(parents=True)
        (self.root / "proc" / "4243").mkdir()

    def test_without_proc_gc_waits(self):
        """macOS and Windows have no /proc: gc ran without knowing what runs from the store."""
        found = hermes_care.tool_store(self.home, root=self.base / "nowhere")
        self.assertIn("processes_unknown:no_proc", found["gc_unsafe"])
        self.assertTrue(hermes_care.gc_blocked(found))
        en, pt = (hermes_care.gc_unsafe_words(found["gc_unsafe"], lang) for lang in ("en", "pt"))
        self.assertEqual(len(en), 1)
        self.assertIn("(no /proc)", en[0])
        self.assertIn("(sem /proc)", pt[0])

    def test_a_scan_out_of_time_or_cut_short_holds_gc_back(self):
        """running_programs stopped silently when its budget ran out (budget=0: no process, no flag)."""
        found = hermes_care.tool_store(self.home, root=self.root, budget=0)
        self.assertIn("processes_unknown:budget", found["gc_unsafe"])
        self.assertTrue(hermes_care.gc_blocked(found))
        self.assertIn("could not all be read", hermes_care.gc_unsafe_words(found["gc_unsafe"], "en")[0])
        self.assertIn("não puderam ser todos lidos", hermes_care.gc_unsafe_words(found["gc_unsafe"], "pt")[0])
        with patch.object(hermes_care, "PROCESS_SCAN", 1):
            self.assertIn("processes_unknown:too_many", hermes_care.tool_store(self.home, root=self.root)["gc_unsafe"])

    def test_a_whole_scan_or_a_given_list_lets_gc_run(self):
        self.assertEqual(hermes_care.scan_programs(self.root), ([], None))
        found = hermes_care.tool_store(self.home, root=self.root)
        self.assertEqual(found["gc_unsafe"], [])
        self.assertFalse(hermes_care.gc_blocked(found))
        self.assertEqual(hermes_care.tool_store(self.home, root=self.base / "nowhere", processes=())["gc_unsafe"], [])


if __name__ == "__main__":
    unittest.main()
