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

from .experience import RETENTION_DAYS, day_offset, utc_day

MODES = ("off", "suggest", "auto")
LANGUAGES = ("en", "pt")
STATUSES = ("candidate", "active", "quarantined", "retired")
ORIGINS = ("derived", "operator", "agent", "reflector")
MIN_TOTAL = 3
BASELINE_DAYS = 14
EVALUATE_AFTER_DAYS = 5
EVALUATE_AFTER_CALLS = 20
RETIRE_AFTER_DAYS = 14
DERIVE_BATCH = 200   # failures derive reads per pass, most frequent first, among those a template can describe
TEXT_LIMIT = 240
_TEXT = re.compile(r"[^\x00-\x08\x0b-\x1f\x7f]{12,%d}\Z" % TEXT_LIMIT)
_SECRET = re.compile(r"(?:sk-|sk_live_|sk_test_|rk_live_|rk_test_|ghp_|gho_|ghs_|ghr_|ghu_|github_pat_|glpat-|"
                     r"xox[a-z]-|AKIA|AIza|ya29\.)[A-Za-z0-9_.-]{8,}|npm_[A-Za-z0-9]{16,}|://[^\s/:@]+:[^\s/@]+@")
# An opaque token: a long run of token characters with a part (between separators) where letters and digits keep
# alternating, as in a key or a password. Names joined by separators, such as a tool name, `System32` in a path or
# a `--flag=4096`, are not one.
OPAQUE_RUN = 24
OPAQUE_PART = 8
OPAQUE_ALTERNATIONS = 3
_TOKEN_RUN = re.compile(r"[A-Za-z0-9_\-+/=.]{%d,}" % OPAQUE_RUN)
_TOKEN_PART = re.compile(r"[^_\-+/=.]{%d,}" % OPAQUE_PART)
_ALTERNATION = re.compile(r"(?<=[A-Za-z])(?=[0-9])|(?<=[0-9])(?=[A-Za-z])")

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
# The header when a shown lesson was written, not derived from a failure (by the owner, the reflector or the agent).
MIXED_HEADERS = {
    "en": "Lessons of this profile (Atlas Evolution), from repeated tool failures and from its owner. Apply them when "
          "relevant:",
    "pt": "Lições deste perfil (Atlas Evolution), de falhas repetidas de ferramentas e do seu dono. Aplique quando couber:",
}
HINT_LABEL = {"en": "Atlas Evolution lesson", "pt": "Lição do Atlas Evolution"}
# The lessons a chat prompt leaves out (written by the owner, the reflector or the agent), given once to a chat
# session whose sender turned out to be the owner (integration: pre_llm_call context).
OWNER_HEADERS = {
    "en": "More lessons this profile learned (Atlas Evolution), for the owner only. Apply them when relevant and do not "
          "quote them to anyone else:",
    "pt": "Mais lições que este perfil aprendeu (Atlas Evolution), só para o dono. Aplique quando couber e não as repita "
          "para outras pessoas:",
}
OWNER_LIMIT, OWNER_MAX_CHARS = 5, 900


def render(row, lang: str = "en") -> str:
    """Human and agent-facing sentence for one lesson."""
    if row["text"]:
        return row["text"]
    template = TEMPLATES.get(row["klass"])
    if template is None:
        return ""
    return template[2 if lang == "pt" else 1].format(tool=row["tool"], hint=row["hint"])


def _opaque(text: str) -> bool:
    """True when the text holds a token-like run (a key, a password, a session id) rather than words."""
    return any(len(_ALTERNATION.findall(part)) >= OPAQUE_ALTERNATIONS
               for run in _TOKEN_RUN.findall(text) for part in _TOKEN_PART.findall(run))


def _redactor_changes(text: str) -> bool:
    """True when Hermes' own secret redactor would change the text. False outside Hermes; a redactor that fails
    counts as a change, so a failure never lets the text through."""
    try:
        from .signals import _native_redactor

        redact = _native_redactor()
    except Exception:   # outside Hermes, or a plugin copy without it: the patterns of this module still apply
        return False
    if redact is None:
        return False
    try:
        return redact(text, force=True) != text   # force: even when the profile turned redaction off
    except Exception:
        return True


def clean_text(text) -> str | None:
    """A lesson sentence in canonical form, or None. Control, format (bidi, zero-width) and private-use
    characters are refused rather than stripped, so what the operator reads is exactly what the agent gets.
    Text that looks like it carries a credential is refused: a known key prefix, credentials in a URL, an opaque
    token, or anything Hermes' own redactor would hide."""
    if not isinstance(text, str):
        return None
    text = " ".join(unicodedata.normalize("NFKC", text).split())
    if any(unicodedata.category(char) in ("Cc", "Cf", "Co", "Cs", "Zl", "Zp") for char in text):
        return None
    if not _TEXT.fullmatch(text) or _SECRET.search(text) or _opaque(text) or _redactor_changes(text):
        return None
    return text


def valid_text(text) -> bool:
    return clean_text(text) is not None


