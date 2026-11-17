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
    try:
        from .evolution_index import current as evolution_now   # `current` is this function's metrics window

        with experience(home) as conn:
            report["evolution_index"] = evolution_now(conn, now=now)
    except (sqlite3.Error, ValueError):
        report["evolution_index"] = None
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
            crossing = (native.get("coverage") or {}).get("rows_crossing_window_start")
            report["native_cost"] = {"estimated_cost_usd": totals.get("estimated_cost_usd"),
                                     "api_call_count": totals.get("api_call_count"), "basis": "hermes_session_accounting",
                                     # Hermes counts a session's cost whole: one that began earlier brings it along
                                     "sessions_started_before": crossing if type(crossing) is int else 0}
    except (sqlite3.Error, ValueError, OSError):
        pass
    return report


_TEXT = {
    "en": {
        "title": "Atlas Evolution · {days}-day digest ({span}, UTC)",
        "summary": "Summary", "turns": "Interactive turns", "of": "of", "failed": ("{n} failed", "{n} failed"),
        "interrupted": ("{n} interrupted", "{n} interrupted"), "slow": ("{n} slow", "{n} slow"), "turn_time": "Turn time", "typical": "half within", "p95": "1 in 20 takes more than",
        "tools": "Tool calls", "errors": ("{n} error", "{n} errors"), "cost": "Estimated cost (Hermes accounting)",
        "cost_earlier": ("includes {n} session that began before this period, counted whole",
                         "includes {n} sessions that began before this period, counted whole"),
        "feedback": "Owner signals", "corrections": "corrections", "reasks": "repeated questions", "thanks": "thanks",
        "vs": "Versus the previous {days} days", "verdict": "Verdict", "failures": "Failures that repeat",
        "failures_none": "No recurring tool failure in this window.", "times": ("1 time", "{n} times"),
        "days": ("1 day", "{n} days"),
        "lesson_state": "lesson", "no_lesson": "no lesson yet", "learning": "Self-improvement loop",
        "never_lesson": "never a lesson: the user's approval decides",
        "other_plugin": "never a lesson: another plugin caused it",
        "reviews": ("{n} background review", "{n} background reviews"), "empty": "produced nothing", "review_tokens": "review tokens (input / output)",
        "share": "of the uncached input and output tokens went to learning (approximate)",
        "rewrites": ("{n} skill rewrite", "{n} skill rewrites"), "by_review": "by the automatic review",
        "skills": ("{n} skill", "{n} skills"),
        "churn": "Skills rewritten the most", "flipflops": "flip-flops", "uses": "uses",
        "outcomes": "Skills by outcome", "outcomes_none": "Not enough turns with a loaded skill yet.",
        "whole_profile": "whole profile", "turns_word": "turns", "failed_short": "failed", "corrected_short": "corrected",
        "interrupted_short": "interrupted", "tool_errors_short": "with tool errors", "below": "worse than the profile",
        "hidden": "(text hidden: read it in your own terminal)",
        "index": "Evolution index", "index_week": "in 7 days",
        "lessons": "Lessons", "active": "active", "candidate": "awaiting approval", "retired": "retired",
        "withdrawn": "dropped from the queue without a decision",
        "effect": "measured effect", "before": "before", "after": "after", "changes": "Changes and their measured effect",
        "changes_none": "No change evaluated yet.", "proposals": "Proposals awaiting a decision",
        "proposals_none": "Nothing is waiting for you.", "risk": "risk", "run": "apply with", "look": "next step",
        "proposal": "Proposal", "trash": "approving moves `{place}` to the plugin's trash",
        "no_data": "not measured", "footer": "Aggregates only. No conversation text, tool output or memory content is stored.",
        "decide": "Decide on each of these in your terminal with `hermes evolution proposals approve <id>` or "
                  "`hermes evolution proposals reject <id>`.",
        "lessons_waiting": "{count} lessons await approval (decide on each in your terminal with `hermes evolution lessons review`)",
        "lessons_waiting_one": "1 lesson awaits approval (decide on it in your terminal with `hermes evolution lessons review`)",
        "health": "Hermes health", "sessions_line": "Sessions", "ended": "ended", "orphaned": "ended as orphans",
        "orphan_note": "reaped after a disconnect or restart, so Hermes' end-of-session work did not run",
        "context_line": "Context per agent call", "avg": "average of", "tokens": "tokens", "cached": "served from cache",
        "calls": "calls", "compaction_line": "Compactions", "kept": "kept", "of_context": "of the context",
        "plugins_line": "Plugins of this process", "enabled": "enabled", "load_errors": "failed to load",
        "undeclared": "register hooks they do not declare", "overlap": "compete for", "first_wins": "only the first result counts",
        "guard_line": "Runtime guard", "vetoed": "commands vetoed", "gateway_stop": "gateway restart or kill",
        "change_kinds": {"lesson": "lesson", "skill": "skill", "config": "setting"},
        "runtime_removal": "removal of the running Hermes", "plugin_settings": "changes to this plugin's settings",
        "health_none": "Nothing measured yet.",
        "external_line": "Failures caused by other plugins",
        "approvals_line": "Refused approvals", "approvals_unattended": "where nobody could approve",
        "slow_line": "Slowest tools", "slow_mean": "s on average", "slow_calls": "calls",
        "approvals_decided": "decided by",
        "approvals_hint": "review those settings with `hermes config get`, and the `command_allowlist`; never approve everything",
        "external_fault": "calls blocked because a plugin's `pre_tool_call` hung or failed",
        "external_rewrite": "commands rewritten by a plugin that then failed (never turned into lessons)",
        "research_line": "Web research", "research_turns": "turns", "research_slow": "over {limit} s",
        "research_errors": "errors in", "research_calls": "calls", "research_p95": "1 in 20 takes more than",
    },
    "pt": {
        "title": "Atlas Evolution · resumo de {days} dias ({span}, UTC)",
        "summary": "Resumo", "turns": "Turnos interativos", "of": "de", "failed": ("{n} falhou", "{n} falharam"),
        "interrupted": ("{n} interrompido", "{n} interrompidos"), "slow": ("{n} lento", "{n} lentos"), "turn_time": "Duração dos turnos", "typical": "metade até", "p95": "1 em cada 20 passa de",
        "tools": "Chamadas de ferramentas", "errors": ("{n} erro", "{n} erros"), "cost": "Custo estimado (contabilidade do Hermes)",
        "cost_earlier": ("inclui {n} sessão que começou antes deste período, contada inteira",
                         "inclui {n} sessões que começaram antes deste período, contadas inteiras"),
        "feedback": "Sinais do dono", "corrections": "correções", "reasks": "perguntas repetidas", "thanks": "agradecimentos",
        "vs": "Comparado aos {days} dias anteriores", "verdict": "Veredito", "failures": "Falhas que se repetem",
        "failures_none": "Nenhuma falha de ferramenta se repetiu neste período.", "times": ("1 vez", "{n} vezes"),
        "days": ("1 dia", "{n} dias"),
        "lesson_state": "lição", "no_lesson": "sem lição ainda", "learning": "Ciclo de automelhoria",
        "never_lesson": "nunca vira lição: quem decide é a aprovação do usuário",
        "other_plugin": "nunca vira lição: outro plugin causou a falha",
        "reviews": ("{n} revisão em segundo plano", "{n} revisões em segundo plano"), "empty": "não produziram nada", "review_tokens": "tokens das revisões (entrada / saída)",
        "share": "dos tokens de entrada sem cache e de saída foram para o aprendizado (aproximado)",
        "rewrites": ("{n} reescrita de skill", "{n} reescritas de skills"), "by_review": "pela revisão automática",
        "skills": ("{n} skill", "{n} skills"),
        "churn": "Skills mais reescritas", "flipflops": "vai-e-volta", "uses": "usos",
        "outcomes": "Skills pelo resultado", "outcomes_none": "Ainda não há turnos suficientes com skill carregada.",
        "whole_profile": "perfil inteiro", "turns_word": "turnos", "failed_short": "falharam", "corrected_short": "corrigidos",
        "interrupted_short": "interrompidos", "tool_errors_short": "com erro de ferramenta", "below": "pior que o perfil",
        "hidden": "(texto oculto: leia no seu próprio terminal)",
        "index": "Índice de evolução", "index_week": "em 7 dias",
        "lessons": "Lições", "active": "ativas", "candidate": "aguardando aprovação", "retired": "aposentadas",
        "withdrawn": "descartadas sem decisão (fila limpa)",
        "effect": "efeito medido", "before": "antes", "after": "depois", "changes": "Mudanças e o efeito medido",
        "changes_none": "Nenhuma mudança avaliada ainda.", "proposals": "Propostas aguardando decisão",
        "proposals_none": "Nada aguardando você.", "risk": "risco", "run": "aplique com", "look": "próximo passo",
        "proposal": "Proposta", "trash": "aprovar move `{place}` para a lixeira do plugin",
        "no_data": "não medido", "footer": "Só agregados. Nenhum texto de conversa, saída de ferramenta ou conteúdo de memória é guardado.",
        "decide": "Decida cada uma no seu terminal com `hermes evolution proposals approve <id>` ou "
                  "`hermes evolution proposals reject <id>`.",
        "lessons_waiting": "{count} lições aguardam aprovação (decida cada uma no seu terminal com `hermes evolution lessons review`)",
        "lessons_waiting_one": "1 lição aguarda aprovação (decida no seu terminal com `hermes evolution lessons review`)",
        "health": "Saúde do Hermes", "sessions_line": "Sessões", "ended": "encerradas", "orphaned": "terminaram como órfãs",
        "orphan_note": "recolhidas após desconexão ou reinício, sem o encerramento normal do Hermes",
        "context_line": "Contexto por chamada do agente", "avg": "média de", "tokens": "tokens", "cached": "servido do cache",
        "calls": "chamadas", "compaction_line": "Compactações", "kept": "mantiveram", "of_context": "do contexto",
        "plugins_line": "Plugins deste processo", "enabled": "ativos", "load_errors": "falharam ao carregar",
        "undeclared": "registram ganchos (hooks) que não declaram", "overlap": "disputam", "first_wins": "só o primeiro resultado vale",
        "guard_line": "Proteção do Hermes", "vetoed": "comandos barrados", "gateway_stop": "reinício ou encerramento do gateway",
        "change_kinds": {"lesson": "lição", "skill": "skill", "config": "configuração"},
        "runtime_removal": "remoção do Hermes em execução", "plugin_settings": "mudanças nas configurações deste plugin",
        "health_none": "Nada medido ainda.",
        "external_line": "Falhas causadas por outros plugins",
        "approvals_line": "Aprovações recusadas", "approvals_unattended": "onde ninguém podia aprovar",
        "slow_line": "Ferramentas mais lentas", "slow_mean": "s em média", "slow_calls": "chamadas",
        "approvals_decided": "decididas por",
        "approvals_hint": "revise essas configurações com `hermes config get`, e o `command_allowlist`; nunca aprove tudo",
        "external_fault": "chamadas bloqueadas porque o `pre_tool_call` de um plugin travou ou falhou",
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


# The failure classes of signals.CLASSES in words: a person never reads `missing_argument`.
FAILURE_CLASSES = {
    "en": {"missing_argument": "a required argument was missing", "approval_refused": "the approval was refused",
           "locked_resource": "the owner locked what it tried to change",
           "review_precondition": "the automatic review was not allowed to change it",
           "edit_precondition": "it tried to edit a file without reading it first",
           "edit_mismatch": "the text to replace was not found", "blocked_by_policy": "the safety policy blocked it",
           "rate_limited": "the provider's rate limit", "timeout": "it took too long",
           "missing_dependency": "a program or module was missing", "permission_denied": "permission was denied",
           "auth": "authentication was refused", "backend_unavailable": "the service it needs was down",
           "not_found": "what it asked for was not found", "limit_exceeded": "a size limit was exceeded",
           "invalid_argument": "an argument was invalid", "code_exception": "the code raised an error",
           "network": "the network failed", "command_failed": "the command failed", "other": "another error",
           "plugin_fault": "another plugin hung or failed before the call",
           "rewritten_command": "a command another plugin rewrote failed"},
    "pt": {"missing_argument": "faltou um argumento obrigatório", "approval_refused": "a aprovação foi recusada",
           "locked_resource": "o dono travou o que ele tentou mudar",
           "review_precondition": "a revisão automática não podia mudar isso",
           "edit_precondition": "tentou editar um arquivo sem lê-lo antes",
           "edit_mismatch": "o trecho a trocar não foi encontrado", "blocked_by_policy": "a política de segurança barrou",
           "rate_limited": "o limite de uso do provedor", "timeout": "demorou demais",
           "missing_dependency": "faltou um programa ou módulo", "permission_denied": "faltou permissão",
           "auth": "a autenticação foi recusada", "backend_unavailable": "o serviço de que precisa estava fora do ar",
           "not_found": "o que pediu não foi encontrado", "limit_exceeded": "passou de um limite de tamanho",
           "invalid_argument": "um argumento era inválido", "code_exception": "o código deu erro",
           "network": "a rede falhou", "command_failed": "o comando falhou", "other": "outro erro",
           "plugin_fault": "outro plugin travou ou falhou antes da chamada",
           "rewritten_command": "um comando que outro plugin reescreveu falhou"},
}


def failure_words(klass, hint, lang: str = "en") -> str:
    """faltou um argumento obrigatório (`servico`); o comando falhou (código de saída 1)."""
    lang = lang if lang in FAILURE_CLASSES else "en"
    words = FAILURE_CLASSES[lang].get(klass) or FAILURE_CLASSES[lang]["other"]
    if not isinstance(hint, str) or not hint:
        return words
    if hint == "exit_signal":
        return words + (" (encerrado por um sinal)" if lang == "pt" else " (ended by a signal)")
    if hint.startswith("exit_") and hint[5:].isdigit():
        return words + (f" (código de saída {hint[5:]})" if lang == "pt" else f" (exit code {hint[5:]})")
    return f"{words} (`{hint}`)"


# The measurements `experiments.compare` weighs, in words.
_METRIC = {
    "en": {"turn_failed_pct": "failed turns (%)", "turn_slow_pct": "slow turns (%)", "turn_mean_ms": "mean turn time (ms)",
           "tool_error_pct": "tool errors (%)", "owner_corrections_per_100_turns": "your corrections per 100 turns",
           "uncached_input_tokens_per_call": "uncached input tokens per call"},
    "pt": {"turn_failed_pct": "turnos com falha (%)", "turn_slow_pct": "turnos lentos (%)",
           "turn_mean_ms": "duração média do turno (ms)", "tool_error_pct": "erros de ferramenta (%)",
           "owner_corrections_per_100_turns": "correções suas a cada 100 turnos",
           "uncached_input_tokens_per_call": "tokens de entrada sem cache por chamada"},
}
# Where refused approvals came from (Hermes' session sources), in words; a chat platform keeps its name.
_ORIGINS = {
    "en": {"oneshot": "one-shot runs", "subagent": "subagents", "local": "your terminal", "cli": "your terminal",
           "cron": "scheduled tasks", "tui": "your terminal", "desktop": "Desktop", "kanban": "Kanban"},
    "pt": {"oneshot": "execuções únicas", "subagent": "subagentes", "local": "o seu terminal", "cli": "o seu terminal",
           "cron": "tarefas agendadas", "tui": "o seu terminal", "desktop": "Desktop", "kanban": "Kanban"},
}


def _date(day: str, lang: str) -> str:
    """2026-11-16 as 16/11/2026 in Portuguese."""
    if lang == "pt" and isinstance(day, str) and len(day) == 10 and day[4] == day[7] == "-":
        return f"{day[8:10]}/{day[5:7]}/{day[:4]}"
    return str(day)


def _span(start: str, end: str, lang: str) -> str:
    """The window of the title: 10/11 a 16/11/2026 in Portuguese, 2026-11-10 to 2026-11-16 in English."""
    if lang != "pt":
        return f"{start} to {end}"
    first, last = _date(start, lang), _date(end, lang)
    return f"{first[:5] if first[6:] == last[6:] else first} a {last}"



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
           "plugin_conflict": "Plugins compete for a hook where only the first counts",
           "approval_policy": "Approvals refused where nobody could approve",
           "tool_latency": "A tool that is slow call after call",
           "plugin_removed": "Plugin removed from the Hermes catalog still installed",
           "plugin_transform_protected": "Two protective plugins change the same tool's result",
           "plugin_transform_unclear": "Two plugins may change the same result",
           "plugin_hints_overlap": "This plugin's hints hide another plugin's change",
           "self_off": "Profile in use with Atlas Evolution turned off",
           "plugin_transform_overlap": "Two plugins change the same tool's result",
           "plugin_duplicate": "Two plugins do the same job", "plugin_unused": "Plugins installed and never loaded",
           "plugin_leftover": "Folder left by a failed plugin install", "plugin_update": "Newer reviewed plugin version",
           "self_missing": "Profile in use without Atlas Evolution", "profile_idle": "Profile unused for 30 days",
           "skills_unused": "Skills unused for 30 days", "skills_duplicate": "The same skill under two names",
           "skills_similar": "Skills that look like the same job", "skills_shared": "Skills copied into many profiles",
           "plugin_obsolete": "Archived or unmaintained plugin",
           "doctor_cache": "Hermes doctor: large cache folder", "doctor_checkpoints": "Rollback snapshots above the cap",
           "doctor_issue": "Hermes doctor lists a problem", "doctor_security": "Hermes doctor: security problem"},
    "pt": {"lesson": "Ativar lição", "lesson_retire": "Aposentar lição que não ajudou",
           "skill_lock": "Travar skill reescrita em excesso", "skill_review": "Revisar reescritas nunca usadas",
           "skill_archive": "Arquivar skill nunca usada", "skill_rollback": "Desfazer alteração de skill que piorou",
           "review_interval": "Espaçar a revisão automática", "investigate_failure": "Falha recorrente sem lição",
           "investigate_latency": "Muitos turnos lentos", "config_review": "Revisar mudança de configuração que piorou",
           "skill_outcome": "Revisar skill cujos turnos vão pior que o perfil",
           "session_lifecycle": "Muitas sessões terminam como órfãs", "context_weight": "Contexto pesado por chamada",
           "compaction": "Compactações mantêm quase todo o contexto", "plugin_health": "Problema em plugin",
           "plugin_conflict": "Plugins disputam um hook em que só o primeiro vale",
           "approval_policy": "Aprovações recusadas onde ninguém podia aprovar",
           "tool_latency": "Ferramenta lenta chamada após chamada",
           "plugin_removed": "Plugin removido do catálogo do Hermes ainda instalado",
           "plugin_transform_protected": "Dois plugins de proteção mudam o resultado da mesma ferramenta",
           "plugin_transform_unclear": "Dois plugins podem mudar o mesmo resultado",
           "plugin_hints_overlap": "As dicas deste plugin escondem a mudança de outro",
           "self_off": "Perfil em uso com o Atlas Evolution desligado",
           "plugin_transform_overlap": "Dois plugins mudam o resultado da mesma ferramenta",
           "plugin_duplicate": "Dois plugins fazem o mesmo trabalho", "plugin_unused": "Plugins instalados que nunca carregam",
           "plugin_leftover": "Pasta deixada por instalação de plugin que falhou",
           "plugin_update": "Versão revisada mais nova de plugin", "self_missing": "Perfil em uso sem o Atlas Evolution",
           "profile_idle": "Perfil sem uso há 30 dias", "skills_unused": "Skills sem uso há 30 dias",
           "skills_duplicate": "A mesma skill com dois nomes", "skills_similar": "Skills que parecem fazer o mesmo trabalho",
           "skills_shared": "Skills copiadas em vários perfis", "plugin_obsolete": "Plugin arquivado ou sem manutenção",
           "doctor_cache": "Checagem do Hermes: pasta de cache grande",
           "doctor_checkpoints": "Pontos de restauração acima do limite",
           "doctor_issue": "A checagem do Hermes (`hermes doctor`) aponta um problema",
           "doctor_security": "Checagem do Hermes: problema de segurança"},
}
_ADVISORY = ("skill_review", "investigate_failure", "investigate_latency", "config_review", "skill_outcome",
             "session_lifecycle", "context_weight", "compaction", "plugin_health", "plugin_conflict", "approval_policy",
             "tool_latency", "profile_idle", "skills_similar", "skills_shared", "plugin_transform_protected",
             "plugin_transform_unclear")


