from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from hermes_atlas_evolution import billing
from hermes_atlas_evolution.billing import native_billing


_NOW = 200_000
_CUTOFF = _NOW - 86400
_SCHEMA = """CREATE TABLE session_model_usage (
    session_id TEXT, task TEXT, billing_provider TEXT, model TEXT, billing_base_url TEXT,
    api_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER,
    cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER,
    estimated_cost_usd REAL, actual_cost_usd REAL, cost_status TEXT, first_seen REAL, last_seen REAL
)"""


class NativeBillingTests(unittest.TestCase):
    def setUp(self):
        workspace = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(workspace.cleanup)
        self.home = Path(workspace.name)
        self.path = self.home / "state.db"
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(_SCHEMA)

    def _insert(self, *rows):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executemany("INSERT INTO session_model_usage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
            connection.commit()

    def _report(self):
        return native_billing(self.home, days=1, now=_NOW)

    def test_summed_costs_carry_no_binary_float_residue(self):
        self._insert(*(
            (f"session-{index}", "", "openai", "gpt-5", "", 1, 10, 1, 0, 0, 0, cost, 0, "estimated", _CUTOFF, _NOW)
            for index, cost in enumerate((0.1, 0.2, 0.000_000_014))
        ))
        report = self._report()
        self.assertEqual(report["totals"]["estimated_cost_usd"], 0.30000001)
        self.assertEqual(report["by_model"][0]["estimated_cost_usd"], 0.30000001)
        self.assertIsNone(report["totals"]["actual_cost_usd"])

    def test_cumulative_window_separates_fully_contained_rows(self):
        self._insert(
            ("session-secret-1", "", "openai", "openai/gpt-5", "https://endpoint.invalid", 2, 80, 20, 20, 0, 15, 0.1, 0, "estimated", _CUTOFF, _NOW),
            ("session-secret-1", "vision", "gemini", "gemini-flash", "https://endpoint.invalid", 3, 50, 10, 10, 5, 0, 0.3, 0.2, "actual", _CUTOFF - 1, _NOW),
            ("session-secret-2", "title_generation", "", "unknown", "", 1, 20, 5, 0, 0, 0, 0, 0, "unknown", None, _NOW),
            ("session-secret-3", "", "openai", "gpt-5", "", 4, 10000, 1, 0, 0, 0, 90, 1, "actual", _CUTOFF - 20, _CUTOFF - 1),
            ("session-secret-4", "", "openai", "gpt-5", "", 4, 10000, 1, 0, 0, 0, 90, 1, "actual", None, None),
            ("session-secret-5", "", "openai", "gpt-5", "", 4, 10000, 1, 0, 0, 0, 90, 1, "actual", _NOW, _NOW + 1),
        )

        before = self.path.read_bytes()
        report = self._report()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["window"]["basis"], "cumulative_rows_last_seen_in_window")
        totals = report["totals"]
        self.assertEqual(totals["rows"], 3)
        self.assertEqual(totals["input_tokens"], 150)
        self.assertEqual(totals["prompt_tokens"], 185)
        self.assertEqual(totals["reasoning_tokens"], 15)
        self.assertEqual(totals["output_tokens"], 35)
        self.assertAlmostEqual(totals["estimated_cost_usd"], 0.4)
        self.assertAlmostEqual(totals["actual_cost_usd"], 0.2)
        self.assertEqual(totals["rows_without_cost_evidence"], 1)
        self.assertEqual(report["fully_in_window_totals"]["rows"], 1)
        self.assertEqual(report["fully_in_window_totals"]["input_tokens"], 80)
        self.assertEqual(report["coverage"]["rows_crossing_window_start"], 1)
        self.assertEqual(report["coverage"]["rows_without_valid_first_seen"], 1)
        self.assertEqual(report["coverage"]["rows_without_last_seen_excluded"], 1)
        self.assertEqual(report["coverage"]["latest_cost_status_rows"], {"actual": 1, "estimated": 1, "unknown": 1})
        routes = {(row["task"], row["provider"], row["model"]) for row in report["by_route"]}
        self.assertEqual(routes, {("main", "openai", "openai/gpt-5"), ("vision", "gemini", "gemini-flash"), ("title_generation", "unknown", "unknown")})
        serialized = json.dumps(report)
        self.assertNotIn("endpoint.invalid", serialized)
        self.assertNotIn("session-secret", serialized)

    def test_unknown_cost_is_distinct_from_included_zero_and_labels_are_filtered(self):
        self._insert(
            ("s", "", "oauth", "model-safe", "", 1, 1, 1, 0, 0, 0, 0, 0, "included", _NOW, _NOW),
            ("s", "https://endpoint.invalid", "sk-secret-test-value", "https://endpoint.invalid", "", 1, 1, 1, 0, 0, 0, 0, 0, "unknown", _NOW, _NOW),
        )
        report = self._report()
        groups = {row["provider"]: row for row in report["by_provider"]}
        self.assertEqual(groups["oauth"]["estimated_cost_usd"], 0.0)
        self.assertIsNone(groups["oauth"]["actual_cost_usd"])
        self.assertIsNone(groups["unknown"]["estimated_cost_usd"])
        self.assertEqual(groups["unknown"]["rows_without_cost_evidence"], 1)
        self.assertNotIn("endpoint.invalid", json.dumps(report))
        self.assertNotIn("secret-test", json.dumps(report))

    def test_an_empty_db_is_empty_and_past_the_row_limit_the_newest_rows_are_totalled(self):
        self.assertEqual(self._report()["status"], "empty")
        self._insert(*(("s", "", "openai", "gpt-5", "", 1, 2, 3, 0, 0, 0, 1, 0, "estimated", _CUTOFF, _NOW - index)
                       for index in range(5)))
        with patch("hermes_atlas_evolution.billing._ROW_LIMIT", 3):
            limited = self._report()
        # 0.12.0: totals of the newest rows, said to be partial and from when, instead of no totals at all
        self.assertEqual((limited["status"], limited["totals"]["rows"], limited["totals"]["api_call_count"]),
                         ("partial", 3, 3))
        self.assertEqual((limited["coverage"]["partial"], limited["coverage"]["covered_from_epoch"]), (True, _NOW - 2))
        whole = self._report()
        self.assertEqual((whole["status"], whole["totals"]["rows"], whole["coverage"]["partial"]), ("ok", 5, False))
        self.assertIsNone(whole["coverage"]["covered_from_epoch"])

    def test_the_window_is_one_range_on_last_seen(self):
        self.assertNotIn(" OR ", billing._USAGE_SQL.upper())
        self.assertIn("ORDER BY last_seen DESC", billing._USAGE_SQL)

    def test_closed_wal_reads_checkpoint_without_initializing_sidecars(self):
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone(), ("wal",))
        self._insert(("s", "", "openai", "gpt-5", "", 1, 2, 3, 0, 0, 0, 1, 0, "estimated", _NOW, _NOW))
        wal = self.path.with_name("state.db-wal")
        shm = self.path.with_name("state.db-shm")
        self.assertFalse(wal.exists())
        self.assertFalse(shm.exists())
        report = self._report()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["mode"], "checkpoint_only")
        self.assertEqual(report["totals"]["input_tokens"], 2)
        self.assertFalse(wal.exists())
        self.assertFalse(shm.exists())

    def test_live_wal_includes_uncheckpointed_usage(self):
        with closing(sqlite3.connect(self.path)) as writer:
            self.assertEqual(writer.execute("PRAGMA journal_mode=WAL").fetchone(), ("wal",))
            writer.execute("INSERT INTO session_model_usage VALUES (?, '', 'openai', 'gpt-5', '', 1, 2, 3, 0, 0, 0, 1, 0, 'estimated', ?, ?)", ("s", _NOW, _NOW))
            writer.commit()
            report = self._report()
            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["mode"], "live")
            self.assertEqual(report["totals"]["rows"], 1)

    def test_checkpoint_result_is_discarded_if_a_sidecar_appears(self):
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone(), ("wal",))

        def writer_appears(*args, **kwargs):
            self.path.with_name("state.db-wal").touch()
            return read_usage(*args, **kwargs)

        read_usage = billing._read_usage
        with patch.object(billing, "_read_usage", side_effect=writer_appears):
            report = self._report()
        self.assertEqual(report["status"], "snapshot_changed")
        self.assertNotIn("totals", report)
        self.assertEqual(self._report()["status"], "wal_sidecars_unavailable")
        self.assertFalse(self.path.with_name("state.db-shm").exists())

    def test_missing_or_incompatible_db_produces_explicit_status(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("DROP TABLE session_model_usage")
        self.assertEqual(self._report()["status"], "unexpected_schema")
        self.path.unlink()
        self.assertEqual(self._report()["status"], "missing_or_unreadable")
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
