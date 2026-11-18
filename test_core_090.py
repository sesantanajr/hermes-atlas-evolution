"""0.9.0: Hermes' own doctor read for the owner (doctor.py), and the fixes of the audit of 0.8.5's decide-and-act path
(trash dates, the owner's removal kept, linked installs, transforms that return the original, the install commit of a
fork, Hermes' rule for profile names, stale changes not reopened, the gateway found by its command line)."""

import json
import os
import subprocess
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_atlas_evolution import actions, doctor, environment, explain, guard, hermes_care
from hermes_atlas_evolution.experience import experience

from test_environment_085 import NOW, POSIX_ONLY, Fleet, plugin
from test_guard_080 import HOME, PLUGIN, PROCESS, PROFILE, PROTECTED, TREES

os.environ.setdefault("ATLAS_EVOLUTION_NO_HERMES", "1")

DOCTOR_TEXT = """
◆ Directory Structure
  ✓ ~/.hermes directory exists
  ⚠ ~/.hermes/profiles/work/cache/uv/ is 1.5 GB and outside every pruner (only cache/scratch/ and cache/terminal/ are reaped) — move task files under cache/scratch/<task>/ or delete it
  ⚠ ~/.hermes/profiles/work/cache/documents/ is 2.3 GB and outside every pruner (only cache/scratch/ and cache/terminal/ are reaped) — move task files under cache/scratch/<task>/ or delete it
  ⚠ Filesystem checkpoints (/rollback) are on: 614.1 MB across 9 project(s), above the 500 MB cap (one snapshot per project is always kept). Not using /rollback? `hermes config set checkpoints.enabled false` then `hermes checkpoints clear`; or lower `checkpoints.retention_days`.
  ⚠ discord.py (optional, not installed)
  ⚠ MiniMax OAuth (not logged in)
  ⚠ x_search (missing XAI_API_KEY)
  ⚠ discord (missing DISCORD_BOT_TOKEN)
  ✗ ~/.hermes/.env file missing
    → Run 'hermes setup'
"""


def fake_result():
    return {"ok": True, "seconds": 6.5, "checks": [
        {"name": "_check_directory_structure", "section": "Directory Structure", "rows": doctor.rows(DOCTOR_TEXT),
         "issues": ["Run 'hermes setup' to configure missing API keys for full tool access"], "manual": [],
         "error": None},
        {"name": "_check_security_advisories", "section": "Security Advisories", "rows": [], "error": None,
         "issues": ["Resolve security advisory GHSA-x: uninstall evil==1.0 and rotate credentials, then run "
                    "`hermes doctor --ack GHSA-x`"], "manual": []},
        {"name": "_check_profiles", "section": None, "rows": [], "error": None, "issues": [],
         "manual": ["Something new Hermes says — run 'hermes frobnicate --now'"]}]}


