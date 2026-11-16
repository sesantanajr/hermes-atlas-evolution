"""The runtime guard vetoes commands that would take down the Hermes the agent runs in, and nothing else."""

import tempfile
import os
import unittest
from pathlib import Path

from hermes_atlas_evolution.guard import RuntimeGuard, check

HOME = Path("/home/owner")
RUNTIME = HOME / ".hermes" / "tools" / "python-3.14.10+build-linux-x64"
PROTECTED = (RUNTIME, Path("/usr/local/lib/hermes-agent"), HOME / ".hermes")



def setUpModule():
    """The rules for POSIX paths, whatever system runs the tests; the Windows test switches them itself."""
    from unittest.mock import patch

    from hermes_atlas_evolution import guard

    global _POSIX
    _POSIX = patch.object(guard, "_WINDOWS", False)
    _POSIX.start()


def tearDownModule():
    _POSIX.stop()

def verdict(command):
    found = check("terminal", {"command": command}, protected=PROTECTED, home=HOME)
    return found["kind"] if found else None


class CheckTest(unittest.TestCase):
    def test_stopping_the_gateway_from_inside_is_vetoed(self):
        for command in ("systemctl --user restart hermes-gateway",
                        "systemctl --user restart hermes-gateway.service && echo ok",
                        "sudo systemctl stop hermes-gateway",
                        "XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart hermes-gateway.service",
                        "cd /tmp; systemctl --user kill hermes-gateway",
                        "hermes gateway stop", "hermes -p atlas-ai gateway stop",
                        "hermes gateway restart", "hermes gateway restart --yes", "hermes -p atlas-ai gateway restart -y",
                        "bash -c 'systemctl --user restart hermes-gateway'",
                        "systemd-run --user --on-active=60 --unit=atlas-gw-otim --collect systemctl --user restart hermes-gateway",
                        "systemd-run --user -p Nice=5 -- systemctl --user restart hermes-gateway.service",
                        "bash -lc 'systemctl --user restart hermes-gateway'", "sh -ec \"killall hermes\"",
                        "setsid systemctl --user restart hermes-gateway", "setsid -f pkill -f hermes",
                        "flock /tmp/gw.lock systemctl --user restart hermes-gateway",
                        "flock -w 5 /tmp/gw.lock -c 'systemctl --user stop hermes-gateway'",
                        "su -c 'systemctl --user restart hermes-gateway' root",
                        "runuser -u root -- systemctl --user restart hermes-gateway",
                        "script -qc 'systemctl --user restart hermes-gateway' /dev/null",
                        "rtk run systemctl --user restart hermes-gateway", "rtk err -- pkill -f hermes",
                        "watch -n 60 systemctl --user restart hermes-gateway",
                        "xargs -n1 systemctl --user restart hermes-gateway",
                        "sh -c \"killall hermes\"",
                        "pkill -f 'hermes gateway'", "killall hermes", "timeout 5 pkill -9 -f hermes"):
            with self.subTest(command):
                self.assertEqual(verdict(command), "gateway_stop")

    def test_deleting_or_moving_what_hermes_runs_on_is_vetoed(self):
        for command in ("rm -rf ~/.hermes/tools/python-3.14*", f"mv {RUNTIME} /tmp/old-python",
                        "rm -rf $HOME/.hermes",
                        "sudo rm -r -f /usr/local/lib/hermes-agent/",
                        "find ~/.hermes/tools -name 'python*' -delete", "cd / && rm -rf ~/.hermes/tools",
                        "bash -c 'rm -rf ~/.hermes/tools'",
                        "cat > a.txt <<EOF\nhello\nEOF\nrm -rf ~/.hermes"):
            with self.subTest(command):
                self.assertEqual(verdict(command), "runtime_removal")

    def test_ordinary_administration_passes(self):
        for command in ("systemctl --user status hermes-gateway", "journalctl --user -u hermes-gateway -n 50",
                        "hermes gateway status", "echo 'systemctl restart hermes-gateway'",
                        "bash -c 'ls -la'", "bash script.sh", "systemd-run --user --on-active=60 echo ok",
                        "rtk run git status", "flock /tmp/x make build", "watch -n 5 systemctl --user status hermes-gateway",
                        "rm -rf ~/.hermes/tools/python-3.13.2-old", "rm -rf /tmp/build", "pgrep -f hermes",
                        "ls ~/.hermes/tools", "grep -r killall ~/notes", "mv ~/.hermes/profiles/a/x.txt /tmp/",
                        "systemctl --user restart atlas-ui.service", "",
                        "cat > restart.sh <<'EOF'\nsystemctl --user restart hermes-gateway\nrm -rf ~/.hermes\nEOF\nchmod +x restart.sh",
                        "cat <<-EOF > notes.txt\n\tkillall hermes\n\tEOF"):
            with self.subTest(command):
                self.assertIsNone(verdict(command))

    def test_other_tools_and_odd_payloads_pass(self):
        self.assertIsNone(check("web_search", {"command": "killall hermes"}, protected=PROTECTED, home=HOME))
        for args in (None, "killall hermes", {"command": 7}, {"cmd": "killall hermes"}):
            self.assertIsNone(check("terminal", args, protected=PROTECTED, home=HOME))


