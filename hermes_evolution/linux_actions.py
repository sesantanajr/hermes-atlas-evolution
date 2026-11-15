"""Narrow systemd/cgroup-v2 adapter for reversible resource weighting."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path

KINDS = {"cpu": "CPUWeight", "io": "IOWeight"}
THRESHOLDS = {"cpu": 10.0, "io": 5.0}
_UNIT_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.@-]{0,100}\.service\Z")
_FIELDS = ("Id", "LoadState", "ActiveState", "SubState", "Slice", "ControlGroup", "InvocationID", "CPUUsageNSec",
           "CPUWeight", "IOWeight")


@dataclass(frozen=True)
class SystemPolicy:
    mode: str
    scope: str
    hermes_unit: str
    background_unit: str
    resources: tuple[str, ...]

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
                or any(resource not in ("cpu", "io") for resource in resources)):
            raise ValueError("linux_supervisor needs two distinct .service units, a valid scope, mode and resources")
        return cls(mode, scope, hermes, background, tuple(dict.fromkeys(resources)))


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
            weight[kind] = 0 if raw in ("[not set]", "18446744073709551615") else _number(raw, maximum=10_000)
        return Unit(name, fields.get("ActiveState") == "active" and fields.get("SubState") == "running",
                    fields.get("Slice", ""), group if controls else "", fields.get("InvocationID", ""),
                    _number(fields.get("CPUUsageNSec")), io, pressure, weight,
                    {"cpu": cpu_weight, "io": _number(io_weight[1], maximum=10_000) if io_weight else None})

    def set_weight(self, scope: str, unit: str, kind: str, value: int) -> bool:
        if not _UNIT_NAME.fullmatch(unit) or kind not in KINDS or type(value) is not int or not 0 <= value <= 10_000:
            raise ValueError("Invalid systemd weight target")
        assignment = f"{KINDS[kind]}={value if value else ''}"
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
            fd = os.open(directory / "hermes-evolution-weights.lock",
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
            "psi": hermes.psi}


def attribute(previous: dict | None, current: dict | None, kinds: tuple[str, ...]) -> list[dict]:
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
