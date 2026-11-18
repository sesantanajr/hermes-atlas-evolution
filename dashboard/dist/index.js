(function () {
  "use strict";
  // Atlas Evolution panel for the Hermes dashboard. Plain JavaScript on the host's plugin SDK: no build step,
  // no dependency. It reads /api/plugins/hermes-atlas-evolution/snapshot for the profile the dashboard shows and,
  // when the owner is logged in to the dashboard, sends the owner's decisions to /decide. Everything is rendered
  // as text through React elements, never as markup.
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const NAME = "hermes-atlas-evolution";
  const React = SDK.React;
  const h = React.createElement;
  const hooks = SDK.hooks;
  const C = SDK.components || {};
  const useI18n = SDK.useI18n || function () { return { locale: "en" }; };
  const useRef = hooks.useRef || React.useRef;

  // Plural forms are "one|other": count() picks one for exactly 1.
  const TEXT = {
    en: {
      lang: "en", sessions: "{n} session|{n} sessions", title: "Atlas Evolution", profile: "profile", loading: "Reading what this profile learned…",
      busy: "The Evolution store is busy right now. Try again in a moment.", failed: "The panel could not load:",
      network: "The dashboard did not answer: check that it is running and reload.", httpError: "the dashboard answered with error {status}.",
      retry: "Reload", days: "days", close: "Close",
      health: { none: "No reading yet: the learning pass reads Hermes' health and the system's within 6 hours.",
                gateway: "Gateway", running: "running", stopped: "not running", rss: "memory", disk: "Free disk", of: "of",
                full: "full in {days} day|full in {days} days", memory: "Free memory", swap: "Swap in use", db: "Session database",
                tools: "Old tools", os: "System updates", security: "{n} security", reboot: "Reboot waiting",
                yes: "yes", no: "no", attention: "Needs attention", nothing: "Nothing needs attention.",
                journal: { wal: "WAL mode", delete: "rollback journal", truncate: "rollback journal", persist: "rollback journal",
                           memory: "journal in memory", off: "no journal" },
                integrityOk: "integrity ok", integrityBad: "integrity problem: see Needs attention",
                how: "Checks and repairs", hint: "Hermes' checks and repairs (hermes doctor, hermes pm doctor, hermes pm gc) and the system's update run from the menu in your terminal (options 12 and 13), with a backup first, never by the agent:" },
      doctor: { line: "Hermes doctor: {problems}, {warnings}, {optional}", checked: "(checked {when})",
                problems: "{n} problem|{n} problems", warnings: "{n} warning|{n} warnings",
                optional: "{n} optional piece not set up|{n} optional pieces not set up",
                failed: "Hermes doctor did not run: {why}",
                errors: { timeout: "it took too long", unavailable: "not available in this Hermes", no_answer: "it did not answer",
                          disabled: "turned off", not_a_profile: "not a Hermes profile",
                          runtime_moved: "Hermes' Python changed meanwhile; it runs again after the gateway restarts" } },
      tabs: { overview: "Overview", lessons: "Lessons", proposals: "Proposals", health: "Health", environment: "Environment", settings: "Settings", history: "History",
              digest: "Digest", status: "Status" },
      mode: { auto: "Lessons: automatic", suggest: "Lessons: on approval", off: "Lessons: off" },
      lessonModes: { auto: "automatic", suggest: "on approval", off: "off" },
      guardOn: "Runtime guard on", guardOff: "Runtime guard off", reflectorOn: "reflector on", reflectorOff: "reflector off",
      kpi: { active: "Active lessons", waiting: "Waiting for you", calls: "Tool calls", errors: "errors", turns: "Turns",
             failed: "failed", vetoed: "Runtime guard vetoes (7 d)", lastPass: "Last learning pass", passFailed: "failed: see Status",
             proposals: "Open proposals", flag: "needs attention", index: "Evolution index", week: "in 7 days",
             indexWait: "needs a few days of use" },
      never: "never", none: "—",
      readOnly: "Read-only: this dashboard has no login, so any program on this machine, the agent included, could act here as you. Turn on the dashboard login (a public dashboard.public_url turns it on) to decide from this panel.",
      readOnlyShared: "Read-only: this session was opened with the dashboard's shared session token (HERMES_DASHBOARD_SESSION_TOKEN), not with a login.",
      readOnlyOther: "Read-only here: Hermes serves this profile's panel from the profile's own process, which cannot see your login, and the dashboard's own profile could not decide for it. Enable Atlas Evolution in the dashboard's own profile too, and log in, to decide here.",
      menuHint: "Meanwhile, decide everything from the menu in your terminal:",
      waitingTitle: "Waiting for your decision", insightsTitle: "What the numbers say",
      waitingEmpty: "Nothing waits for you. Lessons from a built-in template apply by themselves; anything written by an AI model or by the agent shows up here.",
      waitingOthers: "No lesson waits for you. {n} proposal waits in the Proposals tab.|No lesson waits for you. {n} proposals wait in the Proposals tab.",
      approveAll: "Approve ({n})|Approve all ({n})", approve: "Approve", reject: "Reject", retire: "Retire", reactivate: "Reactivate",
      confirmAllTitle: "Approve these {n} lessons?", confirmOneTitle: "Approve this lesson?",
      confirmAllHint: "From the next session on, the agent reads them at the start of every session. Each one is measured and, with automatic lessons, retired if it does not help.",
      confirmOneHint: "From the next session on, the agent reads it at the start of every session. It is measured and, with automatic lessons, retired if it does not help.",
      agentSingle: "proposed by the agent: decide it separately",
      cancel: "Cancel", confirm: "Approve|Approve all",
      done: { approve: "Approved.", reject: "Rejected.", retire: "Retired.", reactivate: "Reactivated.", all: "Approved {n} lesson.|Approved {n} lessons.",
              undo: "Undone.", settings: "Saved. It applies within a minute, without restarting.", proposal: "Recorded.",
              applied: "Applied. You can undo it in the History tab.", dismissed: "Dismissed for 30 days; it comes back only if the situation is still there.",
              appliedN: "Applied {n} change. You can undo it in the History tab.|Applied {n} changes. You can undo each one in the History tab.",
              already: "It was already so in Hermes: nothing to undo.",
              notApplied: "Not applied ({n}):",
              changed: "It changed meanwhile: look at it again." },
      skippedChanged: "{n} skipped: it changed since you saw it.|{n} skipped: they changed since you saw them.",
      reasons: { busy: "The Evolution store is busy; try again in a moment.",
                 open_dashboard: "Read-only: this dashboard has no login, so decisions are taken in the terminal.",
                 shared_token: "Read-only: this session uses the dashboard's shared token, not a login.",
                 other_profile: "This profile is decided from the dashboard's own profile, with a login.",
                 missing_header: "The request did not come from the panel; reload the page and try again.",
                 unexpected: "Something unexpected stopped this here; nothing was changed by it. The dashboard's log has the details.",
                 invalid: "Invalid request.",
                 changed_since: "It changed since then, so it was left as it is.",
                 already_undone: "It was already undone.",
                 nothing_to_undo: "There is nothing to undo.",
                 no_room: "There is no room to put it back; nothing changed.",
                 no_copy: "The saved copy is gone, so it cannot be put back.",
                 unknown_current: "The current value could not be read, so nothing changed.",
                 one_by_one: "decided one by one: open it below",
                 not_open: "It changed meanwhile: look at it again.",
                 already: "It was already so in Hermes: nothing to undo.",
                 changed: "It changed meanwhile: look at it again.",
                 unknown: "it does not exist any more",
                 invalid_id: "not a valid id",
                 agent_needs_single: "proposed by the agent: decide it separately",
                 not_state: "It is no longer in a state that allows this: look at it again.",
                 other: "It was not done.", otherCode: "It was not done ({code})." },
      lessonsEmpty: "No lessons yet. A tool failure that comes back (3 times, in 2 sessions or on 2 days) becomes one.",
      proposalsEmpty: "No open proposals.",
      col: { status: "status", origin: "origin", tool: "tool", lesson: "lesson", effect: "measured effect", risk: "risk",
             subject: "subject", command: "command to run", action: "", when: "when", who: "where", what: "decision" },
      runAfter: "Approved. Hermes makes this change with the command below; run it in your terminal:",
      verdict: { effective: "helps", ineffective: "does not help", inconclusive: "inconclusive", withdrawn: "dropped from the queue without a decision" },
      status: { active: "active", candidate: "waiting", quarantined: "waiting (written)", retired: "retired" },
      origin: { derived: "built-in template", operator: "you", reflector: "reflector", agent: "agent" },
      actor: { terminal: "terminal", dashboard: "panel", automatic: "automatic" },
      action: { approve: "approved", reject: "rejected", retire: "retired", reactivate: "reactivated", set: "changed", apply: "applied" },
      proposalRef: "proposal #{id}", lessonRef: "lesson #{id}", items: "{n} item|{n} items",
      cronCreated: "scheduled the task {name} ({schedule}), a Hermes scheduled task (cron), no agent",
      memoryRemoved: "removed a duplicate entry from",
      undo: "Undo", undone: "undone",
      historyEmpty: "No decision yet.",
      applyAll: "Apply the change|Apply the {n} changes", applyAllHint: "Reversible changes to Hermes, each one recorded with its undo.",
      applyNow: "Apply now", dismiss: "Dismiss", seen: "What was seen", advice: "What to do", undoHow: "To undo",
      maintenanceRan: "ran Hermes' own maintenance (nothing to undo):",
      pluginSynced: "brought Atlas Evolution in profile {profile} to the lead's code ({before} → {after})",
      pluginSyncFailed: "; Hermes refused it, tried again tomorrow",
      gatewayRepaired: "pointed the gateway's service {unit} at the Python Hermes uses now ({before} → {after}); undo: {undo}",
      textHidden: "(text hidden: read it in your terminal)",
      gatewayRepairFailed: " (the gateway did not come up)",
      category: { change: "the plugin applies it", lesson: "lesson", manual: "you apply it", advice: "observation" },
      autoLine: "Mode", autoNames: { auto: "Auto", assist: "Assisted", manual: "Manual" },
      copy: "Copy", copied: "Copied", copyManual: "Text selected: press Ctrl+C (⌘C on a Mac) to copy it.",
      share: "Share what this profile learned with another profile:",
      s: {
        title: "Settings", save: "Save", saving: "Saving…", recommended: "Apply the recommended setup",
        recommendedHint: "Automatic lessons, reflector on, Auto mode, and the channels below that only you use.",
        reloadNote: "applies after `hermes gateway restart` (in the terminal, at the next session)",
        reloadPending: "Applies after `hermes gateway restart` (in the terminal, at the next session): {keys}.|Apply after `hermes gateway restart` (in the terminal, at the next session): {keys}.",
        lessons: "Lessons", lessonsHint: "automatic: lessons from a built-in template apply once your own turns hit the failure; on approval: every lesson waits for you; off: measure only.",
        reflector: "Reflector (an AI model drafts lessons for failures with no built-in template; drafts wait for you)",
        reflector_daily_limit: "Reflector calls per day (1 to 50)", autopilot_minutes: "Learning pass every (minutes: 15 to 1440, or 0 = only by hand)",
        language: "Language", feedback_signals: "Count your corrections and thanks (numbers only)",
        slow_turn_seconds: "A turn is slow after (seconds)", tool_hints: "Add the lesson to the failing tool result",
        agent_tools: "Let the agent read and propose lessons (proposals still wait for you)",
        runtime_guard: "Runtime guard (keeps the agent from taking Hermes down)",
        fleet_managed: "Let the lead profile act here (install, update and move to the trash)",
        fleetHint: "Off: the lead profile, which looks after every profile, still shows what it finds here but changes nothing in this profile by itself.",
        mode: "Mode (what the plugin does by itself)",
        autoModes: { auto: "Auto: thinks and acts for you in every profile, only what is safe and can be undone",
                     assist: "Assisted (default): on its own it only removes duplicate memory entries; everything else waits for you",
                     manual: "Manual: measures and proposes; nothing changes by itself" },
        autoHint: "Each action is recorded in the History with its undo; an automatic change you undo is never repeated.",
        guardWarn: "Turning the Runtime guard off lets the agent restart or delete what Hermes runs on.",
        owner: "Which chats count as you", ownerHint: "Turns from the marked channels count as yours: you get every lesson there, and your failures there teach. Mark only what nobody else uses.",
        platform: "every chat of this channel counts as you", sender: "this sender counts as you", suggested: "suggested",
        reason: { own_interface: "only clients with the API key write here, such as your own interface", hosted_owner: "Hermes marks this sender as the owner" },
        noChannels: "No chat channel in 30 days: your CLI, TUI and Desktop always count as you.",
        auto: "Automatic (follows Hermes and, here, the dashboard)", en: "English", pt: "Português (Brasil)",
        modes: { auto: "automatic (recommended): lessons from a built-in template apply by themselves", suggest: "on approval: every lesson waits for you", off: "off: measure only" },
        range: "Enter a whole number from {min} to {max}.", rangeAutopilot: "Enter 0, or a whole number from 15 to 1440.",
        fixNumbers: "Fix the highlighted numbers before saving.",
        short: { lessons: "Lessons", mode: "Mode", reflector: "Reflector", reflector_daily_limit: "Reflector calls per day",
                 autopilot_minutes: "Learning pass every (minutes)", language: "Language", feedback_signals: "Count corrections and thanks",
                 slow_turn_seconds: "Slow turn after (seconds)", agent_tools: "Agent reads and proposes lessons",
                 tool_hints: "Lesson in the failing tool result", runtime_guard: "Runtime guard", fleet_managed: "Lead profile acts here",
                 owner_sender_ids: "Senders that count as you", owner_platforms: "Channels where every chat counts as you", auto_actions: "Automatic actions" },
        onOff: { default: ["on", "off"] },
        langNames: { auto: "automatic", en: "English", pt: "Português (Brasil)" }, emptyList: "none"
      }
    },
    pt: {
      lang: "pt", sessions: "{n} sessão|{n} sessões", title: "Atlas Evolution", profile: "perfil", loading: "Lendo o que este perfil aprendeu…",
      busy: "O banco do Evolution está ocupado agora. Tente de novo em instantes.", failed: "O painel não carregou:",
      network: "O dashboard não respondeu: veja se ele está rodando e recarregue.", httpError: "o dashboard respondeu com o erro {status}.",
      retry: "Recarregar", days: "dias", close: "Fechar",
      health: { none: "Ainda sem leitura: a rodada de aprendizado lê a saúde do Hermes e a do sistema em até 6 horas.",
                gateway: "Gateway", running: "rodando", stopped: "parado", rss: "memória", disk: "Disco livre", of: "de",
                full: "enche em {days} dia|enche em {days} dias", memory: "Memória livre", swap: "Swap em uso", db: "Banco de sessões",
                tools: "Ferramentas antigas", os: "Atualizações do sistema", security: "{n} de segurança", reboot: "Reinício pendente",
                yes: "sim", no: "não", attention: "Pede atenção", nothing: "Nada pede atenção.",
                journal: { wal: "modo WAL", delete: "registro de reversão", truncate: "registro de reversão", persist: "registro de reversão",
                           memory: "registro em memória", off: "sem registro" },
                integrityOk: "integridade ok", integrityBad: "problema de integridade: veja Pede atenção",
                how: "Checagens e reparos", hint: "As checagens e os reparos do Hermes (hermes doctor, hermes pm doctor, hermes pm gc) e a atualização do sistema rodam pelo menu no seu terminal (opções 12 e 13), com backup antes, nunca pelo agente:" },
      doctor: { line: "Checagem do Hermes: {problems}, {warnings}, {optional}", checked: "(verificado em {when})",
                problems: "{n} problema|{n} problemas", warnings: "{n} aviso|{n} avisos",
                optional: "{n} opcional não configurado|{n} opcionais não configurados",
                failed: "A checagem do Hermes não rodou: {why}",
                errors: { timeout: "demorou demais", unavailable: "não existe neste Hermes", no_answer: "não respondeu",
                          disabled: "está desligado", not_a_profile: "não é um perfil do Hermes",
                          runtime_moved: "o Python do Hermes mudou nesse meio-tempo; volta a rodar depois que o gateway reiniciar" } },
      tabs: { overview: "Visão geral", lessons: "Lições", proposals: "Propostas", health: "Saúde", environment: "Ambiente", settings: "Configurações", history: "Histórico",
              digest: "Resumo", status: "Situação" },
      mode: { auto: "Lições: automáticas", suggest: "Lições: com aprovação", off: "Lições: desligadas" },
      lessonModes: { auto: "automáticas", suggest: "com aprovação", off: "desligadas" },
      guardOn: "Proteção do Hermes ligada", guardOff: "Proteção do Hermes desligada", reflectorOn: "refletor ligado", reflectorOff: "refletor desligado",
      kpi: { active: "Lições ativas", waiting: "Esperando você", calls: "Chamadas de ferramentas", errors: "erros", turns: "Turnos",
             failed: "falharam", vetoed: "Vetos da Proteção do Hermes (7 d)", lastPass: "Última rodada", passFailed: "falhou: veja a Situação",
             proposals: "Propostas abertas", flag: "pede atenção", index: "Índice de evolução", week: "em 7 dias",
             indexWait: "precisa de alguns dias de uso" },
      never: "nunca", none: "—",
      readOnly: "Só leitura: este dashboard não pede login, então qualquer programa desta máquina, inclusive o agente, poderia agir aqui como você. Ative o login do dashboard (um dashboard.public_url público o liga) para decidir por este painel.",
      readOnlyShared: "Só leitura: esta sessão foi aberta com o token de sessão compartilhado do dashboard (HERMES_DASHBOARD_SESSION_TOKEN), não com um login.",
      readOnlyOther: "Só leitura aqui: o Hermes serve o painel deste perfil pelo processo do próprio perfil, que não vê o seu login, e o perfil do próprio dashboard não pôde decidir por ele. Ative o Atlas Evolution também no perfil do dashboard, e entre com login, para decidir aqui.",
      menuHint: "Enquanto isso, decida tudo pelo menu no seu terminal:",
      waitingTitle: "Esperando a sua decisão", insightsTitle: "O que os números dizem",
      waitingEmpty: "Nada espera por você. Lições de padrão fixo valem sozinhas; o que um modelo de IA ou o agente escrever aparece aqui.",
      waitingOthers: "Nenhuma lição espera você. {n} proposta espera na aba Propostas.|Nenhuma lição espera você. {n} propostas esperam na aba Propostas.",
      approveAll: "Aprovar ({n})|Aprovar todas ({n})", approve: "Aprovar", reject: "Rejeitar", retire: "Aposentar", reactivate: "Reativar",
      confirmAllTitle: "Aprovar estas {n} lições?", confirmOneTitle: "Aprovar esta lição?",
      confirmAllHint: "A partir da próxima sessão, o agente lê essas lições no começo de cada sessão. Cada uma é medida e, com lições automáticas, aposentada se não ajudar.",
      confirmOneHint: "A partir da próxima sessão, o agente lê esta lição no começo de cada sessão. Ela é medida e, com lições automáticas, aposentada se não ajudar.",
      agentSingle: "proposta pelo agente: decida esta separadamente",
      cancel: "Cancelar", confirm: "Aprovar|Aprovar todas",
      done: { approve: "Aprovada.", reject: "Rejeitada.", retire: "Aposentada.", reactivate: "Reativada.", all: "{n} lição aprovada.|{n} lições aprovadas.",
              undo: "Desfeito.", settings: "Salvo. Vale em até um minuto, sem reiniciar.", proposal: "Registrado.",
              applied: "Aplicada. Dá para desfazer na aba Histórico.", dismissed: "Dispensada: some por 30 dias e só volta se a situação continuar.",
              appliedN: "{n} mudança aplicada. Dá para desfazer na aba Histórico.|{n} mudanças aplicadas. Dá para desfazer cada uma na aba Histórico.",
              already: "Já estava assim no Hermes: nada a desfazer.",
              notApplied: "Não aplicadas ({n}):",
              changed: "Mudou nesse meio-tempo: veja de novo." },
      skippedChanged: "{n} pulada: mudou desde que você a viu.|{n} puladas: mudaram desde que você as viu.",
      reasons: { busy: "O banco do Evolution está ocupado; tente de novo em instantes.",
                 open_dashboard: "Só leitura: este dashboard não tem login, então as decisões ficam no terminal.",
                 shared_token: "Só leitura: esta sessão usa o token compartilhado do dashboard, não um login.",
                 other_profile: "Este perfil se decide pelo perfil do próprio dashboard, com login.",
                 missing_header: "O pedido não veio do painel; recarregue a página e tente de novo.",
                 unexpected: "Algo inesperado interrompeu isto aqui; nada foi mudado por causa disso. O log do dashboard tem os detalhes.",
                 invalid: "Pedido inválido.",
                 changed_since: "Mudou desde então, por isso ficou como está.",
                 already_undone: "Já tinha sido desfeita.",
                 nothing_to_undo: "Não há nada para desfazer.",
                 no_room: "Não há espaço para pôr de volta; nada mudou.",
                 no_copy: "A cópia guardada não existe mais, então não dá para pôr de volta.",
                 unknown_current: "Não deu para ler o valor atual, então nada mudou.",
                 one_by_one: "se decide uma a uma: abra abaixo",
                 not_open: "Mudou nesse meio-tempo: veja de novo.",
                 already: "Já estava assim no Hermes: nada a desfazer.",
                 changed: "Mudou nesse meio-tempo: veja de novo.",
                 unknown: "não existe mais",
                 invalid_id: "não é um id válido",
                 agent_needs_single: "proposta pelo agente: decida esta separadamente",
                 not_state: "Não está mais numa situação que permita isso: veja de novo.",
                 other: "Não foi feito.", otherCode: "Não foi feito ({code})." },
      lessonsEmpty: "Ainda não há lições. Uma falha de ferramenta que volta (3 vezes, em 2 sessões ou em 2 dias) vira uma.",
      proposalsEmpty: "Nenhuma proposta aberta.",
      col: { status: "situação", origin: "origem", tool: "ferramenta", lesson: "lição", effect: "efeito medido", risk: "risco",
             subject: "assunto", command: "comando para rodar", action: "", when: "quando", who: "onde", what: "decisão" },
      runAfter: "Aprovada. Quem faz essa mudança é o Hermes, com o comando abaixo; rode no seu terminal:",
      verdict: { effective: "ajuda", ineffective: "não ajuda", inconclusive: "inconclusivo", withdrawn: "descartada sem decisão (fila limpa)" },
      status: { active: "ativa", candidate: "esperando", quarantined: "esperando (escrita)", retired: "aposentada" },
      origin: { derived: "padrão fixo", operator: "você", reflector: "refletor", agent: "agente" },
      actor: { terminal: "terminal", dashboard: "painel", automatic: "automático" },
      action: { approve: "aprovou", reject: "rejeitou", retire: "aposentou", reactivate: "reativou", set: "mudou", apply: "aplicou" },
      proposalRef: "a proposta #{id}", lessonRef: "a lição #{id}", items: "{n} item|{n} itens",
      cronCreated: "agendou a tarefa {name} ({schedule}), uma tarefa agendada do Hermes (cron), sem agente",
      memoryRemoved: "removeu uma entrada repetida de",
      undo: "Desfazer", undone: "desfeita",
      historyEmpty: "Nenhuma decisão ainda.",
      applyAll: "Aplicar a mudança|Aplicar as {n} mudanças", applyAllHint: "Mudanças reversíveis no Hermes, cada uma registrada com o desfazer.",
      applyNow: "Aplicar agora", dismiss: "Dispensar", seen: "O que foi visto", advice: "O que fazer", undoHow: "Para desfazer",
      maintenanceRan: "rodou a manutenção do próprio Hermes (nada a desfazer):",
      pluginSynced: "levou o Atlas Evolution do perfil {profile} ao código do perfil que cuida de todos ({before} → {after})",
      pluginSyncFailed: "; o Hermes recusou, nova tentativa amanhã",
      gatewayRepaired: "apontou o serviço do gateway {unit} para o Python que o Hermes usa agora ({before} → {after}); desfazer: {undo}",
      textHidden: "(texto oculto: leia no seu terminal)",
      gatewayRepairFailed: " (o gateway não subiu)",
      category: { change: "o plugin aplica", lesson: "lição", manual: "você aplica", advice: "observação" },
      autoLine: "Modo", autoNames: { auto: "Auto", assist: "Assistido", manual: "Manual" },
      copy: "Copiar", copied: "Copiado", copyManual: "Texto selecionado: tecle Ctrl+C (⌘C no Mac) para copiar.",
      share: "Leve o que este perfil aprendeu para outro perfil:",
      s: {
        title: "Configurações", save: "Salvar", saving: "Salvando…", recommended: "Aplicar a configuração recomendada",
        recommendedHint: "Lições automáticas, refletor ligado, Modo Auto e os canais abaixo que só você usa.",
        reloadNote: "vale depois de `hermes gateway restart` (no terminal, na próxima sessão)",
        reloadPending: "Vale depois de `hermes gateway restart` (no terminal, na próxima sessão): {keys}.|Valem depois de `hermes gateway restart` (no terminal, na próxima sessão): {keys}.",
        lessons: "Lições", lessonsHint: "automáticas: lições de padrão fixo valem quando os seus turnos encontram a falha; com aprovação: toda lição espera você; desligadas: só medir.",
        reflector: "Refletor (um modelo de IA rascunha lições para falhas sem padrão fixo; os rascunhos esperam você)",
        reflector_daily_limit: "Chamadas do refletor por dia (1 a 50)", autopilot_minutes: "Rodada de aprendizado a cada (minutos: 15 a 1440, ou 0 = só à mão)",
        language: "Idioma", feedback_signals: "Contar as suas correções e agradecimentos (só números)",
        slow_turn_seconds: "Um turno é lento depois de (segundos)", tool_hints: "Juntar a lição ao resultado da ferramenta que falhou",
        agent_tools: "Deixar o agente ler e propor lições (as propostas continuam esperando você)",
        runtime_guard: "Proteção do Hermes (impede o agente de derrubar o Hermes)",
        fleet_managed: "Deixar o perfil que cuida de todos agir aqui (instalar, atualizar e mover para a lixeira)",
        fleetHint: "Desligado: o perfil que cuida de todos continua mostrando o que encontra aqui, mas não muda nada neste perfil sozinho.",
        mode: "Modo (o que o plugin faz sozinho)",
        autoModes: { auto: "Auto: pensa e age por você em todos os perfis, só o que é seguro e pode ser desfeito",
                     assist: "Assistido (padrão): sozinho, só remove entradas repetidas da memória; o resto espera você",
                     manual: "Manual: mede e propõe; nada muda sozinho" },
        autoHint: "Cada ação fica no Histórico com o desfazer; uma mudança automática que você desfaz nunca se repete.",
        guardWarn: "Desligar a Proteção do Hermes deixa o agente reiniciar ou apagar aquilo em que o Hermes roda.",
        owner: "Quem é você nos chats", ownerHint: "Os turnos dos canais marcados contam como seus: ali você recebe todas as lições, e as suas falhas ensinam. Marque só o que ninguém mais usa.",
        platform: "todo chat deste canal conta como você", sender: "este remetente conta como você", suggested: "sugerido",
        reason: { own_interface: "só clientes com a chave da API escrevem aqui, como a sua própria interface", hosted_owner: "o Hermes marca este remetente como o dono" },
        noChannels: "Nenhum canal de chat em 30 dias: a sua CLI, TUI e Desktop sempre contam como você.",
        auto: "Automático (segue o Hermes e, aqui, o dashboard)", en: "English", pt: "Português (Brasil)",
        modes: { auto: "automáticas (recomendado): lições de padrão fixo valem sozinhas", suggest: "com aprovação: toda lição espera você", off: "desligadas: só medir" },
        range: "Digite um número inteiro de {min} a {max}.", rangeAutopilot: "Digite 0 ou um número inteiro de 15 a 1440.",
        fixNumbers: "Corrija os números destacados antes de salvar.",
        short: { lessons: "Lições", mode: "Modo", reflector: "Refletor", reflector_daily_limit: "Chamadas do refletor por dia",
                 autopilot_minutes: "Rodada de aprendizado a cada (minutos)", language: "Idioma", feedback_signals: "Contar correções e agradecimentos",
                 slow_turn_seconds: "Turno lento depois de (segundos)", agent_tools: "Agente lê e propõe lições",
                 tool_hints: "Lição no resultado da ferramenta", runtime_guard: "Proteção do Hermes", fleet_managed: "Perfil que cuida de todos age aqui",
                 owner_sender_ids: "Remetentes que contam como você", owner_platforms: "Canais em que todo chat conta como você", auto_actions: "Ações automáticas" },
        onOff: { default: ["ligado", "desligado"], runtime_guard: ["ligada", "desligada"], tool_hints: ["ligada", "desligada"],
                 auto_actions: ["ligadas", "desligadas"] },
        langNames: { auto: "automático", en: "English", pt: "Português (Brasil)" }, emptyList: "nenhum"
      }
    }
  };

  function language(locale) {
    return typeof locale === "string" && locale.toLowerCase().indexOf("pt") === 0 ? "pt" : "en";
  }

  function fill(template, values) {
    return String(template).replace(/\{(\w+)\}/g, function (_, key) { return values[key] === undefined ? "" : values[key]; });
  }

  function localeOf(lang) {
    return lang === "pt" ? "pt-BR" : "en-US";
  }

  // Numbers in the panel's language (2,5 in Portuguese, 2.5 in English), never in the browser's.
  function fmt(value, digits, lang) {
    if (value === null || value === undefined || (typeof value === "number" && Number.isNaN(value))) return "—";
    if (typeof value !== "number") return String(value);
    try {
      return value.toLocaleString(localeOf(lang), digits ? { minimumFractionDigits: digits, maximumFractionDigits: digits }
                                                         : { maximumFractionDigits: 2 });
    } catch (e) { return digits ? value.toFixed(digits) : String(value); }
  }

  // "one|other": the first form for exactly 1, the second otherwise; {n} is the number in the panel's language.
  function count(template, n, lang, values) {
    const forms = String(template).split("|");
    return fill(n === 1 || forms.length < 2 ? forms[0] : forms[1], Object.assign({ n: fmt(n, 0, lang) }, values || {}));
  }

  function when(stamp, lang) {
    const value = typeof stamp === "number" ? stamp * 1000 : typeof stamp === "string" ? Date.parse(stamp) : NaN;
    if (!isFinite(value)) return "—";
    try {
      return new Date(value).toLocaleString(localeOf(lang), { year: "numeric", month: "2-digit", day: "2-digit",
                                                              hour: "2-digit", minute: "2-digit" });
    } catch (e) { return "—"; }
  }

  // A code the plugin or the dashboard answered with, in words; a sentence (the plugin already wrote it in the
  // panel's language) as it came.
  function reason(t, code) {
    if (code === null || code === undefined || code === "") return t.reasons.other;
    const text = String(code);
    if (/\s/.test(text)) return text;
    if (Object.prototype.hasOwnProperty.call(t.reasons, text)) return t.reasons[text];
    if (/^not_/.test(text)) return t.reasons.not_state;
    return fill(t.reasons.otherCode, { code: text });
  }

  // ── a small Markdown renderer for the digest and status the plugin already writes (headings, lists, tables,
  // bold and code). It builds React elements, never HTML strings, so nothing in a lesson can become markup.
  function inline(text, key) {
    const parts = [];
    const pattern = /(\*\*[^*]+\*\*|`[^`]+`)/g;
    let last = 0, match, index = 0;
    while ((match = pattern.exec(text)) !== null) {
      if (match.index > last) parts.push(text.slice(last, match.index));
      const token = match[0];
      parts.push(token[0] === "`" ? h("code", { key: key + "c" + index++, className: "hae-code" }, token.slice(1, -1))
                                  : h("strong", { key: key + "b" + index++ }, token.slice(2, -2)));
      last = match.index + token.length;
    }
    if (last < text.length) parts.push(text.slice(last));
    return parts;
  }

  function cells(line) {
    return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(function (cell) { return cell.trim(); });
  }

  // A list; the items indented under an item (the Environment tab's "What was seen" and "What to do") become a
  // list inside that item.
  const BULLET = /^(\s*)[-*]\s+(.*)$/;

  function list(lines, start, key) {
    const base = BULLET.exec(lines[start])[1].length;
    const items = [];
    let i = start;
    while (i < lines.length) {
      const match = BULLET.exec(lines[i]);
      if (!match || match[1].length < base) break;
      if (match[1].length > base && items.length) {
        const owner = items[items.length - 1];
        const inner = list(lines, i, key + "-" + (items.length - 1) + "-" + owner.children.length);
        owner.children.push(inner.node);
        i = inner.next;
        continue;
      }
      items.push({ text: match[2], children: [] });
      i++;
    }
    return { next: i, node: h("ul", { key: key, className: "hae-list" }, items.map(function (item, n) {
      return h("li", { key: n }, inline(item.text, "li" + key + "-" + n), item.children);
    })) };
  }

  function Markdown(props) {
    const lines = String(props.text || "").split("\n");
    const out = [];
    let i = 0, key = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (/^\s*$/.test(line)) { i++; continue; }
      const heading = /^(#{1,4})\s+(.*)$/.exec(line);
      if (heading) {
        out.push(h("h" + Math.min(heading[1].length + 2, 6), { key: key++, className: "hae-md-h" }, inline(heading[2], "h" + key)));
        i++; continue;
      }
      if (/^\s*\|/.test(line)) {
        const rows = [];
        while (i < lines.length && /^\s*\|/.test(lines[i])) {
          if (!/^\s*\|[\s:|-]+\|\s*$/.test(lines[i])) rows.push(cells(lines[i]));
          i++;
        }
        const head = rows.shift() || [];
        out.push(h("div", { key: key++, className: "hae-table-wrap" }, h("table", { className: "hae-table" },
          h("thead", null, h("tr", null, head.map(function (cell, n) { return h("th", { key: n }, inline(cell, "th" + n)); }))),
          h("tbody", null, rows.map(function (row, r) {
            return h("tr", { key: r }, row.map(function (cell, n) { return h("td", { key: n }, inline(cell, "td" + r + "-" + n)); }));
          })))));
        continue;
      }
      if (BULLET.test(line)) {
        const found = list(lines, i, "l" + key++);
        out.push(found.node);
        i = found.next;
        continue;
      }
      const paragraph = [];
      while (i < lines.length && !/^\s*$/.test(lines[i]) && !/^(#{1,4})\s/.test(lines[i]) && !/^\s*[|*-]/.test(lines[i])) {
        paragraph.push(lines[i]);
        i++;
      }
      if (!paragraph.length) { paragraph.push(line); i++; }
      out.push(h("p", { key: key++, className: "hae-p" }, inline(paragraph.join(" "), "p" + key)));
    }
    return h("div", { className: "hae-md" }, out);
  }

  // ── building blocks
  // Copy with the Clipboard API where the browser offers it (HTTPS or localhost); elsewhere (the dashboard over
  // plain HTTP on the network) the command is selected for Ctrl+C, and the panel says so.
  function CopyCommand(props) {
    const state = hooks.useState("");
    const shown = state[0], setShown = state[1];
    const code = useRef(null);
    const copied = function () { setShown("copied"); setTimeout(function () { setShown(""); }, 1500); };
    const select = function () {
      const node = code.current;
      if (!node || !window.getSelection || !document.createRange) return false;
      try {
        const range = document.createRange();
        range.selectNodeContents(node);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        return true;
      } catch (e) { return false; }
    };
    const fallback = function () {
      let done = false;
      if (select()) { try { done = !!(document.execCommand && document.execCommand("copy")); } catch (e) { done = false; } }
      if (done) copied(); else setShown("manual");
    };
    const copy = function () {
      if (navigator.clipboard && navigator.clipboard.writeText && window.isSecureContext !== false) {
        navigator.clipboard.writeText(props.command).then(copied, fallback);
      } else {
        fallback();
      }
    };
    return h("div", { className: "hae-command" },
      h("code", { className: "hae-code", ref: code }, props.command),
      h("button", { type: "button", className: "hae-copy", onClick: copy, "aria-label": props.t.copy }, shown === "copied" ? props.t.copied : props.t.copy),
      shown === "manual" ? h("span", { className: "hae-muted", role: "status" }, props.t.copyManual) : null);
  }

  function Pill(props) {
    return h("span", { className: "hae-pill hae-pill-" + (props.tone || "neutral") }, props.children);
  }

  function Button(props) {
    return h("button", { type: "button", className: "hae-button" + (props.primary ? " hae-button-primary" : "") +
                                                      (props.danger ? " hae-button-danger" : ""),
                         onClick: props.onClick, disabled: !!props.disabled, title: props.title }, props.children);
  }

  function Section(props) {
    if (C.Card && C.CardHeader && C.CardTitle && C.CardContent) {
      return h(C.Card, { className: "hae-card" },
        h(C.CardHeader, null, h(C.CardTitle, null, props.title)),
        h(C.CardContent, null, props.children));
    }
    return h("section", { className: "hae-card hae-card-plain" }, h("h3", null, props.title), props.children);
  }

  // An alert is said in words too, not only by the border's color.
  function Kpi(props) {
    return h("div", { className: "hae-kpi" + (props.alert ? " hae-kpi-alert" : "") },
      h("div", { className: "hae-kpi-label" },
        props.alert ? h("span", { role: "img", "aria-label": props.t.kpi.flag, title: props.t.kpi.flag }, "⚠ ") : null,
        props.label),
      h("div", { className: "hae-kpi-value" }, props.value),
      props.detail ? h("div", { className: "hae-kpi-detail" }, props.detail) : null);
  }

  function effect(t, row) {
    if (!row.verdict) return t.none;
    const label = t.verdict[row.verdict] || row.verdict;
    if (row.rate_before === null || row.rate_before === undefined) return label;
    return label + " (" + fmt(row.rate_before, 3, t.lang) + " → " + fmt(row.rate_after, 3, t.lang) + ")";
  }

  // A lesson's sentence, or, where the panel cannot show it (no login: the text the owner, the reflector or the
  // agent wrote stays in the owner's terminal), the note that says so.
  function lessonText(t, row, key) {
    if (row.lesson) return inline(String(row.lesson), key + row.id);
    return row.text_hidden ? h("span", { className: "hae-muted" }, t.textHidden) : "";
  }

  function statusTone(status) {
    return status === "active" ? "good" : status === "retired" ? "muted" : "warn";
  }

  // The owner's decisions: one POST, then the snapshot is read again.
  // The panel of a profile other than the dashboard's own is served by that profile's plugin host, which never
  // decides (it cannot see the login). The dashboard's own process can: `profile=current` keeps the SDK from
  // scoping the request to the profile on screen, and `target` names that profile.
  function scope(data) {
    return data && data.target ? "&profile=current&target=" + encodeURIComponent(data.target) : "";
  }

  // fetchJSON throws "<status>: <body>"; the plugin's answers carry a `message` in the panel's language and an
  // `error` code, which the panel words itself when there is no message. Never the raw body.
  function failure(error, t) {
    const raw = String(error && error.message || error || "");
    const body = raw.indexOf("{") >= 0 ? raw.slice(raw.indexOf("{")) : "";
    let parsed = null;
    try { parsed = JSON.parse(body); } catch (e) { parsed = null; }
    if (parsed && typeof parsed === "object") {
      if (typeof parsed.message === "string" && parsed.message) return parsed.message;
      if (typeof parsed.error === "string" && parsed.error) return reason(t, parsed.error);
      if (typeof parsed.detail === "string" && parsed.detail) return reason(t, parsed.detail);
    }
    const status = /^(\d{3})\b/.exec(raw);
    if (status && status[1] === "503") return t.busy;
    return status ? fill(t.httpError, { status: status[1] }) : t.network;
  }

  // What a decision's `done` returns: a sentence, or {text, command} (a command the owner runs: kept on screen,
  // with Copy, until dismissed), or {text, bad}.
  function useActions(props) {
    const state = hooks.useState(null);
    const working = state[0], setWorking = state[1];
    const url = function () { return "/api/plugins/" + NAME + "/decide?lang=" + props.lang + scope(props.data); };
    const send = function (body, done) {
      if (!props.data.can_decide) return;
      setWorking(JSON.stringify(body).slice(0, 60));
      SDK.fetchJSON(url(), {
        method: "POST", headers: { "Content-Type": "application/json", "X-Atlas-Evolution": "1" }, body: JSON.stringify(body)
      }).then(function (result) {
        setWorking(null);
        props.notify(done ? done(result) : null);
        props.reload();
      }).catch(function (error) {
        setWorking(null);
        props.notify({ text: failure(error, props.t), bad: true });
      });
    };
    // Several requests one after the other (each answers in seconds, so no proxy in front of the dashboard times out),
    // then one notice with what happened to all of them.
    const sendAll = function (bodies, done) {
      if (!props.data.can_decide || !bodies.length) return;
      setWorking("batch");
      const results = [];
      const next = function (index) {
        if (index >= bodies.length) {
          setWorking(null);
          props.notify(done(results));
          props.reload();
          return;
        }
        SDK.fetchJSON(url(), {
          method: "POST", headers: { "Content-Type": "application/json", "X-Atlas-Evolution": "1" }, body: JSON.stringify(bodies[index])
        }).then(function (result) { results.push(result); next(index + 1); })
          .catch(function (error) { setWorking(null); props.notify({ text: failure(error, props.t), bad: true }); props.reload(); });
      };
      next(0);
    };
    return { send: send, sendAll: sendAll, working: working };
  }

  function LessonActions(props) {
    const t = props.t, row = props.row, act = props.actions;
    if (!props.data.can_decide) return null;
    const one = function (action) {
      return function () {
        act.send({ kind: "lesson", id: row.id, action: action, shown: row.fingerprint }, function (result) {
          if (!result || result.decided !== false) return t.done[action];
          return result.reason === "changed" ? count(t.skippedChanged, 1, t.lang) : { text: reason(t, result.reason), bad: true };
        });
      };
    };
    const buttons = [];
    if (row.status === "candidate" || row.status === "quarantined") {
      buttons.push(h(Button, { key: "a", primary: true, onClick: one("approve"), disabled: !!act.working }, t.approve));
      buttons.push(h(Button, { key: "r", onClick: one("reject"), disabled: !!act.working }, t.reject));
    } else if (row.status === "active") {
      buttons.push(h(Button, { key: "x", onClick: one("retire"), disabled: !!act.working }, t.retire));
    } else if (row.status === "retired" && row.verdict !== "withdrawn") {
      buttons.push(h(Button, { key: "o", onClick: one("reactivate"), disabled: !!act.working }, t.reactivate));
    }
    return h("div", { className: "hae-row-actions" }, buttons);
  }

  // A modal dialog: focus starts on its main button and stays inside, Esc cancels, and the focus goes back where it
  // was when it closes.
  function ConfirmAll(props) {
    const t = props.t, n = props.items.length;
    const box = useRef(null);
    hooks.useEffect(function () {
      const before = document.activeElement;
      const main = box.current && box.current.querySelector(".hae-button-primary");
      if (main) main.focus();
      return function () { try { if (before && before.focus) before.focus(); } catch (e) { /* gone */ } };
    }, []);
    const keys = function (event) {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); props.onCancel(); return; }
      if (event.key !== "Tab" || !box.current) return;
      const nodes = box.current.querySelectorAll("button:not([disabled])");
      if (!nodes.length) return;
      const first = nodes[0], last = nodes[nodes.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    return h("div", { className: "hae-modal-backdrop", role: "dialog", "aria-modal": "true",
                      "aria-labelledby": "hae-confirm-title", "aria-describedby": "hae-confirm-hint", onKeyDown: keys },
      h("div", { className: "hae-modal", ref: box },
        h("h3", { className: "hae-md-h", id: "hae-confirm-title" }, n === 1 ? t.confirmOneTitle : fill(t.confirmAllTitle, { n: fmt(n, 0, t.lang) })),
        h("p", { className: "hae-muted", id: "hae-confirm-hint" }, n === 1 ? t.confirmOneHint : t.confirmAllHint),
        h("ol", { className: "hae-list hae-confirm-list" }, props.items.map(function (item) {
          return h("li", { key: item.id }, h("span", { className: "hae-muted" }, "#" + item.id + " · " + item.tool + " · "),
            inline(item.lesson, "ca" + item.id));
        })),
        h("div", { className: "hae-actions" },
          h(Button, { onClick: props.onCancel }, t.cancel),
          h(Button, { primary: true, onClick: props.onConfirm }, count(t.confirm, n, t.lang)))));
  }

  // ── the tabs
  function gb(value, lang) {
    return typeof value === "number" ? fmt(value / 1073741824, 1, lang) + " GB" : "—";
  }

  // Days until the disk fills, as a person reads it: whole days (one decimal under 10), nothing beyond a year.
  function daysToFull(t, days) {
    if (typeof days !== "number" || !isFinite(days) || days < 0 || days > 365) return null;
    const shown = days < 10 ? Math.round(days * 10) / 10 : Math.round(days);
    return count(t.health.full, shown, t.lang, { days: fmt(shown, shown < 10 && shown % 1 ? 1 : 0, t.lang) });
  }

  // Hermes' own health and the operating system's, as the last learning pass read them: read-only here; the menu in
  // the terminal runs Hermes' checks and repairs and the system's update.
  function Health(props) {
    const t = props.t, h2 = t.health, lang = t.lang, status = props.data.status || {};
    const care = status.care || null, system = status.system || null;
    if (!care && !system) return h("p", { className: "hae-muted" }, h2.none);
    const gate = (care && care.gateway) || {}, db = (care && care.database) || {}, mem = (care && care.memory) || {};
    const findings = (care && care.findings) || [];
    const kpis = [];
    if (care) {
      const full = daysToFull(t, care.days_to_full);
      const journal = db.journal ? (h2.journal[String(db.journal).toLowerCase()] || String(db.journal)) : "—";
      const integrity = db.quick_check === "ok" ? h2.integrityOk : db.quick_check ? h2.integrityBad : null;
      kpis.push(h(Kpi, { key: "g", t: t, label: h2.gateway, value: gate.running ? h2.running : h2.stopped, alert: !gate.running || gate.stale,
                         detail: (gate.version ? "v" + gate.version + " · " : "") + h2.rss + " " + gb(gate.rss, lang) }));
      kpis.push(h(Kpi, { key: "d", t: t, label: h2.disk, value: gb(care.free, lang), alert: findings.some(function (c) { return /^disk_/.test(c); }),
                         detail: h2.of + " " + gb(care.total, lang) + (full ? " · " + full : "") }));
      kpis.push(h(Kpi, { key: "m", t: t, label: h2.memory, value: gb(mem.available, lang), alert: findings.indexOf("memory_low") >= 0,
                         detail: h2.of + " " + gb(mem.total, lang) }));
      kpis.push(h(Kpi, { key: "s", t: t, label: h2.swap, value: gb(mem.swap_used, lang), alert: findings.indexOf("swap_full") >= 0,
                         detail: h2.of + " " + gb(mem.swap_total, lang) }));
      kpis.push(h(Kpi, { key: "b", t: t, label: h2.db, value: journal, alert: findings.some(function (c) { return /^state_db/.test(c); }),
                         detail: "WAL " + gb(db.wal, lang) + (integrity ? " · " + integrity : "") }));
      kpis.push(h(Kpi, { key: "t", t: t, label: h2.tools, value: gb(care.reclaimable, lang), alert: findings.indexOf("unused_tools") >= 0,
                         detail: (care.unused_tools || []).map(function (item) { return item.folder; }).slice(0, 3).join(", ") || "—" }));
    }
    if (system && system.supported) {
      const osName = (system.os || {}).name;
      kpis.push(h(Kpi, { key: "o", t: t, label: h2.os, value: fmt(system.upgradable || 0, 0, lang), alert: !!system.security,
                         detail: (osName ? osName + " · " : "") + fill(h2.security, { n: fmt(system.security || 0, 0, lang) }) }));
      kpis.push(h(Kpi, { key: "r", t: t, label: h2.reboot, value: system.reboot_required ? h2.yes : h2.no, alert: !!system.reboot_required,
                         detail: (system.reboot_packages || []).join(", ") || "—" }));
    }
    return h("div", { className: "hae-stack" },
      h("div", { className: "hae-kpis" }, kpis),
      h(Section, { title: h2.attention },
        props.data.health_md ? h(Markdown, { text: props.data.health_md }) : h("p", { className: "hae-muted" }, h2.nothing)),
      h(Section, { title: h2.how },
        h("div", { className: "hae-stack" },
          h("span", { className: "hae-muted" }, h2.hint),
          props.data.commands && props.data.commands.menu ? h(CopyCommand, { t: t, command: props.data.commands.menu }) : null)));
  }

  // What waits for the owner, each thing once: a waiting lesson is also an open proposal of the "lesson" category.
  function waitingCount(data) {
    const pending = data.pending || [];
    const lessonIds = {};
    pending.forEach(function (item) { lessonIds[String(item.id)] = true; });
    if (!Array.isArray(data.proposals)) {
      return Math.max(pending.length, ((data.status || {}).proposals_open || 0));
    }
    const others = data.proposals.filter(function (row) {
      const lessonId = row.evidence && typeof row.evidence === "object" ? row.evidence.lesson_id : undefined;
      const isLesson = (row.explain || {}).category === "lesson" || row.kind === "lesson" || row.kind === "lesson_retire";
      return !(isLesson && lessonId !== undefined && lessonIds[String(lessonId)]);
    });
    return pending.length + others.length;
  }

  function Overview(props) {
    const t = props.t, data = props.data, state = data.status || {}, digest = data.report || {};
    const lang = t.lang;
    const lessons = state.lessons || {};
    const guard = state.runtime_guard || {};
    const tools = digest.tools || {};
    const turns = (digest.turns || {}).interactive || {};
    const pending = data.pending || [];
    const bulk = pending.filter(function (item) { return item.bulk; });
    const confirmState = hooks.useState(false);
    const confirming = confirmState[0], setConfirming = confirmState[1];
    const act = props.actions;
    const waiting = waitingCount(data);
    // status.build: {at, ok, seconds, summary, error} after a pass, null before the first one
    const pass = (state.autopilot || {}).last_pass;
    const passAt = pass && typeof pass === "object" ? pass.at : pass;
    const passFailed = !!(pass && typeof pass === "object" && pass.ok === false);
    const approveAll = function () {
      setConfirming(false);
      const items = {};
      bulk.forEach(function (item) { items[item.id] = item.fingerprint; });
      act.send({ kind: "all", items: items }, function (result) {
        const skipped = (result.skipped || []).filter(function (item) { return item.reason === "changed"; }).length;
        return count(t.done.all, (result.approved || []).length, lang) + (skipped ? " " + count(t.skippedChanged, skipped, lang) : "");
      });
    };
    const index = state.evolution_index || {};
    const trend = index.trend_7d;
    return h("div", { className: "hae-stack" },
      h("div", { className: "hae-kpis" },
        h(Kpi, { t: t, label: t.kpi.index, alert: typeof trend === "number" && trend <= -10,
                 value: typeof index.score === "number" ? fmt(index.score, 0, lang) + "/100" : t.none,
                 detail: typeof trend === "number" ? (trend > 0 ? "+" : "") + fmt(trend, 0, lang) + " " + t.kpi.week
                   : (typeof index.score === "number" ? null : t.kpi.indexWait) }),
        h(Kpi, { t: t, label: t.kpi.active, value: fmt(lessons.active || 0, 0, lang) }),
        h(Kpi, { t: t, label: t.kpi.waiting, value: fmt(waiting, 0, lang), alert: waiting > 0 }),
        h(Kpi, { t: t, label: t.kpi.calls + " (" + data.days + " d)", value: fmt(tools.calls || 0, 0, lang),
                 detail: tools.error_pct === null || tools.error_pct === undefined ? null : fmt(tools.error_pct, 1, lang) + "% " + t.kpi.errors }),
        h(Kpi, { t: t, label: t.kpi.turns + " (" + data.days + " d)", value: fmt(turns.turns || 0, 0, lang),
                 detail: turns.failed_pct === null || turns.failed_pct === undefined ? null : fmt(turns.failed_pct, 1, lang) + "% " + t.kpi.failed }),
        h(Kpi, { t: t, label: t.kpi.vetoed, value: fmt(guard.vetoed_7d || 0, 0, lang) }),
        h(Kpi, { t: t, label: t.kpi.lastPass, alert: passFailed,
                 value: passAt ? when(passAt, lang) : t.never,
                 detail: passFailed ? t.kpi.passFailed : null })),
      data.insights_md ? h(Section, { title: t.insightsTitle }, h(Markdown, { text: data.insights_md })) : null,
      h(Section, { title: t.waitingTitle },
        pending.length
          ? h("div", { className: "hae-stack" },
              data.can_decide && bulk.length
                ? h("div", { className: "hae-actions" }, h(Button, { primary: true, disabled: !!act.working,
                    onClick: function () { setConfirming(true); } }, count(t.approveAll, bulk.length, lang)))
                : null,
              pending.map(function (row) {
                return h("div", { key: row.id, className: "hae-waiting" },
                  h("div", { className: "hae-waiting-meta" }, h(Pill, { tone: "warn" }, "#" + row.id),
                    h(Pill, null, t.origin[row.origin] || row.origin), h("code", { className: "hae-code" }, row.tool),
                    row.bulk ? null : h("span", { className: "hae-muted" }, t.agentSingle)),
                  h("div", { className: "hae-lesson" }, lessonText(t, row, "w")),
                  h(LessonActions, { t: t, row: row, data: data, actions: act }));
              }),
              data.can_decide ? null : h(CopyCommand, { t: t, command: data.commands.review }))
          : h("p", { className: "hae-muted" }, waiting > 0 ? count(t.waitingOthers, waiting, lang) : t.waitingEmpty)),
      confirming ? h(ConfirmAll, { t: t, items: bulk, onCancel: function () { setConfirming(false); }, onConfirm: approveAll }) : null,
      h(Section, { title: t.share },
        h("div", { className: "hae-stack" }, h(CopyCommand, { t: t, command: data.commands.export }),
          h(CopyCommand, { t: t, command: data.commands.import }))));
  }

  function Lessons(props) {
    const t = props.t, rows = props.data.lessons || [];
    const marks = {};
    (props.data.pending || []).forEach(function (item) { marks[item.id] = item.fingerprint; });
    if (!rows.length) return h("p", { className: "hae-muted" }, t.lessonsEmpty);
    return h("div", { className: "hae-table-wrap" }, h("table", { className: "hae-table" },
      h("thead", null, h("tr", null, ["#", t.col.status, t.col.origin, t.col.tool, t.col.lesson, t.col.effect, t.col.action].map(function (label, n) {
        return h("th", { key: n }, label);
      }))),
      h("tbody", null, rows.map(function (row) {
        const full = Object.assign({}, row, { fingerprint: marks[row.id] });
        return h("tr", { key: row.id, className: row.status === "retired" ? "hae-row-muted" : "" },
          h("td", null, row.id),
          h("td", null, h(Pill, { tone: statusTone(row.status) }, t.status[row.status] || row.status)),
          h("td", null, t.origin[row.origin] || row.origin),
          h("td", null, h("code", { className: "hae-code" }, row.tool)),
          h("td", { className: "hae-lesson" }, lessonText(t, row, "l")),
          h("td", null, effect(t, row)),
          h("td", null, h(LessonActions, { t: t, row: full, data: props.data, actions: props.actions })));
      }))));
  }

  function Proposals(props) {
    const t = props.t, rows = props.data.proposals || [], act = props.actions;
    if (!rows.length) return h("p", { className: "hae-muted" }, t.proposalsEmpty);
    const changes = rows.filter(function (row) { return (row.explain || {}).bulk; });   // one-by-one calls stay out
    const applied = function (result) {
      if (result && result.applied === true) return t.done.applied;
      if (result && Array.isArray(result.applied)) return count(t.done.appliedN, result.applied.length, t.lang);
      return result && result.reason === "already" ? t.done.already : t.done.changed;
    };
    const applyAll = function () {   // batches of 10; the plugin applies each batch one change after the other
      const ids = changes.map(function (row) { return row.id; }), bodies = [];
      for (let at = 0; at < ids.length; at += 10) bodies.push({ kind: "apply_all", ids: ids.slice(at, at + 10) });
      act.sendAll(bodies, function (results) {
        let done = 0;
        const reasons = [];
        results.forEach(function (result) {
          done += (result.applied || []).length;
          (result.skipped || []).forEach(function (item) { reasons.push("#" + item.id + " " + reason(t, item.reason)); });
        });
        const text = count(t.done.appliedN, done, t.lang) +
          (reasons.length ? " " + fill(t.done.notApplied, { n: fmt(reasons.length, 0, t.lang) }) + " " + reasons.join("; ") : "");
        return reasons.length ? { text: text, bad: !done } : text;
      });
    };
    // a change the plugin has no way to apply here (no plan) is the owner's to run, as the menu shows it
    const kindOf = function (row) {
      const told = row.explain || {};
      return told.category === "change" && told.can_apply === false ? "manual" : told.category;
    };
    const order = ["change", "lesson", "manual", "advice"];
    const sorted = rows.slice().sort(function (a, b) { return order.indexOf(kindOf(a)) - order.indexOf(kindOf(b)); });
    return h("div", { className: "hae-stack" },
      props.data.can_decide && changes.length ? h("div", { className: "hae-actions hae-actions-left" },
        h(Button, { primary: true, onClick: applyAll, disabled: !!act.working }, count(t.applyAll, changes.length, t.lang)),
        h("span", { className: "hae-muted" }, t.applyAllHint)) : null,
      sorted.map(function (row) {
        const told = Object.assign({ title: row.kind, risk: row.risk, category: "manual" }, row.explain || {}, { category: kindOf(row) || "manual" });
        const title = told.title || row.kind || "#" + row.id;   // a kind this panel does not know still has a title
        const lesson = row.evidence && row.evidence.lesson;
        const hidden = !lesson && row.text_hidden;   // written by the owner, the reflector or the agent: not here
        const send = function (body, done) { return function () { act.send(body, done); }; };
        // a command the owner runs stays on screen, with Copy, until dismissed
        const decided = function (result) { return result && result.run_this ? { text: t.runAfter, command: result.run_this } : t.done.proposal; };
        let buttons = null;
        if (props.data.can_decide) {
          if (told.category === "change") {
            buttons = [h(Button, { key: "a", primary: true, disabled: !!act.working,
                                   onClick: send({ kind: "apply", id: row.id }, applied) }, t.applyNow),
                       h(Button, { key: "r", disabled: !!act.working, onClick: send({ kind: "proposal", id: row.id, action: "reject" }, decided) }, t.reject)];
          } else if (told.category === "advice") {
            buttons = [h(Button, { key: "d", disabled: !!act.working, onClick: send({ kind: "proposal", id: row.id, action: "reject" }, function () { return t.done.dismissed; }) }, t.dismiss)];
          } else {
            buttons = [h(Button, { key: "a", primary: true, disabled: !!act.working, onClick: send({ kind: "proposal", id: row.id, action: "approve" }, decided) }, t.approve),
                       h(Button, { key: "r", disabled: !!act.working, onClick: send({ kind: "proposal", id: row.id, action: "reject" }, decided) }, t.reject)];
          }
        }
        return h(Section, { key: row.id, title: title },
          h("div", { className: "hae-stack" },
            h("div", { className: "hae-waiting-meta" },
              h(Pill, { tone: row.risk === "high" ? "bad" : row.risk === "medium" ? "warn" : "neutral" }, t.col.risk + ": " + (told.risk || row.risk || t.none)),
              told.category ? h(Pill, { tone: told.category === "change" ? "good" : "muted" }, t.category[told.category] || told.category) : null,
              row.subject ? (String(row.kind || "").indexOf("doctor_") === 0
                ? h("span", null, (t.lang === "pt" ? "perfil " : "profile "),
                    h("code", { className: "hae-code" }, String(row.subject).split("/")[0]))
                : h("code", { className: "hae-code" }, row.subject)) : null, h("span", { className: "hae-muted" }, "#" + row.id)),
            told.what && told.category !== "lesson" ? h("div", null, h("strong", null, t.seen + ": "), inline(String(told.what), "w" + row.id)) : null,
            lesson ? h("div", { className: "hae-lesson" }, inline(String(lesson), "p" + row.id))
              : hidden ? h("div", { className: "hae-lesson hae-muted" }, t.textHidden) : null,
            told.advice ? h("div", null, h("strong", null, t.advice + ": "), inline(String(told.advice), "a" + row.id)) : null,
            told.does ? h("div", { className: "hae-muted" }, inline(String(told.does), "d" + row.id)) : null,
            told.undo ? h("div", { className: "hae-muted" }, t.undoHow + ": ", h("code", { className: "hae-code" }, told.undo)) : null,
            (told.category === "advice" || told.category === "manual") && row.command ? h(CopyCommand, { t: t, command: row.command }) : null,
            buttons ? h("div", { className: "hae-row-actions" }, buttons) : null));
      }));
  }

  // The number fields keep what the owner types (an empty field included) and are checked on save, with the
  // plugin's own ranges (evolve.options): [min, max], and the learning pass also takes 0 (only by hand).
  const NUMBERS = { reflector_daily_limit: [1, 50], autopilot_minutes: [15, 1440, 0], slow_turn_seconds: [5, 3600] };

  function numberValue(key, value) {   // the whole number the field holds, or null when it holds none in range
    const range = NUMBERS[key];
    const text = typeof value === "number" ? String(value) : String(value === null || value === undefined ? "" : value).trim();
    if (!/^\d+$/.test(text)) return null;
    const n = Number(text);
    if (range.length > 2 && n === range[2]) return n;
    return n >= range[0] && n <= range[1] ? n : null;
  }

  function Settings(props) {
    const t = props.t, s = t.s, data = props.data, act = props.actions;
    const current = (data.settings || {}).values || {};
    const formState = hooks.useState(current);
    const form = formState[0], setForm = formState[1];
    hooks.useEffect(function () { setForm(current); }, [JSON.stringify(current)]);
    const set = function (key, value) { setForm(Object.assign({}, form, (function () { const o = {}; o[key] = value; return o; })())); };
    const channels = (data.owner || {}).channels || [];
    const suggestions = (data.owner || {}).suggestions || [];
    const reloadKeys = (data.settings || {}).reload_keys || [];
    const pendingReload = (data.settings || {}).reload || [];
    const disabled = !data.can_decide || !!act.working;
    const invalid = function (values) {
      return Object.keys(NUMBERS).filter(function (key) { return key in values && numberValue(key, values[key]) === null; });
    };
    const clean = function (values) {   // the numbers as numbers, once they are valid
      const out = Object.assign({}, values);
      Object.keys(NUMBERS).forEach(function (key) {
        if (key in out && numberValue(key, out[key]) !== null) out[key] = numberValue(key, out[key]);
      });
      return out;
    };
    const changed = function (values) {
      const out = {}, wanted = clean(values);
      Object.keys(wanted).forEach(function (key) {
        if (JSON.stringify(wanted[key]) !== JSON.stringify(current[key])) out[key] = wanted[key];
      });
      return out;
    };
    const wrong = invalid(form);
    const save = function (values) {
      if (invalid(values).length) { props.notify({ text: s.fixNumbers, bad: true }); return; }
      const diff = changed(values);
      if (!Object.keys(diff).length) return;
      act.send({ kind: "settings", values: diff }, function () { return t.done.settings; });
    };
    const toggleList = function (key, value) {
      const items = (form[key] || []).slice();
      const at = items.indexOf(value);
      if (at >= 0) items.splice(at, 1); else items.push(value);
      set(key, items);
    };
    const recommended = function () {
      const values = Object.assign({}, form, { lessons: "auto", reflector: true, mode: "auto" });
      suggestions.forEach(function (item) {
        const key = item.kind === "platform" ? "owner_platforms" : "owner_sender_ids";
        if ((values[key] || []).indexOf(item.value) < 0) values[key] = (values[key] || []).concat([item.value]);
      });
      setForm(values);
      save(values);
    };
    const suggestedFor = function (kind, value) {
      return suggestions.find(function (item) { return item.kind === kind && item.value === value; });
    };
    const field = function (key, control, hint) {
      return h("label", { key: key, className: "hae-field" },
        h("span", { className: "hae-field-label" }, s[key] || key,
          reloadKeys.indexOf(key) >= 0 ? h("span", { className: "hae-muted" }, " · ", inline(s.reloadNote, "rn" + key)) : null),
        control, hint ? h("span", { className: "hae-muted" }, hint) : null);
    };
    const checkbox = function (key) {
      return h("input", { type: "checkbox", checked: !!form[key], disabled: disabled,
                          onChange: function (event) { set(key, event.target.checked); } });
    };
    const number = function (key) {
      const range = NUMBERS[key], bad = wrong.indexOf(key) >= 0;
      const control = h("input", { type: "number", className: "hae-input", min: range.length > 2 ? range[2] : range[0], max: range[1], step: 1,
                                   value: form[key] === undefined || form[key] === null ? "" : String(form[key]), disabled: disabled,
                                   "aria-invalid": bad ? "true" : "false",
                                   onChange: function (event) { set(key, event.target.value); } });
      const message = range.length > 2 ? s.rangeAutopilot : fill(s.range, { min: fmt(range[0], 0, t.lang), max: fmt(range[1], 0, t.lang) });
      return [h("span", { key: "c" }, control), bad ? h("span", { key: "e", className: "hae-error", role: "alert" }, message) : null];
    };
    const select = function (key, options, labels) {
      return h("select", { className: "hae-select hae-select-field", value: form[key], disabled: disabled,
                           onChange: function (event) { set(key, event.target.value); } },
        options.map(function (value) { return h("option", { key: value, value: value }, (labels || s)[value] || value); }));
    };
    return h("div", { className: "hae-stack" },
      pendingReload.length ? h("p", { className: "hae-notice" }, inline(count(s.reloadPending, pendingReload.length, t.lang, {
        keys: pendingReload.map(function (key) { return s.short[key] || key; }).join(", ") }), "rp")) : null,
      h(Section, { title: s.title },
        h("div", { className: "hae-stack" },
          data.can_decide ? h("div", { className: "hae-actions hae-actions-left" },
            h(Button, { primary: true, onClick: recommended, disabled: disabled }, s.recommended),
            h("span", { className: "hae-muted" }, s.recommendedHint)) : null,
          field("lessons", select("lessons", ["auto", "suggest", "off"], s.modes), s.lessonsHint),
          field("mode", select("mode", ["auto", "assist", "manual"], s.autoModes), s.autoHint),
          field("reflector", checkbox("reflector")),
          field("reflector_daily_limit", number("reflector_daily_limit")),
          field("autopilot_minutes", number("autopilot_minutes")),
          field("language", select("language", ["auto", "en", "pt"])),
          field("feedback_signals", checkbox("feedback_signals")),
          field("slow_turn_seconds", number("slow_turn_seconds")),
          field("agent_tools", checkbox("agent_tools")),
          field("tool_hints", checkbox("tool_hints")),
          field("runtime_guard", checkbox("runtime_guard"), form.runtime_guard === false ? s.guardWarn : null),
          field("fleet_managed", checkbox("fleet_managed"), s.fleetHint))),
      h(Section, { title: s.owner },
        h("div", { className: "hae-stack" },
          h("p", { className: "hae-muted" }, s.ownerHint),
          channels.length ? channels.map(function (channel) {
            const platform = suggestedFor("platform", channel.origin);
            return h("div", { key: channel.origin, className: "hae-channel" },
              h("label", { className: "hae-field-inline" },
                h("input", { type: "checkbox", disabled: disabled, checked: (form.owner_platforms || []).indexOf(channel.origin) >= 0,
                             onChange: function () { toggleList("owner_platforms", channel.origin); } }),
                h("code", { className: "hae-code" }, channel.origin), " · " + count(t.sessions, channel.sessions, t.lang) + " · " + s.platform,
                platform ? h(Pill, { tone: "good" }, s.suggested) : null),
              platform ? h("div", { className: "hae-muted hae-indent" }, s.reason[platform.reason] || "") : null,
              h("div", { className: "hae-indent hae-stack-tight" }, (channel.senders || []).filter(function (item) { return item.id; }).map(function (sender) {
                const hint = suggestedFor("sender", sender.id);
                return h("label", { key: sender.id, className: "hae-field-inline" },
                  h("input", { type: "checkbox", disabled: disabled, checked: (form.owner_sender_ids || []).indexOf(sender.id) >= 0,
                               onChange: function () { toggleList("owner_sender_ids", sender.id); } }),
                  h("code", { className: "hae-code" }, sender.id), " · " + count(t.sessions, sender.sessions, t.lang) + " · " + s.sender,
                  hint ? h(Pill, { tone: "good" }, s.suggested) : null);
              })));
          }) : h("p", { className: "hae-muted" }, s.noChannels))),
      data.can_decide ? h("div", { className: "hae-actions hae-actions-left" },
        h(Button, { primary: true, onClick: function () { save(form); }, disabled: disabled || !!wrong.length || !Object.keys(changed(form)).length },
          act.working ? s.saving : s.save)) : null);
  }

  // A setting's value as the Settings tab names it: "on", "Lessons: automatic", "Auto", "Português (Brasil)", a
  // count when the values are hidden (decisions.masked writes "<n> item(s)").
  function settingValue(t, key, value, hidden) {
    const s = t.s;
    if (hidden) {
      const found = /^(\d+)/.exec(String(value));
      return found ? count(t.items, Number(found[1]), t.lang) : "…";
    }
    if (value === null || value === undefined) return t.none;
    if (typeof value === "boolean") { const words = s.onOff[key] || s.onOff.default; return value ? words[0] : words[1]; }
    if (key === "lessons" && t.lessonModes[value]) return t.lessonModes[value];
    if (key === "mode" && t.autoNames[value]) return t.autoNames[value];
    if (key === "language" && s.langNames[value]) return s.langNames[value];
    if (typeof value === "number") return fmt(value, 0, t.lang);
    if (Array.isArray(value)) return value.length ? value.join(", ") : s.emptyList;
    if (typeof value === "object") return JSON.stringify(value);
    return String(value);
  }

  function History(props) {
    const t = props.t, rows = props.data.history || [], act = props.actions;
    if (!rows.length) return h("p", { className: "hae-muted" }, t.historyEmpty);
    return h("div", { className: "hae-table-wrap" }, h("table", { className: "hae-table" },
      h("thead", null, h("tr", null, [t.col.when, t.col.who, t.col.what, "", ""].map(function (label, n) { return h("th", { key: n }, label); }))),
      h("tbody", null, rows.map(function (row) {
        const after = row.after || {}, before = row.before || {};
        const applied = row.kind === "proposal" && Array.isArray(after.applied) ? after.applied : null;
        // the command of a proposal: what the plugin ran (applied), or what the owner runs (approved)
        const command = row.kind !== "proposal" ? null
          : row.command || (typeof after.command === "string" ? after.command : null) || (applied ? "hermes " + applied.join(" ") : null);
        const what = row.kind === "lesson" ? (t.action[row.action] || row.action) + " " + fill(t.lessonRef, { id: row.target })
          : row.kind === "setting" ? t.action.set + " " + (t.s.short[row.target] || row.target) + ": " +
              settingValue(t, row.target, before.value, row.values_hidden) + " → " + settingValue(t, row.target, after.value, row.values_hidden)
          : row.kind === "memory" ? t.memoryRemoved + " " + row.target
          : row.kind === "cron" ? fill(t.cronCreated, { name: row.target, schedule: after.schedule || "?" })
          : row.kind === "maintenance" && row.target === "gateway_repair"
            ? fill(t.gatewayRepaired, { unit: before.unit || "?", before: before.path || "?", after: after.path || "?",
                                        undo: (props.data.commands || {}).undo_gateway || "hermes evolution care undo-gateway" })
              + (after.ok ? "" : t.gatewayRepairFailed)
          : row.kind === "maintenance" && row.target === "plugin_sync"
            ? fill(t.pluginSynced, { profile: before.profile || "?", before: before.sha || "?", after: after.sha || "?" })
              + (after.ok ? "" : t.pluginSyncFailed)
          : row.kind === "maintenance" ? t.maintenanceRan + " hermes " + String(row.target).replace(/_/g, " ")
          : row.kind === "proposal" ? (applied ? t.action.apply : (t.action[row.action] || row.action)) + " " + fill(t.proposalRef, { id: row.target })
          : (t.action[row.action] || row.action) + " " + row.target;
        return h("tr", { key: row.id, className: row.undone ? "hae-row-muted" : "" },
          h("td", null, when(row.at, t.lang)),
          h("td", null, t.actor[row.actor] || row.actor),
          h("td", null, what,
            command ? (applied || row.undone ? h("div", null, h("code", { className: "hae-code" }, command))
                                             : h(CopyCommand, { t: t, command: command })) : null),
          h("td", { className: "hae-lesson" }, lessonText(t, row, "h")),
          h("td", null, row.undone ? t.undone : props.data.can_decide && row.undoable !== false
            ? h(Button, { disabled: !!act.working, onClick: function () {
                act.send({ kind: "undo", id: row.id }, function (result) {
                  return result && result.undone ? t.done.undo : { text: reason(t, result && result.reason), bad: true };
                });
              } }, t.undo) : null));
      }))));
  }

  // Hermes' own doctor of each profile (0.9.0), when the plugin ran it: one line each, above the findings.
  function Environment(props) {
    const t = props.t, lang = t.lang, doctor = (props.data.environment || {}).doctor;
    const number = function (value) { return typeof value === "number" && isFinite(value) ? value : 0; };
    const lines = doctor && typeof doctor === "object" ? Object.keys(doctor).sort().map(function (name) {
      const item = doctor[name] && typeof doctor[name] === "object" ? doctor[name] : {};
      let text;
      if (item.ok === false) {
        text = fill(t.doctor.failed, { why: t.doctor.errors[item.error] || (item.error ? String(item.error) : "?") });
      } else {
        const problems = Math.max(number(item.fail), number(item.issues));
        text = fill(t.doctor.line, { problems: count(t.doctor.problems, problems, lang), warnings: count(t.doctor.warnings, number(item.warn), lang),
                                     optional: count(t.doctor.optional, number(item.optional), lang) });
      }
      if (item.at !== undefined && item.at !== null && when(item.at, lang) !== "—") text += " " + fill(t.doctor.checked, { when: when(item.at, lang) });
      return h("li", { key: name }, h("code", { className: "hae-code" }, name), " · ", text);
    }) : [];
    return h("div", { className: "hae-stack" },
      lines.length ? h("ul", { className: "hae-list" }, lines) : null,
      h(Markdown, { text: props.data.environment_md || "" }));
  }

  const TABS = ["overview", "lessons", "proposals", "health", "environment", "settings", "history", "digest", "status"];

  // The panel speaks the profile's language (the plugin's `language` setting); with `auto`, the dashboard's. The
  // snapshot says which (`lang`); until it arrives, the dashboard's.
  function EvolutionPage() {
    const i18n = useI18n() || {};
    const hint = language(i18n.locale);
    const tabState = hooks.useState("overview");
    const tab = tabState[0], setTab = tabState[1];
    const daysState = hooks.useState(7);
    const days = daysState[0], setDays = daysState[1];
    const dataState = hooks.useState({ loading: true });
    const view = dataState[0], setView = dataState[1];
    const noticeState = hooks.useState(null);
    const notice = noticeState[0], setNotice = noticeState[1];
    const stickyState = hooks.useState([]);
    const stickies = stickyState[0], setStickies = stickyState[1];
    const timer = useRef(null);
    const data = view.data;
    const lang = data && TEXT[data.lang] ? data.lang : hint;
    const t = TEXT[lang];

    const load = hooks.useCallback(function () {
      setView(function (previous) { return { loading: true, data: previous.data }; });
      SDK.fetchJSON("/api/plugins/" + NAME + "/snapshot?days=" + days + "&lang=" + hint)
        .then(function (data) {
          if (!data || data.can_decide || data.decide_reason !== "other_profile") return data;
          const own = "/api/plugins/" + NAME + "/snapshot?days=" + days + "&lang=" + hint + scope({ target: data.profile });
          return SDK.fetchJSON(own).then(function (mine) {
            // the dashboard's own process answers for this profile: its verdict (and its reason) is the one that counts
            return mine && mine.profile === data.profile ? Object.assign(mine, { target: data.profile }) : data;
          }).catch(function () { return data; });
        })
        .then(function (data) { setView({ loading: false, data: data }); })
        .catch(function (error) {
          setView(function (previous) { return { loading: false, data: previous.data, error: error }; });
        });
    }, [days, hint]);

    hooks.useEffect(function () { load(); }, [load]);
    hooks.useEffect(function () { return function () { if (timer.current) clearTimeout(timer.current); }; }, []);
    // A sentence goes away by itself; a command the owner has to run stays, with Copy, until dismissed.
    const notify = function (message) {
      if (!message) return;
      const item = typeof message === "object" ? message : { text: message };
      if (item.command) {
        setStickies(function (list) {
          return list.filter(function (other) { return other.command !== item.command; })
            .concat([{ id: Date.now() + Math.random(), text: item.text, command: item.command }]);
        });
        return;
      }
      if (timer.current) clearTimeout(timer.current);
      setNotice({ text: item.text, bad: !!item.bad });
      timer.current = setTimeout(function () { setNotice(null); timer.current = null; }, item.bad ? 12000 : 6000);
    };
    const dismiss = function (id) { setStickies(function (list) { return list.filter(function (item) { return item.id !== id; }); }); };

    const actions = useActions({ data: data || {}, lang: lang, t: t, reload: load, notify: notify });
    const state = (data && data.status) || {};
    const header = h("div", { className: "hae-header" },
      h("div", null,
        h("h1", { className: "hae-title" }, t.title),
        data ? h("div", { className: "hae-subtitle" }, t.profile + " ", h("code", { className: "hae-code" }, data.profile), " · v" + data.version) : null),
      h("div", { className: "hae-actions" },
        data ? h(Pill, { tone: data.mode === "off" ? "muted" : "good" }, t.mode[data.mode] || data.mode) : null,
        data && data.automatic ? h(Pill, { tone: data.automatic.mode === "manual" ? "muted" : "good" },
          t.autoLine + ": " + (t.autoNames[data.automatic.mode] || data.automatic.mode)) : null,
        data ? h(Pill, { tone: (state.runtime_guard || {}).enabled ? "good" : "bad" },
                 (state.runtime_guard || {}).enabled ? t.guardOn : t.guardOff) : null,
        data ? h(Pill, { tone: (state.reflector || {}).enabled ? "neutral" : "muted" },
                 (state.reflector || {}).enabled ? t.reflectorOn : t.reflectorOff) : null,
        h("select", { className: "hae-select", value: days, onChange: function (event) { setDays(Number(event.target.value)); },
                      "aria-label": t.days },
          [7, 14, 30].map(function (value) { return h("option", { key: value, value: value }, value + " " + t.days); })),
        h(Button, { onClick: load, disabled: view.loading }, t.retry)));

    let body;
    const common = { t: t, data: data, actions: actions, notify: notify };
    if (view.error && !data) body = null;
    else if (!data) body = h("p", { className: "hae-muted" }, t.loading);
    else if (tab === "overview") body = h(Overview, common);
    else if (tab === "lessons") body = h(Lessons, common);
    else if (tab === "proposals") body = h(Proposals, common);
    else if (tab === "settings") body = h(Settings, common);
    else if (tab === "history") body = h(History, common);
    else if (tab === "health") body = h(Health, common);
    else if (tab === "environment") body = h(Environment, common);
    else if (tab === "digest") body = h(Markdown, { text: data.report_md });
    else body = h(Markdown, { text: data.status_md });
    const loadError = !view.error ? null
      : /^503\b/.test(String(view.error && view.error.message || view.error)) ? failure(view.error, t)   // busy: try again
      : t.failed + " " + failure(view.error, t);

    // Tabs as WAI-ARIA describes them: arrows, Home and End move between them, and the panel names its tab.
    const tabKeys = function (event) {
      const at = TABS.indexOf(tab);
      const next = event.key === "ArrowRight" ? TABS[(at + 1) % TABS.length]
        : event.key === "ArrowLeft" ? TABS[(at - 1 + TABS.length) % TABS.length]
        : event.key === "Home" ? TABS[0] : event.key === "End" ? TABS[TABS.length - 1] : null;
      if (!next) return;
      event.preventDefault();
      setTab(next);
      const node = document.getElementById("hae-tab-" + next);
      if (node) node.focus();
    };

    return h("div", { className: "hae-page" + (view.loading && data ? " hae-loading" : "") },
      header,
      data && !data.can_decide ? h("div", { className: "hae-notice hae-stack-tight" },
        h("span", null, data.decide_reason === "shared_token" ? t.readOnlyShared
          : data.decide_reason === "other_profile" ? t.readOnlyOther : t.readOnly),
        data.commands && data.commands.menu ? h("span", null, t.menuHint) : null,
        data.commands && data.commands.menu ? h(CopyCommand, { t: t, command: data.commands.menu }) : null) : null,
      loadError ? h("p", { className: "hae-error", role: "alert" }, loadError) : null,
      stickies.map(function (item) {
        return h("div", { key: item.id, className: "hae-notice hae-notice-good hae-stack-tight", role: "status" },
          h("span", null, item.text),
          h(CopyCommand, { t: t, command: item.command }),
          h("div", { className: "hae-actions hae-actions-left" }, h(Button, { onClick: function () { dismiss(item.id); } }, t.close)));
      }),
      notice ? h("p", { className: "hae-notice" + (notice.bad ? " hae-notice-bad" : " hae-notice-good"), role: notice.bad ? "alert" : "status" }, notice.text) : null,
      h("div", { className: "hae-tabs", role: "tablist", "aria-label": t.title, onKeyDown: tabKeys }, TABS.map(function (name) {
        return h("button", { key: name, type: "button", role: "tab", id: "hae-tab-" + name, "aria-selected": tab === name,
                             "aria-controls": "hae-panel-" + name, tabIndex: tab === name ? 0 : -1,
                             className: "hae-tab" + (tab === name ? " hae-tab-active" : ""), onClick: function () { setTab(name); } },
                 t.tabs[name]);
      })),
      h("div", { role: "tabpanel", id: "hae-panel-" + tab, "aria-labelledby": "hae-tab-" + tab, tabIndex: 0 }, body));
  }

  window.__HERMES_PLUGINS__.register(NAME, EvolutionPage);
})();
