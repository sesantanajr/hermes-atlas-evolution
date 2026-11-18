"""0.8.0: the runtime guard after an adversarial review. Each command below passed the 0.7.0 guard."""

import unittest
from pathlib import PurePosixPath

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

    def test_shell_words_that_open_a_compound_command_do_not_hide_it(self):
        self.assertVerdict("gateway_stop", [
            "if ! systemctl --user is-active --quiet hermes-gateway; then systemctl --user restart hermes-gateway; fi",
            "for i in 1 2; do hermes gateway restart --yes; done",
            "while true; do pkill -f hermes; sleep 1; done",
            "true && ! systemctl --user restart hermes-gateway",
            "{ systemctl --user restart hermes-gateway; }"])
        self.assertVerdict("runtime_removal", ["test -d ~/.hermes && { rm -rf /usr/local/lib/hermes-agent/venv; }"])
        self.assertVerdict("plugin_settings", [
            "[ -n x ] && if true; then hermes config set plugins.entries.hermes-atlas-evolution.settings.runtime_guard false; fi",
            "! hermes plugins disable hermes-atlas-evolution"])

    def test_hermes_under_another_name_is_still_hermes(self):
        self.assertVerdict("gateway_stop", [
            "/usr/local/lib/hermes-agent/venv/bin/python -m hermes_cli.main gateway restart --yes",
            "python3 -X utf8 -m hermes_cli gateway stop",
            "python3 -mhermes_cli.main gateway stop",
            "hermes-agent gateway stop",
            "/opt/venv/bin/python /opt/venv/bin/hermes gateway restart",
            "uv run --python 3.11 hermes gateway stop",
            "uvx hermes gateway stop"])
        self.assertVerdict("plugin_settings", [
            "python3 -m hermes_cli.main config set plugins.entries.hermes-atlas-evolution.settings.owner_platforms '[\"telegram\"]'",
            "python3 -m hermes_cli.main plugins disable hermes-atlas-evolution",
            "hermes plugins install --force github.com/someone/hermes-atlas-evolution",
            "hermes plugins trust hermes-atlas-evolution"])

    def test_killing_the_gateway_by_looking_up_its_pid(self):
        self.assertVerdict("gateway_stop", [
            "kill $(pgrep -f 'hermes_cli.main gateway')",
            "pgrep -f hermes_cli | xargs kill -9",
            "kill -TERM `pidof -x hermes`",
            "kill -9 \"$(cat ~/.hermes/gateway.pid)\"",
            "PID=$(pgrep -f hermes); kill $PID",
            "ps aux | grep hermes | awk '{print $2}' | xargs kill",
            "kill $PPID",
            "kill -HUP ${PPID}",
            "kill -9 -1",
            "kill -- -1",
            "pkill -f python",
            "pkill -f 'gateway run'",
            "killall python3",
            "pkill python"])

    def test_substitutions_run_even_inside_quotes(self):
        self.assertVerdict("gateway_stop", ['echo "$(systemctl --user restart hermes-gateway)"',
                                            "x=`hermes gateway restart`"])

    def test_deleting_what_hermes_runs_on_in_every_spelling(self):
        self.assertVerdict("runtime_removal", [
            "rm -rf /usr/local/lib/hermes-agent/venv/*",
            "rm -rf /usr/local/lib/hermes-agent/venv/lib",
            "rm -rf /usr/local/lib/hermes-agent/hermes_cli/main.py",
            "mv /usr/local/lib/hermes-agent{,.bak}",
            "mv ~/.hermes{,.old}",
            "rm -rf /usr/local/lib/{hermes-agent,x}",
            'rm -rf "${HERMES_HOME:?}"',
            'rm -rf "${HOME:?}/.hermes"',
            'rm -rf "${HERMES_HOME:-/tmp/x}"',
            "mv -t /tmp /usr/local/lib/hermes-agent",
            "mv --target-directory=/tmp ~/.hermes",
            "cd /usr/local/lib && rm -rf hermes-agent",
            "cd ~ && rm -rf .hermes",
            "rm -rf ~owner/.hermes",
            "ls -d /usr/local/lib/hermes-agent | xargs rm -rf",
            "find /usr/local/lib/hermes-agent -print0 | xargs -0 rm -rf"])
        self.assertVerdict("runtime_removal", ["rm -rf venv",
                           "rm -rf ./*",
                           "rm -rf ."], workdir="/usr/local/lib/hermes-agent")

    def test_removing_this_plugin_is_switching_it_off(self):
        self.assertVerdict("plugin_settings", [
            "rm -rf ~/.hermes/plugins/hermes-atlas-evolution",
            "mv ~/.hermes/plugins/hermes-atlas-evolution /tmp",
            "rm ~/.hermes/plugins/hermes-atlas-evolution/plugin.yaml"])

    def test_writing_the_plugin_settings_into_config_yaml(self):
        self.assertVerdict("plugin_settings", [
            "sed -i 's/runtime_guard: true/runtime_guard: false/' ~/.hermes/config.yaml",
            "sed -i '/hermes-atlas-evolution/d' $HERMES_HOME/config.yaml",
            "yq -i '.plugins.enabled = []' ~/.hermes/config.yaml",
            "echo 'plugins: {enabled: []}' > ~/.hermes/config.yaml",
            "python3 -c \"import yaml;p='/home/owner/.hermes/config.yaml';c=yaml.safe_load(open(p));"
            "c['plugins']['enabled']=[];yaml.safe_dump(c,open(p,'w'))\""])

    def test_the_panel_decisions_are_the_owners(self):
        self.assertVerdict("plugin_settings", [
            "curl -s -X POST -H 'X-Atlas-Evolution: 1' http://127.0.0.1:9119/api/plugins/hermes-atlas-evolution/decide",
            "python3 -c \"import urllib.request as u; u.urlopen('http://127.0.0.1:9119/api/plugins/hermes-atlas-evolution/decide')\"",
            "wget --post-data='{}' http://localhost:9119/api/plugins/hermes-atlas-evolution//decide?lang=pt"])
        self.assertVerdict("plugin_settings", [   # 0.8.1 review: the same route, spelled to get past a plain match
            "curl -X POST http://127.0.0.1:9119/api/plugins/x/../hermes-atlas-evolution/decide",
            "curl -X POST http://127.0.0.1:9119/api/plugins/hermes-atlas-evolution%2Fdecide",
            "curl -X POST http://127.0.0.1:9119/api/plugins/hermes-atlas-evolution/d%65cide",
            "curl -X POST http://127.0.0.1:9119/api/plugins/hermes-atlas-evolution/./decide",
            "curl -X POST http://127.0.0.1:9119/api/plugins/hermes-atlas-evolution/'de'cide",
            "P=hermes-atlas; curl -X POST http://127.0.0.1:9119/api/plugins/$P-evolution/decide",
            "curl -X POST http://127.0.0.1:9119/api/plugins/hermes-atlas-evolution%252Fdecide"])
        self.assertVerdict(None, ["curl -s http://127.0.0.1:9119/api/plugins/hermes-atlas-evolution/snapshot",
                                  "curl -X POST http://127.0.0.1:9119/api/plugins/kanban/tasks"])

    def test_systemd_spellings(self):
        self.assertVerdict("gateway_stop", [
            "systemctl --user --job-mode replace restart hermes-gateway",
            "systemctl --user -s KILL kill hermes-gateway",
            "systemctl --user -t service restart hermes-gateway",
            "systemctl -M root@ restart hermes-gateway",
            "systemctl --user restart 'hermes-gateway*'",
            "systemctl --user stop hermes-gateway-*",
            "systemctl --user mask --now hermes-gateway",
            "systemctl --user condstop hermes-gateway",
            "systemctl --user freeze hermes-gateway",
            "U=hermes-gateway; systemctl --user restart $U",
            "echo hermes-gateway | xargs systemctl --user restart",
            "systemd-run --user --on-active 30 systemctl --user restart hermes-gateway",
            "systemd-run --user --service-type oneshot systemctl --user restart hermes-gateway",
            "sudo --user root systemctl restart hermes-gateway",
            "env --unset HOME systemctl --user restart hermes-gateway",
            "eval 'systemctl --user restart hermes-gateway'",
            "su -l root --command='systemctl restart hermes-gateway'",
            "taskset -c 0 systemctl --user restart hermes-gateway",
            "nsenter -t 1 -m systemctl restart hermes-gateway",
            "busctl --user call org.freedesktop.systemd1 /org/freedesktop/systemd1 org.freedesktop.systemd1.Manager "
            "RestartUnit ss hermes-gateway.service replace",
            "find /tmp -maxdepth 0 -exec systemctl --user restart hermes-gateway \\;",
            "ssh localhost systemctl --user restart hermes-gateway",
            "python3 -c \"import subprocess; subprocess.run(['systemctl', '--user', 'restart', 'hermes-gateway'])\"",
            "perl -e 'system(\"systemctl --user restart hermes-gateway\")'"])

    def test_text_sent_to_a_background_shell_is_a_command(self):
        found = guard.check("process_manage", {"action": "submit",
                            "session_id": "p1",
                                               "data": "systemctl --user restart hermes-gateway"},
                            protected=PROTECTED, home=HOME, profile=PROFILE)
        self.assertEqual(found, {"kind": "gateway_stop"})
        self.assertIsNone(guard.check("process_manage", {"action": "poll",
                                      "data": "systemctl --user restart hermes-gateway"},
                                      protected=PROTECTED, home=HOME, profile=PROFILE))

    def test_everyday_commands_still_run(self):
        self.assertVerdict(None, [
            "if [ -f x ]; then echo ok; fi",
            "for f in *.py; do python -m py_compile $f; done",
            "kill 12345",
            "kill -9 4242",
            "kill %1",
            "kill -1 4242",
            "kill $(pgrep -f my_server.py)",
            "pkill -f 'node server.js'",
            "pkill -f 'python3 my_script.py'",
            "killall node",
            "pkill -f my_script.py",
            "rm -rf /tmp/build",
            "cd /tmp && rm -rf hermes-agent",
            "rm -rf ~/.hermes/cache/screenshots",
            "rm -rf /usr/local/lib/hermes-agent-old",
            "mv build{,.old}",
            "mv a.txt b.txt",
            "systemctl --user status hermes-gateway",
            "systemctl --user -n 5 status hermes-gateway",
            "systemctl --user -p MainPID show hermes-gateway",
            "systemctl restart nginx",
            "systemctl restart $SVC",
            "hermes config set display.language pt",
            "hermes plugins update hermes-atlas-evolution",
            "hermes plugins install someone/other-plugin",
            "hermes evolution status",
            "sed -i 's/model: a/model: b/' ~/.hermes/config.yaml",
            "grep plugins ~/.hermes/config.yaml > /tmp/plugins.txt",
            "sed -i 's/plugins:/plugins: []/' ~/site/mkdocs/config.yml",
            "cat ~/.hermes/config.yaml | grep plugins",
            "python3 -c \"print('hello')\"",
            "ssh build-server systemctl restart app",
            "find . -name '*.pyc' -exec rm {} \\;",
            "echo $(date)",
            "x=`whoami`"])
        self.assertVerdict(None, ["rm -rf node_modules",
                                  "rm -rf ./dist"], workdir="/home/owner/project")


if __name__ == "__main__":
    unittest.main()
