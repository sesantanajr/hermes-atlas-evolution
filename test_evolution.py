import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hermes_atlas_evolution.operations import close_observers, database, observe, summarize

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp()


class EvolutionTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
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






    def _settings(self, value):
        (self.home / "evolution" / "settings.json").write_text(json.dumps(value), encoding="utf-8")




if __name__ == "__main__":
    unittest.main()
