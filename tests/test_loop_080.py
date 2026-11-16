"""Regression tests for the learning-loop review of 0.8.0: lesson measurement, retention, the reflector and the notice."""

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from hermes_atlas_evolution import backfill, experiments, lessons, proposals, radar, reflector, signals
from hermes_atlas_evolution.evolve import CONFIG_UNREAD, cycle
from hermes_atlas_evolution.experience import RETENTION_CONFIRM_DAYS, experience, prune, utc_day
from hermes_atlas_evolution.operations import close_observers
from hermes_atlas_evolution.signals import record_tool
from hermes_atlas_evolution.status import pending_notice

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp()
DAY = 86_400
MISSING = json.dumps({"error": "missing required argument: query"})
QUIET = {"evolution": {"reflector": False}}


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.addCleanup(close_observers)
        self.home = Path(folder.name)

    def call(self, when, *, session="s", result=None, tool="example_search", sample=False):
        record_tool(self.home, {"tool_name": tool, "status": "error" if result else "ok", "result": result or "{}",
                                "session_id": session}, now=when, sample=sample)

    def failure(self, conn, signature="sig", *, tool="example_search", klass="missing_argument", hint="query",
                total=3, sessions=2, days=()):
        conn.execute("INSERT INTO failure VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'x')",
                     (signature, tool, klass, hint, NOW - 60 * DAY, NOW, total, sessions))
        conn.executemany("INSERT INTO failure_daily VALUES (?, ?, ?)", [(day, signature, count) for day, count in days])

    def active(self, conn, activated, signature="sig"):
        return conn.execute("""INSERT INTO lesson(signature, tool, klass, hint, origin, status, created, updated, activated)
            VALUES (?, 'example_search', 'missing_argument', 'query', 'derived', 'active', ?, ?, ?)""",
                            (signature, activated, activated, activated)).lastrowid


def daily(start, count, per_day=1):
    """(day, count) for `count` consecutive days starting `start` days after (negative: before) NOW."""
    return [(utc_day(NOW + (start + index) * DAY), per_day) for index in range(count)]


class BaselineTest(Base):
    def test_a_lesson_activated_after_a_one_day_burst_counts_the_burst_in_its_baseline(self):
        activated = NOW - 20 * DAY
        for day in range(14, 0, -1):   # two weeks of normal use before: no failures
            for index in range(10):
                self.call(activated - day * DAY + index * 60, session=f"before-{day}")
        for index in range(7):
            self.call(activated - 3000 + index * 60, session="s1")
        for index, session in enumerate(("s1", "s2", "s2")):   # the burst, on the activation day
            self.call(activated - 1200 + index * 60, session=session, result=MISSING)
        with experience(self.home) as conn:
            [lesson_id] = lessons.derive(conn, now=activated, mode="auto")
        for day in range(1, 15):   # after: the lesson works, the failure is gone
            for index in range(10):
                self.call(activated + day * DAY + index * 60, session=f"after-{day}")
        with experience(self.home) as conn:
            [effect] = lessons.evaluate(conn, now=activated + 14 * DAY + 3600, mode="auto")
            row = conn.execute("SELECT status, baseline_failures, baseline_calls FROM lesson WHERE id=?",
                               (lesson_id,)).fetchone()
        self.assertEqual((effect["verdict"], effect["failures_before"]), ("effective", 3))
        self.assertEqual(tuple(row), ("active", 3, 150))

    def test_a_baseline_without_failures_is_inconclusive_and_never_retires_the_lesson(self):
        activated = NOW - 20 * DAY
        with experience(self.home) as conn:
            self.failure(conn, total=18, days=daily(-19, 18))   # none before activation, one a day since
            lesson_id = self.active(conn, activated)
            [effect] = lessons.evaluate(conn, now=NOW, mode="auto")
            status = conn.execute("SELECT status FROM lesson WHERE id=?", (lesson_id,)).fetchone()[0]
        self.assertEqual((effect["verdict"], effect["retired"], status), ("inconclusive", False, "active"))

    def test_an_unreadable_history_pass_does_not_freeze_a_baseline_from_part_of_it(self):
        observed = NOW - 2 * DAY
        with experience(self.home) as conn:
            conn.execute("INSERT INTO cursor VALUES ('observed_since', ?)", (repr(observed),))
            lesson_id = self.active(conn, observed + 3600)   # its 14 days before reach into the history
        with patch.object(backfill, "step", return_value={"status": "unreadable"}):
            cycle(self.home, now=NOW, config=QUIET)
        with experience(self.home) as conn:
            frozen = conn.execute("SELECT baseline_failures FROM lesson WHERE id=?", (lesson_id,)).fetchone()[0]
        self.assertIsNone(frozen)


