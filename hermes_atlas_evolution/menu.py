"""The interactive menu (0.8.2): `hermes evolution` in your own terminal, or `hermes evolution menu`.

Everything the dashboard panel does, step by step, with numbers to choose: decide on lessons (one by one or all at
once), decide proposals, retire lessons, undo any decision, change the settings (or apply the recommended setup),
choose which chat channels are you, take repeated entries out of Hermes' memory, read the digest and the full
status, and run a learning pass now. Decisions go through admin.py, the panel's own rules, and land in the same
history (`hermes evolution decisions`).

It opens only in the owner's terminal: never in a shell of an agent session (cli._agent_shell), and never without
a terminal to answer from. Plain text and numbered choices, so it works the same in any terminal, on Linux, macOS
and Windows, over SSH, and with a screen reader.
"""

from __future__ import annotations

import os
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
        "summary": "Lessons: {mode} mode · {active} active · {waiting} waiting for you · {proposals} open proposals",
        "loop": "Last pass: {last} · runtime guard {guard} · reflector {reflector}",
        "attention": "{n} point(s) need attention: see “Full status” (9).",
        "never": "not yet", "ok": "ok", "failed": "failed", "on": "on", "off": "off", "guard_on": "on", "guard_off": "off",
        "yes_value": "yes", "no_value": "no",
        "items": ["Lessons waiting for you ({waiting})", "Open proposals ({proposals})", "Active lessons ({active})",
                  "Decision history and undo", "Automatic actions ({auto})", "Settings", "Who is you in chats",
                  "Hermes memory: repeats and room", "Digest of the last 7 days", "Full status",
                  "Run a learning pass now"],
        "items_more": ["Hermes health (no agent, no tokens)", "Operating system ({os})"],
        "suggest_digest": "Schedule the weekly digest in your chat (Hermes cron, no agent, no tokens)",
        "suggest_os_repair": "Finish the system update that stopped half-way",
        "suggest_os_security": "Install {n} security update(s) of the system",
        "suggest_unused": "Free {size} of old Hermes tool versions",
        "deliver_prompt": "Deliver it where? local, or a channel such as telegram (Enter = local): ",
        "digest_done": "Scheduled: Mondays at 08:00, job {job}. Undo it in the history.",
        "digest_already": "The weekly digest is already scheduled.",
        "care_title": "Hermes health (read from Hermes' own files: no agent, no network, no tokens)",
        "care_gateway": "Gateway: {state} · version {version} · memory {rss} · heartbeat {age} s ago",
        "care_db": "Session database: journal {journal} · WAL {wal} · integrity {quick}",
        "care_disk": "Disk where Hermes writes: {free} free of {total}{pace}",
        "care_pace": " · it loses {per_day} a day (full in {days} days)",
        "care_memory": "Memory: {available} free of {total} · swap {used} of {swap} · load {load} on {cpus} CPUs",
        "care_folders": "Largest folders: {folders}",
        "care_unused": "Old tool versions nothing uses ({size}): {list}",
        "care_none": "Nothing needs attention.",
        "care_menu": ["Check again now (no network)", "Integrity of the installation (hermes pm doctor, offline)",
                      "Hermes' full check (hermes doctor: calls the providers' APIs; one-token ping only for an /anthropic endpoint)",
                      "Repair with Hermes (hermes backup --quick, then hermes doctor --fix)",
                      "Free old tool versions (hermes pm gc)"],
        "care_prompt": "Number · Enter back: ", "care_q": "Run `{command}` now?",
        "care_ok": "Done (exit {code}).", "care_failed": "Hermes answered with exit {code}:",
        "os_title": "Operating system",
        "os_unsupported": "This system is not one the plugin updates (Linux with apt: Ubuntu, Debian).",
        "os_line": "{name} · kernel {kernel}",
        "os_updates": "Updates waiting: {count} ({security} security) · package lists of {when}",
        "os_reboot": "Reboot waiting: {state}", "os_auto": "Automatic security updates: {state}",
        "os_health": "dpkg: {dpkg} · apt: {apt} · package manager busy: {busy}",
        "os_units": "Hermes' services after a reboot: {units}", "os_ready": "Ready to update: {state}",
        "os_menu": ["Check again", "Plan (refresh the lists and simulate the update)",
                    "Update now (your terminal, as root)", "Repair an update that stopped half-way",
                    "Show the last update's report"],
        "os_plan": "{upgrade} upgrade(s), {install} new, {remove} removed, {kept} kept back.",
        "os_attention": {"restarts_containers": "restarts the containers (Docker)", "needs_reboot": "needs a reboot",
                         "restarts_web": "restarts the web server", "remote_access": "remote access (SSH/sudo)",
                         "boot": "the boot loader", "package_manager": "the package manager itself"},
        "os_skip": "Packages to leave out this time, separated by spaces (Enter = none): ",
        "os_upgrade_q": "Update {n} package(s) now? A list of every package and a copy of /etc are kept first.",
        "os_running": "Updating in its own service ({unit}); Ctrl-C leaves it running.",
        "os_progress": "  {done} step(s)…", "os_background": "The update goes on in the background. Come back here for its report.",
        "os_report": "Exit {exit} · {changed} package(s) changed · dpkg {dpkg} · apt {apt} · Hermes {hermes} · reboot waiting: {reboot}",
        "os_stopped": "Services that were running and stopped: {units}", "os_folder": "Report and copies: {folder}",
        "os_stopped_clean": "Services that were running and ended without an error (most stop when idle; start the ones that should run): {units}",
        "os_released": "Released the packages a stopped update still held: {names}",
        "os_repair_q": "Finish the interrupted update now (dpkg --configure -a, then apt-get -f install)?",
        "os_no_report": "No update yet.", "os_still_running": "The update is still running; come back in a moment.",
        "os_lost": "The update stopped without finishing (it was killed, or the machine went down): repair it with this screen's repair option.",
        "yes_word": "yes", "no_word": "no", "unknown": "unknown",
        "auto_line": "Automatic actions: {mode} · {count} in 7 days",
        "attention_title": "Needs attention:", "suggest_title": "Suggestions:",
        "suggest_waiting": "Decide {n} lesson(s) waiting for you",
        "suggest_apply": "Apply {n} recommended change(s): {what}",
        "suggest_full": "Turn on the complete automatic actions (from now on the plugin applies these by itself)",
        "suggest_memory": "Tidy Hermes' memory: {n} repeat(s)",
        "suggest_advice": "Read {n} observation(s) with their numbers",
        "apply_all_q": "Apply these {n} change(s) to Hermes now? Each one can be undone in the history.",
        "applied": "Applied. To undo: {undo} (or from the history).", "applied_n": "Applied {n}.",
        "already": "It was already so in Hermes (the skill was already locked or pinned): nothing to undo.",
        "auto_names": {"off": "off", "safe": "safe", "full": "full"},
        "auto_title": "Automatic actions", "auto_q": "Set the automatic actions to “{mode}”?",
        "auto_now": "Now: {mode}.",
        "auto_modes": ["off: nothing happens by itself; everything waits for you.",
                       "safe: takes out copies of a memory entry that are the very same text (a copy of the file is kept).",
                       "full: also locks skills the curator keeps rewriting (at most 3 per pass)."],
        "auto_rules": "Each action goes to the history and can be undone; an automatic change you undo is never repeated.",
        "auto_recent": "Automatic actions of the last 7 days:", "auto_none": "None in the last 7 days.",
        "auto_prompt": "Decision number to undo · m change the mode · Enter back: ",
        "mode_prompt": "1 off · 2 safe · 3 full · Enter keeps it: ",
        "group_change": "Changes the plugin applies (reversible):", "group_lesson": "Lessons:",
        "group_manual": "Changes you apply:", "group_advice": "Observations:",
        "proposals_prompt_all": "Number to see · t apply the {n} change(s) · Enter back: ",
        "seen": "What was seen", "does": "What it does", "undo_how": "To undo", "look_with": "See it with",
        "change_prompt": "a apply now · r reject · Enter back: ",
        "advice_prompt": "d dismiss · Enter back: ", "failure_prompt": "e write the lesson · d dismiss · Enter back: ",
        "dismissed": "Dismissed: it stays closed while nothing changes.",
        "lesson_text": "The lesson, one sentence of what to do instead (Enter cancels): ",
        "lesson_activate": "Activate it for the agent now?", "lesson_added": "Lesson #{id} added ({status}).",
        "quit": "Quit", "choose": "Choose: ", "invalid": "Type one of the numbers shown.", "bye": "Bye.",
        "enter": "Press Enter to go back. ", "yes": ("y", "yes"), "yn": " [y/N] ",
        "error": "Not done: {message}",
        "waiting_title": "Lessons waiting for you", "waiting_empty": "Nothing waits for you.",
        "agent_single": "proposed by the agent: approve it on its own",
        "waiting_prompt": "Number to decide · t approve all ({n}) · Enter back: ",
        "waiting_prompt_one": "Number to decide · Enter back: ",
        "lesson_prompt": "a approve · r reject · Enter back: ",
        "approve_all": "Approve these {n} lessons for the agent?",
        "approved": "Approved: the agent reads it from its next session on.", "rejected": "Rejected.",
        "approved_n": "Approved {n}.", "skipped_n": "{n} not approved: they changed since you saw them.",
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
                    "already_undone": "It was already undone.", "unknown_current": "Not undone: the current value is unknown.",
                    "no_room": "Not undone: the memory file has no room for it now.",
                    "no_copy": "Not undone: the copy of the memory file is gone."},
        "where": {"terminal": "terminal", "dashboard": "panel", "automatic": "automatic"},
        "did": {"approve": "approved", "reject": "rejected", "retire": "retired", "reactivate": "reactivated",
                "set": "changed", "remove": "removed", "apply": "applied"},
        "what_lesson": "{did} lesson #{target}", "what_proposal": "{did} proposal #{target}",
        "what_setting": "changed {target}: {before} → {after}", "what_memory": "took a repeated entry out of {target}",
        "settings_title": "Settings", "settings_prompt": "Number to change · r apply the recommended setup · Enter back: ",
        "toggle_q": "Turn “{label}” {state}?", "choice_prompt": "{options} · Enter keeps it: ",
        "number_prompt": "New value, Enter keeps {value}: ", "list_prompt": "Values separated by commas, - clears, Enter keeps: ",
        "saved": "Saved. It applies within a minute, without restarting.",
        "reload": "Applies the next time the plugin loads: {keys} (hermes gateway restart).",
        "recommended_title": "The recommended setup changes:", "recommended_none": "This profile already has the recommended setup.",
        "apply_q": "Apply?", "nothing": "Nothing changed.", "guard_warn": "Turning the guard off lets the agent restart or delete what Hermes runs on.",
        "owner_title": "Who is you in chats (last 30 days)",
        "owner_hint": "Turns from these count as yours: you get every lesson there, and your failures there teach. Mark only what nobody else uses.",
        "owner_none": "No chat channel in 30 days: your CLI, TUI and Desktop always count as you.",
        "owner_prompt": "Number to mark or unmark as you · Enter back: ", "whole": "every chat of this channel",
        "sessions": "{n} sessions", "is_you": "is you", "suggested": "suggested",
        "mark_q": "Count {what} as you?", "unmark_q": "Stop counting {what} as you?",
        "memory_title": "Hermes memory", "memory_use": "{file}: {entries} entries · {chars} of {limit} characters ({percent}%){off}",
        "memory_off": " · turned off in Hermes", "memory_missing": "{file}: none yet",
        "memory_none": "Nothing repeats. Each entry is said once.",
        "memory_issues": "What repeats (taking one out saves about {tokens} tokens on every turn):",
        "memory_issues_free": "What repeats (Hermes already sends an identical repeat only once; taking it out tidies the file):",
        "list_changed": "It changed meanwhile (the panel or another terminal): look at it again; nothing was written.",
        "kinds": {"exact": "same entry twice", "variant": "the same entry, differing only in case or spacing",
                  "contained": "one entry says what the other says", "similar": "very similar entries"},
        "memory_prompt": "Number to choose which one stays · Enter back: ",
        "keep_prompt": "1 keep the first (takes the second out) · 2 keep the second · Enter back: ",
        "memory_removed": "Taken out. A copy of the file is in evolution/memory-backups; undo it in the history. New sessions read the change.",
        "memory_changed": "The entry changed since you saw it: nothing was taken out.",
        "memory_note": "Merging two entries into new words is the agent's job: ask it to rewrite them with its memory tool.",
        "learn_running": "Running a learning pass (up to two minutes)…",
        "learn_done": "Done: {sessions} sessions and {calls} tool calls read · {new} new lesson(s) · {open} open proposal(s).",
        "learn_quiet": "Done: nothing new.",
    },
    "pt": {
        "title": "Atlas Evolution {version} · perfil {profile}",
        "summary": "Lições: modo {mode} · {active} ativas · {waiting} esperando você · {proposals} propostas abertas",
        "loop": "Última passada: {last} · guarda de runtime {guard} · refletor {reflector}",
        "attention": "{n} ponto(s) pedem atenção: veja “Situação completa” (9).",
        "never": "ainda não", "ok": "ok", "failed": "falhou", "on": "ligado", "off": "desligado", "guard_on": "ligada",
        "guard_off": "desligada", "yes_value": "sim", "no_value": "não",
        "items": ["Lições esperando você ({waiting})", "Propostas abertas ({proposals})", "Lições ativas ({active})",
                  "Histórico de decisões e desfazer", "Ações automáticas ({auto})", "Configurações",
                  "Quem é você nos chats", "Memória do Hermes: repetições e espaço", "Resumo dos últimos 7 dias",
                  "Situação completa", "Rodar uma passada de aprendizado agora"],
        "items_more": ["Saúde do Hermes (sem agente, sem tokens)", "Sistema operacional ({os})"],
        "suggest_digest": "Agendar o resumo semanal no seu chat (cron do Hermes, sem agente, sem tokens)",
        "suggest_os_repair": "Terminar a atualização do sistema que parou no meio",
        "suggest_os_security": "Instalar {n} atualização(ões) de segurança do sistema",
        "suggest_unused": "Liberar {size} de versões antigas das ferramentas do Hermes",
        "deliver_prompt": "Entregar onde? local, ou um canal como telegram (Enter = local): ",
        "digest_done": "Agendado: segundas às 08:00, job {job}. Dá para desfazer no histórico.",
        "digest_already": "O resumo semanal já está agendado.",
        "care_title": "Saúde do Hermes (lida dos próprios arquivos do Hermes: sem agente, sem rede, sem tokens)",
        "care_gateway": "Gateway: {state} · versão {version} · memória {rss} · sinal de vida há {age} s",
        "care_db": "Banco de sessões: modo {journal} · WAL {wal} · integridade {quick}",
        "care_disk": "Disco onde o Hermes grava: {free} livres de {total}{pace}",
        "care_pace": " · perde {per_day} por dia (enche em {days} dias)",
        "care_memory": "Memória: {available} livres de {total} · swap {used} de {swap} · carga {load} em {cpus} CPUs",
        "care_folders": "Pastas maiores: {folders}",
        "care_unused": "Versões antigas de ferramentas que nada usa ({size}): {list}",
        "care_none": "Nada pede atenção.",
        "care_menu": ["Checar de novo agora (sem rede)", "Integridade da instalação (hermes pm doctor, sem rede)",
                      "Checagem completa do Hermes (hermes doctor: consulta as APIs dos provedores; ping de 1 token só para endpoint /anthropic)",
                      "Reparar com o Hermes (hermes backup --quick e depois hermes doctor --fix)",
                      "Liberar versões antigas de ferramentas (hermes pm gc)"],
        "care_prompt": "Número · Enter volta: ", "care_q": "Rodar `{command}` agora?",
        "care_ok": "Feito (saída {code}).", "care_failed": "O Hermes respondeu com saída {code}:",
        "os_title": "Sistema operacional",
        "os_unsupported": "Este sistema não é um que o plugin atualiza (Linux com apt: Ubuntu, Debian).",
        "os_line": "{name} · kernel {kernel}",
        "os_updates": "Atualizações esperando: {count} ({security} de segurança) · listas de pacotes de {when}",
        "os_reboot": "Reinício pendente: {state}", "os_auto": "Atualizações automáticas de segurança: {state}",
        "os_health": "dpkg: {dpkg} · apt: {apt} · gerenciador de pacotes ocupado: {busy}",
        "os_units": "Serviços do Hermes depois de um reinício: {units}", "os_ready": "Pronto para atualizar: {state}",
        "os_menu": ["Checar de novo", "Planejar (atualizar as listas e simular a atualização)",
                    "Atualizar agora (no seu terminal, como root)", "Reparar uma atualização que parou no meio",
                    "Ver o relatório da última atualização"],
        "os_plan": "{upgrade} atualização(ões), {install} nova(s), {remove} removida(s), {kept} retida(s).",
        "os_attention": {"restarts_containers": "reinicia os containers (Docker)", "needs_reboot": "pede reinício",
                         "restarts_web": "reinicia o servidor web", "remote_access": "acesso remoto (SSH/sudo)",
                         "boot": "o carregador de boot", "package_manager": "o próprio gerenciador de pacotes"},
        "os_skip": "Pacotes para deixar de fora desta vez, separados por espaço (Enter = nenhum): ",
        "os_upgrade_q": "Atualizar {n} pacote(s) agora? Antes, guarda a lista de todos os pacotes e uma cópia do /etc.",
        "os_running": "Atualizando num serviço próprio ({unit}); Ctrl-C deixa a atualização seguir.",
        "os_progress": "  {done} etapa(s)…", "os_background": "A atualização segue em segundo plano. Volte aqui para ver o relatório.",
        "os_report": "Saída {exit} · {changed} pacote(s) mudaram · dpkg {dpkg} · apt {apt} · Hermes {hermes} · reinício pendente: {reboot}",
        "os_stopped": "Serviços que rodavam e pararam: {units}", "os_folder": "Relatório e cópias: {folder}",
        "os_stopped_clean": "Serviços que rodavam e terminaram sem erro (a maioria para quando fica ociosa; suba os que deveriam rodar): {units}",
        "os_released": "Liberados os pacotes que uma atualização interrompida ainda retinha: {names}",
        "os_repair_q": "Terminar a atualização interrompida agora (dpkg --configure -a e depois apt-get -f install)?",
        "os_no_report": "Nenhuma atualização ainda.", "os_still_running": "A atualização ainda está rodando; volte daqui a pouco.",
        "os_lost": "A atualização parou sem terminar (foi morta, ou a máquina caiu): repare-a pela opção de reparo desta tela.",
        "yes_word": "sim", "no_word": "não", "unknown": "desconhecido",
        "auto_line": "Ações automáticas: {mode} · {count} em 7 dias",
        "attention_title": "Pede atenção:", "suggest_title": "Sugestões:",
        "suggest_waiting": "Decidir {n} lição(ões) esperando você",
        "suggest_apply": "Aplicar {n} mudança(s) recomendada(s): {what}",
        "suggest_full": "Ligar as ações automáticas completas (daqui em diante o plugin aplica isso sozinho)",
        "suggest_memory": "Organizar a memória do Hermes: {n} repetição(ões)",
        "suggest_advice": "Ler {n} observação(ões) com os números",
        "apply_all_q": "Aplicar estas {n} mudança(s) no Hermes agora? Cada uma pode ser desfeita no histórico.",
        "applied": "Aplicada. Para desfazer: {undo} (ou pelo histórico).", "applied_n": "{n} aplicada(s).",
        "already": "Já estava assim no Hermes (a skill já estava travada ou fixada): nada a desfazer.",
        "auto_names": {"off": "desligadas", "safe": "seguras", "full": "completas"},
        "auto_title": "Ações automáticas", "auto_q": "Passar as ações automáticas para “{mode}”?",
        "auto_now": "Agora: {mode}.",
        "auto_modes": ["desligadas: nada acontece sozinho; tudo espera você.",
                       "seguras: tira cópias de uma entrada da memória que são exatamente o mesmo texto (guarda uma cópia do arquivo).",
                       "completas: também trava skills que o curador reescreve sem parar (até 3 por passada)."],
        "auto_rules": "Cada ação vai para o histórico e pode ser desfeita; uma mudança automática que você desfaz nunca se repete.",
        "auto_recent": "Ações automáticas dos últimos 7 dias:", "auto_none": "Nenhuma nos últimos 7 dias.",
        "auto_prompt": "Número da decisão para desfazer · m mudar o modo · Enter volta: ",
        "mode_prompt": "1 desligadas · 2 seguras · 3 completas · Enter mantém: ",
        "group_change": "Mudanças que o plugin aplica (reversíveis):", "group_lesson": "Lições:",
        "group_manual": "Mudanças que você aplica:", "group_advice": "Observações:",
        "proposals_prompt_all": "Número para ver · t aplicar as {n} mudança(s) · Enter volta: ",
        "seen": "O que foi visto", "does": "O que faz", "undo_how": "Para desfazer", "look_with": "Veja com",
        "change_prompt": "a aplicar agora · r rejeitar · Enter volta: ",
        "advice_prompt": "d dispensar · Enter volta: ", "failure_prompt": "e escrever a lição · d dispensar · Enter volta: ",
        "dismissed": "Dispensada: fica fechada enquanto nada mudar.",
        "lesson_text": "A lição, uma frase do que fazer no lugar (Enter cancela): ",
        "lesson_activate": "Ativar para o agente agora?", "lesson_added": "Lição #{id} adicionada ({status}).",
        "quit": "Sair", "choose": "Escolha: ", "invalid": "Digite um dos números mostrados.", "bye": "Até logo.",
        "enter": "Enter para voltar. ", "yes": ("s", "sim", "y", "yes"), "yn": " [s/N] ",
        "error": "Não feito: {message}",
        "waiting_title": "Lições esperando você", "waiting_empty": "Nada espera você.",
        "agent_single": "proposta pelo agente: aprove sozinha",
        "waiting_prompt": "Número para decidir · t aprovar todas ({n}) · Enter volta: ",
        "waiting_prompt_one": "Número para decidir · Enter volta: ",
        "lesson_prompt": "a aprovar · r rejeitar · Enter volta: ",
        "approve_all": "Aprovar estas {n} lições para o agente?",
        "approved": "Aprovada: o agente passa a lê-la a partir da próxima sessão.", "rejected": "Rejeitada.",
        "approved_n": "{n} aprovada(s).", "skipped_n": "{n} não aprovada(s): mudaram desde que você as viu.",
        "changed": "Ela está diferente agora (uma passada a mudou): nada foi decidido. Veja de novo.",
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
        "reasons": {"changed_since": "Não desfeito: mudou depois (outra decisão ou uma passada). Nada mudou.",
                    "already_undone": "Já estava desfeita.", "unknown_current": "Não desfeito: o valor atual é desconhecido.",
                    "no_room": "Não desfeito: o arquivo de memória não tem espaço para ela agora.",
                    "no_copy": "Não desfeito: a cópia do arquivo de memória não existe mais."},
        "where": {"terminal": "terminal", "dashboard": "painel", "automatic": "automático"},
        "did": {"approve": "aprovou", "reject": "rejeitou", "retire": "aposentou", "reactivate": "reativou",
                "set": "mudou", "remove": "tirou", "apply": "aplicou"},
        "what_lesson": "{did} a lição #{target}", "what_proposal": "{did} a proposta #{target}",
        "what_setting": "mudou {target}: {before} → {after}", "what_memory": "tirou uma entrada repetida de {target}",
        "settings_title": "Configurações", "settings_prompt": "Número para mudar · r aplicar a configuração recomendada · Enter volta: ",
        "toggle_q": "{state} “{label}”?", "choice_prompt": "{options} · Enter mantém: ",
        "number_prompt": "Novo valor, Enter mantém {value}: ", "list_prompt": "Valores separados por vírgula, - limpa, Enter mantém: ",
        "saved": "Salvo. Vale em até um minuto, sem reiniciar.",
        "reload": "Vale na próxima carga do plugin: {keys} (hermes gateway restart).",
        "recommended_title": "A configuração recomendada muda:", "recommended_none": "Este perfil já está com a configuração recomendada.",
        "apply_q": "Aplicar?", "nothing": "Nada mudou.", "guard_warn": "Desligar a guarda deixa o agente reiniciar ou apagar aquilo em que o Hermes roda.",
        "owner_title": "Quem é você nos chats (últimos 30 dias)",
        "owner_hint": "Os turnos destes contam como seus: ali você recebe todas as lições, e as suas falhas ensinam. Marque só o que ninguém mais usa.",
        "owner_none": "Nenhum canal de chat em 30 dias: a sua CLI, TUI e Desktop sempre contam como você.",
        "owner_prompt": "Número para marcar ou desmarcar como você · Enter volta: ", "whole": "todo chat deste canal",
        "sessions": "{n} sessões", "is_you": "é você", "suggested": "sugerido",
        "mark_q": "Contar {what} como você?", "unmark_q": "Deixar de contar {what} como você?",
        "memory_title": "Memória do Hermes", "memory_use": "{file}: {entries} entradas · {chars} de {limit} caracteres ({percent}%){off}",
        "memory_off": " · desligada no Hermes", "memory_missing": "{file}: nenhuma ainda",
        "memory_none": "Nada se repete. Cada entrada aparece uma vez.",
        "memory_issues": "O que se repete (tirar uma economiza cerca de {tokens} tokens em todo turno):",
        "memory_issues_free": "O que se repete (o Hermes já envia uma repetição idêntica uma vez só; tirá-la arruma o arquivo):",
        "list_changed": "Mudou nesse meio-tempo (o painel ou outro terminal): veja de novo; nada foi gravado.",
        "kinds": {"exact": "a mesma entrada duas vezes", "variant": "a mesma entrada, diferindo só em maiúsculas ou espaços",
                  "contained": "uma entrada diz o que a outra já diz", "similar": "entradas muito parecidas"},
        "memory_prompt": "Número para escolher qual fica · Enter volta: ",
        "keep_prompt": "1 manter a primeira (tira a segunda) · 2 manter a segunda · Enter volta: ",
        "memory_removed": "Retirada. Uma cópia do arquivo está em evolution/memory-backups; desfaça pelo histórico. As novas sessões já leem a mudança.",
        "memory_changed": "A entrada mudou desde que você a viu: nada foi retirado.",
        "memory_note": "Juntar duas entradas em palavras novas é tarefa do agente: peça que ele as reescreva com a ferramenta de memória.",
        "learn_running": "Rodando uma passada de aprendizado (até dois minutos)…",
        "learn_done": "Pronto: {sessions} sessões e {calls} chamadas de ferramenta lidas · {new} lição(ões) nova(s) · {open} proposta(s) aberta(s).",
        "learn_quiet": "Pronto: nada novo.",
    },
}

