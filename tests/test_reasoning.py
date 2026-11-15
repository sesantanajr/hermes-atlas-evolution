import json
import unittest

from hermes_evolution.reasoning import diagnose


class ReasoningTests(unittest.TestCase):
    def by_code(self, health=None, usage=None, **kwargs):
        return {item["code"]: item for item in diagnose(health or {}, usage or {}, **kwargs)}

    def test_high_file_cache_with_headroom_never_arms_remediation(self):
        host = {
            "memory": {"total_bytes": 100, "free_bytes": 2, "available_bytes": 40, "page_cache_bytes": 50},
            "pressure": {"memory": {"some": {"avg60": 0.1}}},
        }
        findings = self.by_code(host=host)
        self.assertEqual(set(findings), {"page_cache_headroom"})
        self.assertEqual(findings["page_cache_headroom"]["action_ids"], [])

    def test_converging_ram_signals_increase_confidence_independently_of_cache(self):
        host = {
            "memory": {"total_bytes": 100, "available_bytes": 4, "page_cache_bytes": 35},
            "pressure": {"memory": {"some": {"avg60": 4.5}}},
        }
        result = self.by_code(host=host)["memory_reclaim_contention"]
        self.assertEqual(result["confidence"], "high")
        self.assertIn("pause_optional_context", result["action_ids"])
        host["pressure"] = {}
        result = self.by_code(host=host)["memory_reclaim_contention"]
        self.assertEqual(result["confidence"], "medium")
        self.assertNotIn("pause_optional_context", result["action_ids"])

    def test_native_write_capacity_connects_inode_exhaustion_and_logs(self):
        health = {"logs": {"events": {"storage_io": 2, "database_locked": 0}}}
        host = {"disk": {"total_bytes": 100, "free_bytes": 75, "inodes_available": 1, "inodes_total": 100}}
        finding = self.by_code(health, host=host)["native_write_capacity_risk"]
        self.assertEqual(finding["severity"], "critical")
        evidence = {item["signal"]: item["value"] for item in finding["evidence"]}
        self.assertEqual(evidence["profile_disk.free_inode_ratio"], 0.01)
        self.assertEqual(evidence["profile_disk.free_ratio"], 0.75)
        self.assertTrue(finding["alternatives"])

    def test_rebalance_requires_positive_paired_attribution_and_kind_thresholds(self):
        host = {"pressure": {kind: {"some": {"avg60": 20}} for kind in ("cpu", "io")}}
        common = {"attributed": True, "background_units_per_second": 75, "hermes_units_per_second": 25, "share": 0.75}
        cpu = {"kind": "cpu", "pressure_avg10": 12, **common}
        io = {"kind": "io", "pressure_avg10": 5.1, **common}
        findings = self.by_code(host=host, telemetry={"contention_candidates": [cpu, io]})
        self.assertIn("rebalance_background_cpu", findings["cpu_contention"]["action_ids"])
        self.assertIn("rebalance_background_io", findings["io_contention"]["action_ids"])
        for suspected in ({**cpu, "hermes_units_per_second": 0}, {**cpu, "attributed": False},
                          {**cpu, "share": 0.5}, {**cpu, "pressure_avg10": 9.9}):
            result = self.by_code(host=host, telemetry={"contention": suspected})["cpu_contention"]
            self.assertNotIn("rebalance_background_cpu", result["action_ids"])

    def test_research_cache_and_rate_limit_evidence_remain_distinct(self):
        usage = {"groups": [
            {"lane": "main", "outcome": "ok", "requests": 10, "usage_samples": 8,
             "input_tokens": 50_000, "cache_read_tokens": 1_000, "cache_write_tokens": 1_000},
            {"lane": "main", "outcome": "rate_limit", "requests": 4},
            {"lane": "tool", "outcome": "ok", "requests": 8},
            {"lane": "tool", "outcome": "error", "requests": 3},
            {"lane": "tool", "outcome": "blocked", "requests": 98},
        ]}
        research = {"turns": 12, "slow_turns": 4, "p95_ms": 90_000, "limit_ms": 60_000, "max_ms": 100_000,
                    "tool_errors": 4, "tool_calls": 9}
        telemetry = {"research": research, "current_daily_baseline": {"average_prompt_tokens": 6500}}
        result = self.by_code(usage=usage, telemetry=telemetry, history={"average_prompt_tokens": [3000] * 6})
        self.assertEqual(set(result), {"research_slow_turns", "large_prompt_low_reuse", "provider_rate_limit",
                                       "tool_failure_pattern", "daily_baseline_average_prompt_tokens"})
        self.assertIn({"signal": "history.average_prompt_tokens_median", "value": 3000},
                      result["large_prompt_low_reuse"]["evidence"])
        self.assertIn({"signal": "tools.executed_attempts", "value": 11}, result["tool_failure_pattern"]["evidence"])
        self.assertIn({"signal": "main.known_attempts", "value": 14}, result["provider_rate_limit"]["evidence"])
        self.assertNotIn("research_slow_turns", self.by_code(usage=usage))
        self.assertNotIn("daily_baseline_average_prompt_tokens", self.by_code(
            usage=usage, telemetry={"research": research}, history={"average_prompt_tokens": [3000] * 6}))

    def test_single_research_outlier_is_reported_as_low_confidence(self):
        telemetry = {"research": {"turns": 100, "slow_turns": 1, "limit_ms": 60_000,
                                  "p95_ms": 20_000, "max_ms": 100_000, "tool_calls": 20}}
        finding = self.by_code(telemetry=telemetry)["research_slow_turns"]
        self.assertEqual(finding["confidence"], "low")
        self.assertEqual(finding["action_ids"], ["review_research_budget"])

    def test_plugin_skill_and_memory_findings_do_not_copy_raw_text(self):
        health = {"database": {"status": "unexpected_schema"}, "logs": {"events": {"plugin_failure": 3}},
                  "findings": [{"id": "gateway_version_mismatch"}]}
        inventory = {"findings": [
            {"id": "duplicate_skills_exact", "evidence": {"group_count": 2, "body": "private skill text"}},
            {"id": "large_disabled_plugins", "evidence": {"allocated_bytes": 123}},
            {"id": "native_memory_redundancy", "evidence": {"repeated_entries": 4}},
        ]}
        findings = self.by_code(health, inventory=inventory)
        self.assertIn("gateway_runtime_revision_drift", findings)
        self.assertIn("native_state_schema_issue", findings)
        self.assertIn("plugin_fault_pattern", findings)
        self.assertIn("native_memory_redundancy", findings)
        self.assertIn({"signal": "inventory.group_count", "value": 2}, findings["duplicate_skills_exact"]["evidence"])
        self.assertNotIn("private skill text", json.dumps(findings))


if __name__ == "__main__":
    unittest.main()
