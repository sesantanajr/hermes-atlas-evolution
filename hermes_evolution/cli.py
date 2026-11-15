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

from .advisory import EFFORTS, KINDS
from .diagnostics import snapshot
from .memory import CATEGORIES, GLOBAL_SCOPE, MemoryStore
from .native_actions import PLAYBOOKS
from .operations import RETENTION_DAYS, database, recommendations, run_policy, summarize

RESOURCE_FINDINGS = {"low_disk": "disk", "low_memory": "memory", "cgroup_memory_limit": "cgroup_memory"}
EVOLUTION_ACTIONS = ("learn", "lessons", "skills", "proposals", "experiments", "report")
CYCLE_INTERVAL_SECONDS = 3600


def settings(home: Path) -> dict:
    try:
        with (home / "evolution" / "settings.json").open("rb") as stream:
            body = stream.read(65_537)
    except FileNotFoundError:
        return {}
    if len(body) > 65_536:
        raise ValueError("Evolution settings exceed 64 KiB")
    try:
        result = json.loads(body)
    except RecursionError:
        raise ValueError("Evolution settings are nested too deeply") from None
    if not isinstance(result, dict) or not isinstance(result.get("rates_usd_per_million", {}), dict):
        raise ValueError("Invalid Evolution settings object or rate table")
    return result


PLUGIN_ID = "hermes-evolution"
# Keys the Hermes settings form edits (plugin.yaml config_schema) -> where they live in settings.json.
NATIVE_KEYS = {"lessons": ("evolution", "lessons"), "language": ("evolution", "language"),
               "tool_hints": ("evolution", "tool_hints"), "feedback_signals": ("evolution", "feedback_signals"),
               "slow_turn_seconds": ("evolution", "slow_turn_seconds"),
               "owner_sender_ids": ("evolution", "owner_sender_ids"), "agent_tools": ("agent_advice",)}


def native_settings(runtime_config) -> dict:
    """`plugins.entries.hermes-evolution.settings` from the Hermes configuration, when present."""
    plugins = runtime_config.get("plugins") if isinstance(runtime_config, dict) else None
    entries = plugins.get("entries") if isinstance(plugins, dict) else None
    entry = entries.get(PLUGIN_ID) if isinstance(entries, dict) else None
    values = entry.get("settings") if isinstance(entry, dict) else None
    return values if isinstance(values, dict) else {}


def effective_settings(home: Path, runtime_config=None) -> dict:
    """settings.json with the values set through Hermes' plugin settings on top (Hermes wins per key)."""
    result = settings(home)
    for key, path in NATIVE_KEYS.items():
        value = native_settings(runtime_config).get(key)
        if value is None:
            continue
        target = result
        for part in path[:-1]:
            nested = target.get(part)
            target[part] = nested = dict(nested) if isinstance(nested, dict) else {}
            target = nested
        target[path[-1]] = value
    return result


def _accept_lang_everywhere(parser: argparse.ArgumentParser, help_text: str) -> None:
    """Let `--lang` follow any subcommand too. A subcommand without it would reject the option."""
    for action in parser._actions:   # argparse exposes subparsers only through this attribute
        if isinstance(action, argparse._SubParsersAction):
            for child in dict.fromkeys(action.choices.values()):
                if "--lang" not in child._option_string_actions:
                    child.add_argument("--lang", choices=("en", "pt"), default=argparse.SUPPRESS, help=help_text)
                _accept_lang_everywhere(child, help_text)


