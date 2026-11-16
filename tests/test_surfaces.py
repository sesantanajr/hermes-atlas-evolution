"""Where a turn comes from (the owner's own surfaces, automations, subagents, chats) and who it serves."""

import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from hermes_atlas_evolution import backfill
from hermes_atlas_evolution.experience import experience
from hermes_atlas_evolution.operations import close_observers
from hermes_atlas_evolution.signals import (FeedbackSensor, TurnTracker, owner_turn, record_tool, surface_of,
                                            turn_summary, turn_surface)

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp()
DAY = 86_400
FAILED = json.dumps({"output": "Traceback (most recent call last):\n  boom\nValueError: x", "exit_code": 1})


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addCleanup(close_observers)
        self.home = Path(folder.name)

    def hermes_source(self, value):
        """Hermes' own answer for the running session's origin (agent.session_source.session_source_for)."""
        module = types.ModuleType("agent.session_source")
        module.session_source_for = lambda platform: value or platform or "cli"
        patcher = patch.dict(sys.modules, {"agent.session_source": module})
        patcher.start()
        self.addCleanup(patcher.stop)


class SurfaceTest(Base):
    def test_the_real_origin_decides_and_only_a_delegated_child_is_a_subagent(self):
        cases = {("cli", None): "local", ("desktop", None): "local", ("oneshot", None): "local",
                 ("cli", "kanban"): "cron", ("curator", None): "cron", ("cron", None): "cron",
                 ("cli", "tool"): "gateway", ("a2a", None): "gateway", ("whatsapp", None): "gateway",
                 ("subagent", None): "subagent", (None, None): "gateway"}
        self.assertEqual({key: surface_of(*key) for key in cases}, cases)
        self.assertEqual(turn_surface({"platform": "cli", "parent_session_id": "p"}), "local")   # after /branch or /new
        self.assertEqual(turn_surface({"platform": "subagent", "parent_session_id": "p"}), "subagent")
        self.assertEqual(turn_surface({"platform": "", "parent_session_id": "p"}), "subagent")
        self.hermes_source("kanban")   # a kanban worker runs with platform "cli"
        self.assertEqual(turn_surface({"platform": "cli"}), "cron")

    def test_the_owner_is_served_on_their_surfaces_and_by_listed_senders(self):
        owners = frozenset({"555"})
        self.assertEqual([owner_turn(surface, None, owners) for surface in ("local", "cron", "subagent", "gateway")],
                         [True, True, True, False])
        self.assertTrue(owner_turn("gateway", 555, owners))   # a numeric Telegram id
        self.assertFalse(owner_turn("gateway", "stranger", owners))

    def test_a_kanban_worker_is_measured_as_an_automation(self):
        self.hermes_source("kanban")
        tracker = TurnTracker(self.home)
        tracker.start({"turn_id": "k", "session_id": "k", "platform": "cli"}, clock=0.0)
        tracker.finish({"turn_id": "k", "completed": True}, clock=1.0, now=NOW)
        close_observers()
        with experience(self.home) as conn:
            self.assertEqual(turn_summary(conn, now=NOW, days=1, surfaces=("cron",))["turns"], 1)
            self.assertEqual(turn_summary(conn, now=NOW, days=1, surfaces=("local", "gateway"))["turns"], 0)


class FeedbackTest(Base):
    def observe(self, sensor, message, *, clock, session="a", **extra):
        return sensor.observe({"session_id": session, "platform": "cli", "user_message": message, **extra},
                              clock=clock, now=NOW)

    def test_only_messages_a_person_wrote_after_an_earlier_turn_are_feedback(self):
        sensor = FeedbackSensor(self.home)
        self.assertEqual(self.observe(sensor, "Corrija o teste de login que está falhando", clock=0.0, is_first_turn=True), [])
        goal = "[Continuing toward your standing goal]\nGoal: ship the release\n\nContinue working toward this goal."
        self.assertEqual(self.observe(sensor, goal, clock=10.0, session="g"), [])
        self.assertEqual(self.observe(sensor, goal, clock=20.0, session="g"), [])
        tick = "check the build status of the main branch now"
        self.assertEqual(self.observe(sensor, tick, clock=30.0, session="loop"), [])
        self.assertEqual(self.observe(sensor, tick, clock=90.0, session="loop"), [])        # a /loop tick: the same text
        self.assertEqual(self.observe(sensor, tick + " please", clock=120.0, session="loop"), ["reask"])
        self.assertEqual(self.observe(sensor, "não, errado", clock=200.0, session="branch", parent_session_id="p"),
                         ["correction"])   # a session continued after /branch is still the owner's
        self.hermes_source("kanban")
        self.assertEqual(self.observe(sensor, "não, errado", clock=300.0, session="worker"), [])


