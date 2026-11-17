"""0.8.3: proposals in plain words, one-click and automatic actions with undo, and the menu that suggests them."""

import contextlib
import io
import json
import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_atlas_evolution import actions, admin, decisions, explain, memory_care
from hermes_atlas_evolution.cli import _trusted, execute
from hermes_atlas_evolution.evolve import options
from hermes_atlas_evolution.experience import experience
from hermes_atlas_evolution.menu import Console, Menu

from test_menu_082 import Script, write_memory
from test_panel_decisions import Base, load_api

os.environ.setdefault("ATLAS_EVOLUTION_NO_HERMES", "1")   # never the real Hermes of this machine

NOW = time.time()


def propose(home: Path, kind: str, subject: str, evidence: dict, command: str, risk: str = "medium") -> int:
    with experience(home) as conn:
        cursor = conn.execute("""INSERT INTO proposal(code, kind, risk, subject, evidence, command, status, created, updated)
            VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)""", (f"{kind}:{subject}", kind, risk, subject, json.dumps(evidence),
                                                        command, NOW, NOW))
        return cursor.lastrowid


class Hermes:
    """A stand-in for `hermes ...`: records each argument list and answers like the real command."""

    def __init__(self, fail_on=None):
        self.calls, self.fail_on = [], fail_on

    def __call__(self, command, **kwargs):
        self.calls.append(command[1:])
        if self.fail_on and self.fail_on in command:
            return SimpleNamespace(returncode=1, stdout="", stderr="✗ skill not found")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")


LOCK = {"rewrites": 75, "flipflops": 0, "window_days": 7, "review_first": "hermes curator ledger --skill hermes-admin"}


class ExplainTest(unittest.TestCase):
    def test_every_kind_reads_as_a_sentence_with_its_numbers(self):
        samples = {
            "skill_lock": ("hermes-admin", LOCK),
            "skill_review": ("dogfood", {"unreused_rewrites": 5}),
            "skill_outcome": ("hermes-admin", {"turns": 8, "worse": ["interrupted_pct"],
                                               "skill_rates": {"interrupted_pct": 25.0},
                                               "profile_rates": {"interrupted_pct": 7.8}}),
            "investigate_failure": ("terminal", {"class": "command_failed", "count": 16, "days": 6, "hint": "exit_1"}),
            "investigate_latency": ("turns", {"p50_ms": 120000, "p95_ms": 2258601, "slow_pct": 54.49, "turns": 167}),
            "session_lifecycle": ("sessions", {"ended": 84, "orphan_pct": 54.8, "orphaned": 46}),
            "context_weight": ("context", {"avg_prompt_tokens": 90154, "cache_pct": 95.7, "calls": 2217,
                                           "candidates": ["skills.auto_load"]}),
            "plugin_health": ("hermes-security", {"declared": 0, "registered": 3}),
            "plugin_conflict": ("transform_tool_result", {"plugins": ["omh", "security-guidance"]}),
            "tool_latency": ("mcp__m365", {"calls": 45, "mean_s": 37.2, "total_min": 27.9}),
            "approval_policy": ("approvals", {"refused": 16, "unattended": 15, "by_setting": {"approvals.cron_mode": 5}}),
        }
        for kind, (subject, evidence) in samples.items():
            for lang in ("pt", "en"):
                told = explain.describe({"kind": kind, "subject": subject, "evidence": evidence, "risk": "medium"}, lang)
                self.assertNotIn(kind, told["title"], kind)
                self.assertTrue(told["what"] and "?" not in told["what"], (kind, lang, told["what"]))
        lock = explain.describe({"kind": "skill_lock", "subject": "hermes-admin", "evidence": LOCK, "risk": "medium"}, "pt")
        self.assertEqual((lock["title"], lock["risk"], lock["category"]), ("Travar skill reescrita em excesso", "médio", "change"))
        self.assertIn("75 vezes em 7 dias", lock["what"])
        self.assertEqual(lock["undo"], "hermes curator unlock hermes-admin")
        self.assertTrue(lock["automatic"])
        self.assertIn("54,5%", explain.describe({"kind": "investigate_latency", "subject": "turns", "risk": "medium",
                                                 "evidence": samples["investigate_latency"][1]}, "pt")["what"])

    def test_only_a_plain_skill_name_is_ever_applied(self):
        self.assertEqual(explain.plan({"kind": "skill_lock", "subject": "hermes-admin"}),
                         (["curator", "lock", "hermes-admin"], ["curator", "unlock", "hermes-admin"]))
        for subject in ("-rf", "a b", "x;y", "", None, "../x", "$(id)"):
            self.assertIsNone(explain.plan({"kind": "skill_lock", "subject": subject}), subject)
        self.assertIsNone(explain.plan({"kind": "skill_rollback", "subject": "x"}))   # not certain to undo