class DoctorReading(unittest.TestCase):
    def test_rows_and_notes(self):
        found = doctor.rows(DOCTOR_TEXT)
        self.assertEqual(found[0], {"mark": "ok", "text": "~/.hermes directory exists", "notes": []})
        self.assertEqual(found[-1]["mark"], "fail")
        self.assertEqual(found[-1]["notes"], ["Run 'hermes setup'"])

    def test_problems_become_findings_and_optional_pieces_are_only_counted(self):
        found = {item["kind"] + ":" + item["subject"]: item for item in doctor.findings("work", Path("/x"), fake_result())}
        cache = found["doctor_cache:work/cache.documents"]
        self.assertEqual(cache["evidence"]["bytes"], int(2.3 * 2**30))
        self.assertFalse(cache["evidence"]["regenerable"])   # conversation attachments: the owner's
        self.assertIsNone(explain.plan(cache))
        self.assertTrue(found["doctor_cache:work/cache.uv"]["evidence"]["regenerable"])
        points = found["doctor_checkpoints:work/checkpoints"]
        self.assertEqual((points["evidence"]["projects"], points["command"]), (9, "hermes -p work checkpoints status"))
        keys = found["doctor_issue:work/tool_keys"]
        self.assertEqual(keys["evidence"]["tools"], ["x_search", "discord"])
        self.assertEqual(keys["command"], "hermes -p work tools")
        security = [item for item in found.values() if item["kind"] == "doctor_security"]
        self.assertEqual(len(security), 1)
        self.assertEqual(security[0]["risk"], "high")
        # its subject is the advisory's id (a new advisory never inherits a dismissed one's code) and the command
        # closes it once handled
        self.assertEqual(security[0]["subject"], "work/security.GHSA-x")
        self.assertEqual(security[0]["command"], "hermes -p work doctor --ack GHSA-x")
        unknown = [item for item in found.values() if item["kind"] == "doctor_issue" and not item["evidence"]["key"]]
        self.assertEqual(unknown[0]["command"], "hermes -p work frobnicate --now")
        counts = doctor.summary(fake_result())
        self.assertEqual((counts["fail"], counts["optional"], counts["issues"]), (1, 4, 3))
        two = doctor.findings("work", Path("/x"), {"ok": True, "checks": [{"name": "_check_plugins", "section": None,
              "rows": [], "error": None, "issues": [], "manual": [
                  "Plugin 'alpha' was turned off by an update; a later update retries it",
                  "Plugin 'beta' was turned off by an update; a later update retries it"]}]})
        self.assertEqual(sorted(item["subject"] for item in two),
                         ["work/plugin_off_update.alpha", "work/plugin_off_update.beta"])
        self.assertEqual(two[0]["command"], "hermes -p work plugins enable alpha")

    def test_words_in_both_languages(self):
        item = next(found for found in doctor.findings("work", Path("/x"), fake_result())
                    if found["subject"] == "work/tool_keys")
        pt = explain.describe(item, "pt")
        self.assertEqual(pt["title"], "Ferramentas ligadas sem chave")
        self.assertIn("`x_search`, `discord`", pt["what"])
        self.assertIn("o `hermes doctor` diz, em inglês", pt["what"])   # lower case after the colon (0.12.0)
        self.assertIn("hermes -p work setup", pt["advice"])   # what to do is in "what to do", with the profile
        self.assertIn("hermes -p work tools", pt["does"])
        en = explain.describe(item, "en")
        self.assertEqual(en["title"], "Tools that are on have no key")
        caches = {found["subject"]: found for found in doctor.findings("work", Path("/x"), fake_result())
                  if found["kind"] == "doctor_cache"}
        described = explain.describe(caches["work/cache.uv"], "pt")
        self.assertTrue(described["can_apply"])
        self.assertFalse(described["bulk"])   # one at a time, never in "apply all"
        self.assertIn("1,5 GB", described["what"])
        self.assertIn("lixeira do plugin", described["does"])
        self.assertEqual(explain.plan(caches["work/cache.uv"]), (["@work", "__trash__", "cache/uv"],
                                                                ["@work", "__restore__", "cache/uv"]))
        attachments = explain.describe(caches["work/cache.documents"], "pt")
        self.assertFalse(attachments["can_apply"])
        self.assertIn("anexados às conversas", attachments["what"])
        self.assertIn("apague só o que não precisa mais", attachments["advice"])

    def test_the_run_reads_only_its_marked_answer_and_never_fails(self):
        payload = {"checks": [{"name": "_check_x", "section": "X", "text": "  ⚠ thing (optional, not installed)\n",
                               "issues": [], "manual": [], "error": None}]}

        def runner(argv, **kwargs):
            self.assertEqual(argv[1:3], ["-I", "-c"])
            self.assertEqual(kwargs["env"]["HERMES_HOME"], str(Path("/p")))
            self.assertNotIn("OPENAI_API_KEY", kwargs["env"])   # a clean environment: never another profile's keys
            return subprocess.CompletedProcess(argv, 0, "noise\n" + doctor.MARKER + json.dumps(payload) + "\n", "")

        with patch.dict(os.environ, {"OPENAI_API_KEY": "x"}):
            result = doctor.run(Path("/p"), runner=runner)
        self.assertTrue(result["ok"])
        self.assertEqual(result["checks"][0]["rows"][0]["mark"], "warn")
        for out in ("", "garbage", doctor.MARKER + "{not json"):
            self.assertEqual(doctor.run(Path("/p"), runner=lambda argv, out=out, **_: subprocess.CompletedProcess(
                argv, 0, out, ""))["error"], "no_answer")

        def slow(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 1)

        self.assertEqual(doctor.run(Path("/p"), runner=slow)["error"], "timeout")
        old = doctor.run(Path("/p"), runner=lambda argv, **_: subprocess.CompletedProcess(
            argv, 0, doctor.MARKER + json.dumps({"error": "unavailable"}), ""))
        self.assertEqual(old, {"ok": False, "error": "unavailable", "seconds": old["seconds"]})
        # never the machine's Hermes in tests: a folder without config.yaml is not a profile, and nothing is started
        with patch.object(doctor, "_spawn", side_effect=AssertionError("Hermes was started")):
            self.assertEqual(doctor.run(Path("/p")), {"ok": False, "error": "not_a_profile"})


