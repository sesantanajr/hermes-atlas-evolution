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
import unicodedata

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
        "`{tool}`: this kind of command is blocked by the safety policy. Do not retry it or look for another way to the "
        "same result; tell the user what you needed and let them decide.",
        "`{tool}`: esse tipo de comando é bloqueado pela política de segurança. Não repita nem procure outro caminho para o "
        "mesmo resultado; diga ao usuário do que precisava e deixe que ele decida."),
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


def clean_text(text) -> str | None:
    """A lesson sentence in canonical form, or None. Control, format (bidi, zero-width) and private-use
    characters are refused rather than stripped, so what the operator reads is exactly what the agent gets."""
    if not isinstance(text, str):
        return None
    text = " ".join(unicodedata.normalize("NFKC", text).split())
    if any(unicodedata.category(char) in ("Cc", "Cf", "Co", "Cs", "Zl", "Zp") for char in text):
        return None
    if not _TEXT.fullmatch(text) or _SECRET.search(text):
        return None
    return text


def valid_text(text) -> bool:
    return clean_text(text) is not None


# Advice any failure would get: not worth an approval. A draft that names what it is about in backticks passes.
_GENERIC = re.compile(
    r"\b(?:read|check|review|inspect|look at) the (?:output|error|logs?|message|status)\b|\bbefore (?:retrying|repeating|trying again)\b"
    r"|\bdiagnose the (?:cause|problem)\b|\bleia (?:a saída|o erro|os logs?|a mensagem)\b|\bverifique (?:o status|a saída|o erro|os logs?)\b"
    r"|\bantes de (?:repetir|tentar de novo|tentar novamente)\b|\bdiagnosticar a causa\b", re.I)


def generic_advice(text: str) -> bool:
    """True for a sentence that only gives advice every failure would get (read the error, check before retrying)."""
    return isinstance(text, str) and bool(_GENERIC.search(text)) and "`" not in text


def _record_change(conn, lesson_id: int, now: float, subject: str) -> None:
    conn.execute("""INSERT OR IGNORE INTO change(ref, kind, subject, detail, at) VALUES (?, 'lesson', ?, '{}', ?)""",
                 (f"lesson:{lesson_id}:{int(now)}", subject, now))


OWNER_ACTIVATION = 2   # failures in the owner's own turns before a derived lesson may activate by itself


def derive(conn: sqlite3.Connection, *, now: float, mode: str = "suggest") -> list[int]:
    """Create lessons for recurring failures that have a template. Returns the new lesson ids.

    In auto mode a lesson activates by itself only when the owner's own turns hit the failure at least
    OWNER_ACTIVATION times. One that only chats with other people caused waits, so nobody else can put a
    lesson in the owner's prompt: for the owner's approval, or until the owner's own turns hit it too (promote)."""
    if mode not in MODES or mode == "off":
        return []
    rows = conn.execute("""SELECT f.signature, f.tool, f.klass, f.hint, f.total, f.sessions,
          (SELECT COUNT(*) FROM failure_daily d WHERE d.signature=f.signature) AS days,
          COALESCE((SELECT SUM(o.count) FROM failure_owner_daily o WHERE o.signature=f.signature), 0) AS owner_total
        FROM failure f WHERE f.total >= ? AND NOT EXISTS (SELECT 1 FROM lesson l WHERE l.signature=f.signature)
        ORDER BY f.total DESC LIMIT 200""", (MIN_TOTAL,)).fetchall()
    created = []
    rewriters = rewriter_binaries(conn)
    taken = {_text_key(row["tool"], row["klass"], row["hint"])
             for row in conn.execute("SELECT tool, klass, hint FROM lesson WHERE origin='derived' AND status != 'retired'")}
    for row in rows:
        template = TEMPLATES.get(row["klass"])
        if template is None or template[0] and not row["hint"] or row["tool"] == "unknown":
            continue
        if row["sessions"] < 2 and row["days"] < 2:
            continue   # one bad session is not a pattern yet
        if row["klass"] == "missing_dependency" and row["hint"] in rewriters:
            continue   # another plugin rewrote the command into this binary: the agent never called it
        key = _text_key(row["tool"], row["klass"], row["hint"])
        if key in taken:
            continue   # the same sentence is already in the playbook or waiting for a decision
        taken.add(key)
        status = "active" if mode == "auto" and row["owner_total"] >= OWNER_ACTIVATION else "candidate"
        # OR IGNORE: another process may have derived the same lesson since the SELECT above.
        cursor = conn.execute("""INSERT OR IGNORE INTO lesson(signature, tool, klass, hint, origin, status, created, updated,
              activated) VALUES (?, ?, ?, ?, 'derived', ?, ?, ?, ?)""",
                              (row["signature"], row["tool"], row["klass"], row["hint"], status, now, now,
                               now if status == "active" else None))
        if not cursor.rowcount:
            continue
        created.append(cursor.lastrowid)
        if status == "active":
            _record_change(conn, cursor.lastrowid, now, row["tool"])
        elif mode == "auto":   # waits only for the owner's own evidence
            conn.execute("INSERT OR REPLACE INTO lesson_owner_wait VALUES (?, ?)", (cursor.lastrowid, row["signature"]))
    return created


