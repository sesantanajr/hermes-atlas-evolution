"""Narrow systemd/cgroup-v2 adapter for reversible resource weighting."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from contextlib import contextmanager
from pathlib import Path

KINDS = {"cpu": "CPUWeight", "io": "IOWeight", "memory": "MemoryHigh"}
THRESHOLDS = {"cpu": 10.0, "io": 5.0, "memory": 5.0}
_MIB = 1 << 20
_MIN_MEMORY_HIGH = 128 * _MIB
_MAX_MEMORY_HIGH = (1 << 63) - 1
_PEER_MEMORY_PRESSURE_LIMIT = 10.0
_UNIT_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.@-]{0,100}\.service\Z")
_FIELDS = ("Id", "LoadState", "ActiveState", "SubState", "Slice", "ControlGroup", "InvocationID", "CPUUsageNSec",
           "CPUWeight", "IOWeight", "MemoryHigh")


@dataclass(frozen=True)
class SystemPolicy:
    mode: str
    scope: str
    hermes_unit: str
    background_unit: str
    resources: tuple[str, ...]
    memory_high_bytes: int | None = None

    @classmethod
    def read(cls, settings: dict):
        value = settings.get("linux_supervisor")
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("linux_supervisor must be an object")
        mode, scope = value.get("mode", "observe"), value.get("scope", "user")
        hermes, background = value.get("hermes_unit"), value.get("background_unit")
        resources = value.get("resources", ["cpu"])
        if (mode not in ("observe", "apply") or scope not in ("user", "system") or hermes == background
                or not all(isinstance(unit, str) and _UNIT_NAME.fullmatch(unit) for unit in (hermes, background))
                or not isinstance(resources, list) or not resources
                or any(not isinstance(resource, str) or resource not in KINDS for resource in resources)):
            raise ValueError("linux_supervisor needs two distinct .service units, a valid scope, mode and resources")
        memory_high = value.get("memory_high_bytes") if "memory" in resources else None
        if "memory" in resources and not _valid_memory_high(memory_high):
            raise ValueError("linux_supervisor.memory_high_bytes must be a whole MiB at least 128 MiB")
        return cls(mode, scope, hermes, background, tuple(dict.fromkeys(resources)), memory_high)


@dataclass
class Unit:
    name: str
    active: bool
    slice: str
    group: str
    invocation: str
    cpu: int | None
    io: dict[str, int]
    psi: dict[str, float | None]
    weight: dict[str, int | None]
    effective: dict[str, int | None]
    memory: dict[str, int | None] = field(default_factory=dict)


def _valid_memory_high(value) -> bool:
    return type(value) is int and _MIN_MEMORY_HIGH <= value <= _MAX_MEMORY_HIGH and value % _MIB == 0


def _number(value, *, maximum=2**63 - 1) -> int | None:
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) < 21:
        parsed = int(value)
        if 0 <= parsed <= maximum:
            return parsed
    return None


def _read(path: Path) -> str:
    try:
        with path.open("rb") as file:
            data = file.read(8193)
    except OSError:
        return ""
    return data.decode("ascii", "replace") if len(data) <= 8192 else ""


def _pressure(text: str) -> float | None:
    match = re.search(r"(?m)^some avg10=([0-9]{1,3}(?:\.[0-9]+)?)\b", text)
    return float(match[1]) if match and 0 <= float(match[1]) <= 100 else None


def _io(text: str) -> dict[str, int]:
    counters = {}
    for line in text.splitlines():
        device, _, fields = line.partition(" ")
        if not re.fullmatch(r"[0-9]{1,6}:[0-9]{1,8}", device):
            continue
        data = dict(field.split("=", 1) for field in fields.split() if "=" in field)
        read, write = _number(data.get("rbytes")), _number(data.get("wbytes"))
        if read is not None and write is not None:
            counters[device] = read + write
    return counters


def _memory_limit(value: str | None) -> int | None:
    if value in ("infinity", "max", "[not set]", "18446744073709551615"):
        return 0  # Only an explicit unlimited sentinel. A numeric 0 is not unlimited.
    return _number(value, maximum=_MAX_MEMORY_HIGH) or None


def _memory_values(path: Path) -> dict[str, int | None]:
    def fields(filename):
        pairs = [line.split() for line in _read(path / filename).splitlines()]
        return {parts[0]: _number(parts[1]) for parts in pairs if len(parts) == 2}

    stat = fields("memory.stat")
    events = fields("memory.events")
    clean_components = [stat.get(key) for key in (
        "inactive_file", "file", "shmem", "file_mapped", "file_dirty", "file_writeback",
    )]
    clean = None
    if all(value is not None for value in clean_components):
        inactive, files, shmem, mapped, dirty, writeback = clean_components
        clean = max(0, min(inactive, files - shmem) - mapped - dirty - writeback)
    return {
        "current": _number(_read(path / "memory.current").strip()),
        "max": _memory_limit(_read(path / "memory.max").strip()),
        "swap": _number(_read(path / "memory.swap.current").strip()),
        "oom": events.get("oom"), "oom_kill": events.get("oom_kill"),
        "anon": stat.get("anon"), "shmem": stat.get("shmem"), "inactive_clean_file": clean,
    }


def _no_memory_faults(memory: dict) -> bool:
    return all(memory.get(key) == 0 for key in ("swap", "oom", "oom_kill"))


def _protected_working_set(peer: dict) -> int | None:
    current, anon, shmem, clean = (peer.get("current"), peer.get("anon"), peer.get("shmem"),
                                  peer.get("inactive_clean_file"))
    if any(value is None for value in (current, anon, shmem, clean)) or current <= 0:
        return None
    return max(anon + shmem, current - clean) + max(64 * _MIB, current // 10)


def _memory_headroom(hermes: dict, peer: dict, pressure: float | None, target: int) -> float | None:
    if not (_no_memory_faults(hermes) and _no_memory_faults(peer)) or pressure is None or pressure >= _PEER_MEMORY_PRESSURE_LIMIT:
        return None
    metered, current, hard, clean = (hermes.get("current"), peer.get("current"), peer.get("max"),
                                     peer.get("inactive_clean_file"))
    protected = _protected_working_set(peer)
    if any(value is None for value in (metered, current, hard, clean, protected)) or metered <= 0:
        return None
    spare = current - target
    if ((hard and hard <= current) or target < protected
            or not 64 * _MIB <= spare <= min(clean // 2, current // 5)):
        return None
    share = current / (metered + current)
    return share if share >= 0.65 else None


def memory_application_ready(hermes: Unit, peer: Unit, target: int | None) -> bool:
    return (_valid_memory_high(target) and peer.weight.get("memory") == peer.effective.get("memory") == 0
            and (hermes.psi.get("memory") or 0) >= THRESHOLDS["memory"]
            and _memory_headroom(hermes.memory, peer.memory, peer.psi.get("memory"), target) is not None)


def memory_trial_safe(hermes: Unit, peer: Unit, target: int) -> bool:
    stall = peer.psi.get("memory")
    protected = _protected_working_set(peer.memory)
    return (_no_memory_faults(hermes.memory) and _no_memory_faults(peer.memory)
            and stall is not None and stall < _PEER_MEMORY_PRESSURE_LIMIT
            and protected is not None and protected <= target)


class Systemd:
    def __init__(self):
        self.binary = next((str(path) for path in (Path("/usr/bin/systemctl"), Path("/bin/systemctl"))
                            if path.is_file()), None) if sys.platform.startswith("linux") else None
        self.cgroup = Path("/sys/fs/cgroup")

    @property
    def boot(self) -> str:
        value = _read(Path("/proc/sys/kernel/random/boot_id")).strip()
        return value if re.fullmatch(r"[0-9a-f-]{36}", value) else ""

    def _call(self, scope: str, args: list[str]) -> str | None:
        if self.binary is None or scope not in ("user", "system"):
            return None
        env = {"LC_ALL": "C"}
        if scope == "user":
            env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
        try:
            result = subprocess.run([self.binary, f"--{scope}", "--no-pager", "--no-ask-password", *args],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    timeout=4, check=False, env=env)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout.decode("utf-8", "replace") if result.returncode == 0 and len(result.stdout) <= 8192 else None

    def unit(self, scope: str, name: str) -> Unit | None:
        if not _UNIT_NAME.fullmatch(name):
            return None
        result = self._call(scope, ["show", "--all", "--property=" + ",".join(_FIELDS), "--", name])
        fields = dict(line.split("=", 1) for line in (result or "").splitlines() if "=" in line)
        if fields.get("Id") != name or fields.get("LoadState") != "loaded":
            return None
        group = fields.get("ControlGroup", "")
        root = self.cgroup.resolve()
        controls = (root / group.lstrip("/")).resolve() if group.startswith("/") else root
        if controls == root or not controls.is_relative_to(root):
            controls = None
        pressure = {kind: _pressure(_read(controls / (kind + ".pressure"))) if controls else None for kind in KINDS}
        io = _io(_read(controls / "io.stat")) if controls else {}
        cpu_weight = _number(_read(controls / "cpu.weight").strip(), maximum=10_000) if controls else None
        io_weight = re.search(r"(?m)^default ([0-9]{1,5})$", _read(controls / "io.weight")) if controls else None
        weight = {}
        for kind, key in KINDS.items():
            raw = fields.get(key)
            # Zero denotes a recognized unset runtime property, None denotes unavailable.
            if kind == "memory":
                weight[kind] = _memory_limit(raw)
            else:
                weight[kind] = 0 if raw in ("[not set]", "18446744073709551615") else _number(raw, maximum=10_000)
        return Unit(name, fields.get("ActiveState") == "active" and fields.get("SubState") == "running",
                    fields.get("Slice", ""), group if controls else "", fields.get("InvocationID", ""),
                    _number(fields.get("CPUUsageNSec")), io, pressure, weight,
                    {"cpu": cpu_weight, "io": _number(io_weight[1], maximum=10_000) if io_weight else None,
                     "memory": _memory_limit(_read(controls / "memory.high").strip()) if controls else None},
                    _memory_values(controls) if controls else {})

    def set_weight(self, scope: str, unit: str, kind: str, value: int) -> bool:
        valid_value = type(value) is int and 0 <= value <= 10_000
        if kind == "memory":
            valid_value = type(value) is int and (value == 0 or _valid_memory_high(value))
        if not _UNIT_NAME.fullmatch(unit) or kind not in KINDS or not valid_value:
            raise ValueError("Invalid systemd runtime resource target")
        argument = value if value else ("infinity" if kind == "memory" else "")
        assignment = f"{KINDS[kind]}={argument}"
        return self._call(scope, ["--runtime", "set-property", "--", unit, assignment]) is not None

    @contextmanager
    def lock(self, scope: str):
        if not self.binary:
            yield False
            return
        import fcntl

        directory = Path("/run/lock") if scope == "system" else Path("/run/user") / str(os.getuid())
        fd = None
        acquired = False
        try:
            fd = os.open(directory / "hermes-atlas-evolution-weights.lock",
                         os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            info = os.fstat(fd)
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_nlink == 1 and not info.st_mode & 0o077:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
        except OSError:
            pass
        try:
            yield acquired
        finally:
            if fd is not None:
                os.close(fd)


def pair_sample(hermes: Unit | None, other: Unit | None, boot: str, now: float) -> dict | None:
    if (not boot or not hermes or not other or not hermes.active or not other.active
            or not hermes.slice or hermes.slice != other.slice or not hermes.group or not other.group
            or hermes.group == other.group or not hermes.invocation or not other.invocation):
        return None
    return {"boot": boot, "now": now, "invocations": [hermes.invocation, other.invocation],
            "groups": [hermes.group, other.group], "cpu": [hermes.cpu, other.cpu], "io": [hermes.io, other.io],
            "psi": hermes.psi, "memory": [hermes.memory, other.memory],
            "peer_memory_pressure": other.psi.get("memory"),
            "peer_memory_unlimited": other.weight.get("memory") == other.effective.get("memory") == 0}


def attribute(previous: dict | None, current: dict | None, kinds: tuple[str, ...], *,
              memory_high_bytes: int | None = None) -> list[dict]:
    if not previous or not current or any(previous[key] != current[key] for key in ("boot", "invocations", "groups")):
        return []
    elapsed = current["now"] - previous["now"]
    if not 60 <= elapsed <= 1800:
        return []
    candidates = []
    for kind in kinds:
        threshold = THRESHOLDS[kind]
        pressures = [sample["psi"].get(kind) for sample in (previous, current)]
        if any(value is None or value < threshold for value in pressures):
            continue
        if kind == "memory":
            if _valid_memory_high(memory_high_bytes) and all(sample.get("peer_memory_unlimited") for sample in (previous, current)):
                shares = [_memory_headroom(*sample["memory"], sample["peer_memory_pressure"], memory_high_bytes)
                          for sample in (previous, current)]
                if all(share is not None for share in shares):
                    candidates.append({"kind": kind, "attributed": True, "pressure_avg10": pressures[1],
                                       "previous_pressure_avg10": pressures[0], "share": round(shares[-1], 3),
                                       "memory_high_bytes": memory_high_bytes,
                                       "background_memory_current_bytes": current["memory"][1]["current"],
                                       "background_inactive_clean_file_bytes": current["memory"][1]["inactive_clean_file"]})
            continue
        first, last = previous[kind], current[kind]
        if kind == "cpu":
            if None in first + last:
                continue
            rates = [(last[i] - first[i]) / elapsed / 1e9 for i in range(2)]
            minimum = 0.20
        else:
            shared = set(first[0]) & set(first[1]) & set(last[0]) & set(last[1])
            deltas = [[last[i][dev] - first[i][dev] for i in range(2)] for dev in sorted(shared)]
            active = [current_delta for current_delta in deltas if min(current_delta) > 0]
            if not active:
                continue
            rates = [sum(current_delta[i] for current_delta in active) / elapsed for i in range(2)]
            minimum = 1_048_576
        if rates[0] <= 0 or rates[1] < minimum:
            continue
        share = rates[1] / sum(rates)
        if share >= 0.65:
            candidates.append({"kind": kind, "attributed": True, "pressure_avg10": pressures[1],
                               "previous_pressure_avg10": pressures[0], "hermes_units_per_second": round(rates[0], 3),
                               "background_units_per_second": round(rates[1], 3), "share": round(share, 3)})
    return candidates
