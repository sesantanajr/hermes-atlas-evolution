"""The interactive menu (0.8.2): `hermes evolution` in your own terminal, or `hermes evolution menu`.

Everything the dashboard panel does, step by step, with numbers to choose: decide on lessons (one by one or all at
once), decide proposals, retire lessons, undo any decision, change the settings (or apply the recommended setup),
choose which chat channels count as you, remove duplicate entries from Hermes' memory, read the digest and the full
status, and run a learning pass now. Decisions go through admin.py, the panel's own rules, and land in the same
history (`hermes evolution decisions`).

It opens only in the owner's terminal: never in a shell of an agent session (cli._agent_shell), and never without
a terminal to answer from. Plain text and numbered choices, so it works the same in any terminal, on Linux, macOS
and Windows, over SSH, and with a screen reader.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import sys
import textwrap
import time
from datetime import datetime
from pathlib import Path

TEXT = {
    "en": {
        "title": "Atlas Evolution {version} · profile {profile}",
        "summary": "Lessons: {lessons} · {active} · {waiting} · {proposals}",
        "n_active": ("{n} active", "{n} active"), "n_waiting": ("{n} waiting for you", "{n} waiting for you"),
        "n_proposals": ("{n} open proposal", "{n} open proposals"),
        "lesson_modes": {"auto": "automatic", "suggest": "on approval", "off": "off"},
        "loop": "Last learning pass: {last} · Runtime guard {guard} · reflector {reflector}",
        "never": "not yet", "ok": "ok", "failed": "failed", "on": "on", "off": "off", "guard_on": "on", "guard_off": "off",
        "yes_value": "yes", "no_value": "no",
        "items": ["Lessons waiting for you ({waiting})", "Open proposals ({proposals})", "Active lessons ({active})",
                  "Decision history and undo", "Mode and automatic actions ({auto})", "Settings", "Which chats count as you",
                  "Hermes memory: repeats and space", "Digest of the last 7 days", "Full status",
                  "Run a learning pass now"],
        "items_more": ["Hermes health (no agent, no tokens)", "Operating system ({os})",
                       "Environment: every profile, plugin and skill ({profiles})"],
        "suggest_digest": "Schedule the weekly digest in your chat: a Hermes scheduled task (cron), no agent, no tokens",
        "suggest_os_repair": "Finish the system update that stopped half-way",
        "suggest_env": ("See 1 thing to fix across your profiles (plugins, skills)",
                        "See {n} things to fix across your profiles (plugins, skills)"),
        "advice": "What to do",
        "what_maintenance": "ran Hermes' own maintenance: hermes {target} (nothing to undo)",
        "care_gc_blocked": ("Not now: hermes pm gc would delete {folders}, which a Hermes service still starts from. "
                            "Point the service at the current Python first: hermes evolution care fix-gateway."),
        "what_sync": "brought Atlas Evolution in profile {profile} to the lead's code ({before} → {after}){failed}",
        "sync_failed": "; Hermes refused it, tried again tomorrow",
        "what_gateway": ("pointed the gateway's service {unit} at the Python Hermes uses now ({before} → "
                         "{after}){failed}; undo: hermes evolution care undo-gateway"),
        "gateway_failed": " (the gateway did not come up)",
        "and_more": (" (+{n} other kind)", " (+{n} other kinds)"),
        "env_title": "Environment: every profile of this Hermes",
        "env_mode": "Looked after by profile {lead} (its mode: {mode}; each other profile with the plugin follows its own) · last check: {when}",
        "env_lead_elsewhere": "Profile {lead} looks after every profile; this screen shows and decides through it.",
        "env_unseen": "not checked yet",
        "env_doctor": "Hermes doctor: {fail}, {warn}, {optional} (checked {when})",
        "env_doctor_failed": "Hermes doctor: did not run ({error}; checked {when})",
        "n_fail": ("{n} problem", "{n} problems"), "n_warn": ("{n} warning", "{n} warnings"),
        "n_optional": ("{n} optional piece not set up", "{n} optional pieces not set up"),
        "env_none": "No profile has Atlas Evolution turned on, so nobody looks after the whole Hermes yet. Turn it on in"
                    " the profile you use most: hermes -p <profile> plugins enable hermes-atlas-evolution.",
        "env_not_yet": "Not checked yet: press v to check now (about 15 seconds on a large Hermes).",
        "env_catalog": "Hermes plugin catalog: {plugins} plugins ({tiers}) · {removed}",
        "n_removed": ("{n} removed from it", "{n} removed from it"),
        "env_tiers": {"official": "official", "verified": "verified", "community": "community"},
        "env_profiles": "Profiles ({n}):",
        "env_row": "{profile:<18} {sessions} in 7 days · plugins: {on}, {idle} · {skills} · Atlas Evolution: {self}",
        "n_row_sessions": ("{n:>4} session", "{n:>4} sessions"), "n_on": ("{n} on", "{n} on"),
        "n_idle": ("{n} never loaded", "{n} never loaded"), "n_skills": ("{n} skill", "{n} skills"),
        "env_todo": "What to do ({n}), most important first:",
        "env_clean": "Nothing to fix: no plugin conflict, duplicate, removed plugin or leftover found.",
        "env_prompt": "Number to see and decide · v check again now · Enter back: ",
        "env_scanning": "Checking every profile…",
        "env_auto_mark": "⚙ = Auto mode does it by itself in the next learning pass (and you can undo it in the history, option 4).",
        "suggest_os_security": ("Install 1 security update of the system", "Install {n} security updates of the system"),
        "suggest_unused": "Free {size} of old Hermes tool versions",
        "deliver_prompt": "Deliver it where? local, or a channel such as telegram (Enter = local): ",
        "digest_done": "Scheduled as a Hermes scheduled task (cron), no agent: Mondays at 08:00 (task {job}). To undo it: option 4 of this menu (history).",
        "digest_already": "The weekly digest is already scheduled.",
        "care_title": "Hermes health (read from Hermes' own files: no agent, no network, no tokens)",
        "care_gateway": "Gateway: {state}",
        "gate_running": "running", "gate_stopped": "stopped", "gate_crashed": "stopped (it crashed)",
        "gate_degraded": "running (degraded: no chat platform connected yet; scheduled tasks still run)",
        "gate_starting": "starting",
        "care_gc_unsafe": "Not now: {reasons}. Check the tool store with hermes pm doctor.",
        "care_gc_recheck": "Not now: the last health check does not say what hermes pm gc would delete. Choose 0 to "
                           "check again first.",
        "gate_version": "version {version}", "gate_rss": "memory {rss}", "gate_beat": "heartbeat {age} ago",
        "no_data": "no data", "not_checked": "not checked yet",
        "care_db": "Session database: {journal} · WAL {wal} · integrity {quick}",
        "journals": {"wal": "WAL mode", "delete": "rollback journal", "truncate": "rollback journal",
                     "persist": "rollback journal", "memory": "journal in memory", "off": "no journal"},
        "integrity_bad": "a problem (see Needs attention)",
        "care_disk": "Disk where Hermes writes: {free} free of {total}{pace}",
        "care_pace": " · it loses {per_day} a day{full}", "care_full": " (full in {days})",
        "n_days": ("{n} day", "{n} days"), "under_a_day": "less than a day",
        "care_memory": "Memory: {available} free of {total} · swap {used} of {swap} · load {load} on {cpus} CPUs",
        "care_folders": "Largest folders: {folders}",
        "care_unused": "Old tool versions nothing uses ({size}): {list}",
        "care_none": "Nothing needs attention.",
        "care_menu": ["Check again now (no network)", "Installation integrity (hermes pm doctor, no network)",
                      "Hermes doctor, full check (hermes doctor): it calls every configured provider, runs npm audit against"
                      " the npm registry, asks gh auth status and imports the dashboard in a subprocess. It uses the"
                      " network, can take minutes and changes nothing.",
                      "Repair with Hermes (hermes backup --quick, then hermes doctor --fix)",
                      "Free old tool versions (hermes pm gc)"],
        "care_prompt": "Number · Enter back: ", "care_q": "Run “{command}” now?",
        "care_ok": "Done (exit {code}).", "care_failed": "Hermes answered with exit {code}{note}:",
        "care_fix_note": ["What hermes doctor --fix may change: create missing folders and the .env file, migrate"
                          " config.yaml (Hermes keeps its own copy first), repair the session database, recreate the"
                          " hermes launcher, and download Chromium if the browser tool needs it.",
                          "Before that, hermes backup --quick saves config.yaml, .env, the session database and the cron"
                          " jobs; that snapshot puts them back (in a Hermes chat: /snapshot restore <id>)."],
        "care_backup_failed": "The backup failed (exit {code}), so nothing was repaired{note}:",
        "doctor_found": ("Hermes doctor found {n} problem{note}:", "Hermes doctor found {n} problems{note}:"),
        "doctor_found_some": "Hermes doctor found problems{note}:",
        "doctor_fixed": "Hermes doctor repaired what it could; still to do by hand{note}:",
        "doctor_clean": "Hermes doctor found nothing to fix.", "doctor_warnings": "Warnings{note}:",
        "doctor_actions": "What Hermes says to do{note}:", "hermes_words": "",
        "os_title": "Operating system",
        "os_unsupported": "This system is not one the plugin updates (Linux with apt: Ubuntu, Debian).",
        "os_line": "{name} · kernel {kernel}",
        "os_updates": "Updates waiting: {count} ({security} security) · package lists of {when}",
        "os_reboot": "Reboot waiting: {state}", "os_auto": "Automatic security updates: {state}",
        "os_health": "dpkg: {dpkg} · apt: {apt} · package manager busy: {busy}",
        "os_units": "Hermes' services after a reboot: {units}", "os_ready": "Ready to update: {state}",
        "os_findings": {"package_manager_busy": "the package manager is busy",
                        "package_manager_stuck": "a package manager is stuck", "dpkg_interrupted": "an update stopped half-way",
                        "apt_broken": "broken dependencies", "held_by_plugin": "packages held by a stopped update",
                        "disk_low": "little free disk", "reboot_required": "a reboot is waiting",
                        "security_updates": "security updates are waiting", "unattended_off": "automatic security updates are off",
                        "lists_stale": "old package lists", "unit_not_enabled": "a Hermes service is not enabled",
                        "user_units_without_linger": "the gateway would not start after a reboot",
                        "unsupported": "a system the plugin does not update"},
        "os_menu": ["Check again", "Plan (refresh the lists and simulate the update)",
                    "Update now (your terminal, as root)", "Repair an update that stopped half-way",
                    "Show the last update's report"],
        "os_plan": "Packages: {upgrade} to upgrade, {install}, {remove} to remove, {kept}.",
        "n_new": ("{n} new", "{n} new"), "n_kept": ("{n} kept back", "{n} kept back"),
        "os_attention": {"restarts_containers": "restarts the containers (Docker)", "needs_reboot": "needs a reboot",
                         "restarts_web": "restarts the web server", "remote_access": "remote access (SSH/sudo)",
                         "boot": "the boot loader", "package_manager": "the package manager itself"},
        "os_skip": "Packages to leave out this time, separated by spaces (Enter = none): ",
        "os_upgrade_q": ("Update 1 package now? A list of every package and a copy of /etc are kept first.",
                         "Update {n} packages now? A list of every package and a copy of /etc are kept first."),
        "os_running": "Updating in its own service ({unit}); Ctrl-C leaves it running.",
        "os_progress": ("  {n} step done…", "  {n} steps done…"),
        "os_background": "The update goes on in the background. For its report, come back to this screen: option 5.",
        "os_report": "Exit {exit} · {changed} · dpkg {dpkg} · apt {apt} · Hermes {hermes} · reboot waiting: {reboot}",
        "n_changed": ("{n} package changed", "{n} packages changed"),
        "os_stopped": "Services that were running and stopped: {units}", "os_folder": "Report and copies: {folder}",
        "os_stopped_clean": "Services that were running and ended without an error (most stop when idle; start the ones that should run): {units}",
        "os_released": "Released the packages a stopped update still held: {names}",
        "os_repair_q": "Finish the interrupted update now (dpkg --configure -a, then apt-get -f install)?",
        "os_repair_failed": "The repair ended with exit {code}: its log is in the folder below.",
        "os_no_report": "No update yet.", "os_still_running": "The update is still running; come back in a moment.",
        "os_lost": "The update stopped without finishing (it was killed, or the machine went down): repair it with this screen's repair option.",
        "yes_word": "yes", "no_word": "no", "unknown": "unknown",
        "auto_line": ("Mode: {mode} · {n} automatic action in 7 days", "Mode: {mode} · {n} automatic actions in 7 days"),
        "attention_title": "Needs attention:", "suggest_title": "Suggestions:", "letter_hint": " (letter {letter} below)",
        "suggest_waiting": ("Decide 1 lesson waiting for you", "Decide {n} lessons waiting for you"),
        "suggest_apply": ("Apply 1 recommended change: {what}", "Apply {n} recommended changes: {what}"),
        "suggest_full": "Turn on Auto mode (the plugin thinks and acts for you, in every profile)",
        "suggest_memory": ("Tidy Hermes' memory: 1 repeat", "Tidy Hermes' memory: {n} repeats"),
        "suggest_advice": ("Read 1 observation with its numbers", "Read {n} observations with their numbers"),
        "apply_all_q": ("Apply this change to Hermes now? It can be undone in the history.",
                        "Apply these {n} changes to Hermes now? Each one can be undone in the history."),
        "applied": "Applied. To undo: {undo} (or in the history, option 4).", "applied_n": ("Applied 1.", "Applied {n}."),
        "already": "It was already so in Hermes (the skill was already locked or pinned): nothing to undo.",
        "auto_names": {"auto": "Auto", "assist": "Assisted", "manual": "Manual"},
        "auto_title": "Mode", "auto_q": "Switch to the mode “{mode}”?",
        "auto_now": "Now: {mode}.",
        "auto_modes": ["Auto (default): Atlas Evolution thinks and acts for you, in every profile of this Hermes. By itself"
                       " it removes duplicate memory entries (keeps a copy of the file first), locks skills the curator"
                       " keeps rewriting, installs itself"
                       " (same version) in profiles in use that never had it, moves folders of failed installs to its"
                       " trash and frees old tool versions once a week. What could break something waits for you, with"
                       " its reason and the key or command to apply.",
                       "Assisted: on its own it only removes duplicate memory entries; everything else waits for you.",
                       "Manual: it measures and proposes; nothing changes by itself."],
        "auto_rules": "Each action goes to the history and can be undone; an automatic change you undo is never repeated.",
        "auto_recent": "Automatic actions of the last 7 days:", "auto_none": "No automatic action in the last 7 days.",
        "auto_prompt": "Decision number to undo · m change the mode · Enter back: ",
        "mode_prompt": "1 Auto · 2 Assisted · 3 Manual · Enter keeps it: ",
        "group_change": "Changes the plugin applies (reversible):", "group_lesson": "Lessons:",
        "group_manual": "Changes you apply:", "group_advice": "Observations:",
        "proposals_prompt_all": ("Number to see · t apply the change · Enter back: ",
                                 "Number to see · t apply the {n} changes · Enter back: "),
        "proposal_ref": "Proposal #{id}", "list_ref": "(proposal #{id})", "risk_label": "risk: {risk}",
        "profile_subject": "profile {profile}",
        "seen": "What was seen", "does": "What it does", "undo_how": "To undo", "look_with": "See it with",
        "skip_busy": "not applied: the Evolution store was busy; try again in a moment",
        "skip_one_by_one": "not applied with the others: open it on its own and choose which plugin to keep",
        "invalid_key": "That key is not one of the options shown: nothing was done.",
        "number_error": "“{label}” takes a whole number, such as 10.",
        "waiting_attention": "Lessons are waiting for your approval: option 1 of this menu.",
        "change_prompt": "a apply now · r reject · Enter back: ",
        "advice_prompt": "d dismiss · Enter back: ", "failure_prompt": "e write the lesson · d dismiss · Enter back: ",
        "dismissed": "Dismissed for 30 days; it comes back only if the situation is still there.",
        "lesson_text": "The lesson, one sentence of what to do instead (Enter cancels): ",
        "lesson_activate": "Activate it for the agent now?", "lesson_added": "Lesson #{id} added ({status}).",
        "lesson_added_active": "active: the agent reads it from its next session on",
        "lesson_added_waiting": "waiting for you: option 1 of this menu",
        "quit": "Quit", "choose": "Choose: ", "invalid": "Type one of the numbers shown.", "bye": "Bye.",
        "invalid_main": "Type a number or a letter from the list.",
        "enter": "Press Enter to go back. ", "yes": ("y", "yes"), "yn": " [y/N] ",
        "error": "Not done: {message}",
        "waiting_title": "Lessons waiting for you", "waiting_empty": "Nothing waits for you.",
        "agent_single": "proposed by the agent: decide it separately",
        "waiting_prompt": "Number to decide · t approve all ({n}) · Enter back: ",
        "waiting_prompt_one": "Number to decide · Enter back: ",
        "lesson_prompt": "a approve · r reject · Enter back: ",
        "approve_all": ("Approve this lesson for the agent?", "Approve these {n} lessons for the agent?"),
        "approved": "Approved: the agent reads it from its next session on.", "rejected": "Rejected.",
        "approved_n": ("Approved 1.", "Approved {n}."), "skipped_n": ("1 not approved: it changed since you saw it.",
                                                    "{n} not approved: they changed since you saw them."),
        "changed": "It reads differently now (a pass changed it): nothing was decided. Look at it again.",
        "proposals_title": "Open proposals", "proposals_empty": "No open proposals.",
        "proposals_prompt": "Number to see and decide · Enter back: ", "proposal_prompt": "a approve · r reject · Enter back: ",
        "run_after": "Approved. Hermes changes this with the command below; run it in your terminal:",
        "recorded": "Recorded.", "command": "Command", "lesson": "Lesson",
        "active_title": "Active lessons", "active_empty": "No active lesson yet.",
        "active_prompt": "Number to retire · Enter back: ", "retire_q": "Retire this lesson?",
        "retired": "Retired: the agent stops reading it from its next session on.",
        "effect": "failures per call {before} → {after}", "verdict": {"effective": "helps", "ineffective": "does not help",
                                                                   "inconclusive": "inconclusive"},
        "history_title": "Your latest decisions", "history_empty": "No decision yet.",
        "history_prompt": "Decision number to undo · Enter back: ", "undo_q": "Undo decision {id}?",
        "undone": "Undone.", "undone_mark": "undone",
        "reasons": {"changed_since": "Not undone: it changed since (a later decision or a pass). Nothing changed.",
                    "nothing_to_undo": "Not undone: there is nothing to undo for this one (Hermes' own maintenance).",
                    "already_undone": "It was already undone.", "unknown_current": "Not undone: the current value is unknown.",
                    "no_room": "Not undone: the memory file has no room for it now.",
                    "no_copy": "Not undone: the copy of the memory file is gone."},
        "where": {"terminal": "terminal", "dashboard": "panel", "automatic": "automatic"},
        "did": {"approve": "approved", "reject": "rejected", "retire": "retired", "reactivate": "reactivated",
                "set": "changed", "remove": "removed", "apply": "applied"},
        "what_lesson": "{did} lesson #{target}", "what_proposal": "{did} proposal #{target}",
        "what_cron": "scheduled {target} ({schedule}) as a Hermes task",
        "what_setting": "changed {target}: {before} → {after}", "what_memory": "removed a duplicate entry from {target}",
        "settings_title": "Settings", "settings_prompt": "Number to change · r apply the recommended setup · Enter back: ",
        "toggle_q": "Turn “{label}” {state}?", "choice_prompt": "{options} · Enter keeps it: ",
        "number_prompt": "New value, Enter keeps {value}: ", "list_prompt": "Values separated by commas, - clears, Enter keeps: ",
        "saved": "Saved. It applies within a minute, without restarting.",
        "reload": ("Applies after hermes gateway restart (in the terminal, at the next session): {keys}.",
                   "Apply after hermes gateway restart (in the terminal, at the next session): {keys}."),
        "recommended_title": "The recommended setup changes:", "recommended_none": "This profile already has the recommended setup.",
        "apply_q": "Apply?", "nothing": "Nothing changed.", "guard_warn": "Turning the Runtime guard off lets the agent restart or delete what Hermes runs on.",
        "owner_title": "Which chats count as you (last 30 days)",
        "owner_hint": "Turns from the marked channels count as yours: you get every lesson there, and your failures there teach. Mark only what nobody else uses.",
        "owner_none": "No chat channel in 30 days: your CLI, TUI and Desktop always count as you.",
        "owner_prompt": "Number to mark or unmark as you · Enter back: ", "whole": "every chat of this channel",
        "sessions": ("{n} session", "{n} sessions"), "is_you": "counts as you", "suggested": "suggested",
        "mark_q": "Count {what} as you?", "unmark_q": "Stop counting {what} as you?",
        "memory_title": "Hermes memory", "memory_use": "{file}: {entries} · {chars} of {limit} characters ({percent}%){off}",
        "n_entries": ("{n} entry", "{n} entries"),
        "memory_off": " · turned off in Hermes", "memory_missing": "{file}: no entries yet",
        "memory_none": "Nothing repeats. Each entry appears once.",
        "memory_issues": "What repeats (removing one saves about {tokens} tokens on every turn):",
        "memory_issues_free": "What repeats (Hermes already sends an identical repeat only once; removing it tidies the file):",
        "list_changed": "It changed meanwhile (the panel or another terminal): look at it again; nothing was written.",
        "kinds": {"exact": "same entry twice", "variant": "the same entry, differing only in case or spacing",
                  "contained": "one entry says what the other says", "similar": "very similar entries"},
        "memory_prompt": "Number to choose which one stays · Enter back: ",
        "keep_prompt": "1 keep the first (removes the second) · 2 keep the second · Enter back: ",
        "memory_removed": "Removed. A copy of the file is in evolution/memory-backups; to undo it: option 4 of this menu (history). New sessions read the change.",
        "memory_changed": "The entry changed since you saw it: nothing was removed.",
        "memory_note": "Merging two entries into new words is the agent's job: ask it to rewrite them with its memory tool.",
        "learn_running": "Running a learning pass (up to {minutes} minutes)…",
        "learn_done": "Done: read {sessions} and {calls} · {new} · {open}.",
        "n_calls": ("{n} tool call", "{n} tool calls"), "n_new_lessons": ("{n} new lesson", "{n} new lessons"),
        "learn_next": "To decide: option 1 (lessons waiting for you) and option 2 (open proposals).",
        "learn_quiet": "Done: nothing new.",
    },
    "pt": {
        "title": "Atlas Evolution {version} · perfil {profile}",
        "summary": "Lições: {lessons} · {active} · {waiting} · {proposals}",
        "n_active": ("{n} ativa", "{n} ativas"), "n_waiting": ("{n} esperando você", "{n} esperando você"),
        "n_proposals": ("{n} proposta aberta", "{n} propostas abertas"),
        "lesson_modes": {"auto": "automáticas", "suggest": "com aprovação", "off": "desligadas"},
        "loop": "Última rodada: {last} · Proteção do Hermes {guard} · refletor {reflector}",
        "never": "ainda não", "ok": "ok", "failed": "falhou", "on": "ligado", "off": "desligado", "guard_on": "ligada",
        "guard_off": "desligada", "yes_value": "sim", "no_value": "não",
        "items": ["Lições esperando você ({waiting})", "Propostas abertas ({proposals})", "Lições ativas ({active})",
                  "Histórico de decisões e desfazer", "Modo e ações automáticas ({auto})", "Configurações",
                  "Quem é você nos chats", "Memória do Hermes: repetições e espaço", "Resumo dos últimos 7 dias",
                  "Situação completa", "Fazer uma rodada de aprendizado agora"],
        "items_more": ["Saúde do Hermes (sem agente, sem tokens)", "Sistema operacional ({os})",
                       "Ambiente: todos os perfis, plugins e skills ({profiles})"],
        "suggest_digest": "Agendar o resumo semanal no seu chat: uma tarefa agendada do Hermes (cron), sem agente, sem tokens",
        "suggest_os_repair": "Terminar a atualização do sistema que parou no meio",
        "suggest_env": ("Ver 1 coisa para arrumar nos seus perfis (plugins, skills)",
                        "Ver {n} coisas para arrumar nos seus perfis (plugins, skills)"),
        "advice": "O que fazer",
        "what_maintenance": "rodou a manutenção do próprio Hermes: hermes {target} (nada a desfazer)",
        "care_gc_blocked": ("Agora não: o hermes pm gc apagaria {folders}, de onde um serviço do Hermes ainda inicia. "
                            "Aponte o serviço para o Python atual antes: hermes evolution care fix-gateway."),
        "what_sync": "levou o Atlas Evolution do perfil {profile} ao código do perfil que cuida de todos ({before} → {after}){failed}",
        "sync_failed": "; o Hermes recusou, nova tentativa amanhã",
        "what_gateway": ("apontou o serviço do gateway {unit} para o Python que o Hermes usa agora ({before} "
                         "→ {after}){failed}; desfazer: hermes evolution care undo-gateway"),
        "gateway_failed": " (o gateway não subiu)",
        "and_more": (" (+{n} outro tipo)", " (+{n} outros tipos)"),
        "env_title": "Ambiente: todos os perfis deste Hermes",
        "env_mode": "Quem cuida: perfil {lead} (modo dele: {mode}; cada outro perfil com o plugin segue o próprio) · última verificação: {when}",
        "env_lead_elsewhere": "O perfil {lead} cuida de todos os perfis; esta tela mostra e decide por meio dele.",
        "env_unseen": "ainda não verificado",
        "env_doctor": "Checagem do Hermes: {fail}, {warn}, {optional} (verificado em {when})",
        "env_doctor_failed": "Checagem do Hermes: não rodou ({error}; verificado em {when})",
        "n_fail": ("{n} problema", "{n} problemas"), "n_warn": ("{n} aviso", "{n} avisos"),
        "n_optional": ("{n} opcional não configurado", "{n} opcionais não configurados"),
        "env_none": "Nenhum perfil tem o Atlas Evolution ligado, então ninguém cuida do Hermes inteiro ainda. Ligue-o no"
                    " perfil que você mais usa: hermes -p <perfil> plugins enable hermes-atlas-evolution.",
        "env_not_yet": "Ainda não verificado: tecle v para verificar agora (uns 15 segundos num Hermes grande).",
        "env_catalog": "Catálogo de plugins do Hermes: {plugins} plugins ({tiers}) · {removed}",
        "n_removed": ("{n} saiu do catálogo", "{n} saíram do catálogo"),
        "env_tiers": {"official": "oficiais", "verified": "verificados", "community": "da comunidade"},
        "env_profiles": "Perfis ({n}):",
        "env_row": "{profile:<18} {sessions} em 7 dias · plugins: {on}, {idle} · {skills} · Atlas Evolution: {self}",
        "n_row_sessions": ("{n:>4} sessão", "{n:>4} sessões"), "n_on": ("{n} ligado", "{n} ligados"),
        "n_idle": ("{n} nunca carregado", "{n} nunca carregados"), "n_skills": ("{n} skill", "{n} skills"),
        "env_todo": "O que fazer ({n}), do mais importante para o menos:",
        "env_clean": "Nada para arrumar: nenhum conflito, duplicidade, plugin removido ou resto de instalação.",
        "env_prompt": "Número para ver e decidir · v verificar de novo agora · Enter volta: ",
        "env_scanning": "Verificando todos os perfis…",
        "env_auto_mark": "⚙ = o Modo Auto faz sozinho na próxima rodada de aprendizado (e você pode desfazer no histórico, opção 4).",
        "suggest_os_security": ("Instalar 1 atualização de segurança do sistema",
                                "Instalar {n} atualizações de segurança do sistema"),
        "suggest_unused": "Liberar {size} de versões antigas das ferramentas do Hermes",
        "deliver_prompt": "Entregar onde? local, ou um canal como telegram (Enter = local): ",
        "digest_done": "Agendado como tarefa agendada do Hermes (cron), sem agente: segundas às 08:00 (tarefa {job}). Para desfazer: opção 4 deste menu (histórico).",
        "digest_already": "O resumo semanal já está agendado.",
        "care_title": "Saúde do Hermes (lida dos próprios arquivos do Hermes: sem agente, sem rede, sem tokens)",
        "care_gateway": "Gateway: {state}",
        "gate_running": "rodando", "gate_stopped": "parado", "gate_crashed": "parado (caiu)",
        "gate_degraded": "rodando (degradado: nenhuma plataforma de chat conectada ainda; as tarefas agendadas seguem "
                         "rodando)",
        "gate_starting": "subindo",
        "care_gc_unsafe": "Agora não: {reasons}. Confira o depósito de ferramentas com hermes pm doctor.",
        "care_gc_recheck": "Agora não: a última checagem de saúde não diz o que o hermes pm gc apagaria. Escolha 0 para "
                           "checar de novo antes.",
        "gate_version": "versão {version}", "gate_rss": "memória {rss}", "gate_beat": "sinal de vida há {age}",
        "no_data": "sem dados", "not_checked": "ainda não verificada",
        "care_db": "Banco de sessões: {journal} · WAL {wal} · integridade {quick}",
        "journals": {"wal": "modo WAL", "delete": "registro de reversão", "truncate": "registro de reversão",
                     "persist": "registro de reversão", "memory": "registro em memória", "off": "sem registro"},
        "integrity_bad": "com problema (veja Pede atenção)",
        "care_disk": "Disco onde o Hermes grava: {free} livres de {total}{pace}",
        "care_pace": " · perde {per_day} por dia{full}", "care_full": " (enche em {days})",
        "n_days": ("{n} dia", "{n} dias"), "under_a_day": "menos de um dia",
        "care_memory": "Memória: {available} livres de {total} · swap {used} de {swap} · carga {load} em {cpus} CPUs",
        "care_folders": "Pastas maiores: {folders}",
        "care_unused": "Versões antigas de ferramentas que nada usa ({size}): {list}",
        "care_none": "Nada pede atenção.",
        "care_menu": ["Checar de novo agora (sem rede)", "Integridade da instalação (hermes pm doctor, sem rede)",
                      "Checagem completa do Hermes (hermes doctor): chama cada provedor configurado, roda o npm audit"
                      " contra o registro do npm, consulta o gh auth status e importa o painel num subprocesso. Usa a"
                      " rede, pode levar minutos e não muda nada.",
                      "Reparar com o Hermes (hermes backup --quick e depois hermes doctor --fix)",
                      "Liberar versões antigas de ferramentas (hermes pm gc)"],
        "care_prompt": "Número · Enter volta: ", "care_q": "Rodar “{command}” agora?",
        "care_ok": "Feito (saída {code}).", "care_failed": "O Hermes respondeu com saída {code}{note}:",
        "care_fix_note": ["O que o hermes doctor --fix pode mudar: criar pastas que faltam e o arquivo .env, migrar o"
                          " config.yaml (o Hermes guarda uma cópia antes), reparar o banco de sessões, recriar o"
                          " lançador hermes e baixar o Chromium se a ferramenta de navegador precisar.",
                          "Antes disso, o hermes backup --quick guarda o config.yaml, o .env, o banco de sessões e as"
                          " tarefas agendadas (cron); essa cópia os devolve (num chat do Hermes: /snapshot restore <id>)."],
        "care_backup_failed": "A cópia de segurança falhou (saída {code}), então nada foi reparado{note}:",
        "doctor_found": ("A checagem do Hermes (hermes doctor) encontrou {n} problema{note}:",
                         "A checagem do Hermes (hermes doctor) encontrou {n} problemas{note}:"),
        "doctor_found_some": "A checagem do Hermes (hermes doctor) encontrou problemas{note}:",
        "doctor_fixed": "A checagem do Hermes (hermes doctor) reparou o que pôde; falta fazer à mão{note}:",
        "doctor_clean": "A checagem do Hermes (hermes doctor) não encontrou nada para arrumar.",
        "doctor_warnings": "Avisos{note}:",
        "doctor_actions": "O que o Hermes diz para fazer{note}:", "hermes_words": " (texto do Hermes, em inglês)",
        "os_title": "Sistema operacional",
        "os_unsupported": "Este sistema não é um que o plugin atualiza (Linux com apt: Ubuntu, Debian).",
        "os_line": "{name} · kernel {kernel}",
        "os_updates": "Atualizações esperando: {count} ({security} de segurança) · listas de pacotes de {when}",
        "os_reboot": "Reinício pendente: {state}", "os_auto": "Atualizações automáticas de segurança: {state}",
        "os_health": "dpkg: {dpkg} · apt: {apt} · gerenciador de pacotes ocupado: {busy}",
        "os_units": "Serviços do Hermes depois de um reinício: {units}", "os_ready": "Pronto para atualizar: {state}",
        "os_findings": {"package_manager_busy": "o gerenciador de pacotes está ocupado",
                        "package_manager_stuck": "um gerenciador de pacotes está travado",
                        "dpkg_interrupted": "uma atualização parou no meio", "apt_broken": "dependências quebradas",
                        "held_by_plugin": "pacotes retidos por uma atualização interrompida",
                        "disk_low": "pouco espaço em disco", "reboot_required": "há um reinício pendente",
                        "security_updates": "há atualizações de segurança esperando",
                        "unattended_off": "as atualizações automáticas de segurança estão desligadas",
                        "lists_stale": "listas de pacotes antigas", "unit_not_enabled": "um serviço do Hermes não está habilitado",
                        "user_units_without_linger": "o gateway não subiria depois de um reinício",
                        "unsupported": "um sistema que o plugin não atualiza"},
        "os_menu": ["Checar de novo", "Planejar (atualizar as listas e simular a atualização)",
                    "Atualizar agora (no seu terminal, como root)", "Reparar uma atualização que parou no meio",
                    "Ver o relatório da última atualização"],
        "os_plan": "Pacotes: {upgrade} para atualizar, {install}, {remove} para remover, {kept}.",
        "n_new": ("{n} novo", "{n} novos"), "n_kept": ("{n} retido", "{n} retidos"),
        "os_attention": {"restarts_containers": "reinicia os containers (Docker)", "needs_reboot": "pede reinício",
                         "restarts_web": "reinicia o servidor web", "remote_access": "acesso remoto (SSH/sudo)",
                         "boot": "o carregador de boot", "package_manager": "o próprio gerenciador de pacotes"},
        "os_skip": "Pacotes para deixar de fora desta vez, separados por espaço (Enter = nenhum): ",
        "os_upgrade_q": ("Atualizar 1 pacote agora? Antes, guarda a lista de todos os pacotes e uma cópia do /etc.",
                         "Atualizar {n} pacotes agora? Antes, guarda a lista de todos os pacotes e uma cópia do /etc."),
        "os_running": "Atualizando num serviço próprio ({unit}); Ctrl-C deixa a atualização seguir.",
        "os_progress": ("  {n} etapa concluída…", "  {n} etapas concluídas…"),
        "os_background": "A atualização segue em segundo plano. Para ver o relatório, volte a esta tela: opção 5.",
        "os_report": "Saída {exit} · {changed} · dpkg {dpkg} · apt {apt} · Hermes {hermes} · reinício pendente: {reboot}",
        "n_changed": ("{n} pacote mudou", "{n} pacotes mudaram"),
        "os_stopped": "Serviços que rodavam e pararam: {units}", "os_folder": "Relatório e cópias: {folder}",
        "os_stopped_clean": "Serviços que rodavam e terminaram sem erro (a maioria para quando fica ociosa; suba os que deveriam rodar): {units}",
        "os_released": "Liberados os pacotes que uma atualização interrompida ainda retinha: {names}",
        "os_repair_q": "Terminar a atualização interrompida agora (dpkg --configure -a e depois apt-get -f install)?",
        "os_repair_failed": "O reparo terminou com saída {code}: o registro dele está na pasta abaixo.",
        "os_no_report": "Nenhuma atualização ainda.", "os_still_running": "A atualização ainda está rodando; volte daqui a pouco.",
        "os_lost": "A atualização parou sem terminar (foi morta, ou a máquina caiu): repare-a pela opção de reparo desta tela.",
        "yes_word": "sim", "no_word": "não", "unknown": "desconhecido",
        "auto_line": ("Modo: {mode} · {n} ação automática em 7 dias", "Modo: {mode} · {n} ações automáticas em 7 dias"),
        "attention_title": "Pede atenção:", "suggest_title": "Sugestões:", "letter_hint": " (letra {letter} abaixo)",
        "suggest_waiting": ("Decidir 1 lição esperando você", "Decidir {n} lições esperando você"),
        "suggest_apply": ("Aplicar 1 mudança recomendada: {what}", "Aplicar {n} mudanças recomendadas: {what}"),
        "suggest_full": "Ligar o Modo Auto (o plugin pensa e age por você, em todos os perfis)",
        "suggest_memory": ("Organizar a memória do Hermes: 1 repetição", "Organizar a memória do Hermes: {n} repetições"),
        "suggest_advice": ("Ler 1 observação com os números", "Ler {n} observações com os números"),
        "apply_all_q": ("Aplicar esta mudança no Hermes agora? Ela pode ser desfeita no histórico.",
                        "Aplicar estas {n} mudanças no Hermes agora? Cada uma pode ser desfeita no histórico."),
        "applied": "Aplicada. Para desfazer: {undo} (ou pelo histórico, opção 4).", "applied_n": ("1 aplicada.", "{n} aplicadas."),
        "already": "Já estava assim no Hermes (a skill já estava travada ou fixada): nada a desfazer.",
        "auto_names": {"auto": "Auto", "assist": "Assistido", "manual": "Manual"},
        "auto_title": "Modo", "auto_q": "Passar para o modo “{mode}”?",
        "auto_now": "Agora: {mode}.",
        "auto_modes": ["Auto (padrão): o Atlas Evolution pensa e age por você, em todos os perfis deste Hermes. Sozinho ele"
                       " remove entradas repetidas da memória (guarda uma cópia do arquivo antes), trava skills que o"
                       " curador reescreve sem parar, se instala"
                       " (na mesma versão) nos perfis em uso que nunca o tiveram, move para a lixeira pastas de"
                       " instalações que falharam e libera versões antigas de ferramentas uma vez por semana. O que"
                       " poderia quebrar algo espera você, com o motivo e a tecla ou o comando para aplicar.",
                       "Assistido: sozinho, só remove entradas repetidas da memória; o resto espera você.",
                       "Manual: mede e propõe; nada muda sozinho."],
        "auto_rules": "Cada ação vai para o histórico e pode ser desfeita; uma mudança automática que você desfaz nunca se repete.",
        "auto_recent": "Ações automáticas dos últimos 7 dias:", "auto_none": "Nenhuma ação automática nos últimos 7 dias.",
        "auto_prompt": "Número da decisão para desfazer · m mudar o modo · Enter volta: ",
        "mode_prompt": "1 Auto · 2 Assistido · 3 Manual · Enter mantém: ",
        "group_change": "Mudanças que o plugin aplica (reversíveis):", "group_lesson": "Lições:",
        "group_manual": "Mudanças que você aplica:", "group_advice": "Observações:",
        "proposals_prompt_all": ("Número para ver · t aplicar a mudança · Enter volta: ",
                                 "Número para ver · t aplicar as {n} mudanças · Enter volta: "),
        "proposal_ref": "Proposta #{id}", "list_ref": "(proposta #{id})", "risk_label": "risco: {risk}",
        "profile_subject": "perfil {profile}",
        "seen": "O que foi visto", "does": "O que faz", "undo_how": "Para desfazer", "look_with": "Veja com",
        "skip_busy": "não aplicada: o banco do Evolution estava ocupado; tente de novo daqui a pouco",
        "skip_one_by_one": "não aplicada com as outras: abra-a sozinha e escolha qual plugin fica",
        "invalid_key": "Essa tecla não é uma das opções mostradas: nada foi feito.",
        "number_error": "“{label}” aceita só um número inteiro, como 10.",
        "waiting_attention": "Há lições esperando a sua aprovação: opção 1 deste menu.",
        "change_prompt": "a aplicar agora · r rejeitar · Enter volta: ",
        "advice_prompt": "d dispensar · Enter volta: ", "failure_prompt": "e escrever a lição · d dispensar · Enter volta: ",
        "dismissed": "Dispensada: some por 30 dias e só volta se a situação continuar.",
        "lesson_text": "A lição, uma frase do que fazer no lugar (Enter cancela): ",
        "lesson_activate": "Ativar para o agente agora?", "lesson_added": "Lição #{id} adicionada ({status}).",
        "lesson_added_active": "ativa: o agente passa a lê-la a partir da próxima sessão",
        "lesson_added_waiting": "esperando você: opção 1 deste menu",
        "quit": "Sair", "choose": "Escolha: ", "invalid": "Digite um dos números mostrados.", "bye": "Até logo.",
        "invalid_main": "Digite um número ou uma letra da lista.",
        "enter": "Enter para voltar. ", "yes": ("s", "sim", "y", "yes"), "yn": " [s/N] ",
        "error": "Não feito: {message}",
        "waiting_title": "Lições esperando você", "waiting_empty": "Nada espera você.",
        "agent_single": "proposta pelo agente: decida esta separadamente",
        "waiting_prompt": "Número para decidir · t aprovar todas ({n}) · Enter volta: ",
        "waiting_prompt_one": "Número para decidir · Enter volta: ",
        "lesson_prompt": "a aprovar · r rejeitar · Enter volta: ",
        "approve_all": ("Aprovar esta lição para o agente?", "Aprovar estas {n} lições para o agente?"),
        "approved": "Aprovada: o agente passa a lê-la a partir da próxima sessão.", "rejected": "Rejeitada.",
        "approved_n": ("1 aprovada.", "{n} aprovadas."),
        "skipped_n": ("1 não aprovada: mudou desde que você a viu.", "{n} não aprovadas: mudaram desde que você as viu."),
        "changed": "Ela está diferente agora (uma rodada a mudou): nada foi decidido. Veja de novo.",
        "proposals_title": "Propostas abertas", "proposals_empty": "Nenhuma proposta aberta.",
        "proposals_prompt": "Número para ver e decidir · Enter volta: ", "proposal_prompt": "a aprovar · r rejeitar · Enter volta: ",
        "run_after": "Aprovada. O Hermes muda isso com o comando abaixo; rode-o no seu terminal:",
        "recorded": "Registrado.", "command": "Comando", "lesson": "Lição",
        "active_title": "Lições ativas", "active_empty": "Nenhuma lição ativa ainda.",
        "active_prompt": "Número para aposentar · Enter volta: ", "retire_q": "Aposentar esta lição?",
        "retired": "Aposentada: o agente deixa de lê-la a partir da próxima sessão.",
        "effect": "falhas por chamada {before} → {after}", "verdict": {"effective": "ajuda", "ineffective": "não ajuda",
                                                                     "inconclusive": "inconclusivo"},
        "history_title": "Suas últimas decisões", "history_empty": "Nenhuma decisão ainda.",
        "history_prompt": "Número da decisão para desfazer · Enter volta: ", "undo_q": "Desfazer a decisão {id}?",
        "undone": "Desfeito.", "undone_mark": "desfeita",
        "reasons": {"changed_since": "Não desfeito: mudou depois (outra decisão ou uma rodada). Nada mudou.",
                    "nothing_to_undo": "Não desfeito: não há nada a desfazer nesta (manutenção do próprio Hermes).",
                    "already_undone": "Já estava desfeita.", "unknown_current": "Não desfeito: o valor atual é desconhecido.",
                    "no_room": "Não desfeito: o arquivo de memória não tem espaço para ela agora.",
                    "no_copy": "Não desfeito: a cópia do arquivo de memória não existe mais."},
        "where": {"terminal": "terminal", "dashboard": "painel", "automatic": "automático"},
        "did": {"approve": "aprovou", "reject": "rejeitou", "retire": "aposentou", "reactivate": "reativou",
                "set": "mudou", "remove": "removeu", "apply": "aplicou"},
        "what_lesson": "{did} a lição #{target}", "what_proposal": "{did} a proposta #{target}",
        "what_cron": "agendou {target} ({schedule}) como tarefa do Hermes",
        "what_setting": "mudou {target}: {before} → {after}", "what_memory": "removeu uma entrada repetida de {target}",
        "settings_title": "Configurações", "settings_prompt": "Número para mudar · r aplicar a configuração recomendada · Enter volta: ",
        "toggle_q": "{state} “{label}”?", "choice_prompt": "{options} · Enter mantém: ",
        "number_prompt": "Novo valor, Enter mantém {value}: ", "list_prompt": "Valores separados por vírgula, - limpa, Enter mantém: ",
        "saved": "Salvo. Vale em até um minuto, sem reiniciar.",
        "reload": ("Vale depois de hermes gateway restart (no terminal, na próxima sessão): {keys}.",
                   "Valem depois de hermes gateway restart (no terminal, na próxima sessão): {keys}."),
        "recommended_title": "A configuração recomendada muda:", "recommended_none": "Este perfil já está com a configuração recomendada.",
        "apply_q": "Aplicar?", "nothing": "Nada mudou.", "guard_warn": "Desligar a Proteção do Hermes deixa o agente reiniciar ou apagar aquilo em que o Hermes roda.",
        "owner_title": "Quem é você nos chats (últimos 30 dias)",
        "owner_hint": "Os turnos dos canais marcados contam como seus: ali você recebe todas as lições, e as suas falhas ensinam. Marque só o que ninguém mais usa.",
        "owner_none": "Nenhum canal de chat em 30 dias: a sua CLI, TUI e Desktop sempre contam como você.",
        "owner_prompt": "Número para marcar ou desmarcar como você · Enter volta: ", "whole": "todo chat deste canal",
        "sessions": ("{n} sessão", "{n} sessões"), "is_you": "conta como você", "suggested": "sugerido",
        "mark_q": "Contar {what} como você?", "unmark_q": "Deixar de contar {what} como você?",
        "memory_title": "Memória do Hermes", "memory_use": "{file}: {entries} · {chars} de {limit} caracteres ({percent}%){off}",
        "n_entries": ("{n} entrada", "{n} entradas"),
        "memory_off": " · desligada no Hermes", "memory_missing": "{file}: nenhuma entrada ainda",
        "memory_none": "Nada se repete. Cada entrada aparece uma vez.",
        "memory_issues": "O que se repete (remover uma economiza cerca de {tokens} tokens em todo turno):",
        "memory_issues_free": "O que se repete (o Hermes já envia uma repetição idêntica uma vez só; removê-la arruma o arquivo):",
        "list_changed": "Mudou nesse meio-tempo (o painel ou outro terminal): veja de novo; nada foi gravado.",
        "kinds": {"exact": "a mesma entrada duas vezes", "variant": "a mesma entrada, diferindo só em maiúsculas ou espaços",
                  "contained": "uma entrada diz o que a outra já diz", "similar": "entradas muito parecidas"},
        "memory_prompt": "Número para escolher qual fica · Enter volta: ",
        "keep_prompt": "1 manter a primeira (remove a segunda) · 2 manter a segunda · Enter volta: ",
        "memory_removed": "Removida. Uma cópia do arquivo está em evolution/memory-backups; para desfazer: opção 4 deste menu (histórico). As novas sessões já leem a mudança.",
        "memory_changed": "A entrada mudou desde que você a viu: nada foi removido.",
        "memory_note": "Juntar duas entradas em palavras novas é tarefa do agente: peça que ele as reescreva com a ferramenta de memória.",
        "learn_running": "Fazendo uma rodada de aprendizado (até {minutes} minutos)…",
        "learn_done": "Pronto: {sessions} e {calls} lidas · {new} · {open}.",
        "n_calls": ("{n} chamada de ferramenta", "{n} chamadas de ferramenta"),
        "n_new_lessons": ("{n} lição nova", "{n} lições novas"),
        "learn_next": "Para decidir: opção 1 (lições esperando você) e opção 2 (propostas abertas).",
        "learn_quiet": "Pronto: nada novo.",
    },
}

# The settings form, in order, as (key, label EN, label PT). Labels follow the dashboard panel's.
FIELDS = (
    ("lessons", "Lessons", "Lições"),
    ("reflector", "Reflector (an AI model drafts lessons for failures with no built-in template; drafts wait for you)",
     "Refletor (um modelo de IA rascunha lições para falhas sem padrão fixo; os rascunhos esperam você)"),
    ("reflector_daily_limit", "Reflector calls per day (1 to 50)", "Chamadas do refletor por dia (1 a 50)"),
    ("autopilot_minutes", "Learning pass every (minutes: 15 to 1440, or 0 = only by hand)",
     "Rodada de aprendizado a cada (minutos: 15 a 1440, ou 0 = só à mão)"),
    ("language", "Language", "Idioma"),
    ("feedback_signals", "Count your corrections and thanks (numbers only)",
     "Contar as suas correções e agradecimentos (só números)"),
    ("slow_turn_seconds", "A turn is slow after (seconds)", "Um turno é lento depois de (segundos)"),
    ("tool_hints", "Add the lesson to the failing tool result", "Juntar a lição ao resultado da ferramenta que falhou"),
    ("agent_tools", "Let the agent read and propose lessons (proposals still wait for you)",
     "Deixar o agente ler e propor lições (as propostas continuam esperando você)"),
    ("runtime_guard", "Runtime guard (keeps the agent from taking Hermes down)",
     "Proteção do Hermes (impede o agente de derrubar o Hermes)"),
    ("mode", "Mode: Auto, Assisted or Manual (what the plugin does by itself)",
     "Modo: Auto, Assistido ou Manual (o que o plugin faz sozinho)"),
    ("fleet_managed", "Let the lead profile act here (install, update and move to the trash)",
     "Deixar o perfil que cuida de todos agir aqui (instalar, atualizar e mover para a lixeira)"),
    ("owner_platforms", "Channels where every chat counts as you", "Canais em que todo chat conta como você"),
    ("owner_sender_ids", "Senders that count as you", "Remetentes que contam como você"),
)
# The lesson mode (`lessons`), named as the panel names it; the code (auto, suggest, off) never shows.
MODES = {"en": {"auto": "automatic (recommended): lessons from a built-in template apply by themselves",
                "suggest": "on approval: every lesson waits for you", "off": "off: only measure"},
         "pt": {"auto": "automáticas (recomendado): lições de padrão fixo valem sozinhas",
                "suggest": "com aprovação: toda lição espera você", "off": "desligadas: só medir"}}
LANGUAGES = {"en": {"auto": "auto (Hermes' language)", "en": "English", "pt": "Português"},
             "pt": {"auto": "automático (o idioma do Hermes)", "en": "English", "pt": "Português"}}
ORIGINS = {"en": {"derived": "built-in template", "operator": "you", "reflector": "reflector", "agent": "agent"},
           "pt": {"derived": "padrão fixo", "operator": "você", "reflector": "refletor", "agent": "agente"}}


class Quit(Exception):
    """The owner left the menu (0, end of input or Ctrl-C)."""


class Console:
    """Plain-text input and output, so the menu also runs from a script of answers."""

    def __init__(self, *, read=None, write=None, width: int | None = None, color: bool | None = None):
        self._read = read or input
        self._write = write or self._stdout
        columns = width or shutil.get_terminal_size((100, 24)).columns
        self.width = max(50, min(110, columns))
        if color is None:   # only on a terminal that shows it: never with NO_COLOR (no-color.org) or TERM=dumb
            color = (os.name != "nt" and sys.stdout.isatty() and not os.environ.get("NO_COLOR")
                     and os.environ.get("TERM", "").strip().lower() != "dumb")
        self.color = color

    @staticmethod
    def _stdout(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def say(self, text: str = "") -> None:
        self._write(text + "\n")

    def ask(self, prompt: str) -> str:
        try:
            return self._read(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            raise Quit from None

    def bold(self, text: str) -> str:
        return f"\033[1m{text}\033[0m" if self.color else text

    def dim(self, text: str) -> str:
        return f"\033[2m{text}\033[0m" if self.color else text

    def para(self, text: str, indent: int = 5) -> None:
        pad = " " * indent
        for line in str(text).splitlines() or [""]:
            self.say(textwrap.fill(line, width=self.width, initial_indent=pad, subsequent_indent=pad,
                                   break_on_hyphens=False) if line else "")


def _bytes(value, lang: str = "en") -> str:
    """A size as people read it: 236 KB, 1.4 GB (1,4 GB in Portuguese)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "?"
    for unit, size in (("TB", 2**40), ("GB", 2**30), ("MB", 2**20), ("KB", 2**10)):
        if abs(value) >= size:
            text = f"{value / size:.1f}" if abs(value) < 100 * size else f"{value / size:.0f}"
            return f"{text.replace('.', ',') if lang == 'pt' else text} {unit}"
    return f"{int(value)} B"


