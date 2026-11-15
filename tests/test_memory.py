import sqlite3
import unittest

from hermes_evolution.memory import GLOBAL_SCOPE, MemoryStore, make_scope


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.store = MemoryStore(self.connection)
        self.alice = make_scope("Telegram", "alice")
        self.bob = make_scope("Telegram", "bob")

    def test_deduplicates_in_one_category_and_merges_provenance(self):
        first = self.store.remember(self.alice, "writing", "Escrever em português.", confidence=0.4)
        second = self.store.remember(
            self.alice, "writing", "  ESCREVER em português!  ", tags=("pt-BR",), source="feedback", confidence=0.8
        )
        for scope, category in ((self.bob, "writing"), (self.alice, "preference")):
            separate = self.store.remember(scope, category, first["text"])
            self.assertNotEqual(first["id"], separate["id"])

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["text"], second["text"])
        self.assertEqual(second["sources"], ["feedback", "manual"])
        self.assertEqual(second["tags"], ["pt-BR"])
        self.assertEqual(second["confidence"], 0.8)

    def test_exact_and_semantic_ranking_with_cross_category_links(self):
        exact = self.store.remember(self.alice, "workflow", "Implantação do Observatório.", tags=("Observatório",))
        related = self.store.remember(
            self.alice, "lesson", "Deployment do Observatório começa na homologação.", tags=("Observatório",)
        )
        self.store.remember(self.alice, "workflow", "Implantação do arquivo.")
        self.store.remember(self.alice, "research", "Pesquisar planilhas históricas.")

        direct = self.store.search(self.alice, "IMPLANTAÇÃO do Observatório")
        self.assertEqual(direct[0]["id"], exact["id"])
        self.assertTrue(direct[0]["exact_match"])
        self.assertIn(related["id"], direct[0]["connections"])

        semantic = self.store.search(self.alice, "deployment observatorio", categories=("workflow", "lesson"))
        self.assertEqual({item["id"] for item in semantic[:2]}, {exact["id"], related["id"]})
        self.assertEqual([item["matched_terms"] for item in semantic], [2, 2, 1])
        self.assertEqual(self.store.search(self.alice, "planilhas", categories=("lesson",)), [])

    def test_private_scopes_global_opt_in_and_forget(self):
        private = self.store.remember(self.alice, "project", "Projeto Aurora")
        public = self.store.remember(GLOBAL_SCOPE, "project", "Projeto Aurora aberto")
        scopes = (self.bob, make_scope("Discord", "alice"))

        for scope in scopes:
            self.assertEqual(self.store.search(scope, "aurora"), [])
            self.assertFalse(self.store.forget(scope, private["id"]))
            self.assertEqual([item["id"] for item in self.store.search(scope, "aurora", include_global=True)], [public["id"]])

        self.assertEqual(self.store.audit(self.alice)["total"], 1)
        self.assertEqual(self.store.audit(self.alice, include_global=True)["total"], 2)
        self.assertFalse(self.store.forget(self.alice, public["id"]))
        self.assertTrue(self.store.forget(self.alice, private["id"]))
        self.assertEqual(self.store.search(self.alice, "aurora"), [])
        self.assertEqual(self.store.search(GLOBAL_SCOPE, "aurora")[0]["id"], public["id"])
        self.assertIsNone(
            self.connection.execute("SELECT 1 FROM evolution_memory_terms WHERE memory_id = ?", (private["id"],)).fetchone()
        )
        with self.assertRaises(ValueError):
            self.store.search(self.alice, "aurora", include_global="false")

    def test_people_profiles_require_explicit_private_promotion(self):
        with self.assertRaises(ValueError):
            self.store.remember(self.alice, "person", "Uma preferência aprovada.")
        with self.assertRaises(ValueError):
            self.store.remember(GLOBAL_SCOPE, "person", "Uma preferência aprovada.", promote=True)
        item = self.store.remember(self.alice, "person", "Uma preferência aprovada.", promote=True)
        self.assertEqual(self.store.search(self.alice, "preference")[0]["id"], item["id"])
        self.assertEqual(self.store.search(self.bob, "preference", include_global=True), [])

    def test_audit_reports_quality_without_including_content(self):
        first = self.store.remember(self.alice, "lesson", "Usar fila somente para eventos.", confidence=0.3)
        self.store.remember(self.alice, "workflow", first["text"])
        self.connection.execute(
            "UPDATE evolution_memories SET updated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (first["id"],)
        )
        self.connection.execute("DELETE FROM evolution_memory_terms WHERE memory_id = ?", (first["id"],))
        self.assertEqual(
            self.store.audit(self.alice),
            {
                "total": 2,
                "categories": {"lesson": 1, "workflow": 1},
                "quality": {
                    "low_confidence": 1,
                    "stale_180_days": 1,
                    "unindexed": 1,
                    "repeated_across_categories": 1,
                },
            },
        )

    def test_scope_derivation_excludes_the_raw_identity(self):
        self.assertEqual(make_scope(" Telegram ", "alice"), self.alice)
        self.assertNotIn("alice", self.alice)
        for user in ("", "   ", None, True):
            with self.subTest(user=user), self.assertRaises(ValueError):
                make_scope("Telegram", user)

    def test_labeled_retrieval_metrics_at_cutoff(self):
        exact = self.store.remember(self.alice, "project", "Implantação Nebulosa")
        related = self.store.remember(self.alice, "lesson", "Deployment Nebulosa começa em teste")
        unreachable = self.store.remember(self.alice, "writing", "Escrever usando exemplos curtos")
        report = self.store.evaluate(self.alice, [
            {"query": "Implantação Nebulosa", "expected_ids": [exact["id"], related["id"], unreachable["id"]]},
            {"query": "Implantação Nebulosa", "expected_ids": [related["id"], related["id"]]},
            {"query": "imposto inexistente", "expected_ids": [exact["id"]]},
        ], limit=2)
        self.assertEqual(report["case_count"], 3)
        self.assertAlmostEqual(report["hit_at_k"], 2 / 3)
        self.assertAlmostEqual(report["recall_at_k"], (2 / 3 + 1) / 3)
        self.assertAlmostEqual(report["mrr"], 0.5)
        self.assertEqual([case["first_relevant_rank"] for case in report["cases"]], [1, 2, None])
        self.assertEqual([case["case_index"] for case in report["cases"]], [1, 2, 3])
        self.assertNotIn("Nebulosa", str(report))

    def test_retrieval_evaluation_checks_scope_and_requires_labels(self):
        other = self.store.remember(self.bob, "project", "Projeto Privado")
        public = self.store.remember(GLOBAL_SCOPE, "project", "Projeto documentado")
        for expected_id in (other["id"], 9999):
            with self.subTest(expected_id=expected_id), self.assertRaisesRegex(ValueError, "visible memory items"):
                self.store.evaluate(self.alice, [{"query": "projeto", "expected_ids": [expected_id]}], include_global=True)
        case = {"query": "projeto", "expected_ids": [public["id"]]}
        with self.assertRaisesRegex(ValueError, "visible memory items"):
            self.store.evaluate(self.alice, [case])
        self.assertEqual(self.store.evaluate(self.alice, [case], include_global=True)["mrr"], 1.0)
        for malformed in ([], [case] * 101, [{"query": "!", "expected_ids": [public["id"]]}], [{"query": "projeto", "expected_ids": [True]}]):
            with self.subTest(malformed=repr(malformed)[:80]), self.assertRaises(ValueError):
                self.store.evaluate(self.alice, malformed, include_global=True)


if __name__ == "__main__":
    unittest.main()
