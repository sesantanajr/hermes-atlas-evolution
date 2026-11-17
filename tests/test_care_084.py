"""0.8.4: the operating system's update (system_update), Hermes' own health (hermes_care), the weekly digest
scheduled with Hermes' cron, and their places in status, the menu and the command line."""

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_atlas_evolution import admin, decisions, hermes_care, system_update
from hermes_atlas_evolution.cli import execute
from hermes_atlas_evolution.experience import experience
from hermes_atlas_evolution.menu import Console, Menu

from test_menu_082 import Script
from test_panel_decisions import Base

os.environ.setdefault("ATLAS_EVOLUTION_NO_HERMES", "1")   # never the real Hermes of this machine

NOW = time.time()
UPGRADABLE = """Listing...
libc6/resolute-updates 2.42-0ubuntu3.1 amd64 [upgradable from: 2.42-0ubuntu3]
openssl/resolute-security,resolute-updates 3.5.1-1ubuntu2 amd64 [upgradable from: 3.5.1-1ubuntu1]
docker.io/resolute-updates 28.2-0ubuntu1 amd64 [upgradable from: 28.1-0ubuntu1]
"""
SIMULATION = """Reading package lists...
The following packages have been kept back:
  linux-generic linux-image-generic
The following packages will be upgraded:
Inst libc6 [2.42-0ubuntu3] (2.42-0ubuntu3.1 Ubuntu:26.04/resolute-updates [amd64])
Inst docker.io [28.1-0ubuntu1] (28.2-0ubuntu1 Ubuntu:26.04/resolute-updates [amd64])
Inst libfoo2 (1.0-1 Ubuntu:26.04/resolute [amd64])
Remv oldthing [0.9-1]
Conf libc6 (2.42-0ubuntu3.1 Ubuntu:26.04/resolute-updates [amd64])
"""


class FakeMachine:
    """A machine for system_update.Host: files under a temporary root and canned command answers."""

    def __init__(self, root: Path, **answers):
        self.root, self.calls = root, []
        self.answers = {"apt list": (0, UPGRADABLE, ""), "dpkg --audit": (0, "", ""), "apt-get -o": (0, "", ""),
                        "apt-mark showhold": (0, "", ""), "uname -r": (0, "7.0.0-43-generic\n", ""),
                        "id -u": (0, "0\n", ""), "which": (0, "/usr/bin/x\n", ""), "loginctl": (0, "Linger=yes\n", ""),
                        "apt-get -s": (0, SIMULATION, ""), "apt-get -o Dpkg::Lock::Timeout=120 update": (0, "", ""),
                        "dpkg-query": (0, "libc6\t2.42-0ubuntu3\tii \n", ""), "hermes --version": (0, "Hermes 0.23.1\n", ""),
                        "systemctl list-unit-files": (0, "hermes-dashboard.service enabled enabled\n", ""),
                        "systemctl --user list-unit-files": (0, "hermes-gateway.service enabled enabled\n", ""),
                        "systemctl is-active": (0, "active\n", ""), "systemctl --user is-active": (0, "active\n", ""),
                        "systemctl list-units": (0, "hermes-dashboard.service loaded active running x\nnginx.service loaded active running x\n", ""),
                        "systemctl --user list-units": (0, "hermes-gateway.service loaded active running x\n", ""),
                        "systemctl list-units --type=service --state=failed": (0, "", ""),
                        "systemctl show nginx.service": (0, "ActiveState=failed\nResult=exit-code\n", ""),
                        "root-owned": (0, "", ""),
                        **answers}
        (root / "etc").mkdir(parents=True, exist_ok=True)
        (root / "etc" / "os-release").write_text('PRETTY_NAME="Ubuntu 26.04.1 LTS"\nID=ubuntu\nID_LIKE=debian\nVERSION_ID="26.04"\n')
        (root / "etc" / "apt" / "apt.conf.d").mkdir(parents=True, exist_ok=True)
        (root / "etc" / "apt" / "apt.conf.d" / "20auto-upgrades").write_text('APT::Periodic::Unattended-Upgrade "1";\n')
        (root / "usr" / "bin").mkdir(parents=True, exist_ok=True)
        (root / "usr" / "bin" / "unattended-upgrade").write_text("")
        (root / "proc").mkdir(exist_ok=True)

    def process(self, pid: int, name: str, state: str = "S", cmdline: str = ""):
        folder = self.root / "proc" / str(pid)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "comm").write_text(name + "\n")
        (folder / "stat").write_text(f"{pid} ({name}) {state} 1 1\n")
        (folder / "cmdline").write_bytes(cmdline.replace(" ", "\0").encode())

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        line = " ".join(argv)
        if argv[:1] == ["/bin/sh"]:   # the upgrade script: what it would leave behind
            folder = Path(argv[1]).parent
            (folder / "run.log").write_text("Unpacking libc6\nSetting up libc6\n")
            (folder / "status").write_text("0\n")
            (folder / "packages-after.tsv").write_text("libc6\t2.42-0ubuntu3.1\tii \n")
            (folder / "packages-after.tsv").chmod(0o600)   # the script runs with umask 077
            self.ran = (folder / "run.sh").read_text()
            self.answers["dpkg-query"] = (0, "libc6\t2.42-0ubuntu3.1\tii \n", "")
            return 0, "", ""
        for prefix in sorted(self.answers, key=len, reverse=True):
            if line.startswith(prefix):
                return self.answers[prefix]
        return 0, "", ""


