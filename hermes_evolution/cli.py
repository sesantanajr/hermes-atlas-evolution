"""Operator commands for the Hermes plugin and the standalone runner."""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

from .diagnostics import snapshot
from .memory import CATEGORIES, GLOBAL_SCOPE, MemoryStore
from .operations import RETENTION_DAYS, database, recommendations, run_policy, summarize

RESOURCE_FINDINGS = {"low_disk": "disk", "low_memory": "memory", "cgroup_memory_limit": "cgroup_memory"}


def settings(home: Path) -> dict:
    try:
        with (home / "evolution" / "settings.json").open("rb") as stream:
            body = stream.read(65_537)
    except FileNotFoundError:
        return {}
    if len(body) > 65_536:
        raise ValueError("Evolution settings exceed 64 KiB")
    result = json.loads(body)
    if not isinstance(result, dict) or not isinstance(result.get("rates_usd_per_million", {}), dict):
        raise ValueError("Invalid Evolution settings object or rate table")
    return result


def setup(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="evolution_action", required=True)
    health = commands.add_parser("health", help="Inspect Hermes files and host signals")
    usage = commands.add_parser("usage", help="Summarize provider and tool measurements")
    tick = commands.add_parser("tick", help="Run maintenance and record decisions")
    for command in (health, tick):
        command.add_argument("--upstream-tag", help="Reference version to compare against offline")
    for command in (usage, tick):
        command.add_argument("--days", type=int, choices=range(1, RETENTION_DAYS + 1), default=7, metavar="1..35")
    tick.add_argument("--changes-only", action="store_true", help="Only print when a decision changes")

    memory = commands.add_parser("memory", help="Access legacy 0.1 note data")
    audience = memory.add_mutually_exclusive_group()
    audience.add_argument("--shared", action="store_true", help="Explicitly select the profile-wide store")
    audience.add_argument("--dm", nargs=3, metavar=("PLATFORM", "USER", "CHAT"), help="Select a direct chat store")
    memory.add_argument("--namespace", default="", help="Optional platform tenant or workspace ID for --dm")
    tasks = memory.add_subparsers(dest="memory_action", required=True)
    add = tasks.add_parser("remember")
    add.add_argument("category", choices=CATEGORIES)
    add.add_argument("text")
    add.add_argument("--confidence", type=float, default=1.0)
    add.add_argument("--tag", action="append", default=[])
    add.add_argument("--promote", action="store_true", help="Explicitly confirm a personal profile entry")
    recall = tasks.add_parser("recall")
    recall.add_argument("query")
    recall.add_argument("--category", action="append", default=[], choices=CATEGORIES)
    recall.add_argument("--include-shared", action="store_true")
    recall.add_argument("--limit", type=int, default=5)
    evaluate = tasks.add_parser("evaluate", help="Measure retrieval against a labeled JSON file")
    evaluate.add_argument("cases", type=Path)
    evaluate.add_argument("--limit", type=int, default=5)
    evaluate.add_argument("--include-shared", action="store_true")
    tasks.add_parser("audit")
    remove = tasks.add_parser("forget")
    remove.add_argument("id", type=int)

    feedback = commands.add_parser("feedback", help="Record how an operational finding performed")
    feedback.add_argument("code")
    feedback.add_argument("result", choices=("helpful", "dismissed", "reset"))
    commands.add_parser("history", help="Show learned maintenance priorities and feedback")

    ops = commands.add_parser("ops", help="Inspect and supervise Hermes and Linux resource contention")
    ops_commands = ops.add_subparsers(dest="ops_action", required=True)
    for name in ("inspect", "tick"):
        command = ops_commands.add_parser(name)
        command.add_argument("--days", type=int, choices=range(1, RETENTION_DAYS + 1), default=7, metavar="1..35")
        command.add_argument("--upstream-tag")
        command.add_argument("--hermes-source", type=Path, help="Include the bundled Hermes source inventory")
    ops_commands.add_parser("history")
    ops_commands.add_parser("rollback")
    similar = ops_commands.add_parser("similar", help="Find previous diagnostic patterns")
    similar.add_argument("query")
    similar.add_argument("--limit", type=int, choices=range(1, 11), default=5, metavar="1..10")


