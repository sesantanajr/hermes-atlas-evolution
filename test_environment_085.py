"""0.8.5: every profile at once (environment.py), Hermes' plugin catalog and the knowledge base (ecosystem.py),
fleet actions with their undo, the Mode, and the Environment screen. Includes, as regressions, the reproductions of the
independent review of 0.8.5 (forged data, owner decisions overridden, links in the trash path, named pipes, deep
nesting, exclusion read as inclusion, load order, protected plugins)."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_atlas_evolution import actions, admin, ecosystem, environment, explain, fleet_update, status
from hermes_atlas_evolution.experience import experience
from hermes_atlas_evolution.menu import Console, Menu

from test_menu_082 import Script

os.environ.setdefault("ATLAS_EVOLUTION_NO_HERMES", "1")   # never the real Hermes of this machine
NOW = 1_800_000_000.0
POSIX_ONLY = unittest.skipUnless(os.name == "posix", "POSIX only: named pipes, file modes, links")


def plugin(home: Path, folder: str, *, name=None, version="1.0.0", hooks=(), code="", description="", repo="",
           requires=()):
    path = home / "plugins" / folder
    path.mkdir(parents=True, exist_ok=True)
    manifest = f"name: {name or folder}\nversion: \"{version}\"\ndescription: {description or folder}\n"
    if hooks:
        manifest += "provides_hooks:\n" + "".join(f"  - {hook}\n" for hook in hooks)
    if requires:
        manifest += "requires_plugins:\n" + "".join(f"  - id: {item}\n" for item in requires)
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
ALL_BUT_TERMINAL = '''
SKIP = {"terminal"}

def note(tool_name="", args=None, result=None, **_):
    if tool_name in SKIP:
        return None
    return result + " noted"

def register(ctx):
    ctx.register_hook("transform_tool_result", note)
'''
TERMINAL_ONLY = '''
def shorten(tool_name="", args=None, result=None, **_):
    if tool_name != "terminal":
        return None
    return result[:100]

def register(ctx):
    ctx.register_hook("transform_tool_result", shorten)
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
    if tool_name != "terminal" or not isinstance(args, dict):
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
AUDIT_ONLY = '''
LOG = []

def audit(tool_name="", args=None, **_):
    if tool_name != "terminal":
        return None
    record = {}
    record["command"] = args.get("command")
    LOG.append(record)

def register(ctx):
    ctx.register_hook("pre_tool_call", audit)
'''


class Fleet(unittest.TestCase):
    """A Hermes root with profiles, read without PyYAML through a fake config reader."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        # resolved: macOS keeps temporary folders behind /var -> /private/var, Windows behind short names
        self.root = Path(folder.name).resolve() / "hermes"
        self.configs = {}
        self.bundled = Path(folder.name).resolve() / "bundled"
        self.bundled.mkdir()
        self.profile("default", enabled=["hermes-atlas-evolution"])
        plugin(self.root, "hermes-atlas-evolution")
        reader = patch.object(environment, "_read_config",
                              side_effect=lambda home: self.configs.get(str(Path(home).resolve())))
        reader.start()
        self.addCleanup(reader.stop)
        sessions = patch("hermes_atlas_evolution.status._sessions", side_effect=lambda home, since: 5)
        sessions.start()
        self.addCleanup(sessions.stop)
        environment._LEAD_MEMO.clear()

    def profile(self, name: str, *, enabled=(), disabled=(), tool_hints=False, entries=None) -> Path:
        home = self.root if name == "default" else self.root / "profiles" / name
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("model: test\n")
        config = {"plugins": {"enabled": list(enabled), "disabled": list(disabled)}}
        if tool_hints:
            config["plugins"]["entries"] = {"hermes-atlas-evolution": {"settings": {"tool_hints": True}}}
        if entries:
            config["plugins"]["entries"] = entries
        self.configs[str(home.resolve())] = config
        return home

    def scan(self, sessions=lambda home, since: 5):
        return environment.scan(self.root, now=NOW, bundled=self.bundled / "plugins", sessions=sessions)

    def findings(self, catalog=None, known=None, **kwargs):
        reading = self.scan(**kwargs)
        return environment.findings(reading, catalog or {"entries": {}, "removed": []},
                                    known if known is not None else ecosystem.knowledge(), now=NOW,
                                    fleet=environment.state(self.root))

    def propose(self, item, home=None):
        with experience(home or self.root) as conn:
            conn.execute("""INSERT INTO proposal(code, kind, risk, subject, evidence, command, status, created, updated)
                VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)""", (item["code"], item["kind"], item["risk"], item["subject"],
                                                             json.dumps(item["evidence"]), item.get("command", ""), NOW, NOW))
            return conn.execute("SELECT id FROM proposal WHERE code=?", (item["code"],)).fetchone()[0]


def scope(found, hook):
    entry = found[hook]
    return entry["scope"], entry["tools"], entry["rewrites"]


