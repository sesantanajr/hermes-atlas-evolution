"""0.8.1: the owner decides from the dashboard panel or the terminal, approves all at once, and undoes."""

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_atlas_evolution import decisions, lessons, proposals
from hermes_atlas_evolution.cli import AGENT_MARKERS, execute
from hermes_atlas_evolution.experience import experience
from hermes_atlas_evolution.operations import close_observers
from hermes_atlas_evolution.status import owner_suggestions

ROOT = Path(__file__).resolve().parent.parent
NOW = time.time()
WRITTEN = "When the CSV import fails on encoding, convert the file to UTF-8 with iconv first."


def load_api():
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_decisions", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.addCleanup(close_observers)
        self.home = Path(folder.name)
        environment = patch.dict(os.environ, {name: "" for name in AGENT_MARKERS})
        environment.start()
        self.addCleanup(environment.stop)
        tree = patch.multiple("hermes_atlas_evolution.cli", _under_hermes=lambda **_: False,
                              _in_hermes_cgroup=lambda **_: False)
        tree.start()
        self.addCleanup(tree.stop)

    def lessons_waiting(self):
        """A derived candidate, a reflector draft and an agent proposal, all waiting."""
        with experience(self.home) as conn:
            conn.execute("""INSERT INTO lesson(signature, tool, klass, hint, origin, status, created, updated)
                VALUES ('sig1', 'files_search', 'missing_argument', 'query', 'derived', 'candidate', 1, 1)""")
            draft = lessons.add_reflected(conn, signature="sig2", tool="terminal", klass="command_failed", hint="exit_1",
                                          text=WRITTEN, now=1)
            agent = lessons.add_custom(conn, "When a deploy fails, read the release notes of the service first.",
                                       tool="terminal", origin="agent", now=1)
        return 1, draft, agent

    def run_cli(self, **arguments):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(io.StringIO()):
            execute(SimpleNamespace(**{"lang": "en", **arguments}), self.home, runtime_config={})
        return json.loads(stream.getvalue())


class DecisionsTest(Base):
    def test_approve_all_takes_only_the_sentences_shown_and_never_the_agents(self):
        derived, draft, agent = self.lessons_waiting()
        with experience(self.home) as conn:
            shown = {item["id"]: item["fingerprint"] for item in decisions.waiting(conn)}
            conn.execute("UPDATE lesson SET text='When it fails, convert the file with iconv to UTF-8 first.' WHERE id=?",
                         (draft,))   # a pass changed the draft after it was shown
            result = decisions.approve_all(conn, shown, now=NOW, actor="dashboard")
            statuses = dict(conn.execute("SELECT id, status FROM lesson").fetchall())
        self.assertEqual(result["approved"], [derived])
        self.assertEqual(sorted((item["id"], item["reason"]) for item in result["skipped"]),
                         sorted([(draft, "changed"), (agent, "agent_needs_single")]))
        self.assertEqual(statuses, {derived: "active", draft: "quarantined", agent: "quarantined"})

    def test_every_decision_can_be_undone_while_nothing_changed_it_since(self):
        derived, draft, _agent = self.lessons_waiting()
        with experience(self.home) as conn:
            proposals.refresh(conn, now=NOW)
            approved = decisions.decide_lesson(conn, derived, "approve", now=NOW, actor="dashboard")
            rejected = decisions.decide_lesson(conn, draft, "reject", now=NOW, actor="terminal")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM change").fetchone()[0], 1)
            self.assertEqual(decisions.undo(conn, approved["decision"], now=NOW + 1)["status"], "candidate")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM change").fetchone()[0], 0)   # the measurement goes too
            self.assertEqual(conn.execute("SELECT status FROM proposal WHERE code=?", (f"lesson:{derived}",)).fetchone()[0],
                             "open")
            self.assertEqual(decisions.undo(conn, approved["decision"], now=NOW + 2)["reason"], "already_undone")
            decisions.decide_lesson(conn, draft, "reactivate", now=NOW + 3)   # the owner changed their mind since
            self.assertEqual(decisions.undo(conn, rejected["decision"], now=NOW + 4)["reason"], "changed_since")
            history = decisions.history(conn)
        self.assertEqual([item["action"] for item in history], ["reactivate", "reject", "approve"])
        self.assertEqual(history[-1]["actor"], "dashboard")
        self.assertIsNotNone(history[-1]["undone"])
        self.assertNotIn(WRITTEN, json.dumps([{k: v for k, v in item.items() if k != "lesson"} for item in history]))

    def test_a_decision_on_a_lesson_that_reads_differently_now_changes_nothing(self):
        derived, _draft, _agent = self.lessons_waiting()
        with experience(self.home) as conn:
            result = decisions.decide_lesson(conn, derived, "approve", now=NOW, shown="0" * 16)
            self.assertEqual((result["decided"], result["reason"]), (False, "changed"))
            self.assertEqual(conn.execute("SELECT status FROM lesson WHERE id=?", (derived,)).fetchone()[0], "candidate")

    def test_terminal_approve_all_and_undo(self):
        derived, draft, agent = self.lessons_waiting()
        result = self.run_cli(evolution_action="lessons", lessons_action="approve", id=None, all=True, yes=True)
        self.assertEqual(sorted(result["approved"]), sorted([derived, draft]))
        self.assertEqual(result["left_for_one_by_one"], [agent])
        history = self.run_cli(evolution_action="decisions", decisions_action="list", limit=10)
        self.assertEqual(len(history), 2)
        undone = self.run_cli(evolution_action="decisions", decisions_action="undo", id=history[0]["id"])
        self.assertTrue(undone["undone"])
        with patch.dict(os.environ, {"HERMES_SESSION_ID": "chat"}), self.assertRaises(SystemExit):
            self.run_cli(evolution_action="decisions", decisions_action="undo", id=history[1]["id"])
        with patch.dict(os.environ, {"HERMES_SESSION_ID": "chat"}), self.assertRaises(SystemExit):
            self.run_cli(evolution_action="lessons", lessons_action="approve", id=None, all=True, yes=True)

    def test_owner_suggestions_only_name_what_means_the_owner_by_construction(self):
        channels = [{"origin": "api_server", "sessions": 31, "senders": [{"id": "api", "sessions": 31, "owner": False}]},
                    {"origin": "bot_room", "sessions": 3, "senders": [{"id": "hosted-owner:abc", "sessions": 3, "owner": False}]},
                    {"origin": "whatsapp", "sessions": 5, "senders": [{"id": "5511999999999@s.whatsapp.net", "sessions": 2,
                                                                      "owner": False}]}]
        found = owner_suggestions(channels)
        self.assertEqual([(item["kind"], item["value"], item["reason"]) for item in found],
                         [("platform", "api_server", "own_interface"), ("sender", "hosted-owner:abc", "hosted_owner")])
        self.assertEqual(owner_suggestions(channels, owners=["hosted-owner:abc"], platforms=["api_server"]), [])


