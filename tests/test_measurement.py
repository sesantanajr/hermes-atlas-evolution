import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hermes_evolution import experiments
from hermes_evolution.evolve import cycle, notify_view, options
from hermes_evolution.experience import experience, utc_day
from hermes_evolution.logwatch import ingest_reviews, review_summary
from hermes_evolution.operations import database, observe
from hermes_evolution.report import build, markdown
from hermes_evolution.signals import TurnTracker, record_tool
from hermes_evolution.skill_health import read_ledger, read_usage, skill_health

from hermes_evolution.operations import close_observers  # noqa: E402

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp()
DAY = 86_400


def iso(when: float) -> str:
    return datetime.fromtimestamp(when, timezone.utc).isoformat()


def sha(char: str) -> str:
    return char * 64


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addCleanup(close_observers)   # Windows cannot remove a database that is still open.
        self.home = Path(folder.name)
        (self.home / "skills").mkdir()
        (self.home / "logs").mkdir()

    def ledger(self, entries):
        lines = [json.dumps({"id": f"{index:012x}", "ts": iso(when), "actor": actor, "action": "patch", "skill": skill,
                             "evidence": {"session_id": "private-session"},
                             "before": [{"path": f"/home/u/skills/{skill}/SKILL.md", "sha256": sha(before)}],
                             "after": [{"path": f"/home/u/skills/{skill}/SKILL.md", "sha256": sha(after)}]})
                 for index, (when, actor, skill, before, after) in enumerate(entries, 1)]
        (self.home / "skills" / ".curator_ledger.jsonl").write_text("\n".join(lines) + "\nnot json\n{}\n")

    def turns(self, *, day, count, failed=0, seconds=5):
        tracker = TurnTracker(self.home, slow_ms=60_000)
        for index in range(count):
            turn = f"{day}-{index}-{seconds}"
            tracker.start({"turn_id": turn, "platform": "cli"}, clock=0.0)
            tracker.finish({"turn_id": turn, "completed": index >= failed, "failed": index < failed}, clock=float(seconds),
                           now=day)


class SkillHealthTest(Base):
    def test_churn_flipflops_and_unexercised_rewrites_come_from_metadata_only(self):
        states = "abababab"
        self.ledger([(NOW - (8 - index) * 3600, "curator", "deploy", states[index], states[index + 1])
                     for index in range(7)]
                    + [(NOW - 3600, "agent", "notes", "c", "d"), (NOW - 30 * DAY, "curator", "old", "e", "f")])
        (self.home / "skills" / ".usage.json").write_text(json.dumps({
            "deploy": {"use_count": 4, "patch_count": 7, "patch_generation": 7, "last_reused_patch_generation": 7,
                       "pinned": False, "state": "active", "created_by": None, "last_used_at": iso(NOW - DAY)},
            "notes": {"use_count": 1, "patch_generation": 9, "last_reused_patch_generation": 2, "state": "active"},
            "dead": {"use_count": 0, "created_by": "agent", "state": "active", "created_at": iso(NOW - 40 * DAY)},
            "locked": {"use_count": 0, "created_by": "agent", "state": "active", "created_at": iso(NOW - 40 * DAY),
                       "locked": True},
            "../bad": {"use_count": 9}, "weird": "not a record",
        }))
        report = skill_health(self.home, now=NOW, days=7)
        self.assertEqual((report["rewrites"], report["skills_rewritten"], report["skills_tracked"]), (8, 2, 4))
        self.assertEqual(report["rewrites_by_actor"], {"curator": 7, "agent": 1})
        top = report["top"][0]
        self.assertEqual((top["skill"], top["rewrites"], top["rewrites_by_review"], top["idle_days"]), ("deploy", 7, 7, 1))
        self.assertGreaterEqual(top["flipflops"], 2)
        findings = {(item["code"], item["skill"]) for item in report["findings"]}
        self.assertEqual(findings, {("skill_churn", "deploy"), ("skill_unexercised_rewrites", "notes"),
                                    ("skill_never_used", "dead")})
        serialized = json.dumps(report)
        self.assertNotIn("private-session", serialized)
        self.assertNotIn("/home/u", serialized)

    def test_missing_oversized_or_malformed_files_degrade_to_empty(self):
        self.assertEqual(read_usage(self.home), {})
        self.assertEqual(read_ledger(self.home), [])
        (self.home / "skills" / ".usage.json").write_text("[1, 2")
        self.assertEqual(read_usage(self.home), {})
        (self.home / "skills" / ".curator_ledger.jsonl").write_text(json.dumps({"ts": "bad", "skill": "x"}) + "\n")
        self.assertEqual(skill_health(self.home, now=NOW)["rewrites"], 0)