# The settings form, in order, as (key, label EN, label PT). Labels follow the dashboard panel's.
FIELDS = (
    ("lessons", "Lessons", "Lições"),
    ("reflector", "Reflector (an AI model drafts lessons for failures with no template; drafts wait for you)",
     "Refletor (um modelo de IA rascunha lições para falhas sem modelo fixo; os rascunhos esperam você)"),
    ("reflector_daily_limit", "Reflector calls per day (1 to 50)", "Chamadas do refletor por dia (1 a 50)"),
    ("autopilot_minutes", "Learning pass every (minutes: 15 to 1440, or 0 = only by hand)",
     "Passada de aprendizado a cada (minutos: 15 a 1440, ou 0 = só à mão)"),
    ("language", "Language", "Idioma"),
    ("feedback_signals", "Count your corrections and thanks (numbers only)",
     "Contar as suas correções e agradecimentos (só números)"),
    ("slow_turn_seconds", "A turn is slow after (seconds)", "Um turno é lento depois de (segundos)"),
    ("tool_hints", "Add the lesson to the failing tool result", "Juntar a lição ao resultado da ferramenta que falhou"),
    ("agent_tools", "Let the agent read and propose lessons (proposals still wait for you)",
     "Deixar o agente ler e propor lições (as propostas continuam esperando você)"),
    ("runtime_guard", "Runtime guard (keeps the agent from taking Hermes down)",
     "Guarda de runtime (impede o agente de derrubar o Hermes)"),
    ("auto_actions", "Automatic actions (what the plugin does by itself)",
     "Ações automáticas (o que o plugin faz sozinho)"),
    ("owner_platforms", "Channels where every chat is you", "Canais em que todo chat é você"),
    ("owner_sender_ids", "Senders that are you", "Remetentes que são você"),
)
MODES = {"en": {"auto": "auto (recommended): templates apply by themselves", "suggest": "suggest: everything waits for you",
                "off": "off: measure only"},
         "pt": {"auto": "auto (recomendado): modelos fixos valem sozinhos", "suggest": "suggest: tudo espera você",
                "off": "off: só medir"}}
