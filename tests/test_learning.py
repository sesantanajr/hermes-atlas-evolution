import json
import sqlite3
import unittest

from hermes_atlas_evolution.learning import action_cooldowns, initialize, record_feedback, record_outcome, retrospective, review


DAY = 86_400
HOUR = 3_600
NOW = 220 * DAY + 8 * HOUR


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_uses_comparable_utc_slots_and_robust_recent_baseline(self):
        for days_ago in range(1, 7):
            now = NOW - days_ago * DAY
            typical = 30_000 if days_ago == 2 else 5_000
            review(self.conn, metrics={"prompt_tokens": typical}, now=now, record=True)
            for _ in range(3):
                review(self.conn, metrics={"prompt_tokens": 50_000}, now=now + 1, record=True)
            review(self.conn, metrics={"prompt_tokens": 40_000}, now=now + 6 * HOUR, record=True)

        high = review(self.conn, metrics={"prompt_tokens": 6_200}, now=NOW, sensitivity="high")
        balanced = review(self.conn, metrics={"prompt_tokens": 6_200}, now=NOW)
        baseline = high["baselines"]["prompt_tokens"]
        self.assertEqual((baseline["samples"], baseline["median"]), (6, 5_000))
        self.assertTrue(baseline["anomalous"])
        self.assertEqual(high["alerts"][0]["code"], "baseline_prompt_tokens")
        self.assertEqual(balanced["alerts"], [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM learning_sample").fetchone()[0], 12)

    def test_low_available_ram_and_insufficient_history(self):
        for days_ago in range(1, 6):
            review(self.conn, metrics={"memory_available_pct": 55}, now=NOW - days_ago * DAY, record=True)
        result = review(self.conn, metrics={"memory_available_pct": 25, "queue_depth": 900}, now=NOW)
        self.assertEqual({row["code"] for row in result["alerts"]}, {"baseline_memory_available_pct"})
        self.assertEqual(result["baselines"]["memory_available_pct"]["direction"], "low")
        self.assertNotIn("threshold", result["baselines"]["queue_depth"])

    def test_deduplication_feedback_and_immediate_critical_escalation(self):
        findings = [{"code": "provider_errors", "severity": "warning"}]
        first = review(self.conn, metrics={}, findings=findings, now=NOW, record=True)
        self.assertEqual(len(first["alerts"]), 1)
        duplicate = review(self.conn, metrics={}, findings=findings, now=NOW + 1, record=True)
        self.assertEqual(duplicate["suppressed"][0]["reason"], "recently_reported")
        record_feedback(self.conn, "provider_errors", "dismissed", now=NOW + 2)
        stale = record_feedback(self.conn, "provider_errors", "reset", now=NOW + 1)
        self.assertEqual(stale["feedback"], "dismissed")
        dismissed = review(self.conn, metrics={}, findings=findings, now=NOW + DAY)
        self.assertEqual(dismissed["suppressed"][0]["reason"], "recently_dismissed")
        critical = [{"code": "provider_errors", "severity": "critical"}]
        escalation = review(self.conn, metrics={}, findings=critical, now=NOW + 3, record=True)
        self.assertEqual(escalation["alerts"][0]["severity"], "critical")
        self.assertFalse(review(self.conn, metrics={}, findings=critical, now=NOW + 4)["alerts"])
        self.assertEqual(len(review(self.conn, metrics={}, findings=critical, now=NOW + HOUR + 3)["alerts"]), 1)
        record_feedback(self.conn, "provider_errors", "reset", now=NOW + DAY)
        self.assertEqual(len(review(self.conn, metrics={}, findings=findings, now=NOW + 2 * DAY)["alerts"]), 1)

    def test_diagnosis_without_a_proposed_action_is_not_a_notice(self):
        from hermes_atlas_evolution.reasoning import diagnose

        host = {"memory": {"total_bytes": 100, "available_bytes": 48, "page_cache_bytes": 32},
                "pressure": {"memory": {"some": {"avg60": 0.0}}}}
        healthy = diagnose({}, {}, host=host)
        self.assertEqual([(item["code"], item["action_ids"]) for item in healthy], [("page_cache_headroom", [])])
        findings = [*healthy, {"code": "queue_review", "severity": "info", "action_ids": ["review_queue"]},
                    {"id": "low_disk", "severity": "warning"}]
        result = review(self.conn, metrics={}, findings=findings, now=NOW, record=True)
        self.assertEqual([item["code"] for item in result["alerts"]], ["low_disk", "queue_review"])
        self.assertEqual(result["suppressed"], [])
        self.assertIsNone(self.conn.execute("SELECT 1 FROM learning_alert WHERE code='page_cache_headroom'").fetchone())

    def test_ineffective_trials_are_replay_safe_and_same_slot_counts_once(self):
        action = "rebalance_cpu"
        self.assertTrue(record_outcome(self.conn, action, 1, "ineffective", now=NOW))
        self.assertFalse(record_outcome(self.conn, action, 1, "improved", now=NOW + 1))
        record_outcome(self.conn, action, 2, "ineffective", now=NOW + 10)
        self.assertEqual(action_cooldowns(self.conn, now=NOW + 12), {})
        record_outcome(self.conn, action, 3, "inconclusive", now=NOW + 6 * HOUR)
        self.assertEqual(action_cooldowns(self.conn, now=NOW + 7 * HOUR), {})
        record_outcome(self.conn, action, 4, "ineffective", now=NOW + 12 * HOUR)
        hold = action_cooldowns(self.conn, now=NOW + 12 * HOUR)[action]
        self.assertEqual((hold["ineffective_intervals"], hold["until"]), (2, NOW + 36 * HOUR))
        self.assertEqual(action_cooldowns(self.conn, now=NOW + 36 * HOUR), {})
        record_outcome(self.conn, action, 5, "improved", now=NOW + 40 * HOUR)
        record_outcome(self.conn, action, 6, "ineffective", now=NOW + 48 * HOUR)
        self.assertEqual(action_cooldowns(self.conn, now=NOW + 48 * HOUR), {})

    def test_retrospective_and_storage_keep_private_fields_out(self):
        findings = [
            {"id": "contention", "severity": "warning", "text": "SECRET-DIAGNOSTIC"},
            {"code": "SECRET-FILE-INPUT", "severity": "critical"},
        ]
        review(self.conn, metrics={"memory_psi_pct": 10, "SECRET-QUERY": 100, "queue_depth": float("nan")},
               findings=findings, now=NOW, record=True)
        record_feedback(self.conn, "contention", "helpful", now=NOW + 1)
        record_outcome(self.conn, "rebalance_cpu", 1, "improved", now=NOW + 2)
        report = retrospective(self.conn, now=NOW + 3)
        self.assertEqual(report["metrics"]["memory_psi_pct"]["mean"], 10)
        self.assertEqual(report["action_outcomes"], {"rebalance_cpu": {"improved": 1}})
        self.assertEqual(report["alerts"][0]["feedback"], "helpful")
        self.assertNotIn("SECRET", json.dumps(report, allow_nan=False))
        self.assertNotIn("SECRET", "\n".join(self.conn.iterdump()))
        with self.assertRaises(ValueError):
            record_feedback(self.conn, "unknown", "helpful", now=NOW + 4)

    def test_review_and_initialize_leave_transaction_with_caller(self):
        initialize(self.conn)
        self.conn.execute("BEGIN")
        self.conn.execute("CREATE TABLE user_owned (id INTEGER)")
        review(self.conn, metrics={"cpu_psi_pct": 4}, now=NOW, record=True)
        self.conn.rollback()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM learning_sample").fetchone()[0], 0)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='user_owned'").fetchone())
