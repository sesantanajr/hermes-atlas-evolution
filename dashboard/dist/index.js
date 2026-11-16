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

  const TEXT = {
    en: {
      lang: "en", sessions: "sessions", title: "Atlas Evolution", profile: "profile", loading: "Reading what this profile learned…",
      busy: "The Evolution store is busy right now. Try again in a moment.", failed: "The panel could not load:",
      retry: "Reload", days: "days",
      health: { none: "No reading yet: the learning pass reads Hermes' health and the system's within 6 hours.",
                gateway: "Gateway", running: "running", stopped: "not running", rss: "memory", disk: "Free disk", of: "of",
                full: "full in {days} days", memory: "Free memory", swap: "Swap in use", db: "Session database",
                tools: "Old tools", os: "System updates", security: "{n} security", reboot: "Reboot waiting",
                yes: "yes", no: "no", attention: "Needs attention", nothing: "Nothing needs attention.",
                how: "Checks and repairs", hint: "Hermes' checks and repairs (hermes doctor, pm doctor, pm gc) and the system's update run from the menu in your terminal, with a backup first, never by the agent:" },
      tabs: { overview: "Overview", lessons: "Lessons", proposals: "Proposals", health: "Health", environment: "Environment", settings: "Settings", history: "History",
              digest: "Digest", status: "Status" },
      mode: { auto: "lessons: auto", suggest: "lessons: suggest", off: "lessons: off" },
      guardOn: "guard on", guardOff: "guard off", reflectorOn: "reflector on", reflectorOff: "reflector off",
      kpi: { active: "Active lessons", waiting: "Waiting for you", calls: "Tool calls", errors: "errors", turns: "Turns",
             failed: "failed", vetoed: "Guard vetoes (7 d)", lastPass: "Last pass", passFailed: "failed: see Status",
             proposals: "Open proposals" },
      never: "never", none: "—",
      readOnly: "Read-only: this dashboard has no login, so any program on this machine, the agent included, could act here as you. Turn on the dashboard login (a public dashboard.public_url turns it on) to decide from this panel.",
      readOnlyShared: "Read-only: this session was opened with the dashboard's shared session token (HERMES_DASHBOARD_SESSION_TOKEN), not with a login.",
      readOnlyOther: "Read-only here: Hermes serves this profile's panel from the profile's own process, which cannot see your login, and the dashboard's own profile could not decide for it. Enable Atlas Evolution in the dashboard's own profile too, and log in, to decide here.",
      menuHint: "Meanwhile, decide everything from the menu in your terminal:",
      waitingTitle: "Waiting for your decision",
      waitingEmpty: "Nothing waits for you. Lessons from fixed templates apply by themselves; anything written by a model or by the agent shows up here.",
      approveAll: "Approve all ({n})", approve: "Approve", reject: "Reject", retire: "Retire", reactivate: "Reactivate",
      confirmAllTitle: "Approve these {n} lessons?", confirmOneTitle: "Approve this lesson?",
      confirmAllHint: "From the next session on, the agent reads them at the start of every session. Each one is measured, and in auto mode retired if it does not help.",
      agentSingle: "proposed by the agent: approve it on its own",
      cancel: "Cancel", confirm: "Approve all",
      done: { approve: "Approved.", reject: "Rejected.", retire: "Retired.", reactivate: "Reactivated.", all: "Approved {n}.",
              undo: "Undone.", settings: "Saved. It applies within a minute, without restarting.", proposal: "Recorded.",
              applied: "Applied. You can undo it in the History tab.", dismissed: "Dismissed: it stays closed while nothing changes.",
              appliedN: "Applied {n}. You can undo each one in the History tab.", already: "It was already so in Hermes: nothing to undo.",
              notApplied: "Not applied ({n}):",
              changed: "It changed meanwhile: look at it again." },
      skippedChanged: "{n} skipped: they changed since you saw them.",
      lessonsEmpty: "No lessons yet. A tool failure that comes back (3 times, in 2 sessions or on 2 days) becomes one.",
      proposalsEmpty: "No open proposals.",
      col: { status: "status", origin: "origin", tool: "tool", lesson: "lesson", effect: "measured effect", risk: "risk",
             subject: "subject", command: "command to run", action: "", when: "when", who: "where", what: "decision" },
      runAfter: "Approved. Hermes changes this with the command below; run it in your terminal:",
      verdict: { effective: "helps", ineffective: "does not help", inconclusive: "inconclusive", withdrawn: "withdrawn from the queue" },
      status: { active: "active", candidate: "waiting", quarantined: "waiting (written)", retired: "retired" },
      origin: { derived: "template", operator: "you", reflector: "reflector", agent: "agent" },
      actor: { terminal: "terminal", dashboard: "panel", automatic: "automatic" },
      action: { approve: "approved", reject: "rejected", retire: "retired", reactivate: "reactivated", set: "changed", apply: "applied" },
      memoryRemoved: "took a repeated entry out of",
      undo: "Undo", undone: "undone",
      historyEmpty: "No decision yet.",
      applyAll: "Apply the {n} change(s)", applyAllHint: "Reversible changes to Hermes, each one recorded with its undo.",
      applyNow: "Apply now", dismiss: "Dismiss", seen: "What was seen", advice: "What to do", undoHow: "To undo",
      maintenanceRan: "ran Hermes' own maintenance (nothing to undo):",
      category: { change: "the plugin applies it", lesson: "lesson", manual: "you apply it", advice: "observation" },
      autoLine: "Mode", autoNames: { auto: "Auto", assist: "Assisted", manual: "Manual" },
      copy: "Copy", copied: "Copied",
      share: "Share what this profile learned with another profile:",
      s: {
        title: "Settings", save: "Save", saving: "Saving…", recommended: "Apply the recommended setup",
        recommendedHint: "Lessons on auto, reflector on, Mode auto, and the channels below that are yours by construction.",
        reloadNote: "Applies the next time the plugin loads (the gateway reloads plugins when one is enabled or updated).",
        reloadPending: "Waiting for the next load of the plugin: {keys}.",
        lessons: "Lessons", lessonsHint: "auto: lessons from fixed templates apply once your own turns hit the failure; suggest: every lesson waits for you; off: measure only.",
        reflector: "Reflector (an AI model drafts lessons for failures with no template; drafts wait for you)",
        reflector_daily_limit: "Reflector calls per day (1 to 50)", autopilot_minutes: "Learning pass every (minutes: 15 to 1440, or 0 = only by hand)",
        language: "Language", feedback_signals: "Count your corrections and thanks (numbers only)",
        slow_turn_seconds: "A turn is slow after (seconds)", tool_hints: "Add the lesson to the failing tool result",
        agent_tools: "Let the agent read and propose lessons (proposals still wait for you)",
        runtime_guard: "Runtime guard (keeps the agent from taking Hermes down)",
        mode: "Mode (what the plugin does by itself)",
        autoModes: { auto: "Auto (default): thinks and acts for you in every profile, only what is safe and can be undone",
                     assist: "Assisted: only identical memory copies by itself; the rest waits for you",
                     manual: "Manual: measures and proposes; nothing changes by itself" },
        autoHint: "Each action is recorded in the History with its undo; an automatic change you undo is never repeated.",
        guardWarn: "Turning the guard off lets the agent restart or delete what Hermes runs on.",
        owner: "Who is you in chats", ownerHint: "Turns from these count as yours: you get every lesson there, and your failures there teach. List only what nobody else uses.",
        platform: "every chat of this channel is you", sender: "this sender is you", suggested: "suggested",
        reason: { own_interface: "only clients with the API key write here, such as your own interface", hosted_owner: "Hermes marks this sender as the owner" },
        noChannels: "No chat channel in 30 days: your CLI, TUI and Desktop always count as you.",
        auto: "auto", en: "English", pt: "Português",
        modes: { auto: "auto (recommended): templates apply by themselves", suggest: "suggest: everything waits for you", off: "off: measure only" }
      }
    },
    pt: {
      lang: "pt", sessions: "sessões", title: "Atlas Evolution", profile: "perfil", loading: "Lendo o que este perfil aprendeu…",
      busy: "O banco do Evolution está ocupado agora. Tente de novo em instantes.", failed: "O painel não carregou:",
      retry: "Recarregar", days: "dias",
      health: { none: "Ainda sem leitura: a passada de aprendizado lê a saúde do Hermes e a do sistema em até 6 horas.",
                gateway: "Gateway", running: "rodando", stopped: "parado", rss: "memória", disk: "Disco livre", of: "de",
                full: "enche em {days} dias", memory: "Memória livre", swap: "Swap em uso", db: "Banco de sessões",
                tools: "Ferramentas antigas", os: "Atualizações do sistema", security: "{n} de segurança", reboot: "Reinício pendente",
                yes: "sim", no: "não", attention: "Pede atenção", nothing: "Nada pede atenção.",
                how: "Checagens e reparos", hint: "As checagens e os reparos do Hermes (hermes doctor, pm doctor, pm gc) e a atualização do sistema rodam pelo menu no seu terminal, com backup antes, nunca pelo agente:" },
      tabs: { overview: "Visão geral", lessons: "Lições", proposals: "Propostas", health: "Saúde", environment: "Ambiente", settings: "Configurações", history: "Histórico",
              digest: "Resumo", status: "Status" },
      mode: { auto: "lições: auto", suggest: "lições: suggest", off: "lições: off" },
      guardOn: "guarda ligada", guardOff: "guarda desligada", reflectorOn: "refletor ligado", reflectorOff: "refletor desligado",
      kpi: { active: "Lições ativas", waiting: "Esperando você", calls: "Chamadas de ferramentas", errors: "erros", turns: "Turnos",
             failed: "falharam", vetoed: "Vetos da guarda (7 d)", lastPass: "Última passada", passFailed: "falhou: veja o Status",
             proposals: "Propostas abertas" },
      never: "nunca", none: "—",
      readOnly: "Só leitura: este dashboard não pede login, então qualquer programa desta máquina, inclusive o agente, poderia agir aqui como você. Ative o login do dashboard (um dashboard.public_url público o liga) para decidir por este painel.",
      readOnlyShared: "Só leitura: esta sessão foi aberta com o token de sessão compartilhado do dashboard (HERMES_DASHBOARD_SESSION_TOKEN), não com um login.",
      readOnlyOther: "Só leitura aqui: o Hermes serve o painel deste perfil pelo processo do próprio perfil, que não vê o seu login, e o perfil do próprio dashboard não pôde decidir por ele. Ative o Atlas Evolution também no perfil do dashboard, e entre com login, para decidir aqui.",
      menuHint: "Enquanto isso, decida tudo pelo menu no seu terminal:",
      waitingTitle: "Esperando a sua decisão",
      waitingEmpty: "Nada espera por você. Lições de modelos fixos valem sozinhas; o que um modelo ou o agente escrever aparece aqui.",
      approveAll: "Aprovar todas ({n})", approve: "Aprovar", reject: "Rejeitar", retire: "Aposentar", reactivate: "Reativar",
      confirmAllTitle: "Aprovar estas {n} lições?", confirmOneTitle: "Aprovar esta lição?",
      confirmAllHint: "A partir da próxima sessão o agente lê essas lições no começo de cada sessão. Cada uma é medida e, no modo auto, aposentada se não ajudar.",
      agentSingle: "proposta pelo agente: aprove sozinha",
      cancel: "Cancelar", confirm: "Aprovar todas",
      done: { approve: "Aprovada.", reject: "Rejeitada.", retire: "Aposentada.", reactivate: "Reativada.", all: "{n} aprovadas.",
              undo: "Desfeito.", settings: "Salvo. Vale em até um minuto, sem reiniciar.", proposal: "Registrado.",
              applied: "Aplicada. Dá para desfazer na aba Histórico.", dismissed: "Dispensada: fica fechada enquanto nada mudar.",
              appliedN: "{n} aplicada(s). Dá para desfazer cada uma na aba Histórico.", already: "Já estava assim no Hermes: nada a desfazer.",
              notApplied: "Não aplicadas ({n}):",
              changed: "Mudou nesse meio-tempo: veja de novo." },
      skippedChanged: "{n} puladas: mudaram desde que você as viu.",
      lessonsEmpty: "Ainda não há lições. Uma falha de ferramenta que volta (3 vezes, em 2 sessões ou em 2 dias) vira uma.",
      proposalsEmpty: "Nenhuma proposta aberta.",
      col: { status: "situação", origin: "origem", tool: "ferramenta", lesson: "lição", effect: "efeito medido", risk: "risco",
             subject: "assunto", command: "comando para rodar", action: "", when: "quando", who: "onde", what: "decisão" },
      runAfter: "Aprovada. Quem muda isso é o Hermes, com o comando abaixo; rode no seu terminal:",
      verdict: { effective: "ajuda", ineffective: "não ajuda", inconclusive: "inconclusivo", withdrawn: "retirada da fila" },
      status: { active: "ativa", candidate: "esperando", quarantined: "esperando (escrita)", retired: "aposentada" },
      origin: { derived: "modelo fixo", operator: "você", reflector: "refletor", agent: "agente" },
      actor: { terminal: "terminal", dashboard: "painel", automatic: "automático" },
      action: { approve: "aprovou", reject: "rejeitou", retire: "aposentou", reactivate: "reativou", set: "mudou", apply: "aplicou" },
      memoryRemoved: "tirou uma entrada repetida de",
      undo: "Desfazer", undone: "desfeita",
      historyEmpty: "Nenhuma decisão ainda.",
      applyAll: "Aplicar as {n} mudança(s)", applyAllHint: "Mudanças reversíveis no Hermes, cada uma registrada com o desfazer.",
      applyNow: "Aplicar agora", dismiss: "Dispensar", seen: "O que foi visto", advice: "O que fazer", undoHow: "Para desfazer",
      maintenanceRan: "rodou a manutenção do próprio Hermes (nada a desfazer):",
      category: { change: "o plugin aplica", lesson: "lição", manual: "você aplica", advice: "observação" },
      autoLine: "Modo", autoNames: { auto: "Auto", assist: "Assistido", manual: "Manual" },
      copy: "Copiar", copied: "Copiado",
      share: "Leve o que este perfil aprendeu para outro perfil:",
      s: {
        title: "Configurações", save: "Salvar", saving: "Salvando…", recommended: "Aplicar a configuração recomendada",
        recommendedHint: "Lições em auto, refletor ligado, Modo Auto e os canais abaixo que são seus por construção.",
        reloadNote: "Vale na próxima carga do plugin (o gateway recarrega os plugins quando um é ligado ou atualizado).",
        reloadPending: "Esperando a próxima carga do plugin: {keys}.",
        lessons: "Lições", lessonsHint: "auto: lições de modelos fixos valem quando os seus turnos encontram a falha; suggest: toda lição espera você; off: só mede.",
        reflector: "Refletor (um modelo de IA rascunha lições para falhas sem modelo fixo; os rascunhos esperam você)",
        reflector_daily_limit: "Chamadas do refletor por dia (1 a 50)", autopilot_minutes: "Passada de aprendizado a cada (minutos: 15 a 1440, ou 0 = só à mão)",
        language: "Idioma", feedback_signals: "Contar as suas correções e agradecimentos (só números)",
        slow_turn_seconds: "Um turno é lento depois de (segundos)", tool_hints: "Juntar a lição ao resultado da ferramenta que falhou",
        agent_tools: "Deixar o agente ler e propor lições (as propostas continuam esperando você)",
        runtime_guard: "Guarda de runtime (impede o agente de derrubar o Hermes)",
        mode: "Modo (o que o plugin faz sozinho)",
        autoModes: { auto: "Auto (padrão): pensa e age por você em todos os perfis, só o que é seguro e pode ser desfeito",
                     assist: "Assistido: só as cópias idênticas da memória sozinho; o resto espera você",
                     manual: "Manual: mede e propõe; nada muda sozinho" },
        autoHint: "Cada ação fica no Histórico com o desfazer; uma mudança automática que você desfaz nunca se repete.",
        guardWarn: "Desligar a guarda deixa o agente reiniciar ou apagar aquilo em que o Hermes roda.",
        owner: "Quem é você nos chats", ownerHint: "Os turnos destes contam como seus: ali você recebe todas as lições, e as suas falhas ensinam. Marque só o que ninguém mais usa.",
        platform: "todo chat deste canal é você", sender: "este remetente é você", suggested: "sugerido",
        reason: { own_interface: "só clientes com a chave da API escrevem aqui, como a sua própria interface", hosted_owner: "o Hermes marca este remetente como o dono" },
        noChannels: "Nenhum canal de chat em 30 dias: a sua CLI, TUI e Desktop sempre contam como você.",
        auto: "automático", en: "English", pt: "Português",
        modes: { auto: "auto (recomendado): modelos fixos valem sozinhos", suggest: "suggest: tudo espera você", off: "off: só medir" }
      }
    }
  };

  function language(locale) {
    return typeof locale === "string" && locale.toLowerCase().indexOf("pt") === 0 ? "pt" : "en";
  }

  function fill(template, values) {
    return String(template).replace(/\{(\w+)\}/g, function (_, key) { return values[key] === undefined ? "" : values[key]; });
  }

  function fmt(value, digits) {
    if (value === null || value === undefined || Number.isNaN(value)) return "—";
    if (typeof value !== "number") return String(value);
    return digits ? value.toFixed(digits) : value.toLocaleString();
  }

  function when(stamp, lang) {
    if (typeof stamp !== "number") return "—";
    try { return new Date(stamp * 1000).toLocaleString(lang === "pt" ? "pt-BR" : "en-US"); } catch (e) { return "—"; }
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
      if (/^\s*[-*]\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
          items.push(lines[i].replace(/^\s*[-*]\s+/, ""));
          i++;
        }
        out.push(h("ul", { key: key++, className: "hae-list" }, items.map(function (item, n) {
          return h("li", { key: n }, inline(item, "li" + key + "-" + n));
        })));
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
  function CopyCommand(props) {
    const state = hooks.useState(false);
    const copied = state[0], setCopied = state[1];
    const copy = function () {
      const done = function () { setCopied(true); setTimeout(function () { setCopied(false); }, 1500); };
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(props.command).then(done, function () {});
    };
    return h("div", { className: "hae-command" },
      h("code", { className: "hae-code" }, props.command),
      h("button", { type: "button", className: "hae-copy", onClick: copy, "aria-label": props.t.copy }, copied ? props.t.copied : props.t.copy));
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

  function Kpi(props) {
    return h("div", { className: "hae-kpi" + (props.alert ? " hae-kpi-alert" : "") },
      h("div", { className: "hae-kpi-label" }, props.label),
      h("div", { className: "hae-kpi-value" }, props.value),
      props.detail ? h("div", { className: "hae-kpi-detail" }, props.detail) : null);
  }

  function effect(t, row) {
    if (!row.verdict) return t.none;
    const label = t.verdict[row.verdict] || row.verdict;
    if (row.rate_before === null || row.rate_before === undefined) return label;
    return label + " (" + fmt(row.rate_before, 3) + " → " + fmt(row.rate_after, 3) + ")";
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

  // fetchJSON throws "<status>: <body>"; the body of the decide route carries a translated `message`
  function failure(error) {
    let text = String(error && error.message || error);
    const body = text.indexOf("{") >= 0 ? text.slice(text.indexOf("{")) : "";
    try { const parsed = JSON.parse(body); text = parsed.message || parsed.error || text; } catch (e) { /* as is */ }
    return text;
  }

  function useActions(props) {
    const state = hooks.useState(null);
    const working = state[0], setWorking = state[1];
    const send = function (body, done) {
      if (!props.data.can_decide) return;
      setWorking(JSON.stringify(body).slice(0, 60));
      SDK.fetchJSON("/api/plugins/" + NAME + "/decide?lang=" + props.lang + scope(props.data), {
        method: "POST", headers: { "Content-Type": "application/json", "X-Atlas-Evolution": "1" }, body: JSON.stringify(body)
      }).then(function (result) {
        setWorking(null);
        props.notify(done ? done(result) : null, false, result);
        props.reload();
      }).catch(function (error) {
        setWorking(null);
        props.notify(failure(error), true);
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
          props.notify(done(results), false);
          props.reload();
          return;
        }
        SDK.fetchJSON("/api/plugins/" + NAME + "/decide?lang=" + props.lang + scope(props.data), {
          method: "POST", headers: { "Content-Type": "application/json", "X-Atlas-Evolution": "1" }, body: JSON.stringify(bodies[index])
        }).then(function (result) { results.push(result); next(index + 1); })
          .catch(function (error) { setWorking(null); props.notify(failure(error), true); props.reload(); });
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
          return result && result.decided === false ? t.skippedChanged.replace("{n}", "1") : t.done[action];
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

  function ConfirmAll(props) {
    const t = props.t;
    return h("div", { className: "hae-modal-backdrop", role: "dialog", "aria-modal": "true" },
      h("div", { className: "hae-modal" },
        h("h3", { className: "hae-md-h" }, props.items.length === 1 ? t.confirmOneTitle : fill(t.confirmAllTitle, { n: props.items.length })),
        h("p", { className: "hae-muted" }, t.confirmAllHint),
        h("ol", { className: "hae-list hae-confirm-list" }, props.items.map(function (item) {
          return h("li", { key: item.id }, h("span", { className: "hae-muted" }, "#" + item.id + " · " + item.tool + " · "),
            inline(item.lesson, "ca" + item.id));
        })),
        h("div", { className: "hae-actions" },
          h(Button, { onClick: props.onCancel }, t.cancel),
          h(Button, { primary: true, onClick: props.onConfirm }, t.confirm))));
  }

  // ── the tabs
  function gb(value) {
    return typeof value === "number" ? (value / 1073741824).toFixed(1) + " GB" : "—";
  }

  // Hermes' own health and the operating system's, as the last learning pass read them: read-only here; the menu in
  // the terminal runs Hermes' checks and repairs and the system's update.
  function Health(props) {
    const t = props.t, h2 = t.health, status = props.data.status || {};
    const care = status.care || null, system = status.system || null;
    if (!care && !system) return h("p", { className: "hae-muted" }, h2.none);
    const gate = (care && care.gateway) || {}, db = (care && care.database) || {}, mem = (care && care.memory) || {};
    const kpis = [];
    if (care) {
      kpis.push(h(Kpi, { key: "g", label: h2.gateway, value: gate.running ? h2.running : h2.stopped, alert: !gate.running || gate.stale,
                         detail: (gate.version ? "v" + gate.version + " · " : "") + h2.rss + " " + gb(gate.rss) }));
      kpis.push(h(Kpi, { key: "d", label: h2.disk, value: gb(care.free), alert: (care.findings || []).some(function (c) { return /^disk_/.test(c); }),
                         detail: h2.of + " " + gb(care.total) + (care.days_to_full ? " · " + fill(h2.full, { days: care.days_to_full }) : "") }));
      kpis.push(h(Kpi, { key: "m", label: h2.memory, value: gb(mem.available), alert: (care.findings || []).indexOf("memory_low") >= 0,
                         detail: h2.of + " " + gb(mem.total) }));
      kpis.push(h(Kpi, { key: "s", label: h2.swap, value: gb(mem.swap_used), alert: (care.findings || []).indexOf("swap_full") >= 0,
                         detail: h2.of + " " + gb(mem.swap_total) }));
      kpis.push(h(Kpi, { key: "b", label: h2.db, value: db.journal || "—", alert: (care.findings || []).some(function (c) { return /^state_db/.test(c); }),
                         detail: "WAL " + gb(db.wal) + (db.quick_check ? " · " + db.quick_check : "") }));
      kpis.push(h(Kpi, { key: "t", label: h2.tools, value: gb(care.reclaimable), alert: (care.findings || []).indexOf("unused_tools") >= 0,
                         detail: (care.unused_tools || []).map(function (item) { return item.folder; }).slice(0, 3).join(", ") || "—" }));
    }
    if (system && system.supported) {
      kpis.push(h(Kpi, { key: "o", label: h2.os, value: String(system.upgradable || 0), alert: !!system.security,
                         detail: (system.os || {}).name + " · " + fill(h2.security, { n: system.security || 0 }) }));
      kpis.push(h(Kpi, { key: "r", label: h2.reboot, value: system.reboot_required ? h2.yes : h2.no, alert: !!system.reboot_required,
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

  function Overview(props) {
    const t = props.t, data = props.data, state = data.status || {}, digest = data.report || {};
    const lessons = state.lessons || {};
    const guard = state.runtime_guard || {};
    const tools = digest.tools || {};
    const turns = (digest.turns || {}).interactive || {};
    const pending = data.pending || [];
    const bulk = pending.filter(function (item) { return item.bulk; });
    const confirmState = hooks.useState(false);
    const confirming = confirmState[0], setConfirming = confirmState[1];
    const act = props.actions;
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
        return fill(t.done.all, { n: (result.approved || []).length }) + (skipped ? " " + fill(t.skippedChanged, { n: skipped }) : "");
      });
    };
    return h("div", { className: "hae-stack" },
      h("div", { className: "hae-kpis" },
        h(Kpi, { label: t.kpi.active, value: fmt(lessons.active || 0) }),
        h(Kpi, { label: t.kpi.waiting, value: fmt(pending.length + (state.proposals_open || 0)), alert: pending.length + (state.proposals_open || 0) > 0 }),
        h(Kpi, { label: t.kpi.calls + " (" + data.days + " d)", value: fmt(tools.calls || 0),
                 detail: tools.error_pct === null || tools.error_pct === undefined ? null : fmt(tools.error_pct, 1) + "% " + t.kpi.errors }),
        h(Kpi, { label: t.kpi.turns + " (" + data.days + " d)", value: fmt(turns.turns || 0),
                 detail: turns.failed_pct === null || turns.failed_pct === undefined ? null : fmt(turns.failed_pct, 1) + "% " + t.kpi.failed }),
        h(Kpi, { label: t.kpi.vetoed, value: fmt(guard.vetoed_7d || 0) }),
        h(Kpi, { label: t.kpi.lastPass, alert: passFailed,
                 value: passAt ? String(passAt).replace("T", " ").replace(/:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)?$/, "") : t.never,
                 detail: passFailed ? t.kpi.passFailed : passAt ? "UTC" : null })),
      h(Section, { title: t.waitingTitle },
        pending.length
          ? h("div", { className: "hae-stack" },
              data.can_decide && bulk.length
                ? h("div", { className: "hae-actions" }, h(Button, { primary: true, disabled: !!act.working,
                    onClick: function () { setConfirming(true); } }, fill(t.approveAll, { n: bulk.length })))
                : null,
              pending.map(function (row) {
                return h("div", { key: row.id, className: "hae-waiting" },
                  h("div", { className: "hae-waiting-meta" }, h(Pill, { tone: "warn" }, "#" + row.id),
                    h(Pill, null, t.origin[row.origin] || row.origin), h("code", { className: "hae-code" }, row.tool),
                    row.bulk ? null : h("span", { className: "hae-muted" }, t.agentSingle)),
                  h("div", { className: "hae-lesson" }, inline(String(row.lesson || ""), "w" + row.id)),
                  h(LessonActions, { t: t, row: row, data: data, actions: act }));
              }),
              data.can_decide ? null : h(CopyCommand, { t: t, command: data.commands.review }))
          : h("p", { className: "hae-muted" }, t.waitingEmpty)),
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
          h("td", { className: "hae-lesson" }, inline(String(row.lesson || ""), "l" + row.id)),
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
      if (result && Array.isArray(result.applied)) return fill(t.done.appliedN, { n: result.applied.length });
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
          (result.skipped || []).forEach(function (item) {
            reasons.push("#" + item.id + " " + (item.reason === "already" ? t.done.already : item.reason === "not_open" ? t.done.changed : String(item.reason || "")));
          });
        });
        return fill(t.done.appliedN, { n: done }) + (reasons.length ? " " + fill(t.done.notApplied, { n: reasons.length }) + " " + reasons.join("; ") : "");
      });
    };
    const order = ["change", "lesson", "manual", "advice"];
    const sorted = rows.slice().sort(function (a, b) {
      return order.indexOf((a.explain || {}).category) - order.indexOf((b.explain || {}).category);
    });
    return h("div", { className: "hae-stack" },
      props.data.can_decide && changes.length ? h("div", { className: "hae-actions hae-actions-left" },
        h(Button, { primary: true, onClick: applyAll, disabled: !!act.working }, fill(t.applyAll, { n: changes.length })),
        h("span", { className: "hae-muted" }, t.applyAllHint)) : null,
      sorted.map(function (row) {
        const told = row.explain || { title: row.kind, risk: row.risk, category: "manual" };
        const lesson = row.evidence && row.evidence.lesson;
        const send = function (body, done) { return function () { act.send(body, done); }; };
        const decided = function (result) { return result && result.run_this ? t.runAfter + " " + result.run_this : t.done.proposal; };
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
        return h(Section, { key: row.id, title: told.title },
          h("div", { className: "hae-stack" },
            h("div", { className: "hae-waiting-meta" },
              h(Pill, { tone: row.risk === "high" ? "bad" : row.risk === "medium" ? "warn" : "neutral" }, t.col.risk + ": " + told.risk),
              h(Pill, { tone: told.category === "change" ? "good" : "muted" }, t.category[told.category] || told.category),
              h("code", { className: "hae-code" }, row.subject), h("span", { className: "hae-muted" }, "#" + row.id)),
            told.what && told.category !== "lesson" ? h("div", null, h("strong", null, t.seen + ": "), inline(String(told.what), "w" + row.id)) : null,
            lesson ? h("div", { className: "hae-lesson" }, inline(String(lesson), "p" + row.id)) : null,
            told.advice ? h("div", null, h("strong", null, t.advice + ": "), inline(String(told.advice), "a" + row.id)) : null,
            told.does ? h("div", { className: "hae-muted" }, inline(String(told.does), "d" + row.id)) : null,
            told.undo ? h("div", { className: "hae-muted" }, t.undoHow + ": ", h("code", { className: "hae-code" }, told.undo)) : null,
            told.category === "advice" || told.category === "manual" ? h(CopyCommand, { t: t, command: row.command }) : null,
            buttons ? h("div", { className: "hae-row-actions" }, buttons) : null));
      }));
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
    const changed = function (values) {
      const out = {};
      Object.keys(values).forEach(function (key) {
        if (JSON.stringify(values[key]) !== JSON.stringify(current[key])) out[key] = values[key];
      });
      return out;
    };
    const save = function (values) {
      const diff = changed(values);
      if (!Object.keys(diff).length) return;
      act.send({ kind: "settings", values: diff }, function () { return t.done.settings; });
    };
    const toggleList = function (key, value) {
      const list = (form[key] || []).slice();
      const at = list.indexOf(value);
      if (at >= 0) list.splice(at, 1); else list.push(value);
      set(key, list);
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
          reloadKeys.indexOf(key) >= 0 ? h("span", { className: "hae-muted" }, " · " + s.reloadNote) : null),
        control, hint ? h("span", { className: "hae-muted" }, hint) : null);
    };
    const checkbox = function (key) {
      return h("input", { type: "checkbox", checked: !!form[key], disabled: disabled,
                          onChange: function (event) { set(key, event.target.checked); } });
    };
    const number = function (key, min, max) {
      return h("input", { type: "number", className: "hae-input", min: min, max: max, step: 1,
                          value: form[key] === undefined || form[key] === null ? "" : form[key], disabled: disabled,
                          onChange: function (event) {
                            const raw = event.target.value;
                            if (raw === "") return;   // an empty field changes nothing (it never means 0)
                            set(key, Math.trunc(Number(raw)));
                          } });
    };
    const select = function (key, options, labels) {
      return h("select", { className: "hae-select hae-select-field", value: form[key], disabled: disabled,
                           onChange: function (event) { set(key, event.target.value); } },
        options.map(function (value) { return h("option", { key: value, value: value }, (labels || s)[value] || value); }));
    };
    return h("div", { className: "hae-stack" },
      pendingReload.length ? h("p", { className: "hae-notice" }, fill(s.reloadPending, { keys: pendingReload.join(", ") })) : null,
      h(Section, { title: s.title },
        h("div", { className: "hae-stack" },
          data.can_decide ? h("div", { className: "hae-actions hae-actions-left" },
            h(Button, { primary: true, onClick: recommended, disabled: disabled }, s.recommended),
            h("span", { className: "hae-muted" }, s.recommendedHint)) : null,
          field("lessons", select("lessons", ["auto", "suggest", "off"], s.modes), s.lessonsHint),
          field("mode", select("mode", ["auto", "assist", "manual"], s.autoModes), s.autoHint),
          field("reflector", checkbox("reflector")),
          field("reflector_daily_limit", number("reflector_daily_limit", 1, 50)),
          field("autopilot_minutes", number("autopilot_minutes", 0, 1440)),
          field("language", select("language", ["auto", "en", "pt"])),
          field("feedback_signals", checkbox("feedback_signals")),
          field("slow_turn_seconds", number("slow_turn_seconds", 5, 3600)),
          field("agent_tools", checkbox("agent_tools")),
          field("tool_hints", checkbox("tool_hints")),
          field("runtime_guard", checkbox("runtime_guard"), form.runtime_guard === false ? s.guardWarn : null))),
      h(Section, { title: s.owner },
        h("div", { className: "hae-stack" },
          h("p", { className: "hae-muted" }, s.ownerHint),
          channels.length ? channels.map(function (channel) {
            const platform = suggestedFor("platform", channel.origin);
            return h("div", { key: channel.origin, className: "hae-channel" },
              h("label", { className: "hae-field-inline" },
                h("input", { type: "checkbox", disabled: disabled, checked: (form.owner_platforms || []).indexOf(channel.origin) >= 0,
                             onChange: function () { toggleList("owner_platforms", channel.origin); } }),
                h("code", { className: "hae-code" }, channel.origin), " · " + channel.sessions + " " + t.sessions + " · " + s.platform,
                platform ? h(Pill, { tone: "good" }, s.suggested) : null),
              platform ? h("div", { className: "hae-muted hae-indent" }, s.reason[platform.reason] || "") : null,
              h("div", { className: "hae-indent hae-stack-tight" }, (channel.senders || []).filter(function (item) { return item.id; }).map(function (sender) {
                const hint = suggestedFor("sender", sender.id);
                return h("label", { key: sender.id, className: "hae-field-inline" },
                  h("input", { type: "checkbox", disabled: disabled, checked: (form.owner_sender_ids || []).indexOf(sender.id) >= 0,
                               onChange: function () { toggleList("owner_sender_ids", sender.id); } }),
                  h("code", { className: "hae-code" }, sender.id), " · " + sender.sessions + " " + t.sessions + " · " + s.sender,
                  hint ? h(Pill, { tone: "good" }, s.suggested) : null);
              })));
          }) : h("p", { className: "hae-muted" }, s.noChannels))),
      data.can_decide ? h("div", { className: "hae-actions hae-actions-left" },
        h(Button, { primary: true, onClick: function () { save(form); }, disabled: disabled || !Object.keys(changed(form)).length },
          act.working ? s.saving : s.save)) : null);
  }

  function History(props) {
    const t = props.t, rows = props.data.history || [], act = props.actions;
    if (!rows.length) return h("p", { className: "hae-muted" }, t.historyEmpty);
    return h("div", { className: "hae-table-wrap" }, h("table", { className: "hae-table" },
      h("thead", null, h("tr", null, [t.col.when, t.col.who, t.col.what, "", ""].map(function (label, n) { return h("th", { key: n }, label); }))),
      h("tbody", null, rows.map(function (row) {
        const what = row.kind === "lesson" ? (t.action[row.action] || row.action) + " #" + row.target
          : row.kind === "setting" ? (t.action.set) + " " + row.target + ": " + JSON.stringify(row.before.value) + " → " + JSON.stringify(row.after.value)
          : row.kind === "memory" ? t.memoryRemoved + " " + row.target
          : row.kind === "maintenance" ? t.maintenanceRan + " hermes " + String(row.target).replace("_", " ")
          : row.kind === "proposal" && row.after && row.after.applied ? t.action.apply + " " + (row.command || "hermes " + row.after.applied.join(" "))
          : (t.action[row.action] || row.action) + " " + t.tabs.proposals.toLowerCase() + " #" + row.target;
        return h("tr", { key: row.id, className: row.undone ? "hae-row-muted" : "" },
          h("td", null, when(row.at, t.lang)),
          h("td", null, t.actor[row.actor] || row.actor),
          h("td", null, what),
          h("td", { className: "hae-lesson" }, row.lesson ? inline(String(row.lesson), "h" + row.id) : ""),
          h("td", null, row.undone ? t.undone : props.data.can_decide && row.undoable !== false
            ? h(Button, { disabled: !!act.working, onClick: function () {
                act.send({ kind: "undo", id: row.id }, function (result) { return result && result.undone ? t.done.undo : String(result && result.reason || ""); });
              } }, t.undo) : null));
      }))));
  }

  const TABS = ["overview", "lessons", "proposals", "health", "environment", "settings", "history", "digest", "status"];

  function EvolutionPage() {
    const i18n = useI18n() || {};
    const lang = language(i18n.locale);
    const t = TEXT[lang];
    const tabState = hooks.useState("overview");
    const tab = tabState[0], setTab = tabState[1];
    const daysState = hooks.useState(7);
    const days = daysState[0], setDays = daysState[1];
    const dataState = hooks.useState({ loading: true });
    const view = dataState[0], setView = dataState[1];
    const noticeState = hooks.useState(null);
    const notice = noticeState[0], setNotice = noticeState[1];

    const load = hooks.useCallback(function () {
      setView(function (previous) { return { loading: true, data: previous.data }; });
      SDK.fetchJSON("/api/plugins/" + NAME + "/snapshot?days=" + days + "&lang=" + lang)
        .then(function (data) {
          if (!data || data.can_decide || data.decide_reason !== "other_profile") return data;
          const own = "/api/plugins/" + NAME + "/snapshot?days=" + days + "&lang=" + lang + scope({ target: data.profile });
          return SDK.fetchJSON(own).then(function (mine) {
            // the dashboard's own process answers for this profile: its verdict (and its reason) is the one that counts
            return mine && mine.profile === data.profile ? Object.assign(mine, { target: data.profile }) : data;
          }).catch(function () { return data; });
        })
        .then(function (data) { setView({ loading: false, data: data }); })
        .catch(function (error) {
          const busy = String(error && error.message || "").indexOf("503") === 0;
          setView({ loading: false, error: busy ? t.busy : t.failed + " " + String(error && error.message || error) });
        });
    }, [days, lang]);

    hooks.useEffect(function () { load(); }, [load]);
    const notify = function (message, bad) {
      if (!message) return;
      setNotice({ text: message, bad: !!bad });
      setTimeout(function () { setNotice(null); }, 6000);
    };

    const data = view.data;
    const actions = useActions({ data: data || {}, lang: lang, reload: load, notify: notify });
    const header = h("div", { className: "hae-header" },
      h("div", null,
        h("h1", { className: "hae-title" }, t.title),
        data ? h("div", { className: "hae-subtitle" }, t.profile + " ", h("code", { className: "hae-code" }, data.profile), " · v" + data.version) : null),
      h("div", { className: "hae-actions" },
        data ? h(Pill, { tone: data.mode === "off" ? "muted" : "good" }, t.mode[data.mode] || data.mode) : null,
        data && data.automatic ? h(Pill, { tone: data.automatic.mode === "manual" ? "muted" : "good" },
          t.autoLine + ": " + (t.autoNames[data.automatic.mode] || data.automatic.mode)) : null,
        data ? h(Pill, { tone: (data.status.runtime_guard || {}).enabled ? "good" : "bad" },
                 (data.status.runtime_guard || {}).enabled ? t.guardOn : t.guardOff) : null,
        data ? h(Pill, { tone: (data.status.reflector || {}).enabled ? "neutral" : "muted" },
                 (data.status.reflector || {}).enabled ? t.reflectorOn : t.reflectorOff) : null,
        h("select", { className: "hae-select", value: days, onChange: function (event) { setDays(Number(event.target.value)); },
                      "aria-label": t.days },
          [7, 14, 30].map(function (value) { return h("option", { key: value, value: value }, value + " " + t.days); })),
        h(Button, { onClick: load, disabled: view.loading }, t.retry)));

    let body;
    const common = { t: t, data: data, actions: actions };
    if (view.error) body = h("p", { className: "hae-error" }, view.error);
    else if (!data) body = h("p", { className: "hae-muted" }, t.loading);
    else if (tab === "overview") body = h(Overview, common);
    else if (tab === "lessons") body = h(Lessons, common);
    else if (tab === "proposals") body = h(Proposals, common);
    else if (tab === "settings") body = h(Settings, common);
    else if (tab === "history") body = h(History, common);
    else if (tab === "health") body = h(Health, common);
    else if (tab === "environment") body = h("div", { className: "hae-stack" }, h(Markdown, { text: data.environment_md || "" }));
    else if (tab === "digest") body = h(Markdown, { text: data.report_md });
    else body = h(Markdown, { text: data.status_md });

    return h("div", { className: "hae-page" + (view.loading && data ? " hae-loading" : "") },
      header,
      data && !data.can_decide ? h("div", { className: "hae-notice hae-stack-tight" },
        h("span", null, data.decide_reason === "shared_token" ? t.readOnlyShared
          : data.decide_reason === "other_profile" ? t.readOnlyOther : t.readOnly),
        data.commands && data.commands.menu ? h("span", null, t.menuHint) : null,
        data.commands && data.commands.menu ? h(CopyCommand, { t: t, command: data.commands.menu }) : null) : null,
      notice ? h("p", { className: "hae-notice" + (notice.bad ? " hae-notice-bad" : " hae-notice-good"), role: "status" }, notice.text) : null,
      h("div", { className: "hae-tabs", role: "tablist" }, TABS.map(function (name) {
        return h("button", { key: name, type: "button", role: "tab", "aria-selected": tab === name,
                             className: "hae-tab" + (tab === name ? " hae-tab-active" : ""), onClick: function () { setTab(name); } },
                 t.tabs[name]);
      })),
      body);
  }

  window.__HERMES_PLUGINS__.register(NAME, EvolutionPage);
})();