class OwnerDecisionTest(Base):
    def setUp(self):
        super().setUp()
        self.activated = NOW - 16 * DAY
        with experience(self.home) as conn:   # one failure a day before and after: the lesson does not help
            self.failure(conn, total=30, days=daily(-30, 14) + daily(-15, 15))
            self.lesson_id = self.active(conn, self.activated)

    def test_rejecting_the_retire_proposal_keeps_the_lesson_in_auto_mode(self):
        with experience(self.home) as conn:
            [measured] = lessons.evaluate(conn, now=NOW, mode="suggest")
            [proposal] = [item for item in proposals.refresh(conn, now=NOW) if item["kind"] == "lesson_retire"]
            proposals.decide(conn, proposal["id"], "reject", now=NOW, note="keep it")
            [kept] = lessons.evaluate(conn, now=NOW + 60, mode="auto")
            status = conn.execute("SELECT status FROM lesson WHERE id=?", (self.lesson_id,)).fetchone()[0]
        self.assertEqual(measured["verdict"], "ineffective")
        self.assertEqual((kept["verdict"], kept["retired"], status), ("ineffective", False, "active"))

    def test_a_verdict_never_undoes_a_retire_the_owner_made_during_the_pass(self):
        counts = lessons._counts

        def owner_retires_meanwhile(conn, *args):
            conn.execute("UPDATE lesson SET status='retired', updated=? WHERE id=?", (NOW, self.lesson_id))
            return counts(conn, *args)

        with experience(self.home) as conn:
            with patch.object(lessons, "_counts", owner_retires_meanwhile):
                effects = lessons.evaluate(conn, now=NOW, mode="suggest")
            status = conn.execute("SELECT status FROM lesson WHERE id=?", (self.lesson_id,)).fetchone()[0]
        self.assertEqual((effects, status), ([], "retired"))


class DeriveTest(Base):
    def test_failures_without_a_template_never_starve_one_with_a_template(self):
        with experience(self.home) as conn:
            for index in range(lessons.DERIVE_BATCH + 1):
                self.failure(conn, f"untemplated-{index}", tool=f"mcp_tool_{index}", klass="code_exception", hint="",
                             total=10)
            self.failure(conn, "templated", total=3)
            created = lessons.derive(conn, now=NOW, mode="suggest")
            tools = [row[0] for row in conn.execute("SELECT tool FROM lesson")]
        self.assertEqual((len(created), tools), (1, ["example_search"]))


class ConfigTest(Base):
    def test_a_configuration_that_could_not_be_read_records_no_change(self):
        real = {"model": {"default": "deepseek-chat", "provider": "deepseek"}, "curator": {"enabled": True}}
        with experience(self.home) as conn:
            found = [experiments.detect_changes(conn, self.home, config, now=NOW + index * DAY)
                     for index, config in enumerate((real, {CONFIG_UNREAD: True}, real))]
            rows = conn.execute("SELECT COUNT(*) FROM change WHERE kind='config'").fetchone()[0]
        self.assertEqual((found, rows), ([[], [], []], 0))


class InvestigateTest(Base):
    def test_an_investigation_proposal_does_not_depend_on_the_weeks_ranking(self):
        with experience(self.home) as conn:
            self.failure(conn, "auth", tool="mcp_crm", klass="auth", hint="", total=6, days=daily(-1, 2, per_day=3))
            for index in range(12):   # a busier week elsewhere
                self.failure(conn, f"busy-{index}", tool=f"tool{index}", hint=f"a{index}", total=8,
                             days=[(utc_day(NOW), 8)])
        result = cycle(self.home, now=NOW, config=QUIET)
        found = [item["subject"] for item in result["open_proposals"] if item["kind"] == "investigate_failure"]
        self.assertEqual(found, ["mcp_crm"])


