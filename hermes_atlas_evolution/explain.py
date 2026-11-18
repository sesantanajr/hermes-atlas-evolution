"""Each proposal in plain words: what was seen, with its numbers, what applying it does and how to undo it.

The terminal menu and the dashboard panel show this instead of codes such as `skill_lock` or `[medium]`.
A proposal falls in one of four groups:
- "lesson": a lesson to activate or retire; the plugin decides it in its own playbook;
- "change": a reversible change to Hermes that the plugin applies itself, with Hermes' own command, on the
  owner's approval or, in the Mode auto, by itself (`AUTOMATIC`); each one has its undo command;
- "manual": a change to Hermes the owner applies, because its undo is not certain (a rollback, a setting);
- "advice": something worth looking at, with the numbers; dismissing it hides it for 30 days.
"""

from __future__ import annotations

import re

from .report import _ADVISORY, _KIND, _RISK

# Proposal kind -> (Hermes arguments that apply it, Hermes arguments that undo it), with the skill as the last word.
# Only changes whose undo is certain: archiving a skill is not undone by `curator restore` (Hermes 0.23.1 restores it
# at the top level under its frontmatter name, or an older archived copy), so it stays a change the owner applies.
APPLY = {"skill_lock": (("curator", "lock"), ("curator", "unlock"))}
# 0.8.5: changes in any profile of this Hermes (environment.py). The subject is "<profile>/<name>"; what runs is
# recorded as ["@<profile>", ...] and runs with that profile's own `hermes --profile <profile>`. Before it runs,
# actions.apply_proposal reads the profile again from disk (environment.verify): a stored proposal is never trusted.
FLEET = {"plugin_transform_overlap": "disable", "plugin_duplicate": "disable", "self_missing": "install",
         "plugin_leftover": "trash", "doctor_cache": "trash"}
AUTOMATIC = ("skill_lock", "self_missing", "plugin_leftover")   # what the Mode auto applies by itself
NOT_BULK = ("plugin_transform_overlap", "plugin_duplicate", "doctor_cache")   # the owner's call, one by one
_SUBJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_PROFILE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")   # Hermes' rule for a profile name
_PLUGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}")
_LEFTOVER = re.compile(r"\.install-[A-Za-z0-9_-]{1,64}")
_CACHE = re.compile(r"cache/([A-Za-z0-9][A-Za-z0-9._-]{0,95})")
_SHA = re.compile(r"[0-9a-f]{40}")
SELF, SOURCE = "hermes-atlas-evolution", "sesantanajr/hermes-atlas-evolution"


def _fleet_undo(forward: list) -> list | None:
    """The undo of a fleet change, from the change's own shape only."""
    if len(forward) == 3 and forward[:2] == ["plugins", "disable"] and _PLUGIN.fullmatch(forward[2]) \
            and forward[2] != SELF:
        return ["plugins", "enable", forward[2]]
    if forward == ["plugins", "install", SELF, "--enable"] or forward[:4] == ["plugins", "install", SOURCE, "--enable"] \
            and (len(forward) == 4 or len(forward) == 6 and forward[4] == "--ref" and _SHA.fullmatch(forward[5])):
        return ["plugins", "remove", SELF]   # by its catalog name, or this repository (at a commit)
    if len(forward) == 2 and forward[0] == "__trash__" and (_LEFTOVER.fullmatch(forward[1]) or (
            _CACHE.fullmatch(forward[1]) and _CACHE.fullmatch(forward[1]).group(1) not in ("scratch", "terminal"))):
        return ["__restore__", forward[1]]
    return None


def _target(subject) -> tuple[str, str] | None:
    if not isinstance(subject, str) or subject.count("/") != 1:
        return None
    profile, name = subject.split("/")
    return (profile, name) if _PROFILE.fullmatch(profile) and (_PLUGIN.fullmatch(name) or _LEFTOVER.fullmatch(name)) \
        else None


def category(kind: str) -> str:
    if kind in ("lesson", "lesson_retire"):
        return "lesson"
    if kind in APPLY or kind in FLEET:
        return "change"
    if kind in _ADVISORY:
        return "advice"
    return "manual"


