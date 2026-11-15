"""Bounded, read-only diagnostics for a Hermes profile and its local host."""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import time


_LOGS = ("agent.log", "errors.log", "gateway.log", "gateway.error.log")
_LOG_LIMIT = 256 * 1024
_LOG_HOURS = 24
_STATE_LIMIT = 128 * 1024
_DB_TIMEOUT = 0.3
_DB_SIZE_LIMIT = 64 * 1024 * 1024
_VERSION = re.compile(r"v?(\d{1,6})\.(\d{1,6})\.(\d{1,6})")
_PROVIDER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}")
_LOG_LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})(?:[.,]\d+)?\s+"
    r"(WARNING|ERROR|CRITICAL)(?:\s|\[)(.*)$"
)
_LOG_SIGNALS = {
    "storage_io": re.compile(r"no space left|disk full|enospc|database or disk is full", re.I),
    "resource_exhaustion": re.compile(r"out of memory|oom[- ]kill|memoryerror|cannot allocate memory", re.I),
    "database_locked": re.compile(r"database(?: table)? is locked|sqlite_busy|sqlite_locked", re.I),
    "storage_access": re.compile(r"read.only file ?system|permission denied|permissionerror|\bEACCES\b|\bEROFS\b", re.I),
    "rate_limit": re.compile(r"rate[- _]?limit|too many requests|\b429\b", re.I),
    "authentication": re.compile(r"invalid api key|unauthori[sz]ed|authentication failed|\b401\b", re.I),
    "timeout": re.compile(r"timed?\s*out|timeout|deadline exceeded", re.I),
}
_FAILURE_WORD = re.compile(r"failed|error|unavailable", re.I)
_MEMORY_WORD = re.compile(r"\b(?:memory|recall)\b", re.I)
_PLUGIN_WORD = re.compile(r"\b(?:plugin|mcp)\b", re.I)
_PLUGIN_DEPENDENCY = re.compile(r"ModuleNotFoundError|ImportError|No module named", re.I)
_PLUGIN_CONTRACT = re.compile(r"TypeError|AttributeError|unexpected keyword argument|unknown hook", re.I)


def _regular_stat(path: Path) -> os.stat_result | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return info if stat.S_ISREG(info.st_mode) else None


def _read_small(path: Path, limit: int) -> bytes | None:
    if _regular_stat(path) is None:
        return None
    try:
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
    except OSError:
        return None
    return data if len(data) <= limit else None


def _json_state(path: Path) -> dict:
    try:
        value = json.loads(_read_small(path, _STATE_LIMIT) or b"{}")
    except (ValueError, RecursionError):
        return {}
    if path.name == "gateway.pid" and type(value) is int:
        return {"pid": value}
    return value if isinstance(value, dict) else {}


def _version(value: object) -> tuple[int, int, int] | None:
    match = _VERSION.fullmatch(value.strip()) if isinstance(value, str) else None
    return tuple(int(part) for part in match.groups()) if match else None


def _version_text(version: tuple[int, int, int] | None) -> str | None:
    return ".".join(map(str, version)) if version is not None else None


def _update_check(current: object, upstream: object) -> dict:
    local = _version(current)
    target = _version(upstream)
    status = "not_checked"
    if upstream is not None and target is None:
        status = "invalid_reference"
    elif target is not None:
        status = "unknown_version" if local is None else (
            "update_available" if local < target else ("ahead" if local > target else "current")
        )
    return {"current": _version_text(local), "reference": _version_text(target), "status": status}


def _checkpoint_unchanged(path: Path, before: os.stat_result, sidecars: list[Path]) -> bool:
    after = _regular_stat(path)
    try:
        return bool(
            after
            and (before.st_ino, before.st_mtime_ns, before.st_size) == (after.st_ino, after.st_mtime_ns, after.st_size)
            and not any(sidecar.exists() for sidecar in sidecars)
        )
    except OSError:
        return False