class CodeReadingTest(Fleet):
    def test_which_tools_each_hook_acts_on(self):
        cases = {"web": (WEB_ONLY, "transform_tool_result", ("only", ["web_search"], False)),
                 "files": (FILES_ONLY, "transform_tool_result", ("only", ["patch", "write_file"], False)),
                 "skip": (ALL_BUT_TERMINAL, "transform_tool_result", ("except", ["terminal"], False)),
                 "any": (ANY_TOOL, "transform_tool_result", ("any", [], False)),
                 "hidden": ("def register(ctx):\n    ctx.register_hook('transform_tool_result', make())\n",
                            "transform_tool_result", ("unknown", [], False)),
                 "rtk": (REWRITE_IN_PLACE, "pre_tool_call", ("only", ["terminal"], True)),
                 "atlas-rtk": (REWRITE_DIRECTIVE, "pre_tool_call", ("only", ["terminal"], True)),
                 "audit": (AUDIT_ONLY, "pre_tool_call", ("only", ["terminal"], False))}   # a log record is no rewrite
        for folder, (code, hook, expected) in cases.items():
            self.assertEqual(scope(environment.code_hooks(plugin(self.root, folder, code=code)), hook), expected, folder)

    def test_a_broken_deep_or_huge_source_never_fails_and_is_never_run(self):
        folder = plugin(self.root, "broken", code="def oops(:\n")
        (folder / "evil.py").write_text("raise SystemExit('ran')\n")
        self.assertEqual(environment.code_hooks(folder), {})   # registers nothing: not even parsed
        (folder / "hooks.py").write_text("def x(:\n    ctx.register_hook('transform_tool_result', f)\n")
        self.assertEqual(environment.code_hooks(folder), {"_unread": {"scope": "unknown", "tools": [], "rewrites": False}})
        deep = plugin(self.root, "deep", code="ctx.register_hook\nx = " + "-" * 6000 + "1\n")   # MemoryError in the parser
        self.assertEqual(environment.code_hooks(deep), {"_unread": {"scope": "unknown", "tools": [], "rewrites": False}})
        huge = plugin(self.root, "huge", code="# x\n" * 40_000)   # over the per-file limit: unknown, never read
        self.assertEqual(environment.code_hooks(huge), {"_unread": {"scope": "unknown", "tools": [], "rewrites": False}})
        self.assertIsNone(environment.code_hooks(plugin(self.root, "late", code=WEB_ONLY), {"left": 0}))

    @POSIX_ONLY
    def test_a_named_pipe_or_a_link_is_never_opened(self):
        folder = plugin(self.root, "piped", code=WEB_ONLY)
        os.mkfifo(folder / "x.py")
        os.mkfifo(folder / "pipe.txt")
        started = time.monotonic()
        self.assertEqual(scope(environment.code_hooks(folder), "transform_tool_result")[0], "only")
        self.assertIsNone(environment.read_text(folder / "pipe.txt", 1000))
        os.symlink(folder / "plugin.yaml", folder / "link.txt")
        self.assertIsNone(environment.read_text(folder / "link.txt", 1000))
        (folder / "big.txt").write_text("x" * 2000)
        self.assertIsNone(environment.read_text(folder / "big.txt", 1000))
        self.assertLess(time.monotonic() - started, 5)

    def test_overlap_of_scopes(self):
        only = lambda *names: {"scope": "only", "tools": list(names)}   # noqa: E731
        skip = lambda *names: {"scope": "except", "tools": list(names)}   # noqa: E731
        self.assertEqual(environment.overlap(only("a"), only("b")), ("none", []))
        self.assertEqual(environment.overlap(only("a", "b"), only("b")), ("tools", ["b"]))
        self.assertEqual(environment.overlap(skip("terminal"), only("terminal")), ("none", []))
        self.assertEqual(environment.overlap(skip("terminal"), only("web_search")), ("tools", ["web_search"]))
        self.assertEqual(environment.overlap({"scope": "any", "tools": []}, only("x")), ("tools", ["x"]))
        self.assertEqual(environment.overlap(skip("a"), skip("b")), ("any", []))

    def test_load_order_follows_requires_plugins_then_names(self):
        items = [{"name": "alpha", "requires": ["zeta-base"]}, {"name": "beta", "requires": []},
                 {"name": "zeta-base", "requires": []}]
        self.assertEqual([item["name"] for item in environment.load_order(items)], ["beta", "zeta-base", "alpha"])
        loop = [{"name": "a", "requires": ["b"]}, {"name": "b", "requires": ["a"]}]
        self.assertEqual([item["name"] for item in environment.load_order(loop)], ["a", "b"])


