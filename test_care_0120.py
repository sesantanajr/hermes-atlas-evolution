"""0.12.0: the gateway's repair never starts a second gateway, and `hermes pm gc` never deletes what runs.

Regressions of an audit of 0.11.0's health and gateway-repair code, each on a machine made here (system_update.Host
with a fake systemd: this machine's systemctl is never asked, test_care_084.hermetic_start):
- a gateway Hermes calls "degraded" or "starting" is a running gateway (its process, start time, heartbeat, pid file,
  lock, host record decide), and nothing starts while any gateway of the Hermes runs;
- `hermes pm gc` deletes every folder facts.json does not name, whatever its name: one a service or a running
  program uses blocks it, and the Mode auto's gc looks at the machine again right before;
- every gateway unit of both scopes is looked at before a start, with whether it comes back after a reboot;
- the unit names are Hermes' own (a staging home never takes production's `hermes-gateway.service`) and a unit that
  runs another HERMES_HOME is never rewritten;
- an ExecStart path with spaces, ExecStop= lines naming the old Python, a Type=notify start that takes a while;
- the root's unit first for a profile the root serves; the journal of repairs, undo back to the original, from any
  profile, and an undone repair never repeated by itself.
"""

import contextlib
import fnmatch
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_atlas_evolution import actions, admin, hermes_care, system_update
from hermes_atlas_evolution.cli import _care_action_md, _gateway_repair
from hermes_atlas_evolution.experience import experience

from test_care_084 import hermetic_start, hermetic_stop

NOW = time.time()
POSIX = unittest.skipUnless(os.name == "posix", "POSIX only: file modes, systemd paths, os.kill probes")
LINUX = unittest.skipUnless(sys.platform.startswith("linux") and Path("/proc/self/stat").exists(),
                            "Linux only: /proc and /proc/locks")
_GUARD = None


def setUpModule():
    global _GUARD
    _GUARD = hermetic_start(status_look=True)


def tearDownModule():
    hermetic_stop(_GUARD)


def exec_value(path: str, args: str = "-m hermes_cli.main gateway run") -> str:
    return (f"{{ path={path} ; argv[]={path} {args} ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; "
            "pid=0 ; code=(null) ; status=0/0 }")


class Systemd:
    """systemd for system_update.Host: units per scope ({property: value} as `systemctl show` prints them, and
    `file_state`, what list-unit-files says, None for a unit only loaded), with every command recorded."""

    def __init__(self):
        self.units: dict = {}
        self.calls: list = []
        self.is_active: list = []      # answers of `is-active` after a start, in order (then the unit's state)
        self.started_state = "active"
        self.failing_scopes: set = set()

    def add(self, scope: str, name: str, *, file_state="enabled", **props):
        unit = {"Id": name, "LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead", "Result": "success",
                "NRestarts": "0", "ExecMainCode": "0", "ExecMainStatus": "0", "FragmentPath": "", "DropInPaths": "",
                "NeedDaemonReload": "no", "MainPID": "0", "User": "", "Environment": "", "EnvironmentFiles": "",
                "file_state": file_state}
        unit.update(props)
        self.units[(scope, name)] = unit
        return unit

    def failed(self, scope: str, name: str, program: str, fragment: str, **props):
        """A unit systemd cannot start: its program is gone (status=203/EXEC), in a restart loop."""
        return self.add(scope, name, ActiveState="failed", SubState="failed", Result="exit-code", NRestarts="5",
                        ExecMainCode="1", ExecMainStatus="203", ExecStart=exec_value(program), FragmentPath=fragment,
                        **props)

    def mutating(self) -> list:
        return [call for call in self.calls if any(word in call for word in ("start", "daemon-reload", "reset-failed",
                                                                             "stop", "restart"))]

    def __call__(self, argv, timeout):
        argv = [str(part) for part in argv]
        self.calls.append(argv)
        if argv[0] == "which":
            return 0, "/usr/bin/x\n", ""
        if argv[:2] == ["id", "-u"]:
            return 0, "0\n", ""
        if argv[0] == "loginctl":
            return 0, "Linger=yes\n", ""
        if argv[0] != "systemctl":
            return 0, "", ""
        scope = "user" if "--user" in argv else "system"
        rest = [part for part in argv[1:] if part != "--user"]
        if scope in self.failing_scopes:
            return 1, "", f"Failed to connect to {scope} scope bus via local transport: No medium found"
        mine = {name: unit for (where, name), unit in self.units.items() if where == scope}
        verb = rest[0]
        if verb == "list-unit-files":
            pattern = rest[-1]
            lines = [f"{name} {unit['file_state']} enabled" for name, unit in mine.items()
                     if unit["file_state"] is not None and fnmatch.fnmatch(name, pattern)]
            return (0 if lines else 1), "".join(line + "\n" for line in lines), ""
        if verb == "list-units":
            if "--state=failed" in rest or "--state=running" in rest:
                wanted = "failed" if "--state=failed" in rest else "running"
                lines = [f"{name} loaded {unit['ActiveState']} {unit['SubState']} x" for name, unit in mine.items()
                         if unit["SubState" if wanted == "running" else "ActiveState"] == wanted]
            else:
                pattern = rest[-1]
                lines = [f"{name} loaded {unit['ActiveState']} {unit['SubState']} x" for name, unit in mine.items()
                         if fnmatch.fnmatch(name, pattern)]
            return 0, "".join(line + "\n" for line in lines), ""
        if verb == "show":
            names = rest[1:rest.index("-p")] if "-p" in rest else rest[1:]
            blocks = []
            for name in names:
                unit = mine.get(name) or {"Id": name, "LoadState": "not-found", "ActiveState": "inactive"}
                lines = []
                for key, value in unit.items():
                    if key == "file_state":
                        continue
                    for part in str(value).split("\n"):   # systemd prints one line per command
                        lines.append(f"{key}={part}")
                blocks.append("\n".join(lines))
            return 0, "\n\n".join(blocks) + "\n", ""
        if verb == "is-active":
            name = rest[-1]
            if self.is_active:
                return 0, self.is_active.pop(0) + "\n", ""
            state = (mine.get(name) or {}).get("ActiveState", "inactive")
            return (0 if state == "active" else 3), state + "\n", ""
        if verb == "start":
            unit = mine.get(rest[-1])
            if unit is not None:
                unit["ActiveState"], unit["SubState"] = self.started_state, "running"
            return 0, "", ""
        if verb == "show-environment":
            return 0, "LANG=C\n", ""
        return 0, "", ""


def own_start() -> int | None:
    return hermes_care._started(os.getpid())


