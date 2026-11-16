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

from .advisory import EFFORTS, KINDS
from .diagnostics import snapshot
from .memory import CATEGORIES, GLOBAL_SCOPE, MemoryStore
from .native_actions import PLAYBOOKS
from .operations import RETENTION_DAYS, database, recommendations, run_policy, summarize

RESOURCE_FINDINGS = {"low_disk": "disk", "low_memory": "memory", "cgroup_memory_limit": "cgroup_memory"}
EVOLUTION_ACTIONS = ("status", "learn", "lessons", "skills", "proposals", "experiments", "report", "owner",
                     "decisions")
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
               "auto_actions": ("evolution", "auto_actions")}
# Keys the form does not show but `hermes config set plugins.entries.hermes-atlas-evolution.settings.<key>` can set:
# the 0.1 notes, whose recall puts stored text in front of the agent on every turn.
CONFIG_KEYS = {"legacy_memory_commands": ("legacy_memory_commands",),
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
    "auto_actions": (("evolution", "auto_actions"), lambda value: value == "full"),   # changes Hermes by itself
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


def setup(parser: argparse.ArgumentParser) -> None:
    from .i18n import resolve, text

    language = resolve()   # help follows the profile language; output follows --lang or the settings

    def _(sentence: str) -> str:
        return text(sentence, language)

    parser.add_argument("--lang", choices=("en", "pt"), default=None, help=_("Language of lessons, messages and the digest"))
    # Without a command: the interactive menu in the owner's terminal, the status anywhere else (0.8.2).
    commands = parser.add_subparsers(dest="evolution_action", required=False)
    commands.add_parser("menu", help=_("Open the interactive menu: decide, configure, organize Hermes' memory and see "
                                       "the state, step by step"))
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

    state = commands.add_parser("status", help=_("Show whether the plugin is observing, learning and protecting this profile"))
    state.add_argument("--format", choices=("md", "json"), default="md")
    state.add_argument("--no-learn", action="store_true",
                       help=_("Only read; do not run the first pass when this profile never had one"))
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
    learn = commands.add_parser("learn", help=_("Run one evolution pass: derive lessons, measure changes, refresh proposals"))
    learn.add_argument("--notify-only", action="store_true", help=_("Print only new lessons, regressions and high-risk proposals"))
    playbook = commands.add_parser("lessons", help=_("Review the playbook learned from recurring tool failures"))
    playbook_commands = playbook.add_subparsers(dest="lessons_action")
    listing = playbook_commands.add_parser("list")
    listing.add_argument("--status", choices=("candidate", "active", "quarantined", "retired"))
    listing.add_argument("--lang", choices=("en", "pt"), default=argparse.SUPPRESS)
    playbook_commands.add_parser("review", help=_("Decide on each lesson that waits for approval, one by one, in your terminal"))
    for name in ("approve", "retire"):
        decided = playbook_commands.add_parser(name)
        if name == "approve":   # one lesson, or --all: every lesson waiting except the agent's, after showing each
            chosen = decided.add_mutually_exclusive_group(required=True)
            chosen.add_argument("id", type=int, nargs="?")
            chosen.add_argument("--all", action="store_true", help=_("Approve every lesson that waits, after showing "
                                                                     "each one; the agent's own proposals are left out"))
        else:
            decided.add_argument("id", type=int)
        decided.add_argument("--yes", action="store_true", help=_("Confirm without the interactive question"))
    custom = playbook_commands.add_parser("add", help=_("Add an operator lesson"))
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
    history = commands.add_parser("decisions", help=_("List your decisions on lessons, proposals and settings, "
                                                       "from the terminal and the dashboard, and undo one"))
    history_commands = history.add_subparsers(dest="decisions_action")
    history_commands.add_parser("list").add_argument("--limit", type=int, default=50)
    history_commands.add_parser("undo", help=_("Undo one decision while what it changed is still as it left it")) \
        .add_argument("id", type=int)
    skills = commands.add_parser("skills", help=_("Show skill churn, flip-flops and rewrites that were never exercised"))
    queue = commands.add_parser("proposals", help=_("Review and decide what the evolution loop proposes"))
    queue_commands = queue.add_subparsers(dest="proposals_action")
    queue_list = queue_commands.add_parser("list")
    queue_list.add_argument("--all", action="store_true", help=_("Include decided and expired proposals"))
    for name in ("approve", "reject"):
        decision = queue_commands.add_parser(name)
        decision.add_argument("id", type=int)
        decision.add_argument("--note")
        decision.add_argument("--yes", action="store_true", help=_("Confirm without the interactive question"))
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
    for command in (playbook, digest, learn, state, history):
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
    "en": {"title": "Who is you in chats", "current": "Counted as you now", "senders": "sender ids", "platforms": "platforms",
           "none": "none", "channels": "Chat channels in the last 30 days", "col_channel": "channel",
           "col_sessions": "sessions", "col_senders": "senders (sessions)", "col_you": "counts as you",
           "yes": "yes", "no": "no", "empty": "No chat session in 30 days: your CLI, TUI and Desktop always count as you.",
           "add": "To count a sender as you", "platform": "To count every chat of a channel as you (a private bot, your own UI)",
           "channel": "<channel>",
           "restart": "Then restart the gateway so the running hooks see it",
           "run": "Run this in your own terminal (it writes the Hermes settings form's value), then restart the gateway:",
           "why": "The plugin does not write it itself: who counts as you decides what reaches the agent, so it lives in "
                  "the Hermes configuration, which the agent's file tools cannot change.",
           "unchanged": "Nothing to change: {key} is already {value}.",
           "ignored": "Ignored in `settings.json`, because the agent's file tools can write that file: {keys}. To keep a "
                      "value, add it again with `hermes evolution owner add <id>` or `add-platform <channel>`."},
    "pt": {"title": "Quem é você nos chats", "current": "Contam como você agora", "senders": "ids de remetente",
           "platforms": "plataformas", "none": "nenhum", "channels": "Canais de chat dos últimos 30 dias",
           "col_channel": "canal", "col_sessions": "sessões", "col_senders": "remetentes (sessões)",
           "col_you": "conta como você", "yes": "sim", "no": "não",
           "empty": "Nenhuma sessão de chat em 30 dias: o seu CLI, TUI e Desktop sempre contam como você.",
           "add": "Para contar um remetente como você", "channel": "<canal>",
           "platform": "Para contar todo chat de um canal como você (um bot privado, a sua própria interface)",
           "restart": "Depois reinicie o gateway para os hooks em execução verem a mudança",
           "run": "Rode isto no seu próprio terminal (grava o valor do formulário de configurações do Hermes) e depois "
                  "reinicie o gateway:",
           "why": "O plugin não grava isso sozinho: quem conta como você decide o que chega ao agente, então isso fica na "
                  "configuração do Hermes, que as ferramentas de arquivo do agente não conseguem alterar.",
           "unchanged": "Nada a mudar: {key} já é {value}.",
           "ignored": "Ignorado no `settings.json`, porque as ferramentas de arquivo do agente conseguem gravar esse "
                      "arquivo: {keys}. Para manter um valor, adicione de novo com `hermes evolution owner add <id>` ou "
                      "`add-platform <canal>`."},
}


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
        return "\n".join([t["run"], "", f"    {settings_command(home, key, updated)}", "    hermes gateway restart", "",
                          t["why"]]) + "\n"
    channels = chat_senders(home, since=now - 30 * 86_400, owners=chosen["owner_sender_ids"],
                            platforms=chosen["owner_platforms"])
    lines = [f"# {t['title']}", "",
             f"- **{t['current']}:** {t['senders']}: {', '.join(f'`{item}`' for item in chosen['owner_sender_ids']) or t['none']}"
             f" · {t['platforms']}: {', '.join(f'`{item}`' for item in chosen['owner_platforms']) or t['none']}", ""]
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

        result = run_exclusive(home, lambda: cycle(home, now=now, runtime_config=runtime_config, config=config,
                                                   lang=lang, reflect=reflect, backfill_budget_s=120.0))
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
        chosen = options(config, runtime_config, strict=False)
        report = build(home, now=now, days=args.days, lang=lang, rates=config.get("rates_usd_per_million", {}),
                       own_transform=chosen["tool_hints"] and chosen["lessons"] != "off")
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
                return _export_lessons(conn, home, now=now, out=getattr(args, "out", None))
            if task == "import":
                return _import_lessons(conn, home, args, now=now, mode=opts["lessons"], lang=lang)
            if task == "review":
                return _review_lessons(conn, lang, now=now)
            if task == "add":
                activate = bool(getattr(args, "activate", False))
                if activate:
                    text = lessons.clean_text(args.text)
                    _confirm(text or "", lang, yes=getattr(args, "yes", False))
                lesson_id = lessons.add_custom(conn, args.text, tool=args.tool, origin="operator", now=now, active=activate)
                return {"id": lesson_id, "status": "active" if activate else "quarantined"}
            if task == "approve" and getattr(args, "all", False):
                return _approve_all(conn, lang, now=now, yes=getattr(args, "yes", False))
            row = conn.execute("SELECT * FROM lesson WHERE id=?", (args.id,)).fetchone()
            if row is None:
                raise ValueError(f"Unknown lesson id {args.id}")
            if task == "approve":
                _confirm(lessons.render(row, lang), lang, yes=getattr(args, "yes", False))
                withdrawn = row["status"] == "retired" and row["verdict"] == lessons.WITHDRAWN
                action = "approve" if row["status"] in decisions.WAITING or withdrawn else "reactivate"
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
        if task == "approve":
            from . import explain

            row = conn.execute("SELECT kind, subject, evidence, command FROM proposal WHERE id=?", (args.id,)).fetchone()
            if row is None:
                raise ValueError(f"Unknown proposal id {args.id}")
            evidence = json.loads(row["evidence"])
            if explain.plan(dict(row)) is not None:   # a reversible change the plugin applies with Hermes' command
                told = explain.describe({**dict(row), "evidence": evidence}, lang)
                _confirm(f"{told['title']}: {row['subject']}\n  {told['does']}\n  {told['undo']}", lang,
                         yes=getattr(args, "yes", False), apply=True)
                conn.commit()
                from .actions import apply_proposal

                return apply_proposal(home, args.id, now=now, actor="terminal")
            _confirm(evidence.get("lesson") or row["command"], lang, yes=getattr(args, "yes", False))
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
        own = own and bool(_OWN_COMMAND.search(" ".join(argv)))
        if _is_hermes(argv) and not own:
            return True
        if parent <= 1 or parent == pid:
            return False
        pid = parent
    return False