# Advice any failure would get: not worth an approval. A draft is generic when nothing but such advice is left
# once its condition ("When X,") is set aside: reading the output or the error, checking before retrying,
# diagnosing the cause. A draft that also says what to do (delete a lock file, pass a flag, use another tool) is
# specific, and so is one that names something in backticks.
_CONDITION = re.compile(r"\s*(?:when(?:ever)?|if|after|quando|se|sempre\s+que|ao|após)\b[^,;:]*[,;:]\s*", re.I)
_SAME_CALL = (r"(?:\s+(?:it|that|the\s+(?:same\s+)?(?:call|command|request|step|operation)|a\s+(?:mesma\s+)?(?:chamada|operação)"
              r"|o\s+(?:mesmo\s+)?(?:comando|pedido|passo)))?")
_GENERIC_PHRASES = re.compile(
    r"\b(?:read|reading|check|checking|review|reviewing|inspect|inspecting|look\s+at|examine|verify|confirm)\s+"
    r"(?:the\s+)?(?:full\s+|exact\s+|whole\s+)?(?:command\s+)?"
    r"(?:output|error(?:\s+(?:message|output))?s?|logs?|messages?|status|results?|exit\s+code|stderr|stdout)\b"
    r"|\b(?:diagnose|diagnosing|investigate|investigating|identify|identifying|find|finding|understand|understanding"
    r"|determine|determining)\s+(?:the\s+)?(?:root\s+)?(?:cause|problem|issue|error|reason)\b"
    r"|\b(?:fix|fixing|address|resolve|correct|handle)\s+(?:it|that|this|the\s+(?:root\s+)?(?:cause|problem|issue|error))\b"
    r"|\bbefore\s+(?:retrying|repeating|trying\s+again|running\s+it\s+again|you\s+retry|re-?running|calling\s+it\s+again)"
    + _SAME_CALL + r"\b"
    r"|\b(?:do\s+not|don't|never)\s+(?:retry|repeat)(?:\s+it)?\s+blindly\b|\binstead\s+of\s+retrying\s+blindly\b"
    r"|\b(?:leia|ler|verifique|verificar|revise|revisar|inspecione|inspecionar|examine|examinar|confira|conferir|olhe"
    r"|olhar|confirme|confirmar)\s+(?:a\s+|o\s+|os\s+|as\s+)?"
    r"(?:saída|erro|mensagem(?:\s+de\s+erro)?|logs?|status|resultado|código\s+de\s+saída)\b"
    r"|\b(?:diagnostique|diagnosticar|investigue|investigar|identifique|identificar|entenda|entender|descubra|descobrir)"
    r"\s+(?:a\s+|o\s+)?(?:causa|problema|erro|motivo)(?:\s+raiz)?\b"
    r"|\b(?:corrija|corrigir|resolva|resolver|trate|tratar)\s+(?:isso|o\s+problema|a\s+causa|o\s+erro)\b"
    r"|\bantes\s+de\s+(?:repetir|tentar\s+de\s+novo|tentar\s+novamente|rodar\s+de\s+novo|executar\s+de\s+novo"
    r"|chamar\s+de\s+novo)" + _SAME_CALL + r"\b"
    r"|\bnão\s+(?:repita|tente\s+de\s+novo)\s+às\s+cegas\b", re.I)
_FILLER = re.compile(r"\b(?:and|then|always|first|carefully|again|it|the|a|an|to|so|in\s+order|e|depois|sempre|"
                     r"primeiro|com|atenção|isso|o|a|os|as|para|de|novo)\b|[\W_]+", re.I)


def generic_advice(text: str) -> bool:
    """True for a sentence that only gives advice every failure would get (read the error, check before retrying)."""
    if not isinstance(text, str) or "`" in text:
        return False
    condition = _CONDITION.match(text)
    advice = text[condition.end():] if condition else text
    if not _GENERIC_PHRASES.search(advice):
        return False
    return not _FILLER.sub(" ", _GENERIC_PHRASES.sub(" ", advice)).strip()


def _record_change(conn, lesson_id: int, now: float, subject: str) -> None:
    """Every activation passes here (`now` is the moment it took effect). Besides the tracked change, it keeps the
    failures and tool calls of the activation day so far: the baseline (evaluate) is counted later, from whole
    days before that day, and without them a lesson activated right after a one-day burst would have none."""
    conn.execute("""INSERT OR IGNORE INTO change(ref, kind, subject, detail, at) VALUES (?, 'lesson', ?, '{}', ?)""",
                 (f"lesson:{lesson_id}:{int(now)}", subject, now))
    row = conn.execute("SELECT signature, tool FROM lesson WHERE id=?", (lesson_id,)).fetchone()
    if row is not None and row[0] is not None:
        day = utc_day(now)
        failures, calls = _counts(conn, row[0], row[1], day, day)
        conn.execute("INSERT OR REPLACE INTO lesson_activation VALUES (?, ?, ?, ?)", (lesson_id, now, failures, calls))


OWNER_ACTIVATION = 2   # failures in the owner's own turns before a derived lesson may activate by itself
WITHDRAWN = "withdrawn"   # verdict of a lesson tidy took back: kept as retired so its id is never reused


