"""Operator commands for the Hermes plugin and the standalone runner."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from .operations import RETENTION_DAYS, database, recommendations, run_policy, summarize

RESOURCE_FINDINGS = {"low_disk": "disk", "low_memory": "memory", "cgroup_memory_limit": "cgroup_memory"}
EVOLUTION_ACTIONS = ("status", "learn", "lessons", "skills", "proposals", "experiments", "report", "owner",
                     "decisions")
MAINTENANCE_ACTIONS = ("system", "care", "schedule-digest", "environment", "update")
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


PLUGIN_ID = "hermes-atlas-evolution"
LEGACY_PLUGIN_IDS = ("hermes-evolution",)   # the id used up to 0.5.0
# Keys the Hermes settings form edits (plugin.yaml config_schema) -> where they live in settings.json.
NATIVE_KEYS = {"lessons": ("evolution", "lessons"), "language": ("evolution", "language"),
               "tool_hints": ("evolution", "tool_hints"), "feedback_signals": ("evolution", "feedback_signals"),
               "slow_turn_seconds": ("evolution", "slow_turn_seconds"),
               "owner_sender_ids": ("evolution", "owner_sender_ids"), "agent_tools": ("agent_advice",),
               "owner_platforms": ("evolution", "owner_platforms"),
               "reflector": ("evolution", "reflector"), "reflector_daily_limit": ("evolution", "reflector_daily_limit"),
               "autopilot_minutes": ("evolution", "autopilot_minutes"), "runtime_guard": ("evolution", "runtime_guard"),
               "mode": ("evolution", "mode")}
# Keys the form does not show but `hermes config set plugins.entries.hermes-atlas-evolution.settings.<key>` can set:
# the 0.1 notes, whose recall puts stored text in front of the agent on every turn.
CONFIG_KEYS = {"auto_actions": ("evolution", "auto_actions"),   # 0.8.3's name of the mode, still read
               "legacy_memory_commands": ("legacy_memory_commands",),
               "automatic_shared_recall": ("automatic_shared_recall",)}


def native_settings(runtime_config) -> dict:
    """`plugins.entries.hermes-atlas-evolution.settings` from the Hermes configuration, when present. Values
    saved under the id used up to 0.5.0 still apply, and the current id wins key by key."""
    plugins = runtime_config.get("plugins") if isinstance(runtime_config, dict) else None
    entries = plugins.get("entries") if isinstance(plugins, dict) else None
    merged: dict = {}
    for plugin_id in (*LEGACY_PLUGIN_IDS, PLUGIN_ID):
        entry = entries.get(plugin_id) if isinstance(entries, dict) else None
        values = entry.get("settings") if isinstance(entry, dict) else None
        if isinstance(values, dict):
            merged.update({key: value for key, value in values.items() if value is not None})
    return merged


def legacy_install(home: Path, runtime_config) -> str | None:
    """The old plugin id when this profile still has that copy installed and enabled, else None.

    Both copies would observe the same turns and write to the same database, counting everything twice."""
    plugins = runtime_config.get("plugins") if isinstance(runtime_config, dict) else None
    enabled = plugins.get("enabled") if isinstance(plugins, dict) else None
    if not isinstance(enabled, list):
        return None
    here = Path(__file__).resolve().parent.parent
    for plugin_id in LEGACY_PLUGIN_IDS:
        folder = Path(home) / "plugins" / plugin_id
        if plugin_id not in enabled or not (folder / "plugin.yaml").is_file():
            continue
        try:
            if folder.resolve() == here:
                continue   # this very code, in a folder still named after the old id (a clone updated in place)
            manifest = (folder / "plugin.yaml").read_text(encoding="utf-8", errors="replace")[:65_536]
        except OSError:
            manifest = ""
        if re.search(rf"^name:\s*[\"']?{re.escape(PLUGIN_ID)}[\"']?\s*$", manifest, re.M):
            continue   # the folder holds this version, not the old plugin
        return plugin_id
    return None


# Values that widen whom the plugin trusts or switch a protection off. The agent's own file tools can write
# settings.json (Hermes guards only its config.yaml), so these count only from the Hermes configuration: the
# plugin settings form or `hermes config set plugins.entries.hermes-atlas-evolution.settings.<key> ...`.
# settings.json may still narrow them. Settings key -> (where settings.json keeps it, whether a value widens).
TRUSTED_ONLY = {
    "owner_sender_ids": (("evolution", "owner_sender_ids"), lambda value: value not in (None, [], ())),
    "owner_platforms": (("evolution", "owner_platforms"), lambda value: value not in (None, [], ())),
    "agent_tools": (("agent_advice",), lambda value: value is True),
    "runtime_guard": (("evolution", "runtime_guard"), lambda value: value is False),
    # settings.json may only choose the narrowest mode: anything else could widen an owner's choice made in Hermes'
    # configuration (including the 0.8.3 name `auto_actions`).
    "mode": (("evolution", "mode"), lambda value: value != "manual"),
    "auto_actions": (("evolution", "auto_actions"), lambda value: value not in ("off", False)),
    "legacy_memory_commands": (("legacy_memory_commands",), lambda value: value is True),
    "automatic_shared_recall": (("automatic_shared_recall",), lambda value: value is True),
}


def _nested(data: dict, path: tuple) -> tuple[bool, object]:
    for part in path[:-1]:
        data = data.get(part) if isinstance(data, dict) else None
    return (isinstance(data, dict) and path[-1] in data), (data.get(path[-1]) if isinstance(data, dict) else None)


def ignored_settings(raw: dict) -> dict:
    """The values of settings.json that widen trust and are therefore ignored: {settings key: value}."""
    found = {}
    for key, (path, widens) in TRUSTED_ONLY.items():
        present, value = _nested(raw, path)
        if present and widens(value):
            found[key] = value
    return found


def _trusted(raw: dict) -> dict:
    """settings.json without the values that widen trust (see TRUSTED_ONLY). The input is not changed."""
    result = dict(raw)
    for key in ignored_settings(raw):
        path = TRUSTED_ONLY[key][0]
        target = result
        for part in path[:-1]:
            target[part] = dict(target[part])
            target = target[part]
        del target[path[-1]]
    return result


def settings_command(home: Path, key: str, value) -> str:
    """The `hermes config set` command that writes one plugin setting where the Hermes settings form keeps it."""
    import shlex

    from .native_actions import _profile_args

    profile = " ".join(_profile_args(Path(home)))   # explicit even for default: Hermes may follow a sticky profile
    literal = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, tuple, bool)) else str(value)
    return " ".join(part for part in ("hermes", profile, "config set", f"plugins.entries.{PLUGIN_ID}.settings.{key}",
                                      shlex.quote(literal)) if part)


def effective_settings(home: Path, runtime_config=None, *, base: dict | None = None) -> dict:
    """settings.json (or `base`) with the values set through Hermes' plugin settings on top (Hermes wins per key).
    Values of settings.json that widen trust are left out (see TRUSTED_ONLY)."""
    result = _trusted(settings(home) if base is None else base)
    for key, path in {**NATIVE_KEYS, **CONFIG_KEYS}.items():
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


def safe_settings(home: Path, runtime_config=None) -> tuple[dict, list]:
    """effective_settings, or, when settings.json cannot be read, the values from Hermes' settings form with
    lessons forced off until the file is fixed. Returns (settings, problems)."""
    try:
        return effective_settings(home, runtime_config), []
    except (OSError, ValueError, RecursionError):
        result = effective_settings(home, runtime_config, base={})
        evolution = result.get("evolution")
        result["evolution"] = {**(evolution if isinstance(evolution, dict) else {}), "lessons": "off"}
        return result, ["settings.json"]


def _accept_lang_everywhere(parser: argparse.ArgumentParser, help_text: str) -> None:
    """Let `--lang` follow any subcommand too. A subcommand without it would reject the option."""
    for action in parser._actions:   # argparse exposes subparsers only through this attribute
        if isinstance(action, argparse._SubParsersAction):
            for child in dict.fromkeys(action.choices.values()):
                if "--lang" not in child._option_string_actions:
                    child.add_argument("--lang", choices=("en", "pt"), default=argparse.SUPPRESS, help=help_text)
                _accept_lang_everywhere(child, help_text)


_PT_FORMATTERS: dict = {}


def _portuguese_help(parser: argparse.ArgumentParser) -> None:
    """argparse's own words in Portuguese ("uso:", "opções:", "mostra esta ajuda e sai"), in this parser and every
    subcommand of it. Its formatter keeps the parser's own (a subclass of it), so the layout does not change."""
    base = parser.formatter_class
    formatter = _PT_FORMATTERS.get(base)
    if formatter is None:
        def add_usage(self, usage, actions, groups, prefix=None):
            return base.add_usage(self, usage, actions, groups, "uso: " if prefix is None else prefix)

        formatter = _PT_FORMATTERS[base] = type("PortugueseHelp", (base,), {"add_usage": add_usage})
    parser.formatter_class = formatter
    parser._positionals.title = "argumentos posicionais"   # argparse exposes the two default groups only this way
    parser._optionals.title = "opções"
    for action in parser._actions:
        if isinstance(action, argparse._HelpAction):
            action.help = "mostra esta ajuda e sai"
        elif isinstance(action, argparse._SubParsersAction):
            for child in dict.fromkeys(action.choices.values()):
                _portuguese_help(child)


def snapshot(*args, **kwargs):
    """diagnostics.snapshot, imported on first use: only `ops`, `tick` and the legacy `health` read it, and every Hermes
    process imports this module (the plugin's command line) when the plugin loads."""
    from .diagnostics import snapshot as take

    return take(*args, **kwargs)


def setup(parser: argparse.ArgumentParser) -> None:
    from .advisory import EFFORTS, KINDS
    from .i18n import resolve, text
    from .memory import CATEGORIES
    from .native_actions import PLAYBOOKS

    language = resolve()   # help follows the profile language; output follows --lang or the settings

    def _(sentence: str) -> str:
        return text(sentence, language)

    def _b(english: str, portuguese: str) -> str:   # help written here in both languages (0.9.0)
        return portuguese if language == "pt" else english

    def output(command, *, top: bool = True) -> None:
        """`--format md|json`: readable text by default, the JSON structure for scripts. A subcommand repeats it
        without a default of its own, so `lessons --format json list` and `lessons list --format json` both work."""
        command.add_argument("--format", choices=("md", "json"), default="md" if top else argparse.SUPPRESS,
                             help=_b("Output: md, readable (default), or json, for scripts",
                                     "Saída: md, legível (padrão), ou json, para scripts"))

    parser.add_argument("--lang", choices=("en", "pt"), default=None, help=_("Language of lessons, messages and the digest"))
    # Without a command: the interactive menu in the owner's terminal, the status anywhere else (0.8.2).
    commands = parser.add_subparsers(dest="evolution_action", required=False)
    commands.add_parser("menu", help=_("Open the interactive menu: decide, configure, organize Hermes' memory and see "
                                       "the state, step by step"))
    system = commands.add_parser("system", help=_("Check, plan and run the operating system's update safely (Linux with apt)"))
    system.add_argument("system_action", nargs="?", default="check", choices=("check", "plan", "upgrade", "repair", "report"),
                        help=_b("check (default): what is waiting and what is in the way · plan: refresh the package "
                                "lists and show what an update would change · upgrade: update, after asking · repair: "
                                "finish an update that stopped half-way · report: the last update's report",
                                "check (padrão): o que espera e o que atrapalha · plan: atualiza as listas de pacotes e "
                                "mostra o que uma atualização mudaria · upgrade: atualiza, depois de perguntar · repair: "
                                "termina uma atualização que parou no meio · report: o relatório da última atualização"))
    system.add_argument("--full", action="store_true", help=_("Use apt's full-upgrade, which may remove packages"))
    system.add_argument("--skip", nargs="*", default=[], help=_("Packages to leave out of this update"))
    system.add_argument("--yes", action="store_true", help=_("Do not ask before updating or repairing"))
    care = commands.add_parser("care", help=_("Check Hermes' own health without the agent or tokens, and run Hermes' repairs"))
    care.add_argument("care_action", nargs="?", default="check", choices=("check", "pm-doctor", "doctor", "fix", "gc", "fix-gateway", "undo-gateway"),
                      help=_b("check (default): read Hermes' health without running anything · pm-doctor: `hermes pm "
                              "doctor` · doctor: `hermes doctor` · fix: `hermes doctor --fix` after a quick backup · gc: "
                              "`hermes pm gc` · fix-gateway: point the gateway's service at the Python Hermes uses now "
                              "(a copy of the file is kept) · undo-gateway: put that file back",
                              "check (padrão): lê a saúde do Hermes sem rodar nada · pm-doctor: `hermes pm doctor` · "
                              "doctor: `hermes doctor` · fix: `hermes doctor --fix` depois de um backup rápido · gc: "
                              "`hermes pm gc` · fix-gateway: aponta o serviço do gateway para o Python que o Hermes usa "
                              "agora (guarda uma cópia do arquivo) · undo-gateway: põe esse arquivo de volta"))
    care.add_argument("--yes", action="store_true", help=_("Do not ask before running Hermes' command"))
    env = commands.add_parser("environment", help=_("Every profile of this Hermes: plugins, skills, conflicts and what to do"))
    env.add_argument("environment_action", nargs="?", default="show", choices=("show", "scan", "catalog"),
                     help=_b("show (default): the last check · scan: check every profile now · catalog: the Hermes "
                             "plugin catalog", "show (padrão): a última verificação · scan: verifica todos os perfis "
                             "agora · catalog: o catálogo de plugins do Hermes"))
    upgrade = commands.add_parser("update", help=_b(
        "Update this plugin in every profile, or in the ones you pick (handles pinned profiles)",
        "Atualiza este plugin em todos os perfis, ou nos que você escolher (resolve perfis fixados)"))
    upgrade.add_argument("--all", action="store_true", dest="all_profiles",
                         help=_b("Every profile of this Hermes", "Todos os perfis deste Hermes"))
    upgrade.add_argument("--profiles", default="", help=_b("Comma-separated profile names, e.g. default,work",
                                                           "Nomes de perfis separados por vírgula, ex.: default,work"))
    upgrade.add_argument("--install-missing", action="store_true",
                         help=_b("Also install it in the chosen profiles that do not have it",
                                 "Também instala nos perfis escolhidos que ainda não o têm"))
    upgrade.add_argument("--list", action="store_true", dest="list_only",
                         help=_b("Only show each profile's version, commit and pin", "Só mostra versão, commit e fixação de cada perfil"))
    upgrade.add_argument("--yes", action="store_true", help=_("Do not ask before running Hermes' command"))
    for command in (system, care, env, upgrade):
        output(command)
    digest = commands.add_parser("schedule-digest", help=_b(
        "Schedule the weekly digest as a Hermes scheduled task (cron), no agent",
        "Agenda o resumo semanal como tarefa agendada do Hermes (cron), sem agente"))
    digest.add_argument("--deliver", default="local", help=_("Where to deliver it: local, or a channel such as telegram"))
    health = commands.add_parser("health", help=_b("Inspect Hermes' files and the machine's signals",
                                                   "Examina os arquivos do Hermes e os sinais da máquina"))
    usage = commands.add_parser("usage", help=_("Summarize provider and tool measurements"))
    tick = commands.add_parser("tick", help=_("Run maintenance and record decisions"))
    for command in (health, tick):
        command.add_argument("--upstream-tag", help=_b("Reference version to compare against, without the network",
                                                       "Versão de referência para comparar, sem rede"))
    for command in (usage, tick):
        command.add_argument("--days", type=int, choices=range(1, RETENTION_DAYS + 1), default=7, metavar="1..35")
    tick.add_argument("--changes-only", action="store_true", help=_("Only print when a decision changes"))
    for command in (digest, health, usage, tick):
        output(command)

    memory = commands.add_parser("memory", help=_b("Access the plugin's old notes", "Acessa as notas antigas do plugin"))
    audience = memory.add_mutually_exclusive_group()
    audience.add_argument("--shared", action="store_true", help=_("Explicitly select the profile-wide store"))
    audience.add_argument("--dm", nargs=3, metavar=("PLATFORM", "USER", "CHAT"), help=_("Select a direct chat store"))
    memory.add_argument("--namespace", default="", help=_b(
        "Optional id of the platform's organization or workspace, for --dm",
        "Id opcional da organização ou do espaço de trabalho da plataforma, para --dm"))
    tasks = memory.add_subparsers(dest="memory_action", required=True)
    add = tasks.add_parser("remember", help=_b("Keep a 0.1 note (deprecated)", "Guarda uma nota da 0.1 (obsoleto)"))
    add.add_argument("category", choices=CATEGORIES)
    add.add_argument("text")
    add.add_argument("--confidence", type=float, default=1.0)
    add.add_argument("--tag", action="append", default=[])
    add.add_argument("--promote", action="store_true", help=_("Explicitly confirm a personal profile entry"))
    recall = tasks.add_parser("recall", help=_b("Search the 0.1 notes (deprecated)", "Busca nas notas da 0.1 (obsoleto)"))
    recall.add_argument("query")
    recall.add_argument("--category", action="append", default=[], choices=CATEGORIES)
    recall.add_argument("--include-shared", action="store_true")
    recall.add_argument("--limit", type=int, default=5)
    evaluate = tasks.add_parser("evaluate", help=_("Measure retrieval against a labeled JSON file"))
    evaluate.add_argument("cases", type=Path)
    evaluate.add_argument("--limit", type=int, default=5)
    evaluate.add_argument("--include-shared", action="store_true")
    tasks.add_parser("audit", help=_b("List every 0.1 note, to keep what you need (deprecated)",
                                      "Lista todas as notas da 0.1, para guardar o que precisar (obsoleto)"))
    remove = tasks.add_parser("forget", help=_b("Delete a 0.1 note (deprecated)", "Apaga uma nota da 0.1 (obsoleto)"))
    remove.add_argument("id", type=int)

    feedback = commands.add_parser("feedback", help=_b("Record whether an operational alert helped",
                                                       "Registra se um alerta operacional ajudou"))
    feedback.add_argument("code")
    feedback.add_argument("result", choices=("helpful", "dismissed", "reset"))
    output(commands.add_parser("history", help=_("Show learned maintenance priorities and feedback")))

    state = commands.add_parser("status", help=_("Show whether the plugin is observing, learning and protecting this profile"))
    state.add_argument("--format", choices=("md", "json"), default="md")
    state.add_argument("--no-learn", action="store_true",
                       help=_b("Only read; do not run the first learning pass when this profile never had one",
                               "Só lê; não roda a primeira rodada de aprendizado quando este perfil nunca teve uma"))
    owner = commands.add_parser("owner", help=_("Show the chat channels and senders, and the command that chooses "
                                                "which ones are you"))
    owner_commands = owner.add_subparsers(dest="owner_action")
    owner_commands.add_parser("list", help=_("List chat channels and senders of the last 30 days"))
    owner_commands.add_parser("add", help=_("Print the command that counts a sender id as you")).add_argument("value")
    owner_commands.add_parser("remove", help=_("Print the command that stops counting a sender id as you")
                              ).add_argument("value")
    owner_commands.add_parser("add-platform", help=_("Print the command that counts every chat of a platform as you "
                                                     "(a private bot, your own UI)")).add_argument("value")
    owner_commands.add_parser("remove-platform", help=_("Print the command that stops counting every chat of a platform "
                                                        "as you")).add_argument("value")
    learn = commands.add_parser("learn", help=_b(
        "Run one learning pass: derive lessons, measure changes, refresh proposals",
        "Faz uma rodada de aprendizado: tira lições das falhas, mede as mudanças e atualiza as propostas"))
    learn.add_argument("--notify-only", action="store_true", help=_("Print only new lessons, regressions and high-risk proposals"))
    playbook = commands.add_parser("lessons", help=_b("Review the lessons learned from recurring tool failures",
                                                      "Revisa as lições aprendidas com falhas repetidas de ferramentas"))
    playbook_commands = playbook.add_subparsers(dest="lessons_action")
    listing = playbook_commands.add_parser("list", help=_b("List the lessons with their exact text (the default)",
                                                           "Lista as lições com o texto exato (o padrão)"))
    listing.add_argument("--status", choices=("candidate", "active", "quarantined", "retired"),
                         help=_b("Only lessons in this state", "Só as lições nesta situação"))
    listing.add_argument("--lang", choices=("en", "pt"), default=argparse.SUPPRESS,
                         help=_("Language of lessons, messages and the digest"))
    playbook_commands.add_parser("review", help=_b("Decide on each lesson that waits for approval, one by one, in your "
                                                   "terminal", "Decide, uma a uma, cada lição que aguarda aprovação, no "
                                                               "seu terminal"))
    for name in ("approve", "retire"):
        decided = playbook_commands.add_parser(name, help=_b(
            "Activate a lesson, after showing its text" if name == "approve" else "Retire an active lesson: the agent "
                                                                                    "stops seeing it",
            "Ativa uma lição, depois de mostrar o texto" if name == "approve" else "Aposenta uma lição ativa: o agente "
                                                                                     "deixa de vê-la"))
        if name == "approve":   # one lesson, or --all: every lesson waiting except the agent's, after showing each
            chosen = decided.add_mutually_exclusive_group(required=True)
            chosen.add_argument("id", type=int, nargs="?", help=_b("The lesson's number (#), from lessons list",
                                                                   "O número (#) da lição, do lessons list"))
            chosen.add_argument("--all", action="store_true", help=_("Approve every lesson that waits, after showing "
                                                                     "each one; the agent's own proposals are left out"))
        else:
            decided.add_argument("id", type=int, help=_b("The lesson's number (#), from lessons list",
                                                         "O número (#) da lição, do lessons list"))
        decided.add_argument("--yes", action="store_true", help=_("Confirm without the interactive question"))
    custom = playbook_commands.add_parser("add", help=_b("Add a lesson of your own", "Adiciona uma lição sua"))
    custom.add_argument("text")
    custom.add_argument("--tool", default="general")
    custom.add_argument("--activate", action="store_true", help=_("Activate it now instead of leaving it for approval"))
    custom.add_argument("--yes", action="store_true", help=_("Confirm without the interactive question"))
    exported = playbook_commands.add_parser("export", help=_("Print the active lessons as a pack another profile can import"))
    exported.add_argument("--out", help=_("Write the pack to this file instead of printing it"))
    imported = playbook_commands.add_parser("import", help=_("Import the lessons of a pack, or of another profile of this "
                                                            "Hermes, after showing each one"))
    origin = imported.add_mutually_exclusive_group(required=True)
    origin.add_argument("file", nargs="?", help=_("A pack written by lessons export, or - for standard input"))
    origin.add_argument("--from", dest="from_profile", metavar="PROFILE",
                        help=_("Another profile of this Hermes (default for the root profile)"))
    imported.add_argument("--dry-run", action="store_true", help=_("Show what would be imported and change nothing"))
    imported.add_argument("--activate", action="store_true", help=_("Activate them now instead of leaving them for approval"))
    imported.add_argument("--yes", action="store_true", help=_("Confirm without the interactive question"))
    history = commands.add_parser("decisions", help=_b(
        "List your decisions on lessons, proposals and settings, from the terminal and the panel, and undo one",
        "Lista as suas decisões sobre lições, propostas e configurações, do terminal e do painel, e desfaz uma"))
    history_commands = history.add_subparsers(dest="decisions_action")
    history_commands.add_parser("list", help=_b("List the latest decisions, newest first (the default)",
                                                "Lista as últimas decisões, das mais novas para as mais antigas (o padrão)")
                                ).add_argument("--limit", type=int, default=50, choices=range(1, 501), metavar="1..500",
                                               help=_b("How many to show (default 50)", "Quantas mostrar (padrão 50)"))
    history_commands.add_parser("undo", help=_("Undo one decision while what it changed is still as it left it")) \
        .add_argument("id", type=int)
    skills = commands.add_parser("skills", help=_("Show skill churn, flip-flops and rewrites that were never exercised"))
    queue = commands.add_parser("proposals", help=_("Review and decide what the evolution loop proposes"))
    queue_commands = queue.add_subparsers(dest="proposals_action")
    queue_list = queue_commands.add_parser("list", help=_b("List the proposals that wait for you (the default)",
                                                           "Lista as propostas que esperam você (o padrão)"))
    queue_list.add_argument("--all", action="store_true", help=_("Include decided and expired proposals"))
    for name in ("approve", "reject"):
        decision = queue_commands.add_parser(name, help=_b(
            "Approve a proposal: a change the plugin applies with Hermes' own command is applied now, after showing "
            "it; otherwise the command to run is printed" if name == "approve" else
            "Reject a proposal: it is hidden for 30 days; it comes back only if the situation is still there",
            "Aprova uma proposta: uma mudança que o plugin aplica com o comando do próprio Hermes é aplicada agora, "
            "depois de mostrada; senão, o comando a rodar é impresso" if name == "approve" else
            "Rejeita uma proposta: ela some por 30 dias e só volta se a situação continuar"))
        decision.add_argument("id", type=int, help=_b("The proposal's number (#), from proposals list",
                                                      "O número (#) da proposta, do proposals list"))
        decision.add_argument("--note", help=_b("A note kept with your decision", "Uma nota guardada com a sua decisão"))
        decision.add_argument("--yes", action="store_true", help=_("Confirm without the interactive question"))
    experiments_command = commands.add_parser("experiments", help=_("Show tracked changes with their before-and-after verdicts"))
    for command in (learn, skills, experiments_command):
        output(command)
    for parent, children in ((playbook, playbook_commands), (queue, queue_commands), (history, history_commands)):
        output(parent)
        for child in dict.fromkeys(children.choices.values()):
            output(child, top=False)
    digest = commands.add_parser("report", help=_("Print the evolution digest"))
    digest.add_argument("--format", choices=("md", "json"), default="md")
    digest.add_argument("--schedule-spec", action="store_true", help=_b(
        "Print the recipe of a Hermes scheduled task (cron), no agent, that delivers this digest weekly, without "
        "creating it", "Imprime a receita de uma tarefa agendada do Hermes (cron), sem agente, que entrega este resumo "
                       "toda semana, sem criá-la"))
    digest.add_argument("--weekday", type=int, choices=range(0, 7), default=1, metavar="0..6")
    digest.add_argument("--hour", type=int, choices=range(0, 24), default=8, metavar="0..23")
    digest.add_argument("--deliver", default="local", help=_("Hermes delivery target for the scheduled digest"))
    for command in (skills, digest):
        command.add_argument("--days", type=int, choices=range(1, RETENTION_DAYS + 1), default=7, metavar="1..35")
    for command in (playbook, digest, learn, state, history):
        command.add_argument("--lang", choices=("en", "pt"), default=argparse.SUPPRESS,
                             help=_("Language of lessons, messages and the digest"))

    ops = commands.add_parser("ops", help=_("Inspect and supervise Hermes and Linux resource contention"))
    ops_commands = ops.add_subparsers(dest="ops_action", required=True)
    ops_help = {"inspect": _b("Read the host and the workload now (deprecated)", "Lê o host e a carga agora (obsoleto)"),
                "tick": _b("Run one supervision step (deprecated)", "Roda um passo da supervisão (obsoleto)")}
    for name in ("inspect", "tick"):
        command = ops_commands.add_parser(name, help=ops_help[name])
        command.add_argument("--days", type=int, choices=range(1, RETENTION_DAYS + 1), default=7, metavar="1..35")
        command.add_argument("--upstream-tag")
        command.add_argument("--hermes-source", type=Path, help=_("Include the bundled Hermes source inventory"))
        command.add_argument("--sensitivity", choices=("low", "balanced", "high"))
        if name == "tick":
            command.add_argument("--notify-only", action="store_true", help=_("Print only new alerts and action transitions"))
    ops_commands.add_parser("history", help=_b("Past supervision alerts (deprecated)",
                                                "Alertas passados da supervisão (obsoleto)"))
    ops_commands.add_parser("rollback", help=_b("Undo an active Linux trial (deprecated)",
                                                 "Desfaz um ensaio ativo no Linux (obsoleto)"))
    similar = ops_commands.add_parser("similar", help=_("Find previous diagnostic patterns"))
    similar.add_argument("query")
    similar.add_argument("--limit", type=int, choices=range(1, 11), default=5, metavar="1..10")
    review = ops_commands.add_parser("review", help=_("Review learned baselines, feedback and outcomes"))
    review.add_argument("--days", type=int, choices=range(1, RETENTION_DAYS + 1), default=14, metavar="1..35")
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
    if language == "pt":
        _portuguese_help(parser)


