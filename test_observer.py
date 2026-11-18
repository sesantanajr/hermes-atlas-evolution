import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from hermes_atlas_evolution import operations
from hermes_atlas_evolution.operations import database, observe, summarize

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp()


def tool(call, status="ok"):
    return {"turn_id": "turn", "tool_call_id": str(call), "status": status}


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
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


    def test_retention_applies_at_the_learning_pass_prune(self):
        observe(self.home, "tool", tool("old"), now=NOW - 40 * 86400)
        observe(self.home, "tool", tool("kept"), now=NOW - 34 * 86400)
        observe(self.home, "tool", tool("new"), now=NOW)
        with database(self.home) as conn:
            operations._prune(conn, NOW)
            days = [row[0] for row in conn.execute("SELECT day FROM metric ORDER BY day")]
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0], 1)   # delivery ids: 2 days
        self.assertEqual(days, ["2026-10-12", "2026-11-15"])

    @unittest.skipIf(os.name == "nt", "Windows cannot delete a database that is still open")
    @patch.object(operations, "IDENTITY_SECONDS", 0)   # the file's identity is read on every write here
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
