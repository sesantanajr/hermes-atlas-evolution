"""Care of Hermes' built-in memory (MEMORY.md and USER.md of a profile): find what repeats, and take out the copy
the owner chooses, safely.

Hermes injects both files into every session's system prompt, so an entry said twice costs tokens on every turn
and leaves less room for new memories. `review` finds:
- exact repeats (the same entry, ignoring case, spacing and Unicode forms), within a file or across the two;
- similar entries (most of their words in common), which the owner may want to keep once.
It never rewrites an entry's text: merging two entries into new words is the agent's job, with its memory tool.
Each entry is normalised once and exact repeats are grouped in one pass, so a full file (512 KB) is read in well under
a second; the word-by-word comparison looks at the first SIMILAR_MAX distinct entries only.

`remove` takes one entry out, by the fingerprint of its exact text, the way Hermes' memory tool writes: under the
same exclusive lock (`<file>.lock`, flock or msvcrt), after reading the file again, with an atomic replace that keeps
the file's owner and permissions. It names the copy the owner keeps, and removes nothing unless that copy is still
there too (Hermes drops a repeat of the very same text when it writes the file: the only copy left is never the one
taken out); when the two copies are in different files both files are locked, always in the same order. A copy of the
file is kept first in `<profile>/evolution/memory-backups/` (owner-only), and the decision is logged without the text
(the log keeps fingerprints, never memory content). `restore` puts the entry back from that copy, while it still fits.
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
SIMILAR_MAX = 300  # distinct entries compared word by word (the pairs grow with the square of the entries)
BACKUPS_KEPT = 100
MAX_ISSUES = 100   # the most useful first: exact repeats, then what one entry says of another, then similar ones
LOCK_WAIT_SECONDS = 5.0
_WORD = re.compile(r"\w{3,}")
_MARK = re.compile(r"[0-9a-f]{16}")


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


def config_known(runtime_config) -> bool:
    """False when Hermes' configuration could not be read (evolve.CONFIG_UNREAD): which file Hermes loads is then
    unknown, and nothing is taken out by itself."""
    from .evolve import CONFIG_UNREAD

    return not (isinstance(runtime_config, dict) and runtime_config.get(CONFIG_UNREAD))


def limit_of(name: str, runtime_config) -> int:
    key, default, _flag = FILES[name]
    value = _memory_config(runtime_config).get(key)
    return value if type(value) is int and value > 0 else default


def _path(home: Path, name: str) -> Path:
    if name not in FILES:
        raise ValueError("Unknown memory file")
    return Path(home) / "memories" / name


def _read(path: Path) -> str | None:
    """The file's text, or None when it is missing, not a regular file (a link, a pipe, a device), too large or not
    UTF-8. Read with the plugin's one safe reader (environment.read_bytes): never through a link, never blocking."""
    from .environment import read_bytes

    data = read_bytes(path, MAX_BYTES)
    if data is None:
        return None
    try:
        return data.decode("utf-8-sig")   # as Hermes reads it: a byte order mark is not text
    except UnicodeError:
        return None


def _drop_of(first: dict, second: dict, kind: str, files: dict) -> dict:
    """The copy to suggest taking out: of a fact said in both files, the one in the file Hermes does not load (taking
    out the other would hide the fact from the agent), else the one in MEMORY.md (what Hermes keeps about the user
    stays in USER.md); in one file, the later repeat (Hermes keeps the first), or the shorter of two similar ones."""
    if kind == "exact" and first["file"] != second["file"]:
        off = [entry for entry in (first, second) if not files[entry["file"]]["enabled"]]
        return off[0] if len(off) == 1 else first if first["file"] == "MEMORY.md" else second
    return second if kind == "exact" or len(second["text"]) <= len(first["text"]) else first


