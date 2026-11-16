import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hermes_atlas_evolution import lessons, proposals
from hermes_atlas_evolution.agent_tools import LESSONS_TOOL, LessonsTool
from hermes_atlas_evolution.experience import experience
from hermes_atlas_evolution.hints import KEY, MAX_HINTS_PER_SESSION, HintIndex, attach
from hermes_atlas_evolution.signals import classify, record_tool

from hermes_atlas_evolution.operations import close_observers  # noqa: E402

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp()
DAY = 86_400
MISSING = json.dumps({"error": "tool_call to 'files_search' is missing required argument(s): servico."})
ARGS = {"calls": [{"name": "files_search", "arguments": {}}]}


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addCleanup(close_observers)   # Windows cannot remove a database that is still open.
        self.home = Path(folder.name)

    def tool_fails(self, *, now, session="s", times=1, body=MISSING, tool="tool_call", args=ARGS):
        for _ in range(times):
            record_tool(self.home, {"tool_name": tool, "status": "error", "result": body, "session_id": session,
                                    "args": args}, now=now)

    def succeed(self, *, now, times, tool="files_search"):
        for _ in range(times):
            record_tool(self.home, {"tool_name": tool, "status": "ok", "result": "{}"}, now=now)


class LessonsTest(Base):
    def test_a_single_bad_session_is_not_yet_a_lesson_but_recurrence_is(self):
        self.tool_fails(now=NOW, session="one", times=5)
        with experience(self.home) as conn:
            self.assertEqual(lessons.derive(conn, now=NOW, mode="suggest"), [])
        self.tool_fails(now=NOW, session="two")
        with experience(self.home) as conn:
            created = lessons.derive(conn, now=NOW, mode="suggest")
            self.assertEqual(len(created), 1)
            self.assertEqual(lessons.derive(conn, now=NOW, mode="suggest"), [])   # idempotent
            item = lessons.listing(conn, lang="pt")[0]
            self.assertEqual((item["status"], item["origin"], item["tool"]), ("candidate", "derived", "files_search"))
            self.assertEqual(item["lesson"], "Ao chamar `files_search`, informe sempre o argumento obrigatório `servico`.")
            self.assertEqual(lessons.listing(conn, lang="en")[0]["lesson"],
                             "When calling `files_search`, always pass the required argument `servico`.")
            self.assertEqual(lessons.playbook(conn, lang="en"), "")   # a candidate is never shown to the agent

    def test_modes_off_suggest_and_auto(self):
        self.tool_fails(now=NOW - DAY, times=2)
        self.tool_fails(now=NOW, times=2)
        with experience(self.home) as conn:
            self.assertEqual(lessons.derive(conn, now=NOW, mode="off"), [])
            created = lessons.derive(conn, now=NOW, mode="auto")
            self.assertEqual(lessons.listing(conn)[0]["status"], "active")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM change WHERE kind='lesson'").fetchone()[0], 1)
            text = lessons.playbook(conn, lang="en", mark_shown=True)
            self.assertIn("always pass the required argument `servico`", text)
            self.assertEqual(lessons.listing(conn)[0]["shown_in_sessions"], 1)
            self.assertEqual(list(lessons.hint_index(conn)), [classify("tool_call", "error", None, MISSING, ARGS)["signature"]])
            self.assertEqual(len(created), 1)

    def test_classes_without_a_safe_template_never_become_lessons(self):
        trace = json.dumps({"output": "Traceback (most recent call last):\nValueError: x", "exit_code": 1})
        for day in range(3):
            self.tool_fails(now=NOW - day * DAY, session=f"s{day}", times=3, body=trace, tool="terminal", args=None)
        with experience(self.home) as conn:
            self.assertEqual(lessons.derive(conn, now=NOW, mode="auto"), [])

    def test_an_effective_lesson_is_kept_and_an_ineffective_one_is_retired_in_auto_mode(self):
        activated = NOW - 16 * DAY
        for day in range(1, 8):   # before: 3 failures in 10 calls every day
            self.tool_fails(now=activated - day * DAY, session=f"b{day}", times=3)
            self.succeed(now=activated - day * DAY, times=7)
        with experience(self.home) as conn:
            lesson_id = lessons.derive(conn, now=activated, mode="auto")[0]
        for day in range(1, 16):   # after: still failing just as often
            self.tool_fails(now=activated + day * DAY, session=f"a{day}", times=3)
            self.succeed(now=activated + day * DAY, times=7)
        with experience(self.home) as conn:
            suggested = lessons.evaluate(conn, now=NOW, mode="suggest")[0]
            self.assertEqual((suggested["verdict"], suggested["retired"], suggested["basis"]),
                             ("ineffective", False, "failures_per_call"))
            self.assertEqual(lessons.listing(conn)[0]["status"], "active")
            open_items = proposals.refresh(conn, now=NOW)
            self.assertEqual([item["kind"] for item in open_items], ["lesson_retire"])
            auto = lessons.evaluate(conn, now=NOW, mode="auto")[0]
            self.assertTrue(auto["retired"])
            self.assertEqual(lessons.listing(conn)[0]["status"], "retired")
            self.assertEqual(lessons.playbook(conn), "")
            self.assertEqual(lesson_id, auto["id"])

    def test_effect_is_measured_as_effective_when_the_failure_stops(self):
        activated = NOW - 8 * DAY
        for day in range(1, 6):
            self.tool_fails(now=activated - day * DAY, session=f"b{day}", times=4)
            self.succeed(now=activated - day * DAY, times=6)
        with experience(self.home) as conn:
            lessons.derive(conn, now=activated, mode="auto")
        for day in range(1, 8):
            self.succeed(now=activated + day * DAY, times=10)
        with experience(self.home) as conn:
            self.assertIsNone(lessons.evaluate(conn, now=activated + DAY, mode="auto")[0]["verdict"])   # too early
            outcome = lessons.evaluate(conn, now=NOW, mode="auto")[0]
            self.assertEqual((outcome["verdict"], outcome["failures_after"]), ("effective", 0))
            self.assertGreater(outcome["before"], 0)

    def test_custom_lessons_are_validated_and_agent_lessons_are_quarantined(self):
        with experience(self.home) as conn:
            for bad in ("short", "x" * 300, "use the key sk-abcdefghijklmnop for this tool every time", None,
                        "see https://user:pass@example.com/secret for the steps"):
                with self.assertRaises(ValueError):
                    lessons.add_custom(conn, bad, tool="terminal", origin="operator", now=NOW)
            with self.assertRaises(ValueError):
                lessons.add_custom(conn, "A perfectly reasonable lesson sentence.", tool="bad tool!", origin="operator", now=NOW)
            with self.assertRaises(ValueError):
                lessons.add_custom(conn, "A perfectly reasonable lesson sentence.", tool="t", origin="derived", now=NOW)
            operator = lessons.add_custom(conn, "  Validate   PowerShell with PSScriptAnalyzer first.  ", tool="terminal",
                                          origin="operator", now=NOW, active=True)
            agent = lessons.add_custom(conn, "Prefer the files connector for document lookups.", tool="general",
                                       origin="agent", now=NOW, active=True)
            rows = {item["id"]: item for item in lessons.listing(conn)}
            self.assertEqual(rows[operator]["status"], "active")
            self.assertEqual(rows[operator]["lesson"], "Validate PowerShell with PSScriptAnalyzer first.")
            self.assertEqual(rows[agent]["status"], "quarantined")   # an agent can never self-approve
            self.assertNotIn("files connector", lessons.playbook(conn))
            self.assertEqual(lessons.set_status(conn, agent, "active", now=NOW)["previous"], "quarantined")
            self.assertIn("files connector", lessons.playbook(conn))
            self.assertEqual([item["id"] for item in lessons.search(conn, tool="terminal")], [operator, agent])
            with self.assertRaises(ValueError):
                lessons.set_status(conn, 999, "active", now=NOW)

    def test_playbook_respects_its_character_budget(self):
        with experience(self.home) as conn:
            for index in range(30):
                lessons.add_custom(conn, f"Lesson number {index:02d} " + "detail " * 25, tool="general", origin="operator",
                                   now=NOW, active=True)
            text = lessons.playbook(conn, max_chars=600, limit=50)
            self.assertLessEqual(len(text), 600)
            self.assertGreaterEqual(text.count("\n- "), 2)

    def test_two_lessons_with_the_same_sentence_take_one_line(self):
        with experience(self.home) as conn:
            for signature in ("sig-file", "sig-other"):   # e.g. not_found with different details, one template
                conn.execute("""INSERT INTO lesson(signature, tool, klass, hint, origin, status, created, updated, activated)
                    VALUES (?, 'terminal', 'not_found', '', 'derived', 'active', ?, ?, ?)""", (signature, NOW, NOW, NOW))
            chosen: list = []
            text = lessons.playbook(conn, chosen=chosen)
        self.assertEqual(text.count("\n- "), 1)
        self.assertEqual(len(chosen), 2)   # both are in the prompt, so both count as shown


