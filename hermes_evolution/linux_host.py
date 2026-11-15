"""Read-only Linux resource measurements with explicit namespaces and scan coverage."""

from __future__ import annotations

import json
import math
import os
import re
import stat
import time
from pathlib import Path, PurePosixPath

_MEMORY_FIELDS = {
    "MemTotal": "total_bytes", "MemAvailable": "available_bytes", "MemFree": "free_bytes",
    "Cached": "file_cache_bytes", "Buffers": "buffers_bytes", "SReclaimable": "s_reclaimable_bytes",
    "Shmem": "shmem_bytes", "SwapTotal": "swap_total_bytes", "SwapFree": "swap_free_bytes",
    "Dirty": "dirty_bytes", "Writeback": "writeback_bytes",
}
_VM_FIELDS = {"pgmajfault", "pswpin", "pswpout", "pgscan_kswapd", "pgscan_direct"}
_CATEGORIES = {
    "cache": "cache", "audio_cache": "cache", "image_cache": "cache", "bootstrap-cache": "cache",
    "logs": "logs", "sessions": "sessions", "plugins": "plugins", "skills": "skills",
    "runtime": "runtime", ".hermes-runtime": "runtime", "evolution": "evolution",
}
_ENTRY_LIMIT = 1000
_SCAN_SECONDS = 0.15
_DEPTH_LIMIT = 6
_SCANDIR_FD = os.scandir in os.supports_fd and os.open in os.supports_dir_fd


def _read(path: Path, limit: int = 16_384) -> str | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return None
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            data = stream.read(limit + 1)
    except (OSError, ValueError):
        return None
    return data.decode("utf-8", "replace") if len(data) <= limit else None


def _number(value: str | None) -> int | None:
    value = (value or "").strip()
    if re.fullmatch(r"[0-9]{1,20}", value):
        return int(value)
    return None


