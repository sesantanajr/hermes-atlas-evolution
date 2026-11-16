"""The runtime guard vetoes commands that would take down the Hermes the agent runs in, and nothing else."""

import tempfile
import unittest
from pathlib import Path

from hermes_atlas_evolution.guard import RuntimeGuard, check

HOME = Path("/home/owner")
RUNTIME = HOME / ".hermes" / "tools" / "python-3.14.10+build-linux-x64"
PROTECTED = (RUNTIME, Path("/usr/local/lib/hermes-agent"), HOME / ".hermes")


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
                        "bash -c 'ls -la'", "bash script.sh",
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


if __name__ == "__main__":
    unittest.main()