def promote(conn: sqlite3.Connection, *, now: float, mode: str) -> list[int]:
    """In auto mode, activate the derived lessons that waited only for the owner's own turns to hit their failure
    OWNER_ACTIVATION times, now that they have. Returns their ids.

    Only lessons derived in auto mode wait this way: a candidate from suggest mode or from a version before
    0.7.0 keeps waiting for the operator, and one the operator retired or rejected is never touched."""
    conn.execute("""DELETE FROM lesson_owner_wait WHERE NOT EXISTS (SELECT 1 FROM lesson l WHERE l.id=lesson_id
        AND l.signature=lesson_owner_wait.signature AND l.status='candidate' AND l.origin='derived')""")
    if mode != "auto":
        return []
    rows = conn.execute("""SELECT w.lesson_id FROM lesson_owner_wait w WHERE
        COALESCE((SELECT SUM(o.count) FROM failure_owner_daily o WHERE o.signature=w.signature), 0) >= ?
        ORDER BY w.lesson_id""", (OWNER_ACTIVATION,)).fetchall()
    promoted = [row[0] for row in rows]
    for lesson_id in promoted:
        set_status(conn, lesson_id, "active", now=now)
    conn.executemany("DELETE FROM lesson_owner_wait WHERE lesson_id=?", [(item,) for item in promoted])
    return promoted


def _text_key(tool: str, klass: str, hint: str) -> str:
    """The sentence a derived lesson renders (in English), so two failures that read the same are one lesson."""
    template = TEMPLATES.get(klass)
    return template[1].format(tool=tool, hint=hint) if template else f"{tool}\0{klass}\0{hint}"


def rewriter_binaries(conn: sqlite3.Connection) -> set[str]:
    """Binaries seen missing only after another plugin rewrote the command (`rewritten_command` failures)."""
    return {row[0] for row in conn.execute(
        "SELECT DISTINCT hint FROM failure WHERE klass='rewritten_command' AND hint != ''").fetchall()}