class HintsTest(Base):
    def test_attach_keeps_json_valid_and_never_duplicates(self):
        changed = attach(MISSING, "Always pass servico.")
        self.assertEqual(json.loads(changed)[KEY], "Always pass servico.")
        self.assertEqual(json.loads(changed)["error"], json.loads(MISSING)["error"])
        self.assertIsNone(attach(changed, "Always pass servico."))
        self.assertTrue(attach("plain failure", "Do Y.", "pt").endswith("[Lição do Atlas Evolution] Do Y."))
        self.assertTrue(attach("[1, 2]", "Do Y.").startswith("[1, 2]\n\n[Atlas Evolution lesson]"))
        for unusable in (None, 42, "x" * 300_000):
            self.assertIsNone(attach(unusable, "Do Y."))
        self.assertIsNone(attach("x", ""))

    def test_a_native_terminal_hint_is_not_repeated_by_a_lesson(self):
        native = json.dumps({"output": "rg: command not found", "exit_code": 127,
                             "hint": "Install ripgrep or use grep -r instead."})
        self.assertIsNone(attach(native, "When rg is missing, use grep -r."))   # Hermes already said it
        empty_hint = json.dumps({"output": "boom", "exit_code": 1, "hint": None})
        self.assertIsNotNone(attach(empty_hint, "When boom happens, check the input."))

    def test_a_hint_is_given_once_per_session_and_capped(self):
        self.tool_fails(now=NOW - DAY, times=2)
        self.tool_fails(now=NOW, times=2)
        with experience(self.home) as conn:
            lesson_id = lessons.derive(conn, now=NOW, mode="auto")[0]
        signature = classify("tool_call", "error", None, MISSING, ARGS)["signature"]
        index = HintIndex(self.home, lang="en")
        self.assertEqual(index.lookup(signature, "session-a", clock=0.0)[0], lesson_id)
        self.assertIsNone(index.lookup(signature, "session-a", clock=1.0))
        self.assertIsNotNone(index.lookup(signature, "session-b", clock=2.0))
        self.assertIsNone(index.lookup("unknown-signature", "session-a", clock=3.0))
        index.count(lesson_id)
        with experience(self.home) as conn:
            self.assertEqual(lessons.listing(conn)[0]["hints_given"], 1)
            lessons.set_status(conn, lesson_id, "retired", now=NOW)
        self.assertIsNotNone(index.lookup(signature, "session-c", clock=30.0))   # cached for up to a minute
        self.assertIsNone(index.lookup(signature, "session-d", clock=120.0))     # refreshed: the lesson is retired
        self.assertEqual(MAX_HINTS_PER_SESSION, 6)