def _database(home: Path) -> dict:
    path = home / "state.db"
    info = _regular_stat(path)
    if info is None:
        return {"status": "missing_or_unreadable"}
    result = {"bytes": info.st_size, "mode": "read_only"}
    checkpoint_only = False
    wal_bytes = 0
    try:
        path = path.resolve(strict=True)
        with path.open("rb") as stream:
            header = stream.read(20)
        sidecars = [path.with_name(f"{path.name}-{suffix}") for suffix in ("wal", "shm")]
        if header[:16] == b"SQLite format 3\x00" and header[18:20] == b"\x02\x02":
            present = [sidecar.exists() for sidecar in sidecars]
            if any(present) and not all(present):
                return {**result, "status": "wal_sidecars_unavailable"}
            # For a closed WAL database, check the checkpointed base without
            # initializing sidecars. Discard any result if a writer appeared.
            checkpoint_only = not any(present)
            wal_info = _regular_stat(sidecars[0])
            wal_bytes = wal_info.st_size if wal_info is not None else 0
            result["wal_bytes"] = wal_bytes
        query = "?mode=ro&immutable=1" if checkpoint_only else "?mode=ro"
        deadline = time.monotonic() + _DB_TIMEOUT
        with closing(sqlite3.connect(path.as_uri() + query, uri=True, timeout=_DB_TIMEOUT)) as db:
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("BEGIN")
            tables = {
                row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('sessions', 'messages')"
                )
            }
            if tables != {"sessions", "messages"}:
                status = "unexpected_schema"
            elif info.st_size + wal_bytes > _DB_SIZE_LIMIT:
                status = "integrity_skipped_large"
            else:
                check = db.execute("PRAGMA quick_check(1)").fetchone()
                status = "ok" if check == ("ok",) else "integrity_error"
        if checkpoint_only:
            if not _checkpoint_unchanged(path, info, sidecars):
                return {**result, "status": "snapshot_changed"}
            result["mode"] = "checkpoint_only"
        return {**result, "status": status}
    except sqlite3.Error as exc:
        if checkpoint_only and not _checkpoint_unchanged(path, info, sidecars):
            return {**result, "status": "snapshot_changed"}
        code = getattr(exc, "sqlite_errorcode", 0) or 0
        reasons = {
            sqlite3.SQLITE_INTERRUPT: "check_timed_out",
            sqlite3.SQLITE_BUSY: "busy",
            sqlite3.SQLITE_LOCKED: "busy",
            sqlite3.SQLITE_CORRUPT: "integrity_error",
            sqlite3.SQLITE_NOTADB: "integrity_error",
        }
        return {**result, "status": reasons.get(code & 255, "unreadable")}
    except OSError:
        return {**result, "status": "unreadable"}


def _memory(home: Path, provider: object) -> dict:
    files = {}
    for name in ("MEMORY.md", "USER.md"):
        path = home / "memories" / name
        info = _regular_stat(path)
        status = "missing_or_unreadable"
        if info is not None:
            status = "not_checked_too_large"
            if info.st_size <= _STATE_LIMIT:
                raw = _read_small(path, _STATE_LIMIT)
                status = "unreadable"
                if raw is not None:
                    try:
                        raw.decode("utf-8")
                        status = "readable" if raw.strip() else "empty"
                    except UnicodeDecodeError:
                        status = "invalid_utf8"
        files[name] = {"present": info is not None, "bytes": info.st_size if info else None, "status": status}
    return {
        "provider": provider if isinstance(provider, str) and _PROVIDER.fullmatch(provider) else None,
        "native_files": files,
    }


def _process_liveness(record: dict) -> str:
    """Identify the native Linux process without invoking commands or sending signals."""
    pid = record.get("pid")
    if type(pid) is not int or pid < 1 or not Path("/proc/self/stat").is_file():
        return "unverified"
    try:
        with Path(f"/proc/{pid}/stat").open("rb") as stream:
            proc = stream.read(4096)
    except FileNotFoundError:
        return "missing_locally"
    except OSError:
        return "unverified"
    try:
        fields = proc.rsplit(b")", 1)[-1].split()
        if fields[0] == b"Z":
            return "missing_locally"
        start = int(fields[19])
    except (IndexError, ValueError):
        return "unverified"
    expected_start = record.get("start_time")
    if type(expected_start) is not int:
        return "unverified"
    if start != expected_start:
        return "pid_reused_locally"
    return "confirmed" if record.get("kind") == "hermes-gateway" else "unverified"