def setup(parser: argparse.ArgumentParser) -> None:
    from .i18n import resolve, text

    language = resolve()   # help follows the profile language; output follows --lang or the settings

    def _(sentence: str) -> str:
        return text(sentence, language)

    parser.add_argument("--lang", choices=("en", "pt"), default=None, help=_("Language of lessons, messages and the digest"))
    commands = parser.add_subparsers(dest="evolution_action", required=True)
    health = commands.add_parser("health", help=_("Inspect Hermes files and host signals"))
    usage = commands.add_parser("usage", help=_("Summarize provider and tool measurements"))
    tick = commands.add_parser("tick", help=_("Run maintenance and record decisions"))
    for command in (health, tick):
        command.add_argument("--upstream-tag", help=_("Reference version to compare against offline"))
    for command in (usage, tick):
        command.add_argument("--days", type=int, choices=range(1, RETENTION_DAYS + 1), default=7, metavar="1..35")
    tick.add_argument("--changes-only", action="store_true", help=_("Only print when a decision changes"))

    memory = commands.add_parser("memory", help=_("Access legacy 0.1 note data"))
    audience = memory.add_mutually_exclusive_group()
    audience.add_argument("--shared", action="store_true", help=_("Explicitly select the profile-wide store"))
    audience.add_argument("--dm", nargs=3, metavar=("PLATFORM", "USER", "CHAT"), help=_("Select a direct chat store"))
    memory.add_argument("--namespace", default="", help=_("Optional platform tenant or workspace ID for --dm"))
    tasks = memory.add_subparsers(dest="memory_action", required=True)
    add = tasks.add_parser("remember")
    add.add_argument("category", choices=CATEGORIES)
    add.add_argument("text")
    add.add_argument("--confidence", type=float, default=1.0)
    add.add_argument("--tag", action="append", default=[])
    add.add_argument("--promote", action="store_true", help=_("Explicitly confirm a personal profile entry"))
    recall = tasks.add_parser("recall")
    recall.add_argument("query")
    recall.add_argument("--category", action="append", default=[], choices=CATEGORIES)
    recall.add_argument("--include-shared", action="store_true")
    recall.add_argument("--limit", type=int, default=5)
    evaluate = tasks.add_parser("evaluate", help=_("Measure retrieval against a labeled JSON file"))
    evaluate.add_argument("cases", type=Path)
    evaluate.add_argument("--limit", type=int, default=5)
    evaluate.add_argument("--include-shared", action="store_true")
    tasks.add_parser("audit")
    remove = tasks.add_parser("forget")
    remove.add_argument("id", type=int)

    feedback = commands.add_parser("feedback", help=_("Record how an operational finding performed"))
    feedback.add_argument("code")
    feedback.add_argument("result", choices=("helpful", "dismissed", "reset"))
    commands.add_parser("history", help=_("Show learned maintenance priorities and feedback"))

    learn = commands.add_parser("learn", help=_("Run one evolution pass: derive lessons, measure changes, refresh proposals"))
    learn.add_argument("--notify-only", action="store_true", help=_("Print only new lessons, regressions and high-risk proposals"))
    playbook = commands.add_parser("lessons", help=_("Review the playbook learned from recurring tool failures"))
    playbook_commands = playbook.add_subparsers(dest="lessons_action")
    listing = playbook_commands.add_parser("list")
    listing.add_argument("--status", choices=("candidate", "active", "quarantined", "retired"))
    listing.add_argument("--lang", choices=("en", "pt"), default=argparse.SUPPRESS)
    for name in ("approve", "retire"):
        playbook_commands.add_parser(name).add_argument("id", type=int)
    custom = playbook_commands.add_parser("add", help=_("Add an operator lesson"))
    custom.add_argument("text")
    custom.add_argument("--tool", default="general")
    skills = commands.add_parser("skills", help=_("Show skill churn, flip-flops and rewrites that were never exercised"))
    queue = commands.add_parser("proposals", help=_("Review and decide what the evolution loop proposes"))
    queue_commands = queue.add_subparsers(dest="proposals_action")
    queue_list = queue_commands.add_parser("list")
    queue_list.add_argument("--all", action="store_true", help=_("Include decided and expired proposals"))
    for name in ("approve", "reject"):
        decision = queue_commands.add_parser(name)
        decision.add_argument("id", type=int)
        decision.add_argument("--note")
    commands.add_parser("experiments", help=_("Show tracked changes with their before-and-after verdicts"))
    digest = commands.add_parser("report", help=_("Print the evolution digest"))
    digest.add_argument("--format", choices=("md", "json"), default="md")
    digest.add_argument("--schedule-spec", action="store_true",
                        help=_("Print a no-agent cron recipe that delivers this digest weekly, without creating it"))
    digest.add_argument("--weekday", type=int, choices=range(0, 7), default=1, metavar="0..6")
    digest.add_argument("--hour", type=int, choices=range(0, 24), default=8, metavar="0..23")
    digest.add_argument("--deliver", default="local", help=_("Hermes delivery target for the scheduled digest"))
    for command in (skills, digest):
        command.add_argument("--days", type=int, choices=range(1, RETENTION_DAYS + 1), default=7, metavar="1..35")
    for command in (playbook, digest, learn):
        command.add_argument("--lang", choices=("en", "pt"), default=argparse.SUPPRESS,
                             help=_("Language of lessons, messages and the digest"))

    ops = commands.add_parser("ops", help=_("Inspect and supervise Hermes and Linux resource contention"))
    ops_commands = ops.add_subparsers(dest="ops_action", required=True)
    for name in ("inspect", "tick"):
        command = ops_commands.add_parser(name)
        command.add_argument("--days", type=int, choices=range(1, RETENTION_DAYS + 1), default=7, metavar="1..35")
        command.add_argument("--upstream-tag")
        command.add_argument("--hermes-source", type=Path, help=_("Include the bundled Hermes source inventory"))
        command.add_argument("--sensitivity", choices=("low", "balanced", "high"))
        if name == "tick":
            command.add_argument("--notify-only", action="store_true", help=_("Print only new alerts and action transitions"))
    ops_commands.add_parser("history")
    ops_commands.add_parser("rollback")
    similar = ops_commands.add_parser("similar", help=_("Find previous diagnostic patterns"))
    similar.add_argument("query")
    similar.add_argument("--limit", type=int, choices=range(1, 11), default=5, metavar="1..10")
    review = ops_commands.add_parser("review", help=_("Review learned baselines, feedback and outcomes"))
    review.add_argument("--days", type=int, default=14)
    rating = ops_commands.add_parser("feedback", help=_("Rate an adaptive alert"))
    rating.add_argument("code")
    rating.add_argument("result", choices=("helpful", "dismissed", "reset"))
    planner = ops_commands.add_parser("plan", help=_("Create a read-only research, delegation or diagnostic plan"))
    planner.add_argument("kind", choices=KINDS)
    planner.add_argument("--effort", choices=EFFORTS, default="standard")
    planner.add_argument("--durable", action="store_true")
    handoff = ops_commands.add_parser("handoff", help=_("Create a blocked native Kanban verification card"))
    handoff.add_argument("kind", choices=sorted(PLAYBOOKS))
    handoff.add_argument("subject")
    scheduler = ops_commands.add_parser("schedule-spec", help=_("Generate a native timer configuration for review"))
    scheduler.add_argument("--every-minutes", type=int, default=5)
    scheduler.add_argument("--upstream-tag")
    _accept_lang_everywhere(parser, _("Language of lessons, messages and the digest"))


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


