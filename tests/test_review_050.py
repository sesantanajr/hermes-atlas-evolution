"""Fixes found while documenting 0.5.0: retention, settings fallback, wording and stored labels."""

import json
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from hermes_atlas_evolution import radar, report
from hermes_atlas_evolution.cli import safe_settings
from hermes_atlas_evolution.experience import day_offset, experience, prune
from hermes_atlas_evolution.operations import close_observers

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp()


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addCleanup(close_observers)
        self.home = Path(folder.name)


class RetentionTest(Base):
    def test_guard_counters_follow_the_retention_window(self):
        with experience(self.home) as conn:
            conn.execute("INSERT INTO guard_daily VALUES (?, 'gateway_stop', 1)", (day_offset(NOW, 120),))
            conn.execute("INSERT INTO guard_daily VALUES (?, 'gateway_stop', 1)", (day_offset(NOW, 1),))
            prune(conn, now=NOW)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM guard_daily").fetchone()[0], 1)


class SettingsFallbackTest(Base):
    def test_an_unreadable_settings_file_keeps_the_form_values_and_turns_lessons_off(self):
        (self.home / "evolution").mkdir()
        (self.home / "evolution" / "settings.json").write_text("{not json")
        runtime = {"plugins": {"entries": {"hermes-atlas-evolution": {"settings": {
            "autopilot_minutes": 0, "runtime_guard": True, "lessons": "auto"}}}}}
        config, problems = safe_settings(self.home, runtime)
        self.assertEqual(problems, ["settings.json"])
        self.assertEqual(config["evolution"]["autopilot_minutes"], 0)   # what the owner set in Hermes still holds
        self.assertEqual(config["evolution"]["lessons"], "off")         # but nothing reaches the agent until fixed
        good, problems = safe_settings(Path(tempfile.mkdtemp(dir=self.home)), runtime)
        self.assertEqual((good["evolution"]["lessons"], problems), ("auto", []))


class WordingTest(unittest.TestCase):
    def test_one_lesson_is_singular(self):
        base = {"proposals": [{"id": 1, "kind": "lesson", "risk": "low", "subject": "terminal",
                               "evidence": {"lesson_id": 4}, "command": "x"}]}
        self.assertIn("1 lesson awaits approval", "\n".join(report._proposal_lines(base, "en")))
        self.assertIn("1 lição aguarda aprovação", "\n".join(report._proposal_lines(base, "pt")))
        base["proposals"].append({**base["proposals"][0], "id": 2})
        self.assertIn("2 lessons await approval", "\n".join(report._proposal_lines(base, "en")))


class StoredLabelsTest(Base):
    def test_end_reasons_and_plugin_names_are_stored_as_safe_labels(self):
        history = sqlite3.connect(self.home / "state.db")
        history.executescript("""CREATE TABLE sessions (id TEXT, parent_session_id TEXT, started_at REAL, end_reason TEXT);""")
        history.executemany("INSERT INTO sessions VALUES (?, NULL, ?, ?)",
                            [(f"s{index}", NOW - 60, "reaped by /home/owner/x <script>") for index in range(3)])
        history.commit()
        history.close()
        plugins = types.ModuleType("hermes_cli.plugins")
        plugins.get_plugin_manager = lambda: types.SimpleNamespace(list_plugins=lambda: [
            {"name": "bad name/../x", "key": "bad name/../x", "enabled": True, "hooks": 1, "error": "Boom: at /secret"}])
        plugins.get_bundled_plugins_dir = lambda: None
        with patch.dict(sys.modules, {"hermes_cli.plugins": plugins}), experience(self.home) as conn:
            found = radar.scan(self.home, conn, now=NOW)
            stored = conn.execute("SELECT value FROM cursor WHERE key='radar_plugins'").fetchone()[0]
        self.assertNotIn("/", json.dumps(found["sessions"]["by_reason"]))
        self.assertNotIn("/", stored)
        self.assertNotIn("secret", stored)


if __name__ == "__main__":
    unittest.main()
