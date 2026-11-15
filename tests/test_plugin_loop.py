"""End to end: register the plugin against a stand-in for Hermes and drive the evolution loop through its hooks."""

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_evolution import lessons
from hermes_evolution.experience import experience
from hermes_evolution.hints import KEY
from hermes_evolution.integration import register
from hermes_evolution.signals import turn_summary

ERROR = json.dumps({"error": "tool_call to 'files_search' is missing required argument(s): servico."})
ARGS = {"calls": [{"name": "files_search", "arguments": {}}]}


def read_manifest(path: Path) -> dict:
    """Read the list fields of plugin.yaml with the standard library only."""
    result, current = {}, None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("  - ") and current:
            result[current].append(line[4:].strip())
        elif line and not line.startswith((" ", "#")):
            key, _, value = line.partition(":")
            current = key if not value.strip() else None
            if current:
                result[current] = []
    return result


class FakeContext:
    def __init__(self):
        self.hooks, self.tools, self.sections, self.commands, self.cli = {}, {}, {}, {}, {}

    def register_hook(self, name, callback, **_options):
        self.hooks.setdefault(name, []).append(callback)

    def register_tool(self, *, name, toolset, schema, handler, check_fn=None, **_options):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler, "check": check_fn}

    def register_system_prompt_section(self, section_id, content, *, position="after_memory", max_chars=4000):
        self.sections[section_id] = {"content": content, "max_chars": max_chars}

    def register_command(self, name, handler, **_options):
        self.commands[name] = handler

    def register_cli_command(self, name, help_text, setup, handler=None, **_options):
        self.cli[name] = (setup, handler)

    def fire(self, hook, **payload):
        results = [callback(**payload, telemetry_schema_version="hermes.observer.v1") for callback in self.hooks.get(hook, [])]
        return next((item for item in results if item is not None), None)


class PluginLoopTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        constants = types.ModuleType("hermes_constants")
        constants.get_hermes_home = lambda: self.home
        constants.get_default_hermes_root = lambda home=None: self.home
        package = types.ModuleType("hermes_cli")
        package.__version__ = "0.23.1"
        modules = patch.dict(sys.modules, {"hermes_constants": constants, "hermes_cli": package})
        modules.start()
        self.addCleanup(modules.stop)

    def settings(self, **values):
        (self.home / "evolution").mkdir(exist_ok=True)
        (self.home / "evolution" / "settings.json").write_text(json.dumps(values))

    def plugin(self) -> FakeContext:
        context = FakeContext()
        register(context)
        return context

    def test_registrations_match_the_manifest(self):
        context = self.plugin()
        declared = read_manifest(Path(__file__).resolve().parent.parent / "plugin.yaml")
        self.assertEqual(sorted(context.hooks), sorted(declared["provides_hooks"]))
        self.assertEqual(sorted(context.tools), sorted(declared["provides_tools"]))
        self.assertEqual(list(context.sections), ["atlas-evolution.playbook"])
        self.assertLessEqual(context.sections["atlas-evolution.playbook"]["max_chars"], 4000)
        self.assertIn("evolution", context.cli)
        self.assertEqual(context.commands, {})   # the legacy chat command stays opt-in
        for name, tool in context.tools.items():
            self.assertFalse(tool["check"](), name)   # agent-facing tools are off until the operator allows them
            self.assertIn("error", json.loads(tool["handler"]({"action": "search", "kind": "research"})))

    def test_a_recurring_failure_becomes_a_lesson_the_agent_sees_when_it_fails_again(self):
        self.settings(evolution={"lessons": "auto", "language": "en"})
        context = self.plugin()
        section = context.sections["atlas-evolution.playbook"]["content"]
        self.assertEqual(section({"session_id": "s0"}), "")   # nothing learned yet: nothing injected

        for session in ("s1", "s2"):
            for call in range(2):
                payload = {"tool_name": "tool_call", "args": ARGS, "result": ERROR, "status": "error",
                           "session_id": session, "turn_id": f"turn-{session}-{call}", "tool_call_id": f"{session}-{call}",
                           "duration_ms": 20, "error_message": "missing required argument"}
                context.fire("pre_llm_call", session_id=session, turn_id=f"turn-{session}-{call}", platform="cli",
                             user_message="find the contract", is_first_turn=True)
                context.fire("post_tool_call", **payload)
                self.assertIsNone(context.fire("transform_tool_result", **payload))   # no lesson exists yet
                context.fire("on_session_end", session_id=session, turn_id=f"turn-{session}-{call}", completed=True,
                             failed=False, interrupted=False)

        with experience(self.home) as conn:
            created = lessons.derive(conn, now=__import__("time").time(), mode="auto")
            self.assertEqual(len(created), 1)
            summary = turn_summary(conn, now=__import__("time").time(), days=1)
            self.assertEqual((summary["turns"], summary["completed"], summary["tool_errors"]), (4, 4, 4))

        fresh = self.plugin()   # a new gateway process picks the lesson up
        text = fresh.sections["atlas-evolution.playbook"]["content"]({"session_id": "s3"})
        self.assertIn("When calling `files_search`, always pass the required argument `servico`.", text)
        payload = {"tool_name": "tool_call", "args": ARGS, "result": ERROR, "status": "error", "session_id": "s3",
                   "turn_id": "turn-s3", "tool_call_id": "s3-0"}
        fresh.fire("post_tool_call", **payload)
        hinted = fresh.fire("transform_tool_result", **payload)
        self.assertEqual(json.loads(hinted)[KEY], "When calling `files_search`, always pass the required argument `servico`.")
        self.assertEqual(json.loads(hinted)["error"], json.loads(ERROR)["error"])
        self.assertIsNone(fresh.fire("transform_tool_result", **{**payload, "tool_call_id": "s3-1"}))   # once per session
        self.assertIsNone(fresh.fire("transform_tool_result", **{**payload, "status": "ok"}))
        with experience(self.home) as conn:
            item = lessons.listing(conn)[0]
            self.assertEqual((item["shown_in_sessions"], item["hints_given"]), (1, 1))

    def test_lessons_off_registers_no_injection_surface(self):
        self.settings(evolution={"lessons": "off"})
        context = self.plugin()
        self.assertNotIn("transform_tool_result", context.hooks)
        self.assertEqual(context.sections, {})
        self.assertIn("post_tool_call", context.hooks)   # measurement continues

    def test_hooks_fail_open_when_storage_is_unusable_and_tolerate_odd_payloads(self):
        self.settings(evolution={"lessons": "auto"}, agent_advice=True)
        context = self.plugin()
        for payload in ({}, {"tool_name": None, "status": "error", "result": 7, "args": "x", "tool_call_id": 5},
                        {"tool_name": "t" * 500, "status": "blocked", "result": "{" * 10, "session_id": ["x"]}):
            context.fire("post_tool_call", **payload)
            self.assertIsNone(context.fire("transform_tool_result", **payload))
        context.fire("pre_llm_call", user_message=None)
        context.fire("on_session_end")
        context.fire("on_skill_lifecycle", action="loaded", skill_name=None)
        with patch("hermes_evolution.experience.sqlite3.connect", side_effect=__import__("sqlite3").OperationalError("locked")):
            context.fire("post_tool_call", tool_name="terminal", status="ok", turn_id="t", tool_call_id="c")
            context.fire("on_skill_lifecycle", action="loaded", skill_name="deploy")
            self.assertEqual(context.sections["atlas-evolution.playbook"]["content"]({}), "")
        tool = context.tools["atlas_evolution_lessons"]
        self.assertTrue(tool["check"]())
        self.assertEqual(json.loads(tool["handler"]({"action": "search"})), {"lessons": []})

    def test_invalid_settings_fall_back_to_safe_defaults(self):
        self.settings(evolution={"lessons": "everything", "slow_turn_seconds": "fast"})
        context = self.plugin()
        self.assertIn("atlas-evolution.playbook", context.sections)   # 'suggest': shown only after approval


if __name__ == "__main__":
    unittest.main()
