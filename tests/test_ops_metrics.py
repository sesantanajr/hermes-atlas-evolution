import unittest

from hermes_evolution.ops_metrics import current_metrics, workload_correlations


class OpsMetricTests(unittest.TestCase):
    def test_queue_baseline_requires_complete_and_api_errors_need_coverage(self):
        host = {"memory": {"total_bytes": 100, "available_bytes": 30},
                "pressure": {"cpu": {"some": {"avg60": 6.2}}}}
        usage = {"groups": [{"lane": "main", "outcome": "ok", "requests": 7},
                            {"lane": "main", "outcome": "timeout", "requests": 3}]}
        workloads = {"kanban": {"status": "partial", "counts": {"ready": 8}}}
        metrics = current_metrics(host, usage, workloads, {})
        self.assertEqual(metrics["api_error_pct"], 30)
        self.assertEqual(metrics["memory_available_pct"], 30)
        self.assertEqual(metrics["cpu_psi_pct"], 6.2)
        self.assertNotIn("queue_depth", metrics)

    def test_tool_timeouts_raise_the_tool_error_rate(self):
        usage = {"groups": [{"lane": "tool", "outcome": "ok", "requests": 6},
                            {"lane": "tool", "outcome": "timeout", "requests": 3},
                            {"lane": "tool", "outcome": "error", "requests": 1},
                            {"lane": "tool", "outcome": "blocked", "requests": 50}]}
        metrics = current_metrics({}, usage, {"kanban": {"status": "not_found", "counts": {}}}, {})
        self.assertEqual(metrics["tool_error_pct"], 40)

    def test_correlation_requires_actionable_coverage_without_authorizing_a_write(self):
        workloads = {"kanban": {"status": "ok", "counts": {"ready": 4}},
                     "cron": {"status": "ok", "schedule": {"peak_scheduled_agent_starts": 3}}}
        diagnostics = [{"code": "kanban_worker_rate_limits", "evidence": [{"value": 5}]}]
        finding, = workload_correlations(workloads, diagnostics)
        self.assertEqual(finding["confidence"], "medium")
        self.assertEqual(finding["action_ids"], ["test_cron_staggering_before_adding_workers"])
        self.assertEqual(workload_correlations({**workloads, "cron": {"status": "partial"}}, diagnostics), [])