POSIX_ONLY = unittest.skipUnless(os.name == "posix", "Linux and POSIX only: apt, /proc, statvfs, file modes")


@POSIX_ONLY
class SystemUpdateTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / "root"
        self.home = Path(folder.name) / "home"
        self.home.mkdir()
        self.machine = FakeMachine(self.root)
        self.host = system_update.Host(root=self.root, runner=self.machine)

    def test_a_ready_machine_with_security_updates_and_a_reboot_waiting(self):
        (self.root / "var" / "run").mkdir(parents=True)
        (self.root / "var" / "run" / "reboot-required").write_text("*** System restart required ***\n")
        (self.root / "var" / "run" / "reboot-required.pkgs").write_text("libc6\n")
        self.machine.process(900, "unattended-upgr", cmdline="/usr/bin/python3 /usr/share/unattended-upgrades/unattended-upgrade-shutdown --wait-for-signal")
        self.machine.process(901, "apt-get", state="Z")   # a dead process nobody reaped
        self.machine.process(902, "packagekitd")          # idles for minutes after an apt-get update, holds nothing
        with patch.object(system_update.Host, "free", return_value=50 * 2**30):
            report = system_update.check(self.host, now=NOW)
        self.assertTrue(report["supported"])
        self.assertEqual(report["security"], 1)
        self.assertEqual(report["reboot_packages"], ["libc6"])
        self.assertEqual(report["busy"], [])
        self.assertTrue(report["ready"])
        self.assertIn("reboot_required", report["findings"])
        self.assertEqual([unit["scope"] for unit in report["units"]], ["system", "user"])
        self.assertIn("user:hermes-gateway.service", report["running"])
        lock_free = [call for call in self.machine.calls if call[:2] == ["apt-get", "-o"]]
        self.assertEqual(lock_free[0][:3], ["apt-get", "-o", "Debug::NoLocking=1"])   # never makes apt wait

    def test_what_stands_in_the_way(self):
        machine = FakeMachine(self.root, **{"dpkg --audit": (0, "The following packages are only half configured\n", ""),
                                            "loginctl": (0, "Linger=no\n", "")})
        machine.process(950, "dpkg")
        host = system_update.Host(root=self.root, runner=machine)
        with patch.object(system_update.Host, "free", side_effect=lambda mount: 10 * 2**20 if mount == "/boot" else 50 * 2**30):
            busy = system_update.check(host, now=NOW)
            # while a package manager works, what it leaves half-way for a moment is not reported
            self.assertIn("package_manager_busy", busy["findings"])
            self.assertNotIn("dpkg_interrupted", busy["findings"])
            (self.root / "proc" / "950" / "stat").write_text("950 (dpkg) Z 1 1\n")
            report = system_update.check(host, now=NOW)
        for code in ("dpkg_interrupted", "disk_low", "user_units_without_linger"):
            self.assertIn(code, report["findings"])
        self.assertFalse(report["ready"])
        with patch.object(system_update.Host, "free", return_value=50 * 2**30), self.assertRaises(ValueError) as stop:
            system_update.prepare(self.home, host, now=NOW)
        self.assertIn("dpkg_interrupted", str(stop.exception))

    def test_an_unsupported_system_is_left_alone(self):
        (self.root / "etc" / "os-release").write_text('ID=fedora\nPRETTY_NAME="Fedora"\n')
        report = system_update.check(self.host, now=NOW)
        self.assertEqual((report["supported"], report["findings"], report["ready"]), (False, ["unsupported"], False))

    def test_the_plan_classifies_what_needs_attention(self):
        planned = system_update.plan(self.host, refresh=True)
        self.assertTrue(planned["refreshed"])
        self.assertEqual([item["name"] for item in planned["upgrade"]], ["libc6", "docker.io"])
        self.assertEqual([item["name"] for item in planned["install"]], ["libfoo2"])
        self.assertEqual([item["name"] for item in planned["remove"]], ["oldthing"])
        self.assertEqual(planned["kept_back"], ["linux-generic", "linux-image-generic"])
        self.assertEqual(planned["attention"], ["needs_reboot", "restarts_containers"])
        simulated = [call for call in self.machine.calls if "-s" in call][0]
        self.assertIn("--with-new-pkgs", simulated)
        system_update.plan(self.host, refresh=False, full=True)
        self.assertIn("full-upgrade", [call for call in self.machine.calls if "-s" in call][-1])

    def test_the_script_keeps_configuration_and_names_only_packages(self):
        with self.assertRaises(ValueError):   # refused, never dropped in silence
            system_update.script(self.home, full=False, skip=["docker.io", "a; rm -rf /"])
        body = system_update.script(Path("/srv/hermes$(touch /tmp/x)/run"), skip=["docker.io", "libc6:amd64", "nginx"])
        self.assertIn("SKIP='docker.io libc6:amd64 nginx'", body)
        self.assertIn("exec >>'/srv/hermes$(touch /tmp/x)/run/run.log'", body)   # quoted: never run
        self.assertIn("trap 'release; exit 130' INT TERM HUP", body)
        self.assertIn("--force-confold", body)
        self.assertIn("NEEDRESTART_MODE=l", body)
        self.assertIn("--with-new-pkgs upgrade", body)
        self.assertIn("umask 077", body)
        self.assertIn("trap release EXIT", body)

    def test_the_whole_upgrade_with_its_report(self):
        with patch.object(system_update.Host, "free", return_value=50 * 2**30):
            folder, before, snapshot = system_update.prepare(self.home, self.host, now=NOW)
            self.assertEqual(snapshot["count"], 1)
            self.assertEqual(oct(folder.stat().st_mode & 0o777), "0o700")
            self.assertEqual(folder.parent, self.root / "var/lib/hermes-atlas-evolution/system")   # outside the profile
            self.assertFalse((self.home / "evolution" / "system").exists())
            started = system_update.start(self.host, folder, skip=["docker.io"])
            self.assertEqual(started["mode"], "direct")
            self.machine.answers["systemctl list-units"] = (0, "hermes-dashboard.service loaded active running x\n", "")
            report = system_update.after(self.host, folder, before, now=NOW)
        self.assertEqual(report["exit"], 0)
        self.assertEqual(report["changed"], [{"name": "libc6", "from": "2.42-0ubuntu3", "to": "2.42-0ubuntu3.1"}])
        self.assertEqual(report["go_back"], ["apt-get install libc6=2.42-0ubuntu3"])
        self.assertEqual(report["stopped_units"], ["nginx.service"])   # it ran before and not after
        self.assertFalse(report["ok"])
        self.assertEqual(system_update.last_report(self.host)[1]["exit"], 0)
        for name in ("packages-before.tsv", "packages-after.tsv", "etc-before.tar.gz", "report.json", "check-before.json"):
            self.assertEqual(oct((folder / name).stat().st_mode & 0o777), "0o600", name)

    def test_upgrading_and_repairing_need_root(self):
        machine = FakeMachine(self.root, **{"id -u": (0, "1000\n", "")})
        host = system_update.Host(root=self.root, runner=machine)
        with patch.object(system_update.Host, "free", return_value=50 * 2**30):
            with self.assertRaises(ValueError):
                system_update.prepare(self.home, host, now=NOW)
            with self.assertRaises(ValueError):
                system_update.repair(self.home, host)

    def test_repair_finishes_what_was_left_and_refuses_while_apt_runs(self):
        self.machine.process(960, "apt-get")
        with self.assertRaises(ValueError):
            system_update.repair(self.home, self.host)
        (self.root / "proc" / "960" / "stat").write_text("960 (apt-get) Z 1 1\n")
        result = system_update.repair(self.home, self.host)
        self.assertTrue(result["ok"])
        self.assertIn("dpkg --force-confdef --force-confold --configure -a", self.machine.ran)
        self.assertIn("NEEDRESTART_MODE=l", self.machine.ran)

    def test_a_hermes_another_user_can_write_is_never_run_as_root(self):
        machine = FakeMachine(self.root, **{"root-owned": (1, "", "")})
        host = system_update.Host(root=self.root, runner=machine)
        with patch.object(system_update.Host, "free", return_value=50 * 2**30):
            for call in (lambda: system_update.prepare(self.home, host, now=NOW), lambda: system_update.repair(self.home, host)):
                with self.assertRaises(ValueError) as stop:
                    call()
                self.assertIn("not root's", str(stop.exception))
        self.assertFalse((self.root / "var/lib/hermes-atlas-evolution").exists())

    def test_a_held_lock_is_busy_and_a_killed_runs_holds_are_found_and_released(self):
        lock = self.root / "var/lib/dpkg/lock-frontend"
        lock.parent.mkdir(parents=True)
        lock.write_text("")
        info = os.stat(lock)
        (self.root / "proc" / "locks").write_text(
            f"1: POSIX  ADVISORY  WRITE 4242 {os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino} 0 EOF\n")
        with patch.object(system_update.Host, "free", return_value=50 * 2**30):
            report = system_update.check(self.host, now=NOW)
        self.assertEqual(report["busy"], ["lock:/var/lib/dpkg/lock-frontend"])
        self.assertFalse(report["ready"])
        (self.root / "proc" / "locks").write_text("")
        run = self.root / "var/lib/hermes-atlas-evolution/system/20261116T000000Z-dead"
        run.mkdir(parents=True)
        (run / "holds").write_text("zlib1g\n")
        machine = FakeMachine(self.root, **{"apt-mark showhold": (0, "zlib1g\n", "")})
        host = system_update.Host(root=self.root, runner=machine)
        with patch.object(system_update.Host, "free", return_value=50 * 2**30):
            report = system_update.check(host, now=NOW)
            self.assertIn("held_by_plugin", report["findings"])
            result = system_update.repair(self.home, host)
        self.assertEqual(result["released"], ["zlib1g"])
        self.assertIn(["apt-mark", "unhold", "zlib1g"], machine.calls)
        self.assertFalse((run / "holds").exists())

    def test_a_hold_stays_listed_until_apt_mark_really_released_it(self):
        """The real script under /bin/sh with a fake apt toolbox: a failing unhold keeps the list, and a name with
        the machine's own architecture is compared as apt-mark shows it (the owner's own hold is left alone)."""
        bin_dir = self.root / "fakebin"
        bin_dir.mkdir(parents=True)
        log = self.root / "calls.log"
        tools = {"dpkg-query": "exit 0", "dpkg": 'echo amd64', "apt-get": "exit 0", "sleep": "exit 0",
                 "apt-mark": f'echo "apt-mark $*" >> {log}\ncase "$*" in *showhold*) cat {self.root}/showhold ;; '
                             f'*unhold*) exit $(cat {self.root}/unhold_code) ;; esac\nexit 0'}
        for name, body in tools.items():
            (bin_dir / name).write_text("#!/bin/sh\n" + body + "\n")
            (bin_dir / name).chmod(0o755)
        for owner_hold, unhold_code, holds_left in (("zlib1g\n", 0, False), ("", 100, True), ("", 0, False)):
            folder = self.root / f"run-{unhold_code}-{len(owner_hold)}"
            folder.mkdir()
            (self.root / "showhold").write_text(owner_hold)
            (self.root / "unhold_code").write_text(str(unhold_code))
            log.write_text("")
            (folder / "run.sh").write_text(system_update.script(folder, skip=["zlib1g:amd64"]))
            env = {**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin"}
            import subprocess
            subprocess.run(["/bin/sh", str(folder / "run.sh")], env=env, check=False, timeout=60)
            calls = log.read_text()
            if owner_hold:   # the owner held zlib1g already: neither held nor released by the run
                self.assertNotIn("apt-mark hold zlib1g", calls)
                self.assertNotIn("unhold", calls)
            else:
                self.assertIn("apt-mark hold zlib1g", calls)
                self.assertIn("unhold zlib1g", calls)
            self.assertEqual((folder / "holds").exists(), holds_left, calls)
            self.assertEqual((folder / "status").read_text().strip(), "0")
            self.assertTrue((folder / "packages-after.tsv").exists())

    def test_a_hold_listed_with_its_architecture_is_found(self):
        run = self.root / "var/lib/hermes-atlas-evolution/system/20261116T000000Z-arch"
        run.mkdir(parents=True)
        (run / "holds").write_text("zlib1g:amd64\n")
        machine = FakeMachine(self.root, **{"apt-mark showhold": (0, "zlib1g\n", ""),
                                            "dpkg --print-architecture": (0, "amd64\n", "")})
        with patch.object(system_update.Host, "free", return_value=50 * 2**30):
            report = system_update.check(system_update.Host(root=self.root, runner=machine), now=NOW)
        self.assertEqual(report["plugin_holds"], ["zlib1g:amd64"])
        self.assertIn("held_by_plugin", report["findings"])

    def test_repair_keeps_the_list_of_what_it_could_not_release(self):
        run = self.root / "var/lib/hermes-atlas-evolution/system/20261116T000000Z-keep"
        run.mkdir(parents=True)
        (run / "holds").write_text("zlib1g\n")
        machine = FakeMachine(self.root, **{"apt-mark showhold": (0, "zlib1g\n", ""),
                                            "apt-mark unhold": (100, "", "locked")})
        with patch.object(system_update.Host, "free", return_value=50 * 2**30):
            result = system_update.repair(self.home, system_update.Host(root=self.root, runner=machine))
        self.assertEqual(result["released"], [])
        self.assertTrue((run / "holds").exists())

    def test_a_dead_or_reused_process_is_lost_and_a_run_never_started_is_left_out(self):
        import subprocess
        base = self.root / "var/lib/hermes-atlas-evolution/system"
        real = system_update.Host()
        for name, pid in (("zombie", None), ("reused", os.getpid())):
            folder = base / f"20261116T00000{len(name)}Z-{name}"
            folder.mkdir(parents=True)
            (folder / "run.sh").write_text("#")
            if pid is None:
                child = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
                deadline = time.time() + 10
                while time.time() < deadline and Path(f"/proc/{child.pid}/stat").exists() \
                        and Path(f"/proc/{child.pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z":
                    time.sleep(0.05)
                pid = child.pid   # a zombie: kill(pid, 0) still succeeds on it
            (folder / "pid").write_text(str(pid))
            self.assertEqual(system_update.progress(real, folder), "lost", name)
        never = base / "20261117T000000Z-never"
        never.mkdir()
        self.assertEqual(system_update.progress(self.host, never), "preparing")
        self.assertEqual(system_update.progress(self.host, never, now=time.time() + 3600), "abandoned")
        folder, _report = system_update.last_report(self.host)
        self.assertNotEqual(folder, never)

    def test_services_that_ended_cleanly_are_listed_apart(self):
        machine = FakeMachine(self.root, **{"systemctl show nginx.service": (0, "ActiveState=inactive\nResult=success\n", "")})
        host = system_update.Host(root=self.root, runner=machine)
        folder = self.root / "run"
        folder.mkdir()
        (folder / "status").write_text("0\n")
        (folder / "packages-before.tsv").write_text("libc6\t1\tii \n")
        (folder / "packages-after.tsv").write_text("libc6\t2\tii \n")
        machine.answers["systemctl list-units"] = (0, "hermes-dashboard.service loaded active running x\n", "")
        with patch.object(system_update.Host, "free", return_value=50 * 2**30):
            result = system_update.after(host, folder, {"running": ["nginx.service", "hermes-dashboard.service"]})
        self.assertEqual(result["stopped_units"], [])
        self.assertEqual(result["stopped_clean"], ["nginx.service"])
        self.assertEqual(result["after_from"], "run")
        self.assertTrue(result["ok"])

    def test_a_package_manager_busy_for_hours_is_stuck(self):
        (self.root / "proc" / "uptime").write_text("100000.00 1.00\n")
        folder = self.root / "proc" / "777"
        folder.mkdir(parents=True)
        (folder / "comm").write_text("apt\n")
        ticks = os.sysconf("SC_CLK_TCK")
        fields = ["S"] + ["0"] * 18 + [str(int(1000 * ticks))]   # started 1000 s after boot: 27 h ago
        (folder / "stat").write_text("777 (apt) " + " ".join(fields) + "\n")
        (folder / "cmdline").write_bytes(b"apt\0upgrade")
        with patch.object(system_update.Host, "free", return_value=50 * 2**30):
            report = system_update.check(self.host, now=NOW)
        self.assertEqual(report["busy"], ["apt"])
        self.assertIn("package_manager_stuck", report["findings"])
        self.assertGreater(report["busy_seconds"], 6 * 3600)

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "needs root to own the files")
    def test_a_root_owned_link_to_a_root_owned_hermes_is_root_owned(self):
        target = self.root / "real-home"
        target.mkdir(parents=True, mode=0o700)
        link = self.root / "home-link"
        link.symlink_to(target)
        os.chmod(self.root, 0o755)
        os.chmod(self.root.parent, 0o700)
        self.assertTrue(system_update.Host().root_owned(link))

    def test_a_run_whose_unit_is_gone_without_an_exit_is_lost(self):
        folder = self.root / "var/lib/hermes-atlas-evolution/system/20261116T000000Z-abcd"
        folder.mkdir(parents=True)
        (folder / "run.sh").write_text("#")
        (folder / "unit").write_text("hermes-evolution-os-upgrade-x")
        (folder / "check-before.json").write_text(json.dumps({"running": []}))
        machine = FakeMachine(self.root, **{"systemctl is-active hermes-evolution-os-upgrade-x": (3, "inactive\n", "")})
        host = system_update.Host(root=self.root, runner=machine)
        self.assertEqual(system_update.progress(host, folder), "lost")
        found, report = system_update.last_report(host)
        self.assertEqual((found, report["state"], report["ok"]), (folder, "lost", False))

    def test_the_daily_check_is_kept_a_day(self):
        with experience(self.home) as conn, patch.object(system_update.Host, "free", return_value=50 * 2**30):
            first = system_update.cached(conn, self.home, now=NOW, host=self.host)
            calls = len(self.machine.calls)
            again = system_update.cached(conn, self.home, now=NOW + 3600, host=self.host)
            self.assertEqual(len(self.machine.calls), calls)
            self.assertEqual(first["at"], again["at"])
            self.assertEqual(again["upgradable"], 3)
            system_update.cached(conn, self.home, now=NOW + 90_000, host=self.host)
            self.assertGreater(len(self.machine.calls), calls)


def gateway_files(home: Path, *, beat_age: float = 5, rss_kib: int = 600_000):
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "gateway_state.json").write_text(json.dumps({"gateway_state": "running", "code_version": "0.23.1",
                                                         "active_agents": 1}))
    beat = home / "state" / "gateway.heartbeat"
    beat.write_text(json.dumps({"pid": 1, "mem": {"rss_kib": rss_kib}}))
    os.utime(beat, (NOW - beat_age, NOW - beat_age))


class HermesCareTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / "home"
        self.root = Path(folder.name) / "root"
        (self.root / "proc").mkdir(parents=True)
        (self.root / "proc" / "meminfo").write_text("MemTotal: 12000000 kB\nMemAvailable: 6000000 kB\n"
                                                     "SwapTotal: 12000000 kB\nSwapFree: 1000000 kB\n")
        self.home.mkdir()
        tools = self.home / "tools"
        for name in ("node-26.12.1-linux-x64", "node-22.23.4", "python-3.14.9+20261021-linux-x64", "whisper.cpp",
                     "python-3.14.10+atlas-linux-x64"):
            (tools / name).mkdir(parents=True)
            (tools / name / "blob").write_bytes(b"x" * 1000)
        (tools / "facts.json").write_text(json.dumps({"schema": 1, "packages": {
            "node": {"entry": "node-26.12.1-linux-x64"}, "python": {"entry": "python-3.14.10+atlas-linux-x64"}}}))
        conn = sqlite3.connect(self.home / "state.db")
        conn.execute("PRAGMA journal_mode=wal")
        conn.execute("CREATE TABLE t(x)")
        conn.commit()
        conn.close()

    def test_gateway_database_memory_and_unused_tools(self):
        gateway_files(self.home)
        report = hermes_care.check(self.home, now=NOW, quick=True, root=self.root)
        self.assertTrue(report["gateway"]["running"])
        self.assertFalse(report["gateway"]["stale"])
        self.assertEqual(report["database"]["journal"], "wal")
        self.assertEqual(report["database"]["quick_check"], "ok")
        self.assertEqual(sorted(item["folder"] for item in report["unused_tools"]),
                         ["node-22.23.4", "python-3.14.9+20261021-linux-x64"])   # whisper.cpp is not Hermes-managed
        self.assertIn("swap_full", report["findings"])
        self.assertNotIn("memory_low", report["findings"])

    @POSIX_ONLY
    def test_a_stuck_gateway_a_growing_folder_and_a_filling_disk(self):
        gateway_files(self.home, beat_age=900, rss_kib=3_000_000)
        history = [{"at": NOW - 6 * 86_400, "sizes": {"sessions": 1 * 2**30}, "free": 20 * 2**30, "rss": 500 * 2**20}]
        with patch.object(hermes_care, "sizes", return_value={"sessions": 3 * 2**30}), \
                patch("hermes_atlas_evolution.hermes_care.os.statvfs",
                      return_value=SimpleNamespace(f_bavail=8 * 2**30 // 4096, f_frsize=4096, f_blocks=100 * 2**30 // 4096)):
            report = hermes_care.check(self.home, now=NOW, history=history, root=self.root)
        for code in ("gateway_heartbeat_stale", "gateway_memory_growth", "storage_growth", "disk_filling"):
            self.assertIn(code, report["findings"])
        self.assertEqual(report["growth"][0]["folder"], "sessions")
        self.assertEqual(report["days_to_full"], 4.0)

    @POSIX_ONLY
    def test_a_state_file_that_outlived_its_gateway_is_a_crash(self):
        gateway_files(self.home, beat_age=600)
        state = json.loads((self.home / "gateway_state.json").read_text())
        (self.home / "gateway_state.json").write_text(json.dumps({**state, "pid": 2**22 - 7}))
        report = hermes_care.check(self.home, now=NOW, root=self.root)
        self.assertFalse(report["gateway"]["running"])
        self.assertIn("gateway_crashed", report["findings"])
        if Path("/proc/self/stat").exists():   # Linux: this pid alive but started at another time is another program
            (self.home / "gateway_state.json").write_text(json.dumps({**state, "pid": os.getpid(), "start_time": 1}))
            self.assertIn("gateway_crashed", hermes_care.check(self.home, now=NOW, root=self.root)["findings"])

    @POSIX_ONLY
    def test_a_fresh_heartbeat_is_a_running_gateway_whatever_its_pid(self):
        gateway_files(self.home, beat_age=5)   # e.g. a gateway in a container on the same folder: its pid means nothing here
        state = json.loads((self.home / "gateway_state.json").read_text())
        (self.home / "gateway_state.json").write_text(json.dumps({**state, "pid": 2**22 - 7}))
        report = hermes_care.check(self.home, now=NOW, root=self.root)
        self.assertTrue(report["gateway"]["running"])
        self.assertNotIn("gateway_crashed", report["findings"])

    def test_the_check_is_kept_six_hours_with_a_daily_history(self):
        gateway_files(self.home)
        with experience(self.home) as conn:
            first = hermes_care.cached(conn, self.home, now=NOW)
            self.assertEqual(hermes_care.cached(conn, self.home, now=NOW + 3600)["at"], first["at"])
            hermes_care.cached(conn, self.home, now=NOW + 7 * 3600)
            hermes_care.cached(conn, self.home, now=NOW + 30 * 3600)
            history = json.loads(conn.execute("SELECT value FROM cursor WHERE key='care_history'").fetchone()[0])
        self.assertEqual(len(history), 2)   # about one record a day

    def test_hermes_commands_run_with_a_backup_first_and_their_summary(self):
        calls = []

        def hermes(command, **kwargs):
            calls.append(command[3:])
            if command[3:4] == ["doctor"]:
                return SimpleNamespace(returncode=1, stdout="◆ Config\n  ✓ ok\n  ✗ state.db FTS missing\n"
                                       "  ⚠ WAL large\nFound 2 issue(s) to address:\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="Snapshot saved\n", stderr="")

        with patch.object(admin, "hermes_executable", return_value="/usr/local/bin/hermes"):
            result = hermes_care.run_action(self.home, "doctor_fix", runner=hermes)
            self.assertEqual(calls, [["backup", "--quick", "--label", "atlas-evolution"], ["doctor", "--fix"]])
            self.assertEqual(result["doctor"]["problems"], ["✗ state.db FTS missing"])
            self.assertEqual(result["doctor"]["summary"], ["Found 2 issue(s) to address:"])
            failing = lambda command, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="no room")  # noqa: E731
            stopped = hermes_care.run_action(self.home, "doctor_fix", runner=failing)
            self.assertEqual((stopped["ok"], stopped["reason"], len(stopped["steps"])), (False, "backup_failed", 1))
            with self.assertRaises(ValueError):
                hermes_care.run_action(self.home, "rm", runner=hermes)


class DigestTest(Base):
    def test_the_digest_is_scheduled_once_with_no_agent_and_undone(self):
        calls = []

        def hermes(command, **kwargs):
            calls.append(command[3:])
            return SimpleNamespace(returncode=0, stdout="Created job: a1b2c3\n", stderr="")

        with patch.object(admin, "hermes_executable", return_value="/usr/local/bin/hermes"):
            result = admin.decide(self.home, {}, {"kind": "schedule_digest", "deliver": "telegram"}, now=NOW, lang="pt",
                                  runner=hermes)
            self.assertEqual((result["scheduled"], result["job"]), (True, "a1b2c3"))
            create = calls[0]
            self.assertEqual(create[:2], ["cron", "create"])
            self.assertIn("--no-agent", create)
            self.assertEqual(create[create.index("--deliver") + 1], "telegram")
            script = self.home / "scripts" / "hermes-atlas-evolution-digest.py"
            self.assertTrue(script.is_file())
            if os.name != "nt":
                self.assertEqual(oct(script.stat().st_mode & 0o777), "0o600")
            undone = admin.decide(self.home, {}, {"kind": "undo", "id": result["decision"]}, now=NOW, runner=hermes)
            self.assertTrue(undone["undone"])
            self.assertEqual(calls[-1], ["cron", "remove", "a1b2c3"])
            (self.home / "cron").mkdir()
            (self.home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [{"name": "hermes-atlas-evolution-digest",
                                                                                 "enabled": False}]}))   # paused
            self.assertEqual(admin.decide(self.home, {}, {"kind": "schedule_digest"}, now=NOW, runner=hermes)["reason"],
                             "already")
            with self.assertRaises(ValueError):
                (self.home / "cron" / "jobs.json").unlink()
                admin.decide(self.home, {}, {"kind": "schedule_digest", "deliver": "a b; rm"}, now=NOW, runner=hermes)