class PanelRouteTest(Base):
    def setUp(self):
        super().setUp()
        self.api = load_api()
        self.addCleanup(lambda: self.api._module("operations").close_observers())

    def test_only_a_logged_in_owner_decides(self):
        login = {"cookie": "theme=dark; hermes_session_at=abc"}
        self.assertEqual(self.api.decision_policy({"x-hermes-session-token": "t"}, {}), (False, "open_dashboard"))
        self.assertEqual(self.api.decision_policy({}, {"token": "t"}), (False, "open_dashboard"))
        # 0.8.1 review: Hermes also takes the open dashboard's token as `Authorization: Bearer`
        self.assertEqual(self.api.decision_policy({**login, "authorization": "Bearer t"}, {}), (False, "open_dashboard"))
        self.assertEqual(self.api.decision_policy({}, {}), (False, "open_dashboard"))   # no login cookie
        self.assertEqual(self.api.decision_policy(login, {}), (True, "login"))
        self.assertEqual(self.api.decision_policy({"cookie": "hermes_session_at_gw1=abc"}, {}), (True, "login"))
        self.assertEqual(self.api.decision_policy({"cookie": "hermes_session_at="}, {}), (False, "open_dashboard"))
        self.assertEqual(self.api.decision_policy(login, {}, app_state=SimpleNamespace(auth_required=False)),
                         (False, "open_dashboard"))   # the dashboard process itself says there is no login
        self.assertEqual(self.api.decision_policy({}, {}, state=SimpleNamespace(native_http_principal="desktop")),
                         (True, "desktop"))

    def test_the_panel_decides_writes_settings_and_undoes_them(self):
        derived, draft, _agent = self.lessons_waiting()
        close_observers()
        written = []
        runtime = {"plugins": {"entries": {"hermes-atlas-evolution": {"settings": {}}}}}   # Hermes' config.yaml

        def writer(values):   # Hermes' settings writer: the values land in config.yaml
            written.append(dict(values))
            runtime["plugins"]["entries"]["hermes-atlas-evolution"]["settings"].update(values)
            return list(values)

        snap = self.api.snapshot(self.home, now=NOW, runtime_config=runtime, lang="en", can_decide=True, decide_reason="login")
        self.assertEqual(len(snap["pending"]), 3)
        result = self.api.decide(self.home, runtime, {"kind": "all", "items": {str(item["id"]): item["fingerprint"]
                                                                                for item in snap["pending"]}}, now=NOW)
        self.assertEqual(sorted(result["approved"]), sorted([derived, draft]))
        changed = self.api.decide(self.home, runtime, {"kind": "settings", "values": {"owner_platforms": ["api_server"],
                                                                                      "lessons": "auto"}},
                                  now=NOW, writer=writer)
        self.assertEqual(changed["written"], ["owner_platforms"])   # lessons is already auto: nothing to write
        self.assertEqual(written, [{"owner_platforms": ["api_server"]}])
        snap = self.api.snapshot(self.home, now=NOW, runtime_config=runtime, lang="en", can_decide=True)
        self.assertEqual(snap["settings"]["values"]["owner_platforms"], ["api_server"])
        setting = next(item for item in snap["history"] if item["kind"] == "setting")
        undone = self.api.decide(self.home, runtime, {"kind": "undo", "id": setting["id"]}, now=NOW, writer=writer)
        self.assertTrue(undone["undone"])
        self.assertEqual(written[-1], {"owner_platforms": []})
        with self.assertRaises(ValueError):
            self.api.decide(self.home, runtime, {"kind": "settings", "values": {"runtime_guard_off": True}}, now=NOW,
                            writer=writer)
        for bad in ({"reflector_daily_limit": 999}, {"autopilot_minutes": 5}, {"lessons": "always"}, {"reflector": "yes"},
                    {"owner_platforms": ["Telegram Bot!"]}, {"owner_sender_ids": ["x"] * 21}, {"language": "fr"},
                    {"slow_turn_seconds": 1.5}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.api.decide(self.home, runtime, {"kind": "settings", "values": bad}, now=NOW, writer=writer)
        self.assertEqual(len(written), 2)   # nothing invalid reached Hermes' config

    def test_a_setting_is_undone_only_while_it_is_still_what_the_decision_set(self):
        runtime = {"plugins": {"entries": {"hermes-atlas-evolution": {"settings": {}}}}}

        def writer(values):
            runtime["plugins"]["entries"]["hermes-atlas-evolution"]["settings"].update(values)
            return list(values)

        first = self.api.decide(self.home, runtime, {"kind": "settings", "values": {"owner_platforms": ["telegram",
                                                                                                      "api_server"]}},
                                now=NOW, writer=writer)
        self.api.decide(self.home, runtime, {"kind": "settings", "values": {"owner_platforms": ["api_server"]}}, now=NOW,
                        writer=writer)
        self.assertEqual(first["written"], ["owner_platforms"])
        snap = self.api.snapshot(self.home, now=NOW, runtime_config=runtime, can_decide=True)
        older = [item for item in snap["history"] if item["kind"] == "setting"][-1]
        result = self.api.decide(self.home, runtime, {"kind": "undo", "id": older["id"]}, now=NOW, writer=writer)
        self.assertEqual((result["undone"], result["reason"]), (False, "changed_since"))   # a later decision stands
        self.assertEqual(runtime["plugins"]["entries"]["hermes-atlas-evolution"]["settings"]["owner_platforms"],
                         ["api_server"])

        def refusing(values):
            raise ValueError("Hermes refused the setting")

        newer = [item for item in snap["history"] if item["kind"] == "setting"][0]
        with self.assertRaises(ValueError):
            self.api.decide(self.home, runtime, {"kind": "undo", "id": newer["id"]}, now=NOW, writer=refusing)
        with experience(self.home) as conn:   # not marked: the value never went back
            self.assertIsNone(conn.execute("SELECT undone FROM decision_log WHERE id=?", (newer["id"],)).fetchone()[0])

    def test_a_broken_settings_json_never_takes_the_panel_down(self):
        (self.home / "evolution").mkdir(exist_ok=True)
        for body in ('{"evolution": {"lessons": "auto"', "[1, 2]"):
            (self.home / "evolution" / "settings.json").write_text(body)
            snap = self.api.snapshot(self.home, now=NOW, runtime_config={}, can_decide=True)
            self.assertIn("settings.json", snap["settings"]["problems"])

    def test_who_cannot_decide_never_reads_the_owners_chat_accounts(self):
        runtime = {"plugins": {"entries": {"hermes-atlas-evolution": {"settings": {}}}}}
        self.api.decide(self.home, runtime, {"kind": "settings", "values": {"owner_sender_ids": ["5511999990000"]}},
                        now=NOW, writer=lambda values: list(values))
        _derived, draft, _agent = self.lessons_waiting()
        with experience(self.home) as conn:
            decisions.decide_lesson(conn, draft, "approve", now=NOW)
        close_observers()
        hidden = self.api.snapshot(self.home, now=NOW, runtime_config=runtime, can_decide=False)["history"]
        self.assertNotIn("5511999990000", json.dumps(hidden))
        self.assertNotIn(WRITTEN, json.dumps(hidden))
        with patch.dict(os.environ, {"HERMES_SESSION_ID": "chat"}):
            listed = Base.run_cli(self, evolution_action="decisions", decisions_action="list", limit=10)
        self.assertNotIn("5511999990000", json.dumps(listed))
        self.assertIn("5511999990000", json.dumps(Base.run_cli(self, evolution_action="decisions", decisions_action="list",
                                                              limit=10)))

    def test_without_a_login_the_snapshot_hides_sender_ids(self):
        snap = self.api.snapshot(self.home, now=NOW, runtime_config={}, lang="en", can_decide=False)
        self.assertFalse(snap["can_decide"])
        self.assertTrue(all(sender["id"] is None for channel in snap["owner"]["channels"] for sender in channel["senders"]))

    def test_the_route_refuses_an_open_dashboard_and_a_request_without_the_header(self):
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("FastAPI is installed with Hermes' dashboard only")
        app = FastAPI()
        app.include_router(self.api.router, prefix="/p")
        self.api._current = lambda target=None: (self.home, {})
        client = TestClient(app)
        body = {"kind": "undo", "id": 1}
        login = {"Cookie": "hermes_session_at=abc"}
        self.assertEqual(client.post("/p/decide", json=body, headers={"X-Hermes-Session-Token": "t",
                                                                      "X-Atlas-Evolution": "1"}).status_code, 403)
        self.assertEqual(client.post("/p/decide", json=body, headers={**login, "Authorization": "Bearer t",
                                                                      "X-Atlas-Evolution": "1"}).status_code, 403)
        self.assertEqual(client.post("/p/decide", json=body, headers={"X-Atlas-Evolution": "1"}).status_code, 403)
        self.assertEqual(client.post("/p/decide", json=body, headers=login).status_code, 403)   # no custom header
        self.assertEqual(client.post("/p/decide", json=body, headers={**login, "X-Atlas-Evolution": "1"}).status_code, 400)
        self.assertFalse(client.get("/p/snapshot", headers={"Authorization": "Bearer t"}).json()["can_decide"])
        self.assertFalse(client.get("/p/snapshot").json()["can_decide"])
        self.assertTrue(client.get("/p/snapshot", headers=login).json()["can_decide"])


class CliWriterTest(Base):
    """The panel writes settings with Hermes' public `hermes config set`, profile by profile."""

    def test_each_value_is_one_config_set_of_the_profile(self):
        from hermes_atlas_evolution import admin

        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs["env"]["HERMES_HOME"]))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        profile = self.home / "profiles" / "atlas-ai"
        profile.mkdir(parents=True)
        with patch.object(admin, "hermes_executable", return_value="/usr/local/bin/hermes"):
            written = admin.cli_writer(profile, runner=runner)({"owner_platforms": ["api_server"], "lessons": "auto",
                                                                "reflector": False, "reflector_daily_limit": 6})
        self.assertEqual(written, ["owner_platforms", "lessons", "reflector", "reflector_daily_limit"])
        self.assertEqual(calls[0][0], ["/usr/local/bin/hermes", "--profile", "atlas-ai", "config", "set",
                                       "plugins.entries.hermes-atlas-evolution.settings.owner_platforms", '["api_server"]'])
        self.assertEqual([command[-1] for command, _ in calls], ['["api_server"]', "auto", "false", "6"])
        self.assertEqual(calls[0][1], str(profile))   # spelled as Hermes reads it: <root>/profiles/<name>, links not followed

    def test_hermes_refusing_a_value_stops_and_says_why(self):
        from hermes_atlas_evolution import admin

        refuse = lambda command, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="✗ Invalid config key")  # noqa: E731
        with patch.object(admin, "hermes_executable", return_value="/usr/local/bin/hermes"), \
                self.assertRaises(ValueError) as error:
            admin.cli_writer(self.home, runner=refuse)({"lessons": "auto"})
        self.assertIn("Invalid config key", str(error.exception))
        with patch.object(admin, "hermes_executable", return_value=None), self.assertRaises(ValueError):
            admin.cli_writer(self.home)({"lessons": "auto"})


class StatusReloadTest(Base):
    def test_status_lists_settings_that_wait_for_the_next_load(self):
        from hermes_atlas_evolution import status

        with experience(self.home) as conn:
            conn.execute("INSERT OR REPLACE INTO cursor VALUES ('settings_reload', ?)", (json.dumps(["runtime_guard"]),))
        report = status.build(self.home, now=NOW, config={}, runtime_config={})
        self.assertEqual(report["settings_reload"], ["runtime_guard"])
        self.assertIn("settings_reload", report["attention"])
        self.assertIn("`runtime_guard`", status.markdown(report, "pt"))


if __name__ == "__main__":
    unittest.main()
