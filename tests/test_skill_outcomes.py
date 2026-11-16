import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hermes_atlas_evolution import experiments, proposals
from hermes_atlas_evolution.experience import day_offset, experience, utc_day
from hermes_atlas_evolution.operations import close_observers
from hermes_atlas_evolution.signals import FeedbackSensor, TurnTracker, record_skill_correction
from hermes_atlas_evolution.skill_health import compare_skill_windows, skill_outcomes, skill_window

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp()
DAY = 86_400


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.addCleanup(close_observers)
        self.home = Path(folder.name)
        self.tracker = TurnTracker(self.home, slow_ms=60_000)
        self.counter = 0

    def turn(self, *, day, skills=(), failed=False, tool_error=False, seconds=5, session="s", platform="cli",
             skill_session=True):
        self.counter += 1
        turn, task = f"turn-{self.counter}", f"task-{self.counter}"
        self.tracker.start({"turn_id": turn, "session_id": session, "task_id": task, "platform": platform}, clock=0.0)
        for skill in skills:
            # skill_view sends session and task; a /skill command sends only the task
            self.tracker.skill({"action": "loaded", "skill_name": skill, "task_id": task,
                                "session_id": session if skill_session else ""})
        if tool_error:
            self.tracker.tool({"turn_id": turn, "tool_call_id": "c", "status": "error"})
        self.tracker.finish({"turn_id": turn, "completed": not failed, "failed": failed}, clock=float(seconds), now=day)


class TrackingTest(Base):
    def test_loaded_skills_are_attached_to_the_open_turn_of_their_session_or_task(self):
        self.turn(day=NOW, skills=("deploy", "notes"), failed=True, tool_error=True)
        self.turn(day=NOW, skills=("deploy",), skill_session=False)          # matched by task only
        self.turn(day=NOW, skills=("deploy",), platform="cron")              # compared with interactive turns only
        self.tracker.skill({"action": "loaded", "skill_name": "orphan", "session_id": "nobody", "task_id": "x"})
        self.tracker.skill({"action": "patched", "skill_name": "deploy", "session_id": "s"})
        self.tracker.start({"turn_id": "child", "session_id": "c", "task_id": "c", "platform": "subagent",
                            "parent_session_id": "p"}, clock=0.0)
        self.tracker.skill({"action": "loaded", "skill_name": "deploy", "session_id": "c", "task_id": "c"})
        self.tracker.finish({"turn_id": "child", "failed": True}, clock=1.0, now=NOW)   # subagents are not attributed
        with experience(self.home) as conn:
            deploy = skill_window(conn, "deploy", utc_day(NOW), utc_day(NOW))
            notes = skill_window(conn, "notes", utc_day(NOW), utc_day(NOW))
            self.assertEqual(skill_window(conn, "orphan", utc_day(NOW), utc_day(NOW))["turns"], 0)
        self.assertEqual((deploy["turns"], deploy["failed_pct"], deploy["tool_error_turns_pct"]), (2, 50.0, 50.0))
        self.assertEqual((notes["turns"], notes["failed_pct"]), (1, 100.0))

    def test_a_correction_is_charged_to_the_skills_of_the_turn_being_corrected(self):
        sensor = FeedbackSensor(self.home)
        self.turn(day=NOW, skills=("report-writer",), session="a")
        kinds = sensor.observe({"session_id": "a", "platform": "cli", "user_message": "Não, está errado, refaça"}, now=NOW)
        self.assertEqual(kinds, ["correction"])
        record_skill_correction(self.home, self.tracker.recent_skills("a", clock=60.0), now=NOW)   # a minute later
        self.assertEqual(self.tracker.recent_skills("other-session"), ())
        self.assertEqual(self.tracker.recent_skills("a", clock=10_000.0), ())   # outside the correction window
        with experience(self.home) as conn:
            self.assertEqual(skill_window(conn, "report-writer", utc_day(NOW), utc_day(NOW))["corrected_pct"], 100.0)

    def test_the_number_of_skills_per_turn_and_open_turns_are_bounded(self):
        self.tracker.start({"turn_id": "big", "session_id": "s", "task_id": "t", "platform": "cli"}, clock=0.0)
        for index in range(40):
            self.tracker.skill({"action": "loaded", "skill_name": f"skill-{index}", "session_id": "s", "task_id": "t"})
        self.tracker.finish({"turn_id": "big", "completed": True}, clock=1.0, now=NOW)
        for index in range(600):
            self.tracker.start({"turn_id": f"open-{index}", "session_id": f"s{index}", "task_id": f"t{index}"}, clock=0.0)
        self.assertLessEqual(len(self.tracker._turns), 512)
        self.assertLessEqual(len(self.tracker._owner), 1024)
        with experience(self.home) as conn:
            count = conn.execute("SELECT COUNT(DISTINCT skill) FROM skill_outcome_daily").fetchone()[0]
        self.assertEqual(count, 16)