class CronUndoTest(Base):
    def test_a_job_removed_by_hand_is_undone_anyway(self):
        with experience(self.home) as conn:
            decision = decisions.record_cron(conn, "abc123", name="hermes-atlas-evolution-digest", schedule="0 8 * * 1",
                                             deliver="local", now=NOW)
        gone = lambda command, **kwargs: SimpleNamespace(returncode=1, stdout="Failed to remove job: Job with ID or name 'abc123' not found. Use cronjob(action='list') to inspect jobs.\n", stderr="")  # noqa: E731
        with patch.object(admin, "hermes_executable", return_value="/usr/local/bin/hermes"):
            self.assertTrue(admin.decide(self.home, {}, {"kind": "undo", "id": decision}, now=NOW, runner=gone)["undone"])

    def test_a_failure_that_is_not_the_job_gone_leaves_the_undo_open(self):
        with experience(self.home) as conn:
            decision = decisions.record_cron(conn, "abc123", name="hermes-atlas-evolution-digest", schedule="0 8 * * 1",
                                             deliver="local", now=NOW)
        profile_gone = lambda command, **kwargs: SimpleNamespace(  # noqa: E731
            returncode=1, stdout="", stderr="Error: Profile 'x' does not exist. Create it with: hermes profile create x\n")
        other_job = lambda command, **kwargs: SimpleNamespace(  # noqa: E731
            returncode=1, stdout="Failed to remove job: Job with ID or name 'zzz' not found.\n", stderr="")
        for runner in (profile_gone, other_job):
            with patch.object(admin, "hermes_executable", return_value="/usr/local/bin/hermes"), self.assertRaises(ValueError):
                admin.decide(self.home, {}, {"kind": "undo", "id": decision}, now=NOW, runner=runner)
        with patch.object(admin, "hermes_executable", return_value=None), self.assertRaises(ValueError):
            admin.decide(self.home, {}, {"kind": "undo", "id": decision}, now=NOW, runner=profile_gone)
        with experience(self.home) as conn:
            self.assertEqual([row["undone"] for row in conn.execute("SELECT undone FROM decision_log WHERE id=?", (decision,))], [None])