class ApplyTest(Base):
    def test_a_change_is_applied_with_hermes_and_undone_for_good(self):
        hermes = Hermes()
        proposal = propose(self.home, "skill_lock", "hermes-admin", LOCK, "hermes curator lock hermes-admin")
        with patch.object(admin, "hermes_executable", return_value="/usr/local/bin/hermes"):
            result = admin.decide(self.home, {}, {"kind": "apply", "id": proposal}, now=NOW, actor="dashboard",
                                  runner=hermes)
            self.assertTrue(result["applied"])
            self.assertEqual(hermes.calls[0], ["--profile", "default", "curator", "lock", "hermes-admin"])
            again = actions.apply_proposal(self.home, proposal, now=NOW, runner=hermes)
            self.assertEqual(again["reason"], "not_open")
            undone = admin.decide(self.home, {}, {"kind": "undo", "id": result["decision"]}, now=NOW, runner=hermes)
        self.assertTrue(undone["undone"])
        self.assertEqual(hermes.calls[-1], ["--profile", "default", "curator", "unlock", "hermes-admin"])
        with experience(self.home) as conn:
            status = conn.execute("SELECT status FROM proposal WHERE id=?", (proposal,)).fetchone()[0]
            skipped = conn.execute("SELECT 1 FROM cursor WHERE key='auto_skip:skill_lock:hermes-admin'").fetchone()
        self.assertEqual(status, "rejected")
        self.assertIsNotNone(skipped)

    def test_hermes_refusing_leaves_the_proposal_open(self):
        proposal = propose(self.home, "skill_lock", "gone", LOCK, "hermes curator lock gone")
        with patch.object(admin, "hermes_executable", return_value="/usr/local/bin/hermes"), \
                self.assertRaises(ValueError) as refused:
            actions.apply_proposal(self.home, proposal, now=NOW, runner=Hermes(fail_on="gone"))
        self.assertIn("skill not found", str(refused.exception))
        with experience(self.home) as conn:
            self.assertEqual(conn.execute("SELECT status FROM proposal WHERE id=?", (proposal,)).fetchone()[0], "open")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0], 0)

    def test_advice_is_never_applied(self):
        proposal = propose(self.home, "context_weight", "context", {"calls": 3}, "hermes prompt-size", "low")
        with self.assertRaises(ValueError):
            actions.apply_proposal(self.home, proposal, now=NOW, runner=Hermes())

    def test_terminal_approve_applies_a_change(self):
        proposal = propose(self.home, "skill_lock", "hermes-admin", LOCK, "hermes curator lock hermes-admin")
        (self.home / "config.yaml").write_text("model: test\n")   # Hermes runs only for a real profile
        hermes = Hermes()
        stream = io.StringIO()
        with patch.object(admin, "hermes_executable", return_value="/usr/local/bin/hermes"), \
                patch("hermes_atlas_evolution.admin.subprocess.run", hermes), contextlib.redirect_stdout(stream):
            execute(SimpleNamespace(lang="en", evolution_action="proposals", proposals_action="approve", id=proposal,
                                    note=None, yes=True), self.home, runtime_config={})
        self.assertTrue(json.loads(stream.getvalue())["applied"])
        self.assertEqual(hermes.calls[0][-3:], ["curator", "lock", "hermes-admin"])


