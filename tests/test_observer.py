import gc
import os
import sqlite3
import tempfile
import threading
import unittest
import weakref
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from hermes_evolution import operations
from hermes_evolution.native_signals import native_summary, observe_native
from hermes_evolution.operations import database, observe, recommendations, summarize
from hermes_evolution.telemetry import ResearchTracker, research_summary

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp()


def tool(call, status="ok"):
    return {"turn_id": "turn", "tool_call_id": str(call), "status": status}


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(operations.close_observers)
        self.home = Path(self.folder.name)

    def usage(self, now=NOW):
        with database(self.home) as conn:
            return summarize(conn, days=1, now=now, rates={})

    def tools(self):
        return sum(row["requests"] for row in self.usage()["groups"] if row["lane"] == "tool")

    def writer(self):
        other = sqlite3.connect(self.home / "evolution" / "evolution.sqlite3", isolation_level=None)
        self.addCleanup(other.close)
        other.execute("BEGIN IMMEDIATE")
        return other

    def test_events_recorded_while_another_writer_holds_the_database_are_kept(self):
        observe(self.home, "tool", tool("first"), now=NOW)
        other = self.writer()
        observe(self.home, "tool", tool("held"), now=NOW)
        observe(self.home, "tool", tool("held"), now=NOW)
        observe_native(self.home, "subagent_stop", {"child_status": "completed", "duration_ms": 5}, now=NOW)
        tracker = ResearchTracker(self.home)
        tracker.start({"turn_id": "turn"}, clock=1)
        tracker.tool({"turn_id": "turn", "tool_name": "web_search", "tool_call_id": "w", "status": "ok"})
        tracker.finish({"turn_id": "turn"}, clock=3, now=NOW)
        other.execute("ROLLBACK")
        observe(self.home, "tool", tool("after"), now=NOW)
        self.assertEqual(self.tools(), 3)
        with database(self.home) as conn:
            self.assertEqual(native_summary(conn, now=NOW, days=1)["groups"][0]["events"], 1)
            self.assertEqual(research_summary(conn, now=NOW, days=1)["turns"], 1)
        self.assertEqual(self.usage()["dropped_events"], 0)

    def test_waiting_events_hold_no_reference_to_the_hook_payload(self):
        class Payload(dict):
            pass

        observe(self.home, "tool", tool("first"), now=NOW)
        other = self.writer()
        payload = Payload(tool("held"), result="PRIVATE tool output", args={"text": "PRIVATE"})
        released = weakref.ref(payload)
        observe(self.home, "tool", payload, now=NOW)
        observe_native(self.home, "subagent_stop", payload, now=NOW)
        del payload
        gc.collect()
        self.assertEqual(len(operations._observer(self.home).pending), 2)
        self.assertIsNone(released())
        other.execute("ROLLBACK")

    def test_waiting_events_are_written_when_the_process_ends(self):
        observe(self.home, "tool", tool("first"), now=NOW)
        other = self.writer()
        observe(self.home, "tool", tool("held"), now=NOW)
        other.execute("ROLLBACK")
        operations.close_observers()
        self.assertEqual(self.tools(), 2)

    def test_hook_threads_of_one_process_do_not_lose_events(self):
        failures = []

        def worker(name):
            for call in range(40):
                try:
                    observe(self.home, "tool", tool(f"{name}-{call}"), now=NOW)
                except sqlite3.Error as error:
                    failures.append(error)

        threads = [threading.Thread(target=worker, args=(name,)) for name in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        operations.close_observers()
        self.assertEqual((failures, self.tools()), ([], 320))

    def test_overflow_is_bounded_and_reported_as_dropped_events(self):
        observe(self.home, "tool", tool("first"), now=NOW)
        other = self.writer()
        with patch.object(operations, "_OBSERVER_QUEUE", 4):
            for call in range(16):
                observe(self.home, "tool", tool(call), now=NOW)
        other.execute("ROLLBACK")
        observe(self.home, "tool", tool("after"), now=NOW)
        report = self.usage()
        self.assertEqual((self.tools(), report["dropped_events"]), (6, 12))
        self.assertEqual({row["lane"] for row in report["groups"]}, {"tool"})
        self.assertEqual([(item["code"], item["count"]) for item in recommendations(report)],
                         [("observer_dropped_events", 12)])

    def test_replaced_database_and_forked_process_get_a_new_connection(self):
        observe(self.home, "tool", tool("first"), now=NOW)
        first = operations._observer(self.home).connection
        observe(self.home, "tool", tool("second"), now=NOW)
        self.assertIs(operations._observer(self.home).connection, first)
        for item in (self.home / "evolution").iterdir():
            item.unlink()
        observe(self.home, "tool", tool("third"), now=NOW)
        second = operations._observer(self.home).connection
        self.assertIsNot(second, first)
        self.assertEqual(self.tools(), 1)
        with patch.object(os, "getpid", return_value=os.getpid() + 1):
            observe(self.home, "tool", tool("child"), now=NOW)
            self.assertIsNot(operations._observer(self.home).connection, second)
        second.execute("SELECT 1")  # The parent's handle was left open for the parent.
        self.assertEqual(self.tools(), 2)

    def test_database_errors_still_reach_the_caller_and_do_not_poison_later_writes(self):
        observe(self.home, "tool", tool("first"), now=NOW)
        with database(self.home) as conn:
            conn.execute("ALTER TABLE metric RENAME TO metric_away")
        with self.assertRaises(sqlite3.Error):
            observe(self.home, "tool", tool("broken"), now=NOW)
        with database(self.home) as conn:
            conn.execute("ALTER TABLE metric_away RENAME TO metric")
        observe(self.home, "tool", tool("recovered"), now=NOW)
        self.assertEqual(self.tools(), 2)

    def test_shared_connection_serves_readers_and_keeps_their_writes(self):
        with database(self.home, observer=True) as conn:
            conn.execute("INSERT INTO control VALUES ('probe', 'kept')")
            observe(self.home, "tool", tool("nested"), now=NOW)
        with database(self.home, observer=True) as conn:
            self.assertEqual(conn.execute("SELECT value FROM control WHERE key='probe'").fetchone()[0], "kept")
        with self.assertRaises(RuntimeError):
            with database(self.home, observer=True) as conn:
                conn.execute("INSERT INTO control VALUES ('undone', 'x')")
                raise RuntimeError("caller failed")
        with database(self.home) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM control WHERE key='undone'").fetchone())
        self.assertEqual(self.tools(), 1)


if __name__ == "__main__":
    unittest.main()
