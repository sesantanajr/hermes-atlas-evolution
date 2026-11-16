"""Lesson packs: carry what one profile learned to another profile, or to another machine.

Hermes loads plugins per profile, so each profile learns on its own: a tool that needs an argument in one
profile fails three times in every other profile before the lesson exists there. A pack moves the lessons that
already exist.

A pack is a small JSON document. A template lesson travels as its tool, failure class and identifier-like detail,
and the receiving profile renders the sentence from its own fixed templates; nothing free-form travels for it.
A written lesson (the operator's, or a draft the operator approved) travels as its sentence. Nothing else
leaves the profile: no counters of conversations, no session ids, no excerpts, no settings.

Importing is the operator's decision, in the operator's own terminal, after seeing every sentence:
- a template lesson arrives waiting. In `auto` mode it activates by itself once the owner's own turns hit its
  failure OWNER_ACTIVATION times here (the pattern is already proven elsewhere, so it does not wait for three
  failures in two sessions first); in `suggest` mode it waits for approval;
- a written lesson arrives quarantined, like any written lesson;
- `--activate` activates both at once, as `lessons add --activate` does;
- a sentence already in the playbook, or one the owner turned down here, never comes in again.
Every imported lesson is then measured here like any other, and retired here when it does not help.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote

from . import lessons as playbook
from .experience import label, utc_datetime

FORMAT = "hermes-atlas-evolution/lessons"
PACK_VERSION = 1
MAX_PACK_BYTES = 256 * 1024
MAX_LESSONS = 200
_TOOL = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9_.:+-]{0,95}\Z")
_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


def _signature(tool: str, klass: str, hint: str) -> str:
    import hashlib

    return hashlib.sha256("\0".join((tool, klass, hint)).encode()).hexdigest()[:20]


def _iso(stamp) -> str | None:
    return utc_datetime(stamp).strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(stamp, (int, float)) else None


def export(conn: sqlite3.Connection, *, now: float, profile: str = "", plugin_version: str = "") -> dict:
    """The active lessons of this profile as a pack. A lesson measured as not helping is left out."""
    entries = []
    rows = conn.execute("""SELECT * FROM lesson WHERE status='active' AND (verdict IS NULL OR verdict != 'ineffective')
        ORDER BY (verdict='effective') DESC, id LIMIT ?""", (MAX_LESSONS,)).fetchall()
    for row in rows:
        effect = {"verdict": row["verdict"], "rate_before": row["rate_before"], "rate_after": row["rate_after"]}
        if playbook.templated(row) and row["klass"] in playbook.TEMPLATES:
            entries.append({"kind": "template", "tool": row["tool"], "class": row["klass"], "hint": row["hint"], **effect})
        elif row["text"]:
            entries.append({"kind": "text", "tool": row["tool"], "text": row["text"], "origin": row["origin"], **effect})
    return {"format": FORMAT, "version": PACK_VERSION, "exported": _iso(now),
            "profile": profile if _PROFILE.fullmatch(profile or "") else None,
            "plugin_version": plugin_version or None, "lessons": entries}


def read_pack(source: str | Path, *, stdin=None) -> dict:
    """A pack from a file (or '-' for standard input), checked for shape and size before anything else."""
    if str(source) == "-":
        import sys

        body = (stdin or sys.stdin).read(MAX_PACK_BYTES + 1)
    else:
        path = Path(source)
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                body = handle.read(MAX_PACK_BYTES + 1)
        except (OSError, UnicodeDecodeError) as error:
            raise ValueError(f"Cannot read the lesson pack: {type(error).__name__}") from None
    if len(body) > MAX_PACK_BYTES:
        raise ValueError("The lesson pack is larger than 256 KB")
    try:
        pack = json.loads(body)
    except (ValueError, RecursionError):
        raise ValueError("The lesson pack is not valid JSON") from None
    return check_pack(pack)


def check_pack(pack) -> dict:
    if not isinstance(pack, dict) or pack.get("format") != FORMAT:
        raise ValueError("This is not an Atlas Evolution lesson pack")
    if pack.get("version") != PACK_VERSION:
        raise ValueError("This lesson pack has a version this plugin cannot read; update the plugin")
    entries = pack.get("lessons")
    if not isinstance(entries, list) or len(entries) > MAX_LESSONS:
        raise ValueError("A lesson pack holds a list of at most 200 lessons")
    return pack


def from_profile(home: Path, name: str, *, now: float) -> dict:
    """The pack of another profile of the same Hermes, read from its Evolution database without writing to it."""
    if not isinstance(name, str) or not _PROFILE.fullmatch(name):
        raise ValueError("Name a profile of this Hermes, such as default or work")
    home = Path(home).resolve()
    root = home.parent.parent if home.parent.name == "profiles" else home
    folder = root if name == "default" else root / "profiles" / name
    if folder.resolve() == home:
        raise ValueError("That is this profile; name another one")
    path = folder / "evolution" / "evolution.sqlite3"
    if not path.is_file():
        raise ValueError(f"Profile {name} has no Atlas Evolution data")
    try:
        source = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=2)
        try:
            source.row_factory = sqlite3.Row
            from .status import plugin_version

            return export(source, now=now, profile=name, plugin_version=plugin_version())
        finally:
            source.close()
    except sqlite3.Error:
        raise ValueError(f"Cannot read the Atlas Evolution data of profile {name} right now") from None


def _entry(item) -> tuple[dict | None, str | None]:
    """(normalized entry, None) or (None, reason). Everything in a pack is untrusted until it passes here."""
    from .signals import _clean_hint

    if not isinstance(item, dict):
        return None, "invalid"
    tool = item.get("tool")
    if not isinstance(tool, str) or not _TOOL.fullmatch(tool) or label(tool, fallback="") != tool or tool == "unknown":
        return None, "invalid_tool"
    if item.get("kind") == "template":
        klass, hint = item.get("class"), item.get("hint", "")
        template = playbook.TEMPLATES.get(klass) if isinstance(klass, str) else None
        if template is None:
            return None, "unknown_class"
        if not isinstance(hint, str) or (hint and _clean_hint(hint) != hint) or (template[0] and not hint):
            return None, "invalid_hint"
        return {"kind": "template", "tool": tool, "class": klass, "hint": hint}, None
    if item.get("kind") == "text":
        text = playbook.clean_text(item.get("text"))
        if text is None:
            return None, "invalid_text"
        if playbook.generic_advice(text):
            return None, "generic"
        return {"kind": "text", "tool": tool, "text": text}, None
    return None, "invalid"


def plan(conn: sqlite3.Connection, pack: dict, *, lang: str = "en") -> dict:
    """What an import would do, decided against this profile's playbook, before anything is written."""
    check_pack(pack)
    taken = playbook._decided_keys(conn, waiting=True)
    signatures = {row[0] for row in conn.execute("SELECT signature FROM lesson WHERE signature IS NOT NULL")}
    # Written sentences already here in any state but withdrawn: an active one, a waiting one, or one turned down.
    texts = {row[0] for row in conn.execute(
        f"""SELECT text FROM lesson WHERE text IS NOT NULL AND NOT (status='retired' AND verdict IS '{playbook.WITHDRAWN}')""")}
    add, skip, seen = [], [], set()
    for item in pack["lessons"]:
        entry, reason = _entry(item)
        if entry is None:
            skip.append({"reason": reason})
            continue
        if entry["kind"] == "template":
            row = {"text": None, "klass": entry["class"], "tool": entry["tool"], "hint": entry["hint"]}
            key = playbook._text_key(entry["tool"], entry["class"], entry["hint"])
            entry["signature"] = _signature(entry["tool"], entry["class"], entry["hint"])
            entry["lesson"] = playbook.render(row, lang)
            duplicate = key in taken or entry["signature"] in signatures or key in seen
        else:
            key = entry["text"]
            entry["lesson"] = entry["text"]
            duplicate = key in texts or key in seen
        if duplicate:
            skip.append({"reason": "already_here", "lesson": entry["lesson"]})
            continue
        seen.add(key)
        add.append(entry)
    return {"source": pack.get("profile") if isinstance(pack.get("profile"), str) else None, "add": add, "skip": skip}


