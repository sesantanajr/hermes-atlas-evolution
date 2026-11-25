"""0.12.0: the learning core after the audit (findings A1-A12, C1, C10, C16) and its small evolutions (I1, I5, I6)."""

import json
import os
import random
import sys
import threading
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from hermes_atlas_evolution import evolution_index, explain, lessons, proposals
from hermes_atlas_evolution.autopilot import Autopilot, runs_passes
from hermes_atlas_evolution.evolve import CONFIG_UNREAD, cycle, options
from hermes_atlas_evolution.experience import experience, utc_day
from hermes_atlas_evolution.operations import close_observers
from hermes_atlas_evolution.signals import classify

from test_plugin_loop import FakeContext, PluginBase

os.environ.setdefault("ATLAS_EVOLUTION_NO_HERMES", "1")   # never the real Hermes of this machine

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp()
DAY = 86_400
REAL_POKE = Autopilot.poke   # PluginBase quiets it in every test


def terminal(output, exit_code=1):
    return json.dumps({"output": output, "exit_code": exit_code})


class ClassifyTest(unittest.TestCase):
    """A1: a number, a frame or a line of progress in the output never names the class."""

    def klass(self, result=None, error=None):
        found = classify("terminal", "error", error, result, {"command": "x"})
        return found["klass"], found["hint"]

    def test_the_auditors_cases_are_no_longer_misread(self):
        self.assertEqual(self.klass(terminal("FAILED tests/test_x.py::test_a - AssertionError\n"
                                             "===== 1 failed, 429 passed in 12.31s =====")), ("command_failed", "exit_1"))
        self.assertEqual(self.klass(terminal('Traceback (most recent call last):\n  File "/app/run.py", line 403, in main\n'
                                             "    load()\nKeyError: 'column'")), ("code_exception", "KeyError"))
        self.assertEqual(self.klass(terminal('Traceback (most recent call last):\n  File "a.py", line 12, in <module>\n'
                                             "    r = fetch(url, timeout=30)\nValueError: bad url")),
                         ("code_exception", "ValueError"))
        self.assertEqual(self.klass(terminal("build: target 'docs' not found, skipping\nerror: linker failed with exit 2", 2)),
                         ("command_failed", "exit_2"))

    def test_codes_and_causes_that_read_as_such_still_count(self):
        cases = [
            (terminal("> GET /v1/items\n< HTTP/1.1 429 Too Many Requests"), None, "rate_limited"),
            (None, "HTTP Error 403: Forbidden", "auth"),
            (None, "request failed with status code: 401", "auth"),
            (terminal("Traceback (most recent call last):\n  File \"x.py\", line 5, in <module>\n    get(u)\n"
                      "requests.exceptions.ReadTimeout: HTTPSConnectionPool(host='h', port=443): Read timed out."), None,
             "timeout"),
            (terminal("Traceback (most recent call last):\n  File \"x.py\", line 429, in <module>\n    open(p)\n"
                      "PermissionError: [Errno 13] Permission denied: 'x'"), None, "permission_denied"),
            (terminal("Traceback (most recent call last):\n  File \"x.py\", line 2, in <module>\n    import yaml\n"
                      "ModuleNotFoundError: No module named 'yaml'"), None, "missing_dependency"),
            (terminal("cat: notes.txt: No such file or directory"), None, "not_found"),
            (None, "Skill 'abc' not found.", "not_found"),   # Hermes' own error text keeps the plain words
            (terminal("[Command timed out after 60s]", 124), None, "timeout"),
            # 0.11.0 read these as such; they still do (an SDK's `Error code`, curl's `returned error`, Playwright's)
            (None, "Error code: 401 - {'error': {'message': 'Incorrect API key provided'}}", "auth"),
            (None, "Error code: 403 - {'type': 'error'}", "auth"),
            (None, "Error code: 429 - {'error': {'type': 'rate_limit_error'}}", "rate_limited"),
            (None, "HTTPError: 403", "auth"),
            (terminal("curl: (22) The requested URL returned error: 401\n", 22), None, "auth"),
            (None, "Timeout 30000ms exceeded.", "timeout"),
            (terminal("npm ERR! 404 Not Found - GET https://registry/x"), None, "not_found"),
            (terminal("fatal: repository 'https://x' not found", 128), None, "not_found"),   # on the last line
            (terminal("bash: foo: command not found\n" + "\n".join(f"line{index}" for index in range(7)), 127), None,
             "missing_dependency"),   # a phrase that only names the cause counts anywhere
        ]
        for output in ("query returned 401 rows", "search returned 429 results", "Tool returned 403 bytes of output"):
            with self.subTest(output=output):   # a count is not a status code
                self.assertNotIn(self.klass(terminal(output))[0], ("auth", "rate_limited"))
        for result, error, expected in cases:
            with self.subTest(expected=expected, error=error):
                self.assertEqual(self.klass(result, error)[0], expected)

    def test_command_output_is_read_in_its_last_lines(self):
        noisy = "warning: rate limit close\n" + "\n".join(f"step {index} ok" for index in range(8)) + "\nerror: build failed"
        self.assertEqual(self.klass(terminal(noisy, 2)), ("command_failed", "exit_2"))
        self.assertEqual(self.klass(terminal(noisy + "\nerror: 429 Too Many Requests", 2))[0], "rate_limited")


