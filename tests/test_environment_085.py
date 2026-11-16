"""0.8.5: every profile at once (environment.py), Hermes' plugin catalog and the knowledge base (ecosystem.py),
fleet actions with their undo, the Mode, and the Environment screen."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_atlas_evolution import actions, ecosystem, environment, explain, status
from hermes_atlas_evolution.experience import experience
from hermes_atlas_evolution.menu import Console, Menu

from test_menu_082 import Script

os.environ.setdefault("ATLAS_EVOLUTION_NO_HERMES", "1")   # never the real Hermes of this machine
NOW = 1_800_000_000.0


def plugin(home: Path, folder: str, *, name=None, version="1.0.0", hooks=(), code="", description="", repo=""):
    path = home / "plugins" / folder
    path.mkdir(parents=True, exist_ok=True)
    manifest = f"name: {name or folder}\nversion: \"{version}\"\ndescription: {description or folder}\n"
    if hooks:
        manifest += "provides_hooks:\n" + "".join(f"  - {hook}\n" for hook in hooks)
    (path / "plugin.yaml").write_text(manifest)
    (path / "__init__.py").write_text(code)
    if repo:
        (path / ".git" / "refs" / "heads").mkdir(parents=True)
        (path / ".git" / "config").write_text(f'[remote "origin"]\n\turl = {repo}\n')
        (path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (path / ".git" / "refs" / "heads" / "main").write_text("a" * 40 + "\n")
    old = NOW - 30 * 86_400
    for item in [path, *path.rglob("*")]:
        os.utime(item, (old, old))
    return path


def skill(home: Path, folder: str, *, description: str, body: str, used=None, created=NOW - 90 * 86_400):
    path = home / "skills" / folder
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(f"---\nname: {folder}\ndescription: {description}\n---\n{body}\n")
    usage_file = home / "skills" / ".usage.json"
    usage = json.loads(usage_file.read_text()) if usage_file.exists() else {}
    stamp = lambda value: None if value is None else time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(value))  # noqa: E731
    usage[folder] = {"last_used_at": stamp(used), "created_at": stamp(created), "use_count": 3 if used else 0,
                     "pinned": False, "state": "active"}
    usage_file.write_text(json.dumps(usage))


WEB_ONLY = '''
def rerank(tool_name="", args=None, result=None, **_):
    if tool_name != "web_search":
        return None
    return result + " ranked"

def register(ctx):
    ctx.register_hook("transform_tool_result", rerank)
'''
FILES_ONLY = '''
_TARGET_TOOLS = {"write_file": ("path", ("content",)), "patch": ("path", ("new",))}

def _scan(tool_name, args):
    spec = _TARGET_TOOLS.get(tool_name)
    return [] if spec is None else ["warn"]

def warn(tool_name="", args=None, result=None, **_):
    return result + " warned" if _scan(tool_name, args) else None

def register(ctx):
    ctx.register_hook("transform_tool_result", warn)
'''
ANY_TOOL = '''
def _register_optional_hook(ctx, hook_name, callback):
    ctx.register_hook(hook_name, callback)

def nudge(**kwargs):
    return "nudged"

def register(ctx):
    _register_optional_hook(ctx, "transform_tool_result", nudge)
'''
REWRITE_IN_PLACE = '''
def rewrite(tool_name=None, args=None, **_):
    if tool_name != "terminal":
        return None
    args["command"] = "rtk " + args["command"]

def register(ctx):
    ctx.register_hook("pre_tool_call", rewrite)
'''
REWRITE_DIRECTIVE = '''
def rewrite(tool_name="", args=None, **_):
    if tool_name != "terminal":
        return None
    return {"action": "modify", "args": {"command": "rtk " + args["command"]}}

def register(ctx):
    ctx.register_hook("pre_tool_call", rewrite)
'''


class Fleet(unittest.TestCase):
    """A Hermes root with profiles, read without PyYAML through a fake config reader."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / "hermes"
        self.configs = {}
        self.bundled = Path(folder.name) / "bundled"
        self.bundled.mkdir()
        self.profile("default", enabled=["hermes-atlas-evolution"])
        plugin(self.root, "hermes-atlas-evolution")
        reader = patch.object(environment, "_read_config", side_effect=lambda home: self.configs.get(str(Path(home))))
        reader.start()
        self.addCleanup(reader.stop)

    def profile(self, name: str, *, enabled=(), disabled=(), tool_hints=False) -> Path:
        home = self.root if name == "default" else self.root / "profiles" / name
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("model: test\n")
        config = {"plugins": {"enabled": list(enabled), "disabled": list(disabled)}}
        if tool_hints:
            config["plugins"]["entries"] = {"hermes-atlas-evolution": {"settings": {"tool_hints": True}}}
        self.configs[str(home)] = config
        return home

    def scan(self, sessions=lambda home, since: 5):
        return environment.scan(self.root, now=NOW, bundled=self.bundled / "plugins", sessions=sessions)

    def findings(self, catalog=None, known=None, **kwargs):
        reading = self.scan(**kwargs)
        return environment.findings(reading, catalog or {"entries": {}, "removed": []},
                                    known if known is not None else ecosystem.knowledge(), now=NOW)