def _memory(conn, args):
    from .memory import GLOBAL_SCOPE, MemoryStore

    scope = "local"
    if args.namespace and not args.dm:
        raise ValueError("--namespace requires --dm")
    if args.shared:
        scope = GLOBAL_SCOPE
    if args.dm:
        from .integration import dm_scope

        scope = dm_scope(*args.dm, args.namespace)
    store = MemoryStore(conn)
    if args.memory_action in ("remember", "forget"):
        # A note may be recalled in front of the agent (the 0.1 shared recall): only the operator writes them.
        _operator_only()
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


_OWNER_TEXT = {
    "en": {"title": "Which chats count as you", "current": "Counted as you now", "senders": "sender ids",
           "platforms": "platforms", "none": "none", "none_platforms": "none",
           "channels": "Chat channels in the last 30 days", "col_channel": "channel",
           "col_sessions": "sessions", "col_senders": "senders (sessions)", "col_you": "counts as you",
           "yes": "yes", "no": "no", "empty": "No chat session in 30 days: your CLI, TUI and Desktop always count as you.",
           "add": "To count a sender as you", "platform": "To count every chat of a channel as you (a private bot, your own UI)",
           "channel": "<channel>",
           "run": "Run this in your own terminal (it writes the value of the Hermes settings form):",
           "applies": "A running gateway picks the change up by itself at its next check, usually within a minute "
                      "(up to 10 minutes when it is idle); no restart is needed.",
           "why": "The plugin does not write it itself: who counts as you decides what reaches the agent, so it lives in "
                  "the Hermes configuration, which the agent's file tools cannot change.",
           "unchanged": "Nothing to change: {key} is already {value}.",
           "ignored": "Ignored in `settings.json`, because the agent's file tools can write that file: {keys}. To keep a "
                      "value, add it again with `hermes evolution owner add <id>` or `add-platform <channel>`."},
    "pt": {"title": "Quem é você nos chats", "current": "Contam como você agora", "senders": "ids de remetente",
           "platforms": "plataformas", "none": "nenhum", "none_platforms": "nenhuma",
           "channels": "Canais de chat dos últimos 30 dias",
           "col_channel": "canal", "col_sessions": "sessões", "col_senders": "remetentes (sessões)",
           "col_you": "conta como você", "yes": "sim", "no": "não",
           "empty": "Nenhuma sessão de chat em 30 dias: a sua CLI, TUI e Desktop sempre contam como você.",
           "add": "Para contar um remetente como você", "channel": "<canal>",
           "platform": "Para contar todo chat de um canal como você (um bot privado, a sua própria interface)",
           "run": "Rode isto no seu próprio terminal (grava o valor do formulário de configurações do Hermes):",
           "applies": "Um gateway em execução pega a mudança sozinho na próxima verificação, em geral em até um minuto "
                      "(até 10 minutos se estiver parado); não precisa reiniciar.",
           "why": "O plugin não grava isso sozinho: quem conta como você decide o que chega ao agente, então isso fica na "
                  "configuração do Hermes, que as ferramentas de arquivo do agente não conseguem alterar.",
           "unchanged": "Nada a mudar: {key} já é {value}.",
           "ignored": "Ignorado no `settings.json`, porque as ferramentas de arquivo do agente conseguem gravar esse "
                      "arquivo: {keys}. Para manter um valor, adicione de novo com `hermes evolution owner add <id>` ou "
                      "`add-platform <canal>`."},
}


def _stdin_tty() -> bool:
    """Whether someone can answer a question on stdin (menu.tty: NUL is no terminal on Windows)."""
    import sys

    from .menu import tty

    return tty(sys.stdin)


def _owner(args, home: Path, *, now: float, runtime_config, lang: str) -> str:
    """`hermes evolution owner`: the chat channels and senders, and who counts as the owner. Operator only: it
    shows sender ids. A change is printed as the `hermes config set` command that makes it, never written here:
    the Hermes configuration is the one place the agent's file tools cannot change."""
    from .evolve import options
    from .status import chat_senders

    _operator_only()
    t = _OWNER_TEXT[lang if lang in _OWNER_TEXT else "en"]
    action = getattr(args, "owner_action", None) or "list"
    chosen = options(effective_settings(home, runtime_config), runtime_config, strict=False)
    from .evolve import CONFIG_UNREAD

    if action != "list" and isinstance(runtime_config, dict) and runtime_config.get(CONFIG_UNREAD):
        # the owners in config.yaml are unknown here: a printed list built on "none" would drop them all
        raise ValueError("The Hermes configuration could not be read here, so the owners already set are unknown: "
                         "run it as hermes evolution owner")
    if action != "list":
        key = "owner_platforms" if action.endswith("platform") else "owner_sender_ids"
        value = str(args.value).strip()
        if key == "owner_platforms":
            value = value.lower()
            if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,47}", value):
                raise ValueError("Name a platform such as telegram or api_server")
        elif not 0 < len(value) <= 200:
            raise ValueError("A sender id has 1 to 200 characters")
        current = list(chosen[key])
        updated = [*current, value] if action.startswith("add") and value not in current else \
            [item for item in current if item != value] if action.startswith("remove") else current
        if len(updated) > 20:
            raise ValueError("Up to 20 entries are kept; remove one first")
        if updated == current:
            return t["unchanged"].format(key=key, value=json.dumps(current, ensure_ascii=False)) + "\n"
        # integration.refresh_settings re-reads who the owner is on the hooks and the autopilot's clock: no restart
        return "\n".join([t["run"], "", f"    {settings_command(home, key, updated)}", "", t["applies"], "",
                          t["why"]]) + "\n"
    channels = chat_senders(home, since=now - 30 * 86_400, owners=chosen["owner_sender_ids"],
                            platforms=chosen["owner_platforms"])
    lines = [f"# {t['title']}", "",
             f"- **{t['current']}:** {t['senders']}: {', '.join(f'`{item}`' for item in chosen['owner_sender_ids']) or t['none']}"
             f" · {t['platforms']}: {', '.join(f'`{item}`' for item in chosen['owner_platforms']) or t['none_platforms']}",
             ""]
    if not channels:
        lines.append(t["empty"])
    else:
        lines += [f"## {t['channels']}", "", f"| {t['col_channel']} | {t['col_sessions']} | {t['col_senders']} | {t['col_you']} |",
                  "|---|---:|---|---|"]
        for channel in channels:
            senders = ", ".join(f"`{item['id']}` ({item['sessions']})" + (" ✓" if item["owner"] else "")
                                for item in channel["senders"][:8] if item["id"] is not None) or "—"
            lines.append(f"| `{channel['origin']}` | {channel['sessions']} | {senders} | "
                         f"{t['yes'] if channel['covered'] else t['no']} |")
        lines += ["", f"{t['add']}: `hermes evolution owner add <id>`",
                  f"{t['platform']}: `hermes evolution owner add-platform {t['channel']}`"]
    try:
        ignored = [key for key in ignored_settings(settings(home)) if key.startswith("owner_")]
    except (OSError, ValueError, RecursionError):
        ignored = []
    if ignored:
        lines += ["", t["ignored"].format(keys=", ".join(f"`{key}`" for key in ignored))]
    return "\n".join(lines) + "\n"


def _evolution(args, home: Path, *, now: float, runtime_config=None, reflect=None):
    """Commands of the evolution loop: lessons, skills, proposals, experiments and the digest."""
    from . import decisions, experiments, lessons, proposals
    from .evolve import cycle, notify_view, options
    from .experience import experience
    from .report import build, markdown
    from .skill_health import skill_health

    action = args.evolution_action
    config = effective_settings(home, runtime_config)
    opts = options(config, runtime_config)
    lang = getattr(args, "lang", None) or opts["language"]
    if action == "owner":
        return _owner(args, home, now=now, runtime_config=runtime_config, lang=lang)
    if action == "decisions":
        return _decisions(args, home, now=now, lang=lang, runtime_config=runtime_config)
    if action == "status":
        from . import status

        first = None
        if not getattr(args, "no_learn", False) and status.never_ran(home):
            # The first look should show something: read the history now instead of waiting for a turn.
            from .autopilot import run_exclusive

            try:
                ran = run_exclusive(home, lambda: cycle(home, now=now, runtime_config=runtime_config, config=config,
                                                        lang=lang, reflect=reflect, backfill_budget_s=60.0,
                                                        deadline=time.monotonic() + 90), wait_s=5)
                done = ran.get("history") or {}
                first = {"sessions": done.get("sessions", 0), "tool_calls": done.get("tool_calls", 0)}
            except ValueError:   # another pass is running right now: it will show up in a moment
                first = None
        state = status.build(home, now=now, config=config, runtime_config=runtime_config)
        state["first_pass"] = first
        return status.markdown(state, lang) if args.format == "md" else state
    if action == "learn":
        from .autopilot import run_exclusive

        from .autopilot import LEASE_SECONDS

        # within the pass's lease, as the autopilot's: past it another process could start a second pass
        result = run_exclusive(home, lambda: cycle(home, now=now, runtime_config=runtime_config, config=config,
                                                   lang=lang, reflect=reflect, backfill_budget_s=120.0,
                                                   deadline=time.monotonic() + LEASE_SECONDS - 180))
        view = notify_view(result) if args.notify_only else result
        return _masked_pass(view) if _agent_shell() else view
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
        chosen = options(config, runtime_config, strict=False)
        report = build(home, now=now, days=args.days, lang=lang, rates=config.get("rates_usd_per_million", {}),
                       own_transform=chosen["tool_hints"] and chosen["lessons"] != "off")
        if _agent_shell():   # a stranger's chat with a terminal reads counts, never the text written for the owner
            report = {**report, "lessons": _hidden_from_agent(report.get("lessons") or [], "lesson"),
                      "proposals": _hidden_from_agent(report.get("proposals") or [], "evidence")}
        return markdown(report, lang) if args.format == "md" else report
    with experience(home) as conn:
        if action == "experiments":
            return experiments.listing(conn)
        if action == "lessons":
            task = args.lessons_action or "list"
            if task == "list":
                listed = lessons.listing(conn, lang=lang, status=getattr(args, "status", None))
                return {"mode": opts["lessons"], "lessons": _hidden_from_agent(listed, "lesson") if _agent_shell() else listed}
            _operator_only()
            if task == "export":
                return _export_lessons(conn, home, now=now, out=getattr(args, "out", None), lang=lang)
            if task == "import":
                return _import_lessons(conn, home, args, now=now, mode=opts["lessons"], lang=lang)
            if task == "review":
                return _review_lessons(conn, lang, now=now)
            if task == "add":
                activate = bool(getattr(args, "activate", False))
                if activate:
                    import shlex

                    text = lessons.clean_text(args.text)
                    _confirm(text or "", lang, yes=getattr(args, "yes", False), again=_hermes_cmd(
                        home, f"evolution lessons add {shlex.quote(args.text)} --tool {shlex.quote(args.tool)} "
                              "--activate --yes"))
                lesson_id = lessons.add_custom(conn, args.text, tool=args.tool, origin="operator", now=now, active=activate)
                return {"id": lesson_id, "status": "active" if activate else "quarantined"}
            if task == "approve" and getattr(args, "all", False):
                return _approve_all(conn, lang, now=now, yes=getattr(args, "yes", False))
            row = conn.execute("SELECT * FROM lesson WHERE id=?", (args.id,)).fetchone()
            if row is None:
                raise ValueError(f"Unknown lesson id {args.id}")
            if task == "approve":
                if row["status"] == "active":   # nothing to ask: it is already where the command would put it
                    return {"id": args.id, "status": "active", "previous": "active"}
                _confirm(lessons.render(row, lang), lang, yes=getattr(args, "yes", False),
                         read=_hermes_cmd(home, "evolution lessons list"),
                         again=_hermes_cmd(home, f"evolution lessons approve {args.id} --yes"))
                withdrawn = row["status"] == "retired" and row["verdict"] == lessons.WITHDRAWN
                action = "approve" if row["status"] in decisions.WAITING or withdrawn else "reactivate"
            elif row["status"] == "retired" and row["verdict"] != lessons.WITHDRAWN:
                # already retired (by the owner or the measurement): nothing is recorded, so there is no empty
                # decision to undo. One the queue cleaning withdrew is still decided below: from then on it stays
                # retired instead of coming back when its failure repeats.
                return {"id": args.id, "status": "retired", "previous": "retired"}
            else:
                action = "retire" if row["status"] == "active" else "reject"
            outcome = decisions.decide_lesson(conn, args.id, action, now=now, actor="terminal", lang=lang)
            if not outcome["decided"]:   # already where the command would put it
                return {"id": args.id, "status": outcome["status"], "previous": outcome["status"]}
            return {"id": args.id, "status": outcome["status"], "previous": outcome["previous"],
                    "decision": outcome["decision"]}
        task = args.proposals_action or "list"
        if task == "list":
            listed = proposals.listing(conn, status=None if getattr(args, "all", False) else "open")
            return _hidden_from_agent(listed, "evidence") if _agent_shell() else listed
        _operator_only()
        state = conn.execute("SELECT status FROM proposal WHERE id=?", (args.id,)).fetchone()
        if state is not None and state["status"] not in ("open", "approved"):   # said in words, never as a state code
            known = _H["en"]["already"].get(state["status"])
            if known:
                _fail(lang, known.format(id=args.id), _H["pt"]["already"][state["status"]].format(id=args.id))
        if task == "approve":
            from . import explain

            row = conn.execute("SELECT kind, subject, evidence, command FROM proposal WHERE id=?", (args.id,)).fetchone()
            if row is None:
                raise ValueError(f"Unknown proposal id {args.id}")
            evidence = json.loads(row["evidence"])
            read, again = (_hermes_cmd(home, "evolution proposals list"),
                           _hermes_cmd(home, f"evolution proposals approve {args.id} --yes"))
            if explain.plan(dict(row)) is not None:   # a reversible change the plugin applies with Hermes' command
                item = {**dict(row), "evidence": evidence}
                told = explain.describe(item, lang)
                shown = [f"{told['title']} · {row['subject']}", _if_approved(item, told, home, lang)]
                if told.get("undo"):
                    shown.append(f"{_H[lang if lang in _H else 'en']['undo_label']}: {_undo_words(told['undo'])}")
                _confirm("\n  ".join(part for part in shown if part), lang,
                         yes=getattr(args, "yes", False), apply=True, read=read, again=again)
                conn.commit()
                from .actions import apply_proposal

                return apply_proposal(home, args.id, now=now, actor="terminal")
            _confirm(evidence.get("lesson") or row["command"], lang, yes=getattr(args, "yes", False), read=read, again=again)
        return decisions.decide_proposal(conn, args.id, task, now=now, actor="terminal", lang=lang, note=args.note)


AGENT_MARKERS = ("ATLAS_EVOLUTION_AGENT", "HERMES_SESSION_ID", "HERMES_KANBAN_TASK", "HERMES_DELEGATED_CHILD_CONTEXT")