def _text_key(tool: str, klass: str, hint: str) -> str:
    """The sentence a derived lesson renders (in English), so two failures that read the same are one lesson."""
    template = TEMPLATES.get(klass)
    return template[1].format(tool=tool, hint=hint) if template else f"{tool}\0{klass}\0{hint}"


def _owner_by_key(conn: sqlite3.Connection) -> dict[str, int]:
    """Failures in the owner's own turns per lesson sentence: failures that read the same share their evidence."""
    owner = dict(conn.execute("SELECT signature, SUM(count) FROM failure_owner_daily GROUP BY signature").fetchall())
    found: dict[str, int] = {}
    if owner:
        for signature, tool, klass, hint in conn.execute("SELECT signature, tool, klass, hint FROM failure").fetchall():
            if owner.get(signature):
                key = _text_key(tool, klass, hint)
                found[key] = found.get(key, 0) + owner[signature]
    return found


def _decided_keys(conn: sqlite3.Connection, *, waiting: bool) -> set[str]:
    """Sentences of derived lessons that are active, retired by the operator or for not helping, and, with
    `waiting`, still waiting for a decision: another failure that reads the same never brings them back.
    Withdrawn lessons do not count."""
    statuses = "('active', 'candidate', 'retired')" if waiting else "('active', 'retired')"
    return {_text_key(row[0], row[1], row[2]) for row in conn.execute(
        f"""SELECT tool, klass, hint FROM lesson WHERE origin='derived' AND status IN {statuses}
            AND NOT (status='retired' AND verdict IS '{WITHDRAWN}')""")}


def derive(conn: sqlite3.Connection, *, now: float, mode: str = "suggest") -> list[int]:
    """Create lessons for recurring failures that have a template, and bring back a withdrawn lesson whose
    failure recurs (same id, same text). Returns the ids of the lessons created or brought back.

    In auto mode a lesson activates by itself only when the owner's own turns hit the failure (or another that
    reads the same) at least OWNER_ACTIVATION times. One that only chats with other people caused waits, so
    nobody else can put a lesson in the owner's prompt: for the owner's approval, or until the owner's own turns
    hit it too (promote)."""
    if mode not in MODES or mode == "off":
        return []
    # Only failures a template can describe are read, so the batch is never filled with ones that cannot become a
    # lesson (no template, no detail where the template needs one, no tool name, one session on one day).
    classes = list(TEMPLATES)
    detailed = [klass for klass, template in TEMPLATES.items() if template[0]]
    rows = conn.execute(f"""SELECT f.signature, f.tool, f.klass, f.hint, f.total, f.sessions,
          (SELECT COUNT(*) FROM failure_daily d WHERE d.signature=f.signature) AS days, l.id AS lesson_id
        FROM failure f LEFT JOIN lesson l ON l.signature=f.signature
        WHERE f.total >= ? AND f.tool != 'unknown' AND f.klass IN ({",".join("?" * len(classes))})
          AND (f.hint != '' OR f.klass NOT IN ({",".join("?" * len(detailed))}))
          AND (f.sessions >= 2 OR (SELECT COUNT(*) FROM failure_daily d WHERE d.signature=f.signature) >= 2)
          AND (l.id IS NULL OR l.origin='derived' AND l.status='retired' AND l.verdict IS '{WITHDRAWN}')
        ORDER BY f.total DESC LIMIT ?""", (MIN_TOTAL, *classes, *detailed, DERIVE_BATCH)).fetchall()
    created = []
    rewriters = rewriter_binaries(conn)
    taken = _decided_keys(conn, waiting=True)
    owner = None
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
            continue   # the same sentence is already in the playbook, waiting, or was turned down
        taken.add(key)
        owner = _owner_by_key(conn) if owner is None else owner
        status = "active" if mode == "auto" and owner.get(key, 0) >= OWNER_ACTIVATION else "candidate"
        activated = now if status == "active" else None
        if row["lesson_id"] is not None:   # withdrawn when its evidence faded, and the failure is back
            cursor = conn.execute(f"""UPDATE lesson SET status=?, verdict=NULL, updated=?, activated=?, rate_before=NULL,
                  rate_after=NULL, evaluated=NULL, baseline_failures=NULL, baseline_calls=NULL
                WHERE id=? AND status='retired' AND verdict IS '{WITHDRAWN}'""", (status, now, activated, row["lesson_id"]))
            lesson_id = row["lesson_id"]
        else:   # OR IGNORE: another process may have derived the same lesson since the SELECT above
            cursor = conn.execute("""INSERT OR IGNORE INTO lesson(signature, tool, klass, hint, origin, status, created,
                  updated, activated) VALUES (?, ?, ?, ?, 'derived', ?, ?, ?, ?)""",
                                  (row["signature"], row["tool"], row["klass"], row["hint"], status, now, now, activated))
            lesson_id = cursor.lastrowid
        if not cursor.rowcount:
            continue
        created.append(lesson_id)
        if status == "active":
            _record_change(conn, lesson_id, now, row["tool"])
        elif mode == "auto":   # waits only for the owner's own evidence
            conn.execute("INSERT OR REPLACE INTO lesson_owner_wait VALUES (?, ?)", (lesson_id, row["signature"]))
    # A reflector draft withdrawn when its evidence faded goes back to the queue once its failure meets the rule again.
    for row in conn.execute(f"""SELECT l.id, l.text, f.sessions,
          (SELECT COUNT(*) FROM failure_daily d WHERE d.signature=l.signature) AS days
        FROM lesson l JOIN failure f ON f.signature=l.signature
        WHERE l.origin='reflector' AND l.status='retired' AND l.verdict IS '{WITHDRAWN}' AND f.total >= ?""",
                            (MIN_TOTAL,)).fetchall():
        if (row["sessions"] >= 2 or row["days"] >= 2) and not generic_advice(row["text"]):
            conn.execute(f"""UPDATE lesson SET status='quarantined', verdict=NULL, updated=? WHERE id=? AND status='retired'
                AND verdict IS '{WITHDRAWN}'""", (now, row["id"]))
            created.append(row["id"])
    return created


