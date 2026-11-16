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

    def test_safe_takes_out_exact_memory_repeats_only(self):
        path = write_memory(self.home, "MEMORY.md", [self.FACT, "Other fact.", self.FACT.upper(),
                                                     "Reports go to the owner in Brazilian Portuguese, always."])
        lock = propose(self.home, "skill_lock", "hermes-admin", LOCK, "hermes curator lock hermes-admin")
        hermes = Hermes()
        done = actions.automatic(self.home, now=NOW, mode="safe", runner=hermes, usage={})
        self.assertEqual(len(done["memory"]), 1)
        self.assertEqual(hermes.calls, [])   # safe never changes Hermes' settings or skills
        self.assertEqual(len(memory_care.parse(path.read_text(encoding="utf-8"))), 3)   # the similar one stays
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

    def test_the_setting_defaults_to_safe_and_the_agent_cannot_widen_it(self):
        self.assertEqual(options({}, {})["auto_actions"], "safe")
        self.assertEqual(options({"evolution": {"auto_actions": False}}, {})["auto_actions"], "off")
        self.assertEqual(options({"evolution": {"auto_actions": "maybe"}}, {}, strict=False)["auto_actions"], "off")
        with self.assertRaises(ValueError):
            options({"evolution": {"auto_actions": "maybe"}}, {}, strict=True)
        self.assertNotIn("auto_actions", _trusted({"evolution": {"auto_actions": "full"}})["evolution"])
        self.assertEqual(_trusted({"evolution": {"auto_actions": "off"}})["evolution"]["auto_actions"], "off")


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
        environment = patch.dict(os.environ, {"NO_COLOR": "1"})
        environment.start()
        self.addCleanup(environment.stop)

    def test_the_home_screen_suggests_and_applies_the_changes_in_portuguese(self):
        propose(self.home, "skill_lock", "hermes-admin", LOCK, "hermes curator lock hermes-admin")
        propose(self.home, "tool_latency", "mcp__m365", {"calls": 45, "mean_s": 37.2, "total_min": 27.9},
                "hermes evolution report --days 7", "low")
        shown = self.menu("a", "s", "0", lang="pt")
        self.assertIn("Sugestões:", shown)
        self.assertIn("Aplicar 1 mudança(s) recomendada(s): travar skill reescrita em excesso", shown)
        self.assertIn("1 aplicada(s).", shown)
        self.assertEqual(self.hermes.calls[0][-3:], ["curator", "lock", "hermes-admin"])

    def test_proposals_are_grouped_explained_and_dismissed(self):
        propose(self.home, "skill_lock", "hermes-admin", LOCK, "hermes curator lock hermes-admin")
        advice = propose(self.home, "tool_latency", "mcp__m365", {"calls": 45, "mean_s": 37.2, "total_min": 27.9},
                         "hermes evolution report --days 7", "low")
        shown = self.menu("2", "2", "d", "", "0", lang="pt")
        for text in ("Mudanças que o plugin aplica (reversíveis):", "Observações:",
                     "[baixo] Ferramenta lenta chamada após chamada · mcp__m365",
                     "levou em média 37,2 s por chamada", "Dispensada: fica fechada enquanto nada mudar."):
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
        write_memory(self.home, "MEMORY.md", ["Same.", "same."])
        actions.automatic(self.home, now=NOW, mode="safe")
        written = []

        def writer(values):
            written.append(values)
            return list(values)

        script = Script("5", "1", "y", "m", "3", "", "0")
        Menu(self.home, {}, lang="en", console=Console(read=script.read, write=script.write, width=110, color=False),
             clock=lambda: NOW, writer=writer).run()
        self.assertIn("took a repeated entry out of MEMORY.md", script.text)
        self.assertIn("Undone.", script.text)
        self.assertEqual(written, [{"auto_actions": "full"}])
        self.assertEqual(memory_care.parse((self.home / "memories" / "MEMORY.md").read_text(encoding="utf-8")),
                         ["Same.", "same."])


class PanelTest(Base):
    def test_the_snapshot_explains_each_proposal_and_lists_automatic_actions(self):
        api = load_api()
        propose(self.home, "skill_lock", "hermes-admin", LOCK, "hermes curator lock hermes-admin")
        write_memory(self.home, "USER.md", ["A.", "a."])
        actions.automatic(self.home, now=NOW, mode="safe")
        snap = api.snapshot(self.home, now=NOW, runtime_config={}, lang="pt", can_decide=True)
        told = snap["proposals"][0]["explain"]
        self.assertEqual((told["title"], told["category"], told["can_apply"]),
                         ("Travar skill reescrita em excesso", "change", True))
        self.assertEqual(snap["automatic"]["mode"], "safe")
        self.assertEqual(snap["automatic"]["recent"][0]["kind"], "memory")
        self.assertEqual(snap["settings"]["values"]["auto_actions"], "safe")
        self.assertIn("auto_actions", snap["settings"]["recommended"])
        with experience(self.home) as conn:
            self.assertEqual(decisions.history(conn)[0]["actor"], "automatic")


if __name__ == "__main__":
    unittest.main()
