"""Each proposal in plain words (0.8.3): what was seen, with its numbers, what applying it does and how to undo it.

The terminal menu and the dashboard panel show this instead of codes such as `skill_lock` or `[medium]`.
A proposal falls in one of four groups:
- "lesson": a lesson to activate or retire; the plugin decides it in its own playbook;
- "change": a reversible change to Hermes that the plugin applies itself, with Hermes' own command, on the
  owner's approval or, with `auto_actions: full`, by itself (`AUTOMATIC`); each one has its undo command;
- "manual": a change to Hermes the owner applies, because its undo is not certain (a rollback, a setting);
- "advice": something worth looking at, with the numbers; dismissing it keeps it closed while nothing changes.
"""

from __future__ import annotations

import re

from .report import _ADVISORY, _KIND, _RISK

# Proposal kind -> (Hermes arguments that apply it, Hermes arguments that undo it), with the skill as the last word.
APPLY = {"skill_lock": (("curator", "lock"), ("curator", "unlock")),
         "skill_archive": (("curator", "archive"), ("curator", "restore"))}
AUTOMATIC = ("skill_lock",)   # what the autopilot applies by itself with auto_actions: full
_SUBJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def category(kind: str) -> str:
    if kind in ("lesson", "lesson_retire"):
        return "lesson"
    if kind in APPLY:
        return "change"
    if kind in _ADVISORY:
        return "advice"
    return "manual"


def plan(proposal: dict) -> tuple[list, list] | None:
    """(apply, undo) Hermes arguments for a proposal the plugin can apply, or None."""
    found = APPLY.get(proposal.get("kind"))
    subject = proposal.get("subject")
    if found is None or not isinstance(subject, str) or not _SUBJECT.fullmatch(subject):
        return None
    return [*found[0], subject], [*found[1], subject]


def _n(value, lang: str, digits: int = 0) -> str:
    if not isinstance(value, (int, float)):
        return "?"
    text = f"{value:,.{digits}f}"
    return text.replace(",", "X").replace(".", ",").replace("X", ".") if lang == "pt" else text


def _duration(ms, lang: str) -> str:
    if not isinstance(ms, (int, float)):
        return "?"
    seconds = ms / 1000
    if seconds >= 120:
        return f"{_n(seconds / 60, lang, 1)} min"
    return f"{_n(seconds, lang, 0 if seconds >= 10 else 1)} s"


_FAILURE = {"en": {"command_failed": "the command failed", "code_exception": "the code raised an error",
                   "auth": "authentication was refused", "network": "the network failed", "other": "other error"},
            "pt": {"command_failed": "o comando falhou", "code_exception": "o código deu erro",
                   "auth": "a autenticação foi recusada", "network": "a rede falhou", "other": "outro erro"}}
_RATES = {"en": {"interrupted_pct": "interrupted turns", "failed_pct": "failed turns", "slow_pct": "slow turns",
                 "error_pct": "tool errors"},
          "pt": {"interrupted_pct": "turnos interrompidos", "failed_pct": "turnos com falha", "slow_pct": "turnos lentos",
                 "error_pct": "erros de ferramenta"}}