def _pairs(value: str | None, allow: set[str]) -> dict[str, int]:
    output = {}
    for line in (value or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in allow:
            number = _number(parts[1])
            if number is not None:
                output[parts[0]] = number
    return output


def _kib(value: str | None, names: dict[str, str]) -> dict[str, int]:
    output = {}
    for line in (value or "").splitlines():
        name, _, body = line.partition(":")
        parts = body.split()
        if name in names and len(parts) == 2 and parts[1] == "kB":
            number = _number(parts[0])
            if number is not None:
                output[names[name]] = number * 1024
    return output


def _memory(proc: Path) -> dict | None:
    result = _kib(_read(proc / "meminfo", 32_768), _MEMORY_FIELDS)
    if not result.get("total_bytes"):
        return None
    if all(key in result for key in ("file_cache_bytes", "shmem_bytes", "buffers_bytes")):
        # Cached includes tmpfs/shmem. Keep slab separately to avoid counting it as file cache.
        result["page_cache_bytes"] = max(0, result["file_cache_bytes"] - result["shmem_bytes"]) + result["buffers_bytes"]
    if "swap_total_bytes" in result and "swap_free_bytes" in result:
        result["swap_used_bytes"] = max(0, result["swap_total_bytes"] - result["swap_free_bytes"])
    result["vm_counters"] = _pairs(_read(proc / "vmstat", 65_536), _VM_FIELDS)
    return result


def _pressure(path: Path) -> dict:
    values = {}
    for line in (_read(path, 4096) or "").splitlines():
        parts = line.split()
        if not parts or parts[0] not in {"some", "full"}:
            continue
        key, *fields = parts
        info = dict(field.split("=", 1) for field in fields if "=" in field)
        try:
            averages = {period: float(info[period]) for period in ("avg10", "avg60", "avg300")}
        except (KeyError, ValueError):
            continue
        total = _number(info.get("total"))
        if total is not None and all(0 <= value <= 100 for value in averages.values()):
            values[key] = {**averages, "total_us": total}
    return values


def _cgroup_scope(process: Path, sys: Path) -> Path | None:
    relative = next((line[3:] for line in (_read(process / "cgroup") or "").splitlines() if line.startswith("0::/")), None)
    if relative is None or "\\" in relative:
        return None
    parts = PurePosixPath(relative).parts[1:]
    if len(parts) > 24 or any(part == ".." or ":" in part for part in parts):
        return None
    base = sys / "fs" / "cgroup"
    try:
        resolved = base.resolve(strict=True)
        target = base.joinpath(*parts).resolve(strict=True)
        if target.is_relative_to(resolved) and target.is_dir():
            return target
    except (OSError, RuntimeError, ValueError):
        pass
    return None


def _quota(path: Path) -> float | None:
    fields = (_read(path / "cpu.max", 256) or "").split()
    if len(fields) != 2:
        return None
    quota, period = map(_number, fields)
    return quota / period if quota and period else None


def _cgroup(process: Path, sys: Path, scope: str) -> dict | None:
    path = _cgroup_scope(process, sys)
    if path is None:
        return None
    base = (sys / "fs" / "cgroup").resolve()
    # Ancestors can impose tighter limits than the leaf. Keep usage paired with
    # its own group and report the most pressured measured limit separately.
    ancestors = (path, *(parent for parent in path.parents if parent.is_relative_to(base)))
    limits = []
    quotas = []
    for distance, member in enumerate(ancestors):
        maximum = _number(_read(member / "memory.max", 256))
        current = _number(_read(member / "memory.current", 256))
        if maximum and current is not None:
            limits.append((current / maximum, distance, current, maximum))
        quota = _quota(member)
        if quota is not None:
            quotas.append(quota)
    flags = _pairs(_read(path / "memory.events"), {"low", "high", "max", "oom", "oom_kill", "oom_group_kill"})
    memory = _pairs(_read(path / "memory.stat"), {"anon", "file", "kernel", "slab_reclaimable", "shmem"})
    cpu = _pairs(_read(path / "cpu.stat"), {"usage_usec", "nr_periods", "nr_throttled", "throttled_usec"})
    maximum = _number(_read(path / "memory.max", 256))
    current = _number(_read(path / "memory.current", 256))
    limiting_memory = None
    if limits:
        fraction, distance, used, limit = max(limits, key=lambda item: item[0])
        limiting_memory = {"ancestor_levels": distance, "used_bytes": used, "max_bytes": limit,
                           "fraction_used": round(fraction, 4)}
    return {"scope": scope, "memory_max_bytes": maximum, "memory_current_bytes": current,
            "memory_high_bytes": _number(_read(path / "memory.high", 256)),
            "memory_swap_current_bytes": _number(_read(path / "memory.swap.current", 256)),
            "memory_anon_bytes": memory.get("anon"), "memory_file_bytes": memory.get("file"),
            "memory_events": flags, "limiting_memory_group": limiting_memory,
            "pressure": {name: _pressure(path / f"{name}.pressure") for name in ("cpu", "memory", "io")},
            "cpu_usage_us": cpu.get("usage_usec"), "cpu_nr_throttled": cpu.get("nr_throttled"),
            "cpu_throttled_us": cpu.get("throttled_usec"), "quota_cpus": min(quotas, default=None)}


def _load(proc: Path, cgroup: dict | None) -> dict | None:
    load = (_read(proc / "loadavg", 4096) or "").split()
    if len(load) < 3:
        return None
    try:
        averages = list(map(float, load[:3]))
    except ValueError:
        return None
    if not all(number >= 0 and math.isfinite(number) for number in averages):
        return None
    logical = os.cpu_count()
    try:
        affinity = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        affinity = None
    return {"one_minute": averages[0], "five_minutes": averages[1], "fifteen_minutes": averages[2],
            "logical_cpus": logical, "collector_affinity_cpus": affinity,
            "quota_cpus": cgroup.get("quota_cpus") if cgroup else None}


def _disk(home: Path) -> dict | None:
    try:
        values = os.statvfs(home)
    except (AttributeError, OSError):
        return None
    return {"total_bytes": values.f_blocks * values.f_frsize, "free_bytes": values.f_bavail * values.f_frsize,
            "inodes_total": values.f_files or None, "inodes_available": values.f_favail if values.f_files else None}


def _gateway(process_root: Path, home: Path) -> tuple[Path | None, dict]:
    try:
        marker = json.loads(_read(home / "gateway.pid", 4096) or "null")
        if not isinstance(marker, dict) or marker.get("kind") != "hermes-gateway":
            marker = json.loads(_read(home / "gateway_state.json", 65_536) or "null")
    except (ValueError, RecursionError):
        return None, {"status": "unverified"}
    if not isinstance(marker, dict) or marker.get("kind") != "hermes-gateway":
        return None, {"status": "unverified"}
    pid, start = marker.get("pid"), marker.get("start_time")
    if type(pid) is not int or not 0 < pid < 2**31 or type(start) is not int:
        return None, {"status": "unverified"}
    process = process_root / str(pid)

    def verified():
        fields = (_read(process / "stat", 8192) or "").rsplit(")", 1)[-1].split()
        return len(fields) > 19 and fields[0] != "Z" and _number(fields[19]) == start

    if not verified():
        return None, {"status": "unverified"}
    measured = _kib(_read(process / "status", 32_768), {
        "VmRSS": "resident_bytes", "RssAnon": "anonymous_bytes", "RssFile": "file_bytes",
        "RssShmem": "shared_bytes", "VmSwap": "swap_bytes",
    })
    io = _pairs((_read(process / "io", 4096) or "").replace(":", ""), {"read_bytes", "write_bytes"})
    if not verified():
        return None, {"status": "unverified"}
    return process, {"status": "verified", "memory": measured, "lifetime_io": io}


def _plain_info(info: os.stat_result) -> bool:
    return not (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400)


def _storage_area(home: Path, home_fd: int | None, name: str, deadline: float) -> dict:
    result = {"area": name, "kind": _CATEGORIES[name], "status": "ok", "entries": 0,
              "file_count": 0, "apparent_bytes": 0, "allocated_bytes": None}
    hardlinks = set()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)

    def walk(parent_path: Path, parent_fd: int | None, child: str, depth: int, device: int | None):
        path = parent_path / child
        opened = None
        try:
            if parent_fd is not None:
                opened = os.open(child, flags, dir_fd=parent_fd)
                info = os.fstat(opened)
            else:
                info = path.lstat()
            if not _plain_info(info) or not stat.S_ISDIR(info.st_mode) or (device is not None and info.st_dev != device):
                result["status"] = "partial"
                return
            with os.scandir(opened if opened is not None else path) as children:
                for entry in children:
                    if result["entries"] >= _ENTRY_LIMIT or time.monotonic() >= deadline:
                        result["status"] = "partial"
                        break
                    result["entries"] += 1
                    try:
                        item = entry.stat(follow_symlinks=False) if opened is not None else (path / entry.name).lstat()
                    except OSError:
                        result["status"] = "partial"
                        continue
                    if not _plain_info(item) or item.st_dev != info.st_dev:
                        result["status"] = "partial"
                        continue
                    if stat.S_ISDIR(item.st_mode):
                        if depth < _DEPTH_LIMIT:
                            walk(path, opened, entry.name, depth + 1, info.st_dev)
                        else:
                            result["status"] = "partial"
                    elif stat.S_ISREG(item.st_mode):
                        fingerprint = (item.st_dev, item.st_ino)
                        if item.st_nlink > 1 and fingerprint in hardlinks:
                            continue
                        if item.st_nlink > 1:
                            hardlinks.add(fingerprint)
                        result["file_count"] += 1
                        result["apparent_bytes"] += item.st_size
                        if hasattr(item, "st_blocks"):
                            result["allocated_bytes"] = (result["allocated_bytes"] or 0) + item.st_blocks * 512
        except FileNotFoundError:
            result["status"] = "missing" if depth == 0 else "partial"
        except OSError:
            result["status"] = "partial"
        finally:
            if opened is not None:
                os.close(opened)

    if time.monotonic() >= deadline:
        result["status"] = "not_sampled"
    else:
        walk(home, home_fd, name, 0, None)
    return result