class AutomaticTest(Base):
    FACT = "Reports go to the owner in Brazilian Portuguese."

    def test_safe_takes_out_identical_copies_only(self):
        path = write_memory(self.home, "MEMORY.md", [self.FACT, "Other fact.", self.FACT.upper(),
                                                     "Reports go to the owner in Brazilian Portuguese, always."])
        write_memory(self.home, "USER.md", [self.FACT])   # the very same text in both files
        lock = propose(self.home, "skill_lock", "hermes-admin", LOCK, "hermes curator lock hermes-admin")
        hermes = Hermes()
        done = actions.automatic(self.home, now=NOW, mode="safe", runner=hermes, usage={})
        self.assertEqual([item["file"] for item in done["memory"]], ["MEMORY.md"])   # USER.md keeps it
        self.assertEqual(hermes.calls, [])   # safe never changes Hermes' settings or skills
        # the upper-case variant and the similar entry stay: they differ, so they are the owner's to decide
        self.assertEqual(memory_care.parse(path.read_text(encoding="utf-8")),
                         ["Other fact.", self.FACT.upper(), "Reports go to the owner in Brazilian Portuguese, always."])
        with experience(self.home) as conn:
            row = conn.execute("SELECT actor, kind FROM decision_log").fetchone()
            self.assertEqual(tuple(row), ("automatic", "memory"))
            self.assertEqual(conn.execute("SELECT status FROM proposal WHERE id=?", (lock,)).fetchone()[0], "open")
        self.assertEqual(actions.automatic(self.home, now=NOW, mode="off", runner=hermes)["memory"], [])

    def test_full_locks_unstable_skills_within_limits_and_respects_undo(self):
        ids = [propose(self.home, "skill_lock", f"skill-{n}", LOCK, f"hermes curator lock skill-{n}") for n in range(5)]
        hermes = Hermes()
        with experience(self.home) as conn:
            conn.execute("INSERT INTO cursor(key, value) VALUES ('auto_skip:skill_lock:skill-0', '1')")
        with patch.object(admin, "hermes_executable", return_value="/usr/local/bin/hermes"):
            done = actions.automatic(self.home, now=NOW, mode="full", runner=hermes,
                                     usage={"skill-1": {"locked": True, "pinned": False}})
        self.assertEqual([item["subject"] for item in done["applied"]], ["skill-2", "skill-3", "skill-4"])
        self.assertEqual(len(hermes.calls), actions.MAX_CHANGES_PER_PASS)
        with experience(self.home) as conn:
            status = dict(conn.execute("SELECT id, status FROM proposal").fetchall())
            actors = {row[0] for row in conn.execute("SELECT actor FROM decision_log")}
        self.assertEqual(status[ids[0]], "open")      # the owner undid it once: never again by itself
        self.assertEqual(status[ids[1]], "applied")   # already locked: nothing to run
        self.assertEqual(actors, {"automatic"})

    def test_a_failure_is_reported_and_the_pass_goes_on(self):
        propose(self.home, "skill_lock", "gone", LOCK, "hermes curator lock gone")
        with patch.object(admin, "hermes_executable", return_value="/usr/local/bin/hermes"):
            done = actions.automatic(self.home, now=NOW, mode="full", runner=Hermes(fail_on="gone"), usage={})
        self.assertEqual(done["applied"], [])
        self.assertEqual(len(done["failed"]), 1)

    def test_the_mode_defaults_to_auto_and_the_agent_cannot_widen_it(self):
        self.assertEqual((options({}, {})["mode"], options({}, {})["auto_actions"]), ("auto", "full"))
        # an owner who chose the 0.8.3 setting keeps that choice
        self.assertEqual(options({"evolution": {"auto_actions": False}}, {})["mode"], "manual")
        self.assertEqual(options({"evolution": {"auto_actions": "safe"}}, {})["mode"], "assist")
        self.assertEqual(options({"evolution": {"auto_actions": "safe", "mode": "auto"}}, {})["mode"], "auto")
        self.assertEqual(options({"evolution": {"mode": "maybe"}}, {}, strict=False)["auto_actions"], "off")
        with self.assertRaises(ValueError):
            options({"evolution": {"mode": "maybe"}}, {}, strict=True)
        self.assertNotIn("mode", _trusted({"evolution": {"mode": "auto"}})["evolution"])
        self.assertNotIn("auto_actions", _trusted({"evolution": {"auto_actions": "full"}})["evolution"])
        self.assertEqual(_trusted({"evolution": {"mode": "manual"}})["evolution"]["mode"], "manual")