def _ago(seconds, lang: str) -> str | None:
    """How long ago, in the largest unit that reads well (45 s, 12 min, 3 h, 2 days), or None."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0:
        return None
    for limit, size, unit in ((90, 1, "s"), (90 * 60, 60, "min"), (48 * 3600, 3600, "h")):
        if seconds < limit:
            return f"{round(seconds / size)} {unit}"
    days = round(seconds / 86_400)
    return f"{days} {'dias' if lang == 'pt' else 'days'}"


def _commands_in(texts, profile: str, gateway: str):
    """The menu's own sentences with Hermes' commands as the owner types them in this profile (cli.profile_commands)."""
    from .cli import profile_commands

    if isinstance(texts, str):
        return profile_commands(texts, profile, gateway=gateway)
    if isinstance(texts, dict):
        return {key: _commands_in(value, profile, gateway) for key, value in texts.items()}
    if isinstance(texts, (list, tuple)):
        return type(texts)(_commands_in(value, profile, gateway) for value in texts)
    return texts


def _profile(home) -> str:
    """The profile's name as its commands take it (`hermes -p <name> …`)."""
    from .admin import profile_name

    return profile_name(Path(home))


def _plural(forms, n) -> str:
    """forms = (one, many), each with {n}: "1 ativa", "2 ativas"."""
    one, many = forms
    return (one if n == 1 else many).format(n=n)


