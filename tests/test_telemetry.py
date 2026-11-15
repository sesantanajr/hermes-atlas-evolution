import tempfile
import unittest
from pathlib import Path

from hermes_evolution.operations import close_observers, database
from hermes_evolution.telemetry import ResearchTracker, research_summary


class ResearchTrackerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(close_observers)
        self.home = Path(self.folder.name)

    def summary(self, now=2000):
        with database(self.home) as conn:
            return research_summary(conn, now=now, days=1)

    def test_turn_whose_research_tools_all_timed_out_is_measured(self):
        turn = {"turn_id": "turn"}
        tracker = ResearchTracker(self.home)
        tracker.start(turn, clock=10)
        for call in ("a", "b"):
            tracker.tool({**turn, "tool_name": "web_search", "tool_call_id": call, "status": "timeout"})
        tracker.tool({**turn, "tool_name": "web_extract", "tool_call_id": "c", "status": "cancelled"})
        tracker.finish(turn, clock=260, now=2000)
        measured = self.summary()
        self.assertEqual((measured["turns"], measured["slow_turns"]), (1, 1))
        self.assertEqual((measured["tool_calls"], measured["tool_errors"]), (2, 2))


if __name__ == "__main__":
    unittest.main()