def apply(conn: sqlite3.Connection, planned: dict, *, now: float, mode: str, activate: bool, source: str) -> list[dict]:
    """Write the lessons of a plan. The caller has shown every sentence to the operator and owns the transaction."""
    source = source if label(source, fallback="") == source else "file"
    created = []
    for entry in planned["add"]:
        if entry["kind"] == "template":
            status = "active" if activate else "candidate"
            cursor = conn.execute("""INSERT OR IGNORE INTO lesson(signature, tool, klass, hint, origin, status, created,
                  updated, activated) VALUES (?, ?, ?, ?, 'derived', ?, ?, ?, ?)""",
                                  (entry["signature"], entry["tool"], entry["class"], entry["hint"], status, now, now,
                                   now if activate else None))
            if not cursor.rowcount:
                continue
            lesson_id = cursor.lastrowid
            if activate:
                playbook._record_change(conn, lesson_id, now, entry["tool"])
            elif mode == "auto":   # waits only for the owner's own turns to hit the failure here (lessons.promote)
                conn.execute("INSERT OR REPLACE INTO lesson_owner_wait VALUES (?, ?)", (lesson_id, entry["signature"]))
        else:
            lesson_id = playbook.add_custom(conn, entry["text"], tool=entry["tool"], origin="operator", now=now,
                                            active=activate)
            status = "active" if activate else "quarantined"
        conn.execute("INSERT OR REPLACE INTO lesson_import VALUES (?, ?, ?)", (lesson_id, source, now))
        created.append({"id": lesson_id, "status": status, "lesson": entry["lesson"]})
    return created