def _what(kind: str, subject: str, evidence: dict, lang: str) -> str:
    e, pt = evidence if isinstance(evidence, dict) else {}, lang == "pt"
    s = f"`{subject}`"
    if kind == "skill_lock":
        back = e.get("flipflops") if isinstance(e.get("flipflops"), int) and e["flipflops"] > 0 else 0
        return (f"A skill {s} foi reescrita {_n(e.get('rewrites'), lang)} vezes em {_n(e.get('window_days'), lang)} dias"
                + (f" e voltou atrás {_n(back, lang)} vez(es)" if back else "")
                + ". Cada reescrita custa tokens e muda o que o agente faz." if pt else
                f"The skill {s} was rewritten {_n(e.get('rewrites'), lang)} times in {_n(e.get('window_days'), lang)} days"
                + (f" and went back {_n(back, lang)} time(s)" if back else "")
                + ". Each rewrite costs tokens and changes what the agent does.")
    if kind == "skill_review":
        return (f"A skill {s} teve {_n(e.get('unreused_rewrites'), lang)} reescrita(s) que nenhum turno usou depois."
                if pt else f"The skill {s} had {_n(e.get('unreused_rewrites'), lang)} rewrite(s) no turn used afterwards.")
    if kind == "skill_archive":
        return (f"A skill {s} existe há {_n(e.get('age_days'), lang)} dias e nunca foi usada." if pt else
                f"The skill {s} is {_n(e.get('age_days'), lang)} days old and was never used.")
    if kind == "skill_outcome":
        worse = [name for name in e.get("worse") or () if isinstance(name, str)]
        parts = [f"{_RATES[lang].get(name, name)} {_n((e.get('skill_rates') or {}).get(name), lang, 1)}% "
                 f"({'perfil' if pt else 'profile'} {_n((e.get('profile_rates') or {}).get(name), lang, 1)}%)"
                 for name in worse]
        return (f"Nos {_n(e.get('turns'), lang)} turnos que carregaram {s}: " + "; ".join(parts) + "." if pt else
                f"In the {_n(e.get('turns'), lang)} turns that loaded {s}: " + "; ".join(parts) + ".")
    if kind == "investigate_failure":
        cause = _FAILURE[lang].get(e.get("class"), e.get("class") or "?")
        hint = f" ({e['hint']})" if isinstance(e.get("hint"), str) and e["hint"] else ""
        return (f"{s} falhou {_n(e.get('count'), lang)} vezes em {_n(e.get('days'), lang)} dias: {cause}{hint}. Nenhum"
                " modelo fixo de lição cobre essa falha." if pt else
                f"{s} failed {_n(e.get('count'), lang)} times in {_n(e.get('days'), lang)} days: {cause}{hint}. No fixed"
                " lesson template covers this failure.")
    if kind == "investigate_latency":
        return (f"{_n(e.get('slow_pct'), lang, 1)}% de {_n(e.get('turns'), lang)} turnos foram lentos; metade levou até"
                f" {_duration(e.get('p50_ms'), lang)} e os 5% mais lentos, {_duration(e.get('p95_ms'), lang)} ou mais." if pt else
                f"{_n(e.get('slow_pct'), lang, 1)}% of {_n(e.get('turns'), lang)} turns were slow; half took up to"
                f" {_duration(e.get('p50_ms'), lang)} and the slowest 5% {_duration(e.get('p95_ms'), lang)} or more.")
    if kind == "session_lifecycle":
        return (f"{_n(e.get('orphan_pct'), lang, 1)}% das sessões ({_n(e.get('orphaned'), lang)} de"
                f" {_n(e.get('ended'), lang)}) terminaram cortadas, sem fechar normalmente (gateway reiniciado, conexão"
                " perdida)." if pt else
                f"{_n(e.get('orphan_pct'), lang, 1)}% of sessions ({_n(e.get('orphaned'), lang)} of {_n(e.get('ended'), lang)})"
                " were cut off instead of closing normally (a gateway restart, a lost connection).")
    if kind == "context_weight":
        knobs = ", ".join(f"`{item}`" for item in e.get("candidates") or () if isinstance(item, str))
        return (f"Cada chamada ao modelo levou em média {_n(e.get('avg_prompt_tokens'), lang)} tokens de contexto"
                f" ({_n(e.get('cache_pct'), lang, 1)}% em cache) em {_n(e.get('calls'), lang)} chamadas."
                + (f" Ajustes do Hermes que reduzem isso: {knobs}." if knobs else "") if pt else
                f"Each model call carried {_n(e.get('avg_prompt_tokens'), lang)} tokens of context on average"
                f" ({_n(e.get('cache_pct'), lang, 1)}% cached) over {_n(e.get('calls'), lang)} calls."
                + (f" Hermes settings that reduce it: {knobs}." if knobs else ""))
    if kind == "compaction":
        return (f"As compactações mantiveram {_n(e.get('kept_pct'), lang, 1)}% do contexto: liberam pouco espaço." if pt
                else f"Compactions kept {_n(e.get('kept_pct'), lang, 1)}% of the context: they free little room.")
    if kind == "plugin_health":
        return (f"O plugin {s} registra {_n(e.get('registered'), lang)} hook(s) e declara {_n(e.get('declared'), lang)}"
                " no manifesto." if pt else
                f"The plugin {s} registers {_n(e.get('registered'), lang)} hook(s) and declares {_n(e.get('declared'), lang)}"
                " in its manifest.")
    if kind == "plugin_conflict":
        names = ", ".join(f"`{item}`" for item in e.get("plugins") or () if isinstance(item, str))
        return (f"Os plugins {names} mudam o resultado das ferramentas pelo hook {s}, e o Hermes só usa a mudança do"
                " primeiro: os outros não têm efeito." if pt else
                f"The plugins {names} change tool results through the hook {s}, and Hermes keeps only the first one's"
                " change: the others have no effect.")
    if kind == "tool_latency":
        return (f"{s} levou em média {_n(e.get('mean_s'), lang, 1)} s por chamada ({_n(e.get('calls'), lang)} chamadas,"
                f" {_n(e.get('total_min'), lang, 1)} min no total)." if pt else
                f"{s} took {_n(e.get('mean_s'), lang, 1)} s per call on average ({_n(e.get('calls'), lang)} calls,"
                f" {_n(e.get('total_min'), lang, 1)} min in all).")
    if kind == "approval_policy":
        settings = ", ".join(f"`{name}` {_n(count, lang)}" for name, count in (e.get("by_setting") or {}).items())
        return (f"{_n(e.get('refused'), lang)} pedidos de aprovação foram recusados; {_n(e.get('unattended'), lang)}"
                " deles onde ninguém podia aprovar (execuções únicas, cron, subagentes)"
                + (f", decididos por {settings}" if settings else "") + "." if pt else
                f"{_n(e.get('refused'), lang)} approval requests were refused; {_n(e.get('unattended'), lang)} of them where"
                " nobody could approve (one-shot runs, cron, subagents)" + (f", decided by {settings}" if settings else "") + ".")
    if kind == "review_interval":
        return (f"{_n(e.get('empty_pct'), lang, 1)}% das {_n(e.get('reviews'), lang)} revisões automáticas de skills não"
                f" mudaram nada e gastaram {_n(e.get('input_tokens'), lang)} tokens de entrada." if pt else
                f"{_n(e.get('empty_pct'), lang, 1)}% of {_n(e.get('reviews'), lang)} automatic skill reviews changed nothing"
                f" and spent {_n(e.get('input_tokens'), lang)} input tokens.")
    if kind == "lesson":
        return str(e.get("lesson") or "")
    if kind == "lesson_retire":
        return (f"Falhas por chamada antes {_n(e.get('rate_before'), lang, 2)}, depois {_n(e.get('rate_after'), lang, 2)}:"
                " a lição não ajudou." if pt else
                f"Failures per call before {_n(e.get('rate_before'), lang, 2)}, after {_n(e.get('rate_after'), lang, 2)}:"
                " the lesson did not help.")
    if kind in ("skill_rollback", "config_review"):
        worse = ", ".join(str(item) for item in e.get("worse") or ())
        return (f"Depois desta mudança em {s} os números pioraram" + (f": {worse}" if worse else "") + "." if pt else
                f"After this change to {s} the numbers got worse" + (f": {worse}" if worse else "") + ".")
    return ""