def undo_for(applied) -> list | None:
    """The Hermes arguments that undo `applied` (what a decision recorded it ran), derived again from APPLY and the
    fleet changes' shapes and never read from the record: a changed record can only fail to undo, never run
    something else."""
    if not isinstance(applied, list) or len(applied) < 2 or not all(isinstance(part, str) for part in applied):
        return None
    if applied[0].startswith("@"):
        if not _PROFILE.fullmatch(applied[0][1:]):
            return None
        backward = _fleet_undo(list(applied[1:]))
        return None if backward is None else [applied[0], *backward]
    subject = applied[-1]
    if not _SUBJECT.fullmatch(subject):
        return None
    for forward, backward in APPLY.values():
        if list(applied[:-1]) == list(forward):
            return [*backward, subject]
    return None


def plan(proposal: dict, *, home=None) -> tuple[list, list] | None:
    """(apply, undo) Hermes arguments for a proposal the plugin can apply, or None. `home` is the profile that applies
    it: installing this plugin elsewhere gives the code that profile runs (fleet_update.source), so without it there
    is nothing to install."""
    fleet = FLEET.get(proposal.get("kind"))
    if fleet is not None:
        target = _target(proposal.get("subject"))
        if target is None:
            return None
        profile, name = target
        evidence = proposal.get("evidence")
        if isinstance(evidence, str):
            try:
                import json

                evidence = json.loads(evidence)
            except ValueError:
                evidence = {}
        evidence = evidence if isinstance(evidence, dict) else {}
        if fleet == "install":
            if name != SELF:
                return None   # only this plugin installs itself
            # 0.13.0: the code the applying profile runs, never a commit the plugin picks: its catalog name (Hermes
            # installs the reviewed pin) or this repository at that profile's own commit. A copy pinned that way is
            # moved later by `hermes evolution update`, to the code of the profile that runs it (fleet_update).
            from .fleet_update import source

            found = source(home) if home is not None else None
            if found == [SELF]:
                forward = ["plugins", "install", SELF, "--enable"]
            elif found and len(found) == 3 and found[:2] == [SOURCE, "--ref"] and _SHA.fullmatch(found[2]):
                forward = ["plugins", "install", SOURCE, "--enable", "--ref", found[2]]
            else:
                return None
        elif fleet == "trash":
            if proposal.get("kind") == "doctor_cache":
                from .doctor import REGENERABLE

                folder = name[len("cache."):]
                # only a package cache that rebuilds itself; any other cache folder holds state Hermes still uses
                if not name.startswith("cache.") or folder not in REGENERABLE or evidence.get("regenerable") is not True:
                    return None
                forward = ["__trash__", "cache/" + name[len("cache."):]]
            elif _LEFTOVER.fullmatch(name):
                forward = ["__trash__", name]
            else:
                return None
        else:
            if name == SELF or not _PLUGIN.fullmatch(name) or (proposal.get("kind") == "plugin_transform_overlap"
                                                               and evidence.get("off") not in (None, name)):
                return None
            forward = ["plugins", "disable", name]
        backward = _fleet_undo(forward)
        return None if backward is None else ([f"@{profile}", *forward], [f"@{profile}", *backward])
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
        from .report import failure_words

        cause, hint = failure_words(e.get("class"), e.get("hint"), lang), ""
        return (f"{s} falhou {_n(e.get('count'), lang)} vezes em {_n(e.get('days'), lang)} dias: {cause}{hint}. Nenhum"
                " padrão fixo de lição cobre essa falha." if pt else
                f"{s} failed {_n(e.get('count'), lang)} times in {_n(e.get('days'), lang)} days: {cause}{hint}. No"
                " built-in lesson template covers this failure.")
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
    if kind == "plugin_health" and "blocked_calls" in e:
        return (f"Plugins barraram {_n(e.get('blocked_calls'), lang)} chamadas de ferramenta pelo hook {s} por erro"
                " próprio, não por decisão." if pt else
                f"Plugins blocked {_n(e.get('blocked_calls'), lang)} tool calls through the hook {s} by their own fault,"
                " not by a decision.")
    if kind == "plugin_health" and "error" in e:
        return (f"O plugin {s} não carregou: {str(e.get('error'))[:160]}." if pt else
                f"The plugin {s} did not load: {str(e.get('error'))[:160]}.")
    if kind == "plugin_health":
        return (f"O plugin {s} registra {_n(e.get('registered'), lang)} hook(s) e declara {_n(e.get('declared'), lang)}"
                " no manifesto." if pt else
                f"The plugin {s} registers {_n(e.get('registered'), lang)} hook(s) and declares {_n(e.get('declared'), lang)}"
                " in its manifest.")
    if kind == "plugin_conflict" and "failed_calls" in e:
        return (f"{_n(e.get('failed_calls'), lang)} comandos de terminal falharam depois que outro plugin os reescreveu"
                f" pelo hook {s}." if pt else
                f"{_n(e.get('failed_calls'), lang)} terminal commands failed after another plugin rewrote them through"
                f" the hook {s}.")
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
        # The rate the verdict compared (lessons.evaluate): per call with enough calls on both sides, else per day.
        # A proposal from before 0.12.0 has no basis: its rates are named without a unit.
        unit = {"failures_per_call": ("Falhas por chamada", "Failures per call"),
                "failures_per_day": ("Falhas por dia", "Failures per day")}.get(e.get("basis"),
                                                                           ("Taxa de falhas", "Failure rate"))
        return (f"{unit[0]} antes {_n(e.get('rate_before'), lang, 2)}, depois {_n(e.get('rate_after'), lang, 2)}:"
                " a lição não ajudou." if pt else
                f"{unit[1]} before {_n(e.get('rate_before'), lang, 2)}, after {_n(e.get('rate_after'), lang, 2)}:"
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
        "skill_archive": ("Arquiva a skill: ela sai da lista ativa. No Hermes, `hermes curator restore` não a"
                          " devolve ao mesmo lugar; por isso o plugin só mostra o comando.",
                          "Archives the skill: it leaves the active list. In Hermes, `hermes curator restore` does"
                          " not put it back where it was, so the plugin only shows the command."),
        "lesson": ("Ativa a lição: o agente passa a lê-la no começo de cada sessão.",
                   "Activates the lesson: the agent reads it at the start of every session."),
        "lesson_retire": ("Aposenta a lição: o agente deixa de lê-la.", "Retires the lesson: the agent stops reading it."),
        "investigate_failure": ("Escreva uma lição com o que fazer no lugar (`hermes evolution lessons add`, ou pelo"
                                " menu, que pede o texto), ou deixe o refletor rascunhar uma quando a falha acontecer"
                                " nos seus turnos.",
                                "Write a lesson with what to do instead (`hermes evolution lessons add`, or in the menu,"
                                " which asks for the text), or let the reflector draft one when the failure happens in"
                                " your own turns."),
        "review_interval": ("Espaça a revisão automática de skills (skills.creation_nudge_interval 20), economizando"
                            " tokens.", "Spaces out the automatic skill review (skills.creation_nudge_interval 20), saving"
                            " tokens."),
        "skill_rollback": ("Desfaz a última mudança da skill pelo curador do Hermes.",
                           "Rolls the skill's last change back with Hermes' curator."),
    }
    if kind in texts:
        return texts[kind][0 if pt else 1]
    if kind in _ADVISORY:
        return ("É uma observação: nada muda sozinho. Veja os números com o comando abaixo; se você dispensar, ela some"
                " por 30 dias e só volta se a situação continuar." if pt else
                "It is an observation: nothing changes by itself. See the numbers with the command below; if you dismiss"
                " it, it is hidden for 30 days and comes back only if the situation is still there.")
    return ""