class LogwatchTest(Base):
    LINE = ("2026-11-15 10:00:0{n},123 INFO [session-x] agent.background_review: Background review complete: "
            "thread=bg-review calls={calls} in={i} out={o} cache_read={c} result={result}\n")

    def test_only_the_background_review_logger_counts(self):
        log = self.home / "logs" / "agent.log"
        log.write_text("2026-11-15 10:00:01,000 INFO [s] tools.web_search: query=Background review complete: thread=x "
                       "calls=9 in=9 out=9 cache_read=9 result=skill\n"
                       + self.LINE.format(n=2, calls=1, i=1, o=1, c=1, result="none"))
        with experience(self.home) as conn:
            self.assertEqual(ingest_reviews(self.home, conn)["reviews"], 1)

    def test_reviews_are_counted_once_across_runs_and_rotation(self):
        log = self.home / "logs" / "agent.log"
        log.write_text("noise line with a secret sk-should-not-matter\n"
                       + self.LINE.format(n=1, calls=4, i=1000, o=100, c=5000, result="skill")
                       + self.LINE.format(n=2, calls=0, i=0, o=0, c=0, result="none")
                       + "2026-11-15 10:00:03,000 INFO agent.background_review: Background review complete: "
                         "thread=bg-review calls=1 in=5")
        with experience(self.home) as conn:
            self.assertEqual(ingest_reviews(self.home, conn)["reviews"], 2)   # the unterminated line waits
            self.assertEqual(ingest_reviews(self.home, conn)["reviews"], 0)
        with log.open("a") as stream:
            stream.write(" out=1 cache_read=0 result=memory\n" + self.LINE.format(n=4, calls=2, i=10, o=1, c=0, result="error"))
        with experience(self.home) as conn:
            self.assertEqual(ingest_reviews(self.home, conn)["reviews"], 2)
            summary = review_summary(conn, start_day="2026-11-01", end_day="2026-11-30")
            self.assertEqual((summary["reviews"], summary["productive"], summary["empty_pct"]), (4, 2, 25.0))
            self.assertEqual((summary["model_calls"], summary["input_tokens"]), (7, 1015))
        log.unlink()
        log.write_text(self.LINE.format(n=5, calls=1, i=1, o=1, c=1, result="skill"))   # rotated: new inode
        with experience(self.home) as conn:
            self.assertEqual(ingest_reviews(self.home, conn)["reviews"], 1)
        log.unlink()
        with experience(self.home) as conn:
            self.assertEqual(ingest_reviews(self.home, conn)["status"], "log_unavailable")