class FindingsTest(Fleet):
    def test_transforms_on_different_tools_are_no_conflict(self):
        home = self.profile("work", enabled=["atlas-jev-research", "security-guidance", "skipper", "short"])
        plugin(home, "atlas-jev-research", code=WEB_ONLY, hooks=["transform_tool_result"])
        plugin(self.bundled, "security-guidance", code=FILES_ONLY, hooks=["transform_tool_result"])
        plugin(home, "skipper", code=ALL_BUT_TERMINAL, hooks=["transform_tool_result"])
        plugin(home, "short", code=TERMINAL_ONLY, hooks=["transform_tool_result"])
        pairs = {(item["evidence"]["first"], item["evidence"]["later"]) for item in self.findings()
                 if item["kind"].startswith("plugin_transform")}
        # skipper acts on everything but the terminal: it meets web_search and the file tools, never `short`
        self.assertNotIn(("short", "skipper"), pairs)
        self.assertNotIn(("atlas-jev-research", "security-guidance"), pairs)
        self.assertIn(("atlas-jev-research", "skipper"), pairs)

    def test_a_real_overlap_names_who_wins_and_never_turns_security_off(self):
        home = self.profile("work", enabled=["omh", "security-guidance"])
        plugin(home, "omh", code=ANY_TOOL, hooks=["transform_tool_result"])
        plugin(self.bundled, "security-guidance", code=FILES_ONLY, hooks=["transform_tool_result"])
        found = [item for item in self.findings() if item["kind"] == "plugin_transform_overlap"]
        self.assertEqual(len(found), 1)
        evidence = found[0]["evidence"]
        self.assertEqual((evidence["first"], evidence["later"], evidence["off"]), ("omh", "security-guidance", "omh"))
        self.assertEqual((found[0]["subject"], found[0]["risk"]), ("work/omh", "high"))
        told = explain.describe(found[0], "pt")
        self.assertIn("fica só com a primeira", told["what"])
        self.assertIn("plugin de segurança", told["what"])
        self.assertEqual(told["does"], "Ao aplicar, o plugin roda `hermes -p work plugins disable omh`.")
        self.assertEqual(told["undo"], "hermes -p work plugins enable omh")
        self.assertFalse(told["bulk"])   # which plugin to keep is decided one by one, never in Apply all

    def test_this_plugins_hints_are_a_setting_to_change_never_a_plugin_to_turn_off(self):
        home = self.profile("work", enabled=["hermes-atlas-evolution", "security-guidance", "omh"], tool_hints=True)
        plugin(home, "hermes-atlas-evolution", code=ANY_TOOL, hooks=["transform_tool_result"])
        plugin(home, "omh", code=ANY_TOOL, hooks=["transform_tool_result"])
        plugin(self.bundled, "security-guidance", code=FILES_ONLY, hooks=["transform_tool_result"])
        found = self.findings()
        hints = [item for item in found if item["kind"] == "plugin_hints_overlap"]
        self.assertTrue(hints)
        self.assertIn("tool_hints false", hints[0]["command"])
        self.assertIsNone(explain.plan(hints[0]))
        for item in found:
            if item["kind"] == "plugin_transform_overlap":
                self.assertNotIn(item["evidence"]["off"], ("security-guidance", "hermes-atlas-evolution"))
        self.profile("work", enabled=["hermes-atlas-evolution", "omh"])   # hints off: this plugin changes nothing
        self.assertFalse([item for item in self.findings() if item["kind"] == "plugin_hints_overlap"])

    def test_two_protected_plugins_get_advice_only(self):
        home = self.profile("work", enabled=["security-guidance", "zz-guard"])
        plugin(home, "zz-guard", code=ANY_TOOL, hooks=["transform_tool_result"])
        plugin(self.bundled, "security-guidance", code=FILES_ONLY, hooks=["transform_tool_result"])
        known = ecosystem.knowledge()
        known["plugins"]["zz-guard"] = {"role": "security", "protect": True}
        found = [item for item in self.findings(known=known) if item["kind"].startswith("plugin_transform")]
        self.assertEqual([item["kind"] for item in found], ["plugin_transform_protected"])
        self.assertIsNone(explain.plan(found[0]))
        self.assertEqual(explain.category("plugin_transform_protected"), "advice")

    def test_the_winner_follows_hermes_load_order(self):
        home = self.profile("work", enabled=["alpha", "beta"])
        plugin(home, "alpha", code=ANY_TOOL, hooks=["transform_tool_result"], requires=["beta"])
        plugin(home, "beta", code=WEB_ONLY, hooks=["transform_tool_result"])
        found = next(item for item in self.findings() if item["kind"] == "plugin_transform_overlap")
        self.assertEqual((found["evidence"]["first"], found["evidence"]["later"]), ("beta", "alpha"))

    def test_terminal_rewriters_are_a_duplicate_and_an_audit_plugin_is_not(self):
        home = self.profile("work", enabled=["atlas-rtk", "rtk-rewrite", "auditor"])
        plugin(home, "atlas-rtk", code=REWRITE_DIRECTIVE, hooks=["pre_tool_call"])
        plugin(home, "rtk-rewrite", code=REWRITE_IN_PLACE, hooks=["pre_tool_call"])
        plugin(home, "auditor", code=AUDIT_ONLY, hooks=["pre_tool_call"])
        found = [item for item in self.findings() if item["kind"] == "plugin_duplicate"]
        self.assertEqual([(item["subject"], item["evidence"]["keep"]) for item in found], [("work/rtk-rewrite", "atlas-rtk")])
        self.assertIn("reescrevem os comandos do terminal", explain.describe(found[0], "pt")["what"])

    def test_a_removed_plugin_is_advice_with_its_command_and_never_changed_by_the_plugin(self):
        home = self.profile("work", enabled=["badplug"])
        plugin(home, "badplug", repo="https://github.com/evil/badplug.git")
        (home / "cache").mkdir()
        (home / "cache" / "plugin-catalog.json").write_text(json.dumps(
            {"entries": [], "removed": [{"name": "", "repo": "https://github.com/evil/badplug", "reason": "exfiltrates tokens"}]}))
        found = next(item for item in self.findings() if item["kind"] == "plugin_removed")
        self.assertEqual((found["subject"], found["evidence"]["reason"]), ("work/badplug", "exfiltrates tokens"))
        self.assertIsNone(explain.plan(found))
        self.assertEqual(explain.category("plugin_removed"), "manual")
        self.assertEqual(found["command"], "hermes -p work plugins remove badplug")
        self.assertIn("já se recusa a carregá-lo", explain.describe(found, "pt")["what"])
        # installed with --allow-removed: still reported, said so, never touched
        (home / "plugins" / ".install-metadata.json").write_text(json.dumps({"badplug": {"allow_removed": True}}))
        allowed = next(item for item in self.findings() if item["kind"] == "plugin_removed")
        self.assertIn("--allow-removed", explain.describe(allowed, "pt")["what"])

    def test_another_profiles_catalog_copy_never_speaks_for_this_one(self):
        home = self.profile("work", enabled=["guard-plugin"])
        plugin(home, "guard-plugin")
        other = self.profile("second")
        (other / "cache").mkdir()
        (other / "cache" / "plugin-catalog.json").write_text(json.dumps(
            {"entries": [], "removed": [{"name": "guard-plugin", "repo": "", "reason": "planted"}]}))
        self.assertFalse([item for item in self.findings() if item["kind"] == "plugin_removed"
                          and item["subject"] == "work/guard-plugin"])

    def test_installed_plugins_never_loaded_leftovers_and_profiles_without_the_plugin(self):
        home = self.profile("work", enabled=["kept"], disabled=["parked"])
        plugin(home, "kept")
        plugin(home, "parked")
        plugin(home, "forgotten", name="forgotten-plugin")
        leftover = home / "plugins" / ".install-abc123"
        leftover.mkdir()
        os.utime(leftover, (NOW - 5 * 86_400, NOW - 5 * 86_400))
        found = {item["kind"]: item for item in self.findings()}
        self.assertEqual(sorted(entry["name"] for entry in found["plugin_unused"]["evidence"]["plugins"]),
                         ["forgotten-plugin", "parked"])   # Hermes' key is the manifest name, not the folder
        self.assertIn("hermes -p work plugins remove forgotten-plugin", found["plugin_unused"]["evidence"]["commands"])
        self.assertEqual(found["plugin_leftover"]["subject"], "work/.install-abc123")
        self.assertEqual(found["self_missing"]["subject"], "work/hermes-atlas-evolution")
        # 0.13.0: nothing to install from without the profile that applies it (its catalog name or its commit)
        self.assertIsNone(explain.plan(found["self_missing"]))
        self.assertEqual(found["self_missing"]["command"], "hermes -p work plugins install hermes-atlas-evolution --enable")
        quiet = {item["kind"] for item in self.findings(sessions=lambda home, since: 0)}
        self.assertNotIn("self_missing", quiet)   # never used: nothing to learn there
        self.assertIn("profile_idle", quiet)

    def test_the_owners_decision_about_this_plugin_is_kept(self):
        self.profile("off", disabled=["hermes-atlas-evolution"])
        self.profile("removed", entries={"hermes-atlas-evolution": {"settings": {"language": "pt"}}})
        self.profile("fresh")
        self.profile("seen-before")
        environment.remember(self.root, seen=["seen-before"])
        kinds = {item["subject"].split("/")[0]: item["kind"] for item in self.findings()
                 if item["kind"] in ("self_missing", "self_off")}
        self.assertEqual(kinds, {"off": "self_off", "removed": "self_off", "fresh": "self_missing",
                                 "seen-before": "self_off"})
        forged = {"kind": "self_missing", "subject": "fresh/some-other-plugin", "evidence": {}}
        self.assertIsNone(explain.plan(forged))   # only this plugin installs itself

    def test_the_install_gives_the_code_the_lead_runs(self):
        # 0.13.0 (catalog rule 3): the plugin never picks a commit and never reads the network. A profile gets the
        # lead's own commit of this repository (`--ref`), with Hermes' own command.
        plugin(self.root, "hermes-atlas-evolution", repo="https://github.com/sesantanajr/hermes-atlas-evolution")
        self.profile("fresh")
        found = environment.findings(self.scan(), {"entries": {}, "removed": []}, ecosystem.knowledge(), now=NOW,
                                     self_sha=environment.self_sha(self.root),
                                     self_source=fleet_update.source(self.root))
        item = next(entry for entry in found if entry["kind"] == "self_missing")
        forward = ["@fresh", "plugins", "install", "sesantanajr/hermes-atlas-evolution", "--enable", "--ref", "a" * 40]
        self.assertEqual(explain.plan(item, home=self.root)[0], forward)
        self.assertEqual(item["command"], "hermes -p fresh " + " ".join(forward[1:]))
        self.assertEqual(explain.undo_for(forward), ["@fresh", "plugins", "remove", "hermes-atlas-evolution"])
        self.propose(item)
        calls = []

        def run(argv, **kwargs):
            calls.append(argv[1:])
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch.object(admin, "hermes_executable", return_value="/usr/bin/hermes"), \
                patch("hermes_atlas_evolution.admin.run_command", run), \
                patch("subprocess.run", side_effect=AssertionError("no git, no network")):
            waiting = actions.automatic(self.root, now=NOW, mode="full", usage={})
            # by itself only from the catalog: an install from the repository waits for the owner's key
            self.assertEqual((calls, waiting["applied"]), ([], []))
            self.assertEqual([entry["reason"] for entry in waiting["waiting"]], ["not_catalog"])
            proposal = actions.apply_proposal(self.root, waiting["waiting"][0]["proposal"], now=NOW)
        self.assertEqual(calls, [["--profile", "fresh", *forward[1:]]])
        self.assertTrue(proposal["applied"])

    def test_a_catalog_lead_installs_the_reviewed_pin_by_name(self):
        plugin(self.root, "hermes-atlas-evolution")
        (self.root / "plugins" / ".install-metadata.json").write_text(json.dumps({"hermes-atlas-evolution": {
            "source": "https://github.com/sesantanajr/hermes-atlas-evolution", "pinned": True, "revision": "b" * 40,
            "catalog": {"name": "hermes-atlas-evolution", "tier": "community", "sha": "b" * 40, "pin": "b" * 40}}}),
            encoding="utf-8")
        self.assertEqual(fleet_update.source(self.root), ["hermes-atlas-evolution"])
        lead = fleet_update.inspect("default", self.root)
        self.assertEqual((lead["catalog"], lead["pinned"]), ("hermes-atlas-evolution", ""))   # Hermes moves it
        self.assertEqual(fleet_update.step(lead, install_missing=False), ["plugins", "update", "hermes-atlas-evolution"])
        self.profile("fresh")
        item = next(entry for entry in self.findings() if entry["kind"] == "self_missing")
        forward = ["@fresh", "plugins", "install", "hermes-atlas-evolution", "--enable"]
        self.assertEqual(explain.plan(item, home=self.root), (forward, ["@fresh", "plugins", "remove",
                                                                        "hermes-atlas-evolution"]))
        self.propose(item)
        calls = []
        with patch.object(admin, "hermes_executable", return_value="/usr/bin/hermes"), \
                patch("hermes_atlas_evolution.admin.run_command",
                      lambda argv, **k: calls.append(argv[1:]) or SimpleNamespace(returncode=0, stdout="", stderr="")):
            done = actions.automatic(self.root, now=NOW, mode="full", usage={})
        self.assertEqual(calls, [["--profile", "fresh", *forward[1:]]])   # the reviewed pin, by name, by itself
        self.assertEqual(len(done["applied"]), 1)

    def test_a_fork_lead_never_installs_itself_elsewhere(self):
        plugin(self.root, "hermes-atlas-evolution", repo="https://github.com/someone/hermes-atlas-evolution-fork")
        self.profile("fresh")
        item = next(entry for entry in self.findings() if entry["kind"] == "self_missing")
        self.propose(item)
        calls = []
        self.assertIsNone(fleet_update.source(self.root))
        with patch.object(admin, "hermes_executable", return_value="/usr/bin/hermes"), \
                patch("hermes_atlas_evolution.admin.run_command", lambda argv, **k: calls.append(argv)):
            done = actions.automatic(self.root, now=NOW, mode="full", usage={})
        self.assertEqual((done["applied"], calls), ([], []))   # a fork's commit is not on GitHub: nothing to spread

    def test_obsolete_plugins_and_updates(self):
        home = self.profile("work", enabled=["goodplug", "oldplug"])
        plugin(home, "goodplug", version="1.0.0", repo="git@github.com:Good/goodplug.git")
        plugin(home, "oldplug", repo="https://github.com/jrezmo/1guy-plugin")
        catalog = {"entries": {"goodplug": {"name": "goodplug", "tier": "verified", "version": "1.2.0",
                                            "repo": "https://github.com/good/goodplug", "category": "tools"}}, "removed": []}
        found = {item["kind"]: item for item in self.findings(catalog=catalog)}
        self.assertEqual(found["plugin_update"]["evidence"]["catalog"], "1.2.0")
        self.assertEqual(found["plugin_update"]["evidence"]["profiles"], [{"profile": "work", "installed": "1.0.0"}])
        self.assertEqual(found["plugin_obsolete"]["evidence"]["state"], "archived")
        self.assertIn("arquivado no GitHub", explain.describe(found["plugin_obsolete"], "pt")["what"])

    def test_skills_unused_duplicated_similar_and_shared_hermes_own_left_out(self):
        homes = [self.profile(name, enabled=["hermes-atlas-evolution"]) for name in ("a", "b", "c")]
        for home in homes:
            plugin(home, "hermes-atlas-evolution")
            skill(home, "deploy-site", description="Deploy the website to the server with rsync", body="steps " * 40,
                  used=NOW - 86_400)
            skill(home, "arxiv", description="Hermes bundled skill", body="bundled " * 40)
            (home / "skills" / ".bundled_manifest").write_text("arxiv:abc\n")
        skill(homes[0], "deploy-site-copy", description="A copy", body="steps " * 40)
        skill(homes[0], "old-report", description="Write the monthly finance report for the board", body="x " * 50)
        skill(homes[0], "board-report", description="Write the monthly finance report for the board members",
              body="y " * 50, used=NOW - 86_400)
        found = {}
        for item in self.findings():
            found.setdefault(item["kind"], []).append(item)
        unused = next(item for item in found["skills_unused"] if item["evidence"]["profile"] == "a")
        self.assertEqual(sorted(unused["evidence"]["skills"]), ["deploy-site-copy", "old-report"])   # never arxiv
        duplicate = next(item for item in found["skills_duplicate"] if item["evidence"]["profile"] == "a")
        self.assertEqual(duplicate["evidence"]["keep"], "deploy-site")
        similar = next(item for item in found["skills_similar"] if item["evidence"]["profile"] == "a")
        self.assertIn(["board-report", "old-report"], similar["evidence"]["pairs"])
        self.assertEqual(found["skills_shared"][0]["evidence"]["skills"], 1)   # deploy-site; arxiv is Hermes' own


