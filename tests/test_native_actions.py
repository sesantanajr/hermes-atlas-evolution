"""Profile isolation and conservative native action boundaries."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from hermes_atlas_evolution.native_actions import create_handoff, schedule_spec


class NativeActionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.config = {"kanban_handoff": {"enabled": True, "board": "operations", "assignee": "ops-review"}}
        self.launcher = self.home / "hermes"
        self.which = patch("hermes_atlas_evolution.native_actions.shutil.which", return_value=str(self.launcher))
        self.which.start()
        self.addCleanup(self.which.stop)
        clean = patch.dict(os.environ, {
            "HERMES_DELEGATED_CHILD_CONTEXT": "", "HERMES_KANBAN_TASK": "", "HERMES_KANBAN_RUN_ID": "",
        })
        clean.start()
        self.addCleanup(clean.stop)

    def response(self, status="blocked", task_id="t_0123abcd"):
        return subprocess.CompletedProcess(
            [], 0, json.dumps({"id": task_id, "status": status, "body": "do-not-copy"}), "do-not-copy"
        )

    def test_bounded_handoff_is_blocked_and_uses_selected_profile_board(self):
        run = Mock(return_value=self.response())
        result = create_handoff(self.home, self.config, kind="research", subject="cite-review", now=0, runner=run)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["requires_operator_release"])
        self.assertNotIn("do-not-copy", str(result))
        args, kwargs = run.call_args
        self.assertEqual(args[0][:7], [
            str(self.launcher.resolve()), "--profile", "default", "kanban", "--board", "operations", "create",
        ])
        self.assertEqual(args[0][args[0].index("--initial-status") + 1], "blocked")
        self.assertEqual(args[0][args[0].index("--assignee") + 1], "ops-review")
        self.assertNotIn("--triage", args[0])
        self.assertNotIn("--goal", args[0])
        self.assertEqual(kwargs["env"]["HERMES_HOME"], str(self.home.resolve()))
        self.assertEqual(kwargs["cwd"], self.home.resolve())

    def test_native_idempotency_is_stable_today_and_requires_no_new_release_for_running_card(self):
        run = Mock(return_value=self.response("running"))
        first = create_handoff(self.home, self.config, kind="resources", subject="cpu", now=0, runner=run)
        again = create_handoff(self.home, self.config, kind="resources", subject="cpu", now=3600, runner=run)
        tomorrow = create_handoff(self.home, self.config, kind="resources", subject="cpu", now=86400, runner=run)
        self.assertEqual(first["idempotency_key"], again["idempotency_key"])
        self.assertNotEqual(first["idempotency_key"], tomorrow["idempotency_key"])
        self.assertFalse(again["requires_operator_release"])

    def test_config_and_worker_restrictions_fail_without_calling_the_cli(self):
        run = Mock()
        self.assertEqual(create_handoff(self.home, {}, kind="research", subject="test", runner=run)["status"], "disabled")
        with patch.dict(os.environ, {"HERMES_DELEGATED_CHILD_CONTEXT": "1"}):
            self.assertEqual(
                create_handoff(self.home, self.config, kind="research", subject="test", runner=run)["status"], "skipped"
            )
        with self.assertRaisesRegex(ValueError, "identifier"):
            create_handoff(self.home, self.config, kind="research", subject="--flag\nnext", runner=run)
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            create_handoff(self.home, self.config, kind="shell", subject="test", runner=run)
        bad = {"kanban_handoff": {"enabled": True, "board": "../other", "assignee": "ops-review"}}
        with self.assertRaisesRegex(ValueError, "board slug"):
            create_handoff(self.home, bad, kind="gateway", subject="timeout", runner=run)
        run.assert_not_called()

    def test_timeout_or_bad_reply_is_safe_to_retry_by_idempotency_key(self):
        timed_out = Mock(side_effect=subprocess.TimeoutExpired("hermes", 20))
        first = create_handoff(self.home, self.config, kind="curator", subject="reused", now=0, runner=timed_out)
        bad = Mock(return_value=self.response(task_id="secret@example.test"))
        second = create_handoff(self.home, self.config, kind="curator", subject="reused", now=0, runner=bad)
        self.assertEqual(first["idempotency_key"], second["idempotency_key"])
        self.assertEqual(first["status"], "unverified")
        self.assertEqual(second["status"], "unverified")
        self.assertNotIn("secret", str(second))

    def test_schedule_plan_is_local_no_agent_and_preserves_existing_files(self):
        plan = schedule_spec(self.home, every_minutes=15, upstream_tag="v0.23.1")
        self.assertEqual(plan["create_args"], [
            "hermes", "--profile", "default", "cron", "create", "every 15m", "--name", "hermes-atlas-evolution-watchdog",
            "--no-agent", "--script", "hermes-atlas-evolution-watchdog.py", "--deliver", "local",
        ])
        self.assertEqual(Path(plan["script_path"]), self.home.resolve() / "scripts" / plan["script_name"])
        self.assertIn("'ops', 'tick', '--notify-only'", plan["script_content"])
        self.assertIn("'--upstream-tag', 'v0.23.1'", plan["script_content"])
        self.assertIn("hermes --profile default cron create", plan["posix_command"])
        compile(plan["script_content"], plan["script_name"], "exec")
        self.assertFalse((self.home / "scripts").exists())
        for invalid in (None, True, 1, 1441):
            with self.assertRaises(ValueError):
                schedule_spec(self.home, every_minutes=invalid)
        with self.assertRaises(ValueError):
            schedule_spec(self.home, upstream_tag="1.0.0; expose secrets")

    def test_named_profile_is_pinned_in_both_subprocess_arguments_and_environment(self):
        named = self.home / "profiles" / "ops"
        named.mkdir(parents=True)
        run = Mock(return_value=self.response())
        default_result = create_handoff(self.home, self.config, kind="gateway", subject="health", now=0, runner=run)
        named_result = create_handoff(named, self.config, kind="gateway", subject="health", now=0, runner=run)
        self.assertNotEqual(default_result["idempotency_key"], named_result["idempotency_key"])
        args, kwargs = run.call_args
        self.assertEqual(args[0][1:3], ["--profile", "ops"])
        self.assertEqual(kwargs["env"]["HERMES_HOME"], str(named.resolve()))
        plan = schedule_spec(named)
        self.assertEqual(plan["create_args"][1:3], ["--profile", "ops"])
        self.assertIn("'--profile', 'ops'", plan["script_content"])


if __name__ == "__main__":
    unittest.main()