@POSIX
class Machine(unittest.TestCase):
    """A Hermes at self.home (a real folder), and a machine (self.root, its files) whose user unit for that Hermes
    starts a Python Hermes removed; the current one is in /srv/h/tools/python-3.14.10."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.root, self.home = self.base / "machine", self.base / "hermes"
        (self.home / "state").mkdir(parents=True)
        (self.home / "config.yaml").write_text("{}\n")
        (self.root / "proc").mkdir(parents=True)
        self.tools = "/srv/h/tools"
        self.python("python-3.14.10")
        self.facts("python-3.14.10")
        self.old = f"{self.tools}/python-3.14.9/bin/python3"
        self.name = hermes_care.gateway_unit_name(self.home)
        self.unit_file = f"/u/.config/systemd/user/{self.name}"
        self.write_unit(self.old)
        self.systemd = Systemd()
        self.systemd.failed("user", self.name, self.old, self.unit_file, Environment=f"HERMES_HOME={self.home}",
                            ExecStop=exec_value(self.old, "-m hermes_cli.main gateway stop"))
        self.host = system_update.Host(root=self.root, runner=self.systemd)
        patcher = patch.dict(os.environ, {"HERMES_GATEWAY_LOCK_DIR": str(self.base / "locks")})
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("time.sleep", lambda seconds: None)   # a start being watched never waits here
        patcher.start()
        self.addCleanup(patcher.stop)

    def python(self, folder: str):
        program = self.root / self.tools.lstrip("/") / folder / "bin" / "python3"
        program.parent.mkdir(parents=True, exist_ok=True)
        program.write_text("#!/bin/sh\n")
        program.chmod(0o755)

    def facts(self, entry: str):
        (self.root / self.tools.lstrip("/") / "facts.json").write_text(json.dumps(
            {"schema": 1, "packages": {"python": {"entry": entry}}}))

    def write_unit(self, program: str, *, home: Path | None = None):
        path = self.root / self.unit_file.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"[Service]\nEnvironment=HERMES_HOME={home or self.home}\n"
                        f"ExecStart={program} -m hermes_cli.main gateway run\n"
                        f"ExecStop=-{program} -m hermes_cli.main gateway stop\nRestart=always\n")
        return path

    def unit_text(self) -> str:
        return (self.root / self.unit_file.lstrip("/")).read_text()

    def check(self, home: Path | None = None, now: float = NOW) -> dict:
        with experience(home or self.home) as conn:
            return hermes_care.cached(conn, home or self.home, now=now, force=True, host=self.host)

    def gateway_state(self, state: str, *, pid: int | None = None, start: int | None = None, where=None, **more):
        where = where or self.home
        where.mkdir(parents=True, exist_ok=True)
        record = {"gateway_state": state, "pid": os.getpid() if pid is None else pid, **more}
        if start is not None:
            record["start_time"] = start
        (where / "gateway_state.json").write_text(json.dumps(record))


class GatewayRepair(Machine):
    def test_a_unit_whose_python_is_gone_is_repaired_when_no_gateway_runs(self):
        """The case the repair is for, with ExecStop= lines naming the old Python (rewritten too), a start that
        does not wait on systemd's job and a gateway that takes a while to say it is ready."""
        self.assertIn("gateway_exec_missing", self.check()["findings"])
        self.systemd.is_active = ["activating", "activating", "active"]
        seen = {}
        refresh = hermes_care.refresh_after_repair
        with patch.object(hermes_care, "refresh_after_repair",
                          side_effect=lambda *args, **kwargs: seen.update(kwargs) or refresh(*args, **kwargs)):
            result = actions.repair_gateway(self.home, now=NOW, host=self.host)
        self.assertTrue(result["ok"], result)
        self.assertIs(seen["host"], self.host)   # the check after the repair reads the same machine
        text = self.unit_text()
        self.assertNotIn("python-3.14.9", text)
        self.assertIn(f"ExecStop=-{self.tools}/python-3.14.10/bin/python3 -m hermes_cli.main gateway stop", text)
        self.assertIn(["systemctl", "--user", "start", "--no-block", self.name], self.systemd.calls)
        events = hermes_care.pending_repairs(self.home)
        self.assertEqual([(item["unit"], item["from"], item["actor"]) for item in events],
                         [(self.name, self.old, "automatic")])
        self.assertTrue((self.home / "evolution" / "gateway-repair.json").is_file())

    def test_a_degraded_or_starting_gateway_runs_and_nothing_is_started_beside_it(self):
        """Hermes writes "degraded" (no platform connected yet; it still runs cron) and "starting" for a live
        gateway: its process and start time say it runs, with no heartbeat at all."""
        self.assertIn("gateway_exec_missing", self.check()["findings"])
        before = self.unit_text()
        for state in ("degraded", "starting", "draining"):
            self.gateway_state(state, start=own_start())
            gate = hermes_care.gateway(self.home, now=NOW)
            self.assertTrue(gate["running"], state)
            self.assertIn("process", gate["evidence"])
            self.assertFalse(gate["crashed"])
            self.assertIsNone(actions.repair_gateway(self.home, now=NOW, host=self.host))
            with self.assertRaisesRegex(ValueError, "a gateway of this Hermes runs now"):
                _gateway_repair(self.home, "fix-gateway", "en", yes=True, now=NOW, host=self.host)
            with self.assertRaisesRegex(ValueError, "um gateway deste Hermes está rodando agora"):
                _gateway_repair(self.home, "fix-gateway", "pt", yes=True, now=NOW, host=self.host)
        self.assertEqual(self.unit_text(), before)
        self.assertEqual(self.systemd.mutating(), [])

    def test_the_auditors_degraded_gateway_with_a_fresh_heartbeat(self):
        """repro_degraded.py: degraded, its own pid and start time, a fresh heartbeat; the old code rewrote the
        leftover unit and started a second gateway."""
        self.gateway_state("degraded", start=own_start())
        (self.home / "state" / "gateway.heartbeat").write_text(json.dumps({"mem": {"rss_kib": 1000}}))
        care = self.check()
        self.assertTrue(care["gateway"]["running"])
        self.assertNotIn("gateway_exec_missing", care["findings"])
        with experience(self.home) as conn:   # even with an old check that still says it cannot start
            conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('care_check', ?)",
                         (json.dumps({**care, "findings": ["gateway_exec_missing"]}),))
        self.assertIsNone(actions.repair_gateway(self.home, now=NOW, host=self.host))
        self.assertEqual(self.systemd.mutating(), [])

    def test_its_pid_file_lock_host_record_or_another_profiles_gateway_count(self):
        self.assertEqual(hermes_care.live_gateways(self.home, now=NOW), [])
        (self.home / "gateway.pid").write_text(json.dumps({"pid": os.getpid(), "start_time": own_start()}))
        self.assertEqual(hermes_care.live_gateways(self.home, now=NOW)[0]["evidence"], ["pid_file"])
        (self.home / "gateway.pid").unlink()
        work = self.home / "profiles" / "work"   # a legacy per-profile gateway of the same Hermes
        self.gateway_state("running", start=own_start(), where=work)
        self.assertEqual([item["home"] for item in hermes_care.live_gateways(self.home, now=NOW)], [str(work)])
        (work / "gateway_state.json").unlink()
        locks = self.base / "locks"
        locks.mkdir()
        record = {"role": "gateway", "pid": os.getpid(), "startTime": own_start(), "home": str(self.home),
                  "profiles": ["default", "work"]}
        (locks / "host-gateway.json").write_text(json.dumps(record))
        with self.held(locks / "host-gateway.lock"):
            found = hermes_care.live_gateways(work, now=NOW)
            self.assertEqual([item["evidence"][0] for item in found], ["host_record"])
            (locks / "host-gateway.json").write_text(json.dumps({**record, "home": str(self.base / "other")}))
            self.assertEqual(hermes_care.live_gateways(self.home, now=NOW), [])   # another Hermes' gateway

    @contextlib.contextmanager
    def held(self, path: Path):
        """An flock on `path` held by this process (what a live gateway holds on its lock files)."""
        try:
            import fcntl
        except ImportError:
            yield
            return
        with open(path, "a+") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @LINUX
    def test_a_held_gateway_lock_is_a_running_gateway(self):
        (self.home / "gateway.lock").write_text("")
        self.assertEqual(hermes_care.live_gateways(self.home, now=NOW), [])
        with self.held(self.home / "gateway.lock"):
            self.assertEqual(hermes_care.live_gateways(self.home, now=NOW)[0]["evidence"], ["lock"])
            self.assertIsNone(actions.repair_gateway(self.home, now=NOW, host=self.host))

    @LINUX
    def test_a_pid_another_program_took_is_a_crash_and_a_gateway_like_one_is_not(self):
        other = subprocess.Popen(["/bin/sleep", "30"])
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        like = subprocess.Popen(["/bin/sh", "-c", "sleep 30; :", "hermes-gateway"])
        self.addCleanup(like.wait)
        self.addCleanup(like.kill)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (hermes_care._cmdline(other.pid) or "").startswith("/bin/sleep"):
            time.sleep(0.02)
        self.gateway_state("running", pid=other.pid)   # no start time recorded: the command line decides
        self.assertTrue(hermes_care.gateway(self.home, now=NOW)["crashed"])
        self.gateway_state("running", pid=like.pid)
        self.assertTrue(hermes_care.gateway(self.home, now=NOW)["running"])
        self.gateway_state("running", pid=other.pid, start=hermes_care._started(other.pid))   # the start time wins
        self.assertTrue(hermes_care.gateway(self.home, now=NOW)["running"])

    def test_a_clean_stop_is_not_a_crash_and_its_last_heartbeat_says_nothing(self):
        self.gateway_state("stopped", pid=2**22 - 7)
        (self.home / "state" / "gateway.heartbeat").write_text("{}")
        gate = hermes_care.gateway(self.home, now=NOW)
        self.assertEqual((gate["running"], gate["crashed"]), (False, False))

    def test_every_gateway_unit_of_both_scopes_is_looked_at_before_a_start(self):
        """repro_probe.py: the system scope's hermes-gateway.service runs while this Hermes' user unit is broken;
        the old probe saw only user units with expected names and none of them said whether it was enabled."""
        self.check()
        self.systemd.add("system", "hermes-gateway.service", ActiveState="active", SubState="running",
                         ExecStart=exec_value("/usr/bin/true"))
        self.systemd.add("user", "hermes-gateway-old.service", file_state="disabled")
        self.systemd.add("user", "hermes-gateway-run-1.service", file_state=None, ActiveState="activating",
                         SubState="start")   # transient: only `list-units` knows it
        probe = system_update.probe_gateway_units(self.host)
        units = {(unit["scope"], unit["unit"]): unit for unit in probe["units"]}
        self.assertEqual(units[("user", "hermes-gateway-old.service")]["enabled"], False)
        self.assertIsNone(units[("user", "hermes-gateway-run-1.service")]["enabled"])
        self.assertEqual(units[("user", self.name)]["enabled"], True)
        self.assertIsNone(actions.repair_gateway(self.home, now=NOW, host=self.host))
        with self.assertRaises(ValueError) as stop:
            _gateway_repair(self.home, "fix-gateway", "en", yes=True, now=NOW, host=self.host)
        self.assertIn("the service `hermes-gateway.service` (system) is active", str(stop.exception))
        self.assertIn("`hermes-gateway-run-1.service` (user) is activating", str(stop.exception))
        self.assertEqual(self.systemd.mutating(), [])

    def test_a_scope_that_cannot_be_read_stops_the_start(self):
        self.check()
        self.systemd.failing_scopes.add("system")
        self.assertIsNone(actions.repair_gateway(self.home, now=NOW, host=self.host))
        with self.assertRaisesRegex(ValueError, "services could not be read here"):
            _gateway_repair(self.home, "fix-gateway", "en", yes=True, now=NOW, host=self.host)
        self.assertEqual(self.systemd.mutating(), [])

    def test_a_unit_that_runs_another_hermes_is_never_rewritten(self):
        """F4: the unit's HERMES_HOME, as systemd builds it (an EnvironmentFile overrides Environment=), must be this
        Hermes; the bare production name is never this staging home's."""
        self.check()
        before = self.unit_text()
        unit = self.systemd.units[("user", self.name)]
        unit["Environment"] = "HERMES_HOME=/root/.hermes PATH=/usr/bin"
        self.assertIsNone(actions.repair_gateway(self.home, now=NOW, host=self.host))
        with self.assertRaisesRegex(ValueError, r"runs another Hermes \(HERMES_HOME=/root/.hermes\)"):
            _gateway_repair(self.home, "fix-gateway", "en", yes=True, now=NOW, host=self.host)
        unit["Environment"] = f"HERMES_HOME={self.home}"
        env_file = self.root / "etc" / "hermes.env"
        env_file.parent.mkdir(parents=True)
        env_file.write_text("TOKEN=secret\nexport HERMES_HOME='/srv/staging'\n")
        unit["EnvironmentFiles"] = "/etc/hermes.env (ignore_errors=no)"
        detail = system_update.probe_gateway_units(self.host)["units"][0]
        self.assertNotIn("TOKEN", json.dumps(detail))   # only HERMES_HOME is ever kept from its environment
        self.assertEqual(system_update.service_home(self.host, detail), ("/srv/staging", "ok"))
        with self.assertRaises(ValueError):
            system_update.fix_unit_exec(self.host, detail, now=NOW, home=self.home)
        unit["EnvironmentFiles"] = "/etc/missing.env (ignore_errors=no)"
        detail = system_update.probe_gateway_units(self.host)["units"][0]
        self.assertEqual(system_update.service_home(self.host, detail), (None, "unreadable"))
        self.assertEqual(self.unit_text(), before)
        # the production gateway's bare name, seen from a staging home: not this Hermes' unit
        self.systemd.units.clear()
        self.systemd.failed("user", "hermes-gateway.service", self.old, self.unit_file,
                            Environment=f"HERMES_HOME={self.home}")
        bare = system_update.probe_gateway_units(self.host)["units"][0]
        whys = [item["why"] for item in hermes_care.start_blockers(self.home, bare, [bare], now=NOW, host=self.host)]
        self.assertEqual(whys, ["not_this_hermes"])
        self.assertEqual(self.systemd.mutating(), [])

    def test_unit_names_are_hermes_own(self):
        resolved = os.path.realpath(self.home)
        self.assertEqual(hermes_care.gateway_unit_name(self.home, owners=[]),
                         f"hermes-gateway-{hashlib.sha256(resolved.encode()).hexdigest()[:8]}.service")
        (self.home / "profiles" / "work").mkdir(parents=True)
        (self.home / "profiles" / "Bad").mkdir()
        self.assertEqual(hermes_care.gateway_unit_name(self.home / "profiles" / "work", owners=[]),
                         "hermes-gateway-work.service")
        self.assertTrue(hermes_care.gateway_unit_name(self.home / "profiles" / "Bad", owners=[])
                        .startswith("hermes-gateway-") and "Bad" not in
                        hermes_care.gateway_unit_name(self.home / "profiles" / "Bad", owners=[]))
        # the bare name: the account's own ~/.hermes, or the home the installed bare unit pins
        self.assertEqual(hermes_care.gateway_unit_name(self.home, owners=[str(self.home)]), "hermes-gateway.service")
        with patch.object(hermes_care, "_pinned_home", return_value=str(self.home)):
            self.assertEqual(hermes_care.gateway_unit_name(self.home), "hermes-gateway.service")
        with patch.object(system_update, "_account_home", return_value=self.base), \
                patch.object(hermes_care, "_pinned_home", return_value=None):
            native = self.base / ".hermes"
            native.mkdir()
            self.assertEqual(hermes_care.gateway_unit_name(native), "hermes-gateway.service")
            self.assertNotEqual(hermes_care.gateway_unit_name(self.home), "hermes-gateway.service")

    def test_for_a_profile_the_root_serves_the_roots_unit_comes_first(self):
        work = self.home / "profiles" / "work"
        work.mkdir(parents=True)
        (work / "config.yaml").write_text("{}\n")
        self.gateway_state("stopped", pid=2**22 - 7, served_profiles=["work"])
        self.assertTrue(hermes_care.gateway(work, now=NOW)["served_by_root"])
        self.assertEqual(hermes_care.expected_gateway_units(work, served=True, owners=[]),
                         [self.name, "hermes-gateway-work.service"])
        legacy = {"unit": "hermes-gateway-work.service", "scope": "user", "active_state": "failed",
                  "exec_missing": True}
        rooted = {"unit": self.name, "scope": "user", "active_state": "inactive", "enabled": True}
        self.assertIs(hermes_care.pick_gateway_unit([legacy, rooted], home=work, served=True), rooted)
        self.systemd.failed("user", "hermes-gateway-work.service", self.old, self.unit_file,
                            Environment=f"HERMES_HOME={work}")
        unit = [item for item in system_update.probe_gateway_units(self.host)["units"]
                if item["unit"] == "hermes-gateway-work.service"][0]
        whys = [item["why"] for item in hermes_care.start_blockers(work, unit, [unit], now=NOW, automatic=True,
                                                                   served=True, host=self.host)]
        self.assertEqual(whys, ["legacy_unit"])   # never started by itself: the root's gateway serves the profile

    def test_a_program_path_with_spaces(self):
        program = "/srv/hermes data/.hermes/tools/python-3.14.10/bin/python3"
        path = self.root / program.lstrip("/")
        path.parent.mkdir(parents=True)
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
        detail = system_update.unit_detail(self.host, "hermes-gateway.service", "user",
                                           {"ActiveState": "active", "ExecStart": exec_value(program)})
        self.assertEqual((detail["exec_path"], detail["exec_missing"]), (program, False))
        self.assertEqual(system_update._exec_program(f'[Service]\nExecStart=-"{program}" -m x\n'), program)

    def test_every_command_of_a_unit_keeps_its_tools_folder(self):
        """ExecStopPost= naming an old Python keeps that folder from `hermes pm gc` (and the repair rewrites it)."""
        store = self.home / "tools"
        for name in ("python-3.14.10", "python-3.14.9"):
            (store / name / "bin").mkdir(parents=True)
        (store / "facts.json").write_text(json.dumps({"schema": 1, "packages": {"python": {"entry": "python-3.14.10"}}}))
        raw = {"ActiveState": "active", "ExecStart": exec_value(f"{store}/python-3.14.10/bin/python3"),
               "ExecStopPost": exec_value("/bin/true") + "\n" + exec_value(f"{store}/python-3.14.9/bin/python3")}
        detail = system_update.unit_detail(self.host, "hermes-gateway.service", "user", raw)
        found = hermes_care.tool_store(self.home, in_use=[("hermes-gateway.service", path)
                                                          for path in detail["exec_paths"]], processes=())
        self.assertEqual(found["in_use_unreferenced"], ["python-3.14.9"])
        self.assertTrue(hermes_care.gc_blocked({**found}))

    def test_a_gateway_running_a_deleted_program_is_said_apart(self):
        self.gateway_state("running", start=own_start())
        unit = self.systemd.units[("user", self.name)]
        unit.update(ActiveState="active", SubState="running", ExecMainStatus="0", NRestarts="0")
        care = self.check()
        self.assertIn("gateway_program_deleted", care["findings"])
        self.assertNotIn("gateway_exec_missing", care["findings"])
        # the owner may fix the file (its next start works); systemd's start changes nothing for a running unit
        result = _gateway_repair(self.home, "fix-gateway", "pt", yes=True, now=NOW, host=self.host)
        self.assertTrue(result["ok"] and result["was_running"])
        self.assertIn("continua rodando o programa antigo", _care_action_md(result, "fix-gateway", "pt"))
        self.assertIn("keeps running its old program", _care_action_md(result, "fix-gateway", "en"))

    def test_a_slow_start_is_still_starting_not_failed(self):
        self.check()
        self.systemd.started_state = "activating"
        with patch.object(system_update, "START_WAIT_SECONDS", 0):
            result = _gateway_repair(self.home, "fix-gateway", "en", yes=True, now=NOW, host=self.host)
        self.assertEqual((result["ok"], result["state"]), (False, "activating"))
        self.assertIn("still starting", _care_action_md(result, "fix-gateway", "en"))
        self.assertIn("ainda está subindo", _care_action_md(result, "fix-gateway", "pt"))


