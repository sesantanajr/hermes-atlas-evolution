import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hermes_atlas_evolution.experience import experience, label, prune
from hermes_atlas_evolution.operations import close_observers
from hermes_atlas_evolution.signals import (FeedbackSensor, TurnTracker, classify, effective_tool, failure_summary,
                                      feedback_summary, record_skill_event, record_tool, skill_event_summary,
                                      surface_of, tool_summary, turn_summary)

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp()
DAY = 86_400


def result(**fields):
    return json.dumps(fields)


class ClassifyTest(unittest.TestCase):
    def test_real_hermes_error_shapes_map_to_a_class_and_an_identifier_hint(self):
        cases = [
            ("tool_call", result(error="tool_call to 'mcp__files__search' is missing required argument(s): servico. "
                                       "The tool was NOT invoked."), "mcp__files__search", "missing_argument", "servico"),
            ("skill_manage", result(success=False, error="Refusing patch: Skill 'x' is locked by the user"),
             "skill_manage", "locked_resource", ""),
            ("skill_manage", result(error="operations[0] (patch on 'x') failed: Refusing background curator patch for "
                                          "skill 'x': SKILL.md has not been loaded in this review"),
             "skill_manage", "review_precondition", ""),
            ("write_file", result(error="Refusing to overwrite /a/b: /a/b exists but this task has not seen its full content"),
             "write_file", "edit_precondition", ""),
            ("patch", result(success=False, error="Found 3 matches for old_string. Provide more context"),
             "patch", "edit_mismatch", ""),
            ("terminal", result(output="", exit_code=-1, error="BLOCKED: Command flagged as dangerous (script execution via -e/-c flag)"),
             "terminal", "blocked_by_policy", "inline_script"),
            ("terminal", result(output="", exit_code=-1, error="BLOCKED: Command timed out without user response."),
             "terminal", "approval_refused", ""),
            ("terminal", result(output="[Command timed out after 60s]", exit_code=124, error=None), "terminal", "timeout", ""),
            ("terminal", result(output="/bin/bash: line 1: pwsh: command not found", exit_code=127), "terminal",
             "missing_dependency", "pwsh"),
            ("terminal", result(output="Traceback (most recent call last):\n File x\nModuleNotFoundError: No module named 'openpyxl'",
                                exit_code=1), "terminal", "missing_dependency", "openpyxl"),
            ("terminal", result(output="Traceback (most recent call last):\n File x\nValueError: bad", exit_code=1),
             "terminal", "code_exception", "ValueError"),
            ("skill_view", result(success=False, error="Skill 'abc' not found."), "skill_view", "not_found", "skill"),
            ("terminal", result(output="boom", exit_code=3), "terminal", "command_failed", "exit_3"),
            ("web_extract", None, "web_extract", "other", ""),
        ]
        for tool, body, expected_tool, klass, hint in cases:
            with self.subTest(tool=tool, klass=klass):
                found = classify(tool, "error", None, body)
                self.assertEqual((found["tool"], found["klass"], found["hint"]), (expected_tool, klass, hint))
                self.assertRegex(found["signature"], r"^[0-9a-f]{20}$")

    def test_hint_is_read_from_the_same_line_as_its_marker(self):
        split = classify("tool_call", "error", "missing required argument",
                         result(error="tool_call to 'files_search' is missing required argument(s): servico."))
        self.assertEqual((split["tool"], split["klass"], split["hint"]), ("files_search", "missing_argument", "servico"))
        bare = classify("t", "error", "missing required argument\nnext_line_word", None)
        self.assertEqual((bare["klass"], bare["hint"]), ("missing_argument", ""))

    def test_every_hermes_approval_refusal_is_its_own_class(self):
        # Literal texts of tools/approval.py in Hermes 0.23.1 (the {placeholders} filled as Hermes does).
        stop = (" The user has NOT consented to this action. Do NOT retry this command, do NOT rephrase it, and do NOT "
                "attempt the same outcome via a different command. Stop the current workflow and wait for the user to "
                "respond before taking any further destructive or irreversible action.")
        refusals = [
            "BLOCKED: User denied this command." + stop,
            "BLOCKED: Command timed out without user response." + stop + " Silence is not consent.",
            "BLOCKED: User denied this command through the selected approval transport. The user has NOT consented to "
            "this action. Do NOT retry or attempt the same outcome through another route.",
            "BLOCKED: Failed to send approval request to user. Do NOT retry.",
            "BLOCKED: Failed to send execute_code approval request to user. Do NOT retry.",
            "BLOCKED: User denied execute_code script execution (matched 'x'). Do NOT retry — the user has explicitly rejected it.",
            "BLOCKED: Command flagged as dangerous (script execution via -e/-c flag) but no user is present in cron jobs. "
            "Use normal tools instead. To allow commands in cron jobs, set approvals.cron: approve in config.yaml.",
            "BLOCKED: Command flagged as dangerous (script execution via -e/-c flag)." + stop,
        ]
        for message in refusals:
            with self.subTest(message=message[:60]):
                found = classify("terminal", "error", None, result(output="", exit_code=-1, error=message))
                self.assertEqual(found["klass"], "approval_refused")

    def test_hints_from_command_output_must_look_like_a_command_or_module(self):
        leaks = [
            (result(output="bash: SenhaDoBancoProducao: command not found", exit_code=127), "missing_dependency"),
            (result(output="bash: maria.souza: command not found", exit_code=127), "missing_dependency"),
            (result(output="Error: missing required argument(s): maria.souza", exit_code=2), "missing_argument"),
            (result(output="tool_call to 'evil_tool_name_from_output' is missing required argument(s): x", exit_code=2),
             "missing_argument"),
        ]
        for body, klass in leaks:
            with self.subTest(body=body):
                found = classify("terminal", "error", None, body)
                self.assertEqual((found["tool"], found["klass"]), ("terminal", klass))
                self.assertEqual(found["hint"] in ("", "x"), True, found["hint"])
        module = classify("terminal", "error", None, result(output="ModuleNotFoundError: No module named 'google.cloud'",
                                                            exit_code=1))
        self.assertEqual((module["klass"], module["hint"]), ("missing_dependency", "google.cloud"))
        command = classify("terminal", "error", None, result(output="/bin/sh: 1: php8.2: command not found", exit_code=127))
        self.assertEqual(command["hint"], "")   # a dotted command name could be anything: dropped
        named = classify("tool_call", "error", "tool_call to 'mcp__crm__lookup' is missing required argument(s): id", None,
                         {"calls": [{"name": "a"}, {"name": "b"}]})
        self.assertEqual((named["tool"], named["hint"]), ("mcp__crm__lookup", "id"))

    def test_success_is_not_a_failure_and_blocked_defaults_to_policy(self):
        self.assertIsNone(classify("terminal", "ok", None, "{}"))
        self.assertEqual(classify("terminal", "blocked", None, result(note="x"))["klass"], "blocked_by_policy")
        self.assertEqual(classify("web_search", "error", "HTTP 429 Too Many Requests", None)["klass"], "rate_limited")

    def test_hints_never_carry_paths_urls_or_credentials(self):
        leaks = [
            result(error="missing required argument(s): /etc/passwd"),
            result(error="missing required argument(s): sk-abcdefghijklmnop"),
            result(output="https://user:pass@host/x: command not found", exit_code=127),
            result(error="requires a 'ghp_abcdefghijklmnopqrstuv'"),
        ]
        for body in leaks:
            with self.subTest(body=body):
                found = classify("terminal", "error", None, body)
                self.assertNotIn("/", found["hint"])
                self.assertFalse(found["hint"].startswith(("sk-", "ghp_")))

    def test_same_failure_has_a_stable_signature_and_different_hints_differ(self):
        first = classify("t", "error", "missing required argument(s): a", None)
        again = classify("t", "error", "x missing required argument(s): a.", None)
        other = classify("t", "error", "missing required argument(s): b", None)
        self.assertEqual(first["signature"], again["signature"])
        self.assertNotEqual(first["signature"], other["signature"])

    def test_wrapper_tool_is_attributed_to_the_tool_that_ran(self):
        self.assertEqual(effective_tool("tool_call", {"calls": [{"name": "web_search", "arguments": {}}]}), "web_search")
        self.assertEqual(effective_tool("tool_call", {"calls": json.dumps([{"name": "x_tool"}])}), "x_tool")
        self.assertEqual(effective_tool("tool_call", {"name": "legacy_tool"}), "legacy_tool")
        self.assertEqual(effective_tool("tool_call", {"calls": [{"name": "a"}, {"name": "b"}]}), "tool_call")
        self.assertEqual(effective_tool("terminal", {"calls": [{"name": "a"}]}), "terminal")
        self.assertEqual(effective_tool("bad name!", None), "unknown")

    def test_label_rejects_urls_and_secrets(self):
        self.assertEqual(label("mcp__server__tool"), "mcp__server__tool")
        self.assertEqual(label("https://x"), "unknown")
        self.assertEqual(label("sk-abcdefgh12345678"), "unknown")
        self.assertEqual(label(None, fallback=""), "")


class StoreTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addCleanup(close_observers)   # Windows cannot remove a database that is still open.
        self.home = Path(folder.name)

    def test_tool_outcomes_are_counted_without_storing_any_payload_text(self):
        canary = "CANARY-SECRET-ARGUMENT"
        failing = {"tool_name": "terminal", "status": "error", "session_id": "session-" + canary, "duration_ms": 50,
                   "args": {"command": canary}, "error_message": canary,
                   "result": result(output=f"{canary}: command not found", exit_code=127)}
        for index in range(3):
            record_tool(self.home, {**failing, "session_id": f"s{index % 2}-{canary}"}, now=NOW - index * DAY)
        record_tool(self.home, {"tool_name": "terminal", "status": "ok", "result": canary, "duration_ms": 150}, now=NOW)
        record_tool(self.home, {"tool_name": "terminal", "status": "cancelled"}, now=NOW)
        with experience(self.home) as conn:
            tools = tool_summary(conn, now=NOW, days=7)
            failures = failure_summary(conn, now=NOW, days=7)
            dump = "\n".join(conn.iterdump())
        self.assertNotIn(canary, dump)
        self.assertEqual((tools["calls"], tools["errors"]), (4, 3))
        self.assertEqual(tools["by_tool"][0]["mean_ms"], 75)
        self.assertEqual(len(failures), 1)
        self.assertEqual((failures[0]["class"], failures[0]["count"], failures[0]["days"], failures[0]["sessions"]),
                         ("missing_dependency", 3, 3, 3))
        self.assertEqual(failures[0]["hint"], "")   # the canary is not an identifier-safe hint

    def test_turns_are_measured_per_surface_with_latency_percentiles(self):
        tracker = TurnTracker(self.home, slow_ms=60_000)
        for index, seconds in enumerate([5, 5, 5, 5, 5, 5, 20, 20, 100, 400]):
            turn = f"t{index}"
            tracker.start({"turn_id": turn, "platform": "cli"}, clock=1000.0)
            tracker.start({"turn_id": turn, "platform": "telegram"}, clock=1500.0)   # duplicate start is ignored
            tracker.tool({"turn_id": turn, "tool_call_id": "c1", "status": "error"})
            tracker.tool({"turn_id": turn, "tool_call_id": "c1", "status": "error"})   # replay of the same call
            self.assertTrue(tracker.finish({"turn_id": turn, "completed": index != 9, "failed": index == 9},
                                           clock=1000.0 + seconds, now=NOW))
        tracker.start({"turn_id": "cron", "platform": "cron"}, clock=0.0)
        tracker.finish({"turn_id": "cron", "completed": True}, clock=1.0, now=NOW)
        tracker.start({"turn_id": "child", "platform": "telegram", "parent_session_id": "p"}, clock=0.0)
        tracker.finish({"turn_id": "child", "interrupted": True}, clock=1.0, now=NOW)
        self.assertFalse(tracker.finish({"turn_id": "never-started", "completed": True}, clock=1.0, now=NOW))
        with experience(self.home) as conn:
            local = turn_summary(conn, now=NOW, days=1, surfaces=("local", "gateway"))
            cron = turn_summary(conn, now=NOW, days=1, surfaces=("cron",))
            child = turn_summary(conn, now=NOW, days=1, surfaces=("subagent",))
        self.assertEqual((local["turns"], local["completed"], local["failed"], local["slow"]), (10, 9, 1, 2))
        self.assertEqual((local["with_tools"], local["tool_errors"]), (10, 10))
        self.assertEqual((local["p50_ms"], local["p95_ms"], local["max_ms"]), (15_000, 400_000, 400_000))
        self.assertEqual((cron["turns"], child["turns"], child["interrupted"]), (1, 1, 1))

    def test_surface_classification(self):
        self.assertEqual([surface_of(name) for name in ("cli", "desktop", "cron", "subagent", "whatsapp", None)],
                         ["local", "local", "cron", "subagent", "gateway", "gateway"])

    def test_owner_feedback_counts_only_owner_surfaces_and_keeps_no_text(self):
        sensor = FeedbackSensor(self.home, owner_sender_ids=("owner-1",))
        canary = "relatório trimestral confidencial da diretoria"
        self.assertEqual(sensor.observe({"session_id": "a", "platform": "cli", "user_message": "Gere o " + canary},
                                        clock=0.0, now=NOW), [])
        self.assertEqual(sensor.observe({"session_id": "a", "platform": "cli", "user_message": "gere o " + canary + " agora"},
                                        clock=60.0, now=NOW), ["reask"])
        self.assertEqual(sensor.observe({"session_id": "a", "platform": "cli", "user_message": "Não, está errado: " + canary},
                                        clock=900.0, now=NOW), ["correction"])
        self.assertEqual(sensor.observe({"session_id": "b", "platform": "tui", "user_message": "Thanks, that worked"},
                                        clock=0.0, now=NOW), ["positive"])
        self.assertEqual(sensor.observe({"session_id": "c", "platform": "whatsapp", "sender_id": "stranger",
                                         "user_message": "não, errado"}, clock=0.0, now=NOW), [])
        self.assertEqual(sensor.observe({"session_id": "d", "platform": "whatsapp", "sender_id": "owner-1",
                                         "user_message": "wrong, try again"}, clock=0.0, now=NOW), ["correction"])
        self.assertEqual(sensor.observe({"session_id": "e", "platform": "cli", "parent_session_id": "p",
                                         "user_message": "não, errado"}, clock=0.0, now=NOW), [])
        self.assertEqual(sensor.observe({"session_id": "f", "platform": "cli", "user_message": ["multimodal"]},
                                        clock=0.0, now=NOW), [])
        for ordinary in ("Não precisa de tabela, só me diga o total", "Não sei se o relatório saiu", "No worries, go ahead",
                         "De novo o relatório de ontem, por favor"):
            self.assertEqual(sensor.observe({"session_id": "z", "platform": "cli", "user_message": ordinary},
                                            clock=5000.0, now=NOW), [], ordinary)
        for correction in ("Não, não era isso", "Ainda está errado", "Não funcionou", "não é isso que pedi"):
            self.assertEqual(sensor.observe({"session_id": "y" + correction, "platform": "cli", "user_message": correction},
                                            clock=6000.0, now=NOW), ["correction"], correction)
        with experience(self.home) as conn:
            summary = feedback_summary(conn, now=NOW, days=1)
            dump = "\n".join(conn.iterdump())
        self.assertEqual((summary["correction"], summary["reask"], summary["positive"]), (6, 1, 1))
        self.assertNotIn("confidencial", dump)

    def test_skill_events_and_retention(self):
        record_skill_event(self.home, {"action": "loaded", "skill_name": "deploy", "reuse_after_patch": True}, now=NOW)
        record_skill_event(self.home, {"action": "patched", "skill_name": "deploy"}, now=NOW)
        record_skill_event(self.home, {"action": "exfiltrate", "skill_name": "deploy"}, now=NOW)
        record_skill_event(self.home, {"action": "loaded", "skill_name": "../../etc"}, now=NOW)
        record_tool(self.home, {"tool_name": "old", "status": "ok"}, now=NOW - 200 * DAY)
        with experience(self.home) as conn:
            self.assertEqual(skill_event_summary(conn, now=NOW, days=1),
                             {"deploy": {"loaded": 1, "reused_after_patch": 1, "patched": 1}})
            prune(conn, now=NOW)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tool_daily").fetchone()[0], 0)

    def test_store_is_private_opens_a_newer_schema_and_rejects_an_invalid_one(self):
        with experience(self.home):
            pass
        path = self.home / "evolution" / "evolution.sqlite3"   # one database for operations and experience
        self.assertEqual(path.stat().st_mode & 0o077, 0)
        self.assertFalse((self.home / "evolution" / "experience.sqlite3").exists())
        import sqlite3
        raw = sqlite3.connect(path)
        raw.execute("PRAGMA user_version=99")   # a newer plugin version only adds tables and columns
        raw.close()
        with experience(self.home) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM lesson").fetchone()[0], 0)
        raw = sqlite3.connect(path)
        raw.execute("PRAGMA user_version=-1")
        raw.close()
        with self.assertRaises(ValueError):
            with experience(self.home):
                pass

    def test_a_missing_profile_is_an_error_not_a_new_tree(self):
        missing = self.home / "typo"
        with self.assertRaises(OSError):
            record_tool(missing, {"tool_name": "terminal", "status": "ok"}, now=NOW)
        self.assertFalse(missing.exists())

    def test_concurrent_hook_threads_lose_no_event(self):
        import threading

        def worker(index):
            for call in range(50):
                record_tool(self.home, {"tool_name": "terminal", "status": "error" if call % 5 == 0 else "ok",
                                        "result": result(output="boom", exit_code=2), "session_id": f"s{index}"}, now=NOW)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        close_observers()   # flush anything a busy thread left in the retry queue
        with experience(self.home) as conn:
            tools = tool_summary(conn, now=NOW, days=1)
            failures = failure_summary(conn, now=NOW, days=1)
        self.assertEqual((tools["calls"], tools["errors"]), (400, 80))
        self.assertEqual((failures[0]["count"], failures[0]["sessions"] >= 2), (80, True))


if __name__ == "__main__":
    unittest.main()