class CodeReadingTest(Fleet):
    def test_the_tools_a_transform_acts_on_are_read_from_its_source(self):
        cases = {"web": (WEB_ONLY, "transform_tool_result", ["web_search"], False),
                 "files": (FILES_ONLY, "transform_tool_result", ["patch", "write_file"], False),
                 "any": (ANY_TOOL, "transform_tool_result", None, False),
                 "rtk": (REWRITE_IN_PLACE, "pre_tool_call", ["terminal"], True),
                 "atlas-rtk": (REWRITE_DIRECTIVE, "pre_tool_call", ["terminal"], True)}
        for folder, (code, hook, tools, rewrites) in cases.items():
            found = environment.code_hooks(plugin(self.root, folder, code=code))
            self.assertEqual(found[hook], {"tools": tools, "rewrites": rewrites}, folder)

    def test_a_broken_or_huge_source_is_skipped_never_run(self):
        folder = plugin(self.root, "broken", code="def oops(:\n")
        (folder / "evil.py").write_text("raise SystemExit('ran')\n")
        self.assertEqual(environment.code_hooks(folder), {})


class FindingsTest(Fleet):
    def test_transforms_on_different_tools_are_no_conflict(self):
        self.profile("work", enabled=["atlas-jev-research", "security-guidance"])
        plugin(self.root / "profiles" / "work", "atlas-jev-research", code=WEB_ONLY, hooks=["transform_tool_result"])
        plugin(self.bundled, "security-guidance", code=FILES_ONLY, hooks=["transform_tool_result"])
        kinds = [item["kind"] for item in self.findings()]
        self.assertNotIn("plugin_transform_overlap", kinds)

    def test_a_real_overlap_names_who_wins_and_never_turns_security_off(self):
        self.profile("work", enabled=["omh", "security-guidance"])
        plugin(self.root / "profiles" / "work", "omh", code=ANY_TOOL, hooks=["transform_tool_result"])
        plugin(self.bundled, "security-guidance", code=FILES_ONLY, hooks=["transform_tool_result"])
        found = [item for item in self.findings() if item["kind"] == "plugin_transform_overlap"]
        self.assertEqual(len(found), 1)
        evidence = found[0]["evidence"]
        self.assertEqual((evidence["first"], evidence["later"], evidence["off"]), ("omh", "security-guidance", "omh"))
        self.assertEqual(found[0]["subject"], "work/omh")
        self.assertEqual(found[0]["risk"], "high")
        told = explain.describe(found[0], "pt")
        self.assertIn("fica só com a primeira mudança de cada chamada", told["what"])
        self.assertIn("`patch`, `write_file` (o código de `omh` não se limita", told["what"])
        self.assertIn("plugin de segurança", told["what"])
        self.assertEqual(told["does"], "Aplicar roda: hermes -p work plugins disable omh")
        self.assertEqual(told["undo"], "hermes -p work plugins enable omh")

    def test_this_plugin_competes_only_while_its_hints_are_on(self):
        self.profile("work", enabled=["hermes-atlas-evolution", "omh"])
        plugin(self.root / "profiles" / "work", "hermes-atlas-evolution", code=ANY_TOOL, hooks=["transform_tool_result"])
        plugin(self.root / "profiles" / "work", "omh", code=ANY_TOOL, hooks=["transform_tool_result"])
        self.assertNotIn("plugin_transform_overlap", [item["kind"] for item in self.findings()])
        self.profile("work", enabled=["hermes-atlas-evolution", "omh"], tool_hints=True)
        self.assertIn("plugin_transform_overlap", [item["kind"] for item in self.findings()])

    def test_two_plugins_rewriting_the_terminal_are_a_duplicate(self):
        home = self.profile("work", enabled=["atlas-rtk", "rtk-rewrite"])
        plugin(home, "atlas-rtk", code=REWRITE_DIRECTIVE, hooks=["pre_tool_call"])
        plugin(home, "rtk-rewrite", code=REWRITE_IN_PLACE, hooks=["pre_tool_call"])
        found = [item for item in self.findings() if item["kind"] == "plugin_duplicate"]
        self.assertEqual([(item["subject"], item["evidence"]["keep"]) for item in found], [("work/rtk-rewrite", "atlas-rtk")])
        self.assertIn("reescrevem os comandos do terminal", explain.describe(found[0], "pt")["what"])

    def test_installed_plugins_never_loaded_leftovers_and_a_profile_without_the_plugin(self):
        home = self.profile("work", enabled=["kept"], disabled=["parked"])
        plugin(home, "kept")
        plugin(home, "parked")
        plugin(home, "forgotten")
        leftover = home / "plugins" / ".install-abc123"
        leftover.mkdir()
        os.utime(leftover, (NOW - 5 * 86_400, NOW - 5 * 86_400))
        found = {item["kind"]: item for item in self.findings()}
        self.assertEqual(sorted(entry["name"] for entry in found["plugin_unused"]["evidence"]["plugins"]),
                         ["forgotten", "parked"])
        self.assertEqual(found["plugin_leftover"]["subject"], "work/.install-abc123")
        self.assertEqual(found["self_missing"]["subject"], "work/hermes-atlas-evolution")
        self.assertEqual(explain.plan(found["self_missing"])[0],
                         ["@work", "plugins", "install", "sesantanajr/hermes-atlas-evolution", "--enable"])
        quiet = {item["kind"] for item in self.findings(sessions=lambda home, since: 0)}
        self.assertNotIn("self_missing", quiet)   # never used: nothing to learn there
        self.assertIn("profile_idle", quiet)

    def test_catalog_removals_updates_and_obsolete_plugins(self):
        home = self.profile("work", enabled=["badplug", "goodplug", "oldplug"])
        plugin(home, "badplug", repo="https://github.com/evil/badplug.git")
        plugin(home, "goodplug", version="1.0.0", repo="git@github.com:Good/goodplug.git")
        plugin(home, "oldplug", repo="https://github.com/jrezmo/1guy-plugin")
        catalog = {"entries": {"goodplug": {"name": "goodplug", "tier": "verified", "version": "1.2.0",
                                            "repo": "https://github.com/good/goodplug", "category": "tools"}},
                   "removed": [{"name": "", "repo": "https://github.com/evil/badplug", "reason": "exfiltrates tokens"}]}
        found = {item["kind"]: item for item in self.findings(catalog=catalog)}
        self.assertEqual((found["plugin_removed"]["subject"], found["plugin_removed"]["evidence"]["reason"]),
                         ("work/badplug", "exfiltrates tokens"))
        self.assertEqual(found["plugin_update"]["evidence"]["catalog"], "1.2.0")
        self.assertEqual(found["plugin_update"]["evidence"]["profiles"], [{"profile": "work", "installed": "1.0.0"}])
        self.assertEqual(found["plugin_obsolete"]["evidence"]["state"], "archived")
        self.assertIn("arquivado no GitHub", explain.describe(found["plugin_obsolete"], "pt")["what"])
        self.assertEqual(explain.plan(found["plugin_removed"])[0], ["@work", "plugins", "disable", "badplug"])
        self.assertEqual([item["risk"] for item in self.findings(catalog=catalog)][0], "high")   # most important first

    def test_skills_unused_duplicated_similar_and_shared(self):
        homes = [self.profile(name, enabled=["hermes-atlas-evolution"]) for name in ("a", "b", "c")]
        for home in homes:
            plugin(home, "hermes-atlas-evolution")
            skill(home, "deploy-site", description="Deploy the website to the server with rsync", body="steps " * 40,
                  used=NOW - 86_400)
        skill(homes[0], "deploy-site-copy", description="A copy", body="steps " * 40)
        skill(homes[0], "old-report", description="Write the monthly finance report for the board", body="x " * 50)
        skill(homes[0], "board-report", description="Write the monthly finance report for the board members",
              body="y " * 50, used=NOW - 86_400)
        found = {}
        for item in self.findings():
            found.setdefault(item["kind"], []).append(item)
        unused = next(item for item in found["skills_unused"] if item["evidence"]["profile"] == "a")
        self.assertEqual(sorted(unused["evidence"]["skills"]), ["deploy-site-copy", "old-report"])
        duplicate = next(item for item in found["skills_duplicate"] if item["evidence"]["profile"] == "a")
        self.assertEqual(duplicate["evidence"]["keep"], "deploy-site")
        similar = next(item for item in found["skills_similar"] if item["evidence"]["profile"] == "a")
        self.assertIn(["board-report", "old-report"], similar["evidence"]["pairs"])
        self.assertEqual(found["skills_shared"][0]["evidence"]["skills"], 1)