# ── Markdown, as plain terminal text ───────────────────────────────────────────────────────────────────────
# `status` and the digest are written in Markdown for chats and files; in the menu they read as plain text.
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_STRONG = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_EMPHASIS = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])|(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])")
_CODE = re.compile(r"`([^`]*)`")
_TABLE_RULE = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def inline(text: str) -> str:
    """One line of Markdown without its marks: bold, emphasis, code and links."""
    text = _LINK.sub(lambda match: f"{match.group(1)} ({match.group(2)})", str(text))
    text = _STRONG.sub(lambda match: match.group(1) or match.group(2), text)
    text = _CODE.sub(lambda match: match.group(1), text)
    return _EMPHASIS.sub(lambda match: match.group(1) or match.group(2), text)


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [inline(cell.strip()) for cell in line.split("|")]


def _table(rows: list[str], width: int) -> list[str]:
    """A Markdown table in aligned columns, or, when it does not fit, one "label: value" block per row."""
    body = [_cells(row) for row in rows if not _TABLE_RULE.match(row.strip())]
    if not body:
        return []
    head, data = body[0], body[1:]
    columns = max(len(row) for row in body)
    body = [row + [""] * (columns - len(row)) for row in body]
    widths = [max(len(row[index]) for row in body) for index in range(columns)]
    if sum(widths) + 2 * (columns - 1) + 2 <= width:
        lines = ["  " + "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip() for row in body]
        lines.insert(1, "  " + "  ".join("─" * size for size in widths))
        return lines
    lines = []
    for row in data:
        row = row + [""] * (columns - len(row))
        lines.append("  • " + (f"{head[0]}: {row[0]}" if head[0] else row[0]))
        lines += [f"      {label}: {value}" for label, value in zip(head[1:], row[1:]) if value]
    return lines


def plain(markdown: str, width: int = 100) -> str:
    """Markdown as plain terminal text: headings underlined, lists with bullets and indented when nested, tables in
    columns, and no #, **, backticks or | left."""
    out: list[str] = []
    lines = str(markdown).splitlines()
    index = 0

    def wrap(text: str, first: str, rest: str) -> None:
        out.extend(textwrap.wrap(text, width=width, initial_indent=first, subsequent_indent=rest,
                                 break_on_hyphens=False) or [first.rstrip()])

    while index < len(lines):
        line = lines[index].rstrip()
        stripped = line.strip()
        if stripped.startswith("|"):
            block = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                block.append(lines[index])
                index += 1
            out.extend(_table(block, width))
            continue
        index += 1
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            title = inline(heading.group(2)).strip()
            if out and out[-1] != "":
                out.append("")
            out.append(title)
            out.append(("═" if len(heading.group(1)) == 1 else "─" if len(heading.group(1)) == 2 else "·")
                       * min(len(title), width))
            continue
        if not stripped:
            if out and out[-1] != "":
                out.append("")
            continue
        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):
            out.append("─" * min(40, width))
            continue
        bullet = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", line)
        if bullet:
            depth = len(bullet.group(1).expandtabs(4)) // 2
            mark = "•" if depth == 0 else "·"
            if bullet.group(2)[0].isdigit():
                mark = bullet.group(2)
            pad = "  " + "  " * depth
            wrap(inline(bullet.group(3)), f"{pad}{mark} ", pad + " " * (len(mark) + 1))
            continue
        if stripped.startswith(">"):
            wrap(inline(stripped.lstrip("> ").strip()), "  │ ", "  │ ")
            continue
        wrap(inline(stripped), "", "")
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