def _agent_shell() -> bool:
    """True when this command runs in a shell of an agent session: what _operator_only refuses."""
    return any(os.environ.get(name, "").strip() for name in AGENT_MARKERS) or _under_hermes() or _in_hermes_cgroup()


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

    if not sys.stdin.isatty():
        raise ValueError("Run lessons review in your own terminal")
    order = "CASE origin WHEN 'derived' THEN 0 WHEN 'operator' THEN 1 WHEN 'reflector' THEN 2 ELSE 3 END, id"
    rows = conn.execute(f"SELECT * FROM lesson WHERE status IN ('candidate','quarantined') ORDER BY {order}").fetchall()
    decided = {"approved": [], "rejected": [], "skipped": []}
    conn.commit()   # nothing is held while the owner reads: the gateway's hooks keep writing meanwhile
    for row in rows:
        shown = playbook.render(row, lang)
        sys.stderr.write(f"\n#{row['id']} · {row['tool']} · {row['klass']} · {row['origin']}\n  "
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


def _confirm(text: str, lang: str, *, yes: bool, apply: bool = False) -> None:
    """Show exactly what the agent would read and ask before it takes effect."""
    import sys

    if yes:
        return
    if not sys.stdin.isatty():
        raise ValueError("Confirm with --yes after reading the text with 'proposals list' or 'lessons list'")
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
    home = Path(home).resolve()
    return home.name if home.parent.name == "profiles" else "default"


def _export_lessons(conn, home: Path, *, now: float, out: str | None = None) -> dict:
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
    except OSError as error:
        raise ValueError(f"Cannot write the lesson pack: {type(error).__name__}") from None
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
        if not sys.stdin.isatty():
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
        if not sys.stdin.isatty():
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
    except (OSError, ValueError, sqlite3.Error) as error:
        recovery = ""
        if args.evolution_action == "ops" and args.ops_action == "tick":
            try:
                outcome = _rollback_trial(home, now)
                if outcome["status"] != "idle":
                    recovery = "\n" + text(f"Active trial recovery: {outcome['status']}", language)
            except (OSError, ValueError, sqlite3.Error):
                recovery = "\n" + text("Trial recovery is unverified. Run ops rollback to recheck.", language)
        raise SystemExit(f"Evolution: {text(str(error), language)}{recovery}") from None
    if isinstance(result, str):
        print(result, end="" if result.endswith("\n") else "\n")
    elif result is not None:
        print(json.dumps(localize(result, language), indent=2, ensure_ascii=False, allow_nan=False))


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