class MenuActionsTest(Base):
    def menu(self, *answers, lang="en"):
        script = Script(*answers)
        with patch.object(admin, "hermes_executable", return_value="/usr/local/bin/hermes"), \
                patch("hermes_atlas_evolution.admin.subprocess.run", self.hermes):
            Menu(self.home, {}, lang=lang, console=Console(read=script.read, write=script.write, width=110, color=False),
                 clock=lambda: NOW).run()
        return script.text

    def setUp(self):
        super().setUp()
        self.hermes = Hermes()
        (self.home / "config.yaml").write_text("model: test\n")   # Hermes runs only for a real profile
        environment = patch.dict(os.environ, {"NO_COLOR": "1"})
        environment.start()
        self.addCleanup(environment.stop)

    def test_the_home_screen_suggests_and_applies_the_changes_in_portuguese(self):
        propose(self.home, "skill_lock", "hermes-admin", LOCK, "hermes curator lock hermes-admin")
        propose(self.home, "tool_latency", "mcp__m365", {"calls": 45, "mean_s": 37.2, "total_min": 27.9},
                "hermes evolution report --days 7", "low")
        shown = self.menu("a", "s", "0", lang="pt")
        self.assertIn("Sugestões:", shown)
        self.assertIn("Aplicar 1 mudança recomendada: Travar skill reescrita em excesso", shown)
        self.assertIn("1 aplicada.", shown)
        self.assertEqual(self.hermes.calls[0][-3:], ["curator", "lock", "hermes-admin"])

    def test_proposals_are_grouped_explained_and_dismissed(self):
        propose(self.home, "skill_lock", "hermes-admin", LOCK, "hermes curator lock hermes-admin")
        advice = propose(self.home, "tool_latency", "mcp__m365", {"calls": 45, "mean_s": 37.2, "total_min": 27.9},
                         "hermes evolution report --days 7", "low")
        shown = self.menu("2", "2", "d", "", "0", lang="pt")
        for text in ("Mudanças que o plugin aplica (reversíveis):", "Observações:",
                     "[risco: baixo] Ferramenta lenta chamada após chamada · mcp__m365 (proposta #2)",
                     "levou em média 37,2 s por chamada",
                     "Dispensada: some por 30 dias e só volta se a situação continuar."):
            self.assertIn(text, shown)
        self.assertNotIn("tool_latency", shown)
        with experience(self.home) as conn:
            self.assertEqual(conn.execute("SELECT status FROM proposal WHERE id=?", (advice,)).fetchone()[0], "rejected")

    def test_a_failure_without_a_lesson_gets_one_from_the_menu(self):
        propose(self.home, "investigate_failure", "terminal", {"class": "command_failed", "count": 16, "days": 6,
                                                               "hint": "exit_1"}, "hermes evolution lessons add", "low")
        shown = self.menu("2", "1", "e", "Before running a long command, set a timeout and run it in the background.",
                          "y", "", "0")
        self.assertIn("Lesson #1 added", shown)
        with experience(self.home) as conn:
            row = conn.execute("SELECT tool, origin, status FROM lesson").fetchone()
        self.assertEqual(tuple(row), ("terminal", "operator", "active"))

    def test_the_automatic_screen_changes_the_mode_and_undoes(self):
        write_memory(self.home, "MEMORY.md", ["Same.", "Same."])
        actions.automatic(self.home, now=NOW, mode="safe")
        written = []

        def writer(values):
            written.append(values)
            return list(values)

        script = Script("5", "1", "y", "m", "2", "", "0")
        Menu(self.home, {}, lang="en", console=Console(read=script.read, write=script.write, width=110, color=False),
             clock=lambda: NOW, writer=writer).run()
        self.assertIn("removed a duplicate entry from MEMORY.md", script.text)
        self.assertIn("Undone.", script.text)
        self.assertEqual(written, [{"mode": "assist"}])
        self.assertEqual(memory_care.parse((self.home / "memories" / "MEMORY.md").read_text(encoding="utf-8")),
                         ["Same.", "Same."])
        self.assertEqual(actions.automatic(self.home, now=NOW, mode="safe")["memory"], [])   # put back: never again