LANGUAGES = {"en": {"auto": "auto (Hermes' language)", "en": "English", "pt": "Português"},
             "pt": {"auto": "automático (o idioma do Hermes)", "en": "English", "pt": "Português"}}
ORIGINS = {"en": {"derived": "template", "operator": "you", "reflector": "reflector", "agent": "agent"},
           "pt": {"derived": "modelo fixo", "operator": "você", "reflector": "refletor", "agent": "agente"}}


class Quit(Exception):
    """The owner left the menu (0, end of input or Ctrl-C)."""


class Console:
    """Plain-text input and output, so tests can drive the menu with a script of answers."""

    def __init__(self, *, read=None, write=None, width: int | None = None, color: bool | None = None):
        self._read = read or input
        self._write = write or self._stdout
        columns = width or shutil.get_terminal_size((100, 24)).columns
        self.width = max(50, min(110, columns))
        if color is None:
            color = os.name != "nt" and sys.stdout.isatty() and not os.environ.get("NO_COLOR")
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
            self.say(textwrap.fill(line, width=self.width, initial_indent=pad, subsequent_indent=pad) if line else "")


def _bytes(value) -> str:
    if not isinstance(value, (int, float)):
        return "?"
    for unit, size in (("GB", 2**30), ("MB", 2**20)):
        if abs(value) >= size:
            return f"{value / size:.1f} {unit}"
    return f"{int(value)} B"