def _number(value, lang: str, digits: int = 0) -> str:
    if value is None:
        return _TEXT[lang]["no_data"]
    text = f"{value:,.{digits}f}"
    return text.replace(",", "X").replace(".", ",").replace("X", ".") if lang == "pt" else text


def _plural(entry, count, lang: str) -> str:
    """A (singular, plural) pair with the count written for a person."""
    return (entry[0] if count == 1 else entry[1]).format(n=_number(count, lang))


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
    slowest = health.get("slow_tools") or []
    if slowest:
        lines.append(f"- **{t['slow_line']}:** " + " · ".join(
            f"`{item['tool']}` {_number(item['mean_s'], lang, 1)} {t['slow_mean']} ({_number(item['calls'], lang)} "
            f"{t['slow_calls']})" for item in slowest))
    refusals = health.get("approvals") or {}
    if refusals.get("refused"):
        origins = ", ".join(f"{_ORIGINS[lang].get(name, f'`{name}`')} {_number(count, lang)}"
                            for name, count in list(refusals["by_origin"].items())[:5])
        line = (f"- **{t['approvals_line']}:** {_number(refusals['refused'], lang)} ({origins}) · "
                f"{_number(refusals['unattended'], lang)} {t['approvals_unattended']}")
        if refusals["unattended"]:
            settings = ", ".join(f"`{name}` {_number(count, lang)}"
                                 for name, count in list((refusals.get("by_setting") or {}).items())[:4])
            line += f" ({t['approvals_decided']} {settings}) · {t['approvals_hint']}" if settings else \
                f" · {t['approvals_hint']}"
        lines.append(line)
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