def promote(conn: sqlite3.Connection, *, now: float, mode: str) -> list[int]:
    """In auto mode, activate the derived lessons that waited only for the owner's own turns to hit their failure
    (or another that reads the same) OWNER_ACTIVATION times, now that they have. Returns their ids.

    Only lessons derived in auto mode wait this way: a candidate from suggest mode or from a version before
    0.7.0 keeps waiting for the operator, and one the operator retired or rejected is never touched. Everything
    is read before anything is written, so the write lock is held only for the changes."""
    rows = conn.execute("""SELECT w.lesson_id, w.signature, l.signature AS current, l.tool, l.klass, l.hint, l.status,
          l.origin FROM lesson_owner_wait w LEFT JOIN lesson l ON l.id=w.lesson_id""").fetchall()
    if not rows:
        return []
    stale = [row["lesson_id"] for row in rows
             if row["status"] != "candidate" or row["origin"] != "derived" or row["current"] != row["signature"]]
    promoted = []
    if mode == "auto":
        owner = _owner_by_key(conn)
        promoted = [row["lesson_id"] for row in rows if row["lesson_id"] not in stale
                    and owner.get(_text_key(row["tool"], row["klass"], row["hint"]), 0) >= OWNER_ACTIVATION]
    activated = []
    for lesson_id in promoted:   # only if it is still waiting: an operator may have decided on it meanwhile
        cursor = conn.execute("""UPDATE lesson SET status='active', updated=?, activated=?, verdict=NULL,
              baseline_failures=NULL, baseline_calls=NULL WHERE id=? AND status='candidate'""", (now, now, lesson_id))
        if cursor.rowcount:
            _record_change(conn, lesson_id, now, next(row["tool"] for row in rows if row["lesson_id"] == lesson_id))
            activated.append(lesson_id)
    conn.executemany("DELETE FROM lesson_owner_wait WHERE lesson_id=?", [(item,) for item in [*stale, *promoted]])
    return activated


def rewriter_binaries(conn: sqlite3.Connection) -> set[str]:
    """Binaries seen missing only after another plugin rewrote the command (`rewritten_command` failures)."""
    return {row[0] for row in conn.execute(
        "SELECT DISTINCT hint FROM failure WHERE klass='rewritten_command' AND hint != ''").fetchall()}