class HookTest(unittest.TestCase):
    def test_the_hook_blocks_with_the_safe_alternative_in_the_profile_language_and_counts(self):
        seen = []
        guard = RuntimeGuard(protected=PROTECTED, home=HOME, lang="pt", on_block=seen.append)
        directive = guard(tool_name="terminal", args={"command": "systemctl --user restart hermes-gateway"})
        self.assertEqual(directive["action"], "block")
        self.assertIn("hermes gateway restart", directive["message"])
        self.assertIn("dono", directive["message"])          # the owner restarts, from their own terminal
        self.assertIn("turnos", directive["message"])
        self.assertEqual(seen, ["gateway_stop"])
        self.assertIsNone(guard(tool_name="terminal", args={"command": "ls"}))

    def test_the_hook_never_raises(self):
        def broken(kind):
            raise RuntimeError("storage down")

        guard = RuntimeGuard(protected=PROTECTED, home=HOME, lang="en", on_block=broken)
        directive = guard(tool_name="terminal", args={"command": "killall hermes"})
        self.assertEqual(directive["action"], "block")   # a counting failure never lets it through or raises
        self.assertIsNone(guard(tool_name="terminal", args={"command": "x" * 500_000}))

    def test_protected_paths_come_from_the_running_interpreter_and_hermes(self):
        from hermes_atlas_evolution.guard import protected_paths

        folder = Path(tempfile.mkdtemp())
        paths = protected_paths(hermes_root=folder, profile_home=folder / "profiles" / "work")
        self.assertIn(folder.resolve(), paths)
        self.assertIn((folder / "profiles" / "work").resolve(), paths)
        self.assertTrue(all(isinstance(path, Path) and path.is_absolute() for path in paths))