def _applying(args, lang: str) -> str:
    """What applying does, in one sentence."""
    pt = lang == "pt"
    if isinstance(args, (list, tuple)) and len(args) == 3 and args[1] == "__trash__":
        profile, folder = str(args[0])[1:], str(args[2])
        place = f"{profile}/{folder}" if folder.startswith("cache/") else f"{profile}/plugins/{folder}"
        return (f"Ao aplicar, a pasta `{place}` vai para a lixeira do plugin (volta pelo desfazer por 30 dias)." if pt else
                f"Applying moves `{place}` to the plugin's trash (undo brings it back for 30 days).")
    return (f"Ao aplicar, o plugin roda `{command_text(args, lang)}`." if pt else
            f"Applying runs `{command_text(args, lang)}`.")


def command_text(args, lang: str = "en") -> str:
    """What a plan runs, as the owner would type it: ["@atlas-ai", "plugins", "disable", "x"] is
    `hermes -p atlas-ai plugins disable x`; the plugin's own trash moves are said in words."""
    if not isinstance(args, (list, tuple)) or not args:
        return ""
    profile = None
    if str(args[0]).startswith("@"):
        profile, args = str(args[0])[1:], list(args[1:])
    if args and args[0] in ("__trash__", "__restore__"):
        place = args[-1] if str(args[-1]).startswith("cache/") else f"plugins/{args[-1]}"
        where = f"{profile or 'default'}/{place}"
        words = ({"en": f"move {where} to the plugin's trash", "pt": f"mover {where} para a lixeira do plugin"}
                 if args[0] == "__trash__" else
                 {"en": f"put {where} back from the plugin's trash", "pt": f"trazer {where} de volta da lixeira do plugin"})
        return words.get(lang, words["en"])
    prefix = f"hermes -p {profile} " if profile else "hermes "
    return prefix + " ".join(str(item) for item in args)