class RobustnessTest(Fleet):
    def test_an_unclear_overlap_is_advice_never_a_plugin_turned_off(self):
        home = self.profile("work", enabled=["hidden", "web"])
        plugin(home, "hidden", code="def register(ctx):\n    ctx.register_hook('transform_tool_result', make())\n",
               hooks=["transform_tool_result"])
        plugin(home, "web", code=WEB_ONLY, hooks=["transform_tool_result"])
        found = [item for item in self.findings() if item["kind"].startswith("plugin_transform")]
        self.assertEqual([item["kind"] for item in found], ["plugin_transform_unclear"])
        self.assertIsNone(explain.plan(found[0]))
        self.assertEqual(explain.category("plugin_transform_unclear"), "advice")
        self.assertIn("possibilidade, não uma certeza", explain.describe(found[0], "pt")["what"])

    def test_the_same_plugin_in_many_profiles_is_read_once(self):
        budget = {"left": 10_000}
        first = environment.code_hooks(plugin(self.profile("a"), "web", code=WEB_ONLY), budget)
        left = budget["left"]
        second = environment.code_hooks(plugin(self.profile("b"), "web", code=WEB_ONLY), budget)
        self.assertEqual((first, budget["left"]), (second, left))

    def test_odd_configs_catalog_entries_and_skill_trees_never_fail_the_scan(self):
        odd = self.profile("odd")
        self.configs[str(odd.resolve())] = {"plugins": ["not", "a", "mapping"]}
        heavy = odd / "skills" / "tool" / "node_modules"
        for index in range(50):
            (heavy / f"pkg{index}").mkdir(parents=True)
        skill(odd, "real-skill", description="A real skill", body="x " * 20)
        reading = self.scan()
        row = next(item for item in reading["profiles"] if item["profile"] == "odd")
        self.assertEqual([item["folder"] for item in row["skills"]], ["real-skill"])
        self.assertIsNotNone(ecosystem._entry({"name": "x", "known_issues": {"a": 1}}))

    @POSIX_ONLY
    def test_a_folder_name_that_is_not_utf8_is_skipped(self):
        home = self.profile("work")
        (home / "skills").mkdir(parents=True, exist_ok=True)
        try:
            os.mkdir(os.fsencode(home / "skills") + b"/bad\xff")
        except OSError:
            self.skipTest("this file system refuses names that are not UTF-8 (macOS)")
        os.mkdir(os.fsencode(home / "plugins") + b"/.install-\xff") if (home / "plugins").exists() else None
        found = self.findings()
        json.dumps(found)   # storable, never a surrogate
        self.assertIsInstance(found, list)

    def test_config_is_read_without_hermes_loader(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not installed")
        home = self.profile("work")
        (home / "config.yaml").write_text("plugins:\n  enabled: [a]\n")
        before = (home / "config.yaml").read_bytes()
        with patch("hermes_atlas_evolution.integration._read_hermes_config", side_effect=AssertionError("loader used")):
            data = environment._READ_CONFIG(home)
        self.assertEqual(data, {"plugins": {"enabled": ["a"]}})
        self.assertEqual((home / "config.yaml").read_bytes(), before)


class CatalogTest(Fleet):
    def test_the_leads_catalog_copy_comes_first_and_is_kept_in_the_database(self):
        lead_copy, other = self.root / "cache", self.profile("work") / "cache"
        for folder, count in ((lead_copy, 1), (other, 2)):
            folder.mkdir(parents=True)
            entries = [{"name": f"p{index}", "repo": f"https://github.com/x/p{index}", "tier": "official",
                        "capabilities": {"provides_hooks": ["pre_tool_call"]}} for index in range(count)]
            (folder / "plugin-catalog.json").write_text(json.dumps({"generated_at": "2026-11-16", "entries": entries,
                                                                    "removed": [{"name": "bad", "reason": "unsafe"}]}))
        os.utime(other / "plugin-catalog.json", (NOW + 10**6, NOW + 10**6))   # newer, but not the lead's
        catalog = ecosystem.load_catalog([self.root, self.root / "profiles" / "work"])
        self.assertEqual((catalog["source"], len(catalog["entries"])), ("cache", 1))
        self.assertEqual(ecosystem.lookup({"repo": "git@github.com:X/p0.git"}, catalog)["name"], "p0")
        with experience(self.root) as conn:
            self.assertTrue(ecosystem.sync(conn, catalog, ecosystem.knowledge()))
            self.assertFalse(ecosystem.sync(conn, catalog, ecosystem.knowledge()))   # unchanged: not rewritten
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM catalog_plugin").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT reason FROM catalog_removed").fetchone()[0], "unsafe")
            self.assertGreater(conn.execute("SELECT COUNT(*) FROM playbook").fetchone()[0], 10)
            self.assertEqual(conn.execute("SELECT level FROM playbook WHERE situation='plugin_removed'").fetchone()[0], "manual")

    def test_the_knowledge_base_ships_with_the_plugin(self):
        known = ecosystem.knowledge()
        self.assertTrue(known["plugins"]["security-guidance"]["protect"])
        self.assertIn("plugin_transform_overlap", known["playbook"])
        self.assertEqual(known["obsolete"]["1guy"]["state"], "archived")
        for situation, entry in known["playbook"].items():
            if not situation.startswith("_"):
                self.assertTrue(entry["title_pt"] and entry["advice_pt"] and entry["title_en"], situation)
        for kind in environment.KINDS:
            self.assertIn(kind, known["playbook"], kind)