def _memory(conn, args):
    scope = "local"
    if args.namespace and not args.dm:
        raise ValueError("--namespace requires --dm")
    if args.shared:
        scope = GLOBAL_SCOPE
    if args.dm:
        from .integration import dm_scope

        scope = dm_scope(*args.dm, args.namespace)
    store = MemoryStore(conn)
    if args.memory_action == "remember":
        return store.remember(scope, args.category, args.text, source="operator", confidence=args.confidence,
                              tags=args.tag, promote=args.promote)
    if args.memory_action == "recall":
        return store.search(scope, args.query, categories=args.category, limit=args.limit,
                            include_global=args.include_shared)
    if args.memory_action == "evaluate":
        with args.cases.open("rb") as stream:
            body = stream.read(131_073)
        if len(body) > 131_072:
            raise ValueError("Evaluation file exceeds 128 KiB")
        return store.evaluate(scope, json.loads(body), limit=args.limit, include_global=args.include_shared)
    if args.memory_action == "forget":
        return {"removed": store.forget(scope, args.id)}
    return store.audit(scope)


def _report(args, home: Path, *, running_version=None, provider=None, now: float, runtime_config=None,
            hermes_source=None, supplemental=(), health_override=None):
    action = args.evolution_action
    if action == "ops":
        return _ops(args, home, now=now, running_version=running_version, provider=provider,
                    runtime_config=runtime_config, source=hermes_source)
    health = None
    if action in {"health", "tick"}:
        health = health_override or snapshot(home, upstream_tag=args.upstream_tag, provider=provider,
                                             running_version=running_version)
        if action == "health":
            return health
    config = settings(home) if action in {"usage", "tick"} else {}
    rates = config.get("rates_usd_per_million", {})
    with database(home) as conn:
        if action == "memory":
            if args.memory_action == "remember" and settings(home).get("legacy_memory_commands") is not True:
                raise ValueError("New legacy notes require legacy_memory_commands in Evolution settings")
            return _memory(conn, args)
        if action == "feedback":
            result = None if args.result == "reset" else args.result
            if not conn.execute("UPDATE maintenance SET feedback=? WHERE code=?", (result, args.code)).rowcount:
                raise ValueError("Unknown finding code. Run tick first.")
            return {"code": args.code, "feedback": result}
        if action == "history":
            return [dict(row) for row in conn.execute("SELECT * FROM maintenance ORDER BY active DESC,last_seen DESC,code")]
        measured = summarize(conn, days=args.days, now=now, rates=rates)
        if action == "tick":
            assert health is not None
            findings = [{"code": item["id"], "severity": item["severity"], "action": item["recommendation"]}
                        for item in health["findings"]]
            for item in findings:
                if item["code"] in RESOURCE_FINDINGS:
                    item["reduce_context"] = True
            for code, measurement in RESOURCE_FINDINGS.items():
                if health["host"][measurement] is None and conn.execute(
                    "SELECT 1 FROM maintenance WHERE code=? AND active=1", (code,)
                ).fetchone():
                    findings.append({"code": code, "severity": "warning", "reduce_context": True,
                                     "action": "Confirm recovery of the previously reported resource pressure."})
            if health["database"]["status"] not in {"ok", "integrity_error"} and conn.execute(
                "SELECT 1 FROM maintenance WHERE code='state_integrity' AND active=1"
            ).fetchone():
                findings.append({"code": "state_integrity", "severity": "critical",
                                 "action": "Confirm recovery of the previously reported native database integrity issue.",
                                 "current_check": health["database"]["status"]})
            findings += recommendations(measured)
            findings.extend(supplemental)
            recovery_ready = True
            budget = config.get("daily_estimated_budget_usd")
            if budget is not None:
                if type(budget) not in (int, float) or not 0 < budget <= 1_000_000 or not math.isfinite(budget):
                    raise ValueError("daily_estimated_budget_usd must be positive and at most 1000000")
                day_usage = measured if args.days == 1 else summarize(conn, days=1, now=now, rates=rates)
                amount = day_usage["estimated_usd"]
                recovery_ready = recovery_ready and day_usage["successful_llm_calls"] == day_usage["priced_calls"]
                if amount is not None and amount >= budget:
                    findings.append({"code": "evolution_daily_budget", "severity": "warning", "reduce_context": True,
                                     "observed_estimate_usd": amount, "daily_budget_usd": budget,
                                     "action": "Optional Evolution context paused. Review current provider and job usage."})
            legacy_table = conn.execute("SELECT 1 FROM sqlite_master WHERE name='evolution_memories' AND type='table'").fetchone()
            quality = MemoryStore(conn).audit("local", include_global=True) if legacy_table else None
            if quality and (quality["quality"]["low_confidence"] or quality["quality"]["repeated_across_categories"]):
                findings.append({"code": "evolution_memory_review", "severity": "info", "evidence": quality["quality"],
                                 "action": "Review low-confidence memories and cross-category repeats."})
            decision = run_policy(conn, findings, now=now, recovery_ready=recovery_ready)
            if args.changes_only and not decision["changes"]:
                return None
            return {"decision": decision, "health": health, "memory_quality_local_and_shared": quality,
                    "observer_usage": measured}
    from .billing import native_billing

    return {"observer_usage": measured, "hermes_native_billing": native_billing(home, days=args.days, now=now)}


