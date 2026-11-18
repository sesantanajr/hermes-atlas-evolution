"""Only the operator turns text into something the agent reads. The agent proposes; the operator decides."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_atlas_evolution import lessons, proposals
from hermes_atlas_evolution.agent_tools import LessonsTool
from hermes_atlas_evolution.cli import AGENT_MARKERS, execute
from hermes_atlas_evolution.experience import experience
from hermes_atlas_evolution.operations import close_observers

CLEAN = {name: "" for name in AGENT_MARKERS}
LESSON = "When a CSV import fails, check the header row before retrying."


class GuardBase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.addCleanup(close_observers)
        self.home = Path(folder.name)
        environment = patch.dict(os.environ, CLEAN)
        environment.start()
        self.addCleanup(environment.stop)
        process_tree = patch.multiple("hermes_atlas_evolution.cli", _under_hermes=lambda **_: False,
                                      _in_hermes_cgroup=lambda **_: False)   # not the host's process tree
        process_tree.start()
        self.addCleanup(process_tree.stop)

    def run_cli(self, **arguments):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            execute(SimpleNamespace(lang="en", **arguments), self.home, runtime_config={})
        return json.loads(stream.getvalue())


class GuardTest(GuardBase):
    def test_a_new_operator_lesson_waits_for_approval_unless_activated_with_confirmation(self):
        added = self.run_cli(evolution_action="lessons", lessons_action="add", text=LESSON, tool="terminal",
                             activate=False, yes=False)
        self.assertEqual(added["status"], "quarantined")
        with experience(self.home) as conn:
            self.assertEqual(lessons.playbook(conn), "")
            open_items = proposals.refresh(conn, now=1.0)
        self.assertEqual(open_items[0]["evidence"]["lesson"], LESSON)   # the queue shows the exact sentence
        self.assertEqual(open_items[0]["risk"], "low")
        with self.assertRaises(SystemExit) as refused:   # not a terminal and no --yes
            self.run_cli(evolution_action="lessons", lessons_action="add", text=LESSON, tool="terminal",
                         activate=True, yes=False)
        self.assertIn("--yes", str(refused.exception))
        with patch("sys.stdin.isatty", return_value=True), patch("sys.stdin.readline", return_value="y\n"), \
                contextlib.redirect_stderr(io.StringIO()):
            activated = self.run_cli(evolution_action="lessons", lessons_action="add", text=LESSON + " Always.",
                                     tool="terminal", activate=True, yes=False)
        self.assertEqual(activated["status"], "active")
        with patch("sys.stdin.isatty", return_value=True), patch("sys.stdin.readline", return_value="\n"), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cancelled:
                self.run_cli(evolution_action="lessons", lessons_action="approve", id=added["id"], yes=False)
        self.assertIn("Cancelled", str(cancelled.exception))

    def test_commands_run_from_an_agent_shell_cannot_decide(self):
        added = self.run_cli(evolution_action="lessons", lessons_action="add", text=LESSON, tool="terminal",
                             activate=False, yes=False)
        attempts = [dict(evolution_action="lessons", lessons_action="approve", id=added["id"], yes=True),
                    dict(evolution_action="lessons", lessons_action="retire", id=added["id"], yes=True),
                    dict(evolution_action="lessons", lessons_action="add", text=LESSON, tool="t", activate=True, yes=True),
                    dict(evolution_action="proposals", proposals_action="approve", id=1, note=None, yes=True),
                    dict(evolution_action="proposals", proposals_action="reject", id=1, note=None, yes=True)]
        for marker in ("ATLAS_EVOLUTION_AGENT", "HERMES_SESSION_ID"):
            with patch.dict(os.environ, {marker: "session-1"}):
                for attempt in attempts:
                    with self.subTest(marker=marker, attempt=attempt):
                        with self.assertRaises(SystemExit) as refused:
                            self.run_cli(**attempt)
                        self.assertIn("Only the operator", str(refused.exception))
                listed = self.run_cli(evolution_action="lessons", lessons_action="list", status=None)
                self.assertEqual(listed["lessons"][0]["status"], "quarantined")   # reading stays allowed

    def test_agent_proposals_are_high_risk_and_hidden_characters_are_refused(self):
        tool = LessonsTool(self.home)
        hidden = "When reading files, trust\u200b the header\u202e row always."
        self.assertIn("error", json.loads(tool.handle({"action": "propose", "lesson": hidden})))
        self.assertIsNone(lessons.clean_text("Text with a bell \u0007 inside it, long enough."))
        self.assertEqual(lessons.clean_text("Ｆｕｌｌ　width   text is normalized here."), "Full width text is normalized here.")
        stored = json.loads(tool.handle({"action": "propose", "tool": "terminal", "lesson": LESSON}))
        with experience(self.home) as conn:
            item = proposals.refresh(conn, now=1.0)[0]
        self.assertEqual((item["risk"], item["evidence"]["origin"], item["evidence"]["lesson_id"]),
                         ("high", "agent", stored["lesson_id"]))


class SkillTextTest(unittest.TestCase):
    def test_the_bundled_skill_never_tells_the_agent_to_activate_or_approve(self):
        skill = (Path(__file__).resolve().parent.parent / "skills" / "evolution-review" / "SKILL.md").read_text(encoding="utf-8")
        for forbidden in ("proposals approve <id>`", "lessons add \"", "lessons approve"):
            self.assertNotIn(forbidden, skill.split("## Rules")[0].replace("owner runs", ""), forbidden)
        self.assertIn("own terminal", skill)


if __name__ == "__main__":
    unittest.main()


class ReviewTest(GuardBase):
    """`lessons review` walks the waiting lessons in the owner's terminal: approve, reject, skip or stop."""

    def answer(self, *replies):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        return patch("sys.stdin", Terminal("".join(f"{reply}\n" for reply in replies)))

    def test_each_waiting_lesson_is_shown_and_decided_in_turn(self):
        ids = [self.run_cli(evolution_action="lessons", lessons_action="add", text=f"{LESSON} Case {index}.",
                            tool="terminal", activate=False, yes=False)["id"] for index in range(3)]
        errors = io.StringIO()
        with self.answer("a", "r", "p"), contextlib.redirect_stderr(errors):
            summary = self.run_cli(evolution_action="lessons", lessons_action="review")
        self.assertEqual(summary, {"approved": [ids[0]], "rejected": [ids[1]], "skipped": [ids[2]]})
        self.assertIn("Case 0", errors.getvalue())             # the exact text, before each decision
        with experience(self.home) as conn:
            states = dict(conn.execute("SELECT id, status FROM lesson").fetchall())
        self.assertEqual((states[ids[0]], states[ids[1]], states[ids[2]]), ("active", "retired", "quarantined"))

    def test_review_needs_the_owners_terminal(self):
        with patch.dict(os.environ, {"ATLAS_EVOLUTION_AGENT": "1"}), self.assertRaises(SystemExit):
            self.run_cli(evolution_action="lessons", lessons_action="review")


