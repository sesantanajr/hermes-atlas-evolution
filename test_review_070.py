"""0.7.0: owner platforms and the owner command, refused approvals by origin, lesson hygiene, sharper drafts."""

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_atlas_evolution import backfill, lessons, radar, reflector
from hermes_atlas_evolution.experience import experience, utc_day
from hermes_atlas_evolution.operations import close_observers
from hermes_atlas_evolution.signals import FeedbackSensor, command_program, owner_turn, record_tool

os.environ.setdefault("ATLAS_EVOLUTION_NO_HERMES", "1")   # never the real Hermes of this machine

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp()
DAY = 86_400
REFUSED = json.dumps({"output": "", "exit_code": -1, "error": "BLOCKED: User denied this command. The user has NOT "
                      "consented to this action. Do NOT retry this command."})
MISSING = json.dumps({"output": "bash: rtk: command not found", "exit_code": 127})
CLEAN = {key: "" for key in ("ATLAS_EVOLUTION_AGENT", "HERMES_SESSION_ID", "HERMES_KANBAN_TASK",
                             "HERMES_DELEGATED_CHILD_CONTEXT")}


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.addCleanup(close_observers)
        self.home = Path(folder.name)

    def history(self, sessions):
        """A state.db with the given (id, source, user_id, started_at) sessions."""
        db = sqlite3.connect(self.home / "state.db")
        db.executescript("""CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, user_id TEXT, parent_session_id TEXT,
            started_at REAL, ended_at REAL, end_reason TEXT);
            CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,
            tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL, finish_reason TEXT);""")
        db.executemany("INSERT INTO sessions VALUES (?, ?, ?, NULL, ?, ?, 'done')",
                       [(sid, source, user, start, start + 600) for sid, source, user, start in sessions])
        return db


class OwnerPlatformTest(Base):
    def test_a_platform_listed_as_the_owners_serves_the_owner_from_the_first_message(self):
        from hermes_atlas_evolution.evolve import options

        chosen = options({"evolution": {"owner_platforms": ["Telegram", "api_server", "telegram"]}})
        self.assertEqual(chosen["owner_platforms"], ("telegram", "api_server"))
        with self.assertRaises(ValueError):
            options({"evolution": {"owner_platforms": ["two words"]}})
        self.assertEqual(options({"evolution": {"owner_platforms": ["two words"]}}, strict=False)["owner_platforms"], ())
        self.assertTrue(owner_turn("gateway", "anyone", frozenset(), origin="api_server", owner_platforms={"api_server"}))
        self.assertFalse(owner_turn("gateway", "anyone", frozenset(), origin="whatsapp", owner_platforms={"api_server"}))
        sensor = FeedbackSensor(self.home, owner_platforms=("api_server",))
        said = {"session_id": "s", "user_message": "não, errado"}
        self.assertEqual(sensor.observe({**said, "platform": "api_server"}, clock=0.0, now=NOW), ["correction"])
        self.assertEqual(sensor.observe({**said, "session_id": "w", "platform": "whatsapp"}, clock=0.0, now=NOW), [])


# Hermes 0.23.1's own text when single-query mode decides (tools/approval.py, _Unattended.block_message).
SINGLE_QUERY = json.dumps({"output": "", "exit_code": -1, "error": (
    "BLOCKED: this command needs approval but single-query mode (-q) runs without a user present to approve it. "
    "Use normal tools instead. To allow commands in single-query mode, set approvals.single_query_mode: approve in "
    "config.yaml.")})