def _ops(args, home: Path, *, now, running_version, provider, runtime_config, source):
    from .inventory import scan_inventory
    from .linux_actions import SystemPolicy, Systemd
    from .linux_host import scan_host
    from .reasoning import diagnose
    from .supervisor import _journal, baseline_metrics, journal_history, learned_baseline, record_observations, supervise
    from .telemetry import research_summary

    action = args.ops_action
    if action == "history":
        return journal_history(home)
    if action == "rollback":
        with _journal(home) as journal:
            _, result = supervise(journal, Systemd(), None, now=now, safe=False, rollback=True)
        return result
    config = settings(home)
    if action == "similar":
        from .semantic_ops import match_incidents

        model = config.get("semantic_incident_model")
        if model is not None and (not isinstance(model, str) or not Path(model).is_absolute()):
            raise ValueError("semantic_incident_model must be an absolute local model directory")
        return match_incidents(args.query, journal_history(home)["incidents"], model_path=Path(model) if model else None,
                               limit=args.limit)
    health = snapshot(home, upstream_tag=args.upstream_tag, provider=provider, running_version=running_version)
    host = scan_host(home)
    cfg = (runtime_config or {}).get("plugins", {})
    cfg = cfg if isinstance(cfg, dict) else {}
    inventory = scan_inventory(home, source=args.hermes_source or source, enabled_plugins=cfg.get("enabled"),
                               disabled_plugins=cfg.get("disabled"), now=now)
    rates = config.get("rates_usd_per_million", {})
    with database(home) as conn:
        usage = summarize(conn, days=args.days, now=now, rates=rates)
        research = research_summary(conn, days=args.days, now=now)
        daily_usage = summarize(conn, days=1, now=now, rates={}) if action == "tick" else None
        daily_research = research_summary(conn, days=1, now=now) if action == "tick" else None
    result = {"health": health, "linux_host": host, "inventory": inventory, "usage": usage, "research": research}
    if action == "inspect":
        result["diagnoses"] = diagnose(health, usage, host=host, inventory=inventory, telemetry={"research": research})
        return result
    policy = SystemPolicy.read(config)
    safe = (health["profile_present"] and health["database"]["status"] in ("ok", "integrity_skipped_large")
            and not any(item["severity"] == "critical" or item["id"] in ("gateway_degraded", "gateway_startup_failure")
                        for item in health["findings"]))
    with _journal(home) as journal:
        history = learned_baseline(journal, now=now)
        current_baseline = baseline_metrics(daily_usage, daily_research)

        def diagnose_with(candidates):
            return diagnose(health, usage, host=host, inventory=inventory,
                            telemetry={"research": research, "contention_candidates": candidates,
                                       "current_daily_baseline": current_baseline}, history=history)

        def approved_by_evidence(candidate):
            return any(f"rebalance_background_{candidate['kind']}" in issue["action_ids"]
                       for issue in diagnose_with([candidate]))

        candidates, outcome = supervise(journal, Systemd(), policy, now=now, safe=safe, allow_kind=approved_by_evidence)
        issues = diagnose_with(candidates)
        record_observations(journal, issues, daily_usage, daily_research, now=now)
    context_actions = [{"code": issue["code"], "action": issue["recommendation"], "severity": issue["severity"],
                        "reduce_context": True} for issue in issues if "pause_optional_context" in issue["action_ids"]]
    args_for_tick = SimpleNamespace(evolution_action="tick", upstream_tag=args.upstream_tag,
                                    days=args.days, changes_only=False)
    maintenance = _report(args_for_tick, home, now=now, health_override=health, supplemental=context_actions)
    return {**result, "diagnoses": issues, "paired_contention": candidates, "host_action": outcome,
            "maintenance": maintenance["decision"]}


def execute(args, home: Path, *, running_version=None, provider=None, runtime_config=None, hermes_source=None) -> None:
    try:
        result = _report(args, home, running_version=running_version, provider=provider, now=time.time(),
                         runtime_config=runtime_config, hermes_source=hermes_source)
    except (OSError, ValueError, sqlite3.Error) as error:
        raise SystemExit(f"Evolution: {error}") from None
    if result is not None:
        print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False))


def main():
    parser = argparse.ArgumentParser(prog="python -m hermes_evolution")
    parser.add_argument("--home", type=Path, default=os.environ.get("HERMES_HOME"), help="Explicit Hermes profile path")
    parser.add_argument("--hermes-version", help="Installed version if running outside Hermes")
    setup(parser)
    args = parser.parse_args()
    if args.home is None:
        parser.error("Pass --home or set HERMES_HOME")
    execute(args, args.home.expanduser().resolve(), running_version=args.hermes_version)
