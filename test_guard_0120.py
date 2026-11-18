"""0.12.0: the runtime guard after an adversarial review. Each command below passed the 0.11.0 guard (except the
false-positive probes, which must keep passing). One hostile command per line: the install scanner's regex is
greedy across string literals, so a second hostile string on a line would be read as one match."""

import time
import unittest
from pathlib import PurePosixPath
from unittest.mock import patch

from hermes_atlas_evolution import guard

HOME = PurePosixPath("/home/owner")
PROFILE = HOME / ".hermes"
SOURCE = PurePosixPath("/usr/local/lib/hermes-agent")
VENV = SOURCE / "venv"
PLUGIN = PROFILE / "plugins" / "hermes-atlas-evolution"
PROTECTED = (SOURCE, VENV, PROFILE)
TREES = (VENV, SOURCE / "hermes_cli")
PROCESS = ("python3",
           "/usr/local/lib/hermes-agent/venv/bin/python3 -m hermes_cli.main gateway run --replace")


class GuardTest(unittest.TestCase):
    def setUp(self):
        windows, guard._WINDOWS = guard._WINDOWS, False   # these are POSIX commands on every system
        self.addCleanup(setattr, guard, "_WINDOWS", windows)

    def verdict(self, command, **args):
        found = guard.check("terminal", {"command": command, **args}, protected=PROTECTED, home=HOME, profile=PROFILE,
                            trees=TREES, own=(PLUGIN,), process=PROCESS)
        return found["kind"] if found else None

    def assertVerdict(self, kind, commands, **args):
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(self.verdict(command, **args), kind)

    # F1 — destructive non-delete changes to the code the running Hermes needs
    def test_truncating_or_changing_the_code_trees_is_vetoed(self):
        self.assertVerdict("runtime_removal", [
            "truncate -s0 /usr/local/lib/hermes-agent/venv/pyvenv.cfg",
            "echo x > /usr/local/lib/hermes-agent/venv/pyvenv.cfg",
            "dd if=/dev/null of=/usr/local/lib/hermes-agent/venv/pyvenv.cfg",
            "echo x | tee /usr/local/lib/hermes-agent/venv/pyvenv.cfg",
            "chmod 000 /usr/local/lib/hermes-agent/venv",
            "chmod -R 000 ~/.hermes",
            "chown -R nobody /usr/local/lib/hermes-agent/venv",
            "rsync -a --delete /tmp/empty/ /usr/local/lib/hermes-agent/venv/",
            "ln -sf /dev/null /usr/local/lib/hermes-agent/hermes_cli/__init__.py",
            "cp /tmp/evil.py /usr/local/lib/hermes-agent/hermes_cli/main.py"])

    def test_clobbering_a_config_yaml_is_a_plugin_settings_veto(self):
        self.assertVerdict("plugin_settings", [
            "ln -sf /dev/null ~/.hermes/config.yaml",
            "truncate -s0 ~/.hermes/config.yaml",
            "echo '' > $HERMES_HOME/config.yaml"])

    def test_plain_writes_inside_the_profile_home_still_run(self):
        self.assertVerdict(None, [
            "echo note > ~/.hermes/skills/reminder.md",
            "echo data | tee ~/.hermes/memories/today.jsonl",
            "chmod +x ~/.hermes/skills/run.sh",
            "cp ~/.hermes/config.yaml /tmp/backup.yaml",
            "sed -i 's/model: a/model: b/' ~/.hermes/config.yaml",
            "truncate -s0 ~/.hermes/cache/screenshots/old.png",
            "echo x > ~/.hermes/profiles/work/notes.txt"])

    # F3 — nesting deeper than the old cap of three (each `eval` re-runs its arguments as a command, one level down)
    def test_deep_nesting_is_followed(self):
        self.assertVerdict("gateway_stop", [
            "eval eval eval eval systemctl --user restart hermes-gateway",
            "eval eval eval eval eval pkill -f hermes"])
        self.assertVerdict("runtime_removal", [
            "eval eval eval eval rm -rf /usr/local/lib/hermes-agent"])

    # F4 — simple literal assignments resolved before the command is classified
    def test_literal_variable_assignments_are_resolved(self):
        self.assertVerdict("gateway_stop", [
            "X=systemctl; $X --user restart hermes-gateway",
            "S=systemctl && $S --user stop hermes-gateway",
            "c=restart; systemctl --user $c hermes-gateway",
            "export G=hermes-gateway; systemctl --user restart ${G}"])
        self.assertVerdict("runtime_removal", ["X=~/.hermes; rm -rf $X"])
        self.assertVerdict(None, ["Y=$UNKNOWN; rm -rf $Y"])   # an unknown variable is still let through

    # F5 — watch runs a shell string; a heredoc fed into a shell; a glued here-string
    def test_string_runners_and_stdin_scripts(self):
        self.assertVerdict("gateway_stop", [
            "watch -n1 'systemctl --user restart hermes-gateway'",
            "cat <<EOF | bash\nsystemctl --user restart hermes-gateway\nEOF",
            "bash <<<'systemctl --user restart hermes-gateway'",
            "bash <<< 'systemctl --user restart hermes-gateway'"])
        self.assertVerdict("runtime_removal", ["watch -n 1 'rm -rf /usr/local/lib/hermes-agent'"])
        self.assertVerdict(None, ["watch -n 5 systemctl --user status hermes-gateway"])

    # F6 — applet multiplexers and glob command words
    def test_applet_multiplexers_and_glob_verbs(self):
        self.assertVerdict("gateway_stop", [
            "busybox pkill -f hermes",
            "/usr/bin/sys*ctl --user restart hermes-gateway",
            "/usr/bin/sys?ctl --user restart hermes-gateway"])
        self.assertVerdict("runtime_removal", [
            "busybox rm -rf /usr/local/lib/hermes-agent",
            "toybox rm -rf ~/.hermes"])

    # F7 — a relative path from `cd /`
    def test_relative_path_from_root_is_joined_correctly(self):
        self.assertVerdict("runtime_removal", [
            "cd / && rm -rf usr/local/lib/hermes-agent",
            "cd /usr && rm -rf local/lib/hermes-agent"])

    # F8 — padding past the length limit must not disable the guard
    def test_padding_past_the_limit_does_not_disable_the_guard(self):
        comment = "systemctl --user restart hermes-gateway #" + ("A" * 20_001)
        self.assertEqual(self.verdict(comment), "gateway_stop")
        heredoc = "rm -rf /usr/local/lib/hermes-agent <<EOF\n" + ("x\n" * 20_000) + "EOF"
        self.assertEqual(self.verdict(heredoc), "runtime_removal")
        self.assertIsNone(self.verdict("printf '%s' '" + ("A" * 20_050) + "'"))   # pure data, no hostile verb

    # F2 — the worst cases finish quickly (used to take 10-22 s and could cross Hermes' 30 s timeout)
    def test_the_worst_cases_are_well_under_a_second(self):
        worst = [
            "ls" + "|xargs rm" * 1600,
            ("ls" + "|xargs rm -rf x" * 3000)[:19_990],
            ("cat <<E\nx\nE\n" * 2000)[:19_990],
            ("bash <<E\nx\nE\n" * 2000)[:19_990],
            ("$(x) " * 5000)[:19_990],
            "ps aux" + "| grep x" * 2400,
            "find / -name x" + " | xargs rm -f" * 1500,
            "sudo " * 3990 + "ls",                          # wrappers: 3.9 s before the bounded scan
            "flock " * 3330 + "ls",                         # 10.7 s
            "flock x " * 2490 + "ls config.yaml plugins"]   # 41 s
        for command in worst:
            with self.subTest(command=command[:40]):
                start = time.perf_counter()
                self.verdict(command)
                # ~0.6 s here; 8 s leaves room for a loaded CI runner and still fails the old 12-22 s
                self.assertLess(time.perf_counter() - start, 8.0)

    # review of 0.12.0: a change is judged with its own segment's folder and variables
    def test_a_change_is_read_from_the_folder_cd_moved_to(self):
        self.assertVerdict("runtime_removal", [
            "cd /usr/local/lib/hermes-agent && chmod -R 000 venv",
            "cd /usr/local/lib/hermes-agent/venv/bin && truncate -s 0 python3",
            "D=/usr/local/lib/hermes-agent/venv; truncate -s0 $D/bin/python",
            "cd /usr/local/lib/hermes-agent/venv && echo x > pyvenv.cfg"])
        self.assertVerdict("plugin_settings", [
            "cd ~/.hermes/plugins && ln -sfn /tmp/fake hermes-atlas-evolution",
            "cd ~/.hermes && echo 'plugins: {}' > config.yaml"])

    def test_replacing_a_config_yaml_is_vetoed_and_appending_is_not(self):
        self.assertVerdict("plugin_settings", [
            "cp new.yaml ~/.hermes/config.yaml",
            "mv /tmp/c.yaml ~/.hermes/profiles/work/config.yaml",
            "install -m600 c.yaml ~/.hermes/config.yaml",
            "echo 'plugins: {}' >> ~/.hermes/config.yaml"])
        self.assertVerdict(None, [
            "echo 'display: {language: pt}' >> ~/.hermes/config.yaml",
            "cp x ~/site/hermes/config.yaml",
            "cp ~/.hermes/config.yaml ~/.hermes/config.yaml.bak",
            "grep '>' /usr/local/lib/hermes-agent/venv/pyvenv.cfg"])

    # review of 0.12.0, second round: a segment after a filtered `find -delete` is still checked (a shadowed name
    # once made the next segment raise inside the guard, which fails open)
    def test_a_segment_after_a_filtered_find_delete_is_checked(self):
        self.assertVerdict("gateway_stop", ["find /tmp -name '*.x' -delete; systemctl --user restart hermes-gateway"])
        self.assertVerdict("runtime_removal", ["find /tmp -name '*.x' -delete; rm -rf ~/.hermes"])
        self.assertVerdict(None, ["find /tmp -name '*.x' -delete; echo done"])

    # however many options come first, the switch that hands over a command string is found
    def test_a_command_string_after_many_options_is_read(self):
        self.assertVerdict("gateway_stop", [
            "bash " + "-e " * 40 + "-c 'systemctl --user restart hermes-gateway'",
            "runuser " + "-u root " * 20 + "-c 'systemctl --user restart hermes-gateway'",
            "flock " + "-w 1 " * 20 + "/tmp/l -c 'systemctl --user restart hermes-gateway'",
            "env " + "-u X " * 20 + "-S 'systemctl --user restart hermes-gateway'",
            "watch " + "-n 1 " * 30 + "'systemctl --user restart hermes-gateway'"])
        self.assertVerdict("runtime_removal", ["su " + "-l " * 40 + "-c 'rm -rf ~/.hermes'"])
        self.assertVerdict(None, ["bash script.sh -c x",
                                  "su - root",
                                  "runuser -u root -- ls",
                                  "flock /tmp/l make build"])

    def test_inline_writes_of_a_config_yaml_and_noclobber_redirects(self):
        self.assertVerdict("plugin_settings", [
            "python3 -c 'open(\"/home/owner/.hermes/config.yaml\", \"w\").write(\"x: 1\")'"])
        self.assertVerdict("runtime_removal", ["echo x >| /usr/local/lib/hermes-agent/venv/bin/python3"])
        self.assertVerdict(None, [
            "python3 -c 'print(open(\"/home/owner/.hermes/config.yaml\").read())'",
            "python3 -c 'open(\"/home/owner/site/hermes/config.yaml\", \"w\").write(\"x\")'",
            "echo 'a >| b' | cat"])

    # a change to the runtime is told as a change (chmod does not delete anything), and counted as runtime_removal
    def test_a_change_has_its_own_sentence_and_the_portuguese_name_is_protecao_do_hermes(self):
        for lang, changed, removed in (("en", "change, overwrite or lock", "delete or move"),
                                       ("pt", "mudaria, sobrescreveria ou travaria", "apagaria ou moveria")):
            counted = []
            runtime = guard.RuntimeGuard(protected=PROTECTED, home=HOME, profile=PROFILE, trees=TREES, own=(PLUGIN,),
                                         process=PROCESS, lang=lang, on_block=counted.append)
            for command, sentence in (("chmod 000 /usr/local/lib/hermes-agent/venv", changed),
                                      ("truncate -s0 /usr/local/lib/hermes-agent/venv/pyvenv.cfg", changed),
                                      ("rm -rf /usr/local/lib/hermes-agent/venv", removed)):
                with self.subTest(lang=lang, command=command):
                    message = runtime(tool_name="terminal", args={"command": command})["message"]
                    self.assertIn(sentence, message)
                    self.assertIn("/usr/local/lib/hermes-agent/venv", message)
            self.assertEqual(counted, ["runtime_removal"] * 3)   # signals.record_guard keeps its three kinds
        for text in guard.MESSAGES["pt"].values():
            self.assertTrue(text.startswith("Proteção do Hermes"), text)
            self.assertNotIn("Guarda de runtime", text)
        self.assertEqual(set(guard.MESSAGES["en"]), set(guard.MESSAGES["pt"]))

    # the guard records its own internal failures instead of swallowing them, and still fails open
    def test_an_internal_failure_is_recorded_and_fails_open(self):
        errors = []
        runtime = guard.RuntimeGuard(protected=PROTECTED, home=HOME, on_error=errors.append)
        with patch.object(guard, "check", side_effect=RuntimeError("boom")):
            directive = runtime(tool_name="terminal", args={"command": "systemctl --user restart hermes-gateway"})
        self.assertIsNone(directive)                  # fail open: any doubt lets the command through
        self.assertEqual(runtime.errors, 1)           # but the failure is counted, not silent
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)

    # about forty ordinary commands the review confirmed must keep running
    def test_ordinary_commands_are_not_touched(self):
        self.assertVerdict(None, [
            "systemctl --user status hermes-gateway",
            "systemctl show hermes-gateway",
            "systemctl --user is-active hermes-gateway",
            "journalctl --user -u hermes-gateway -n 50",
            "grep restart /etc/hermes/notes.txt",
            "echo 'systemctl restart hermes-gateway'",
            "git log --oneline -5",
            "ps aux | grep hermes",
            "kill 4242",
            "kill -9 999999",
            "kill $(cat /home/owner/app/app.pid)",
            "pkill -f my_worker",
            "rm -rf ./build",
            "rm -rf /tmp/scratch",
            "rm -rf /home/owner/project/hermes/build",
            "rm -rf /usr/local/lib/hermes-agent-old",
            "hermes config set display.language pt",
            "hermes config set plugins.entries.other-plugin.settings.key v",
            "hermes config get plugins.entries.hermes-atlas-evolution.settings.mode",
            "hermes plugins update hermes-atlas-evolution",
            "hermes gateway status",
            "pip install requests",
            "cat ~/.hermes/plugins/hermes-atlas-evolution/README.md",
            "systemctl restart my-hermes-helper",
            "docker restart some-container",
            "grep -rn hermes_cli .",
            "find ~/.hermes -name '*.log' -newermt yesterday",
            "chmod +x ./build/run.sh",
            "cp build/out /tmp/out",
            "watch -n 5 ls"])