def _does(kind: str, subject: str, lang: str) -> str:
    pt = lang == "pt"
    texts = {
        "skill_lock": ("Trava a skill: o agente e o curador deixam de reescrevê-la; ela continua sendo lida e carregada,"
                       " e as suas próprias edições continuam valendo.",
                       "Locks the skill: the agent and the curator stop rewriting it; it is still read and loaded, and"
                       " your own edits still apply."),
        "skill_archive": ("Arquiva a skill: ela sai da lista ativa e pode ser restaurada.",
                          "Archives the skill: it leaves the active list and can be restored."),
        "lesson": ("Ativa a lição: o agente passa a lê-la no começo de cada sessão.",
                   "Activates the lesson: the agent reads it at the start of every session."),
        "lesson_retire": ("Aposenta a lição: o agente deixa de lê-la.", "Retires the lesson: the agent stops reading it."),
        "investigate_failure": ("Escreva uma lição com o que fazer no lugar (o menu pergunta o texto), ou deixe o"
                                " refletor rascunhar uma quando a falha acontecer nos seus turnos.",
                                "Write a lesson with what to do instead (the menu asks for the text), or let the"
                                " reflector draft one when the failure happens in your own turns."),
        "review_interval": ("Espaça a revisão automática de skills (skills.creation_nudge_interval 20), economizando"
                            " tokens.", "Spaces out the automatic skill review (skills.creation_nudge_interval 20), saving"
                            " tokens."),
        "skill_rollback": ("Desfaz a última mudança da skill pelo curador do Hermes.",
                           "Rolls the skill's last change back with Hermes' curator."),
    }
    if kind in texts:
        return texts[kind][0 if pt else 1]
    if kind in _ADVISORY:
        return ("É uma observação: nada muda sozinho. Veja os números com o comando abaixo; dispensar a fecha enquanto"
                " nada mudar." if pt else
                "It is an observation: nothing changes by itself. See the numbers with the command below; dismissing it"
                " closes it while nothing changes.")
    return ""


def describe(proposal: dict, lang: str = "en") -> dict:
    """The proposal as the menu and the panel show it."""
    lang = lang if lang in ("en", "pt") else "en"
    kind, subject = proposal.get("kind") or "", str(proposal.get("subject") or "")
    found = plan(proposal)
    undo = None
    if found is not None:
        undo = "hermes " + " ".join(found[1])
    return {"title": _KIND[lang].get(kind, kind), "risk": _RISK[lang].get(proposal.get("risk"), proposal.get("risk")),
            "category": category(kind), "what": _what(kind, subject, proposal.get("evidence") or {}, lang),
            "does": _does(kind, subject, lang), "undo": undo, "can_apply": found is not None,
            "automatic": kind in AUTOMATIC and found is not None}
