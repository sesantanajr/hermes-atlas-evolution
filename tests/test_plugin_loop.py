"""End to end: register the plugin against a stand-in for Hermes and drive the evolution loop through its hooks."""

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_atlas_evolution import lessons
from hermes_atlas_evolution.autopilot import Autopilot
from hermes_atlas_evolution.experience import experience
from hermes_atlas_evolution.hints import KEY
from hermes_atlas_evolution.integration import register
from hermes_atlas_evolution.operations import close_observers
from hermes_atlas_evolution.signals import turn_summary

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
        self.assertEqual(list(context.sections), ["hermes-atlas-evolution.playbook"])
        self.assertLessEqual(context.sections["hermes-atlas-evolution.playbook"]["max_chars"], 4000)
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
        self.settings(evolution={"lessons": "auto", "language": "en", "tool_hints": True})
        context = self.plugin()
        section = context.sections["hermes-atlas-evolution.playbook"]["content"]
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
        text = fresh.sections["hermes-atlas-evolution.playbook"]["content"]({"session_id": "s3"})
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
            text = context.sections["hermes-atlas-evolution.playbook"]["content"]({"session_id": "busy", "platform": "tui"})
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
        quiet = self.plugin()   # still remembers what the agent wrote, but vetoes nothing
        self.assertIsNone(quiet.fire("pre_tool_call", tool_name="terminal", args={"command": "systemctl --user restart hermes-gateway"}))

    def test_pending_decisions_are_announced_once_a_day_and_only_to_the_owner(self):
        self.settings(evolution={"language": "en"})
        context = self.plugin()
        self.assertIsNone(context.fire("on_session_finalize", session_id="s", platform="cli", reason="exit"))  # nothing pending
        with experience(self.home) as conn:
            lessons.add_custom(conn, "When the build fails, read the first error line first.", tool="terminal",
                               origin="operator", now=1.0)
        self.assertIsNone(context.fire("on_session_finalize", session_id="s", platform="whatsapp", reason="exit"))
        notice = context.fire("on_session_finalize", session_id="s", platform="cli", reason="exit")
        self.assertIn("hermes evolution status", notice["message"])
        self.assertIn("1 lesson", notice["message"])
        close_observers()
        self.assertIsNone(context.fire("on_session_finalize", session_id="s2", platform="tui", reason="exit"))  # once a day

    def test_the_kanban_tick_only_nudges_the_autopilot(self):
        context = self.plugin()
        with patch.object(Autopilot, "poke", return_value=True) as poke:
            self.assertIsNone(context.fire("on_kanban_dispatch_tick", board="default", outcome="idle"))
        poke.assert_called_once()

    def test_the_agent_proposal_cap_holds_across_calls_of_the_registered_tool(self):
        self.settings(agent_advice=True)
        context = self.plugin()
        context.fire("pre_llm_call", session_id="own", turn_id="t", platform="cli", user_message="go")
        handler = context.tools["hermes_atlas_evolution_lessons"]["handler"]
        replies = [json.loads(handler({"action": "propose", "tool": "terminal",
                                       "lesson": f"When the build number {index} fails, read the first error line first."},
                                      session_id="own"))
                   for index in range(7)]
        accepted = [reply for reply in replies if "error" not in reply]
        self.assertEqual(len(accepted), 5)   # PROPOSALS_PER_HOUR, kept by the one tool instance of this load

    def test_failures_that_are_not_the_agents_doing_never_become_lessons(self):
        self.settings(evolution={"lessons": "auto"})
        context = self.plugin()
        stuck = json.dumps({"error": "pre_tool_call plugin callback timed out or is still running"})
        for index in range(6):   # another plugin's guard hung: Hermes blocked the tool, fail-closed
            context.fire("post_tool_call", tool_name="read_file", status="blocked", result=stuck, error_type="plugin_block",
                         error_message="pre_tool_call plugin callback timed out or is still running",
                         session_id=f"s{index % 3}", turn_id=f"t{index}", tool_call_id=f"stuck-{index}")
        for index in range(6):   # another plugin rewrote the command into a binary that is not installed
            call = f"rw-{index}"
            context.fire("pre_tool_call", tool_name="terminal", args={"command": "git status"}, tool_call_id=call)
            context.fire("post_tool_call", tool_name="terminal", status="error", args={"command": "rtk git status"},
                         result=json.dumps({"output": "bash: rtk: command not found", "exit_code": 127}),
                         session_id=f"s{index % 3}", turn_id=f"r{index}", tool_call_id=call)
        close_observers()
        with experience(self.home) as conn:
            classes = dict(conn.execute("SELECT klass, SUM(total) FROM failure GROUP BY klass").fetchall())
        self.assertEqual(classes, {"plugin_fault": 6, "rewritten_command": 6})
        from hermes_atlas_evolution.evolve import cycle

        learned = cycle(self.home, now=__import__("time").time(), config={"evolution": {"lessons": "auto", "reflector": False}})
        self.assertEqual(learned["new_lessons"], [])

    def test_hints_on_failing_results_are_off_unless_asked_for(self):
        with experience(self.home) as conn:
            lessons.add_custom(conn, "Always pass servico to files_search.", tool="files_search", origin="operator",
                               now=1.0, active=True)
            conn.execute("UPDATE lesson SET signature=?", (__import__("hermes_atlas_evolution.signals", fromlist=["x"])
                                                          .classify("tool_call", "error", None, ERROR, ARGS)["signature"],))
        failing = dict(tool_name="tool_call", status="error", result=ERROR, args=ARGS, session_id="s1", tool_call_id="c1")
        self.settings(evolution={"lessons": "auto"})
        context = self.plugin()
        context.fire("pre_llm_call", session_id="s1", turn_id="t1", platform="cli", user_message="go")
        # Registered (the manifest declares it) but silent: only the first string of all plugins counts, so a
        # hint would discard another plugin's transform of the same result.
        self.assertIsNone(context.fire("transform_tool_result", **failing))
        self.settings(evolution={"lessons": "auto", "tool_hints": True})
        context = self.plugin()
        context.fire("pre_llm_call", session_id="s1", turn_id="t1", platform="cli", user_message="go")
        context.fire("pre_llm_call", session_id="g1", turn_id="t2", platform="whatsapp", sender_id="stranger",
                     user_message="go")
        self.assertIn("files_search", context.fire("transform_tool_result", **failing))
        # an operator's free text never goes to a chat where someone else may read it
        self.assertIsNone(context.fire("transform_tool_result", **{**failing, "session_id": "g1", "tool_call_id": "c2"}))

    def test_a_lesson_counts_as_shown_once_per_session(self):
        self.settings(evolution={"lessons": "auto"})
        context = self.plugin()
        with experience(self.home) as conn:
            lessons.add_custom(conn, "When the build fails, read the first error line first.", tool="terminal",
                               origin="operator", now=1.0, active=True)
        render = context.sections["hermes-atlas-evolution.playbook"]["content"]
        for session in ("a", "a", "a", "b"):   # re-renders of one session count once
            self.assertIn("build fails", render({"session_id": session, "platform": "cli"}))
        close_observers()
        with experience(self.home) as conn:
            self.assertEqual(lessons.listing(conn)[0]["shown_in_sessions"], 2)

    def test_the_agent_proposal_cap_is_per_profile_not_per_process(self):
        from hermes_atlas_evolution.agent_tools import LessonsTool

        first, second = LessonsTool(self.home), LessonsTool(self.home)   # two processes of the same profile
        accepted = 0
        for index in range(8):
            tool = first if index % 2 else second
            reply = json.loads(tool.handle({"action": "propose", "tool": "terminal",
                                            "lesson": f"When build {index} fails, read the first error line first."}))
            accepted += "error" not in reply
        self.assertEqual(accepted, 5)

    def test_error_excerpts_are_kept_only_when_the_reflector_could_use_them(self):
        crash = json.dumps({"output": "Traceback (most recent call last):\nKeyError: 'column'", "exit_code": 1})
        for lessons_mode, expected in (("off", 0), ("suggest", 1)):
            with self.subTest(lessons=lessons_mode):
                self.settings(evolution={"lessons": lessons_mode, "reflector": True})
                context = self.plugin()
                context.fire("pre_llm_call", session_id="s", turn_id="t", platform="cli", user_message="go")
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
        with patch("hermes_atlas_evolution.operations._open", side_effect=broken), self.assertLogs(
                "hermes_atlas_evolution.integration", level="WARNING") as logged:
            for _ in range(3):   # a storage failure is reported once per event kind, never raised into the turn
                context.fire("post_tool_call", tool_name="terminal", status="ok", turn_id="t", tool_call_id="c")
                context.fire("on_skill_lifecycle", action="loaded", skill_name="deploy")
            self.assertIsNone(context.fire("transform_tool_result", tool_name="terminal", status="error",
                                           result="{}", session_id="s", tool_call_id="x"))
            self.assertEqual(context.sections["hermes-atlas-evolution.playbook"]["content"]({}), "")
        self.assertEqual(len(logged.records), 3)   # tool, tool_outcome and skill
        tool = context.tools["hermes_atlas_evolution_lessons"]
        self.assertTrue(tool["check"]())
        context.fire("pre_llm_call", session_id="own", turn_id="t9", platform="cli", user_message="go")
        self.assertEqual(json.loads(tool["handler"]({"action": "search"}, session_id="own")), {"lessons": []})

    def test_invalid_settings_never_switch_on_what_the_operator_switched_off(self):
        self.settings(evolution={"lessons": "everything", "slow_turn_seconds": "fast"})
        with self.assertLogs("hermes_atlas_evolution.integration", level="WARNING"):
            context = self.plugin()
        self.assertEqual(context.sections, {})                    # an invalid mode means off
        self.assertNotIn("transform_tool_result", context.hooks)
        self.settings(evolution={"lessons": "off", "slow_turn_seconds": 120.5})   # another key is wrong
        self.assertEqual(self.plugin().sections, {})
        self.settings(evolution={"lessons": "auto", "slow_turn_seconds": 120.0})  # a form may store whole floats
        self.assertIn("hermes-atlas-evolution.playbook", self.plugin().sections)
        (self.home / "evolution" / "settings.json").write_text("{not json")
        self.assertEqual(self.plugin().sections, {})              # unreadable file: measure only