class RepairJournal(Machine):
    def repair_again(self, folder: str):
        """Hermes moves its Python once more: the one the unit names is gone, `folder` is the current one."""
        current = system_update._exec_program(self.unit_text())
        self.python(folder)
        self.facts(folder)
        import shutil

        shutil.rmtree(self.root / current.lstrip("/").rsplit("/bin/", 1)[0])
        unit = self.systemd.units[("user", self.name)]
        unit.update(ActiveState="failed", SubState="failed", ExecMainStatus="203", ExecStart=exec_value(current))
        return _gateway_repair(self.home, "fix-gateway", "en", yes=True, now=NOW, host=self.host)

    def test_undo_walks_back_to_the_original_unit_from_any_profile(self):
        original = self.unit_text()
        self.check()
        first = actions.repair_gateway(self.home, now=NOW, host=self.host)
        self.assertTrue(first["ok"])
        after_first = self.unit_text()
        self.assertTrue(self.repair_again("python-3.14.11")["ok"])
        self.assertIn("python-3.14.11", self.unit_text())
        work = self.home / "profiles" / "work"
        work.mkdir(parents=True)
        (work / "config.yaml").write_text("{}\n")
        undone = _gateway_repair(work, "undo-gateway", "en", yes=True, now=NOW + 60, host=self.host)
        self.assertEqual((undone["ok"], undone["earlier"]), (True, 1))
        self.assertIn("1 earlier repair(s) can still be undone", _care_action_md(undone, "undo-gateway", "en"))
        self.assertEqual(self.unit_text(), after_first)
        again = _gateway_repair(work, "undo-gateway", "pt", yes=True, now=NOW + 120, host=self.host)
        self.assertEqual((again["ok"], again["earlier"]), (True, 0))
        self.assertEqual(self.unit_text(), original)
        with self.assertRaisesRegex(ValueError, "Não há reparo do gateway para desfazer"):
            _gateway_repair(work, "undo-gateway", "pt", yes=True, now=NOW + 180, host=self.host)
        with experience(self.home) as conn:   # the Mode auto's line in the lead's history is marked undone
            rows = conn.execute("SELECT undone FROM decision_log WHERE target='gateway_repair'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["undone"])

    def test_an_undone_repair_is_never_repeated_by_itself(self):
        self.check()
        self.assertTrue(actions.repair_gateway(self.home, now=NOW, host=self.host)["ok"])
        self.systemd.units[("user", self.name)].update(ActiveState="failed", SubState="failed", ExecMainStatus="203")
        _gateway_repair(self.home, "undo-gateway", "en", yes=True, now=NOW + 60, host=self.host)
        self.assertIn("python-3.14.9", self.unit_text())
        later = NOW + 3 * 86_400   # past the daily backoff: only the owner's "no" holds it back
        self.check(now=later)
        self.assertIsNone(actions.repair_gateway(self.home, now=later, host=self.host))
        self.assertIn("python-3.14.9", self.unit_text())
        # the owner repairs it by hand later: the situation is the Mode auto's again
        self.assertTrue(_gateway_repair(self.home, "fix-gateway", "en", yes=True, now=later, host=self.host)["ok"])
        self.assertFalse(hermes_care.repair_skipped(self.home, {"scope": "user", "unit": self.name,
                                                                "exec_path": self.old}))

    def test_undo_never_overwrites_a_file_changed_since(self):
        self.check()
        self.assertTrue(actions.repair_gateway(self.home, now=NOW, host=self.host)["ok"])
        path = self.root / self.unit_file.lstrip("/")
        path.write_text(path.read_text() + "# hermes gateway install wrote this\n")
        changed = path.read_text()
        with self.assertRaisesRegex(ValueError, "changed since the repair"):
            _gateway_repair(self.home, "undo-gateway", "en", yes=True, now=NOW + 60, host=self.host)
        self.assertEqual(path.read_text(), changed)
        with self.assertRaisesRegex(ValueError, "mudou depois do reparo"):   # the record itself refuses too
            system_update.undo_unit_exec(self.host, {**hermes_care.repair_events(self.home)[0]}, lang="pt")
        self.assertEqual(path.read_text(), changed)

    def test_a_record_of_0_11_is_still_undone(self):
        backup = self.root / (self.unit_file.lstrip("/") + ".atlas-20261101T000000Z-abc123.bak")
        backup.write_text(self.unit_text())
        self.write_unit(f"{self.tools}/python-3.14.10/bin/python3")
        (self.home / "evolution").mkdir()
        (self.home / "evolution" / "gateway-repair.json").write_text(json.dumps(
            {"unit": self.name, "scope": "user", "file": self.unit_file, "backup": str(backup)}))
        result = _gateway_repair(self.home, "undo-gateway", "en", yes=True, now=NOW, host=self.host)
        self.assertTrue(result["ok"])
        self.assertIn("python-3.14.9", self.unit_text())
        self.assertTrue(hermes_care.repair_skipped(self.home, {"scope": "user", "unit": self.name,
                                                               "exec_path": self.old}))


@POSIX
class ToolStore(unittest.TestCase):
    """`hermes pm gc` (pm.cli._gc_store) removes every folder of the store facts.json does not name."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / "h"
        self.tools = self.home / "tools"
        for name in ("python-3.14.10", "python-3.14.9", "atlas-native-runtime", "fetch-0a1b", ".previous-node",
                     ".staging-x", "chromium-1208"):
            (self.tools / name / "bin").mkdir(parents=True)
            (self.tools / name / "blob").write_bytes(b"x" * 100)
        (Path(folder.name) / "elsewhere").mkdir()
        (self.tools / "linked").symlink_to(Path(folder.name) / "elsewhere")
        self.write_facts({"python": {"entry": "python-3.14.10"}, "agent-browser": {"entry": "chromium-1208"}})

    def write_facts(self, packages):
        (self.tools / "facts.json").write_text(json.dumps({"schema": 1, "packages": packages}))

    def test_a_service_running_from_any_folder_blocks_gc(self):
        """repro_store.py: a service started from tools/atlas-native-runtime/ (not a name Hermes manages)."""
        service = str(self.tools / "atlas-native-runtime" / "bin" / "server")
        found = hermes_care.tool_store(self.home, in_use=[("hermes-gateway.service", service)], processes=())
        self.assertEqual(found["in_use_unreferenced"], ["atlas-native-runtime"])
        self.assertEqual(found["in_use_by"], {"atlas-native-runtime": ["hermes-gateway.service"]})
        # gc removes the retired default's entry, download caches, a killed installer's staging folder; it keeps
        # dot folders (a restore point) and never follows a link
        self.assertEqual(sorted(item["folder"] for item in found["unused"]),
                         [".staging-x", "chromium-1208", "fetch-0a1b", "python-3.14.9"])
        self.assertTrue(hermes_care.gc_blocked(found))
        found = hermes_care.tool_store(self.home, processes=())
        self.assertFalse(hermes_care.gc_blocked(found))
        self.assertTrue(hermes_care.gc_blocked({**found, "gc_model": None}))   # a check by an older version

    def test_a_running_program_keeps_its_folder(self):
        root = self.home.parent / "machine"
        (root / "proc" / "4242").mkdir(parents=True)
        (root / "proc" / "4242" / "exe").symlink_to(self.tools / "python-3.14.9" / "bin" / "python3")
        (root / "proc" / "4243").mkdir()
        (root / "proc" / "4243" / "exe").symlink_to("/usr/bin/python3")
        found = hermes_care.tool_store(self.home, processes=hermes_care.running_programs(root))
        self.assertEqual(found["in_use_unreferenced"], ["python-3.14.9"])
        self.assertEqual(found["in_use_by"]["python-3.14.9"], ["pid 4242"])

    def test_no_facts_or_unreadable_facts_never_lets_gc_run(self):
        (self.tools / "facts.json").unlink()
        found = hermes_care.tool_store(self.home, processes=())
        self.assertTrue(found["gc_unsafe"][0].startswith("no_facts:"))
        self.assertTrue(hermes_care.gc_blocked(found))
        (self.tools / "facts.json").write_text("{not json")
        found = hermes_care.tool_store(self.home, processes=())
        self.assertTrue(found["gc_unsafe"][0].startswith("unreadable:"))
        self.assertTrue(hermes_care.gc_blocked(found))

    def test_the_mode_auto_looks_again_right_before_gc(self):
        from test_care_084 import NoUnits

        (self.tools / "big").mkdir()
        with open(self.tools / "big" / "blob", "wb") as stream:
            stream.truncate(400 * 2**20)   # sparse: its size counts, no disk is used
        (self.home / "config.yaml").write_text("{}\n")
        machine = Systemd()
        machine.add("user", "hermes-gateway-x.service", ActiveState="active", SubState="running",
                    ExecStart=exec_value(str(self.tools / "big" / "bin" / "python3")))
        host = system_update.Host(root=self.home.parent, runner=machine)
        stale = {**hermes_care.tool_store(self.home, processes=()), "at": NOW, "findings": ["unused_tools"]}
        with experience(self.home) as conn:
            conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('care_check', ?)", (json.dumps(stale),))
        calls = []

        def hermes(command, **kwargs):
            calls.append(command)
            from types import SimpleNamespace
            return SimpleNamespace(returncode=0, stdout="removed\n", stderr="")

        with patch.object(admin, "hermes_executable", return_value="/usr/local/bin/hermes"):
            self.assertIsNone(actions.reclaim(self.home, now=NOW, runner=hermes, host=host))
            self.assertEqual(calls, [])   # a service started from tools/big meanwhile: no gc
            with experience(self.home) as conn:   # (the check it took now says so; an older one did not)
                stored = json.loads(conn.execute("SELECT value FROM cursor WHERE key='care_check'").fetchone()[0])
                self.assertIn("tools_in_use_unreferenced", stored["findings"])
                conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('care_check', ?)", (json.dumps(stale),))
            result = actions.reclaim(self.home, now=NOW,
                                     runner=hermes, host=system_update.Host(root=self.home.parent, runner=NoUnits()))
        self.assertTrue(result["ok"])
        self.assertEqual(calls[0][-2:], ["pm", "gc"])



@POSIX
class Review2(Machine):
    """The second review: gc with a scope it could not read, the store and the dependency state gc really sweeps,
    PATH folders, undo past a repair that can no longer be undone, a start re-checked after the question, locks on
    filesystems that name their device differently."""

    def store(self, *folders: str, entry: str = "python-3.14.10"):
        tools = self.home / "tools"
        for folder in folders:
            (tools / folder / "bin").mkdir(parents=True, exist_ok=True)
        (tools / "facts.json").write_text(json.dumps({"schema": 1, "packages": {"python": {"entry": entry}}}))
        return tools

    def test_a_scope_that_cannot_be_read_holds_gc_back(self):
        """gc1.py: the user scope's bus is unreachable; its stopped gateway unit starts tools/python-3.14.9."""
        self.store("python-3.14.10", "python-3.14.9")
        self.systemd.failing_scopes.add("user")
        care = self.check()
        self.assertEqual(care["gc_unsafe"], ["unverified:user"])
        self.assertTrue(hermes_care.gc_blocked(care))
        self.assertNotIn("unused_tools", care["findings"])
        self.assertIn("could not be read here", hermes_care.gc_unsafe_words(care["gc_unsafe"], "en")[0])
        self.assertIn("não puderam ser lidos", hermes_care.gc_unsafe_words(care["gc_unsafe"], "pt")[0])

    def test_the_gc_of_the_mode_auto_reads_the_units_again_not_a_cached_list(self):
        seen = []
        units = system_update._user_units
        with experience(self.home) as conn:
            conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('care_check', ?)", (json.dumps(
                {"at": NOW, "findings": ["unused_tools"], "in_use_unreferenced": [], "gc_unsafe": [],
                 "gc_model": hermes_care.GC_MODEL}),))
        with patch.object(system_update, "_user_units", side_effect=lambda host, **kw: seen.append(kw) or units(host, **kw)):
            self.assertIsNone(actions.reclaim(self.home, now=NOW, runner=lambda *a, **k: None, host=self.host))
        self.assertEqual(seen, [{"fresh": True}])

    def test_a_folder_on_a_units_path_is_in_use(self):
        tools = self.store("python-3.14.10", "node-24.1.0")
        unit = self.systemd.units[("user", self.name)]
        unit["Environment"] = f'HERMES_HOME={self.home} "PATH={tools}/node-24.1.0/bin:/usr/bin"'
        care = self.check()
        self.assertEqual(care["in_use_unreferenced"], ["node-24.1.0"])
        env_file = self.root / "etc" / "gw.env"
        env_file.parent.mkdir(parents=True)
        env_file.write_text(f"SECRET=x\nPATH={tools}/node-24.1.0/bin\n")
        unit["Environment"] = f"HERMES_HOME={self.home}"
        unit["EnvironmentFiles"] = "/etc/gw.env (ignore_errors=no)"
        self.assertEqual(self.check()["in_use_unreferenced"], ["node-24.1.0"])

    def test_dependency_environments_and_runtime_generations_gc_collects_are_counted(self):
        self.store("python-3.14.10")
        state = self.home / "installs" / "0123456789abcdef"
        for generation in ("old", "new"):
            (state / "environments" / generation / "venv" / "bin").mkdir(parents=True)
            (state / "environments" / generation / ".lease-managed").write_text("")
        (state / "facts.json").write_text(json.dumps({"packages": {"venv": {
            "environment": str(state / "environments" / "new" / "venv")}}}))
        (state / "pm-runtime" / "generations" / "aborted" / "bin").mkdir(parents=True)
        (state / "pm-runtime" / "selected.json").write_text(json.dumps({"generation": "generations/current"}))
        (state / "inputs").mkdir()
        (state / "inputs" / ".project-root").write_text(str(self.base))   # the checkout exists: not an orphan
        uses = lambda path: hermes_care.tool_store(self.home, in_use=[("hermes-x.service", path)],  # noqa: E731
                                                   processes=())["in_use_unreferenced"]
        self.assertEqual(uses(str(state / "environments" / "new" / "venv" / "bin" / "python")), [])
        self.assertEqual(uses(str(state / "environments" / "old" / "venv" / "bin" / "python")),
                         ["installs/0123456789abcdef/environments/old"])
        self.assertEqual(uses(str(state / "pm-runtime" / "generations" / "aborted" / "bin" / "python")),
                         ["installs/0123456789abcdef/pm-runtime/generations/aborted"])
        (state / "inputs" / ".project-root").write_text(str(self.base / "deleted-checkout"))
        self.assertEqual(uses(str(state / "environments" / "new" / "venv" / "bin" / "python")),
                         ["installs/0123456789abcdef"])   # a gone checkout's whole state goes

    def test_the_store_gc_sweeps_is_hermes_own(self):
        """A staging home that borrows the checkout sweeps the owner's store (pm.environments._unstamped_store)."""
        checkout = self.base / "lib" / "hermes-agent"
        checkout.mkdir(parents=True)
        key = hashlib.sha256(str(checkout.resolve()).encode()).hexdigest()[:16]
        account = self.base / "account"
        owner = account / ".hermes"
        (owner / "installs" / key).mkdir(parents=True)
        (owner / "installs" / key / "facts.json").write_text("{}")
        staging = self.base / "staging" / ".hermes"
        staging.mkdir(parents=True)
        with patch.object(hermes_care, "hermes_checkout", return_value=(checkout, None)), \
                patch.dict(os.environ, {"HOME": str(account)}):
            os.environ.pop("HERMES_RUNTIME_DIR", None)
            self.assertEqual(hermes_care.gc_store(staging), (owner / "tools", "ok"))
            self.assertEqual(hermes_care.gc_store(owner), (owner / "tools", "ok"))
            (checkout / "install-stamp.json").write_text(json.dumps({"runtimeDir": str(self.base / "rt")}))
            self.assertEqual(hermes_care.gc_store(staging), ((self.base / "rt").resolve(), "ok"))
            (checkout / "install-stamp.json").unlink()
            (checkout / "manifest.json").write_text(json.dumps({"store": "store"}))
            self.assertEqual(hermes_care.gc_store(staging), (None, "payload"))
            with patch.dict(os.environ, {"HERMES_RUNTIME_DIR": str(self.base / "override")}):
                self.assertEqual(hermes_care.gc_store(staging)[0], (self.base / "override").resolve())
        with patch.object(hermes_care, "hermes_checkout", return_value=(None, None)):
            found = hermes_care.tool_store(self.home, processes=())
        self.assertEqual(found["gc_unsafe"], ["store_unknown:checkout_unknown"])
        self.assertTrue(hermes_care.gc_blocked(found))
        launcher = self.base / "bin" / "hermes"
        launcher.parent.mkdir()
        launcher.write_text(f"#!/bin/sh\nexec {self.base}/tools/python-3/bin/python3 -I -c 'import sys\n"
                            f"sys.path.insert(0, '\"'\"'{checkout}'\"'\"')\n'\n")
        wrapper = self.base / "bin" / "hermes-wrapper"
        wrapper.write_text(f'#!/bin/sh\nexec {launcher} "$@"\n')
        self.assertEqual(hermes_care._launcher_target(wrapper),
                         (checkout, self.base / "tools" / "python-3" / "bin" / "python3"))

    def write_repair(self, unit: str, at: float, *, changed: bool):
        """A repair in the journal whose unit file is as the repair left it, or changed since."""
        path = self.root / "u" / f"{unit}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ExecStart=/new/python\n")
        backup = path.with_name(path.name + f".atlas-{int(at)}.bak")
        backup.write_text("ExecStart=/old/python\n")
        hermes_care.record_repair_event(self.home, {
            "event": "repair", "at": at, "unit": unit, "scope": "user", "file": f"/u/{unit}", "backup": str(backup),
            "from": "/old/python", "to": "/new/python",
            "after_sha256": hashlib.sha256(b"ExecStart=/new/python\n").hexdigest()})
        if changed:
            path.write_text("ExecStart=/reinstalled/by/hermes\n")
        return path

    def test_a_repair_that_can_no_longer_be_undone_leaves_the_list_once(self):
        """j1.py: the newest repair's file was reinstalled since; it never blocks the older ones again."""
        older = self.write_repair("hermes-gateway-a.service", 1, changed=False)
        self.write_repair("hermes-gateway-b.service", 2, changed=True)
        result = _gateway_repair(self.home, "undo-gateway", "en", yes=True, now=NOW, host=self.host)
        self.assertEqual(older.read_text(), "ExecStart=/old/python\n")
        self.assertEqual([item["unit"] for item in result["retired"]], ["hermes-gateway-b.service"])
        self.assertIn("Left out (can no longer be undone", _care_action_md(result, "undo-gateway", "en"))
        self.assertIn("Saiu da lista", _care_action_md(result, "undo-gateway", "pt"))
        self.assertEqual(hermes_care.pending_repairs(self.home), [])
        self.write_repair("hermes-gateway-c.service", 3, changed=True)
        with self.assertRaisesRegex(ValueError, "Nenhum reparo do gateway pode mais ser desfeito"):
            _gateway_repair(self.home, "undo-gateway", "pt", yes=True, now=NOW, host=self.host)
        with self.assertRaisesRegex(ValueError, "There is no gateway repair to undo"):   # said once
            _gateway_repair(self.home, "undo-gateway", "en", yes=True, now=NOW, host=self.host)

    def test_the_start_is_checked_again_after_the_owner_answers(self):
        self.check()
        before = self.unit_text()

        def meanwhile(*args, **kwargs):   # while the owner reads the question, a gateway comes up
            self.gateway_state("degraded", start=own_start())

        from hermes_atlas_evolution import cli

        with patch.object(cli, "_confirm", side_effect=meanwhile):
            with self.assertRaisesRegex(ValueError, "a gateway of this Hermes runs now"):
                _gateway_repair(self.home, "fix-gateway", "en", yes=False, now=NOW, host=self.host)
        self.assertEqual(self.unit_text(), before)
        self.assertEqual(self.systemd.mutating(), [])

    def test_a_lock_on_a_device_proc_locks_names_otherwise_is_unknown(self):
        lock = self.home / "gateway.lock"
        lock.write_text("")
        info = os.stat(lock)
        line = "1: FLOCK  ADVISORY  WRITE 99 {:02x}:{:02x}:{} 0 EOF\n"
        exact = line.format(os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
        other = line.format(0, 77, info.st_ino)   # btrfs subvolume, overlayfs: another device for the same file
        self.assertIs(hermes_care._lock_held(lock, table=exact), True)
        self.assertIsNone(hermes_care._lock_held(lock, table=other))
        self.assertIs(hermes_care._lock_held(lock, table=""), False)
        # a lock that reads free never disproves a host record whose process runs with its start time
        locks = self.base / "locks"
        locks.mkdir()
        (locks / "host-gateway.lock").write_text("")
        (locks / "host-gateway.json").write_text(json.dumps({"role": "gateway", "pid": os.getpid(),
                                                             "startTime": own_start(), "home": str(self.home)}))
        self.assertEqual([item["evidence"][0] for item in hermes_care.live_gateways(self.home, now=NOW)],
                         ["host_record"])



@POSIX
class CustomBuild(unittest.TestCase):
    """The gateway's service runs, on purpose, a Python build Hermes does not manage (production's
    `python-3.14.10+atlas.…`) while facts.json names Hermes' own `python-3.14.9+20261021`: gc stays off, and the
    advice never presents `care fix-gateway` (which would drop that build) as the fix."""

    CUSTOM = "python-3.14.10+atlas.20261101.sqlite3541-linux-x64"

    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / "hermes"
        (self.home / "state").mkdir(parents=True)
        (self.home / "config.yaml").write_text("{}\n")
        tools = self.home / "tools"
        for name in (self.CUSTOM, "python-3.14.9+20261021-linux-x64", "python-3.14.8+20261001-linux-x64"):
            program = tools / name / "bin" / "python3"
            program.parent.mkdir(parents=True)
            program.write_text("#!/bin/sh\n")
            program.chmod(0o755)
        (tools / "facts.json").write_text(json.dumps({"schema": 1, "packages": {
            "python": {"entry": "python-3.14.9+20261021-linux-x64"}}}))
        self.name = hermes_care.gateway_unit_name(self.home)
        self.systemd = Systemd()
        self.systemd.add("user", self.name, ActiveState="active", SubState="running", MainPID=str(os.getpid()),
                         ExecStart=exec_value(str(tools / self.CUSTOM / "bin" / "python3"), "-B -I native_gateway.py"),
                         Environment=f"HERMES_HOME={self.home}")
        self.host = system_update.Host(runner=self.systemd)   # this machine's files (the temporary Hermes)
        (self.home / "gateway_state.json").write_text(json.dumps({"gateway_state": "running", "pid": os.getpid(),
                                                                  **({"start_time": own_start()} if own_start() else {})}))

    def care(self):
        with experience(self.home) as conn:
            return hermes_care.cached(conn, self.home, now=NOW, force=True, host=self.host, fresh=True)

    def test_gc_stays_off_and_the_advice_is_honest(self):
        care = self.care()
        self.assertEqual(care["in_use_unreferenced"], [self.CUSTOM])
        self.assertEqual(care["in_use_kind"], {self.CUSTOM: "custom"})
        self.assertTrue(hermes_care.gc_blocked(care))
        en, pt = (" ".join(hermes_care.gc_block_advice(care, lang)) for lang in ("en", "pt"))
        self.assertIn(f"which the service `{self.name}` runs on purpose (a build Hermes does not manage)", en)
        self.assertIn("would drop that build — only do it if you want that", en)
        self.assertNotIn("Point the service at the current Python", en)
        self.assertIn("roda de propósito (uma versão que o Hermes não gerencia)", pt)
        self.assertIn("descartaria essa versão", pt)
        from hermes_atlas_evolution import status

        values = status._in_use_values(care)
        self.assertIn("runs on purpose", values["in_use_advice_en"])
        self.assertNotIn("First point the service", values["in_use_advice_en"])
        self.assertIn("roda de propósito", values["in_use_advice_pt"])
        # the command line says the same, from a check taken now
        import contextlib
        import io
        from types import SimpleNamespace

        from hermes_atlas_evolution.cli import execute

        cached, said = hermes_care.cached, io.StringIO()
        from hermes_atlas_evolution import cli

        with patch.object(hermes_care, "cached", lambda conn, home, **kw: cached(conn, home, **{**kw, "host": self.host})), \
                patch.object(hermes_care, "run_action", side_effect=AssertionError("gc ran")), \
                patch.object(cli, "_agent_shell", return_value=False), \
                self.assertRaises((ValueError, SystemExit)) as stop, contextlib.redirect_stderr(said):
            execute(SimpleNamespace(lang="en", evolution_action="care", care_action="gc", yes=True), self.home,
                    runtime_config={})
        told = str(stop.exception) + said.getvalue()
        self.assertIn("runs on purpose (a build Hermes does not manage)", told)
        self.assertNotIn("Point the service at the current Python", told)

    def test_an_older_python_of_hermes_is_still_pointed_at_the_current_one(self):
        unit = self.systemd.units[("user", self.name)]
        unit["ExecStart"] = exec_value(str(self.home / "tools" / "python-3.14.8+20261001-linux-x64" / "bin" / "python3"))
        care = self.care()
        self.assertEqual(care["in_use_kind"], {"python-3.14.8+20261001-linux-x64": "older"})
        self.assertIn("Point the service at the current Python first", " ".join(hermes_care.gc_block_advice(care)))

    def test_the_mode_auto_never_touches_it_and_the_owner_is_told_what_fix_gateway_drops(self):
        care = self.care()
        self.assertNotIn("gateway_exec_missing", care["findings"])
        with experience(self.home) as conn:   # even a check that says the gateway cannot start: its program exists
            conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('care_check', ?)",
                         (json.dumps({**care, "findings": ["gateway_exec_missing"]}),))
        (self.home / "gateway_state.json").unlink()
        self.assertIsNone(actions.repair_gateway(self.home, now=NOW, host=self.host))
        self.assertEqual(self.systemd.mutating(), [])
        asked = []
        from hermes_atlas_evolution import cli

        def answer_no(text, *args, **kwargs):
            asked.append(text)
            raise SystemExit(1)

        with patch.object(cli, "_confirm", side_effect=answer_no), self.assertRaises(SystemExit):
            _gateway_repair(self.home, "fix-gateway", "en", yes=False, now=NOW, host=self.host)
        self.assertIn(f"This drops `{self.CUSTOM}`, the build the service runs now, which Hermes does not manage",
                      asked[0])
        self.assertEqual(self.systemd.mutating(), [])