def _process(pid: int):
    """(parent pid, argv) of a process from /proc, or None (not Linux, gone, or unreadable)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()[:8192]
        argv = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
        return int(stat.rsplit(")", 1)[1].split()[1]), argv
    except (OSError, ValueError, IndexError):
        return None


# "evolution" as a word of its own: this command (hermes evolution ..., or argv quoted after a relaunch), not a
# path such as hermes-atlas-evolution or hermes_atlas_evolution.
_OWN_COMMAND = re.compile(r"(?:^|[\s'\"\[,])evolution(?:[\s'\"\],]|$)")


def _is_hermes(argv: list[str]) -> bool:
    """A Hermes process: the hermes launcher, or Python running the installed Hermes code."""
    if not argv:
        return False
    if os.path.basename(argv[0]) in ("hermes", "hermes-agent"):
        return True
    if re.fullmatch(r"python[\d.]*w?(?:\.exe)?", os.path.basename(argv[0]), re.I) and len(argv) > 1 and \
            os.path.basename(argv[1]) in ("hermes", "hermes-agent"):
        return True   # the launcher script of a virtual environment, run by its python
    return any("hermes-agent" in part or "hermes_cli" in part for part in argv[:8])


def _own_launcher(argv: list[str]) -> bool:
    """Whether a Hermes process is this very command's launcher: `hermes [global options] evolution ...`, read from
    the structure of its arguments. The word "evolution" anywhere (a profile named evolution, a one-shot prompt that
    mentions it) does not make a gateway or a chat session this command's own."""
    words = list(argv[1:])
    if "-c" in words[:4] and words.index("-c") + 1 < len(words):
        # Hermes relaunching itself: python -c "...; sys.argv = ['hermes', 'evolution', ...]; runpy.run_path(...)"
        found = re.search(r"sys\.argv\s*=\s*(\[[^\]]{0,2000}\])", words[words.index("-c") + 1])
        if not found:
            return False
        try:
            import ast

            relaunched = ast.literal_eval(found.group(1))
        except (ValueError, SyntaxError):
            return False
        return isinstance(relaunched, list) and all(isinstance(item, str) for item in relaunched) \
            and _own_launcher(["python", *relaunched])
    if words[:1] == ["-m"]:
        words = words[2:]   # python -m hermes_cli.main ...
    elif words and os.path.basename(words[0]) in ("hermes", "hermes-agent"):
        words = words[1:]   # python .../bin/hermes ...
    while words and words[0].startswith("-"):
        words = words[2:] if words[0] in ("-p", "--profile") else words[1:]
    return words[:1] == ["evolution"]


def _in_hermes_cgroup(*, text: str | None = ...) -> bool:
    """True when this process sits in the cgroup of a Hermes gateway service. Detaching with setsid or a
    double fork changes the parent, not the cgroup."""
    if text is ...:
        try:
            text = Path("/proc/self/cgroup").read_text(encoding="utf-8", errors="replace")[:8192]
        except OSError:
            return False
    return isinstance(text, str) and "hermes-gateway" in text


def _under_hermes(*, start: int | None = None, read=_process, depth: int = 24) -> bool:
    """True when a Hermes process (a gateway, a chat session, a one-shot run) is an ancestor of this command.

    An agent can clear the environment markers of a shell it runs; it cannot change which process started
    it. Only this command's own launcher chain (`hermes evolution ...`, unbroken from this process up) is
    skipped: a Hermes ancestor further up counts even when its own words include "evolution" (a one-shot
    `hermes -z "...evolution..."`, a profile named evolution)."""
    pid = os.getpid() if start is None else start
    own = True
    for _ in range(depth):
        entry = read(pid)
        if entry is None:
            return False
        parent, argv = entry
        hermes = _is_hermes(argv)
        # a Hermes process is this command's own only by the shape of its arguments; a shell or `env` in between
        # (sh -c "hermes evolution ...") by the word
        own = own and (_own_launcher(argv) if hermes else bool(_OWN_COMMAND.search(" ".join(argv))))
        if hermes and not own:
            return True
        if parent <= 1 or parent == pid:
            return False
        pid = parent
    return False


def _agent_shell() -> bool:
    """True when this command runs in a shell of an agent session: what _operator_only refuses."""
    return any(os.environ.get(name, "").strip() for name in AGENT_MARKERS) or _under_hermes() or _in_hermes_cgroup()


def _masked_pass(view):
    """A learning pass's result as an agent's shell may print it: no lesson or draft text, no proposal's lesson."""
    if not isinstance(view, dict):
        return view
    view = dict(view)
    if isinstance(view.get("new_lessons"), list):
        view["new_lessons"] = _hidden_from_agent(view["new_lessons"], "lesson")
    for key in ("open_proposals", "high_risk_proposals"):
        if isinstance(view.get(key), list):
            view[key] = _hidden_from_agent(view[key], "evidence")

    def drafts(items):   # written by a model: always hidden
        return [{**item, "lesson": None, "text_hidden": True} for item in items if isinstance(item, dict)]

    if isinstance(view.get("reflector_drafts"), list):
        view["reflector_drafts"] = drafts(view["reflector_drafts"])
    if isinstance(view.get("reflector"), dict) and isinstance(view["reflector"].get("drafted"), list):
        view["reflector"] = {**view["reflector"], "drafted": drafts(view["reflector"]["drafted"])}
    return view


def _hidden_from_agent(rows: list[dict], field: str) -> list[dict]:
    """The rows an agent's shell may print: written text (the operator's, a model's or the agent's) is left out,
    because a shell of a stranger's chat is an agent shell too. The operator reads it in their own terminal."""
    shown = []
    for row in rows:
        row = dict(row)
        if field == "lesson" and row.get("origin") != "derived":
            row["lesson"], row["text_hidden"] = None, True
        elif field == "evidence" and isinstance(row.get("evidence"), dict) and "lesson" in row["evidence"]:
            row["evidence"] = {**row["evidence"], "lesson": None}
            row["text_hidden"] = True
        shown.append(row)
    return shown


def _operator_only() -> None:
    """Refuse playbook decisions from a shell the agent runs. Hermes marks those shells with the session id,
    and the plugin adds ATLAS_EVOLUTION_AGENT to every subprocess of an agent session."""
    if _agent_shell():
        raise ValueError("Only the operator can decide on lessons and proposals. Run this command in your own "
                         "terminal; the agent can propose lessons, which wait for your approval.")


_REVIEW_PROMPT = {"en": "[a]pprove · [r]eject · Enter skips · [x] quit: ",
                  "pt": "[a]provar · [r]ejeitar · Enter pula · [x] sair: "}


def _review_lessons(conn, lang: str, *, now: float) -> dict:
    """Show each lesson that waits for approval with its exact text and record the owner's decision."""
    import sys

    from . import decisions
    from . import lessons as playbook

    if not _stdin_tty():
        raise ValueError("Run lessons review in your own terminal")
    order = "CASE origin WHEN 'derived' THEN 0 WHEN 'operator' THEN 1 WHEN 'reflector' THEN 2 ELSE 3 END, id"
    rows = conn.execute(f"SELECT * FROM lesson WHERE status IN ('candidate','quarantined') ORDER BY {order}").fetchall()
    decided = {"approved": [], "rejected": [], "skipped": []}
    conn.commit()   # nothing is held while the owner reads: the gateway's hooks keep writing meanwhile
    for row in rows:
        shown = playbook.render(row, lang)
        from .report import failure_words

        origin = _H.get(lang, _H["en"])["origins"].get(row["origin"], row["origin"])
        sys.stderr.write(f"\n#{row['id']} · {row['tool']} · {failure_words(row['klass'], '', lang)} · {origin}\n  "
                         f"{shown}\n{_REVIEW_PROMPT.get(lang, _REVIEW_PROMPT['en'])}")
        sys.stderr.flush()
        answer = sys.stdin.readline().strip().lower()[:1]
        if answer == "x":
            break
        if answer in ("a", "r"):
            approve = answer == "a"
            # A pass may have changed the lesson while the owner read it: decide only on what was shown.
            now_row = conn.execute("SELECT * FROM lesson WHERE id=?", (row["id"],)).fetchone()
            if now_row is None or now_row["status"] not in ("candidate", "quarantined") or \
                    playbook.render(now_row, lang) != shown:
                decided["skipped"].append(row["id"])
                continue
            decisions.decide_lesson(conn, row["id"], "approve" if approve else "reject", now=now, actor="terminal",
                                    lang=lang, shown=decisions.fingerprint(shown))
            conn.commit()   # each decision is saved at once, and the write lock is released before the next question
            decided["approved" if approve else "rejected"].append(row["id"])
        else:
            decided["skipped"].append(row["id"])
    return decided


def _confirm(text: str, lang: str, *, yes: bool, apply: bool = False, again: str = "", read: str = "",
             what: str = "") -> None:
    """Show exactly what the agent would read, or what will run, and ask before it takes effect. Without a terminal
    to ask in, nothing happens and the message names the command that goes ahead (`again`, with --yes), what to read
    first (`read`) and what would run (`what`)."""
    import sys

    if yes:
        return
    if not _stdin_tty():
        about = (_say(lang, f" before running `{what}`", f" antes de rodar `{what}`")) if what else ""
        step = (_say(lang, f"Read it with `{read}`, then run", f"Leia com `{read}` e depois rode") if read
                else _say(lang, "To go ahead, run", "Para seguir, rode"))
        _fail(lang, f"Nothing changed: there is no terminal here to ask you{about}. {step} `{again or '... --yes'}`.",
              f"Nada mudou: não há um terminal aqui para perguntar{about}. {step} `{again or '... --yes'}`.")
    if apply:
        question = "Aplicar esta mudança no Hermes agora? [s/N] " if lang == "pt" else "Apply this change to Hermes now? [y/N] "
    else:
        question = "Ativar este texto para o agente? [s/N] " if lang == "pt" else "Activate this text for the agent? [y/N] "
    sys.stderr.write(f"\n  {text}\n\n{question}")   # stdout stays machine-readable
    sys.stderr.flush()
    answer = sys.stdin.readline().strip().lower()
    if answer not in ("y", "yes", "s", "sim"):
        raise ValueError("Cancelled; nothing changed")


def _profile_name(home: Path) -> str:
    from .admin import profile_name

    return profile_name(home)   # read before links are followed: a linked profile keeps its own name