class ApprovalTest(Base):
    def refuse(self, origin, times, tool="terminal", text=None):
        text = text or (SINGLE_QUERY if origin == "oneshot" else REFUSED)
        for index in range(times):
            record_tool(self.home, {"tool_name": tool, "status": "error", "result": text, "session_id": f"{origin}{index}"},
                        now=NOW, origin=origin)

    def test_refusals_nobody_could_answer_become_one_proposal_for_the_owner(self):
        from hermes_atlas_evolution.report import build, markdown

        self.refuse("oneshot", 4)
        self.refuse("subagent", 2, tool="execute_code")
        self.refuse("local", 1)
        close_observers()
        with experience(self.home) as conn:
            reading = radar.approvals(conn, now=NOW)
            found = {item["code"]: item for item in radar.findings({"approvals": reading})}
        self.assertEqual((reading["refused"], reading["unattended"]), (7, 6))
        self.assertEqual(reading["by_origin"], {"oneshot": 4, "subagent": 2, "local": 1})
        self.assertEqual(found["approval_unattended"]["kind"], "approval_policy")
        self.assertEqual(found["approval_unattended"]["evidence"]["by_setting"],
                         {"approvals.single_query_mode": 4, "delegation.subagent_auto_approve": 2})
        self.assertEqual(found["approval_unattended"]["command"], "hermes config get approvals")
        text = markdown(build(self.home, now=NOW, days=7, lang="pt"), "pt")
        self.assertIn("Aprovações recusadas", text)
        self.assertIn("6 onde ninguém podia aprovar (decididas por `approvals.single_query_mode` 4, "
                      "`delegation.subagent_auto_approve` 2)", text)
        self.assertIn("nunca aprove tudo", text)

    def test_a_few_refusals_or_attended_ones_propose_nothing(self):
        self.refuse("local", 9)
        self.refuse("oneshot", 4)
        close_observers()
        with experience(self.home) as conn:
            codes = [item["code"] for item in radar.findings({"approvals": radar.approvals(conn, now=NOW)})]
        self.assertNotIn("approval_unattended", codes)