def _storage(home: Path) -> dict:
    descriptor = None
    deadline = time.monotonic() + _SCAN_SECONDS
    try:
        if _SCANDIR_FD:
            descriptor = os.open(home, os.O_RDONLY | os.O_DIRECTORY)
        areas = [_storage_area(home, descriptor, name, deadline) for name in _CATEGORIES]
    except OSError:
        areas = []
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return {"areas": areas, "truncated": not areas or any(item["status"] not in {"ok", "missing"} for item in areas)}


def _avg(pressure: dict, resource: str, category: str) -> float:
    return pressure.get(resource, {}).get(category, {}).get("avg60", 0.0)


def _findings(report: dict) -> list[dict]:
    memory = report["memory"] or {}
    disk = report["disk"] or {}
    cgroup = report["cgroup"] or {}
    pressure = report["pressure"]
    available = memory.get("available_bytes")
    ram_fraction = available / memory["total_bytes"] if available is not None and memory.get("total_bytes") else None
    memory_stall = _avg(pressure, "memory", "some")
    cpu_stall = _avg(pressure, "cpu", "some")
    io_stall = _avg(pressure, "io", "some")
    io_full = _avg(pressure, "io", "full")
    limit = cgroup.get("limiting_memory_group") or {}
    namespace_stall = _avg(cgroup.get("pressure", {}), "memory", "some")
    swap_total = memory.get("swap_total_bytes", 0)
    swap_fraction = memory.get("swap_used_bytes", 0) / swap_total if swap_total else 0
    inodes_total = disk.get("inodes_total")
    inodes = disk["inodes_available"] / inodes_total if inodes_total and disk["inodes_available"] is not None else None
    capacity = disk["free_bytes"] / disk["total_bytes"] if disk.get("total_bytes") else None
    observations = [
        (ram_fraction is not None and ram_fraction < 0.12 and memory_stall >= 2,
         "host_memory_reclaim_pressure", {"available_fraction": ram_fraction, "memory_some_avg60": memory_stall},
         "Meça RSS anônimo, limites de serviços e rotinas concorrentes antes de ajustar a carga residente."),
        (swap_fraction >= 0.50 and memory_stall >= 2,
         "host_swap_pressure", {"swap_used_fraction": swap_fraction, "memory_some_avg60": memory_stall},
         "Correlacione a pressão de memória com aumentos nos contadores de swap e processos residentes."),
        (cpu_stall >= 15,
         "host_cpu_pressure", {"cpu_some_avg60": cpu_stall},
         "Meça a concorrência das rotinas e compare a pressão da CPU do host com a do serviço."),
        (io_full >= 3 or io_stall >= 12,
         "host_io_pressure", {"io_some_avg60": io_stall, "io_full_avg60": io_full},
         "Correlacione a espera de I/O com horários de backup, indexação e crescimento dos arquivos."),
        (limit.get("fraction_used", 0) >= 0.90 and namespace_stall >= 2,
         "host_cgroup_memory_pressure", {"limiting_memory_group": limit, "memory_some_avg60": namespace_stall,
                                         "scope": cgroup.get("scope")},
         "Confirme a competição por memória no cgroup limitante e examine as rotinas que dividem o serviço."),
        (inodes is not None and inodes < 0.08,
         "host_disk_inodes_low", {"available_fraction": inodes},
         "Identifique diretórios com muitos arquivos pequenos e revise a retenção nativa."),
        (capacity is not None and capacity < 0.10,
         "host_disk_capacity_low", {"available_fraction": capacity},
         "Verifique a origem do crescimento do volume e os mecanismos nativos de retenção."),
    ]
    findings = [{"id": code, "severity": "warning", "evidence": evidence, "recommendation": recommendation,
                 "confidence": "corroborated" if code in {"host_memory_reclaim_pressure", "host_swap_pressure",
                                                          "host_cgroup_memory_pressure"} else "direct_measurement"}
                for applicable, code, evidence, recommendation in observations if applicable]
    large = [{"area": item["area"], "allocated_bytes_at_least": item["allocated_bytes"], "status": item["status"]}
             for item in report["storage"]["areas"] if (item["allocated_bytes"] or 0) >= 2**30]
    if large:
        findings.append({"id": "host_hermes_storage_hotspot", "severity": "info", "evidence": {"areas": large},
                         "confidence": "sampled_lower_bound",
                         "recommendation": "Compare as áreas de maior alocação com retenção, uso e dependências atuais."})
    return findings


def scan_host(home: Path, *, proc: Path = Path("/proc"), sys: Path = Path("/sys")) -> dict:
    """Inspect the Linux namespace visible to the installed process without executing commands."""
    pressure = {name: _pressure(proc / "pressure" / name) for name in ("cpu", "memory", "io")}
    memory = _memory(proc)
    process, gateway = _gateway(proc, home)
    cgroup = _cgroup(process or proc / "self", sys, "gateway" if process else "collector")
    disk = _disk(home)
    load = _load(proc, cgroup)
    reliable = memory and "available_bytes" in memory and disk and load and all(pressure.values())
    status = "ok" if reliable else "partial"
    if not any((memory, load, cgroup, *pressure.values())) and not proc.is_dir():
        status = "unsupported"
    report = {"status": status, "memory": memory, "cgroup": cgroup, "load": load, "pressure": pressure,
              "disk": disk, "storage": _storage(home), "processes": {"gateway": gateway}}
    report["findings"] = _findings(report)
    return report