class CatalogTest(Fleet):
    def test_the_newest_cached_catalog_is_read_and_kept_in_the_database(self):
        old, new = self.root / "cache", self.profile("work") / "cache"
        for folder, count in ((old, 1), (new, 2)):
            folder.mkdir(parents=True)
            entries = [{"name": f"p{index}", "repo": f"https://github.com/x/p{index}", "tier": "official",
                        "capabilities": {"provides_hooks": ["pre_tool_call"]}} for index in range(count)]
            (folder / "plugin-catalog.json").write_text(json.dumps({"generated_at": "2026-11-16", "entries": entries,
                                                                    "removed": [{"name": "bad", "reason": "unsafe"}]}))
        os.utime(old / "plugin-catalog.json", (NOW - 100, NOW - 100))
        os.utime(new / "plugin-catalog.json", (NOW, NOW))
        catalog = ecosystem.load_catalog([self.root, self.root / "profiles" / "work"])
        self.assertEqual((catalog["source"], len(catalog["entries"])), ("cache", 2))
        self.assertEqual(catalog["entries"]["p1"]["hooks"], ["pre_tool_call"])
        self.assertEqual(ecosystem.lookup({"repo": "git@github.com:X/p0.git"}, catalog)["name"], "p0")
        with experience(self.root) as conn:
            self.assertTrue(ecosystem.sync(conn, catalog, ecosystem.knowledge()))
            self.assertFalse(ecosystem.sync(conn, catalog, ecosystem.knowledge()))   # unchanged: not rewritten
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM catalog_plugin").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT reason FROM catalog_removed").fetchone()[0], "unsafe")
            self.assertGreater(conn.execute("SELECT COUNT(*) FROM playbook").fetchone()[0], 10)
            self.assertEqual(conn.execute("SELECT level FROM playbook WHERE situation='plugin_removed'").fetchone()[0], "auto")

    def test_the_knowledge_base_ships_with_the_plugin(self):
        known = ecosystem.knowledge()
        self.assertTrue(known["plugins"]["security-guidance"]["protect"])
        self.assertIn("plugin_transform_overlap", known["playbook"])
        self.assertEqual(known["obsolete"]["1guy"]["state"], "archived")
        for situation, entry in known["playbook"].items():
            if not situation.startswith("_"):
                self.assertTrue(entry["title_pt"] and entry["advice_pt"] and entry["title_en"], situation)