class HistoryRedoTest(Base):
    def test_a_backfill_done_by_060_is_redone_with_owner_days_and_approval_origins(self):
        db = self.history([("cli1", "oneshot", "u", NOW - 2 * DAY), ("chat", "whatsapp", "stranger", NOW - 2 * DAY + 900)])
        for sid, start in (("cli1", NOW - 2 * DAY), ("chat", NOW - 2 * DAY + 900)):
            db.executemany("""INSERT INTO messages(session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp,
                finish_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", [
                (sid, "user", "run it", None, None, None, start + 1, None),
                (sid, "assistant", "", None, json.dumps([{"id": sid + "c", "function": {"name": "terminal", "arguments": "{}"}}]),
                 None, start + 2, "tool_calls"),
                (sid, "tool", REFUSED, sid + "c", None, "terminal", start + 3, None),
                (sid, "assistant", "ok", None, None, None, start + 4, "stop")])
        db.commit()
        db.close()
        observed = datetime(2026, 11, 15, 0, tzinfo=timezone.utc).timestamp()
        with experience(self.home) as conn:
            for key, value in (("observed_since", repr(observed)), ("backfill_done", "done"), ("backfill_version", "2"),
                               ("backfill_cursor", "1.0|x")):
                conn.execute("INSERT OR REPLACE INTO cursor VALUES (?, ?)", (key, value))
            conn.execute("INSERT INTO failure_owner VALUES ('old', 3)")
        self.assertEqual(backfill.step(self.home, now=NOW, budget_s=10)["status"], "done")
        close_observers()
        with experience(self.home) as conn:
            state = dict(conn.execute("SELECT key, value FROM cursor WHERE key IN ('backfill_version', 'backfill_revision')"))
            self.assertEqual(state, {"backfill_revision": backfill.VERSION, "backfill_version": "2"})   # 0.6.0 reads "2"
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM failure_owner").fetchone()[0], 0)
            origins = dict(conn.execute("SELECT source, SUM(count) FROM approval_daily GROUP BY source").fetchall())
            owned = conn.execute("SELECT SUM(count) FROM failure_owner_daily").fetchone()[0]
        self.assertEqual(origins, {"oneshot": 1, "whatsapp": 1})
        self.assertEqual(owned, 1)   # the one-shot run is the owner's; the stranger's chat is not


class HygieneTest(Base):
    def failure(self, conn, tool, klass, hint, total, sessions, days=(), owner=0):
        import hashlib

        signature = hashlib.sha256("\0".join((tool, klass, hint)).encode()).hexdigest()[:20]
        conn.execute("INSERT INTO failure VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'x')",
                     (signature, tool, klass, hint, NOW - DAY, NOW, total, sessions))
        for day in days:
            conn.execute("INSERT INTO failure_daily VALUES (?, ?, 1)", (utc_day(NOW - day * DAY), signature))
        if owner:
            conn.execute("INSERT INTO failure_owner_daily VALUES (?, ?, ?)", (utc_day(NOW), signature, owner))
        return signature

    def test_derive_writes_one_lesson_per_sentence_and_none_for_a_binary_another_plugin_put_there(self):
        with experience(self.home) as conn:
            self.failure(conn, "terminal", "not_found", "file", 5, 3, owner=5)
            self.failure(conn, "terminal", "not_found", "skill", 4, 3, owner=4)            # the same sentence
            self.failure(conn, "terminal", "missing_dependency", "rtk", 6, 3, owner=6)
            self.failure(conn, "terminal", "rewritten_command", "rtk", 2, 2)               # rtk came from a rewrite
            created = lessons.derive(conn, now=NOW, mode="auto")
            kinds = [row[0] for row in conn.execute("SELECT klass FROM lesson")]
        self.assertEqual((len(created), kinds), (1, ["not_found"]))

    def test_tidy_withdraws_what_the_evidence_no_longer_supports_and_keeps_the_rest(self):
        with experience(self.home) as conn:
            for signature in (self.failure(conn, "skill_manage", "limit_exceeded", "", 1, 1),           # stale
                              self.failure(conn, "terminal", "timeout", "", 9, 4)):                     # still good
                conn.execute("""INSERT INTO lesson(signature, tool, klass, hint, origin, status, created, updated)
                    SELECT signature, tool, klass, hint, 'derived', 'candidate', 1, 1 FROM failure WHERE signature=?""",
                             (signature,))
            for hint in ("file", "skill"):                                                              # one sentence
                signature = self.failure(conn, "terminal", "not_found", hint, 6 if hint == "file" else 5, 3)
                conn.execute("""INSERT INTO lesson(signature, tool, klass, hint, origin, status, created, updated)
                    VALUES (?, 'terminal', 'not_found', ?, 'derived', 'candidate', 1, 1)""", (signature, hint))
            signature = self.failure(conn, "terminal", "missing_dependency", "rtk", 5, 3)
            conn.execute("""INSERT INTO lesson(signature, tool, klass, hint, origin, status, created, updated)
                VALUES (?, 'terminal', 'missing_dependency', 'rtk', 'derived', 'candidate', 1, 1)""", (signature,))
            self.failure(conn, "terminal", "rewritten_command", "rtk", 3, 2)
            for text in ("When a terminal command fails with exit code 1, read the output before retrying it.",   # generic
                         "When a memory write is rejected by validation, do not write it again in the same turn."):
                signature = self.failure(conn, "terminal" if "terminal" in text else "memory", "other", text[:8], 9, 5)
                lessons.add_reflected(conn, signature=signature, tool="terminal", klass="other", hint="", text=text, now=NOW)
            active = self.failure(conn, "web_extract", "timeout", "", 1, 1)                              # decided: kept
            conn.execute("""INSERT INTO lesson(signature, tool, klass, hint, origin, status, created, updated, activated)
                VALUES (?, 'web_extract', 'timeout', '', 'derived', 'active', 1, 1, 1)""", (active,))
            self.assertEqual(lessons.tidy(conn, now=NOW), 4)
            left = {(row[0], row[1], row[2]) for row in conn.execute(
                "SELECT tool, klass, hint FROM lesson WHERE verdict IS NOT 'withdrawn'")}
            kept = conn.execute("SELECT COUNT(*) FROM lesson WHERE status='retired' AND verdict='withdrawn'").fetchone()[0]
        self.assertEqual(kept, 4)   # withdrawn, not deleted: their ids are never given to another lesson
        self.assertEqual(left, {("terminal", "timeout", ""), ("terminal", "not_found", "file"), ("terminal", "other", ""),
                                ("web_extract", "timeout", "")})

    def test_nothing_is_withdrawn_while_the_history_is_still_being_replayed(self):
        from hermes_atlas_evolution.evolve import cycle

        with experience(self.home) as conn:
            signature = self.failure(conn, "skill_manage", "limit_exceeded", "", 1, 1)
            conn.execute("""INSERT INTO lesson(signature, tool, klass, hint, origin, status, created, updated)
                VALUES (?, 'skill_manage', 'limit_exceeded', '', 'derived', 'candidate', 1, 1)""", (signature,))
        with patch.object(backfill, "step", lambda *a, **k: {"status": "running"}):
            result = cycle(self.home, now=NOW, config={"evolution": {"reflector": False}})
        self.assertEqual(result["lessons_withdrawn"], 0)
        with patch.object(backfill, "step", lambda *a, **k: {"status": "done"}):
            self.assertEqual(cycle(self.home, now=NOW, config={"evolution": {"reflector": False}})["lessons_withdrawn"], 1)


class PromoteTest(Base):
    failure = HygieneTest.failure

    def test_a_lesson_waiting_for_the_owner_activates_once_the_owners_turns_hit_its_failure(self):
        with experience(self.home) as conn:
            signature = self.failure(conn, "terminal", "timeout", "", 5, 3)              # only others hit it so far
            older = self.failure(conn, "web_extract", "timeout", "", 5, 3)
            conn.execute("""INSERT INTO lesson(signature, tool, klass, hint, origin, status, created, updated)
                VALUES (?, 'web_extract', 'timeout', '', 'derived', 'candidate', 1, 1)""", (older,))   # suggest or 0.6.0
            [waiting] = lessons.derive(conn, now=NOW, mode="auto")
            self.assertEqual(lessons.promote(conn, now=NOW, mode="auto"), [])
            for each in (signature, older):
                conn.execute("INSERT INTO failure_owner_daily VALUES (?, ?, 2)", (utc_day(NOW), each))
            self.assertEqual(lessons.promote(conn, now=NOW, mode="suggest"), [])     # suggest: every lesson waits
            self.assertEqual(lessons.promote(conn, now=NOW + 60, mode="auto"), [waiting])
            self.assertEqual(lessons.promote(conn, now=NOW + 120, mode="auto"), [])  # once
            states = dict(conn.execute("SELECT tool, status FROM lesson").fetchall())
            changes = conn.execute("SELECT COUNT(*) FROM change WHERE kind='lesson'").fetchone()[0]
        self.assertEqual(states, {"terminal": "active", "web_extract": "candidate"})
        self.assertEqual(changes, 1)   # measured from its activation, like a lesson derived active

    def test_a_rejected_or_withdrawn_lesson_is_never_promoted(self):
        with experience(self.home) as conn:
            rejected = self.failure(conn, "terminal", "timeout", "", 5, 3)
            stale = self.failure(conn, "skill_manage", "limit_exceeded", "", 3, 2)
            first, second = sorted(lessons.derive(conn, now=NOW, mode="auto"))
            lessons.set_status(conn, first, "retired", now=NOW)                       # the operator said no
            conn.execute("UPDATE failure SET total=1, sessions=1 WHERE signature=?", (stale,))
            self.assertEqual(lessons.tidy(conn, now=NOW), 1)                                   # its evidence went away
            for each in (rejected, stale):
                conn.execute("INSERT INTO failure_owner_daily VALUES (?, ?, 3)", (utc_day(NOW), each))
            self.assertEqual(lessons.promote(conn, now=NOW, mode="auto"), [])
            waiting = conn.execute("SELECT COUNT(*) FROM lesson_owner_wait").fetchone()[0]
            states = dict(conn.execute("SELECT id, COALESCE(verdict, status) FROM lesson").fetchall())
        self.assertEqual((waiting, states), (0, {first: "retired", second: "withdrawn"}))

    def test_a_pass_reports_the_promoted_lesson_as_new(self):
        from hermes_atlas_evolution.evolve import cycle

        with experience(self.home) as conn:
            signature = self.failure(conn, "terminal", "timeout", "", 5, 3)
        settings = {"evolution": {"reflector": False}}
        with patch.object(backfill, "step", lambda *a, **k: {"status": "done"}):
            first = cycle(self.home, now=NOW, config=settings)
            with experience(self.home) as conn:
                conn.execute("INSERT INTO failure_owner_daily VALUES (?, ?, 2)", (utc_day(NOW), signature))
            second = cycle(self.home, now=NOW + 3600, config=settings)
        self.assertEqual([item["status"] for item in first["new_lessons"]], ["candidate"])
        self.assertEqual(([item["status"] for item in second["new_lessons"]], second["lessons_promoted"]), (["active"], 1))


class DraftTest(Base):
    def test_generic_drafts_are_declined_and_the_failing_program_reaches_the_model(self):
        failing = {"tool_name": "terminal", "status": "error", "args": {"command": "sudo -u ops systemctl restart nginx"},
                   "result": json.dumps({"output": "Job for nginx.service failed", "exit_code": 1})}
        for index in range(3):
            record_tool(self.home, {**failing, "session_id": f"s{index}"}, now=NOW - index * DAY, sample=True)
        close_observers()
        seen = []

        def ask(instructions, text):
            seen.append(json.loads(text)["latest_error_excerpt"])
            return {"applies": True, "lesson": "When a command fails, read the error message before retrying it."}

        with experience(self.home) as conn:
            outcome = reflector.reflect(conn, ask, now=NOW)
        self.assertEqual((outcome["drafted"], outcome["declined"]), ([], 1))
        self.assertTrue(seen[0].startswith("[systemctl] "))
        self.assertEqual([command_program({"command": c}) for c in ("cd /tmp && make", "FOO=1 /usr/bin/python3 x.py", "echo hi")],
                         ["make", "python3", ""])


class StatusAttentionTest(Base):
    def setUp(self):
        super().setUp()
        self.home = self.home / "profiles" / "work"
        self.home.mkdir(parents=True)

    def test_chats_that_count_as_someone_else_and_a_missing_digest_are_pointed_out(self):
        from hermes_atlas_evolution import status

        db = self.history([("t1", "telegram", "555", NOW - DAY), ("t2", "telegram", "555", NOW - 2 * DAY),
                           ("api", "api_server", "ui", NOW - DAY), ("me", "cli", None, NOW - DAY)])
        db.commit()
        db.close()
        with experience(self.home) as conn:
            conn.execute("""INSERT INTO lesson(tool, klass, hint, origin, status, created, updated)
                VALUES ('terminal', 'timeout', '', 'derived', 'candidate', 1, 1)""")
        report = status.build(self.home, now=NOW, config={})
        self.assertIn("owner_unknown", report["attention"])
        self.assertIn("digest_unscheduled", report["attention"])
        self.assertEqual({item["origin"]: (item["sessions"], item["senders"]) for item in report["chats"]},
                         {"telegram": (2, 1), "api_server": (1, 1)})
        text = status.markdown(report, "pt")
        self.assertIn("`telegram`: 2 sessões em 7 dias de 1 remetente", text)
        self.assertIn("`api_server`: 1 sessão em 7 dias de 1 remetente", text)
        self.assertNotIn("(s)", text)
        self.assertNotIn("555", text)                      # ids stay in the operator's owner command
        (self.home / "cron").mkdir()
        (self.home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [{"name": "hermes-atlas-evolution-digest",
                                                                             "enabled": True}]}))
        report = status.build(self.home, now=NOW, config={"evolution": {"owner_sender_ids": ["555"],
                                                                        "owner_platforms": ["api_server"]}})
        self.assertNotIn("owner_unknown", report["attention"])
        self.assertNotIn("digest_unscheduled", report["attention"])


class OwnerCommandTest(Base):
    def setUp(self):
        super().setUp()
        environment = patch.dict(os.environ, CLEAN)
        environment.start()
        self.addCleanup(environment.stop)
        tree = patch.multiple("hermes_atlas_evolution.cli", _under_hermes=lambda **_: False,
                              _in_hermes_cgroup=lambda **_: False)
        tree.start()
        self.addCleanup(tree.stop)

    def run_cli(self, lang="pt", runtime_config=None, **arguments):
        from hermes_atlas_evolution.cli import execute

        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            execute(SimpleNamespace(lang=lang, evolution_action="owner", **arguments), self.home,
                    runtime_config=runtime_config or {})
        return stream.getvalue()

    def test_the_owner_lists_channels_and_chooses_who_is_them(self):
        db = self.history([("t1", "telegram", "555", NOW - DAY), ("w1", "whatsapp", "5511999", NOW - DAY)])
        db.commit()
        db.close()
        with patch("time.time", return_value=NOW):
            listing = self.run_cli(owner_action=None)
        self.assertIn("`555` (1)", listing)
        self.assertIn("hermes -p default evolution owner add <id>", listing)
        key = "plugins.entries.hermes-atlas-evolution.settings"
        saved = self.run_cli(owner_action="add", value="555")
        self.assertIn(f"hermes -p default config set {key}.owner_sender_ids '[\"555\"]'", saved)
        self.assertNotIn("hermes gateway restart", saved)   # 0.9.0: refresh_settings picks it up, no restart
        self.assertIn("não precisa reiniciar", saved)
        self.assertFalse((self.home / "evolution" / "settings.json").exists())   # printed for the owner, never written
        form = {"plugins": {"entries": {"hermes-atlas-evolution": {"settings": {"owner_sender_ids": ["555"],
                                                                                "owner_platforms": ["telegram"]}}}}}
        both = self.run_cli(owner_action="add-platform", value="API_Server", runtime_config=form)
        self.assertIn(f"{key}.owner_platforms '[\"telegram\", \"api_server\"]'", both)
        self.assertIn(f"{key}.owner_sender_ids '[]'", self.run_cli(owner_action="remove", value="555", runtime_config=form))
        self.assertIn("Nada a mudar", self.run_cli(owner_action="add", value="555", runtime_config=form))
        with self.assertRaises(SystemExit) as refused:
            self.run_cli(owner_action="add-platform", value="two words")
        self.assertIn("Informe uma plataforma", str(refused.exception))
        # Values an agent could have written to settings.json are shown as ignored, never used.
        (self.home / "evolution").mkdir(exist_ok=True)
        (self.home / "evolution" / "settings.json").write_text(json.dumps({"evolution": {"owner_platforms": ["whatsapp"]}}))
        with patch("time.time", return_value=NOW):
            listing = self.run_cli(owner_action=None)
        self.assertIn("plataformas: nenhum", listing)
        self.assertIn("Ignorado no `settings.json`", listing)

    def test_an_agent_cannot_run_it(self):
        with patch.dict(os.environ, {"ATLAS_EVOLUTION_AGENT": "1"}), self.assertRaises(SystemExit):
            self.run_cli(owner_action="add", value="123")
        self.assertFalse((self.home / "evolution" / "settings.json").exists())



class SlowToolTest(Base):
    def test_the_history_times_each_tool_and_a_tool_slow_call_after_call_is_proposed(self):
        from hermes_atlas_evolution.report import build, markdown

        db = self.history([("s", "cli", None, NOW - DAY)])
        rows, clock = [], NOW - DAY
        # web_extract: 40 s from the request to the result; terminal: 2 s; clarify waits for a person and
        # delegate_task for a whole subagent, which is not a slow tool
        for index in range(6):
            for name, seconds in (("web_extract", 40), ("terminal", 2), ("clarify", 300), ("delegate_task", 200)):
                call = f"{name}{index}"
                rows += [("s", "assistant", "", None, json.dumps([{"id": call, "function": {"name": name, "arguments": "{}"}}]),
                          None, clock, "tool_calls"),
                         ("s", "tool", "{}", call, None, name, clock + seconds, None)]
                clock += seconds + 1
        db.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES ('s', 'user', 'go', ?)", (NOW - DAY - 1,))
        db.executemany("""INSERT INTO messages(session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp,
            finish_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", rows)
        db.commit()
        db.close()
        with experience(self.home) as conn:
            conn.execute("INSERT INTO cursor VALUES ('observed_since', ?)", (repr(NOW),))
        self.assertEqual(backfill.step(self.home, now=NOW, budget_s=10)["status"], "done")
        close_observers()
        with experience(self.home) as conn:
            durations = dict(conn.execute("SELECT tool, duration_ms FROM tool_daily").fetchall())
            reading = radar.scan(self.home, conn, now=NOW)
        self.assertEqual(durations, {"web_extract": 240_000, "terminal": 12_000, "clarify": 1_800_000,
                                     "delegate_task": 1_200_000})
        codes = {item["code"]: item for item in reading["findings"]}
        self.assertEqual(codes["tool_slow:web_extract"]["evidence"]["mean_s"], 40.0)
        self.assertEqual(sorted(code for code in codes if code.startswith("tool_slow:")), ["tool_slow:web_extract"])
        self.assertEqual([item["tool"] for item in reading["slow_tools"]], ["web_extract", "terminal"])
        text = markdown(build(self.home, now=NOW, days=7, lang="pt"), "pt")
        self.assertIn("**Ferramentas mais lentas:** `web_extract` 40,0 s em média (6 chamadas)", text)

    def test_the_digest_points_to_lessons_review_and_speaks_portuguese_in_commands(self):
        from hermes_atlas_evolution import proposals

        with experience(self.home) as conn:
            proposals.refresh(conn, now=NOW, failures=[{"signature": "x", "tool": "terminal", "class": "other", "hint": "",
                                                       "count": 6, "days": 3}], lang="pt")
            command = conn.execute("SELECT command FROM proposal WHERE kind='investigate_failure'").fetchone()[0]
            listed = proposals.listing(conn, status="open")
        # 0.12.0: the stored command keeps a placeholder of no language; the reader's language is written when shown
        self.assertIn("<what to do instead>", command)
        from hermes_atlas_evolution import cli

        shown = cli._proposals_md(listed, self.home, "pt", everything=False)
        self.assertIn('evolution lessons add --tool terminal "<o que fazer no lugar>"', shown)
        self.assertNotIn("what to do instead", shown)


if __name__ == "__main__":
    unittest.main()