# `hermes evolution`, also as the plugin prints it since 0.12.0 with the profile: `hermes -p work evolution`,
# `hermes --profile work evolution`.
_EVOLUTION = r"hermes (?:(?:-p|--profile)[ =][\w.-]+ )?evolution"


def _command_re(pattern: str):
    """A pattern written with `hermes evolution` that also matches the command with its profile."""
    return re.compile(pattern.replace("hermes evolution", _EVOLUTION))


# Words of `status` and the digest that read better inside the menu (in Portuguese, the names the menu uses), and
# sentences that send the owner to the menu they are already in.
_IN_MENU = {
    "en": ((_command_re(r"decide in the menu \(`?hermes evolution`?\) or with `?hermes evolution lessons review`?,"
                        r" in your own terminal\."), "option 1 of this menu."),
           (_command_re(r"in your (?:own )?terminal with `hermes evolution proposals approve <id>` or "
                        r"`hermes evolution proposals reject <id>`"), "in option 2 of this menu"),
           (_command_re(r",? or in the menu \(`?hermes evolution`?\)"), ""),
           (_command_re(r"in the menu \(`?hermes evolution`?\)"), "in this menu"),
           (_command_re(r"\b(?:with )?`hermes evolution schedule-digest --deliver <channel>`"),
            "with the “Schedule the weekly digest” suggestion on this menu's first screen")),
    "pt": ((_command_re(r"decida pelo menu \(`?hermes evolution`?\) ou com `?hermes evolution lessons review`?,"
                        r" no seu próprio terminal\."), "opção 1 deste menu."),
           (_command_re(r"no seu (?:próprio )?terminal com `hermes evolution proposals approve <id>` ou "
                        r"`hermes evolution proposals reject <id>`"), "na opção 2 deste menu"),
           (_command_re(r",? ou pelo menu \(`?hermes evolution`?\)"), ""),
           (_command_re(r"(?:pelo|no) menu \(`?hermes evolution`?\)"), "neste menu"),
           (_command_re(r"\b(?:com )?`hermes evolution schedule-digest --deliver <canal>`"),
            "com a sugestão “Agendar o resumo semanal” na tela inicial deste menu"),
           (re.compile(r"\bPlaybook\b"), "Manual de lições"),
           (re.compile(r"\bpassadas\b"), "rodadas"), (re.compile(r"\bpassada\b"), "rodada"),
           (re.compile(r"\bPassada\b"), "Rodada"),
           (re.compile(r"\bCuidado pelo perfil\b"), "Quem cuida: perfil"),
           (re.compile(r"\bmodelos fixos\b"), "padrões fixos"), (re.compile(r"\bmodelo fixo\b"), "padrão fixo")),
}

# Where this menu does what `status` and the digest send the owner to with a command or a menu path, longest first:
# (the command or path, the option of the main screen, the option inside that screen, Hermes' own command it runs).
# Both the menu paths ("menu → Hermes health → `hermes pm gc`") and the commands that may replace them
# (`hermes evolution care gc`) are understood, so the menu reads the same whichever wording `status` uses.
_PLACES = (
    (r"menu → (?:Saúde do Hermes|Hermes health) → `?hermes pm gc`?|`hermes evolution care gc`", 12, 5, "hermes pm gc"),
    (r"menu → (?:Saúde do Hermes|Hermes health) → `?hermes doctor`?(?! --)|`hermes evolution care doctor`", 12, 3,
     "hermes doctor"),
    (r"menu → (?:Saúde do Hermes → reparar|Hermes health → repair)|`hermes evolution care fix`", 12, 4,
     "hermes doctor --fix"),
    (r"`hermes evolution care pm-doctor`", 12, 2, "hermes pm doctor"),
    (r"menu → (?:Saúde do Hermes|Hermes health)|`hermes evolution care(?: check)?`", 12, None, None),
    (r"menu → (?:Sistema operacional → reparar|Operating system → repair)|`hermes evolution system repair`", 13, 4, None),
    (r"`hermes evolution system plan`", 13, 2, None),
    (r"`hermes evolution system upgrade`", 13, 3, None),
    (r"`hermes evolution system report`", 13, 5, None),
    (r"menu → (?:Sistema operacional|Operating system)|`hermes evolution system(?: check)?`", 13, None, None),
    (r"menu → (?:Ambiente|Environment) → v|`hermes evolution environment scan`", 14, "v", None),
    (r"menu → (?:Ambiente|Environment)|`hermes evolution environment(?: show)?`", 14, None, None),
    (r"`hermes evolution learn`", 11, None, None),
    (r"`hermes evolution lessons review`", 1, None, None),
    (r"`hermes evolution proposals(?: list)?`", 2, None, None),
    (r"`hermes evolution owner`", 7, None, None),
    (r"`hermes evolution decisions(?: list)?`", 4, None, None),
)
# A verb, or "in your terminal, with", before the command and "in your own terminal" after it go with it.
_LEAD = {"en": r"(?P<lead>\bin your (?:own )?terminal,? with |\b(?i:with|run|use|call|open|see)\s+)?",
         "pt": r"(?P<lead>\bno seu (?:próprio )?terminal,? com (?:o\s+)?|\b(?i:com|rode|use|chame|chama|abra|veja)\s+(?:o\s+)?)?"}
_TAIL = {"en": r"(?P<tail>,? in your (?:own )?terminal\b)?", "pt": r"(?P<tail>,? no seu (?:próprio )?terminal\b)?"}
_PLACES_RE = {lang: tuple((_command_re(_LEAD[lang] + "(?:" + pattern + ")" + _TAIL[lang]), main, inner, command)
                          for pattern, main, inner, command in _PLACES) for lang in ("en", "pt")}


def _place(match, lang: str, main: int, inner, command: str | None, screen: int | None) -> str:
    """The option of this menu in place of a command or a menu path, fitted to the sentence around it: "rode a opção
    11 deste menu", "…: opção 5 abaixo (`hermes pm gc`)", "; a opção 4 abaixo faz isso agora"."""
    pt = lang == "pt"
    if screen == main:   # the owner is on that screen: its own option, below
        where = (("esta tela" if pt else "this screen") if inner is None else
                 ("tecla v abaixo" if pt else "the key v below") if inner == "v" else
                 (f"opção {inner} abaixo" if pt else f"option {inner} below"))
    else:
        where = f"opção {main} deste menu" if pt else f"option {main} of this menu"
        if inner == "v":
            where += ", depois a tecla v" if pt else ", then the key v"
        elif inner is not None:
            where += f", depois a {inner}" if pt else f", then {inner}"
    text, after = match.string, match.string[match.end():]
    if command and f"`{command}`" not in text[max(0, match.start() - 160):match.start()] + after[:160]:
        where += f" (`{command}`)"   # unless the sentence names Hermes' command itself
    lead = match.group("lead") or ""
    if lead.startswith(("no seu", "in your")):
        return ("n" + where if where.startswith("esta") else "na " + where) if pt else "in " + where
    if lead:
        verb = lead.split()[0]
        return f"{verb} {where}" if not pt or where.startswith("esta") else f"{verb} a {where}"
    before = text[:match.start()].rstrip()
    starts = not before or before[-1] in ".!?-•\n"
    subject = re.match(r"\s+[a-zà-ú]", after)   # "… removes them": the option does it
    if subject and ", " in where:
        where += ","
    # in Portuguese the option takes its article, except as the answer after a colon or inside parentheses
    if pt and not where.startswith("esta") and (starts or subject or before[-1] not in ":("):
        where = "a " + where
    return where[0].upper() + where[1:] if starts else where