class LabFindings(unittest.TestCase):
    """A real Hermes 0.23.2: the launcher's Python in a same-named tools folder, the doctor's auxiliary-client
    warnings in the profile's errors.log, `pm doctor` failures lost from the kept lines."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)

    def test_this_process_python_counts_only_where_it_really_is(self):
        home = self.base / "hermes"
        for name in ("python-3.14.10", "python-3.14.9+20261021-linux-x64"):
            (home / "tools" / name / "bin").mkdir(parents=True)
        (home / "tools" / "facts.json").write_text(json.dumps({"schema": 1, "packages": {
            "python": {"entry": "python-3.14.10"}}}))
        elsewhere = self.base / "launcher" / "tools" / "python-3.14.9+20261021-linux-x64" / "bin" / "python3"
        with patch.object(sys, "executable", str(elsewhere)):
            found = hermes_care.tool_store(home, processes=())
        self.assertEqual(found["in_use_unreferenced"], [])   # another Hermes' launcher Python, same folder name
        inside = home / "tools" / "python-3.14.9+20261021-linux-x64" / "bin" / "python3"
        with patch.object(sys, "executable", str(inside)):
            found = hermes_care.tool_store(home, processes=())
        self.assertEqual(found["in_use_unreferenced"], ["python-3.14.9+20261021-linux-x64"])

    @POSIX
    def test_the_doctor_run_keeps_the_auxiliary_clients_warnings_out_of_errors_log(self):
        """Hermes' Tool Availability check resolves each tool's auxiliary model, and the auxiliary client warns for
        every provider it cannot use; a fake Hermes here sets up its errors.log as Hermes' CLI config load does."""
        from hermes_atlas_evolution import doctor

        fake = self.base / "fake"
        (fake / "hermes_cli").mkdir(parents=True)
        (fake / "agent").mkdir()
        (fake / "agent" / "__init__.py").write_text("")
        (fake / "agent" / "auxiliary_client.py").write_text(
            "import logging\nlogger = logging.getLogger(__name__)\n"
            "def resolve():\n    logger.warning('Auxiliary Nous client unavailable: no Nous authentication found')\n"
            "    logger.error('auxiliary: a real failure')\n")
        (fake / "hermes_cli" / "__init__.py").write_text("")
        (fake / "hermes_cli" / "left_core_migration.py").write_text(
            "import contextlib\n@contextlib.contextmanager\ndef installs_held():\n    yield\n")
        (fake / "hermes_cli" / "doctor.py").write_text(
            "import logging, os, pathlib\n"
            "def _check_tool_availability(fix):\n"
            "    logs = pathlib.Path(os.environ['HERMES_HOME']) / 'logs'\n"
            "    logs.mkdir(exist_ok=True)\n"
            "    handler = logging.FileHandler(logs / 'errors.log')   # Hermes sets its log files up lazily\n"
            "    handler.setLevel(logging.WARNING)\n"
            "    logging.getLogger().addHandler(handler)\n"
            "    from agent import auxiliary_client\n"
            "    auxiliary_client.resolve()\n"
            "    logging.getLogger('tools.web').warning('web backend not configured')\n"
            "    print('  ⚠ vision (missing OPENROUTER_API_KEY)')\n"
            "    class F: issues = ['Run hermes setup to configure missing API keys for full tool access']; manual = []\n"
            "    return F()\n"
            "DOCTOR_CHECKS = [('Tool Availability', _check_tool_availability)]\n")
        home = self.base / "profile"
        home.mkdir()
        (home / "config.yaml").write_text("{}\n")
        with patch.object(sys, "path", [str(fake), *sys.path]):
            result = doctor.run(home)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["checks"][0]["issues"], ["Run hermes setup to configure missing API keys for full tool access"])
        self.assertEqual(result["checks"][0]["rows"][0]["mark"], "warn")   # what the check found is all there
        log = (home / "logs" / "errors.log").read_text()
        self.assertNotIn("Nous authentication", log)
        self.assertIn("auxiliary: a real failure", log)   # an error of the auxiliary client is still logged
        self.assertIn("web backend not configured", log)   # and every other logger as Hermes set it

    def test_a_failing_step_keeps_the_lines_that_say_why(self):
        output = ("◆ Package manager\n" + "".join(f"  ✓ package {n} ok\n" for n in range(30)) +
                  "  ✗ \x1b[31mpython-3.14.9 digest mismatch\x1b[0m\n  ⚠ uv cache is large\n"
                  "  ✗ token ghp_" "ABCDEFGHIJKLMNOPQRSTUV in facts.json\n  ✗ " + "x" * 900 + "\n" +
                  "".join(f"  ✓ entry {n} ok\n" for n in range(10)))
        from types import SimpleNamespace

        runner = lambda command, **kwargs: SimpleNamespace(returncode=1, stdout=output, stderr="")  # noqa: E731
        with patch.object(admin, "hermes_executable", return_value="/usr/local/bin/hermes"):
            result = hermes_care.run_action(self.base, "pm_doctor", runner=runner)
        step = result["steps"][-1]
        self.assertEqual(len(step["tail"]), 6)
        self.assertTrue(all(line.lstrip().startswith("✓") for line in step["tail"]))   # the tail alone says nothing
        self.assertEqual(step["problems"][0], "  ✗ python-3.14.9 digest mismatch")
        self.assertEqual(step["problems"][1], "  ✗ token <secret> in facts.json")
        self.assertEqual(len(step["problems"][2]), hermes_care.STEP_LINE_CHARS)
        self.assertEqual(step["problems"][3:], ["  ⚠ uv cache is large", "◆ Package manager"])
        self.assertNotIn("ghp_", json.dumps(result))
        from hermes_atlas_evolution.cli import step_lines

        self.assertEqual(step_lines(step)[0], "  ✗ python-3.14.9 digest mismatch")


