"""The playbook: short conditional lessons derived from recurring failures, with a measured effect.

A lesson is one sentence of the form "in situation X, do Y". Derived lessons are rendered from fixed
templates and identifier-like hints, so no model call and no conversation text is involved. Operators
and the agent can add custom lessons; those stay quarantined until an operator approves them.

Every active lesson is measured: the failure it targets is counted before and after activation, and a
lesson that does not reduce it is retired. Updates are incremental; the playbook is never rewritten whole.
"""

from __future__ import annotations

import re
import sqlite3

from .experience import day_offset, utc_day

MODES = ("off", "suggest", "auto")
LANGUAGES = ("en", "pt")
STATUSES = ("candidate", "active", "quarantined", "retired")
ORIGINS = ("derived", "operator", "agent", "reflector")
MIN_TOTAL = 3
BASELINE_DAYS = 14
EVALUATE_AFTER_DAYS = 5
EVALUATE_AFTER_CALLS = 20
RETIRE_AFTER_DAYS = 14
TEXT_LIMIT = 240
_TEXT = re.compile(r"[^\x00-\x08\x0b-\x1f\x7f]{12,%d}\Z" % TEXT_LIMIT)
_SECRET = re.compile(r"(?:sk-|ghp_|gho_|github_pat_|xox[baprs]-|AKIA|AIza)[A-Za-z0-9_-]{8,}|://[^\s/:@]+:[^\s/@]+@")

# class -> (needs_hint, English, Portuguese). {tool} and {hint} are identifier-like labels.
TEMPLATES = {
    "missing_argument": (True,
        "When calling `{tool}`, always pass the required argument `{hint}`.",
        "Ao chamar `{tool}`, informe sempre o argumento obrigatório `{hint}`."),
    "locked_resource": (False,
        "`{tool}`: the target is locked by the user. Do not retry the change; describe it and ask the user to apply or unlock it.",
        "`{tool}`: o item está travado pelo usuário. Não repita a alteração; descreva a mudança e peça que ele aplique ou destrave."),
    "review_precondition": (False,
        "Before patching a skill in a background review, load it first with `skill_view`; patches to an unloaded skill are refused.",
        "Antes de alterar uma skill na revisão em segundo plano, carregue-a com `skill_view`; alterações em skill não carregada são recusadas."),
    "edit_precondition": (False,
        "`{tool}`: read the whole file before overwriting or editing it; writes to unseen content are refused.",
        "`{tool}`: leia o arquivo inteiro antes de sobrescrever ou editar; a escrita em conteúdo não lido é recusada."),
    "edit_mismatch": (False,
        "`{tool}`: re-read the current text and use an exact, unique `old_string` (add surrounding lines) before patching again.",
        "`{tool}`: releia o trecho atual e use um `old_string` exato e único (inclua linhas vizinhas) antes de tentar de novo."),
    "blocked_by_policy": (False,
        "`{tool}`: this kind of command is blocked by the safety policy. Do not retry it; write a script file or use the dedicated tool instead.",
        "`{tool}`: esse tipo de comando é bloqueado pela política de segurança. Não repita; grave um arquivo de script ou use a ferramenta dedicada."),
    "approval_timeout": (False,
        "`{tool}`: commands that need approval time out when nobody answers. Ask the user first, or choose a path that needs no approval.",
        "`{tool}`: comandos que pedem aprovação expiram sem resposta. Pergunte antes ao usuário ou escolha um caminho que não exija aprovação."),
    "timeout": (False,
        "`{tool}` timed out before. Split the work, run long jobs in the background, or set an explicit longer limit.",
        "`{tool}` já estourou o tempo. Divida o trabalho, rode tarefas longas em segundo plano ou defina um limite maior explícito."),
    "missing_dependency": (True,
        "`{hint}` is not installed in this environment. Check availability before using it, or use an alternative.",
        "`{hint}` não está instalado neste ambiente. Verifique a disponibilidade antes de usar ou escolha uma alternativa."),
    "backend_unavailable": (False,
        "The backend behind `{tool}` has been unavailable. Do not loop on it; report it and continue another way.",
        "O serviço por trás de `{tool}` esteve indisponível. Não insista; avise e siga por outro caminho."),
    "not_found": (False,
        "`{tool}`: confirm the exact name from a listing before opening it; guessed names were not found.",
        "`{tool}`: confirme o nome exato numa listagem antes de abrir; nomes presumidos não foram encontrados."),
    "limit_exceeded": (False,
        "`{tool}`: keep content within the size limits the tool reports; oversized input was rejected.",
        "`{tool}`: respeite os limites de tamanho informados pela ferramenta; conteúdo acima do limite foi recusado."),
    "invalid_argument": (False,
        "`{tool}`: check the accepted arguments (`--help` or the tool schema) before calling it again with the same options.",
        "`{tool}`: confira os argumentos aceitos (`--help` ou o esquema da ferramenta) antes de repetir com as mesmas opções."),
    "rate_limited": (False,
        "`{tool}` has hit rate limits. Batch requests, wait before retrying and avoid parallel calls to it.",
        "`{tool}` já bateu no limite de requisições. Agrupe pedidos, espere antes de repetir e evite chamadas paralelas."),
    "permission_denied": (False,
        "`{tool}`: this location or action is not permitted for the agent. Do not retry; ask the user or use an allowed path.",
        "`{tool}`: esse local ou ação não é permitido ao agente. Não repita; peça ao usuário ou use um caminho permitido."),
}
HEADERS = {
    "en": "Lessons this profile learned from repeated tool failures (Atlas Evolution). Apply them when relevant:",
    "pt": "Lições que este perfil aprendeu com falhas repetidas de ferramentas (Atlas Evolution). Aplique quando couber:",
}
HINT_LABEL = {"en": "Atlas Evolution lesson", "pt": "Lição do Atlas Evolution"}