class Store(unittest.TestCase):
    def setUp(self):
        import tempfile

        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.addCleanup(close_observers)
        self.home = Path(folder.name)

    @staticmethod
    def lesson(conn, index, activated, *, shown=0, verdict=None, text=None, origin="derived"):
        return conn.execute("""INSERT INTO lesson(signature, tool, klass, hint, origin, status, text, created, updated,
              activated, shown, verdict) VALUES (?, ?, 'missing_argument', 'path', ?, 'active', ?, ?, ?, ?, ?, ?)""",
                            (f"sig{index:02d}", f"tool_{index:02d}", origin, text, activated, activated, activated, shown,
                             verdict)).lastrowid

    @staticmethod
    def days(conn, signature, tool, start, count, *, failures, calls=0):
        for offset in range(count):
            day = utc_day(start + offset * DAY)
            if failures:
                conn.execute("INSERT INTO failure_daily VALUES (?, ?, ?) ON CONFLICT DO UPDATE SET count=count+excluded.count",
                             (day, signature, failures))
            if calls:
                conn.execute("INSERT INTO tool_daily VALUES (?, ?, ?, 0, 0, 0) ON CONFLICT DO UPDATE SET calls=calls+excluded.calls",
                             (day, tool, calls))


class FirstReadingTest(Store):
    """A2: a lesson is measured from the first time the agent read it; one the prompt never showed is not judged."""

    def test_only_the_lessons_the_prompt_showed_are_judged(self):   # the auditor's r4
        activated = NOW - 20 * DAY
        with experience(self.home) as conn:
            for index in range(12):
                lesson_id = self.lesson(conn, index, activated)
                # a detail of its own each: lessons of one cause on several tools share a sentence (0.14.0), and
                # this test is about the prompt's limit of lines
                conn.execute("UPDATE lesson SET hint=? WHERE id=?", (f"path{index:02d}", lesson_id))
                conn.execute("INSERT INTO failure VALUES (?, ?, 'missing_argument', 'path', ?, ?, ?, 2, '')",
                             (f"sig{index:02d}", f"tool_{index:02d}", activated - 5 * DAY, NOW, 10 + index))
                self.days(conn, f"sig{index:02d}", f"tool_{index:02d}", activated - 7 * DAY, 7, failures=2, calls=10)
                self.days(conn, f"sig{index:02d}", f"tool_{index:02d}", activated + DAY, 19, failures=2 * (index % 2), calls=10)
            shown: list = []
            lessons.playbook(conn, chosen=shown)
            lessons.shown(conn, shown, now=activated)
            judged = lessons.evaluate(conn, now=NOW, mode="auto")
            never = conn.execute(f"""SELECT id, verdict, status FROM lesson WHERE id NOT IN ({",".join("?" * len(shown))})""",
                                 shown).fetchall()
        self.assertEqual(len(shown), 8)
        self.assertEqual(sorted(item["id"] for item in judged), sorted(shown))
        self.assertEqual({(row[1], row[2]) for row in never}, {(None, "active")})

    def test_a_lesson_first_read_days_later_is_measured_from_then(self):
        activated = NOW - 20 * DAY
        read = activated + 6 * DAY
        with experience(self.home) as conn:
            lesson_id = self.lesson(conn, 1, activated)
            # 3 failures in 10 calls a day before the activation and until the agent first read it, none after
            self.days(conn, "sig01", "tool_01", activated - 10 * DAY, 16, failures=3, calls=10)
            self.days(conn, "sig01", "tool_01", read + DAY, 13, failures=0, calls=10)
            self.assertEqual(lessons.evaluate(conn, now=NOW, mode="auto"), [])   # not read yet: not judged
            lessons.shown(conn, [lesson_id], now=read)
            [effect] = lessons.evaluate(conn, now=NOW, mode="auto")
            baseline = conn.execute("SELECT baseline_failures, baseline_calls FROM lesson WHERE id=?", (lesson_id,)).fetchone()
        # the baseline is the 14 days before the first reading, the 5 days after the activation included
        self.assertEqual(tuple(baseline), (14 * 3, 14 * 10))
        self.assertEqual((effect["verdict"], effect["days_active"]), ("effective", 14))

    def test_a_tool_hint_is_a_reading_and_a_new_activation_needs_a_new_one(self):
        activated = NOW - 10 * DAY
        with experience(self.home) as conn:
            lesson_id = self.lesson(conn, 1, activated)
            lessons.count_hint(conn, lesson_id, now=activated + 3600)
            first = conn.execute("SELECT activated, first_shown FROM lesson_measure WHERE lesson_id=?", (lesson_id,)).fetchone()
            lessons.set_status(conn, lesson_id, "retired", now=NOW - DAY)
            lessons.set_status(conn, lesson_id, "active", now=NOW - DAY + 60)
            self.assertEqual([item["id"] for item in lessons.evaluate(conn, now=NOW, mode="auto")], [])
            lessons.shown(conn, [lesson_id], now=NOW - DAY + 120)
            again = conn.execute("SELECT activated, first_shown FROM lesson_measure WHERE lesson_id=?", (lesson_id,)).fetchone()
        self.assertEqual(tuple(first), (activated, activated + 3600))
        self.assertEqual(tuple(again), (NOW - DAY + 60, NOW - DAY + 120))

    def test_lessons_active_before_the_upgrade_keep_their_measure_only_if_they_were_ever_shown(self):
        activated = NOW - 20 * DAY
        with experience(self.home) as conn:
            seen = self.lesson(conn, 1, activated, shown=3, verdict="ineffective")
            unseen = self.lesson(conn, 2, activated, shown=0, verdict="effective")
            for signature, tool in (("sig01", "tool_01"), ("sig02", "tool_02")):
                self.days(conn, signature, tool, activated - 7 * DAY, 27, failures=2, calls=10)
            judged = {item["id"]: item["verdict"] for item in lessons.evaluate(conn, now=NOW, mode="suggest")}
            verdict = conn.execute("SELECT verdict FROM lesson WHERE id=?", (unseen,)).fetchone()[0]
            lessons.evaluate(conn, now=NOW, mode="suggest")   # the migration runs once
        self.assertEqual(judged, {seen: "ineffective"})
        self.assertIsNone(verdict)   # an "effective" it never earned no longer puts it first in the prompt