def _step(item: dict, lang: str) -> str:
    """What approving a proposal runs, as the owner would type it, or its next step; never the plugin's own codes."""
    from .explain import command_text, plan

    t = _TEXT[lang]
    found = plan(item)
    if found is not None:
        text = command_text(found[0], lang)
        if text.startswith("hermes "):
            return f"{t['run']}: `{text}`"
        args = list(found[0])
        profile = str(args[0])[1:] if args and str(args[0]).startswith("@") else "default"
        folder = str(args[-1])
        place = f"{profile}/{folder}" if folder.startswith("cache/") else f"{profile}/plugins/{folder}"
        return t["trash"].format(place=place)   # the plugin's trash, said in words
    command = item.get("command")
    if isinstance(command, str) and command.strip() and not command.startswith("__"):
        return f"{t['look']}: `{command}`"
    return ""


def subject_words(item: dict, lang: str = "en") -> str:
    """What a proposal is about, for a person: a finding of Hermes' doctor names its profile ("perfil `work`"), not
    its internal key (`work/security.GHSA-x`); anything else shows its subject."""
    subject = str(item.get("subject") or "")
    if str(item.get("kind") or "").startswith("doctor_"):
        return ("perfil" if lang == "pt" else "profile") + f" `{subject.split('/', 1)[0]}`"
    return f"`{subject}`"