def render(row, lang: str = "en") -> str:
    """Human and agent-facing sentence for one lesson."""
    if row["text"]:
        return row["text"]
    template = TEMPLATES.get(row["klass"])
    if template is None:
        return ""
    return template[2 if lang == "pt" else 1].format(tool=row["tool"], hint=row["hint"])


def valid_text(text) -> bool:
    return isinstance(text, str) and _TEXT.fullmatch(text.strip()) is not None and not _SECRET.search(text)


def _record_change(conn, lesson_id: int, now: float, subject: str) -> None:
    conn.execute("""INSERT OR IGNORE INTO change(ref, kind, subject, detail, at) VALUES (?, 'lesson', ?, '{}', ?)""",
                 (f"lesson:{lesson_id}:{int(now)}", subject, now))


def derive(conn: sqlite3.Connection, *, now: float, mode: str = "suggest") -> list[int]:
    """Create lessons for recurring failures that have a template. Returns the new lesson ids."""
    if mode not in MODES or mode == "off":
        return []
    rows = conn.execute("""SELECT f.signature, f.tool, f.klass, f.hint, f.total, f.sessions,
          (SELECT COUNT(*) FROM failure_daily d WHERE d.signature=f.signature) AS days
        FROM failure f WHERE f.total >= ? AND NOT EXISTS (SELECT 1 FROM lesson l WHERE l.signature=f.signature)
        ORDER BY f.total DESC LIMIT 200""", (MIN_TOTAL,)).fetchall()
    created = []
    for row in rows:
        template = TEMPLATES.get(row["klass"])
        if template is None or template[0] and not row["hint"] or row["tool"] == "unknown":
            continue
        if row["sessions"] < 2 and row["days"] < 2:
            continue   # one bad session is not a pattern yet
        status = "active" if mode == "auto" else "candidate"
        cursor = conn.execute("""INSERT INTO lesson(signature, tool, klass, hint, origin, status, created, updated, activated)
            VALUES (?, ?, ?, ?, 'derived', ?, ?, ?, ?)""",
                              (row["signature"], row["tool"], row["klass"], row["hint"], status, now, now,
                               now if status == "active" else None))
        created.append(cursor.lastrowid)
        if status == "active":
            _record_change(conn, cursor.lastrowid, now, row["tool"])
    return created