class ObservedDaysTest(Store):
    """A8: a per-day baseline divides by the days the failure existed in, not by 14."""

    def test_a_failure_two_days_old_has_two_days_of_baseline(self):
        activated = NOW - 10 * DAY
        with experience(self.home) as conn:
            self.lesson(conn, 1, activated)
            lessons.shown(conn, [1], now=activated)
            self.days(conn, "sig01", "tool_01", activated - 2 * DAY, 2, failures=2)   # 2 a day, from 2 days before
            self.days(conn, "sig01", "tool_01", activated + DAY, 10, failures=1)      # halved since
            [effect] = lessons.evaluate(conn, now=NOW, mode="auto")
        self.assertEqual((effect["basis"], effect["before"], effect["after"], effect["verdict"]),
                         ("failures_per_day", 2.0, 1.0, "effective"))   # 4/14 would have called it ineffective


class RetireWordingTest(Store):
    """A9: the retire proposal says what its rates count."""

    def test_the_rates_are_named_by_their_basis_in_both_languages(self):
        for basis, en, pt in (("failures_per_call", "Failures per call", "Falhas por chamada"),
                              ("failures_per_day", "Failures per day", "Falhas por dia"),
                              (None, "Failure rate", "Taxa de falhas")):
            evidence = {"lesson_id": 1, "rate_before": 1.5, "rate_after": 1.5, "basis": basis}
            with self.subTest(basis=basis):
                self.assertTrue(explain._what("lesson_retire", "terminal", evidence, "en").startswith(en))
                self.assertTrue(explain._what("lesson_retire", "terminal", evidence, "pt").startswith(pt))

    def test_the_proposal_carries_the_basis_of_the_verdict(self):
        activated = NOW - 20 * DAY
        with experience(self.home) as conn:
            self.lesson(conn, 1, activated)
            lessons.shown(conn, [1], now=activated)
            self.days(conn, "sig01", "tool_01", activated - 5 * DAY, 25, failures=1)
            lessons.evaluate(conn, now=NOW, mode="suggest")
            [proposal] = [item for item in proposals.refresh(conn, now=NOW) if item["kind"] == "lesson_retire"]
        self.assertEqual(proposal["evidence"]["basis"], "failures_per_day")


class IndexTest(Store):
    """A10: the lessons part counts the lessons retired in the window too."""

    def test_a_lesson_retired_this_week_still_counts_against_it(self):
        with experience(self.home) as conn:
            self.lesson(conn, 1, NOW - 30 * DAY, verdict="effective")
            retired = self.lesson(conn, 2, NOW - 30 * DAY, verdict="ineffective")
            conn.execute("UPDATE lesson SET status='retired', updated=? WHERE id=?", (NOW - 2 * DAY, retired))
            self.assertEqual(evolution_index.parts(conn, now=NOW)["lessons"], 50)
            conn.execute("UPDATE lesson SET updated=? WHERE id=?", (NOW - 10 * DAY, retired))
            self.assertEqual(evolution_index.parts(conn, now=NOW)["lessons"], 100)


class ConfigUnreadTest(unittest.TestCase):
    """C1: without config.yaml (no YAML parser, a damaged file), nothing runs by itself."""

    def test_an_unread_configuration_means_manual_and_says_so(self):
        unread = options({"evolution": {"mode": "auto"}}, {CONFIG_UNREAD: True}, strict=False)
        self.assertEqual((unread["mode"], unread["auto_actions"], unread["config_unread"]), ("manual", "off", True))
        read = options({"evolution": {"mode": "auto"}}, {}, strict=False)
        self.assertEqual((read["mode"], read["config_unread"]), ("auto", False))