def _marker_age(value: object) -> float | None:
    try:
        if isinstance(value, str):
            when = datetime.fromisoformat(value)
            if when.tzinfo is None:
                return None
        elif type(value) in (int, float):
            when = datetime.fromtimestamp(value, timezone.utc)
        else:
            return None
        age = (datetime.now(timezone.utc) - when).total_seconds()
    except (ValueError, OverflowError, OSError):
        return None
    return round(age, 1) if age >= 0 else None


def _gateway(home: Path) -> dict:
    marker = _json_state(home / "gateway_state.json")
    pid_file = _json_state(home / "gateway.pid")
    process = _process_liveness(pid_file or marker)
    if process != "confirmed" and pid_file and pid_file != marker:
        process_from_marker = _process_liveness(marker)
        if process_from_marker != "unverified":
            process = process_from_marker
    reported = marker.get("gateway_state")
    states = ("starting", "running", "restarting", "stopping", "stopped", "startup_failed", "degraded")
    markers_match = not pid_file or (
        marker.get("pid") == pid_file.get("pid")
        and (pid_file.get("start_time") is None or marker.get("start_time") == pid_file.get("start_time"))
    )
    platform_states = marker.get("platforms")
    attention = sum(
        isinstance(value, dict) and value.get("needs_attention") is True
        for value in platform_states.values()
    ) if isinstance(platform_states, dict) else 0
    store = marker.get("session_store")
    store_status = store.get("status") if isinstance(store, dict) else None
    version = _version(marker.get("code_version"))
    age = _marker_age(marker.get("updated_at"))
    heartbeat = "unverified"
    if version == (0, 23, 1) and age is not None and reported in ("running", "degraded"):
        heartbeat = "stale" if age > 120 else "recent"
    return {
        "reported_state": reported if reported in states else None,
        "process_liveness": process,
        "marker_consistent": bool(marker and markers_match),
        "reported_age_seconds": age,
        "reported_version": _version_text(version),
        "heartbeat": heartbeat,
        "platforms_needing_attention": attention,
        "reported_session_store": store_status if store_status in ("ok", "unavailable", "retrying") else None,
    }


def _log_tail(path: Path) -> tuple[bytes, bool] | None:
    info = _regular_stat(path)
    if info is None:
        return None
    try:
        with path.open("rb") as stream:
            if info.st_size <= _LOG_LIMIT:
                return stream.read(_LOG_LIMIT), False
            stream.seek(-_LOG_LIMIT, os.SEEK_END)
            return stream.read(_LOG_LIMIT).partition(b"\n")[2], True
    except OSError:
        return None


def _log_signals(home: Path) -> dict:
    recent = datetime.now()
    cutoff = recent - timedelta(hours=_LOG_HOURS)
    counts: Counter[str] = Counter()
    files = []
    lines_seen: set[bytes] = set()
    truncated = False
    for name in _LOGS:
        sample = _log_tail(home / "logs" / name)
        if sample is None:
            continue
        tail, partial = sample
        truncated |= partial
        files.append(name)
        for line in tail.splitlines():
            if line in lines_seen:
                continue
            match = _LOG_LINE.match(line.decode("utf-8", "replace"))
            if match is None:
                continue
            try:
                log_time = datetime.fromisoformat(match[1])
            except ValueError:
                continue
            if not cutoff <= log_time <= recent:
                continue
            lines_seen.add(line)
            counts[match[2].lower()] += 1
            for name, pattern in _LOG_SIGNALS.items():
                if pattern.search(match[3]):
                    counts[name] += 1
            if _FAILURE_WORD.search(match[3]):
                if _MEMORY_WORD.search(match[3]):
                    counts["memory_failure"] += 1
                if _PLUGIN_WORD.search(match[3]):
                    counts["plugin_failure"] += 1
                    if _PLUGIN_DEPENDENCY.search(match[3]):
                        counts["plugin_dependency_error"] += 1
                    if _PLUGIN_CONTRACT.search(match[3]):
                        counts["plugin_contract_error"] += 1
    return {"window_hours": _LOG_HOURS, "files_sampled": files, "truncated": truncated, "events": dict(counts)}