class DoctorInTheFleet(Fleet):
    def test_once_a_day_per_profile_in_use_and_joined_to_the_look(self):
        self.profile("work", enabled=["hermes-atlas-evolution"])
        calls = []

        def runner(argv, **kwargs):
            calls.append(kwargs["env"]["HERMES_HOME"])
            return subprocess.CompletedProcess(argv, 0, doctor.MARKER + json.dumps({"checks": [
                {"name": "_check_directory_structure", "section": "D", "text": DOCTOR_TEXT, "issues": [], "manual": [],
                 "error": None}]}), "")

        rows = self.scan()["profiles"]
        runs = doctor.collect(self.root, rows, now=NOW, runner=runner)
        self.assertEqual(len(calls), 2)
        self.assertEqual(set(runs), {"default", "work"})
        doctor.collect(self.root, rows, now=NOW + 3600, runner=runner)
        self.assertEqual(len(calls), 2)   # a day between runs
        doctor.collect(self.root, rows, now=NOW + 25 * 3600, runner=runner)
        self.assertEqual(len(calls), 4)

    def test_a_cache_folder_goes_to_the_trash_after_a_fresh_check_and_comes_back(self):
        home = self.profile("work", enabled=["hermes-atlas-evolution"])
        folder = home / "cache" / "uv"
        folder.mkdir(parents=True)
        (folder / "a.bin").write_text("x")
        item = doctor.findings("work", home, fake_result())[0]
        self.assertEqual(item["subject"], "work/cache.uv")
        proposal = self.propose(item)
        result = {"ok": True, "checks": [{"name": "_check_directory_structure", "section": "D", "error": None,
                                          "rows": doctor.rows(DOCTOR_TEXT), "issues": [], "manual": []}]}
        with patch.object(doctor, "run", return_value=result) as fresh:
            applied = actions.apply_proposal(self.root, proposal, now=NOW)
        self.assertEqual(fresh.call_args.kwargs["only"], ("_check_directory_structure",))
        kept = self.root / "evolution" / "trash" / "work" / "cache.uv"
        self.assertTrue((kept / "a.bin").is_file())
        self.assertFalse(folder.exists())
        self.assertEqual(applied["undo_args"], ["@work", "__restore__", "cache/uv"])
        folder.mkdir()   # the package manager made it again meanwhile
        with self.assertRaisesRegex(ValueError, "exists again"):
            actions._hermes(self.root, applied["undo_args"])
        folder.rmdir()
        actions._hermes(self.root, applied["undo_args"])
        self.assertTrue((folder / "a.bin").is_file())
        with self.assertRaises(ValueError):   # Hermes' own cleaner reaps these two
            actions._trash(self.root, home, "work", "cache/scratch", back=False)
        with self.assertRaises(ValueError):
            actions._trash(self.root, home, "work", "cache/../config", back=False)

    def test_a_doctor_problem_solved_meanwhile_runs_nothing_and_leaves_the_look(self):
        home = self.profile("work", enabled=["hermes-atlas-evolution"])
        (home / "cache" / "uv").mkdir(parents=True)
        item = doctor.findings("work", home, fake_result())[0]
        proposal = self.propose(item)
        with experience(self.root) as conn:
            conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('environment', ?)",
                         (json.dumps({"at": NOW, "findings": [item]}),))
            conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('doctor_runs', ?)",
                         (json.dumps({"work": {"at": NOW, "result": fake_result()}}),))
        with patch.object(doctor, "run", return_value={"ok": True, "checks": []}):
            with self.assertRaises(actions.Stale):
                actions.apply_proposal(self.root, proposal, now=NOW)
        self.assertTrue((home / "cache" / "uv").is_dir())
        with experience(self.root) as conn:
            self.assertEqual(environment.stored(conn)["findings"], [])
            self.assertNotIn("work", doctor.stored(conn))   # the stale run never reopens it

    def test_an_old_run_stops_speaking_and_a_machine_problem_is_said_once(self):
        self.profile("work", enabled=["hermes-atlas-evolution"])
        machine = {"ok": True, "checks": [{"name": "_check_gateway_supervision", "section": None, "rows": [],
                                           "error": None, "manual": [],
                                           "issues": ["Enable linger for the gateway user service: sudo loginctl "
                                                      "enable-linger $USER"]}]}
        with experience(self.root) as conn:
            conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('doctor_runs', ?)", (json.dumps({
                "default": {"at": NOW, "result": machine}, "work": {"at": NOW, "result": machine}}),))
        found = []
        with patch.object(doctor, "run", side_effect=AssertionError("fresh runs are not needed here")):
            environment._doctor(self.root, self.scan(), found, now=NOW + 3600, deadline=None, force=False)
        linger = [item for item in found if item["subject"].endswith("/linger")]
        self.assertEqual(len(linger), 1)
        self.assertEqual(linger[0]["evidence"]["profiles"], ["default", "work"])
        # 0.11.0: no sudo in what the plugin prints (Hermes' install scan); loginctl asks for a password if needed
        self.assertEqual(linger[0]["command"], "loginctl enable-linger $USER")
        later = []
        with patch.object(doctor, "collect", return_value={"default": {"at": NOW, "result": machine}}):
            environment._doctor(self.root, self.scan(), later, now=NOW + 4 * 86_400, deadline=None, force=False)
        self.assertEqual(later, [])