def add_custom(conn: sqlite3.Connection, text: str, *, tool: str, origin: str, now: float, active: bool = False) -> int:
    """Store an operator, agent or reflector lesson. Agent and reflector lessons start quarantined."""
    if origin not in ORIGINS[1:]:
        raise ValueError("Unsupported lesson origin")
    if not valid_text(text):
        raise ValueError(f"A lesson is one sentence of 12 to {TEXT_LIMIT} characters without secrets")
    if not isinstance(tool, str) or not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.:+-]{0,95}", tool):
        raise ValueError("Name the tool this lesson applies to, or use 'general'")
    limit = conn.execute("SELECT COUNT(*) FROM lesson WHERE origin != 'derived' AND status != 'retired'").fetchone()[0]
    if limit >= 200:
        raise ValueError("The playbook already holds 200 custom lessons; retire some first")
    status = "active" if active and origin == "operator" else "quarantined"
    cursor = conn.execute("""INSERT INTO lesson(tool, klass, hint, origin, status, text, created, updated, activated)
        VALUES (?, 'custom', '', ?, ?, ?, ?, ?, ?)""",
                          (tool, origin, status, " ".join(text.split()), now, now, now if status == "active" else None))
    if status == "active":
        _record_change(conn, cursor.lastrowid, now, tool)
    return cursor.lastrowid


def add_reflected(conn: sqlite3.Connection, *, signature: str, tool: str, klass: str, hint: str, text: str,
                  now: float) -> int:
    """Store a reflector draft bound to its failure. It stays quarantined until an operator approves it."""
    if not valid_text(text):
        raise ValueError(f"A lesson is one sentence of 12 to {TEXT_LIMIT} characters without secrets")
    cursor = conn.execute("""INSERT INTO lesson(signature, tool, klass, hint, origin, status, text, created, updated)
        VALUES (?, ?, ?, ?, 'reflector', 'quarantined', ?, ?, ?)""", (signature, tool, klass, hint, text, now, now))
    return cursor.lastrowid


def set_status(conn: sqlite3.Connection, lesson_id: int, status: str, *, now: float) -> dict:
    if status not in ("active", "retired"):
        raise ValueError("A lesson can be activated or retired")
    row = conn.execute("SELECT * FROM lesson WHERE id=?", (lesson_id,)).fetchone()
    if row is None:
        raise ValueError(f"Unknown lesson id {lesson_id}")
    if row["status"] != status:
        conn.execute("UPDATE lesson SET status=?, updated=?, activated=CASE WHEN ?='active' THEN ? ELSE activated END,"
                     " verdict=CASE WHEN ?='active' THEN NULL ELSE verdict END WHERE id=?",
                     (status, now, status, now, status, lesson_id))
        if status == "active":
            _record_change(conn, lesson_id, now, row["tool"])
    return {"id": lesson_id, "status": status, "previous": row["status"]}