def _proposal_lines(report: dict, lang: str) -> list[str]:
    """Lessons awaiting approval on one line (their text is in `lessons list`), then the other proposals."""
    from .explain import describe

    t = _TEXT[lang]
    lines = ["", "## " + t["proposals"], ""]
    waiting = [item for item in report["proposals"] if item["kind"] == "lesson"]
    if waiting:
        listed = ", ".join(f"#{item['evidence'].get('lesson_id', item['id'])} `{item['subject']}`" for item in waiting[:10])
        heading = t["lessons_waiting_one"] if len(waiting) == 1 else t["lessons_waiting"].format(count=len(waiting))
        lines.append(f"- {heading}: {listed}")
    others = [entry for entry in report["proposals"] if entry["kind"] != "lesson"]
    for item in others[:20]:
        title = describe(item, lang)["title"]   # the title the Proposals screens show
        step = _step(item, lang)
        lines.append(f"- {t['proposal']} #{item['id']} [{t['risk']}: {_RISK[lang].get(item['risk'], item['risk'])}] "
                     f"{title if title != item['kind'] else _KIND[lang].get(item['kind'], item['kind'])} · "
                     f"{subject_words(item, lang)}" + (f" · {step}" if step else ""))
    if others:   # lessons are decided with `lessons review` (said above); every other proposal with this
        lines += ["", t["decide"]]
    if not report["proposals"]:
        lines.append(t["proposals_none"])
    return lines


