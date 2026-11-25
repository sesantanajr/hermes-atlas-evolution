"""Hermes' own warnings, read from its log and said in plain words (0.14.0).

Hermes logs what goes wrong around a turn even when the turn itself succeeds: the gateway's memory going over the
budget of its agent cache, the browser tools failing to reach their browser, a race of the session database at a
restart, a backup that came out incomplete, the event loop stalling, the security audit's root warning at every
start, and a plugin's capability check. Read once, each line is a mystery; counted per day with what it means, they
become one sentence each: whether it matters, and what to do.

The lead profile reads the log of the Hermes root (where a multi-profile gateway writes) incrementally, with the
same cursor rules as the background reviews (logwatch: rotated files first, never a line twice), and keeps per day
and kind only a count, a peak number (a memory size in MB, a stall in seconds, a backup size in bytes) and a few
labels (a port, a capability, a profile name, a method). No line of the log is stored.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

WINDOW_DAYS = 7
READS_PER_PASS = 8          # at most 32 MiB of log per pass (logwatch reads 4 MiB per call)
DETAIL_LABELS = 12
_HEAD = re.compile(rb"^(?P<day>\d{4}-\d{2}-\d{2}) [\d:,.]{8,15} (?P<level>[A-Z]{3,8})\b")

# code -> (a literal every matching line has, the pattern, the group kept as the peak, the groups kept as labels)
SIGNALS = {
    "memory_pressure": (b"Agent cache pressure", re.compile(
        rb"Agent cache pressure: anon RSS (?P<rss>\d{1,7})MB over budget (?P<budget>\d{1,7})MB"
        rb"(?P<stuck> but no evictable session)?"), "rss", ("budget",)),
    "browser_unreachable": (b"CDP supervisor", re.compile(
        rb"CDP supervisor \S{1,200}: connect failed \(attempt \d{1,3}/\d{1,3}\): .{0,300}?"
        rb"\('(?P<host>[0-9a-fA-F.:]{1,64})', (?P<port>\d{1,5})\)"), None, ("port",)),
    "state_db_race": (b"was still in flight", re.compile(
        rb"state\.db connection for \S{1,400} was closed while a (?:read|write) was still in flight"), None, ()),
    "state_db_replaced": (b"state.db was replaced", re.compile(
        rb"FATAL: state\.db was replaced underneath the gateway"), None, ()),
    "running_as_root": (b"Running as ROOT", re.compile(rb"\[security \d{1,3}/\d{1,3}\] Running as ROOT"), None, ()),
    "backup_incomplete": (b"status=incomplete", re.compile(
        rb"automatic backup phase=\w{1,40} status=incomplete duration_ms=(?P<ms>[\d.]{1,16}) files=\d{1,9} "
        rb"errors=(?P<errors>\d{1,9}) bytes=(?P<bytes>\d{1,15})"), "bytes", ()),
    "loop_stall": (b"event loop stalled", re.compile(rb"event loop stalled (?P<seconds>\d{1,5}(?:\.\d{1,3})?)s"),
                   "seconds", ()),
    "capability_denied": (b"decision=deny", re.compile(
        rb"capability_check plugin=(?P<plugin>[\w.-]{1,96}) capability=(?P<capability>[\w.]{1,64}) decision=deny"),
                          None, ("capability", "plugin")),
    "sessions_unknown_profile": (b"has no resolvable home", re.compile(
        rb"profile '(?P<profile>[a-z0-9][a-z0-9_-]{0,63})' has no resolvable home"), None, ("profile",)),
    "plugin_host_exited": (b"Plugin host for", re.compile(rb"Plugin host for \S{1,400} exited"), None, ()),
    "contract_violation": (b"wire contract", re.compile(
        rb"params accepted by '(?P<method>[\w.]{1,80})' violates its wire contract"), None, ("method",)),
}
# Each kind: whether it calls for the owner (attention), or is only explained (info).
ATTENTION = {"memory_pressure", "state_db_replaced", "backup_incomplete", "contract_violation"}


def _q(count, one: str, many: str) -> str:
    """A count with its word in the right number: 1 vez, 2 vezes."""
    return f"{count} {one if count == 1 else many}"


def _consume(conn, data: bytes, *, since: str | None) -> int:
    counted: dict[tuple[str, str], list] = {}
    for line in data.split(b"\n"):
        head = None
        for code, (literal, pattern, peak_group, label_groups) in SIGNALS.items():
            if literal not in line:
                continue
            match = pattern.search(line)
            if match is None:
                continue
            head = head or _HEAD.match(line)
            if head is None:
                break
            day = head["day"].decode()
            if since and day < since:
                break
            entry = counted.setdefault((day, code), [0, 0.0, {}, 0])
            entry[0] += 1
            if peak_group:
                try:
                    entry[1] = max(entry[1], float(match[peak_group]))
                except (TypeError, ValueError):
                    pass
            for group in label_groups:
                value = match[group]
                if value:
                    labels = entry[2].setdefault(group, set())
                    if len(labels) < DETAIL_LABELS:
                        labels.add(value.decode("ascii", "replace"))
            if code == "memory_pressure" and match["stuck"]:
                entry[3] += 1
            break   # one kind per line
    for (day, code), (count, peak, labels, stuck) in counted.items():
        row = conn.execute("SELECT count, peak, detail FROM log_signal_daily WHERE day=? AND code=?", (day, code)).fetchone()
        detail = {}
        if row is not None:
            try:
                detail = json.loads(row[2]) if row[2] else {}
            except ValueError:
                detail = {}
        for group, values in labels.items():
            merged = list(dict.fromkeys([*(detail.get(group) or ()), *sorted(values)]))[:DETAIL_LABELS]
            detail[group] = merged
        if stuck:
            detail["stuck"] = int(detail.get("stuck") or 0) + stuck
        conn.execute("""INSERT INTO log_signal_daily(day, code, count, peak, detail) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(day, code) DO UPDATE SET count=count+excluded.count, peak=MAX(peak, excluded.peak),
              detail=excluded.detail""", (day, code, count, peak, json.dumps(detail, sort_keys=True)))
    return sum(entry[0] for entry in counted.values())


def ingest(root: Path, conn, *, now: float | None = None) -> dict:
    """Count the warnings logged in the Hermes root's agent.log since the last run (at most READS_PER_PASS reads)."""
    from .logwatch import ingest_lines, local_days

    now = time.time() if now is None else now
    since = local_days(now, WINDOW_DAYS)[0]
    added, status = 0, "ok"
    for _ in range(READS_PER_PASS):
        step = ingest_lines(Path(root), conn, key="log_signals", consume=_consume, since_day=since)
        status = step.get("status", "ok")
        added += step.get("lines") or 0
        if not step.get("more"):
            break
    return {"status": status, "lines": added}


