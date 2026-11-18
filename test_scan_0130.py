"""0.13.0: the scan of a large Hermes (an LRU sized to one scan, unchanged skill trees and plugin code not read again),
credential files never touched, a long session replayed in parts inside the time budget, a login cookie's name never
decides in the panel, and only Hermes' own commands with the plugin's name suggested."""

import ast
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_atlas_evolution import backfill, environment, status
from hermes_atlas_evolution.experience import experience
from hermes_atlas_evolution.operations import close_observers

from test_backfill import DAY, MISSING, NOW as HISTORY_NOW, History
from test_environment_085 import WEB_ONLY, plugin, skill
from test_panel_decisions import load_api

os.environ.setdefault("ATLAS_EVOLUTION_NO_HERMES", "1")   # never the real Hermes of this machine
NOW = time.time()
REPO = Path(__file__).resolve().parent.parent
RACY = 0.05   # the racy-change window these tests use instead of 2 s (a file this fresh is never cached)
CREDENTIAL = re.compile(r"(?i)(^|[\\/])(\.env|auth\.json|[^\\/]*\.env)$")


def settle():
    """Wait out the racy window, so what was just written can be cached."""
    time.sleep(RACY * 1.5)


class Folder(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.addCleanup(close_observers)
        self.base = Path(folder.name).resolve()
        self.root = self.base / ".hermes"
        self.root.mkdir()
        (self.root / "config.yaml").write_text("model: x\n", encoding="utf-8")
        racy = patch.object(environment, "_RACY_NS", int(RACY * 1e9), create=True)
        racy.start()
        self.addCleanup(racy.stop)
        self.config = {"plugins": {"enabled": ["web"]}}

    def profile(self, name: str) -> Path:
        home = self.root / "profiles" / name
        home.mkdir(parents=True)
        (home / "config.yaml").write_text("model: x\n", encoding="utf-8")
        return home

    def scan(self, **options):
        return environment.scan(self.root, now=NOW, read_config=lambda home: self.config, bundled=self.base / "bundled",
                                sessions=lambda home, since: 0, **options)


# ── 1. the scan of a large Hermes ────────────────────────────────────────────────────────────────────────────────
class ScanCostTest(Folder):
    def setUp(self):
        super().setUp()
        for home in (self.root, self.profile("work")):
            for index in range(12):
                skill(home, f"c{index % 3}/s{index:02d}", description=f"skill {index}", body="steps " * 20)
            plugin(home, "web", code=WEB_ONLY, hooks=["transform_tool_result"])
        settle()

    def test_the_skill_cache_has_a_ceiling_and_keeps_its_first_skills_past_it(self):
        # 0.12.0 cleared it whole at 20,000 skills (past that, every scan read every SKILL.md again); a cache sized to
        # every skill one scan can read (3,000 x 101 profiles) could hold ~386 MB in the gateway
        self.assertLessEqual(environment._SKILL_CACHE.cap, 100_000)
        self.assertGreaterEqual(environment._SKILL_CACHE.cap, 60_000)
        self.assertLessEqual(environment._TREES.cap, 150_000)
        small = environment._Recent(5, keep_first=True)
        read, real_read = [], environment.read_text

        def counting(path, limit, **options):
            read.append(os.fspath(path))
            return real_read(path, limit, **options)

        with patch.object(environment, "_SKILL_CACHE", small), \
                patch.object(environment, "_TREES", environment._Recent(4, weight=len, keep_first=True)):
            first = self.scan()   # 24 skills, 5 cached
            for _ in range(2):
                read.clear()
                with patch.object(environment, "read_text", counting):
                    again = self.scan()
                self.assertEqual(again["profiles"], first["profiles"])   # past the ceiling the scan is the same
                self.assertEqual(len([path for path in read if path.endswith("SKILL.md")]), 24 - 5)   # 5 still found
            self.assertEqual(len(small.items), 5)
        recent = environment._Recent(3)
        for key in "abc":
            recent.put(key, key.upper())
        self.assertEqual(recent.get("a"), "A")   # used: the least recently used is now b
        recent.put("d", "D")
        self.assertEqual(list(recent.items), ["c", "a", "d"])
        recent.trim(2)   # a whole scan keeps what it used
        self.assertEqual(list(recent.items), ["a", "d"])

    def test_an_unchanged_hermes_is_scanned_again_without_listing_reading_or_parsing(self):
        first = self.scan()
        listed, read, parsed = [], [], []
        real_scandir, real_read, real_parse = os.scandir, environment.read_text, ast.parse

        def scandir(path="."):
            listed.append(os.fspath(path))
            return real_scandir(path)

        def read_text(path, limit, **options):
            read.append(os.fspath(path))
            return real_read(path, limit, **options)

        def parse(*args, **options):
            parsed.append(1)
            return real_parse(*args, **options)

        with patch("os.scandir", scandir), patch.object(environment, "read_text", read_text), \
                patch.object(environment.ast, "parse", parse):
            second = self.scan()
        self.assertEqual(second["profiles"], first["profiles"])
        self.assertEqual([path for path in listed if f"{os.sep}skills" in path], [])   # one lstat per folder instead
        self.assertEqual([path for path in read if path.endswith(("SKILL.md", ".py"))], [])
        self.assertEqual(parsed, [])
        # a change is seen: a SKILL.md rewritten, a skill added, a skill removed (and let go by the cache)
        work = self.root / "profiles" / "work" / "skills"
        (work / "c0" / "s00" / "SKILL.md").write_text("---\nname: c0/s00\ndescription: rewritten\n---\nnew body\n")
        skill(self.root / "profiles" / "work", "c1/added", description="added", body="x")
        gone = work / "c2" / "s02" / "SKILL.md"
        shutil.rmtree(gone.parent)
        settle()
        rows = {item["path"]: item for item in self.scan()["profiles"][1]["skills"]}
        self.assertEqual(rows["c0/s00"]["description"], "rewritten")
        self.assertIn("c1/added", rows)
        self.assertNotIn("c2/s02", rows)
        self.assertNotIn(os.fspath(gone), environment._SKILL_CACHE.items)
        self.assertEqual([item["path"] for item in first["profiles"][0]["skills"]],
                         sorted((item["path"] for item in first["profiles"][0]["skills"]), key=lambda p: p.split("/")))

    def test_the_same_plugin_code_is_parsed_once_per_process_and_spends_the_same_budget(self):
        # code_hooks parsed the same copy again in every profile and every scan (20 profiles: 20 times a scan)
        copies = [self.root / "plugins" / "web", self.root / "profiles" / "work" / "plugins" / "web"]
        cold = {"left": 10_000}
        first = environment.code_hooks(copies[0], cold)
        parsed, read = [], []
        real_parse, real_read = ast.parse, environment.read_text
        with patch.object(environment.ast, "parse", lambda *a, **k: parsed.append(1) or real_parse(*a, **k)), \
                patch.object(environment, "read_text", lambda path, *a, **k: read.append(path) or real_read(path, *a, **k)):
            warm = {"left": 10_000}   # the next scan
            again = environment.code_hooks(copies[0], warm)
            other = environment.code_hooks(copies[1], {"left": 10_000})   # the same code in another profile
        self.assertEqual((again, warm["left"]), (first, cold["left"]))   # the budget is spent as if read
        self.assertEqual(other, first)
        self.assertEqual(parsed, [])
        self.assertEqual([path for path in read if Path(path).parent == copies[0]], [])   # unchanged: not read again
        self.assertIsNone(environment.code_hooks(copies[0], {"left": 0}))   # past the budget: still no conclusion
        again["transform_tool_result"]["tools"].append("terminal")   # a caller's copy, never the memo
        self.assertEqual(environment.code_hooks(copies[0], {"left": 10_000}), first)


# ── 2. catalog rule 11: credential files ─────────────────────────────────────────────────────────────────────────
class CredentialFileTest(Folder):
    def test_a_folder_holding_only_credentials_is_no_profile(self):
        creds = self.root / "profiles" / "creds"
        creds.mkdir(parents=True)
        (creds / ".env").write_text("KEY=value\n", encoding="utf-8")
        (creds / "auth.json").write_text("{}", encoding="utf-8")
        self.assertFalse(environment.is_profile(creds))
        self.assertEqual([name for name, _home in environment.profiles(self.root)], ["default"])
        (creds / "SOUL.md").write_text("me\n", encoding="utf-8")
        self.assertTrue(environment.is_profile(creds))
        elsewhere = self.base / "srv"
        (elsewhere / "profiles").mkdir(parents=True)
        (elsewhere / ".env").write_text("KEY=value\n", encoding="utf-8")
        self.assertFalse(environment._real_profiles_folder(elsewhere / "profiles"))
        self.assertTrue(environment.credential_file("Prod.ENV") and not environment.credential_file("venv"))

    @unittest.skipUnless(os.name == "posix", "POSIX: os.path probes go through os.stat and os.lstat")
    def test_a_scan_never_opens_measures_or_tests_a_credential_file(self):
        work = self.profile("work")
        for home in (self.root, work):
            for name in (".env", "auth.json", "prod.env"):
                (home / name).write_text("KEY=value\n", encoding="utf-8")
            folder = plugin(home, "web", code=WEB_ONLY, hooks=["transform_tool_result"])
            for name in (".env", "secrets.env", "auth.json"):
                (folder / name).write_text("KEY=value\n", encoding="utf-8")
            (folder / "sub").mkdir()
            (folder / "sub" / "auth.json").write_text("{}", encoding="utf-8")
            (home / "plugins" / "auth.json").write_text("{}", encoding="utf-8")
            (home / "plugins" / "x.env").write_text("KEY=value\n", encoding="utf-8")
            skill(home, "c0/s0", description="a skill", body="x")
            (home / "skills" / "c0" / ".env").write_text("KEY=value\n", encoding="utf-8")
            (home / "skills" / "c0" / "s0" / "auth.json").write_text("{}", encoding="utf-8")
        touched = []

        def watch(real):
            def call(path, *args, **options):
                if isinstance(path, (str, bytes, os.PathLike)) and CREDENTIAL.search(os.fsdecode(path)):
                    touched.append((real.__name__, os.fsdecode(path)))
                return real(path, *args, **options)
            return call

        class Entry:
            def __init__(self, entry):
                self.entry, self.name, self.path = entry, entry.name, entry.path

            def __getattr__(self, name):   # stat, is_dir, is_file, is_symlink, inode
                def call(*args, **options):
                    if CREDENTIAL.search(self.path):
                        touched.append((f"DirEntry.{name}", self.path))
                    return getattr(self.entry, name)(*args, **options)
                return call

        real_scandir = os.scandir

        class Listing:
            def __init__(self, path="."):
                self.found = real_scandir(path)

            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.found.close()

            def __iter__(self):
                return (Entry(entry) for entry in self.found)

            def close(self):
                self.found.close()

        with patch("os.stat", watch(os.stat)), patch("os.lstat", watch(os.lstat)), patch("os.open", watch(os.open)), \
                patch("builtins.open", watch(open)), patch("os.scandir", Listing):
            reading = self.scan()
            self.assertTrue(environment.is_profile(work))
            environment._created(work)
            status._profiles(self.root, now=NOW)
        self.assertEqual(touched, [])
        self.assertEqual([item["folder"] for item in reading["profiles"][1]["skills"]], ["s0"])

    @unittest.skipUnless(os.name == "posix" and shutil.which("sh"), "POSIX sh")
    def test_install_sh_never_takes_a_folder_of_credentials_for_a_profile(self):
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        log = self.base / "hermes.log"
        for name, body in (("hermes", f'printf "%s|%s\\n" "$HERMES_HOME" "$*" >> "{log}"\nexit 0\n'),
                           ("git", "exit 1\n"), ("systemctl", "exit 0\n")):
            (bin_dir / name).write_text("#!/bin/sh\n" + body, encoding="utf-8")
            (bin_dir / name).chmod(0o755)
        self.profile("work")
        creds = self.root / "profiles" / "creds"
        creds.mkdir()
        (creds / ".env").write_text("KEY=value\n", encoding="utf-8")
        (creds / "auth.json").write_text("{}", encoding="utf-8")
        done = subprocess.run(["sh", str(REPO / "install.sh"), "--all", "--yes", "--install-missing"],
                              env={"PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin", "HOME": str(self.base)},
                              cwd=str(self.base), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        homes = [line.split("|", 1)[0] for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(homes, [str(self.root), str(self.root / "profiles" / "work")])
        self.assertNotIn("creds", done.stdout)


# ── 3. a long session replayed in parts ──────────────────────────────────────────────────────────────────────────
class LongSessionTest(unittest.TestCase):
    TABLES = ("tool_daily", "failure_daily", "failure", "turn_daily", "skill_daily", "feedback_daily")

    def setUp(self):
        self.homes = []
        for _ in range(2):
            folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
            self.addCleanup(folder.cleanup)
            self.homes.append(Path(folder.name))
        self.addCleanup(close_observers)
        for name, value in (("PAUSE_MESSAGES", 40), ("_FORCE_PAUSE", 160)):
            patcher = patch.object(backfill, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def history(self, *, turns: int, calls_per_turn: int, uids: bool = True, compact: bool = False, waiting: int = 0):
        for home in self.homes:
            history = History(home)
            history.uids = uids
            history.session("long", start=HISTORY_NOW - DAY)
            clock = HISTORY_NOW - DAY + 10
            for index in range(waiting):   # messages sent before the agent answered: all one turn
                history.message("long", "user", clock + index, content=f"and also {index}")
            clock += waiting
            for index in range(turns):
                extra = tuple(("terminal", {"command": f"rg {index}-{n}"}, MISSING if n % 3 else "{}")
                              for n in range(calls_per_turn - 1))
                clock = history.tool_turn("long", clock, result=MISSING if index % 2 else "{}", extra_tools=extra) + 30
            if compact:   # copies of every row after it, same timestamps (no ids: byte for byte)
                history.compact("long")
            history.save()
            with experience(home) as conn:
                conn.execute("INSERT OR IGNORE INTO cursor VALUES ('observed_since', ?)", (repr(HISTORY_NOW),))

    def finish(self, home: Path, **options) -> dict:
        for _ in range(500):
            result = backfill.step(home, now=HISTORY_NOW, budget_s=10, **options)
            if result["status"] != "running":
                break
        close_observers()
        return result

    def tables(self, home: Path, names=TABLES) -> dict:
        conn = sqlite3.connect(home / "evolution" / "evolution.sqlite3")
        try:
            return {name: sorted(conn.execute(f"SELECT * FROM {name}").fetchall()) for name in names}
        finally:
            conn.close()

    def cursor(self, home: Path) -> str:
        with experience(home) as conn:
            return conn.execute("SELECT value FROM cursor WHERE key='backfill_cursor'").fetchone()[0]

    @contextmanager
    def batch_sizes(self, sizes: list):
        real = backfill.batched

        @contextmanager
        def counting(home, **options):
            with real(home, **options) as jobs:
                yield jobs
                sizes.append(len(jobs))

        with patch.object(backfill, "batched", counting):
            yield

    def test_a_long_session_stops_inside_it_at_the_budget_and_goes_on_from_there(self):
        # one session of 150,000 calls ran 46.6 s on a 20 s budget: the budget was checked only between 25 sessions
        self.history(turns=60, calls_per_turn=1)   # 240 messages, one session
        whole = self.finish(self.homes[0])
        flags = iter([False] + [True] * 1000)   # the budget runs out at the first check inside the session
        sizes = []
        with self.batch_sizes(sizes):
            first = backfill.step(self.homes[1], now=HISTORY_NOW, budget_s=10, cancelled=lambda: next(flags))
        close_observers()
        self.assertEqual(first["status"], "running")
        self.assertLess(first["tool_calls"], whole["tool_calls"])
        self.assertGreater(first["tool_calls"], 0)
        self.assertIn("|@", self.cursor(self.homes[1]))   # a cursor inside the session
        with self.batch_sizes(sizes):
            parts = self.finish(self.homes[1])
        self.assertEqual(parts, whole)
        self.assertEqual(self.tables(self.homes[1]), self.tables(self.homes[0]))   # cut between turns: nothing lost
        self.assertLessEqual(max(sizes) * 3, sum(sizes))   # written in parts, never the whole session at once

    def test_one_turn_longer_than_any_pause_is_cut_where_it_is(self):
        self.history(turns=1, calls_per_turn=150, uids=False)   # one user message, then 300 messages
        whole = self.finish(self.homes[0])
        flags = iter([False] + [True] * 1000)
        first = backfill.step(self.homes[1], now=HISTORY_NOW, budget_s=10, cancelled=lambda: next(flags))
        close_observers()
        self.assertLess(first["tool_calls"], 150)
        self.assertIn("|@", self.cursor(self.homes[1]))
        parts = self.finish(self.homes[1])
        self.assertEqual((parts["tool_calls"], parts["failures"], parts["sessions"]),
                         (whole["tool_calls"], whole["failures"], whole["sessions"]))
        names = ("tool_daily", "failure_daily", "failure")
        self.assertEqual(self.tables(self.homes[1], names), self.tables(self.homes[0], names))

    def test_a_cut_between_a_row_and_its_copy_counts_the_call_once(self):
        # review of 0.13.0: a forced cut between a row and its compaction copy (no message ids) counted it twice
        self.history(turns=1, calls_per_turn=150, uids=False, compact=True)   # orig, copy, orig, copy...
        whole = self.finish(self.homes[0])
        flags = iter([False] + [True] * 1000)
        with patch.object(backfill, "_FORCE_PAUSE", 42):   # the 42nd message read: a tool result, its copy next
            backfill.step(self.homes[1], now=HISTORY_NOW, budget_s=10, cancelled=lambda: next(flags))
        close_observers()
        cursor = self.cursor(self.homes[1])
        at, last = cursor.rsplit("|@", 1)[1].split("|")
        conn = sqlite3.connect(self.homes[1] / "state.db")
        try:
            self.assertEqual(conn.execute("SELECT role FROM messages WHERE id=?", (int(last),)).fetchone()[0], "tool")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages WHERE timestamp=? AND id>?",
                                          (float(at), int(last))).fetchone()[0], 1)   # its copy, after the cut
        finally:
            conn.close()
        parts = self.finish(self.homes[1])
        self.assertEqual((parts["tool_calls"], parts["failures"]), (whole["tool_calls"], whole["failures"]))
        self.assertEqual(parts["tool_calls"], 150)
        self.assertEqual(self.tables(self.homes[1], ("failure",)), self.tables(self.homes[0], ("failure",)))

    def test_a_turn_still_waiting_for_its_answer_is_never_cut(self):
        # cut among messages sent before the agent answered, the next one opened a turn of its own after the cut
        self.history(turns=1, calls_per_turn=2, waiting=200)
        whole = self.finish(self.homes[0])
        flags = iter([False] + [True] * 1000)
        backfill.step(self.homes[1], now=HISTORY_NOW, budget_s=10, cancelled=lambda: next(flags))
        close_observers()
        parts = self.finish(self.homes[1])
        self.assertEqual((whole["turns"], parts), (1, whole))

    def test_a_cursor_inside_a_session_is_read_back_and_a_damaged_one_never_replays_it_twice(self):
        home = self.homes[0]
        for value, inside, cursor in (("1.5|s1|@2.5|7", ((1.5, "s1"), (2.5, 7)), (1.5, "s1")),
                                      ('1.5|a|b|@2.0|"x"', ((1.5, "a|b"), (2.0, "x")), (1.5, "a|b")),
                                      ("1.5|s1|@x|[1]", None, (1.5, "s1|@x|[1]")),   # skips s1: never counted twice
                                      ("1.5|s1", None, (1.5, "s1"))):
            with experience(home) as conn:
                conn.execute("INSERT OR REPLACE INTO cursor VALUES ('backfill_cursor', ?)", (value,))
            state = backfill._state(home)
            self.assertEqual((state["inside"], state["cursor"]), (inside, cursor), value)
        self.assertEqual(backfill._cursor({"started_at": 1.5, "id": "s1"}, (2.5, 7)), "1.5|s1|@2.5|7")


# ── 4. the panel: a cookie's name is no login ────────────────────────────────────────────────────────────────────
class LoginCookieTest(unittest.TestCase):
    def test_without_hermes_verdict_a_login_cookie_decides_nothing(self):
        api = load_api()
        for cookie in ("hermes_session_at=abc", "__Host-hermes_session_at=abc", "theme=dark; hermes_session_at_gw1=x"):
            self.assertEqual(api.decision_policy({"cookie": cookie}, {}), (False, "open_dashboard"), cookie)
            self.assertEqual(api.decision_policy({"cookie": cookie}, {}, state=SimpleNamespace(),
                                                 app_state=SimpleNamespace()), (False, "open_dashboard"), cookie)
        # every other path as before: Hermes' verified session, its Desktop grant, its own "no login"
        verified = SimpleNamespace(session=SimpleNamespace(provider="basic"))
        self.assertEqual(api.decision_policy({}, {}, state=verified, app_state=SimpleNamespace(auth_required=True)),
                         (True, "login"))
        self.assertEqual(api.decision_policy({"x-hermes-gateway-ticket": "t"}, {}), (True, "desktop"))
        self.assertEqual(api.decision_policy({"cookie": "hermes_session_at=abc"}, {},
                                             app_state=SimpleNamespace(auth_required=False)), (False, "open_dashboard"))


# ── 5. suggestions: Hermes' own commands with the plugin's name ──────────────────────────────────────────────────
class SuggestionTest(Folder):
    def test_install_and_update_suggestions_name_the_plugin_never_a_url_a_commit_or_force(self):
        self.profile("work")
        legacy = self.profile("old")
        (legacy / "plugins" / "hermes-evolution").mkdir(parents=True)
        (legacy / "plugins" / "hermes-evolution" / "plugin.yaml").write_text("name: hermes-evolution\n")
        rows = {row["profile"]: row for row in status._profiles(self.root, now=NOW)}
        self.assertEqual(rows["work"]["install"], "hermes -p work plugins install hermes-atlas-evolution --enable")
        self.assertEqual(rows["old"]["migrate"], "hermes -p old plugins remove hermes-evolution && "
                                                 "hermes -p old plugins install hermes-atlas-evolution --enable")
        for row in rows.values():
            for command in (row["install"], row["migrate"]):
                self.assertNotRegex(command, r"github\.com|sesantanajr/|--ref|--force")
        self.assertEqual(environment._bring_current("work", False), "hermes -p work plugins update hermes-atlas-evolution")
        self.assertEqual(environment._update_command("other", {"profile": "work"}), "hermes -p work plugins update other")
        self.assertEqual(environment._self_install(None), "plugins install hermes-atlas-evolution --enable")
        for lang, texts in status._ENV_TEXT.items():
            self.assertIn("plugins update hermes-atlas-evolution", texts["self_skew"], lang)
            self.assertNotRegex(texts["pinned"] + texts["self_skew"], r"(?i)reposit|without `--ref`|sem `--ref`")

    def test_no_file_of_these_suggests_plugins_update_with_force(self):
        # `--force` on `plugins update` only skips Hermes' own scan prompt
        for name in ("hermes_atlas_evolution/status.py", "hermes_atlas_evolution/report.py",
                     "hermes_atlas_evolution/environment.py", "dashboard/plugin_api.py", "install.sh"):
            text = (REPO / name).read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"plugins update[^\n`\"']*--force", text), name)
            self.assertNotRegex(text, r"plugins install \{REPOSITORY\}", name)