def tidy(conn: sqlite3.Connection, *, now: float) -> int:
    """Withdraw lessons nobody decided on yet that the evidence no longer supports. Returns how many.

    A derived candidate or a reflector draft goes when its failure no longer meets the rule that made it
    (seen at least MIN_TOTAL times, in 2 sessions or on 2 days; a replay that recounted the history can
    lower it), when it would say the same sentence as another lesson (active, turned down, or a candidate with
    more evidence), when its missing binary turned out to come from another plugin's rewrite, or, for a draft,
    when it only gives advice every failure would get. Active, retired and operator lessons are never touched.
    A lesson a pack brought in (packs.apply) is not withdrawn for lacking local failures: its evidence was
    gathered in another profile, and it waits for the owner's own turns here.
    A withdrawn lesson stays as retired with the verdict WITHDRAWN, so its id is never given to another lesson,
    and derive brings it back, same id and text, if its failure returns. Run it only when the history replay is
    settled."""
    rewriters = rewriter_binaries(conn)
    taken = _decided_keys(conn, waiting=False)
    rows = conn.execute("""SELECT l.id, l.tool, l.klass, l.hint, l.origin, l.text,
          COALESCE(f.total, 0) AS total, COALESCE(f.sessions, 0) AS sessions,
          (SELECT COUNT(*) FROM failure_daily d WHERE d.signature=l.signature) AS days,
          EXISTS (SELECT 1 FROM lesson_import i WHERE i.lesson_id=l.id) AS imported
        FROM lesson l LEFT JOIN failure f ON f.signature=l.signature
        WHERE (l.status='candidate' AND l.origin='derived') OR (l.status='quarantined' AND l.origin='reflector')
        ORDER BY total DESC, l.id""").fetchall()
    withdrawn = []
    for row in rows:
        stale = not row["imported"] and (row["total"] < MIN_TOTAL or row["sessions"] < 2 and row["days"] < 2)
        foreign = row["klass"] == "missing_dependency" and row["hint"] in rewriters
        bland = row["origin"] == "reflector" and generic_advice(row["text"])
        key = _text_key(row["tool"], row["klass"], row["hint"]) if row["origin"] == "derived" else None
        if stale or foreign or bland or key in taken:
            withdrawn.append(row["id"])
        elif key is not None:
            taken.add(key)
    taken_back = []
    for lesson_id in withdrawn:   # only if still undecided: an operator may have decided on it meanwhile
        cursor = conn.execute(f"""UPDATE lesson SET status='retired', verdict='{WITHDRAWN}', updated=?
            WHERE id=? AND status IN ('candidate', 'quarantined')""", (now, lesson_id))
        if cursor.rowcount:
            taken_back.append(lesson_id)
    conn.executemany("DELETE FROM lesson_owner_wait WHERE lesson_id=?", [(item,) for item in taken_back])
    return len(taken_back)


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
    if row["status"] == status == "retired" and row["verdict"] == WITHDRAWN:
        # The queue cleaning had taken it back; now the operator says no: that decision stays, and the lesson no
        # longer comes back when its failure does.
        conn.execute("UPDATE lesson SET verdict=NULL, updated=? WHERE id=?", (now, lesson_id))
    elif row["status"] != status:
        conn.execute("UPDATE lesson SET status=?, updated=?, activated=CASE WHEN ?='active' THEN ? ELSE activated END,"
                     " verdict=CASE WHEN ?='active' THEN NULL ELSE verdict END,"
                     " baseline_failures=CASE WHEN ?='active' THEN NULL ELSE baseline_failures END,"
                     " baseline_calls=CASE WHEN ?='active' THEN NULL ELSE baseline_calls END WHERE id=?",
                     (status, now, status, now, status, status, status, lesson_id))
        if status == "active":
            _record_change(conn, lesson_id, now, row["tool"])
    return {"id": lesson_id, "status": status, "previous": row["status"]}


def keep(conn: sqlite3.Connection, lesson_id: int, *, now: float) -> None:
    """The owner keeps an active lesson whose retirement was proposed: auto mode no longer retires it for this
    activation (evaluate still measures it and reports the verdict). Another activation needs another decision."""
    conn.execute("""INSERT OR REPLACE INTO lesson_keep(lesson_id, activated, at)
        SELECT id, activated, ? FROM lesson WHERE id=? AND status='active' AND activated IS NOT NULL""", (now, lesson_id))


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
    lines, used = [], max(len(HEADERS.get(lang, HEADERS["en"])), len(MIXED_HEADERS.get(lang, MIXED_HEADERS["en"])))
    written_by_hand = False
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
        written_by_hand = written_by_hand or row["origin"] != "derived"
        if len(lines) >= limit:
            break
    if not lines:
        return ""
    if mark_shown:
        import time

        shown(conn, chosen, now=time.time())
    headers = MIXED_HEADERS if written_by_hand else HEADERS   # failures only when every lesson shown came from one
    return headers.get(lang, headers["en"]) + "\n" + "\n".join(lines)


def owner_lessons(conn: sqlite3.Connection, *, lang: str = "en", chosen: list | None = None) -> str:
    """The active lessons a chat with someone else leaves out of the prompt (playbook with templates_only), bounded
    to OWNER_LIMIT lines and OWNER_MAX_CHARS: for a chat the owner turned out to be in. Empty when there are none.
    `chosen` receives their ids."""
    header = OWNER_HEADERS.get(lang, OWNER_HEADERS["en"])
    lines, used, written = [], len(header), set()
    chosen = [] if chosen is None else chosen
    for row in active_lessons(conn):
        if templated(row):
            continue   # already in the chat's prompt
        sentence = render(row, lang)
        if not sentence or sentence in written or used + len(sentence) + 3 > OWNER_MAX_CHARS:
            continue
        written.add(sentence)
        lines.append("- " + sentence)
        used += len(sentence) + 3
        chosen.append(row["id"])
        if len(lines) >= OWNER_LIMIT:
            break
    return header + "\n" + "\n".join(lines) if lines else ""


def shown(conn: sqlite3.Connection, lesson_ids, *, now: float) -> None:
    """The lessons were in one more session's prompt: count it, and note when the agent first read each one in its
    current activation, which is where its measurement starts (evaluate)."""
    ids = [(item,) for item in lesson_ids if type(item) is int]
    if ids:
        _migrate_measure(conn, now)   # before the count: a lesson shown only now was never shown before 0.12.0
        conn.executemany("UPDATE lesson SET shown=shown+1 WHERE id=?", ids)
        _first_read(conn, [item for (item,) in ids], now)


