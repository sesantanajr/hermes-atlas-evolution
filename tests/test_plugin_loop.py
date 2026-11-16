"""End to end: register the plugin against a stand-in for Hermes and drive the evolution loop through its hooks."""

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_evolution import lessons
from hermes_evolution.autopilot import Autopilot
from hermes_evolution.experience import experience
from hermes_evolution.hints import KEY
from hermes_evolution.integration import register
from hermes_evolution.operations import close_observers
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
        self.hooks, self.tools, self.sections, self.commands, self.cli, self.skills = {}, {}, {}, {}, {}, {}

    def register_skill(self, name, path, description="", frontmatter=None):
        self.skills[name] = Path(path)

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


class PluginBase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addCleanup(close_observers)   # Windows cannot remove a database that is still open.
        self.home = Path(folder.name)
        constants = types.ModuleType("hermes_constants")
        constants.get_hermes_home = lambda: self.home
        constants.get_default_hermes_root = lambda home=None: self.home
        package = types.ModuleType("hermes_cli")
        package.__version__ = "0.23.1"
        modules = patch.dict(sys.modules, {"hermes_constants": constants, "hermes_cli": package})
        modules.start()
        self.addCleanup(modules.stop)
        if getattr(self, "real_autopilot", False) is not True:   # AutopilotLoopTest drives the real one
            quiet = patch.object(Autopilot, "poke", lambda _self: False)
            quiet.start()
            self.addCleanup(quiet.stop)

    def settings(self, **values):
        (self.home / "evolution").mkdir(exist_ok=True)
        (self.home / "evolution" / "settings.json").write_text(json.dumps(values))

    def plugin(self) -> FakeContext:
        context = FakeContext()
        register(context)
        return context


class PluginLoopTest(PluginBase):
    def test_registrations_match_the_manifest(self):
        context = self.plugin()
        declared = read_manifest(Path(__file__).resolve().parent.parent / "plugin.yaml")
        self.assertEqual(sorted(context.hooks), sorted(declared["provides_hooks"]))
        self.assertEqual(sorted(context.tools), sorted(declared["provides_tools"]))
        self.assertEqual(list(context.sections), ["atlas-evolution.playbook"])
        self.assertLessEqual(context.sections["atlas-evolution.playbook"]["max_chars"], 4000)
        self.assertIn("evolution", context.cli)
        self.assertEqual(list(context.skills), ["evolution-review"])
        skill = context.skills["evolution-review"].read_text(encoding="utf-8")
        self.assertTrue(skill.startswith("---\nname: evolution-review\ndescription: "))
        self.assertLess(len(skill), 15_000)
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

    def test_a_new_session_gets_the_playbook_while_another_process_holds_the_write_lock(self):
        import sqlite3

        self.settings(evolution={"lessons": "auto", "language": "en"})
        with experience(self.home) as conn:
            lessons.add_custom(conn, "Validate PowerShell with PSScriptAnalyzer before delivering it.", tool="terminal",
                               origin="operator", now=1.0, active=True)
        context = self.plugin()
        writer = sqlite3.connect(self.home / "evolution" / "evolution.sqlite3", timeout=0)
        writer.execute("BEGIN IMMEDIATE")   # e.g. `hermes evolution learn` running in another process
        try:
            text = context.sections["atlas-evolution.playbook"]["content"]({"session_id": "busy"})
        finally:
            writer.execute("ROLLBACK")
            writer.close()
        self.assertIn("PSScriptAnalyzer", text)
        close_observers()   # the queued "shown" counter is written once the database is free
        with experience(self.home) as conn:
            self.assertEqual(lessons.listing(conn)[0]["shown_in_sessions"], 1)

    def test_lessons_off_registers_no_injection_surface(self):
        self.settings(evolution={"lessons": "off"})
        context = self.plugin()
        self.assertNotIn("transform_tool_result", context.hooks)
        self.assertEqual(context.sections, {})
        self.assertIn("post_tool_call", context.hooks)   # measurement continues

    def test_the_runtime_guard_vetoes_a_gateway_restart_and_counts_it(self):
        self.settings(evolution={"language": "en"})
        context = self.plugin()
        directive = context.fire("pre_tool_call", tool_name="terminal",
                                 args={"command": "systemctl --user restart hermes-gateway"})
        self.assertEqual(directive["action"], "block")
        self.assertIn("hermes gateway restart", directive["message"])
        self.assertIsNone(context.fire("pre_tool_call", tool_name="terminal", args={"command": "ls -la"}))
        close_observers()
        with experience(self.home) as conn:
            self.assertEqual(conn.execute("SELECT kind, count FROM guard_daily").fetchall()[0][:], ("gateway_stop", 1))
        self.settings(evolution={"runtime_guard": False})
        self.assertNotIn("pre_tool_call", self.plugin().hooks)

    def test_error_excerpts_are_kept_only_when_the_reflector_could_use_them(self):
        crash = json.dumps({"output": "Traceback (most recent call last):\nKeyError: 'column'", "exit_code": 1})
        for lessons_mode, expected in (("off", 0), ("suggest", 1)):
            with self.subTest(lessons=lessons_mode):
                self.settings(evolution={"lessons": lessons_mode, "reflector": True})
                context = self.plugin()
                context.fire("post_tool_call", tool_name="terminal", status="error", result=crash,
                             session_id="s", turn_id="t", tool_call_id=f"c-{lessons_mode}")
                close_observers()
                with experience(self.home) as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM failure_sample").fetchone()[0], expected)

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
        close_observers()   # the next write has to open the database again
        broken = __import__("sqlite3").OperationalError("unable to open database file")
        with patch("hermes_evolution.operations._open", side_effect=broken), self.assertLogs(
                "hermes_evolution.integration", level="WARNING") as logged:
            for _ in range(3):   # a storage failure is reported once per event kind, never raised into the turn
                context.fire("post_tool_call", tool_name="terminal", status="ok", turn_id="t", tool_call_id="c")
                context.fire("on_skill_lifecycle", action="loaded", skill_name="deploy")
            self.assertIsNone(context.fire("transform_tool_result", tool_name="terminal", status="error",
                                           result="{}", session_id="s", tool_call_id="x"))
            self.assertEqual(context.sections["atlas-evolution.playbook"]["content"]({}), "")
        self.assertEqual(len(logged.records), 3)   # tool, tool_outcome and skill
        tool = context.tools["atlas_evolution_lessons"]
        self.assertTrue(tool["check"]())
        self.assertEqual(json.loads(tool["handler"]({"action": "search"})), {"lessons": []})

    def test_invalid_settings_never_switch_on_what_the_operator_switched_off(self):
        self.settings(evolution={"lessons": "everything", "slow_turn_seconds": "fast"})
        with self.assertLogs("hermes_evolution.integration", level="WARNING"):
            context = self.plugin()
        self.assertEqual(context.sections, {})                    # an invalid mode means off
        self.assertNotIn("transform_tool_result", context.hooks)
        self.settings(evolution={"lessons": "off", "slow_turn_seconds": 120.5})   # another key is wrong
        self.assertEqual(self.plugin().sections, {})
        self.settings(evolution={"lessons": "auto", "slow_turn_seconds": 120.0})  # a form may store whole floats
        self.assertIn("atlas-evolution.playbook", self.plugin().sections)
        (self.home / "evolution" / "settings.json").write_text("{not json")
        self.assertEqual(self.plugin().sections, {})              # unreadable file: measure only