class StatusAndMenuTest(Base):
    def stored(self):
        care = {"at": NOW, "gateway": {"running": True, "version": "0.23.1", "rss": 640 * 2**20, "heartbeat_age": 12,
                                       "stale": False}, "database": {"present": True, "journal": "wal", "wal": 900_000},
                "free": 67 * 2**30, "total": 193 * 2**30,
                "memory": {"total": 12 * 2**30, "available": 7 * 2**30, "swap_total": 12 * 2**30, "swap_used": 11 * 2**30},
                "load": {"1m": 1.0, "15m": 1.1, "cpus": 6}, "sizes": {"tools": 2 * 2**30}, "growth": [],
                "unused_tools": [{"folder": "node-22.23.4", "package": "node", "bytes": 200 * 2**20}],
                "reclaimable": 700 * 2**20, "findings": ["swap_full", "unused_tools"]}
        system = {"at": NOW, "supported": True, "os": {"name": "Ubuntu 26.04.1 LTS"}, "kernel": "7.0.0", "security": 2,
                  "upgradable": 3, "reboot_required": True, "reboot_packages": ["libc6"], "dpkg_ok": True, "apt_ok": True,
                  "busy": [], "held": [], "unattended": {"enabled": True}, "units": [], "linger": True,
                  "findings": ["reboot_required", "security_updates"], "ready": True}
        with experience(self.home) as conn:
            conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('care_check', ?)", (json.dumps(care),))
            conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('system_check', ?)", (json.dumps(system),))
        return care, system

    def test_status_shows_hermes_and_the_system_with_values(self):
        from hermes_atlas_evolution import status

        self.stored()
        report = status.build(self.home, now=NOW, config={}, runtime_config={})
        for code in ("hermes_swap_full", "hermes_unused_tools", "os_reboot_required", "os_security_updates"):
            self.assertIn(code, report["attention"])
        for lang in ("pt", "en"):
            text = status.markdown(report, lang)
            self.assertNotIn("{", text)
            self.assertIn("libc6", text)
            self.assertIn("0,7 GB" if lang == "pt" else "0.7 GB", text)
        self.assertIn("## Hermes e sistema", status.markdown(report, "pt"))

    def test_the_menu_suggests_and_opens_the_two_new_screens(self):
        care, system = self.stored()
        runs = []
        script = Script("12", "", "13", "", "0")
        with patch.object(hermes_care, "cached", return_value=care), \
                patch("hermes_atlas_evolution.system_update.cached", return_value=system), \
                patch.object(hermes_care, "run_action", side_effect=lambda *a, **k: runs.append(a) or {}):
            Menu(self.home, {}, lang="pt", console=Console(read=script.read, write=script.write, width=120, color=False),
                 clock=lambda: NOW).run()
        text = script.text
        for expected in ("Instalar 2 atualizações de segurança do sistema", "Liberar 700 MB",
                         "12  Saúde do Hermes (sem agente, sem tokens)", "13  Sistema operacional (Ubuntu 26.04.1 LTS)",
                         "Gateway: rodando · versão 0.23.1", "node-22.23.4 (200 MB)", "Reinício pendente: sim (libc6)",
                         "Pronto para atualizar: sim"):
            self.assertIn(expected, text)
        self.assertEqual(runs, [])

    def test_the_menu_repair_shows_what_it_released_and_its_folder(self):
        care, system = self.stored()
        script = Script("13", "4", "s", "", "0", "0")
        result = {"released": ["zlib1g"], "exit": 0, "state": "done", "ok": True, "folder": "/var/lib/x/run"}
        with patch.object(hermes_care, "cached", return_value=care), \
                patch("hermes_atlas_evolution.system_update.cached", return_value=system), \
                patch("hermes_atlas_evolution.system_update.repair", return_value=result):
            Menu(self.home, {}, lang="pt", console=Console(read=script.read, write=script.write, width=120, color=False),
                 clock=lambda: NOW).run()
        self.assertIn("Liberados os pacotes que uma atualização interrompida ainda retinha: zlib1g", script.text)
        self.assertIn("/var/lib/x/run", script.text)
        self.assertNotIn("'steps'", script.text)

    def test_the_command_line_reads_for_anyone_and_changes_for_the_operator(self):
        stream = io.StringIO()
        with patch("hermes_atlas_evolution.system_update.check", return_value={"supported": True, "findings": []}), \
                contextlib.redirect_stdout(stream):
            execute(SimpleNamespace(lang="en", evolution_action="system", system_action="check", full=False, skip=[],
                                    yes=False), self.home, runtime_config={})
        self.assertEqual(json.loads(stream.getvalue())["supported"], True)
        with patch.dict(os.environ, {"HERMES_SESSION_ID": "s1"}), self.assertRaises(SystemExit):
            execute(SimpleNamespace(lang="en", evolution_action="system", system_action="upgrade", full=False, skip=[],
                                    yes=True), self.home, runtime_config={})
        with patch.dict(os.environ, {"HERMES_SESSION_ID": "s1"}), self.assertRaises(SystemExit):
            execute(SimpleNamespace(lang="en", evolution_action="care", care_action="fix", yes=True), self.home,
                    runtime_config={})

    def test_decisions_list_the_digest_job(self):
        with experience(self.home) as conn:
            decisions.record_cron(conn, "abc", name="hermes-atlas-evolution-digest", schedule="0 8 * * 1",
                                  deliver="local", now=NOW)
            row = decisions.history(conn)[0]
        self.assertEqual((row["kind"], row["after"]["job"]), ("cron", "abc"))