def _first_read(conn: sqlite3.Connection, lesson_ids: list[int], now: float) -> None:
    # One row per lesson, tied to its activation: a lesson activated again is measured again from its next reading.
    conn.executemany("""INSERT INTO lesson_measure(lesson_id, activated, first_shown)
        SELECT id, activated, ? FROM lesson WHERE id=? AND status='active' AND activated IS NOT NULL
        ON CONFLICT(lesson_id) DO UPDATE SET activated=excluded.activated, first_shown=excluded.first_shown, basis=''
          WHERE lesson_measure.activated != excluded.activated""", [(now, item) for item in lesson_ids])


def hint_index(conn: sqlite3.Connection, *, lang: str = "en") -> dict[str, tuple[int, str, bool]]:
    """signature -> (lesson id, sentence, templated) for active lessons bound to a failure signature."""
    return {row["signature"]: (row["id"], render(row, lang), templated(row))
            for row in active_lessons(conn) if row["signature"]}


def count_hint(conn: sqlite3.Connection, lesson_id: int, *, now: float | None = None) -> None:
    """The lesson was added to a failing tool result: the agent read it there (its measurement may start now)."""
    import time

    now = time.time() if now is None else now
    _migrate_measure(conn, now)
    conn.execute("UPDATE lesson SET hints=hints+1 WHERE id=?", (lesson_id,))
    if type(lesson_id) is int:
        _first_read(conn, [lesson_id], now)


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


MEASURE_MIGRATED = "lesson_measure_since"   # cursor: the lessons active before 0.12.0 have their first reading
# cursor: when this database, kept by an earlier version, was first touched by 0.12.0, which reads failures another way
# (signals: tracebacks by their exception line, command output by its last lines, status codes only where they read as
# one). Failures counted before it and after it are not the same count (see evaluate).
CLASSIFIER_CHANGED = "classifier_0120_since"
VERDICTS = ("effective", "ineffective", "inconclusive")


def _migrate_measure(conn: sqlite3.Connection, now: float) -> None:
    """Once per database, at the first thing 0.12.0 does with a lesson (a prompt, a tool hint or a learning pass),
    whichever comes first: the lessons active before 0.12.0 get when each was first read. One the agent was shown (in
    a prompt or a tool hint) counts as read since its activation, as it was measured then; one it was never shown
    loses the verdict and the baseline it was judged on, and is measured from its first reading. Whether the earlier
    classifier counted this database is decided first (_mark_classifier)."""
    _mark_classifier(conn, now)
    if conn.execute("SELECT 1 FROM cursor WHERE key=?", (MEASURE_MIGRATED,)).fetchone():
        return
    conn.execute("""INSERT OR IGNORE INTO lesson_measure(lesson_id, activated, first_shown)
        SELECT id, activated, activated FROM lesson WHERE status='active' AND activated IS NOT NULL
          AND (shown > 0 OR hints > 0)""")
    conn.execute("""UPDATE lesson SET verdict=NULL, rate_before=NULL, rate_after=NULL, evaluated=NULL,
          baseline_failures=NULL, baseline_calls=NULL
        WHERE status='active' AND activated IS NOT NULL AND shown=0 AND hints=0
          AND NOT EXISTS (SELECT 1 FROM lesson_measure m WHERE m.lesson_id=lesson.id AND m.activated=lesson.activated)""")
    conn.execute("INSERT OR REPLACE INTO cursor VALUES (?, ?)", (MEASURE_MIGRATED, repr(now)))


CLASSIFIER_CHECKED = "classifier_0120_checked"   # cursor: whether CLASSIFIER_CHANGED applies here was decided


def _mark_classifier(conn: sqlite3.Connection, now: float) -> None:
    """Once per database, apart from the migration (a database a 0.12.0 development build already migrated, or one
    whose start of observation was not written yet, still gets it): a database observed for more than a day was
    counted by the earlier classifier, from now on it is not (CLASSIFIER_CHANGED). A new install replays its history
    with this version: nothing to mark. Undecided until the start of observation is known."""
    if conn.execute("SELECT 1 FROM cursor WHERE key=?", (CLASSIFIER_CHECKED,)).fetchone():
        return
    row = conn.execute("SELECT value FROM cursor WHERE key='observed_since'").fetchone()
    try:
        observed = float(row[0]) if row else None
    except (TypeError, ValueError):
        observed = None
    if observed is None:
        return
    if observed < now - 86_400:
        conn.execute("INSERT OR IGNORE INTO cursor VALUES (?, ?)", (CLASSIFIER_CHANGED, repr(now)))
    conn.execute("INSERT OR REPLACE INTO cursor VALUES (?, ?)", (CLASSIFIER_CHECKED, repr(now)))


def _measure_columns(conn: sqlite3.Connection) -> None:
    # baseline_days came after the first 0.12.0 development builds created lesson_measure
    if "baseline_days" not in {row[1] for row in conn.execute("PRAGMA table_info(lesson_measure)")}:
        conn.execute("ALTER TABLE lesson_measure ADD COLUMN baseline_days INTEGER")