class SettingsReloadTest(PluginBase):
    """A4 and A5: what waits for the next load is measured against what this load registered."""

    def reload_keys(self):
        close_observers()
        with experience(self.home) as conn:
            row = conn.execute("SELECT value FROM cursor WHERE key='settings_reload'").fetchone()
        return json.loads(row[0]) if row else None

    def apply(self, plugin, **values):
        read = lambda home: {"plugins": {"entries": {"hermes-atlas-evolution": {"settings": values}}}}   # noqa: E731
        self.assertTrue(plugin.refresh_settings(force=True, read=read))

    def test_another_change_never_hides_one_that_still_waits(self):   # the auditor's r1
        plugin = self.plugin().hooks["on_kanban_dispatch_tick"][0].__self__
        self.apply(plugin, tool_hints=True)
        self.assertEqual(self.reload_keys(), ["tool_hints"])
        self.apply(plugin, tool_hints=True, language="pt")
        self.assertEqual(self.reload_keys(), ["tool_hints"])
        self.apply(plugin, language="pt", runtime_guard=False)
        self.assertEqual(self.reload_keys(), ["runtime_guard"])
        self.apply(plugin, language="pt", runtime_guard=True)
        self.assertEqual(self.reload_keys(), [])

    def test_a_reflector_switched_on_while_running_says_it_waits_for_the_next_load(self):   # the auditor's r6
        class Context(FakeContext):
            def __init__(self):
                super().__init__()
                self.aux = []
                self.llm = types.SimpleNamespace(complete_structured=lambda **_: None)

            def register_auxiliary_task(self, key, **_):
                self.aux.append(key)

        self.settings(evolution={"reflector": False})
        context = Context()
        from hermes_atlas_evolution.integration import register

        register(context)
        plugin = context.hooks["on_kanban_dispatch_tick"][0].__self__
        self.apply(plugin, reflector=True)
        self.assertEqual((context.aux, plugin.reflect, self.reload_keys()), ([], None, ["reflector"]))
        self.assertTrue(plugin.reflector_possible)   # the pass says "after a restart", not "outside Hermes"
        self.settings(evolution={})
        loaded = Context()
        register(loaded)
        on = loaded.hooks["on_kanban_dispatch_tick"][0].__self__
        self.assertEqual(loaded.aux, ["evolution_reflector"])
        self.apply(on, reflector=False)   # switching it off applies at the next pass: nothing waits
        self.assertEqual(self.reload_keys(), [])

    def test_the_pass_names_why_the_reflector_did_not_run(self):
        quiet = {"evolution": {"mode": "manual", "lessons": "auto", "reflector": True}}
        with patch("subprocess.run", side_effect=OSError("no subprocess in tests")):
            waits = cycle(self.home, now=NOW, config=quiet, reflector_reload=True)["reflector"]["status"]
            outside = cycle(self.home, now=NOW + 3600, config=quiet)["reflector"]["status"]
            off = cycle(self.home, now=NOW + 7200, config={"evolution": {"mode": "manual", "reflector": False}})
        self.assertEqual((waits, outside, off["reflector"]["status"]), ("needs_restart", "unavailable_without_hermes_llm", "off"))


class HostTest(PluginBase):
    """A6: only a process that stays up claims the learning pass."""

    def test_which_processes_claim_the_pass(self):
        cases = [(["/usr/bin/hermes"], True, True), (["/usr/bin/hermes"], False, False),
                 (["hermes", "-z", "hi"], True, False), (["hermes", "chat", "-q", "x"], True, True),
                 (["hermes", "chat", "-q", "x", "-Q"], True, False), (["hermes", "-p", "work"], True, True),
                 (["hermes", "evolution", "status"], True, False), (["/opt/h/tui_gateway/entry.py"], False, True),
                 (["hermes", "acp"], False, True), (["/opt/h/hermes_cli/kanban_worker_client.py"], False, False),
                 (["hermes", "gateway", "run"], False, True), (["hermes", "dashboard"], False, True),
                 (["hermes", "serve"], False, True), (["hermes", "-p", "work", "serve", "--port", "9"], False, True),
                 (["hermes", "chat", "-q", "x", "--format", "stream-json"], True, False),
                 (["hermes", "chat", "--format=stream-json"], True, False)]
        for argv, terminal, expected in cases:
            with self.subTest(argv=argv, terminal=terminal), patch.object(sys, "argv", argv):
                self.assertEqual(runs_passes(terminal=terminal), expected)

    def test_a_one_shot_run_never_starts_a_pass(self):
        ran = []
        pilot = Autopilot(self.home, lambda: ran.append(1), interval_minutes=60)
        pilot.claims = False
        self.assertFalse(REAL_POKE(pilot))
        pilot.claims = True
        with patch("hermes_atlas_evolution.autopilot.claim", return_value=False):   # due, but another process has it
            self.assertTrue(REAL_POKE(pilot))
            pilot._thread.join(5)
        with patch.object(sys, "argv", ["hermes", "-z", "hello"]):
            self.assertFalse(self.plugin().hooks["on_kanban_dispatch_tick"][0].__self__.autopilot.claims)
        with patch.object(sys, "argv", ["hermes", "gateway", "run"]):
            self.assertTrue(self.plugin().hooks["on_kanban_dispatch_tick"][0].__self__.autopilot.claims)
        self.assertEqual(ran, [])
        self.assertEqual([thread for thread in threading.enumerate() if thread.name == "hermes-atlas-evolution-autopilot"], [])


