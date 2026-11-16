"""Learning from what Hermes recorded before the plugin was installed (state.db), with the hooks' semantics."""

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hermes_atlas_evolution import backfill
from hermes_atlas_evolution.evolve import cycle
from hermes_atlas_evolution.experience import experience, utc_day
from hermes_atlas_evolution.operations import close_observers

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp()
DAY = 86_400
SECRET = "sk-live-0123456789abcdefSECRET"


class History:
    """A Hermes state.db with the columns the backfill reads."""

    def __init__(self, home: Path):
        self.path = home / "state.db"
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript("""
            CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, user_id TEXT, parent_session_id TEXT,
                started_at REAL, ended_at REAL, end_reason TEXT);
            CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,
                tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL, finish_reason TEXT,
                _compressed_summary INTEGER DEFAULT 0, active INTEGER DEFAULT 1, compacted INTEGER DEFAULT 0,
                message_uid TEXT);""")
        self.calls = 0
        self.uids = False

    def session(self, sid, *, start, source="local", parent=None, end_reason="cli_close"):
        self.conn.execute("INSERT INTO sessions VALUES (?, ?, 'u1', ?, ?, ?, ?)",
                          (sid, source, parent, start, start + 3600, end_reason))

    def message(self, sid, role, ts, *, content="", tool_calls=None, tool_name=None, call_id=None, finish=None, summary=0):
        self.conn.execute("""INSERT INTO messages(session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp,
            finish_reason, _compressed_summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                          (sid, role, content, call_id, json.dumps(tool_calls) if tool_calls else None, tool_name, ts, finish,
                           summary))
        if self.uids:   # Hermes stamps every message with a durable id that its copies keep
            self.conn.execute("UPDATE messages SET message_uid='uid-' || id WHERE id=last_insert_rowid()")

    def compact(self, sid, *, later=0, prune_tools=False):
        """What Hermes' in-place compaction leaves: the old rows archived, copies of them re-inserted (same uid;
        with `later`, a later timestamp; with `prune_tools`, tool results shortened to a stub), and a summary."""
        rows = self.conn.execute("SELECT session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp, "
                                 "finish_reason, message_uid FROM messages WHERE session_id=? AND active=1", (sid,)).fetchall()
        self.conn.execute("UPDATE messages SET active=0, compacted=1 WHERE session_id=? AND active=1", (sid,))
        self.conn.executemany("""INSERT INTO messages(session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp,
            finish_reason, message_uid) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                              [(*row[:2], "[pruned]" if prune_tools and row[1] == "tool" else row[2], *row[3:6],
                                row[6] + later, *row[7:]) for row in rows])
        self.conn.execute("""INSERT INTO messages(session_id, role, content, timestamp, _compressed_summary, message_uid)
            VALUES (?, 'user', '[summary of the conversation so far]', ?, 1, ?)""", (sid, rows[-1][6] + later + 1, rows[0][8]))

    def tool_turn(self, sid, ts, *, user="please do it", tool="terminal", args=None, result="{}", final=True,
                  extra_tools=()):
        """user -> assistant(tool call) -> tool result [-> more tools] -> assistant(stop)."""
        self.message(sid, "user", ts, content=user)
        clock = ts
        for name, arguments, body in ((tool, args or {"command": "x"}, result), *extra_tools):
            self.calls += 1
            call = f"call-{self.calls}"
            clock += 2
            self.message(sid, "assistant", clock, tool_calls=[{"id": call, "call_id": call, "type": "function",
                         "function": {"name": name, "arguments": json.dumps(arguments)}}], finish="tool_calls")
            clock += 3
            self.message(sid, "tool", clock, content=body, tool_name=name, call_id=call)
        if final:
            self.message(sid, "assistant", clock + 5, content="done", finish="stop")
        return clock + 5

    def save(self):
        self.conn.commit()
        self.conn.close()


MISSING = json.dumps({"output": f"bash: rg: command not found\n{SECRET}", "exit_code": 127, "error": None})


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.addCleanup(close_observers)
        self.home = Path(folder.name)
        self.history = History(self.home)

    def run_backfill(self, *, observed=NOW, **options):
        with experience(self.home) as conn:
            conn.execute("INSERT OR IGNORE INTO cursor VALUES ('observed_since', ?)", (repr(observed),))
        result = None
        for _ in range(50):
            result = backfill.step(self.home, now=NOW, budget_s=10, **options)
            if result["status"] != "running":
                break
        close_observers()
        return result


