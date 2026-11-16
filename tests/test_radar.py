"""The radar reads Hermes' own records for what slows it down or wastes context, and proposes inspections."""

import json
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from hermes_atlas_evolution import proposals, radar
from hermes_atlas_evolution.experience import experience, utc_day
from hermes_atlas_evolution.operations import close_observers, database

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp()
DAY = 86_400


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addCleanup(close_observers)
        self.home = Path(folder.name)
        self.history = sqlite3.connect(self.home / "state.db")
        self.history.executescript("""
            CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, parent_session_id TEXT, started_at REAL,
                ended_at REAL, end_reason TEXT);
            CREATE TABLE session_model_usage (session_id TEXT, model TEXT, task TEXT, api_call_count INTEGER,
                input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
                estimated_cost_usd REAL, first_seen REAL, last_seen REAL);""")

    def sessions(self, reasons, *, parent=None, age=DAY):
        for index, reason in enumerate(reasons):
            self.history.execute("INSERT INTO sessions VALUES (?, 'local', ?, ?, ?, ?)",
                                 (f"{reason}-{index}-{parent}-{age}", parent, NOW - age, NOW - age + 60, reason))

    def usage(self, *, calls, prompt, task="", cache_share=0.9):
        cached = int(prompt * calls * cache_share)
        self.history.execute("INSERT INTO session_model_usage VALUES ('s', 'm', ?, ?, ?, 100, ?, 0, 0.01, ?, ?)",
                             (task, calls, prompt * calls - cached, cached, NOW - DAY, NOW - DAY))

    def scan(self, **options):
        try:
            self.history.commit()
        except sqlite3.ProgrammingError:   # closed on purpose by the test
            pass
        with experience(self.home) as conn:
            return radar.scan(self.home, conn, now=NOW, **options)


class HistoryTest(Base):
    def test_sessions_that_end_as_orphans_are_reported_with_their_share(self):
        self.sessions(["ws_orphan_reap"] * 6 + ["startup_orphan_reap"] * 2 + ["cli_close"] * 4 + [None])
        self.sessions(["agent_close"] * 5, parent="p")                  # subagents do not count
        self.sessions(["ws_orphan_reap"] * 9, age=30 * DAY)              # outside the window
        found = self.scan()
        self.assertEqual((found["sessions"]["ended"], found["sessions"]["orphaned"]), (12, 8))
        finding = next(item for item in found["findings"] if item["kind"] == "session_lifecycle")
        self.assertEqual((finding["evidence"]["orphan_pct"], finding["risk"]), (66.7, "low"))

    def test_a_heavy_context_per_call_is_reported_with_the_cache_share(self):
        self.usage(calls=60, prompt=90_000)
        self.usage(calls=40, prompt=10_000, task="compression")         # auxiliary calls are not the agent's context
        found = self.scan()
        self.assertEqual((found["context"]["calls"], found["context"]["avg_prompt_tokens"]), (60, 90_000))
        self.assertEqual(found["context"]["cache_pct"], 90.0)
        finding = next(item for item in found["findings"] if item["kind"] == "context_weight")
        self.assertEqual(finding["command"], "hermes prompt-size")

    def test_light_or_scarce_history_proposes_nothing(self):
        self.sessions(["ws_orphan_reap"] * 2 + ["cli_close"] * 2)
        self.usage(calls=10, prompt=150_000)
        self.assertEqual(self.scan()["findings"], [])

    def test_a_missing_history_is_not_an_error(self):
        self.history.close()
        (self.home / "state.db").unlink()
        found = self.scan()
        self.assertEqual((found["sessions"], found["context"], found["findings"]), (None, None, []))


class CompactionTest(Base):
    def test_compactions_that_keep_most_of_the_context_are_reported(self):
        with database(self.home) as ops:
            ops.execute("INSERT INTO native_activity VALUES (?, 'post_compaction', 'observed', 8, 0, 800000, 680000, 8)",
                        (utc_day(NOW),))
        found = self.scan()
        self.assertEqual((found["compaction"]["compactions"], found["compaction"]["kept_pct"]), (8, 85.0))
        self.assertIn("compaction", [item["kind"] for item in found["findings"]])