def _host(home: Path) -> dict:
    result = {"platform": os.name, "disk": None, "memory": None, "cgroup_memory": None, "load": None}
    try:
        disk = shutil.disk_usage(home)
        result["disk"] = {"total_bytes": disk.total, "free_bytes": disk.free}
    except OSError:
        pass
    if hasattr(os, "getloadavg"):
        try:
            cpus = os.cpu_count()
            result["load"] = {"one_minute": os.getloadavg()[0], "logical_cpus": cpus}
        except OSError:
            pass
    fields = dict(re.findall(
        rb"^(MemTotal|MemAvailable|SwapTotal|SwapFree):\s+(\d+)\s+kB",
        _read_small(Path("/proc/meminfo"), 32 * 1024) or b"", re.M,
    ))
    if b"MemTotal" in fields and b"MemAvailable" in fields:
        result["memory"] = {
            "total_bytes": int(fields[b"MemTotal"]) * 1024,
            "available_bytes": int(fields[b"MemAvailable"]) * 1024,
            "swap_total_bytes": int(fields.get(b"SwapTotal", 0)) * 1024,
            "swap_free_bytes": int(fields.get(b"SwapFree", 0)) * 1024,
            "scope": "linux_proc",
        }
    cgroup = _read_small(Path("/proc/self/cgroup"), 16 * 1024) or b""
    relative = next((os.fsdecode(line[3:]) for line in cgroup.splitlines() if line.startswith(b"0::/")), None)
    if relative is not None:
        root = Path("/sys/fs/cgroup")
        scope = root / relative.lstrip("/")
        if ".." not in scope.parts:
            limits = []
            while scope.is_relative_to(root):
                raw_limit = (_read_small(scope / "memory.max", 64) or b"").strip()
                raw_current = (_read_small(scope / "memory.current", 64) or b"").strip()
                if raw_limit.isdigit() and raw_current.isdigit():
                    total, usage = int(raw_limit), int(raw_current)
                    if total > 0:
                        limits.append({"total_bytes": total, "available_bytes": max(0, total - usage)})
                scope = scope.parent
            if limits:
                result["cgroup_memory"] = min(limits, key=lambda limit: limit["available_bytes"])
    return result