class CareSizeTest(unittest.TestCase):
    def test_the_health_check_never_measures_a_credential_file(self):
        from unittest.mock import patch as _patch

        from hermes_atlas_evolution import hermes_care

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "plugins" / "x").mkdir(parents=True)
            (root / "plugins" / "x" / "code.py").write_text("x = 1\n", encoding="utf-8")
            for name in (".env", "prod.env", "auth.json"):
                (root / "plugins" / "x" / name).write_text("SECRET=1\n", encoding="utf-8")
            touched = []
            real_scandir = os.scandir

            class Entry:
                def __init__(self, entry):
                    self._entry, self.name, self.path = entry, entry.name, entry.path

                def is_dir(self, **kwargs):
                    return self._entry.is_dir(**kwargs)

                def is_file(self, **kwargs):
                    touched.append(self.name)
                    return self._entry.is_file(**kwargs)

                def stat(self, **kwargs):
                    touched.append(self.name)
                    return self._entry.stat(**kwargs)

            @contextmanager
            def scandir(path):
                with real_scandir(path) as entries:
                    yield [Entry(entry) for entry in entries]

            with _patch.object(hermes_care.os, "scandir", scandir):
                total = hermes_care._size(root, time.monotonic() + 10)
            self.assertEqual(total, 6)   # "x = 1\n" only
            self.assertEqual(sorted(set(touched)), ["code.py"])


if __name__ == "__main__":
    unittest.main()
