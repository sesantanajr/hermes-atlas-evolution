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

def build(home: Path, *, now: float, days: int = 7, lang: str = "en", rates: dict | None = None) -> dict:
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
        }
        try:
            with database(home) as ops:
                current = experiments.window_metrics(conn, ops, start, end)
                before = experiments.window_metrics(conn, ops, previous_start, previous_end)
                usage = summarize(ops, days=days, now=now, rates=rates or {})
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
    },
    "pt": {
        "title": "Atlas Evolution · resumo de {days} dias ({start} a {end}, UTC)",
        "summary": "Resumo", "turns": "Turnos interativos", "of": "de", "failed": "falharam", "interrupted": "interrompidos",
        "slow": "lentos", "typical": "turno típico (p50) até", "p95": "1 em cada 20 passa de",
        "tools": "Chamadas de ferramentas", "errors": "erros", "cost": "Custo estimado (contabilidade do Hermes)",
        "feedback": "Sinais do dono", "corrections": "correções", "reasks": "perguntas repetidas", "thanks": "agradecimentos",
        "vs": "Comparado aos {days} dias anteriores", "verdict": "veredito", "failures": "Falhas que se repetem",
        "failures_none": "Nenhuma falha de ferramenta recorrente nesta janela.", "times": "vezes", "days": "dias",
        "lesson_state": "lição", "no_lesson": "sem lição ainda", "learning": "Ciclo de auto-melhoria",
        "never_lesson": "nunca vira lição: quem decide é a aprovação do usuário",
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
           "skill_outcome": "Review a skill whose turns go worse than the profile"},
    "pt": {"lesson": "Ativar lição", "lesson_retire": "Aposentar lição que não ajudou",
           "skill_lock": "Travar skill reescrita em excesso", "skill_review": "Revisar reescritas nunca usadas",
           "skill_archive": "Arquivar skill nunca usada", "skill_rollback": "Desfazer alteração de skill que piorou",
           "review_interval": "Espaçar a revisão automática", "investigate_failure": "Falha recorrente sem lição",
           "investigate_latency": "Muitos turnos lentos", "config_review": "Revisar mudança de configuração que piorou",
           "skill_outcome": "Revisar skill cujos turnos vão pior que o perfil"},
}
_ADVISORY = ("skill_review", "investigate_failure", "investigate_latency", "config_review", "skill_outcome")


def _number(value, lang: str, digits: int = 0) -> str:
    if value is None:
        return _TEXT[lang]["no_data"]
    text = f"{value:,.{digits}f}"
    return text.replace(",", "X").replace(".", ",").replace("X", ".") if lang == "pt" else text


def _seconds(ms, lang: str) -> str:
    return _TEXT[lang]["no_data"] if ms is None else _number(ms / 1000, lang, 0 if ms >= 10_000 else 1) + " s"


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
            state = (t["never_lesson"] if item["class"] == "approval_refused"
                     else f"{t['lesson_state']}: {_STATUS[lang].get(item['lesson'], t['no_lesson'])}")
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

    lines += ["", "## " + t["proposals"], ""]
    if report["proposals"]:
        for item in report["proposals"][:12]:
            step = t["look"] if item["kind"] in _ADVISORY else t["run"]
            lines.append(f"- #{item['id']} [{t['risk']}: {_RISK[lang][item['risk']]}] {_KIND[lang].get(item['kind'], item['kind'])}"
                         f" · `{item['subject']}` · {step}: `{item['command']}`")
        lines += ["", t["decide"]]
    else:
        lines.append(t["proposals_none"])
    lines += ["", "_" + t["footer"] + "_"]
    return "\n".join(lines) + "\n"
