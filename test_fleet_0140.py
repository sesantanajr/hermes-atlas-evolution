"""0.14.0: the audit of the live fleet, each finding as a regression, and what the release adds.

- A gateway started by a launcher of its own (runpy, argv `[".../gateway/run.py"]`) is a gateway: it claims the learning
  passes of every profile it serves (up to 0.13.0 it counted as a short command, and 11 of 12 profiles never ran one),
  one pass at a time per process, at a staggered start, and a process asks again later when it did not claim at load.
- A lesson's verdict needs evidence: "effective" only when the baseline predicts at least 3 failures after and seeing
  that few is unlikely by chance; an "effective" judged on 20 calls before 0.12.0 is dropped; a lesson whose failure
  keeps happening while the agent reads it is ineffective.
- A lesson whose tool nobody calls any more is obsolete; template lessons of one cause share one sentence.
- Drift after a Hermes update (receipts, overlays, local changes, stashes, configuration schema), Hermes' warnings in
  its log, the gateway's memory against its budget, every profile in one view, Auto for all of them and back, lessons
  shared across profiles, the daily report and its schedule, and the `fleet` command.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_atlas_evolution import (autopilot, drift, environment, fleet, gateway_memory, lessons, logsignals,
                                    logwatch, proposals, status)
from hermes_atlas_evolution.experience import day_offset, experience, utc_day

from test_environment_085 import NOW, Fleet, plugin

os.environ.setdefault("ATLAS_EVOLUTION_NO_HERMES", "1")   # never the real Hermes of this machine
DAY = 86_400
GIT = unittest.skipUnless(shutil.which("git"), "needs git")
POSIX = unittest.skipUnless(os.name == "posix", "POSIX only")


class Scratch(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name).resolve()
        self.home = self.base / "hermes"
        self.home.mkdir()


# ── the gateway, however it was started ─────────────────────────────────────────────────────────────────────────
class GatewayKindTest(Scratch):
    def setUp(self):
        super().setUp()
        status._GATEWAY_RECORD.update(pid=None, checked=float("-inf"), found=False)
        self.addCleanup(status._GATEWAY_RECORD.update, pid=None, checked=float("-inf"), found=False)

    def test_a_launcher_that_runs_gateway_run_as_main_is_a_gateway(self):
        main = types.ModuleType("__main__")
        main.__file__ = "/opt/hermes/gateway/run.py"
        with patch.object(sys, "argv", ["/opt/hermes/gateway/run.py"]), patch.dict(sys.modules, {"__main__": main}), \
                patch.dict(os.environ, {"HERMES_HOME": str(self.home)}):
            self.assertEqual(status.process_kind(), "gateway")
            self.assertTrue(autopilot.runs_passes(terminal=False))

    def test_hermes_own_record_names_the_gateway(self):
        (self.home / "gateway.pid").write_text(json.dumps({"pid": os.getpid(), "kind": "hermes-gateway"}))
        with patch.object(sys, "argv", ["hermes-gateway"]), patch.dict(os.environ, {"HERMES_HOME": str(self.home)}):
            self.assertTrue(status.gateway_record(roots=[self.home]))
            self.assertEqual(status.process_kind(), "gateway")

    def test_a_short_command_is_still_a_command(self):
        (self.home / "gateway.pid").write_text(json.dumps({"pid": os.getpid() + 1, "kind": "hermes-gateway"}))
        with patch.object(sys, "argv", ["hermes", "chat", "-q", "hi"]), \
                patch.dict(os.environ, {"HERMES_HOME": str(self.home)}):
            self.assertFalse(status.gateway_record(roots=[self.home]))
            self.assertFalse(autopilot.runs_passes(["hermes", "chat", "-q", "hi"], terminal=False))

    def test_a_record_written_after_load_is_found_later(self):
        self.assertFalse(status.gateway_record(roots=[self.home]))
        (self.home / "gateway.pid").write_text(json.dumps({"pid": os.getpid(), "kind": "hermes-gateway"}))
        self.assertFalse(status.gateway_record(roots=[self.home]))   # looked at again only after a minute
        status._GATEWAY_RECORD["checked"] -= status.GATEWAY_RECORD_SECONDS + 1
        self.assertTrue(status.gateway_record(roots=[self.home]))


class AutopilotFleetTest(Scratch):
    def test_a_process_that_did_not_claim_asks_again(self):
        clock = [1000.0]
        pilot = autopilot.Autopilot(self.home, lambda: None, interval_minutes=60, clock=lambda: clock[0])
        pilot.claims, asked = False, []
        pilot.recheck = lambda: asked.append(1) or True
        pilot._rechecked = clock[0]
        self.assertFalse(pilot.poke())
        clock[0] += autopilot.CLAIMS_RECHECK_SECONDS
        with patch.object(threading.Thread, "start"):
            pilot.poke()
        self.assertEqual((asked, pilot.claims), ([1], True))

    def test_one_pass_at_a_time_per_process(self):
        ran = []
        pilot = autopilot.Autopilot(self.home, lambda: ran.append(1), interval_minutes=60)
        with autopilot._PASS_SLOT:   # another profile's pass runs now
            pilot._work()
        self.assertEqual(ran, [])
        pilot._work()
        self.assertEqual(ran, [1])

    def test_the_clocks_of_a_gateways_profiles_start_spread_out(self):
        delays = {autopilot.stagger(self.home / "profiles" / f"p{index}") for index in range(12)}
        self.assertTrue(all(0 <= delay <= autopilot.STAGGER_SECONDS for delay in delays))
        self.assertGreater(len(delays), 6)
        self.assertEqual(autopilot.stagger(self.home), autopilot.stagger(self.home))


# ── verdicts that need evidence ─────────────────────────────────────────────────────────────────────────────────
class VerdictTest(unittest.TestCase):
    def test_too_little_use_since_says_nothing(self):
        # 5 failures in 1,769 calls before, none in the 20 calls after: the baseline predicted 0.06 failures there
        self.assertIsNone(lessons.judge(base_failures=5, failures_after=0, before=5 / 1769, after=0.0, exposure=20,
                                        days_active=6))

    def test_enough_use_with_no_failure_is_effective(self):
        self.assertEqual(lessons.judge(base_failures=30, failures_after=0, before=0.1, after=0.0, exposure=40,
                                       days_active=14), "effective")

    def test_a_reduction_chance_explains_is_inconclusive(self):
        # 4 expected, 2 seen: P(X <= 2 | 4) is about 0.24
        self.assertEqual(lessons.judge(base_failures=40, failures_after=2, before=0.1, after=0.05, exposure=40,
                                       days_active=14), "inconclusive")

    def test_a_failure_that_keeps_happening_is_ineffective(self):
        self.assertEqual(lessons.judge(base_failures=10, failures_after=5, before=0.1, after=0.1, exposure=50,
                                       days_active=14), "ineffective")

    def test_a_rare_failure_ends_inconclusive(self):
        self.assertIsNone(lessons.judge(base_failures=2, failures_after=0, before=0.01, after=0.0, exposure=50,
                                        days_active=20))
        self.assertEqual(lessons.judge(base_failures=2, failures_after=0, before=0.01, after=0.0, exposure=50,
                                       days_active=lessons.INCONCLUSIVE_AFTER_DAYS), "inconclusive")

    def test_poisson(self):
        self.assertAlmostEqual(lessons.poisson_at_most(0, 3.0), 0.0498, places=3)
        self.assertAlmostEqual(lessons.poisson_at_most(2, 4.0), 0.2381, places=3)
        self.assertEqual(lessons.poisson_at_most(0, 0), 1.0)


class LegacyVerdictTest(Scratch):
    def lesson(self, conn, *, verdict, activated, rate=0.01, basis="failures_per_call"):
        lesson_id = conn.execute("""INSERT INTO lesson(signature, tool, klass, hint, origin, status, created, updated,
              activated, shown, verdict, rate_before, rate_after, baseline_failures, baseline_calls)
            VALUES ('sig', 'terminal', 'timeout', '', 'derived', 'active', ?, ?, ?, 80, ?, ?, 0.0, 16, 1769)""",
                                 (activated, activated, activated, verdict, rate)).lastrowid
        conn.execute("INSERT INTO lesson_measure(lesson_id, activated, first_shown, basis) VALUES (?, ?, ?, ?)",
                     (lesson_id, activated, activated, basis))
        return lesson_id

    def days(self, conn, start, count, *, failures, calls):
        for offset in range(count):
            day = utc_day(start + offset * DAY)
            conn.execute("INSERT OR REPLACE INTO failure_daily VALUES (?, 'sig', ?)", (day, failures))
            conn.execute("INSERT OR REPLACE INTO tool_daily VALUES (?, 'terminal', ?, 0, 0, 0)", (day, calls))

    def test_an_effective_judged_before_the_classifier_change_is_dropped(self):
        with experience(self.home) as conn:
            conn.execute("INSERT INTO cursor VALUES ('observed_since', ?)", (repr(NOW - 60 * DAY),))
            conn.execute("INSERT INTO cursor VALUES (?, ?)", (lessons.CLASSIFIER_CHANGED, repr(NOW - 7 * DAY)))
            conn.execute("INSERT INTO cursor VALUES (?, ?)", (lessons.CLASSIFIER_CHECKED, repr(NOW - 7 * DAY)))
            conn.execute("INSERT INTO cursor VALUES (?, ?)", (lessons.MEASURE_MIGRATED, repr(NOW - 7 * DAY)))
            lesson_id = self.lesson(conn, verdict="effective", activated=NOW - 9 * DAY)
            self.days(conn, NOW - 13 * DAY, 6, failures=1, calls=40)   # counted the old way: never compared
            [effect] = lessons.evaluate(conn, now=NOW, mode="auto")
            row = conn.execute("SELECT status, verdict FROM lesson WHERE id=?", (lesson_id,)).fetchone()
        self.assertEqual((effect["verdict"], tuple(row)), (None, ("active", None)))

    def test_a_legacy_lesson_still_failing_since_the_change_is_ineffective(self):
        with experience(self.home) as conn:
            conn.execute("INSERT INTO cursor VALUES ('observed_since', ?)", (repr(NOW - 60 * DAY),))
            conn.execute("INSERT INTO cursor VALUES (?, ?)", (lessons.CLASSIFIER_CHANGED, repr(NOW - 7 * DAY)))
            conn.execute("INSERT INTO cursor VALUES (?, ?)", (lessons.CLASSIFIER_CHECKED, repr(NOW - 7 * DAY)))
            conn.execute("INSERT INTO cursor VALUES (?, ?)", (lessons.MEASURE_MIGRATED, repr(NOW - 7 * DAY)))
            self.lesson(conn, verdict="effective", activated=NOW - 9 * DAY)
            self.days(conn, NOW - 6 * DAY, 7, failures=1, calls=40)   # one a day since the change: 7 in 280 calls
            [effect] = lessons.evaluate(conn, now=NOW, mode="auto")
        self.assertEqual((effect["verdict"], effect["basis"]), ("ineffective", "failures_per_call"))


class ObsoleteLessonTest(Scratch):
    def lesson(self, conn, tool, *, auto: bool, activated=NOW - 40 * DAY):
        lesson_id = conn.execute("""INSERT INTO lesson(signature, tool, klass, hint, origin, status, created, updated,
              activated, shown) VALUES (?, ?, 'timeout', '', 'derived', 'active', ?, ?, ?, 9)""",
                                 (f"sig-{tool}", tool, activated, activated, activated)).lastrowid
        conn.execute("INSERT INTO failure VALUES (?, ?, 'timeout', '', ?, ?, 5, 3, 'x')",
                     (f"sig-{tool}", tool, activated - 10 * DAY, NOW - 35 * DAY))
        if auto:
            conn.execute("INSERT INTO lesson_auto VALUES (?, ?)", (lesson_id, activated))
        return lesson_id

    def test_a_lesson_whose_tool_nobody_calls_any_more_goes(self):
        with experience(self.home) as conn:
            conn.execute("INSERT INTO cursor VALUES (?, '0')", (lessons.AUTO_MARKED,))
            gone = self.lesson(conn, "mem0_add", auto=True)
            used = self.lesson(conn, "terminal", auto=True)
            conn.execute("INSERT INTO tool_daily VALUES (?, 'terminal', 12, 0, 0, 0)", (day_offset(NOW, 2),))
            lessons.tidy(conn, now=NOW)
            rows = dict(conn.execute("SELECT id, status FROM lesson").fetchall())
            reason = conn.execute("SELECT reason FROM lesson_reason WHERE lesson_id=?", (gone,)).fetchone()
        self.assertEqual((rows[gone], rows[used], reason[0]), ("retired", "active", "tool_unused"))

    def test_one_the_owner_approved_gets_a_proposal_instead(self):
        with experience(self.home) as conn:
            approved = self.lesson(conn, "mem0_add", auto=False)
            conn.execute("INSERT INTO cursor VALUES (?, '0')", (lessons.AUTO_MARKED,))
            lessons.tidy(conn, now=NOW)
            proposals.refresh(conn, now=NOW)
            row = conn.execute("SELECT kind, evidence FROM proposal WHERE code=?", (f"lesson_retire:{approved}",)).fetchone()
            state = conn.execute("SELECT status FROM lesson WHERE id=?", (approved,)).fetchone()[0]
        self.assertEqual((row[0], json.loads(row[1])["reason"], state), ("lesson_retire", "tool_unused", "active"))


class MergedSentenceTest(Scratch):
    def test_one_cause_on_several_tools_is_one_line(self):
        with experience(self.home) as conn:
            for index, tool in enumerate(("terminal", "tool_search", "skill_manage")):
                conn.execute("""INSERT INTO lesson(signature, tool, klass, hint, origin, status, created, updated,
                      activated) VALUES (?, ?, 'blocked_by_policy', '', 'derived', 'active', ?, ?, ?)""",
                             (f"s{index}", tool, NOW, NOW, NOW))
            conn.execute("""INSERT INTO lesson(signature, tool, klass, hint, origin, status, created, updated, activated)
                VALUES ('t', 'terminal', 'timeout', '', 'derived', 'active', ?, ?, ?)""", (NOW, NOW, NOW))
            chosen: list = []
            text = lessons.playbook(conn, lang="pt", chosen=chosen)
        lines = [line for line in text.splitlines() if line.startswith("- ")]
        self.assertEqual(len(lines), 2)
        self.assertIn("`terminal`, `tool_search` e `skill_manage`: esse tipo de comando", text)
        self.assertEqual(len(chosen), 4)


# ── drift after a Hermes update ─────────────────────────────────────────────────────────────────────────────────
RECEIPT = {
    "schema": 1, "started_at": "2026-11-25T14:01:52+00:00", "finished_at": "2026-11-25T14:23:44+00:00",
    "outcome": "partial", "pre_update": {"sha": "a" * 40, "version": "0.23.3"},
    "post_update": {"sha": "b" * 40, "version": "0.23.4"},
    "steps": [{"name": "pre_update_backup", "ok": True},
              {"name": "local_changes_stash", "ok": False, "detail": "parked: " + "c" * 40 + " (restore hit conflicts)"}],
    "pm_warnings": [{"message": "Disabled plugin 'phoenix' in {home}/profiles/work: the dependency environment no longer "
                                "builds with it: cause"}],
    "gateway_restart": {"failed_units": ["hermes-dashboard.service"], "incomplete": False},
}


class DriftTest(Scratch):
    def receipt(self, **change):
        folder = self.home / "logs" / "update_receipts"
        folder.mkdir(parents=True, exist_ok=True)
        body = json.loads(json.dumps({**RECEIPT, **change}).replace("{home}", str(self.home)))
        (folder / "update_20261125_140152_1_x.json").write_text(json.dumps(body))

    def test_a_receipt_in_plain_fields(self):
        (self.home / "profiles" / "work").mkdir(parents=True)
        self.receipt()
        [item] = drift.receipts(self.home)
        self.assertEqual((item["from"]["version"], item["to"]["version"], item["outcome"]), ("0.23.3", "0.23.4", "partial"))
        self.assertEqual((item["local_changes"], item["stash"]), ("parked", "c" * 12))
        self.assertEqual(item["plugins_disabled"], [{"plugin": "phoenix", "profile": "work"}])
        self.assertEqual(item["failed_units"], ["hermes-dashboard.service"])

    def test_overlays_seen_in_the_process(self):
        install = self.base / "hermes-agent"
        (install / "tools").mkdir(parents=True)
        (install / "hermes_cli").mkdir()
        (install / "gateway").mkdir()
        for folder in ("tools", "hermes_cli", "gateway"):
            (install / folder / "__init__.py").write_text("")
        upstream = install / "tools" / "handlers.py"
        upstream.write_text("new = 1\n")
        overlay_dir = self.base / "overlay" / "tools"
        overlay_dir.mkdir(parents=True)
        overlay = overlay_dir / "handlers.py"
        overlay.write_text("old = 1\n")
        package = types.ModuleType("tools")
        package.__file__ = str(install / "tools" / "__init__.py")
        package.__path__ = [str(overlay_dir), str(install / "tools")]
        shadow = types.ModuleType("tools.handlers")
        shadow.__file__ = str(overlay)
        native = types.ModuleType("tools.other")
        native.__file__ = str(install / "tools" / "__init__.py")
        os.utime(overlay, (NOW - 2 * DAY, NOW - 2 * DAY))
        os.utime(upstream, (NOW - DAY, NOW - DAY))   # an update replaced Hermes' file after the overlay was written
        snapshot = drift.runtime_overlays(install, modules={"tools": package, "tools.handlers": shadow,
                                                            "tools.other": native}, now=NOW)
        self.assertEqual([item["module"] for item in snapshot["overlays"]], ["tools.handlers"])
        self.assertEqual(snapshot["paths"], [{"package": "tools", "folders": [str(overlay_dir)]}])
        snapshot["loaded_at"] = NOW - DAY / 2   # the update restarted the gateway with the old overlay
        self.assertEqual(drift.assess(snapshot)[0]["state"], "stale")
        reading = {"overlays": drift.assess(snapshot), "update": {}}
        self.assertIn("drift_overlay_stale", drift.findings(reading))
        self.assertIn("Sobreposição desatualizada", "\n".join(drift.lines(reading, "pt")))
        os.utime(overlay, (NOW, NOW))   # rebuilt after the gateway loaded it
        self.assertEqual(drift.assess(snapshot)[0]["state"], "reload")
        snapshot["loaded_at"] = NOW + 10   # the gateway restarted since
        self.assertEqual(drift.assess(snapshot)[0]["state"], "ok")

    def test_the_snapshot_is_written_once_an_hour_and_keeps_the_first_upstream(self):
        taken = {"at": NOW, "pid": os.getpid(), "loaded_at": NOW, "install": "/x", "paths": [],
                 "overlays": [{"module": "tools.a", "overlay": "/o/a.py", "upstream": "/x/tools/a.py",
                               "upstream_exists": True, "overlay_sha": "1", "upstream_sha": "u1"}]}
        with patch.object(drift, "runtime_overlays", return_value=json.loads(json.dumps(taken))):
            drift.write_snapshot(self.home, now=NOW)
        changed = json.loads(json.dumps(taken))
        changed["overlays"][0]["upstream_sha"] = "u2"
        with patch.object(drift, "runtime_overlays", return_value=changed):
            self.assertEqual(drift.write_snapshot(self.home, now=NOW + 60)["overlays"][0]["upstream_sha"], "u1")
            later = drift.write_snapshot(self.home, now=NOW + 2 * 3600)
        self.assertEqual((later["overlays"][0]["upstream_sha"], later["overlays"][0]["first_upstream_sha"]), ("u2", "u1"))

    @GIT
    def test_stashes_left_by_updates_say_what_is_back(self):
        repo = self.base / "agent"
        repo.mkdir()
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull}

        def git(*args):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)

        git("init", "-q")
        (repo / "kept.py").write_text("base line one\n")
        (repo / "lost.py").write_text("base line two\n")
        git("add", ".")
        git("commit", "-qm", "base")
        (repo / "kept.py").write_text("base line one\nlocal change that came back\n")
        (repo / "lost.py").write_text("base line two\nlocal change that was lost\n")
        git("stash", "push", "-q", "-m", "hermes-update-autostash-20261101-101010")
        (repo / "kept.py").write_text("base line one\nlocal change that came back\n")   # re-applied by hand
        from hermes_atlas_evolution.system_update import Host

        host = Host()
        [item] = drift.stashes(repo, host, now=time.time() + 30 * DAY)
        self.assertEqual((item["back"], item["missing"], item["old"]), (["kept.py"], ["lost.py"], True))
        local = drift.local_changes(repo, host)
        self.assertEqual((local["modified"], local["count"]), (["kept.py"], 1))
        reading = {"stashes": [item], "update": {"local_changes": "parked", "stash": "abc"}, "install": str(repo)}
        self.assertEqual(drift.findings(reading), ["drift_update_parked", "drift_stash_missing"])
        self.assertIn("stash show -p stash@{0}", "\n".join(drift.lines(reading, "en")))

    def test_a_profile_behind_the_configuration_schema(self):
        install = self.base / "agent"
        (install / "hermes_cli").mkdir(parents=True)
        (install / "hermes_cli" / "config_defaults.py").write_text('DEFAULT = {\n    "_config_version": 55,\n}\n')
        (self.home / "config.yaml").write_text("model: x\n_config_version: 54\n")
        (self.home / "profiles" / "work").mkdir(parents=True)
        (self.home / "profiles" / "work" / "config.yaml").write_text("_config_version: 55\n")
        latest = drift.latest_config_version(install)
        self.assertEqual(drift.config_versions(self.home, latest), [{"profile": "default", "stamp": 54, "latest": 55}])

    def test_a_reading_is_kept_until_a_new_receipt(self):
        (self.home / "config.yaml").write_text("x: 1\n")
        with experience(self.home) as conn:
            first = drift.cached(conn, self.home, now=NOW, install=None)
            self.assertEqual((first["at"], drift.cached(conn, self.home, now=NOW + 60, install=None)["at"]), (NOW, NOW))
            self.receipt()
            self.assertEqual(drift.cached(conn, self.home, now=NOW + 120, install=None)["at"], NOW + 120)


# ── Hermes' warnings in its log ─────────────────────────────────────────────────────────────────────────────────
LOG = """2026-11-24 19:47:45,237 WARNING gateway.run: Agent cache pressure: anon RSS 1372MB over budget 1331MB but no evictable session (1 cached) — memory will keep climbing
2026-11-25 11:20:28,177 WARNING gateway.run: Agent cache pressure: anon RSS 1846MB over budget 1331MB — evicting 1 LRU session(s): agent:x
2026-11-25 08:03:19,079 WARNING tools.browser_supervisor: CDP supervisor local-6bf7: connect failed (attempt 2/5): [Errno 111] Connect call failed ('127.0.0.1', 9231)
2026-11-25 11:11:24,185 WARNING hermes_cli.backup: automatic backup phase=archive status=incomplete duration_ms=539354.2 files=53938 errors=3 bytes=9021627927 salvage=/x
2026-11-25 11:52:17,291 WARNING hermes_cli.web_server: event loop stalled 26.9s (GIL pressure suspected)
2026-11-25 11:13:12,712 INFO hermes_cli.plugin_capabilities: capability_check plugin=hermes-atlas-evolution capability=tools.override decision=deny checked_by=plugin_capability_granted evidence=not granted
2026-11-25 11:30:13,132 WARNING hermes_state: state.db connection for /x/state.db was closed while a read was still in flight — reopening (teardown/worker race, #94736)
2026-11-25 11:15:31,224 WARNING hermes.security_audit:   [security 1/1] Running as ROOT. The agent's terminal tools
2026-11-25 11:16:00,000 INFO some.module: a line that carries none of them
2026-11-01 10:00:00,000 WARNING gateway.run: Agent cache pressure: anon RSS 9999MB over budget 1331MB — old
"""


class LogSignalsTest(Scratch):
    def test_each_kind_is_counted_with_numbers_only(self):
        (self.home / "logs").mkdir()
        (self.home / "logs" / "agent.log").write_text(LOG)
        now = time.mktime(time.strptime("2026-11-25 12:00", "%Y-%m-%d %H:%M"))
        with experience(self.home) as conn:
            result = logsignals.ingest(self.home, conn, now=now)
            found = logsignals.summary(conn, now=now)
            stored = conn.execute("SELECT group_concat(detail, ' ') FROM log_signal_daily").fetchone()[0]
        self.assertEqual(result["lines"], 8)   # the 1 Nov line is outside the window
        self.assertEqual((found["memory_pressure"]["count"], found["memory_pressure"]["peak"],
                          found["memory_pressure"]["detail"]["stuck"]), (2, 1846.0, 1))
        self.assertEqual(found["browser_unreachable"]["detail"]["port"], ["9231"])
        self.assertEqual(found["backup_incomplete"]["peak"], 9021627927.0)
        self.assertEqual(found["loop_stall"]["peak"], 26.9)
        self.assertEqual(found["capability_denied"]["detail"]["capability"], ["tools.override"])
        self.assertNotIn("/x", stored)   # no path, no line of the log
        self.assertEqual(logsignals.findings(found), ["log_backup_incomplete", "log_memory_pressure", "log_loop_stall"])
        text = "\n".join(logsignals.lines(found, "pt"))
        self.assertIn("padrão seguro", text)   # capability_check deny explained as harmless
        self.assertIn("#94736", text)

    def test_the_review_lines_still_count_after_the_refactor(self):
        (self.home / "logs").mkdir()
        (self.home / "logs" / "agent.log").write_text(
            "2026-11-25 10:00:00,000 INFO agent.background_review: Background review complete: thread=t1 calls=2 in=10 "
            "out=5 cache_read=1 result=skill\n")
        with experience(self.home) as conn:
            result = logwatch.ingest_reviews(self.home, conn)
        self.assertEqual(result["reviews"], 1)


# ── the gateway's memory ────────────────────────────────────────────────────────────────────────────────────────
class FakeHost:
    def __init__(self, files=None, show=""):
        self.files, self.show, self.calls = files or {}, show, []

    def read(self, name, limit=65_536):
        return self.files.get(name)

    def run(self, argv, timeout=60):
        self.calls.append(argv)
        return (0, self.show, "") if "show" in argv else (1, "", "")


class GatewayMemoryTest(Scratch):
    def test_a_budget_below_the_peak_suggests_a_higher_memory_high(self):
        (self.home / "gateway.pid").write_text(json.dumps({"pid": os.getpid()}))
        host = FakeHost({f"/proc/{os.getpid()}/status": "VmRSS:\t 720896 kB\nRssAnon:\t 655360 kB\n",
                         "/proc/meminfo": "MemTotal:       12241920 kB\nMemAvailable:    7097344 kB\n"},
                        show="MemoryHigh=2147483648\nMemoryMax=3221225472\nMemoryCurrent=1369440256\n"
                             "MemoryPeak=2148532224\nLoadState=loaded\n")
        signals = {"memory_pressure": {"count": 17, "days": 5, "peak": 1846.0, "detail": {"stuck": 4}}}
        found = gateway_memory.reading(self.home, host=host, signals=signals, config={})
        self.assertEqual((found["budget_mb"], found["memory_high_mb"], found["rss_mb"]), (1331, 2048, 704))
        self.assertEqual(found["suggestion"]["kind"], "unit")
        self.assertGreaterEqual(found["suggestion"]["budget_after_mb"], 1846 * gateway_memory.HEADROOM)
        text = "\n".join(gateway_memory.lines(found, "pt"))
        self.assertIn("MemoryHigh=", text)
        self.assertIn("nenhuma conversa podia sair", text)

    def test_no_room_on_the_machine_suggests_a_smaller_cache(self):
        found = {"events": 3, "peak_mb": 1800, "budget_mb": 1000, "budget_setting": "auto", "machine_mb": 4096}
        self.assertEqual(gateway_memory.suggest(found)["kind"], "cache")
        self.assertIsNone(gateway_memory.suggest({**found, "budget_mb": 3000}))
        self.assertEqual(gateway_memory.suggest({**found, "budget_setting": "fixed"})["kind"], "budget")

    def test_the_setting_as_hermes_reads_it(self):
        self.assertEqual(gateway_memory.configured_budget({}), ("auto", None))
        self.assertEqual(gateway_memory.configured_budget({"agent": {"agent_cache": {"memory_high_mb": 1500}}}),
                         ("fixed", 1500))
        self.assertEqual(gateway_memory.configured_budget({"agent": {"agent_cache": {"memory_high_mb": "off"}}}),
                         ("off", None))


class DoctorRoomTest(Scratch):
    def test_a_check_run_by_a_gateway_counts_the_room_under_its_limit(self):
        from hermes_atlas_evolution import doctor

        folder = self.base / "cg" / "app.slice" / "hermes-gateway.service"
        folder.mkdir(parents=True)
        (folder / "memory.high").write_text("2147483648\n")
        (folder / "memory.max").write_text("3221225472\n")
        (folder / "memory.current").write_text(str(1_900 * 2**20) + "\n")
        room = doctor._cgroup_room(str(self.base / "cg"), own="/app.slice/hermes-gateway.service")
        self.assertEqual(room, 2147483648 - 1_900 * 2**20)
        self.assertLess(room, doctor.MEMORY_FLOOR)   # the check waits for the next pass
        (folder / "memory.high").write_text("max\n")
        (folder / "memory.max").write_text("max\n")
        self.assertIsNone(doctor._cgroup_room(str(self.base / "cg"), own="/app.slice/hermes-gateway.service"))


# ── every profile in one view, Auto for all of them ─────────────────────────────────────────────────────────────
class FleetViewTest(Fleet):
    def database(self, home: Path, *, last=None, lessons_rows=0, tool=("terminal", 0)):
        with experience(home) as conn:
            if last is not None:
                conn.execute("INSERT OR REPLACE INTO cursor VALUES ('last_cycle', ?)", (repr(last),))
                conn.execute("INSERT OR REPLACE INTO cursor VALUES ('loop_status', ?)",
                             (json.dumps({"at": last, "ok": True, "seconds": 0.2}),))
            for index in range(lessons_rows):
                conn.execute("""INSERT INTO lesson(signature, tool, klass, hint, origin, status, created, updated)
                    VALUES (?, 'terminal', 'timeout', '', 'derived', 'candidate', ?, ?)""", (f"s{index}", NOW, NOW))
            if tool[1]:
                conn.execute("INSERT INTO tool_daily VALUES (?, ?, ?, 1, 0, 0)", (utc_day(NOW), tool[0], tool[1]))

    def test_a_profile_in_use_whose_pass_stopped_is_late(self):
        work = self.profile("work", enabled=["hermes-atlas-evolution"])
        plugin(work, "hermes-atlas-evolution")
        self.database(self.root, last=NOW - 600)
        self.database(work, last=NOW - 7 * DAY, lessons_rows=2, tool=("terminal", 30))
        view = fleet.overview(self.root, now=NOW)
        rows = {row["profile"]: row for row in view["profiles"]}
        self.assertEqual((rows["default"]["overdue"], rows["work"]["overdue"]), (False, True))
        self.assertEqual(rows["work"]["lessons_waiting"], 2)
        self.assertIn("fleet_pass_overdue", view["attention"])
        text = fleet.markdown(view, "pt")
        self.assertIn("| work |", text)
        self.assertIn("rodada atrasada em 1", text)

    def test_auto_for_all_and_back(self):
        work = self.profile("work", enabled=["hermes-atlas-evolution"])
        plugin(work, "hermes-atlas-evolution")
        self.profile("other", enabled=[])
        written = []

        def writer_for(home):   # admin.write_settings writes one key at a time
            def write(values):
                written.append((Path(home).name, dict(values)))
                return list(values)
            return write

        def merged(name):
            found = {}
            for profile, values in written:
                if profile == name:
                    found.update(values)
            return found

        current = {"mode": "assist", "lessons": "suggest", "autopilot_minutes": 0, "owner_platforms": [],
                   "owner_sender_ids": []}
        with patch("hermes_atlas_evolution.admin.current_settings", side_effect=lambda home, config: dict(current)):
            result = fleet.enable(self.root, now=NOW, writer_for=writer_for)
            states = {item["profile"]: item["status"] for item in result["results"]}
            self.assertEqual((states["default"], states["work"], states["other"]), ("changed", "changed", "not_installed"))
            self.assertEqual(merged("work"), {"mode": "auto", "lessons": "auto", "autopilot_minutes": 60})
            state = fleet.auto_state(self.root)
            self.assertTrue(state["on"])
            self.assertEqual(state["before"]["work"], {"mode": "assist", "lessons": "suggest", "autopilot_minutes": 0})
            current.update(mode="auto", lessons="auto", autopilot_minutes=60)
            written.clear()
            back = fleet.disable(self.root, now=NOW + 60, writer_for=writer_for)
        self.assertEqual({item["status"] for item in back["results"]}, {"restored"})
        self.assertEqual(merged("work"), {"mode": "assist", "lessons": "suggest", "autopilot_minutes": 0})
        self.assertFalse(fleet.auto_state(self.root)["on"])

    def test_a_profile_that_opted_out_is_left_alone(self):
        work = self.profile("work", enabled=["hermes-atlas-evolution"],
                            entries={"hermes-atlas-evolution": {"settings": {"fleet_managed": False}}})
        plugin(work, "hermes-atlas-evolution")
        with patch("hermes_atlas_evolution.admin.current_settings", return_value={"mode": "assist"}):
            result = fleet.enable(self.root, now=NOW, profiles=["work"], writer_for=lambda home: (lambda values: []))
        self.assertEqual(result["results"], [{"profile": "work", "status": "opted_out"}])

    def test_a_proven_lesson_is_offered_to_the_other_profiles_in_auto(self):
        work = self.profile("work", enabled=["hermes-atlas-evolution"],
                            entries={"hermes-atlas-evolution": {"settings": {"mode": "auto"}}})
        plugin(work, "hermes-atlas-evolution")
        self.configs[str(self.root.resolve())]["plugins"]["entries"] = {
            "hermes-atlas-evolution": {"settings": {"mode": "auto"}}}
        with experience(self.root) as conn:
            conn.execute("""INSERT INTO lesson(signature, tool, klass, hint, origin, status, created, updated, activated,
                  verdict) VALUES ('x', 'terminal', 'timeout', '', 'derived', 'active', ?, ?, ?, 'effective')""",
                         (NOW, NOW, NOW))
            conn.execute("INSERT INTO cursor VALUES (?, ?)", (fleet.AUTO_KEY, json.dumps({"on": True})))
        self.database(work, tool=("terminal", 10))
        shared = fleet.share_lessons(self.root, now=NOW)
        again = fleet.share_lessons(self.root, now=NOW + 60)
        with experience(work) as conn:
            row = conn.execute("SELECT status, tool, klass FROM lesson").fetchone()
            source = conn.execute("SELECT source FROM lesson_import").fetchone()[0]
        self.assertEqual(([item["profile"] for item in shared], again), (["work"], []))
        self.assertEqual((tuple(row), source), (("candidate", "terminal", "timeout"), "fleet"))

    def test_the_daily_report_runs_no_agent(self):
        spec = fleet.report_spec(self.root, hour=7, lang="pt", deliver="local")
        self.assertIn("--no-agent", spec["create_args"])
        self.assertEqual(spec["create_args"][spec["create_args"].index("create") + 1], "0 7 * * *")
        self.assertIn("'evolution', 'fleet', 'report'", spec["script_content"])
        compile(spec["script_content"], "script", "exec")
        with self.assertRaises(ValueError):
            fleet.report_spec(self.root, hour=24)

    def test_scheduling_twice_keeps_one_job(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout="Created job: abc123\n", stderr="")

        with patch("hermes_atlas_evolution.admin.hermes_executable", return_value="/usr/bin/hermes"), \
                patch("hermes_atlas_evolution.admin.run_command", runner):
            first = fleet.schedule(self.root, now=NOW, lang="pt")
            (self.root / "cron").mkdir(exist_ok=True)
            (self.root / "cron" / "jobs.json").write_text(json.dumps({"jobs": [{"id": "abc123", "name": fleet.JOB_NAME}]}))
            second = fleet.schedule(self.root, now=NOW + 60, lang="pt")
        self.assertEqual((first["scheduled"], first["job"], second["reason"]), (True, "abc123", "already"))
        self.assertEqual(len(calls), 1)
        self.assertTrue((self.root / "scripts" / fleet.SCRIPT_NAME).is_file())

    def test_auto_status_says_what_it_does_and_never_does(self):
        text = fleet.auto_markdown(self.root, "pt")
        self.assertIn("O que ele nunca faz", text)
        self.assertIn("hermes evolution care undo-gateway", text)
        self.assertIn("usa a rede ou manda telemetria", text)


class FleetCommandTest(Fleet):
    def run_cli(self, *argv, env=None):
        import argparse
        import contextlib
        import io

        from hermes_atlas_evolution import cli

        parser = argparse.ArgumentParser()
        cli.setup(parser)
        args = parser.parse_args(list(argv))
        out = io.StringIO()
        with patch.dict(os.environ, env or {}), contextlib.redirect_stdout(out):
            cli.execute(args, self.root, runtime_config={})
        return out.getvalue()

    def test_reading_the_fleet_is_open_and_switching_auto_is_the_owners(self):
        self.assertIn("default", json.loads(self.run_cli("fleet", "--format", "json"))["lead"])
        with self.assertRaises(SystemExit) as refused:
            self.run_cli("fleet", "auto", "on", "--yes", env={"ATLAS_EVOLUTION_AGENT": "1"})
        self.assertIn("operator", str(refused.exception))
        self.assertIn("Auto", self.run_cli("fleet", "auto", "--lang", "pt"))


class FleetPanelTest(Fleet):
    def test_the_fleet_tab_reads_every_profile_and_the_switch_is_a_decision(self):
        from test_dashboard import load_api

        api = load_api()
        self.addCleanup(lambda: api._module("operations").close_observers())
        work = self.profile("work", enabled=["hermes-atlas-evolution"])
        plugin(work, "hermes-atlas-evolution")
        with patch.object(api._module("environment"), "_read_config",
                          side_effect=lambda home: self.configs.get(str(Path(home).resolve()))), \
                patch(api.PACKAGE + ".status._sessions", side_effect=lambda home, since: 5):
            view = api.fleet_view(self.root, now=NOW, lang="pt")
            self.assertIn("| work |", view["fleet_md"])
            self.assertIn("O que ele nunca faz", view["auto_md"])
            self.assertFalse(view["auto"].get("on"))
            calls = []
            with patch(api.PACKAGE + ".admin.current_settings", return_value={"mode": "assist", "lessons": "auto",
                                                                             "autopilot_minutes": 60}):
                result = api.decide(self.root, {}, {"kind": "fleet_auto", "on": True}, now=NOW, lang="pt",
                                    writer=lambda values: calls.append(values) or list(values))
        self.assertTrue(result["on"])
        self.assertIn({"mode": "auto"}, calls)
        script = (Path(api.__file__).parent / "dist" / "index.js").read_text(encoding="utf-8")
        self.assertIn('"fleet"', script)
        self.assertIn("fleet_alert_md", script)


# ── status says the real reason ─────────────────────────────────────────────────────────────────────────────────
class StatusFleetTest(Scratch):
    def test_a_running_gateway_that_claims_no_pass_is_named(self):
        (self.home / "config.yaml").write_text("x: 1\n")
        (self.home / "gateway_state.json").write_text(json.dumps({"pid": os.getpid(), "gateway_state": "running",
                                                                  "code_version": "0.23.4"}))
        old = time.time() - 9 * DAY
        with experience(self.home) as conn:
            conn.execute("INSERT INTO cursor VALUES ('loop_status', ?)", (json.dumps({"at": old, "ok": True}),))
            conn.execute("INSERT INTO cursor VALUES ('care_check', ?)",
                         (json.dumps({"at": old, "gateway": {"running": True, "version": "0.23.2"}, "findings": []}),))
            conn.execute("INSERT INTO cursor VALUES (?, ?)", (fleet.HEALTH_KEY, json.dumps({
                "at": old, "drift": {"findings": ["drift_overlay_stale"], "overlays": [
                    {"module": "tools.x", "overlay": "/o/x.py", "upstream": "/h/tools/x.py", "state": "stale"}]},
                "signals_findings": ["log_backup_incomplete"],
                "signals": {"backup_incomplete": {"count": 1, "days": 1, "peak": 9e9, "detail": {}}}})))
        with patch.object(status, "live_units", return_value=None):
            report = status.build(self.home, live=False)
            text = status.markdown(report, "pt")
        self.assertIn("hermes_pass_unclaimed", report["attention"])
        self.assertIn("drift_overlay_stale", report["attention"])
        self.assertIn(f"pid {os.getpid()}", text)
        self.assertIn("versão 0.23.4", text)   # the version it runs now, not the stored 0.23.2
        self.assertIn("Sobreposição desatualizada", text)
        self.assertIn("Backup antes da atualização incompleto", text)


if __name__ == "__main__":
    unittest.main()