class SurfacesTest(PluginBase):
    """Who is being served decides what reaches the agent and what may change the owner's prompt."""

    MISSING_GIT = json.dumps({"output": "bash: git: command not found", "exit_code": 127})

    def turn(self, context, session, turn, **who):
        context.fire("pre_llm_call", session_id=session, turn_id=turn, user_message="go", **who)

    def failing_call(self, context, session, turn, call):
        context.fire("post_tool_call", tool_name="terminal", status="error", args={"command": "git status"},
                     result=self.MISSING_GIT, session_id=session, turn_id=turn, tool_call_id=call)

    def test_failures_a_stranger_causes_never_activate_a_lesson_by_themselves(self):
        from hermes_atlas_evolution.evolve import cycle

        self.settings(evolution={"lessons": "auto", "reflector": False})
        context = self.plugin()
        for index in range(4):   # a member of a free-response group makes the agent fail, in two chats
            session = f"group-{index % 2}"
            self.turn(context, session, f"t{index}", platform="whatsapp", sender_id="stranger")
            self.failing_call(context, session, f"t{index}", f"c{index}")
        close_observers()
        cycle(self.home, now=__import__("time").time(), config={"evolution": {"lessons": "auto", "reflector": False}})
        with experience(self.home) as conn:
            self.assertEqual(conn.execute("SELECT total FROM failure").fetchone()[0], 4)   # still measured
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM failure_owner").fetchone()[0], 0)
            self.assertEqual([row[0] for row in conn.execute("SELECT status FROM lesson")], ["candidate"])
        for index in range(2):   # the owner hits it twice in their own terminal: now it may activate by itself
            self.turn(context, f"own-{index}", f"o{index}", platform="cli")
            self.failing_call(context, f"own-{index}", f"o{index}", f"oc{index}")
        close_observers()
        with experience(self.home) as conn:
            conn.execute("DELETE FROM lesson")
            self.assertEqual(len(lessons.derive(conn, now=__import__("time").time(), mode="auto")), 1)
            self.assertEqual(conn.execute("SELECT status FROM lesson").fetchone()[0], "active")

    def test_a_chat_gets_template_lessons_only_and_the_tools_answer_only_the_owner(self):
        self.settings(evolution={"lessons": "auto"}, agent_advice=True)
        context = self.plugin()
        with experience(self.home) as conn:
            lessons.add_custom(conn, "Use ssh admin@10.0.0.7 for the tenant MCP.", tool="terminal", origin="operator",
                               now=1.0, active=True)
            conn.execute("""INSERT INTO lesson(signature, tool, klass, hint, origin, status, created, updated, activated)
                VALUES ('sig', 'terminal', 'missing_dependency', 'rg', 'derived', 'active', 1, 1, 1)""")
        render = context.sections["hermes-atlas-evolution.playbook"]["content"]
        own, chat = render({"session_id": "a", "platform": "cli"}), render({"session_id": "b", "platform": "telegram"})
        self.assertIn("10.0.0.7", own)
        self.assertIn("`rg`", chat)
        self.assertNotIn("10.0.0.7", chat)
        tool = context.tools["hermes_atlas_evolution_lessons"]["handler"]
        self.turn(context, "b", "t1", platform="telegram", sender_id="stranger")
        self.turn(context, "a", "t2", platform="cli")
        self.assertIn("error", json.loads(tool({"action": "search"}, session_id="b")))
        self.assertIn("error", json.loads(tool({"action": "search"}, session_id="never-seen")))
        self.assertIn("lessons", json.loads(tool({"action": "search"}, session_id="a")))

    def test_an_owner_listed_in_owner_sender_ids_is_the_owner_in_a_chat_too(self):
        self.settings(evolution={"lessons": "auto", "owner_sender_ids": ["555"]}, agent_advice=True)
        context = self.plugin()
        self.turn(context, "dm", "t1", platform="telegram", sender_id="555")
        tool = context.tools["hermes_atlas_evolution_lessons"]["handler"]
        self.assertIn("lessons", json.loads(tool({"action": "search"}, session_id="dm")))
        self.failing_call(context, "dm", "t1", "c1")
        close_observers()
        with experience(self.home) as conn:
            self.assertEqual(conn.execute("SELECT total FROM failure_owner").fetchone()[0], 1)

    def test_the_owner_notice_waits_for_a_session_end_hermes_shows(self):
        self.settings(evolution={"lessons": "suggest"})
        context = self.plugin()
        with experience(self.home) as conn:
            conn.execute("""INSERT INTO lesson(signature, tool, klass, hint, origin, status, created, updated)
                VALUES ('sig', 'terminal', 'missing_dependency', 'rg', 'derived', 'candidate', 1, 1)""")
        for reason in ("cli_close", "ws_orphan_reap", "startup_orphan_reap", "tui_close", "new_session"):
            self.assertIsNone(context.fire("on_session_finalize", session_id="s", platform="cli", reason=reason), reason)
        close_observers()
        with experience(self.home) as conn:
            self.assertIsNone(conn.execute("SELECT value FROM cursor WHERE key='notice_day'").fetchone())
        notice = context.fire("on_session_finalize", session_id="s", platform="cli", reason="user_exit")
        self.assertIn("1", notice["message"])



