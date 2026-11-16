(function () {
  "use strict";
  // Atlas Evolution panel for the Hermes dashboard. Plain JavaScript on the host's plugin SDK: no build step,
  // no dependency. It reads /api/plugins/hermes-atlas-evolution/snapshot for the profile the dashboard shows
  // and decides nothing: every decision is a command for the owner's own terminal, shown with a copy button.
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
      title: "Atlas Evolution", profile: "profile", loading: "Reading what this profile learned…",
      busy: "The Evolution store is busy right now. Try again in a moment.",
      failed: "The panel could not load:", retry: "Reload", days: "days",
      tabs: { overview: "Overview", lessons: "Lessons", proposals: "Proposals", digest: "Digest", status: "Status" },
      mode: { auto: "lessons: auto", suggest: "lessons: suggest", off: "lessons: off" },
      guardOn: "guard on", guardOff: "guard off", reflectorOn: "reflector on", reflectorOff: "reflector off",
      kpi: { active: "Active lessons", waiting: "Waiting for you", proposals: "Open proposals", calls: "Tool calls",
             errors: "errors", turns: "Turns", failed: "failed", vetoed: "Guard vetoes (7 d)", lastPass: "Last pass",
             passFailed: "failed: see Status" },
      never: "never", none: "—",
      waitingTitle: "Waiting for your decision",
      waitingEmpty: "Nothing waits for you. Lessons from fixed templates apply by themselves; anything written by a model or by the agent will show up here.",
      waitingHint: "Decide in your own terminal. The agent cannot approve anything, and neither can this page.",
      lessonsEmpty: "No lessons yet. A tool failure that comes back (3 times, in 2 sessions or on 2 days) becomes one.",
      proposalsEmpty: "No open proposals.",
      col: { status: "status", origin: "origin", tool: "tool", lesson: "lesson", effect: "measured effect", risk: "risk",
             subject: "subject", command: "command to run" },
      verdict: { effective: "helps", ineffective: "does not help", inconclusive: "inconclusive", withdrawn: "withdrawn from the queue" },
      status: { active: "active", candidate: "waiting", quarantined: "waiting (written)", retired: "retired" },
      origin: { derived: "template", operator: "you", reflector: "reflector", agent: "agent" },
      copy: "Copy", copied: "Copied",
      share: "Share what this profile learned with another profile:",
      readOnly: "This panel only reads. Approving, rejecting, retiring and importing stay in your terminal."
    },
    pt: {
      title: "Atlas Evolution", profile: "perfil", loading: "Lendo o que este perfil aprendeu…",
      busy: "O banco do Evolution está ocupado agora. Tente de novo em instantes.",
      failed: "O painel não carregou:", retry: "Recarregar", days: "dias",
      tabs: { overview: "Visão geral", lessons: "Lições", proposals: "Propostas", digest: "Resumo", status: "Status" },
      mode: { auto: "lições: auto", suggest: "lições: suggest", off: "lições: off" },
      guardOn: "guarda ligada", guardOff: "guarda desligada", reflectorOn: "refletor ligado", reflectorOff: "refletor desligado",
      kpi: { active: "Lições ativas", waiting: "Esperando você", proposals: "Propostas abertas", calls: "Chamadas de ferramentas",
             errors: "erros", turns: "Turnos", failed: "falharam", vetoed: "Vetos da guarda (7 d)", lastPass: "Última passada",
             passFailed: "falhou: veja o Status" },
      never: "nunca", none: "—",
      waitingTitle: "Esperando a sua decisão",
      waitingEmpty: "Nada espera por você. Lições de modelos fixos valem sozinhas; o que um modelo ou o agente escrever aparece aqui.",
      waitingHint: "Decida no seu próprio terminal. O agente não aprova nada, e esta página também não.",
      lessonsEmpty: "Ainda não há lições. Uma falha de ferramenta que volta (3 vezes, em 2 sessões ou em 2 dias) vira uma.",
      proposalsEmpty: "Nenhuma proposta aberta.",
      col: { status: "situação", origin: "origem", tool: "ferramenta", lesson: "lição", effect: "efeito medido", risk: "risco",
             subject: "assunto", command: "comando para rodar" },
      verdict: { effective: "ajuda", ineffective: "não ajuda", inconclusive: "inconclusivo", withdrawn: "retirada da fila" },
      status: { active: "ativa", candidate: "esperando", quarantined: "esperando (escrita)", retired: "aposentada" },
      origin: { derived: "modelo fixo", operator: "você", reflector: "refletor", agent: "agente" },
      copy: "Copiar", copied: "Copiado",
      share: "Leve o que este perfil aprendeu para outro perfil:",
      readOnly: "Este painel só lê. Aprovar, rejeitar, aposentar e importar continuam no seu terminal."
    }
  };

  function language(locale) {
    return typeof locale === "string" && locale.toLowerCase().indexOf("pt") === 0 ? "pt" : "en";
  }

  function fmt(value, digits) {
    if (value === null || value === undefined || Number.isNaN(value)) return "—";
    if (typeof value !== "number") return String(value);
    return digits ? value.toFixed(digits) : value.toLocaleString();
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

  // ── the tabs
  function Overview(props) {
    const t = props.t, data = props.data, state = data.status || {}, digest = data.report || {};
    const lessons = state.lessons || {};
    const guard = state.runtime_guard || {};
    const tools = digest.tools || {};
    const turns = (digest.turns || {}).interactive || {};
    const waiting = (data.lessons || []).filter(function (row) { return row.status === "candidate" || row.status === "quarantined"; });
    // status.build: {at, ok, seconds, summary, error} after a pass, null before the first one
    const pass = (state.autopilot || {}).last_pass;
    const passAt = pass && typeof pass === "object" ? pass.at : pass;
    const passFailed = !!(pass && typeof pass === "object" && pass.ok === false);
    return h("div", { className: "hae-stack" },
      h("div", { className: "hae-kpis" },
        h(Kpi, { label: t.kpi.active, value: fmt(lessons.active || 0) }),
        h(Kpi, { label: t.kpi.waiting, value: fmt(waiting.length + (state.proposals_open || 0)), alert: waiting.length + (state.proposals_open || 0) > 0 }),
        h(Kpi, { label: t.kpi.calls + " (" + data.days + " d)", value: fmt(tools.calls || 0),
                 detail: tools.error_pct === null || tools.error_pct === undefined ? null : fmt(tools.error_pct, 1) + "% " + t.kpi.errors }),
        h(Kpi, { label: t.kpi.turns + " (" + data.days + " d)", value: fmt(turns.turns || 0),
                 detail: turns.failed_pct === null || turns.failed_pct === undefined ? null : fmt(turns.failed_pct, 1) + "% " + t.kpi.failed }),
        h(Kpi, { label: t.kpi.vetoed, value: fmt(guard.vetoed_7d || 0) }),
        h(Kpi, { label: t.kpi.lastPass, alert: passFailed,
                 value: passAt ? String(passAt).replace("T", " ").replace(/:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)?$/, "") : t.never,
                 detail: passFailed ? t.kpi.passFailed : passAt ? "UTC" : null })),
      h(Section, { title: t.waitingTitle },
        waiting.length || state.proposals_open
          ? h("div", { className: "hae-stack" },
              waiting.map(function (row) {
                return h("div", { key: row.id, className: "hae-waiting" },
                  h("div", { className: "hae-waiting-meta" }, h(Pill, { tone: "warn" }, "#" + row.id), h(Pill, null, t.origin[row.origin] || row.origin),
                    h("code", { className: "hae-code" }, row.tool)),
                  h("div", { className: "hae-lesson" }, inline(String(row.lesson || ""), "w" + row.id)));
              }),
              state.proposals_open ? h("p", { className: "hae-p" }, t.kpi.proposals + ": " + state.proposals_open) : null,
              h("p", { className: "hae-muted" }, t.waitingHint),
              h(CopyCommand, { t: t, command: data.commands.review }))
          : h("p", { className: "hae-muted" }, t.waitingEmpty)),
      h(Section, { title: t.share },
        h("div", { className: "hae-stack" }, h(CopyCommand, { t: t, command: data.commands.export }),
          h(CopyCommand, { t: t, command: data.commands.import }))),
      h("p", { className: "hae-muted" }, t.readOnly));
  }

  function Lessons(props) {
    const t = props.t, rows = props.data.lessons || [];
    if (!rows.length) return h("p", { className: "hae-muted" }, t.lessonsEmpty);
    return h("div", { className: "hae-table-wrap" }, h("table", { className: "hae-table" },
      h("thead", null, h("tr", null, ["#", t.col.status, t.col.origin, t.col.tool, t.col.lesson, t.col.effect].map(function (label, n) {
        return h("th", { key: n }, label);
      }))),
      h("tbody", null, rows.map(function (row) {
        return h("tr", { key: row.id, className: row.status === "retired" ? "hae-row-muted" : "" },
          h("td", null, row.id),
          h("td", null, h(Pill, { tone: statusTone(row.status) }, t.status[row.status] || row.status)),
          h("td", null, t.origin[row.origin] || row.origin),
          h("td", null, h("code", { className: "hae-code" }, row.tool)),
          h("td", { className: "hae-lesson" }, inline(String(row.lesson || ""), "l" + row.id)),
          h("td", null, effect(t, row)));
      }))));
  }

  function Proposals(props) {
    const t = props.t, rows = props.data.proposals || [];
    if (!rows.length) return h("p", { className: "hae-muted" }, t.proposalsEmpty);
    return h("div", { className: "hae-stack" }, rows.map(function (row) {
      const lesson = row.evidence && row.evidence.lesson;
      return h(Section, { key: row.id, title: "#" + row.id + " · " + row.kind },
        h("div", { className: "hae-stack" },
          h("div", { className: "hae-waiting-meta" },
            h(Pill, { tone: row.risk === "high" ? "bad" : row.risk === "medium" ? "warn" : "neutral" }, t.col.risk + ": " + row.risk),
            h("code", { className: "hae-code" }, row.subject)),
          lesson ? h("div", { className: "hae-lesson" }, inline(String(lesson), "p" + row.id)) : null,
          h("div", { className: "hae-muted" }, t.col.command),
          h(CopyCommand, { t: t, command: row.command })));
    }));
  }

  const TABS = ["overview", "lessons", "proposals", "digest", "status"];

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

    const load = hooks.useCallback(function () {
      setView(function (previous) { return { loading: true, data: previous.data }; });
      SDK.fetchJSON("/api/plugins/" + NAME + "/snapshot?days=" + days + "&lang=" + lang)
        .then(function (data) { setView({ loading: false, data: data }); })
        .catch(function (error) {
          const busy = String(error && error.message || "").indexOf("503") === 0;
          setView({ loading: false, error: busy ? t.busy : t.failed + " " + String(error && error.message || error) });
        });
    }, [days, lang]);

    hooks.useEffect(function () { load(); }, [load]);

    const data = view.data;
    const header = h("div", { className: "hae-header" },
      h("div", null,
        h("h1", { className: "hae-title" }, t.title),
        data ? h("div", { className: "hae-subtitle" }, t.profile + " ", h("code", { className: "hae-code" }, data.profile), " · v" + data.version) : null),
      h("div", { className: "hae-actions" },
        data ? h(Pill, { tone: data.mode === "off" ? "muted" : "good" }, t.mode[data.mode] || data.mode) : null,
        data ? h(Pill, { tone: (data.status.runtime_guard || {}).enabled ? "good" : "bad" },
                 (data.status.runtime_guard || {}).enabled ? t.guardOn : t.guardOff) : null,
        data ? h(Pill, { tone: (data.status.reflector || {}).enabled ? "neutral" : "muted" },
                 (data.status.reflector || {}).enabled ? t.reflectorOn : t.reflectorOff) : null,
        h("select", { className: "hae-select", value: days, onChange: function (event) { setDays(Number(event.target.value)); },
                      "aria-label": t.days },
          [7, 14, 30].map(function (value) { return h("option", { key: value, value: value }, value + " " + t.days); })),
        h("button", { type: "button", className: "hae-button", onClick: load, disabled: view.loading }, t.retry)));

    let body;
    if (view.error) body = h("p", { className: "hae-error" }, view.error);
    else if (!data) body = h("p", { className: "hae-muted" }, t.loading);
    else if (tab === "overview") body = h(Overview, { t: t, data: data });
    else if (tab === "lessons") body = h(Lessons, { t: t, data: data });
    else if (tab === "proposals") body = h(Proposals, { t: t, data: data });
    else if (tab === "digest") body = h(Markdown, { text: data.report_md });
    else body = h(Markdown, { text: data.status_md });

    return h("div", { className: "hae-page" + (view.loading && data ? " hae-loading" : "") },
      header,
      h("div", { className: "hae-tabs", role: "tablist" }, TABS.map(function (name) {
        return h("button", { key: name, type: "button", role: "tab", "aria-selected": tab === name,
                             className: "hae-tab" + (tab === name ? " hae-tab-active" : ""), onClick: function () { setTab(name); } },
                 t.tabs[name]);
      })),
      body);
  }

  window.__HERMES_PLUGINS__.register(NAME, EvolutionPage);
})();
