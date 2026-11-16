"""0.7.0, after an adversarial review of the fixes: the guard reads `hermes` the way Hermes does, delegated children
in the history, decisions the plugin never overrides, lesson records that are kept, the replay beside an older
copy or without the owner settings, who answered an approval, the excerpt's end and tools that wait for people."""

import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from hermes_atlas_evolution import backfill, lessons
from hermes_atlas_evolution.experience import experience, prune, utc_day
from hermes_atlas_evolution.guard import check
from hermes_atlas_evolution.operations import close_observers
from hermes_atlas_evolution.signals import NOT_LATENCY, approval_setting, classify, record_tool, slow_calls

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp()
DAY = 86_400
TIMEOUT = json.dumps({"error": "Command timed out after 180 seconds"})


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.addCleanup(close_observers)
        self.home = Path(folder.name)

    def failure(self, conn, tool, klass, hint, total, sessions, *, owner=0):
        signature = hashlib.sha256("\0".join((tool, klass, hint)).encode()).hexdigest()[:20]
        conn.execute("INSERT INTO failure VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'x')",
                     (signature, tool, klass, hint, NOW - DAY, NOW, total, sessions))
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

    def timed_out(self, db, sid, source, user, start, parent=None):
        db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, 'done')", (sid, source, user, parent, start, start + 900))
        db.executemany("""INSERT INTO messages(session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp,
            finish_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", [
            (sid, "user", "run the build", None, None, None, start + 1, None),
            (sid, "assistant", "", None, json.dumps([{"id": f"{sid}c", "function": {"name": "terminal", "arguments": "{}"}}]),
             None, start + 2, "tool_calls"),
            (sid, "tool", TIMEOUT, f"{sid}c", None, "terminal", start + 3, None),
            (sid, "assistant", "gave up", None, None, None, start + 5, "stop")])


class GuardArgvTest(unittest.TestCase):
    """The profile flag works anywhere in a `hermes` command line, and the shell can fill words in."""

    def kind(self, command):
        found = check("terminal", {"command": command}, protected=(), home=Path("/home/x"))
        return found and found["kind"]

    def test_options_between_the_words_do_not_hide_the_command(self):
        key = "plugins.entries.hermes-atlas-evolution.settings"
        for command in (f"hermes config set -p default {key}.owner_platforms '[\"telegram\"]'",
                        f"hermes config set --profile default {key}.runtime_guard false",
                        f"hermes config -p default set {key}.agent_tools true",
                        f"hermes --model gpt config set {key}.lessons auto",
                        "hermes plugins -p default disable hermes-atlas-evolution",
                        f"hermes config set $'{key}.owner_platforms' '[\"telegram\"]'",
                        "K=plugins.disabled; hermes config set $K '[\"hermes-atlas-evolution\"]'",
                        "hermes plugins $(echo disable) hermes-atlas-evolution"):
            self.assertEqual(self.kind(command), "plugin_settings", command)
        for command in ("hermes gateway -p default restart --yes", "hermes gateway --profile default stop",
                        "hermes gateway $'restart' --yes"):
            self.assertEqual(self.kind(command), "gateway_stop", command)

    def test_switching_the_plugin_or_the_guard_off_through_the_plugins_section_is_vetoed(self):
        for command in ("hermes config set plugins.disabled '[\"hermes-atlas-evolution\"]'",
                        "hermes config set plugins.enabled '[]'", "hermes config set plugins.isolation host",
                        "hermes config unset plugins.entries", "EDITOR='sed -i s/x/y/' hermes config edit"):
            self.assertEqual(self.kind(command), "plugin_settings", command)
        for command in ("hermes config set plugins.entries.other-plugin.settings.mode fast",
                        "hermes config get plugins.disabled", "hermes plugins enable other-plugin",
                        "hermes gateway status", "hermes -p work chat -q hello", "hermes config set 'plugins\\.x' y"):
            self.assertIsNone(self.kind(command), command)


class HistoryChildTest(Base):
    def test_a_replayed_child_serves_whoever_its_parent_served(self):
        db = self.history()
        self.timed_out(db, "tg", "telegram", "stranger", NOW - 3 * DAY)
        self.timed_out(db, "tg-child", "subagent", None, NOW - 3 * DAY + 60, parent="tg")
        self.timed_out(db, "me", "cli", None, NOW - 2 * DAY)
        self.timed_out(db, "me-child", "subagent", None, NOW - 2 * DAY + 60, parent="me")
        self.timed_out(db, "me-grandchild", "subagent", None, NOW - 2 * DAY + 120, parent="me-child")
        self.timed_out(db, "orphan", "subagent", None, NOW - DAY, parent="gone")
        db.commit()
        db.close()
        with experience(self.home) as conn:
            conn.execute("INSERT INTO cursor VALUES ('observed_since', ?)", (repr(NOW),))
        self.assertEqual(backfill.step(self.home, now=NOW, budget_s=10)["status"], "done")
        close_observers()
        with experience(self.home) as conn:
            owned = conn.execute("SELECT SUM(count) FROM failure_owner_daily").fetchone()[0]
            total = conn.execute("SELECT SUM(count) FROM failure_daily").fetchone()[0]
        self.assertEqual((owned, total), (3, 6))   # me, me-child, me-grandchild; never tg-child or the orphan


class DecisionTest(Base):
    def test_retiring_a_withdrawn_lesson_records_the_decision_and_it_never_comes_back(self):
        with experience(self.home) as conn:
            self.failure(conn, "terminal", "timeout", "", 5, 3)
            [lesson] = lessons.derive(conn, now=NOW, mode="auto")
            conn.execute("UPDATE failure SET total=1, sessions=1")
            conn.execute("DELETE FROM failure_daily")
            self.assertEqual(lessons.tidy(conn, now=NOW), 1)
            lessons.set_status(conn, lesson, "retired", now=NOW + 60)   # the operator says no to it
            conn.execute("UPDATE failure SET total=9, sessions=4")     # the failure is back
            conn.execute("INSERT INTO failure_owner_daily SELECT ?, signature, 5 FROM failure", (utc_day(NOW),))
            self.assertEqual(lessons.derive(conn, now=NOW + DAY, mode="auto"), [])
            state = conn.execute("SELECT status, verdict FROM lesson").fetchone()
        self.assertEqual(tuple(state), ("retired", None))

    def test_a_pass_never_overrides_a_decision_the_operator_made_meanwhile(self):
        with experience(self.home) as conn:
            approved = self.failure(conn, "terminal", "timeout", "", 1, 1)       # stale: tidy would withdraw it
            conn.execute("""INSERT INTO lesson(signature, tool, klass, hint, origin, status, created, updated)
                VALUES (?, 'terminal', 'timeout', '', 'derived', 'candidate', 1, 1)""", (approved,))
            first = conn.execute("SELECT id FROM lesson").fetchone()[0]

            text_key = lessons._text_key

            def operator_approves(*args):   # runs after tidy read the candidates
                conn.execute("UPDATE lesson SET status='active' WHERE id=?", (first,))
                return text_key(*args)

            with patch.object(lessons, "_text_key", operator_approves):
                self.assertEqual(lessons.tidy(conn, now=NOW), 0)
            waiting = self.failure(conn, "web_extract", "timeout", "", 5, 3)
            [second] = lessons.derive(conn, now=NOW, mode="auto")
            conn.execute("INSERT INTO failure_owner_daily VALUES (?, ?, 3)", (utc_day(NOW), waiting))
            owner_by_key = lessons._owner_by_key

            def operator_rejects(connection):   # runs after promote read the waiting lessons
                conn.execute("UPDATE lesson SET status='retired' WHERE id=?", (second,))
                return owner_by_key(connection)

            with patch.object(lessons, "_owner_by_key", operator_rejects):
                self.assertEqual(lessons.promote(conn, now=NOW, mode="auto"), [])
            states = dict(conn.execute("SELECT id, status FROM lesson").fetchall())
        self.assertEqual(states, {first: "active", second: "retired"})

    def test_retired_lessons_outlive_retention_so_a_turned_down_sentence_stays_down(self):
        with experience(self.home) as conn:
            self.failure(conn, "terminal", "timeout", "", 5, 3, owner=5)
            [lesson] = lessons.derive(conn, now=NOW - 100 * DAY, mode="auto")
            lessons.set_status(conn, lesson, "retired", now=NOW - 100 * DAY)
            prune(conn, now=NOW)
            kept = conn.execute("SELECT COUNT(*) FROM lesson").fetchone()[0]
            conn.execute("DELETE FROM failure")
            conn.execute("DELETE FROM failure_daily")
            conn.execute("DELETE FROM failure_owner_daily")
            self.failure(conn, "terminal", "timeout", "", 6, 3, owner=6)   # recorded again after it was pruned
            again = lessons.derive(conn, now=NOW, mode="auto")
            newer = lessons.add_custom(conn, "When the build fails, read the first error line first.", tool="terminal",
                                       origin="operator", now=NOW)
        self.assertEqual((kept, again), (1, []))
        self.assertGreater(newer, lesson)


class ReplayStateTest(Base):
    def settled(self, owner="x", revision="3", version="2"):
        with experience(self.home) as conn:
            for key, value in (("observed_since", repr(NOW - DAY)), ("backfill_done", "done"),
                               ("backfill_revision", revision), ("backfill_version", version),
                               ("backfill_owner", owner), ("backfill_cursor", f"{NOW - 2 * DAY!r}|s")):
                if value is not None:
                    conn.execute("INSERT OR REPLACE INTO cursor VALUES (?, ?)", (key, value))

    def state(self):
        close_observers()
        with experience(self.home) as conn:
            return dict(conn.execute("SELECT key, value FROM cursor WHERE key LIKE 'backfill_%'").fetchall())

    def test_an_older_copy_beside_this_one_reads_a_replay_it_must_not_redo(self):
        db = self.history()
        db.commit()
        db.close()
        self.settled(owner=backfill.owner_fingerprint(), revision=None, version="2")   # what 0.6.0 left
        self.assertEqual(backfill.step(self.home, now=NOW, budget_s=5)["status"], "done")
        state = self.state()
        self.assertEqual((state["backfill_version"], state["backfill_revision"]), ("2", backfill.VERSION))

    def test_a_run_that_cannot_read_the_owner_settings_never_redoes_for_them(self):
        db = self.history()
        db.commit()
        db.close()
        self.settled(owner=backfill.owner_fingerprint(["555"], ()))
        self.assertEqual(backfill.step(self.home, now=NOW, budget_s=5, owner_known=False)["status"], "done")
        state = self.state()   # not redone: the cursor and the owner it was replayed for stay
        self.assertEqual((state["backfill_cursor"], state["backfill_owner"]),
                         (f"{NOW - 2 * DAY!r}|s", backfill.owner_fingerprint(["555"], ())))
        self.assertEqual(backfill.step(self.home, now=NOW, budget_s=5, owner_sender_ids=["555"])["status"], "done")
        self.assertEqual(self.state()["backfill_owner"], backfill.owner_fingerprint(["555"], ()))

    def test_a_new_owner_without_a_readable_history_keeps_what_was_counted(self):
        with experience(self.home) as conn:
            self.failure(conn, "terminal", "timeout", "", 5, 3)
            conn.execute("UPDATE failure_daily SET day=?", (utc_day(NOW - 3 * DAY),))
        self.settled(owner=backfill.owner_fingerprint())
        backfill.step(self.home, now=NOW, budget_s=5, owner_sender_ids=["555"])   # no state.db to replay again
        state = self.state()
        with experience(self.home) as conn:
            total = conn.execute("SELECT total FROM failure").fetchone()[0]
        self.assertEqual((state["backfill_done"], state["backfill_owner"], total),
                         ("done", backfill.owner_fingerprint(["555"], ()), 5))

    def test_the_oldest_day_a_replay_reads_is_fixed_when_it_starts(self):
        import time

        now = time.time()   # the hooks' daily prune follows the real clock
        floor = utc_day(now - 90 * DAY)
        db = self.history()
        self.timed_out(db, "old", "cli", None, backfill._day_start(floor) + 60)   # the oldest day retention keeps
        db.commit()
        db.close()
        with experience(self.home) as conn:
            conn.execute("INSERT INTO cursor VALUES ('observed_since', ?)", (repr(now - 80 * DAY),))
            conn.execute("INSERT INTO cursor VALUES ('backfill_floor', ?)", (floor,))
        # the replay started before midnight goes on after it: it still reads the day it began with
        self.assertEqual(backfill.step(self.home, now=now + DAY, budget_s=5)["status"], "done")
        close_observers()
        with experience(self.home) as conn:
            self.assertEqual(conn.execute("SELECT SUM(count) FROM failure_daily").fetchone()[0], 1)


class WhoAnsweredTest(unittest.TestCase):
    def test_a_persons_answer_is_never_counted_as_nobody_could_answer(self):
        gateway_denied = "BLOCKED: Command denied by user. The user has NOT consented to this action."
        cli_denied = "BLOCKED: User denied this command. The user has NOT consented to this action."
        timed_out = "BLOCKED: Command timed out without user response. Silence is not consent."
        unattended = "BLOCKED: ... To allow it on unattended platforms, set approvals.unattended_mode: approve in config.yaml."
        transport = "BLOCKED: User denied this command through the selected approval transport."
        self.assertEqual([approval_setting(gateway_denied, "kanban"), approval_setting(transport, "tool"),
                          approval_setting(cli_denied, "oneshot")], ["", "", ""])
        self.assertEqual([approval_setting(gateway_denied, "api_server"), approval_setting(unattended, "api_server"),
                          approval_setting(timed_out, "api_server"), approval_setting(gateway_denied, "subagent"),
                          approval_setting(cli_denied, "subagent"), approval_setting(timed_out, "kanban"),
                          approval_setting(cli_denied, "local"), approval_setting(timed_out, "telegram")],
                         ["", "approvals.unattended_mode", "", "", "delegation.subagent_auto_approve", "approvals", "",
                          ""])


class LegacyRecallTest(Base):
    def test_the_01_notes_are_switched_on_only_from_the_hermes_configuration(self):
        from hermes_atlas_evolution.cli import effective_settings

        (self.home / "evolution").mkdir()
        (self.home / "evolution" / "settings.json").write_text(json.dumps({"legacy_memory_commands": True,
                                                                           "automatic_shared_recall": True}))
        untrusted = effective_settings(self.home, {})
        self.assertEqual((untrusted.get("legacy_memory_commands"), untrusted.get("automatic_shared_recall")), (None, None))
        form = {"plugins": {"entries": {"hermes-atlas-evolution": {"settings": {"legacy_memory_commands": True}}}}}
        self.assertIs(effective_settings(self.home, form).get("legacy_memory_commands"), True)


class ExcerptTailTest(Base):
    def test_the_program_prefix_keeps_the_end_of_the_error(self):
        error = json.dumps({"output": "noise " * 200 + "fatal: refusing to merge unrelated histories", "exit_code": 128})
        record_tool(self.home, {"tool_name": "terminal", "status": "error", "result": error,
                                "args": {"command": "git pull"}, "session_id": "s"}, now=NOW, sample=True, owner=True)
        close_observers()
        with experience(self.home) as conn:
            excerpt = conn.execute("SELECT excerpt FROM failure_sample").fetchone()[0]
        self.assertTrue(excerpt.startswith("[git] "))
        self.assertTrue(excerpt.endswith("unrelated histories"))
        self.assertLessEqual(len(excerpt), 300)


class WaitingToolsTest(Base):
    def test_tools_that_wait_for_a_person_are_never_slow(self):
        self.assertLessEqual({"clarify", "setup_beat", "gui_tour", "browser_vault", "delegate_task"}, NOT_LATENCY)
        with experience(self.home) as conn:
            for tool in ("setup_beat", "gui_tour"):
                conn.execute("INSERT INTO tool_daily VALUES (?, ?, 9, 0, 0, ?)", (utc_day(NOW), tool, 9 * 300_000))
            conn.commit()
            self.assertEqual(slow_calls(conn, now=NOW, days=7, min_calls=5, min_mean_s=20), [])


class GenericWordingTest(unittest.TestCase):
    def test_generic_advice_in_other_words_is_still_generic(self):
        self.assertTrue(lessons.generic_advice("Quando um comando de terminal falhar com código de saída 1, leia a saída "
                                               "para diagnosticar a causa antes de repetir a mesma chamada."))
        self.assertTrue(lessons.generic_advice("When it fails, read the output to understand the cause before "
                                               "repeating the same command."))
        self.assertFalse(lessons.generic_advice("Quando precisar parar um serviço do sistema, verifique o status antes e "
                                                "use `systemctl --user` em vez do comando direto."))


if __name__ == "__main__":
    unittest.main()