class LeadAndRefreshTest(Fleet):
    def test_one_profile_looks_after_all_and_others_do_nothing(self):
        work = self.profile("work", enabled=["hermes-atlas-evolution"])
        plugin(work, "hermes-atlas-evolution")
        self.assertEqual(environment.lead(self.root, now=NOW), "default")
        with patch.object(environment, "scan", wraps=environment.scan) as scanned:
            self.assertIsNone(environment.refresh(work, now=NOW))
            first = environment.refresh(self.root, now=NOW)
            again = environment.refresh(self.root, now=NOW + 3600)
        self.assertEqual(scanned.call_count, 1)   # kept for six hours
        self.assertEqual(first["at"], again["at"])
        self.assertEqual(first["summary"]["profiles"], 2)
        self.configs[str(self.root.resolve())] = {"plugins": {"enabled": []}}
        self.assertEqual(environment.lead(self.root, now=NOW), "work")

    def test_the_lead_is_a_profile_that_actually_runs_passes(self):
        work = self.profile("work", enabled=["hermes-atlas-evolution"])
        plugin(work, "hermes-atlas-evolution")
        with experience(work) as conn:
            conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('loop_status', ?)", (json.dumps({"at": NOW - 60}),))
        with experience(self.root) as conn:   # default: plugin on, but its last pass was long ago
            conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('loop_status', ?)",
                         (json.dumps({"at": NOW - 30 * 86_400}),))
        self.assertEqual(environment.lead(self.root, now=NOW), "work")

    def test_a_forged_or_future_record_is_not_reused_and_never_breaks_a_screen(self):
        with experience(self.root) as conn:
            conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('environment', ?)",
                         (json.dumps({"at": 10**30, "findings": []}),))
            self.assertEqual(environment.stored(conn)["at"], 0.0)
        self.assertTrue(status.environment_markdown(self.root, "pt"))