class TrashDates(Fleet):
    def test_the_trash_is_emptied_every_pass_after_thirty_days(self):
        home = self.profile("work")
        (home / "plugins" / ".install-a").mkdir(parents=True)
        actions._trash(self.root, home, "work", ".install-a", back=False)
        kept = self.root / "evolution" / "trash" / "work" / ".install-a"
        self.assertEqual(actions.empty_trash(self.root, now=time.time()), 0)
        old = time.time() - 31 * 86_400
        os.utime(kept, (old, old))
        self.assertEqual(actions.empty_trash(self.root, now=time.time()), 1)
        self.assertFalse(kept.exists())

    def test_an_old_folder_is_kept_thirty_days_from_the_day_it_entered(self):
        home = self.profile("work")
        leftover = home / "plugins" / ".install-old"
        leftover.mkdir(parents=True)
        os.utime(leftover, (NOW - 60 * 86_400, NOW - 60 * 86_400))
        actions._trash(self.root, home, "work", ".install-old", back=False)
        kept = self.root / "evolution" / "trash" / "work" / ".install-old"
        self.assertTrue(kept.is_dir())
        actions._trash(self.root, home, "work", ".install-old", back=True)   # the undo still finds it
        self.assertTrue(leftover.is_dir())


class OwnerDecisionsAndInstalls(Fleet):
    def test_a_profile_where_the_owner_removed_the_plugin_is_never_reinstalled(self):
        home = self.profile("work")
        (home / "evolution").mkdir()
        (home / "evolution" / "evolution.sqlite3").write_text("")
        kinds = {item["kind"] for item in self.findings() if item["evidence"].get("profile") == "work"}
        self.assertIn("self_off", kinds)
        self.assertNotIn("self_missing", kinds)

    @POSIX_ONLY
    def test_a_linked_development_checkout_is_installed(self):
        home = self.profile("work", enabled=["hermes-atlas-evolution"])
        source = plugin(self.root.parent / "dev", "hermes-atlas-evolution")
        (home / "plugins").mkdir(exist_ok=True)
        os.symlink(source, home / "plugins" / "hermes-atlas-evolution")
        self.assertTrue(environment.has_plugin(home, self.configs[str(home.resolve())]))
        kinds = {item["kind"] for item in self.findings() if item["evidence"].get("profile") == "work"}
        self.assertFalse(kinds & {"self_missing", "self_off"})

    def test_the_commit_is_pinned_only_for_the_published_repository(self):
        folder = self.root / "plugins" / "hermes-atlas-evolution"
        for repo, pinned in (("https://github.com/sesantanajr/hermes-atlas-evolution.git", True),
                             ("git@github.com:someone/hermes-atlas-evolution.git", False)):
            (folder / ".git" / "refs" / "heads").mkdir(parents=True, exist_ok=True)
            (folder / ".git" / "config").write_text(f'[remote "origin"]\n\turl = {repo}\n')
            (folder / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
            (folder / ".git" / "refs" / "heads" / "main").write_text("b" * 40 + "\n")
            self.assertEqual(environment.self_sha(self.root), "b" * 40 if pinned else "", repo)

    def test_only_names_hermes_accepts_are_profiles(self):
        for name in ("work.bak", "Work", "ok_name-2"):
            (self.root / "profiles" / name).mkdir(parents=True, exist_ok=True)
            (self.root / "profiles" / name / "config.yaml").write_text("{}\n")   # Hermes counts a folder with its files
        names = [name for name, _home in environment.profiles(self.root)]
        self.assertIn("ok_name-2", names)
        self.assertNotIn("work.bak", names)
        self.assertNotIn("Work", names)


class TransformsThatReturnTheOriginal(Fleet):
    def test_returning_the_result_unchanged_still_wins_over_a_later_plugin(self):
        first = '''
def keep(tool_name="", result=None, **_):
    if tool_name != "web_search":
        return result
    return result + " ranked"

def register(ctx):
    ctx.register_hook("transform_tool_result", keep)
'''
        later = '''
def mine(tool_name="", result=None, **_):
    if tool_name != "terminal":
        return None
    return result + " checked"

def register(ctx):
    ctx.register_hook("transform_tool_result", mine)
'''
        home = self.profile("work", enabled=["aaa-first", "zzz-later"])
        plugin(home, "aaa-first", hooks=["transform_tool_result"], code=first)
        plugin(home, "zzz-later", hooks=["transform_tool_result"], code=later)
        kinds = {item["kind"] for item in self.findings() if item["evidence"].get("profile") == "work"}
        self.assertTrue(kinds & {"plugin_transform_overlap", "plugin_transform_unclear"}, kinds)


class Agreement(unittest.TestCase):
    def test_words_follow_their_number(self):
        self.assertEqual(explain.agree("ele tem 1 plugin(s) ligado(s) e 44 skill(s)."), "ele tem 1 plugin ligado e 44 skills.")
        self.assertEqual(explain.agree("2 plugin(s) instalado(s) não carrega(m), porque não está(ão) lá"),
                         "2 plugins instalados não carregam, porque não estão lá")
        self.assertEqual(explain.agree("teve 1 sessão(ões); em 3 perfil(is)"), "teve 1 sessão; em 3 perfis")
        self.assertEqual(explain.agree("had 1 session(s) in 7 days"), "had 1 session in 7 days")


class Care(unittest.TestCase):
    OUTPUT = """
  ✓ fine
  ⚠ thing
  ✗ broken thing
────────────────────────────────────────
  Found 2 issue(s) to address:

  1. Run 'hermes setup' to configure missing API keys for full tool access
  2. Enable linger for the gateway user service: sudo loginctl enable-linger $USER

  Tip: run 'hermes doctor --fix' to auto-fix what's possible.
"""

    def test_the_doctors_list_of_what_to_do_is_kept(self):
        found = hermes_care.summarize(self.OUTPUT)
        self.assertEqual(found["found"], 2)
        self.assertEqual(len(found["actions"]), 2)
        self.assertTrue(found["actions"][1].startswith("Enable linger"))
        self.assertEqual(found["problems"], ["✗ broken thing"])

    def test_problems_found_are_an_answer_not_a_failure(self):
        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, self.OUTPUT, "")

        with patch("hermes_atlas_evolution.admin.hermes_executable", return_value="/bin/hermes"), \
                patch.object(Path, "is_file", return_value=True):
            result = hermes_care.run_action(Path("/p"), "doctor", runner=runner)
        self.assertTrue(result["ok"])
        self.assertEqual(result["problems_found"], 2)


