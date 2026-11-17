"""Just-in-time lesson hints attached to a failing tool result.

When a tool fails with a signature the playbook already has an active lesson for, the lesson is added to
that one result, so the agent reads it exactly when it is relevant. The session prompt and its cache are
left untouched, and nothing is added to successful calls.
"""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from pathlib import Path

from . import lessons
from .experience import experience, record

REFRESH_SECONDS = 60
MAX_HINTS_PER_SESSION = 6
_RESULT_LIMIT = 262_144
KEY = "hermes_atlas_evolution_lesson"


def attach(result, sentence: str, lang: str = "en") -> str | None:
    """Return the tool result with the lesson added, or None when it cannot be added safely."""
    if not isinstance(result, str) or not sentence or len(result) > _RESULT_LIMIT:
        return None
    if result.lstrip()[:1] == "{":
        try:
            data = json.loads(result)
        except (ValueError, RecursionError):
            data = None
        if isinstance(data, dict):
            hint = data.get("hint")
            if KEY in data or isinstance(hint, str) and hint.strip():
                return None   # already advised, by this plugin or by Hermes' own terminal hints
            data[KEY] = sentence
            try:
                return json.dumps(data, ensure_ascii=False)
            except (ValueError, RecursionError):
                # A deep enough stack (Python 3.14, a large thread stack) parses nesting it cannot write back:
                # the lesson goes after the result as text, as for a result that cannot be parsed at all.
                pass
    return f"{result}\n\n[{lessons.HINT_LABEL.get(lang, lessons.HINT_LABEL['en'])}] {sentence}"


class HintIndex:
    """In-memory view of active lessons by failure signature, refreshed from the store at most once a minute."""

    def __init__(self, home: Path, *, lang: str = "en"):
        self.home = home
        self.lang = lang
        self._index: dict[str, tuple[int, str, bool]] = {}
        self._loaded = float("-inf")
        self._given: OrderedDict[str, set[str]] = OrderedDict()
        self._lock = threading.Lock()

    def _refresh(self, clock: float) -> None:
        """Reload the index at most once a minute. The read runs outside the lock, so other hook threads keep
        using the current index meanwhile; one thread reloads, the new index is swapped in under the lock."""
        with self._lock:
            if clock - self._loaded < REFRESH_SECONDS:
                return
            self._loaded = clock   # claimed: no other thread reloads during this read
        with experience(self.home) as conn:   # WAL readers never wait for a writer
            index = lessons.hint_index(conn, lang=self.lang)
        with self._lock:
            self._index = index

    def lookup(self, signature: str, session: str, *, clock: float | None = None,
               templates_only: bool = False) -> tuple[int, str] | None:
        """The lesson for this failure, once per session and at most MAX_HINTS_PER_SESSION per session.
        `templates_only`: a chat where someone else may read along gets template lessons only."""
        clock = time.monotonic() if clock is None else clock
        self._refresh(clock)
        with self._lock:
            entry = self._index.get(signature)
            if entry is None or templates_only and not entry[2]:
                return None
            given = self._given.setdefault(session, set())
            self._given.move_to_end(session)
            while len(self._given) > 256:
                self._given.popitem(last=False)
            if signature in given or len(given) >= MAX_HINTS_PER_SESSION:
                return None
            given.add(signature)
            return entry[0], entry[1]

    def count(self, lesson_id: int) -> None:
        record(self.home, lambda conn: lessons.count_hint(conn, lesson_id))
