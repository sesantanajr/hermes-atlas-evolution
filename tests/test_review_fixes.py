"""Regression tests for the findings of the independent security and correctness review (0.4.0)."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from hermes_atlas_evolution import experiments, lessons, proposals
from hermes_atlas_evolution.evolve import cycle, notify_view
from hermes_atlas_evolution.experience import experience, prune
from hermes_atlas_evolution.operations import close_observers
from hermes_atlas_evolution.signals import TurnTracker, record_tool

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp()
DAY = 86_400
MISSING = json.dumps({"error": "tool_call to 'files_search' is missing required argument(s): servico."})
ARGS = {"calls": [{"name": "files_search", "arguments": {}}]}


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.addCleanup(close_observers)
        self.home = Path(folder.name)

    def failures(self, when, times, session="s"):
        for _ in range(times):
            record_tool(self.home, {"tool_name": "tool_call", "status": "error", "result": MISSING, "session_id": session,
                                    "args": ARGS}, now=when)

    def calls(self, when, times):
        for _ in range(times):
            record_tool(self.home, {"tool_name": "files_search", "status": "ok", "result": "{}"}, now=when)


class LessonMeasurementTest(Base):
    def test_an_effective_lesson_stays_effective_after_the_baseline_is_pruned(self):
        activated = NOW - 95 * DAY
        for day in range(1, 15):   # before: 5 failures in 10 calls a day
            self.failures(activated - day * DAY, 5, session=f"b{day}")
            self.calls(activated - day * DAY, 5)
        with experience(self.home) as conn:
            lesson_id = lessons.derive(conn, now=activated, mode="auto")[0]
        for day in range(1, 21):   # after: no failures in the first weeks
            self.calls(activated + day * DAY, 10)
        with experience(self.home) as conn:   # the hourly cycle measures (and freezes the baseline) early on
            first = lessons.evaluate(conn, now=activated + 20 * DAY, mode="auto")[0]
        for day in range(21, 96):
            self.calls(activated + day * DAY, 10)
            if day % 30 == 0:
                self.failures(activated + day * DAY, 1, session=f"a{day}")
        with experience(self.home) as conn:
            prune(conn, now=NOW)   # 90-day retention deletes the baseline days
            later = lessons.evaluate(conn, now=NOW, mode="auto")[0]
            row = conn.execute("SELECT status, baseline_failures, baseline_calls FROM lesson WHERE id=?", (lesson_id,)).fetchone()
        self.assertEqual(first["verdict"], "effective")
        self.assertEqual((later["verdict"], later["retired"]), ("effective", False))
        self.assertEqual((row["status"], row["baseline_failures"], row["baseline_calls"]), ("active", 70, 140))

    def test_per_day_rates_use_the_calendar_days_of_each_window(self):
        activated = NOW - 6 * DAY
        for day in range(1, 5):
            self.failures(activated - day * DAY, 1, session=f"b{day}")   # 4 failures (4 calls) in the 14-day baseline
        with experience(self.home) as conn:
            lessons.derive(conn, now=activated, mode="auto")
        for day in (1, 3, 5):
            self.failures(activated + day * DAY, 1, session=f"a{day}")   # 3 failures in the 6 calendar days after
        with experience(self.home) as conn:
            outcome = lessons.evaluate(conn, now=NOW, mode="auto")[0]
        self.assertEqual((outcome["basis"], outcome["before"], outcome["after"]), ("failures_per_day", round(4 / 14, 4), 0.5))
        self.assertEqual(outcome["verdict"], "ineffective")

    def test_failed_calls_count_as_calls_so_the_rate_is_per_call_when_there_is_volume(self):
        activated = NOW - 8 * DAY
        for day in range(1, 8):
            self.failures(activated - day * DAY, 2, session=f"b{day}")
            self.calls(activated - day * DAY, 2)
        with experience(self.home) as conn:
            lessons.derive(conn, now=activated, mode="auto")
        for day in range(1, 8):
            self.failures(activated + day * DAY, 2, session=f"a{day}")
            self.calls(activated + day * DAY, 2)
        with experience(self.home) as conn:
            outcome = lessons.evaluate(conn, now=NOW, mode="auto")[0]
        self.assertEqual((outcome["basis"], outcome["before"], outcome["after"], outcome["verdict"]),
                         ("failures_per_call", 0.5, 0.5, "ineffective"))


class RegressionProposalTest(Base):
    def turns(self, day, failed):
        tracker = TurnTracker(self.home)
        for index in range(10):
            tracker.start({"turn_id": f"{day}-{index}", "platform": "cli"}, clock=0.0)
            tracker.finish({"turn_id": f"{day}-{index}", "completed": index >= failed, "failed": index < failed},
                           clock=1.0, now=day)

    def test_a_regression_proposal_stays_open_and_is_announced_once(self):
        changed = NOW - 10 * DAY
        with experience(self.home) as conn:
            experiments.detect_changes(conn, self.home, {"model": {"default": "a"}}, now=changed - DAY)
            experiments.detect_changes(conn, self.home, {"model": {"default": "b"}}, now=changed)
        for offset in range(1, 8):
            self.turns(changed - offset * DAY, failed=0)
            self.turns(changed + offset * DAY, failed=4)
        announced = []
        for hour in range(5):
            result = cycle(self.home, now=NOW + hour * 3600, runtime_config={"model": {"default": "b"}}, config={})
            view = notify_view(result)
            announced.append(bool(view and view["regressions"]))
            kinds = [item["kind"] for item in result["open_proposals"]]
            self.assertIn("config_review", kinds, hour)
        self.assertEqual(announced, [True, False, False, False, False])
        with experience(self.home) as conn:
            self.assertEqual(sum(item["kind"] == "config_review" for item in proposals.listing(conn)), 1)


class HookHardeningTest(Base):
    def test_empty_ids_never_carry_a_lesson_to_another_call_or_session(self):
        import sys
        import types

        from hermes_atlas_evolution.integration import register

        for when in (NOW - DAY, NOW):
            self.failures(when, 2, session=f"s{when}")
        with experience(self.home) as conn:
            lessons.derive(conn, now=NOW, mode="auto")
        (self.home / "evolution" / "settings.json").write_text(json.dumps({"evolution": {"lessons": "auto", "tool_hints": True}}))

        class Context:
            hooks, envs = {}, []

            def register_hook(self, name, callback, **_):
                self.hooks.setdefault(name, []).append(callback)

            def register_session_env(self, names, resolver):
                self.envs.append((names, resolver("session-1")))

            def __getattr__(self, name):
                return lambda *args, **kwargs: None

        constants = types.ModuleType("hermes_constants")
        constants.get_hermes_home = lambda: self.home
        constants.get_default_hermes_root = lambda home=None: self.home
        context = Context()
        with patch.dict(sys.modules, {"hermes_constants": constants}):
            register(context)
        self.assertEqual(context.envs, [(["ATLAS_EVOLUTION_AGENT"], {"ATLAS_EVOLUTION_AGENT": "1"})])
        transform = context.hooks["transform_tool_result"][0]
        payload = {"tool_name": "tool_call", "status": "error", "result": MISSING, "args": ARGS}
        self.assertIsNone(transform(**payload, tool_call_id="", session_id=""))    # no session: no hint
        self.assertIsNotNone(transform(**payload, tool_call_id="", session_id="a"))
        self.assertIsNone(transform(**payload, tool_call_id="x", session_id="a"))   # once per session
        self.assertIsNone(transform(status=object(), session_id="b"))              # hostile payload fails open

    def test_an_unusable_folder_keeps_the_event_for_later(self):
        from hermes_atlas_evolution import operations

        record_tool(self.home, {"tool_name": "t", "status": "ok"}, now=NOW)
        observer = operations._observer(self.home)
        with patch.object(operations, "_open", side_effect=OSError("disk full")):
            close_observers()
            with self.assertRaises(OSError):
                record_tool(self.home, {"tool_name": "t", "status": "ok"}, now=NOW)
        self.assertEqual(len(operations._observer(self.home).pending) + len(observer.pending), 1)


if __name__ == "__main__":
    unittest.main()
