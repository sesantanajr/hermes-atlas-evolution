"""0.8.0: the Evolution panel in the Hermes dashboard reads the profile's data and decides nothing."""

import importlib.util
import json
import re
import tempfile
import time
import unittest
from pathlib import Path

from hermes_atlas_evolution import lessons
from hermes_atlas_evolution.experience import experience
from hermes_atlas_evolution.operations import close_observers

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "dashboard"


def load_api():
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_test", DASHBOARD / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PanelTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.home = self.root / "profiles" / "work"
        self.home.mkdir(parents=True)
        self.api = load_api()
        self.addCleanup(close_observers)
        self.addCleanup(lambda: self.api._module("operations").close_observers())

    def test_the_snapshot_shows_status_digest_lessons_and_proposals_of_the_profile(self):
        with experience(self.home) as conn:
            lessons.add_custom(conn, "When the CSV import fails on encoding, convert it to UTF-8 with iconv.",
                               tool="terminal", origin="operator", now=time.time())
        close_observers()
        snapshot = self.api.snapshot(self.home, now=time.time(), runtime_config={}, lang="pt", days=14)
        self.assertEqual((snapshot["profile"], snapshot["lang"], snapshot["days"], snapshot["mode"]), ("work", "pt", 14, "auto"))
        self.assertEqual(snapshot["lessons"][0]["status"], "quarantined")
        self.assertIn("Atlas Evolution", snapshot["status_md"])
        self.assertIn("resumo de 14 dias", snapshot["report_md"])
        self.assertEqual(snapshot["commands"]["review"], "hermes -p work evolution lessons review")
        json.dumps(snapshot)   # the dashboard receives it as JSON

    def test_the_default_profile_gets_commands_with_p_default(self):
        # explicit even for default (0.10.0): `hermes profile use` can make another profile sticky
        snapshot = self.api.snapshot(self.root, now=time.time(), runtime_config={}, lang="en", days=99)
        self.assertEqual(snapshot["days"], 7)   # out of range falls back
        self.assertTrue(all(command.startswith("hermes -p default evolution") for command in snapshot["commands"].values()))

    def test_the_router_answers_where_fastapi_is_installed(self):
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ImportError:
            self.assertIsNone(self.api.router)   # outside the dashboard there is nothing to mount
            return
        app = FastAPI()
        app.include_router(self.api.router, prefix="/api/plugins/hermes-atlas-evolution")
        self.api._current = lambda target=None: (self.home, {})
        client = TestClient(app)
        reply = client.get("/api/plugins/hermes-atlas-evolution/snapshot?days=7&lang=en")
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json()["profile"], "work")
        self.assertEqual(client.get("/api/plugins/hermes-atlas-evolution/snapshot?days=0").status_code, 422)
        methods = {method for route in self.api.router.routes for method in getattr(route, "methods", ())}
        self.assertEqual(methods, {"GET", "POST"})   # reads, and one route for the owner's decisions

    def test_the_bundle_renders_text_never_markup(self):
        script = (DASHBOARD / "dist" / "index.js").read_text(encoding="utf-8")
        self.assertNotIn("dangerouslySetInnerHTML", script)
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("eval(", script)
        self.assertIn('register(NAME, EvolutionPage)', script)
        self.assertIn('"/api/plugins/" + NAME + "/snapshot', script)
        self.assertIn("pass.at", script)   # status.build's last_pass is an object once a pass ran


class ReleaseTest(unittest.TestCase):
    def test_one_version_everywhere(self):
        version = re.search(r'^version:\s*"([\d.]+)"', (ROOT / "plugin.yaml").read_text(encoding="utf-8"), re.M).group(1)
        manifest = json.loads((DASHBOARD / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], version)
        self.assertEqual(manifest["name"], "hermes-atlas-evolution")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertEqual(re.search(r"^## (\S+)", changelog, re.M).group(1), version)


if __name__ == "__main__":
    unittest.main()
