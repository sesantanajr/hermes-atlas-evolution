import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from hermes_evolution.linux_host import scan_host


class LinuxHostTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.proc = self.base / "proc"
        self.sys = self.base / "sys"
        self.home = self.base / "profile"
        self.home.mkdir()

    def write(self, path, content):
        target = self.base / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def pressure(self, path, some, full=0):
        self.write(path, f"some avg10={some + 1:.2f} avg60={some:.2f} avg300=1.00 total=123456\n"
                         f"full avg10={full + 1:.2f} avg60={full:.2f} avg300=0.00 total=1234\n")

    def scan(self):
        return scan_host(self.home, proc=self.proc, sys=self.sys)

    def test_cache_and_historical_swap_without_pressure_are_not_faults(self):
        self.write("proc/meminfo", "MemTotal: 16000 kB\nMemAvailable: 10000 kB\nMemFree: 100 kB\n"
                                  "Cached: 8500 kB\nBuffers: 400 kB\nShmem: 500 kB\nSReclaimable: 20 kB\n"
                                  "SwapTotal: 2000 kB\nSwapFree: 300 kB\n")
        self.write("proc/vmstat", "pswpin 3\npswpout 18\npgmajfault 25\nunknown 99\n")
        self.pressure("proc/pressure/memory", 0)
        report = self.scan()
        self.assertEqual(report["memory"]["page_cache_bytes"], 8400 * 1024)
        self.assertEqual(report["memory"]["available_bytes"], 10000 * 1024)
        self.assertEqual(report["memory"]["swap_used_bytes"], 1700 * 1024)
        self.assertEqual(report["memory"]["vm_counters"], {"pswpin": 3, "pswpout": 18, "pgmajfault": 25})
        self.assertFalse(any(item["id"] in {"host_memory_reclaim_pressure", "host_swap_pressure"}
                             for item in report["findings"]))

    def test_actual_stall_and_inode_exhaustion_have_direct_evidence(self):
        self.write("proc/meminfo", "MemTotal: 1000 kB\nMemAvailable: 70 kB\nSwapTotal: 100 kB\nSwapFree: 40 kB\n")
        self.write("proc/loadavg", "2.15 1.30 0.70 1/122 500\n")
        self.pressure("proc/pressure/memory", 7.75)
        self.pressure("proc/pressure/cpu", 20)
        self.pressure("proc/pressure/io", 15, 5)
        disk = SimpleNamespace(f_blocks=1000, f_frsize=1024, f_bavail=90, f_files=100, f_favail=7)
        with patch("hermes_evolution.linux_host.os.statvfs", return_value=disk, create=True):
            report = self.scan()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["pressure"]["io"]["some"]["avg10"], 16.0)
        self.assertEqual(report["load"]["five_minutes"], 1.3)
        findings = {item["id"]: item for item in report["findings"]}
        self.assertEqual(set(findings), {"host_memory_reclaim_pressure", "host_swap_pressure", "host_io_pressure",
                                         "host_cpu_pressure", "host_disk_inodes_low", "host_disk_capacity_low"})
        self.assertEqual(findings["host_memory_reclaim_pressure"]["confidence"], "corroborated")
        self.assertEqual(findings["host_disk_inodes_low"]["evidence"]["available_fraction"], 0.07)

    def test_gateway_identity_gate_and_ancestor_quota(self):
        self.write("profile/gateway.pid", '{"kind":"hermes-gateway","pid":42,"start_time":77}')
        self.write("proc/42/stat", "42 (agent process) S " + "0 " * 18 + "77 0 0\n")
        self.write("proc/42/status", "Name:\tprivate-name\nVmRSS: 1360 kB\nRssAnon: 1300 kB\nVmSwap: 2 kB\n")
        self.write("proc/42/io", "read_bytes: 20000\nwrite_bytes: 19\n")
        self.write("proc/42/cgroup", "0::/private-group/gateway\n")
        self.write("proc/self/cgroup", "0::/other\n")
        self.write("sys/fs/cgroup/private-group/cpu.max", "200000 100000\n")
        self.write("sys/fs/cgroup/private-group/memory.max", "2000\n")
        self.write("sys/fs/cgroup/private-group/memory.current", "1980\n")
        self.write("sys/fs/cgroup/private-group/gateway/cpu.max", "max 100000\n")
        self.write("sys/fs/cgroup/private-group/gateway/memory.max", "max\n")
        self.write("sys/fs/cgroup/private-group/gateway/memory.current", "500\n")
        self.write("sys/fs/cgroup/private-group/gateway/memory.stat", "anon 230\nfile 245\n")
        self.write("sys/fs/cgroup/private-group/gateway/memory.events", "oom 1\noom_kill 0\nhigh 9\n")
        self.write("sys/fs/cgroup/private-group/gateway/cpu.stat", "nr_throttled 30\nthrottled_usec 399\n")
        self.pressure("sys/fs/cgroup/private-group/gateway/memory.pressure", 4)
        report = self.scan()
        self.assertEqual(report["cgroup"]["scope"], "gateway")
        self.assertEqual(report["cgroup"]["quota_cpus"], 2.0)
        self.assertIsNone(report["cgroup"]["memory_max_bytes"])
        self.assertEqual(report["cgroup"]["limiting_memory_group"]["ancestor_levels"], 1)
        self.assertEqual(report["cgroup"]["memory_events"]["oom"], 1)
        self.assertEqual(report["cgroup"]["cpu_nr_throttled"], 30)
        self.assertEqual(report["processes"]["gateway"]["memory"]["anonymous_bytes"], 1300 * 1024)
        self.assertIn("host_cgroup_memory_pressure", {item["id"] for item in report["findings"]})
        self.assertNotIn("private-", json.dumps(report))
        self.write("profile/gateway.pid", '{"kind":"hermes-gateway","pid":42,"start_time":88}')
        report = self.scan()
        self.assertEqual(report["processes"]["gateway"], {"status": "unverified"})
        self.assertIsNone(report["cgroup"])

    def test_inventory_is_bounded_and_reports_only_known_roots(self):
        self.write("profile/cache/file-one", "abc")
        self.write("profile/cache/file-two", "de")
        self.write("profile/cache/file-three", "f")
        self.write("profile/private-folder/secret", "a long secret")
        with patch("hermes_evolution.linux_host._ENTRY_LIMIT", 2):
            report = self.scan()
        cache = next(area for area in report["storage"]["areas"] if area["area"] == "cache")
        self.assertEqual(cache["entries"], 2)
        self.assertEqual(cache["file_count"], 2)
        self.assertLess(cache["apparent_bytes"], 6)
        self.assertTrue(report["storage"]["truncated"])
        self.assertNotIn("file-one", json.dumps(report))
        self.assertNotIn("private-folder", json.dumps(report))

    def test_symlinks_do_not_cross_scope(self):
        self.write("outside/secret", "secret data")
        link = self.home / "cache"
        try:
            link.symlink_to(self.base / "outside", target_is_directory=True)
        except OSError:
            self.skipTest("symbolic links unavailable")
        report = self.scan()
        cache = next(area for area in report["storage"]["areas"] if area["area"] == "cache")
        self.assertEqual(cache["file_count"], 0)
        self.assertEqual(cache["status"], "partial")

    def test_malformed_metrics_and_cgroup_path_escape_are_ignored(self):
        self.write("proc/pressure/io", "\nsome avg10=nan avg60=1 avg300=0 total=50\n")
        self.write("proc/self/cgroup", "0::/../outside\n")
        self.write("sys/fs/outside/memory.max", "2000\n")
        report = self.scan()
        self.assertEqual(report["pressure"]["io"], {})
        self.assertIsNone(report["cgroup"])


if __name__ == "__main__":
    unittest.main()