class BundledRuntimeTest(unittest.TestCase):
    """Hermes' bundled layout (and the production host): its Python lives in ~/.hermes/tools/python-*, so the home,
    ~/.hermes and ~/.hermes/tools are all folders above a protected tree. A plain copy into one of them is fine."""

    ROOT = PurePosixPath("/root")
    PROFILE = ROOT / ".hermes" / "profiles" / "work"
    TOOLS = ROOT / ".hermes" / "tools" / "python-3.14.10+build-linux-x64"

    def setUp(self):
        windows, guard._WINDOWS = guard._WINDOWS, False
        self.addCleanup(setattr, guard, "_WINDOWS", windows)

    def verdict(self, command):
        found = guard.check("terminal", {"command": command}, home=self.ROOT, profile=self.PROFILE,
                            protected=(self.ROOT / ".hermes", self.PROFILE, SOURCE),
                            trees=(self.TOOLS, VENV), own=(self.PROFILE / "plugins" / "hermes-atlas-evolution",))
        return found["kind"] if found else None

    def test_plain_copies_above_the_bundled_python_are_allowed(self):
        for command in ("cp report.pdf ~/",
                        "mv /tmp/x.zip /root",
                        "cp -r project ~",
                        "cp notes.md ~/.hermes/",
                        "cp a.txt ~/.hermes/tools/",
                        "install -m755 tool /usr/local/lib/"):
            with self.subTest(command=command):
                self.assertIsNone(self.verdict(command))

    def test_changes_that_reach_the_bundled_python_are_vetoed(self):
        for command in ("chmod -R 000 ~/.hermes/tools",
                        "rsync -a --delete /tmp/empty/ ~/.hermes/",
                        "cp evil ~/.hermes/tools/python-3.14.10+build-linux-x64/bin/python3",
                        "cd ~/.hermes/tools/python-3.14.10+build-linux-x64/bin && truncate -s 0 python3"):
            with self.subTest(command=command):
                self.assertEqual(self.verdict(command), "runtime_removal")