class BackfillTest(Base):
    def test_recurring_failures_of_the_past_become_a_lesson_on_the_first_pass(self):
        for day in (3, 2, 1):
            self.history.session(f"s{day}", start=NOW - day * DAY)
            self.history.tool_turn(f"s{day}", NOW - day * DAY + 10, args={"command": "rg foo"}, result=MISSING)
            self.history.tool_turn(f"s{day}", NOW - day * DAY + 100, args={"command": "rg foo"}, result=MISSING)
        self.history.save()
        result = self.run_backfill()
        self.assertEqual((result["status"], result["sessions"], result["tool_calls"], result["turns"]), ("done", 3, 6, 6))
        with experience(self.home) as conn:
            failure = conn.execute("SELECT tool, klass, hint, total, sessions FROM failure").fetchone()
            self.assertEqual(tuple(failure), ("terminal", "missing_dependency", "rg", 6, 3))
            self.assertEqual(tuple(conn.execute("SELECT SUM(calls), SUM(errors) FROM tool_daily").fetchone()), (6, 6))
        learned = cycle(self.home, now=NOW, config={"evolution": {"reflector": False}})
        self.assertEqual([item["class"] for item in learned["new_lessons"]], ["missing_dependency"])

    def test_turns_skills_and_surfaces_follow_the_hooks(self):
        self.history.session("chat", start=NOW - DAY)
        self.history.tool_turn("chat", NOW - DAY + 10, tool="skill_view", args={"name": "deploy"},
                               result=json.dumps({"success": True, "content": "..."}),
                               extra_tools=(("terminal", {"command": "ls"}, json.dumps({"output": "a", "exit_code": 0})),))
        self.history.message("chat", "user", NOW - DAY + 400, content="Não, está errado, refaça")   # corrects that turn
        self.history.message("chat", "assistant", NOW - DAY + 500, content="ok", finish="stop")
        self.history.session("cron-job", start=NOW - DAY, source="cron", end_reason="cron_complete")
        self.history.tool_turn("cron-job", NOW - DAY + 50)
        self.history.session("child", start=NOW - DAY, source="subagent", parent="chat")
        self.history.tool_turn("child", NOW - DAY + 60, final=False)                                     # never answered
        self.history.save()
        self.run_backfill()
        with experience(self.home) as conn:
            surfaces = {row["surface"]: (row["turns"], row["completed"], row["failed"])
                        for row in conn.execute("SELECT * FROM turn_daily")}
            self.assertEqual(surfaces, {"local": (2, 2, 0), "cron": (1, 1, 0), "subagent": (1, 0, 1)})
            skill = conn.execute("SELECT turns, corrected FROM skill_outcome_daily WHERE skill='deploy'").fetchone()
            self.assertEqual(tuple(skill), (1, 1))
            feedback = dict(conn.execute("SELECT kind, count FROM feedback_daily").fetchall())
            self.assertEqual(feedback, {"correction": 1})
            loaded = conn.execute("SELECT count FROM skill_daily WHERE skill='deploy' AND action='loaded'").fetchone()
            self.assertEqual(loaded[0], 1)

    def test_messages_sent_before_the_agent_answers_form_one_turn_and_are_not_a_repeated_question(self):
        self.history.session("chat", start=NOW - DAY)
        question = "please summarize the quarterly sales report for the north region today"
        self.history.message("chat", "user", NOW - DAY + 10, content=question)
        self.history.message("chat", "user", NOW - DAY + 20, content=question + " please")    # sent while it was busy
        self.history.message("chat", "assistant", NOW - DAY + 60, content="done", finish="stop")
        self.history.message("chat", "user", NOW - DAY + 100, content="ok thanks, next one")
        self.history.message("chat", "user", NOW - DAY + 105, content="actually wait")      # interjects mid-turn
        self.history.message("chat", "assistant", NOW - DAY + 110, content="...", finish="stop")
        self.history.save()
        self.assertEqual(self.run_backfill()["turns"], 2)
        with experience(self.home) as conn:
            row = conn.execute("SELECT turns, completed, interrupted FROM turn_daily").fetchone()
            self.assertEqual(tuple(row), (2, 2, 0))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM feedback_daily WHERE kind='reask'").fetchone()[0], 0)

    def test_research_turns_of_the_past_are_timed_and_reach_the_digest(self):
        from hermes_atlas_evolution.report import build, markdown

        self.history.session("s", start=NOW - DAY)
        self.history.tool_turn("s", NOW - DAY + 10, tool="web_search", args={"query": "x"},
                               result=json.dumps({"results": []}),
                               extra_tools=(("web_extract", {"urls": ["u"]}, json.dumps({"error": "fetch failed"})),))
        self.history.save()
        self.run_backfill()
        report = build(self.home, now=NOW, days=7, lang="pt")
        self.assertEqual((report["research"]["turns"], report["research"]["tool_calls"], report["research"]["tool_errors"]),
                         (1, 2, 1))
        self.assertIn("Pesquisas na web", markdown(report, "pt"))

    def test_nothing_after_the_plugin_started_observing_is_counted_and_a_rerun_adds_nothing(self):
        self.history.session("s", start=NOW - DAY)
        self.history.tool_turn("s", NOW - DAY + 10, result=MISSING)
        self.history.tool_turn("s", NOW + 10, result=MISSING)          # the hooks saw this one
        self.history.save()
        self.assertEqual(self.run_backfill()["tool_calls"], 1)
        self.assertEqual(self.run_backfill()["status"], "done")        # finished: a second run reads nothing
        with experience(self.home) as conn:
            self.assertEqual(conn.execute("SELECT SUM(calls) FROM tool_daily").fetchone()[0], 1)

    def test_only_the_backfill_window_is_read_and_slices_resume(self):
        for index in range(7):
            self.history.session(f"s{index}", start=NOW - (index + 1) * 3 * DAY)
            self.history.tool_turn(f"s{index}", NOW - (index + 1) * 3 * DAY + 10)
        self.history.save()
        result = self.run_backfill(slice_sessions=2)
        self.assertEqual((result["status"], result["sessions"]), ("done", 4))   # 3, 6, 9 and 12 days ago

    def test_a_slice_is_dropped_when_another_pass_moved_the_cursor_meanwhile(self):
        from unittest.mock import patch

        for index in range(3):
            self.history.session(f"s{index}", start=NOW - (index + 1) * DAY)
            self.history.tool_turn(f"s{index}", NOW - (index + 1) * DAY + 10, result=MISSING)
        self.history.save()
        with experience(self.home) as conn:
            conn.execute("INSERT OR REPLACE INTO cursor VALUES ('observed_since', ?)", (repr(NOW),))
        original = backfill._Replay.session

        def overtaken(replay, *args, **kwargs):   # another process advanced the cursor while this slice ran
            original(replay, *args, **kwargs)
            with experience(self.home) as conn:
                conn.execute("INSERT OR REPLACE INTO cursor VALUES ('backfill_cursor', 'other|x')")

        with patch.object(backfill._Replay, "session", overtaken):
            result = backfill.step(self.home, now=NOW, budget_s=10, slice_sessions=1)
        close_observers()
        self.assertEqual((result["status"], result["tool_calls"]), ("running", 0))
        with experience(self.home) as conn:
            self.assertEqual(conn.execute("SELECT COALESCE(SUM(calls), 0) FROM tool_daily").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT value FROM cursor WHERE key='backfill_cursor'").fetchone()[0], "other|x")

    def test_a_compacted_session_is_counted_once(self):
        self.history.session("long", start=NOW - DAY)
        self.history.tool_turn("long", NOW - DAY + 10, result=MISSING)
        self.history.tool_turn("long", NOW - DAY + 100, result=MISSING)
        self.history.compact("long")
        self.history.save()
        result = self.run_backfill()
        self.assertEqual((result["turns"], result["tool_calls"], result["failures"]), (2, 2, 2))
        with experience(self.home) as conn:
            self.assertEqual(conn.execute("SELECT SUM(calls) FROM tool_daily").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT SUM(turns) FROM turn_daily").fetchone()[0], 2)

    def test_copies_with_a_later_timestamp_or_a_shortened_body_are_counted_once(self):
        self.history.uids = True
        self.history.session("long", start=NOW - DAY)
        self.history.tool_turn("long", NOW - DAY + 10, result=MISSING)
        self.history.tool_turn("long", NOW - DAY + 100, result=MISSING)
        self.history.compact("long", later=900, prune_tools=True)    # carried forward: new time, stub body
        self.history.compact("long", later=1800)                     # and compacted again
        self.history.tool_turn("long", NOW - DAY + 5000, result=MISSING)
        self.history.save()
        result = self.run_backfill()
        self.assertEqual((result["turns"], result["tool_calls"], result["failures"]), (3, 3, 3))
        with experience(self.home) as conn:
            self.assertEqual(tuple(conn.execute("SELECT SUM(turns), SUM(completed) FROM turn_daily").fetchone()), (3, 3))

    def test_a_pathological_payload_is_skipped_not_fatal(self):
        deep = '{"a":' + "[" * 100_000 + "]" * 100_000 + "}"   # under the size limits, too deep for the JSON parser
        self.history.session("s", start=NOW - DAY)
        self.history.tool_turn("s", NOW - DAY + 10, result=deep)
        self.history.message("s", "assistant", NOW - DAY + 60, finish="tool_calls",
                             tool_calls=[{"id": "deep", "function": {"name": "terminal", "arguments": "[" * 60_000}}])
        self.history.tool_turn("s", NOW - DAY + 100, result=MISSING)
        self.history.save()
        result = self.run_backfill()
        self.assertEqual((result["status"], result["tool_calls"], result["skipped"]), ("done", 2, 0))

    def test_a_session_that_cannot_be_replayed_is_skipped_and_counted_and_leaves_nothing(self):
        from unittest.mock import patch

        self.history.session("bad", start=NOW - 2 * DAY)
        self.history.tool_turn("bad", NOW - 2 * DAY + 10, result=MISSING)
        self.history.tool_turn("bad", NOW - 2 * DAY + 100, result="explode")
        self.history.session("good", start=NOW - DAY)
        self.history.tool_turn("good", NOW - DAY + 10, result=MISSING)
        self.history.save()
        original = backfill._Replay._tool

        def fragile(replay, message, *args, **kwargs):
            if message["content"] == "explode":
                raise MemoryError("a malformed row")
            return original(replay, message, *args, **kwargs)

        with patch.object(backfill._Replay, "_tool", fragile):
            result = self.run_backfill()
        self.assertEqual((result["status"], result["sessions"], result["skipped"], result["tool_calls"]), ("done", 1, 1, 1))
        with experience(self.home) as conn:
            self.assertEqual(conn.execute("SELECT SUM(calls) FROM tool_daily").fetchone()[0], 1)   # nothing of "bad"
            self.assertEqual(conn.execute("SELECT SUM(turns) FROM turn_daily").fetchone()[0], 1)

    def test_a_turn_cut_by_the_start_of_observation_is_left_to_the_hooks(self):
        self.history.session("open", start=NOW - 3600)
        self.history.message("open", "user", NOW - 30, content="run it")
        self.history.message("open", "assistant", NOW - 20, finish="tool_calls", tool_calls=[
            {"id": "c1", "function": {"name": "terminal", "arguments": "{}"}}])
        self.history.message("open", "tool", NOW + 5, content="{}", tool_name="terminal", call_id="c1")   # after install
        self.history.message("open", "assistant", NOW + 9, content="done", finish="stop")
        self.history.save()
        self.run_backfill()
        with experience(self.home) as conn:
            self.assertIsNone(conn.execute("SELECT SUM(turns) FROM turn_daily").fetchone()[0])

    def test_the_redo_takes_back_failure_totals_and_thaws_baselines_once(self):
        from hermes_atlas_evolution.experience import utc_day

        observed = datetime(2026, 11, 15, 0, tzinfo=timezone.utc).timestamp()
        before, live = utc_day(NOW - 2 * DAY), utc_day(NOW)
        self.history.save()
        with experience(self.home) as conn:
            conn.executemany("INSERT INTO failure VALUES (?, 'terminal', 'missing_dependency', 'rg', ?, ?, ?, 4, 'tag')",
                             [("mixed", NOW - 2 * DAY, NOW, 5), ("history_only", NOW - 2 * DAY, NOW - 2 * DAY, 2)])
            conn.executemany("INSERT INTO failure_daily VALUES (?, ?, ?)",
                             [(before, "mixed", 3), (live, "mixed", 2), (before, "history_only", 2)])
            conn.execute("INSERT INTO failure_sample VALUES ('history_only', 'excerpt', ?)", (NOW - 2 * DAY,))
            conn.execute("""INSERT INTO lesson(signature, tool, klass, hint, origin, status, created, updated, activated,
                baseline_failures, baseline_calls) VALUES ('mixed', 'terminal', 'missing_dependency', 'rg', 'derived',
                'active', ?, ?, ?, 9, 9)""", (NOW - DAY, NOW - DAY, NOW - DAY))
            for key, value in (("backfill_done", "done"), ("backfill_cursor", "1.0|x")):
                conn.execute("INSERT OR REPLACE INTO cursor VALUES (?, ?)", (key, value))
        self.assertEqual(self.run_backfill(observed=observed)["status"], "done")
        with experience(self.home) as conn:
            failures = {row[0]: tuple(row[1:]) for row in conn.execute("SELECT signature, total, sessions FROM failure")}
            self.assertEqual(failures, {"mixed": (2, 1)})
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM failure_sample").fetchone()[0], 0)
            self.assertEqual(tuple(conn.execute("SELECT baseline_failures, baseline_calls FROM lesson").fetchone()), (None, None))
            conn.execute("UPDATE failure SET total=7 WHERE signature='mixed'")
        self.assertEqual(self.run_backfill(observed=observed)["status"], "done")   # done once: nothing taken back again
        with experience(self.home) as conn:
            self.assertEqual(conn.execute("SELECT total FROM failure").fetchone()[0], 7)

    def test_a_backfill_counted_by_an_older_version_is_redone_without_touching_live_data(self):
        from hermes_atlas_evolution.experience import utc_day

        self.history.session("s", start=NOW - 2 * DAY)
        self.history.tool_turn("s", NOW - 2 * DAY + 10, result=MISSING)
        self.history.compact("s")
        self.history.save()
        observed = datetime(2026, 11, 15, 0, tzinfo=timezone.utc).timestamp()   # an upgrade: the start of a day
        with experience(self.home) as conn:   # what 0.5.0 left: duplicates counted, backfill done
            conn.execute("INSERT INTO tool_daily VALUES (?, 'terminal', 2, 2, 0, 0)", (utc_day(NOW - 2 * DAY),))
            conn.execute("INSERT INTO tool_daily VALUES (?, 'terminal', 7, 0, 0, 0)", (utc_day(NOW),))   # live
            for key, value in (("backfill_done", "done"), ("backfill_totals", '{"sessions": 1, "turns": 2, "tool_calls": 2, "failures": 2}')):
                conn.execute("INSERT OR REPLACE INTO cursor VALUES (?, ?)", (key, value))
        result = self.run_backfill(observed=observed)
        self.assertEqual((result["status"], result["tool_calls"]), ("done", 1))
        with experience(self.home) as conn:
            days = dict(conn.execute("SELECT day, calls FROM tool_daily").fetchall())
        self.assertEqual(days, {utc_day(NOW - 2 * DAY): 1, utc_day(NOW): 7})

    def test_a_finished_turn_of_a_session_that_continues_after_install_is_kept(self):
        self.history.session("open", start=NOW - 3600)
        self.history.tool_turn("open", NOW - 3000)                 # finished before the plugin started observing
        self.history.tool_turn("open", NOW + 60)                   # the hooks saw this one
        self.history.save()
        self.assertEqual(self.run_backfill()["turns"], 1)
        with experience(self.home) as conn:
            self.assertEqual(conn.execute("SELECT SUM(turns) FROM turn_daily").fetchone()[0], 1)

    def test_stored_rows_never_contain_text_from_the_history(self):
        self.history.session("s", start=NOW - DAY)
        self.history.tool_turn("s", NOW - DAY + 10, user=f"my key is {SECRET}", result=MISSING)
        self.history.save()
        self.run_backfill()
        stored = sqlite3.connect(self.home / "evolution" / "evolution.sqlite3")
        self.addCleanup(stored.close)
        dump = "\n".join(stored.iterdump())
        self.assertNotIn("SECRET", dump)
        self.assertNotIn("my key", dump)

    def test_missing_or_foreign_history_is_reported_not_raised(self):
        self.history.save()
        root = self.home
        for name, prepare, expected in (
                ("missing", lambda path: None, "no_history"),
                ("garbage", lambda path: path.write_bytes(b"not a database"), "unreadable"),
                ("foreign", lambda path: sqlite3.connect(path).execute("CREATE TABLE unrelated (x)").connection.close(),
                 "unsupported_history")):
            with self.subTest(name):
                self.home = Path(tempfile.mkdtemp(dir=root))
                prepare(self.home / "state.db")
                self.assertEqual(self.run_backfill()["status"], expected)


class ObservedSinceTest(Base):
    def test_the_first_load_marks_when_observation_started_and_keeps_the_earliest_live_data(self):
        self.history.save()
        backfill.mark_observed(self.home, now=NOW)
        backfill.mark_observed(self.home, now=NOW + 99)                 # later loads keep the first mark
        close_observers()
        with experience(self.home) as conn:
            self.assertEqual(float(conn.execute("SELECT value FROM cursor WHERE key='observed_since'").fetchone()[0]), NOW)
        other = Path(tempfile.mkdtemp(dir=self.home))
        with experience(other) as conn:   # a 0.4.0 database that already holds live data from earlier that day
            conn.execute("INSERT INTO tool_daily VALUES (?, 'terminal', 1, 0, 0, 0)", (utc_day(NOW - DAY),))
        backfill.mark_observed(other, now=NOW)
        close_observers()
        with experience(other) as conn:
            mark = float(conn.execute("SELECT value FROM cursor WHERE key='observed_since'").fetchone()[0])
        self.assertLessEqual(mark, NOW - DAY)


if __name__ == "__main__":
    unittest.main()
