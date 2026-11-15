"""Structured counters for Hermes-native delegation, compaction and maintenance."""

from __future__ import annotations

import time
from pathlib import Path

from .operations import _day, record

HOOKS = ("subagent_start", "subagent_stop", "post_compaction", "post_curator_run", "on_kanban_worker_exited")
DELEGATE_OUTCOMES = {"completed", "failed", "error", "partial", "timeout", "cancelled", "interrupted"}
WORKER_OUTCOMES = {"clean_exit", "rate_limited", "nonzero_exit", "signaled", "unknown"}


def _count(value, limit=1_000_000_000):
    return value if type(value) is int and 0 <= value <= limit else None


def observe_native(home: Path, hook: str, payload: dict, *, now: float | None = None) -> None:
    if hook not in HOOKS:
        raise ValueError("Unsupported native event")
    duration, tokens_before, tokens_after, measured = 0, 0, 0, 0
    outcome = "observed"
    if hook == "subagent_stop":
        status = payload.get("child_status")
        outcome = status if isinstance(status, str) and status in DELEGATE_OUTCOMES else "unknown"
        duration = _count(payload.get("duration_ms"), 86_400_000) or 0
    if hook == "post_compaction":
        before, after = _count(payload.get("before_tokens")), _count(payload.get("after_tokens"))
        if before is not None and after is not None:
            tokens_before, tokens_after, measured = before, after, 1
    if hook == "post_curator_run":
        dry_run = payload.get("dry_run")
        outcome = "dry_run" if dry_run is True else "executed" if dry_run is False else "unknown"
    if hook == "on_kanban_worker_exited":
        status = payload.get("exit_kind")
        outcome = status if isinstance(status, str) and status in WORKER_OUTCOMES else "unknown"
    now = time.time() if now is None else now
    row = (_day(now), hook, outcome, duration, tokens_before, tokens_after, measured)
    record(home, lambda conn: conn.execute(
        """INSERT INTO native_activity VALUES(?,?,?,1,?,?,?,?)
            ON CONFLICT DO UPDATE SET events=events+1, duration_ms=duration_ms+excluded.duration_ms,
            tokens_before=tokens_before+excluded.tokens_before, tokens_after=tokens_after+excluded.tokens_after,
            measured=measured+excluded.measured""", row), now=now)


def native_summary(conn, *, now: float, days: int) -> dict:
    rows = [dict(row) for row in conn.execute("""SELECT hook,outcome,SUM(events) AS events, SUM(duration_ms) AS duration_ms,
          SUM(tokens_before) AS tokens_before, SUM(tokens_after) AS tokens_after, SUM(measured) AS measured
          FROM native_activity WHERE day BETWEEN ? AND ? GROUP BY hook,outcome ORDER BY hook,outcome""",
                                            (_day(now - (days - 1) * 86400), _day(now)))]
    return {"basis": "events_seen_by_this_profile_plugin", "groups": rows}


def native_findings(summary: dict) -> list[dict]:
    rows = summary["groups"]
    children = [row for row in rows if row["hook"] == "subagent_stop"]
    measured = sum(row["events"] for row in children if row["outcome"] not in ("unknown", "cancelled", "interrupted"))
    failures = sum(row["events"] for row in children if row["outcome"] in ("failed", "error", "timeout"))
    worker_limited = sum(row["events"] for row in rows if row["hook"] == "on_kanban_worker_exited"
                         and row["outcome"] == "rate_limited")
    findings = []
    if measured >= 5 and failures >= 2 and failures / measured >= 0.3:
        findings.append({"code": "delegation_failure_rate", "severity": "warning", "confidence": "medium",
                         "hypothesis": "As delegações observadas tiveram falhas recorrentes.",
                         "evidence": [{"signal": "delegation.finished_measured", "value": measured},
                                      {"signal": "delegation.failed", "value": failures}],
                         "alternatives": ["Escopo impreciso, concorrência e falhas do provedor podem ter o mesmo efeito."],
                         "action_ids": ["review_delegation_contract_and_concurrency"],
                         "recommendation": "Compare tarefas por tamanho e teste menos workers com critérios verificáveis."})
    if worker_limited >= 3:
        findings.append({"code": "kanban_worker_rate_limits", "severity": "warning", "confidence": "high",
                         "hypothesis": "Workers Kanban registraram saídas por limite de taxa.",
                         "evidence": [{"signal": "kanban.worker_rate_limited", "value": worker_limited}],
                         "alternatives": ["A cota pode ser compartilhada com outros perfis ou rotinas de Cron."],
                         "action_ids": ["review_dispatch_concurrency"],
                         "recommendation": "Compare os horários de despacho e Cron antes de aumentar concorrência."})
    compaction = [row for row in rows if row["hook"] == "post_compaction"]
    samples = sum(row["measured"] for row in compaction)
    before = sum(row["tokens_before"] for row in compaction)
    after = sum(row["tokens_after"] for row in compaction)
    if samples >= 5 and before >= 30_000 and after / before >= 0.9:
        findings.append({"code": "compaction_low_measured_reduction", "severity": "info", "confidence": "medium",
                         "hypothesis": "As compactações medidas reduziram pouco o contexto agregado.",
                         "evidence": [{"signal": "compaction.samples", "value": samples},
                                      {"signal": "compaction.tokens_before", "value": before},
                                      {"signal": "compaction.tokens_after", "value": after}],
                         "alternatives": ["Compactações diferentes podem preservar informações úteis ou refletir cargas distintas."],
                         "action_ids": ["validate_native_compaction_on_fixed_tasks"],
                         "recommendation": "Repita um conjunto de tarefas com o compressor nativo e confira qualidade e tokens."})
    return findings