class ExperimentsTest(Base):
    def test_config_snapshot_keeps_only_allowlisted_scalars(self):
        snapshot = experiments.config_snapshot({
            "compression": {"threshold_tokens": 80_000, "enabled": True},
            "model": {"default": "deepseek-flash", "api_key": "sk-secret-value-123456"},
            "auxiliary": {"background_review": {"model": "https://user:pw@host/v1"}},
            "plugins": {"enabled": ["b", "a"]}})
        self.assertEqual(snapshot["compression.threshold_tokens"], "80000")
        self.assertEqual(snapshot["compression.enabled"], "true")
        self.assertEqual(snapshot["model.default"], "deepseek-flash")
        self.assertRegex(snapshot["auxiliary.background_review.model"], r"^set:[0-9a-f]{10}$")   # value withheld
        other = experiments.config_snapshot({"model": {"default": "openrouter/anthropic/claude"}})
        changed = experiments.config_snapshot({"model": {"default": "openrouter/openai/gpt"}})
        self.assertNotEqual(other["model.default"], changed["model.default"])   # provider/model changes are seen
        self.assertNotIn("openrouter", json.dumps(other))
        self.assertIn("model.model", other)
        self.assertEqual(snapshot["memory.provider"], "unset")
        self.assertTrue(snapshot["plugins.enabled"].startswith("2:"))
        self.assertNotIn("sk-secret", json.dumps(snapshot))

    def test_a_regressing_config_change_is_detected_measured_and_reported(self):
        changed = NOW - 9 * DAY
        with experience(self.home) as conn:
            self.assertEqual(experiments.detect_changes(conn, self.home, {"compression": {"threshold_tokens": 200_000}},
                                                        now=changed - DAY), [])   # the first run only records a baseline
            found = experiments.detect_changes(conn, self.home, {"compression": {"threshold_tokens": 80_000}}, now=changed)
            self.assertEqual(found, [{"kind": "config", "subject": "compression.threshold_tokens", "from": "200000",
                                      "to": "80000"}])
            self.assertEqual(experiments.detect_changes(conn, self.home, {"compression": {"threshold_tokens": 80_000}},
                                                        now=changed), [])
        for offset in range(1, 8):
            self.turns(day=changed - offset * DAY, count=10, failed=0, seconds=5)
            self.turns(day=changed + offset * DAY, count=10, failed=3, seconds=90)
            for index in range(8):
                observe(self.home, "api", {"turn_id": f"b{offset}", "api_request_id": f"{index}", "provider": "p", "model": "m",
                                           "usage": {"input_tokens": 2_000}}, now=changed - offset * DAY)
                observe(self.home, "api", {"turn_id": f"a{offset}", "api_request_id": f"{index}", "provider": "p", "model": "m",
                                           "usage": {"input_tokens": 6_000}}, now=changed + offset * DAY)
        with experience(self.home) as conn, database(self.home) as ops:
            verdicts = experiments.evaluate_changes(conn, ops, now=NOW)
            self.assertEqual(len(verdicts), 1)
            outcome = verdicts[0]
            self.assertEqual((outcome["verdict"], outcome["final"]), ("regressed", True))
            self.assertEqual(set(outcome["worse"]), {"turn_failed_pct", "turn_slow_pct", "turn_mean_ms",
                                                     "uncached_input_tokens_per_call"})
            self.assertEqual(outcome["metrics"]["uncached_input_tokens_per_call"], {"before": 2000.0, "after": 6000.0})
            self.assertEqual(outcome["concurrent_changes"], 0)
            self.assertEqual(experiments.evaluate_changes(conn, ops, now=NOW), [])   # final verdicts are not recomputed
            listed = experiments.listing(conn)[0]
            self.assertEqual((listed["verdict"], listed["final"], listed["detail"]),
                             ("regressed", True, {"from": "200000", "to": "80000"}))

    def test_compare_needs_volume_and_separates_improved_neutral_and_mixed(self):
        base = {"turns": 100, "tool_calls": 100, "api_calls": 100, "turn_failed_pct": 10.0, "turn_slow_pct": 20.0,
                "turn_mean_ms": 20_000.0, "tool_error_pct": 10.0, "owner_corrections_per_100_turns": 4.0,
                "uncached_input_tokens_per_call": 3000.0}
        self.assertEqual(experiments.compare(base, {**base, "turn_failed_pct": 4.0})["verdict"], "improved")
        self.assertEqual(experiments.compare(base, {**base, "turn_failed_pct": 10.5})["verdict"], "neutral")
        self.assertEqual(experiments.compare(base, {**base, "turn_failed_pct": 4.0, "tool_error_pct": 30.0})["verdict"], "mixed")
        self.assertEqual(experiments.compare({**base, "turns": 3, "tool_calls": 3, "api_calls": 3}, base)["verdict"],
                         "insufficient_data")

    def test_skill_rewrites_become_one_change_per_skill_and_day(self):
        self.ledger([(NOW - 5 * 3600, "curator", "deploy", "a", "b"), (NOW - 4 * 3600, "curator", "deploy", "b", "c"),
                     (NOW - 3 * 3600, "agent", "notes", "a", "b")])
        with experience(self.home) as conn:
            found = experiments.detect_changes(conn, self.home, None, now=NOW)
            self.assertEqual(sorted((item["subject"], item["rewrites"]) for item in found), [("deploy", 2), ("notes", 1)])
            self.assertEqual(experiments.detect_changes(conn, self.home, None, now=NOW), [])
            detail = experiments.listing(conn)
            deploy = next(item for item in detail if item["subject"] == "deploy")
            self.assertEqual(deploy["detail"], {"rewrites": 2, "by_review": 2, "last_entry": "000000000002"})


