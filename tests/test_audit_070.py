"""0.7.0, after an independent review: who may widen trust, sentences and decisions, the history replay, latency,
the reflector excerpt, generic drafts, lesson ids, unattended approvals, indexes and the review loop."""

import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from hermes_atlas_evolution import backfill, lessons, operations, proposals, radar
from hermes_atlas_evolution.experience import experience, prune, utc_day
from hermes_atlas_evolution.operations import close_observers
from hermes_atlas_evolution.signals import approval_setting, classify, command_program, record_tool

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp()
DAY = 86_400
TIMEOUT = json.dumps({"error": "Command timed out after 180 seconds"})


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.addCleanup(close_observers)
        self.home = Path(folder.name)

    def failure(self, conn, tool, klass, hint, total, sessions, *, owner=0, daily=True):
        signature = hashlib.sha256("\0".join((tool, klass, hint)).encode()).hexdigest()[:20]
        conn.execute("INSERT INTO failure VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'x')",
                     (signature, tool, klass, hint, NOW - DAY, NOW, total, sessions))
        if daily:
            conn.execute("INSERT INTO failure_daily VALUES (?, ?, ?)", (utc_day(NOW), signature, total))
        if owner:
            conn.execute("INSERT INTO failure_owner_daily VALUES (?, ?, ?)", (utc_day(NOW), signature, owner))
        return signature

    def history(self):
        db = sqlite3.connect(self.home / "state.db")
        db.executescript("""CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, user_id TEXT, parent_session_id TEXT,
            started_at REAL, ended_at REAL, end_reason TEXT);
            CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,
            tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL, finish_reason TEXT);""")
        return db

    def timed_out_session(self, db, sid, source, user, start, calls=1):
        db.execute("INSERT INTO sessions VALUES (?, ?, ?, NULL, ?, ?, 'done')", (sid, source, user, start, start + 900))
        rows = [(sid, "user", "run the build", None, None, None, start + 1, None)]
        for n in range(calls):
            rows += [(sid, "assistant", "", None, json.dumps([{"id": f"{sid}c{n}", "function": {"name": "terminal",
                                                                                                "arguments": "{}"}}]),
                      None, start + 10 * n + 2, "tool_calls"),
                     (sid, "tool", TIMEOUT, f"{sid}c{n}", None, "terminal", start + 10 * n + 3, None)]
        rows.append((sid, "assistant", "gave up", None, None, None, start + 10 * calls + 5, "stop"))
        db.executemany("""INSERT INTO messages(session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp,
            finish_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", rows)


class TrustTest(Base):
    """settings.json can be written by the agent's own file tools: it cannot widen whom the plugin trusts."""

    def test_values_that_widen_trust_count_only_from_the_hermes_configuration(self):
        from hermes_atlas_evolution import status
        from hermes_atlas_evolution.cli import effective_settings
        from hermes_atlas_evolution.evolve import options

        (self.home / "evolution").mkdir()
        (self.home / "evolution" / "settings.json").write_text(json.dumps({
            "evolution": {"owner_sender_ids": ["5511987654321"], "owner_platforms": ["telegram"], "runtime_guard": False,
                          "lessons": "suggest"},
            "agent_advice": True}))
        chosen = options(effective_settings(self.home, {}), {}, strict=False)
        self.assertEqual((chosen["owner_sender_ids"], chosen["owner_platforms"], chosen["runtime_guard"]), ((), (), True))
        self.assertEqual(chosen["lessons"], "suggest")   # narrowing still works from settings.json
        self.assertIsNot(effective_settings(self.home, {}).get("agent_advice"), True)
        form = {"plugins": {"entries": {"hermes-atlas-evolution": {"settings": {
            "owner_platforms": ["api_server"], "agent_tools": True, "runtime_guard": False}}}}}
        trusted = effective_settings(self.home, form)
        chosen = options(trusted, form, strict=False)
        self.assertEqual((chosen["owner_platforms"], chosen["runtime_guard"], trusted["agent_advice"]),
                         (("api_server",), False, True))
        report = status.build(self.home, now=NOW, config=effective_settings(self.home, {}))
        self.assertEqual(report["settings_ignored"], ["agent_advice", "owner_platforms", "owner_sender_ids", "runtime_guard"])
        text = status.markdown(report, "pt")
        self.assertIn("Estes valores do `settings.json` são ignorados", text)
        self.assertNotIn("5511987654321", text)   # status never shows a sender id
        self.assertNotIn("5511987654321", json.dumps(report))

    def test_the_guard_keeps_the_agent_from_changing_this_plugin_or_switching_it_off(self):
        from hermes_atlas_evolution.guard import MESSAGES, RuntimeGuard, check

        vetoed = ["hermes config set plugins.entries.hermes-atlas-evolution.settings.owner_platforms '[\"telegram\"]'",
                  "hermes -p work config set plugins.entries.hermes-atlas-evolution.settings.runtime_guard false",
                  "hermes config unset plugins.entries.hermes-atlas-evolution",
                  "hermes config set plugins '{}'",
                  "hermes config set plugins.entries.hermes-evolution.settings.agent_tools true",
                  "hermes plugins disable hermes-atlas-evolution",
                  "hermes --profile work plugins uninstall hermes-atlas-evolution",
                  "bash -lc 'hermes plugins rm hermes-atlas-evolution'"]
        allowed = ["hermes config set display.language pt", "hermes config get plugins.entries.hermes-atlas-evolution",
                   "hermes plugins disable another-plugin", "hermes plugins update hermes-atlas-evolution",
                   "hermes evolution status", "echo hermes config set plugins"]
        for command in vetoed:
            self.assertEqual(check("terminal", {"command": command}, protected=(), home=self.home),
                             {"kind": "plugin_settings"}, command)
        for command in allowed:
            self.assertIsNone(check("terminal", {"command": command}, protected=(), home=self.home), command)
        counted = []
        guard = RuntimeGuard(protected=(), home=self.home, lang="pt", on_block=counted.append)
        directive = guard(tool_name="terminal", args={"command": vetoed[0]})
        self.assertEqual((directive["action"], counted), ("block", ["plugin_settings"]))
        self.assertEqual(directive["message"], MESSAGES["pt"]["plugin_settings"])
        self.assertIn("Só o dono decide", directive["message"])


class SentenceTest(Base):
    def test_a_sentence_the_operator_turned_down_never_comes_back_through_another_failure(self):
        with experience(self.home) as conn:
            self.failure(conn, "read_file", "not_found", "file", 6, 3)
            [first] = lessons.derive(conn, now=NOW, mode="auto")
            lessons.set_status(conn, first, "retired", now=NOW)                      # the operator said no
            self.failure(conn, "read_file", "not_found", "", 4, 2, owner=3)            # reads the same
            self.assertEqual(lessons.derive(conn, now=NOW + 60, mode="auto"), [])
            self.assertEqual(lessons.playbook(conn), "")

    def test_failures_that_read_the_same_share_the_owners_evidence(self):
        with experience(self.home) as conn:
            self.failure(conn, "read_file", "not_found", "file", 9, 4)                 # strangers in a chat
            sibling = self.failure(conn, "read_file", "not_found", "", 5, 3)
            [waiting] = lessons.derive(conn, now=NOW, mode="auto")
            self.assertEqual(lessons.promote(conn, now=NOW, mode="auto"), [])
            conn.execute("INSERT INTO failure_owner_daily VALUES (?, ?, 2)", (utc_day(NOW), sibling))   # the owner
            self.assertEqual(lessons.promote(conn, now=NOW + 60, mode="auto"), [waiting])
            listed = {item["id"]: item for item in lessons.listing(conn)}
        self.assertEqual((listed[waiting]["status"], listed[waiting]["failures_in_owner_turns"]), ("active", 2))

    def test_a_lesson_derived_after_the_owner_hit_a_sibling_starts_active(self):
        with experience(self.home) as conn:
            self.failure(conn, "read_file", "not_found", "file", 9, 4)
            self.failure(conn, "read_file", "not_found", "", 5, 3, owner=5)
            [lesson] = lessons.derive(conn, now=NOW, mode="auto")
            self.assertEqual(conn.execute("SELECT status FROM lesson WHERE id=?", (lesson,)).fetchone()[0], "active")


class HistoryTest(Base):
    def test_a_redo_never_counts_again_what_retention_already_dropped(self):
        signature = classify("terminal", "error", None, TIMEOUT)["signature"]
        start = NOW - 105 * DAY      # in the replay window of a profile observed since 100 days ago
        db = self.history()
        self.timed_out_session(db, "old", "cli", None, start, calls=3)
        db.commit()
        db.close()
        with experience(self.home) as conn:
            # What 0.6.0 left: 3 replayed failures whose days were pruned, 2 replayed on a day older than retention
            # that no pass pruned yet, and 5 live ones. None of the replayed days is read again.
            conn.execute("INSERT INTO failure VALUES (?, 'terminal', 'timeout', '', ?, ?, 10, 4, 'x')",
                         (signature, start, NOW - DAY))
            conn.execute("INSERT INTO failure_daily VALUES (?, ?, 2)", (utc_day(NOW - 102 * DAY), signature))
            conn.execute("INSERT INTO failure_daily VALUES (?, ?, 5)", (utc_day(NOW - DAY), signature))
            for key, value in (("observed_since", repr(NOW - 100 * DAY)), ("backfill_done", "done"),
                               ("backfill_version", "2"), ("backfill_cursor", f"{start!r}|old")):
                conn.execute("INSERT OR REPLACE INTO cursor VALUES (?, ?)", (key, value))
        self.assertEqual(backfill.step(self.home, now=NOW, budget_s=10)["status"], "done")
        close_observers()
        with experience(self.home) as conn:
            prune(conn, now=NOW)
            total = conn.execute("SELECT total FROM failure WHERE signature=?", (signature,)).fetchone()[0]
            old_days = conn.execute("SELECT COUNT(*) FROM tool_daily WHERE day < ?", (utc_day(NOW - 90 * DAY),)).fetchone()[0]
        self.assertEqual((total, old_days), (10, 0))

    def test_naming_the_owner_replays_the_history_as_theirs(self):
        from hermes_atlas_evolution.evolve import cycle

        db = self.history()
        for index in range(3):
            self.timed_out_session(db, f"tg{index}", "telegram", "555", NOW - (3 - index) * DAY)
        db.commit()
        db.close()
        first = cycle(self.home, now=NOW, config={"evolution": {"reflector": False}})
        self.assertEqual([(item["tool"], item["status"]) for item in first["new_lessons"]], [("terminal", "candidate")])
        named = {"evolution": {"reflector": False, "owner_sender_ids": ["555"]}}
        second = cycle(self.home, now=NOW + 3600, config=named)
        close_observers()
        with experience(self.home) as conn:
            owner = conn.execute("SELECT COALESCE(SUM(count), 0) FROM failure_owner_daily").fetchone()[0]
            state = conn.execute("SELECT status FROM lesson").fetchall()
            total = conn.execute("SELECT total FROM failure").fetchone()[0]
        self.assertEqual((second["history"]["status"], owner, total), ("done", 3, 3))   # replayed again, never doubled
        self.assertEqual((second["lessons_promoted"], [row[0] for row in state]), (1, ["active"]))
        third = cycle(self.home, now=NOW + 7200, config=named)   # same owner: nothing replayed again
        self.assertEqual((third["history"]["status"], third["lessons_promoted"]), ("done", 0))


class LatencyTest(Base):
    def test_a_slow_tool_is_found_whatever_its_share_of_the_total_time(self):
        with experience(self.home) as conn:
            for tool, calls, mean_ms in (("terminal", 2000, 1000), ("read_file", 5000, 200), ("web_search", 500, 3000),
                                         ("browser_navigate", 300, 5000), ("patch", 1000, 500),
                                         ("mcp_slow_tool", 6, 60_000), ("clarify", 50, 600_000)):
                conn.execute("INSERT INTO tool_daily VALUES (?, ?, ?, 0, 0, ?)", (utc_day(NOW), tool, calls, calls * mean_ms))
            conn.commit()
            reading = radar.scan(self.home, conn, now=NOW)
        codes = [item["code"] for item in reading["findings"] if item["code"].startswith("tool_slow:")]
        self.assertEqual(codes, ["tool_slow:mcp_slow_tool"])
        self.assertNotIn("mcp_slow_tool", [item["tool"] for item in reading["slow_tools"]])   # the digest's top by total


class ExcerptTest(Base):
    def test_the_program_prefix_names_only_programs_never_a_script_or_a_path(self):
        command = "cd /srv && ./clientes/acme_financeiro/sync_acme_folha.sh --mes 11"
        result = json.dumps({"output": "/srv/clientes/acme_financeiro/sync_acme_folha.sh: upload failed", "exit_code": 1})
        record_tool(self.home, {"tool_name": "terminal", "status": "error", "result": result, "args": {"command": command},
                                "session_id": "s1"}, now=NOW, sample=True, owner=True)
        long_error = json.dumps({"output": "fatal: " + "x" * 600, "exit_code": 128})
        record_tool(self.home, {"tool_name": "terminal", "status": "error", "result": long_error,
                                "args": {"command": "git push origin main"}, "session_id": "s2"}, now=NOW, sample=True,
                    owner=True)
        close_observers()
        with experience(self.home) as conn:
            excerpts = [row[0] for row in conn.execute("SELECT excerpt FROM failure_sample")]
        self.assertTrue(excerpts)
        self.assertFalse(any("acme" in item or "sync" in item for item in excerpts), excerpts)
        self.assertTrue(any(item.startswith("[git] ") for item in excerpts), excerpts)
        self.assertTrue(all(len(item) <= 300 for item in excerpts))
        self.assertEqual([command_program({"command": item}) for item in (
            "/usr/bin/python3 tool.py", "~/bin/acme-sync", "deploy_acme.sh", "./build", "C:\\tools\\x.exe", "npm ci")],
            ["python3", "", "", "", "", "npm"])


class GenericTest(unittest.TestCase):
    def test_only_advice_any_failure_would_get_is_generic(self):
        generic = ["When a terminal command fails with exit code 1, read the output before retrying it.",
                   "Read the error message carefully and fix the problem before retrying.",
                   "If the call fails, check the logs and diagnose the root cause first.",
                   "Quando o comando falhar, leia a saída antes de tentar de novo.",
                   "Se der erro, verifique os logs e corrija o problema."]
        specific = ["When npm ci fails with EINTEGRITY, delete package-lock.json and clear the npm cache before retrying.",
                    "When pip install fails on a pinned version, check the status of the index mirror and pass --index-url.",
                    "When the deploy is refused for a missing tag, create the release tag first and read the output of git tag.",
                    "Quando o rsync avisar arquivo sumido, rode de novo com --ignore-missing-args antes de tentar novamente.",
                    "When a memory write is rejected by validation, do not write it again in the same turn.",
                    "When `make` fails, read the error before retrying it."]
        for text in generic:
            self.assertTrue(lessons.generic_advice(text), text)
        for text in specific:
            self.assertFalse(lessons.generic_advice(text), text)


class LessonIdTest(Base):
    def test_a_withdrawn_lesson_keeps_its_id_and_an_open_proposal_never_shows_another_text(self):
        with experience(self.home) as conn:
            first = self.failure(conn, "terminal", "other", "x", 9, 4)
            draft = lessons.add_reflected(conn, signature=first, tool="terminal", klass="other", hint="x",
                                          text="When `rsync` reports a vanished file, rerun it with --ignore-missing-args.",
                                          now=NOW)
            proposals.refresh(conn, now=NOW)
            conn.execute("UPDATE failure SET total=1, sessions=1 WHERE signature=?", (first,))   # the evidence faded
            conn.execute("DELETE FROM failure_daily WHERE signature=?", (first,))
            self.assertEqual(lessons.tidy(conn, now=NOW), 1)
            other = self.failure(conn, "terminal", "code_exception", "ValueError", 9, 4)
            newer = lessons.add_reflected(conn, signature=other, tool="terminal", klass="code_exception",
                                          hint="ValueError", now=NOW + 60,
                                          text="When `deploy.py` raises ValueError, export PROD_TOKEN before running it.")
            proposals.refresh(conn, now=NOW + 60)
            shown = {row[0]: (row[1], json.loads(row[2])["lesson"]) for row in conn.execute(
                "SELECT code, status, evidence FROM proposal")}
        self.assertNotEqual(newer, draft)
        self.assertEqual(shown[f"lesson:{draft}"][0], "expired")
        self.assertIn("deploy.py", shown[f"lesson:{newer}"][1])

    def test_a_withdrawn_lesson_comes_back_with_its_id_when_its_failure_does(self):
        with experience(self.home) as conn:
            signature = self.failure(conn, "terminal", "timeout", "", 5, 3)
            drafted = self.failure(conn, "memory", "other", "y", 9, 4)
            bland = self.failure(conn, "terminal", "other", "z", 9, 4)
            [lesson] = lessons.derive(conn, now=NOW, mode="suggest")
            draft = lessons.add_reflected(conn, signature=drafted, tool="memory", klass="other", hint="y", now=NOW,
                                          text="When a memory write is rejected by validation, do not write it again.")
            generic = lessons.add_reflected(conn, signature=bland, tool="terminal", klass="other", hint="z", now=NOW,
                                            text="When a command fails, read the output before retrying it.")
            conn.execute("UPDATE failure SET total=1, sessions=1")
            conn.execute("DELETE FROM failure_daily")
            self.assertEqual(lessons.tidy(conn, now=NOW), 3)
            conn.execute("UPDATE failure SET total=6, sessions=3")   # all three failures are back
            back = lessons.derive(conn, now=NOW + DAY, mode="suggest")
            states = dict(conn.execute("SELECT id, COALESCE(verdict, status) FROM lesson").fetchall())
        self.assertEqual(sorted(back), sorted([lesson, draft]))
        self.assertEqual(states, {lesson: "candidate", draft: "quarantined", generic: "withdrawn"})


class ApprovalSettingTest(Base):
    def refuse(self, origin, text, times):
        for index in range(times):
            record_tool(self.home, {"tool_name": "terminal", "status": "error", "result": json.dumps({"error": text}),
                                    "session_id": f"{origin}{index}"}, now=NOW, origin=origin)

    def test_hermes_names_the_setting_and_the_radar_follows_it(self):
        unattended = ("BLOCKED: this command needs approval but this session runs on an unattended platform "
                      "(msgraph_webhook) with no user present to approve it. To allow it on unattended platforms, set "
                      "approvals.unattended_mode: approve in config.yaml.")
        cron = ("BLOCKED: this command needs approval but cron jobs run without a user present to approve it. To allow "
                "it in cron jobs, set approvals.cron_mode: approve in config.yaml.")
        denied = ("BLOCKED: User denied this command. The user has NOT consented to this action. "
                  "Do NOT retry this command.")
        self.refuse("msgraph_webhook", unattended, 4)
        self.refuse("matrix", cron, 2)        # an origin the plugin does not know: Hermes' own text decides
        self.refuse("local", denied, 3)       # a person said no
        close_observers()
        with experience(self.home) as conn:
            reading = radar.approvals(conn, now=NOW)
            found = {item["code"]: item for item in radar.findings({"approvals": reading})}
        self.assertEqual((reading["refused"], reading["unattended"]), (9, 6))
        self.assertEqual(reading["by_setting"], {"approvals.unattended_mode": 4, "approvals.cron_mode": 2})
        self.assertEqual(found["approval_unattended"]["command"], "hermes config get approvals")
        self.assertEqual(approval_setting(denied, "subagent"), "delegation.subagent_auto_approve")
        self.assertEqual(approval_setting(denied, "telegram"), "")


class IndexTest(Base):
    def test_per_signature_lookups_use_an_index_also_on_an_older_database(self):
        with experience(self.home) as conn:
            conn.execute("DROP INDEX failure_owner_daily_signature")
            conn.execute("DROP INDEX failure_daily_signature")
        close_observers()
        operations._ADDITIVE_READY.clear()   # a new process opens the database
        with experience(self.home) as conn:
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            plan = " ".join(str(row[-1]) for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT SUM(count) FROM failure_owner_daily WHERE signature=?", ("x",)))
        self.assertLessEqual({"failure_owner_daily_signature", "failure_daily_signature"}, names)
        self.assertIn("failure_owner_daily_signature", plan)


class ReviewTest(Base):
    def test_the_review_holds_no_lock_while_the_owner_reads_and_skips_a_lesson_that_changed(self):
        from hermes_atlas_evolution.cli import _review_lessons

        with experience(self.home) as conn:
            for hint, total in (("a", 9), ("b", 8), ("c", 7)):
                signature = self.failure(conn, "terminal", "missing_argument", hint, total, 3)
                conn.execute("""INSERT INTO lesson(signature, tool, klass, hint, origin, status, created, updated)
                    VALUES (?, 'terminal', 'missing_argument', ?, 'derived', 'candidate', 1, 1)""", (signature, hint))
        path = self.home / "evolution" / "evolution.sqlite3"
        writes = []

        class Terminal(io.StringIO):
            answers = iter(["a\n", "a\n", "a\n"])

            def isatty(self):
                return True

            def readline(self):
                other = sqlite3.connect(path, timeout=0)
                try:
                    other.execute("BEGIN IMMEDIATE")   # a hook of the running gateway writing meanwhile
                    other.execute("INSERT OR REPLACE INTO cursor VALUES ('probe', ?)", (str(len(writes)),))
                    if len(writes) == 1:   # a pass withdraws the next lesson while the owner reads it
                        other.execute("UPDATE lesson SET status='retired', verdict='withdrawn' WHERE hint='b'")
                    other.execute("COMMIT")
                    writes.append(True)
                finally:
                    other.close()
                return next(self.answers)

        with experience(self.home) as conn, patch("sys.stdin", Terminal()), patch("sys.stderr", io.StringIO()):
            decided = _review_lessons(conn, "en", now=NOW)
        self.assertEqual(len(writes), 3)
        with experience(self.home) as conn:
            states = dict(conn.execute("SELECT hint, COALESCE(verdict, status) FROM lesson").fetchall())
        self.assertEqual((len(decided["approved"]), len(decided["skipped"])), (2, 1))
        self.assertEqual(states, {"a": "active", "b": "withdrawn", "c": "active"})


if __name__ == "__main__":
    unittest.main()