class PanelTest(Base):
    def test_the_snapshot_explains_each_proposal_and_lists_automatic_actions(self):
        api = load_api()
        propose(self.home, "skill_lock", "hermes-admin", LOCK, "hermes curator lock hermes-admin")
        write_memory(self.home, "USER.md", ["A.", "A."])
        actions.automatic(self.home, now=NOW, mode="safe")
        snap = api.snapshot(self.home, now=NOW, runtime_config={}, lang="pt", can_decide=True)
        told = snap["proposals"][0]["explain"]
        self.assertEqual((told["title"], told["category"], told["can_apply"]),
                         ("Travar skill reescrita em excesso", "change", True))
        self.assertEqual(snap["automatic"]["mode"], "auto")
        self.assertEqual(snap["automatic"]["recent"][0]["kind"], "memory")
        self.assertEqual(snap["settings"]["values"]["mode"], "auto")
        self.assertNotIn("mode", snap["settings"]["recommended"])   # already auto by default
        with experience(self.home) as conn:
            self.assertEqual(decisions.history(conn)[0]["actor"], "automatic")


class Review083Test(Base):
    """The independent review of 0.8.3: each confirmed finding, as a test."""

    def setUp(self):
        super().setUp()
        executable = patch.object(admin, "hermes_executable", return_value="/usr/local/bin/hermes")
        executable.start()
        self.addCleanup(executable.stop)

    def test_safe_never_touches_entries_that_only_look_alike(self):
        pairs = [("Keys live in /opt/App/keys.", "Keys live in /opt/app/keys."),
                 ("Password hint: Abc-123.", "password hint: abc-123."),
                 ("deploy:\n  steps: 2", "deploy:\n    steps: 2"),
                 ("Water is H₂O.", "Water is H2O.")]
        entries = [text for pair in pairs for text in pair]
        path = write_memory(self.home, "MEMORY.md", entries)
        self.assertEqual(actions.automatic(self.home, now=NOW, mode="full", usage={}, runner=Hermes())["memory"], [])
        self.assertEqual(memory_care.parse(path.read_text(encoding="utf-8")), entries)

    def test_terminal_undo_of_automatic_actions(self):
        write_memory(self.home, "MEMORY.md", ["Same fact.", "Same fact."])
        removed = actions.automatic(self.home, now=NOW, mode="safe")["memory"][0]["decision"]
        proposal = propose(self.home, "skill_lock", "hermes-admin", LOCK, "hermes curator lock hermes-admin")
        hermes = Hermes()
        applied = actions.apply_proposal(self.home, proposal, now=NOW, runner=hermes, usage={})["decision"]
        for decision in (removed, applied):
            stream = io.StringIO()
            (self.home / "config.yaml").write_text("model: test\n")
            with patch("hermes_atlas_evolution.admin.subprocess.run", hermes), contextlib.redirect_stdout(stream), \
                    patch.object(admin, "hermes_executable", return_value="/usr/local/bin/hermes"):
                execute(SimpleNamespace(lang="en", evolution_action="decisions", decisions_action="undo", id=decision),
                        self.home, runtime_config={})
            self.assertTrue(json.loads(stream.getvalue())["undone"], decision)
        self.assertEqual(hermes.calls[-1][-3:], ["curator", "unlock", "hermes-admin"])

    def test_the_undo_is_derived_never_read_from_the_record(self):
        proposal = propose(self.home, "skill_lock", "harmless", LOCK, "hermes curator lock harmless")
        hermes = Hermes()
        decision = actions.apply_proposal(self.home, proposal, now=NOW, runner=hermes, usage={})["decision"]
        with experience(self.home) as conn:   # what an agent writing the database directly could do
            conn.execute("UPDATE decision_log SET after=? WHERE id=?", (json.dumps(
                {"status": "applied", "code": "skill_lock:harmless", "applied": ["curator", "lock", "harmless"],
                 "undo": ["config", "set", "plugins.entries.hermes-atlas-evolution.settings.auto_actions", "full"]}),
                decision))
        admin.decide(self.home, {}, {"kind": "undo", "id": decision}, now=NOW, runner=hermes)
        self.assertEqual(hermes.calls[-1][-3:], ["curator", "unlock", "harmless"])
        other = propose(self.home, "skill_lock", "other", LOCK, "hermes curator lock other")
        second = actions.apply_proposal(self.home, other, now=NOW, runner=hermes, usage={})["decision"]
        with experience(self.home) as conn:
            conn.execute("UPDATE decision_log SET after=? WHERE id=?", (json.dumps(
                {"status": "applied", "applied": ["config", "set", "x", "y"]}), second))
        calls = len(hermes.calls)
        self.assertFalse(admin.decide(self.home, {}, {"kind": "undo", "id": second}, now=NOW, runner=hermes)["undone"])
        self.assertEqual(len(hermes.calls), calls)

    def test_automatic_applies_only_what_the_loop_wrote_itself(self):
        with experience(self.home) as conn:   # a row whose kind and subject were changed after the loop wrote it
            conn.execute("""INSERT INTO proposal(code, kind, risk, subject, evidence, command, status, created, updated)
                VALUES ('slow_turns', 'skill_lock', 'medium', 'victim', '{}', 'x', 'open', 1, 1)""")
        hermes = Hermes()
        self.assertEqual(actions.automatic(self.home, now=NOW, mode="full", runner=hermes, usage={})["applied"], [])
        self.assertEqual(hermes.calls, [])

    def test_a_decision_made_while_hermes_runs_finds_the_proposal_taken(self):
        from hermes_atlas_evolution import proposals

        proposal = propose(self.home, "skill_lock", "hermes-admin", LOCK, "hermes curator lock hermes-admin")
        refused = []

        def meanwhile(command, **kwargs):
            with experience(self.home) as conn:
                try:
                    proposals.decide(conn, proposal, "reject", now=NOW)
                except ValueError as error:
                    refused.append(str(error))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        self.assertTrue(actions.apply_proposal(self.home, proposal, now=NOW, runner=meanwhile, usage={})["applied"])
        self.assertEqual(len(refused), 1)
        self.assertEqual(actions.apply_proposal(self.home, proposal, now=NOW, runner=meanwhile, usage={})["reason"],
                         "not_open")
        with experience(self.home) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0], 1)

    def test_the_owners_own_lock_is_never_undone(self):
        proposal = propose(self.home, "skill_lock", "hermes-admin", LOCK, "hermes curator lock hermes-admin")
        hermes = Hermes()
        result = actions.apply_proposal(self.home, proposal, now=NOW, runner=hermes,
                                        usage={"hermes-admin": {"locked": True}})
        self.assertEqual((result["applied"], result["reason"], hermes.calls), (False, "already", []))
        with experience(self.home) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0], 0)

    def test_a_proposal_after_the_hundredth_still_applies(self):
        for n in range(105):
            propose(self.home, "investigate_failure", f"tool{n}", {"count": 5}, "x", "low")
        proposal = propose(self.home, "skill_lock", "hermes-admin", LOCK, "hermes curator lock hermes-admin")
        self.assertTrue(actions.apply_proposal(self.home, proposal, now=NOW, runner=Hermes(), usage={})["applied"])

    def test_a_refusal_waits_a_day_and_a_pass_near_its_limit_runs_no_command(self):
        import time as clock

        propose(self.home, "skill_lock", "gone", LOCK, "hermes curator lock gone")
        propose(self.home, "skill_lock", "other", LOCK, "hermes curator lock other")
        failing = Hermes(fail_on="gone")
        first = actions.automatic(self.home, now=NOW, mode="full", runner=failing, usage={})
        # one refusal does not hold up the others (0.8.5); two in one pass stop it
        self.assertEqual((len(first["failed"]), len(failing.calls)), (1, 2))
        self.assertEqual([item["subject"] for item in first["applied"]], ["other"])
        second = actions.automatic(self.home, now=NOW + 60, mode="full", runner=failing, usage={})
        self.assertEqual((second["applied"], second["failed"]), ([], []))   # the refused one waits a day
        late = Hermes()
        actions.automatic(self.home, now=NOW + 120, mode="full", runner=late, usage={}, deadline=clock.monotonic() + 5)
        self.assertEqual(late.calls, [])
        tomorrow = actions.automatic(self.home, now=NOW + 90_000, mode="full", runner=Hermes(fail_on="gone"), usage={})
        self.assertEqual(len(tomorrow["failed"]), 1)   # tried again a day later

    def test_apply_all_is_one_sequential_decision(self):
        ids = [propose(self.home, "skill_lock", f"s{n}", LOCK, f"hermes curator lock s{n}") for n in range(3)]
        hermes = Hermes(fail_on="s1")
        result = admin.decide(self.home, {}, {"kind": "apply_all", "ids": ids}, now=NOW, runner=hermes)
        self.assertEqual(result["applied"], [ids[0], ids[2]])
        self.assertEqual([item["id"] for item in result["skipped"]], [ids[1]])

    def test_radar_findings_read_as_sentences(self):
        for kind, subject, evidence in (("plugin_health", "pre_tool_call", {"blocked_calls": 4}),
                                        ("plugin_health", "broken", {"error": "ImportError"}),
                                        ("plugin_conflict", "pre_tool_call", {"failed_calls": 7}),
                                        ("compaction", "compression", {"compactions": 5, "kept_pct": 92.0})):
            for lang in ("pt", "en"):
                what = explain.describe({"kind": kind, "subject": subject, "evidence": evidence, "risk": "low"}, lang)["what"]
                self.assertTrue(what and "?" not in what and "  " not in what, (kind, lang, what))

    def test_the_attention_box_fills_in_its_values(self):
        with experience(self.home) as conn:
            conn.execute("INSERT INTO cursor(key, value) VALUES ('settings_reload', '[\"runtime_guard\"]')")
        script = Script("0")
        Menu(self.home, {}, lang="en", console=Console(read=script.read, write=script.write, width=110, color=False),
             clock=lambda: NOW).run()
        self.assertIn("apply after hermes gateway restart (in the terminal, at the next session): runtime_guard.",
                      " ".join(script.text.split()))   # plain text, no backticks (wrapped to the terminal's width)
        self.assertNotIn("{keys}", script.text)