class ExternalFailuresTest(Base):
    def test_calls_blocked_or_broken_by_other_plugins_are_reported_not_learned(self):
        from hermes_atlas_evolution.signals import record_tool

        stuck = json.dumps({"error": "pre_tool_call plugin callback timed out or is still running"})
        for index in range(4):
            record_tool(self.home, {"tool_name": "read_file", "status": "blocked", "result": stuck, "session_id": "s"},
                        now=NOW - 60)
        close_observers()
        found = self.scan()
        self.assertEqual(found["external"], {"plugin_fault": 4, "rewritten_command": 0})
        codes = {item["code"]: item for item in found["findings"]}
        self.assertEqual(codes["plugin_guard_faults"]["evidence"]["blocked_calls"], 4)


class PluginHealthTest(Base):
    def manager(self, rows, *, bundled=None, user_manifests=()):
        plugins = types.ModuleType("hermes_cli.plugins")
        plugins.get_plugin_manager = lambda: types.SimpleNamespace(list_plugins=lambda: rows)
        plugins.get_bundled_plugins_dir = lambda: bundled or (self.home / "bundled")
        for name, hooks in user_manifests:
            folder = self.home / "plugins" / name
            folder.mkdir(parents=True)
            body = "\n".join([f"name: {name}", "provides_hooks:", *[f"  - {hook}" for hook in hooks]])
            (folder / "plugin.yaml").write_text(body + "\n")
        return patch.dict(sys.modules, {"hermes_cli.plugins": plugins})

    def test_load_errors_undeclared_hooks_and_first_wins_transforms_are_found(self):
        rows = [{"name": "broken", "key": "broken", "enabled": True, "hooks": 0, "error": "ModuleNotFoundError: No module named 'x' at /secret/path"},
                {"name": "omh", "key": "omh", "enabled": True, "hooks": 9, "error": None},
                {"name": "aaa-pretty", "key": "aaa-pretty", "enabled": True, "hooks": 1, "error": None},
                {"name": "zzz-safety", "key": "zzz-safety", "enabled": True, "hooks": 1, "error": None},
                {"name": "off", "key": "off", "enabled": False, "hooks": 0, "error": "whatever"}]
        manifests = (("broken", ()), ("omh", ("pre_llm_call", "post_tool_call")),
                     ("aaa-pretty", ("transform_tool_result",)), ("zzz-safety", ("transform_tool_result",)))
        with self.manager(rows, user_manifests=manifests):
            found = self.scan()
        codes = {item["code"]: item for item in found["findings"] if item["kind"] in ("plugin_health", "plugin_conflict")}
        self.assertEqual(codes["plugin_error:broken"]["evidence"]["error"], "ModuleNotFoundError")   # no message text
        self.assertEqual(codes["plugin_undeclared_hooks:omh"]["evidence"], {"registered": 9, "declared": 2})
        overlap = codes["plugin_transform_overlap:transform_tool_result"]
        self.assertEqual(overlap["evidence"]["plugins"], ["aaa-pretty", "zzz-safety"])   # load order: the first wins
        self.assertNotIn("plugin_error:off", codes)

    def test_without_a_loaded_plugin_manager_the_radar_skips_plugins(self):
        with patch.dict(sys.modules, {"hermes_cli.plugins": None}):
            self.assertEqual(self.scan()["plugins"], None)


class ProposalTest(Base):
    def test_radar_findings_become_proposals_that_expire_with_their_evidence(self):
        self.sessions(["ws_orphan_reap"] * 8 + ["cli_close"] * 4)
        found = self.scan()
        with experience(self.home) as conn:
            opened = proposals.refresh(conn, now=NOW, radar=found["findings"])
            self.assertIn("session_lifecycle", [item["kind"] for item in opened])
            self.assertEqual(proposals.refresh(conn, now=NOW + 60, radar=[]), [])


if __name__ == "__main__":
    unittest.main()