class LeadAndRefreshTest(Fleet):
    def test_one_profile_looks_after_all_and_others_do_nothing(self):
        work = self.profile("work", enabled=["hermes-atlas-evolution"])
        plugin(work, "hermes-atlas-evolution")
        self.assertEqual(environment.lead(self.root), "default")
        with patch.object(environment, "scan", wraps=environment.scan) as scanned, \
                patch("hermes_atlas_evolution.status._sessions", return_value=1):
            self.assertIsNone(environment.refresh(work, now=NOW))
            first = environment.refresh(self.root, now=NOW)
            again = environment.refresh(self.root, now=NOW + 3600)
        self.assertEqual(scanned.call_count, 1)   # kept for six hours
        self.assertEqual(first["at"], again["at"])
        self.assertEqual(first["summary"]["profiles"], 2)
        self.configs[str(self.root)] = {"plugins": {"enabled": []}}
        self.assertEqual(environment.lead(self.root), "work")


class FleetActionsTest(Fleet):
    def propose(self, item):
        with experience(self.root) as conn:
            conn.execute("""INSERT INTO proposal(code, kind, risk, subject, evidence, command, status, created, updated)
                VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)""", (item["code"], item["kind"], item["risk"], item["subject"],
                                                             json.dumps(item["evidence"]), item["command"], NOW, NOW))
            return conn.execute("SELECT id FROM proposal WHERE code=?", (item["code"],)).fetchone()[0]

    def test_a_change_runs_in_its_profile_and_is_undone_there(self):
        from hermes_atlas_evolution import admin

        home = self.profile("work", enabled=["atlas-rtk", "rtk-rewrite"])
        plugin(home, "atlas-rtk", code=REWRITE_DIRECTIVE)
        plugin(home, "rtk-rewrite", code=REWRITE_IN_PLACE)
        item = next(found for found in self.findings() if found["kind"] == "plugin_duplicate")
        proposal = self.propose(item)
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv[1:], kwargs["env"]["HERMES_HOME"]))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch.object(admin, "hermes_executable", return_value="/usr/bin/hermes"), \
                patch("hermes_atlas_evolution.admin.subprocess.run", runner):
            result = actions.apply_proposal(self.root, proposal, now=NOW)
            self.assertEqual(result["undo"], "hermes -p work plugins enable rtk-rewrite")
            undone = admin.decide(self.root, {}, {"kind": "undo", "id": result["decision"]}, now=NOW + 1)
        self.assertTrue(undone["undone"])
        self.assertEqual(calls, [(["--profile", "work", "plugins", "disable", "rtk-rewrite"], str(home.resolve())),
                                 (["--profile", "work", "plugins", "enable", "rtk-rewrite"], str(home.resolve()))])

    def test_a_failed_install_folder_goes_to_the_trash_and_back(self):
        from hermes_atlas_evolution import admin

        home = self.profile("work", enabled=["hermes-atlas-evolution"])
        leftover = home / "plugins" / ".install-zz9"
        leftover.mkdir(parents=True)
        (leftover / "junk").write_text("x")
        os.utime(leftover, (NOW - 9 * 86_400, NOW - 9 * 86_400))
        item = next(found for found in self.findings() if found["kind"] == "plugin_leftover")
        proposal = self.propose(item)
        result = actions.apply_proposal(self.root, proposal, now=NOW)
        kept = self.root / "evolution" / "trash" / "work" / ".install-zz9"
        self.assertTrue((kept / "junk").is_file())
        self.assertFalse(leftover.exists())
        admin.decide(self.root, {}, {"kind": "undo", "id": result["decision"]}, now=NOW + 1)
        self.assertTrue((leftover / "junk").is_file())
        with self.assertRaises(ValueError):   # only a failed install's folder, never a plugin
            actions._trash(self.root, home, "work", "hermes-atlas-evolution", back=False)

    def test_mode_auto_applies_only_the_safe_fleet_changes(self):
        from hermes_atlas_evolution import admin

        home = self.profile("work", enabled=["atlas-rtk", "rtk-rewrite", "badplug"])
        plugin(home, "atlas-rtk", code=REWRITE_DIRECTIVE)
        plugin(home, "rtk-rewrite", code=REWRITE_IN_PLACE)
        plugin(home, "badplug", repo="https://github.com/evil/badplug")
        catalog = {"entries": {}, "removed": [{"name": "badplug", "repo": "", "reason": "unsafe"}]}
        for item in self.findings(catalog=catalog):
            if item["kind"] in ("plugin_duplicate", "plugin_removed", "self_missing"):
                self.propose(item)
        calls = []
        runner = lambda argv, **kwargs: calls.append(argv[1:]) or SimpleNamespace(returncode=0, stdout="", stderr="")  # noqa: E731
        with patch.object(admin, "hermes_executable", return_value="/usr/bin/hermes"), \
                patch("hermes_atlas_evolution.admin.subprocess.run", runner):
            done = actions.automatic(self.root, now=NOW, mode="full", usage={})
        ran = sorted(" ".join(call[2:]) for call in calls)
        self.assertEqual(ran, ["plugins disable badplug", "plugins install sesantanajr/hermes-atlas-evolution --enable"])
        self.assertEqual(len(done["applied"]), 2)   # the duplicate waits for the owner
        with patch.object(admin, "hermes_executable", return_value="/usr/bin/hermes"), \
                patch("hermes_atlas_evolution.admin.subprocess.run", runner):
            self.assertEqual(actions.automatic(self.root, now=NOW, mode="safe", usage={})["applied"], [])

    def test_the_weekly_reclaim_runs_hermes_pm_gc_once_a_week(self):
        with experience(self.root) as conn:
            conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('care_check', ?)",
                         (json.dumps({"findings": ["unused_tools"], "reclaimable": 700 * 2**20}),))
        ran = []
        fake = lambda home, key, runner=None: ran.append(key) or {"ok": True, "steps": []}  # noqa: E731
        with patch("hermes_atlas_evolution.hermes_care.run_action", fake):
            first = actions.reclaim(self.root, now=NOW)
            second = actions.reclaim(self.root, now=NOW + 86_400)
            later = actions.reclaim(self.root, now=NOW + 8 * 86_400)
        self.assertEqual((ran, first["ok"], second), (["pm_gc", "pm_gc"], True, None))
        self.assertTrue(later["ok"])