class HostIsolationTest(PluginBase):
    def test_in_a_plugin_host_nothing_is_registered_that_could_block_every_tool(self):
        with patch.dict("os.environ", {"HERMES_PLUGIN_HOST_PROCESS": "1"}):
            hosted = self.plugin()
        self.assertNotIn("pre_tool_call", hosted.hooks)
        self.assertIn("post_tool_call", hosted.hooks)          # everything else still observes
        self.assertIn("pre_tool_call", self.plugin().hooks)


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
        threads = [thread for thread in __import__("threading").enumerate() if thread.name == "hermes-atlas-evolution-autopilot"]
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
                          if thread.name == "hermes-atlas-evolution-autopilot"], [])


class LegacyInstallTest(PluginBase):
    """A profile that still runs the plugin under its old id must not count everything twice."""

    def test_the_new_plugin_stays_passive_while_the_old_id_is_still_enabled(self):
        old = self.home / "plugins" / "hermes-evolution"
        old.mkdir(parents=True)
        (old / "plugin.yaml").write_text("name: hermes-evolution\nversion: \"0.5.0\"\n")
        config = types.ModuleType("hermes_cli.config")
        config.load_config_readonly = lambda: {"plugins": {"enabled": ["hermes-evolution", "other"]}}
        with patch.dict(sys.modules, {"hermes_cli.config": config}), self.assertLogs(
                "hermes_atlas_evolution.integration", level="WARNING") as logged:
            context = self.plugin()
        self.assertEqual((context.hooks, context.tools, context.cli), ({}, {}, {}))
        self.assertIn("hermes plugins remove hermes-evolution", logged.output[0])
        config.load_config_readonly = lambda: {"plugins": {"enabled": ["hermes-atlas-evolution"]}}
        with patch.dict(sys.modules, {"hermes_cli.config": config}):
            self.assertIn("post_tool_call", self.plugin().hooks)    # removed from the enabled list: active again


class NativeSettingsTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addCleanup(close_observers)
        self.home = Path(folder.name)

    def test_values_from_the_hermes_settings_form_override_the_settings_file(self):
        from hermes_atlas_evolution.cli import effective_settings

        (self.home / "evolution").mkdir()
        (self.home / "evolution" / "settings.json").write_text(json.dumps(
            {"evolution": {"lessons": "off", "language": "en", "slow_turn_seconds": 30}, "agent_advice": False,
             "rates_usd_per_million": {}}))
        runtime = {"plugins": {"entries": {"hermes-atlas-evolution": {"settings": {
            "lessons": "auto", "agent_tools": True, "language": None, "owner_sender_ids": ["u1"]}}}}}
        merged = effective_settings(self.home, runtime)
        legacy = {"plugins": {"entries": {"hermes-evolution": {"settings": {"lessons": "suggest", "reflector": False}}}}}
        self.assertEqual(effective_settings(self.home, legacy)["evolution"]["reflector"], False)   # old id still read
        both = {"plugins": {"entries": {"hermes-evolution": {"settings": {"lessons": "suggest"}},
                                        "hermes-atlas-evolution": {"settings": {"lessons": "auto"}}}}}
        self.assertEqual(effective_settings(self.home, both)["evolution"]["lessons"], "auto")      # the new id wins
        self.assertEqual(merged["evolution"]["lessons"], "auto")
        self.assertEqual(merged["evolution"]["language"], "en")          # unset in Hermes: the file value stays
        self.assertEqual(merged["evolution"]["slow_turn_seconds"], 30)
        self.assertEqual(merged["evolution"]["owner_sender_ids"], ["u1"])
        self.assertTrue(merged["agent_advice"])
        self.assertEqual(effective_settings(self.home, None)["evolution"]["lessons"], "off")
        self.assertEqual(effective_settings(self.home, {"plugins": "garbage"})["evolution"]["lessons"], "off")

    def test_manifest_schema_matches_the_keys_the_code_reads(self):
        from hermes_atlas_evolution.cli import NATIVE_KEYS

        manifest = (Path(__file__).resolve().parent.parent / "plugin.yaml").read_text(encoding="utf-8")
        schema = manifest.split("config_schema:", 1)[1]
        declared = {line.strip()[:-1] for line in schema.splitlines() if line.startswith("  ") and not line.startswith("   ")
                    and line.strip().endswith(":")}
        self.assertEqual(declared, set(NATIVE_KEYS))