class MemoryQualityTest(unittest.TestCase):
    def test_entries_that_differ_in_a_number_are_two_facts(self):
        from hermes_atlas_evolution import memory_care

        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        home = Path(folder.name)
        (home / "memories").mkdir()
        entries = [f"Service {n} listens on port {8000 + n} and keeps its data in volume {n % 7}." for n in range(150)]
        entries += ["The owner ships Atlas with patch packages and never copies dev over production.",
                    "The owner ships Atlas with patch packages, and never copies the dev tree over production."]
        (home / "memories" / "MEMORY.md").write_text("\n§\n".join(entries), encoding="utf-8")
        found = memory_care.review(home, {"memory": {"memory_char_limit": 100_000}})
        self.assertEqual([issue["kind"] for issue in found["issues"]], ["similar"])
        many = [f"Same text {n % 2}." for n in range(400)]   # 400 entries, 2 texts: tens of thousands of exact pairs
        (home / "memories" / "MEMORY.md").write_text("\n§\n".join(many), encoding="utf-8")
        capped = memory_care.review(home, {"memory": {"memory_char_limit": 100_000}})
        self.assertEqual(len(capped["issues"]), memory_care.MAX_ISSUES)
        self.assertGreater(capped["issues_total"], memory_care.MAX_ISSUES)


class PanelHealthTest(Base):
    def test_the_snapshot_carries_the_health_text_and_keeps_the_settings(self):
        from test_panel_decisions import load_api

        StatusAndMenuTest.stored(self)
        snap = load_api().snapshot(self.home, now=NOW, runtime_config={}, lang="pt", can_decide=True)
        self.assertIn("libc6", snap["health_md"])
        self.assertNotIn("{", snap["health_md"])
        self.assertIn("mode", snap["settings"]["values"])
        self.assertEqual(snap["status"]["system"]["security"], 2)


if __name__ == "__main__":
    unittest.main()