def in_menu(text: str, lang: str, screen: int | None = None) -> str:
    """`status` and the digest as the menu shows them: the menu's own words, and the option of this menu where they
    send the owner to a command or a menu path. `screen`: the option of the main screen being shown (12 for Hermes
    health), whose own options are then said as "option 5 below"."""
    for pattern, replacement in _IN_MENU.get(lang, ()):
        text = pattern.sub(replacement, text)
    for pattern, main, inner, command in _PLACES_RE.get(lang, ()):
        text = pattern.sub(lambda match, main=main, inner=inner, command=command:
                           _place(match, lang, main, inner, command, screen), text)
    return text


def _count(value, lang: str) -> str:
    text = f"{value:,}" if isinstance(value, int) else str(value)
    return text.replace(",", ".") if lang == "pt" else text


def _when(stamp, lang: str) -> str:
    try:
        moment = datetime.fromtimestamp(float(stamp))
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"
    return moment.strftime("%d/%m %H:%M" if lang == "pt" else "%b %d %H:%M")


def _value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value) if value else "—"
    return "—" if value is None else str(value)


class Menu:
    def __init__(self, home: Path, runtime_config: dict | None, *, lang: str, console: Console | None = None,
                 writer=None, clock=time.time, reflect=None):
        self.home, self.runtime = Path(home), runtime_config if isinstance(runtime_config, dict) else {}
        self.lang = lang if lang in TEXT else "en"
        self.io = console or Console()
        self.writer, self.clock, self.reflect = writer, clock, reflect
        # every command the menu prints names this profile, as the terminal's commands do (cli.profile_commands): its
        # own words also name the gateway that serves it; what it prints of other profiles keeps their `-p`
        from .admin import profile_name
        from .cli import gateway_profile, profile_commands

        write, profile = self.io._write, profile_name(self.home)
        self.t = _commands_in(TEXT[self.lang], profile, gateway_profile(self.home))
        self.io._write = lambda text: write(profile_commands(text, profile))

    # ── helpers ────────────────────────────────────────────────────────────────────────────────────
    def _(self, sentence: str) -> str:
        from .i18n import text

        return text(sentence, self.lang)

    def n(self, key: str, count, **values) -> str:
        """A sentence with a count, singular or plural as the count asks."""
        sentence = self.t[key]
        if isinstance(sentence, tuple):
            sentence = sentence[0 if count == 1 else 1]
        return sentence.format(n=count, **values)

    def confirm(self, question: str) -> bool:
        return self.io.ask(question + self.t["yn"]).lower() in self.t["yes"]

    def pause(self) -> None:
        self.io.ask(self.t["enter"])

    def heading(self, title: str) -> None:
        self.io.say()
        self.io.say(self.io.bold(title))
        self.io.say("─" * min(len(title) + 4, self.io.width))

    def pick(self, answer: str, count: int) -> int | None:
        """The index chosen by a number from 1 to count, or None."""
        return int(answer) - 1 if answer.isascii() and answer.isdigit() and 1 <= int(answer) <= count else None

    def guarded(self, action) -> None:
        """Run one action; a refusal or a busy store is shown and the menu goes on."""
        try:
            action()
        except (ValueError, KeyError, TypeError, PermissionError) as error:
            self.io.say(self.t["error"].format(message=self.named(self._(str(error)))))
        except (OSError, sqlite3.Error) as error:
            self.io.say(self.t["error"].format(message=self._("The Evolution store is busy; try again in a moment")
                                               if isinstance(error, sqlite3.OperationalError) else type(error).__name__))

    def named(self, message: str) -> str:
        """A message with each setting's internal name (`reflector_daily_limit`, evolution.slow_turn_seconds) said
        with the name the menu shows for it."""
        for key, _english, _portuguese in FIELDS:
            name = re.escape(key)
            # a plain word (mode, language) is a setting only when written as one: in backticks, after `evolution.`,
            # or opening a "<key> must ..." refusal
            found = rf"`(?:evolution\.)?{name}`|\bevolution\.{name}\b|^{name}(?= must)" + (rf"|\b{name}\b" if "_" in key else "")
            message = re.sub(found, "“" + self.short_label(key) + "”", message)
        return message

    def say_markdown(self, text: str) -> None:
        """`status` or the digest, written in Markdown, as plain text with the menu's words (and, in Portuguese, sizes
        with a decimal comma)."""
        from .status import decimal_comma

        self.io.say()
        self.io.say(plain(in_menu(decimal_comma(str(text), self.lang), self.lang), width=self.io.width).rstrip())

    def settings_now(self) -> tuple[dict, list[dict]]:
        from . import admin, status
        from .cli import safe_settings
        from .evolve import options

        config, problems = safe_settings(self.home, self.runtime)
        opts = options(config, self.runtime, strict=False, problems=problems)
        channels = status.chat_senders(self.home, since=self.clock() - 30 * 86_400, owners=opts["owner_sender_ids"],
                                       platforms=opts["owner_platforms"])
        suggestions = status.owner_suggestions(channels, owners=opts["owner_sender_ids"], platforms=opts["owner_platforms"])
        return admin.current_settings(self.home, self.runtime), suggestions

    def write(self, values: dict) -> None:
        from . import admin

        result = admin.write_settings(self.home, self.runtime, values, now=self.clock(), writer=self.writer,
                                      actor="terminal")
        if not result["written"]:
            self.io.say(self.t["nothing"])
            return
        self.io.say(self.t["saved"])
        reload = list(result.get("reload") or ())
        if values.get("agent_tools") is True and "agent_tools" in result["written"] and "agent_tools" not in reload:
            reload.append("agent_tools")   # the agent's tools are registered only when the plugin loads with them on
        if reload:
            self.io.say(self.n("reload", len(reload), keys=", ".join("“" + self.short_label(key) + "”" for key in reload)))

    # ── main screen ────────────────────────────────────────────────────────────────────────────────
    def state(self) -> dict:
        from . import status
        from .cli import safe_settings

        config, problems = safe_settings(self.home, self.runtime)
        return status.build(self.home, now=self.clock(), config=config, runtime_config=self.runtime, problems=problems)

    def open_proposals(self, home=None) -> list[dict]:
        from . import explain, proposals
        from .experience import experience

        from .admin import profile_name
        from .cli import profile_commands

        with experience(home or self.home) as conn:
            items = proposals.listing(conn, status="open")
        owner = profile_name(Path(home or self.home))   # the profile that wrote them: the lead's keep the lead's `-p`
        found = []
        for item in items:
            told = {key: profile_commands(value, owner) for key, value in explain.describe(item, self.lang).items()}
            if told.get("category") == "change" and told.get("can_apply") is False:
                told = {**told, "category": "manual"}   # nothing the plugin can apply here: it is the owner's to run
            found.append({**item, "command": profile_commands(item.get("command"), owner), "explain": told})
        return found

    def attention_lines(self, state: dict, letters: dict | None = None) -> list[str]:
        """The "Needs attention" items as `status` writes them (values and lists filled in), in plain text and with
        the menu's own words where `status` sends the owner to this menu. `letters`: {attention code: the letter of
        the suggestion below that does it}, named at the end of its item."""
        from .status import _TEXT, markdown

        heading = "## " + _TEXT.get(self.lang, _TEXT["en"])["attention"]
        lines, inside = [], False
        for line in markdown(state, self.lang).splitlines():
            if line.startswith("## "):
                inside = line == heading
                continue
            if inside and line.startswith("- "):
                lines.append(line[2:])
            elif inside and line.startswith("  - ") and lines:
                lines[-1] += "\n    · " + line[4:]
        codes = list(state.get("attention") or ())
        if len(codes) == len(lines):   # one line per code: the ones the menu says itself
            lines = [self.t["waiting_attention"] if code == "lessons_waiting" else line for code, line in zip(codes, lines)]
        lines = ["\n".join(inline(in_menu(part, self.lang)) for part in line.split("\n")) for line in lines]
        if letters and len(codes) == len(lines):
            lines = [self.lettered(line, letters[code]) if code in letters else line for code, line in zip(codes, lines)]
        return lines[:5] if codes else []

    def lettered(self, item: str, letter: str) -> str:
        """An attention item that ends naming the suggestion letter that does it: "…: option 1 of this menu (letter a
        below)."."""
        first, newline, rest = item.partition("\n")
        said = self.t["letter_hint"].format(letter=letter)
        first = first[:-1] + said + first[-1] if first[-1:] in (".", ":") else first + said
        return first + newline + rest

    def letters(self, ideas: list) -> dict:
        """{attention code: the letter of the suggestion that does it}, for the suggestions shown on the main screen."""
        shown: dict = {}
        for letter, (_text, action) in zip("abcdef", ideas):
            shown.setdefault(action, letter)
        does = {"lessons_waiting": self.waiting, "digest_unscheduled": self.schedule_digest,
                "hermes_unused_tools": self.care, "os_security_updates": self.operating_system,
                "os_dpkg_interrupted": self.operating_system, "os_held_by_plugin": self.operating_system}
        return {code: shown[action] for code, action in does.items() if action in shown}

    def suggestions(self, state: dict, items: list[dict]) -> list[tuple[str, object]]:
        """(text, action) for what is worth doing now, most useful first."""
        from . import memory_care

        found = []
        waiting = state["lessons"]["awaiting_approval"]
        if waiting:
            found.append((self.n("suggest_waiting", waiting), self.waiting))
        changes = [item for item in items if item["explain"].get("bulk")]   # one-by-one calls stay out
        if changes:
            titles = list(dict.fromkeys(item["explain"]["title"] for item in changes))
            what = titles[0] + (self.n("and_more", len(titles) - 1) if len(titles) > 1 else "")
            found.append((self.n("suggest_apply", len(changes), what=what), lambda: self.apply_all(changes)))
        if (state.get("auto_actions") or {}).get("mode") != "auto":
            found.append((self.t["suggest_full"], lambda: self.set_auto("auto")))
        env = state.get("environment") or {}
        acts = [item for item in env.get("findings") or () if item.get("risk") in ("high", "medium")]
        if acts:
            found.insert(0, (self.n("suggest_env", len(acts)), self.environment))
        repeats = len(memory_care.review(self.home, self.runtime)["issues"])
        if repeats:
            found.append((self.n("suggest_memory", repeats), self.memory))
        system = state.get("system") or {}
        if {"dpkg_interrupted", "held_by_plugin"} & set(system.get("findings") or ()):
            found.insert(0, (self.t["suggest_os_repair"], self.operating_system))
        elif system.get("security"):
            found.append((self.n("suggest_os_security", system["security"]), self.operating_system))
        care = state.get("care") or {}
        if "unused_tools" in (care.get("findings") or ()):
            found.append((self.t["suggest_unused"].format(size=_bytes(care.get("reclaimable"), self.lang)), self.care))
        if "digest_unscheduled" in (state.get("attention") or ()):
            found.append((self.t["suggest_digest"], self.schedule_digest))
        advice = [item for item in items if item["explain"]["category"] == "advice"]
        if advice:
            found.append((self.n("suggest_advice", len(advice)), self.proposals))
        return found[:6]

    def run(self) -> None:
        try:
            while True:
                state = self.state()
                items = self.open_proposals()
                automatic = state.get("auto_actions") or {"mode": "manual", "last_7d": 0}
                counts = {"waiting": state["lessons"]["awaiting_approval"], "proposals": state["proposals_open"],
                          "active": state["lessons"]["active"],
                          "auto": self.t["auto_names"].get(automatic["mode"], automatic["mode"]),
                          "profiles": ((state.get("environment") or {}).get("summary") or {}).get("profiles",
                                                                                                    self.t["env_unseen"])}
                last = (state["autopilot"] or {}).get("last_pass")
                from .status import local_time

                last_text = self.t["never"] if not last else \
                    f"{local_time(last.get('at'), self.lang)} ({self.t['ok'] if last.get('ok') is not False else self.t['failed']})"
                self.heading(self.t["title"].format(version=state["plugin_version"], profile=state["profile"]))
                lessons_mode = state["lessons"]["mode"]
                self.io.say(self.t["summary"].format(
                    lessons=self.t["lesson_modes"].get(lessons_mode, lessons_mode),
                    active=_plural(self.t["n_active"], counts["active"]),
                    waiting=_plural(self.t["n_waiting"], counts["waiting"]),
                    proposals=_plural(self.t["n_proposals"], counts["proposals"])))
                self.io.say(self.t["loop"].format(last=last_text,
                                                  guard=self.t["guard_on" if state["runtime_guard"]["enabled"] else "guard_off"],
                                                  reflector=self.t["on" if state["reflector"]["enabled"] else "off"]))
                self.io.say(self.n("auto_line", automatic.get("last_7d", 0), mode=counts["auto"]))
                ideas = self.suggestions(state, items)
                attention = self.attention_lines(state, self.letters(ideas))
                if attention:
                    self.io.say()
                    self.io.say(self.io.bold(self.t["attention_title"]))
                    for item in attention:
                        first, *rest = item.split("\n")
                        self.io.para("• " + first, indent=2)
                        for sub in rest[:6]:
                            self.io.say(sub)
                if ideas:
                    self.io.say()
                    self.io.say(self.io.bold(self.t["suggest_title"]))
                    for letter, (text, _action) in zip("abcdef", ideas):
                        self.io.say(f"   {letter}  {text}")
                self.io.say()
                os_name = ((state.get("system") or {}).get("os") or {}).get("name") or "Linux"
                labels = [*self.t["items"], *(label.format(os=os_name, profiles=counts["profiles"])
                                              for label in self.t["items_more"])]
                for number, label in enumerate(labels, start=1):
                    self.io.say(f"  {number:>2}  {label.format(**counts)}")
                self.io.say(f"   0  {self.t['quit']}")
                answer = self.io.ask(self.t["choose"]).lower()
                if answer in ("0", "q", "x", "exit", "quit", "sair"):
                    raise Quit
                letters = dict(zip("abcdef", ideas))
                if answer in letters:
                    self.guarded(letters[answer][1])
                    continue
                screens = [self.waiting, self.proposals, self.active, self.history, self.automatic, self.settings,
                           self.owner, self.memory, self.digest, self.full_status, self.learn, self.care,
                           self.operating_system, self.environment]
                chosen = self.pick(answer, len(screens))
                if chosen is None:
                    self.io.say(self.t["invalid_main"] if ideas else self.t["invalid"])
                    continue
                self.guarded(screens[chosen])
        except Quit:
            self.io.say()
            self.io.say(self.t["bye"])

    # ── lessons waiting ────────────────────────────────────────────────────────────────────────────
    def waiting(self) -> None:
        from . import decisions
        from .experience import experience

        while True:
            with experience(self.home) as conn:
                items = decisions.waiting(conn, lang=self.lang)
            self.heading(self.t["waiting_title"])
            if not items:
                self.io.say(self.t["waiting_empty"])
                self.pause()
                return
            for number, item in enumerate(items, start=1):
                origin = ORIGINS[self.lang].get(item["origin"], item["origin"])
                note = "" if item["bulk"] else f" · {self.t['agent_single']}"
                self.io.say(f"  {number:>2}  [{item['tool']}] {origin}{note}")
                self.io.para(item["lesson"])
            bulk = [item for item in items if item["bulk"]]
            prompt = self.t["waiting_prompt"].format(n=len(bulk)) if bulk else self.t["waiting_prompt_one"]
            answer = self.io.ask(prompt).lower()
            if not answer:
                return
            if answer == "t" and bulk:
                if self.confirm(self.n("approve_all", len(bulk))):
                    with experience(self.home) as conn:
                        outcome = decisions.approve_all(conn, {item["id"]: item["fingerprint"] for item in bulk},
                                                        now=self.clock(), actor="terminal", lang=self.lang)
                    self.io.say(self.n("approved_n", len(outcome["approved"])))
                    if outcome["skipped"]:
                        self.io.say(self.n("skipped_n", len(outcome["skipped"])))
                continue
            chosen = self.pick(answer, len(items))
            if chosen is None:
                self.io.say(self.t["invalid"])
                continue
            item = items[chosen]
            self.io.say()
            self.io.para(item["lesson"], indent=2)
            key = self.io.ask(self.t["lesson_prompt"]).lower()
            action = {"a": "approve", "r": "reject"}.get(key[:1])
            if action is None:
                if key:
                    self.io.say(self.t["invalid_key"])
                continue
            with experience(self.home) as conn:
                result = decisions.decide_lesson(conn, item["id"], action, now=self.clock(), actor="terminal",
                                                 lang=self.lang, shown=item["fingerprint"])
            self.io.say(self.t["approved" if action == "approve" else "rejected"] if result["decided"]
                        else self.t["changed"])

    # ── proposals ──────────────────────────────────────────────────────────────────────────────────
    def subject(self, item: dict) -> str:
        """What a proposal is about, as people read it: Hermes' own doctor findings are about a profile ("profile
        work"), whatever key the plugin files them under (work/cache.documents)."""
        subject = str(item.get("subject") or "")
        if str(item.get("kind") or "").startswith("doctor_") and "/" in subject:
            return self.t["profile_subject"].format(profile=subject.split("/", 1)[0])
        return subject

    def line(self, number: int, item: dict, mark: str = "") -> str:
        """One proposal in a list: the number to type, the risk, the title, the subject and the proposal's own id
        (the one the detail, the digest and `hermes evolution proposals` show)."""
        told = item["explain"]
        risk = self.t["risk_label"].format(risk=told["risk"])
        return (f"  {number:>2}  [{risk}] {told['title']} · {self.subject(item)}{mark} "
                + self.t["list_ref"].format(id=item["id"]))

    def apply_all(self, changes: list[dict]) -> None:
        from . import admin

        for item in changes:
            self.io.say(f"   · {item['explain']['title']}: {self.subject(item)}")
        if not self.confirm(self.n("apply_all_q", len(changes))):
            return
        result = admin.decide(self.home, self.runtime, {"kind": "apply_all", "ids": [item["id"] for item in changes][:50]},
                              now=self.clock(), lang=self.lang, actor="terminal")
        self.io.say(self.n("applied_n", len(result["applied"])))
        reasons = {"already": self.t["already"], "not_open": self.t["changed"], "busy": self.t["skip_busy"],
                   "one_by_one": self.t["skip_one_by_one"]}
        for item in result["skipped"]:
            reason = item.get("reason")
            self.io.say("   · " + self.t["proposal_ref"].format(id=item["id"]) + ": " + reasons.get(reason, self._(str(reason))))

    def set_auto(self, mode: str) -> None:
        for line in self.t["auto_modes"]:
            self.io.para("• " + line, indent=2)
        if self.confirm(self.t["auto_q"].format(mode=self.t["auto_names"].get(mode, mode))):
            self.guarded(lambda: self.write({"mode": mode}))

    def proposals(self) -> None:
        while True:
            items = self.open_proposals()
            self.heading(self.t["proposals_title"])
            if not items:
                self.io.say(self.t["proposals_empty"])
                self.pause()
                return
            ordered = []
            for group in ("change", "lesson", "manual", "advice"):
                members = [item for item in items if item["explain"]["category"] == group]
                if not members:
                    continue
                self.io.say(self.io.bold(self.t["group_" + group]))
                for item in members:
                    ordered.append(item)
                    self.io.say(self.line(len(ordered), item))
            changes = [item for item in ordered if item["explain"].get("bulk")]
            prompt = self.n("proposals_prompt_all", len(changes)) if changes else self.t["proposals_prompt"]
            answer = self.io.ask(prompt).lower()
            if not answer:
                return
            if answer == "t" and changes:
                self.apply_all(changes)
                continue
            chosen = self.pick(answer, len(ordered))
            if chosen is None:
                self.io.say(self.t["invalid"])
                continue
            self.detail(ordered[chosen])

    def detail(self, item: dict, home=None) -> None:
        """One proposal: what was seen, what to do about it, what applying does and its undo; and the decision.
        `home`: the profile whose database holds it (the lead profile for the whole Hermes' situations)."""
        from . import admin, decisions, lessons
        from .experience import experience

        home = home or self.home
        told = item["explain"]
        self.io.say()
        self.io.say(self.io.bold(f"  {self.t['proposal_ref'].format(id=item['id'])} · {told['title']} · {self.subject(item)}"
                                 f" · {self.t['risk_label'].format(risk=told['risk'])}"))
        lesson = self.lesson_of(item, home) if item.get("kind") in ("lesson", "lesson_retire") else None
        if item.get("kind") == "lesson":   # its sentence, read now in the menu's language (not the last pass's)
            lesson = lesson or told["what"]
        elif told["what"]:
            self.io.say(f"  {self.t['seen']}:")
            self.io.para(told["what"])
        if lesson:
            self.io.say(f"  {self.t['lesson']}:")
            self.io.para(lesson)
        if told.get("advice"):
            self.io.say(f"  {self.t['advice']}:")
            self.io.para(told["advice"])
        if told["does"]:
            self.io.say(f"  {self.t['does']}:")
            self.io.para(told["does"])
        if told["undo"]:
            self.io.say(f"  {self.t['undo_how']}: {told['undo']}")
        elif item.get("command") and told["category"] in ("advice", "manual") and item["command"] not in (told["does"] or ""):
            self.io.say(f"  {self.t['look_with'] if told['category'] == 'advice' else self.t['command']}: "
                        f"{item['command']}")
        if told["category"] == "change":
            prompt, keys = self.t["change_prompt"], {"a": "apply", "r": "reject"}
        elif told["category"] == "advice":
            failure = item["kind"] == "investigate_failure"
            prompt = self.t["failure_prompt"] if failure else self.t["advice_prompt"]
            keys = {"d": "reject", **({"e": "write"} if failure else {})}
        else:
            prompt, keys = self.t["proposal_prompt"], {"a": "approve", "r": "reject"}
        answer = self.io.ask(prompt).lower()
        action = keys.get(answer[:1])
        if action is None:
            if answer:
                self.io.say(self.t["invalid_key"])
            return
        if action == "write":
            text = self.io.ask(self.t["lesson_text"])
            if not text:
                return
            activate = self.confirm(self.t["lesson_activate"])
            with experience(home) as conn:
                lesson_id = lessons.add_custom(conn, text, tool=item["subject"], origin="operator", now=self.clock(),
                                               active=activate)
                decisions.decide_proposal(conn, item["id"], "approve", now=self.clock(), actor="terminal",
                                          lang=self.lang)
            self.io.say(self.t["lesson_added"].format(id=lesson_id, status=self.t["lesson_added_active"] if activate
                                                      else self.t["lesson_added_waiting"]))
            return
        if action == "apply":
            result = admin.decide(home, self.runtime, {"kind": "apply", "id": item["id"]}, now=self.clock(),
                                  lang=self.lang, actor="terminal")
            self.io.say(self.t["applied"].format(undo=self.undo_text(result, item)) if result.get("applied")
                        else self.t["already"] if result.get("reason") == "already" else self.t["changed"])
            return
        now_item = next((row for row in self.open_proposals(home) if row["id"] == item["id"]), None)
        if now_item is None or (now_item["evidence"], now_item["command"]) != (item["evidence"], item["command"]):
            self.io.say(self.t["changed"])   # decide only on what was shown
            return
        with experience(home) as conn:
            result = decisions.decide_proposal(conn, item["id"], action, now=self.clock(), actor="terminal",
                                               lang=self.lang)
        command = result.get("run_this")
        if told["category"] == "advice":
            self.io.say(self.t["dismissed"])
        elif command:
            self.io.say(self.t["run_after"])
            self.io.say(f"    {command}")
        else:
            self.io.say(self.t["recorded"])


    def lesson_of(self, item: dict, home=None) -> str | None:
        """The sentence of the lesson a proposal is about, rendered now in the menu's language (a proposal keeps
        the sentence in the language of the pass that wrote it)."""
        from . import lessons
        from .experience import experience

        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        try:
            lesson_id = int(evidence.get("lesson_id"))
        except (TypeError, ValueError):
            return None
        with experience(home or self.home) as conn:
            row = conn.execute("SELECT * FROM lesson WHERE id=?", (lesson_id,)).fetchone()
        return lessons.render(row, self.lang) if row is not None else None

    def undo_text(self, result: dict, item: dict) -> str:
        """How to undo an applied change, said in the menu's language: from the undo arguments the result carries,
        else from the proposal's own plan (the same one actions.apply_proposal checked again before it ran)."""
        from . import explain

        args = result.get("undo_args")
        if not isinstance(args, (list, tuple)) or not args:
            found = explain.plan(item)
            args = found[1] if found is not None else None
        return explain.command_text(args, self.lang) if args else str(result.get("undo") or "")

    # ── the whole Hermes ───────────────────────────────────────────────────────────────────────────
    def environment(self) -> None:
        """Every profile of this Hermes at once: what is there, and what to do, from the lead profile's last look."""
        from . import environment
        from .admin import profile_name
        from .experience import experience

        while True:
            lead = environment.lead_home(self.home)
            self.heading(self.t["env_title"])
            if lead is None:
                self.io.para(self.t["env_none"], indent=0)
                self.pause()
                return
            with experience(lead) as conn:
                found = environment.stored(conn)
            doctor_runs = self.doctor_runs(lead)
            values, _suggestions = self.settings_now()
            lead_mode = environment.profile_mode(lead) or values.get("mode")
            mode = self.t["auto_names"].get(lead_mode, lead_mode)
            when = _when(found["at"], self.lang) if found else self.t["never"]
            self.io.say(self.t["env_mode"].format(mode=mode, lead=profile_name(lead), when=when))
            if Path(lead).resolve() != Path(self.home).resolve():
                self.io.para(self.t["env_lead_elsewhere"].format(lead=profile_name(lead)), indent=0)
            if not found:
                self.io.para(self.t["env_not_yet"], indent=0)
            else:
                catalog = found.get("catalog") or {}
                if catalog.get("plugins"):
                    tiers = ", ".join(f"{_count(n, self.lang)} {self.t['env_tiers'].get(tier, tier)}"
                                      for tier, n in sorted((catalog.get("tiers") or {}).items()))
                    removed = catalog.get("removed") if isinstance(catalog.get("removed"), int) else 0
                    self.io.say(self.t["env_catalog"].format(plugins=_count(catalog["plugins"], self.lang), tiers=tiers,
                                                             removed=_plural(self.t["n_removed"], removed)))
                rows = found.get("profiles") or []
                self.io.say()
                self.io.say(self.io.bold(self.t["env_profiles"].format(n=len(rows))))
                for row in rows:
                    self.io.say("  " + self.t["env_row"].format(
                        profile=row["profile"], sessions=_plural(self.t["n_row_sessions"], row["sessions_7d"]),
                        on=_plural(self.t["n_on"], row["plugins_on"]), idle=_plural(self.t["n_idle"], row["plugins_idle"]),
                        skills=_plural(self.t["n_skills"], row["skills"]),
                        self=self.t["yes_word"] if row["self"] else self.t["no_word"]))
                    doctor_line = self.doctor_line(doctor_runs.get(row["profile"]))
                    if doctor_line:
                        self.io.para(doctor_line, indent=6)
            items = [item for item in self.open_proposals(lead) if item["kind"] in environment.KINDS]
            order = {"high": 0, "medium": 1, "low": 2}
            items.sort(key=lambda item: (order.get(item.get("risk"), 3), item["kind"]))
            self.io.say()
            if found and not items:
                self.io.say(self.t["env_clean"])
            elif items:
                self.io.say(self.io.bold(self.t["env_todo"].format(n=len(items))))
                for number, item in enumerate(items, start=1):
                    auto = " ⚙" if item["explain"].get("automatic") and lead_mode == "auto" else ""
                    self.io.say(self.line(number, item, auto))
                if lead_mode == "auto" and any(item["explain"].get("automatic") for item in items):
                    self.io.say(self.t["env_auto_mark"])
            answer = self.io.ask(self.t["env_prompt"]).lower()
            if not answer:
                return
            if answer == "v":
                self.io.say(self.t["env_scanning"])
                self.guarded(lambda: self._look_again(lead))
                continue
            chosen = self.pick(answer, len(items))
            if chosen is None:
                self.io.say(self.t["invalid"])
                continue
            self.detail(items[chosen], home=lead)

    @staticmethod
    def doctor_runs(lead) -> dict:
        """{profile: {at, result}} of Hermes' own doctor checks (doctor.py, 0.9.0), or {} where there are none."""
        try:
            from . import doctor
            from .experience import experience

            with experience(lead) as conn:
                runs = doctor.stored(conn)
        except Exception:   # an older plugin file set, or a busy store: the screen goes on without the line
            return {}
        return runs if isinstance(runs, dict) else {}

    def doctor_line(self, run) -> str | None:
        """One profile's last doctor check in plain words, or None."""
        if not isinstance(run, dict) or not isinstance(run.get("result"), dict):
            return None
        try:
            from . import doctor

            counts = doctor.summary(run["result"])
        except Exception:
            return None
        when = _when(run.get("at"), self.lang)
        if not counts.get("ok"):
            error = self.named(self._(str(counts.get("error") or self.t["unknown"])))
            return self.t["env_doctor_failed"].format(error=error, when=when)
        number = lambda key: counts.get(key) if isinstance(counts.get(key), int) else 0   # noqa: E731
        return self.t["env_doctor"].format(fail=_plural(self.t["n_fail"], number("fail")),
                                           warn=_plural(self.t["n_warn"], number("warn")),
                                           optional=_plural(self.t["n_optional"], number("optional")), when=when)

    def _look_again(self, lead) -> None:
        """Look at every profile now and refresh the proposals of the lead profile with what was found."""
        from . import environment

        environment.publish(lead, environment.refresh(lead, now=self.clock(), force=True), now=self.clock())

    # ── automatic actions ──────────────────────────────────────────────────────────────────────────
    def automatic(self) -> None:
        from . import actions
        from .experience import experience

        while True:
            values, _suggestions = self.settings_now()
            mode = values.get("mode") or "manual"
            self.heading(self.t["auto_title"])
            self.io.say(self.t["auto_now"].format(mode=self.io.bold(self.t["auto_names"].get(mode, mode))))
            for line in self.t["auto_modes"]:
                self.io.para("• " + line, indent=2)
            self.io.para(self.t["auto_rules"], indent=0)
            with experience(self.home) as conn:
                done = actions.recent(conn, now=self.clock())
            self.io.say()
            self.io.say(self.t["auto_recent"] if done else self.t["auto_none"])
            for row in done[:15]:
                mark = f" · {self.t['undone_mark']}" if row["undone"] else ""
                self.io.say(f"  {row['id']:>4}  {_when(row['at'], self.lang)}  {self.describe(row)}{mark}")
            answer = self.io.ask(self.t["auto_prompt"]).lower()
            if not answer:
                return
            if answer == "m":
                chosen = self.pick(self.io.ask(self.t["mode_prompt"]), 3)
                if chosen is not None and ("auto", "assist", "manual")[chosen] != mode:
                    self.guarded(lambda: self.write({"mode": ("auto", "assist", "manual")[chosen]}))
                continue
            if not (answer.isascii() and answer.isdigit()) or int(answer) not in {
                    row["id"] for row in done if not row["undone"] and row["kind"] != "maintenance"}:
                self.io.say(self.t["invalid"])
                continue
            self.undo(int(answer))

    def undo(self, decision_id: int) -> None:
        from . import admin

        if not self.confirm(self.t["undo_q"].format(id=decision_id)):
            return
        result = admin.decide(self.home, self.runtime, {"kind": "undo", "id": decision_id}, now=self.clock(),
                              lang=self.lang, writer=self.writer, actor="terminal")
        self.io.say(self.t["undone"] if result.get("undone") else
                    self.t["reasons"].get(result.get("reason"), self.t["reasons"]["changed_since"]))

    # ── active lessons ─────────────────────────────────────────────────────────────────────────────
    def active(self) -> None:
        from . import decisions, lessons
        from .experience import experience

        while True:
            with experience(self.home) as conn:
                items = lessons.listing(conn, lang=self.lang, status="active")
            self.heading(self.t["active_title"])
            if not items:
                self.io.say(self.t["active_empty"])
                self.pause()
                return
            for number, item in enumerate(items, start=1):
                origin = ORIGINS[self.lang].get(item["origin"], item["origin"])
                verdict = self.t["verdict"].get(item["verdict"] or "", "")
                effect = ""
                if item["rate_before"] is not None and item["rate_after"] is not None:
                    effect = " · " + self.t["effect"].format(before=f"{item['rate_before']:.2f}",
                                                            after=f"{item['rate_after']:.2f}")
                self.io.say(f"  {number:>2}  [{item['tool']}] {origin}{' · ' + verdict if verdict else ''}{effect}")
                self.io.para(item["lesson"])
            answer = self.io.ask(self.t["active_prompt"])
            if not answer:
                return
            chosen = self.pick(answer, len(items))
            if chosen is None:
                self.io.say(self.t["invalid"])
                continue
            if self.confirm(self.t["retire_q"]):
                with experience(self.home) as conn:
                    result = decisions.decide_lesson(conn, items[chosen]["id"], "retire", now=self.clock(),
                                                     actor="terminal", lang=self.lang)
                self.io.say(self.t["retired"] if result["decided"] else self.t["changed"])

    # ── history and undo ───────────────────────────────────────────────────────────────────────────
    def describe(self, row: dict) -> str:
        did = self.t["did"].get(row["action"], row["action"])
        if row["kind"] == "setting":
            return self.t["what_setting"].format(target="“" + self.short_label(row["target"]) + "”",
                                                 before=self.shown(row["target"], row["before"].get("value")),
                                                 after=self.shown(row["target"], row["after"].get("value")))
        if row["kind"] == "memory":
            return self.t["what_memory"].format(target=row["target"])
        if row["kind"] == "maintenance" and row["target"] == "gateway_repair":
            before, after = row.get("before") or {}, row.get("after") or {}
            return self.t["what_gateway"].format(unit=before.get("unit") or "?", before=before.get("path") or "?",
                                                 after=after.get("path") or "?",
                                                 failed="" if after.get("ok") else self.t["gateway_failed"])
        if row["kind"] == "maintenance" and row["target"] == "plugin_sync":
            before, after = row.get("before") or {}, row.get("after") or {}
            return self.t["what_sync"].format(profile=before.get("profile") or "?", before=before.get("sha") or "?",
                                              after=after.get("sha") or "?",
                                              failed="" if after.get("ok") else self.t["sync_failed"])
        if row["kind"] == "maintenance":
            return self.t["what_maintenance"].format(target=str(row["target"]).replace("_", " "))
        if row["kind"] == "proposal" and isinstance((row.get("after") or {}).get("applied"), list):
            from .explain import command_text

            return f"{did} " + command_text(row["after"]["applied"], self.lang)
        if row["kind"] == "cron":
            return self.t["what_cron"].format(target=row["target"],
                                              schedule=(row.get("after") or {}).get("schedule") or "?")
        return self.t["what_lesson" if row["kind"] == "lesson" else "what_proposal"].format(did=did, target=row["target"])

    def history(self) -> None:
        from . import decisions
        from .experience import experience

        while True:
            with experience(self.home) as conn:
                rows = decisions.history(conn, lang=self.lang, limit=20)
            self.heading(self.t["history_title"])
            if not rows:
                self.io.say(self.t["history_empty"])
                self.pause()
                return
            for row in rows:
                mark = f" · {self.t['undone_mark']}" if row["undone"] else ""
                self.io.say(f"  {row['id']:>4}  {_when(row['at'], self.lang)}  "
                            f"{self.t['where'].get(row['actor'], row['actor'])}  {self.describe(row)}{mark}")
                if row.get("lesson"):
                    self.io.para(row["lesson"], indent=8)
            answer = self.io.ask(self.t["history_prompt"])
            if not answer:
                return
            if not (answer.isascii() and answer.isdigit()) or int(answer) not in {row["id"] for row in rows
                                                                                  if row.get("undoable", not row["undone"])}:
                self.io.say(self.t["invalid"])
                continue
            self.undo(int(answer))

    # ── settings ───────────────────────────────────────────────────────────────────────────────────
    def label(self, key: str) -> str:
        for field, english, portuguese in FIELDS:
            if field == key:
                return portuguese if self.lang == "pt" else english
        return key

    def short_label(self, key: str) -> str:
        """The setting's name without its explanation: “Reflector calls per day”."""
        return self.label(key).split(" (")[0].split(":")[0].strip()

    def shown(self, key: str, value) -> str:
        if key == "lessons":
            return self.t["lesson_modes"].get(value, _value(value))
        if key == "language":
            return LANGUAGES[self.lang].get(value, _value(value))
        if key == "mode":
            return self.t["auto_names"].get(value, _value(value))
        if isinstance(value, bool):
            return self.t["yes_value"] if value else self.t["no_value"]
        return _value(value)

    def settings(self) -> None:
        from . import admin

        while True:
            values, suggestions = self.settings_now()
            self.heading(self.t["settings_title"])
            for number, (key, _english, _portuguese) in enumerate(FIELDS, start=1):
                self.io.say(f"  {number:>2}  {self.label(key)}: {self.io.bold(self.shown(key, values.get(key)))}")
            answer = self.io.ask(self.t["settings_prompt"]).lower()
            if not answer:
                return
            if answer == "r":
                changes = admin.recommended_settings(values, suggestions)
                if not changes:
                    self.io.say(self.t["recommended_none"])
                    continue
                self.io.say(self.t["recommended_title"])
                for key, value in changes.items():
                    self.io.say(f"    {self.label(key)}: {self.shown(key, values.get(key))} → {self.shown(key, value)}")
                if self.confirm(self.t["apply_q"]):
                    again = admin.recommended_settings(*self.settings_now())   # what is there now, not what was read
                    if again != changes:
                        self.io.say(self.t["list_changed"])
                    else:
                        self.guarded(lambda: self.write(changes))
                continue
            chosen = self.pick(answer, len(FIELDS))
            if chosen is None:
                self.io.say(self.t["invalid"])
                continue
            key = FIELDS[chosen][0]

            def change(key=key, current=values.get(key)):
                value = self.ask_value(key, current)
                if value is not None and value != current:
                    self.write({key: value})

            self.guarded(change)   # a refused value is said here, and the owner stays in the settings

    def ask_value(self, key: str, current):
        from . import admin

        if key in admin.BOOLEANS:
            if key == "runtime_guard" and current:
                self.io.say(self.t["guard_warn"])
            state = self.t["off"] if current else self.t["on"]
            if self.lang == "pt":
                state = "Desligar" if current else "Ligar"
            return (not current) if self.confirm(self.t["toggle_q"].format(label=self.label(key), state=state)) else None
        if key in admin.CHOICES:
            choices = admin.CHOICES[key]
            names = MODES if key == "lessons" else LANGUAGES if key == "language" else {self.lang: self.t["auto_names"]}
            options = " · ".join(f"{n} {names[self.lang][choice]}" for n, choice in enumerate(choices, start=1))
            chosen = self.pick(self.io.ask(self.t["choice_prompt"].format(options=options)), len(choices))
            return None if chosen is None else choices[chosen]
        if key in admin.NUMBERS:
            answer = self.io.ask(self.t["number_prompt"].format(value=_value(current)))
            if not answer:
                return None
            try:
                return int(answer)
            except ValueError:
                raise ValueError(self.t["number_error"].format(label=self.short_label(key))) from None
        answer = self.io.ask(self.t["list_prompt"])
        if not answer:
            return None
        if answer == "-":
            return []
        return [part.strip() for part in answer.split(",") if part.strip()]

    # ── owner ──────────────────────────────────────────────────────────────────────────────────────
    def owner(self) -> None:
        from . import status

        while True:
            values, suggestions = self.settings_now()
            platforms, senders = list(values["owner_platforms"]), list(values["owner_sender_ids"])
            channels = status.chat_senders(self.home, since=self.clock() - 30 * 86_400, owners=senders,
                                           platforms=platforms)
            suggested = {(item["kind"], item["value"]) for item in suggestions}
            self.heading(self.t["owner_title"])
            self.io.para(self.t["owner_hint"], indent=0)
            rows = []
            for channel in channels:
                rows.append(("platform", channel["origin"]))
                tags = [_plural(self.t["sessions"], channel["sessions"])]
                if channel["origin"] in platforms:
                    tags.append(self.io.bold(self.t["is_you"]))
                elif ("platform", channel["origin"]) in suggested:
                    tags.append(self.t["suggested"])
                self.io.say(f"  {len(rows):>2}  {channel['origin']} ({self.t['whole']}) · {' · '.join(tags)}")
                for sender in channel["senders"]:
                    if sender["id"] is None:
                        continue
                    rows.append(("sender", sender["id"]))
                    tags = [_plural(self.t["sessions"], sender["sessions"])]
                    if sender["id"] in senders:
                        tags.append(self.io.bold(self.t["is_you"]))
                    elif ("sender", sender["id"]) in suggested:
                        tags.append(self.t["suggested"])
                    self.io.say(f"  {len(rows):>2}      └ {sender['id']} · {' · '.join(tags)}")
            if not rows:
                self.io.say(self.t["owner_none"])
                self.pause()
                return
            answer = self.io.ask(self.t["owner_prompt"])
            if not answer:
                return
            chosen = self.pick(answer, len(rows))
            if chosen is None:
                self.io.say(self.t["invalid"])
                continue
            kind, value = rows[chosen]
            key, listed = ("owner_platforms", platforms) if kind == "platform" else ("owner_sender_ids", senders)
            what = f"“{value}”" + (f" ({self.t['whole']})" if kind == "platform" else "")
            present = value in listed
            if self.confirm(self.t["unmark_q" if present else "mark_q"].format(what=what)):
                fresh = list(self.settings_now()[0][key])   # the list as it is now: a change made meanwhile stays
                if (value in fresh) != present:
                    self.io.say(self.t["list_changed"])
                    continue
                updated = [item for item in fresh if item != value] if present else fresh + [value]
                self.guarded(lambda: self.write({key: updated}))

    # ── memory ─────────────────────────────────────────────────────────────────────────────────────
    def memory(self) -> None:
        from . import admin, memory_care

        while True:
            found = memory_care.review(self.home, self.runtime)
            self.heading(self.t["memory_title"])
            for name, info in found["files"].items():
                if not info["present"]:
                    self.io.say("  " + self.t["memory_missing"].format(file=name))
                    continue
                self.io.say("  " + self.t["memory_use"].format(
                    file=name, entries=_plural(self.t["n_entries"], info["entries"]), chars=_count(info["characters"], self.lang),
                    limit=_count(info["limit"], self.lang), percent=info["percent"],
                    off="" if info["enabled"] else self.t["memory_off"]))
            issues = found["issues"]
            if not issues:
                self.io.say()
                self.io.say(self.t["memory_none"])
                self.pause()
                return
            self.io.say()
            tokens = sum(item["tokens_per_turn"] for item in issues)
            self.io.say(self.t["memory_issues"].format(tokens=tokens) if tokens else self.t["memory_issues_free"])
            for number, issue in enumerate(issues, start=1):
                first, second = issue["entries"]
                label = "variant" if issue["kind"] == "exact" and not issue.get("identical") else issue["kind"]
                self.io.say(f"  {number:>2}  {self.t['kinds'][label]} · {first['file']} / {second['file']}")
                for entry in (first, second):
                    preview = entry["text"] if len(entry["text"]) <= 160 else entry["text"][:157] + "…"
                    self.io.para(preview, indent=7)
            self.io.say(self.io.dim(self.t["memory_note"]))
            answer = self.io.ask(self.t["memory_prompt"])
            if not answer:
                return
            chosen = self.pick(answer, len(issues))
            if chosen is None:
                self.io.say(self.t["invalid"])
                continue
            first, second = issues[chosen]["entries"]
            self.io.say()
            for label, entry in (("1", first), ("2", second)):
                self.io.say(f"  {label}  {entry['file']}")
                self.io.para(entry["text"])
            keep = self.io.ask(self.t["keep_prompt"])
            if keep not in ("1", "2"):
                continue
            drop, kept = (second, first) if keep == "1" else (first, second)

            def take_out(entry=drop, kept=kept):
                result = admin.decide(self.home, self.runtime, {"kind": "memory", "action": "remove",
                                                                "file": entry["file"], "fingerprint": entry["fingerprint"],
                                                                "keep": {"file": kept["file"],
                                                                         "fingerprint": kept["fingerprint"]}},
                                      now=self.clock(), lang=self.lang, writer=self.writer, actor="terminal")
                self.io.say(self.t["memory_removed"] if result.get("removed") else self.t["memory_changed"])

            self.guarded(take_out)

    # ── weekly digest ──────────────────────────────────────────────────────────────────────────────
    def schedule_digest(self) -> None:
        from . import admin

        deliver = self.io.ask(self.t["deliver_prompt"]) or "local"
        result = admin.decide(self.home, self.runtime, {"kind": "schedule_digest", "deliver": deliver}, now=self.clock(),
                              lang=self.lang, actor="terminal")
        self.io.say(self.t["digest_done"].format(job=result["job"]) if result.get("scheduled") else self.t["digest_already"])

    # ── Hermes health ──────────────────────────────────────────────────────────────────────────────
    def care(self) -> None:
        from . import hermes_care
        from .cli import gateway_profile, profile_commands
        from .experience import experience
        from .status import _TEXT, _care_values, decimal_comma

        force = True
        while True:
            with experience(self.home) as conn:
                found = hermes_care.cached(conn, self.home, now=self.clock(), force=force)
            force = False
            self.heading(self.t["care_title"])
            gate, db, memory = found["gateway"], found["database"], found["memory"]
            size = lambda value: _bytes(value, self.lang)   # noqa: E731
            self.io.say(self.t["care_gateway"].format(state=self.gateway_state(gate)))
            if db.get("present"):
                journal = str(db.get("journal") or "").lower()
                quick = db.get("quick_check")
                self.io.say(self.t["care_db"].format(
                    journal=self.t["journals"].get(journal, journal) if journal else self.t["no_data"], wal=size(db.get("wal")),
                    quick="ok" if quick == "ok" else self.t["not_checked"] if not quick else self.t["integrity_bad"]))
            self.io.say(self.t["care_disk"].format(free=size(found.get("free")), total=size(found.get("total")),
                                                   pace=self.disk_pace(found)))
            load = found.get("load") or {}
            self.io.say(self.t["care_memory"].format(available=size(memory.get("available")),
                                                     total=size(memory.get("total")), used=size(memory.get("swap_used")),
                                                     swap=size(memory.get("swap_total")), load=self.decimal(load.get("15m")),
                                                     cpus=load.get("cpus", "?")))
            biggest = sorted(((name, value) for name, value in (found.get("sizes") or {}).items() if isinstance(value, int)),
                             key=lambda item: -item[1])[:6]
            if biggest:
                self.io.para(self.t["care_folders"].format(folders=" · ".join(f"{name} {size(value)}" for name, value in biggest)),
                             indent=0)
            if found.get("unused_tools"):
                self.io.para(self.t["care_unused"].format(size=size(found.get("reclaimable")), list=", ".join(
                    f"{item['folder']} ({size(item['bytes'])})" for item in found["unused_tools"])), indent=0)
            texts = _TEXT.get(self.lang, _TEXT["en"])
            values = _care_values({"care": found, "system": {}, "profile": _profile(self.home)})
            codes = [f"hermes_{code}" for code in found.get("findings") or () if f"hermes_{code}" in texts]
            self.io.say()
            if codes:
                self.io.say(self.io.bold(self.t["attention_title"]))
                for code in codes:   # this screen's own options, said as such ("option 5 below")
                    text = decimal_comma(profile_commands(texts[code].format(**values), values["profile"],
                                                          gateway=gateway_profile(self.home)), self.lang)
                    self.io.para("• " + inline(in_menu(text, self.lang, screen=12)), indent=2)
            else:
                self.io.say(self.t["care_none"])
            self.io.say()
            for number, label in enumerate(self.t["care_menu"], start=1):
                self.io.para(f"{number}  {label}", indent=2)
            chosen = self.pick(self.io.ask(self.t["care_prompt"]), len(self.t["care_menu"]))
            if chosen is None:
                return
            if chosen == 0:
                force = True
                continue
            key = ("pm_doctor", "doctor", "doctor_fix", "pm_gc")[chosen - 1]
            if key == "pm_gc" and hermes_care.gc_blocked(found):
                # it would delete a Python a Hermes service still starts from, or every tool of a store without a
                # facts.json it can read (hermes_care.gc_blocked): said as cli.py says it
                folders = [str(item) for item in found.get("in_use_unreferenced") or ()]
                reasons = hermes_care.gc_unsafe_words(found.get("gc_unsafe"), self.lang)
                if folders:   # honest about the remedy (hermes_care.gc_block_advice): a build run on purpose stays
                    for line in hermes_care.gc_block_advice(found, self.lang):
                        self.io.para(("Agora não: " if self.lang == "pt" else "Not now: ") + line.replace("`", ""),
                                     indent=2)
                elif reasons:
                    self.io.para(self.t["care_gc_unsafe"].format(reasons="; ".join(reasons[:3])), indent=2)
                else:   # no check yet, or one stored by an older version: it proves nothing either way
                    self.io.para(self.t["care_gc_recheck"], indent=2)
                self.pause()
                continue
            command = "hermes " + " ".join(hermes_care.ACTIONS[key][0])
            if key == "doctor_fix":   # what the repair may change, and what the copy before it puts back
                for line in self.t["care_fix_note"]:
                    self.io.para(line, indent=2)
            if not self.confirm(self.t["care_q"].format(command=command)):
                continue
            self.show_care_result(key, hermes_care.run_action(self.home, key))
            force = True

    def decimal(self, value) -> str:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "?"
        text = f"{value:.1f}"
        return text.replace(".", ",") if self.lang == "pt" else text

    def gateway_state(self, gate: dict) -> str:
        """“running · version 0.23.1 · memory 412 MB · heartbeat 20 s ago”, or “stopped · no data”."""
        # a degraded or starting gateway runs (hermes_care.gateway: running): said with its state
        running = self.t["gate_" + gate["state"]] if gate.get("state") in ("degraded", "starting") else self.t["gate_running"]
        state = running if gate.get("running") else \
            self.t["gate_crashed"] if gate.get("crashed") else self.t["gate_stopped"]
        parts = []
        if gate.get("version"):
            parts.append(self.t["gate_version"].format(version=gate["version"]))
        if isinstance(gate.get("rss"), int) and not isinstance(gate.get("rss"), bool):
            parts.append(self.t["gate_rss"].format(rss=_bytes(gate["rss"], self.lang)))
        age = _ago(gate.get("heartbeat_age"), self.lang)
        if age:
            parts.append(self.t["gate_beat"].format(age=age))
        return " · ".join([state, *(parts or [self.t["no_data"]])])

    def disk_pace(self, found: dict) -> str:
        """How fast the disk fills, and when it would be full: rounded, and left out beyond a year."""
        per_day = found.get("disk_per_day")
        if isinstance(per_day, bool) or not isinstance(per_day, (int, float)) or per_day <= 0:
            return ""
        days, full = found.get("days_to_full"), ""
        if isinstance(days, (int, float)) and not isinstance(days, bool) and 0 <= days <= 365:
            full = self.t["care_full"].format(days=self.t["under_a_day"] if days < 1 else
                                              _plural(self.t["n_days"], int(round(days))))
        return self.t["care_pace"].format(per_day=_bytes(per_day, self.lang), full=full)

    def show_care_result(self, key: str, result: dict) -> None:
        """What one of Hermes' own commands answered. A doctor run that found problems is an answer, not a failure:
        its problems and Hermes' list of what to do are shown. Hermes writes them in English; in Portuguese, each
        heading over them says so."""
        steps = result.get("steps") or [{}]
        last = steps[-1]

        def note(lines) -> str:
            return self.t["hermes_words"] if lines else ""

        if result.get("reason") == "backup_failed":
            tail = list(last.get("tail") or ())
            self.io.say(self.t["care_backup_failed"].format(code=last.get("exit"), note=note(tail)))
            for line in tail:
                self.io.para(line, indent=4)
            return
        doctor = result.get("doctor") if isinstance(result.get("doctor"), dict) else None
        if doctor is None:   # what did not pass (✗) first: the last lines alone may be all ✓ (cli.step_lines)
            from .cli import step_lines

            tail = step_lines(last)
            self.io.say(self.t["care_ok"].format(code=last.get("exit")) if result.get("ok")
                        else self.t["care_failed"].format(code=last.get("exit"), note=note(tail)))
            for line in tail:
                self.io.para(line, indent=4)
            return
        actions = [line for line in doctor.get("actions") or () if isinstance(line, str)]
        problems = [line for line in doctor.get("problems") or () if isinstance(line, str)]
        summary = [line for line in doctor.get("summary") or () if isinstance(line, str)]
        found = result.get("problems_found")
        if found is None and last.get("exit") == 1 and (actions or problems or summary):
            found = doctor.get("found") or len(actions) or len(problems) or True   # an older hermes_care.py
        if not found and not result.get("ok"):
            lines = (problems[:8] + summary) or list(last.get("tail") or ())
            self.io.say(self.t["care_failed"].format(code=last.get("exit"), note=note(lines)))
            for line in lines:
                self.io.para(line, indent=4)
            return
        fixed = any(line.startswith("Fixed") for line in summary)
        if found:
            shown = problems[:8]
            if key == "doctor_fix" and fixed:
                shown = summary + shown
                self.io.say(self.t["doctor_fixed"].format(note=note(shown)))
            elif isinstance(found, int) and not isinstance(found, bool):
                self.io.say(self.n("doctor_found", found, note=note(shown)))
            else:
                self.io.say(self.t["doctor_found_some"].format(note=note(shown)))
            for line in shown:
                self.io.para(line, indent=4)
            if actions:
                self.io.say("  " + self.t["doctor_actions"].format(note=note(actions)))
                for number, line in enumerate(actions[:12], start=1):
                    self.io.para(f"{number}. {line}", indent=4)
        else:
            self.io.say(self.t["care_ok"].format(code=last.get("exit")) if fixed else self.t["doctor_clean"])
            for line in summary:
                self.io.para(line, indent=4)
        warnings = [line for line in doctor.get("warnings") or () if isinstance(line, str)]
        if warnings:
            self.io.say("  " + self.t["doctor_warnings"].format(note=note(warnings)))
            for line in warnings[:6]:
                self.io.para(line, indent=4)

    # ── operating system ───────────────────────────────────────────────────────────────────────────
    def operating_system(self) -> None:
        from . import system_update
        from .experience import experience

        host, planned = system_update.Host(), None
        while True:
            with experience(self.home) as conn:
                found = system_update.cached(conn, self.home, now=self.clock(), force=True)
            self.heading(self.t["os_title"])
            if not found or not found.get("supported"):
                self.io.say(self.t["os_unsupported"])
                self.pause()
                return
            yes, no = self.t["yes_word"], self.t["no_word"]
            self.io.say(self.t["os_line"].format(name=(found.get("os") or {}).get("name"), kernel=found.get("kernel")))
            self.io.say(self.t["os_updates"].format(count=found.get("upgradable", 0), security=found.get("security", 0),
                                                    when=_when(found.get("last_refresh"), self.lang)))
            reboot = yes + (f" ({', '.join(found.get('reboot_packages') or ())})" if found.get("reboot_packages") else "") \
                if found.get("reboot_required") else no
            self.io.say(self.t["os_reboot"].format(state=reboot))
            self.io.say(self.t["os_auto"].format(state=yes if (found.get("unattended") or {}).get("enabled") else no))
            self.io.say(self.t["os_health"].format(dpkg="ok" if found.get("dpkg_ok") else "⚠",
                                                   apt={True: "ok", False: "⚠", None: "?"}[found.get("apt_ok")],
                                                   busy=", ".join(found.get("busy") or ()) or no))
            units = [f"{unit['unit']} ({'ok' if unit['enabled'] else '⚠'})" for unit in found.get("units") or ()]
            if units:
                self.io.para(self.t["os_units"].format(units=", ".join(units) + (f" · linger {yes if found.get('linger') else no}"
                                                                                if found.get("linger") is not None else "")), indent=0)
            blocking = ", ".join(self.t["os_findings"].get(code, code) for code in found.get("findings") or ())
            self.io.say(self.t["os_ready"].format(state=yes if found.get("ready") else no + (f" ({blocking})" if blocking else "")))
            self.io.say()
            for number, label in enumerate(self.t["os_menu"], start=1):
                self.io.say(f"  {number}  {label}")
            chosen = self.pick(self.io.ask(self.t["care_prompt"]), len(self.t["os_menu"]))
            if chosen is None:
                return
            if chosen == 0:
                continue
            if chosen == 1 or (chosen == 2 and planned is None):
                planned = system_update.plan(host, refresh=True)
                if planned.get("error"):
                    self.io.say(self.t["error"].format(message=planned["error"]))
                    planned = None
                    continue
                self.io.say(self.t["os_plan"].format(upgrade=len(planned["upgrade"]),
                                                     install=_plural(self.t["n_new"], len(planned["install"])),
                                                     remove=len(planned["remove"]),
                                                     kept=_plural(self.t["n_kept"], len(planned["kept_back"]))))
                for item in planned["upgrade"] + planned["install"]:
                    if item["attention"]:
                        notes = ", ".join(self.t["os_attention"].get(code, code) for code in item["attention"])
                        self.io.say(f"    · {item['name']} {item.get('from') or ''} → {item['to']}: {notes}")
                if chosen == 1:
                    continue
            if chosen == 2:
                count = len(planned["upgrade"]) + len(planned["install"])
                if not count:
                    continue
                skip = [name for name in self.io.ask(self.t["os_skip"]).split() if name]
                if not self.confirm(self.n("os_upgrade_q", count)):
                    continue
                self.guarded(lambda: self.run_upgrade(host, system_update.clean_skip(skip)))
                planned = None
            elif chosen == 3:
                if self.confirm(self.t["os_repair_q"]):
                    def repair():
                        result = system_update.repair(self.home, host)
                        if result.get("released"):
                            self.io.say(self.t["os_released"].format(names=", ".join(result["released"])))
                        if result.get("state") == "lost":
                            self.io.say(self.t["os_lost"])
                        code = result.get("exit")
                        self.io.say(self.t["care_ok"].format(code=0) if result.get("ok") else self.t["os_repair_failed"].format(
                            code="?" if code is None else code))
                        self.io.say(self.t["os_folder"].format(folder=result.get("folder")))
                    self.guarded(repair)
            elif chosen == 4:
                folder, report = system_update.last_report(host)
                if folder is None:
                    self.io.say(self.t["os_no_report"])
                    continue
                if report.get("state") == "running":
                    self.io.say(self.t["os_still_running"])
                    continue
                self.show_upgrade(report, folder)

    def run_upgrade(self, host, skip: list) -> None:
        import time as _time

        from . import system_update

        folder, before, _snapshot = system_update.prepare(self.home, host, now=self.clock())
        started = system_update.start(host, folder, skip=skip)
        self.io.say(self.t["os_running"].format(unit=started.get("unit") or started.get("pid") or "process"))
        last = -1
        try:
            while system_update.progress(host, folder) == "running":
                _time.sleep(2)
                try:
                    done = sum(1 for line in (folder / "run.log").read_text(encoding="utf-8", errors="replace").splitlines()
                               if line.startswith(("Unpacking", "Setting up")))
                except OSError:
                    done = 0
                if done != last:
                    self.io.say(self.n("os_progress", done))
                    last = done
        except KeyboardInterrupt:
            self.io.say(self.t["os_background"])
            return
        self.show_upgrade(system_update.after(host, folder, before), folder)

    def show_upgrade(self, report: dict, folder) -> None:
        yes, no = self.t["yes_word"], self.t["no_word"]
        if report.get("state") == "lost":
            self.io.say(self.t["os_lost"])
        self.io.say(self.t["os_report"].format(exit=report.get("exit"), changed=_plural(self.t["n_changed"], len(report.get("changed") or ())),
                                               dpkg="ok" if report.get("dpkg_ok") else "⚠",
                                               apt="ok" if report.get("apt_ok") else "⚠",
                                               hermes={True: "ok", False: "⚠", None: "?"}[report.get("hermes_ok")],
                                               reboot=yes if report.get("reboot_required") else no))
        if report.get("stopped_units"):
            self.io.say(self.t["os_stopped"].format(units=", ".join(report["stopped_units"])))
        if report.get("stopped_clean"):
            self.io.say(self.t["os_stopped_clean"].format(units=", ".join(report["stopped_clean"])))
        self.io.say(self.t["os_folder"].format(folder=folder))
        self.pause()

    # ── reading ────────────────────────────────────────────────────────────────────────────────────
    def digest(self) -> None:
        from .cli import safe_settings
        from .evolve import options
        from .report import build, markdown

        config, _problems = safe_settings(self.home, self.runtime)
        chosen = options(config, self.runtime, strict=False)
        report = build(self.home, now=self.clock(), days=7, lang=self.lang, rates=config.get("rates_usd_per_million", {}),
                       own_transform=chosen["tool_hints"] and chosen["lessons"] != "off")
        self.say_markdown(markdown(report, self.lang))
        self.pause()

    def full_status(self) -> None:
        from . import status

        self.say_markdown(status.markdown(self.state(), self.lang))
        self.pause()

    def learn(self) -> None:
        from .autopilot import run_exclusive
        from .cli import effective_settings
        from .evolve import cycle

        config = effective_settings(self.home, self.runtime)
        import time

        from .autopilot import LEASE_SECONDS

        # the pass stops by itself before its lease ends, as the autopilot's: the time said is that limit
        self.io.say(self.t["learn_running"].format(minutes=(LEASE_SECONDS - 180) // 60))

        result = run_exclusive(self.home, lambda: cycle(self.home, now=self.clock(), runtime_config=self.runtime,
                                                        config=config, lang=self.lang, reflect=self.reflect,
                                                        backfill_budget_s=120.0,
                                                        deadline=time.monotonic() + LEASE_SECONDS - 180))
        history = result.get("history") or {}
        new = len(result.get("new_lessons") or [])
        opened = len(result.get("open_proposals") or [])
        if not (new or history.get("sessions")):
            self.io.say(self.t["learn_quiet"])
        else:
            self.io.say(self.t["learn_done"].format(sessions=_plural(self.t["sessions"], history.get("sessions", 0)),
                                                    calls=_plural(self.t["n_calls"], history.get("tool_calls", 0)),
                                                    new=_plural(self.t["n_new_lessons"], new),
                                                    open=_plural(self.t["n_proposals"], opened)))
            if new or opened:
                self.io.say(self.t["learn_next"])


def tty(stream) -> bool:
    """True when `stream` is a terminal someone types in. On Windows the NUL device (where `< NUL` and a service's
    stdin point) also says isatty(): there it must be a console as well."""
    try:
        if not stream.isatty():
            return False
        if os.name != "nt":
            return True
        try:
            number = stream.fileno()
        except (AttributeError, ValueError, OSError):
            return True   # no file behind it (a wrapper): its own isatty() said so
        import ctypes
        import msvcrt

        mode = ctypes.c_uint32()
        handle = msvcrt.get_osfhandle(number)
        return bool(ctypes.windll.kernel32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)))
    except (AttributeError, ValueError, OSError):
        return False


def interactive(stdin=None, stdout=None) -> bool:
    """True when someone can answer: both ends of this command are a terminal."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    return tty(stdin) and tty(stdout)


def run(home: Path, runtime_config: dict | None, *, lang: str, reflect=None) -> None:
    Menu(home, runtime_config, lang=lang, reflect=reflect).run()