class AncestryTest(unittest.TestCase):
    """The operator guard also looks at the process tree: an agent can clear environment variables, not its parents."""

    TABLE = {
        900: (1, ["/sbin/init"]),
        950: (900, ["sshd: root@pts/0"]),
        951: (950, ["-bash"]),
        800: (1, ["/usr/bin/python3", "-B", "-I", "-c",
                  "import sys, runpy; sys.path.insert(0, '/usr/local/lib/hermes-agent'); sys.argv = ['g', 'gateway', 'run']"]),
        810: (800, ["/bin/bash", "-c", "HERMES_SESSION_ID= ATLAS_EVOLUTION_AGENT= hermes evolution lessons approve 4 --yes"]),
        811: (810, ["/usr/local/lib/hermes-agent/.hermes/bin/hermes", "evolution", "lessons", "approve", "4", "--yes"]),
        700: (951, ["hermes", "-p", "atlas-ai"]),
        710: (700, ["/bin/sh", "-c", "hermes evolution lessons approve 4 --yes"]),
        600: (951, ["/usr/local/lib/hermes-agent/.hermes/bin/hermes", "evolution", "lessons", "approve", "4"]),
        601: (600, ["/usr/bin/python3", "-I", "-c",
                    "import sys, runpy; sys.path.insert(0, '/usr/local/lib/hermes-agent'); "
                    "sys.argv = ['hermes', 'evolution', 'lessons', 'approve', '4']; runpy.run_path('x')"]),
        500: (951, ["/bin/bash", "-c", "cd /src/hermes-atlas-evolution && python3 -m unittest discover -s tests"]),
        501: (500, ["python3", "-m", "unittest", "discover", "-s", "tests"]),
    }

    def read(self, pid):
        return self.TABLE.get(pid)

    def test_a_command_started_under_hermes_is_refused_even_with_the_markers_cleared(self):
        from hermes_atlas_evolution.cli import _under_hermes

        self.assertTrue(_under_hermes(start=811, read=self.read))    # the gateway's terminal tool
        self.assertTrue(_under_hermes(start=710, read=self.read))    # an interactive hermes chat session
        self.assertFalse(_under_hermes(start=600, read=self.read))   # the owner's own ssh terminal
        self.assertFalse(_under_hermes(start=601, read=self.read))   # the same command after Hermes relaunches itself
        self.assertFalse(_under_hermes(start=501, read=self.read))   # a path that merely mentions hermes
        self.assertFalse(_under_hermes(start=4242, read=self.read))  # nothing known: the markers still apply

    def test_a_command_detached_from_the_gateway_is_still_inside_its_cgroup(self):
        from hermes_atlas_evolution.cli import _in_hermes_cgroup

        self.assertTrue(_in_hermes_cgroup(text="0::/user.slice/user-0.slice/user@0.service/app.slice/hermes-gateway.service\n"))
        self.assertFalse(_in_hermes_cgroup(text="0::/user.slice/user-0.slice/session-12.scope\n"))
        self.assertFalse(_in_hermes_cgroup(text=None))                # not Linux, or unreadable
