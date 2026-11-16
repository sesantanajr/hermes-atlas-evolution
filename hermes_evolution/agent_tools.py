"""Agent-facing access to the playbook. Read-mostly: a proposed lesson is quarantined until an operator approves it."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from . import lessons, proposals
from .experience import experience

PROPOSALS_PER_HOUR = 5
LESSONS_TOOL = {
    "name": "hermes_evolution_lessons",
    "description": ("Look up lessons this profile learned from repeated tool failures, propose a new lesson for "
                    "operator review, or see the state of the improvement queue."),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["search", "propose", "status"]},
            "tool": {"type": "string", "description": "Tool the lesson applies to. Use 'general' for a cross-tool lesson."},
            "lesson": {"type": "string",
                       "description": "For propose: one sentence of 12 to 240 characters, shaped as 'when X, do Y'."},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


class LessonsTool:
    def __init__(self, home: Path, *, lang: str = "en"):
        self.home = home
        self.lang = lang
        self._recent: list[float] = []
        self._lock = threading.Lock()

    def _allow_proposal(self, clock: float) -> bool:
        with self._lock:
            self._recent = [stamp for stamp in self._recent if clock - stamp < 3600]
            if len(self._recent) >= PROPOSALS_PER_HOUR:
                return False
            self._recent.append(clock)
            return True

    def handle(self, args, **_context) -> str:
        try:
            if not isinstance(args, dict) or set(args) - {"action", "tool", "lesson"}:
                raise ValueError("Unexpected field")
            action, tool = args.get("action"), args.get("tool")
            if tool is not None and (not isinstance(tool, str) or len(tool) > 96):
                raise ValueError("tool must be a tool name")
            with experience(self.home) as conn:
                if action == "search":
                    result = {"lessons": lessons.search(conn, tool=tool or None, lang=self.lang)}
                elif action == "propose":
                    if not self._allow_proposal(time.monotonic()):
                        raise ValueError("Too many proposals this hour; the operator has not reviewed the earlier ones")
                    lesson_id = lessons.add_custom(conn, args.get("lesson"), tool=tool or "general", origin="agent",
                                                   now=time.time())
                    result = {"lesson_id": lesson_id, "status": "quarantined",
                              "note": "Stored for operator review. It is not applied until approved."}
                elif action == "status":
                    rows = lessons.listing(conn, lang=self.lang)
                    result = {"lessons": {status: sum(item["status"] == status for item in rows)
                                          for status in lessons.STATUSES},
                              "open_proposals": [{"id": item["id"], "kind": item["kind"], "risk": item["risk"],
                                                  "subject": item["subject"]}
                                                 for item in proposals.listing(conn, status="open", limit=20)]}
                else:
                    raise ValueError("action must be search, propose or status")
        except ValueError as error:
            result = {"error": str(error)}
        except (OSError, sqlite3.Error):
            result = {"error": "Evolution storage is temporarily unavailable."}
        return json.dumps(result, ensure_ascii=False)