def _export_lessons(conn, home: Path, *, now: float, out: str | None = None, lang: str = "en") -> dict:
    """The active lessons as a pack: printed, or written to a new file (never over an existing one)."""
    from .packs import export
    from .status import plugin_version

    pack = export(conn, now=now, profile=_profile_name(home), plugin_version=plugin_version())
    if not out:
        return pack
    try:
        with open(out, "x", encoding="utf-8") as handle:
            json.dump(pack, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except FileExistsError:
        raise ValueError("That file already exists; choose another name or remove it first") from None
    except OSError:
        _fail(lang, "Cannot write the lesson pack there: check that the folder exists and that you can write in it",
              "Não foi possível gravar o pacote de lições aí: confira se a pasta existe e se você pode gravar nela")
    return {"written": str(Path(out).resolve()), "lessons": len(pack["lessons"])}


_IMPORT_PROMPT = {"en": ("Import these {count} lessons? They wait for approval or for your own turns to hit their failure. [y/N] ",
                         "Import and activate these {count} lessons for the agent? [y/N] "),
                  "pt": ("Importar estas {count} lições? Elas esperam aprovação ou que os seus turnos encontrem a falha. [s/N] ",
                         "Importar e ativar estas {count} lições para o agente? [s/N] ")}


def _import_lessons(conn, home: Path, args, *, now: float, mode: str, lang: str) -> dict:
    """Import a pack after showing every sentence it adds. Operator only (the caller checked)."""
    import sys

    from . import packs

    source = getattr(args, "from_profile", None)
    pack = packs.from_profile(home, source, now=now) if source is not None else packs.read_pack(args.file)
    planned = packs.plan(conn, pack, lang=lang)
    result = {"mode": mode, "source": source or planned["source"] or "file", "to_import": [
        {"kind": item["kind"], "tool": item["tool"], "lesson": item["lesson"]} for item in planned["add"]],
        "skipped": planned["skip"], "imported": []}
    if getattr(args, "dry_run", False) or not planned["add"]:
        return result
    activate = bool(getattr(args, "activate", False))
    if not getattr(args, "yes", False):
        if not _stdin_tty():
            raise ValueError("Confirm with --yes after reading the lessons with --dry-run")
        prompt = _IMPORT_PROMPT.get(lang, _IMPORT_PROMPT["en"])[1 if activate else 0]
        shown = "\n".join(f"  - [{item['tool']}] {item['lesson']}" for item in planned["add"])
        sys.stderr.write(f"\n{shown}\n\n{prompt.format(count=len(planned['add']))}")   # stdout stays machine-readable
        sys.stderr.flush()
        if sys.stdin.readline().strip().lower() not in ("y", "yes", "s", "sim"):
            raise ValueError("Cancelled; nothing changed")
    result["imported"] = packs.apply(conn, planned, now=now, mode=mode, activate=activate,
                                     source=source or "file")
    return result


_APPROVE_ALL_PROMPT = {"en": "Approve these {count} lessons for the agent? [y/N] ",
                       "pt": "Aprovar estas {count} lições para o agente? [s/N] "}


def _approve_all(conn, lang: str, *, now: float, yes: bool) -> dict:
    """Approve every lesson that waits, after showing each sentence; the agent's own proposals stay for a decision
    of their own. Operator only (the caller checked)."""
    import sys

    from . import decisions

    waiting = decisions.waiting(conn, lang=lang)
    chosen = [item for item in waiting if item["bulk"]]
    result = {"approved": [], "skipped": [], "left_for_one_by_one": [item["id"] for item in waiting if not item["bulk"]]}
    if not chosen:
        return result
    if not yes:
        if not _stdin_tty():
            raise ValueError("Confirm with --yes after reading the lessons with 'lessons list'")
        shown = "\n".join(f"  #{item['id']} [{item['tool']}] {item['lesson']}" for item in chosen)
        sys.stderr.write(f"\n{shown}\n\n{_APPROVE_ALL_PROMPT.get(lang, _APPROVE_ALL_PROMPT['en']).format(count=len(chosen))}")
        sys.stderr.flush()
        if sys.stdin.readline().strip().lower() not in ("y", "yes", "s", "sim"):
            raise ValueError("Cancelled; nothing changed")
    outcome = decisions.approve_all(conn, {item["id"]: item["fingerprint"] for item in chosen}, now=now,
                                    actor="terminal", lang=lang)
    return {**result, **outcome}


def setting_values(home: Path, runtime_config: dict) -> dict:
    """The value of each option of the settings form as the plugin uses it now, by its key in the form. A broken
    settings.json falls back as in the gateway (safe_settings)."""
    from .evolve import options

    config, _problems = safe_settings(home, runtime_config)
    opts = options(config, runtime_config, strict=False)
    raw = config.get("evolution") if isinstance(config.get("evolution"), dict) else {}
    values = {key: opts.get(key) for key in NATIVE_KEYS if key in opts}
    values["agent_tools"] = config.get("agent_advice") is True
    values["owner_sender_ids"] = list(opts["owner_sender_ids"])
    values["owner_platforms"] = list(opts["owner_platforms"])
    values["language"] = raw.get("language") if raw.get("language") in ("auto", "en", "pt") else "auto"
    return values


def _decisions(args, home: Path, *, now: float, lang: str, runtime_config=None):
    """`hermes evolution decisions [list | undo <id>]`: the owner's decisions from the terminal and the panel."""
    from . import decisions
    from .experience import experience

    task = getattr(args, "decisions_action", None) or "list"
    with experience(home) as conn:
        if task == "list":
            history = decisions.history(conn, lang=lang, limit=getattr(args, "limit", 50) or 50)
            return decisions.masked(history) if _agent_shell() else history
        _operator_only()
        row = conn.execute("SELECT kind, target FROM decision_log WHERE id=?", (args.id,)).fetchone()
        if row is not None and row["kind"] != "setting":
            # a lesson, a proposal, a change applied with Hermes' command or a memory entry: undone here, as in the menu
            conn.commit()
            from .admin import decide

            return decide(home, runtime_config or {}, {"kind": "undo", "id": args.id}, now=now, lang=lang,
                          actor="terminal")
        current = ...
        if row is not None and row["kind"] == "setting":
            current = setting_values(home, runtime_config or {}).get(row["target"])
        result = decisions.undo(conn, args.id, now=now, actor="terminal", current=current)
        if result.pop("restorable", False):
            # Hermes' configuration is the owner's to set: the command is printed, and the decision stays as it is until
            # a change is made through the panel (which writes it) or this command is run.
            result["run_this"] = settings_command(home, result["key"], result["restore"])
        return result


def _pick_profiles(rows: list, lang: str) -> list:
    """Ask in the operator's terminal which profiles to update: Enter = all, or numbers / names."""
    import sys

    sys.stderr.write("\n")
    for number, row in enumerate(rows, 1):
        state = (_say(lang, "pinned", "fixado") if row["pinned"] else
                 (row["version"] or "?") if row["installed"] else _say(lang, "not installed", "não instalado"))
        sys.stderr.write(f"  {number:>2}. {row['profile']}  ({state})\n")
    sys.stderr.write(_say(lang, "\nProfiles to update (Enter = all, e.g. 1,3 or default,work; q = quit): ",
                          "\nPerfis para atualizar (Enter = todos, ex.: 1,3 ou default,work; q = sair): "))
    sys.stderr.flush()
    answer = sys.stdin.readline().strip()
    if answer.lower() in ("q", "quit", "sair"):
        raise ValueError("Cancelled; nothing changed")
    if not answer or answer.lower() in ("a", "all", "todos", "t"):
        return ["all"]
    picked = []
    for part in answer.replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if part.isdigit() and 1 <= int(part) <= len(rows):
            picked.append(rows[int(part) - 1]["profile"])
        else:
            picked.append(part)
    return picked


def _fleet_update(args, home: Path, lang: str) -> dict:
    """`hermes evolution update`: this plugin in all or the chosen profiles, with Hermes' own commands."""
    import sys

    from . import fleet_update
    from .admin import launch_home

    rows = fleet_update.plan(launch_home(home))
    if args.list_only:
        return {"listed": rows}
    _operator_only()
    if args.all_profiles:
        wanted = "all"
    elif args.profiles.strip():
        wanted = [name for name in args.profiles.split(",")]
    elif _stdin_tty() and not args.yes:
        picked = _pick_profiles(rows, lang)
        wanted = "all" if picked == ["all"] else picked
    else:
        _fail(lang, "Choose the profiles: `hermes evolution update --all` or `--profiles default,work`.",
              "Escolha os perfis: `hermes evolution update --all` ou `--profiles default,work`.")
    chosen = fleet_update.select(rows, wanted)
    if not chosen:
        _fail(lang, "No profile to update.", "Nenhum perfil para atualizar.")
    names = ", ".join(row["profile"] for row in chosen)
    again = "hermes evolution update " + ("--all" if wanted == "all" else
                                          "--profiles " + ",".join(row["profile"] for row in chosen)) \
        + (" --install-missing" if args.install_missing else "") + " --yes"
    _confirm(_say(lang, f"Update {fleet_update.PLUGIN} in: {names}", f"Atualizar {fleet_update.PLUGIN} em: {names}"),
             lang, yes=args.yes, apply=True, again=again)

    def progress(name):
        sys.stderr.write(_say(lang, f"  updating {name}...\n", f"  atualizando {name}...\n"))
        sys.stderr.flush()

    return {"results": fleet_update.run(chosen, install_missing=args.install_missing, progress=progress)}


GATEWAY_UNITS = {"user": ["hermes-gateway.service"], "system": ["hermes-gateway.service"]}


def _gateway_repair(home: Path, task: str, lang: str, *, yes: bool, now: float) -> dict:
    """`care fix-gateway`: when Hermes replaced its bundled Python and the gateway's service still starts the old one
    (systemd: status=203/EXEC, restart loop), point the service at the new Python, reload and start it.
    `care undo-gateway` puts the service file back from the copy kept by the repair. The operator's only."""
    import json

    from . import system_update

    host = system_update.Host()
    record = Path(home) / "evolution" / "gateway-repair.json"
    if task == "undo-gateway":
        try:
            saved = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _fail(lang, "There is no gateway repair to undo.", "Não há reparo do gateway para desfazer.")
        _confirm(_say(lang, f"Restore {saved.get('file')} from {saved.get('backup')}",
                      f"Restaurar {saved.get('file')} a partir de {saved.get('backup')}"),
                 lang, yes=yes, apply=True, again=_hermes_cmd(home, "evolution care undo-gateway --yes"))
        result = system_update.undo_unit_exec(host, saved)
        if result.get("ok"):
            record.unlink(missing_ok=True)
        from . import hermes_care

        hermes_care.refresh_after_repair(home, now)   # status says it again from systemd, not from a 6-hour-old look
        return {**result, "task": task}
    from . import hermes_care

    served = bool(hermes_care.gateway(home, now=now).get("served_by_root"))
    names = hermes_care.expected_gateway_units(home, served=served) or GATEWAY_UNITS["user"]
    probe = system_update.probe_units({"user": names, "system": names}, host=host, timeout=10)
    # a missing Python, or one facts.json dropped (the next `hermes pm gc` deletes it)
    broken = [unit for unit in probe["units"] if unit.get("exec_exists") is False or unit.get("exec_unreferenced")]
    if not broken:
        if probe.get("unverified") and not probe["units"]:
            _fail(lang, "systemd could not be asked about the gateway's service here (run this as the user the "
                        "service belongs to, with its session): nothing was changed.",
                  "Não deu para perguntar ao systemd sobre o serviço do gateway aqui (rode isto como o usuário dono "
                  "do serviço, na sessão dele): nada foi mudado.")
        return {"ok": True, "task": task, "reason": "nothing_to_fix", "units": probe["units"],
                "unverified": probe.get("unverified")}
    unit = hermes_care.pick_gateway_unit(broken, home=home, served=served) or broken[0]
    running = [item for item in probe["units"] if item.get("active") and item is not unit]
    if running and unit.get("enabled") is False and not unit.get("active"):
        # a leftover unit that is off, while another one runs the gateway: starting it would run a second gateway
        return {"ok": True, "task": task, "reason": "nothing_to_fix", "units": probe["units"],
                "unverified": probe.get("unverified"), "leftover": unit.get("unit")}
    if not unit.get("replacement"):
        _fail(lang, "The gateway service starts a Python that no longer exists and no newer Hermes Python was found: "
                    "reinstall Hermes' service.",
              "O serviço do gateway inicia um Python que não existe mais e nenhum Python mais novo do Hermes foi "
              "encontrado: reinstale o serviço do Hermes.")
    _confirm(_say(lang, f"In {unit.get('exec_source')}: {unit['exec_path']} -> {unit['replacement']}, then "
                        "daemon-reload, reset-failed and start the gateway",
                  f"Em {unit.get('exec_source')}: {unit['exec_path']} -> {unit['replacement']}, depois "
                  "daemon-reload, reset-failed e iniciar o gateway"),
             lang, yes=yes, apply=True, again=_hermes_cmd(home, "evolution care fix-gateway --yes"))
    result = system_update.fix_unit_exec(host, unit, now=now)
    if result.get("undo"):   # a reload-only repair changed no file: an earlier record must not become {}
        try:
            record.parent.mkdir(parents=True, exist_ok=True)
            temporary = record.with_name(f".{record.name}.tmp")
            temporary.write_text(json.dumps(result["undo"]), encoding="utf-8")
            os.replace(temporary, record)
        except OSError:
            pass
    hermes_care.refresh_after_repair(home, now)   # status stops saying it cannot start once it runs
    return {**result, "task": task}


def _maintenance(args, home: Path, *, now: float, runtime_config=None):
    """`system`, `care` and `schedule-digest`: what reads is open to anyone; what changes is the operator's."""
    from . import actions, hermes_care, system_update
    from .experience import experience

    lang = getattr(args, "lang", None) or "en"
    if args.evolution_action == "update":
        return _fleet_update(args, home, lang)
    if args.evolution_action == "environment":
        from . import environment, explain

        lead = environment.lead_home(home)
        if lead is None:   # the JSON keeps its reason code; people read status.environment_markdown's sentence
            return {"lead": None, "reason": "no profile has Atlas Evolution on"}
        task = getattr(args, "environment_action", "show")
        if task == "scan":
            found = environment.refresh(lead, now=now, force=True)
            environment.publish(lead, found, now=now)   # what menu screen 14 and the panel then show
        else:
            with experience(lead) as conn:
                found = environment.stored(conn)
                if task == "catalog":
                    rows = conn.execute("SELECT tier, COUNT(*) FROM catalog_plugin GROUP BY tier").fetchall()
                    removed = conn.execute("SELECT name, reason FROM catalog_removed").fetchall()
                    return {"tiers": {row[0]: row[1] for row in rows}, "removed": [dict(row) for row in removed],
                            "catalog": (found or {}).get("catalog")}
        if not found:
            from .admin import profile_name

            return {"lead": profile_name(lead), "scanned": False}
        return {**{key: found.get(key) for key in ("at", "lead", "summary", "catalog", "profiles", "doctor")},
                "findings": [{**item, "explain": explain.describe(item, lang)} for item in found.get("findings") or ()]}
    if args.evolution_action == "schedule-digest":
        _operator_only()
        try:
            return actions.schedule_digest(home, deliver=args.deliver, lang=lang, now=now, actor="terminal")
        except ValueError as error:
            if str(error) == "The hermes command was not found: run this where `hermes --version` works":
                _fail(lang, "The `hermes` command is not on this terminal's PATH: run this from where "
                            "`hermes --version` works.",
                      "O comando `hermes` não está no PATH deste terminal: rode de onde `hermes --version` funciona.")
            raise
    if args.evolution_action == "care":
        task = args.care_action
        if task == "check":
            with experience(home) as conn:
                return hermes_care.cached(conn, home, now=now, force=True)
        _operator_only()
        if task in ("fix-gateway", "undo-gateway"):
            return _gateway_repair(home, task, lang, yes=args.yes, now=now)
        key = {"pm-doctor": "pm_doctor", "doctor": "doctor", "fix": "doctor_fix", "gc": "pm_gc"}[task]
        if key == "pm_gc":
            with experience(home) as conn:
                care = hermes_care.cached(conn, home, now=now, force=True)
            if hermes_care.gc_blocked(care):
                folders = ", ".join(str(item) for item in (care or {}).get("in_use_unreferenced") or ()) or "?"
                _fail(lang, f"Not now: `hermes pm gc` would delete {folders}, which a Hermes service still starts "
                            f"from. Point the service at the current Python first: "
                            f"`{_hermes_cmd(home, 'evolution care fix-gateway')}`.",
                      f"Agora não: o `hermes pm gc` apagaria {folders}, de onde um serviço do Hermes ainda inicia. "
                      f"Aponte o serviço para o Python atual antes: "
                      f"`{_hermes_cmd(home, 'evolution care fix-gateway')}`.")
        command = "hermes " + " ".join(hermes_care.ACTIONS[key][0])
        _confirm(command, lang, yes=args.yes, apply=True, what=command,
                 again=_hermes_cmd(home, f"evolution care {task} --yes"))
        return hermes_care.run_action(home, key)
    host, task = system_update.Host(), args.system_action
    if task == "check":
        return system_update.check(host, now=now, home=home)
    if task == "report":
        folder, found = system_update.last_report(host)
        return {"folder": str(folder) if folder else None, "report": found}
    _operator_only()
    if task == "plan":
        return system_update.plan(host, refresh=True, full=args.full)
    if task == "repair":
        _confirm("dpkg --configure -a && apt-get -f install", lang, yes=args.yes, apply=True,
                 what="dpkg --configure -a && apt-get -f install", again=_hermes_cmd(home, "evolution system repair --yes"))
        return system_update.repair(home, host)
    skip = system_update.clean_skip(args.skip)   # refused, never dropped in silence
    planned = system_update.plan(host, refresh=True, full=args.full)
    if planned.get("error"):
        raise ValueError(planned["error"])
    count = len(planned["upgrade"]) + len(planned["install"]) + len(planned["remove"])
    if not count:
        return {"planned": planned, "upgraded": False, "reason": "nothing_to_do"}
    upgrade = f"apt-get {'full-upgrade' if args.full else '--with-new-pkgs upgrade'}"
    flags = (" --full" if args.full else "") + (" --skip " + " ".join(skip) if skip else "")
    _confirm(f"{upgrade}: " + _p(("1 pacote", "{n} pacotes") if lang == "pt" else ("1 package", "{n} packages"), count),
             lang, yes=args.yes, apply=True, what=upgrade, read=_hermes_cmd(home, "evolution system plan" + (" --full" if args.full else "")),
             again=_hermes_cmd(home, f"evolution system upgrade{flags} --yes"))
    folder, before, _snapshot = system_update.prepare(home, host, now=now)
    started = system_update.start(host, folder, full=args.full, skip=skip)
    while system_update.progress(host, folder) == "running":
        time.sleep(3)
    report = system_update.after(host, folder, before)
    with experience(home) as conn:
        system_update.cached(conn, home, now=time.time(), host=host, force=True)
    return {"started": started, "folder": str(folder), "report": report}


def _scheduled_cycle(home: Path, config: dict, *, now: float, runtime_config=None, reflect=None):
    """Run the evolution pass from `ops tick`, at most once an hour and never alongside the autopilot of a
    running gateway (both claim the same pass). Returns its notification view."""
    from .autopilot import claim, finish
    from .evolve import cycle, notify_view

    owner = f"tick-{os.getpid()}"
    # The clock of this moment, not of when the command started: an autopilot pass may have finished since.
    if not claim(home, now=time.time(), interval_s=CYCLE_INTERVAL_SECONDS, owner=owner):
        return None
    started = time.time()
    try:   # the cron recipe allows 300 s; the pass ends well before, skipping model calls that would not fit
        result = cycle(home, now=started, runtime_config=runtime_config, config=config, reflect=reflect,
                       deadline=time.monotonic() + 200)
    except BaseException as problem:
        finish(home, now=time.time(), owner=owner, ok=False, interval_s=CYCLE_INTERVAL_SECONDS, started=started,
               error=type(problem).__name__)
        raise
    finish(home, now=time.time(), owner=owner, ok=True, interval_s=CYCLE_INTERVAL_SECONDS, started=started)
    return notify_view(result)


def _report(args, home: Path, *, running_version=None, provider=None, now: float, runtime_config=None,
            hermes_source=None, kanban_home=None, supplemental=(), health_override=None, reflect=None):
    action = args.evolution_action
    if action in EVOLUTION_ACTIONS:
        return _evolution(args, home, now=now, runtime_config=runtime_config, reflect=reflect)
    if action in MAINTENANCE_ACTIONS:
        return _maintenance(args, home, now=now, runtime_config=runtime_config)
    if action == "ops":
        return _ops(args, home, now=now, running_version=running_version, provider=provider,
                    runtime_config=runtime_config, source=hermes_source, kanban_home=kanban_home, reflect=reflect)
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
            if args.memory_action == "remember" and \
                    effective_settings(home, runtime_config).get("legacy_memory_commands") is not True:
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
            from .memory import MemoryStore

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


def _ops(args, home: Path, *, now, running_version, provider, runtime_config, source, kanban_home, reflect=None):
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
        return schedule_spec(home, every_minutes=args.every_minutes, upstream_tag=args.upstream_tag, lang=args.lang)
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
        evolution = _scheduled_cycle(home, config, now=now, runtime_config=runtime_config, reflect=reflect)
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


# ── what a person reads (0.9.0) ──────────────────────────────────────────────────────────────────────────────────
# Every command a person runs prints Markdown in the profile's language by default; `--format json` keeps the machine
# output (the same structure as before 0.9.0). A caller that builds the arguments itself, without `format`, gets the
# structure: the menu and the panel call `execute` that way.
HUMAN_ACTIONS = ("care", "environment", "decisions", "lessons", "proposals", "learn", "skills", "experiments", "system",
                 "health", "usage", "history", "tick", "schedule-digest", "update")


def _wants_text(args) -> bool:
    return getattr(args, "evolution_action", None) in HUMAN_ACTIONS and getattr(args, "format", "json") == "md"


def _say(lang: str, en: str, pt: str) -> str:
    return pt if lang == "pt" else en


def _fail(lang: str, en: str, pt: str):
    """A refusal in the person's language (the sentence is chosen here, not looked up in the catalog)."""
    raise ValueError(pt if lang == "pt" else en)


def _hermes_cmd(home: Path, rest: str) -> str:
    """`hermes [-p profile] <rest>`, as the owner types it in this profile."""
    name = _profile_name(home)
    return f"hermes -p {name} {rest}"   # explicit even for default: `hermes profile use` makes another one sticky


_H = {
    "en": {
        "attention": "Needs attention", "nothing": "Nothing needs attention.", "yes": "yes", "no": "no",
        "more": "For scripts: add `--format json`.",
        # care
        "care_title": "Hermes health · profile `{profile}`", "gateway": "Gateway", "db": "Session database",
        "disk": "Disk", "free": "{free} free of {total}", "pace": "{per_day} less each day: full in about {days} days",
        "memory": "Memory", "cpu": "Processors",
        "load": "load {load} on {cpus} CPUs over 15 minutes", "folders": "Largest folders",
        "unused": "Old tool versions nothing uses", "checked": "Checked {when}.",
        "care_run": "What you can run (each one asks first)",
        "care_actions": {"pm-doctor": "checks Hermes' installed files (offline)",
                         "doctor": "the full Hermes doctor; it also calls your providers' APIs, npm's registry and GitHub, and takes minutes",
                         "fix": "`hermes doctor --fix`, after a quick backup Hermes can restore",
                         "gc": "removes tool versions nothing uses"},
        "ran_ok": "`{command}` finished.", "ran_code": "`{command}` ended with code {code}.",
        "backup_failed": "The backup did not work (code {code}), so `hermes doctor --fix` was not run.",
        "doctor_clean": "Hermes doctor found no problem.",
        "doctor_found": ("Hermes doctor found 1 problem.", "Hermes doctor found {n} problems."),
        "doctor_todo": "What Hermes suggests", "doctor_listed": "What it listed", "tail": "Last lines it printed",
        "fixed": "`hermes doctor --fix` finished.",
        # environment
        "scanned": "Looked at every profile now.",
        "catalog_title": "Hermes plugin catalog", "catalog_tiers": "Plugins by tier",
        "catalog_none": "The catalog has not been read yet: it is read in the next learning pass of the profile that "
                        "looks after the whole Hermes.",
        "catalog_removed": "Removed from the catalog", "tiers": {"official": "official", "verified": "verified",
                                                                 "community": "community"},
        # decisions
        "decisions_title": "Your decisions", "decisions_none": "No decision recorded yet.",
        "where": {"terminal": "terminal", "dashboard": "panel", "automatic": "automatic"},
        "did": {"approve": "approved", "reject": "rejected", "retire": "retired", "reactivate": "reactivated",
                "apply": "applied", "set": "changed", "undo": "undid", "dedupe": "removed a duplicate memory entry"},
        "what_lesson": "{did} lesson #{target}", "what_proposal": "{did} proposal #{target}",
        "what_cron": "scheduled `{target}` ({schedule}) as a Hermes task",
        "what_setting": "changed `{target}`: {before} → {after}",
        "what_memory": "removed a duplicate memory entry from `{target}` (a copy of the file was kept first)",
        "what_maintenance": "ran Hermes' own maintenance: `hermes {target}` (nothing to undo)",
        "what_sync": "brought Atlas Evolution in profile `{profile}` to the lead's code ({before} → {after}){failed}",
        "sync_failed": "; Hermes refused it, tried again tomorrow",
        "what_gateway": ("pointed the gateway's service `{unit}` at the Python Hermes uses now (`{before}` → "
                         "`{after}`){failed}; undo: `hermes evolution care undo-gateway`"),
        "gateway_failed": " (the gateway did not come up)",
        "undone_mark": "undone", "hidden": "(text hidden: read it in your own terminal)",
        "undo_hint": "Undo one with `{command}`.",
        "undo_done": "Decision #{id} undone.", "undo_not": "Decision #{id} was not undone: {reason}",
        "undo_reasons": {"changed_since": "it changed since (a later decision or a pass); nothing changed.",
                         "nothing_to_undo": "there is nothing to undo for this one (Hermes' own maintenance).",
                         "already_undone": "it was already undone.", "unknown": "there is no decision with that number.",
                         "not_undoable": "this kind of decision cannot be undone."},
        "undo_run": "Hermes' configuration is yours to change: run this in your own terminal to put it back:",
        "agent_tools_restart": "Turning the agent's tools on applies after `hermes gateway restart` (in the terminal, "
                               "at the next session); turning them off applies within a minute.",
        "applies_soon": "A running gateway picks it up by itself, usually within a minute; no restart is needed.",
        # lessons
        "lessons_title": "Lessons", "lessons_mode": "**Lessons:** {mode}",
        "lesson_modes": {"auto": ("automatic", "built-in templates apply by themselves; the rest waits for you"),
                         "suggest": ("on approval", "every lesson waits for you"),
                         "off": ("off", "measured only; nothing is shown to the agent")},
        "lessons_none": "No lesson yet.",
        "status_names": {"active": "Active", "candidate": "Awaiting approval", "quarantined": "Awaiting approval",
                         "retired": "Retired", "withdrawn": "Dropped from the queue without a decision"},
        "origins": {"derived": "built-in template", "operator": "you", "reflector": "reflector", "agent": "agent"},
        "seen": ("seen once in {s}", "seen {n} times in {s}"), "sessions": ("1 session", "{n} sessions"),
        "verdicts": {"effective": "it helped", "ineffective": "it did not help", "inconclusive": "inconclusive",
                     "withdrawn": "dropped from the queue without a decision"},
        "rates": "failure rate {before} → {after}",
        "lessons_decide": "Decide on the waiting ones with `{review}` (one by one, in your own terminal), or "
                          "`{approve}` / `{retire}`.",
        "approved": "Lesson #{id} is active now: the agent reads it from its next session.",
        "reactivated": "Lesson #{id} is active again.",
        "retired_now": "Lesson #{id} is retired: the agent no longer sees it.",
        "rejected": "Lesson #{id} was rejected: it will not be shown to the agent.",
        "already_active": "Lesson #{id} is already active; nothing changed.",
        "already_retired": "Lesson #{id} is already retired; nothing changed.",
        "kept_retired": "Lesson #{id} had been dropped from the queue without a decision; now it stays retired and does "
                        "not come back when its failure repeats.",
        "not_changed": "Lesson #{id} is {status}; nothing changed.",
        "added": "Lesson #{id} added and active now.", "added_waiting": "Lesson #{id} added: it waits for your approval "
                                                                          "(`{command}`).",
        "review_done": "Approved: {approved} · rejected: {rejected} · skipped: {skipped}.",
        "all_done": ("1 lesson approved.", "{n} lessons approved."), "all_none": "No lesson was waiting.",
        "all_left": "Left for a decision of their own (the agent proposed them): {ids}.",
        "all_skipped": "Not approved because they changed or were decided meanwhile: {ids}.",
        "written": ("1 lesson written to `{path}`.", "{n} lessons written to `{path}`."),
        "import_plan": "From {source}", "import_add": "To import", "import_skip": "Already here or turned down",
        "import_done": ("1 lesson imported.", "{n} lessons imported."), "import_dry": "Nothing was imported (--dry-run).",
        "import_none": "Nothing new to import.",
        "kinds": {"lesson": "lesson"},
        # proposals
        "proposals_title": "Proposals", "proposals_all": "Proposals (all)", "proposals_none": "Nothing is waiting for you.",
        "risk": "risk", "seen_label": "Seen", "advice_label": "What to do", "does_label": "If approved",
        "changes_label": "What it changes",
        "proposal": "Proposal", "lesson_title": "Activate lesson #{id}", "retire_title": "Retire lesson #{id}",
        "lesson_on": "If approved: the lesson becomes active and the agent reads it at the start of every session.",
        "lesson_retire": "If approved: the lesson is retired and the agent stops reading it.",
        "write_lesson": "write a lesson with `{command}`, or let the reflector draft one when the failure happens in "
                        "your own turns",
        "undo_label": "Undo", "command_label": "Command", "state_label": "state",
        "states": {"open": "open", "approved": "approved, waiting for you to run it", "applying": "being applied",
                   "applied": "applied", "rejected": "rejected", "expired": "expired"},
        "already": {"applied": "Proposal {id} is already applied; nothing changed.",
                    "rejected": "Proposal {id} was already rejected; nothing changed.",
                    "expired": "Proposal {id} expired (the situation is gone); nothing changed.",
                    "applying": "Proposal {id} is being applied right now; nothing changed."},
        "proposals_decide": "Decide on each one in your own terminal with `{approve}` or `{reject}`; lessons can also "
                            "be decided one by one with `{review}`.",
        "p_applied": "Proposal #{id} applied.", "p_undo": "To undo: {undo} (or `{command}`).",
        "p_not_applied": {"not_open": "Proposal #{id} was not applied: it was decided meanwhile.",
                          "already": "Proposal #{id}: nothing to do, it was already so; it is marked as applied."},
        "p_approved": "Proposal #{id} approved. Run this in your own terminal to apply it:",
        "p_lesson_on": "Proposal #{id} approved: its lesson is active now; the agent reads it from its next session.",
        "p_lesson_off": "Proposal #{id} approved: its lesson is retired now.",
        "p_approved_plain": "Proposal #{id} approved.",
        "p_rejected": "Proposal #{id} rejected: hidden for 30 days; it comes back only if the situation is still there.",
        # learn
        "learn_title": "Learning pass", "learn_mode": "{mode}: {did}",
        "modes": {"full": ("Auto mode", "acts by itself in every profile"),
                  "safe": ("Assisted", "on its own it only removes duplicate memory entries; everything else waits "
                                       "for you"),
                  "off": ("Manual", "only measures and proposes")},
        "history": "Hermes history", "history_states": {"done": "read", "running": "being read",
                                                        "waiting": "waiting for the first learning pass",
                                                        "no_history": "no history", "unreadable": "unreadable",
                                                        "unsupported_history": "unsupported format",
                                                        "damaged_history": "Hermes' history is damaged; replay ended"},
        "history_counts": "{sessions} sessions · {tool_calls} tool calls",
        "new_lessons": "New lessons", "drafts": "Reflector drafts to read", "regressions": "Changes that made things worse",
        "retired_lessons": "Lessons retired because they did not help", "risky": "High-risk proposals",
        "automatic": "Done by itself", "automatic_failed": "Tried by itself and failed",
        "memory_done": ("1 duplicate memory entry removed (a copy of the file was kept first)",
                        "{n} duplicate memory entries removed (a copy of the file was kept first)"),
        "open": ("1 proposal waits for you", "{n} proposals wait for you"),
        "reflector": "Reflector", "reflector_states": {"off": "off", "lessons_off": "off (lessons are off)",
                                                       "unavailable_without_hermes_llm": "not available outside Hermes",
                                                       "ran": "ran", "limit": "daily limit reached",
                                                       "nothing": "nothing to read"},
        "nothing_new": "Nothing new in this learning pass.", "see_proposals": "See them with `{command}`.",
        "verdict_label": "verdict", "pass_done": "Pass finished on {when}.",
        "risky_lesson": "Proposal #{id}: {who} for `{tool}` · decide with `{command}`",
        "risky_other": "Proposal #{id}: {title} · {subject} · see it with `{command}`",
        "who": {"agent": "lesson written by the agent", "reflector": "reflector draft", "operator": "your lesson",
                "derived": "built-in template lesson"},
        # skills
        "skills_title": "Skills · last {days} days", "tracked": "Skills Hermes tracks", "rewrites": "Rewrites",
        "by_review": "{n} by the automatic review", "rewritten": ("1 skill rewritten", "{n} skills rewritten"),
        "top": "Rewritten the most", "flipflops": ("1 flip-flop", "{n} flip-flops"), "uses": ("1 use", "{n} uses"),
        "unreused": ("1 rewrite never used since", "{n} rewrites never used since"),
        "skill_findings": "Worth a look",
        "skill_codes": {"skill_churn": "`{skill}` keeps being rewritten ({rewrites} rewrites, {flipflops} flip-flops)",
                        "skill_unexercised_rewrites": "`{skill}` was rewritten {unreused_rewrites} times and not used since",
                        "skill_never_used": "`{skill}` was created by the agent {age_days} days ago and never used",
                        "skill_underperforming": "`{skill}`: its turns go worse than the profile's ({worse})"},
        "outcomes": "Turns that loaded each skill (last {days} days)", "outcomes_none": "Not enough turns with a loaded "
                                                                                        "skill yet (at least {n}).",
        "whole": "whole profile", "turns": ("1 turn", "{n} turns"), "failed_pct": "{v}% failed",
        "corrected_pct": "{v}% corrected", "basis_skills": "Read from Hermes' skill usage records and the Curator's "
                                                           "ledger; nothing of the skills' text is stored.",
        # experiments
        "experiments_title": "Changes and their measured effect", "experiments_none": "No change tracked yet.",
        "change_kinds": {"lesson": "lesson", "skill": "skill", "config": "setting"},
        "change_verdicts": {"improved": "improved", "regressed": "got worse", "neutral": "no material change",
                            "mixed": "mixed", "insufficient_data": "not enough data yet", None: "being measured"},
        "final": "final", "provisional": "provisional",
        # system
        "system_title": "Operating system", "unsupported": "This system is not one the plugin updates (Linux with apt only).",
        "kernel": "kernel {k}", "updates": "Updates waiting", "updates_line": "{n} ({s} security) · lists refreshed {when}",
        "reboot": "Reboot waiting", "auto_updates": "Automatic security updates", "pm": "Package manager",
        "pm_ok": "ok", "pm_busy": "busy: {what}", "pm_broken": "needs a repair", "units": "Hermes services",
        "unit_ok": "starts at boot", "unit_off": "does not start at boot", "linger": "user services come back after a reboot",
        "ready": "Ready for an update", "ready_yes": "yes", "ready_no": "not now ({why})",
        "system_codes": {"package_manager_busy": "a package manager is working", "package_manager_stuck":
                         "a package manager looks stuck", "dpkg_interrupted": "an update stopped half-way",
                         "apt_broken": "broken dependencies", "held_by_plugin": "packages held by a stopped update",
                         "disk_low": "little free disk", "reboot_required": "a reboot is waiting",
                         "security_updates": "security updates waiting", "unattended_off": "automatic updates off",
                         "lists_stale": "package lists older than 7 days", "unit_not_enabled": "a Hermes service does "
                                                                                                 "not start at boot",
                         "user_units_without_linger": "user services stop after a reboot", "unsupported": "unsupported"},
        "system_run": "What you can run",
        "system_actions": {"plan": "refreshes the package lists and shows what an update would change",
                           "upgrade": "updates the system (a list of every package and a copy of /etc are kept first)",
                           "repair": "finishes an update that stopped half-way", "report": "the report of the last update"},
        "plan_title": "What an update would change", "plan_error": "The package manager refused: {error}",
        "plan_counts": "{upgrade} to update · {install} new · {remove} to remove · {kept} kept back",
        "plan_new": ("{n}", "{n}"), "plan_kept": ("{n}", "{n}"),
        "plan_attention": "Worth knowing", "plan_none": "Nothing to update.",
        "attention_codes": {"restarts_containers": "restarts the containers (Docker)", "needs_reboot": "needs a reboot",
                            "restarts_web": "restarts the web server", "remote_access": "remote access (SSH/sudo)",
                            "boot": "the boot loader", "package_manager": "the package manager itself"},
        "report_none": "No update was run by the plugin yet.", "report_running": "An update is running now.",
        "report_title": "Last update", "report_line": "exit code {exit} · {changed} · package manager {pm} · Hermes {hermes} · reboot {reboot}",
        "changed": ("1 package changed", "{n} packages changed"), "folder": "Kept in `{folder}`.",
        "stopped_units": "Services that stopped", "go_back": "To go back to the old version of a package",
        "repair_ok": "The repair finished: the package manager is in order.", "repair_bad": "The repair ended with code {code}; "
                                                                                           "look at `{folder}`.",
        "released": "Packages released: {names}.", "lost": "The run was interrupted before it wrote its result.",
        "nothing_to_do": "Nothing to update.",
        "upgrade_started": "Update finished.",
    },
    "pt": {
        "attention": "Precisa de atenção", "nothing": "Nada precisa de atenção.", "yes": "sim", "no": "não",
        "more": "Para scripts: acrescente `--format json`.",
        "care_title": "Saúde do Hermes · perfil `{profile}`", "gateway": "Gateway", "db": "Banco de sessões",
        "disk": "Disco", "free": "{free} livres de {total}", "pace": "{per_day} a menos por dia: enche em cerca de {days} dias",
        "memory": "Memória", "cpu": "Processadores",
        "load": "carga {load} em {cpus} CPUs nos últimos 15 minutos", "folders": "Pastas que mais ocupam",
        "unused": "Versões antigas de ferramentas que nada usa", "checked": "Conferido em {when}.",
        "care_run": "O que você pode rodar (cada um pergunta antes)",
        "care_actions": {"pm-doctor": "confere os arquivos instalados do Hermes (sem rede)",
                         "doctor": "a checagem completa do Hermes (`hermes doctor`); também consulta as APIs dos seus provedores, o registro do npm e o GitHub, e leva minutos",
                         "fix": "`hermes doctor --fix`, depois de um backup rápido que o Hermes sabe restaurar",
                         "gc": "remove versões de ferramentas que nada usa"},
        "ran_ok": "`{command}` terminou.", "ran_code": "`{command}` terminou com o código {code}.",
        "backup_failed": "O backup não deu certo (código {code}), então o `hermes doctor --fix` não rodou.",
        "doctor_clean": "A checagem do Hermes não encontrou nenhum problema.",
        "doctor_found": ("A checagem do Hermes encontrou 1 problema.", "A checagem do Hermes encontrou {n} problemas."),
        "doctor_todo": "O que o Hermes sugere (texto do Hermes, em inglês)",
        "doctor_listed": "O que ela apontou (texto do Hermes, em inglês)",
        "tail": "Últimas linhas que ele mostrou (texto do Hermes, em inglês)",
        "fixed": "O `hermes doctor --fix` terminou.",
        "scanned": "Todos os perfis foram olhados agora.",
        "catalog_title": "Catálogo de plugins do Hermes", "catalog_tiers": "Plugins por categoria",
        "catalog_none": "O catálogo ainda não foi lido: ele é lido na próxima rodada de aprendizado do perfil que cuida "
                        "do Hermes inteiro.",
        "catalog_removed": "Removidos do catálogo", "tiers": {"official": "oficiais", "verified": "verificados",
                                                             "community": "da comunidade"},
        "decisions_title": "Suas decisões", "decisions_none": "Nenhuma decisão registrada ainda.",
        "where": {"terminal": "terminal", "dashboard": "painel", "automatic": "automático"},
        "did": {"approve": "aprovou", "reject": "rejeitou", "retire": "aposentou", "reactivate": "reativou",
                "apply": "aplicou", "set": "mudou", "undo": "desfez", "dedupe": "removeu uma entrada repetida da memória"},
        "what_lesson": "{did} a lição #{target}", "what_proposal": "{did} a proposta #{target}",
        "what_cron": "agendou `{target}` ({schedule}) como tarefa do Hermes",
        "what_setting": "mudou `{target}`: {before} → {after}",
        "what_memory": "removeu uma entrada repetida da memória `{target}` (guardou uma cópia do arquivo antes)",
        "what_maintenance": "rodou a manutenção do próprio Hermes: `hermes {target}` (nada a desfazer)",
        "what_sync": "levou o Atlas Evolution do perfil `{profile}` ao código do perfil que cuida de todos ({before} → {after}){failed}",
        "sync_failed": "; o Hermes recusou, nova tentativa amanhã",
        "what_gateway": ("apontou o serviço do gateway `{unit}` para o Python que o Hermes usa agora (`{before}` "
                         "→ `{after}`){failed}; desfazer: `hermes evolution care undo-gateway`"),
        "gateway_failed": " (o gateway não subiu)",
        "undone_mark": "desfeita", "hidden": "(texto oculto: leia no seu próprio terminal)",
        "undo_hint": "Para desfazer uma: `{command}`.",
        "undo_done": "Decisão #{id} desfeita.", "undo_not": "A decisão #{id} não foi desfeita: {reason}",
        "undo_reasons": {"changed_since": "mudou depois (outra decisão ou uma rodada); nada mudou.",
                         "nothing_to_undo": "não há nada a desfazer nesta (manutenção do próprio Hermes).",
                         "already_undone": "ela já tinha sido desfeita.", "unknown": "não há decisão com esse número.",
                         "not_undoable": "esse tipo de decisão não se desfaz."},
        "undo_run": "A configuração do Hermes é sua: rode isto no seu próprio terminal para voltar ao valor anterior:",
        "agent_tools_restart": "Ligar as ferramentas do agente vale depois de `hermes gateway restart` (no terminal, na "
                               "próxima sessão); desligar vale em até um minuto.",
        "applies_soon": "Um gateway em execução pega a mudança sozinho, em geral em até um minuto; não precisa reiniciar.",
        "lessons_title": "Lições", "lessons_mode": "**Lições:** {mode}",
        "lesson_modes": {"auto": ("automáticas", "padrões fixos valem sozinhos; o resto espera você"),
                         "suggest": ("com aprovação", "toda lição espera você"),
                         "off": ("desligadas", "só medem; nada é mostrado ao agente")},
        "lessons_none": "Nenhuma lição ainda.",
        "status_names": {"active": "Ativas", "candidate": "Aguardando aprovação", "quarantined": "Aguardando aprovação",
                         "retired": "Aposentadas", "withdrawn": "Descartadas sem decisão (fila limpa)"},
        "origins": {"derived": "padrão fixo", "operator": "você", "reflector": "refletor", "agent": "agente"},
        "seen": ("vista 1 vez em {s}", "vista {n} vezes em {s}"), "sessions": ("1 sessão", "{n} sessões"),
        "verdicts": {"effective": "ajudou", "ineffective": "não ajudou", "inconclusive": "inconclusivo",
                     "withdrawn": "descartada sem decisão (fila limpa)"},
        "rates": "taxa de falha {before} → {after}",
        "lessons_decide": "Decida as que esperam com `{review}` (uma a uma, no seu próprio terminal), ou "
                          "`{approve}` / `{retire}`.",
        "approved": "A lição #{id} está ativa agora: o agente passa a lê-la a partir da próxima sessão.",
        "reactivated": "A lição #{id} está ativa de novo.",
        "retired_now": "A lição #{id} foi aposentada: o agente não a vê mais.",
        "rejected": "A lição #{id} foi rejeitada: ela não será mostrada ao agente.",
        "already_active": "A lição #{id} já está ativa; nada mudou.",
        "already_retired": "A lição #{id} já está aposentada; nada mudou.",
        "kept_retired": "A lição #{id} tinha sido descartada sem decisão (fila limpa); agora fica aposentada e não "
                        "volta quando a falha se repetir.",
        "not_changed": "A lição #{id} está {status}; nada mudou.",
        "added": "Lição #{id} adicionada e já ativa.", "added_waiting": "Lição #{id} adicionada: ela espera a sua "
                                                                         "aprovação (`{command}`).",
        "review_done": "Aprovadas: {approved} · rejeitadas: {rejected} · puladas: {skipped}.",
        "all_done": ("1 lição aprovada.", "{n} lições aprovadas."), "all_none": "Nenhuma lição estava esperando.",
        "all_left": "Ficaram para uma decisão própria (o agente as propôs): {ids}.",
        "all_skipped": "Não aprovadas porque mudaram ou foram decididas nesse meio-tempo: {ids}.",
        "written": ("1 lição gravada em `{path}`.", "{n} lições gravadas em `{path}`."),
        "import_plan": "De {source}", "import_add": "Para importar", "import_skip": "Já estão aqui ou foram recusadas",
        "import_done": ("1 lição importada.", "{n} lições importadas."), "import_dry": "Nada foi importado (--dry-run).",
        "import_none": "Nada novo para importar.",
        "kinds": {"lesson": "lição"},
        "proposals_title": "Propostas", "proposals_all": "Propostas (todas)", "proposals_none": "Nada aguardando você.",
        "risk": "risco", "seen_label": "Visto", "advice_label": "O que fazer", "does_label": "Se aprovar",
        "changes_label": "O que muda",
        "proposal": "Proposta", "lesson_title": "Ativar lição #{id}", "retire_title": "Aposentar lição #{id}",
        "lesson_on": "Se aprovar: a lição fica ativa e o agente passa a lê-la no começo de cada sessão.",
        "lesson_retire": "Se aprovar: a lição é aposentada e o agente deixa de lê-la.",
        "write_lesson": "escreva uma lição com `{command}`, ou deixe o refletor rascunhar uma quando a falha acontecer "
                        "nos seus turnos",
        "undo_label": "Desfazer", "command_label": "Comando", "state_label": "situação",
        "states": {"open": "aberta", "approved": "aprovada, esperando você rodar", "applying": "sendo aplicada",
                   "applied": "aplicada", "rejected": "rejeitada", "expired": "expirada"},
        "already": {"applied": "A proposta {id} já foi aplicada; nada mudou.",
                    "rejected": "A proposta {id} já foi rejeitada; nada mudou.",
                    "expired": "A proposta {id} expirou (a situação acabou); nada mudou.",
                    "applying": "A proposta {id} está sendo aplicada agora; nada mudou."},
        "proposals_decide": "Decida cada uma no seu próprio terminal com `{approve}` ou `{reject}`; as lições também "
                            "se decidem uma a uma com `{review}`.",
        "p_applied": "Proposta #{id} aplicada.", "p_undo": "Para desfazer: {undo} (ou `{command}`).",
        "p_not_applied": {"not_open": "A proposta #{id} não foi aplicada: ela foi decidida nesse meio-tempo.",
                          "already": "Proposta #{id}: nada a fazer, já estava assim; ficou marcada como aplicada."},
        "p_approved": "Proposta #{id} aprovada. Rode isto no seu próprio terminal para aplicá-la:",
        "p_lesson_on": "Proposta #{id} aprovada: a lição dela está ativa agora; o agente passa a lê-la a partir da próxima sessão.",
        "p_lesson_off": "Proposta #{id} aprovada: a lição dela foi aposentada.",
        "p_approved_plain": "Proposta #{id} aprovada.",
        "p_rejected": "Proposta #{id} rejeitada: some por 30 dias e só volta se a situação continuar.",
        "learn_title": "Rodada de aprendizado", "learn_mode": "{mode}: {did}",
        "modes": {"full": ("Modo Auto", "age sozinho em todos os perfis"),
                  "safe": ("Modo Assistido", "sozinho, só remove entradas repetidas da memória; o resto espera você"),
                  "off": ("Modo Manual", "só mede e propõe")},
        "history": "Histórico do Hermes", "history_states": {"done": "lido", "running": "em leitura",
                                                             "waiting": "aguardando a primeira rodada",
                                                             "no_history": "sem histórico", "unreadable": "ilegível",
                                                             "unsupported_history": "formato não suportado",
                                                             "damaged_history": "o histórico do Hermes está danificado; replay encerrado"},
        "history_counts": "{sessions} sessões · {tool_calls} chamadas de ferramentas",
        "new_lessons": "Lições novas", "drafts": "Rascunhos do refletor para ler", "regressions": "Mudanças que pioraram",
        "retired_lessons": "Lições aposentadas porque não ajudaram", "risky": "Propostas de risco alto",
        "automatic": "Feito sozinho", "automatic_failed": "Tentado sozinho e falhou",
        "memory_done": ("1 entrada repetida da memória removida (uma cópia do arquivo foi guardada antes)",
                        "{n} entradas repetidas da memória removidas (uma cópia do arquivo foi guardada antes)"),
        "open": ("1 proposta espera você", "{n} propostas esperam você"),
        "reflector": "Refletor", "reflector_states": {"off": "desligado", "lessons_off": "desligado (as lições estão desligadas)",
                                                      "unavailable_without_hermes_llm": "indisponível fora do Hermes",
                                                      "ran": "rodou", "limit": "limite diário atingido",
                                                      "nothing": "nada para ler"},
        "nothing_new": "Nada novo nesta rodada.", "see_proposals": "Veja com `{command}`.",
        "verdict_label": "veredito", "pass_done": "Rodada concluída em {when}.",
        "risky_lesson": "Proposta #{id}: {who} para `{tool}` · decida com `{command}`",
        "risky_other": "Proposta #{id}: {title} · {subject} · veja com `{command}`",
        "who": {"agent": "lição escrita pelo agente", "reflector": "rascunho do refletor", "operator": "lição sua",
                "derived": "lição de padrão fixo"},
        "skills_title": "Skills · últimos {days} dias", "tracked": "Skills que o Hermes acompanha", "rewrites": "Reescritas",
        "by_review": "{n} pela revisão automática", "rewritten": ("1 skill reescrita", "{n} skills reescritas"),
        "top": "As mais reescritas", "flipflops": ("1 vai-e-volta", "{n} vai-e-voltas"), "uses": ("1 uso", "{n} usos"),
        "unreused": ("1 reescrita nunca usada depois", "{n} reescritas nunca usadas depois"),
        "skill_findings": "Vale olhar",
        "skill_codes": {"skill_churn": "`{skill}` é reescrita sem parar ({rewrites} reescritas, {flipflops} vai-e-voltas)",
                        "skill_unexercised_rewrites": "`{skill}` foi reescrita {unreused_rewrites} vezes e não foi usada depois",
                        "skill_never_used": "`{skill}` foi criada pelo agente há {age_days} dias e nunca foi usada",
                        "skill_underperforming": "`{skill}`: os turnos com ela vão pior que o perfil ({worse})"},
        "outcomes": "Turnos que carregaram cada skill (últimos {days} dias)",
        "outcomes_none": "Ainda não há turnos suficientes com skill carregada (pelo menos {n}).",
        "whole": "perfil inteiro", "turns": ("1 turno", "{n} turnos"), "failed_pct": "{v}% falharam",
        "corrected_pct": "{v}% corrigidos", "basis_skills": "Lido dos registros de uso de skills do Hermes e do livro do "
                                                            "Curator; nada do texto das skills é guardado.",
        "experiments_title": "Mudanças e o efeito medido", "experiments_none": "Nenhuma mudança acompanhada ainda.",
        "change_kinds": {"lesson": "lição", "skill": "skill", "config": "configuração"},
        "change_verdicts": {"improved": "melhorou", "regressed": "piorou", "neutral": "sem mudança relevante",
                            "mixed": "misto", "insufficient_data": "dados insuficientes ainda", None: "em medição"},
        "final": "final", "provisional": "provisório",
        "system_title": "Sistema operacional", "unsupported": "Este sistema não é um que o plugin atualiza (só Linux com apt).",
        "kernel": "kernel {k}", "updates": "Atualizações esperando",
        "updates_line": "{n} ({s} de segurança) · listas atualizadas em {when}",
        "reboot": "Reinício pendente", "auto_updates": "Atualizações automáticas de segurança", "pm": "Gerenciador de pacotes",
        "pm_ok": "ok", "pm_busy": "ocupado: {what}", "pm_broken": "precisa de reparo", "units": "Serviços do Hermes",
        "unit_ok": "sobe na inicialização", "unit_off": "não sobe na inicialização",
        "linger": "serviços do usuário voltam depois de reiniciar",
        "ready": "Pronto para atualizar", "ready_yes": "sim", "ready_no": "agora não ({why})",
        "system_codes": {"package_manager_busy": "um gerenciador de pacotes está trabalhando",
                         "package_manager_stuck": "um gerenciador de pacotes parece travado",
                         "dpkg_interrupted": "uma atualização parou no meio", "apt_broken": "dependências quebradas",
                         "held_by_plugin": "pacotes retidos por uma atualização interrompida",
                         "disk_low": "pouco espaço em disco", "reboot_required": "um reinício está pendente",
                         "security_updates": "atualizações de segurança esperando",
                         "unattended_off": "atualizações automáticas desligadas",
                         "lists_stale": "listas de pacotes com mais de 7 dias",
                         "unit_not_enabled": "um serviço do Hermes não sobe na inicialização",
                         "user_units_without_linger": "serviços do usuário param depois de reiniciar",
                         "unsupported": "não suportado"},
        "system_run": "O que você pode rodar",
        "system_actions": {"plan": "atualiza as listas de pacotes e mostra o que uma atualização mudaria",
                           "upgrade": "atualiza o sistema (antes guarda a lista de todos os pacotes e uma cópia do /etc)",
                           "repair": "termina uma atualização que parou no meio", "report": "o relatório da última atualização"},
        "plan_title": "O que uma atualização mudaria", "plan_error": "O gerenciador de pacotes recusou: {error}",
        "plan_counts": "{upgrade} para atualizar · {install} · {remove} para remover · {kept}",
        "plan_new": ("1 novo", "{n} novos"), "plan_kept": ("1 retido", "{n} retidos"),
        "plan_attention": "Bom saber", "plan_none": "Nada para atualizar.",
        "attention_codes": {"restarts_containers": "reinicia os containers (Docker)", "needs_reboot": "pede reinício",
                            "restarts_web": "reinicia o servidor web", "remote_access": "acesso remoto (SSH/sudo)",
                            "boot": "o carregador de inicialização (boot)", "package_manager": "o próprio gerenciador de pacotes"},
        "report_none": "O plugin ainda não rodou nenhuma atualização.", "report_running": "Uma atualização está rodando agora.",
        "report_title": "Última atualização",
        "report_line": "código de saída {exit} · {changed} · gerenciador de pacotes {pm} · Hermes {hermes} · reinício {reboot}",
        "changed": ("1 pacote mudou", "{n} pacotes mudaram"), "folder": "Guardado em `{folder}`.",
        "stopped_units": "Serviços que pararam", "go_back": "Para voltar à versão antiga de um pacote",
        "repair_ok": "O reparo terminou: o gerenciador de pacotes está em ordem.",
        "repair_bad": "O reparo terminou com o código {code}; veja `{folder}`.",
        "released": "Pacotes liberados: {names}.", "lost": "A execução foi interrompida antes de gravar o resultado.",
        "nothing_to_do": "Nada para atualizar.",
        "upgrade_started": "Atualização concluída.",
    },
}


def _p(entry, count, **values) -> str:
    """A text of the tables with its count: a (singular, plural) pair, or one form for both."""
    form = (entry[0] if count == 1 else entry[1]) if isinstance(entry, tuple) else entry
    return form.format(n=count, **values)


def _num(value, lang: str) -> str:
    """A count or a percentage the way the person writes it (1.234 and 12,5 in Portuguese)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "?" if value is None else str(value)
    text = f"{value:,}" if isinstance(value, int) else f"{value:,.1f}"
    return text.replace(",", "X").replace(".", ",").replace("X", ".") if lang == "pt" else text


def _value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value) if value else "—"
    return "—" if value is None else str(value)


def _care_md(found: dict | None, home: Path, lang: str) -> str:
    from .status import _TEXT, _care_values, database_words, gateway_words, human_size, local_time, memory_words

    t, texts = _H[lang], _TEXT[lang]
    found = found or {}
    db, memory = found.get("database") or {}, found.get("memory") or {}
    lines = ["# " + t["care_title"].format(profile=_profile_name(home)), ""]
    lines.append(f"- **{t['gateway']}:** " + gateway_words(found.get("gateway") or {}, lang, beat=True))
    lines.append(f"- **{t['db']}:** " + database_words(db, lang))
    if found.get("free") is not None:
        line = f"- **{t['disk']}:** " + t["free"].format(free=human_size(found["free"]), total=human_size(found.get("total")))
        if (found.get("disk_per_day") or 0) > 0 and found.get("days_to_full"):
            line += " · " + t["pace"].format(per_day=human_size(found["disk_per_day"]), days=_num(found["days_to_full"], lang))
        lines.append(line)
    if memory.get("total"):
        lines.append(f"- **{t['memory']}:** " + memory_words(memory, lang))
    load = found.get("load") or {}
    if load.get("cpus"):
        lines.append(f"- **{t['cpu']}:** " + t["load"].format(load=_num(load.get("15m"), lang), cpus=load["cpus"]))
    biggest = sorted(((name, size) for name, size in (found.get("sizes") or {}).items() if isinstance(size, int) and size),
                     key=lambda item: -item[1])[:6]
    if biggest:
        lines.append(f"- **{t['folders']}:** " + " · ".join(f"`{name}` {human_size(size)}" for name, size in biggest))
    if found.get("unused_tools"):
        lines.append(f"- **{t['unused']}:** {human_size(found.get('reclaimable'))} (" + ", ".join(
            f"`{item['folder']}`" for item in found["unused_tools"][:8]) + ")")
    values = _care_values({"care": found, "system": {}})
    codes = [f"hermes_{code}" for code in found.get("findings") or () if f"hermes_{code}" in texts]
    lines += ["", f"## {t['attention']}", ""]
    lines += [f"- {texts[code].format(**values)}" for code in codes] or [t["nothing"]]
    if found.get("at"):
        lines += ["", t["checked"].format(when=local_time(found["at"], lang))]
    lines += ["", f"## {t['care_run']}", ""]
    for action, does in t["care_actions"].items():
        lines.append(f"- `{_hermes_cmd(home, 'evolution care ' + action)}`: {does}")
    return "\n".join(lines) + "\n"


def _care_action_md(result: dict, task: str, lang: str) -> str:
    t = _H[lang]
    if task in ("fix-gateway", "undo-gateway"):
        pt = lang == "pt"
        if result.get("reason") == "nothing_to_fix" and result.get("leftover"):
            name = result["leftover"]
            return (f"Outro serviço roda o gateway; `{name}` é uma sobra desligada: nada a reparar (remova-a se "
                    "quiser).\n" if pt else f"Another service runs the gateway; `{name}` is a leftover that is off: "
                                             "nothing to repair (remove it if you like).\n")
        if result.get("reason") == "nothing_to_fix":
            return ("O serviço do gateway já aponta para um programa que existe: nada a reparar.\n" if pt else
                    "The gateway service already points at a program that exists: nothing to repair.\n")
        if task == "undo-gateway":
            return (f"{'Restaurado' if pt else 'Restored'}: {result.get('file')}\n")
        if result.get("reload_only"):   # the file was right already: systemd had not read it again
            head = (("Gateway ativo: o arquivo já estava certo, o systemd só precisava relê-lo." if pt else
                     "Gateway active: the file was right already, systemd only had to read it again.")
                    if result.get("ok") else
                    ("O systemd releu o arquivo, mas o gateway não ficou ativo." if pt else
                     "systemd read the file again, but the gateway is not active."))
            lines = [head, "", f"- {result.get('file')}"]
            if not result.get("ok"):
                lines.append("- `journalctl --user -u hermes-gateway -n 30`")
            return "\n".join(lines) + "\n"
        head = (("Gateway reparado e ativo." if pt else "Gateway repaired and active.") if result.get("ok") else
                ("Arquivo corrigido, mas o gateway não ficou ativo." if pt else "File fixed, but the gateway is not active."))
        lines = [head, "", f"- {result.get('file')}: `{result.get('from')}` -> `{result.get('to')}`",
                 f"- {'Cópia' if pt else 'Backup'}: {result.get('backup')}",
                 f"- {'Desfazer' if pt else 'Undo'}: `hermes evolution care undo-gateway`"]
        if not result.get("ok"):
            lines.append("- `journalctl --user -u hermes-gateway -n 30`")
        return "\n".join(lines) + "\n"
    steps = result.get("steps") or []
    last = steps[-1] if steps else {}
    if result.get("reason") == "backup_failed":
        return t["backup_failed"].format(code=last.get("exit")) + "\n"
    lines = []
    doctor = result.get("doctor")
    if doctor is not None:   # `hermes doctor` ends with 1 when it found problems: its answer, not a failure
        found = result.get("problems_found") or doctor.get("found") or 0
        if last.get("exit") == 0 and not found:
            lines.append(t["fixed"] if task == "fix" else t["doctor_clean"])
        elif result.get("ok") or last.get("exit") in (0, 1):
            lines.append(_p(t["doctor_found"], found or len(doctor.get("problems") or ()) or 1))
        else:
            lines.append(t["ran_code"].format(command=last.get("command", "hermes"), code=last.get("exit")))
        if doctor.get("actions"):
            lines += ["", f"## {t['doctor_todo']}", ""] + [f"{n}. {item}" for n, item in enumerate(doctor["actions"], 1)]
        elif doctor.get("problems"):
            lines += ["", f"## {t['doctor_listed']}", ""] + [f"- {item}" for item in doctor["problems"][:12]]
        return "\n".join(lines) + "\n"
    if last.get("exit") == 0:
        lines.append(t["ran_ok"].format(command=last.get("command", "hermes")))
    else:
        lines.append(t["ran_code"].format(command=last.get("command", "hermes"), code=last.get("exit")))
    if last.get("tail"):
        lines += ["", f"{t['tail']}:", ""] + [f"    {line}" for line in last["tail"]]
    return "\n".join(lines) + "\n"


def _catalog_md(result: dict, lang: str) -> str:
    t = _H[lang]
    lines = [f"# {t['catalog_title']}", ""]
    tiers = result.get("tiers") or {}
    if not tiers:
        return "\n".join(lines + [t["catalog_none"]]) + "\n"
    lines.append(f"- **{t['catalog_tiers']}:** " + " · ".join(
        f"{_num(count, lang)} {t['tiers'].get(tier, tier)}" for tier, count in sorted(tiers.items())))
    removed = result.get("removed") or []
    if removed:
        lines += ["", f"## {t['catalog_removed']}", ""]
        lines += [f"- `{item.get('name')}`" + (f": {item['reason']}" if item.get("reason") else "") for item in removed[:40]]
    return "\n".join(lines) + "\n"


def _describe_decision(row: dict, lang: str) -> str:
    t = _H[lang]
    did = t["did"].get(row.get("action"), row.get("action"))
    kind = row.get("kind")
    if kind == "setting":
        return t["what_setting"].format(target=row["target"], before=_value((row.get("before") or {}).get("value")),
                                        after=_value((row.get("after") or {}).get("value")))
    if kind == "memory":
        return t["what_memory"].format(target=row["target"])
    if kind == "maintenance" and row.get("target") == "gateway_repair":
        before, after = row.get("before") or {}, row.get("after") or {}
        return t["what_gateway"].format(unit=before.get("unit") or "?", before=before.get("path") or "?",
                                        after=after.get("path") or "?", failed="" if after.get("ok") else
                                        t["gateway_failed"])
    if kind == "maintenance" and row.get("target") == "plugin_sync":
        before, after = row.get("before") or {}, row.get("after") or {}
        return t["what_sync"].format(profile=before.get("profile") or "?", before=before.get("sha") or "?",
                                     after=after.get("sha") or "?", failed="" if after.get("ok") else t["sync_failed"])
    if kind == "maintenance":
        return t["what_maintenance"].format(target=str(row["target"]).replace("_", " "))
    if kind == "proposal" and isinstance((row.get("after") or {}).get("applied"), list):
        from .explain import command_text

        return f"{did}: `" + command_text(row["after"]["applied"], lang) + "`"
    if kind == "cron":
        return t["what_cron"].format(target=row["target"], schedule=(row.get("after") or {}).get("schedule") or "?")
    return t["what_lesson" if kind == "lesson" else "what_proposal"].format(did=did, target=row["target"])


def _decisions_md(rows: list, home: Path, lang: str) -> str:
    from .status import local_time

    t = _H[lang]
    lines = [f"# {t['decisions_title']}", ""]
    if not rows:
        return "\n".join(lines + [t["decisions_none"]]) + "\n"
    for row in rows:
        mark = f" · _{t['undone_mark']}_" if row.get("undone") else ""
        lines.append(f"- **#{row['id']}** · {local_time(row['at'], lang)} · {t['where'].get(row['actor'], row['actor'])} · "
                     f"{_describe_decision(row, lang)}{mark}")
        if row.get("lesson"):
            lines.append(f"  - {row['lesson']}")
        elif row.get("text_hidden"):
            lines.append(f"  - {t['hidden']}")
    if any(row.get("undoable", not row.get("undone")) for row in rows):
        lines += ["", t["undo_hint"].format(command=_hermes_cmd(home, "evolution decisions undo <id>"))]
    return "\n".join(lines) + "\n"


def _undo_md(result: dict, lang: str) -> str:
    t = _H[lang]
    number = result.get("id", "?")
    if result.get("run_this"):
        lines = [t["undo_run"], "", f"    {result['run_this']}", ""]
        lines.append(t["agent_tools_restart"] if result.get("key") == "agent_tools" and result.get("restore") is True
                     else t["applies_soon"])
        return "\n".join(lines) + "\n"
    if result.get("undone"):
        return t["undo_done"].format(id=number) + "\n"
    reason = t["undo_reasons"].get(result.get("reason"), t["undo_reasons"]["changed_since"])
    return t["undo_not"].format(id=number, reason=reason) + "\n"


def _lesson_line(item: dict, lang: str) -> list[str]:
    t = _H[lang]
    origin = t["origins"].get(item.get("origin"), item.get("origin"))
    parts = [f"`{item.get('tool')}`", origin]
    if item.get("failures_seen"):
        parts.append(_p(t["seen"], item["failures_seen"], s=_p(t["sessions"], item.get("sessions") or 0)))
    if item.get("verdict") in t["verdicts"] and item["verdict"] != "withdrawn":   # its own group says it
        parts.append(t["verdicts"][item["verdict"]])
    if item.get("rate_before") is not None and item.get("rate_after") is not None:
        parts.append(t["rates"].format(before=_num(round(item["rate_before"], 3), lang),
                                       after=_num(round(item["rate_after"], 3), lang)))
    text = item.get("lesson") if item.get("lesson") else t["hidden"]
    return [f"- **#{item['id']}** · " + " · ".join(str(part) for part in parts), f"  {text}"]


def _lessons_md(result: dict, home: Path, lang: str) -> str:
    t = _H[lang]
    rows = result.get("lessons") or []
    mode = result.get("mode")
    name, does = t["lesson_modes"].get(mode, (str(mode), ""))
    lines = [f"# {t['lessons_title']}", "", t["lessons_mode"].format(mode=name) + (f" ({does})" if does else "")]
    if not rows:
        return "\n".join(lines + ["", t["lessons_none"]]) + "\n"
    groups: dict[str, list] = {}
    for item in rows:   # a lesson the queue cleaning dropped without a decision is not one the owner retired
        state = "withdrawn" if item.get("status") == "retired" and item.get("verdict") == "withdrawn" else item.get("status")
        groups.setdefault(t["status_names"].get(state, str(state)), []).append(item)
    for title, items in sorted(groups.items(), key=lambda entry: entry[0] == t["status_names"]["withdrawn"]):
        lines += ["", f"## {title} · {len(items)}", ""]
        for item in items:
            lines += _lesson_line(item, lang)
    if any(item.get("status") in ("candidate", "quarantined") for item in rows):
        lines += ["", t["lessons_decide"].format(review=_hermes_cmd(home, "evolution lessons review"),
                                                 approve=_hermes_cmd(home, "evolution lessons approve <id>"),
                                                 retire=_hermes_cmd(home, "evolution lessons retire <id>"))]
    return "\n".join(lines) + "\n"


def _lesson_task_md(result: dict, task: str, home: Path, lang: str) -> str:
    t = _H[lang]
    if task == "add":
        if result.get("status") == "active":
            return t["added"].format(id=result["id"]) + "\n"
        return t["added_waiting"].format(id=result["id"], command=_hermes_cmd(home, f"evolution lessons approve {result['id']}")) + "\n"
    if task == "review":
        return t["review_done"].format(**{key: ", ".join(f"#{item}" for item in result.get(key) or ()) or "0"
                                          for key in ("approved", "rejected", "skipped")}) + "\n"
    if task == "approve" and "approved" in result and "id" not in result:   # --all
        lines = [_p(t["all_done"], len(result["approved"])) if result["approved"] else t["all_none"]]
        if result.get("left_for_one_by_one"):
            lines.append(t["all_left"].format(ids=", ".join(f"#{item}" for item in result["left_for_one_by_one"])))
        skipped = [item.get("id") if isinstance(item, dict) else item for item in result.get("skipped") or ()]
        if skipped:
            lines.append(t["all_skipped"].format(ids=", ".join(f"#{item}" for item in skipped)))
        return "\n".join(lines) + "\n"
    if task == "export":
        return _p(t["written"], result.get("lessons", 0), path=result.get("written")) + "\n"
    if task == "import":
        lines = [t["import_plan"].format(source=f"`{result.get('source')}`"), ""]
        if result.get("to_import"):
            lines += [f"## {t['import_add']}", ""] + [f"- `{item['tool']}`: {item['lesson']}" for item in result["to_import"]]
            lines.append("")
        if result.get("skipped"):
            lines += [f"## {t['import_skip']}", ""] + [
                f"- {item.get('lesson') or item.get('tool') or item}" if isinstance(item, dict) else f"- {item}"
                for item in result["skipped"][:40]]
        lines.append("")
        if result.get("imported"):
            lines.append(_p(t["import_done"], len(result["imported"])))
        elif result.get("to_import"):
            lines.append(t["import_dry"])
        else:
            lines.append(t["import_none"])
        return "\n".join(lines) + "\n"
    number, status, previous = result.get("id"), result.get("status"), result.get("previous")
    if result.get("decision") is None:   # nothing changed: say where the lesson already is
        if status == "active":
            return t["already_active"].format(id=number) + "\n"
        if status == "retired":
            return t["already_retired"].format(id=number) + "\n"
        return t["not_changed"].format(id=number, status=t["status_names"].get(status, status).lower()) + "\n"
    if status == "active":
        return t["reactivated" if previous == "retired" else "approved"].format(id=number) + "\n"
    if previous == "retired":
        return t["kept_retired"].format(id=number) + "\n"
    return t["rejected" if previous in ("candidate", "quarantined") else "retired_now"].format(id=number) + "\n"


def _proposals_md(rows: list, home: Path, lang: str, *, everything: bool) -> str:
    from . import explain
    from .report import subject_words
    from .status import local_time

    t = _H[lang]
    lines = [f"# {t['proposals_all'] if everything else t['proposals_title']}", ""]
    if not rows:
        return "\n".join(lines + [t["proposals_none"]]) + "\n"
    for item in rows:
        told = explain.describe(item, lang)
        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        title = told["title"]
        if item.get("kind") in ("lesson", "lesson_retire") and evidence.get("lesson_id") is not None:
            # the lesson's own number, the one `lessons list` and `lessons approve` use; the proposal keeps its own
            title = t["lesson_title" if item["kind"] == "lesson" else "retire_title"].format(id=evidence["lesson_id"])
        state = "" if item.get("status") == "open" else f" · {t['state_label']}: {t['states'].get(item['status'], item['status'])}"
        lines.append(f"- **{t['proposal']} #{item['id']}** [{t['risk']}: {told['risk']}] {title} · "
                     f"{subject_words(item, lang)}{state}" + (f" · {local_time(item['created'], lang)}" if item.get("created") else ""))
        if item.get("kind") == "lesson":
            text = evidence.get("lesson")
            lines.append(f"  - {text}" if text else f"  - {t['hidden']}")
        elif told.get("what"):
            lines.append(f"  - {t['seen_label']}: {told['what']}")
        if told.get("advice"):
            lines.append(f"  - {t['advice_label']}: {told['advice']}")
        waiting = item.get("status") in ("open", "approved")
        approving = _if_approved(item, told, home, lang) if waiting else ""
        if approving:
            lines.append(f"  - {approving}")
        if told.get("undo") and waiting:
            lines.append(f"  - {t['undo_label']}: {_undo_words(told['undo'])}")
        elif item.get("command") and item.get("kind") not in ("lesson", "investigate_failure"):
            lines.append(f"  - {t['command_label']}: `{item['command']}`")
    if any(item.get("status") in ("open", "approved") for item in rows):
        lines += ["", t["proposals_decide"].format(approve=_hermes_cmd(home, "evolution proposals approve <id>"),
                                                   reject=_hermes_cmd(home, "evolution proposals reject <id>"),
                                                   menu=_hermes_cmd(home, "evolution"),
                                                   review=_hermes_cmd(home, "evolution lessons review"))]
    return "\n".join(lines) + "\n"


def _if_approved(item: dict, told: dict, home: Path, lang: str) -> str:
    """What approving a proposal does, as one sentence that starts with "Se aprovar"/"If approved" (or what to do,
    for an observation), from explain.describe's `does`."""
    t = _H[lang]
    kind = item.get("kind")
    if kind == "lesson":
        return t["lesson_on"]
    if kind == "lesson_retire":
        return t["lesson_retire"]
    if kind == "investigate_failure":
        command = item.get("command") or _hermes_cmd(
            home, f"evolution lessons add \"<{_say(lang, 'text', 'texto')}>\" --tool {item.get('subject')}")
        return f"{t['advice_label']}: " + t["write_lesson"].format(command=command) + "."
    does = str(told.get("does") or "").strip()
    if not does or does.startswith(("Rode no seu terminal", "Run in your terminal")):
        return ""   # nothing is applied: the command line below says what to run
    if does.startswith(("É uma observação", "It is an observation")):
        return does
    for prefix, said in (("Ao aplicar, ", "Se aprovar, "), ("Applying ", "If approved, this ")):
        if does.startswith(prefix):
            return said + does[len(prefix):]
    for prefix in ("Aplicar roda: ", "Applying runs: "):
        if does.startswith(prefix):
            ran = does[len(prefix):].rstrip(".")
            return f"{t['does_label']}: " + (("roda " if lang == "pt" else "it runs ") + f"`{ran}`."
                                              if ran.startswith("hermes ") else ran + ".")
    head, colon, rest = does.partition(": ")
    sentence = f"{head} — {rest}" if colon else does
    # approving applies only what the plugin can apply itself; otherwise it prints the command shown below
    label = t["does_label"] if told.get("can_apply") else t["changes_label"]
    return f"{label}: {sentence[:1].lower()}{sentence[1:]}"


def _undo_words(undo: str) -> str:
    """The undo of a proposal: a Hermes command in code, the plugin's own trash move in words."""
    if undo.startswith("hermes "):
        return f"`{undo}`"
    verb, colon, place = undo.partition(": ")
    return f"{verb} `{place}`" if colon else undo


def _proposal_task_md(result: dict, task: str, home: Path, lang: str) -> str:
    t = _H[lang]
    number = result.get("id")
    if "status" not in result and isinstance(result.get("applied"), bool):   # applied with Hermes' own command
        if not result["applied"]:
            return t["p_not_applied"].get(result.get("reason"), t["p_not_applied"]["not_open"]).format(id=number) + "\n"
        from .explain import command_text

        undo = command_text(result.get("undo_args"), lang) if result.get("undo_args") else result.get("undo")
        lines = [t["p_applied"].format(id=number)]
        if undo or result.get("decision"):
            lines.append(t["p_undo"].format(undo=_undo_words(undo) if undo else "—",
                                            command=_hermes_cmd(home, f"evolution decisions undo {result.get('decision')}")))
        return "\n".join(lines) + "\n"
    status = result.get("status")
    if status == "rejected":
        return t["p_rejected"].format(id=number) + "\n"
    if result.get("kind") in ("lesson", "lesson_retire") and status == "applied":
        return t["p_lesson_on" if result["kind"] == "lesson" else "p_lesson_off"].format(id=number) + "\n"
    if result.get("run_this"):
        return "\n".join([t["p_approved"].format(id=number), "", f"    {result['run_this']}"]) + "\n"
    return t["p_approved_plain"].format(id=number) + "\n"


def _learn_md(result: dict | None, home: Path, lang: str, *, quiet: bool) -> str | None:
    from .status import local_time

    t = _H[lang]
    if result is None:
        return None
    lines = [f"# {t['learn_title']}", ""]
    automatic = result.get("automatic") or result.get("automatic_actions") or {}
    if not quiet:
        mode = automatic.get("mode")
        if mode in t["modes"]:
            name, does = t["modes"][mode]
            lines.append(f"- **{name}:** {does}")
        history = result.get("history") or {}
        if history:
            line = f"- **{t['history']}:** {t['history_states'].get(history.get('status'), history.get('status'))}"
            if history.get("sessions") or history.get("tool_calls"):
                line += " · " + t["history_counts"].format(sessions=_num(history.get("sessions", 0), lang),
                                                           tool_calls=_num(history.get("tool_calls", 0), lang))
            lines.append(line)
        reflector = (result.get("reflector") or {}).get("status")
        if reflector:
            lines.append(f"- **{t['reflector']}:** {t['reflector_states'].get(reflector, reflector)}")
        lines.append("")
    sections = [
        (t["new_lessons"], [_lesson_line(item, lang) for item in result.get("new_lessons") or ()]),
        (t["drafts"], [[f"- #{item.get('lesson_id')} `{item.get('tool')}`: {item.get('lesson') or t['hidden']}"]
                       for item in result.get("reflector_drafts") or (result.get("reflector") or {}).get("drafted") or ()
                       if isinstance(item, dict)]),
        (t["regressions"], [[f"- {t['change_kinds'].get(item.get('kind'), item.get('kind'))} `{item.get('subject')}`: "
                             f"{t['change_verdicts']['regressed']}"] for item in result.get("regressions") or
                            [entry for entry in result.get("change_verdicts") or () if entry.get("verdict") == "regressed"
                             and entry.get("final")]]),
        (t["retired_lessons"], [[f"- #{item.get('id')} `{item.get('tool', '')}`"] for item in result.get("lessons_retired") or
                                [entry for entry in result.get("lesson_effects") or () if entry.get("retired")]]),
        (t["risky"], [[_risky_line(item, home, lang)] for item in result.get("high_risk_proposals") or
                      [entry for entry in result.get("open_proposals") or () if entry.get("risk") == "high"
                       and entry.get("created") == result.get("now")]]),
    ]
    done = []
    if automatic.get("memory"):
        done.append(_p(t["memory_done"], len(automatic["memory"])))
    from .explain import describe
    from .report import subject_words

    for item in automatic.get("applied") or ():
        title = describe({"kind": item.get("kind"), "subject": item.get("subject")}, lang)["title"]
        done.append(f"{t['proposal']} #{item.get('proposal')}: {title} · "
                    + subject_words({"kind": item.get("kind"), "subject": item.get("subject")}, lang))
    sections.append((t["automatic"], [[f"- {line}"] for line in done]))
    sections.append((t["automatic_failed"], [[f"- #{item['proposal']}" if item.get("proposal") else f"- {t['memory']}"]
                                             for item in automatic.get("failed") or ()]))
    shown = False
    for title, entries in sections:
        if entries:
            shown = True
            lines += [f"## {title}", ""] + [line for entry in entries for line in entry] + [""]
    if not shown:
        lines += [t["nothing_new"], ""]
    if not quiet and result.get("open_proposals"):
        lines.append(_p(t["open"], len(result["open_proposals"])) + ". "
                     + t["see_proposals"].format(command=_hermes_cmd(home, "evolution proposals list")))
    if result.get("now"):
        when, _, zone = local_time(result["now"], lang).rpartition(" ")
        lines += ["", t["pass_done"].format(when=f"{when} ({zone})" if when else zone)]
    return "\n".join(lines).rstrip() + "\n"


def _risky_line(item: dict, home: Path, lang: str) -> str:
    """A high-risk proposal of a learning pass, with what it is and the command that decides it."""
    t = _H[lang]
    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    if item.get("kind") == "lesson":
        who = t["who"].get(evidence.get("origin"), t["kinds"]["lesson"])
        return "- " + t["risky_lesson"].format(id=item.get("id"), who=who, tool=item.get("subject"),
                                               command=_hermes_cmd(home, "evolution lessons review"))
    from .explain import describe
    from .report import subject_words

    return "- " + t["risky_other"].format(id=item.get("id"), title=describe(item, lang)["title"],
                                          subject=subject_words(item, lang),
                                          command=_hermes_cmd(home, "evolution proposals list"))


def _skills_md(report: dict, lang: str) -> str:
    t = _H[lang]
    days = report.get("window_days", 7)
    lines = [f"# {t['skills_title'].format(days=days)}", "",
             f"- **{t['tracked']}:** {_num(report.get('skills_tracked', 0), lang)}",
             f"- **{t['rewrites']}:** {_num(report.get('rewrites', 0), lang)} ("
             + t["by_review"].format(n=_num((report.get("rewrites_by_actor") or {}).get("curator", 0), lang)) + ") · "
             + _p(t["rewritten"], report.get("skills_rewritten", 0))]
    if report.get("top"):
        lines += ["", f"## {t['top']}", ""]
        for item in report["top"]:
            parts = [f"{item['rewrites']}×", _p(t["flipflops"], item.get("flipflops", 0)), _p(t["uses"], item.get("uses_total", 0))]
            if item.get("unreused_rewrites"):
                parts.append(_p(t["unreused"], item["unreused_rewrites"]))
            lines.append(f"- `{item['skill']}`: " + " · ".join(parts))
    outcomes = report.get("outcomes") or {}
    findings = list(report.get("findings") or ()) + list(outcomes.get("findings") or ())
    if findings:
        lines += ["", f"## {t['skill_findings']}", ""]
        for item in findings:
            template = t["skill_codes"].get(item.get("code"))
            values = {**item, "worse": ", ".join(str(name).replace("_pct", "") for name in item.get("worse") or ())}
            try:
                lines.append("- " + (template.format(**values) if template else f"`{item.get('skill')}`: {item.get('code')}"))
            except (KeyError, IndexError):
                lines.append(f"- `{item.get('skill')}`")
    if outcomes:
        lines += ["", f"## {t['outcomes'].format(days=days)}", ""]
        base = outcomes.get("profile") or {}
        measured = [item for item in outcomes.get("skills") or () if item.get("turns", 0) >= outcomes.get("min_turns", 8)]
        if not (measured and base.get("turns")):
            lines.append(t["outcomes_none"].format(n=outcomes.get("min_turns", 8)))
        else:
            for name, item in [(t["whole"], base)] + [(f"`{entry['skill']}`", entry) for entry in measured[:12]]:
                lines.append(f"- {name}: {_p(t['turns'], item['turns'])} · "
                             + t["failed_pct"].format(v=_num(item.get("failed_pct"), lang)) + " · "
                             + t["corrected_pct"].format(v=_num(item.get("corrected_pct"), lang)))
    lines += ["", f"_{t['basis_skills']}_"]
    return "\n".join(lines) + "\n"


def _experiments_md(rows: list, lang: str) -> str:
    from .status import local_time

    t = _H[lang]
    lines = [f"# {t['experiments_title']}", ""]
    if not rows:
        return "\n".join(lines + [t["experiments_none"]]) + "\n"
    for item in rows:
        verdict = t["change_verdicts"].get(item.get("verdict"), item.get("verdict"))
        final = f" ({t['final'] if item.get('final') else t['provisional']})" if item.get("verdict") else ""
        lines.append(f"- **#{item['id']}** {t['change_kinds'].get(item.get('kind'), item.get('kind'))} "
                     f"`{item.get('subject')}` · {local_time(item.get('at'), lang)} · {verdict}{final}")
    return "\n".join(lines) + "\n"


def _system_md(result: dict, task: str, home: Path, lang: str) -> str:
    from .status import local_time

    t = _H[lang]
    yes, no = t["yes"], t["no"]
    if task == "check":
        lines = [f"# {t['system_title']}", ""]
        if not result.get("supported"):
            return "\n".join(lines + [t["unsupported"]]) + "\n"
        name = (result.get("os") or {}).get("name") or "?"
        lines.append(f"- **{name}** · " + t["kernel"].format(k=result.get("kernel") or "?"))
        pending = result.get("upgradable")
        count = len(pending) if isinstance(pending, list) else pending or 0
        when = local_time(result["last_refresh"], lang) if result.get("last_refresh") else "?"
        lines.append(f"- **{t['updates']}:** " + t["updates_line"].format(n=count, s=result.get("security", 0), when=when))
        if result.get("reboot_required"):
            packages = ", ".join(result.get("reboot_packages") or ())
            lines.append(f"- **{t['reboot']}:** {yes}" + (f" ({packages})" if packages else ""))
        lines.append(f"- **{t['auto_updates']}:** {yes if (result.get('unattended') or {}).get('enabled') else no}")
        if result.get("busy"):
            state = t["pm_busy"].format(what=", ".join(result["busy"]))
        elif result.get("dpkg_ok") is False or result.get("apt_ok") is False:
            state = t["pm_broken"]
        else:
            state = t["pm_ok"]
        lines.append(f"- **{t['pm']}:** {state}")
        units = result.get("units") or []
        if units:
            lines.append(f"- **{t['units']}:** " + ", ".join(
                f"`{unit['unit']}` ({t['unit_ok'] if unit.get('enabled') else t['unit_off']})" for unit in units)
                + (f" · {t['linger']}: {yes if result.get('linger') else no}" if result.get("linger") is not None
                   and any(unit.get("scope") == "user" for unit in units) else ""))
        findings = [code for code in result.get("findings") or () if code != "package_manager_busy"]
        why = ", ".join(t["system_codes"].get(code, code) for code in result.get("findings") or ()
                        if code in ("dpkg_interrupted", "apt_broken", "package_manager_busy", "disk_low"))
        lines.append(f"- **{t['ready']}:** " + (t["ready_yes"] if result.get("ready") else t["ready_no"].format(why=why or "?")))
        from .status import _TEXT, _care_values

        texts, values = _TEXT[lang], _care_values({"care": {}, "system": result})
        lines += ["", f"## {t['attention']}", ""]
        lines += [f"- {texts['os_' + code].format(**values) if 'os_' + code in texts else t['system_codes'].get(code, code)}"
                  for code in findings] or [t["nothing"]]
        if result.get("at"):
            lines += ["", t["checked"].format(when=local_time(result["at"], lang))]
        lines += ["", f"## {t['system_run']}", ""]
        for action, does in t["system_actions"].items():
            lines.append(f"- `{_hermes_cmd(home, 'evolution system ' + action)}`: {does}")
        return "\n".join(lines) + "\n"
    if task == "plan" or (task == "upgrade" and result.get("reason") == "nothing_to_do"):
        planned = result.get("planned", result)
        lines = [f"# {t['plan_title']}", ""]
        if planned.get("error"):
            return "\n".join(lines + [t["plan_error"].format(error=planned["error"])]) + "\n"
        counts = {key: len(planned.get(key) or ()) for key in ("upgrade", "install", "remove")}
        if not any(counts.values()):
            return "\n".join(lines + [t["nothing_to_do"]]) + "\n"
        lines.append(t["plan_counts"].format(upgrade=counts["upgrade"], remove=counts["remove"],
                                             install=_p(t["plan_new"], counts["install"]),
                                             kept=_p(t["plan_kept"], len(planned.get("kept_back") or ()))))
        noted = [item for item in (planned.get("upgrade") or []) + (planned.get("install") or []) + (planned.get("remove") or [])
                 if item.get("attention")]
        if noted:
            lines += ["", f"## {t['plan_attention']}", ""]
            for item in noted[:40]:
                notes = ", ".join(t["attention_codes"].get(code, code) for code in item["attention"])
                lines.append(f"- `{item['name']}` {item.get('from') or ''} → {item.get('to') or '—'}: {notes}")
        lines += ["", f"`{_hermes_cmd(home, 'evolution system upgrade')}`: {t['system_actions']['upgrade']}"]
        return "\n".join(lines) + "\n"
    if task == "repair":
        lines = []
        if result.get("released"):
            lines.append(t["released"].format(names=", ".join(result["released"])))
        if result.get("state") == "lost":
            lines.append(t["lost"])
        lines.append(t["repair_ok"] if result.get("ok") else t["repair_bad"].format(
            code="?" if result.get("exit") is None else result["exit"], folder=result.get("folder")))
        return "\n".join(lines) + "\n"
    report = result.get("report")
    folder = result.get("folder")
    lines = [f"# {t['report_title']}", ""]
    if task == "upgrade":
        lines = [t["upgrade_started"], ""] + lines
    if not report:
        return "\n".join(lines + [t["report_none"]]) + "\n"
    if report.get("state") == "running":
        return "\n".join(lines + [t["report_running"]]) + "\n"
    if report.get("state") == "lost":
        lines.append(t["lost"])
    state = {True: "ok", False: "⚠", None: "?"}
    lines.append(t["report_line"].format(exit=report.get("exit"), changed=_p(t["changed"], len(report.get("changed") or ())),
                                         pm=state[bool(report.get("dpkg_ok")) and report.get("apt_ok") is not False],
                                         hermes=state[report.get("hermes_ok")],
                                         reboot=yes if report.get("reboot_required") else no))
    if report.get("stopped_units"):
        lines.append(f"- **{t['stopped_units']}:** " + ", ".join(f"`{unit}`" for unit in report["stopped_units"]))
    if report.get("go_back"):
        lines += ["", f"{t['go_back']}:", ""] + [f"    {line}" for line in report["go_back"][:10]]
    if folder:
        lines += ["", t["folder"].format(folder=folder)]
    return "\n".join(lines) + "\n"


# `health`, `usage`, `history`, `tick` and `schedule-digest` as text (0.9.0); `--format json` keeps the structure.
_OPS = {
    "en": {
        "health_title": "Hermes' files and the machine · profile `{profile}`",
        "states": {"starting": "starting", "running": "running", "restarting": "restarting", "stopping": "stopping",
                   "stopped": "stopped", "startup_failed": "failed to start", "degraded": "degraded"},
        "no_state": "no state reported",
        "process": {"confirmed": "process confirmed", "missing_locally": "process not found",
                    "pid_reused_locally": "its process number now belongs to another program",
                    "unverified": "process not checked"},
        "beat": {"recent": "recent sign of life", "stale": "late sign of life"},
        "version": "version {v}", "served": "served by the main profile's gateway",
        "platforms": ("1 platform needs attention", "{n} platforms need attention"),
        "store": {"ok": "session store ok", "unavailable": "session store unavailable",
                  "retrying": "session store retrying"},
        "db": "Session database",
        "db_states": {"ok": "integrity ok", "integrity_error": "failed its integrity check",
                      "integrity_skipped_large": "too large to check its integrity",
                      "missing_or_unreadable": "not found or unreadable",
                      "wal_sidecars_unavailable": "the write-ahead log files are incomplete",
                      "snapshot_changed": "changed while it was read", "unexpected_schema": "unexpected format",
                      "check_timed_out": "the check took too long", "busy": "busy", "unreadable": "unreadable"},
        "wal": "write-ahead log {size}",
        "notes": "Agent memory files", "provider": "memory provider `{name}`",
        "file_states": {"empty": "empty", "missing_or_unreadable": "missing", "not_checked_too_large": "too large to read",
                        "invalid_utf8": "invalid text", "unreadable": "unreadable"},
        "disk": "Disk", "free": "{free} free of {total}", "memory": "Memory", "swap": "swap: {used} of {total} in use",
        "cgroup": "process group limit: {free} free of {total}",
        "load": "Load", "load_line": "{load} on {cpus} CPUs (last minute)",
        "logs": "Logs of the last 24 h", "logs_none": "nothing unusual",
        "events": {"warning": ("1 warning", "{n} warnings"), "error": ("1 error", "{n} errors"),
                   "critical": ("1 critical error", "{n} critical errors"),
                   "storage_io": ("1 full-disk error", "{n} full-disk errors"),
                   "resource_exhaustion": ("1 out-of-memory error", "{n} out-of-memory errors"),
                   "database_locked": ("1 locked database", "{n} locked databases"),
                   "storage_access": ("1 file access denied", "{n} file accesses denied"),
                   "rate_limit": ("1 provider rate limit", "{n} provider rate limits"),
                   "authentication": ("1 authentication refused", "{n} authentications refused"),
                   "timeout": ("1 timeout", "{n} timeouts"), "tool_result_error": ("1 tool error", "{n} tool errors"),
                   "memory_failure": ("1 memory failure", "{n} memory failures"),
                   "plugin_failure": ("1 plugin failure", "{n} plugin failures"),
                   "plugin_dependency_error": ("1 plugin missing a dependency", "{n} plugins missing a dependency"),
                   "plugin_contract_error": ("1 plugin calling Hermes the wrong way",
                                             "{n} plugins calling Hermes the wrong way")},
        "update": "Hermes update",
        "update_states": {"not_checked": "not checked (pass `--upstream-tag <version>`)",
                          "invalid_reference": "the reference version is not valid",
                          "unknown_version": "the installed version is unknown",
                          "update_available": "{current} → {reference} available", "ahead": "{current}, newer than {reference}",
                          "current": "{current}, up to date"},
        "severity": {"critical": "critical", "error": "error", "warning": "warning", "info": "note"},
        "read_at": "Read {when}.",
        # usage
        "usage_title": "Model and tool use · {days} days ({span}, UTC)",
        "llm": "Model calls that worked", "llm_line": "{calls} · {measured} with token counts · {priced} with a known price",
        "estimate": "Estimated cost (prices from the plugin's settings)", "no_price": "no price set",
        "dropped": "Events not recorded (database busy)",
        "groups": "By lane and model", "cols": "| lane | provider | model | result | calls | input | output | cache read |",
        "lanes": {"main": "agent", "tool": "tools"}, "aux": "auxiliary `{name}`",
        "outcomes": {"ok": "ok", "error": "error", "interrupted": "interrupted", "failed": "failed"},
        "native": "Hermes' own accounting",
        "native_states": {"ok": "US$ {cost} in {calls} calls", "empty": "nothing recorded in this window",
                          "missing_or_unreadable": "not available (the session database could not be read)",
                          "unreadable": "not available (the session database could not be read)"},
        "native_other": "not available",
        "usage_none": "No model call measured in this window.",
        # history
        "history_title": "Maintenance alerts the plugin learned",
        "history_cols": "| alert | state | last seen | days seen | your rating |",
        "history_none": "No maintenance alert recorded yet.", "open": "open", "cleared": "cleared",
        "ratings": {"helpful": "helped", "dismissed": "dismissed"},
        "rate_hint": "Rate one with `{command}`.",
        # tick
        "tick_title": "Maintenance", "mode": "Optional context",
        "modes": {"normal": "normal", "conservative": "paused: the machine or the budget is under pressure"},
        "changes": "What changed now",
        "transitions": {"opened": "opened", "cleared": "cleared", "normal": "optional context back to normal",
                        "conservative": "optional context paused"},
        "alerts": "Active alerts", "alerts_none": "No active alert.",
        "days_seen": ("seen on 1 day", "seen on {n} days"),
        "more_health": "The details of the machine: `{command}`.",
        # schedule-digest
        "digest_done": "The weekly digest is scheduled as a Hermes scheduled task (cron), no agent: `{job}`, "
                       "{schedule}, delivered to `{deliver}`. See it with `hermes cron list`.",
        "digest_already": "The weekly digest is already scheduled; nothing changed. See it with `hermes cron list`.",
    },
    "pt": {
        "health_title": "Arquivos do Hermes e a máquina · perfil `{profile}`",
        "states": {"starting": "iniciando", "running": "rodando", "restarting": "reiniciando", "stopping": "parando",
                   "stopped": "parado", "startup_failed": "falhou ao iniciar", "degraded": "com problemas"},
        "no_state": "sem estado informado",
        "process": {"confirmed": "processo confirmado", "missing_locally": "processo não encontrado",
                    "pid_reused_locally": "o número do processo agora é de outro programa",
                    "unverified": "processo não verificado"},
        "beat": {"recent": "sinal de vida recente", "stale": "sinal de vida atrasado"},
        "version": "versão {v}", "served": "atendido pelo gateway do perfil principal",
        "platforms": ("1 plataforma pede atenção", "{n} plataformas pedem atenção"),
        "store": {"ok": "registro de sessões ok", "unavailable": "registro de sessões indisponível",
                  "retrying": "registro de sessões tentando de novo"},
        "db": "Banco de sessões",
        "db_states": {"ok": "integridade ok", "integrity_error": "falhou na checagem de integridade",
                      "integrity_skipped_large": "grande demais para checar a integridade",
                      "missing_or_unreadable": "não encontrado ou ilegível",
                      "wal_sidecars_unavailable": "os arquivos do registro de escrita estão incompletos",
                      "snapshot_changed": "mudou durante a leitura", "unexpected_schema": "formato inesperado",
                      "check_timed_out": "a checagem demorou demais", "busy": "ocupado", "unreadable": "ilegível"},
        "wal": "registro de escrita {size}",
        "notes": "Arquivos de memória do agente", "provider": "provedor de memória `{name}`",
        "file_states": {"empty": "vazio", "missing_or_unreadable": "não existe", "not_checked_too_large": "grande demais para ler",
                        "invalid_utf8": "texto inválido", "unreadable": "ilegível"},
        "disk": "Disco", "free": "{free} livres de {total}", "memory": "Memória", "swap": "swap: {used} de {total} em uso",
        "cgroup": "limite do grupo de processos: {free} livres de {total}",
        "load": "Carga", "load_line": "{load} em {cpus} CPUs (último minuto)",
        "logs": "Registros das últimas 24 h", "logs_none": "nada fora do comum",
        "events": {"warning": ("1 aviso", "{n} avisos"), "error": ("1 erro", "{n} erros"),
                   "critical": ("1 erro crítico", "{n} erros críticos"),
                   "storage_io": ("1 erro de disco cheio", "{n} erros de disco cheio"),
                   "resource_exhaustion": ("1 erro de falta de memória", "{n} erros de falta de memória"),
                   "database_locked": ("1 banco travado", "{n} bancos travados"),
                   "storage_access": ("1 acesso negado a arquivo", "{n} acessos negados a arquivos"),
                   "rate_limit": ("1 limite de uso do provedor", "{n} limites de uso do provedor"),
                   "authentication": ("1 autenticação recusada", "{n} autenticações recusadas"),
                   "timeout": ("1 tempo esgotado", "{n} tempos esgotados"),
                   "tool_result_error": ("1 erro de ferramenta", "{n} erros de ferramenta"),
                   "memory_failure": ("1 falha da memória", "{n} falhas da memória"),
                   "plugin_failure": ("1 falha de plugin", "{n} falhas de plugin"),
                   "plugin_dependency_error": ("1 plugin sem uma dependência", "{n} plugins sem uma dependência"),
                   "plugin_contract_error": ("1 plugin chamando o Hermes do jeito errado",
                                             "{n} plugins chamando o Hermes do jeito errado")},
        "update": "Atualização do Hermes",
        "update_states": {"not_checked": "não verificada (passe `--upstream-tag <versão>`)",
                          "invalid_reference": "a versão de referência não é válida",
                          "unknown_version": "a versão instalada é desconhecida",
                          "update_available": "{current} → {reference} disponível",
                          "ahead": "{current}, mais nova que {reference}", "current": "{current}, em dia"},
        "severity": {"critical": "crítico", "error": "erro", "warning": "aviso", "info": "nota"},
        "read_at": "Lido em {when}.",
        "usage_title": "Uso de modelos e ferramentas · {days} dias ({span}, UTC)",
        "llm": "Chamadas ao modelo que deram certo",
        "llm_line": "{calls} · {measured} com contagem de tokens · {priced} com preço conhecido",
        "estimate": "Custo estimado (preços das configurações do plugin)", "no_price": "nenhum preço configurado",
        "dropped": "Eventos não gravados (banco ocupado)",
        "groups": "Por faixa e modelo",
        "cols": "| faixa | provedor | modelo | resultado | chamadas | entrada | saída | lido do cache |",
        "lanes": {"main": "agente", "tool": "ferramentas"}, "aux": "auxiliar `{name}`",
        "outcomes": {"ok": "ok", "error": "erro", "interrupted": "interrompida", "failed": "falhou"},
        "native": "Contabilidade do próprio Hermes",
        "native_states": {"ok": "US$ {cost} em {calls} chamadas", "empty": "nada registrado neste período",
                          "missing_or_unreadable": "indisponível (o banco de sessões não pôde ser lido)",
                          "unreadable": "indisponível (o banco de sessões não pôde ser lido)"},
        "native_other": "indisponível",
        "usage_none": "Nenhuma chamada ao modelo medida neste período.",
        "history_title": "Alertas de manutenção que o plugin aprendeu",
        "history_cols": "| alerta | situação | visto por último | dias em que apareceu | sua avaliação |",
        "history_none": "Nenhum alerta de manutenção registrado ainda.", "open": "aberto", "cleared": "resolvido",
        "ratings": {"helpful": "ajudou", "dismissed": "dispensado"},
        "rate_hint": "Para avaliar um: `{command}`.",
        "tick_title": "Manutenção", "mode": "Contexto opcional",
        "modes": {"normal": "normal", "conservative": "pausado: a máquina ou o orçamento estão sob pressão"},
        "changes": "O que mudou agora",
        "transitions": {"opened": "aberto", "cleared": "resolvido", "normal": "contexto opcional de volta ao normal",
                        "conservative": "contexto opcional pausado"},
        "alerts": "Alertas ativos", "alerts_none": "Nenhum alerta ativo.",
        "days_seen": ("visto em 1 dia", "visto em {n} dias"),
        "more_health": "Os detalhes da máquina: `{command}`.",
        "digest_done": "O resumo semanal foi agendado como tarefa agendada do Hermes (cron), sem agente: `{job}`, "
                       "{schedule}, entregue em `{deliver}`. Veja com `hermes cron list`.",
        "digest_already": "O resumo semanal já está agendado; nada mudou. Veja com `hermes cron list`.",
    },
}


def _health_md(result: dict, home: Path, lang: str) -> str:
    from .i18n import text
    from .status import human_size, local_time

    t = _OPS[lang]
    gate, host, db = result.get("gateway") or {}, result.get("host") or {}, result.get("database") or {}
    lines = ["# " + t["health_title"].format(profile=_profile_name(home)), ""]
    parts = [t["states"].get(gate.get("reported_state"), t["no_state"]),
             t["process"].get(gate.get("process_liveness"), t["process"]["unverified"])]
    if gate.get("heartbeat") in t["beat"]:
        parts.append(t["beat"][gate["heartbeat"]])
    if gate.get("reported_version"):
        parts.append(t["version"].format(v=gate["reported_version"]))
    if gate.get("served_by_host_gateway"):
        parts.append(t["served"])
    if gate.get("platforms_needing_attention"):
        parts.append(_p(t["platforms"], gate["platforms_needing_attention"]))
    if gate.get("reported_session_store") in t["store"]:
        parts.append(t["store"][gate["reported_session_store"]])
    lines.append(f"- **{_H[lang]['gateway']}:** " + " · ".join(parts))
    parts = [t["db_states"].get(db.get("status"), t["db_states"]["unreadable"])]
    if db.get("bytes") is not None:
        parts.insert(0, human_size(db["bytes"]))
    if db.get("wal_bytes"):
        parts.append(t["wal"].format(size=human_size(db["wal_bytes"])))
    lines.append(f"- **{t['db']}:** " + " · ".join(parts))
    notes = (result.get("memory") or {})
    files = [f"`{name}` " + (human_size(item.get("bytes")) if item.get("status") == "readable"
                             else t["file_states"].get(item.get("status"), t["file_states"]["unreadable"]))
             for name, item in (notes.get("native_files") or {}).items() if isinstance(item, dict)]
    if notes.get("provider"):
        files.append(t["provider"].format(name=notes["provider"]))
    if files:
        lines.append(f"- **{t['notes']}:** " + " · ".join(files))
    disk = host.get("disk") or {}
    if disk.get("total_bytes"):
        lines.append(f"- **{t['disk']}:** " + t["free"].format(free=human_size(disk.get("free_bytes")),
                                                              total=human_size(disk["total_bytes"])))
    memory = host.get("memory") or {}
    if memory.get("total_bytes"):
        line = t["free"].format(free=human_size(memory.get("available_bytes")), total=human_size(memory["total_bytes"]))
        if memory.get("swap_total_bytes"):
            line += " · " + t["swap"].format(used=human_size(memory["swap_total_bytes"] - (memory.get("swap_free_bytes") or 0)),
                                             total=human_size(memory["swap_total_bytes"]))
        group = host.get("cgroup_memory") or {}
        if group.get("total_bytes"):
            line += " · " + t["cgroup"].format(free=human_size(group.get("available_bytes")),
                                               total=human_size(group["total_bytes"]))
        lines.append(f"- **{t['memory']}:** {line}")
    load = host.get("load") or {}
    if load.get("logical_cpus"):
        lines.append(f"- **{t['load']}:** " + t["load_line"].format(load=_num(round(load.get("one_minute") or 0, 1), lang),
                                                                   cpus=load["logical_cpus"]))
    events = ((result.get("logs") or {}).get("events") or {})
    said = [_p(t["events"][name], count) for name, count in events.items() if name in t["events"] and count]
    lines.append(f"- **{t['logs']}:** " + (" · ".join(said) if said else t["logs_none"]))
    update = result.get("update") or {}
    state = t["update_states"].get(update.get("status"), t["update_states"]["not_checked"])
    lines.append(f"- **{t['update']}:** " + state.format(current=update.get("current") or "?",
                                                         reference=update.get("reference") or "?"))
    lines += ["", f"## {_H[lang]['attention']}", ""]
    findings = result.get("findings") or []
    lines += [f"- [{t['severity'].get(item.get('severity'), item.get('severity'))}] "
              f"{text(str(item.get('recommendation') or ''), lang)}" for item in findings] or [_H[lang]["nothing"]]
    if result.get("observed_at"):
        lines += ["", t["read_at"].format(when=local_time(result["observed_at"], lang))]
    return "\n".join(lines) + "\n"


def _usage_md(result: dict, lang: str, *, days: int) -> str:
    from .report import _number, _span

    t = _OPS[lang]
    usage = result.get("observer_usage") or {}
    start, end = usage.get("window_start_utc") or "?", usage.get("window_end_utc") or "?"
    lines = ["# " + t["usage_title"].format(days=days, span=_span(start, end, lang)), ""]
    lines.append(f"- **{t['llm']}:** " + t["llm_line"].format(calls=_num(usage.get("successful_llm_calls", 0), lang),
                                                             measured=_num(usage.get("usage_samples", 0), lang),
                                                             priced=_num(usage.get("priced_calls", 0), lang)))
    estimate = usage.get("estimated_usd")
    lines.append(f"- **{t['estimate']}:** " + (f"US$ {_number(estimate, lang, 2)}" if estimate is not None
                                                else t["no_price"]))
    if usage.get("dropped_events"):
        lines.append(f"- **{t['dropped']}:** {_num(usage['dropped_events'], lang)}")
    native = result.get("hermes_native_billing") or {}
    totals = native.get("totals") or {}
    state = t["native_states"].get(native.get("status"))
    cost = totals.get("actual_cost_usd") if totals.get("actual_cost_usd") is not None else totals.get("estimated_cost_usd")
    lines.append(f"- **{t['native']}:** " + (state.format(cost=_number(cost or 0, lang, 2),
                                                           calls=_num(totals.get("api_call_count") or 0, lang))
                                              if state else t["native_other"]))
    groups = usage.get("groups") or []
    if not groups:
        return "\n".join(lines + ["", t["usage_none"]]) + "\n"
    lines += ["", f"## {t['groups']}", "", t["cols"], "|---|---|---|---|---:|---:|---:|---:|"]
    for row in groups[:40]:
        lane = str(row.get("lane") or "?")
        lane = t["aux"].format(name=lane[4:]) if lane.startswith("aux:") else t["lanes"].get(lane, f"`{lane}`")
        lines.append(f"| {lane} | {row.get('provider') or '—'} | {row.get('model') or '—'} | "
                     f"{t['outcomes'].get(row.get('outcome'), row.get('outcome'))} | {_num(row.get('requests') or 0, lang)} | "
                     f"{_num(row.get('input_tokens') or 0, lang)} | {_num(row.get('output_tokens') or 0, lang)} | "
                     f"{_num(row.get('cache_read_tokens') or 0, lang)} |")
    return "\n".join(lines) + "\n"


def _history_md(rows: list, home: Path, lang: str) -> str:
    from .status import local_time

    t = _OPS[lang]
    lines = [f"# {t['history_title']}", ""]
    if not rows:
        return "\n".join(lines + [t["history_none"]]) + "\n"
    lines += [t["history_cols"], "|---|---|---|---:|---|"]
    for row in rows[:80]:
        lines.append(f"| `{row.get('code')}` | {t['open'] if row.get('active') else t['cleared']} | "
                     f"{local_time(row.get('last_seen'), lang)} | {row.get('samples', 0)} | "
                     f"{t['ratings'].get(row.get('feedback'), '—')} |")
    lines += ["", t["rate_hint"].format(command=_hermes_cmd(home, "evolution feedback <alerta> helpful|dismissed"
                                                            if lang == "pt" else "evolution feedback <alert> helpful|dismissed"))]
    return "\n".join(lines) + "\n"


def _tick_md(result: dict | None, home: Path, lang: str) -> str | None:
    from .i18n import text

    if result is None:   # --changes-only and nothing changed
        return None
    t = _OPS[lang]
    decision = result.get("decision") or {}
    lines = [f"# {t['tick_title']}", "",
             f"- **{t['mode']}:** {t['modes'].get(decision.get('mode'), decision.get('mode'))}"]
    changes = decision.get("changes") or []
    if changes:
        lines += ["", f"## {t['changes']}", ""]
        for item in changes:
            said = t["transitions"].get(item.get("transition"), item.get("transition"))
            lines.append(f"- {said}" if item.get("code") == "evolution_mode" else f"- `{item.get('code')}`: {said}")
    lines += ["", f"## {t['alerts']}", ""]
    findings = decision.get("findings") or []
    lines += [f"- [{t['severity'].get(item.get('severity'), item.get('severity'))}] "
              f"{text(str(item.get('action') or ''), lang)} (`{item.get('code')}`, "
              f"{_p(t['days_seen'], item.get('observed_days') or 1)})" for item in findings] or [t["alerts_none"]]
    lines += ["", t["more_health"].format(command=_hermes_cmd(home, "evolution health"))]
    return "\n".join(lines) + "\n"


def _digest_md(result: dict, lang: str) -> str:
    t = _OPS[lang]
    if not result.get("scheduled"):
        return t["digest_already"] + "\n"
    return t["digest_done"].format(job=result.get("job"), schedule=result.get("schedule"),
                                   deliver=result.get("deliver")) + "\n"


def _human(args, result, home: Path, lang: str):
    """The text a person reads for the result of `args` (a str), or the result unchanged when it is printed as JSON."""
    from . import status

    lang = lang if lang in _H else "en"
    action = args.evolution_action
    if action == "care":
        if args.care_action == "check":
            return _care_md(result, home, lang)
        return _care_action_md(result, args.care_action, lang)
    if action == "update":
        from .fleet_update import markdown

        return markdown(result, lang)
    if action == "environment":
        task = getattr(args, "environment_action", "show")
        if task == "catalog":
            return _catalog_md(result, lang)
        try:   # the same mode the panel shows (0.9.1: the terminal always said "auto")
            from .evolve import options as _options

            settings_now, problems = safe_settings(home)
            mode = _options(settings_now, strict=False, problems=problems).get("mode") or "auto"
        except Exception:
            mode = "auto"
        text = status.environment_markdown(home, lang, mode=mode)
        return (_H[lang]["scanned"] + "\n\n" + text) if task == "scan" else text
    if action == "decisions":
        if (getattr(args, "decisions_action", None) or "list") == "list":
            return _decisions_md(result, home, lang)
        return _undo_md(result, lang)
    if action == "lessons":
        task = getattr(args, "lessons_action", None) or "list"
        if task == "list":
            return _lessons_md(result, home, lang)
        if task == "export" and "written" not in result:
            return result   # the pack itself: data to save, printed as it is imported
        return _lesson_task_md(result, task, home, lang)
    if action == "proposals":
        task = getattr(args, "proposals_action", None) or "list"
        if task == "list":
            return _proposals_md(result, home, lang, everything=bool(getattr(args, "all", False)))
        return _proposal_task_md(result, task, home, lang)
    if action == "learn":
        return _learn_md(result, home, lang, quiet=bool(getattr(args, "notify_only", False)))
    if action == "skills":
        return _skills_md(result, lang)
    if action == "experiments":
        return _experiments_md(result, lang)
    if action == "system":
        return _system_md(result, args.system_action, home, lang)
    if action == "health":
        return _health_md(result, home, lang)
    if action == "usage":
        return _usage_md(result, lang, days=getattr(args, "days", 7))
    if action == "history":
        return _history_md(result, home, lang)
    if action == "tick":
        return _tick_md(result, home, lang)
    if action == "schedule-digest":
        return _digest_md(result, lang)
    return result


# English keeps the supervisor's own state words (scripts match "Active trial recovery: rolled_back").
_TRIAL_STATES = {"en": {"active": "still running", "rolled_back": "undone", "released": "released",
                        "not_applied": "not applied", "observing": "being observed", "pending": "pending",
                        "skipped": "skipped", "improved": "kept: it helped"},
                 "pt": {"active": "ainda em andamento", "rolled_back": "desfeito", "released": "liberado",
                        "not_applied": "não aplicado", "observing": "em observação", "pending": "pendente",
                        "skipped": "pulado", "improved": "mantido: ajudou"}}


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
            kanban_home=None, reflect=None) -> None:
    from .i18n import localize, text

    now = time.time()
    language = _output_language(args, home, runtime_config)
    if getattr(args, "lang", None) is None:
        args.lang = language
    if getattr(args, "evolution_action", None) in (None, "menu"):
        from .menu import interactive, run

        if interactive() and not _agent_shell():
            try:
                run(home, runtime_config, lang=language, reflect=reflect)
            except (OSError, ValueError, sqlite3.Error) as error:
                raise SystemExit(f"Evolution: {text(str(error), language)}") from None
            return
        if args.evolution_action == "menu":
            raise SystemExit("Evolution: " + text("Open the menu in your own terminal: it needs a terminal to answer "
                                                  "from, outside an agent session", language))
        args.evolution_action, args.format, args.no_learn = "status", "md", False
    try:
        result = _report(args, home, running_version=running_version, provider=provider, now=now,
                         runtime_config=runtime_config, hermes_source=hermes_source, kanban_home=kanban_home,
                         reflect=reflect)
    except KeyboardInterrupt:
        hint = (_say(language, " A system update goes on in the background: `hermes evolution system report`.",
                     " Uma atualização do sistema continua em segundo plano: `hermes evolution system report`.")
                if getattr(args, "evolution_action", None) == "system" else "")
        raise SystemExit("Evolution: " + _say(language, "Interrupted.", "Interrompido.") + hint) from None
    except (OSError, ValueError, sqlite3.Error) as error:
        recovery = ""
        if args.evolution_action == "ops" and args.ops_action == "tick":
            try:
                outcome = _rollback_trial(home, now)
                if outcome["status"] != "idle":
                    states = _TRIAL_STATES.get(language, _TRIAL_STATES["en"])
                    recovery = "\n" + _say(language, "Active trial recovery: ", "Recuperação do teste ativo: ") + \
                        states.get(outcome["status"], str(outcome["status"]))
            except (OSError, ValueError, sqlite3.Error):
                recovery = "\n" + text("Trial recovery is unverified. Run ops rollback to recheck.", language)
        raise SystemExit(f"Evolution: {text(str(error), language)}{recovery}") from None
    failed = _failed(args, result)
    if _wants_text(args):
        from .status import decimal_comma

        result = decimal_comma(_human(args, result, home, language), language)
    if isinstance(result, str):
        print(result, end="" if result.endswith("\n") else "\n")
    elif result is not None:
        print(json.dumps(localize(result, language), indent=2, ensure_ascii=False, allow_nan=False))
    notice = deprecation(getattr(args, "evolution_action", None), language)
    if notice:
        print(notice, file=sys.stderr)
    if failed:
        raise SystemExit(1)


