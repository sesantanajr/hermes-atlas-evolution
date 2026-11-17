"""Care of Hermes' built-in memory (MEMORY.md and USER.md of a profile): find what repeats, and take out the copy
the owner chooses, safely.

Hermes injects both files into every session's system prompt, so an entry said twice costs tokens on every turn
and leaves less room for new memories. `review` finds:
- exact repeats (the same entry, ignoring case, spacing and Unicode forms), within a file or across the two;
- similar entries (most of their words in common), which the owner may want to keep once.
It never rewrites an entry's text: merging two entries into new words is the agent's job, with its memory tool.

`remove` takes one entry out, by the fingerprint of its exact text, the way Hermes' memory tool writes: under the
same exclusive lock (`<file>.lock`, flock or msvcrt), after reading the file again, with an atomic replace, and only
when the entry is still there as the owner saw it. A copy of the file is kept first in
`<profile>/evolution/memory-backups/` (owner-only), and the decision is logged without the text (the log keeps
fingerprints, never memory content). `restore` puts the entry back from that copy, while it still fits.
Sessions already running keep the memory they started with; new sessions read the change.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import tempfile
import time
import unicodedata
from pathlib import Path

DELIMITER = "\n§\n"   # Hermes' ENTRY_DELIMITER
FILES = {"MEMORY.md": ("memory_char_limit", 2200, "memory_enabled"),
         "USER.md": ("user_char_limit", 1375, "user_profile_enabled")}
MAX_BYTES = 512 * 1024
SIMILAR = 0.6      # words in common over words in either
CONTAINED = 0.85   # words of the shorter entry found in the longer one
BACKUPS_KEPT = 100
MAX_ISSUES = 100   # the most useful first: exact repeats, then what one entry says of another, then similar ones
LOCK_WAIT_SECONDS = 5.0
_WORD = re.compile(r"\w{3,}")


def fingerprint(entry: str) -> str:
    return hashlib.sha256(entry.encode("utf-8", "replace")).hexdigest()[:16]


def _normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _words(text: str) -> set:
    return set(_WORD.findall(unicodedata.normalize("NFKC", text).casefold()))


def parse(raw: str) -> list[str]:
    """Stripped, non-empty entries, as Hermes parses them (split on the full delimiter, so a bare § survives)."""
    return [entry for entry in (part.strip() for part in raw.replace("\r\n", "\n").split(DELIMITER)) if entry]


def _memory_config(runtime_config) -> dict:
    memory = runtime_config.get("memory") if isinstance(runtime_config, dict) else None
    return memory if isinstance(memory, dict) else {}


def limit_of(name: str, runtime_config) -> int:
    key, default, _flag = FILES[name]
    value = _memory_config(runtime_config).get(key)
    return value if type(value) is int and value > 0 else default


def _path(home: Path, name: str) -> Path:
    if name not in FILES:
        raise ValueError("Unknown memory file")
    return Path(home) / "memories" / name


def _read(path: Path) -> str | None:
    """The file's text, or None when it is missing, not a regular file, too large or not UTF-8."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
            return None
        return path.read_text(encoding="utf-8-sig")   # as Hermes reads it: a byte order mark is not text
    except (OSError, UnicodeError):
        return None