def tidy(conn: sqlite3.Connection) -> int:
    """Withdraw lessons nobody decided on yet that the evidence no longer supports. Returns how many.

    A derived candidate or a reflector draft goes when its failure no longer meets the rule that made it
    (seen at least MIN_TOTAL times, in 2 sessions or on 2 days; a replay that recounted the history can
    lower it), when it would say the same sentence as another lesson, when its missing binary turned out to
    come from another plugin's rewrite, or, for a draft, when it only gives advice every failure would get. Active, retired and operator lessons are never touched. If
    the failure comes back, the lesson is derived again. Run it only when the history replay is settled."""
    rewriters = rewriter_binaries(conn)
    taken = {_text_key(row["tool"], row["klass"], row["hint"]) for row in conn.execute(
        "SELECT tool, klass, hint FROM lesson WHERE origin='derived' AND status='active'")}
    rows = conn.execute("""SELECT l.id, l.tool, l.klass, l.hint, l.origin, l.text,
          COALESCE(f.total, 0) AS total, COALESCE(f.sessions, 0) AS sessions,
          (SELECT COUNT(*) FROM failure_daily d WHERE d.signature=l.signature) AS days
        FROM lesson l LEFT JOIN failure f ON f.signature=l.signature
        WHERE (l.status='candidate' AND l.origin='derived') OR (l.status='quarantined' AND l.origin='reflector')
        ORDER BY total DESC, l.id""").fetchall()
    withdrawn = []
    for row in rows:
        stale = row["total"] < MIN_TOTAL or row["sessions"] < 2 and row["days"] < 2
        foreign = row["klass"] == "missing_dependency" and row["hint"] in rewriters
        bland = row["origin"] == "reflector" and generic_advice(row["text"])
        key = _text_key(row["tool"], row["klass"], row["hint"]) if row["origin"] == "derived" else None
        if stale or foreign or bland or key in taken:
            withdrawn.append(row["id"])
        elif key is not None:
            taken.add(key)
    conn.executemany("DELETE FROM lesson WHERE id=?", [(item,) for item in withdrawn])
    conn.executemany("DELETE FROM lesson_owner_wait WHERE lesson_id=?", [(item,) for item in withdrawn])
    return len(withdrawn)


def add_custom(conn: sqlite3.Connection, text: str, *, tool: str, origin: str, now: float, active: bool = False) -> int:
    """Store an operator, agent or reflector lesson. Agent and reflector lessons start quarantined."""
    if origin not in ORIGINS[1:]:
        raise ValueError("Unsupported lesson origin")
    text = clean_text(text)
    if text is None:
        raise ValueError(f"A lesson is one sentence of 12 to {TEXT_LIMIT} characters without secrets")
    if not isinstance(tool, str) or not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.:+-]{0,95}", tool):
        raise ValueError("Name the tool this lesson applies to, or use 'general'")
    limit = conn.execute("SELECT COUNT(*) FROM lesson WHERE origin != 'derived' AND status != 'retired'").fetchone()[0]
    if limit >= 200:
        raise ValueError("The playbook already holds 200 custom lessons; retire some first")
    status = "active" if active and origin == "operator" else "quarantined"
    cursor = conn.execute("""INSERT INTO lesson(tool, klass, hint, origin, status, text, created, updated, activated)
        VALUES (?, 'custom', '', ?, ?, ?, ?, ?, ?)""",
                          (tool, origin, status, text, now, now, now if status == "active" else None))
    if status == "active":
        _record_change(conn, cursor.lastrowid, now, tool)
    return cursor.lastrowid


def add_reflected(conn: sqlite3.Connection, *, signature: str, tool: str, klass: str, hint: str, text: str,
                  now: float) -> int:
    """Store a reflector draft bound to its failure. It stays quarantined until an operator approves it."""
    text = clean_text(text)
    if text is None:
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
                     " verdict=CASE WHEN ?='active' THEN NULL ELSE verdict END,"
                     " baseline_failures=CASE WHEN ?='active' THEN NULL ELSE baseline_failures END,"
                     " baseline_calls=CASE WHEN ?='active' THEN NULL ELSE baseline_calls END WHERE id=?",
                     (status, now, status, now, status, status, status, lesson_id))
        if status == "active":
            _record_change(conn, lesson_id, now, row["tool"])
    return {"id": lesson_id, "status": status, "previous": row["status"]}