class OwnerFailureTest(Base):
    def test_only_the_owners_failures_count_toward_activation_and_keep_an_excerpt(self):
        payload = {"tool_name": "terminal", "status": "error", "result": FAILED, "session_id": "s"}
        record_tool(self.home, payload, now=NOW, sample=True, owner=False)
        close_observers()
        with experience(self.home) as conn:
            self.assertEqual(conn.execute("SELECT total FROM failure").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM failure_owner_daily").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM failure_sample").fetchone()[0], 0)
        record_tool(self.home, payload, now=NOW, sample=True, owner=True)
        close_observers()
        with experience(self.home) as conn:
            self.assertEqual(conn.execute("SELECT SUM(count) FROM failure_owner_daily").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM failure_sample").fetchone()[0], 1)


class HistoryTest(Base):
    def setUp(self):
        super().setUp()
        import sqlite3

        self.db = sqlite3.connect(self.home / "state.db")
        self.db.executescript("""
            CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, user_id TEXT, parent_session_id TEXT,
                started_at REAL, ended_at REAL, end_reason TEXT);
            CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,
                tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL, finish_reason TEXT);""")
        self.calls = 0

    def session(self, sid, source, *, start, parent=None, user="u1", messages=("please run the job",)):
        self.db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, 'done')", (sid, source, user, parent, start, start + 600))
        clock = start
        for text in messages:
            self.calls += 1
            call = f"c{self.calls}"
            self.db.executemany("""INSERT INTO messages(session_id, role, content, tool_call_id, tool_calls, tool_name,
                timestamp, finish_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", [
                (sid, "user", text, None, None, None, clock + 1, None),
                (sid, "assistant", "", None, json.dumps([{"id": call, "function": {"name": "terminal", "arguments": "{}"}}]),
                 None, clock + 2, "tool_calls"),
                (sid, "tool", FAILED, call, None, "terminal", clock + 3, None),
                (sid, "assistant", "done", None, None, None, clock + 4, "stop")])
            clock += 60

    def replay(self, **options):
        self.db.commit()
        with experience(self.home) as conn:
            conn.execute("INSERT OR IGNORE INTO cursor VALUES ('observed_since', ?)", (repr(NOW),))
        result = backfill.step(self.home, now=NOW, budget_s=10, **options)
        close_observers()
        return result

    def test_the_history_follows_the_same_rules_as_the_hooks(self):
        self.session("own", "cli", start=NOW - DAY, messages=("Corrija o build que quebrou ontem", "não, errado"))
        self.session("branch", "cli", start=NOW - DAY + 1000, parent="own")
        self.session("worker", "kanban", start=NOW - DAY + 2000)
        self.session("integration", "tool", start=NOW - DAY + 3000, user="someone-else")
        self.session("child", "subagent", start=NOW - DAY + 4000, parent="own")
        self.session("group", "whatsapp", start=NOW - DAY + 5000, user="stranger")
        self.session("dm", "telegram", start=NOW - DAY + 6000, user="555")
        self.assertEqual(self.replay(owner_sender_ids=("555",))["status"], "done")
        with experience(self.home) as conn:
            surfaces = dict(conn.execute("SELECT surface, SUM(turns) FROM turn_daily GROUP BY surface").fetchall())
            feedback = dict(conn.execute("SELECT kind, count FROM feedback_daily").fetchall())
            owned = conn.execute("SELECT SUM(count) FROM failure_owner_daily").fetchone()[0]
            total = conn.execute("SELECT total FROM failure").fetchone()[0]
        self.assertEqual(surfaces, {"local": 3, "cron": 1, "gateway": 3, "subagent": 1})
        self.assertEqual(feedback, {"correction": 1})          # the session's first message is not a correction
        self.assertEqual((total, owned), (8, 6))               # the integration and the stranger are not the owner


if __name__ == "__main__":
    unittest.main()
