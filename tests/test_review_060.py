"""Regression tests for the findings of the 0.6.0 data audit."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from hermes_atlas_evolution import hints, lessons, logwatch, signals
from hermes_atlas_evolution.experience import experience, prune
from hermes_atlas_evolution.operations import close_observers

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp()
DAY = 86_400
DEEP = '{"error": "x", "a":' + "[" * 100_000 + "]" * 100_000 + "}"   # under the size limits, deeper than the parser goes
LINE = ("2026-11-15 10:00:0{n},123 INFO [session-x] agent.background_review: Background review complete: "
        "thread=bg-review calls=1 in=1 out=1 cache_read=1 result=skill\n")


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.addCleanup(close_observers)
        self.home = Path(folder.name)
        (self.home / "logs").mkdir()


class BaselineTest(Base):
    def test_a_baseline_that_reaches_into_unreplayed_history_waits_for_the_backfill(self):
        activated = NOW - 3 * DAY
        for day in range(1, 4):
            signals.record_tool(self.home, {"tool_name": "terminal", "status": "error", "session_id": f"s{day}",
                                            "result": json.dumps({"output": "bash: rg: command not found", "exit_code": 127})},
                                now=activated - day * DAY)
        close_observers()
        with experience(self.home) as conn:
            lesson_id = lessons.derive(conn, now=activated, mode="auto")[0]
            waiting = lessons.evaluate(conn, now=NOW, mode="auto", history_until=activated - 2 * DAY)
            frozen = conn.execute("SELECT baseline_failures FROM lesson WHERE id=?", (lesson_id,)).fetchone()[0]
            self.assertEqual((waiting, frozen), ([], None))
            measured = lessons.evaluate(conn, now=NOW, mode="auto", history_until=None)
            frozen = conn.execute("SELECT baseline_failures FROM lesson WHERE id=?", (lesson_id,)).fetchone()[0]
            self.assertEqual(([item["id"] for item in measured], frozen), ([lesson_id], 3))
            late = lessons.evaluate(conn, now=NOW, mode="auto", history_until=activated - 30 * DAY)   # older history
            self.assertEqual([item["id"] for item in late], [lesson_id])


class RetentionTest(Base):
    def test_failure_excerpts_expire_in_the_daily_prune_and_never_outlive_their_failure(self):
        with experience(self.home) as conn:
            conn.executemany("INSERT INTO failure VALUES (?, 't', 'other', 'h', ?, ?, 3, 2, 'x')",
                             [("fresh", NOW, NOW), ("stale", NOW - 20 * DAY, NOW - 20 * DAY)])
            conn.executemany("INSERT INTO failure_sample VALUES (?, 'excerpt', ?)",
                             [("fresh", NOW - DAY), ("stale", NOW - 20 * DAY), ("orphan", NOW)])
            prune(conn, now=NOW)
            self.assertEqual([row[0] for row in conn.execute("SELECT signature FROM failure_sample")], ["fresh"])


class LiveHookTest(Base):
    def test_a_deeply_nested_tool_result_is_classified_not_raised(self):
        failure = signals.classify("tool_call", "error", None, DEEP, {"calls": "[" * 60_000})
        self.assertEqual(failure["tool"], "tool_call")
        advised = hints.attach(DEEP, "Install rg first.")   # cannot be parsed: the lesson goes after it as text
        self.assertTrue(advised.startswith(DEEP) and advised.endswith("Install rg first."))


class LogwatchTest(Base):
    def drain(self):
        total = 0
        with experience(self.home) as conn:
            for _ in range(30):
                step = logwatch.ingest_reviews(self.home, conn)
                total += step["reviews"]
                if not step.get("more"):
                    break
        return total

    def test_every_file_rotated_since_the_last_run_is_read(self):
        log = self.home / "logs" / "agent.log"
        log.write_text(LINE.format(n=1))
        self.assertEqual(self.drain(), 1)
        with log.open("a") as stream:
            stream.write(LINE.format(n=2))
        log.rename(log.with_name("agent.log.1"))                          # first rotation
        log.write_text(LINE.format(n=3) + LINE.format(n=4))
        log.with_name("agent.log.1").rename(log.with_name("agent.log.2"))   # second rotation before the next run
        log.rename(log.with_name("agent.log.1"))
        log.write_text(LINE.format(n=5))
        self.assertEqual(self.drain(), 4)
        self.assertEqual(self.drain(), 0)

    def test_a_line_longer_than_one_read_is_skipped_and_the_cursor_moves_on(self):
        log = self.home / "logs" / "agent.log"
        with patch.object(logwatch, "_READ_LIMIT", 256):
            log.write_text(LINE.format(n=1) + "x" * 1000 + "\n" + LINE.format(n=2))
            self.assertEqual(self.drain(), 2)
            with log.open("a") as stream:
                stream.write("y" * 600)                                    # still being written: waits
            self.assertEqual(self.drain(), 0)
            with log.open("a") as stream:
                stream.write("\n" + LINE.format(n=3))
            self.assertEqual(self.drain(), 1)
            rotated = log.with_name("agent.log.1")
            log.rename(rotated)
            log.write_text("")
            with rotated.open("a") as stream:                              # a long last line in a rotated file
                stream.write("z" * 700 + LINE.format(n=4))
            self.assertEqual(self.drain(), 0)
            log.write_text(LINE.format(n=5))
            self.assertEqual(self.drain(), 1)



class OwnerIdsTest(Base):
    def test_numeric_owner_ids_from_the_settings_form_are_accepted(self):
        from hermes_atlas_evolution.evolve import options
        from hermes_atlas_evolution.signals import FeedbackSensor

        chosen = options({"evolution": {"owner_sender_ids": [123456789, "abc"]}})
        self.assertEqual(chosen["owner_sender_ids"], ("123456789", "abc"))
        sensor = FeedbackSensor(self.home, owner_sender_ids=(123456789,))
        kinds = sensor.observe({"session_id": "dm", "platform": "telegram", "sender_id": 123456789,
                                "user_message": "não, errado"}, clock=0.0, now=NOW)
        self.assertEqual(kinds, ["correction"])
        with self.assertRaises(ValueError):
            options({"evolution": {"owner_sender_ids": [True]}})


class DurabilityTest(Base):
    def test_macos_gets_full_syncs_like_hermes_state_db(self):
        import sys

        from hermes_atlas_evolution import operations

        with patch.object(sys, "platform", "darwin"):
            with experience(self.home) as conn:
                self.assertEqual(conn.execute("PRAGMA checkpoint_fullfsync").fetchone()[0], 1)
                self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 2)
            observer = operations._observer(self.home)
            self.assertEqual(observer._connect().execute("PRAGMA synchronous").fetchone()[0], 2)
        close_observers()
        if sys.platform != "darwin":   # on a real Mac the full syncs are the point
            with experience(self.home) as conn:   # elsewhere nothing changes
                self.assertEqual(conn.execute("PRAGMA checkpoint_fullfsync").fetchone()[0], 0)



class DigestLabelTest(Base):
    def test_failures_another_plugin_caused_say_they_never_become_lessons(self):
        from hermes_atlas_evolution.report import build, markdown

        stuck = json.dumps({"error": "pre_tool_call plugin callback timed out or is still running"})
        for index in range(3):
            signals.record_tool(self.home, {"tool_name": "read_file", "status": "blocked", "result": stuck,
                                            "error_message": "pre_tool_call plugin callback timed out or is still running",
                                            "session_id": f"s{index}"}, now=NOW)
        close_observers()
        text = markdown(build(self.home, now=NOW, days=7, lang="pt"), "pt")
        self.assertIn("nunca vira lição: outro plugin causou a falha", text)
        self.assertIn("Ciclo de automelhoria", text)


if __name__ == "__main__":
    unittest.main()