class OwnerContextTest(PluginBase):
    """A7 / I1: a chat whose sender turns out to be the owner gets the lessons its prompt left out, once."""

    def setUp(self):
        super().setUp()
        self.chat_type = "dm"   # what Hermes' gateway binds to the turn (gateway.session_context)
        bound = types.ModuleType("gateway.session_context")
        bound.get_session_env = lambda name, default="": self.chat_type if name == "HERMES_SESSION_CHAT_TYPE" else default
        modules = patch.dict(sys.modules, {"gateway.session_context": bound})
        modules.start()
        self.addCleanup(modules.stop)
        self.settings(evolution={"lessons": "auto", "language": "en"})
        self.hermes_settings(owner_sender_ids=["555"])
        self.context = self.plugin()
        with experience(self.home) as conn:
            self.own = lessons.add_custom(conn, "Use the staging tenant for every Atlas UI test run.", tool="terminal",
                                          origin="operator", now=1.0, active=True)
            conn.execute("""INSERT INTO lesson(signature, tool, klass, hint, origin, status, created, updated, activated)
                VALUES ('sig', 'terminal', 'missing_dependency', 'rg', 'derived', 'active', 1, 1, 1)""")
        self.render = self.context.sections["hermes-atlas-evolution.playbook"]["content"]

    def turn(self, session, turn, sender, platform="telegram"):
        return self.context.fire("pre_llm_call", session_id=session, turn_id=turn, platform=platform, sender_id=sender,
                                 user_message="hello", is_first_turn=turn.endswith("1"))

    def test_the_owner_gets_them_once_and_a_stranger_never(self):
        self.assertNotIn("staging tenant", self.render({"session_id": "dm", "platform": "telegram"}))
        given = self.turn("dm", "t1", "555")
        self.assertIn("staging tenant", given["context"])
        self.assertNotIn("`rg`", given["context"])   # already in the chat's prompt
        self.assertLessEqual(len(given["context"]), lessons.OWNER_MAX_CHARS)
        self.assertIsNone(self.turn("dm", "t2", "555"))   # once per session
        self.render({"session_id": "group", "platform": "telegram"})
        self.assertIsNone(self.turn("group", "g1", "stranger"))
        self.assertIsNone(self.turn("group", "g2", "555"))   # someone else wrote here: it never gets them
        self.assertIsNone(self.turn("unseen", "u1", "555"))   # a prompt this process did not build
        self.render({"session_id": "cli", "platform": "cli"})
        self.assertIsNone(self.turn("cli", "c1", None, platform="cli"))   # its prompt already had everything
        close_observers()
        with experience(self.home) as conn:
            shown = conn.execute("SELECT shown FROM lesson WHERE id=?", (self.own,)).fetchone()[0]
            first = conn.execute("SELECT COUNT(*) FROM lesson_measure WHERE lesson_id=?", (self.own,)).fetchone()[0]
        self.assertEqual((shown, first), (2, 1))   # the cli prompt and the dm context; read since the first

    def test_only_a_direct_message_gets_them(self):
        for chat_type in ("group", "thread", "channel", ""):   # one session may be everyone's; unknown means no
            with self.subTest(chat_type=chat_type):
                self.chat_type = chat_type
                self.render({"session_id": f"s-{chat_type}", "platform": "telegram"})
                self.assertIsNone(self.turn(f"s-{chat_type}", "t1", "555"))
        self.chat_type = "dm"   # a refusal marks nothing: the same session, read as a direct message, gets them
        self.assertIn("staging tenant", self.turn("s-group", "t2", "555")["context"])
        with patch.dict(sys.modules, {"gateway.session_context": None}):   # outside a gateway: no chat type
            self.render({"session_id": "plain", "platform": "telegram"})
            self.assertIsNone(self.turn("plain", "t1", "555"))

    def test_nothing_is_given_with_lessons_off_or_on_a_busy_database(self):
        plugin = self.context.hooks["on_kanban_dispatch_tick"][0].__self__
        self.render({"session_id": "dm", "platform": "telegram"})
        with patch("hermes_atlas_evolution.operations.database", side_effect=OSError("busy")):
            self.assertIsNone(self.turn("dm", "t1", "555"))   # fails open
        self.render({"session_id": "dm2", "platform": "telegram"})
        plugin.evolution = {**plugin.evolution, "lessons": "off"}
        self.assertIsNone(self.turn("dm2", "t1", "555"))


class PlaybookCacheTest(PluginBase):
    """A12: a render that started before forget_playbook() is not kept."""

    def test_a_render_overtaken_by_a_change_is_not_cached(self):
        self.settings(evolution={"lessons": "auto"})
        context = self.plugin()
        plugin = context.hooks["on_kanban_dispatch_tick"][0].__self__
        real = lessons.playbook

        def overtaken(*args, **kwargs):
            text = real(*args, **kwargs)
            plugin.forget_playbook()   # the learning pass changed the lessons while this render read them
            return text

        with patch.object(lessons, "playbook", overtaken):
            plugin.playbook_section({"session_id": "a", "platform": "cli"})
        self.assertEqual(plugin._playbook, {})
        plugin.playbook_section({"session_id": "b", "platform": "cli"})
        self.assertEqual(len(plugin._playbook), 1)