# Commands that only the 0.1 notes and the 0.3 ops supervisor use, none of them by default: deprecated in 0.10.0,
# removed in 0.11.0. Each says so, with what replaces it, on stderr (a JSON answer on stdout stays parseable).
DEPRECATED = {
    "health": ("use `hermes evolution care check`", "use `hermes evolution care check`"),
    "history": ("use `hermes evolution decisions`", "use `hermes evolution decisions`"),
    "tick": ("the learning pass runs by itself (`hermes evolution learn` runs one now)",
             "a rodada de aprendizado roda sozinha (`hermes evolution learn` roda uma agora)"),
    "feedback": ("", ""),
    "memory": ("keep what you need of these 0.1 notes: `hermes evolution memory audit`",
               "guarde o que precisar dessas notas da 0.1: `hermes evolution memory audit`"),
    "ops": ("", ""),
}


def deprecation(action, lang: str, *, name: str | None = None) -> str | None:
    """The notice a deprecated command prints, in `lang` (`name`: how the owner typed it); None for any other."""
    if action not in DEPRECATED:
        return None
    en, pt = DEPRECATED[action]
    name = name or f"hermes evolution {action}"
    if lang == "pt":
        return f"Aviso: `{name}` está obsoleto e sai na versão 0.11.0" + (f"; {pt}." if pt else ".")
    return f"Note: `{name}` is deprecated and goes away in 0.11.0" + (f"; {en}." if en else ".")


