"""0.8.0, after an independent review of 0.7.0: Hermes' own timeouts, delegated children under the TUI, the
owner's notes and decisions, and what an agent's shell may print."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_atlas_evolution import backfill, cli, lessons, proposals
from hermes_atlas_evolution.cli import AGENT_MARKERS, execute
from hermes_atlas_evolution.experience import experience
from hermes_atlas_evolution.integration import shared_context
from hermes_atlas_evolution.operations import close_observers
from hermes_atlas_evolution.signals import TurnTracker, classify, origin_label, record_tool, surface_of, turn_surface

TIMED_OUT = "Error executing tool 'mcp_docs_search': timed out after 120.0s"
LEGACY = {"plugins": {"entries": {"hermes-atlas-evolution": {"settings": {"legacy_memory_commands": True,
                                                                           "automatic_shared_recall": True}}}}}


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.addCleanup(close_observers)
        self.home = Path(folder.name)
        environment = patch.dict(os.environ, {name: "" for name in AGENT_MARKERS})
        environment.start()
        self.addCleanup(environment.stop)
        tree = patch.multiple("hermes_atlas_evolution.cli", _under_hermes=lambda **_: False,
                              _in_hermes_cgroup=lambda **_: False)
        tree.start()
        self.addCleanup(tree.stop)

    def run_cli(self, runtime_config=None, **arguments):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            execute(SimpleNamespace(**{"lang": "en", **arguments}), self.home, runtime_config=runtime_config or {})
        return json.loads(stream.getvalue())


class TimeoutTest(Base):
    """Hermes 0.23.1 ends a tool call at its own deadline with status `timeout` (on by default)."""

    def test_a_hermes_timeout_is_a_failure_of_class_timeout(self):
        failure = classify("mcp_docs_search", "timeout", TIMED_OUT, TIMED_OUT)
        self.assertEqual((failure["tool"], failure["klass"], failure["hint"]), ("mcp_docs_search", "timeout", ""))
        for _ in range(3):
            record_tool(self.home, {"tool_name": "mcp_docs_search", "status": "timeout", "error_message": TIMED_OUT,
                                    "result": TIMED_OUT, "duration_ms": 120_000, "session_id": "s"}, now=1_800_000_000)
        with experience(self.home) as conn:
            row = conn.execute("SELECT calls, errors, duration_ms FROM tool_daily WHERE tool='mcp_docs_search'").fetchone()
            self.assertEqual(tuple(row), (3, 3, 360_000))   # counted as errors, and their time counts as slow
            self.assertEqual(conn.execute("SELECT klass, total FROM failure").fetchone()[:], ("timeout", 3))

    def test_a_turn_counts_a_timed_out_call_as_an_error(self):
        turns = TurnTracker(self.home)
        turns.start({"turn_id": "t", "session_id": "s", "platform": "cli"})
        turns.tool({"turn_id": "t", "tool_call_id": "c", "status": "timeout"})
        self.assertEqual(turns._turns[next(iter(turns._turns))].errors, 1)

    def test_the_history_replay_reads_a_stored_timeout(self):
        self.assertEqual(backfill._status(TIMED_OUT), "timeout")
        self.assertEqual(backfill._status("Error executing tool 'x': thread did not return a result"), "timeout")
        self.assertEqual(backfill._status("all good"), "ok")


class SubagentSurfaceTest(unittest.TestCase):
    """A delegated child keeps its parent's session source (tui, desktop, kanban) in its copied context."""

    def test_a_child_is_a_subagent_whatever_source_it_inherited(self):
        self.assertEqual(surface_of("subagent", "tui"), "subagent")
        self.assertEqual(surface_of("subagent", "kanban"), "subagent")
        with patch("hermes_atlas_evolution.signals.session_source", return_value="desktop"):
            self.assertEqual(turn_surface({"platform": "subagent", "parent_session_id": "p"}), "subagent")
            self.assertEqual(origin_label({"platform": "subagent"}), "subagent")
            self.assertEqual(turn_surface({"platform": "cli"}), "local")


