"""`hermes evolution status` answers "is it working here?" and points at active profiles without the plugin."""

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hermes_evolution import status
from hermes_evolution.autopilot import claim, finish
from hermes_evolution.experience import experience, utc_day
from hermes_evolution.operations import close_observers
from hermes_evolution.signals import record_guard, record_tool

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp()


def history(home: Path, sessions: int, *, age: float = 3600) -> None:
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(home / "state.db")
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL)")
    conn.executemany("INSERT INTO sessions VALUES (?, ?)", [(f"s{index}", NOW - age) for index in range(sessions)])
    conn.commit()
    conn.close()


class StatusTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addCleanup(close_observers)
        self.root = Path(folder.name)
        self.home = self.root / "profiles" / "work"
        history(self.root, 2)                                   # the default profile is barely used
        history(self.home, 40)
        history(self.root / "profiles" / "cron-box", 30)        # busy, without the plugin
        history(self.root / "profiles" / "idle", 0)
        (self.home / "plugins" / "hermes-evolution").mkdir(parents=True)

    def test_a_working_profile_reports_events_the_last_pass_and_the_profiles_left_out(self):
        record_tool(self.home, {"tool_name": "terminal", "status": "ok"}, now=NOW)
        record_guard(self.home, "gateway_stop", now=NOW)
        claim(self.home, now=NOW - 60, interval_s=3600, owner="a")
        finish(self.home, now=NOW - 50, owner="a", ok=True, interval_s=3600, started=NOW - 60, summary={"new_lessons": 2})
        with experience(self.home) as conn:
            conn.execute("INSERT OR REPLACE INTO cursor VALUES ('backfill_done', 'done')")
            conn.execute("INSERT OR REPLACE INTO cursor VALUES ('backfill_totals', ?)",
                         (json.dumps({"sessions": 9, "turns": 20, "tool_calls": 300, "failures": 4}),))
        close_observers()
        report = status.build(self.home, now=NOW, config={"evolution": {"reflector": True}})
        self.assertEqual(report["profile"], "work")
        self.assertEqual(report["today"]["tool_calls"], 1)
        self.assertTrue(report["autopilot"]["last_pass"]["ok"])
        self.assertEqual(report["backfill"]["status"], "done")
        self.assertEqual(report["runtime_guard"]["vetoed_7d"], 1)
        left_out = {item["profile"]: item["sessions_7d"] for item in report["profiles_without_plugin"]}
        self.assertEqual(left_out, {"cron-box": 30, "default": 2})      # idle profiles are not worth mentioning
        self.assertRegex(report["plugin_version"], r"^\d+\.\d+\.\d+$")
        text = status.markdown(report, "pt")
        self.assertIn("cron-box", text)
        self.assertIn("hermes -p cron-box plugins install sesantanajr/hermes-atlas-evolution --enable", text)

    def test_a_silent_profile_says_so(self):
        report = status.build(self.home, now=NOW, config={})
        self.assertIn("no_events_today", report["attention"])
        self.assertIn("no_autopilot_pass", report["attention"])
        self.assertIn("Nenhum evento", status.markdown(report, "pt"))


if __name__ == "__main__":
    unittest.main()