class EventsTest(PluginBase):
    """I5: the plugin's events on Hermes' bus carry ids and numbers only, and never fail anything."""

    def test_activations_retirements_and_spikes_are_emitted_once(self):
        sent = []
        context = FakeContext()
        context.emit = lambda name, payload=None: sent.append((name, payload))
        from hermes_atlas_evolution.integration import register

        register(context)
        plugin = context.hooks["on_kanban_dispatch_tick"][0].__self__
        self.assertIsNotNone(plugin.emit)
        with experience(self.home) as conn:
            conn.execute("INSERT INTO failure VALUES ('spike', 'terminal', 'timeout', '', ?, ?, 12, 3, '')", (NOW, NOW))
            conn.execute("INSERT INTO failure_daily VALUES (?, 'spike', 12)", (utc_day(NOW),))
        result = {"now": NOW, "new_lessons": [{"id": 3, "status": "active", "class": "missing_argument",
                                               "lesson": "When calling x, always pass y."},
                                              {"id": 4, "status": "candidate", "class": "timeout", "lesson": "z"}],
                  "lesson_effects": [{"id": 5, "retired": True, "verdict": "ineffective", "basis": "failures_per_day"},
                                     {"id": 6, "retired": False, "verdict": "effective", "basis": "failures_per_call"}]}
        plugin.pass_events(result)
        plugin.pass_events(result)   # the spike is told once a day
        names = [name for name, _ in sent]
        self.assertEqual(names.count("failure_spike"), 1)
        self.assertEqual(names.count("lesson_activated"), 2)
        self.assertEqual(names.count("lesson_retired"), 2)
        for _name, payload in sent:
            self.assertNotIn("lesson", payload)
            self.assertNotIn("tool", payload)
            self.assertTrue(all(isinstance(value, (int, float)) or isinstance(value, str) and len(value) <= 24
                                for value in payload.values()), payload)
        context.emit = None
        plugin.emit = lambda *_: (_ for _ in ()).throw(RuntimeError("bus down"))
        plugin.pass_events(result)   # never raises

    def test_without_a_bus_the_pass_hears_nothing(self):
        plugin = self.plugin().hooks["on_kanban_dispatch_tick"][0].__self__
        self.assertIsNone(plugin.emit)
        plugin.pass_events({"now": NOW, "new_lessons": [{"id": 1, "status": "active"}]})


class SessionEndTest(PluginBase):
    """I6: what the plugin keeps in memory about a session goes when Hermes resets or deletes it."""

    def test_reset_and_delete_forget_the_session(self):
        context = self.plugin()
        plugin = context.hooks["on_kanban_dispatch_tick"][0].__self__
        for session in ("old", "a", "b", "keep"):
            context.fire("pre_llm_call", session_id=session, turn_id=f"t-{session}", platform="cli", user_message="hi")
            plugin.playbook_section({"session_id": session, "platform": "cli"})
        context.fire("on_session_reset", session_id="new", old_session_id="old", reason="new_session")
        context.fire("on_session_delete", session_id="a", reason="delete", profile="default", deleted_ids=["a", "b"])
        context.fire("on_session_reset", session_id="keep", reason="new_session")   # names the new session only
        self.assertEqual([session for session in ("old", "a", "b", "keep") if plugin.served_owner(session)], ["keep"])
        self.assertEqual(list(plugin.counted), ["keep"])
        for odd in ({}, {"session_id": 5, "deleted_ids": "x"}, {"old_session_id": ["x"]}):
            self.assertIsNone(context.fire("on_session_delete", **odd))
            self.assertIsNone(context.fire("on_session_reset", **odd))


class HooksNeverRaiseTest(PluginBase):
    """The auditor's r2: every hook, the new ones included, tolerates any payload."""

    def test_odd_payloads(self):
        self.hermes_settings(tool_hints=True, agent_tools=True, owner_platforms=["telegram"], owner_sender_ids=[5])
        context = self.plugin()
        odd = [None, "", "x" * 500, 7, 3.5, float("nan"), [], ["x"], {}, {"a": 1}, b"bytes", True, object(), "\ud800",
               "error", "ok", "blocked", "timeout", "cli", "telegram", "subagent", "t1", "s1", "loaded"]
        keys = ["tool_name", "status", "result", "args", "tool_call_id", "session_id", "turn_id", "error_message",
                "platform", "user_message", "sender_id", "parent_session_id", "is_first_turn", "child_session_id",
                "old_session_id", "new_session_id", "deleted_ids", "reason", "action", "skill_name", "completed"]
        chance = random.Random(12)
        for hook, callbacks in context.hooks.items():
            for _ in range(60):
                payload = {key: chance.choice(odd) for key in chance.sample(keys, 7)}
                for callback in callbacks:
                    with self.subTest(hook=hook):
                        callback(**payload)
        close_observers()


class GuardFailureTest(PluginBase):
    """A guard that fails internally still lets the command through, but says so in the log, once per process."""

    def test_a_failing_guard_is_logged_once_and_fails_open(self):
        context = self.plugin()
        plugin = context.hooks["on_kanban_dispatch_tick"][0].__self__
        self.assertIs(plugin.guard.on_error.__func__, type(plugin).guard_failed)
        plugin.guard = lambda **_: (_ for _ in ()).throw(RuntimeError("broken"))
        with self.assertLogs("hermes_atlas_evolution.integration", level="WARNING") as logged:
            for _ in range(3):
                self.assertIsNone(context.fire("pre_tool_call", tool_name="terminal", args={"command": "ls"}))
            plugin.guard_failed(ValueError("again"))
        self.assertEqual(len(logged.records), 1)
        self.assertIsNotNone(logged.records[0].exc_info)
        self.assertIn("gateway_program_deleted", evolution_index._HEALTH_HIGH)