def _evolution(args, home: Path, *, now: float, runtime_config=None):
    """Commands of the evolution loop: lessons, skills, proposals, experiments and the digest."""
    from . import experiments, lessons, proposals
    from .evolve import cycle, notify_view, options
    from .experience import experience
    from .report import build, markdown
    from .skill_health import skill_health

    action = args.evolution_action
    config = effective_settings(home, runtime_config)
    opts = options(config, runtime_config)
    lang = getattr(args, "lang", None) or opts["language"]
    if action == "learn":
        result = cycle(home, now=now, runtime_config=runtime_config, config=config, lang=lang)
        return notify_view(result) if args.notify_only else result
    if action == "skills":
        from .experience import day_offset, utc_day
        from .skill_health import skill_outcomes

        report = skill_health(home, now=now, days=args.days)
        with experience(home) as conn:
            report["outcomes"] = skill_outcomes(conn, start_day=day_offset(now, args.days - 1), end_day=utc_day(now))
        return report
    if action == "report" and args.schedule_spec:
        from .evolve import digest_schedule_spec

        return digest_schedule_spec(home, weekday=args.weekday, hour=args.hour, days=args.days, lang=lang,
                                    deliver=args.deliver)
    if action == "report":
        report = build(home, now=now, days=args.days, lang=lang, rates=config.get("rates_usd_per_million", {}))
        return markdown(report, lang) if args.format == "md" else report
    with experience(home) as conn:
        if action == "experiments":
            return experiments.listing(conn)
        if action == "lessons":
            task = args.lessons_action or "list"
            if task == "list":
                return {"mode": opts["lessons"],
                        "lessons": lessons.listing(conn, lang=lang, status=getattr(args, "status", None))}
            if task == "add":
                lesson_id = lessons.add_custom(conn, args.text, tool=args.tool, origin="operator", now=now, active=True)
                return {"id": lesson_id, "status": "active"}
            outcome = lessons.set_status(conn, args.id, "active" if task == "approve" else "retired", now=now)
            # A direct decision also settles the matching proposal, so it does not linger in the queue.
            conn.execute("UPDATE proposal SET status=?, updated=? WHERE code=? AND status IN ('open','approved')",
                         ("applied" if task == "approve" else "rejected", now, f"lesson:{args.id}"))
            if task == "retire":
                conn.execute("UPDATE proposal SET status='applied', updated=? WHERE code=? AND status IN ('open','approved')",
                             (now, f"lesson_retire:{args.id}"))
            return outcome
        task = args.proposals_action or "list"
        if task == "list":
            return proposals.listing(conn, status=None if getattr(args, "all", False) else "open")
        return proposals.decide(conn, args.id, task, now=now, note=args.note)