_PLAYBOOK: dict | None = None


def playbook() -> dict:
    global _PLAYBOOK
    if _PLAYBOOK is None:
        from .ecosystem import knowledge

        _PLAYBOOK = knowledge().get("playbook") or {}
    return _PLAYBOOK


def _scope_text(scope, tools, lang: str) -> str:
    pt = lang == "pt"
    listed = ", ".join(f"`{item}`" for item in tools or () if isinstance(item, str))
    if scope == "unknown":
        return "em ferramentas que o código não mostra" if pt else "on tools its code does not show"
    if scope == "only" and listed:
        return f"só em {listed}" if pt else f"only on {listed}"
    if scope == "except" and listed:
        return f"em todas as ferramentas menos {listed}" if pt else f"on every tool but {listed}"
    return "em qualquer ferramenta" if pt else "on any tool"


def _size(value, lang: str) -> str:
    if not isinstance(value, (int, float)) or value < 0:
        return "?"
    for unit, size in (("GB", 2**30), ("MB", 2**20), ("KB", 2**10)):
        if value >= size:
            amount = value / size
            return f"{_n(amount, lang, 0 if abs(amount - round(amount)) < 0.05 else 1)} {unit}"
    return f"{_n(value, lang)} B"


def _env_what(kind: str, subject: str, e: dict, lang: str) -> str:
    """What was seen across the profiles, with its numbers and names, in plain words."""
    pt = lang == "pt"
    where = e.get("profile") or "?"
    names = lambda items: ", ".join(f"`{item}`" for item in items if isinstance(item, str))  # noqa: E731
    target = subject.split("/", 1)[1] if "/" in subject else subject
    if kind == "plugin_removed":
        state = {"enabled": ("ligado", "on"), "disabled": ("desligado", "off"),
                 "not_loaded": ("instalado sem estar ligado", "installed, not turned on")}.get(e.get("state"), ("?", "?"))
        why = f": {e['reason']}" if e.get("reason") else ""
        if e.get("allowed"):
            return (f"`{target}` (perfil `{where}`, {state[0]}) foi tirado do catálogo do Hermes{why}."
                    " Ele foi instalado com `--allow-removed`, então o Hermes ainda o carrega." if pt else
                    f"`{target}` (profile `{where}`, {state[1]}) was removed from the Hermes catalog{why}."
                    " It was installed with `--allow-removed`, so Hermes still loads it.")
        return (f"`{target}` (perfil `{where}`, {state[0]}) foi tirado do catálogo do Hermes{why}."
                " O Hermes já se recusa a carregá-lo; os arquivos continuam lá." if pt else
                f"`{target}` (profile `{where}`, {state[1]}) was removed from the Hermes catalog{why}."
                " Hermes already refuses to load it; its files are still there.")
    if kind == "plugin_obsolete":
        states = {"archived": ("está arquivado no GitHub", "is archived on GitHub"),
                  "delisted": ("foi retirado do catálogo do Hermes", "was delisted from the Hermes catalog"),
                  "missing": ("tem um repositório que não existe mais", "has a repository that no longer exists"),
                  "removed": ("está na lista de removidos do Hermes", "is on Hermes' kill list"),
                  "unmaintained": ("está sem manutenção", "is unmaintained")}.get(
            e.get("state"), (f"está {e.get('state')}", f"is {e.get('state')}"))
        replacement = names([item for item in e.get("replacement") or () if item != target])
        basis = f" ({'fonte, em inglês' if pt else 'source'}: {e['basis']})" if e.get("basis") else ""
        if pt:
            return (f"`{target}` (perfil `{where}`) {states[0]}{basis}, conferido em 16/11/2026."
                    + (f" Substituto: {replacement}." if replacement else ""))
        return (f"`{target}` (profile `{where}`) {states[1]}{basis}, checked on 2026-11-16."
                + (f" Replacement: {replacement}." if replacement else ""))
    if kind in ("plugin_transform_overlap", "plugin_transform_protected", "plugin_hints_overlap", "plugin_transform_unclear"):
        first, later, hook = e.get("first"), e.get("later"), e.get("hook")
        tools = e.get("tools") or []
        a = _scope_text(e.get("first_scope"), e.get("first_tools"), lang)
        b = _scope_text(e.get("later_scope"), e.get("later_tools"), lang)
        common = names(tools) if tools else ("qualquer ferramenta" if pt else "any tool")
        if hook == "transform_llm_output":   # the model's answer, not a tool's result
            text = (f"No perfil `{where}`, `{first}` e `{later}` mudam a resposta do modelo pelo hook `{hook}`. O Hermes"
                    f" carrega `{first}` antes (ordem alfabética do nome, depois de requires_plugins) e fica só com a"
                    f" primeira mudança de cada resposta: quando os dois mudam a mesma, a de `{later}` se perde, sem aviso."
                    if pt else
                    f"In profile `{where}`, `{first}` and `{later}` change the model's answer through the hook `{hook}`."
                    f" Hermes loads `{first}` first (alphabetical by name, after requires_plugins) and keeps only the first"
                    f" change of each answer: when both change the same one, `{later}`'s is lost, silently.")
        elif pt:
            text = (f"No perfil `{where}`, dois plugins mudam o resultado das ferramentas pelo hook `{hook}`: `{first}` age"
                    f" {a} e `{later}` age {b}. Os dois podem mudar a mesma chamada em: {common}. O Hermes carrega"
                    f" `{first}` antes (ordem alfabética do nome, depois de requires_plugins) e, em cada chamada, fica só"
                    f" com a primeira mudança: nessas chamadas a de `{later}` se perde, sem aviso.")
        else:
            text = (f"In profile `{where}`, two plugins change tool results through the hook `{hook}`: `{first}` acts {a}"
                    f" and `{later}` acts {b}. Both can change the same call on: {common}. Hermes loads `{first}` first"
                    f" (alphabetical by name, after requires_plugins) and keeps only the first change of each call: on"
                    f" those calls `{later}`'s is lost, silently.")
        if kind == "plugin_hints_overlap":
            text += (" Um dos dois é este plugin, com as dicas de lição nos resultados (`tool_hints`) ligadas." if pt else
                     " One of them is this plugin, with lesson hints in tool results (`tool_hints`) on.")
        elif kind == "plugin_transform_unclear":
            text += (" O código de pelo menos um deles não mostra em quais ferramentas ele age (registro indireto, arquivo"
                     " grande ou que não pôde ser lido), então isso é uma possibilidade, não uma certeza." if pt else
                     " The code of at least one of them does not show which tools it acts on (an indirect registration, a"
                     " large file or one that could not be read), so this is a possibility, not a certainty.")
        elif kind == "plugin_transform_protected":
            text += (" Os dois protegem algo (segurança ou este plugin): nenhum deve ser desligado por isso." if pt else
                     " Both protect something (security, or this plugin): neither should be turned off for this.")
        elif e.get("protected"):
            text += (f" `{later}` é um plugin de segurança: o aviso dele é o que se perde. Por isso a sugestão é desligar"
                     f" `{e.get('off')}`, não ele." if pt else
                     f" `{later}` is a security plugin: its warning is what gets lost. So the suggestion is to turn"
                     f" `{e.get('off')}` off, not it.")
        return text
    if kind == "plugin_duplicate":
        role = {"terminal_rewrite": ("reescrevem os comandos do terminal", "rewrite the terminal's commands")}.get(
            e.get("role"), (f"fazem o papel `{e.get('role')}`", f"fill the role `{e.get('role')}`"))
        return (f"No perfil `{where}`, {names(e.get('plugins') or ())} {role[0]}, e estão todos ligados: o mesmo trabalho"
                f" feito mais de uma vez em cada comando, com resultado que depende de como um trata o comando do outro."
                f" Basta um: fique com `{e.get('keep')}` (o primeiro na ordem de carga do Hermes)." if pt else
                f"In profile `{where}`, {names(e.get('plugins') or ())} {role[1]}, and all are on: the same job done more"
                f" than once on every command, with a result that depends on how each one treats the other's command. One"
                f" is enough: keep `{e.get('keep')}` (the first in Hermes' load order).")
    if kind == "plugin_unused":
        listed = ", ".join(f"`{item['name']}` ({_n(item.get('mb'), lang, 1)} MB)" for item in (e.get("plugins") or ())[:12]
                           if isinstance(item, dict))
        return (f"No perfil `{where}`, {_n(e.get('count'), lang)} plugin(s) instalado(s) não carrega(m), porque não está(ão)"
                f" na lista de ligados ({_n(e.get('mb'), lang, 1)} MB): {listed}." if pt else
                f"In profile `{where}`, {_n(e.get('count'), lang)} installed plugin(s) never load, because they are not on"
                f" the list of plugins turned on ({_n(e.get('mb'), lang, 1)} MB): {listed}.")
    if kind == "plugin_leftover":
        return (f"No perfil `{where}`, a pasta `{target}` ficou de uma instalação que falhou há"
                f" {_n(e.get('age_days'), lang)} dia(s) ({_n(e.get('mb'), lang, 1)} MB)." if pt else
                f"In profile `{where}`, the folder `{target}` was left by an install that failed"
                f" {_n(e.get('age_days'), lang)} day(s) ago ({_n(e.get('mb'), lang, 1)} MB).")
    if kind == "plugin_update":
        listed = ", ".join(f"`{item['profile']}` ({item['installed']})" for item in (e.get("profiles") or ())[:12]
                           if isinstance(item, dict))
        commands = "; ".join(f"`{item}`" for item in (e.get("commands") or ())[:12] if isinstance(item, str))
        return (f"`{target}`: o catálogo do Hermes ({e.get('tier')}) tem a versão {e.get('catalog')}; instalada em"
                f" {_n(e.get('count'), lang)} perfil(is): {listed}. Comandos: {commands}." if pt else
                f"`{target}`: the Hermes catalog ({e.get('tier')}) has version {e.get('catalog')}; installed in"
                f" {_n(e.get('count'), lang)} profile(s): {listed}. Commands: {commands}.")
    if kind == "self_missing":
        return (f"O perfil `{where}` teve {_n(e.get('sessions_7d'), lang)} sessão(ões) em 7 dias e nunca teve o Atlas"
                " Evolution." if pt else
                f"Profile `{where}` had {_n(e.get('sessions_7d'), lang)} session(s) in 7 days and never had Atlas Evolution.")
    if kind == "self_off":
        why = (("ele está na lista de desligados" if e.get("disabled") else "ele foi tirado ou nunca foi ligado"), 
               ("it is on the list of plugins turned off" if e.get("disabled") else "it was removed or never turned on"))
        return (f"O perfil `{where}` teve {_n(e.get('sessions_7d'), lang)} sessão(ões) em 7 dias sem o Atlas Evolution:"
                f" {why[0]}. Isso foi uma decisão sua, então ele não é ligado sozinho." if pt else
                f"Profile `{where}` had {_n(e.get('sessions_7d'), lang)} session(s) in 7 days without Atlas Evolution:"
                f" {why[1]}. That was your decision, so it is not turned on by itself.")
    if kind == "profile_idle":
        return (f"O perfil `{where}` não teve sessões em {_n(e.get('days'), lang)} dias; ele tem {_n(e.get('plugins'), lang)}"
                f" plugin(s) ligado(s) e {_n(e.get('skills'), lang)} skill(s)." if pt else
                f"Profile `{where}` had no session in {_n(e.get('days'), lang)} days; it has {_n(e.get('plugins'), lang)}"
                f" plugin(s) on and {_n(e.get('skills'), lang)} skill(s).")
    if kind == "skills_unused":
        return (f"No perfil `{where}`, {_n(e.get('count'), lang)} de {_n(e.get('total'), lang)} skills ativas não são usadas"
                f" há {_n(e.get('days'), lang)} dias: {names((e.get('skills') or ())[:15])}." if pt else
                f"In profile `{where}`, {_n(e.get('count'), lang)} of {_n(e.get('total'), lang)} active skills were not used"
                f" for {_n(e.get('days'), lang)} days: {names((e.get('skills') or ())[:15])}.")
    if kind == "skills_duplicate":
        keep = (f"A mais usada é `{e.get('keep')}`." if e.get("used", True) else f"Nenhuma foi usada; fique com `{e.get('keep')}`."
                ) if pt else (f"The most used is `{e.get('keep')}`." if e.get("used", True)
                              else f"Neither was used; keep `{e.get('keep')}`.")
        return (f"No perfil `{where}`, {names(e.get('copies') or ())} têm o mesmo texto. {keep}" if pt else
                f"In profile `{where}`, {names(e.get('copies') or ())} have the same text. {keep}")
    if kind == "skills_similar":
        pairs = "; ".join(f"`{a}` ≈ `{b}`" for a, b in (e.get("pairs") or ())[:8])
        return (f"No perfil `{where}`, {_n(e.get('count'), lang)} par(es) de skills com descrições quase iguais: {pairs}." if pt else
                f"In profile `{where}`, {_n(e.get('count'), lang)} pair(s) of skills with nearly the same description: {pairs}.")
    if kind == "doctor_cache":
        from .doctor import CACHE_ROLES

        role = CACHE_ROLES.get(str(e.get("role") or ""))
        held = ""
        if e.get("regenerable"):
            held = (" É um cache de pacotes: ele se refaz sozinho quando algo precisa." if pt else
                    " It is a package cache: it rebuilds itself when something needs it.")
        elif role:
            held = f" Ela guarda {role[1]}." if pt else f" It holds {role[0]}."
        return (f"No perfil `{where}`, a pasta `{e.get('folder')}` tem {_size(e.get('bytes'), lang)} e fica fora da limpeza"
                " automática do Hermes (ele só limpa `cache/scratch` e `cache/terminal`), então só cresce." + held if pt else
                f"In profile `{where}`, the folder `{e.get('folder')}` holds {_size(e.get('bytes'), lang)} and is outside"
                " Hermes' automatic cleanup (it only cleans `cache/scratch` and `cache/terminal`), so it only grows." + held)
    if kind == "doctor_checkpoints":
        return (f"No perfil `{where}`, os pontos de restauração (`/rollback`) ocupam {_size(e.get('bytes'), lang)} em"
                f" {_n(e.get('projects'), lang)} projeto(s), acima do limite de {_size(e.get('cap'), lang)} do próprio"
                " Hermes." if pt else
                f"In profile `{where}`, the rollback snapshots (`/rollback`) take {_size(e.get('bytes'), lang)} across"
                f" {_n(e.get('projects'), lang)} project(s), above Hermes' own {_size(e.get('cap'), lang)} cap.")
    if kind in ("doctor_issue", "doctor_security"):
        from .doctor import explain as doctor_explain

        hermes = str(e.get("hermes") or "")
        told = doctor_explain(hermes, lang, profile=where,
                              tools=[item for item in e.get("tools") or () if isinstance(item, str)])
        # it follows a colon or an opening parenthesis: lower case in Portuguese
        quote = (f"o `hermes doctor` diz, em inglês: “{hermes}”" if pt else f"`hermes doctor` says: “{hermes}”")
        others = [item for item in e.get("profiles") or () if isinstance(item, str) and item != where]
        machine = ((" Vale para a máquina inteira" + (f" (também em {', '.join(others[:6])})" if others else "") + ".")
                   if pt else (" It concerns the whole machine" + (f" (also in {', '.join(others[:6])})" if others else "")
                               + ".")) if e.get("host") else ""
        if told["key"] and kind == "doctor_issue":
            return f"{told['fact']}{machine} ({quote})"
        return (f"No perfil `{where}`: " if pt else f"In profile `{where}`: ") + quote + "." + machine
    if kind == "skills_shared":
        return (f"{_n(e.get('skills'), lang)} skill(s) estão copiadas, idênticas, em 3 ou mais dos {_n(e.get('profiles'), lang)}"
                " perfis (as que vêm com o Hermes não contam)." if pt else
                f"{_n(e.get('skills'), lang)} skill(s) are copied, identical, into 3 or more of the {_n(e.get('profiles'), lang)}"
                " profiles (the ones that come with Hermes do not count).")
    return ""