def _findings(report: dict) -> list[dict]:
    findings = []
    disk = report["host"]["disk"] or {}
    memory = report["host"]["memory"] or {}
    cgroup_memory = report["host"]["cgroup_memory"] or {}
    load = report["host"]["load"] or {}
    logs = report["logs"]["events"]
    gateway = report["gateway"]
    memory_files = report["memory"]["native_files"]
    signals = (
        (disk.get("total_bytes", 0) and disk["free_bytes"] / disk["total_bytes"] < 0.10,
         "low_disk", "warning", "Reserve espaço no volume do perfil e confira retenção, backups e gerações em uso."),
        (memory.get("total_bytes", 0) and memory["available_bytes"] / memory["total_bytes"] < 0.10,
         "low_memory", "warning", "Meça os processos residentes e considere iniciar serviços ociosos sob demanda."),
        (cgroup_memory.get("total_bytes", 0) and cgroup_memory["available_bytes"] / cgroup_memory["total_bytes"] < 0.10,
         "cgroup_memory_limit", "warning", "Meça a pressão de memória no grupo do serviço e confira seu limite configurado."),
        (load.get("logical_cpus") and load["one_minute"] / load["logical_cpus"] > 1.25,
         "host_load", "warning", "Correlacione a carga com rotinas e indexação, usando uma série de medições."),
        (report["database"]["status"] == "integrity_error",
         "state_integrity", "critical", "Preserve um backup consistente do estado e investigue a integridade offline."),
        (report["database"]["status"] == "unexpected_schema",
         "state_schema", "warning", "Confira o HERMES_HOME, a migração e o esquema da versão em execução."),
        (any(file["status"] in ("invalid_utf8", "unreadable") for file in memory_files.values()),
         "native_memory_readability", "warning", "Confira o formato UTF-8 e as permissões do arquivo de memória nativa."),
        (gateway["reported_state"] == "startup_failed",
         "gateway_startup_failure", "warning", "Confira o serviço e os erros recentes antes de repetir a inicialização."),
        (gateway["reported_state"] in ("running", "degraded") and gateway["process_liveness"] in ("missing_locally", "pid_reused_locally"),
         "gateway_liveness_unconfirmed", "warning", "Compare o registro com o supervisor e o namespace do gateway."),
        (gateway["reported_state"] == "degraded" and gateway["process_liveness"] == "confirmed",
         "gateway_degraded", "warning", "Correlacione a degradação com as conexões e os registros do gateway."),
        (gateway["marker_consistent"] and gateway["heartbeat"] == "stale" and gateway["process_liveness"] == "confirmed",
         "gateway_heartbeat_stale", "warning", "O processo existe e seu heartbeat está atrasado. Confira bloqueios nas rotinas do gateway."),
        (gateway["platforms_needing_attention"] > 0,
         "gateway_platform_attention", "warning", "Verifique as conexões em recuperação no painel nativo do Hermes."),
        (gateway["reported_session_store"] in ("unavailable", "retrying"),
         "gateway_store_attention", "warning", "Correlacione o armazenamento de sessões com os sinais de SQLite e disco."),
        (gateway["process_liveness"] == "confirmed" and gateway["marker_consistent"]
         and gateway["reported_version"] and report["update"]["current"]
         and gateway["reported_version"] != report["update"]["current"],
         "gateway_version_mismatch", "warning", "Confira a versão usada pelo processo e pelo ambiente que executa o complemento."),
        (report["update"]["status"] == "update_available",
         "newer_release", "info", "Compare as notas da versão de referência e valide o upgrade em um perfil de teste."),
        (logs.get("storage_io", 0) > 0 or logs.get("resource_exhaustion", 0) > 0,
         "log_resource_pressure", "warning", "Correlacione os eventos de falta de recursos com uso de disco e memória."),
        (logs.get("database_locked", 0) >= 2,
         "log_database_contention", "warning", "Meça a concorrência de leitura e escrita e procure rotinas coincidentes."),
        (logs.get("rate_limit", 0) >= 3,
         "log_rate_limit", "warning", "Compare concorrência, tentativas e cotas por provedor antes de ajustar o roteador."),
        (logs.get("authentication", 0) >= 2,
         "log_authentication", "warning", "Verifique a configuração e a validade das credenciais no provedor correspondente."),
        (logs.get("timeout", 0) >= 3,
         "log_timeouts", "warning", "Correlacione latência com chamadas de modelo, memória, ferramentas e tarefas agendadas."),
        (logs.get("memory_failure", 0) >= 2,
         "log_memory", "warning", "Teste o recall no provedor ativo e compare latência, disponibilidade e qualidade."),
        (logs.get("plugin_failure", 0) >= 2,
         "log_plugins", "warning", "Revise a configuração e inicialização de plugins e conectores com erros recorrentes."),
    )
    for observed, identifier, severity, action in signals:
        if observed:
            findings.append({"id": identifier, "severity": severity, "recommendation": action})
    return findings


def snapshot(
    home: Path,
    *,
    upstream_tag: str | None = None,
    provider: str | None = None,
    running_version: str | None = None,
) -> dict:
    """Inspect one profile with bounded reads. Only aggregate or allowlisted values leave this module."""
    home = Path(home).expanduser()
    gateway = _gateway(home)
    report = {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "profile_present": home.is_dir(),
        "gateway": gateway,
        "host": _host(home),
        "database": _database(home),
        "memory": _memory(home, provider),
        "logs": _log_signals(home),
        "update": _update_check(running_version or gateway["reported_version"], upstream_tag),
    }
    report["findings"] = _findings(report)
    return report