def evaluate(conn: sqlite3.Connection, *, now: float, mode: str = "suggest", history_until: float | None = None) -> list[dict]:
    """Measure each active lesson bound to a failure against that failure before and after the agent first read it.

    A lesson is measured from its first reading in this activation (lesson_measure: a session prompt or a tool hint):
    the prompt shows a few lessons only, and one it never showed changed nothing, so it is not judged at all. Read on
    its activation day, it is measured from the activation, as before 0.12.0; read on a later day, from that day.
    The baseline is the 14 days before that start (with an activation on the same day, the activation day up to that
    moment too, kept by _record_change), counted once and frozen with the days it divides by, so retention never
    erases or shrinks it. While the backfill is still replaying Hermes' history up to `history_until`, a baseline
    that reaches into that history waits: frozen now, it would hold only part of it.
    The after window is the latest 14 days since the start (the rest of the starting day is left out).
    Rates are per call when both sides have call volume, otherwise per day: a baseline counts only the days the
    failure was seen in (from its first day in the window), not 14 days it may not have existed for. A baseline
    without failures gives no verdict but inconclusive: there was nothing to reduce. A lesson the owner chose to
    keep (keep) is never retired by auto mode.

    0.12.0 counts failures another way (CLASSIFIER_CHANGED): a failure the earlier classifier misread (`429 passed` as a
    rate limit) is no longer counted under its old signature, and others are counted under new ones. A comparison
    across that change could call a wrong lesson effective for ever (its failure simply stopped being counted), or a
    good one ineffective. So a baseline is counted only from the change on, and a lesson whose whole baseline was
    counted before it keeps the verdict it had then (like with like), or stays unjudged: retired, kept or approved
    only by what was measured consistently, or by the owner."""
    _migrate_measure(conn, now)
    _measure_columns(conn)
    changed_row = conn.execute("SELECT value FROM cursor WHERE key=?", (CLASSIFIER_CHANGED,)).fetchone()
    try:
        changed_day = utc_day(float(changed_row[0])) if changed_row else None
    except (TypeError, ValueError):
        changed_day = None
    results = []
    today = utc_day(now)
    for row in conn.execute("""SELECT l.*, m.first_shown, m.baseline_days, m.basis AS measured_basis FROM lesson l
          LEFT JOIN lesson_measure m ON m.lesson_id=l.id AND m.activated=l.activated
        WHERE l.status='active' AND l.signature IS NOT NULL AND l.activated IS NOT NULL""").fetchall():
        if row["first_shown"] is None:
            continue   # never in a prompt or a hint since its activation: the agent cannot have acted on it
        same_day = utc_day(row["first_shown"]) <= utc_day(row["activated"])
        start = row["activated"] if same_day else row["first_shown"]
        start_day = utc_day(start)
        legacy = changed_day is not None and start_day <= changed_day   # its whole baseline counted the old way
        window = day_offset(start, BASELINE_DAYS)
        if changed_day is not None and not legacy and window < changed_day:
            window = changed_day   # only failures counted the same way as after
        base_failures, base_calls, base_days = row["baseline_failures"], row["baseline_calls"], row["baseline_days"]
        partial = same_day and not legacy and conn.execute(
            "SELECT 1 FROM lesson_activation WHERE lesson_id=? AND activated=?", (row["id"], row["activated"])).fetchone() is not None
        if (base_failures is None or base_calls is None) and not legacy:
            if history_until is not None and start - BASELINE_DAYS * 86_400 < history_until:
                continue
            base_failures, base_calls = _counts(conn, row["signature"], row["tool"], window, day_offset(start, 1))
            if partial:
                snapshot = conn.execute("SELECT failures, calls FROM lesson_activation WHERE lesson_id=? AND activated=?",
                                        (row["id"], row["activated"])).fetchone()
                base_failures, base_calls = base_failures + snapshot[0], base_calls + snapshot[1]
            base_days = _baseline_days(conn, row["signature"], window, start, partial)
            conn.execute("UPDATE lesson SET baseline_failures=?, baseline_calls=? WHERE id=?",
                         (base_failures, base_calls, row["id"]))
            conn.execute("UPDATE lesson_measure SET baseline_days=? WHERE lesson_id=? AND activated=?",
                         (base_days, row["id"], row["activated"]))
        elif base_days is None and not legacy and base_failures is not None:
            # frozen by a build without the divisor: counted now if the window is still all there, else the 14 days
            # it was divided by then (retention may have removed its first days: never a smaller divisor)
            whole = window >= day_offset(now, RETENTION_DAYS - 1)
            base_days = _baseline_days(conn, row["signature"], window, start, partial) if whole else BASELINE_DAYS
            conn.execute("UPDATE lesson_measure SET baseline_days=? WHERE lesson_id=? AND activated=?",
                         (base_days, row["id"], row["activated"]))
        after_start = max(day_offset(start, -1), day_offset(now, AFTER_WINDOW_DAYS - 1))
        days_after = _calendar_days(after_start, today) if after_start <= today else 0
        failures_after, calls_after = _counts(conn, row["signature"], row["tool"], after_start, today) if days_after else (0, 0)
        days_active = _calendar_days(day_offset(start, -1), today) if start_day < today else 0
        if legacy:   # the verdict measured consistently before 0.12.0, if any, with its rates
            verdict = row["verdict"] if row["verdict"] in VERDICTS else None
            before, after = row["rate_before"] or 0.0, row["rate_after"] or 0.0
            basis = row["measured_basis"] or ""
        else:
            per_call = base_calls >= 5 and calls_after >= 5
            if per_call:
                before, after = base_failures / base_calls, failures_after / calls_after
            else:
                before = base_failures / max(1, base_days or BASELINE_DAYS)
                after = failures_after / days_after if days_after else 0.0
            basis = "failures_per_call" if per_call else "failures_per_day"
            ready = days_active >= EVALUATE_AFTER_DAYS or calls_after >= EVALUATE_AFTER_CALLS
            verdict = None
            if ready and days_after:
                if base_failures == 0:
                    verdict = "inconclusive"   # nothing to reduce: no rate after can show it helped or did not
                elif after <= 0.5 * before:
                    verdict = "effective"
                elif failures_after >= 3 and after >= 0.9 * before:
                    verdict = "ineffective"
                else:
                    verdict = "inconclusive"
        kept = verdict == "ineffective" and conn.execute(
            "SELECT 1 FROM lesson_keep WHERE lesson_id=? AND activated=?", (row["id"], row["activated"])).fetchone()
        retire = verdict == "ineffective" and days_active >= RETIRE_AFTER_DAYS and mode == "auto" and not kept
        if verdict:
            # Only while it is still active: an operator who retired it since this pass read it has the last word.
            cursor = conn.execute("""UPDATE lesson SET verdict=?, rate_before=?, rate_after=?, evaluated=?, status=?, updated=?
                WHERE id=? AND status='active'""", (verdict, before, after, now, "retired" if retire else "active",
                                                    now if retire or verdict != row["verdict"] else row["updated"], row["id"]))
            if not cursor.rowcount:
                continue
            if not legacy:
                conn.execute("UPDATE lesson_measure SET basis=? WHERE lesson_id=? AND activated=?",
                             (basis, row["id"], row["activated"]))
        results.append({"id": row["id"], "tool": row["tool"], "class": row["klass"], "verdict": verdict,
                        "basis": basis, "before": round(before, 4), "after": round(after, 4),
                        "failures_before": base_failures, "failures_after": failures_after, "days_active": days_active,
                        "retired": retire})
    return results