class ReflectorBackoffTest(Base):
    def test_a_failure_whose_draft_keeps_raising_waits_and_others_are_drafted(self):
        for number, count in ((0, 9), (1, 8), (2, 7), (3, 5)):
            error = json.dumps({"error": f"Traceback (most recent call last):\nRuntimeError: boom {number}"})
            for index in range(count):
                self.call(NOW - 3600 + index, session=f"s{index % 2}", result=error, tool=f"tool{number}", sample=True)
        asked = []

        def ask(instructions, payload):
            tool = json.loads(payload)["tool"]
            asked.append(tool)
            if tool != "tool3":
                raise RuntimeError("provider rejected this input")
            return {"applies": True, "lesson": "When tool3 raises RuntimeError, pass --safe-mode to `tool3`."}

        with experience(self.home) as conn:
            for minute in range(reflector.BACKOFF_AFTER_FAILED_CALLS + 1):
                result = reflector.reflect(conn, ask, now=NOW + minute * 60, daily_limit=50)
            later = reflector.reflect(conn, ask, now=NOW + reflector.BACKOFF_DAYS * DAY + 3600, daily_limit=50)
        self.assertEqual([item["tool"] for item in result["drafted"]], ["tool3"])
        self.assertEqual({tool: asked.count(tool) for tool in ("tool0", "tool3")},
                         {"tool0": reflector.BACKOFF_AFTER_FAILED_CALLS + 1, "tool3": 1})
        self.assertEqual(later["failed"], 3)   # the wait ends: they are asked again


class RetentionClockTest(Base):
    def setUp(self):
        super().setUp()
        for day in range(30, 0, -1):
            for index in range(5):
                self.call(NOW - day * DAY + index, session=f"s{day}", result=MISSING if index == 0 else None)
        with experience(self.home) as conn:
            prune(conn, now=NOW)

    def counts(self):
        close_observers()
        with experience(self.home) as conn:
            return [conn.execute(f"SELECT COUNT(*) FROM {table} WHERE day < ?", (utc_day(NOW),)).fetchone()[0]
                    for table in ("tool_daily", "failure_daily")]

    def test_a_clock_far_ahead_does_not_delete_the_history(self):
        self.call(NOW + 365 * DAY, session="skewed")   # a hook write while the clock is a year ahead
        with experience(self.home) as conn:
            prune(conn, now=NOW + 365 * DAY + 60)       # and a pass under that clock
        self.assertEqual(self.counts(), [30, 30])

    def test_a_clock_that_stays_ahead_is_believed_and_retention_catches_up(self):
        for day in range(RETENTION_CONFIRM_DAYS):
            self.call(NOW + (365 + day) * DAY, session=f"later-{day}")
        with experience(self.home) as conn:
            prune(conn, now=NOW + (365 + RETENTION_CONFIRM_DAYS) * DAY)
        self.assertEqual(self.counts(), [0, 0])


class NoticeTest(Base):
    def test_the_daily_notice_is_shown_once_and_never_while_it_cannot_be_marked(self):
        with experience(self.home) as conn:
            lessons.add_custom(conn, "When deploying, run the smoke tests first.", tool="general", origin="operator",
                               now=NOW)
        busy = sqlite3.connect(self.home / "evolution" / "evolution.sqlite3", isolation_level=None)
        self.addCleanup(busy.close)
        busy.execute("BEGIN IMMEDIATE")   # a learning pass or a replay slice is writing
        while_busy = [pending_notice(self.home, now=NOW + index, lang="en") for index in range(3)]
        busy.execute("ROLLBACK")
        shown = [pending_notice(self.home, now=NOW + 10 + index, lang="en") for index in range(2)]
        self.assertEqual(while_busy, [None, None, None])
        self.assertIn("1 lesson", shown[0]["message"])
        self.assertIsNone(shown[1])


class RadarTest(Base):
    def test_an_unreadable_history_connection_is_closed(self):
        (self.home / "state.db").write_bytes(b"not a database" * 100)
        opened = []

        class Source:
            def __init__(self):
                self.closed = False
                opened.append(self)

            def execute(self, *args):
                raise sqlite3.DatabaseError("file is not a database")

            def close(self):
                self.closed = True

        with patch.object(radar.sqlite3, "connect", lambda *args, **kwargs: Source()):
            self.assertIsNone(radar._history(self.home))
        self.assertEqual([source.closed for source in opened], [True])


SECRETS = ("S3cr3tPassw0rdX", "8f3KxQ2mZ7pLwR9vT1nB4yC6dH0sJ5gU", "a0AfH6SMBx3kL9mQ2rT7vW1yZ4cF8hJ0nP", "Hunter2Hunter2",
           "Ab3dEf6hIj9kLm2nOp5q", "51HxYzAbCdEfGh12345678", "AbCdEfGh1234567890abcdefGHIJ123456",
           "IOSFODNN7EXAMPLE", "SyD1x2y3z4w5v6u7t8s9r0q", "a1b2c3d4e5f6g7h8i9j0k1l2")


