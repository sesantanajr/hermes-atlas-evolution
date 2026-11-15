"""Bounded, metadata-only inspection of Hermes cron and Kanban workloads."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from contextlib import closing
from datetime import datetime
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import time

from .diagnostics import _checkpoint_unchanged
from .reasoning import _finding

_CRON_BYTES = 2 * 1024 * 1024
_JOB_LIMIT = 2048
_BOARD_LIMIT = 12
_BOARD_ENTRIES = 64
_KANBAN_MAX_BYTES = 64 * 1024 * 1024
_KANBAN_SCAN_SECONDS = 0.35
_RUN_WINDOW = 86400
_UPCOMING_SECONDS = 900
_PAST_DUE_SECONDS = 300
_FIRE_CLAIM_SECONDS = 300
_TASK_STATES = ("triage", "todo", "ready", "running", "review", "blocked", "done", "archived", "failed", "cancelled")


def _mapping(value) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _positive_int(value) -> int | None:
    return value if type(value) is int and 0 < value <= 10**9 else None


def _timestamp(value) -> float | None:
    if isinstance(value, str) and len(value) <= 64:
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is not None:
                return parsed.timestamp()
        except (ValueError, OverflowError, OSError):
            pass
    return None


def _stat_local(path: Path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
        raise OSError("linked workload path")
    return info


def _regular_file(path: Path):
    info = _stat_local(path)
    if not stat.S_ISREG(info.st_mode):
        raise OSError("unsupported workload file")
    return info


def _directory(path: Path) -> None:
    if not stat.S_ISDIR(_stat_local(path).st_mode):
        raise OSError("unsupported workload directory")


def _load_json(path: Path, limit: int) -> tuple[str, object]:
    try:
        info = _regular_file(path)
        if info.st_size > limit:
            return "file_limit", None
        with path.open("rb") as stream:
            if not os.path.samestat(info, os.fstat(stream.fileno())):
                return "snapshot_changed", None
            raw = stream.read(limit + 1)
    except FileNotFoundError:
        return "not_found", None
    except OSError:
        return "unreadable", None
    if len(raw) > limit:
        return "file_limit", None
    try:
        return "ok", json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError, RecursionError):
        return "invalid_format", None


def _short_interval(schedule: Mapping) -> bool:
    if schedule.get("kind") == "interval":
        minutes = _positive_int(schedule.get("minutes"))
        return minutes is not None and minutes <= 5
    expression = schedule.get("expr")
    if schedule.get("kind") == "cron" and isinstance(expression, str) and len(expression) <= 128:
        fields = expression.split()
        return len(fields) == 5 and fields[0] in ("*", "*/1", "*/2", "*/3", "*/4", "*/5") and fields[1:] == ["*"] * 4
    return False


def _burst(instants: list[float]) -> int:
    earliest = maximum = 0
    ordered = sorted(instants)
    for i, instant in enumerate(ordered):
        while instant - ordered[earliest] >= 60:
            earliest += 1
        maximum = max(maximum, i - earliest + 1)
    return maximum


def _cron(home: Path, config: Mapping, now: float) -> dict:
    result = {"status": "unreadable", "scope": "profile_home", "history_window_seconds": _RUN_WINDOW,
              "configured_max_parallel_jobs": _positive_int(config.get("max_parallel_jobs"))}
    try:
        _directory(home / "cron")
    except FileNotFoundError:
        return {**result, "status": "not_found"}
    except OSError:
        return result
    status, raw = _load_json(home / "cron" / "jobs.json", _CRON_BYTES)
    if status != "ok":
        return {**result, "status": status}
    jobs = raw.get("jobs") if isinstance(raw, dict) else raw
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    if not isinstance(jobs, list):
        return {**result, "status": "invalid_format"}

    counts = Counter()
    starts = []
    agent_starts = []
    for job in jobs[:_JOB_LIMIT]:
        if not isinstance(job, dict):
            counts["invalid_records"] += 1
            continue
        counts["jobs"] += 1
        last_status = job.get("last_status")
        last_ran = _timestamp(job.get("last_run_at"))
        last_failure = _timestamp(_mapping(job.get("last_failure")).get("at"))
        if last_failure is None and last_status in ("error", "failed", "timeout"):
            last_failure = last_ran
        if last_failure is not None and now - _RUN_WINDOW <= last_failure <= now:
            counts["recent_job_failures"] += 1
        if last_status == "delivery_failed" and last_ran is not None and now - _RUN_WINDOW <= last_ran <= now:
            counts["recent_delivery_failures"] += 1
        if job.get("enabled") is not True or job.get("state") in ("paused", "completed", "exhausted"):
            counts["inactive_jobs"] += 1
            continue
        counts["enabled_jobs"] += 1
        no_agent = job.get("no_agent") is True
        counts["script_only_jobs" if no_agent else "agent_jobs"] += 1
        if (_positive_int(job.get("failure_streak")) or 0) >= 2:
            counts["repeated_failures"] += 1
        if not no_agent and _short_interval(_mapping(job.get("schedule"))):
            counts["frequent_agent_jobs"] += 1
        claim_time = _timestamp(_mapping(job.get("fire_claim")).get("at"))
        recently_claimed = claim_time is not None and now - _FIRE_CLAIM_SECONDS <= claim_time <= now
        counts["recent_fire_claims"] += int(recently_claimed)
        next_run = _timestamp(job.get("next_run_at"))
        if next_run is None:
            counts["unverified_next_run"] += 1
        elif next_run < now - _PAST_DUE_SECONDS and not recently_claimed:
            counts["past_due_timestamps"] += 1
        elif now <= next_run <= now + _UPCOMING_SECONDS:
            starts.append(next_run)
            if not no_agent:
                agent_starts.append(next_run)
    keys = ("jobs", "enabled_jobs", "inactive_jobs", "invalid_records", "script_only_jobs", "agent_jobs",
            "recent_job_failures", "recent_delivery_failures", "repeated_failures", "frequent_agent_jobs",
            "unverified_next_run", "past_due_timestamps", "recent_fire_claims")
    partial = len(jobs) > _JOB_LIMIT or counts["invalid_records"] > 0
    return {**result, "status": "partial" if partial else "ok", "counts": {key: counts[key] for key in keys},
            "row_limit_reached": len(jobs) > _JOB_LIMIT,
            "schedule": {"horizon_seconds": _UPCOMING_SECONDS, "burst_window_seconds": 60,
                         "past_due_threshold_seconds": _PAST_DUE_SECONDS, "upcoming_starts": len(starts),
                         "peak_scheduled_starts": _burst(starts), "peak_scheduled_agent_starts": _burst(agent_starts)}}


def _named_boards(root: Path, deadline: float):
    boards = root / "kanban" / "boards"
    paths = []
    partial = False
    try:
        _directory(boards.parent)
        _directory(boards)
        with os.scandir(boards) as entries:
            for index, entry in enumerate(entries):
                if index >= _BOARD_ENTRIES or time.monotonic() >= deadline:
                    return sorted(paths), True
                directory = Path(entry.path)
                if entry.name in ("default", "_archived") or not entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    _directory(directory)
                except OSError:
                    partial = True
                    continue
                status, metadata = _load_json(directory / "board.json", 64 * 1024)
                if status == "ok" and _mapping(metadata).get("archived") is True:
                    continue
                if len(paths) >= _BOARD_LIMIT:
                    return sorted(paths), True
                paths.append(directory / "kanban.db")
            return sorted(paths), partial
    except FileNotFoundError:
        return [], False
    except OSError:
        return sorted(paths), True


def _board_counts(db: sqlite3.Connection, now: float, stale_seconds: int) -> dict | None:
    columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
    if not {"status", "assignee", "created_at", "started_at", "last_heartbeat_at", "claim_expires", "consecutive_failures"} <= columns:
        return None
    statuses = ",".join(f"COUNT(CASE WHEN status = '{state}' THEN 1 END)" for state in _TASK_STATES)
    tasks = db.execute(f"""
        SELECT {statuses}, COUNT(CASE WHEN status='ready' AND assignee IS NOT NULL THEN 1 END),
               COUNT(CASE WHEN status='ready' AND assignee IS NULL THEN 1 END),
               COUNT(CASE WHEN status='ready' AND created_at BETWEEN 1 AND ? THEN 1 END),
               COUNT(CASE WHEN status='running' AND claim_expires BETWEEN 1 AND ? THEN 1 END),
               COUNT(CASE WHEN status='running' AND started_at BETWEEN 1 AND ?
                            AND (last_heartbeat_at IS NULL OR last_heartbeat_at BETWEEN 1 AND ?) THEN 1 END),
               COUNT(CASE WHEN status IN ('ready','blocked','triage','running') AND consecutive_failures >= 2 THEN 1 END)
        FROM tasks
    """, (now - 1800, now, now - stale_seconds, now - 3600)).fetchone()
    counts = dict(zip((*_TASK_STATES, "ready_with_assignee", "ready_without_assignee", "ready_created_over_30m",
                       "expired_claims", "stale_running_candidates", "repeated_task_failures"), tasks))
    run_columns = {row[1] for row in db.execute("PRAGMA table_info(task_runs)")}
    if {"status", "ended_at"} <= run_columns:
        finished, failed = db.execute("""
            SELECT COUNT(*), COUNT(CASE WHEN status IN ('failed', 'crashed', 'timed_out') THEN 1 END)
            FROM task_runs WHERE ended_at BETWEEN ? AND ? AND status IN ('done','failed','crashed','timed_out')
        """, (now - _RUN_WINDOW, now)).fetchone()
        counts.update(finished_runs_24h=finished, failed_runs_24h=failed)
    return counts


def _read_board(path: Path, now: float, stale_seconds: int, deadline: float):
    try:
        info = _regular_file(path)
        sidecars = [path.with_name(path.name + suffix) for suffix in ("-wal", "-shm", "-journal")]
        parts = []
        for item in sidecars:
            try:
                parts.append(_regular_file(item))
            except FileNotFoundError:
                parts.append(None)
        checkpoint = not any(parts)
        if parts[2] or (not checkpoint and (not parts[0] or not parts[1])):
            return "snapshot_unavailable", {}
        if info.st_size + (parts[0].st_size if parts[0] else 0) > _KANBAN_MAX_BYTES:
            return "file_limit", {}
        options = "?mode=ro&immutable=1" if checkpoint else "?mode=ro&readonly_shm=1"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "scan_timeout", {}
        with closing(sqlite3.connect(path.as_uri() + options, uri=True, timeout=min(remaining, 0.035))) as db:
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 500)
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("BEGIN")
            counts = _board_counts(db, now, stale_seconds)
        if checkpoint and not _checkpoint_unchanged(path, info, sidecars):
            return "snapshot_changed", {}
        return ("ok", counts) if counts is not None else ("unsupported_schema", {})
    except FileNotFoundError:
        return "not_found", {}
    except (OSError, sqlite3.Error):
        return "unreadable", {}


def _kanban(root: Path, config: Mapping, now: float, *, scope: str) -> dict:
    deadline = time.monotonic() + _KANBAN_SCAN_SECONDS
    named, partial = _named_boards(root, deadline)
    paths = [root / "kanban.db", *named]
    limits = len(paths) > _BOARD_LIMIT or partial
    statuses = Counter()
    counts = Counter()
    stale_seconds = _positive_int(config.get("dispatch_stale_timeout_seconds")) or 14400
    run_coverage = 0
    for path in paths[:_BOARD_LIMIT]:
        state, metrics = _read_board(path, now, stale_seconds, deadline)
        statuses[state] += 1
        counts.update(metrics)
        run_coverage += int("finished_runs_24h" in metrics)
    reads = statuses.pop("ok", 0)
    statuses.pop("not_found", None)
    incomplete = bool(statuses or limits or run_coverage < reads)
    return {"status": ("partial" if incomplete else "ok") if reads else ("unavailable" if incomplete else "not_found"),
            "scope": scope, "boards_read": reads, "boards_with_run_history": run_coverage,
            "board_limit_reached": limits, "unavailable": dict(statuses), "counts": dict(counts),
            "configured_max_in_progress": _positive_int(config.get("max_in_progress")),
            "configured_max_in_progress_per_profile": _positive_int(config.get("max_in_progress_per_profile")),
            "stale_running_threshold_seconds": stale_seconds}


def _recommendations(cron: dict, kanban: dict) -> list[dict]:
    counts = cron.get("counts", {})
    board = kanban["counts"]
    schedule = cron.get("schedule", {})
    peak = schedule.get("peak_scheduled_starts", 0)
    cap = cron["configured_max_parallel_jobs"]
    if peak >= 3 or (cap and peak > cap):
        yield _finding("cron_schedule_concentration", "info", "medium", "Vários disparos do cron estão previstos no mesmo minuto.",
                       {"cron.peak_starts_60s": peak, "cron.peak_agent_starts_60s": schedule["peak_scheduled_agent_starts"],
                        "cron.configured_max_parallel_jobs": cap},
                       ("Os horários são projeções dos próximos disparos. Duração e concorrência efetiva podem variar.",),
                       ("review_cron_staggering",), "Distribua horários coincidentes e compare atraso, pressão de recursos e o limite nativo cron.max_parallel_jobs.")
    if counts.get("frequent_agent_jobs", 0) >= 2:
        yield _finding("cron_frequent_agent_jobs", "info", "medium", "Há rotinas com agente programadas a cada cinco minutos ou menos.",
                       {"cron.frequent_agent_jobs": counts["frequent_agent_jobs"]},
                       ("A frequência pode ser intencional se a rotina depende de análise ou redação.",),
                       ("review_cron_script_only",), "Considere o modo nativo no_agent com script para verificações de resultado determinístico.")
    if counts.get("past_due_timestamps", 0) or counts.get("repeated_failures", 0) or counts.get("recent_job_failures", 0):
        yield _finding("cron_dispatch_or_failures", "warning", "medium", "Há sinais de atraso ou falha nos disparos do cron.",
                       {"cron.past_due_timestamps": counts["past_due_timestamps"], "cron.repeated_failures": counts["repeated_failures"],
                        "cron.recent_job_failures": counts["recent_job_failures"]},
                       ("Uma retomada do gateway, política de recuperação ou execução ativa pode explicar horários vencidos.",),
                       ("inspect_native_cron_status",), "Confira hermes cron list e o estado do agendador antes de reenfileirar uma rotina.")
    if counts.get("recent_delivery_failures", 0):
        yield _finding("cron_delivery_failures", "warning", "high", "O cron registrou entregas recentes com falha.",
                       {"cron.recent_delivery_failures": counts["recent_delivery_failures"]},
                       ("O destino pode ter recebido a mensagem antes do timeout de confirmação.",),
                       ("review_cron_delivery",), "Valide a saída e a entrega nativa antes de reexecutar uma rotina que pode ter produzido efeito externo.")
    if board.get("ready_created_over_30m", 0) or board.get("ready_without_assignee", 0) or board.get("blocked", 0):
        yield _finding("kanban_queue_review", "warning", "medium", "A fila do Kanban contém trabalho pronto antigo, sem responsável ou bloqueado.",
                       {"kanban.ready_created_over_30m": board["ready_created_over_30m"],
                        "kanban.ready_without_assignee": board["ready_without_assignee"], "kanban.blocked": board["blocked"],
                        "kanban.running": board["running"], "kanban.configured_max_in_progress": kanban["configured_max_in_progress"]},
                       ("A idade conta desde a criação do cartão. Dependências, filtros de despacho e quotas podem afetar a fila.",),
                       ("review_kanban_dispatch_capacity",), "Revise dependências, responsáveis, perfis autorizados e limites de despacho antes de aumentar a concorrência.")
    if board.get("expired_claims", 0) or board.get("stale_running_candidates", 0) or board.get("repeated_task_failures", 0):
        yield _finding("kanban_worker_recovery_review", "warning", "medium", "Há tarefas com lease vencida, sinal de vida antigo ou falhas repetidas.",
                       {"kanban.expired_claims": board["expired_claims"], "kanban.stale_running_candidates": board["stale_running_candidates"],
                        "kanban.repeated_task_failures": board["repeated_task_failures"],
                        "kanban.failed_runs_24h": board.get("failed_runs_24h")},
                       ("O worker pode estar ativo ou o dispatcher pode estar prestes a recuperar a tarefa.",),
                       ("inspect_native_kanban_dispatcher",), "Confira o dispatcher e a execução nativa antes de tentar retomar, reclamar ou reatribuir a tarefa.")


def inspect_workloads(home: Path, *, provider_options: dict | None = None, now: float | None = None) -> dict:
    """Observe only explicitly scoped stores and return counts with their coverage."""
    now = time.time() if now is None else now
    if type(now) not in (int, float) or not math.isfinite(now):
        raise ValueError("Workload inspection requires a finite observation time")
    home = Path(home).resolve()
    options = _mapping(provider_options)
    kanban_home = options.get("kanban_home")
    if isinstance(kanban_home, (str, Path)) and Path(kanban_home).is_absolute():
        kanban_root = Path(kanban_home).resolve()
    else:
        kanban_root = home
    cron = _cron(home, _mapping(options.get("cron")), now)
    kanban = _kanban(kanban_root, _mapping(options.get("kanban")), now,
                      scope="profile_home" if kanban_root == home else "shared_root")
    delegation = _mapping(options.get("delegation"))
    return {"cron": cron, "kanban": kanban,
            "delegation": {"status": "config_only" if delegation else "configuration_unavailable",
                           "configured_max_concurrent_children": _positive_int(delegation.get("max_concurrent_children"))},
            "recommendations": list(_recommendations(cron, kanban))}
