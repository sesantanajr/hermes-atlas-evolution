import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_atlas_evolution.cli import _report, execute
from hermes_atlas_evolution.integration import shared_context, slash
from hermes_atlas_evolution.memory import GLOBAL_SCOPE, MemoryStore
from hermes_atlas_evolution.operations import close_observers, database, observe, recommendations, run_policy, summarize

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp()


class EvolutionTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(close_observers)  # Windows cannot remove a database that is still open.
        self.home = Path(self.folder.name)

    def test_uses_normalized_tokens_and_keeps_payload_out_of_storage(self):
        payload = {
            "turn_id": "canary-turn", "api_request_id": "canary-attempt", "provider": "Example", "model": "A",
            "user_message": "do not store this canary prompt", "request": {"api_key": "api-key-canary"},
            "usage": {"input_tokens": 100, "cache_read_tokens": 200, "cache_write_tokens": 300,
                      "output_tokens": 10, "reasoning_tokens": 4}, "api_duration": 0.5,
        }
        for _ in range(2):
            observe(self.home, "api", payload, now=NOW)
        observe(self.home, "aux", {"aux_task": "compression", "model": "B", "streaming": True}, now=NOW)
        rates = {"example": {"a": {"input": 2, "output": 4, "cache_write": 3, "cache_read": 1}}}
        with database(self.home) as conn:
            report = summarize(conn, days=1, now=NOW, rates=rates)
            backing_store = "\n".join(conn.iterdump())
        self.assertEqual(report["successful_llm_calls"], 2)
        self.assertEqual(report["usage_samples"], 1)
        self.assertEqual(report["priced_calls"], 1)
        self.assertAlmostEqual(report["estimated_usd"], 0.00134)
        for secret in ("canary-turn", "canary-attempt", "canary prompt", "api-key-canary"):
            self.assertNotIn(secret, backing_store)

    def test_failed_operator_tick_attempts_rollback_even_with_invalid_settings(self):
        tick = SimpleNamespace(evolution_action="ops", ops_action="tick")
        with patch("hermes_atlas_evolution.cli._report", side_effect=ValueError("broken settings")), patch(
            "hermes_atlas_evolution.cli._rollback_trial", return_value={"status": "rolled_back"}
        ) as recover:
            with self.assertRaisesRegex(SystemExit, "broken settings\\nActive trial recovery: rolled_back"):
                execute(tick, self.home)
            recover.assert_called_once()

    def test_identifies_repeated_rate_limits_and_high_prompt_budget(self):
        for n in range(5):
            attempt = {"api_request_id": str(n), "turn_id": "rate", "provider": "example", "model": "a"}
            observe(self.home, "api_error", {**attempt, "status_code": 429}, now=NOW)
            observe(self.home, "api", {**attempt, "usage": {"input_tokens": 6000}}, now=NOW)
        with database(self.home) as conn:
            report = summarize(conn, days=1, now=NOW, rates={})
        self.assertEqual({item["code"] for item in recommendations(report)}, {"large_main_prompt", "llm_rate_limit"})

    def test_tool_timeouts_count_as_failed_attempts(self):
        for n in range(10):
            observe(self.home, "tool", {"turn_id": "t", "tool_call_id": str(n),
                                        "status": "timeout" if n < 4 else "ok"}, now=NOW)
        with database(self.home) as conn:
            report = summarize(conn, days=1, now=NOW, rates={})
        self.assertEqual({row["outcome"]: row["requests"] for row in report["groups"]}, {"ok": 6, "timeout": 4})
        finding, = recommendations(report)
        self.assertEqual((finding["code"], finding["errors"], finding["attempts"]), ("tool_errors", 4, 10))

    def test_provider_failures_use_the_hermes_reason_and_auxiliary_error_type(self):
        attempts = (
            ("api_error", {"reason": "timeout", "status_code": None}, "timeout"),
            ("api_error", {"reason": "overloaded", "status_code": 529}, "upstream"),
            ("api_error", {"reason": "upstream_blocked", "status_code": 403}, "upstream"),
            ("api_error", {"reason": "rate_limit", "status_code": None}, "rate_limit"),
            ("api_error", {"reason": "unknown", "status_code": 401}, "auth"),
            ("api_error", {"reason": "format_error", "status_code": 400}, "error"),
            ("aux", {"error": "RateLimitError: PRIVATE provider text", "error_type": "RateLimitError"}, "rate_limit"),
            ("aux", {"error": "APIStatusError: Error code: 503 - PRIVATE", "error_type": "APIStatusError"}, "upstream"),
            ("aux", {"error": "ReadTimeout: PRIVATE", "error_type": "ReadTimeout"}, "timeout"),
            ("aux", {"error": "ValueError: PRIVATE 429", "error_type": "ValueError"}, "error"),
        )
        for index, (event, fields, expected) in enumerate(attempts):
            observe(self.home, event, {"turn_id": "t", "api_request_id": str(index), "provider": f"p{index}",
                                       "model": "m", "aux_task": "compression", **fields}, now=NOW)
        with database(self.home) as conn:
            report = summarize(conn, days=1, now=NOW, rates={})
            self.assertNotIn("PRIVATE", "\n".join(conn.iterdump()))
        outcomes = {row["provider"]: row["outcome"] for row in report["groups"]}
        self.assertEqual(outcomes, {f"p{index}": expected for index, (_, _, expected) in enumerate(attempts)})

    def test_each_failed_attempt_of_one_request_is_counted_once(self):
        # Hermes 0.23.1 reuses "<turn>:api:<n>" for all retries of a request.
        request = {"turn_id": "turn", "api_request_id": "turn:api:1", "provider": "example", "model": "a",
                   "status_code": 429}
        for retry in range(3):
            attempt = {**request, "retry_count": retry, "ended_at": NOW + retry}
            for _ in range(2):
                observe(self.home, "api_error", attempt, now=NOW)
        with database(self.home) as conn:
            report = summarize(conn, days=1, now=NOW, rates={})
        self.assertEqual([(row["outcome"], row["requests"]) for row in report["groups"]], [("rate_limit", 3)])
        self.assertEqual([item["code"] for item in recommendations(report)], ["llm_rate_limit"])

    def test_reused_request_and_tool_call_ids_do_not_hide_real_events(self):
        usage = {"usage": {"input_tokens": 10}, "provider": "example", "model": "a"}
        refunded = {"turn_id": "turn", "api_request_id": "turn:api:2", **usage}
        for ended in (NOW + 1, NOW + 1, NOW + 9):
            observe(self.home, "api", {**refunded, "ended_at": ended}, now=NOW)
        for request in ("turn:api:1", "turn:api:1", "turn:api:2"):
            observe(self.home, "tool", {"turn_id": "turn", "tool_call_id": "call_0", "api_request_id": request,
                                        "status": "ok"}, now=NOW)
        with database(self.home) as conn:
            report = summarize(conn, days=1, now=NOW, rates={})
        self.assertEqual({row["lane"]: row["requests"] for row in report["groups"]}, {"main": 2, "tool": 2})
        self.assertEqual(report["groups"][0]["input_tokens"], 20)

    def test_model_reported_as_a_file_path_is_not_stored(self):
        for index, model in enumerate(("/home/alice/models/q.gguf", "../models/q.gguf", "models//q.gguf", "vendor/q:free",
                                       "C:/Users/alice/q.gguf")):
            observe(self.home, "api", {"provider": f"p{index}", "model": model}, now=NOW)
        with database(self.home) as conn:
            stored = dict(conn.execute("SELECT provider, model FROM metric"))
        self.assertEqual(stored, {"p0": "unknown", "p1": "unknown", "p2": "unknown", "p3": "vendor/q:free", "p4": "unknown"})

    def test_decisions_learn_across_days_and_recover(self):
        def critical():
            return [{"code": "disk", "severity": "critical"}]

        with database(self.home) as conn:
            first = run_policy(conn, critical(), now=NOW)
            self.assertEqual(first["mode"], "conservative")
            conn.execute("UPDATE maintenance SET feedback='helpful' WHERE code='disk'")
            same_day = run_policy(conn, critical(), now=NOW + 600)
            self.assertEqual(same_day["changes"], [])
            self.assertEqual(same_day["findings"][0]["observed_days"], 1)
            next_day = run_policy(conn, critical(), now=NOW + 86400)
            self.assertEqual(next_day["findings"][0]["observed_days"], 2)
            self.assertEqual(next_day["findings"][0]["feedback"], "helpful")
            self.assertEqual(run_policy(conn, [], now=NOW + 87000)["mode"], "conservative")
            self.assertEqual(run_policy(conn, [], now=NOW + 87001)["mode"], "conservative")
            self.assertEqual(run_policy(conn, [], now=NOW + 87600, recovery_ready=False)["mode"], "conservative")
            self.assertEqual(run_policy(conn, [], now=NOW + 88200)["mode"], "conservative")
            self.assertEqual(run_policy(conn, [], now=NOW + 88800)["mode"], "normal")

    def test_policy_keeps_one_decision_per_code_and_survives_unknown_feedback(self):
        reported = [{"code": "disk", "severity": "warning"}, {"code": "disk", "severity": "critical"},
                    {"code": "queue", "severity": "info"}]
        with database(self.home) as conn:
            first = run_policy(conn, [dict(item) for item in reported], now=NOW)
            self.assertEqual([(item["code"], item["severity"]) for item in first["findings"]],
                             [("disk", "critical"), ("queue", "info")])
            self.assertEqual(first["mode"], "conservative")
            conn.execute("UPDATE maintenance SET feedback='written-by-a-newer-version' WHERE code='queue'")
            later = run_policy(conn, [dict(item) for item in reported], now=NOW + 86400)
            self.assertEqual([item["observed_days"] for item in later["findings"]], [2, 2])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM maintenance").fetchone()[0], 2)

    def test_one_auxiliary_finding_per_task_across_models(self):
        for n in range(6):
            observe(self.home, "aux", {"aux_task": "compression", "model": str(n % 2),
                                       "usage": {"input_tokens": 4000}}, now=NOW)
        with database(self.home) as conn:
            findings = recommendations(summarize(conn, days=1, now=NOW, rates={}))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["tokens"], 24_000)

    def test_dm_explicit_storage_isolated_from_users_workspaces_and_group_dms(self):
        def source(**overrides):
            return SimpleNamespace(**({"platform": "telegram", "chat_type": "dm", "chat_id": "1", "user_id": "2",
                                       "scope_id": "first"} | overrides))

        response = slash(self.home, "remember writing Memorável para documentação", source=source())
        self.assertTrue(response.startswith("Saved"))
        self.assertIn("documentação", slash(self.home, "recall memorável", source=source()))
        for other in (source(user_id="3"), source(scope_id="other"), source(chat_id="4")):
            self.assertEqual(slash(self.home, "recall memorável", source=other), "No matching memories.")
        for other in (source(chat_type="group"), source(shared_session=True), source(is_bot=True),
                      source(role_authorized=True), source(thread_id="t")):
            self.assertNotIn("documentação", slash(self.home, "recall memorável", source=other))
        self.assertEqual(slash(self.home, "recall memorável", platform="cli"), "No matching memories.")

    def test_context_only_uses_opted_in_shared_lessons_and_respects_budget(self):
        request = {"user_message": "continuidade avaliação"}
        with database(self.home) as conn:
            store = MemoryStore(conn)
            store.remember("local", "lesson", "continuidade avaliação SECRET")
            store.remember(GLOBAL_SCOPE, "preference", "continuidade avaliação PRIVATE")
            store.remember(GLOBAL_SCOPE, "lesson", "continuidade avaliação SHARED " + '"' * 110)
        self.assertIsNone(shared_context(self.home, request))
        self._settings({"legacy_memory_commands": True, "automatic_shared_recall": True, "daily_estimated_budget_usd": 0.01,
                        "rates_usd_per_million": {"example": {"a": {"input": 20, "output": 0,
                                                                     "cache_read": 0, "cache_write": 0}}}})
        context = shared_context(self.home, request)["context"]
        self.assertIn("SHARED", context)
        self.assertNotIn("SECRET", context)
        self.assertNotIn("PRIVATE", context)
        self.assertLessEqual(len(context), 480)
        self.assertIsNone(shared_context(self.home, {**request, "parent_session_id": "parent"}))
        observe(self.home, "api", {"provider": "example", "model": "a", "usage": {"input_tokens": 2000}}, now=NOW)
        tick = SimpleNamespace(evolution_action="tick", days=7, upstream_tag=None, changes_only=False)
        self.assertEqual(_report(tick, self.home, now=NOW)["decision"]["mode"], "conservative")
        self.assertIsNone(shared_context(self.home, request))

    def _settings(self, value):
        (self.home / "evolution" / "settings.json").write_text(json.dumps(value), encoding="utf-8")

    def test_old_telemetry_pruned_while_explicit_notes_remain(self):
        observe(self.home, "api", {"api_request_id": "old", "turn_id": "old"}, now=NOW - 36 * 86400)
        with database(self.home) as conn:
            note = MemoryStore(conn).remember("local", "lesson", "Mantenha configuração versionada")
            run_policy(conn, [], now=NOW)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM metric").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0], 0)
            self.assertEqual(MemoryStore(conn).search("local", "configuração")[0]["id"], note["id"])

    def test_unknown_resource_does_not_clear_previously_observed_pressure(self):
        tick = SimpleNamespace(evolution_action="tick", days=7, upstream_tag=None, changes_only=False)
        measured = {"host": {"disk": {}, "memory": None, "cgroup_memory": None}, "database": {"status": "ok"},
                    "findings": [{"id": "low_disk", "severity": "warning", "recommendation": "Review disk."}]}
        unknown = {**measured, "host": {**measured["host"], "disk": None}, "findings": []}
        clear = {**measured, "findings": []}
        with patch("hermes_atlas_evolution.cli.snapshot", side_effect=[measured, unknown, clear, clear]):
            modes = [_report(tick, self.home, now=NOW + i * 600)["decision"]["mode"] for i in range(4)]
        self.assertEqual(modes, ["conservative", "conservative", "conservative", "normal"])


if __name__ == "__main__":
    unittest.main()
