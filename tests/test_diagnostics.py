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


if __name__ == "__main__":
    unittest.main()