def _baseline_days(conn: sqlite3.Connection, signature: str, window: str, start: float, partial: bool) -> int:
    """The days the per-day baseline divides by: those of its window (from `window` to the day before the start) from
    the failure's first day on (a failure first seen two days earlier had two days to happen in, not 14), and the
    starting day when its part up to the activation is in the baseline (`partial`). Frozen with the baseline."""
    first = conn.execute("SELECT MIN(day) FROM failure_daily WHERE signature=? AND day >= ?", (signature, window)).fetchone()[0]
    since = max(window, first) if isinstance(first, str) else window
    return max(1, _calendar_days(since, day_offset(start, 1)) + int(partial))


def basis_of(conn: sqlite3.Connection, lesson_id: int) -> str:
    """The rate the lesson's latest verdict compared: failures_per_call, failures_per_day, or '' (not judged yet)."""
    row = conn.execute("""SELECT m.basis FROM lesson_measure m JOIN lesson l ON l.id=m.lesson_id AND l.activated=m.activated
        WHERE m.lesson_id=?""", (lesson_id,)).fetchone()
    return row[0] if row is not None and row[0] in ("failures_per_call", "failures_per_day") else ""


def listing(conn: sqlite3.Connection, *, lang: str = "en", status: str | None = None) -> list[dict]:
    rows = conn.execute("""SELECT l.*, COALESCE(f.total, 0) AS evidence, COALESCE(f.sessions, 0) AS sessions
        FROM lesson l
        LEFT JOIN failure f ON f.signature=l.signature WHERE (? IS NULL OR l.status=?)
        ORDER BY CASE l.status WHEN 'active' THEN 0 WHEN 'candidate' THEN 1 WHEN 'quarantined' THEN 2 ELSE 3 END,
          evidence DESC, l.id""", (status, status)).fetchall()
    owner = _owner_by_key(conn)   # what derive and promote decide on: failures that read the same count together
    return [{"id": row["id"], "status": row["status"], "origin": row["origin"], "tool": row["tool"], "class": row["klass"],
             "lesson": render(row, lang), "failures_seen": row["evidence"], "sessions": row["sessions"],
             "failures_in_owner_turns": owner.get(_text_key(row["tool"], row["klass"], row["hint"]), 0),
             "shown_in_sessions": row["shown"], "hints_given": row["hints"], "verdict": row["verdict"],
             "rate_before": row["rate_before"], "rate_after": row["rate_after"], "activated": row["activated"]}
            for row in rows]


def search(conn: sqlite3.Connection, *, tool: str | None = None, lang: str = "en", limit: int = 5) -> list[dict]:
    """Active lessons for one tool (or all), most evidenced first. Used by the agent-facing tool."""
    rows = [row for row in active_lessons(conn) if tool is None or row["tool"] == tool or row["tool"] == "general"]
    return [{"id": row["id"], "tool": row["tool"], "lesson": render(row, lang)} for row in rows[:max(1, min(limit, 10))]]