class AgentToolTest(Base):
    def test_schema_is_closed_and_actions_are_bounded(self):
        self.assertFalse(LESSONS_TOOL["parameters"]["additionalProperties"])
        tool = LessonsTool(self.home, lang="en")
        self.assertEqual(json.loads(tool.handle({"action": "search"})), {"lessons": []})
        self.assertIn("error", json.loads(tool.handle({"action": "search", "extra": 1})))
        self.assertIn("error", json.loads(tool.handle({"action": "delete"})))
        self.assertIn("error", json.loads(tool.handle("not a dict")))
        proposed = json.loads(tool.handle({"action": "propose", "tool": "terminal",
                                           "lesson": "When a build fails twice, read the full log before retrying."}))
        self.assertEqual(proposed["status"], "quarantined")
        self.assertEqual(json.loads(tool.handle({"action": "search", "tool": "terminal"})), {"lessons": []})
        status = json.loads(tool.handle({"action": "status"}))
        self.assertEqual(status["lessons"]["quarantined"], 1)
        for _ in range(4):
            tool.handle({"action": "propose", "lesson": "Another reasonable lesson sentence to store."})
        limited = json.loads(tool.handle({"action": "propose", "lesson": "One more reasonable lesson sentence."}))
        self.assertIn("Too many proposals", limited["error"])