def review(home: Path, runtime_config=None) -> dict:
    """Each memory file's use of its limit, and what repeats, with the entries' text (for the owner's own terminal
    or logged-in panel: memory is the owner's)."""
    files, entries = {}, []
    for name in FILES:
        raw = _read(_path(home, name))
        limit = limit_of(name, runtime_config)
        enabled = _memory_config(runtime_config).get(FILES[name][2])
        found = parse(raw) if raw is not None else []
        size = sum(map(len, found)) + max(0, len(found) - 1) * len(DELIMITER)
        files[name] = {"present": raw is not None, "entries": len(found), "characters": size, "limit": limit,
                       "percent": round(100 * size / limit) if limit else None, "enabled": enabled is not False}
        entries += [{"file": name, "index": index, "text": text, "fingerprint": fingerprint(text)}
                    for index, text in enumerate(found)]
    issues = []
    for i, first in enumerate(entries):
        for second in entries[i + 1:]:
            same = _normalized(first["text"]) == _normalized(second["text"])
            if same:
                kind, score = "exact", 1.0
            else:
                a, b = _words(first["text"]), _words(second["text"])
                if not a or not b:
                    continue
                if any(any(ch.isdigit() for ch in word) for word in a ^ b):
                    continue   # they differ in a number (a port, a version, an id): two facts, not one said twice
                shared = len(a & b)
                jaccard, contained = shared / len(a | b), shared / min(len(a), len(b))
                if jaccard >= SIMILAR:
                    kind, score = "similar", round(jaccard, 2)
                elif contained >= CONTAINED and min(len(a), len(b)) >= 4:
                    kind, score = "contained", round(contained, 2)
                else:
                    continue
            # the shorter one is the usual one to take out; an exact repeat keeps the first, and a fact said in both
            # files stays in USER.md (what Hermes keeps about the user)
            if kind == "exact" and first["file"] != second["file"]:
                off = [entry for entry in (first, second) if not files[entry["file"]]["enabled"]]
                # the copy in a file Hermes does not load goes: taking out the other would hide the fact from the agent
                drop = off[0] if len(off) == 1 else first if first["file"] == "MEMORY.md" else second
            else:
                drop = second if kind == "exact" or len(second["text"]) <= len(first["text"]) else first
            # Hermes drops a repeat of the very same text in one file when it loads the file: that one costs nothing
            identical = first["file"] == second["file"] and first["text"] == second["text"]
            issues.append({"kind": kind, "score": score, "entries": [first, second], "identical": first["text"] == second["text"],
                           "suggested_drop": {"file": drop["file"], "fingerprint": drop["fingerprint"]},
                           "tokens_per_turn": 0 if identical else max(1, len(drop["text"]) // 4)})
    issues.sort(key=lambda item: ({"exact": 0, "contained": 1, "similar": 2}[item["kind"]], -item["score"]))
    total = len(issues)
    issues = issues[:MAX_ISSUES]
    return {"files": files, "issues": issues, "issues_total": total,
            "tokens_saved_per_turn": sum(item["tokens_per_turn"] for item in issues if item["kind"] == "exact")}


@contextlib.contextmanager
def _locked(path: Path, *, wait_s: float = LOCK_WAIT_SECONDS):
    """Hermes' memory lock: an exclusive lock on `<file>.lock` (flock on POSIX, msvcrt on Windows)."""
    try:
        import fcntl
    except ImportError:
        fcntl = None
    try:
        import msvcrt
    except ImportError:
        msvcrt = None
    lock_path = path.with_name(path.name + ".lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags, 0o600)
    locked = False
    try:
        deadline = time.monotonic() + wait_s
        while fcntl is not None or msvcrt is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ValueError("Hermes is writing its memory right now; try again in a moment") from None
                time.sleep(0.1)
        yield
    finally:
        if locked:
            with contextlib.suppress(OSError):
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                else:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)


def _write(path: Path, entries: list[str]) -> None:
    """Atomic replace in the same folder, keeping the file's permissions."""
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    handle, temporary = tempfile.mkstemp(prefix=".mem_", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(DELIMITER.join(entries))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _backup_dir(home: Path) -> Path:
    folder = Path(home) / "evolution" / "memory-backups"
    folder.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(folder, 0o700)
    return folder


def _backup(home: Path, name: str, raw: str, now: float) -> str:
    """An owner-only copy of the file as it was, never over another copy. Returns its file name."""
    folder = _backup_dir(home)
    for n in range(1000):
        candidate = folder / f"{name}.{int(now)}{f'.{n}' if n else ''}.bak"
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(raw)
        _prune(folder)
        return candidate.name
    raise ValueError("Cannot keep a copy of the memory file")


def _prune(folder: Path) -> None:
    copies = sorted(folder.glob("*.bak"), key=lambda item: item.stat().st_mtime)
    for old in copies[:-BACKUPS_KEPT]:
        with contextlib.suppress(OSError):
            old.unlink()


def remove(home: Path, name: str, mark: str, *, now: float, actor: str = "terminal", runtime_config=None) -> dict:
    """Take out one entry (the last one with this fingerprint) while it is still there, and log the decision."""
    from . import decisions
    from .experience import experience

    path = _path(home, name)
    if not path.is_file():
        raise ValueError("Unknown memory file")
    with _locked(path):
        raw = _read(path)
        if raw is None:
            raise ValueError("Cannot read the memory file")
        entries = parse(raw)
        found = [index for index, entry in enumerate(entries) if fingerprint(entry) == mark]
        if not found:
            return {"removed": False, "reason": "changed"}
        position = found[-1]
        copy = _backup(home, name, raw, now)
        kept = entries[:position] + entries[position + 1:]
        _write(path, kept)
    import sqlite3
    import time

    decision = None
    for attempt in range(3):   # the file is changed already: its record must not be lost to a busy moment
        try:
            with experience(home) as conn:
                decision = decisions.record_memory(conn, name, mark, position=position, backup=copy,
                                                   before=len(entries), after=len(kept), copies=len(found), now=now,
                                                   actor=actor)
            break
        except (sqlite3.Error, OSError):
            time.sleep(0.5 * (attempt + 1))
    return {"removed": True, "file": name, "entries": len(kept), "backup": copy, "decision": decision,
            "logged": decision is not None}


def restore(home: Path, name: str, mark: str, backup: str, *, position=None, copies=None, runtime_config=None) -> dict:
    """Put an entry back from the copy kept when it was removed, while the file still holds it as the removal left it
    (`copies` - 1 times: an exact repeat keeps its other copy) and it still fits the limit."""
    path = _path(home, name)
    folder = Path(home) / "evolution" / "memory-backups"
    if not isinstance(backup, str) or "/" in backup or "\\" in backup or not backup.endswith(".bak"):
        return {"restored": False, "reason": "no_copy"}
    saved = _read(folder / backup)
    text = next((entry for entry in parse(saved or "") if fingerprint(entry) == mark), None)
    if text is None:
        return {"restored": False, "reason": "no_copy"}
    with _locked(path):
        raw = _read(path) if path.exists() else ""
        if raw is None:
            raise ValueError("Cannot read the memory file")
        entries = parse(raw)
        left = (copies - 1) if type(copies) is int and copies >= 1 else 0
        if sum(fingerprint(entry) == mark for entry in entries) != left:
            return {"restored": False, "reason": "changed_since"}
        size = sum(map(len, entries)) + len(DELIMITER) * len(entries) + len(text)
        if size > limit_of(name, runtime_config):
            return {"restored": False, "reason": "no_room"}
        at = position if type(position) is int and 0 <= position <= len(entries) else len(entries)
        _write(path, entries[:at] + [text] + entries[at:])
    return {"restored": True, "file": name}