class CycleAndReportTest(Base):
    def test_options_validate_and_default_safely(self):
        self.assertEqual(options({})["lessons"], "suggest")
        self.assertEqual(options({"evolution": {"language": "pt"}})["language"], "pt")
        self.assertIn(options({"evolution": {"language": "klingon"}})["language"], ("en", "pt"))
        for bad in ({"lessons": "always"}, {"slow_turn_seconds": 1}, {"slow_turn_seconds": True},
                    {"owner_sender_ids": "me"}, {"owner_sender_ids": [""]}):
            with self.assertRaises(ValueError):
                options({"evolution": bad})

    def test_cycle_turns_recurring_failures_into_lessons_and_a_bilingual_digest(self):
        body = json.dumps({"error": "tool_call to 'files_search' is missing required argument(s): servico."})
        for offset in range(3):
            for _ in range(3):
                record_tool(self.home, {"tool_name": "tool_call", "status": "error", "result": body,
                                        "session_id": f"s{offset}", "args": {"calls": [{"name": "files_search"}]}},
                            now=NOW - offset * DAY)
            self.turns(day=NOW - offset * DAY, count=10, failed=1, seconds=70)
        self.ledger([(NOW - index * 600, "curator", "deploy", "a", "b") for index in range(9)])
        (self.home / "logs" / "agent.log").write_text(LogwatchTest.LINE.format(n=1, calls=3, i=900, o=90, c=0, result="none") * 12)
        first = cycle(self.home, now=NOW, runtime_config={"model": {"default": "m"}}, config={})
        self.assertEqual((first["lessons_mode"], len(first["new_lessons"]), first["reviews_ingested"]), ("suggest", 1, 12))
        kinds = {item["kind"] for item in first["open_proposals"]}
        self.assertEqual(kinds, {"lesson", "skill_lock", "review_interval", "investigate_latency"})
        self.assertEqual(notify_view(first)["new_lessons"][0]["tool"], "files_search")
        second = cycle(self.home, now=NOW + 60, runtime_config={"model": {"default": "m"}}, config={})
        self.assertEqual((second["new_lessons"], second["reviews_ingested"]), ([], 0))
        self.assertIsNone(notify_view(second))
        auto = cycle(self.home, now=NOW + 120, config={"evolution": {"lessons": "off"}})
        self.assertEqual(auto["lessons_mode"], "off")

        report = build(self.home, now=NOW, days=7, lang="pt")
        self.assertEqual(report["turns"]["interactive"]["turns"], 30)
        self.assertEqual(report["recurring_failures"][0]["tool"], "files_search")
        self.assertEqual(report["self_improvement"]["background_reviews"]["reviews"], 12)
        self.assertEqual(report["window"], {"days": 7, "from_utc": utc_day(NOW - 6 * DAY), "to_utc": utc_day(NOW)})
        portuguese, english = markdown(report, "pt"), markdown(build(self.home, now=NOW, days=7, lang="en"), "en")
        self.assertIn("# Atlas Evolution · resumo de 7 dias", portuguese)
        self.assertIn("Falhas que se repetem", portuguese)
        self.assertIn("ativas: 0 · aguardando aprovação: 1 · aposentadas: 0", portuguese)
        self.assertIn("Travar skill reescrita em excesso · `deploy` · aplique com: `hermes curator lock deploy`", portuguese)
        self.assertIn("# Atlas Evolution · 7-day digest", english)
        self.assertIn("Lock a skill that keeps being rewritten", english)
        self.assertIn("When calling `files_search`", json.dumps(build(self.home, now=NOW, days=7, lang="en")["lessons"]))
        self.assertEqual(markdown(report, "xx").splitlines()[0], markdown(report, "en").splitlines()[0])
        with self.assertRaises(ValueError):
            build(self.home, now=NOW, days=0)

    def test_digest_schedule_is_a_recipe_that_changes_nothing(self):
        from hermes_evolution.evolve import digest_schedule_spec

        before = sorted(path.name for path in self.home.rglob("*"))
        spec = digest_schedule_spec(self.home, weekday=1, hour=8, lang="pt")
        self.assertEqual(sorted(path.name for path in self.home.rglob("*")), before)
        self.assertEqual(spec["create_args"][3:6], ["cron", "create", "0 8 * * 1"])
        self.assertIn("--no-agent", spec["create_args"])
        self.assertIn("'report', '--days', '7', '--lang', 'pt'", spec["script_content"])
        self.assertIn("'learn', '--notify-only'", spec["script_content"])
        compile(spec["script_content"], "digest", "exec")
        for bad in ({"weekday": 9}, {"hour": 24}, {"lang": "xx"}, {"days": 0}, {"deliver": "a b; rm -rf"}):
            with self.assertRaises(ValueError):
                digest_schedule_spec(self.home, **bad)

    def test_cli_runs_the_loop_and_a_direct_lesson_decision_settles_its_proposal(self):
        import contextlib
        import io
        from types import SimpleNamespace

        from hermes_evolution.cli import execute

        body = json.dumps({"error": "tool_call to 'files_search' is missing required argument(s): servico."})
        for offset in range(2):
            for _ in range(2):
                record_tool(self.home, {"tool_name": "tool_call", "status": "error", "result": body,
                                        "session_id": f"s{offset}", "args": {"calls": [{"name": "files_search"}]}},
                            now=NOW - offset * DAY)

        def run(**arguments):
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                execute(SimpleNamespace(**arguments), self.home, runtime_config={})
            return stream.getvalue()

        learned = json.loads(run(evolution_action="learn", notify_only=False, lang="pt"))
        self.assertEqual(learned["new_lessons"][0]["lesson"],
                         "Ao chamar `files_search`, informe sempre o argumento obrigatório `servico`.")   # accents kept
        queue = json.loads(run(evolution_action="proposals", proposals_action="list", all=False))
        self.assertEqual([item["kind"] for item in queue], ["lesson"])
        approved = json.loads(run(evolution_action="lessons", lessons_action="approve", id=learned["new_lessons"][0]["id"],
                                  lang=None, yes=True))
        self.assertEqual(approved["status"], "active")
        self.assertEqual(json.loads(run(evolution_action="proposals", proposals_action="list", all=False)), [])
        everything = json.loads(run(evolution_action="proposals", proposals_action="list", all=True))
        self.assertEqual(everything[0]["status"], "applied")
        digest = run(evolution_action="report", format="md", days=7, lang="en", schedule_spec=False)
        self.assertTrue(digest.startswith("# Atlas Evolution · 7-day digest"))
        self.assertIn("active: 1", digest)
        self.assertEqual(json.loads(run(evolution_action="skills", days=7))["rewrites"], 0)
        self.assertEqual(json.loads(run(evolution_action="experiments"))[0]["kind"], "lesson")
        with self.assertRaises(SystemExit):
            run(evolution_action="lessons", lessons_action="approve", id=999, lang=None, yes=True)

    def test_an_empty_profile_still_produces_a_digest(self):
        report = build(self.home, now=NOW, days=7)
        text = markdown(report, "en")
        self.assertIn("No recurring tool failure in this window.", text)
        self.assertIn("Nothing is waiting for you.", text)
        self.assertEqual(cycle(self.home, now=NOW, config={})["open_proposals"], [])


if __name__ == "__main__":
    unittest.main()
