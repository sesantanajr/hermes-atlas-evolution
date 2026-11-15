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
from .experience import experience

REFRESH_SECONDS = 60
MAX_HINTS_PER_SESSION = 6
_RESULT_LIMIT = 262_144
KEY = "atlas_evolution_lesson"


def attach(result, sentence: str, lang: str = "en") -> str | None:
    """Return the tool result with the lesson added, or None when it cannot be added safely."""
    if not isinstance(result, str) or not sentence or len(result) > _RESULT_LIMIT:
        return None
    if result.lstrip()[:1] == "{":
        try:
            data = json.loads(result)
        except ValueError:
            data = None
        if isinstance(data, dict):
            if KEY in data:
                return None
            data[KEY] = sentence
            return json.dumps(data, ensure_ascii=False)
    return f"{result}\n\n[{lessons.HINT_LABEL.get(lang, lessons.HINT_LABEL['en'])}] {sentence}"


class HintIndex:
    """In-memory view of active lessons by failure signature, refreshed from the store at most once a minute."""

    def __init__(self, home: Path, *, lang: str = "en"):
        self.home = home
        self.lang = lang
        self._index: dict[str, tuple[int, str]] = {}
        self._loaded = float("-inf")
        self._given: OrderedDict[str, set[str]] = OrderedDict()
        self._lock = threading.Lock()

    def _refresh(self, clock: float) -> None:
        if clock - self._loaded < REFRESH_SECONDS:
            return
        self._loaded = clock
        with experience(self.home, observer=True) as conn:
            self._index = lessons.hint_index(conn, lang=self.lang)

    def lookup(self, signature: str, session: str, *, clock: float | None = None) -> tuple[int, str] | None:
        """The lesson for this failure, once per session and at most MAX_HINTS_PER_SESSION per session."""
        clock = time.monotonic() if clock is None else clock
        with self._lock:
            self._refresh(clock)
            entry = self._index.get(signature)
            if entry is None:
                return None
            given = self._given.setdefault(session, set())
            self._given.move_to_end(session)
            while len(self._given) > 256:
                self._given.popitem(last=False)
            if signature in given or len(given) >= MAX_HINTS_PER_SESSION:
                return None
            given.add(signature)
            return entry

    def count(self, lesson_id: int) -> None:
        with experience(self.home, observer=True) as conn:
            lessons.count_hint(conn, lesson_id)