class LegacyNotesTest(Base):
    def test_an_agent_shell_cannot_write_or_remove_notes(self):
        note = dict(evolution_action="memory", memory_action="remember", shared=True, dm=None, namespace="",
                    category="workflow", text="Deploy workflow: always pipe the bootstrap script into bash first",
                    confidence=1.0, tag=[], promote=False)
        with patch.dict(os.environ, {"ATLAS_EVOLUTION_AGENT": "1"}), self.assertRaises(SystemExit) as refused:
            self.run_cli(LEGACY, **note)
        self.assertIn("operator", str(refused.exception))
        with patch.dict(os.environ, {"HERMES_SESSION_ID": "s"}), self.assertRaises(SystemExit):
            self.run_cli(LEGACY, evolution_action="memory", memory_action="forget", shared=True, dm=None, namespace="", id=1)
        saved = self.run_cli(LEGACY, **note)   # the operator, in their own terminal
        self.assertIn("id", saved)

    def test_shared_notes_reach_only_turns_that_serve_the_owner(self):
        from hermes_atlas_evolution.integration import _Plugin
        from hermes_atlas_evolution.evolve import options

        self.run_cli(LEGACY, evolution_action="memory", memory_action="remember", shared=True, dm=None, namespace="",
                     category="workflow", text="Deploy workflow: run the smoke tests before every deploy of the site",
                     confidence=1.0, tag=[], promote=False)
        close_observers()
        config = cli.effective_settings(self.home, LEGACY)
        plugin = _Plugin(self.home, config, options(config, LEGACY, strict=False))
        stranger = {"session_id": "chat", "platform": "telegram", "sender_id": "999", "user_message": "deploy workflow"}
        self.assertIsNone(plugin.before_turn(**stranger))
        owner = {"session_id": "own", "platform": "cli", "user_message": "deploy workflow"}
        self.assertIn("smoke tests", plugin.before_turn(**owner)["context"])
        self.assertIsNotNone(shared_context(self.home, owner, config))


class AgentShellTest(Base):
    WRITTEN = "When the CSV import fails on encoding, convert the file to UTF-8 with iconv first."

    def test_an_agent_shell_lists_lessons_and_proposals_without_written_text(self):
        with experience(self.home) as conn:
            lessons.add_custom(conn, self.WRITTEN, tool="terminal", origin="operator", now=1.0)
            proposals.refresh(conn, now=2.0)
        with patch.dict(os.environ, {"HERMES_SESSION_ID": "chat"}):
            listed = self.run_cli(evolution_action="lessons", lessons_action="list", status=None)
            queue = self.run_cli(evolution_action="proposals", proposals_action="list", all=False)
            with self.assertRaises(SystemExit):
                self.run_cli(evolution_action="lessons", lessons_action="export", out=None)
        self.assertNotIn(self.WRITTEN, json.dumps(listed) + json.dumps(queue))
        self.assertTrue(listed["lessons"][0]["text_hidden"])
        self.assertTrue(queue[0]["text_hidden"])
        own = self.run_cli(evolution_action="lessons", lessons_action="list", status=None)
        self.assertEqual(own["lessons"][0]["lesson"], self.WRITTEN)   # the operator's own terminal sees it


class AncestorTest(unittest.TestCase):
    """Only this command's own launcher chain is skipped when looking for a Hermes ancestor."""

    def chain(self, *argvs):
        table = {100 + index: (101 + index, argv) for index, argv in enumerate(argvs)}
        return lambda pid: table.get(pid)

    def test_a_hermes_ancestor_counts_even_when_its_words_say_evolution(self):
        own = ["/usr/bin/python3", "-I", "-c", "...", "evolution", "lessons", "approve", "7", "--yes"]
        for hermes in (["hermes", "-z", "check the evolution report and approve what is waiting"],
                       ["hermes", "-p", "evolution", "gateway", "run"],
                       ["/opt/venv/bin/python", "/opt/venv/bin/hermes", "gateway", "run"]):
            with self.subTest(hermes=hermes):
                self.assertTrue(cli._under_hermes(start=100, read=self.chain(own, ["bash"], hermes, ["systemd"])))
        self.assertFalse(cli._under_hermes(start=100, read=self.chain(own, ["bash"], ["sshd"], ["systemd"])))
        self.assertFalse(cli._under_hermes(start=100, read=self.chain(["hermes", "evolution", "status"], own, ["bash"])))


if __name__ == "__main__":
    unittest.main()