def active_lessons(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("""SELECT l.*, COALESCE(f.total, 0) AS evidence FROM lesson l
        LEFT JOIN failure f ON f.signature=l.signature WHERE l.status='active'
        ORDER BY (l.verdict='effective') DESC, evidence DESC, l.id""").fetchall()


def playbook(conn: sqlite3.Connection, *, lang: str = "en", limit: int = 8, max_chars: int = 1200,
             mark_shown: bool = False) -> str:
    """Bounded text for the session prompt section. Empty when there is nothing active."""
    lines, used, chosen = [], len(HEADERS.get(lang, HEADERS["en"])), []
    for row in active_lessons(conn):
        sentence = render(row, lang)
        if not sentence or used + len(sentence) + 3 > max_chars:
            continue
        lines.append("- " + sentence)
        used += len(sentence) + 3
        chosen.append(row["id"])
        if len(lines) >= limit:
            break
    if not lines:
        return ""
    if mark_shown:
        conn.executemany("UPDATE lesson SET shown=shown+1 WHERE id=?", [(item,) for item in chosen])
    return HEADERS.get(lang, HEADERS["en"]) + "\n" + "\n".join(lines)


def hint_index(conn: sqlite3.Connection, *, lang: str = "en") -> dict[str, tuple[int, str]]:
    """signature -> (lesson id, sentence) for active lessons bound to a failure signature."""
    return {row["signature"]: (row["id"], render(row, lang)) for row in active_lessons(conn) if row["signature"]}


def count_hint(conn: sqlite3.Connection, lesson_id: int) -> None:
    conn.execute("UPDATE lesson SET hints=hints+1 WHERE id=?", (lesson_id,))


def _rate(conn, signature: str, tool: str, start: str, end: str, days: int) -> tuple[float | None, int, int]:
    failures = conn.execute("SELECT COALESCE(SUM(count),0) FROM failure_daily WHERE signature=? AND day BETWEEN ? AND ?",
                            (signature, start, end)).fetchone()[0]
    calls = conn.execute("SELECT COALESCE(SUM(calls),0) FROM tool_daily WHERE tool=? AND day BETWEEN ? AND ?",
                         (tool, start, end)).fetchone()[0]
    if calls >= 5:
        return failures / calls, failures, calls
    return (failures / days if days else None), failures, calls


def evaluate(conn: sqlite3.Connection, *, now: float, mode: str = "suggest") -> list[dict]:
    """Measure each active derived lesson against its failure before and after activation."""
    results = []
    today = utc_day(now)
    for row in conn.execute("SELECT * FROM lesson WHERE status='active' AND signature IS NOT NULL AND activated IS NOT NULL"):
        activated_day = utc_day(row["activated"])
        days_after = int((now - row["activated"]) // 86_400)
        before, failures_before, calls_before = _rate(
            conn, row["signature"], row["tool"], day_offset(row["activated"], BASELINE_DAYS), day_offset(row["activated"], 1),
            BASELINE_DAYS)
        # The activation day is mixed (partly before the lesson); it is left out of both windows.
        after_start = day_offset(row["activated"], -1)
        after, failures_after, calls_after = _rate(conn, row["signature"], row["tool"], after_start, today, max(days_after, 1))
        per_call = calls_before >= 5 and calls_after >= 5
        if not per_call:   # compare per day on both sides when one side lacks call volume
            before = failures_before / BASELINE_DAYS
            after = failures_after / max(days_after, 1)
        ready = days_after >= EVALUATE_AFTER_DAYS or calls_after >= EVALUATE_AFTER_CALLS
        verdict = None
        if ready and activated_day < today:
            if failures_before == 0 and failures_after == 0:
                verdict = "inconclusive"
            elif after <= 0.5 * before:
                verdict = "effective"
            elif failures_after >= 3 and after >= 0.9 * before:
                verdict = "ineffective"
            else:
                verdict = "inconclusive"
        retire = verdict == "ineffective" and days_after >= RETIRE_AFTER_DAYS and mode == "auto"
        if verdict:
            conn.execute("UPDATE lesson SET verdict=?, rate_before=?, rate_after=?, evaluated=?, status=?, updated=? WHERE id=?",
                         (verdict, before, after, now, "retired" if retire else "active",
                          now if retire or verdict != row["verdict"] else row["updated"], row["id"]))
        results.append({"id": row["id"], "tool": row["tool"], "class": row["klass"], "verdict": verdict,
                        "basis": "failures_per_call" if per_call else "failures_per_day",
                        "before": round(before, 4), "after": round(after, 4), "failures_before": failures_before,
                        "failures_after": failures_after, "days_active": days_after, "retired": retire})
    return results


def listing(conn: sqlite3.Connection, *, lang: str = "en", status: str | None = None) -> list[dict]:
    rows = conn.execute("""SELECT l.*, COALESCE(f.total, 0) AS evidence, COALESCE(f.sessions, 0) AS sessions FROM lesson l
        LEFT JOIN failure f ON f.signature=l.signature WHERE (? IS NULL OR l.status=?)
        ORDER BY CASE l.status WHEN 'active' THEN 0 WHEN 'candidate' THEN 1 WHEN 'quarantined' THEN 2 ELSE 3 END,
          evidence DESC, l.id""", (status, status)).fetchall()
    return [{"id": row["id"], "status": row["status"], "origin": row["origin"], "tool": row["tool"], "class": row["klass"],
             "lesson": render(row, lang), "failures_seen": row["evidence"], "sessions": row["sessions"],
             "shown_in_sessions": row["shown"], "hints_given": row["hints"], "verdict": row["verdict"],
             "rate_before": row["rate_before"], "rate_after": row["rate_after"], "activated": row["activated"]}
            for row in rows]


def search(conn: sqlite3.Connection, *, tool: str | None = None, lang: str = "en", limit: int = 5) -> list[dict]:
    """Active lessons for one tool (or all), most evidenced first. Used by the agent-facing tool."""
    rows = [row for row in active_lessons(conn) if tool is None or row["tool"] == tool or row["tool"] == "general"]
    return [{"id": row["id"], "tool": row["tool"], "lesson": render(row, lang)} for row in rows[:max(1, min(limit, 10))]]