def _scheduled_cycle(home: Path, config: dict, *, now: float, runtime_config=None):
    """Run the evolution pass from `ops tick`, at most once an hour. Returns its notification view."""
    from .evolve import cycle, notify_view
    from .experience import experience

    with experience(home) as conn:
        saved = conn.execute("SELECT value FROM cursor WHERE key='last_cycle'").fetchone()
        try:
            last = float(saved[0]) if saved else 0.0
        except ValueError:
            last = 0.0
        if 0 <= now - last < CYCLE_INTERVAL_SECONDS:
            return None
        conn.execute("INSERT OR REPLACE INTO cursor VALUES ('last_cycle', ?)", (repr(now),))
    return notify_view(cycle(home, now=now, runtime_config=runtime_config, config=config))


def _report(args, home: Path, *, running_version=None, provider=None, now: float, runtime_config=None,
            hermes_source=None, kanban_home=None, supplemental=(), health_override=None):
    action = args.evolution_action
    if action in EVOLUTION_ACTIONS:
        return _evolution(args, home, now=now, runtime_config=runtime_config)
    if action == "ops":
        return _ops(args, home, now=now, running_version=running_version, provider=provider,
                    runtime_config=runtime_config, source=hermes_source, kanban_home=kanban_home)
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


def _rollback_trial(home: Path, now: float) -> dict:
    from .linux_actions import Systemd
    from .supervisor import _journal, supervise

    if not (home / "evolution" / "supervisor.sqlite3").is_file():
        return {"status": "idle", "reason": "no_active_trial"}
    with _journal(home) as journal:
        _, result = supervise(journal, Systemd(), None, now=now, safe=False, rollback=True)
    return result