def _failed(args, result) -> bool:
    """Whether a command that changes something did not do it (the exit code then says so)."""
    if not isinstance(result, dict):
        return False
    action = getattr(args, "evolution_action", None)
    if action == "update":
        return any(isinstance(item, dict) and item.get("status") == "failed" for item in result.get("results") or ())
    if action == "care" and getattr(args, "care_action", "check") != "check":
        return result.get("ok") is False
    return False


def _standalone_config(home: Path) -> dict:
    """The profile's config.yaml when a YAML parser is installed. Without one, or when the file cannot be read,
    the runner uses defaults and marks the configuration as unread: the settings only it holds (who the owner
    is) are unknown there, not empty."""
    from .evolve import CONFIG_UNREAD

    if not (home / "config.yaml").exists():
        return {}
    try:
        import yaml
    except ImportError:
        return {CONFIG_UNREAD: True}
    try:
        with (home / "config.yaml").open("rb") as stream:
            body = stream.read(1_048_577)
        loaded = yaml.safe_load(body) if len(body) <= 1_048_576 else None
    except (OSError, ValueError, RecursionError, yaml.YAMLError):
        return {CONFIG_UNREAD: True}
    if loaded is None:   # an empty file: Hermes' defaults
        return {}
    return loaded if isinstance(loaded, dict) else {CONFIG_UNREAD: True}


