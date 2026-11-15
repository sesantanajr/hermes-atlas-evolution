from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from hermes_evolution.diagnostics import snapshot


def create_state(db: sqlite3.Connection) -> None:
    db.executescript("CREATE TABLE sessions(id TEXT); CREATE TABLE messages(content TEXT);")
    db.execute("INSERT INTO messages VALUES ('PRIVATE_DATABASE_CONTENT')")
    db.commit()


class DiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)

    def test_reports_aggregates_and_ignores_stale_logs_and_duplicate_sink_lines(self):
        (self.home / "logs").mkdir()
        (self.home / "memories").mkdir()
        with closing(sqlite3.connect(self.home / "state.db")) as db:
            create_state(db)
        now = datetime.now() - timedelta(seconds=1)
        stale = now - timedelta(days=2)
        lines = [
            f"{now:%Y-%m-%d %H:%M:%S},123 WARNING[1] gateway: timeout PRIVATE_LOG",
            f"{now:%Y-%m-%d %H:%M:%S},124 ERROR gateway: rate limit PRIVATE_LOG",
            f"{stale:%Y-%m-%d %H:%M:%S},000 ERROR gateway: database is locked PRIVATE_LOG",
        ]
        (self.home / "logs" / "agent.log").write_text("\n".join(lines), encoding="utf-8")
        (self.home / "logs" / "errors.log").write_text(lines[0], encoding="utf-8")
        (self.home / "memories" / "MEMORY.md").write_text("PRIVATE_MEMORY_CONTENT", encoding="utf-8")
        (self.home / "memories" / "USER.md").write_bytes(b"\xff")

        result = snapshot(self.home, provider="openviking", running_version="0.23.1", upstream_tag="v0.24.0")
        self.assertEqual(result["database"]["status"], "ok")
        self.assertEqual(result["memory"]["provider"], "openviking")
        self.assertEqual(result["memory"]["native_files"]["MEMORY.md"]["status"], "readable")
        self.assertEqual(result["logs"]["events"], {"warning": 1, "timeout": 1, "error": 1, "rate_limit": 1})
        self.assertEqual(result["update"]["status"], "update_available")
        self.assertEqual({item["id"] for item in result["findings"]} & {
            "newer_release", "native_memory_readability",
        }, {"newer_release", "native_memory_readability"})
        self.assertNotIn("PRIVATE_", json.dumps(result))

    def test_closed_wal_uses_checkpoint_with_no_sidecar_creation(self):
        with closing(sqlite3.connect(self.home / "state.db")) as db:
            db.execute("PRAGMA journal_mode=WAL").fetchone()
            create_state(db)
        self.assertEqual({file.name for file in self.home.iterdir()}, {"state.db"})
        result = snapshot(self.home)["database"]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["mode"], "checkpoint_only")
        self.assertEqual({file.name for file in self.home.iterdir()}, {"state.db"})

    def test_plugin_failure_fingerprints_are_aggregated_without_message(self):
        (self.home / "logs").mkdir()
        now = datetime.now() - timedelta(seconds=1)
        rows = [f"{now:%Y-%m-%d %H:%M:%S} ERROR plugin failed: {kind} PRIVATE_PLUGIN_NAME"
                for kind in ("ModuleNotFoundError", "unexpected keyword argument")]
        (self.home / "logs" / "errors.log").write_text("\n".join(rows), encoding="utf-8")
        result = snapshot(self.home)
        self.assertEqual(result["logs"]["events"]["plugin_dependency_error"], 1)
        self.assertEqual(result["logs"]["events"]["plugin_contract_error"], 1)
        self.assertNotIn("PRIVATE_PLUGIN_NAME", json.dumps(result))

    def write_log(self, messages):
        (self.home / "logs").mkdir(exist_ok=True)
        now = datetime.now() - timedelta(seconds=1)
        rows = [f"{now:%Y-%m-%d %H:%M:%S},{index:03d} {message}" for index, message in enumerate(messages)]
        (self.home / "logs" / "errors.log").write_text("\n".join(rows), encoding="utf-8")

    def test_tool_output_and_file_names_are_not_component_failures(self):
        echo = "WARNING [local-1f] agent.tool_executor: Tool {} returned error (0.12s): "
        self.write_log([
            echo.format("memory") + '{"error": "PRIVATE proposal queued, memory write failed"}',
            echo.format("memory") + '{"error": "PRIVATE proposal queued, memory write failed again"}',
            echo.format("terminal") + '{"output": "PRIVATE permission denied, 429 rate limit, timeout, plugin failed"}',
            echo.format("terminal") + '{"output": "PRIVATE no space left on device, database is locked, 401"}',
            "WARNING gateway.platforms.base: [Local] native file send unavailable for /PRIVATE/Project-Memory-1.zip",
            "WARNING gateway.platforms.base: [Local] native file send unavailable for /PRIVATE/Project-Memory-2.zip",
            "WARNING hermes_cli.local_runtime.bootstrap: free GPU memory probe failed (PRIVATE)",
        ])
        result = snapshot(self.home)
        self.assertEqual(result["logs"]["events"], {"warning": 7, "tool_result_error": 4})
        self.assertEqual(result["findings"], [])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_memory_component_failures_are_recognized_by_their_logger(self):
        self.write_log([
            "WARNING agent.memory_manager: Memory provider 'PRIVATE' prefetch timed out after 5.0s; it keeps running",
            "WARNING [local-1f] agent.memory_manager: Memory provider 'PRIVATE' has 3 background calls stuck for over 60s",
            "ERROR plugins.memory.PRIVATE: writer error: PRIVATE",
            "WARNING run_agent: Memory provider on_session_end failed during shutdown: PRIVATE",
            "WARNING agent.memory_manager: Memory manager is shutting down; rejecting late sync task",
        ])
        result = snapshot(self.home)
        self.assertEqual(result["logs"]["events"], {"warning": 4, "error": 1, "memory_failure": 4, "timeout": 1})
        self.assertEqual([item["id"] for item in result["findings"]], ["log_memory"])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_checks_live_wal_and_preserves_contents(self):
        with closing(sqlite3.connect(self.home / "state.db")) as db:
            db.execute("PRAGMA journal_mode=WAL").fetchone()
            create_state(db)
            result = snapshot(self.home)["database"]
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["mode"], "read_only")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone(), (1,))

    def test_large_database_checks_schema_without_claiming_integrity(self):
        with closing(sqlite3.connect(self.home / "state.db")) as db:
            create_state(db)
        with patch("hermes_evolution.diagnostics._DB_SIZE_LIMIT", 1):
            result = snapshot(self.home)["database"]
        self.assertEqual(result["status"], "integrity_skipped_large")

    def test_reports_bad_state_and_does_not_create_absent_profile(self):
        profile = self.home / "missing"
        result = snapshot(profile, provider="https://PRIVATE_BAD_PROVIDER", upstream_tag="PRIVATE_BAD_TAG")
        self.assertFalse(profile.exists())
        self.assertEqual(result["database"]["status"], "missing_or_unreadable")
        self.assertEqual(result["update"]["status"], "invalid_reference")
        self.assertNotIn("PRIVATE_", json.dumps(result))
        (self.home / "state.db").write_bytes(b"broken database file" * 1024)
        result = snapshot(self.home)
        self.assertEqual(result["database"]["status"], "integrity_error")
        self.assertIn("state_integrity", {item["id"] for item in result["findings"]})

    def test_gateway_distinguishes_process_liveness_from_target_heartbeat(self):
        marker = {
            "pid": 123,
            "kind": "hermes-gateway",
            "start_time": 321,
            "gateway_state": "running",
            "code_version": "0.23.1",
            "updated_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
            "argv": ["PRIVATE_ARG"],
            "platforms": {"PRIVATE_PLATFORM": {"needs_attention": True, "error_message": "PRIVATE_ERROR"}},
        }
        state = self.home / "gateway_state.json"
        with patch("hermes_evolution.diagnostics._process_liveness", return_value="confirmed"):
            state.write_text(json.dumps(marker), encoding="utf-8")
            result = snapshot(self.home)
            self.assertNotIn("PRIVATE_", json.dumps(result))
            self.assertEqual(result["gateway"]["heartbeat"], "stale")
            self.assertEqual(result["gateway"]["process_liveness"], "confirmed")
            self.assertEqual(result["gateway"]["platforms_needing_attention"], 1)
            self.assertIn("gateway_heartbeat_stale", {item["id"] for item in result["findings"]})
            marker["code_version"] = "0.21.1"
            state.write_text(json.dumps(marker), encoding="utf-8")
            self.assertEqual(snapshot(self.home)["gateway"]["heartbeat"], "unverified")

    def test_named_profile_served_by_the_root_gateway_uses_the_host_record(self):
        profiles = self.home / "profiles"
        for name in ("work", "idle"):
            (profiles / name).mkdir(parents=True)
        record = {
            "pid": 123, "kind": "hermes-gateway", "start_time": 321, "gateway_state": "degraded",
            "code_version": "0.23.1", "updated_at": datetime.now(timezone.utc).isoformat(),
            "served_profiles": ["default", "work"], "hermes_home": "PRIVATE_HOME",
            "platforms": {"work:telegram": {"needs_attention": True, "error_message": "PRIVATE_ERROR"},
                          "other:slack": {"needs_attention": True}, "api_server": {"needs_attention": True}},
        }
        (self.home / "gateway_state.json").write_text(json.dumps(record), encoding="utf-8")
        with patch("hermes_evolution.diagnostics._process_liveness", return_value="confirmed"):
            served = snapshot(profiles / "work")
            unserved = snapshot(profiles / "idle")
            root = snapshot(self.home)
        self.assertEqual((served["gateway"]["reported_state"], served["gateway"]["platforms_needing_attention"],
                          served["gateway"]["served_by_host_gateway"]), ("degraded", 1, True))
        self.assertLessEqual({"gateway_degraded", "gateway_platform_attention"},
                             {item["id"] for item in served["findings"]})
        self.assertEqual((unserved["gateway"]["reported_state"], unserved["gateway"]["served_by_host_gateway"]),
                         (None, False))
        self.assertEqual((root["gateway"]["platforms_needing_attention"], root["gateway"]["served_by_host_gateway"]),
                         (1, False))
        self.assertNotIn("PRIVATE_", json.dumps([served, unserved, root]))


if __name__ == "__main__":
    unittest.main()