def _ops(args, home: Path, *, now, running_version, provider, runtime_config, source, kanban_home):
    from .advisory import plan
    from .inventory import scan_inventory
    from .learning import record_feedback, retrospective, review as adaptive_review
    from .linux_actions import SystemPolicy, Systemd
    from .linux_host import scan_host
    from .native_actions import create_handoff, schedule_spec
    from .native_signals import native_findings, native_summary
    from .ops_metrics import current_metrics, workload_correlations
    from .reasoning import diagnose
    from .supervisor import _journal, baseline_metrics, journal_history, learned_baseline, record_observations, supervise
    from .telemetry import research_summary
    from .workloads import inspect_workloads

    action = args.ops_action
    if action == "history":
        return journal_history(home)
    if action in ("review", "feedback"):
        with _journal(home) as journal:
            return (retrospective(journal, now=now, days=args.days) if action == "review"
                    else record_feedback(journal, args.code, args.result, now=now))
    if action == "plan":
        return plan(args.kind, args.effort, args.durable)
    if action == "schedule-spec":
        return schedule_spec(home, every_minutes=args.every_minutes, upstream_tag=args.upstream_tag)
    if action == "rollback":
        return _rollback_trial(home, now)
    config = effective_settings(home, runtime_config)
    if action == "handoff":
        return create_handoff(home, config, kind=args.kind, subject=args.subject, now=now, lang=args.lang)
    if action == "similar":
        from .semantic_ops import match_incidents

        model = config.get("semantic_incident_model")
        if model is not None and (not isinstance(model, str) or not Path(model).is_absolute()):
            raise ValueError("semantic_incident_model must be an absolute local model directory")
        return match_incidents(args.query, journal_history(home)["incidents"], model_path=Path(model) if model else None,
                               limit=args.limit)
    health = snapshot(home, upstream_tag=args.upstream_tag, provider=provider, running_version=running_version)
    sensitivity = args.sensitivity or config.get("sensitivity", "balanced")
    if sensitivity not in ("low", "balanced", "high"):
        raise ValueError("sensitivity must be low, balanced, or high")
    host = scan_host(home)
    runtime = runtime_config or {}
    cfg = runtime.get("plugins", {})
    cfg = cfg if isinstance(cfg, dict) else {}
    inventory = scan_inventory(home, source=args.hermes_source or source, enabled_plugins=cfg.get("enabled"),
                               disabled_plugins=cfg.get("disabled"), memory_limits=runtime.get("memory"), now=now)
    workload_options = {key: runtime.get(key) for key in ("cron", "kanban", "delegation")}
    if config.get("inspect_shared_kanban") is True:
        workload_options["kanban_home"] = kanban_home
    workloads = inspect_workloads(home, provider_options=workload_options, now=now)
    rates = config.get("rates_usd_per_million", {})
    with database(home) as conn:
        usage = summarize(conn, days=args.days, now=now, rates=rates)
        research = research_summary(conn, days=args.days, now=now)
        native = native_summary(conn, days=args.days, now=now)
        daily_usage = usage if args.days == 1 else summarize(conn, days=1, now=now, rates={})
        daily_research = research if args.days == 1 else research_summary(conn, days=1, now=now)
    observer_issues = native_findings(native)
    related = workloads["recommendations"] + observer_issues + workload_correlations(workloads, observer_issues)
    result = {"health": health, "linux_host": host, "inventory": inventory, "usage": usage, "research": research,
              "workloads": workloads, "native_activity": native}
    current_baseline = baseline_metrics(daily_usage, daily_research)
    metrics = current_metrics(host, daily_usage, workloads, current_baseline)
    if action == "inspect":
        issues = diagnose(health, usage, host=host, inventory=inventory, telemetry={"research": research}) + related
        result["diagnoses"] = issues
        if (home / "evolution" / "supervisor.sqlite3").is_file():
            with _journal(home) as journal:
                result["learning"] = adaptive_review(journal, metrics=metrics, findings=issues, now=now,
                                                      sensitivity=sensitivity)
        return result
    policy = SystemPolicy.read(config)
    safe = (health["profile_present"] and health["database"]["status"] in ("ok", "integrity_skipped_large")
            and not any(item["severity"] == "critical" or item["id"] in ("gateway_degraded", "gateway_startup_failure")
                        for item in health["findings"]))
    with _journal(home) as journal:
        history = learned_baseline(journal, now=now)
        def diagnose_with(candidates):
            return diagnose(health, usage, host=host, inventory=inventory,
                            telemetry={"research": research, "contention_candidates": candidates,
                                       "current_daily_baseline": current_baseline}, history=history)

        def approved_by_evidence(candidate):
            return any(f"rebalance_background_{candidate['kind']}" in issue["action_ids"] for issue in diagnose_with([candidate]))

        candidates, outcome = supervise(journal, Systemd(), policy, now=now, safe=safe, allow_kind=approved_by_evidence)
        issues = diagnose_with(candidates) + related
        record_observations(journal, issues, daily_usage, daily_research, now=now)
        adaptive = adaptive_review(journal, metrics=metrics, findings=issues, now=now, sensitivity=sensitivity, record=True)
    context_actions = [{"code": issue["code"], "action": issue["recommendation"], "severity": issue["severity"],
                        "reduce_context": True} for issue in issues if "pause_optional_context" in issue["action_ids"]]
    args_for_tick = SimpleNamespace(evolution_action="tick", upstream_tag=args.upstream_tag,
                                    days=args.days, changes_only=False)
    maintenance = _report(args_for_tick, home, now=now, health_override=health, supplemental=context_actions)
    try:
        evolution = _scheduled_cycle(home, config, now=now, runtime_config=runtime_config)
    except (OSError, ValueError, sqlite3.Error):
        evolution = None   # the learning pass never blocks operational supervision
    if args.notify_only:
        changes = maintenance["decision"]["changes"]
        event = outcome if outcome["status"] in ("active", "rolled_back", "released", "not_applied") else None
        if not adaptive["alerts"] and not event and not changes and not evolution:
            return None
        return {"alerts": adaptive["alerts"], "host_action": event, "maintenance_changes": changes,
                "evolution": evolution}
    return {**result, "diagnoses": issues, "paired_contention": candidates, "host_action": outcome,
            "maintenance": maintenance["decision"], "learning": adaptive, "evolution": evolution}


