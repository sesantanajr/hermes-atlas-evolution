"""The evolution digest: what failed, what was learned and whether it helped.

`build` returns plain numbers; `markdown` renders them for a person in English or Portuguese. The digest
contains aggregates and identifier-like labels only.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import experiments, lessons, logwatch, proposals, signals
from .experience import day_offset, experience, utc_day
from .skill_health import skill_health, skill_outcomes

def build(home: Path, *, now: float, days: int = 7, lang: str = "en", rates: dict | None = None,
          own_transform: bool = True) -> dict:
    """Collect the digest for the last `days` days and the same span before it."""
    from .billing import native_billing
    from .operations import database, summarize

    if type(days) is not int or not 1 <= days <= 35:
        raise ValueError("days must be between 1 and 35")
    with experience(home) as conn:
        start, end = day_offset(now, days - 1), utc_day(now)
        previous_start, previous_end = day_offset(now, 2 * days - 1), day_offset(now, days)
        interactive = signals.turn_summary(conn, now=now, days=days, surfaces=("local", "gateway"))
        report = {
            "window": {"days": days, "from_utc": start, "to_utc": end},
            "turns": {"interactive": interactive,
                      "cron": signals.turn_summary(conn, now=now, days=days, surfaces=("cron",)),
                      "subagent": signals.turn_summary(conn, now=now, days=days, surfaces=("subagent",))},
            "tools": signals.tool_summary(conn, now=now, days=days),
            "recurring_failures": signals.failure_summary(conn, now=now, days=days),
            "owner_feedback": signals.feedback_summary(conn, now=now, days=days),
            "skill_events": signals.skill_event_summary(conn, now=now, days=days),
            "self_improvement": {"background_reviews": logwatch.review_summary(
                conn, **dict(zip(("start_day", "end_day"), logwatch.local_days(now, days))))},
            "lessons": lessons.listing(conn, lang=lang),
            "changes": experiments.listing(conn, limit=20),
            "proposals": proposals.listing(conn, status="open"),
            "skill_outcomes": skill_outcomes(conn, start_day=start, end_day=end, limit=10),
            "guard": signals.guard_summary(conn, now=now, days=days),
        }
        from . import radar

        report["health"] = {key: value for key, value in radar.scan(home, conn, now=now, days=min(days, 14),
                                                                    own_transform=own_transform).items()
                            if key != "findings"}
        try:
            with database(home) as ops:
                current = experiments.window_metrics(conn, ops, start, end)
                before = experiments.window_metrics(conn, ops, previous_start, previous_end)
                usage = summarize(ops, days=days, now=now, rates=rates or {})
                from .telemetry import research_summary

                report["research"] = research_summary(ops, now=now, days=days)
        except (sqlite3.Error, ValueError, OSError):
            current = experiments.window_metrics(conn, None, start, end)
            before = experiments.window_metrics(conn, None, previous_start, previous_end)
            usage = None
    report["self_improvement"]["skills"] = skill_health(home, now=now, days=days, limit=8)
    report["comparison"] = {"current": current, "previous": before, "verdict": experiments.compare(before, current)}
    if usage is not None:
        main = [row for row in usage["groups"] if row["lane"] == "main" and row["outcome"] == "ok"]
        report["usage"] = {
            "main_calls": sum(row["requests"] for row in main),
            "input_tokens": sum(row["input_tokens"] for row in main),
            "cache_read_tokens": sum(row["cache_read_tokens"] for row in main),
            "output_tokens": sum(row["output_tokens"] for row in main),
            "auxiliary_calls": sum(row["requests"] for row in usage["groups"] if row["lane"].startswith("aux:")),
            "basis": "calls_observed_by_this_plugin",
        }
        reviews = report["self_improvement"]["background_reviews"]
        paid = report["usage"]["input_tokens"] + report["usage"]["output_tokens"]
        spent = reviews["input_tokens"] + reviews["output_tokens"]
        if paid and spent:
            # Review calls run on the main lane, so they are already inside `paid`.
            report["self_improvement"]["learning_share_pct"] = round(min(100.0, 100 * spent / paid), 1)
    try:
        native = native_billing(home, days=days, now=now)
        totals = native.get("totals") if isinstance(native, dict) else None
        if isinstance(totals, dict):
            report["native_cost"] = {"estimated_cost_usd": totals.get("estimated_cost_usd"),
                                     "api_call_count": totals.get("api_call_count"), "basis": "hermes_session_accounting"}
    except (sqlite3.Error, ValueError, OSError):
        pass
    return report


_TEXT = {
    "en": {
        "title": "Atlas Evolution · {days}-day digest ({start} to {end}, UTC)",
        "summary": "Summary", "turns": "Interactive turns", "of": "of", "failed": "failed", "interrupted": "interrupted",
        "slow": "slow", "typical": "typical turn (p50) up to", "p95": "1 in 20 takes more than",
        "tools": "Tool calls", "errors": "errors", "cost": "Estimated cost (Hermes accounting)",
        "feedback": "Owner signals", "corrections": "corrections", "reasks": "repeated questions", "thanks": "thanks",
        "vs": "Versus the previous {days} days", "verdict": "verdict", "failures": "Failures that repeat",
        "failures_none": "No recurring tool failure in this window.", "times": "times", "days": "days",
        "lesson_state": "lesson", "no_lesson": "no lesson yet", "learning": "Self-improvement loop",
        "never_lesson": "never a lesson: the user's approval decides",
        "other_plugin": "never a lesson: another plugin caused it",
        "reviews": "background reviews", "empty": "produced nothing", "review_tokens": "review tokens (input / output)",
        "share": "of the uncached input and output tokens went to learning (approximate)",
        "rewrites": "skill rewrites", "by_review": "by the automatic review", "skills": "skills",
        "churn": "Skills rewritten the most", "flipflops": "flip-flops", "uses": "uses",
        "outcomes": "Skills by outcome", "outcomes_none": "Not enough turns with a loaded skill yet.",
        "whole_profile": "whole profile", "turns_word": "turns", "failed_short": "failed", "corrected_short": "corrected",
        "interrupted_short": "interrupted", "tool_errors_short": "with tool errors", "below": "worse than the profile",
        "lessons": "Playbook", "active": "active", "candidate": "awaiting approval", "retired": "retired",
        "effect": "measured effect", "before": "before", "after": "after", "changes": "Changes and their measured effect",
        "changes_none": "No change evaluated yet.", "proposals": "Proposals awaiting a decision",
        "proposals_none": "Nothing is waiting for you.", "risk": "risk", "run": "apply with", "look": "next step",
        "no_data": "not measured", "footer": "Aggregates only. No conversation text, tool output or memory content is stored.",
        "decide": "Decide with `hermes evolution proposals approve <id>` or `reject <id>`.",
        "lessons_waiting": "{count} lessons await approval (read them with `hermes evolution lessons list`, approve with `hermes evolution lessons approve <lesson id>`)",
        "lessons_waiting_one": "1 lesson awaits approval (read it with `hermes evolution lessons list`, approve with `hermes evolution lessons approve <lesson id>`)",
        "health": "Hermes health", "sessions_line": "Sessions", "ended": "ended", "orphaned": "ended as orphans",
        "orphan_note": "reaped after a disconnect or restart, so Hermes' end-of-session work did not run",
        "context_line": "Context per agent call", "avg": "average of", "tokens": "tokens", "cached": "served from cache",
        "calls": "calls", "compaction_line": "Compactions", "kept": "kept", "of_context": "of the context",
        "plugins_line": "Plugins of this process", "enabled": "enabled", "load_errors": "failed to load",
        "undeclared": "register hooks they do not declare", "overlap": "compete for", "first_wins": "only the first result counts",
        "guard_line": "Runtime guard", "vetoed": "commands vetoed", "gateway_stop": "gateway restart or kill",
        "runtime_removal": "removal of the running Hermes", "health_none": "Nothing measured yet.",
        "external_line": "Failures caused by other plugins",
        "external_fault": "calls blocked because a plugin's pre_tool_call hung or failed",
        "external_rewrite": "commands rewritten by a plugin that then failed (never turned into lessons)",
        "research_line": "Web research", "research_turns": "turns", "research_slow": "over {limit} s",
        "research_errors": "errors in", "research_calls": "calls", "research_p95": "1 in 20 takes more than",
    },
    "pt": {
        "title": "Atlas Evolution · resumo de {days} dias ({start} a {end}, UTC)",
        "summary": "Resumo", "turns": "Turnos interativos", "of": "de", "failed": "falharam", "interrupted": "interrompidos",
        "slow": "lentos", "typical": "turno típico (p50) até", "p95": "1 em cada 20 passa de",
        "tools": "Chamadas de ferramentas", "errors": "erros", "cost": "Custo estimado (contabilidade do Hermes)",
        "feedback": "Sinais do dono", "corrections": "correções", "reasks": "perguntas repetidas", "thanks": "agradecimentos",
        "vs": "Comparado aos {days} dias anteriores", "verdict": "veredito", "failures": "Falhas que se repetem",
        "failures_none": "Nenhuma falha de ferramenta recorrente nesta janela.", "times": "vezes", "days": "dias",
        "lesson_state": "lição", "no_lesson": "sem lição ainda", "learning": "Ciclo de automelhoria",
        "never_lesson": "nunca vira lição: quem decide é a aprovação do usuário",
        "other_plugin": "nunca vira lição: outro plugin causou a falha",
        "reviews": "revisões em segundo plano", "empty": "não produziram nada", "review_tokens": "tokens das revisões (entrada / saída)",
        "share": "dos tokens de entrada sem cache e de saída foram para o aprendizado (aproximado)",
        "rewrites": "reescritas de skills", "by_review": "pela revisão automática", "skills": "skills",
        "churn": "Skills mais reescritas", "flipflops": "vai-e-volta", "uses": "usos",
        "outcomes": "Skills pelo resultado", "outcomes_none": "Ainda não há turnos suficientes com skill carregada.",
        "whole_profile": "perfil inteiro", "turns_word": "turnos", "failed_short": "falharam", "corrected_short": "corrigidos",
        "interrupted_short": "interrompidos", "tool_errors_short": "com erro de ferramenta", "below": "pior que o perfil",
        "lessons": "Playbook", "active": "ativas", "candidate": "aguardando aprovação", "retired": "aposentadas",
        "effect": "efeito medido", "before": "antes", "after": "depois", "changes": "Mudanças e o efeito medido",
        "changes_none": "Nenhuma mudança avaliada ainda.", "proposals": "Propostas aguardando decisão",
        "proposals_none": "Nada aguardando você.", "risk": "risco", "run": "aplique com", "look": "próximo passo",
        "no_data": "não medido", "footer": "Só agregados. Nenhum texto de conversa, saída de ferramenta ou conteúdo de memória é guardado.",
        "decide": "Decida com `hermes evolution proposals approve <id>` ou `reject <id>`.",
        "lessons_waiting": "{count} lições aguardam aprovação (leia com `hermes evolution lessons list` e aprove com `hermes evolution lessons approve <id da lição>`)",
        "lessons_waiting_one": "1 lição aguarda aprovação (leia com `hermes evolution lessons list` e aprove com `hermes evolution lessons approve <id da lição>`)",
        "health": "Saúde do Hermes", "sessions_line": "Sessões", "ended": "encerradas", "orphaned": "terminaram como órfãs",
        "orphan_note": "recolhidas após desconexão ou reinício, sem o encerramento normal do Hermes",
        "context_line": "Contexto por chamada do agente", "avg": "média de", "tokens": "tokens", "cached": "servido do cache",
        "calls": "chamadas", "compaction_line": "Compactações", "kept": "mantiveram", "of_context": "do contexto",
        "plugins_line": "Plugins deste processo", "enabled": "ativos", "load_errors": "falharam ao carregar",
        "undeclared": "registram hooks que não declaram", "overlap": "disputam", "first_wins": "só o primeiro resultado vale",
        "guard_line": "Guarda de runtime", "vetoed": "comandos barrados", "gateway_stop": "reinício ou kill do gateway",
        "runtime_removal": "remoção do Hermes em execução", "health_none": "Nada medido ainda.",
        "external_line": "Falhas causadas por outros plugins",
        "external_fault": "chamadas bloqueadas porque o pre_tool_call de um plugin travou ou falhou",
        "external_rewrite": "comandos reescritos por um plugin que depois falharam (nunca viram lição)",
        "research_line": "Pesquisas na web", "research_turns": "turnos", "research_slow": "acima de {limit} s",
        "research_errors": "erros em", "research_calls": "chamadas", "research_p95": "1 em cada 20 passa de",
    },
}
_VERDICT = {
    "en": {"improved": "improved", "regressed": "regressed", "neutral": "no material change", "mixed": "mixed",
           "insufficient_data": "not enough data", "effective": "effective", "ineffective": "ineffective",
           "inconclusive": "inconclusive", None: "pending"},
    "pt": {"improved": "melhorou", "regressed": "piorou", "neutral": "sem mudança relevante", "mixed": "misto",
           "insufficient_data": "dados insuficientes", "effective": "funcionou", "ineffective": "não funcionou",
           "inconclusive": "inconclusivo", None: "pendente"},
}


_STATUS = {
    "en": {"active": "active", "candidate": "awaiting approval", "quarantined": "quarantined", "retired": "retired"},
    "pt": {"active": "ativa", "candidate": "aguardando aprovação", "quarantined": "em quarentena", "retired": "aposentada"},
}
_RISK = {"en": {"low": "low", "medium": "medium", "high": "high"}, "pt": {"low": "baixo", "medium": "médio", "high": "alto"}}
_KIND = {
    "en": {"lesson": "Activate lesson", "lesson_retire": "Retire a lesson that did not help",
           "skill_lock": "Lock a skill that keeps being rewritten", "skill_review": "Review rewrites that were never used",
           "skill_archive": "Archive a skill that was never used", "skill_rollback": "Roll back a skill change that made things worse",
           "review_interval": "Space out the automatic review", "investigate_failure": "Recurring failure without a lesson",
           "investigate_latency": "Many slow turns", "config_review": "Review a setting change that made things worse",
           "skill_outcome": "Review a skill whose turns go worse than the profile",
           "session_lifecycle": "Many sessions end as orphans", "context_weight": "Heavy context per call",
           "compaction": "Compactions keep most of the context", "plugin_health": "Plugin problem",
           "plugin_conflict": "Plugins compete for a hook where only the first counts"},
    "pt": {"lesson": "Ativar lição", "lesson_retire": "Aposentar lição que não ajudou",
           "skill_lock": "Travar skill reescrita em excesso", "skill_review": "Revisar reescritas nunca usadas",
           "skill_archive": "Arquivar skill nunca usada", "skill_rollback": "Desfazer alteração de skill que piorou",
           "review_interval": "Espaçar a revisão automática", "investigate_failure": "Falha recorrente sem lição",
           "investigate_latency": "Muitos turnos lentos", "config_review": "Revisar mudança de configuração que piorou",
           "skill_outcome": "Revisar skill cujos turnos vão pior que o perfil",
           "session_lifecycle": "Muitas sessões terminam como órfãs", "context_weight": "Contexto pesado por chamada",
           "compaction": "Compactações mantêm quase todo o contexto", "plugin_health": "Problema em plugin",
           "plugin_conflict": "Plugins disputam um hook em que só o primeiro vale"},
}
_ADVISORY = ("skill_review", "investigate_failure", "investigate_latency", "config_review", "skill_outcome",
             "session_lifecycle", "context_weight", "compaction", "plugin_health", "plugin_conflict")


def _number(value, lang: str, digits: int = 0) -> str:
    if value is None:
        return _TEXT[lang]["no_data"]
    text = f"{value:,.{digits}f}"
    return text.replace(",", "X").replace(".", ",").replace("X", ".") if lang == "pt" else text


def _seconds(ms, lang: str) -> str:
    return _TEXT[lang]["no_data"] if ms is None else _number(ms / 1000, lang, 0 if ms >= 10_000 else 1) + " s"


def _health(report: dict, lang: str) -> list[str]:
    """The "Hermes health" section: readings from Hermes' own records and this process's plugins."""
    t, health = _TEXT[lang], report.get("health") or {}
    lines = ["", "## " + t["health"], ""]
    lifecycle = health.get("sessions")
    if lifecycle and lifecycle["ended"]:
        lines.append(f"- **{t['sessions_line']}:** {_number(lifecycle['ended'], lang)} {t['ended']} · "
                     f"{_number(lifecycle['orphaned'], lang)} {t['orphaned']} ({_number(lifecycle['orphan_pct'], lang, 1)}%)"
                     + (f" · _{t['orphan_note']}_" if lifecycle["orphaned"] else ""))
    weight = health.get("context")
    if weight and weight["calls"]:
        lines.append(f"- **{t['context_line']}:** {t['avg']} {_number(weight['avg_prompt_tokens'], lang)} {t['tokens']} · "
                     f"{_number(weight['cache_pct'], lang, 1)}% {t['cached']} · {_number(weight['calls'], lang)} {t['calls']}")
    squeeze = health.get("compaction") or {}
    if squeeze.get("compactions"):
        lines.append(f"- **{t['compaction_line']}:** {_number(squeeze['compactions'], lang)} · {t['kept']} "
                     f"{_number(squeeze['kept_pct'], lang, 1)}% {t['of_context']}")
    loaded = health.get("plugins") or health.get("plugins_snapshot")
    if loaded:
        parts = [f"{_number(loaded['enabled'], lang)} {t['enabled']}"]
        if loaded["errors"]:
            parts.append(f"{t['load_errors']}: " + ", ".join(f"`{item['plugin']}`" for item in loaded["errors"][:6]))
        if loaded["undeclared"]:
            parts.append(f"{t['undeclared']}: " + ", ".join(f"`{item['plugin']}`" for item in loaded["undeclared"][:6]))
        for hook, names in loaded["overlaps"].items():
            parts.append(f"{t['overlap']} `{hook}` ({t['first_wins']}): " + " → ".join(f"`{name}`" for name in names))
        lines.append(f"- **{t['plugins_line']}:** " + " · ".join(parts))
    outside = health.get("external") or {}
    if outside.get("plugin_fault") or outside.get("rewritten_command"):
        lines.append(f"- **{t['external_line']}:** {_number(outside.get('plugin_fault', 0), lang)} {t['external_fault']} · "
                     f"{_number(outside.get('rewritten_command', 0), lang)} {t['external_rewrite']}")
    research = report.get("research") or {}
    if research.get("turns"):
        lines.append(f"- **{t['research_line']}:** {_number(research['turns'], lang)} {t['research_turns']} · "
                     f"{t['research_p95']} {_seconds(research['p95_ms'], lang)} · {_number(research['slow_turns'], lang)} "
                     f"{t['research_slow'].format(limit=research['limit_ms'] // 1000)} · {_number(research['tool_errors'], lang)} "
                     f"{t['research_errors']} {_number(research['tool_calls'], lang)} {t['research_calls']}")
    guard = report.get("guard") or {}
    if guard:
        detail = " · ".join(f"{t[kind]} {count}" for kind, count in sorted(guard.items()) if kind in t)
        lines.append(f"- **{t['guard_line']}:** {_number(sum(guard.values()), lang)} {t['vetoed']} ({detail})")
    if len(lines) == 3:
        lines.append(t["health_none"])
    return lines


def _proposal_lines(report: dict, lang: str) -> list[str]:
    """Lessons awaiting approval on one line (their text is in `lessons list`), then the other proposals."""
    t = _TEXT[lang]
    lines = ["", "## " + t["proposals"], ""]
    waiting = [item for item in report["proposals"] if item["kind"] == "lesson"]
    if waiting:
        listed = ", ".join(f"#{item['evidence'].get('lesson_id', item['id'])} `{item['subject']}`" for item in waiting[:10])
        heading = t["lessons_waiting_one"] if len(waiting) == 1 else t["lessons_waiting"].format(count=len(waiting))
        lines.append(f"- {heading}: {listed}")
    if report["proposals"]:
        for item in [entry for entry in report["proposals"] if entry["kind"] != "lesson"][:20]:
            step = t["look"] if item["kind"] in _ADVISORY else t["run"]
            lines.append(f"- #{item['id']} [{t['risk']}: {_RISK[lang][item['risk']]}] {_KIND[lang].get(item['kind'], item['kind'])}"
                         f" · `{item['subject']}` · {step}: `{item['command']}`")
        lines += ["", t["decide"]]
    else:
        lines.append(t["proposals_none"])
    return lines


def markdown(report: dict, lang: str = "en") -> str:
    """Render the digest for a person."""
    lang = lang if lang in _TEXT else "en"
    t, verdicts = _TEXT[lang], _VERDICT[lang]
    window = report["window"]
    turns, tools, feedback = report["turns"]["interactive"], report["tools"], report["owner_feedback"]
    lines = ["# " + t["title"].format(days=window["days"], start=window["from_utc"], end=window["to_utc"]), "",
             "## " + t["summary"], ""]
    lines.append(f"- **{t['turns']}:** {_number(turns['turns'], lang)} · {_number(turns['failed'], lang)} {t['failed']}"
                 f" · {_number(turns['interrupted'], lang)} {t['interrupted']} · {_number(turns['slow'], lang)} {t['slow']}")
    if turns["turns"]:
        lines.append(f"- **p50/p95:** {t['typical']} {_seconds(turns['p50_ms'], lang)} · {t['p95']} {_seconds(turns['p95_ms'], lang)}")
    lines.append(f"- **{t['tools']}:** {_number(tools['calls'], lang)} · {_number(tools['errors'], lang)} {t['errors']}"
                 + (f" ({_number(tools['error_pct'], lang, 1)}%)" if tools["error_pct"] is not None else ""))
    cost = report.get("native_cost", {}).get("estimated_cost_usd")
    if cost is not None:
        lines.append(f"- **{t['cost']}:** US$ {_number(cost, lang, 2)}")
    lines.append(f"- **{t['feedback']}:** {t['corrections']} {feedback['correction']} · {t['reasks']} {feedback['reask']}"
                 f" · {t['thanks']} {feedback['positive']}")
    comparison = report["comparison"]
    lines += ["", "### " + t["vs"].format(days=window["days"]), ""]
    if comparison["verdict"]["metrics"]:
        for name, values in comparison["verdict"]["metrics"].items():
            mark = "▲" if name in comparison["verdict"]["worse"] else "▼" if name in comparison["verdict"]["better"] else "·"
            lines.append(f"- `{name}`: {_number(values['before'], lang, 1)} → {_number(values['after'], lang, 1)} {mark}")
    lines.append(f"- **{t['verdict']}:** {verdicts[comparison['verdict']['verdict']]}")

    lines += ["", "## " + t["failures"], ""]
    if report["recurring_failures"]:
        for item in report["recurring_failures"][:8]:
            detail = f" (`{item['hint']}`)" if item["hint"] else ""
            if item["class"] == "approval_refused":
                state = t["never_lesson"]
            elif item["class"] in ("plugin_fault", "rewritten_command"):
                state = t["other_plugin"]
            else:
                state = f"{t['lesson_state']}: {_STATUS[lang].get(item['lesson'], t['no_lesson'])}"
            lines.append(f"- `{item['tool']}` · {item['class']}{detail}: {item['count']} {t['times']}, {item['days']} {t['days']}"
                         f" · {state}")
    else:
        lines.append(t["failures_none"])

    reviews, skills = report["self_improvement"]["background_reviews"], report["self_improvement"]["skills"]
    lines += ["", "## " + t["learning"], ""]
    lines.append(f"- {_number(reviews['reviews'], lang)} {t['reviews']}"
                 + (f" · {_number(reviews['empty_pct'], lang, 1)}% {t['empty']}" if reviews["empty_pct"] is not None else ""))
    if reviews["reviews"]:
        lines.append(f"- {t['review_tokens']}: {_number(reviews['input_tokens'], lang)} / {_number(reviews['output_tokens'], lang)}")
    share = report["self_improvement"].get("learning_share_pct")
    if share is not None:
        lines.append(f"- {_number(share, lang, 1)}% {t['share']}")
    by_review = skills["rewrites_by_actor"].get("curator", 0)
    lines.append(f"- {_number(skills['rewrites'], lang)} {t['rewrites']} ({_number(by_review, lang)} {t['by_review']})"
                 f" · {skills['skills_rewritten']} {t['skills']}")
    if skills["top"]:
        lines += ["", "### " + t["churn"], ""]
        for item in skills["top"][:5]:
            lines.append(f"- `{item['skill']}`: {item['rewrites']}× · {item['flipflops']} {t['flipflops']} · {item['uses_total']} {t['uses']}")

    outcomes = report["skill_outcomes"]
    lines += ["", "### " + t["outcomes"], ""]
    measured_skills = [item for item in outcomes["skills"] if item["turns"] >= outcomes["min_turns"]]
    if measured_skills and outcomes["profile"]["turns"]:
        flagged = {item["skill"] for item in outcomes["findings"]}
        base = outcomes["profile"]
        lines.append(f"- {t['whole_profile']}: {_number(base['turns'], lang)} {t['turns_word']} · "
                     f"{_number(base['failed_pct'], lang, 1)}% {t['failed_short']} · "
                     f"{_number(base['corrected_pct'], lang, 1)}% {t['corrected_short']}")
        for item in measured_skills[:8]:
            mark = f" · **{t['below']}**" if item["skill"] in flagged else ""
            lines.append(f"- `{item['skill']}`: {_number(item['turns'], lang)} {t['turns_word']} · "
                         f"{_number(item['failed_pct'], lang, 1)}% {t['failed_short']} · "
                         f"{_number(item['corrected_pct'], lang, 1)}% {t['corrected_short']} · "
                         f"{_number(item['tool_error_turns_pct'], lang, 1)}% {t['tool_errors_short']}{mark}")
    else:
        lines.append(t["outcomes_none"])

    playbook = report["lessons"]
    counts = {status: sum(item["status"] == status for item in playbook) for status in ("active", "candidate", "quarantined", "retired")}
    lines += ["", "## " + t["lessons"], "",
              f"- {t['active']}: {counts['active']} · {t['candidate']}: {counts['candidate'] + counts['quarantined']}"
              f" · {t['retired']}: {counts['retired']}"]
    for item in [entry for entry in playbook if entry["status"] == "active"][:8]:
        effect = verdicts[item["verdict"]]
        numbers = (f" ({t['before']} {_number(item['rate_before'], lang, 3)} → {t['after']} {_number(item['rate_after'], lang, 3)})"
                   if item["rate_before"] is not None and item["rate_after"] is not None else "")
        lines.append(f"- #{item['id']} {item['lesson']} · {t['effect']}: {effect}{numbers}")

    lines += ["", "## " + t["changes"], ""]
    measured = [item for item in report["changes"] if item["verdict"]]
    if measured:
        for item in measured[:8]:
            lines.append(f"- {item['kind']} `{item['subject']}` ({utc_day(item['at'])}): {verdicts[item['verdict']]}")
    else:
        lines.append(t["changes_none"])

    lines += _health(report, lang)
    lines += _proposal_lines(report, lang)
    lines += ["", "_" + t["footer"] + "_"]
    return "\n".join(lines) + "\n"
