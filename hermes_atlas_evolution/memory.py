"""Scoped, searchable memory with local normalization and explicit provenance."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import sqlite3
import unicodedata


GLOBAL_SCOPE = "global"
CATEGORIES = ("preference", "writing", "workflow", "research", "project", "lesson", "person")

_SCOPE_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}\Z")
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)
_STOPWORDS = frozenset(
    "a as ao aos aonde com como da das de do dos e em esta este foi mais na nas no nos o os "
    "para pela pelo por que se seu sua um uma and are as at be but by for from have in is it of "
    "on or that the to was were will with i my you your".split()
)
_ALIASES = {
    variant: group[0]
    for group in (
        ("preferencia", "preferencias", "preference", "preferences", "prefiro", "gosto", "prefer"),
        ("escrita", "escrever", "redacao", "redigir", "writing", "write", "copywriting"),
        ("fluxo", "fluxos", "workflow", "workflows"),
        ("pesquisa", "pesquisas", "pesquisar", "research"),
        ("projeto", "projetos", "project", "projects"),
        ("licao", "licoes", "lesson", "lessons", "aprendizado", "aprendizagem"),
        ("memoria", "memorias", "memory", "memories"),
        ("observabilidade", "observability"),
        ("implantacao", "implantacoes", "deploy", "deployment", "deployments"),
        ("incidente", "incidentes", "incident", "incidents"),
        ("custo", "custos", "cost", "costs"),
        ("relatorio", "relatorios", "report", "reports"),
        ("portugues", "portuguese"),
        ("ingles", "english"),
    )
    for variant in group
}


def make_scope(platform: str, user_id: str | int) -> str:
    """Derive a scope identifier without storing raw platform user identifiers."""
    if not isinstance(platform, str) or not platform.strip():
        raise ValueError("A platform is required to derive a private memory scope.")
    if isinstance(user_id, bool) or not isinstance(user_id, (str, int)) or not str(user_id).strip():
        raise ValueError("A user identifier is required to derive a private memory scope.")
    identity = json.dumps([platform.strip().casefold(), str(user_id).strip()], ensure_ascii=False)
    return "private:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _check_scope(scope: str) -> None:
    if not isinstance(scope, str) or not _SCOPE_PATTERN.fullmatch(scope):
        raise ValueError("A valid memory scope is required.")


def _words(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    without_accents = "".join(char for char in normalized if not unicodedata.combining(char))
    return _WORDS.findall(without_accents)


def _fingerprint(value: str) -> str:
    content = unicodedata.normalize("NFKC", value).casefold()
    normalized = " ".join(content.split()).rstrip(".!?。！？")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _terms(value: str) -> set[str]:
    words = _words(value)
    meaningful = [word for word in words if word not in _STOPWORDS]
    return {_ALIASES.get(word, word) for word in meaningful or words}


@contextmanager
def _atomic(connection: sqlite3.Connection):
    connection.execute("SAVEPOINT evolution_memory")
    try:
        yield
    except BaseException:
        connection.execute("ROLLBACK TO evolution_memory")
        connection.execute("RELEASE evolution_memory")
        raise
    connection.execute("RELEASE evolution_memory")


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _visible_scopes(scope: str, include_global: bool) -> tuple[str, ...]:
    _check_scope(scope)
    if type(include_global) is not bool:
        raise ValueError("Global memory access must be selected explicitly.")
    if include_global and scope != GLOBAL_SCOPE:
        return (scope, GLOBAL_SCOPE)
    return (scope,)


def _placeholders(items) -> str:
    return ", ".join("?" for _ in items)


class MemoryStore:
    """Keep user-approved memories in the service's local SQLite connection."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        with _atomic(connection):
            connection.execute(
                """CREATE TABLE IF NOT EXISTS evolution_memories (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       scope TEXT NOT NULL,
                       category TEXT NOT NULL,
                       content TEXT NOT NULL,
                       fingerprint TEXT NOT NULL,
                       confidence REAL NOT NULL,
                       tags_json TEXT NOT NULL,
                       sources_json TEXT NOT NULL,
                       created_at TEXT NOT NULL,
                       updated_at TEXT NOT NULL,
                       UNIQUE(scope, category, fingerprint)
                   )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS evolution_memory_terms (
                       memory_id INTEGER NOT NULL,
                       term TEXT NOT NULL,
                       weight INTEGER NOT NULL,
                       PRIMARY KEY(memory_id, term),
                       FOREIGN KEY(memory_id) REFERENCES evolution_memories(id) ON DELETE CASCADE
                   )"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS evolution_memory_term_lookup
                   ON evolution_memory_terms(term, memory_id)"""
            )

    def remember(
        self,
        scope: str,
        category: str,
        text: str,
        *,
        source: str = "manual",
        confidence: float = 1.0,
        tags=(),
        promote: bool = False,
    ) -> dict:
        """Save an approved item and merge repeat entries within its scope and category."""
        _check_scope(scope)
        if category not in CATEGORIES:
            raise ValueError(f"Memory category must be one of: {', '.join(CATEGORIES)}.")
        if category == "person" and (promote is not True or scope == GLOBAL_SCOPE):
            raise ValueError("Person profiles require explicit promotion into a private scope.")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2400 or not _words(text):
            raise ValueError("Memory text must contain words and be at most 2400 characters.")
        if not isinstance(source, str) or not 1 <= len(source.strip()) <= 120:
            raise ValueError("Memory source must be 1 to 120 characters.")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError("Memory confidence must be between 0 and 1.")
        if tags is None or isinstance(tags, str):
            raise ValueError("Memory tags must be a sequence of strings.")
        tags = tuple(tags)
        if len(tags) > 12 or any(not isinstance(tag, str) or not 1 <= len(tag.strip()) <= 80 for tag in tags):
            raise ValueError("Use at most 12 memory tags, each 1 to 80 characters.")

        content = text.strip()
        fingerprint = _fingerprint(content)
        now = _timestamp()
        with _atomic(self.connection):
            self.connection.execute(
                """INSERT INTO evolution_memories (
                       scope, category, content, fingerprint, confidence, tags_json,
                       sources_json, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, '[]', '[]', ?, ?)
                   ON CONFLICT(scope, category, fingerprint) DO UPDATE SET
                       confidence = MAX(confidence, excluded.confidence),
                       updated_at = excluded.updated_at""",
                (scope, category, content, fingerprint, confidence, now, now),
            )
            row = self.connection.execute(
                """SELECT id, scope, category, content, confidence, tags_json, sources_json,
                          created_at, updated_at
                   FROM evolution_memories WHERE scope = ? AND category = ? AND fingerprint = ?""",
                (scope, category, fingerprint),
            ).fetchone()
            item = self._record(row)
            item["tags"] = sorted(set(item["tags"]) | {tag.strip() for tag in tags}, key=lambda tag: (tag.casefold(), tag))[:24]
            item["sources"] = sorted(set(item["sources"]) | {source.strip()})[:24]
            self.connection.execute(
                "UPDATE evolution_memories SET tags_json = ?, sources_json = ? WHERE id = ?",
                (json.dumps(item["tags"], ensure_ascii=False), json.dumps(item["sources"], ensure_ascii=False), item["id"]),
            )
            weighted_terms = dict.fromkeys(_terms(item["text"]), 1)
            for tag in item["tags"]:
                weighted_terms.update(dict.fromkeys(_terms(tag), 2))
            self.connection.execute("DELETE FROM evolution_memory_terms WHERE memory_id = ?", (item["id"],))
            self.connection.executemany(
                "INSERT INTO evolution_memory_terms(memory_id, term, weight) VALUES (?, ?, ?)",
                ((item["id"], term, weight) for term, weight in weighted_terms.items()),
            )
        return item

    @staticmethod
    def _record(row) -> dict:
        return {
            "id": row[0],
            "scope": row[1],
            "category": row[2],
            "text": row[3],
            "confidence": row[4],
            "tags": json.loads(row[5]),
            "sources": json.loads(row[6]),
            "created_at": row[7],
            "updated_at": row[8],
        }

    def search(self, scope: str, query: str, *, categories=(), limit: int = 5, include_global: bool = False) -> list[dict]:
        scopes = _visible_scopes(scope, include_global)
        if not isinstance(query, str) or len(query) > 1000:
            raise ValueError("Memory query must be at most 1000 characters.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("Memory search limit must be between 1 and 50.")
        if categories is None or isinstance(categories, str):
            raise ValueError("Memory category filters must be a sequence.")
        categories = tuple(categories)
        if any(category not in CATEGORIES for category in categories):
            raise ValueError("An unknown memory category was provided.")
        categories = tuple(dict.fromkeys(categories))

        condition = f"m.scope IN ({_placeholders(scopes)})"
        parameters = (*scopes, *categories)
        if categories:
            condition += f" AND m.category IN ({_placeholders(categories)})"
        columns = "m.id, m.scope, m.category, m.content, m.confidence, m.tags_json, m.sources_json, m.created_at, m.updated_at"
        terms = tuple(sorted(_terms(query)))[:80]
        if not terms:
            if query.strip():
                return []
            rows = self.connection.execute(
                f"""SELECT {columns} FROM evolution_memories AS m WHERE {condition}
                    ORDER BY m.updated_at DESC, m.id DESC LIMIT ?""",
                (*parameters, limit),
            ).fetchall()
            return self._with_connections([self._record(row) for row in rows])

        rows = self.connection.execute(
            f"""SELECT {columns}, MAX(m.fingerprint = ?) AS exact_match,
                       COUNT(*) AS matched_terms, SUM(t.weight) AS matched_weight
                FROM evolution_memories AS m
                JOIN evolution_memory_terms AS t ON t.memory_id = m.id
                WHERE {condition} AND t.term IN ({_placeholders(terms)})
                GROUP BY m.id
                ORDER BY exact_match DESC, matched_terms DESC, matched_weight DESC,
                         m.confidence DESC, m.updated_at DESC, m.id DESC LIMIT ?""",
            (_fingerprint(query), *parameters, *terms, limit),
        ).fetchall()
        items = [self._record(row) for row in rows]
        for item, row in zip(items, rows):
            item["exact_match"] = bool(row[9])
            item["matched_terms"] = row[10]
        return self._with_connections(items)

    @staticmethod
    def _with_connections(items: list[dict]) -> list[dict]:
        tags_by_id = {item["id"]: _terms(" ".join(item["tags"])) for item in items}
        topics_by_id = {item["id"]: _terms(item["text"]) | tags_by_id[item["id"]] for item in items}
        for item in items:
            item["connections"] = [
                other["id"]
                for other in items
                if item["scope"] == other["scope"] and item["category"] != other["category"]
                and (
                    len(topics_by_id[item["id"]] & topics_by_id[other["id"]]) >= 2
                    or tags_by_id[item["id"]] & topics_by_id[other["id"]]
                    or tags_by_id[other["id"]] & topics_by_id[item["id"]]
                )
            ][:5]
        return items

    def forget(self, scope: str, memory_id: int) -> bool:
        _check_scope(scope)
        if type(memory_id) is not int or not 1 <= memory_id <= 2**63 - 1:
            raise ValueError("Memory ID must be a positive integer.")
        with _atomic(self.connection):
            removed = self.connection.execute(
                "DELETE FROM evolution_memories WHERE scope = ? AND id = ?", (scope, memory_id)
            ).rowcount
            if removed:
                self.connection.execute("DELETE FROM evolution_memory_terms WHERE memory_id = ?", (memory_id,))
        return bool(removed)

    def evaluate(self, scope: str, cases, *, limit: int = 5, include_global: bool = False) -> dict:
        """Measure retrieval against explicitly labeled, visible memory IDs."""
        scopes = _visible_scopes(scope, include_global)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("Memory search limit must be between 1 and 50.")
        if not isinstance(cases, (list, tuple)) or not 1 <= len(cases) <= 100:
            raise ValueError("Provide 1 to 100 labeled retrieval cases.")

        validated = []
        for case in cases:
            if not isinstance(case, dict):
                raise ValueError("Every retrieval case needs a query and expected_ids.")
            query, expected_ids = case.get("query"), case.get("expected_ids")
            if not isinstance(query, str) or not 1 <= len(query) <= 1000 or not _terms(query):
                raise ValueError("Each retrieval query must contain words and be at most 1000 characters.")
            if (
                not isinstance(expected_ids, (list, tuple)) or not 1 <= len(expected_ids) <= 50
                or any(type(item) is not int or not 1 <= item <= 2**63 - 1 for item in expected_ids)
            ):
                raise ValueError("Each retrieval case needs 1 to 50 positive expected IDs.")
            validated.append((query, set(expected_ids)))

        scores = []
        with _atomic(self.connection):
            for index, (query, expected) in enumerate(validated, start=1):
                visible_count = self.connection.execute(
                    f"""SELECT COUNT(*) FROM evolution_memories
                        WHERE scope IN ({_placeholders(scopes)}) AND id IN ({_placeholders(expected)})""",
                    (*scopes, *expected),
                ).fetchone()[0]
                if visible_count != len(expected):
                    raise ValueError("Expected IDs must refer to visible memory items.")

                retrieved = self.search(scope, query, limit=limit, include_global=include_global)
                relevant_ranks = [rank for rank, item in enumerate(retrieved, start=1) if item["id"] in expected]
                first_rank = relevant_ranks[0] if relevant_ranks else None
                scores.append({
                    "case_index": index,
                    "expected_count": len(expected),
                    "retrieved_count": len(retrieved),
                    "matched_count": len(relevant_ranks),
                    "first_relevant_rank": first_rank,
                    "hit_at_k": int(bool(relevant_ranks)),
                    "recall_at_k": len(relevant_ranks) / len(expected),
                    "mrr": 1 / first_rank if first_rank is not None else 0.0,
                })
        return {
            "limit": limit,
            "case_count": len(scores),
            **{name: sum(case[name] for case in scores) / len(scores) for name in ("hit_at_k", "recall_at_k", "mrr")},
            "cases": scores,
        }

    def audit(self, scope: str, *, include_global: bool = False) -> dict:
        scopes = _visible_scopes(scope, include_global)
        stale_since = (datetime.now(timezone.utc) - timedelta(days=180)).isoformat(timespec="seconds")
        condition = f"m.scope IN ({_placeholders(scopes)})"
        with _atomic(self.connection):
            rows = self.connection.execute(
                f"""SELECT m.category, COUNT(*), SUM(m.confidence < 0.6), SUM(m.updated_at < ?),
                           SUM(NOT EXISTS (SELECT 1 FROM evolution_memory_terms AS t WHERE t.memory_id = m.id))
                    FROM evolution_memories AS m WHERE {condition} GROUP BY m.category""",
                (stale_since, *scopes),
            ).fetchall()
            repeated = self.connection.execute(
                f"""SELECT COUNT(*) FROM (
                        SELECT m.scope, m.fingerprint FROM evolution_memories AS m WHERE {condition}
                        GROUP BY m.scope, m.fingerprint HAVING COUNT(*) > 1
                    )""",
                scopes,
            ).fetchone()[0]
        return {
            "total": sum(row[1] for row in rows),
            "categories": {row[0]: row[1] for row in rows},
            "quality": {
                "low_confidence": sum(row[2] for row in rows),
                "stale_180_days": sum(row[3] for row in rows),
                "unindexed": sum(row[4] for row in rows),
                "repeated_across_categories": repeated,
            },
        }