def active_lessons(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("""SELECT l.*, COALESCE(f.total, 0) AS evidence FROM lesson l
        LEFT JOIN failure f ON f.signature=l.signature WHERE l.status='active'
        ORDER BY (l.verdict='effective') DESC, evidence DESC, l.id""").fetchall()


def templated(row) -> bool:
    """A derived lesson in its template wording: fixed text and an identifier-like detail, nothing free-form."""
    return row["origin"] == "derived" and not row["text"]


def playbook(conn: sqlite3.Connection, *, lang: str = "en", limit: int = 8, max_chars: int = 1200,
             mark_shown: bool = False, chosen: list | None = None, templates_only: bool = False) -> str:
    """Bounded text for the session prompt section. Empty when there is nothing active.

    `chosen`, when given, receives the ids of the lessons in the text (so a caller can count them later
    without writing during the read). `templates_only` (a chat where someone else may be on the other side)
    leaves out lessons written by the operator, the reflector or the agent: free text that was never meant
    for a third party."""
    lines, used = [], len(HEADERS.get(lang, HEADERS["en"]))
    chosen = [] if chosen is None else chosen
    written: set[str] = set()
    for row in active_lessons(conn):
        if templates_only and not templated(row):
            continue
        sentence = render(row, lang)
        if sentence in written:   # one template, several failure details: the sentence is already there
            chosen.append(row["id"])
            continue
        if not sentence or used + len(sentence) + 3 > max_chars:
            continue
        written.add(sentence)
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


def hint_index(conn: sqlite3.Connection, *, lang: str = "en") -> dict[str, tuple[int, str, bool]]:
    """signature -> (lesson id, sentence, templated) for active lessons bound to a failure signature."""
    return {row["signature"]: (row["id"], render(row, lang), templated(row))
            for row in active_lessons(conn) if row["signature"]}


def count_hint(conn: sqlite3.Connection, lesson_id: int) -> None:
    conn.execute("UPDATE lesson SET hints=hints+1 WHERE id=?", (lesson_id,))


AFTER_WINDOW_DAYS = 14


def _counts(conn, signature: str, tool: str, start: str, end: str) -> tuple[int, int]:
    failures = conn.execute("SELECT COALESCE(SUM(count),0) FROM failure_daily WHERE signature=? AND day BETWEEN ? AND ?",
                            (signature, start, end)).fetchone()[0]
    calls = conn.execute("SELECT COALESCE(SUM(calls),0) FROM tool_daily WHERE tool=? AND day BETWEEN ? AND ?",
                         (tool, start, end)).fetchone()[0]
    return failures, calls


def _calendar_days(start: str, end: str) -> int:
    from datetime import date

    return max(0, (date.fromisoformat(end) - date.fromisoformat(start)).days + 1)


def evaluate(conn: sqlite3.Connection, *, now: float, mode: str = "suggest", history_until: float | None = None) -> list[dict]:
    """Measure each active lesson bound to a failure against that failure before and after activation.

    The baseline is the 14 days before activation, counted once and frozen, so retention never erases it.
    While the backfill is still replaying Hermes' history up to `history_until`, a baseline that reaches
    into that history waits: frozen now, it would hold only part of it.
    The after window is the latest 14 days since activation (the mixed activation day is left out of both).
    Rates are per call when both sides have call volume, otherwise per calendar day of each window."""
    results = []
    today = utc_day(now)
    for row in conn.execute("SELECT * FROM lesson WHERE status='active' AND signature IS NOT NULL AND activated IS NOT NULL").fetchall():
        activated_day = utc_day(row["activated"])
        base_failures, base_calls = row["baseline_failures"], row["baseline_calls"]
        if base_failures is None or base_calls is None:
            if history_until is not None and row["activated"] - BASELINE_DAYS * 86_400 < history_until:
                continue
            base_failures, base_calls = _counts(conn, row["signature"], row["tool"], day_offset(row["activated"], BASELINE_DAYS),
                                                day_offset(row["activated"], 1))
            conn.execute("UPDATE lesson SET baseline_failures=?, baseline_calls=? WHERE id=?",
                         (base_failures, base_calls, row["id"]))
        after_start = max(day_offset(row["activated"], -1), day_offset(now, AFTER_WINDOW_DAYS - 1))
        days_after = _calendar_days(after_start, today) if after_start <= today else 0
        failures_after, calls_after = _counts(conn, row["signature"], row["tool"], after_start, today) if days_after else (0, 0)
        per_call = base_calls >= 5 and calls_after >= 5
        if per_call:
            before, after = base_failures / base_calls, failures_after / calls_after
        else:
            before, after = base_failures / BASELINE_DAYS, failures_after / days_after if days_after else 0.0
        days_active = _calendar_days(day_offset(row["activated"], -1), today) if activated_day < today else 0
        ready = days_active >= EVALUATE_AFTER_DAYS or calls_after >= EVALUATE_AFTER_CALLS
        verdict = None
        if ready and days_after:
            if base_failures == 0 and failures_after == 0:
                verdict = "inconclusive"
            elif after <= 0.5 * before:
                verdict = "effective"
            elif failures_after >= 3 and after >= 0.9 * before:
                verdict = "ineffective"
            else:
                verdict = "inconclusive"
        retire = verdict == "ineffective" and days_active >= RETIRE_AFTER_DAYS and mode == "auto"
        if verdict:
            conn.execute("UPDATE lesson SET verdict=?, rate_before=?, rate_after=?, evaluated=?, status=?, updated=? WHERE id=?",
                         (verdict, before, after, now, "retired" if retire else "active",
                          now if retire or verdict != row["verdict"] else row["updated"], row["id"]))
        results.append({"id": row["id"], "tool": row["tool"], "class": row["klass"], "verdict": verdict,
                        "basis": "failures_per_call" if per_call else "failures_per_day",
                        "before": round(before, 4), "after": round(after, 4), "failures_before": base_failures,
                        "failures_after": failures_after, "days_active": days_active, "retired": retire})
    return results


def listing(conn: sqlite3.Connection, *, lang: str = "en", status: str | None = None) -> list[dict]:
    rows = conn.execute("""SELECT l.*, COALESCE(f.total, 0) AS evidence, COALESCE(f.sessions, 0) AS sessions,
          COALESCE((SELECT SUM(o.count) FROM failure_owner_daily o WHERE o.signature=l.signature), 0) AS owner_evidence
        FROM lesson l
        LEFT JOIN failure f ON f.signature=l.signature WHERE (? IS NULL OR l.status=?)
        ORDER BY CASE l.status WHEN 'active' THEN 0 WHEN 'candidate' THEN 1 WHEN 'quarantined' THEN 2 ELSE 3 END,
          evidence DESC, l.id""", (status, status)).fetchall()
    return [{"id": row["id"], "status": row["status"], "origin": row["origin"], "tool": row["tool"], "class": row["klass"],
             "lesson": render(row, lang), "failures_seen": row["evidence"], "sessions": row["sessions"],
             "failures_in_owner_turns": row["owner_evidence"],
             "shown_in_sessions": row["shown"], "hints_given": row["hints"], "verdict": row["verdict"],
             "rate_before": row["rate_before"], "rate_after": row["rate_after"], "activated": row["activated"]}
            for row in rows]


def search(conn: sqlite3.Connection, *, tool: str | None = None, lang: str = "en", limit: int = 5) -> list[dict]:
    """Active lessons for one tool (or all), most evidenced first. Used by the agent-facing tool."""
    rows = [row for row in active_lessons(conn) if tool is None or row["tool"] == tool or row["tool"] == "general"]
    return [{"id": row["id"], "tool": row["tool"], "lesson": render(row, lang)} for row in rows[:max(1, min(limit, 10))]]
