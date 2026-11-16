import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from hermes_atlas_evolution.inventory import scan_inventory


NOW = 1_795_000_000
SCHEMA = """CREATE TABLE messages (
    id INTEGER PRIMARY KEY, tool_name TEXT, role TEXT, active INTEGER, timestamp REAL, content TEXT
)"""


class InventoryTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.home = Path(scratch.name) / "profile"
        self.source = Path(scratch.name) / "bundled"

    def write(self, root, relative, content):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_deduplicates_skill_documents_and_flags_variant_and_body_matches(self):
        body = "Start the provider and check status. Confirm each step in the report. " * 4
        skill = f"---\nname: routing\n---\n{body}"
        self.write(self.home, "skills/network/routing/SKILL.md", skill)
        self.write(self.source, "skills/routing/SKILL.md", skill)
        self.write(self.home, "skills/copy/SKILL.md", f"---\nname: routing-copy\n---\n{body}")
        self.write(self.home, "skills/variant/SKILL.md", "---\nname: routing\n---\nDifferent execution procedure")
        self.write(self.home, "skills/network/routing/assets/example/SKILL.md", "fixture ignored")
        self.write(self.home, "skills/node_modules/example/SKILL.md", "fixture ignored")
        result = scan_inventory(self.home, source=self.source)
        skills = result["skills"]
        self.assertEqual(skills["inspected"], 4)
        self.assertEqual(skills["identical_groups"][0]["copies"], 2)
        self.assertEqual(skills["name_collisions"][0]["versions"], 2)
        self.assertEqual(skills["body_duplicates"][0]["names"], ["routing", "routing-copy"])
        self.assertNotIn(body, json.dumps(result))

    def test_plugin_gate_semantics_and_footprints(self):
        self.write(self.home, "plugins/ops/helper/plugin.yaml", "name: declared\nprovides_tools: [bench, health]\n")
        self.write(self.home, "plugins/ops/helper/data", "12345")
        self.write(self.home, "plugins/feature/plugin.yaml", 'name: "feature"\nprovides_tools:\n  - reports # comment\n')
        self.write(self.home, "plugins/legacy/plugin.yaml", "name: legacy\n")
        self.write(self.home, "plugins/legacy/__init__.py", "# Hermes detects MemoryProvider in the prefix.\n")
        self.write(self.home, "plugins/memory/onnx/plugin.yaml", "name: onnx\n")
        self.write(self.source, "plugins/feature/plugin.yaml", "name: feature\n")
        unknown = scan_inventory(self.home)
        unknown_topology = {item["name"]: item["state"] for item in unknown["plugins"]["entries"]}
        self.assertEqual(unknown_topology["ops/helper"], "unknown")
        result = scan_inventory(self.home, source=self.source, enabled_plugins={"declared", "feature"},
                                disabled_plugins={"ops/helper", "legacy", "onnx"})
        plugins = {(row["origin"], row["name"]): row for row in result["plugins"]["entries"]}
        nested = plugins["profile", "ops/helper"]
        self.assertEqual(nested["state"], "explicitly_disabled")
        self.assertEqual(nested["tools"], ["bench", "health"])
        self.assertGreater(nested["bytes"], 5)
        self.assertEqual(plugins["profile", "feature"]["state"], "explicitly_enabled")
        self.assertEqual(plugins["profile", "feature"]["tools"], ["reports"])
        self.assertEqual(plugins["profile", "legacy"]["state"], "separate_loader")
        self.assertEqual(plugins["profile", "memory/onnx"]["state"], "separate_loader")
        self.assertIn(("bundled", "feature"), plugins)

    def test_saved_memory_structure_excludes_payloads(self):
        secret = "unique parsed literal"
        self.write(self.home, "memories/MEMORY.md", f"{secret}\n§\n\n§\n{secret.upper()}")
        self.write(self.home, "memories/USER.md", secret)
        result = scan_inventory(self.home, memory_limits={"memory_char_limit": 12})
        data = result["native_memory"]
        memory = data["files"]["MEMORY.md"]
        self.assertEqual(memory["entries"], 2)
        self.assertEqual(memory["empty_sections"], 1)
        self.assertEqual(memory["duplicates"], 1)
        self.assertEqual(memory["oversized_entries"], 2)
        self.assertEqual(memory["limit_source"], "configured")
        self.assertEqual(data["cross_file_duplicates"], 1)
        self.assertNotIn(secret, json.dumps(result))
        self.assertIn("native_memory_redundancy", {finding["id"] for finding in result["findings"]})

    def test_only_recent_active_tool_names_with_unique_declarations(self):
        self.write(self.home, "plugins/alpha/plugin.yaml", "name: alpha\nprovides_tools: [unique, shared]\n")
        self.write(self.home, "plugins/beta/plugin.yaml", "name: beta\nprovides_tools: [shared]\n")
        self.write(self.home, "plugins/gamma/plugin.yaml", "name: gamma\nprovides_tools: [nomatch]\n")
        dbpath = self.home / "state.db"
        with sqlite3.connect(dbpath) as db:
            db.execute(SCHEMA)
            db.executemany("INSERT INTO messages(tool_name,role,active,timestamp,content) VALUES (?,?,?,?,?)", (
                ("unique", "tool", 1, NOW - 10, "sensitive native payload"),
                ("unique", "tool", 0, NOW - 10, "rewound"),
                ("unique", "tool", 1, NOW - 20 * 86400, "old"),
                ("unique", "assistant", 1, NOW - 10, "not a result"),
                ("shared", "tool", 1, NOW - 10, "attribution ambiguous"),
            ))
        db.close()
        before = dbpath.read_bytes()
        result = scan_inventory(self.home, now=NOW)
        entries = {p["name"]: p["recent_matches"] for p in result["plugins"]["entries"]}
        self.assertEqual(entries, {"alpha": 1, "beta": None, "gamma": 0})
        self.assertEqual(result["plugins"]["usage"]["rows_sampled"], 5)
        self.assertNotIn("sensitive native payload", json.dumps(result))
        self.assertEqual(dbpath.read_bytes(), before)
        self.assertFalse(dbpath.with_name("state.db-wal").exists())

    def test_avoids_symlinks_and_bounded_footprints_are_reported(self):
        self.write(self.home, "plugins/one/plugin.yaml", "name: one\n")
        self.write(self.home, "plugins/one/large", "1234")
        external = self.write(self.source, "outside/SKILL.md", "shared file outside selected roots")
        symlink = self.home / "skills" / "link"
        symlink.parent.mkdir()
        try:
            symlink.symlink_to(external.parent, target_is_directory=True)
        except (NotImplementedError, OSError):
            pass
        with patch("hermes_atlas_evolution.inventory._PLUGIN_FILES_LIMIT", 1):
            result = scan_inventory(self.home)
        self.assertEqual(result["skills"]["inspected"], 0)
        self.assertFalse(result["plugins"]["entries"][0]["footprint_complete"])

    def test_reads_bounded_curator_usage_without_unrelated_fields(self):
        self.write(self.home, "skills/.usage.json", json.dumps({
            "old": {"state": "stale", "last_used_at": "2020-04-01T10:00:00Z", "pinned": True, "private": "unreported"},
            "new": {"state": "active", "last_used_at": "2026-11-10T12:00:00Z"},
            "bad": [1, 2],
        }))
        result = scan_inventory(self.home, now=NOW)
        usage = result["skills"]["curator_usage"]
        self.assertEqual(usage["recent_usage_records"], 1)
        self.assertEqual(usage["pinned_records"], 1)
        self.assertEqual(usage["states"], {"stale": 1, "active": 1})
        self.assertEqual(usage["unknown_record_shapes"], 1)
        self.assertNotIn("unreported", json.dumps(result))

    def test_flags_large_explicitly_disabled_copy_without_claiming_reclaimable_space(self):
        self.write(self.home, "plugins/bulky/plugin.yaml", "name: bulky\nkind: standalone\n")
        self.write(self.home, "plugins/bulky/aux-data", "test data to exceed reduced threshold")
        self.write(self.home, "plugins/current/plugin.yaml", "name: current\nkind: standalone\n")
        with patch("hermes_atlas_evolution.inventory._MIB", 1):
            result = scan_inventory(self.home, enabled_plugins={"current"}, disabled_plugins={"bulky"})
        finding = next(item for item in result["findings"] if item["id"] == "large_disabled_plugins")
        self.assertEqual(finding["evidence"], {"count": 1, "plugins": ["bulky"]})
        self.assertNotIn("unused", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