def summary(conn, *, now: float | None = None, days: int = WINDOW_DAYS) -> dict:
    """{code: {"count", "days", "peak", "last_day", "detail"}} for the last `days` local days."""
    from .logwatch import local_days

    now = time.time() if now is None else now
    start, end = local_days(now, days)
    found: dict = {}
    for day, code, count, peak, detail in conn.execute(
            "SELECT day, code, count, peak, detail FROM log_signal_daily WHERE day BETWEEN ? AND ? ORDER BY day",
            (start, end)).fetchall():
        entry = found.setdefault(code, {"count": 0, "days": 0, "peak": 0.0, "last_day": day, "detail": {}})
        entry["count"] += int(count)
        entry["days"] += 1
        entry["peak"] = max(entry["peak"], float(peak or 0))
        entry["last_day"] = day
        try:
            detail = json.loads(detail) if detail else {}
        except ValueError:
            detail = {}
        for key, value in detail.items():
            if key == "stuck":
                entry["detail"]["stuck"] = int(entry["detail"].get("stuck") or 0) + int(value or 0)
            elif isinstance(value, list):
                entry["detail"][key] = list(dict.fromkeys([*(entry["detail"].get(key) or ()), *value]))[:DETAIL_LABELS]
    return found


def findings(found: dict) -> list[str]:
    """The attention codes: the kinds that call for the owner, when they happened in the window."""
    codes = [f"log_{code}" for code in sorted(found) if code in ATTENTION and found[code]["count"]]
    if "loop_stall" in found and found["loop_stall"]["peak"] >= 10:
        codes.append("log_loop_stall")
    return codes


def _size(value: float, lang: str) -> str:
    from .status import human_size

    return human_size(value) if lang != "pt" else human_size(value).replace(".", ",")