class GatewayByCommandLine(unittest.TestCase):
    def setUp(self):
        windows, guard._WINDOWS = guard._WINDOWS, False
        self.addCleanup(setattr, guard, "_WINDOWS", windows)

    def test_a_kill_of_pids_looked_up_by_the_gateways_command_line_is_stopped(self):
        for command in ("kill $(pgrep -f 'gateway run')", "pgrep -f gateway | xargs kill",
                        "kill $(ps aux | awk '/gateway run/{print $2}')"):
            with self.subTest(command=command):
                found = guard.check("terminal", {"command": command}, protected=PROTECTED, home=HOME, profile=PROFILE,
                                    trees=TREES, own=(PLUGIN,), process=PROCESS)
                self.assertEqual(found and found["kind"], "gateway_stop")
        self.assertIsNone(guard.check("terminal", {"command": "kill $(pgrep -f myapp)"}, protected=PROTECTED,
                                      home=HOME, profile=PROFILE, trees=TREES, own=(PLUGIN,), process=PROCESS))
        for command in ("kill $(awk '{print $1}' /run/myapp.pid)", "kill $(pgrep -f /usr/bin/node)",
                        "kill $(grep -o '[0-9]*' /srv/app/app.pid)", "kill -HUP $(grep -m1 -o '^[0-9]*' nginx.pid)",
                        "kill $(pgrep -u root myworker)", "kill $(pgrep -x sys)", "kill $(grep -oP '\\d+' /tmp/app.pid)",
                        "kill $(rg -o '[0-9]+' pids.txt)"):
            with self.subTest(ordinary=command):   # ordinary administration: files read, option values, -x names
                self.assertIsNone(guard.check("terminal", {"command": command}, protected=PROTECTED, home=HOME,
                                              profile=PROFILE, trees=TREES, own=(PLUGIN,), process=PROCESS))


if __name__ == "__main__":
    unittest.main()