class ReviewFollowUpTest(Store):
    """The second review: a frozen divisor (F1), the migration before the first reading (F2), the classifier change
    (F5) and fleet_managed read as the lead reads it (F7)."""

    def frozen_lesson(self, conn, activated, *, baseline_days=None):
        conn.execute("""INSERT INTO lesson(id, signature, tool, klass, hint, origin, status, created, updated, activated,
              shown, verdict, baseline_failures, baseline_calls) VALUES (1, 'sig1', 'terminal', 'timeout', '', 'derived',
              'active', ?, ?, ?, 7, 'ineffective', 14, 0)""", (activated, activated, activated))
        conn.execute("INSERT INTO lesson_measure(lesson_id, activated, first_shown, baseline_days) VALUES (1, ?, ?, ?)",
                     (activated, activated, baseline_days))
        conn.execute("INSERT INTO cursor VALUES ('lesson_measure_since', '0')")
        for day in range(1, 89):   # what 90 days of retention left: the failure on 4 days out of 5
            if day % 5:
                conn.execute("INSERT INTO failure_daily VALUES (?, 'sig1', 1)", (utc_day(NOW - day * DAY),))

    def test_retention_never_shrinks_a_frozen_per_day_baseline(self):   # the reviewer's prune.py
        for age in (60, 80, 100):
            with self.subTest(age=age):
                self.setUp()
                with experience(self.home) as conn:
                    self.frozen_lesson(conn, NOW - age * DAY)
                    [effect] = lessons.evaluate(conn, now=NOW, mode="suggest")
                    days = conn.execute("SELECT baseline_days FROM lesson_measure").fetchone()[0]
                self.assertEqual((effect["before"], effect["verdict"], days), (1.0, "inconclusive", 14))

    def test_the_divisor_is_frozen_with_the_baseline(self):
        activated = NOW - 10 * DAY
        with experience(self.home) as conn:
            self.lesson(conn, 1, activated)
            lessons.shown(conn, [1], now=activated)
            self.days(conn, "sig01", "tool_01", activated - 2 * DAY, 2, failures=2)
            self.days(conn, "sig01", "tool_01", activated + DAY, 10, failures=1)
            first = lessons.evaluate(conn, now=NOW, mode="suggest")[0]["before"]
            conn.execute("DELETE FROM failure_daily WHERE day < ?", (utc_day(activated),))   # retention, much later
            again = lessons.evaluate(conn, now=NOW, mode="suggest")[0]["before"]
        self.assertEqual((first, again), (2.0, 2.0))

    def test_a_prompt_before_the_first_pass_keeps_the_measure_since_the_activation(self):   # the reviewer's race.py
        activated = NOW - 10 * DAY
        with experience(self.home) as conn:
            conn.execute("""INSERT INTO lesson(id, signature, tool, klass, hint, origin, status, created, updated,
                  activated, shown, verdict, baseline_failures, baseline_calls) VALUES (1, 'sig1', 'terminal', 'timeout',
                  '', 'derived', 'active', ?, ?, ?, 7, 'effective', 30, 100)""", (activated, activated, activated))
            self.days(conn, "sig1", "terminal", NOW - 8 * DAY, 8, failures=1, calls=10)
            lessons.shown(conn, [1], now=NOW)   # the gateway's first prompt after the update
            [effect] = lessons.evaluate(conn, now=NOW, mode="auto")
            first = conn.execute("SELECT first_shown FROM lesson_measure").fetchone()[0]
        self.assertEqual((first, effect["days_active"], effect["basis"]), (activated, 10, "failures_per_call"))

    def test_the_classifier_mark_has_its_own_once_flag(self):
        with experience(self.home) as conn:
            conn.execute("INSERT INTO cursor VALUES ('lesson_measure_since', '0')")   # migrated by a development build
            lessons.evaluate(conn, now=NOW, mode="auto")   # no start of observation yet: undecided
            self.assertIsNone(conn.execute("SELECT 1 FROM cursor WHERE key=?", (lessons.CLASSIFIER_CHECKED,)).fetchone())
            conn.execute("INSERT INTO cursor VALUES ('observed_since', ?)", (repr(NOW - 30 * DAY),))
            lessons.evaluate(conn, now=NOW, mode="auto")
            marked = conn.execute("SELECT value FROM cursor WHERE key=?", (lessons.CLASSIFIER_CHANGED,)).fetchone()
        self.assertEqual(float(marked[0]), NOW)
        self.setUp()
        with experience(self.home) as conn:   # a new install: its history is replayed by this version
            conn.execute("INSERT INTO cursor VALUES ('observed_since', ?)", (repr(NOW - 3600),))
            lessons.evaluate(conn, now=NOW, mode="auto")
            lessons.evaluate(conn, now=NOW + 3 * DAY, mode="auto")
            self.assertIsNone(conn.execute("SELECT 1 FROM cursor WHERE key=?", (lessons.CLASSIFIER_CHANGED,)).fetchone())

    def test_a_measure_never_compares_counts_of_two_classifiers(self):
        updated = NOW - 20 * DAY
        with experience(self.home) as conn:
            conn.execute("INSERT INTO cursor VALUES ('observed_since', ?)", (repr(NOW - 200 * DAY),))
            # read before 0.12.0, judged then; its failure (`429 passed`) is no longer counted under its signature
            old = conn.execute("""INSERT INTO lesson(signature, tool, klass, hint, origin, status, created, updated,
                  activated, shown, verdict, rate_before, rate_after, baseline_failures, baseline_calls)
                VALUES ('old', 'terminal', 'rate_limited', '', 'derived', 'active', ?, ?, ?, 4, 'inconclusive', 0.2, 0.2,
                  10, 50)""", (updated - 30 * DAY, updated - 30 * DAY, updated - 30 * DAY)).lastrowid
            self.days(conn, "old", "terminal", updated - 40 * DAY, 40, failures=1, calls=5)
            lessons.evaluate(conn, now=updated, mode="auto")   # the first 0.12.0 pass marks the change
            # a lesson activated a week after the update: its baseline starts at the update
            new = self.lesson(conn, 5, updated + 7 * DAY)
            lessons.shown(conn, [new], now=updated + 7 * DAY)
            self.days(conn, "sig05", "tool_05", updated - 10 * DAY, 10, failures=5, calls=10)   # the old way
            self.days(conn, "sig05", "tool_05", updated + DAY, 6, failures=1, calls=10)          # the new way
            judged = {item["id"]: item for item in lessons.evaluate(conn, now=NOW, mode="auto")}
            frozen = conn.execute("SELECT baseline_failures, baseline_calls FROM lesson WHERE id=?", (new,)).fetchone()
        self.assertEqual((judged[old]["verdict"], judged[old]["retired"]), ("inconclusive", False))   # not "effective"
        self.assertEqual(tuple(frozen), (6, 60))   # the update day and the 5 days after it, not the 5 failures a day before


