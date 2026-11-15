import tempfile
import unittest
from pathlib import Path

from hermes_evolution.native_signals import native_findings, native_summary, observe_native
from hermes_evolution.operations import close_observers, database


class NativeSignalTests(unittest.TestCase):
    def test_hooks_keep_only_counts_and_classify_interrupted_separately(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            for status in ("completed", "completed", "completed", "failed", "timeout", "interrupted", "secret-state"):
                observe_native(home, "subagent_stop", {"child_status": status, "child_summary": "private-summary",
                                                       "duration_ms": 250}, now=1000)
            observe_native(home, "post_compaction", {"before_tokens": 9000, "after_tokens": 4000,
                                                     "session_id": "private-session"}, now=1000)
            observe_native(home, "post_compaction", {"before_tokens": 7000}, now=1000)
            observe_native(home, "post_curator_run", {"dry_run": True, "run_json_path": "private-path"}, now=1000)
            for _ in range(3):
                observe_native(home, "on_kanban_worker_exited", {"exit_kind": "rate_limited", "task_id": "private-task"},
                               now=1000)
            with database(home) as conn:
                report = native_summary(conn, days=1, now=1000)
                self.assertNotIn("private-", "\n".join(conn.iterdump()))
                self.assertNotIn("secret-state", "\n".join(conn.iterdump()))
            close_observers()  # Windows cannot remove a database that is still open.
        findings = {item["code"]: item for item in native_findings(report)}
        self.assertEqual(findings["delegation_failure_rate"]["evidence"][0]["value"], 5)
        self.assertIn("kanban_worker_rate_limits", findings)
        compaction = next(item for item in report["groups"] if item["hook"] == "post_compaction")
        self.assertEqual((compaction["events"], compaction["measured"], compaction["tokens_after"]), (2, 1, 4000))