class ScreensTest(Fleet):
    def test_the_environment_screen_and_the_status_tab(self):
        home = self.profile("work", enabled=["atlas-rtk", "rtk-rewrite"])
        plugin(home, "atlas-rtk", code=REWRITE_DIRECTIVE)
        plugin(home, "rtk-rewrite", code=REWRITE_IN_PLACE)
        with patch("hermes_atlas_evolution.status._sessions", return_value=3):
            found = environment.refresh(self.root, now=NOW, force=True)
        with experience(self.root) as conn:
            for item in found["findings"]:
                conn.execute("""INSERT INTO proposal(code, kind, risk, subject, evidence, command, status, created, updated)
                    VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)""", (item["code"], item["kind"], item["risk"], item["subject"],
                                                                 json.dumps(item["evidence"]), item["command"], NOW, NOW))
        script = Script("14", "1", "", "", "0")
        with patch.dict(os.environ, {"NO_COLOR": "1"}):
            Menu(self.root, {}, lang="pt", console=Console(read=script.read, write=script.write, width=130, color=False),
                 clock=lambda: NOW).run()
        text = script.text
        for expected in ("Ambiente: todos os perfis deste Hermes", "Perfis (2):", "Dois plugins fazem o mesmo trabalho",
                         "O que fazer:", "Fique com um", "hermes -p work plugins enable rtk-rewrite"):
            self.assertIn(expected, text)
        markdown = status.environment_markdown(self.root, "pt", mode="auto")
        self.assertIn("| work | 3 |", markdown)
        self.assertIn("Dois plugins fazem o mesmo trabalho", markdown)


if __name__ == "__main__":
    unittest.main()