def _when(stamp, lang: str) -> str:
    try:
        moment = datetime.fromtimestamp(float(stamp))
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"
    return moment.strftime("%d/%m %H:%M" if lang == "pt" else "%m-%d %H:%M")


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
        self.t = TEXT[self.lang]
        self.io = console or Console()
        self.writer, self.clock, self.reflect = writer, clock, reflect

    # ── helpers ────────────────────────────────────────────────────────────────────────────────────
    def _(self, sentence: str) -> str:
        from .i18n import text

        return text(sentence, self.lang)

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
            self.io.say(self.t["error"].format(message=self._(str(error))))
        except (OSError, sqlite3.Error) as error:
            self.io.say(self.t["error"].format(message=self._("The Evolution store is busy; try again in a moment")
                                               if isinstance(error, sqlite3.OperationalError) else type(error).__name__))

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
        if result["reload"]:
            self.io.say(self.t["reload"].format(keys=", ".join(result["reload"])))

    # ── main screen ────────────────────────────────────────────────────────────────────────────────
    def state(self) -> dict:
        from . import status
        from .cli import safe_settings

        config, _problems = safe_settings(self.home, self.runtime)
        return status.build(self.home, now=self.clock(), config=config, runtime_config=self.runtime)

    def open_proposals(self) -> list[dict]:
        from . import explain, proposals
        from .experience import experience

        with experience(self.home) as conn:
            items = proposals.listing(conn, status="open")
        return [{**item, "explain": explain.describe(item, self.lang)} for item in items]

    def attention_lines(self, state: dict) -> list[str]:
        """The "Needs attention" items exactly as `status` writes them (values and lists filled in)."""
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
        return lines[:5] if state.get("attention") else []

    def suggestions(self, state: dict, items: list[dict]) -> list[tuple[str, object]]:
        """(text, action) for what is worth doing now, most useful first."""
        from . import memory_care

        found = []
        waiting = state["lessons"]["awaiting_approval"]
        if waiting:
            found.append((self.t["suggest_waiting"].format(n=waiting), self.waiting))
        changes = [item for item in items if item["explain"]["can_apply"]]
        if changes:
            what = ", ".join(dict.fromkeys(item["explain"]["title"].lower() for item in changes))
            found.append((self.t["suggest_apply"].format(n=len(changes), what=what), lambda: self.apply_all(changes)))
        if (state.get("auto_actions") or {}).get("mode") != "full":
            found.append((self.t["suggest_full"], lambda: self.set_auto("full")))
        repeats = len(memory_care.review(self.home, self.runtime)["issues"])
        if repeats:
            found.append((self.t["suggest_memory"].format(n=repeats), self.memory))
        system = state.get("system") or {}
        if {"dpkg_interrupted", "held_by_plugin"} & set(system.get("findings") or ()):
            found.insert(0, (self.t["suggest_os_repair"], self.operating_system))
        elif system.get("security"):
            found.append((self.t["suggest_os_security"].format(n=system["security"]), self.operating_system))
        care = state.get("care") or {}
        if "unused_tools" in (care.get("findings") or ()):
            found.append((self.t["suggest_unused"].format(size=_bytes(care.get("reclaimable"))), self.care))
        if "digest_unscheduled" in (state.get("attention") or ()):
            found.append((self.t["suggest_digest"], self.schedule_digest))
        advice = [item for item in items if item["explain"]["category"] == "advice"]
        if advice:
            found.append((self.t["suggest_advice"].format(n=len(advice)), self.proposals))
        return found[:6]

    def run(self) -> None:
        try:
            while True:
                state = self.state()
                items = self.open_proposals()
                automatic = state.get("auto_actions") or {"mode": "off", "last_7d": 0}
                counts = {"waiting": state["lessons"]["awaiting_approval"], "proposals": state["proposals_open"],
                          "active": state["lessons"]["active"],
                          "auto": self.t["auto_names"].get(automatic["mode"], automatic["mode"])}
                last = (state["autopilot"] or {}).get("last_pass")
                last_text = self.t["never"] if not last else \
                    f"{str(last.get('at') or '')[:16].replace('T', ' ')} ({self.t['ok'] if last.get('ok') is not False else self.t['failed']})"
                self.heading(self.t["title"].format(version=state["plugin_version"], profile=state["profile"]))
                self.io.say(self.t["summary"].format(mode=state["lessons"]["mode"], **counts))
                self.io.say(self.t["loop"].format(last=last_text,
                                                  guard=self.t["guard_on" if state["runtime_guard"]["enabled"] else "guard_off"],
                                                  reflector=self.t["on" if state["reflector"]["enabled"] else "off"]))
                self.io.say(self.t["auto_line"].format(mode=counts["auto"], count=automatic.get("last_7d", 0)))
                attention = self.attention_lines(state)
                if attention:
                    self.io.say()
                    self.io.say(self.io.bold(self.t["attention_title"]))
                    for item in attention:
                        first, *rest = item.split("\n")
                        self.io.para("• " + first, indent=2)
                        for sub in rest[:6]:
                            self.io.say(sub)
                ideas = self.suggestions(state, items)
                if ideas:
                    self.io.say()
                    self.io.say(self.io.bold(self.t["suggest_title"]))
                    for letter, (text, _action) in zip("abcdef", ideas):
                        self.io.say(f"   {letter}  {text}")
                self.io.say()
                os_name = ((state.get("system") or {}).get("os") or {}).get("name") or "Linux"
                labels = [*self.t["items"], *(label.format(os=os_name) for label in self.t["items_more"])]
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
                           self.operating_system]
                chosen = self.pick(answer, len(screens))
                if chosen is None:
                    self.io.say(self.t["invalid"])
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
                if self.confirm(self.t["approve_all"].format(n=len(bulk))):
                    with experience(self.home) as conn:
                        outcome = decisions.approve_all(conn, {item["id"]: item["fingerprint"] for item in bulk},
                                                        now=self.clock(), actor="terminal", lang=self.lang)
                    self.io.say(self.t["approved_n"].format(n=len(outcome["approved"])))
                    if outcome["skipped"]:
                        self.io.say(self.t["skipped_n"].format(n=len(outcome["skipped"])))
                continue
            chosen = self.pick(answer, len(items))
            if chosen is None:
                self.io.say(self.t["invalid"])
                continue
            item = items[chosen]
            self.io.say()
            self.io.para(item["lesson"], indent=2)
            action = {"a": "approve", "r": "reject"}.get(self.io.ask(self.t["lesson_prompt"]).lower()[:1])
            if action is None:
                continue
            with experience(self.home) as conn:
                result = decisions.decide_lesson(conn, item["id"], action, now=self.clock(), actor="terminal",
                                                 lang=self.lang, shown=item["fingerprint"])
            self.io.say(self.t["approved" if action == "approve" else "rejected"] if result["decided"]
                        else self.t["changed"])

    # ── proposals ──────────────────────────────────────────────────────────────────────────────────
    def apply_all(self, changes: list[dict]) -> None:
        from . import admin

        for item in changes:
            self.io.say(f"   · {item['explain']['title']}: {item['subject']}")
        if not self.confirm(self.t["apply_all_q"].format(n=len(changes))):
            return
        result = admin.decide(self.home, self.runtime, {"kind": "apply_all", "ids": [item["id"] for item in changes][:50]},
                              now=self.clock(), lang=self.lang, actor="terminal")
        self.io.say(self.t["applied_n"].format(n=len(result["applied"])))
        for item in result["skipped"]:
            reason = item.get("reason")
            self.io.say("   · #" + str(item["id"]) + ": " + (self.t["already"] if reason == "already" else
                                                           self.t["changed"] if reason == "not_open" else self._(str(reason))))

    def set_auto(self, mode: str) -> None:
        for line in self.t["auto_modes"]:
            self.io.para("• " + line, indent=2)
        if self.confirm(self.t["auto_q"].format(mode=self.t["auto_names"].get(mode, mode))):
            self.guarded(lambda: self.write({"auto_actions": mode}))

    def proposals(self) -> None:
        from . import admin, decisions, lessons
        from .experience import experience

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
                    told = item["explain"]
                    self.io.say(f"  {len(ordered):>2}  [{told['risk']}] {told['title']} · {item['subject']}")
            changes = [item for item in ordered if item["explain"]["can_apply"]]
            prompt = self.t["proposals_prompt_all"].format(n=len(changes)) if changes else self.t["proposals_prompt"]
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
            item = ordered[chosen]
            told = item["explain"]
            self.io.say()
            self.io.say(self.io.bold(f"  #{item['id']} · {told['title']} · {item['subject']} · {told['risk']}"))
            if told["what"]:
                self.io.say(f"  {self.t['seen']}:")
                self.io.para(told["what"])
            if told["does"]:
                self.io.say(f"  {self.t['does']}:")
                self.io.para(told["does"])
            if told["undo"]:
                self.io.say(f"  {self.t['undo_how']}: {told['undo']}")
            elif item.get("command") and told["category"] in ("advice", "manual"):
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
            action = keys.get(self.io.ask(prompt).lower()[:1])
            if action is None:
                continue
            if action == "write":
                text = self.io.ask(self.t["lesson_text"])
                if not text:
                    continue
                activate = self.confirm(self.t["lesson_activate"])
                with experience(self.home) as conn:
                    lesson_id = lessons.add_custom(conn, text, tool=item["subject"], origin="operator", now=self.clock(),
                                                   active=activate)
                    decisions.decide_proposal(conn, item["id"], "approve", now=self.clock(), actor="terminal",
                                              lang=self.lang)
                self.io.say(self.t["lesson_added"].format(id=lesson_id, status=self.t["did"]["approve"] if activate
                                                          else self.t["waiting_title"].lower()))
                continue
            if action == "apply":
                result = admin.decide(self.home, self.runtime, {"kind": "apply", "id": item["id"]}, now=self.clock(),
                                      lang=self.lang, actor="terminal")
                self.io.say(self.t["applied"].format(undo=result.get("undo")) if result.get("applied")
                            else self.t["already"] if result.get("reason") == "already" else self.t["changed"])
                continue
            now_item = next((row for row in self.open_proposals() if row["id"] == item["id"]), None)
            if now_item is None or (now_item["evidence"], now_item["command"]) != (item["evidence"], item["command"]):
                self.io.say(self.t["changed"])   # decide only on what was shown
                continue
            with experience(self.home) as conn:
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

    # ── automatic actions ──────────────────────────────────────────────────────────────────────────
    def automatic(self) -> None:
        from . import actions
        from .experience import experience

        while True:
            values, _suggestions = self.settings_now()
            mode = values.get("auto_actions") or "off"
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
                if chosen is not None and ("off", "safe", "full")[chosen] != mode:
                    self.guarded(lambda: self.write({"auto_actions": ("off", "safe", "full")[chosen]}))
                continue
            if not (answer.isascii() and answer.isdigit()) or int(answer) not in {row["id"] for row in done if not row["undone"]}:
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
            return self.t["what_setting"].format(target=row["target"], before=_value(row["before"].get("value")),
                                                 after=_value(row["after"].get("value")))
        if row["kind"] == "memory":
            return self.t["what_memory"].format(target=row["target"])
        if row["kind"] == "proposal" and isinstance((row.get("after") or {}).get("applied"), list):
            return f"{did} hermes " + " ".join(str(part) for part in row["after"]["applied"])
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
            if not (answer.isascii() and answer.isdigit()) or int(answer) not in {row["id"] for row in rows if not row["undone"]}:
                self.io.say(self.t["invalid"])
                continue
            self.undo(int(answer))

    # ── settings ───────────────────────────────────────────────────────────────────────────────────
    def label(self, key: str) -> str:
        for field, english, portuguese in FIELDS:
            if field == key:
                return portuguese if self.lang == "pt" else english
        return key

    def shown(self, key: str, value) -> str:
        if key == "lessons":
            return MODES[self.lang].get(value, str(value))
        if key == "language":
            return LANGUAGES[self.lang].get(value, str(value))
        if key == "auto_actions":
            return self.t["auto_names"].get(value, str(value))
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
            value = self.ask_value(key, values.get(key))
            if value is not None and value != values.get(key):
                self.guarded(lambda: self.write({key: value}))

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
                raise ValueError(f"{key} must be a whole number") from None
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
                tags = [self.t["sessions"].format(n=channel["sessions"])]
                if channel["origin"] in platforms:
                    tags.append(self.io.bold(self.t["is_you"]))
                elif ("platform", channel["origin"]) in suggested:
                    tags.append(self.t["suggested"])
                self.io.say(f"  {len(rows):>2}  {channel['origin']} ({self.t['whole']}) · {' · '.join(tags)}")
                for sender in channel["senders"]:
                    if sender["id"] is None:
                        continue
                    rows.append(("sender", sender["id"]))
                    tags = [self.t["sessions"].format(n=sender["sessions"])]
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
                    file=name, entries=info["entries"], chars=info["characters"], limit=info["limit"],
                    percent=info["percent"], off="" if info["enabled"] else self.t["memory_off"]))
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
            drop = second if keep == "1" else first

            def take_out(entry=drop):
                result = admin.decide(self.home, self.runtime, {"kind": "memory", "action": "remove",
                                                                "file": entry["file"], "fingerprint": entry["fingerprint"]},
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
        from .experience import experience
        from .status import _TEXT, _care_values

        force = True
        while True:
            with experience(self.home) as conn:
                found = hermes_care.cached(conn, self.home, now=self.clock(), force=force)
            force = False
            self.heading(self.t["care_title"])
            gate, db, memory = found["gateway"], found["database"], found["memory"]
            self.io.say(self.t["care_gateway"].format(
                state=self.t["yes_word"] if gate["running"] else self.t["no_word"], version=gate.get("version") or "?",
                rss=_bytes(gate.get("rss")), age=gate.get("heartbeat_age") if gate.get("heartbeat_age") is not None else "?"))
            if db.get("present"):
                self.io.say(self.t["care_db"].format(journal=db.get("journal") or "?", wal=_bytes(db.get("wal")),
                                                     quick=db.get("quick_check") or self.t["unknown"]))
            pace = ""
            if found.get("disk_per_day") and found["disk_per_day"] > 0 and found.get("days_to_full"):
                pace = self.t["care_pace"].format(per_day=_bytes(found["disk_per_day"]), days=found["days_to_full"])
            self.io.say(self.t["care_disk"].format(free=_bytes(found.get("free")), total=_bytes(found.get("total")), pace=pace))
            load = found.get("load") or {}
            self.io.say(self.t["care_memory"].format(available=_bytes(memory.get("available")),
                                                     total=_bytes(memory.get("total")), used=_bytes(memory.get("swap_used")),
                                                     swap=_bytes(memory.get("swap_total")), load=load.get("15m", "?"),
                                                     cpus=load.get("cpus", "?")))
            biggest = sorted(((name, size) for name, size in (found.get("sizes") or {}).items() if isinstance(size, int)),
                             key=lambda item: -item[1])[:6]
            if biggest:
                self.io.para(self.t["care_folders"].format(folders=" · ".join(f"{name} {_bytes(size)}" for name, size in biggest)),
                             indent=0)
            if found.get("unused_tools"):
                self.io.para(self.t["care_unused"].format(size=_bytes(found.get("reclaimable")), list=", ".join(
                    f"{item['folder']} ({_bytes(item['bytes'])})" for item in found["unused_tools"])), indent=0)
            texts = _TEXT.get(self.lang, _TEXT["en"])
            values = _care_values({"care": found, "system": {}})
            codes = [f"hermes_{code}" for code in found.get("findings") or () if f"hermes_{code}" in texts]
            self.io.say()
            if codes:
                self.io.say(self.io.bold(self.t["attention_title"]))
                for code in codes:
                    self.io.para("• " + texts[code].format(**values), indent=2)
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
            command = "hermes " + " ".join(hermes_care.ACTIONS[key][0])
            if not self.confirm(self.t["care_q"].format(command=command)):
                continue
            result = hermes_care.run_action(self.home, key)
            last = result["steps"][-1]
            self.io.say(self.t["care_ok"].format(code=last["exit"]) if result["ok"] else self.t["care_failed"].format(code=last["exit"]))
            doctor = result.get("doctor") or {}
            for line in (doctor.get("problems") or [])[:8] + (doctor.get("summary") or []):
                self.io.para(line, indent=4)
            if not doctor:
                for line in last.get("tail") or ():
                    self.io.para(line, indent=4)
            force = True

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
            self.io.say(self.t["os_ready"].format(state=yes if found.get("ready") else no + " (" + ", ".join(found.get("findings") or ()) + ")"))
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
                self.io.say(self.t["os_plan"].format(upgrade=len(planned["upgrade"]), install=len(planned["install"]),
                                                     remove=len(planned["remove"]), kept=len(planned["kept_back"])))
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
                if not self.confirm(self.t["os_upgrade_q"].format(n=count)):
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
                        self.io.say(self.t["care_ok"].format(code=0) if result.get("ok") else self.t["care_failed"].format(
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
                    self.io.say(self.t["os_progress"].format(done=done))
                    last = done
        except KeyboardInterrupt:
            self.io.say(self.t["os_background"])
            return
        self.show_upgrade(system_update.after(host, folder, before), folder)

    def show_upgrade(self, report: dict, folder) -> None:
        yes, no = self.t["yes_word"], self.t["no_word"]
        if report.get("state") == "lost":
            self.io.say(self.t["os_lost"])
        self.io.say(self.t["os_report"].format(exit=report.get("exit"), changed=len(report.get("changed") or ()),
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
        self.io.say()
        self.io.say(markdown(report, self.lang).rstrip())
        self.pause()

    def full_status(self) -> None:
        from . import status

        self.io.say()
        self.io.say(status.markdown(self.state(), self.lang).rstrip())
        self.pause()

    def learn(self) -> None:
        from .autopilot import run_exclusive
        from .cli import effective_settings
        from .evolve import cycle

        config = effective_settings(self.home, self.runtime)
        self.io.say(self.t["learn_running"])
        result = run_exclusive(self.home, lambda: cycle(self.home, now=self.clock(), runtime_config=self.runtime,
                                                        config=config, lang=self.lang, reflect=self.reflect,
                                                        backfill_budget_s=120.0))
        history = result.get("history") or {}
        new = len(result.get("new_lessons") or [])
        opened = len(result.get("open_proposals") or [])
        if not (new or history.get("sessions")):
            self.io.say(self.t["learn_quiet"])
        else:
            self.io.say(self.t["learn_done"].format(sessions=history.get("sessions", 0), calls=history.get("tool_calls", 0),
                                                    new=new, open=opened))


def interactive(stdin=None, stdout=None) -> bool:
    """True when someone can answer: both ends of this command are a terminal."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    try:
        return bool(stdin.isatty() and stdout.isatty())
    except (AttributeError, ValueError):
        return False


def run(home: Path, runtime_config: dict | None, *, lang: str, reflect=None) -> None:
    Menu(home, runtime_config, lang=lang, reflect=reflect).run()