class SettingsFollowUpTest(PluginBase):
    """F6: /new in the CLI forgets the session it ended; F7: fleet_managed as the lead reads it; F8: only what changed."""

    def test_a_reset_without_the_old_id_forgets_the_surfaces_last_session(self):
        context = self.plugin()
        plugin = context.hooks["on_kanban_dispatch_tick"][0].__self__
        context.fire("pre_llm_call", session_id="first", turn_id="t1", platform="cli", user_message="hi")
        context.fire("pre_llm_call", session_id="other", turn_id="t2", platform="tui", user_message="hi")
        context.fire("on_session_reset", session_id="window-2", platform="tui")   # a TUI window opening: no reason
        self.assertTrue(plugin.served_owner("other"))   # the other window may still be open
        context.fire("on_session_reset", session_id="second", platform="cli", reason="new_session")
        self.assertEqual((plugin.served_owner("first"), plugin.served_owner("other")), (False, True))

    def test_fleet_managed_reads_an_opt_out_as_the_lead_does(self):
        from hermes_atlas_evolution.environment import fleet_managed

        for value in (False, "off", "no", "0", 0):
            self.assertFalse(options({"evolution": {"fleet_managed": value}}, {}, strict=False)["fleet_managed"])
        self.assertTrue(options({"evolution": {}}, {}, strict=False)["fleet_managed"])
        (self.home / "evolution").mkdir(exist_ok=True)
        (self.home / "evolution" / "settings.json").write_text(json.dumps({"evolution": {"fleet_managed": False}}))
        runtime = {"plugins": {"entries": {"hermes-atlas-evolution": {"settings": {"fleet_managed": True}}}}}
        merged = {"evolution": {"fleet_managed": True}}   # config.yaml wins the merge
        self.assertEqual(options(merged, runtime, strict=False, home=self.home)["fleet_managed"],
                         fleet_managed(self.home, read_config=lambda _home: runtime))
        self.assertFalse(options(merged, runtime, strict=False, home=self.home)["fleet_managed"])
        (self.home / "evolution" / "settings.json").write_text("{}")
        for home in (self.home, None):   # config.yaml not read: an opt-out it may hold
            self.assertFalse(options({"evolution": {}}, {CONFIG_UNREAD: True}, strict=False, home=home)["fleet_managed"])

    def test_switching_lessons_off_names_no_other_setting(self):
        self.hermes_settings(tool_hints=True)
        plugin = self.plugin().hooks["on_kanban_dispatch_tick"][0].__self__
        settings = {"lessons": "off", "tool_hints": True}
        plugin.refresh_settings(force=True, read=lambda home: {"plugins": {"entries": {
            "hermes-atlas-evolution": {"settings": settings}}}})
        close_observers()
        with experience(self.home) as conn:
            waiting = json.loads(conn.execute("SELECT value FROM cursor WHERE key='settings_reload'").fetchone()[0])
        self.assertEqual(waiting, [])


class HeaderTest(Store):
    """The prompt section names failures as the source only when every lesson shown came from one."""

    def test_the_header_fits_the_lessons_shown(self):
        with experience(self.home) as conn:
            self.lesson(conn, 1, NOW)
            for lang in ("en", "pt"):
                self.assertTrue(lessons.playbook(conn, lang=lang).startswith(lessons.HEADERS[lang]))
            lessons.add_custom(conn, "When deploying, run the smoke tests first.", tool="general", origin="operator",
                               now=NOW, active=True)
            for lang in ("en", "pt"):
                text = lessons.playbook(conn, lang=lang)
                self.assertTrue(text.startswith(lessons.MIXED_HEADERS[lang]))
                self.assertLessEqual(len(text), 1400)
            self.assertTrue(lessons.playbook(conn, templates_only=True).startswith(lessons.HEADERS["en"]))


class CommandLineTest(PluginBase):
    """C10: the one line Hermes shows for `hermes evolution` says what the plugin is now, in both languages."""

    def test_the_command_help(self):
        from hermes_atlas_evolution.integration import CLI_HELP

        helps = {}
        context = FakeContext()
        context.register_cli_command = lambda name, help_text, setup, handler=None, **_: helps.setdefault(name, help_text)
        from hermes_atlas_evolution.integration import register

        register(context)
        self.assertEqual(helps["evolution"], CLI_HELP)
        self.assertIn("Lições", CLI_HELP)
        self.assertNotIn("operations", CLI_HELP)


if __name__ == "__main__":
    unittest.main()