class FleetActionsTest(Fleet):
    def runner(self, calls):
        def run(argv, **kwargs):
            calls.append((argv[1:], kwargs["env"]["HERMES_HOME"]))
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return run

    def test_a_change_runs_in_its_profile_and_is_undone_there(self):
        home = self.profile("work", enabled=["atlas-rtk", "rtk-rewrite"])
        plugin(home, "atlas-rtk", code=REWRITE_DIRECTIVE)
        plugin(home, "rtk-rewrite", code=REWRITE_IN_PLACE)
        item = next(found for found in self.findings() if found["kind"] == "plugin_duplicate")
        proposal = self.propose(item)
        calls = []
        with patch.object(admin, "hermes_executable", return_value="/usr/bin/hermes"), \
                patch("hermes_atlas_evolution.admin.run_command", self.runner(calls)):
            result = actions.apply_proposal(self.root, proposal, now=NOW)
            self.assertEqual(result["undo"], "hermes -p work plugins enable rtk-rewrite")
            undone = admin.decide(self.root, {}, {"kind": "undo", "id": result["decision"]}, now=NOW + 1)
        self.assertTrue(undone["undone"])
        self.assertEqual(calls, [(["--profile", "work", "plugins", "disable", "rtk-rewrite"], str(home.resolve())),
                                 (["--profile", "work", "plugins", "enable", "rtk-rewrite"], str(home.resolve()))])
        self.assertIn(item["code"], environment.state(self.root)["skip"])   # kept for any future lead

    def test_a_forged_proposal_or_cached_scan_runs_nothing(self):
        plugin(self.root, "hermes-atlas-evolution", repo="https://github.com/sesantanajr/hermes-atlas-evolution")
        home = self.profile("work", enabled=["guard-plugin", "other"], disabled=["hermes-atlas-evolution"])
        plugin(home, "guard-plugin", code=WEB_ONLY)
        plugin(home, "other", code=TERMINAL_ONLY)
        forged = [{"code": "env:plugin_duplicate:work:guard-plugin", "kind": "plugin_duplicate", "risk": "medium",
                   "subject": "work/guard-plugin", "evidence": {"profile": "work", "keep": "other"}},
                  {"code": "env:self_missing:work:hermes-atlas-evolution", "kind": "self_missing", "risk": "low",
                   "subject": "work/hermes-atlas-evolution", "evidence": {"profile": "work"}}]
        calls = []
        with patch.object(admin, "hermes_executable", return_value="/usr/bin/hermes"), \
                patch("hermes_atlas_evolution.admin.run_command", self.runner(calls)):
            for item in forged:
                proposal = self.propose(item)
                with self.assertRaises(actions.Stale):
                    actions.apply_proposal(self.root, proposal, now=NOW)
            with experience(self.root) as conn:
                conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('environment', ?)",
                             (json.dumps({"at": NOW, "findings": forged}),))
                conn.execute("UPDATE proposal SET status='open'")
            done = actions.automatic(self.root, now=NOW, mode="full", usage={})
        self.assertEqual(calls, [])
        self.assertEqual(done["applied"], [])

    def test_apply_all_leaves_out_what_is_decided_one_by_one(self):
        home = self.profile("work", enabled=["atlas-rtk", "rtk-rewrite"])
        plugin(home, "atlas-rtk", code=REWRITE_DIRECTIVE)
        plugin(home, "rtk-rewrite", code=REWRITE_IN_PLACE)
        item = next(found for found in self.findings() if found["kind"] == "plugin_duplicate")
        proposal = self.propose(item)
        result = actions.apply_many(self.root, [proposal], now=NOW)
        self.assertEqual(result, {"applied": [], "skipped": [{"id": proposal, "reason": "one_by_one"}]})

    def test_a_failed_install_folder_goes_to_the_trash_and_back(self):
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

    @POSIX_ONLY
    def test_the_trash_never_goes_through_a_link(self):
        home = self.profile("work")
        (home / "plugins" / ".install-aa1").mkdir(parents=True)
        elsewhere = self.root.parent / "elsewhere"
        elsewhere.mkdir()
        os.chmod(elsewhere, 0o755)
        (self.root / "evolution" / "trash").mkdir(parents=True, exist_ok=True)
        os.symlink(elsewhere, self.root / "evolution" / "trash" / "work")
        with self.assertRaises(ValueError):
            actions._trash(self.root, home, "work", ".install-aa1", back=False)
        self.assertTrue((home / "plugins" / ".install-aa1").is_dir())
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertEqual(oct(elsewhere.stat().st_mode & 0o777), "0o755")
        linked = self.profile("linked")
        os.symlink(home / "plugins", linked / "plugins")
        with self.assertRaises(ValueError):
            actions._trash(self.root, linked, "linked", ".install-aa1", back=False)

    def test_mode_auto_applies_only_the_safe_fleet_changes_with_time_to_finish(self):
        plugin(self.root, "hermes-atlas-evolution", repo="https://github.com/sesantanajr/hermes-atlas-evolution")
        home = self.profile("work", enabled=["atlas-rtk", "rtk-rewrite"])
        plugin(home, "atlas-rtk", code=REWRITE_DIRECTIVE)
        plugin(home, "rtk-rewrite", code=REWRITE_IN_PLACE)
        for item in self.findings():
            if item["kind"] in ("plugin_duplicate", "self_missing"):
                self.propose(item)
        calls = []
        with patch.object(admin, "hermes_executable", return_value="/usr/bin/hermes"), \
                patch("hermes_atlas_evolution.admin.run_command", self.runner(calls)):
            short = actions.automatic(self.root, now=NOW, mode="full", usage={}, deadline=time.monotonic() + 60)
            self.assertEqual((short["applied"], calls), ([], []))   # an install needs more than a minute left
            done = actions.automatic(self.root, now=NOW, mode="full", usage={})
        # the duplicate waits for the owner, and so does an install from the repository (not the catalog)
        self.assertEqual((calls, done["applied"]), ([], []))
        self.assertEqual([entry["reason"] for entry in done["waiting"]], ["not_catalog"])

    def test_the_weekly_reclaim_runs_hermes_pm_gc_once_a_week(self):
        from hermes_atlas_evolution import hermes_care

        ran = []
        fake = lambda home, key, runner=None, **kw: ran.append(key) or {"ok": True, "steps": []}  # noqa: E731
        # nothing a service still starts from is left unreferenced (gc_blocked), and the check taken again right
        # before gc (never this machine's) says the same
        care = {"findings": ["unused_tools"], "reclaimable": 700 * 2**20, "in_use_unreferenced": [],
                "gc_model": hermes_care.GC_MODEL}
        with experience(self.root) as conn:
            conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('care_check', ?)", (json.dumps(care),))
        with patch("hermes_atlas_evolution.hermes_care.run_action", fake), \
                patch("hermes_atlas_evolution.hermes_care.cached", lambda conn, home, **kw: dict(care)):
            first = actions.reclaim(self.root, now=NOW)
            second = actions.reclaim(self.root, now=NOW + 86_400)
            later = actions.reclaim(self.root, now=NOW + 8 * 86_400)
        self.assertEqual((ran, first["ok"], second), (["pm_gc", "pm_gc"], True, None))
        self.assertTrue(later["ok"])