def _utf8_output() -> None:
    """Print UTF-8 whatever the console: on Windows a redirected stdout is the ANSI code page until Python 3.15,
    and the digest's arrows and Portuguese would stop the run (Hermes' own CLI does the same)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def main():
    from .i18n import resolve, text

    _utf8_output()
    def _(sentence: str) -> str:
        return text(sentence, resolve())

    parser = argparse.ArgumentParser(prog="python -m hermes_atlas_evolution")
    parser.add_argument("--home", type=Path, default=os.environ.get("HERMES_HOME") or None,
                        help=_("Explicit Hermes profile path"))
    parser.add_argument("--hermes-version", help=_("Installed version if running outside Hermes"))
    setup(parser)
    args = parser.parse_args()
    if args.home is None:
        parser.error(_("Pass --home or set HERMES_HOME"))
    from .environment import root_of

    given = args.home.expanduser()
    home = given.resolve()
    config = _standalone_config(home)
    memory = config.get("memory")
    provider = memory.get("provider") if isinstance(memory, dict) else None
    # Named profiles live in <root>/profiles/<name>; shared Kanban boards belong to the root.
    root = root_of(given)
    execute(args, home, running_version=args.hermes_version, provider=provider if isinstance(provider, str) else None,
            runtime_config=config, kanban_home=root)
