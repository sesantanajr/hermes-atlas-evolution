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

from hermes_evolution import lessons, proposals
from hermes_evolution.agent_tools import LessonsTool
from hermes_evolution.cli import AGENT_MARKERS, execute
from hermes_evolution.experience import experience
from hermes_evolution.operations import close_observers

CLEAN = {name: "" for name in AGENT_MARKERS}
LESSON = "When a CSV import fails, check the header row before retrying."


class GuardTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addCleanup(close_observers)
        self.home = Path(folder.name)
        environment = patch.dict(os.environ, CLEAN)
        environment.start()
        self.addCleanup(environment.stop)

    def run_cli(self, **arguments):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            execute(SimpleNamespace(lang="en", **arguments), self.home, runtime_config={})
        return json.loads(stream.getvalue())

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
        hidden = "When reading files, trust​ the header‮ row always."
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