class TrustTest(unittest.TestCase):
    def test_settings_json_may_only_narrow_the_mode(self):
        from hermes_atlas_evolution.cli import _trusted
        from hermes_atlas_evolution.evolve import options

        self.assertNotIn("mode", _trusted({"evolution": {"mode": "assist"}})["evolution"])
        self.assertEqual(_trusted({"evolution": {"mode": "manual"}})["evolution"]["mode"], "manual")
        self.assertNotIn("auto_actions", _trusted({"evolution": {"auto_actions": "safe"}})["evolution"])
        # the owner's 0.8.3 choice `auto_actions: off` in Hermes' config is not widened by an agent's settings.json
        merged = {"evolution": {"auto_actions": "off", **_trusted({"evolution": {"mode": "assist"}})["evolution"]}}
        self.assertEqual(options(merged, {})["mode"], "manual")


class ScreensTest(Fleet):
    def test_the_environment_screen_and_the_status_tab(self):
        home = self.profile("work", enabled=["atlas-rtk", "rtk-rewrite"])
        plugin(home, "atlas-rtk", code=REWRITE_DIRECTIVE)
        plugin(home, "rtk-rewrite", code=REWRITE_IN_PLACE)
        with patch("hermes_atlas_evolution.status._sessions", return_value=3):
            found = environment.refresh(self.root, now=NOW, force=True)
            for item in found["findings"]:
                self.propose(item)
            script = Script("14", "1", "", "", "0")
            with patch.dict(os.environ, {"NO_COLOR": "1"}):
                Menu(self.root, {}, lang="pt", console=Console(read=script.read, write=script.write, width=130, color=False),
                     clock=lambda: NOW).run()
            markdown = status.environment_markdown(self.root, "pt", mode="auto")
        text = script.text
        for expected in ("Ambiente: todos os perfis deste Hermes", "Perfis (2):", "Dois plugins fazem o mesmo trabalho",
                         "O que fazer:", "Fique com um", "hermes -p work plugins enable rtk-rewrite"):
            self.assertIn(expected, text)
        self.assertIn("| work | 3 |", markdown)
        self.assertIn("Dois plugins fazem o mesmo trabalho", markdown)
        self.assertIn("modo dele: Assistido", markdown)


if __name__ == "__main__":
    unittest.main()