class ProposalsTest(Base):
    def test_lesson_proposal_lifecycle(self):
        self.tool_fails(now=NOW - DAY, times=2)
        self.tool_fails(now=NOW, times=2)
        with experience(self.home) as conn:
            lessons.derive(conn, now=NOW, mode="suggest")
            open_items = proposals.refresh(conn, now=NOW)
            self.assertEqual([(item["kind"], item["risk"]) for item in open_items], [("lesson", "low")])
            self.assertEqual(len(proposals.refresh(conn, now=NOW)), 1)   # refresh does not duplicate
            decided = proposals.decide(conn, open_items[0]["id"], "approve", now=NOW, note="  looks   right ")
            self.assertEqual((decided["status"], decided["applied"]["status"]), ("applied", "active"))
            self.assertEqual(lessons.listing(conn)[0]["status"], "active")
            self.assertEqual(proposals.refresh(conn, now=NOW), [])
            with self.assertRaises(ValueError):
                proposals.decide(conn, open_items[0]["id"], "approve", now=NOW)
            with self.assertRaises(ValueError):
                proposals.decide(conn, 12345, "approve", now=NOW)
            with self.assertRaises(ValueError):
                proposals.decide(conn, open_items[0]["id"], "maybe", now=NOW)
            self.assertEqual(proposals.listing(conn)[0]["note"], "looks right")

    def test_rejecting_a_lesson_retires_it_and_native_proposals_only_return_a_command(self):
        self.tool_fails(now=NOW - DAY, times=2)
        self.tool_fails(now=NOW, times=2)
        skills = {"findings": [{"code": "skill_churn", "skill": "deploy", "rewrites": 12, "flipflops": 2, "window_days": 7,
                                "last_entry": "abc123456789"},
                               {"code": "skill_unexercised_rewrites", "skill": "notes", "unreused_rewrites": 6},
                               {"code": "skill_never_used", "skill": "old", "age_days": 40}]}
        reviews = {"reviews": 20, "empty_pct": 60.0, "model_calls": 80, "input_tokens": 9, "output_tokens": 1}
        turns = {"turns": 50, "slow_pct": 40.0, "p50_ms": 30_000, "p95_ms": 300_000}
        failures = [{"signature": "f" * 20, "tool": "terminal", "class": "code_exception", "hint": "ValueError", "count": 9,
                     "days": 3}]
        with experience(self.home) as conn:
            lessons.derive(conn, now=NOW, mode="suggest")
            open_items = proposals.refresh(conn, now=NOW, skills=skills, reviews=reviews, turns=turns, failures=failures)
            kinds = {item["kind"]: item for item in open_items}
            self.assertEqual(set(kinds), {"lesson", "skill_lock", "skill_review", "skill_archive", "review_interval",
                                          "investigate_failure", "investigate_latency"})
            self.assertEqual(open_items[0]["risk"], "medium")   # highest risk first
            self.assertEqual(kinds["skill_lock"]["command"], "hermes curator lock deploy")
            rejected = proposals.decide(conn, kinds["lesson"]["id"], "reject", now=NOW)
            self.assertEqual(rejected["status"], "rejected")
            self.assertEqual(lessons.listing(conn)[0]["status"], "retired")
            approved = proposals.decide(conn, kinds["skill_lock"]["id"], "approve", now=NOW)
            self.assertEqual((approved["status"], approved["run_this"]), ("approved", "hermes curator lock deploy"))
            remaining = proposals.refresh(conn, now=NOW + DAY)   # the evidence is gone
            self.assertEqual(remaining, [])
            states = {item["kind"]: item["status"] for item in proposals.listing(conn)}
            self.assertEqual(states["skill_review"], "expired")
            self.assertEqual(states["skill_lock"], "approved")   # an approved proposal waits for the operator
            reopened = proposals.refresh(conn, now=NOW + 2 * DAY, skills=skills)
            self.assertIn("skill_review", {item["kind"] for item in reopened})


if __name__ == "__main__":
    unittest.main()