class Recheck083Test(Base):
    """The second pass of the review on the fixes: each confirmed finding, as a test."""

    def setUp(self):
        super().setUp()
        executable = patch.object(admin, "hermes_executable", return_value="/usr/local/bin/hermes")
        executable.start()
        self.addCleanup(executable.stop)

    def test_a_claim_taken_back_meanwhile_records_nothing(self):
        proposal = propose(self.home, "skill_lock", "hermes-admin", LOCK, "hermes curator lock hermes-admin")

        def another_process(command, **kwargs):   # it thought this claim dead, applied and recorded it
            with experience(self.home) as conn:
                conn.execute("UPDATE proposal SET status='applied' WHERE id=?", (proposal,))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        result = actions.apply_proposal(self.home, proposal, now=NOW - 2000, runner=another_process, usage={})
        self.assertEqual(result["reason"], "not_open")
        with experience(self.home) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0], 0)
            claimed = conn.execute("SELECT updated FROM proposal WHERE id=?", (proposal,)).fetchone()[0]
        self.assertGreater(claimed, NOW - 1000)   # claims carry the real clock, never the pass's start time

    def test_a_claim_the_loop_did_not_write_expires_before_it_can_apply(self):
        from hermes_atlas_evolution import proposals

        with experience(self.home) as conn:
            conn.execute("""INSERT INTO proposal(code, kind, risk, subject, evidence, command, status, created, updated)
                VALUES ('skill_lock:victim', 'skill_lock', 'medium', 'victim', '{}', 'x', 'applying', 0, 0)""")
            actions.recover(conn)
            proposals.refresh(conn, now=NOW)
            self.assertEqual(conn.execute("SELECT status FROM proposal").fetchone()[0], "expired")
        hermes = Hermes()
        actions.automatic(self.home, now=NOW, mode="full", runner=hermes, usage={})
        self.assertEqual(hermes.calls, [])

    def test_a_lock_kept_under_the_folder_name_is_the_owners(self):
        folder = self.home / "skills" / "ops" / "nested-dir"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text("---\nname: nested-skill\ndescription: x\n---\nBody\n", encoding="utf-8")
        proposal = propose(self.home, "skill_lock", "nested-skill", LOCK, "hermes curator lock nested-skill")
        hermes = Hermes()
        result = actions.apply_proposal(self.home, proposal, now=NOW, runner=hermes,
                                        usage={"nested-dir": {"locked": True}})
        self.assertEqual((result["reason"], hermes.calls), ("already", []))

    def test_a_name_hermes_would_refuse_never_holds_up_the_others(self):
        propose(self.home, "skill_lock", "análise dados", LOCK, "x")
        propose(self.home, "skill_lock", "good-skill", LOCK, "hermes curator lock good-skill")
        done = actions.automatic(self.home, now=NOW, mode="full", runner=Hermes(), usage={})
        self.assertEqual(([item["subject"] for item in done["applied"]], done["failed"]), (["good-skill"], []))

    def test_settings_the_agent_tried_to_widen_are_listed_once(self):
        from hermes_atlas_evolution import status

        (self.home / "evolution").mkdir(exist_ok=True)
        (self.home / "evolution" / "settings.json").write_text(json.dumps({"evolution": {"auto_actions": "full"}}),
                                                               encoding="utf-8")
        text = status.markdown(status.build(self.home, now=NOW, config={}, runtime_config={}), "pt")
        self.assertIn("`auto_actions`", text)
        self.assertNotIn("{keys}", text)

    def test_a_busy_store_stops_apply_all_and_says_so(self):
        import sqlite3

        ids = [propose(self.home, "skill_lock", f"s{n}", LOCK, f"hermes curator lock s{n}") for n in range(3)]
        with patch.object(actions, "apply_proposal", side_effect=sqlite3.OperationalError("database is locked")):
            result = actions.apply_many(self.home, ids, now=NOW)
        self.assertEqual((result["applied"], result["skipped"]), ([], [{"id": ids[0], "reason": "busy"}]))


if __name__ == "__main__":
    unittest.main()
