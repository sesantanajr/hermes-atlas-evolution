import json
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from hermes_evolution.learning import record_outcome, retrospective
from hermes_evolution.linux_actions import SystemPolicy, Systemd, Unit, attribute, pair_sample
from hermes_evolution.operations import close_observers, database
from hermes_evolution.supervisor import _journal, journal_history, supervise
from hermes_evolution.telemetry import ResearchTracker, research_summary

MIB = 1 << 20


def unit(name: str):
    return Unit(name, True, "user.slice", f"/user.slice/{name}", name, 0, {"8:0": 0},
                {"cpu": 22.0, "io": 12.0}, {"cpu": 0, "io": 100}, {"cpu": 100, "io": 100})


class FakeSystemd:
    def __init__(self):
        self.boot = "boot-one"
        self.units = {name: unit(name) for name in ("hermes.service", "index.service")}
        self.writes = []
        self.observed_prepared = False
        self.conn = None
        self.effectual = True

    def unit(self, scope, name):
        return self.units.get(name)

    @contextmanager
    def lock(self, scope):
        yield True

    def set_weight(self, scope, name, kind, value):
        self.writes.append((scope, name, kind, value))
        if value == 50 and self.conn is not None:
            self.observed_prepared = self.conn.execute("SELECT status FROM weight_trial").fetchone()[0] == "prepared"
        self.units[name].weight[kind] = value
        if self.effectual:
            self.units[name].effective[kind] = value if kind == "memory" else value or 100
        return True

    def advance_pair(self, *, pressure=22.0):
        self.units["hermes.service"].psi["cpu"] = pressure
        for name, rate in (("hermes.service", 300_000_000), ("index.service", 1_200_000_000)):
            self.units[name].cpu += rate * 120


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addCleanup(close_observers)
        self.home = Path(folder.name)
        self.backend = FakeSystemd()
        self.policy = SystemPolicy("apply", "user", "hermes.service", "index.service", ("cpu",))

    def _tick(self, when, **kwargs):
        with _journal(self.home) as conn:
            self.backend.conn = conn
            return supervise(conn, self.backend, self.policy, now=when, safe=True, **kwargs)

    def test_sustained_contention_trial_improves_then_restores_baseline(self):
        self.assertEqual(self._tick(1000)[1]["status"], "observing")
        self.backend.advance_pair()
        evidence, outcome = self._tick(1120)
        self.assertEqual(evidence[0]["share"], 0.8)
        self.assertEqual(outcome["status"], "active")
        self.assertTrue(self.backend.observed_prepared)
        self.assertEqual(self.backend.writes, [("user", "index.service", "cpu", 50)])
        self.backend.advance_pair(pressure=8.0)
        self.assertEqual(self._tick(1240)[1]["status"], "improved")
        self.assertEqual(self._tick(2100)[1]["reason"], "lease_expired")
        self.assertEqual(self.backend.writes[-1], ("user", "index.service", "cpu", 0))
        self.assertEqual(journal_history(self.home)["actions"][0]["status"], "rolled_back")

    def test_no_improvement_reverts_and_suppresses_repeat(self):
        self._tick(1000)
        self.backend.advance_pair()
        self._tick(1120)
        self.assertEqual(self._tick(1240)[1]["reason"], "insufficient_improvement")
        self._tick(6400)
        self.backend.advance_pair()
        self.assertEqual(self._tick(6520)[1]["reason"], "diagnosis_gate_or_ineffective_previous_trial")
        self.assertEqual(len(self.backend.writes), 2)

    def test_quieter_host_after_missed_checks_does_not_credit_the_action(self):
        self._tick(1000)
        self.backend.advance_pair()
        self._tick(1120)
        self.backend.advance_pair(pressure=8.0)
        self.assertEqual(self._tick(2100)[1]["reason"], "lease_expired")
        with _journal(self.home) as conn:
            learned = retrospective(conn, now=2100)
        self.assertEqual(learned["action_outcomes"], {"rebalance_cpu": {"inconclusive": 1}})

    def test_external_change_is_preserved_and_unverifiable_effect_is_reverted(self):
        self._tick(1000)
        self.backend.advance_pair()
        self._tick(1120)
        self.backend.units["index.service"].weight["cpu"] = 70
        self.assertEqual(self._tick(1240, rollback=True)[1]["status"], "released")
        self.assertEqual(len(self.backend.writes), 1)
        other = self.backend.units["index.service"]
        other.weight["cpu"], other.effective["cpu"] = 100, 100
        self._tick(6400)
        self.backend.advance_pair()
        self.backend.effectual = False
        self.assertEqual(self._tick(6520)[1]["reason"], "effective_weight_unverified")
        self.assertEqual(self.backend.writes[-1][-1], 100)

    def test_observe_mode_and_diagnosis_gate_do_not_mutate_linux(self):
        self.policy = replace(self.policy, mode="observe")
        self._tick(1000)
        self.backend.advance_pair()
        self.assertEqual(self._tick(1120)[1]["reason"], "observe_mode")
        self.policy = replace(self.policy, mode="apply")
        self.backend.advance_pair()
        self.assertEqual(self._tick(1240, allow_kind=lambda candidate: False)[1]["status"], "skipped")
        self.assertEqual(self.backend.writes, [])

    def test_service_restart_invalidates_comparison_and_restores_weight(self):
        self._tick(1000)
        self.backend.advance_pair()
        self._tick(1120)
        self.backend.units["hermes.service"].invocation = "restarted"
        self.backend.advance_pair(pressure=8.0)
        self.assertEqual(self._tick(1240)[1]["reason"], "service_replaced")
        self.assertEqual(self.backend.writes[-1][-1], 0)

    def test_systemd_reader_distinguishes_unset_from_unavailable(self):
        adapter = Systemd()
        adapter.cgroup = self.home
        root = self.home / "user.slice" / "index.service"
        root.mkdir(parents=True)
        (root / "cpu.weight").write_text("100\n", encoding="ascii")
        (root / "cpu.pressure").write_text("some avg10=25.00 avg60=20.00 total=99\n", encoding="ascii")
        fields = "\n".join(["Id=index.service", "Slice=user.slice", "ControlGroup=/user.slice/index.service",
                             "LoadState=loaded", "ActiveState=active", "SubState=running", "InvocationID=example",
                             "CPUWeight=[not set]", "CPUUsageNSec=123"])
        with patch.object(adapter, "_call", return_value=fields) as called:
            service = adapter.unit("user", "index.service")
            self.assertIn("--all", called.call_args.args[1])
        self.assertEqual(service.weight, {"cpu": 0, "io": None, "memory": None})
        self.assertEqual(service.effective, {"cpu": 100, "io": None, "memory": None})
        self.assertEqual(service.psi["cpu"], 25.0)

    def test_memory_runtime_writes_only_aligned_absolute_soft_limit_or_restore(self):
        adapter = Systemd()
        with patch.object(adapter, "_call", return_value="") as command:
            self.assertTrue(adapter.set_weight("user", "index.service", "memory", 896 * MIB))
            self.assertEqual(command.call_args.args, ("user", ["--runtime", "set-property", "--", "index.service",
                                                                "MemoryHigh=939524096"]))
            self.assertTrue(adapter.set_weight("user", "index.service", "memory", 0))
            self.assertEqual(command.call_args.args[1][-1], "MemoryHigh=infinity")
            with self.assertRaises(ValueError):
                adapter.set_weight("user", "index.service", "memory", 896 * MIB + 1)
        self.assertEqual(command.call_count, 2)

    def _memory_scenario(self, *, target=896 * MIB):
        self.policy = replace(self.policy, resources=("memory",), memory_high_bytes=target)
        gateway, peer = self.backend.units.values()
        gateway.psi["memory"], peer.psi["memory"] = 8.0, 2.0
        for service in (gateway, peer):
            service.weight["memory"] = service.effective["memory"] = 0
            service.memory.update(swap=0, oom=0, oom_kill=0, max=0, shmem=16 * MIB)
        gateway.memory.update(current=256 * MIB, anon=128 * MIB, inactive_clean_file=80 * MIB)
        peer.memory.update(current=1024 * MIB, anon=384 * MIB, inactive_clean_file=512 * MIB)
        return gateway, peer

    def test_memory_trial_uses_paired_pressure_and_restores_verified_soft_limit(self):
        gateway, peer = self._memory_scenario()
        self.assertEqual(self._tick(1000)[1]["status"], "observing")
        self.assertEqual(self.backend.writes, [])
        evidence, started = self._tick(1120)
        self.assertEqual(evidence[0]["background_inactive_clean_file_bytes"], 512 * MIB)
        self.assertEqual((started["status"], started["property"], started["prior_limit"]),
                         ("active", "MemoryHigh", "unlimited"))
        self.assertEqual(self.backend.writes[-1], ("user", "index.service", "memory", 896 * MIB))
        gateway.psi["memory"] = 2.0
        self.assertEqual(self._tick(1240)[1]["status"], "improved")
        self.assertEqual(self._tick(2100)[1]["reason"], "lease_expired")
        self.assertEqual(self.backend.writes[-1], ("user", "index.service", "memory", 0))
        self.assertEqual(peer.weight["memory"], 0)
        with _journal(self.home) as conn:
            learned = retrospective(conn, now=2100)
        self.assertEqual(learned["action_outcomes"], {"rebalance_memory": {"improved": 1}})

    def test_memory_trial_refuses_existing_limit_swapping_or_missing_file_headroom(self):
        gateway, peer = self._memory_scenario()
        self._tick(1000)
        peer.weight["memory"] = 900 * MIB
        self.assertEqual(self._tick(1120)[0], [])
        peer.weight["memory"] = 0
        gateway.memory["swap"] = 3 * MIB
        self.assertEqual(self._tick(1240)[0], [])
        gateway.memory["swap"] = 0
        peer.memory["inactive_clean_file"] = 64 * MIB
        self.assertEqual(self._tick(1360)[0], [])
        self.assertEqual(self.backend.writes, [])

    def test_memory_trial_unwinds_when_peer_starts_swapping(self):
        _, peer = self._memory_scenario()
        self._tick(1000)
        self._tick(1120)
        peer.memory["swap"] = MIB
        outcome = self._tick(1240)[1]
        self.assertEqual((outcome["status"], outcome["reason"]), ("rolled_back", "memory_trial_guard_changed"))
        self.assertEqual(self.backend.writes[-1], ("user", "index.service", "memory", 0))

    def test_repeated_failed_trials_only_defer_later_independently_approved_candidates(self):
        now = 130_000
        with _journal(self.home) as conn:
            record_outcome(conn, "rebalance_cpu", 101, "ineffective", now=now - 48_000)
            record_outcome(conn, "rebalance_cpu", 102, "ineffective", now=now - 1_000)
        self._tick(now)
        self.backend.advance_pair()
        deferred = self._tick(now + 120)[1]
        self.assertEqual((deferred["reason"], deferred["ineffective_intervals"]), ("learned_action_cooldown", 2))
        self.assertEqual(self.backend.writes, [])

    def test_io_requires_common_device_and_pair_identity(self):
        hermes, other = self.backend.units.values()
        start = pair_sample(hermes, other, self.backend.boot, 1000)
        # Snapshots read by production are copied through JSON in the journal.
        start = json.loads(json.dumps(start))
        hermes.io["8:0"] = 120 * 1000
        other.io["8:1"] = 120 * 4_000_000
        current = pair_sample(hermes, other, self.backend.boot, 1120)
        self.assertEqual(attribute(start, current, ("io",)), [])
        other.io["8:0"] = 120 * 4_000_000
        current = pair_sample(hermes, other, self.backend.boot, 1120)
        self.assertEqual(attribute(start, current, ("io",))[0]["kind"], "io")
        self.assertEqual(attribute(start, {**current, "boot": "new"}, ("io",)), [])

    def test_bad_policy_rejects_unit_aliases_and_shell_metacharacters(self):
        for unit_name in ("--help.service", "a.service; touch x", "hermes.service", "batch@.service/path"):
            with self.assertRaises(ValueError):
                SystemPolicy.read({"linux_supervisor": {"mode": "apply", "hermes_unit": "hermes.service",
                                                       "background_unit": unit_name}})

    def test_memory_requires_an_explicit_streamlined_limit(self):
        base = {"resources": ["memory"], "hermes_unit": "hermes.service", "background_unit": "index.service"}
        for invalid in (None, "512MiB", 127 * MIB, 512 * MIB + 1, False, 2 ** 63):
            with self.assertRaises(ValueError):
                SystemPolicy.read({"linux_supervisor": {**base, "memory_high_bytes": invalid}})
        config = SystemPolicy.read({"linux_supervisor": {**base, "memory_high_bytes": 512 * MIB}})
        self.assertEqual(config.memory_high_bytes, 512 * MIB)

    def test_research_turn_metrics_and_schema_upgrade_keep_legacy_state(self):
        with database(self.home) as conn:
            conn.execute("INSERT INTO control VALUES ('legacy','true')")
            conn.execute("DROP TABLE research_latency")
            conn.execute("PRAGMA user_version=1")
        turn = {"turn_id": "turn-secret", "user_message": "raw-secret"}
        tracker = ResearchTracker(self.home)
        tracker.start(turn, clock=4)
        for _ in range(2):
            tracker.tool({**turn, "tool_name": "web_search", "tool_call_id": "id-secret", "status": "error"})
        tracker.tool({**turn, "tool_name": "terminal", "status": "ok"})
        tracker.finish(turn, clock=134, now=2000)
        tracker.finish(turn, clock=135, now=2000)
        with database(self.home) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(conn.execute("SELECT value FROM control WHERE key='legacy'").fetchone()[0], "true")
            measured = research_summary(conn, now=2000, days=1)
            self.assertEqual((measured["turns"], measured["slow_turns"], measured["tool_errors"]), (1, 1, 1))
            self.assertEqual(measured["p95_ms"], 130_000)
            self.assertNotIn("-secret", "\n".join(conn.iterdump()))
