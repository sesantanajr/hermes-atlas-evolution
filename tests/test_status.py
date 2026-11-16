"""`hermes evolution status` answers "is it working here?" and points at active profiles without the plugin."""

import json
import sqlite3
import tempfile
import os
import re
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from hermes_atlas_evolution import status
from hermes_atlas_evolution.autopilot import claim, finish
from hermes_atlas_evolution.experience import experience, utc_day
from hermes_atlas_evolution.operations import close_observers
from hermes_atlas_evolution.signals import record_guard, record_tool

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
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.addCleanup(close_observers)
        self.root = Path(folder.name)
        self.home = self.root / "profiles" / "work"
        history(self.root, 2)                                   # the default profile is barely used
        history(self.home, 40)
        history(self.root / "profiles" / "cron-box", 30)        # busy, without the plugin
        history(self.root / "profiles" / "idle", 0)
        (self.home / "plugins" / "hermes-atlas-evolution").mkdir(parents=True)

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

    @unittest.skipIf(os.name == "nt", "the liveness probe is POSIX only, by design")
    def test_a_long_running_process_that_still_runs_older_code_is_flagged(self):
        import os

        from hermes_atlas_evolution.status import record_runtime

        with patch.object(status, "process_kind", return_value="gateway"):
            record_runtime(self.home, now=NOW, version="0.4.0")         # this process loaded an older copy
        record_runtime(self.home, now=NOW, version="9.9.9", pid=2 ** 22 + 7)   # a gateway that is gone
        close_observers()
        report = status.build(self.home, now=NOW, config={})
        stale = report["stale_runtimes"]
        self.assertEqual([(item["pid"], item["version"]) for item in stale], [(os.getpid(), "0.4.0")])
        self.assertIn("stale_runtime", report["attention"])
        self.assertIn("hermes gateway restart", status.markdown(report, "en"))

    def test_every_profile_of_the_root_is_listed_with_its_plugin_state(self):
        legacy = self.root / "profiles" / "legacy-box"
        history(legacy, 12)
        (legacy / "plugins" / "hermes-evolution").mkdir(parents=True)
        (legacy / "plugins" / "hermes-evolution" / "plugin.yaml").write_text("name: hermes-evolution\n")
        claim(self.home, now=NOW - 60, interval_s=3600, owner="a")
        finish(self.home, now=NOW - 50, owner="a", ok=True, interval_s=3600, started=NOW - 60)
        close_observers()
        quiet = status.build(self.root, now=NOW, config={})               # run from the barely used default profile
        rows = {row["profile"]: row for row in quiet["profiles"]}
        self.assertEqual(rows["work"]["plugin"], "installed")
        self.assertIsNotNone(rows["work"]["last_pass"])                  # read from that profile's own database
        self.assertEqual(rows["legacy-box"]["plugin"], "legacy")
        self.assertEqual(rows["cron-box"]["plugin"], "missing")
        self.assertTrue(rows["default"]["current"])
        self.assertIn("busier_profile", quiet["attention"])
        self.assertIn("legacy_install", quiet["attention"])
        text = status.markdown(quiet, "pt")
        self.assertIn("hermes -p work evolution status", text)
        self.assertIn("hermes -p legacy-box plugins remove hermes-evolution", text)

    def test_the_first_status_runs_the_first_pass_and_reads_the_history(self):
        import contextlib
        import io
        from types import SimpleNamespace

        from hermes_atlas_evolution.cli import execute

        history_db = sqlite3.connect(self.home / "state.db")    # sessions only: an empty but valid history
        history_db.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
                           "content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL)")
        history_db.commit()
        history_db.close()

        def run(**extra):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                execute(SimpleNamespace(evolution_action="status", format="json", lang="en", **extra), self.home,
                        runtime_config={})
            return json.loads(out.getvalue())

        quiet = run(no_learn=True)
        self.assertIsNone(quiet["first_pass"])
        self.assertIn("no_autopilot_pass", quiet["attention"])
        first = run(no_learn=False)
        self.assertIsNotNone(first["first_pass"])
        self.assertIsNotNone(first["autopilot"]["last_pass"])
        self.assertIsNone(run(no_learn=False)["first_pass"])     # only the first time

    def test_a_silent_profile_says_so(self):
        report = status.build(self.home, now=NOW, config={})
        self.assertIn("no_events_today", report["attention"])
        self.assertIn("no_autopilot_pass", report["attention"])
        self.assertIn("Nenhum evento", status.markdown(report, "pt"))