def _output_language(args, home: Path, runtime_config) -> str:
    """--lang, then the plugin setting, then the profile language Hermes uses."""
    from .i18n import normalize, resolve

    chosen = normalize(getattr(args, "lang", None))
    if chosen:
        return chosen
    try:
        configured = (effective_settings(home, runtime_config).get("evolution") or {}).get("language")
    except (OSError, ValueError):
        configured = None
    return resolve(configured if isinstance(configured, str) else None, runtime_config)


def execute(args, home: Path, *, running_version=None, provider=None, runtime_config=None, hermes_source=None,
            kanban_home=None) -> None:
    from .i18n import localize, text

    now = time.time()
    language = _output_language(args, home, runtime_config)
    if getattr(args, "lang", None) is None:
        args.lang = language
    try:
        result = _report(args, home, running_version=running_version, provider=provider, now=now,
                         runtime_config=runtime_config, hermes_source=hermes_source, kanban_home=kanban_home)
    except (OSError, ValueError, sqlite3.Error) as error:
        recovery = ""
        if args.evolution_action == "ops" and args.ops_action == "tick":
            try:
                outcome = _rollback_trial(home, now)
                if outcome["status"] != "idle":
                    recovery = f"\nActive trial recovery: {outcome['status']}"
            except (OSError, ValueError, sqlite3.Error):
                recovery = "\nTrial recovery is unverified. Run ops rollback to recheck."
        raise SystemExit(f"Evolution: {text(str(error), language)}{text(recovery, language)}") from None
    if isinstance(result, str):
        print(result, end="" if result.endswith("\n") else "\n")
    elif result is not None:
        print(json.dumps(localize(result, language), indent=2, ensure_ascii=False, allow_nan=False))


def _standalone_config(home: Path) -> dict:
    """The profile's config.yaml when a YAML parser is installed. Without one the runner uses defaults."""
    try:
        import yaml
    except ImportError:
        return {}
    try:
        with (home / "config.yaml").open("rb") as stream:
            body = stream.read(1_048_577)
        loaded = yaml.safe_load(body) if len(body) <= 1_048_576 else None
    except (OSError, ValueError, RecursionError, yaml.YAMLError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def main():
    from .i18n import resolve, text

    def _(sentence: str) -> str:
        return text(sentence, resolve())

    parser = argparse.ArgumentParser(prog="python -m hermes_evolution")
    parser.add_argument("--home", type=Path, default=os.environ.get("HERMES_HOME"), help=_("Explicit Hermes profile path"))
    parser.add_argument("--hermes-version", help=_("Installed version if running outside Hermes"))
    setup(parser)
    args = parser.parse_args()
    if args.home is None:
        parser.error(_("Pass --home or set HERMES_HOME"))
    home = args.home.expanduser().resolve()
    config = _standalone_config(home)
    memory = config.get("memory")
    provider = memory.get("provider") if isinstance(memory, dict) else None
    # Named profiles live in <root>/profiles/<name>; shared Kanban boards belong to the root.
    root = home.parent.parent if home.parent.name == "profiles" else home
    execute(args, home, running_version=args.hermes_version, provider=provider if isinstance(provider, str) else None,
            runtime_config=config, kanban_home=root)