class NoInternalErrorTest(unittest.TestCase):
    """Every command the reviews of 0.12.0 probed the guard with goes through the hook with an `on_error` that fails
    the test. A guard that raises fails open, so an internal error is a bypass: a shadowed name once let
    `find ... -delete; rm -rf ~/.hermes` through. One command per line (see the module docstring)."""

    PROBES = [
        'systemctl --user restart hermes-gateway',
        'X=systemctl; $X --user restart hermes-gateway',
        'S=systemctl && $S --user stop hermes-gateway',
        'c=restart; systemctl --user $c hermes-gateway',
        'bash -c \'bash -c "bash -c \\"systemctl --user restart hermes-gateway\\""\'',
        'sh -c \'sh -c "sh -c \\"sh -c systemctl\\""\'',
        "bash <<< 'systemctl --user restart hermes-gateway'",
        "bash <<<'systemctl --user restart hermes-gateway'",
        'sh < <(echo systemctl --user restart hermes-gateway)',
        "bash <(echo 'systemctl --user restart hermes-gateway')",
        'cat <<EOF | bash\nsystemctl --user restart hermes-gateway\nEOF',
        "printf 'systemctl --user restart hermes-gateway' | bash",
        'echo c3lzdGVtY3Rs--skip | base64 -d | bash',
        'cat unit.txt | xargs systemctl --user restart',
        'exec systemctl --user restart hermes-gateway',
        '\\systemctl --user restart hermes-gateway',
        "'systemctl' --user restart hermes-gateway",
        "sys''temctl --user restart hermes-gateway",
        '/usr/bin/sys*ctl --user restart hermes-gateway',
        'command systemctl --user restart hermes-gateway',
        'nohup systemctl --user restart hermes-gateway',
        'setsid systemctl --user restart hermes-gateway',
        'timeout 5 systemctl --user restart hermes-gateway',
        'sudo -u root systemctl restart hermes-gateway',
        'doas systemctl restart hermes-gateway',
        'busybox pkill -f hermes',
        'find / -name xx -exec systemctl --user restart hermes-gateway \\;',
        "watch -n1 'systemctl --user restart hermes-gateway'",
        'pkill -f hermes',
        'pkill -9 -f gateway',
        'killall python3',
        'fuser -k 8787/tcp',
        'systemd-run --user systemctl restart hermes-gateway',
        'systemd-run --on-active=1s systemctl --user restart hermes-gateway',
        'python3 -c "import os;os.system(\'systemctl --user restart hermes-gateway\')"',
        'python3 -c "import subprocess;subprocess.run([\'systemctl\',\'--user\',\'restart\',\'hermes-gateway\'])"',
        'python3 -c \'import os,base64;os.system(base64.b64decode(b"eA=="))\'',
        'perl -e \'system("systemctl --user restart hermes-gateway")\'',
        'node -e "require(\'child_process\').execSync(\'systemctl --user restart hermes-gateway\')"',
        'env X=1 systemctl --user restart hermes-gateway',
        'env systemctl --user restart hermes-gateway',
        'rm -rf ~/.hermes',
        'rm -rf $HERMES_HOME',
        'rm -rf ${HERMES_HOME}',
        'find ~/.hermes -delete',
        'mv ~/.hermes /tmp/x',
        'mv ~/.hermes{,.bak}',
        'rm -rf /usr/local/lib/hermes-agent/venv',
        'rm -rf /usr/local/lib/hermes-agent/venv/*',
        '>| /usr/local/lib/hermes-agent/venv/bin/python3',
        'truncate -s0 /usr/local/lib/hermes-agent/venv/pyvenv.cfg',
        'chmod 000 /usr/local/lib/hermes-agent/venv',
        'chmod -R 000 ~/.hermes',
        'ln -sf /dev/null ~/.hermes/config.yaml',
        'rsync -a --delete /tmp/empty/ /usr/local/lib/hermes-agent/venv/',
        'rm -rf ~/.hermes/plugins/hermes-atlas-evolution',
        'X=~/.hermes; rm -rf $X',
        'cd ~/.hermes && rm -rf .',
        'cd / && rm -rf usr/local/lib/hermes-agent',
        'rm -rf -- /usr/local/lib/hermes-agent',
        'hermes config set plugins.entries.hermes-atlas-evolution.settings.runtime_guard false',
        'hermes plugins disable hermes-atlas-evolution',
        'hermes config set plugins.isolation host',
        "sed -i 's/true/false/' ~/.hermes/config.yaml  # plugins runtime_guard",
        'systemctl status hermes-gateway',
        'systemctl --user is-active hermes-gateway',
        'systemctl show hermes-gateway',
        'journalctl -u hermes-gateway -n 50',
        'grep restart /etc/hermes/notes.txt',
        'echo "systemctl restart hermes-gateway"',
        'git log --oneline -5',
        'ps aux | grep hermes',
        'kill 4242',
        'kill -9 999999',
        'rm -rf ./build',
        'rm -rf /tmp/scratch',
        'hermes config set plugins.entries.other-plugin.settings.key v',
        'hermes config set agent.model gpt-4',
        'cat ~/.hermes/plugins/hermes-atlas-evolution/README.md',
        'pip install requests',
        'rm -rf /home/owner/project/hermes/build',
        'systemctl restart my-hermes-helper',
        'docker restart some-container',
        'kill $(cat /home/owner/app/app.pid)',
        'pkill -f my_worker',
        'grep -rn hermes_cli .',
        'hermes gateway status',
        'hermes config get plugins.entries.hermes-atlas-evolution.settings.mode',
        "find ~/.hermes -name '*.log' -newermt yesterday",
        'cp ~/.hermes/config.yaml /tmp/backup.yaml',
        'systemctl ' + ' ' * 19000 + 'restart',
        'rm -rf ' + '{a,b}' * 80,
        '$($($($($($($($($($($($($($($($($($($($($($($($($($($($($($($($($($($($($($($($($($($($($($($($($($($($($($($($($($($($(x))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))',
        ';'.join(['echo x'] * 2000),
        'echo x' + '| xargs echo ' * 500,
        'cat <<EOF\n' + '\n'.join(['x'] * 3000) + '\nEOF',
        "pgrep -f '(a+)+aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa!'",
        "ps | awk '" + '/x/' * 200 + "{print}'",
        'bash -c ' + ('"' + 'echo ') * 1 + 'x' * 5000,
        "curl 'http://x/" + '%25' * 3000 + "'",
        'echo ' + '{a,b,c,d}' * 40,
        123,
        ['systemctl', 'restart'],
        'systemctl\x00 --user restart hermes-gateway',
        '\x00\x00\x00',
        '$(' * 5000 + 'x' + ')' * 5000,
        "rm -rf '~/.hermes",
        'rm -rf x',
        'systemctl restart ' + '[' * 500,
        'hermes config set plugins\x00x set',
        'watch systemctl --user restart hermes-gateway',
        "watch -n 1 'rm -rf /usr/local/lib/hermes-agent'",
        'busybox rm -rf /usr/local/lib/hermes-agent',
        'toybox rm -rf ~/.hermes',
        'bash -c "bash -c \'rm -rf /usr/local/lib/hermes-agent\'"',
        'bash -c \'bash -c "bash -c \\\'rm -rf /usr/local/lib/hermes-agent\\\'"\'',
        'cd /usr && rm -rf local/lib/hermes-agent',
        'echo x > /usr/local/lib/hermes-agent/venv/pyvenv.cfg',
        'dd if=/dev/null of=/usr/local/lib/hermes-agent/venv/pyvenv.cfg',
        'echo x | tee /usr/local/lib/hermes-agent/venv/pyvenv.cfg',
        'chown nobody -R /usr/local/lib/hermes-agent/venv',
        '/usr/bin/sys?ctl --user restart hermes-gateway',
        'flock x ' * 2490 + 'ls config.yaml plugins',
        'flock ' * 3320 + ' ls config.yaml plugins | bash',
        'sudo ' * 3990 + 'ls',
        'time ' * 3995 + 'ls',
        'nohup ' * 3330 + 'ls',
        'find . -exec ' * 1500 + 'rm {} +',
        'ssh localhost ' * 1400 + 'ls',
        'watch ' * 3000 + 'ls',
        'echo ' + '$(ls)' * 4000,
        'find / -name x' + ' | xargs rm -f' * 1500,
        "echo 'rm -rf /tmp/x' | sh; " * 800,
        'echo x > /tmp/a ' * 1250,
        'echo \'>\' ">" ' * 1500,
        'cd /tmp && echo > x; ' * 900,
        ''.join((f'A{i}=/tmp/{i}; echo > $A{i}; ' for i in range(700))),
        'ln -s /a ' * 2200,
        'chmod 1 /a ' * 1800,
        'tee -a /a ' * 2000,
        'bash -c "bash -c "bash -c "bash -c "bash -c "bash -c "bash -c "bash -c "ls""""""""',
        "bash -c 'echo x > /tmp/y'; " * 700,
        ('cat <<EOF | bash\n' + 'echo hi > /tmp/z\n' * 5 + 'EOF\n') * 250,
        'echo "' + '> /tmp/x ' * 2000,
        "find /tmp -name '*.x' -delete; systemctl --user restart hermes-gateway",
        "find /tmp -name '*.x' -delete; rm -rf ~/.hermes",
        "find /tmp -name '*.x' -delete; echo ok",
        "bash -e -e -e -e -e -e -e -e -e -e -e -e -e -e -e -e -e -e -e -e -e -e -e -e -e -e -e -e -e -e -e -e -e -e -e -e -e -e -e -e -c 'systemctl --user restart hermes-gateway'",
        "su -l -l -l -l -l -l -l -l -l -l -l -l -l -l -l -l -l -l -l -l -l -l -l -l -l -l -l -l -l -l -l -l -l -l -l -l -l -l -l -l -c 'rm -rf ~/.hermes'",
        "runuser -u root -u root -u root -u root -u root -u root -u root -u root -u root -u root -u root -u root -u root -u root -u root -u root -u root -u root -u root -u root -c 'systemctl --user restart hermes-gateway'",
        "flock -w 1 -w 1 -w 1 -w 1 -w 1 -w 1 -w 1 -w 1 -w 1 -w 1 -w 1 -w 1 -w 1 -w 1 -w 1 -w 1 -w 1 -w 1 -w 1 -w 1 /tmp/l -c 'systemctl --user restart hermes-gateway'",
        "env -u X -u X -u X -u X -u X -u X -u X -u X -u X -u X -u X -u X -u X -u X -u X -u X -u X -u X -u X -u X -S 'systemctl --user restart hermes-gateway'",
        "watch -n 1 -n 1 -n 1 -n 1 -n 1 -n 1 -n 1 -n 1 -n 1 -n 1 -n 1 -n 1 -n 1 -n 1 -n 1 -n 1 -n 1 -n 1 -n 1 -n 1 -n 1 -n 1 -n 1 -n 1 -n 1 -n 1 -n 1 -n 1 -n 1 -n 1 'systemctl --user restart hermes-gateway'",
        'python3 -c \'open("/root/.hermes/profiles/work/config.yaml","w").write("x: 1")\'',
        'echo x >| /usr/local/lib/hermes-agent/venv/bin/python3',
    ]

    def setUp(self):
        windows, guard._WINDOWS = guard._WINDOWS, False
        self.addCleanup(setattr, guard, "_WINDOWS", windows)

    def test_no_probe_raises_inside_the_guard(self):
        root, work = PurePosixPath("/root"), PurePosixPath("/root/.hermes/profiles/work")
        layouts = ({"protected": PROTECTED, "home": HOME, "profile": PROFILE, "trees": TREES, "own": (PLUGIN,)},
                   {"protected": (root / ".hermes", work, SOURCE), "home": root, "profile": work,
                    "trees": (root / ".hermes" / "tools" / "python-3.14.10+x", VENV),
                    "own": (work / "plugins" / "hermes-atlas-evolution",)})
        for number, layout in enumerate(layouts):
            errors = []
            runtime = guard.RuntimeGuard(process=PROCESS, on_error=errors.append, **layout)
            for command in self.PROBES:
                if number and isinstance(command, str) and len(command) > 2_000:
                    continue   # the long timing probes once is enough; the second layout reads the rest
                runtime(tool_name="terminal", args={"command": command})
                self.assertEqual(errors, [], repr(command)[:200])


if __name__ == "__main__":
    unittest.main()