def _join(values, limit: int = 5) -> str:
    values = list(values or ())
    text = ", ".join(f"`{value}`" for value in values[:limit])
    return text + ("…" if len(values) > limit else "")


def lines(found: dict | None, lang: str = "en", *, days: int = WINDOW_DAYS) -> list[str]:
    """One Markdown bullet per kind seen in the window: what it is, whether it matters, what to do. Attention first."""
    if not found:
        return []
    pt = lang == "pt"
    out = []
    order = sorted(found, key=lambda code: (code not in ATTENTION, code))
    for code in order:
        item = found[code]
        n, d, peak, detail = item["count"], item["days"], item["peak"], item.get("detail") or {}
        if code == "memory_pressure":
            budget = ", ".join(detail.get("budget") or ()) or "?"
            stuck = int(detail.get("stuck") or 0)
            out.append(
                f"- ⚠️ **Pressão de memória no gateway:** {_q(n, 'vez', 'vezes')} em {_q(d, 'dia', 'dias')} a memória passou do orçamento do cache "
                f"de agentes (pico {peak:.0f} MB, orçamento {budget} MB)"
                + (f"; em {stuck} delas nenhuma sessão podia sair" if stuck else "") +
                ". Cada vez o Hermes tira conversas da memória (a próxima mensagem delas fica mais lenta). Veja a "
                "seção **Memória do gateway**." if pt else
                f"- ⚠️ **Gateway memory pressure:** {_q(n, 'time', 'times')} on {_q(d, 'day', 'days')} memory went over the agent cache's budget "
                f"(peak {peak:.0f} MB, budget {budget} MB)" + (f"; {stuck} of them with no session that could go" if stuck else "")
                + ". Each time Hermes drops conversations from memory (their next message is slower). See **Gateway "
                "memory**.")
        elif code == "state_db_replaced":
            out.append(f"- ⚠️ **O `state.db` foi trocado com o gateway rodando** ({_q(n, 'vez', 'vezes')}): o Hermes parou de gravar "
                       "nele e desviou as conversas para arquivos. Reinicie o gateway e descubra o que mexeu no arquivo "
                       "(uma restauração, uma sincronização)." if pt else
                       f"- ⚠️ **`state.db` was replaced while the gateway ran** ({_q(n, 'time', 'times')}): Hermes stopped writing "
                       "to it and diverted conversations to files. Restart the gateway and find what touched the file "
                       "(a restore, a sync).")
        elif code == "backup_incomplete":
            out.append(f"- ⚠️ **Backup antes da atualização incompleto** {_q(n, 'vez', 'vezes')} (o maior com {_size(peak, lang)}): "
                       "arquivos mudaram enquanto o Hermes os copiava, e a atualização seguiu sem um backup inteiro. "
                       "Um backup desse tamanho costuma ser a pasta de um perfil com caches, mídia ou sessões antigas: "
                       "`du -sh ~/.hermes/profiles/* | sort -h | tail`." if pt else
                       f"- ⚠️ **Pre-update backup incomplete** {_q(n, 'time', 'times')} (the largest {_size(peak, lang)}): files "
                       "changed while Hermes copied them, and the update went on without a whole backup. A backup that "
                       "big is usually a profile folder full of caches, media or old sessions: "
                       "`du -sh ~/.hermes/profiles/* | sort -h | tail`.")
        elif code == "contract_violation":
            out.append(f"- ⚠️ **Erros de contrato do Hermes** ({n}, em {_join(detail.get('method'))}): a resposta desse "
                       "método não bate com o contrato que o painel e o TUI esperam — costuma ser uma mudança local no "
                       "código do Hermes que a versão nova não acompanha (veja **Depois da atualização**)." if pt else
                       f"- ⚠️ **Hermes contract errors** ({n}, in {_join(detail.get('method'))}): that method's answer "
                       "does not match the contract the panel and the TUI expect — usually a local change to Hermes' "
                       "code the new version does not follow (see **After the update**).")
        elif code == "loop_stall":
            out.append(f"- O laço de eventos do Hermes travou {_q(n, 'vez', 'vezes')} (pior: {f'{peak:.1f}'.replace('.', ',')} s): o processo ficou sem CPU "
                       "para responder (muitas sessões ao mesmo tempo ou um trabalho pesado no mesmo processo)." if pt else
                       f"- Hermes' event loop stalled {_q(n, 'time', 'times')} (worst: {peak:.1f} s): the process had no CPU to "
                       "answer (many sessions at once, or heavy work in the same process).")
        elif code == "browser_unreachable":
            out.append(f"- O navegador das ferramentas de browser recusou {_q(n, 'conexão', 'conexões')} (porta {_join(detail.get('port'))}) "
                       f"em {_q(d, 'dia', 'dias')}: ele não estava aberto quando uma sessão tentou se reconectar. Normal se ninguém "
                       "usou o navegador; se o agente reclamou dele, confira com `/browser status` numa sessão." if pt else
                       f"- The browser tools' browser refused {_q(n, 'connection', 'connections')} (port {_join(detail.get('port'))}) on "
                       f"{_q(d, 'day', 'days')}: it was not open when a session tried to reconnect. Normal if nobody used the "
                       "browser; if the agent complained about it, check with `/browser status` in a session.")
        elif code == "state_db_race":
            out.append(f"- {_q(n, 'aviso', 'avisos')} de corrida no banco de sessões ao fechar uma conexão (problema conhecido do Hermes, "
                       "#94736): ele mesmo reabre a conexão. Acontece nos reinícios; só pede atenção fora deles." if pt else
                       f"- {_q(n, 'session database race warning', 'session database race warnings')} while a connection closed (a known Hermes issue, #94736): "
                       "it reopens the connection itself. It happens at restarts; it matters only outside them.")
        elif code == "running_as_root":
            out.append(f"- O Hermes avisa a cada partida que roda como root ({_q(n, 'vez', 'vezes')}): as ferramentas de terminal e de "
                       "arquivos do agente podem tudo na máquina. Se foi uma escolha sua, nada a fazer; para reduzir o "
                       "risco nos perfis que falam com outras pessoas, use o terminal em contêiner "
                       "(`terminal.backend: docker`)." if pt else
                       f"- Hermes warns at every start that it runs as root ({_q(n, 'time', 'times')}): the agent's terminal and file "
                       "tools can do anything on the machine. If that was your choice, nothing to do; to lower the risk "
                       "in profiles that talk to other people, run the terminal in a container (`terminal.backend: docker`).")
        elif code == "capability_denied":
            plugins = detail.get("plugin") or ()
            out.append(f"- {_q(n, 'registro', 'registros')} `capability_check … decision=deny` ({_join(detail.get('capability'))}, de "
                       f"{len(plugins)}{'+' if len(plugins) >= DETAIL_LABELS else ''} {'plugin' if len(plugins) == 1 else 'plugins'}): é o Hermes anotando "
                       "que esses plugins **não** podem substituir ferramentas nativas — o padrão seguro. Não é erro e não "
                       "pede nada, a menos que um plugin precise mesmo substituir uma ferramenta." if pt else
                       f"- {n} `capability_check … decision=deny` {'record' if n == 1 else 'records'} ({_join(detail.get('capability'))}, from "
                       f"{len(plugins)}{'+' if len(plugins) >= DETAIL_LABELS else ''} {'plugin' if len(plugins) == 1 else 'plugins'}): Hermes noting that those "
                       "plugins may **not** replace built-in tools — the safe default. Not an error, and nothing to do "
                       "unless a plugin really needs to replace a tool.")
        elif code == "sessions_unknown_profile":
            out.append(f"- {_q(n, 'aviso', 'avisos')} de sessões de perfis que não existem mais ({_join(detail.get('profile'))}): o "
                       "gateway ainda guarda conversas deles. `hermes sessions repair-profiles` mostra e acerta." if pt else
                       f"- {_q(n, 'warning', 'warnings')} about sessions of profiles that no longer exist ({_join(detail.get('profile'))}): "
                       "the gateway still keeps their conversations. `hermes sessions repair-profiles` shows and settles it.")
        elif code == "plugin_host_exited":
            out.append(f"- O host de plugins de um perfil saiu {_q(n, 'vez', 'vezes')}; o Hermes o reinicia sozinho." if pt else
                       f"- A profile's plugin host exited {_q(n, 'time', 'times')}; Hermes restarts it by itself.")
    return out