class PlatformTest(unittest.TestCase):
    """How Hermes runs its gateway on each system: systemd units with a suffix, launchd, Windows."""

    def test_profile_and_custom_home_units_are_vetoed(self):
        for command in ("systemctl --user restart hermes-gateway-atlas-ai", "systemctl --user restart hermes-gateway-atlas-ai.service",
                        "systemctl restart hermes-gateway-1a2b3c4d.service", "service hermes-gateway-coder restart"):
            with self.subTest(command):
                self.assertEqual(verdict(command), "gateway_stop")
        self.assertIsNone(verdict("systemctl --user status hermes-gateway-atlas-ai"))

    def test_launchd_stops_are_vetoed_and_reads_pass(self):
        for command in ("launchctl kickstart -k gui/501/ai.hermes.gateway", "launchctl kickstart -kp gui/501/ai.hermes.gateway-coder",
                        "launchctl stop ai.hermes.gateway", "launchctl bootout gui/501/ai.hermes.gateway",
                        "launchctl unload ~/Library/LaunchAgents/ai.hermes.gateway.plist", "launchctl remove ai.hermes.gateway-coder",
                        "launchctl kill SIGTERM gui/501/ai.hermes.gateway"):
            with self.subTest(command):
                self.assertEqual(verdict(command), "gateway_stop")
        for command in ("launchctl kickstart gui/501/ai.hermes.gateway", "launchctl print gui/501/ai.hermes.gateway",
                        "launchctl list | grep hermes", "launchctl stop com.example.other"):
            with self.subTest(command):
                self.assertIsNone(verdict(command))

    def test_windows_service_and_task_stops_are_vetoed_and_reads_pass(self):
        for command in ("sc stop Hermes_Gateway", "sc.exe stop Hermes_Gateway_coder", "net stop Hermes_Gateway",
                        "powershell -Command \"Stop-Service -Name Hermes_Gateway\"",
                        "pwsh -NoProfile -c 'Restart-Service Hermes_Gateway'", "Stop-ScheduledTask -TaskName Hermes_Gateway",
                        "schtasks /End /TN Hermes_Gateway", "taskkill /F /IM hermes.exe", "Stop-Process -Name hermes -Force",
                        "cmd //c \"sc stop Hermes_Gateway\"", "C:/Windows/System32/sc.exe stop Hermes_Gateway"):
            with self.subTest(command):
                self.assertEqual(verdict(command), "gateway_stop")
        for command in ("sc query Hermes_Gateway", "schtasks /Query /TN Hermes_Gateway", "Get-Service Hermes_Gateway",
                        "net start Hermes_Gateway", "taskkill /F /IM notepad.exe"):
            with self.subTest(command):
                self.assertIsNone(verdict(command))

    def test_windows_paths_are_compared_in_every_spelling(self):
        from unittest.mock import patch

        from hermes_atlas_evolution import guard

        home, profile = Path("C:/Users/owner"), "C:/Users/owner/AppData/Local/hermes"
        env = {"LOCALAPPDATA": "C:\\Users\\owner\\AppData\\Local"}
        with patch.object(guard, "_WINDOWS", True), patch.dict(guard.os.environ, env):
            def windows(command):
                found = check("terminal", {"command": command}, protected=(profile, "S:/hermes/runtime"), home=home)
                return found["kind"] if found else None

            for command in ("rm -r -f 'C:\\Users\\owner\\AppData\\Local\\hermes'", "rm -r -f C:/Users/owner/AppData/Local/hermes",
                            "rm -r -f /c/Users/owner/AppData/Local/hermes", "rm -r -f /C/users/OWNER/appdata/local/HERMES/",
                            "rm -r -f ~/AppData/Local/hermes", "rm -r -f $LOCALAPPDATA/hermes",
                            "Remove-Item -Recurse -Force $env:LOCALAPPDATA/hermes", "del /s /q 'C:\\Users\\owner\\AppData'",
                            "rm -r -f 'S:\\hermes'"):
                with self.subTest(command):
                    self.assertEqual(windows(command), "runtime_removal")
            for command in ("rm -r -f C:/Users/owner/AppData/Local/hermes-backup", "del /s /q 'S:\\tmp\\build'",
                            "rm -r -f $UNSET_VARIABLE/hermes", "rm -r -f ./hermes", "Remove-Item C:/Users/owner/Downloads/x.zip"):
                with self.subTest(command):
                    self.assertIsNone(windows(command))

    @unittest.skipIf(os.name == "nt", "uv-style symlinks and 8.3 short names: POSIX scenario")
    def test_a_linked_runtime_is_protected_under_the_name_commands_use(self):
        import os
        import sys
        from unittest.mock import patch

        from hermes_atlas_evolution.guard import protected_paths

        root = Path(tempfile.mkdtemp())
        real = root / "uv" / "python" / "cpython-3.11.16-linux-x86_64-gnu"
        (real / "bin").mkdir(parents=True)
        link = root / "uv" / "python" / "cpython-3.11-linux-x86_64-gnu"
        os.symlink(real, link)
        venv = root / "hermes-agent" / "venv"
        venv.mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text(f"home = {link / 'bin'}\ninclude-system-site-packages = false\n")
        data = root / "data" / "hermes"
        data.mkdir(parents=True)
        home_link = root / "owner" / ".hermes"
        home_link.parent.mkdir()
        os.symlink(data, home_link)
        with patch.object(sys, "prefix", str(venv)), patch.object(sys, "exec_prefix", str(venv)), \
                patch.object(sys, "base_prefix", str(real)), patch.object(sys, "_base_executable", str(link / "bin" / "python3.11")):
            paths = protected_paths(hermes_root=home_link, profile_home=home_link / "profiles" / "work")
        self.assertTrue({link, real, home_link, data.resolve()} <= set(paths), paths)
        for command in (f"rm -rf {link}", f"rm -rf {root}/uv/python/cpython-3.11*", f"rm -rf {home_link}",
                        f"mv {link} /tmp/old"):
            with self.subTest(command):
                found = check("terminal", {"command": command}, protected=paths, home=root / "owner")
                self.assertEqual(found and found["kind"], "runtime_removal")

    def test_hermes_home_in_a_command_is_the_profile(self):
        profile = Path("/home/owner/.hermes/profiles/work")
        found = check("terminal", {"command": "rm -rf $HERMES_HOME"}, protected=(profile,), home=HOME, profile=profile)
        self.assertEqual(found["kind"], "runtime_removal")


if __name__ == "__main__":
    unittest.main()