class EvaluationTest(Base):
    def test_a_skill_that_goes_worse_than_the_profile_is_flagged_and_proposed_for_review(self):
        for index in range(40):
            self.turn(day=NOW - (index % 5) * DAY, skills=("steady",) if index % 2 else ())
        for index in range(10):
            self.turn(day=NOW - (index % 5) * DAY, skills=("fragile",), failed=index < 4)
        with experience(self.home) as conn:
            outcome = skill_outcomes(conn, start_day=day_offset(NOW, 13), end_day=utc_day(NOW))
            self.assertEqual(outcome["profile"]["turns"], 50)
            self.assertEqual([item["skill"] for item in outcome["findings"]], ["fragile"])
            finding = outcome["findings"][0]
            self.assertEqual((finding["turns"], finding["worse"]), (10, ["failed_pct"]))
            self.assertEqual(finding["skill_rates"], {"failed_pct": 40.0})
            open_items = proposals.refresh(conn, now=NOW, outcomes=outcome)
            self.assertEqual([(item["kind"], item["command"]) for item in open_items],
                             [("skill_outcome", "hermes curator ledger --skill fragile")])

    def test_compare_requires_turns_on_both_sides_and_a_material_difference(self):
        base = {"turns": 20, "failed_pct": 10.0, "interrupted_pct": 0.0, "tool_error_turns_pct": 10.0,
                "corrected_pct": 5.0, "slow_pct": 20.0}
        self.assertEqual(compare_skill_windows(base, {**base, "turns": 3})["verdict"], "insufficient_data")
        self.assertEqual(compare_skill_windows(base, {**base, "failed_pct": 30.0})["verdict"], "regressed")
        self.assertEqual(compare_skill_windows(base, {**base, "failed_pct": 2.0})["verdict"], "improved")
        self.assertEqual(compare_skill_windows(base, {**base, "failed_pct": 12.0})["verdict"], "neutral")
        self.assertEqual(compare_skill_windows(base, {**base, "failed_pct": 2.0, "corrected_pct": 20.0})["verdict"], "mixed")

    def test_a_skill_rewrite_is_judged_by_the_turns_that_loaded_that_skill(self):
        changed = NOW - 9 * DAY
        (self.home / "skills").mkdir()
        entry = {"id": "0000000000aa", "ts": datetime.fromtimestamp(changed, timezone.utc).isoformat(), "actor": "curator",
                 "action": "patch", "skill": "deploy", "evidence": {}, "before": [{"path": "/x/SKILL.md", "sha256": "a" * 64}],
                 "after": [{"path": "/x/SKILL.md", "sha256": "b" * 64}]}
        (self.home / "skills" / ".curator_ledger.jsonl").write_text(json.dumps(entry) + "\n")
        for offset in range(1, 8):
            for index in range(2):
                self.turn(day=changed - offset * DAY, skills=("deploy",))                      # healthy before
                self.turn(day=changed + offset * DAY, skills=("deploy",), failed=index == 0)   # half fail after
                self.turn(day=changed + offset * DAY)                                          # the rest of the profile is fine
        with experience(self.home) as conn:
            experiments.detect_changes(conn, self.home, None, now=changed + 60)
            verdict = experiments.evaluate_changes(conn, None, now=NOW)[0]
            self.assertEqual((verdict["kind"], verdict["subject"], verdict["verdict"], verdict["final"]),
                             ("skill", "deploy", "regressed", True))
            self.assertEqual((verdict["skill_turns_before"], verdict["skill_turns_after"]), (14, 14))
            self.assertEqual(verdict["metrics"]["failed_pct"], {"before": 0.0, "after": 50.0})
            self.assertEqual(verdict["method"], "turns_that_loaded_the_skill_week_before_vs_week_after")
            open_items = proposals.refresh(conn, now=NOW, changes=[verdict])
            self.assertEqual([(item["kind"], item["risk"], item["command"]) for item in open_items],
                             [("skill_rollback", "high", "hermes curator rollback 0000000000aa")])


if __name__ == "__main__":
    unittest.main()