if __name__ == "__main__":
    unittest.main()


class DoctorRelaunchTest(unittest.TestCase):
    """Hermes 0.23.2 can relaunch the doctor's process with the same argv and environment: the boot script must read
    its settings without removing them, or the second run fails (KeyError) and the doctor never answers."""

    def test_the_boot_script_survives_running_twice_in_one_environment(self):
        import subprocess
        import sys as _sys

        from hermes_atlas_evolution import doctor

        # The first run imports a fake hermes_cli that relaunches the process once, exactly as Hermes may.
        with tempfile.TemporaryDirectory() as folder:
            fake = Path(folder) / "hermes_cli"
            fake.mkdir()
            (fake / "__init__.py").write_text(
                "import os, subprocess, sys\n"
                "if not os.environ.get('FAKE_RELAUNCHED'):\n"
                "    os.environ['FAKE_RELAUNCHED'] = '1'\n"
                "    argv = [sys.executable, *sys.orig_argv[1:]]\n"
                "    if os.name == 'nt':   # os.execv does not quote its arguments there\n"
                "        os._exit(subprocess.call(argv))\n"
                "    os.execv(sys.executable, argv)\n")
            (fake / "doctor.py").write_text("DOCTOR_CHECKS = []\n")
            (fake / "left_core_migration.py").write_text(
                "import contextlib\n@contextlib.contextmanager\ndef installs_held():\n    yield\n")
            env = {key: value for key, value in os.environ.items() if key in ("PATH", "SYSTEMROOT", "HOME")}
            env.update({"ATLAS_EVOLUTION_PATH": json.dumps([folder]), "ATLAS_EVOLUTION_SKIP": "[]",
                        "ATLAS_EVOLUTION_MARKER": doctor.MARKER})
            out = subprocess.run([_sys.executable, "-I", "-c", doctor.BOOT], env=env, capture_output=True, text=True,
                                 timeout=60)
        self.assertIn(doctor.MARKER + '{"checks": []}', out.stdout, out.stderr[-500:])