if __name__ == "__main__":
    unittest.main()


class AutopilotLoopTest(PluginBase):
    """The loop runs by itself after a turn, in the background, and records what it did."""
    real_autopilot = True

    def test_a_finished_turn_starts_one_background_pass(self):
        self.settings(evolution={"lessons": "suggest", "reflector": False})
        context = self.plugin()
        for index in range(3):
            context.fire("post_tool_call", tool_name="tool_call", status="error", result=ERROR, args=ARGS,
                         session_id=f"s{index}", turn_id=f"t{index}", tool_call_id=f"c{index}")
        context.fire("pre_llm_call", session_id="s0", turn_id="turn", platform="cli", user_message="ok")
        context.fire("post_llm_call", session_id="s0", turn_id="turn")
        threads = [thread for thread in __import__("threading").enumerate() if thread.name == "atlas-evolution-autopilot"]
        for thread in threads:
            thread.join(10)
        close_observers()
        with experience(self.home) as conn:
            status = json.loads(conn.execute("SELECT value FROM cursor WHERE key='loop_status'").fetchone()[0])
            self.assertTrue(status["ok"])
            self.assertEqual(status["summary"]["new_lessons"], 1)    # the recurring failure became a lesson
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM lesson").fetchone()[0], 1)

    def test_autopilot_zero_leaves_the_loop_to_the_operator(self):
        self.settings(evolution={"autopilot_minutes": 0})
        context = self.plugin()
        context.fire("post_llm_call", session_id="s0", turn_id="turn")
        self.assertEqual([thread for thread in __import__("threading").enumerate()
                          if thread.name == "atlas-evolution-autopilot"], [])


class NativeSettingsTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addCleanup(close_observers)
        self.home = Path(folder.name)

    def test_values_from_the_hermes_settings_form_override_the_settings_file(self):
        from hermes_evolution.cli import effective_settings

        (self.home / "evolution").mkdir()
        (self.home / "evolution" / "settings.json").write_text(json.dumps(
            {"evolution": {"lessons": "off", "language": "en", "slow_turn_seconds": 30}, "agent_advice": False,
             "rates_usd_per_million": {}}))
        runtime = {"plugins": {"entries": {"hermes-evolution": {"settings": {
            "lessons": "auto", "agent_tools": True, "language": None, "owner_sender_ids": ["u1"]}}}}}
        merged = effective_settings(self.home, runtime)
        self.assertEqual(merged["evolution"]["lessons"], "auto")
        self.assertEqual(merged["evolution"]["language"], "en")          # unset in Hermes: the file value stays
        self.assertEqual(merged["evolution"]["slow_turn_seconds"], 30)
        self.assertEqual(merged["evolution"]["owner_sender_ids"], ["u1"])
        self.assertTrue(merged["agent_advice"])
        self.assertEqual(effective_settings(self.home, None)["evolution"]["lessons"], "off")
        self.assertEqual(effective_settings(self.home, {"plugins": "garbage"})["evolution"]["lessons"], "off")

    def test_manifest_schema_matches_the_keys_the_code_reads(self):
        from hermes_evolution.cli import NATIVE_KEYS

        manifest = (Path(__file__).resolve().parent.parent / "plugin.yaml").read_text(encoding="utf-8")
        schema = manifest.split("config_schema:", 1)[1]
        declared = {line.strip()[:-1] for line in schema.splitlines() if line.startswith("  ") and not line.startswith("   ")
                    and line.strip().endswith(":")}
        self.assertEqual(declared, set(NATIVE_KEYS))