class CredentialTest(Base):
    ERRORS = ("mysql: [ERROR] Access denied for user 'app'; ran: mysql -h db -u app -pS3cr3tPassw0rdX shop",
              "RuntimeError: provider rejected API key 8f3KxQ2mZ7pLwR9vT1nB4yC6dH0sJ5gU (project acme)",
              "google.auth.exceptions.RefreshError: token ya29." "a0AfH6SMBx3kL9mQ2rT7vW1yZ4cF8hJ0nP expired",
              "psycopg.OperationalError: connection failed with password=Hunter2Hunter2! for user app",
              "docker login failed: glpat-" "Ab3dEf6hIj9kLm2nOp5q rejected; rk_live_" "51HxYzAbCdEfGh12345678 revoked",
              "npm ERR! npm_" "AbCdEfGh1234567890abcdefGHIJ123456 expired; AKIA" "IOSFODNN7EXAMPLE denied",
              "requests.exceptions.HTTPError: 403 for AIza" "SyD1x2y3z4w5v6u7t8s9r0q (sk-" "a1b2c3d4e5f6g7h8i9j0k1l2)")

    def test_credentials_never_reach_the_excerpt_and_the_cause_stays(self):
        for error in self.ERRORS:
            excerpt = reflector.scrub(error)
            for secret in SECRETS:
                self.assertNotIn(secret, excerpt, error)
        self.assertIn("RefreshError", reflector.scrub(self.ERRORS[2]))
        self.assertIn("Access denied", reflector.scrub(self.ERRORS[0]))
        self.assertIn("urllib3.exceptions.MaxRetryError", reflector.scrub("urllib3.exceptions.MaxRetryError: retries"))

    def test_the_stored_excerpt_and_the_model_input_hold_no_credential(self):
        for index, error in enumerate(self.ERRORS):
            for session in ("a", "b", "c"):
                record_tool(self.home, {"tool_name": f"tool{index}", "status": "error", "session_id": session,
                                        "result": json.dumps({"output": "", "exit_code": 1, "error": error})},
                            now=NOW - DAY, sample=True)
        close_observers()
        sent = []
        with experience(self.home) as conn:
            dump = "\n".join(conn.iterdump())
            reflector.reflect(conn, lambda instructions, text: sent.append(text) or {"applies": False, "lesson": ""},
                              now=NOW, daily_limit=50)
        self.assertTrue(sent)
        for secret in SECRETS:
            self.assertNotIn(secret, dump)
            self.assertNotIn(secret, "\n".join(sent))

    def test_a_lesson_with_a_credential_is_refused_and_ordinary_ones_are_not(self):
        for text in ("When the registry push fails, log in with glpat-" "Ab3dEf6hIj9kLm2nOp5q first.",
                     "Use rk_live_" "51HxYzAbCdEfGh12345678 for the payment test.",
                     "Publish with npm_" "AbCdEfGh1234567890abcdefGHIJ123456 as the token.",
                     "Refresh with ya29." "a0AfH6SMBx3kL9mQ2rT7vW1yZ4cF8hJ0nP before calling it.",
                     "When the API refuses, send 8f3KxQ2mZ7pLwR9vT1nB4yC6dH0sJ5gU as the key."):
            self.assertIsNone(lessons.clean_text(text), text)
        for text in ("Pass `--max-old-space-size=4096` to node before building.",
                     "When calling `mcp_atlas_m365_list_messages`, pass `top`.",
                     "Set `npm_config_cache` to a writable folder before npm install.",
                     "Use `python3.11 -m venv .venv` for the build step."):
            self.assertEqual(lessons.clean_text(text), text)
        for klass, (_needs_hint, english, portuguese) in lessons.TEMPLATES.items():
            for template in (english, portuguese):
                sentence = template.format(tool="mcp_atlas_m365_list_messages", hint="servico")
                self.assertEqual(lessons.clean_text(sentence), sentence, klass)

    def test_a_lesson_hermes_redactor_would_change_is_refused(self):
        text = "When the deploy fails, ask the owner for the vault entry."

        def redactor(value, force=False):
            return value.replace("vault entry", "***")

        def broken(value, force=False):
            raise RuntimeError("redactor failed")

        with patch.object(signals, "_native_redactor", lambda: redactor):
            self.assertIsNone(lessons.clean_text(text))
        with patch.object(signals, "_native_redactor", lambda: broken):
            self.assertIsNone(lessons.clean_text(text))
        with patch.object(signals, "_native_redactor", lambda: None):
            self.assertEqual(lessons.clean_text(text), text)


if __name__ == "__main__":
    unittest.main()