def _issue(first: dict, second: dict, kind: str, score: float, files: dict) -> dict:
    drop = _drop_of(first, second, kind, files)
    keep = second if drop is first else first
    identical = first["text"] == second["text"]
    # Hermes drops a repeat of the very same text in one file when it loads the file, and a file it does not load
    # costs nothing: taking out such a copy saves no token
    free = (identical and first["file"] == second["file"]) or not files[drop["file"]]["enabled"]
    return {"kind": kind, "score": score, "entries": [first, second], "identical": identical,
            "suggested_drop": {"file": drop["file"], "fingerprint": drop["fingerprint"]},
            "suggested_keep": {"file": keep["file"], "fingerprint": keep["fingerprint"]},
            "tokens_per_turn": 0 if free else max(1, len(drop["text"]) // 4)}


def _entries(home: Path, runtime_config) -> tuple[dict, list[dict]]:
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
    return files, entries


def review(home: Path, runtime_config=None) -> dict:
    """Each memory file's use of its limit, and what repeats, with the entries' text (for the owner's own terminal
    or logged-in panel: memory is the owner's). Each issue names the copy to take out and the one to keep."""
    files, entries = _entries(home, runtime_config)
    issues = []
    # exact repeats, in one pass: each later copy is paired with the first one of the very same text, and each other
    # way of writing it with the first copy of the entry, so k copies make k - 1 issues
    groups: dict[str, dict[str, list[dict]]] = {}
    for entry in entries:
        groups.setdefault(_normalized(entry["text"]), {}).setdefault(entry["text"], []).append(entry)
    anchors = []
    for spellings in groups.values():
        copies = list(spellings.values())
        anchor = copies[0][0]
        anchors.append(anchor)
        for same in copies:
            issues += [_issue(same[0], other, "exact", 1.0, files) for other in same[1:]]
            if same[0] is not anchor:
                issues.append(_issue(anchor, same[0], "exact", 1.0, files))
    # similar ones, word by word, among the first SIMILAR_MAX distinct entries
    pool = sorted(anchors, key=lambda entry: (entry["file"] != "MEMORY.md", entry["index"]))[:SIMILAR_MAX]
    words = [_words(entry["text"]) for entry in pool]
    numbers = [{word for word in found if any(ch.isdigit() for ch in word)} for found in words]
    for i, first in enumerate(pool):
        a, a_numbers = words[i], numbers[i]
        if not a:
            continue
        for j in range(i + 1, len(pool)):
            b = words[j]
            if not b or (a_numbers - b) or (numbers[j] - a):
                continue   # they differ in a number (a port, a version, an id): two facts, not one said twice
            shared = len(a & b)
            if not shared:
                continue
            jaccard, contained = shared / len(a | b), shared / min(len(a), len(b))
            if jaccard >= SIMILAR:
                issues.append(_issue(first, pool[j], "similar", round(jaccard, 2), files))
            elif contained >= CONTAINED and min(len(a), len(b)) >= 4:
                issues.append(_issue(first, pool[j], "contained", round(contained, 2), files))
    issues.sort(key=lambda item: ({"exact": 0, "contained": 1, "similar": 2}[item["kind"]], -item["score"]))
    total = len(issues)
    issues = issues[:MAX_ISSUES]
    return {"files": files, "issues": issues, "issues_total": total, "config_known": config_known(runtime_config),
            "tokens_saved_per_turn": sum(item["tokens_per_turn"] for item in issues if item["kind"] == "exact")}


def cross_file_copies(home: Path, runtime_config=None) -> list[dict]:
    """Entries said word for word in both MEMORY.md and USER.md while Hermes loads both: [{"drop", "keep"}], the copy
    in MEMORY.md taken out (what Hermes keeps about the user stays in USER.md). What the Mode assist removes by itself.
    Nothing when the configuration was not read, or a file is off (a copy Hermes does not load costs nothing), and
    never a repeat inside one file (Hermes drops those itself when it loads or writes the file)."""
    if not config_known(runtime_config):
        return []
    files, entries = _entries(home, runtime_config)
    if not all(info["enabled"] for info in files.values()):
        return []
    in_user = {entry["text"] for entry in entries if entry["file"] == "USER.md"}
    found, seen = [], set()
    for entry in entries:
        if entry["file"] == "MEMORY.md" and entry["text"] in in_user and entry["fingerprint"] not in seen:
            seen.add(entry["fingerprint"])
            found.append({"drop": {"file": "MEMORY.md", "fingerprint": entry["fingerprint"]},
                          "keep": {"file": "USER.md", "fingerprint": entry["fingerprint"]}})
    return found


@contextlib.contextmanager
def _locked(path: Path, *, wait_s: float = LOCK_WAIT_SECONDS):
    """Hermes' memory lock: an exclusive lock on `<file>.lock` (flock on POSIX, msvcrt on Windows)."""
    from .environment import exclusive_lock

    try:
        lock = exclusive_lock(path.with_name(path.name + ".lock"), wait_s=wait_s)
        lock.__enter__()
    except TimeoutError:
        raise ValueError("Hermes is writing its memory right now; try again in a moment") from None
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


def _write(path: Path, entries: list[str]) -> None:
    """Atomic replace in the same folder, keeping the file's permissions and, when the plugin runs as root, its owner
    (a profile's memory belongs to the user Hermes runs as; a root-owned file would lock that Hermes out of it)."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        info = None
    mode = info.st_mode & 0o777 if info is not None else 0o600
    handle, temporary = tempfile.mkstemp(prefix=".mem_", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(DELIMITER.join(entries))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        if info is not None and hasattr(os, "chown") and hasattr(os, "geteuid") and os.geteuid() == 0 \
                and (info.st_uid, info.st_gid) != (0, 0):
            os.chown(temporary, info.st_uid, info.st_gid)
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


def _kept(keep) -> tuple[str, str]:
    if not isinstance(keep, dict) or keep.get("file") not in FILES or not isinstance(keep.get("fingerprint"), str) \
            or not _MARK.fullmatch(keep["fingerprint"]):
        raise ValueError("Name the entry to keep: {file, fingerprint}")
    return keep["file"], keep["fingerprint"]


def remove(home: Path, name: str, mark: str, *, keep: dict, now: float, actor: str = "terminal",
           runtime_config=None) -> dict:
    """Take out one entry of file `name` (the last one with fingerprint `mark`), only while the copy the owner keeps
    (`keep`: {"file", "fingerprint"}) is still there too: another entry, or, for the very same text in the same file,
    another copy of it. Both files are read again under Hermes' lock (both locks, in name order, when the copies are in
    different files). The decision is logged."""
    from . import decisions
    from .experience import experience

    path = _path(home, name)
    if not path.is_file():
        raise ValueError("Unknown memory file")
    keep_file, keep_mark = _kept(keep)
    paths = {name: path, keep_file: _path(home, keep_file)}
    with contextlib.ExitStack() as held:
        for file in sorted(paths):   # always the same order: two removals never wait on each other for good
            held.enter_context(_locked(paths[file]))
        raws = {file: _read(where) for file, where in paths.items()}
        raw = raws[name]
        if raw is None:
            raise ValueError("Cannot read the memory file")
        entries = parse(raw)
        found = [index for index, entry in enumerate(entries) if fingerprint(entry) == mark]
        kept = sum(fingerprint(entry) == keep_mark for entry in parse(raws[keep_file] or ""))
        same = keep_file == name and keep_mark == mark
        if not found or kept < (2 if same else 1):
            return {"removed": False, "reason": "changed"}   # nothing taken out: the copy to keep is not there
        position = found[-1]
        copy = _backup(home, name, raw, now)
        left = entries[:position] + entries[position + 1:]
        _write(path, left)
    import sqlite3

    decision = None
    for attempt in range(3):   # the file is changed already: its record must not be lost to a busy moment
        try:
            with experience(home) as conn:
                decision = decisions.record_memory(conn, name, mark, position=position, backup=copy,
                                                   before=len(entries), after=len(left), copies=len(found), now=now,
                                                   actor=actor, keep={"file": keep_file, "fingerprint": keep_mark})
            break
        except (sqlite3.Error, OSError):
            time.sleep(0.5 * (attempt + 1))
    return {"removed": True, "file": name, "entries": len(left), "backup": copy, "decision": decision,
            "logged": decision is not None}


def backup_exists(home: Path, backup) -> bool:
    """Whether the copy of a memory file kept by a removal is still there (the newest BACKUPS_KEPT are kept)."""
    if not isinstance(backup, str) or "/" in backup or "\\" in backup or not backup.endswith(".bak"):
        return False
    from .environment import read_bytes

    return read_bytes(Path(home) / "evolution" / "memory-backups" / backup, MAX_BYTES) is not None


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
        raw = _read(path) if os.path.lexists(path) else ""   # a link or a pipe there is never written over
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