def markdown(report: dict, lang: str = "en") -> str:
    """Render the digest for a person."""
    lang = lang if lang in _TEXT else "en"
    t, verdicts = _TEXT[lang], _VERDICT[lang]
    window = report["window"]
    turns, tools, feedback = report["turns"]["interactive"], report["tools"], report["owner_feedback"]
    lines = ["# " + t["title"].format(days=window["days"], span=_span(window["from_utc"], window["to_utc"], lang)), "",
             "## " + t["summary"], ""]
    lines.append(f"- **{t['turns']}:** {_number(turns['turns'], lang)} · {_plural(t['failed'], turns['failed'], lang)}"
                 f" · {_plural(t['interrupted'], turns['interrupted'], lang)} · {_plural(t['slow'], turns['slow'], lang)}")
    if turns["turns"]:
        lines.append(f"- **{t['turn_time']}:** {t['typical']} {_seconds(turns['p50_ms'], lang)} · {t['p95']} "
                     f"{_seconds(turns['p95_ms'], lang)}")
    lines.append(f"- **{t['tools']}:** {_number(tools['calls'], lang)} · {_plural(t['errors'], tools['errors'], lang)}"
                 + (f" ({_number(tools['error_pct'], lang, 1)}%)" if tools["error_pct"] is not None else ""))
    index = report.get("evolution_index") or {}
    if index.get("score") is not None:
        trend = index.get("trend_7d")
        lines.append(f"- **{t['index']}:** {index['score']}/100"
                     + ("" if trend is None else f" ({'+' if trend > 0 else ''}{trend} {t['index_week']})"))
    cost = report.get("native_cost", {}).get("estimated_cost_usd")
    if cost is not None:
        earlier = report.get("native_cost", {}).get("sessions_started_before") or 0
        lines.append(f"- **{t['cost']}:** US$ {_number(cost, lang, 2)}"
                     + (" (" + _plural(t["cost_earlier"], earlier, lang) + ")" if earlier else ""))
    lines.append(f"- **{t['feedback']}:** {t['corrections']} {feedback['correction']} · {t['reasks']} {feedback['reask']}"
                 f" · {t['thanks']} {feedback['positive']}")
    comparison = report["comparison"]
    lines += ["", "### " + t["vs"].format(days=window["days"]), ""]
    if comparison["verdict"]["metrics"]:
        for name, values in comparison["verdict"]["metrics"].items():
            mark = "▲" if name in comparison["verdict"]["worse"] else "▼" if name in comparison["verdict"]["better"] else "·"
            lines.append(f"- {_METRIC[lang].get(name, f'`{name}`')}: {_number(values['before'], lang, 1)} → "
                         f"{_number(values['after'], lang, 1)} {mark}")
    lines.append(f"- **{t['verdict']}:** {verdicts[comparison['verdict']['verdict']]}")

    lines += ["", "## " + t["failures"], ""]
    if report["recurring_failures"]:
        for item in report["recurring_failures"][:8]:
            if item["class"] == "approval_refused":
                state = t["never_lesson"]
            elif item["class"] in ("plugin_fault", "rewritten_command"):
                state = t["other_plugin"]
            else:
                state = f"{t['lesson_state']}: {_STATUS[lang].get(item['lesson'], t['no_lesson'])}"
            lines.append(f"- `{item['tool']}` · {failure_words(item['class'], item['hint'], lang)}: "
                         f"{_plural(t['times'], item['count'], lang)}, {_plural(t['days'], item['days'], lang)} · {state}")
    else:
        lines.append(t["failures_none"])

    reviews, skills = report["self_improvement"]["background_reviews"], report["self_improvement"]["skills"]
    lines += ["", "## " + t["learning"], ""]
    lines.append(f"- {_plural(t['reviews'], reviews['reviews'], lang)}"
                 + (f" · {_number(reviews['empty_pct'], lang, 1)}% {t['empty']}" if reviews["empty_pct"] is not None else ""))
    if reviews["reviews"]:
        lines.append(f"- {t['review_tokens']}: {_number(reviews['input_tokens'], lang)} / {_number(reviews['output_tokens'], lang)}")
    share = report["self_improvement"].get("learning_share_pct")
    if share is not None:
        lines.append(f"- {_number(share, lang, 1)}% {t['share']}")
    by_review = skills["rewrites_by_actor"].get("curator", 0)
    lines.append(f"- {_plural(t['rewrites'], skills['rewrites'], lang)} ({_number(by_review, lang)} {t['by_review']})"
                 f" · {_plural(t['skills'], skills['skills_rewritten'], lang)}")
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
    withdrawn = sum(item["status"] == "retired" and item.get("verdict") == "withdrawn" for item in playbook)
    lines += ["", "## " + t["lessons"], "",
              f"- {t['active']}: {counts['active']} · {t['candidate']}: {counts['candidate'] + counts['quarantined']}"
              f" · {t['retired']}: {counts['retired'] - withdrawn}"
              + (f" · {t['withdrawn']}: {withdrawn}" if withdrawn else "")]
    for item in [entry for entry in playbook if entry["status"] == "active"][:8]:
        effect = verdicts[item["verdict"]]
        numbers = (f" ({t['before']} {_number(item['rate_before'], lang, 3)} → {t['after']} {_number(item['rate_after'], lang, 3)})"
                   if item["rate_before"] is not None and item["rate_after"] is not None else "")
        lines.append(f"- #{item['id']} {item['lesson'] or t['hidden']} · {t['effect']}: {effect}{numbers}")

    lines += ["", "## " + t["changes"], ""]
    measured = [item for item in report["changes"] if item["verdict"]]
    if measured:
        for item in measured[:8]:
            lines.append(f"- {t['change_kinds'].get(item['kind'], item['kind'])} `{item['subject']}` "
                         f"({_date(utc_day(item['at']), lang)}): {verdicts[item['verdict']]}")
    else:
        lines.append(t["changes_none"])

    lines += _health(report, lang)
    lines += _proposal_lines(report, lang)
    lines += ["", "_" + t["footer"] + "_"]
    return "\n".join(lines) + "\n"