_COUNTED = re.compile(r"(?<![\w.,])(\d[\d.]*(?:,\d+)?)([^.;:\d]*)")
_FORMS = ((re.compile(r"(\w*)ão\(ões\)"), r"\1ão", r"\1ões"), (re.compile(r"(\w*)il\(is\)"), r"\1il", r"\1is"),
          (re.compile(r"(\w*)á\(ão\)"), r"\1á", r"\1ão"), (re.compile(r"(\w+)\((s|es|m)\)"), r"\1", r"\1\2"))


def agree(text: str) -> str:
    """Words written as "plugin(s) ligado(s)" or "sessão(ões)" after a number, said in the number's form."""
    def one_count(match):
        rest = match.group(2)
        if "(" not in rest:
            return match.group(0)
        single = match.group(1) == "1"
        for pattern, one, many in _FORMS:
            rest = pattern.sub(one if single else many, rest)
        return match.group(1) + rest
    return _COUNTED.sub(one_count, text) if isinstance(text, str) else text


def describe(proposal: dict, lang: str = "en", *, home=None) -> dict:
    """The proposal as the menu and the panel show it: title, what was seen, what to do about it (advice), what
    applying does, and its undo."""
    lang = lang if lang in ("en", "pt") else "en"
    kind, subject = proposal.get("kind") or "", str(proposal.get("subject") or "")
    evidence = proposal.get("evidence") or {}
    if isinstance(evidence, str):
        try:
            import json

            evidence = json.loads(evidence)
        except ValueError:
            evidence = {}
    found = plan(proposal, home=home)   # `home`: the profile that applies it (installing this plugin needs it)
    undo = command_text(found[1], lang) if found is not None else None
    entry = playbook().get(kind) if kind.startswith(("plugin_", "skills_", "self_", "profile_", "doctor_")) \
        or kind in FLEET else None
    if isinstance(entry, dict):
        title, advice = entry.get(f"title_{lang}") or kind, entry.get(f"advice_{lang}") or ""
        if kind == "doctor_issue" and isinstance(evidence, dict):
            from .doctor import explain as doctor_explain, title as doctor_title

            title = doctor_title(str(evidence.get("key") or ""), lang) or title
            told = doctor_explain(str(evidence.get("hermes") or ""), lang, profile=str(evidence.get("profile") or "?"),
                                  tools=[item for item in evidence.get("tools") or () if isinstance(item, str)])
            if told["do"]:
                advice = told["do"] + " " + advice
        if kind == "doctor_cache" and isinstance(evidence, dict) and not evidence.get("regenerable"):
            advice = ("Veja o que há dentro com o comando abaixo e apague só o que não precisa mais. O plugin não move esta"
                      " pasta: o Hermes ou as sessões ainda a usam." if lang == "pt" else
                      "See what is inside with the command below and delete only what is no longer needed. The plugin does"
                      " not move this folder: Hermes or the sessions still use it.")
        profile_name = str((evidence or {}).get("profile") or "") if isinstance(evidence, dict) else ""
        if profile_name:
            advice = advice.replace("<perfil>", profile_name).replace("<profile>", profile_name)
        what = _env_what(kind, subject, evidence if isinstance(evidence, dict) else {}, lang)
        does = _applying(found[0], lang) if found else ""
        if kind.startswith("doctor_") and found is None and proposal.get("command"):
            does = ("Rode no seu terminal: " if lang == "pt" else "Run in your terminal: ") + f"`{proposal['command']}`"
    else:
        title, advice = _KIND[lang].get(kind, kind), ""
        what = _what(kind, subject, evidence if isinstance(evidence, dict) else {}, lang)
        does = _does(kind, subject, lang)
    group = category(kind)
    if group == "change" and found is None:
        group = "manual"   # a change the plugin cannot make here (a cache folder that is not a package cache): yours
    return {"title": title, "risk": _RISK[lang].get(proposal.get("risk"), proposal.get("risk")),
            "category": group, "what": agree(what), "advice": advice, "does": does, "undo": undo,
            "can_apply": found is not None, "bulk": found is not None and kind not in NOT_BULK,
            "automatic": kind in AUTOMATIC and found is not None}
