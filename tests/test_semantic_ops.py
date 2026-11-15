import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hermes_evolution.semantic_ops import match_incidents


def incident(code, text, recent=1, **extra):
    return {"code": code, "key_text": text, "occurrences": 2, "last_day": "2026-11-15",
            "last_seen": recent, "severity": "warning", **extra}


class SemanticOpsTest(unittest.TestCase):
    def setUp(self):
        self.records = [incident("background_index_job", "Long indexing and slow storage read", secret="raw logs"),
                        incident("tool_failures", "Repeated network authentication failures"),
                        incident("cpu_wait", "CPU stall caused by concurrent background jobs")]

    def valid_model(self):
        model_dir = tempfile.TemporaryDirectory()
        self.addCleanup(model_dir.cleanup)
        model = Path(model_dir.name)
        (model / "config.json").write_text("{}", encoding="utf-8")
        (model / "model.safetensors").write_bytes(b"mock weights")
        (model / "1_Pooling").mkdir()
        (model / "modules.json").write_text(json.dumps([
            {"type": "sentence_transformers.models.Transformer", "path": ""},
            {"type": "sentence_transformers.models.Pooling", "path": "1_Pooling"},
        ]), encoding="utf-8")
        return model

    def test_lexical_similarity_understands_accented_portuguese_and_english(self):
        answer = match_incidents("lentidão de disco após indexação", self.records)
        self.assertEqual(answer["backend"], "lexical")
        self.assertIsNone(answer["fallback_reason"])
        self.assertEqual(answer["results"][0]["code"], "background_index_job")
        self.assertNotIn("raw logs", json.dumps(answer))
        self.assertNotIn("lentidão de disco", json.dumps(answer, ensure_ascii=False))

    def test_deterministic_ties_and_filtering(self):
        rows = [None, incident("attempt_b", "provider failure", 5), incident("attempt_a", "provider failure", 6),
                {**incident("attempt_u", "provider failure"), "last_seen": float("inf")}]
        answer = match_incidents("provedor falha", rows, limit=1)
        self.assertEqual([row["code"] for row in answer["results"]], ["attempt_a"])
        self.assertEqual(match_incidents("zzz", rows)["results"], [])
        for kwargs in ({"limit": True}, {"limit": 21}, {"model_path": "hub/model"}):
            with self.assertRaises(ValueError):
                match_incidents("problem", rows, **kwargs)

    def test_neural_matching_is_local_cpu_only_and_capped(self):
        model = self.valid_model()
        engine = Mock(max_seq_length=900)
        engine.encode.return_value = [[1, 0], [0, 1], [0.8, 0.6], [0, 1]]
        factory = Mock(return_value=engine)
        module = SimpleNamespace(SentenceTransformer=factory)
        with patch.dict(sys.modules, {"sentence_transformers": module}):
            answer = match_incidents("a phrase without lexical matches", self.records, model_path=model)
        self.assertEqual(answer["backend"], "sentence_transformer")
        self.assertEqual(answer["results"][0]["code"], "tool_failures")
        self.assertIsNone(answer["fallback_reason"])
        factory.assert_called_once_with(str(model.resolve()), device="cpu", local_files_only=True,
                                        trust_remote_code=False, token=False, model_kwargs={"use_safetensors": True})
        self.assertEqual(engine.max_seq_length, 192)
        self.assertEqual(engine.encode.call_args.kwargs["batch_size"], 8)

    def test_fallback_reports_unavailable_dependency_or_bad_embedding(self):
        model = self.valid_model()
        with patch.dict(sys.modules, {"sentence_transformers": None}):
            result = match_incidents("indexação", self.records, model_path=model)
        self.assertEqual(result["fallback_reason"], "sentence_transformers_unavailable")
        self.assertEqual(result["results"][0]["code"], "background_index_job")
        engine = Mock(max_seq_length=192)
        engine.encode.return_value = [[float("nan")], [1], [1], [1]]
        with patch.dict(sys.modules, {"sentence_transformers": SimpleNamespace(SentenceTransformer=Mock(return_value=engine))}):
            result = match_incidents("indexação", self.records, model_path=model)
        self.assertEqual(result["backend"], "lexical")
        self.assertEqual(result["fallback_reason"], "neural_backend_failed")

    def test_custom_modules_and_invalid_model_paths_are_not_loaded(self):
        model = self.valid_model()
        (model / "modules.json").write_text('[{"type":"local.untrusted.Code","path":""}, {}]', encoding="utf-8")
        loader = Mock()
        with patch.dict(sys.modules, {"sentence_transformers": SimpleNamespace(SentenceTransformer=loader)}):
            for path in (Path("remote/identifier"), model, model / "missing"):
                item = match_incidents("indexação", self.records, model_path=path)
                self.assertEqual(item["backend"], "lexical")
                self.assertEqual(item["fallback_reason"], "invalid_local_model")
            loader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