class GuardStateTest(unittest.TestCase):
    setUp = StatusTest.setUp   # the same profiles, without running StatusTest's tests again

    def test_the_guard_says_what_it_covers_and_that_it_is_off_in_a_plugin_host(self):
        import sys
        import types

        report = status.build(self.home, now=NOW, config={})
        self.assertEqual(report["runtime_guard"]["state"], "on")
        self.assertIn("LaunchAgent", status.markdown(report, "en"))
        self.assertIn("serviço ou tarefa do Windows", status.markdown(report, "pt"))
        isolation = types.ModuleType("hermes_cli.plugin_isolation")
        isolation.isolation_mode = lambda config=None: "host"
        with patch.dict(sys.modules, {"hermes_cli.plugin_isolation": isolation}):
            report = status.build(self.home, now=NOW, config={})
        self.assertEqual(report["runtime_guard"]["state"], "host")
        self.assertIn("plugins.isolation: host", status.markdown(report, "pt"))



class MigrationStateTest(unittest.TestCase):
    setUp = StatusTest.setUp

    def install(self, profile: str, folder: str, name: str):
        target = self.root / "profiles" / profile / "plugins" / folder
        target.mkdir(parents=True)
        (target / "plugin.yaml").write_text(f"name: {name}\nversion: 0.6.0\n")

    def test_a_profile_with_both_copies_is_told_to_remove_the_old_one_and_restart(self):
        self.install("both-box", "hermes-evolution", "hermes-evolution")
        self.install("both-box", "hermes-atlas-evolution", "hermes-atlas-evolution")
        self.install("in-place", "hermes-evolution", "hermes-atlas-evolution")   # a clone updated in place
        report = status.build(self.home, now=NOW, config={})
        rows = {row["profile"]: row for row in report["profiles"]}
        self.assertEqual((rows["both-box"]["plugin"], rows["in-place"]["plugin"]), ("both", "installed"))
        self.assertEqual(rows["both-box"]["migrate"],
                         "hermes -p both-box plugins remove hermes-evolution && hermes gateway restart")
        self.assertIn("both-box", [row["profile"] for row in report["legacy_installs"]])
        text = status.markdown(report, "pt")
        self.assertIn("duas cópias: a antiga roda", text)
        self.assertIn("hermes -p both-box plugins remove hermes-evolution && hermes gateway restart", text)
        manifest = (Path(__file__).resolve().parents[1] / "plugin.yaml").read_text(encoding="utf-8")
        shipped = re.search(r'^version: "([^"]+)"', manifest, re.M).group(1)
        self.assertEqual(report["version"] if "version" in report else status.plugin_version(), shipped)

    def test_the_new_code_in_a_folder_named_after_the_old_id_is_not_mistaken_for_the_old_plugin(self):
        from hermes_atlas_evolution.cli import legacy_install

        self.install("work", "hermes-evolution", "hermes-atlas-evolution")
        config = {"plugins": {"enabled": ["hermes-evolution"]}}
        self.assertIsNone(legacy_install(self.home, config))
        old = self.root / "profiles" / "work" / "plugins" / "hermes-evolution" / "plugin.yaml"
        old.write_text("name: hermes-evolution\nversion: 0.5.0\n")
        self.assertEqual(legacy_install(self.home, config), "hermes-evolution")


if __name__ == "__main__":
    unittest.main()
