from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from hermes_evolution.workloads import inspect_workloads


_NOW = 1_800_000_000
_SECRET = "PRIVATE_PLACEHOLDER_DO_NOT_EXPOSE"
_SCHEMA = """
CREATE TABLE tasks(id TEXT PRIMARY KEY, title TEXT, status TEXT NOT NULL, assignee TEXT, created_at INTEGER,
    started_at INTEGER, last_heartbeat_at INTEGER, claim_expires INTEGER, consecutive_failures INTEGER DEFAULT 0);
CREATE TABLE task_runs(status TEXT NOT NULL, ended_at INTEGER, error TEXT);
"""


def _iso(offset: int) -> str:
    return datetime.fromtimestamp(_NOW + offset, timezone.utc).isoformat()


def _task(db: sqlite3.Connection, number: int, status: str, *, assignee: str | None = _SECRET, age: int = 10,
          started: int | None = None, heartbeat: int | None = None, claim: int | None = None,
          failures: int = 0):
    db.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (f"{_SECRET}-{number}", _SECRET, status, assignee, _NOW - age, started, heartbeat, claim, failures))


class WorkloadsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "profile"

    def _write_cron(self, data):
        directory = self.home / "cron"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "jobs.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def _inspect(self, options=None):
        return inspect_workloads(self.home, provider_options=options, now=_NOW)

    def test_missing_stores_are_never_created(self):
        output = self._inspect()
        self.assertEqual(output["cron"]["status"], "not_found")
        self.assertEqual(output["kanban"]["status"], "not_found")
        self.assertFalse(self.home.exists())

    def test_cron_counts_bursts_and_sticky_failures_without_payloads(self):
        base = {"enabled": True, "state": "scheduled", "id": _SECRET, "script": _SECRET, "prompt": _SECRET}
        short = {"kind": "interval", "minutes": 2}
        fields = {"kind": "cron", "expr": "*/5 * * * *"}
        jobs = [
            {**base, "next_run_at": _iso(15), "last_status": "ok", "last_failure": {"at": _iso(-600), "detail": _SECRET},
             "schedule": short},
            {**base, "next_run_at": _iso(60), "last_status": "delivery_failed", "last_run_at": _iso(-20),
             "schedule": fields},
            {**base, "next_run_at": _iso(70), "no_agent": True, "schedule": short},
            {**base, "next_run_at": _iso(-600), "failure_streak": 2, "last_status": "error", "last_run_at": _iso(-900)},
            {**base, "next_run_at": _iso(-601), "fire_claim": {"at": _iso(-20), "by": _SECRET}},
            {**base, "next_run_at": _iso(61), "enabled": False},
            {**base, "next_run_at": "2026-01-01T00:00:00"},
        ]
        path = self._write_cron({"jobs": jobs})
        original = path.read_bytes()
        report = self._inspect({"cron": {"max_parallel_jobs": 2}, "delegation": {"max_concurrent_children": 4}})
        cron = report["cron"]
        self.assertEqual(cron["counts"]["frequent_agent_jobs"], 2)
        self.assertEqual(cron["counts"]["script_only_jobs"], 1)
        self.assertEqual(cron["counts"]["recent_job_failures"], 2)
        self.assertEqual(cron["counts"]["recent_delivery_failures"], 1)
        self.assertEqual(cron["counts"]["unverified_next_run"], 1)
        self.assertEqual(cron["counts"]["past_due_timestamps"], 1)
        self.assertEqual(cron["counts"]["recent_fire_claims"], 1)
        self.assertEqual(cron["schedule"]["peak_scheduled_starts"], 3)
        self.assertEqual(cron["schedule"]["peak_scheduled_agent_starts"], 2)
        self.assertEqual(report["delegation"]["configured_max_concurrent_children"], 4)
        self.assertEqual({result["code"] for result in report["recommendations"]}, {
            "cron_schedule_concentration", "cron_frequent_agent_jobs", "cron_dispatch_or_failures", "cron_delivery_failures"})
        self.assertEqual(path.read_bytes(), original)
        self.assertNotIn(_SECRET, json.dumps(report))

    def test_cron_json_and_record_limits_report_incomplete_coverage(self):
        path = self._write_cron({"jobs": [{"enabled": True}, {"enabled": True}]})
        with patch("hermes_evolution.workloads._JOB_LIMIT", 1):
            result = self._inspect()["cron"]
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["row_limit_reached"])
        self.assertEqual(result["counts"]["jobs"], 1)
        with patch("hermes_evolution.workloads._CRON_BYTES", 5):
            self.assertEqual(self._inspect()["cron"]["status"], "file_limit")
        path.write_text("not JSON", encoding="utf-8")
        self.assertEqual(self._inspect()["cron"]["status"], "invalid_format")

    def test_kanban_checkpoint_snapshot_aggregates_without_native_changes(self):
        self.home.mkdir()
        path = self.home / "kanban.db"
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)
            _task(db, 1, "ready", age=4000)
            _task(db, 2, "ready", assignee=None)
            _task(db, 3, "running", started=_NOW - 22000, heartbeat=_NOW - 4100, claim=_NOW - 4)
            _task(db, 4, "running", started=_NOW - 22000, heartbeat=_NOW - 10, claim=_NOW + 30)
            _task(db, 5, "blocked", failures=2)
            db.executemany("INSERT INTO task_runs VALUES (?, ?, ?)", [
                ("timed_out", _NOW - 50, _SECRET), ("done", _NOW - 30, _SECRET),
                ("failed", _NOW - 90000, _SECRET), ("blocked", _NOW - 40, _SECRET),
            ])
            db.commit()
        original = path.read_bytes()
        report = self._inspect({"kanban": {"max_in_progress": 2, "max_in_progress_per_profile": 1}})
        board = report["kanban"]
        self.assertEqual(board["status"], "ok")
        self.assertEqual(board["configured_max_in_progress"], 2)
        self.assertEqual(board["configured_max_in_progress_per_profile"], 1)
        self.assertEqual(board["counts"]["ready_with_assignee"], 1)
        self.assertEqual(board["counts"]["ready_without_assignee"], 1)
        self.assertEqual(board["counts"]["expired_claims"], 1)
        self.assertEqual(board["counts"]["stale_running_candidates"], 1)
        self.assertEqual(board["counts"]["repeated_task_failures"], 1)
        self.assertEqual(board["counts"]["finished_runs_24h"], 2)
        self.assertEqual(board["counts"]["failed_runs_24h"], 1)
        self.assertNotIn(_SECRET, json.dumps(report))
        self.assertEqual({file.name for file in self.home.iterdir()}, {"kanban.db"})
        self.assertEqual(path.read_bytes(), original)

    def test_kanban_live_wal_reads_committed_metadata(self):
        self.home.mkdir()
        with closing(sqlite3.connect(self.home / "kanban.db")) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)
            _task(db, 1, "ready")
            db.commit()
            files = {file.name for file in self.home.iterdir()}
            self.assertEqual(self._inspect()["kanban"]["counts"]["ready"], 1)
            self.assertEqual({file.name for file in self.home.iterdir()}, files)

    def test_shared_kanban_root_is_explicit_and_archived_boards_are_skipped(self):
        other = Path(self.temp.name) / "common"
        for board, archived in (("active", False), ("retired", True)):
            directory = other / "kanban" / "boards" / board
            directory.mkdir(parents=True)
            (directory / "board.json").write_text(json.dumps({"archived": archived}), encoding="utf-8")
            with closing(sqlite3.connect(directory / "kanban.db")) as db:
                db.executescript(_SCHEMA)
                _task(db, 1, "ready")
                db.commit()
        self.assertEqual(self._inspect()["kanban"]["status"], "not_found")
        board = self._inspect({"kanban_home": other})["kanban"]
        self.assertEqual(board["scope"], "shared_root")
        self.assertEqual(board["boards_read"], 1)
        self.assertEqual(board["counts"]["ready"], 1)

    def test_kanban_unsupported_schema_marks_board_unavailable(self):
        self.home.mkdir()
        with closing(sqlite3.connect(self.home / "kanban.db")) as db:
            db.execute("CREATE TABLE tasks(status TEXT)")
            db.commit()
        board = self._inspect()["kanban"]
        self.assertEqual(board["status"], "unavailable")
        self.assertEqual(board["unavailable"], {"unsupported_schema": 1})


if __name__ == "__main__":
    unittest.main()
