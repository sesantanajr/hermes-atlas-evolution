"""Actions the plugin takes for the owner: a proposal applied with one click, and automatic actions.

The `mode` (a setting the agent cannot widen, see cli.TRUSTED_ONLY; internally auto_actions full/safe/off) says what
runs by itself at the end of each learning pass:
- manual ("off"): nothing; everything waits for the owner.
- assist ("safe"): an entry said character for character in both MEMORY.md and USER.md (both loaded) is taken out
  of MEMORY.md, only while the copy in USER.md is still there (a copy of the file is kept); nothing else. A repeat
  inside one file is left to Hermes, which drops it itself. An entry the owner put back stays.
- auto ("full", the default): also the reversible changes of explain.AUTOMATIC (locking a skill the curator keeps
  rewriting, installing this plugin where a profile in use never had it, moving a failed install's folder to the
  trash), at most MAX_CHANGES_PER_PASS per pass, with Hermes' own command; one Hermes refuses waits a day. The plugin
  installs itself only while the repository's default branch is on the commit the lead runs. The lead profile also
  brings this plugin's copy in the other profiles to its own commit (fleet_update.auto_sync: a pinned, older,
  different or half-installed copy), where that profile's own mode is auto too. A profile with `fleet_managed: false`
  is never changed by itself.

Every action is recorded in the decision history with "automatic" as the actor and can be undone from the menu,
the panel or `hermes evolution decisions undo <id>`. An automatic change the owner undoes is never applied
automatically again (`auto_skip:<code>`).
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

MODES = ("off", "safe", "full")
MAX_CHANGES_PER_PASS = 3
MAX_MEMORY_PER_PASS = 10
FAILURE_BACKOFF_SECONDS = 86_400   # an automatic change Hermes refused is tried again a day later
STALE_APPLYING_SECONDS = 900       # a claim left by a process that died while Hermes ran
COMMAND_SECONDS = 180              # Hermes may finish its own maintenance before the command (after an update)
COMMAND_MIN_SECONDS = 30           # a pass this close to its deadline runs no Hermes command (a lock takes ~2 s)
# Time a pass must still have before it starts each kind of change (Hermes may download a plugin, or collect garbage
# for minutes): a pass is never cut in the middle of one.
NEEDED_SECONDS = {"self_missing": COMMAND_SECONDS + 30, "pm_gc": 330}
PM_GC_SECONDS = 900                # `hermes pm gc` from the menu; a pass gives it only what is left of its own time
DIGEST_CLAIM_SECONDS = 600         # a claim on scheduling the digest left by a process that died


def _hermes(home: Path, args: list, *, runner=None) -> None:
    """Run a plan's arguments. ["@<profile>", ...] runs in that profile of the same Hermes (`hermes --profile`), and
    ["@<profile>", "__trash__" | "__restore__", <folder>] moves a failed install's folder, or a cache folder Hermes never
    cleans, to the plugin's trash or back."""
    from .admin import run_hermes

    if args and str(args[0]).startswith("@"):
        target = _profile_home(home, str(args[0])[1:])
        if len(args) == 3 and args[1] in ("__trash__", "__restore__"):
            _trash(home, target, str(args[0])[1:], str(args[2]), back=args[1] == "__restore__")
            return
        run_hermes(target, list(args[1:]), runner=runner, timeout=COMMAND_SECONDS, what="command")
        return
    run_hermes(home, args, runner=runner, timeout=COMMAND_SECONDS, what="command")


def _profile_home(home: Path, name: str) -> Path:
    """The home of profile `name` of the same Hermes as `home`; ValueError for anything that is not one."""
    from .environment import profiles, root_of

    for profile, folder in profiles(root_of(home)):
        if profile == name:
            return Path(folder)
    raise ValueError("Unknown profile")


TRASH_KEEP_SECONDS = 30 * 86_400


class Stale(ValueError):
    """A change in another profile that is not true any more when read again from disk: nothing ran."""


def _real_folder_chain(base: Path, parts: list[str], *, create: bool) -> Path:
    """`base`/parts..., each part a real folder (never a link), made owner-only where it is created. ValueError when a
    part is a link or not a folder."""
    import os
    import stat

    current = Path(base)
    for part in parts:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if not create:
                raise ValueError("Nothing to move") from None
            os.mkdir(current, 0o700)
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("Not a folder, or a link: nothing was moved")
    return current


_LEFTOVER = re.compile(r"\.install-[A-Za-z0-9_-]{1,64}")
_CACHE_FOLDER = re.compile(r"cache/([A-Za-z0-9][A-Za-z0-9._-]{0,95})")
_TRASHED = re.compile(r"(\.install-[A-Za-z0-9_-]{1,64}|cache\.[A-Za-z0-9][A-Za-z0-9._-]{0,95})")
_NEVER_CACHE = ("scratch", "terminal")   # Hermes' own cleaner reaps these two


def _trash_places(folder: str) -> tuple[list[str], str, str]:
    """(the folders under the profile that hold it, its name there, its name in the trash) for the two kinds of
    folders the plugin moves: a failed install (`plugins/.install-*`) and a folder of the profile's cache that Hermes'
    cleaner never touches (`cache/<name>`)."""
    if _LEFTOVER.fullmatch(folder):
        return ["plugins"], folder, folder
    cache = _CACHE_FOLDER.fullmatch(folder)
    if cache and cache.group(1) not in _NEVER_CACHE and cache.group(1) not in (".", ".."):
        return ["cache"], cache.group(1), "cache." + cache.group(1)
    raise ValueError("Only a failed install's folder or a cache folder is moved")


def _trash(home: Path, target: Path, profile: str, folder: str, *, back: bool) -> None:
    """Move a folder of profile `profile` (a failed install's `plugins/.install-*`, or a `cache/<name>` Hermes never
    cleans) to this profile's own `evolution/trash/<profile>/`, or back. Never through a link (any part of either
    path); never over something that is already there. An entry is deleted from the trash 30 days after it entered
    (the date it entered is the folder's own date, set when it is moved in)."""
    import os
    import stat
    import time

    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", profile):   # Hermes' rule for a profile name
        raise ValueError("Only a profile of this Hermes")
    parents, live_name, kept_name = _trash_places(folder)
    own, target = Path(home).resolve(), Path(target).resolve()
    holder = _real_folder_chain(target, parents, create=False)
    trash = _real_folder_chain(own, ["evolution", "trash", profile], create=not back)
    live, kept = holder / live_name, trash / kept_name
    source, destination = (kept, live) if back else (live, kept)
    try:
        if not stat.S_ISDIR(os.lstat(source).st_mode):
            raise ValueError("Nothing to move")
    except FileNotFoundError:
        raise ValueError("Nothing to move") from None
    if os.path.lexists(destination):
        if back:   # Hermes made the folder again meanwhile (a cache rebuilds itself): the copy stays in the trash
            raise ValueError("The folder exists again; the copy in the trash stays until it is deleted")
        raise ValueError("Something is already there")
    os.rename(source, destination)   # same file system: never a copy that could follow a link
    now = time.time()
    if not back:
        try:   # a rename keeps the folder's old date: the 30 days count from today
            os.utime(destination, (now, now), follow_symlinks=False)
        except (OSError, NotImplementedError):
            os.utime(destination, (now, now))
    _purge(trash, now=now, keep=kept_name)


def _writable_again(function, path, _error) -> None:
    """shutil.rmtree's error handler: a read-only file (git keeps its packs read-only, and Windows refuses to delete
    a read-only file) or a folder without write permission is made writable by its owner and deleted again. Never
    through a link; what still cannot be deleted stays (the next pass tries again)."""
    import os
    import stat

    try:
        parent = os.path.dirname(path)
        if parent and stat.S_ISDIR(os.lstat(parent).st_mode):
            os.chmod(parent, stat.S_IMODE(os.lstat(parent).st_mode) | stat.S_IRWXU)
        info = os.lstat(path)
        if not stat.S_ISLNK(info.st_mode):
            os.chmod(path, stat.S_IMODE(info.st_mode) | stat.S_IWRITE | stat.S_IREAD
                     | (stat.S_IXUSR if stat.S_ISDIR(info.st_mode) else 0))
        function(path)
    except (OSError, NotImplementedError):
        pass


def _delete_tree(path: str) -> bool:
    """Delete a folder of the trash, read-only files included; whether it is gone."""
    import os
    import shutil
    import sys

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_writable_again)
    else:
        shutil.rmtree(path, onerror=_writable_again)
    return not os.path.lexists(path)


def _purge(trash: Path, *, now: float, keep: str = "") -> int:
    """Delete what entered one trash folder more than 30 days ago; the number of entries deleted."""
    import os

    try:
        entries = list(os.scandir(trash))
    except OSError:
        return 0
    gone = 0
    for entry in entries:
        try:
            if entry.name != keep and _TRASHED.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False) \
                    and now - entry.stat(follow_symlinks=False).st_mtime > TRASH_KEEP_SECONDS:
                gone += _delete_tree(entry.path)
        except OSError:
            continue
    return gone


def empty_trash(home: Path, *, now: float) -> int:
    """Every pass: delete what has been in this profile's trash for more than 30 days (it frees the disk even when
    nothing else goes to the trash). Never through a link."""
    import os
    import stat

    base = Path(home) / "evolution" / "trash"
    try:
        if not stat.S_ISDIR(os.lstat(base).st_mode):
            return 0
        folders = [entry for entry in os.scandir(base) if entry.is_dir(follow_symlinks=False)]
    except OSError:
        return 0
    return sum(_purge(Path(entry.path), now=now) for entry in folders)


def recover(conn) -> None:
    """Reopen a claim a process left when it died while Hermes ran. The learning pass calls it before it refreshes the
    proposals, so a claim the loop did not write itself expires there instead of being applied."""
    import time

    real = time.time()   # claims carry the real clock, never a pass's start time
    conn.execute("UPDATE proposal SET status='open', updated=? WHERE status='applying' AND updated < ?",
                 (real, real - STALE_APPLYING_SECONDS))


def _held_by_owner(home: Path, subject: str, usage: dict) -> bool:
    """Whether the skill is already locked or pinned. Hermes keys both by the skill's folder, which may differ from the
    name a proposal carries (the SKILL.md frontmatter): the folders that declare this name count too."""
    from .environment import _is_dir, _skill_files, _skill_text

    names = {subject}
    root = Path(home) / "skills"
    # the walk and the reading of the environment scan: never through a link, never a pipe or a device, each file
    # up to 256 KB, at most 3,000 skills five folders deep
    for skill_file in _skill_files(root) if _is_dir(root) else ():
        parsed = _skill_text(skill_file)
        if parsed is not None and parsed[0] == subject:
            names.add(skill_file.parent.name)
    return any(isinstance(usage.get(name), dict) and (usage[name].get("locked") or usage[name].get("pinned"))
               for name in names)


def _usage(home: Path) -> dict:
    try:
        from .skill_health import read_usage

        found = read_usage(home)
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def apply_proposal(home: Path, proposal_id: int, *, now: float, actor: str = "terminal", runner=None,
                   usage: dict | None = None) -> dict:
    """Apply a proposal the plugin can apply (explain.plan) with Hermes' own command, and record it.

    The proposal is claimed first (status `applying`, only from open or approved), so a decision made meanwhile in the
    panel or another terminal, or a second click, finds it taken and nothing runs twice; Hermes runs with no
    transaction held; a refusal puts the proposal back as it was. A skill the owner already locked or pinned is left
    as it is and no undo is recorded: undoing must never unlock the owner's own lock."""
    from . import decisions, explain
    from .experience import experience

    import time

    with experience(home) as conn:
        recover(conn)
        row = conn.execute("SELECT * FROM proposal WHERE id=?", (int(proposal_id),)).fetchone()
        if row is None:
            raise ValueError(f"Unknown proposal id {proposal_id}")
        found = explain.plan(dict(row))
        if found is None:
            raise ValueError("The plugin cannot apply this proposal; approve it to see the command")
        previous = row["status"]
    if row["kind"] in explain.FLEET:
        # Another profile is changed: what is on disk now decides, never the stored proposal or the cached scan.
        from .environment import verify

        fresh = verify(home, dict(row), now=now)
        again = explain.plan(fresh) if fresh is not None else None
        if again is None or again != found:
            with experience(home) as conn:
                conn.execute("UPDATE proposal SET status='expired', updated=? WHERE id=? AND status IN ('open', 'approved')",
                             (now, row["id"]))
            from .environment import forget

            forget(home, row["code"])   # the cached look would reopen it at the next pass
            raise Stale("Not so any more: checked again on disk, the situation changed or is gone; nothing was run")
    with experience(home) as conn:
        claimed = conn.execute("UPDATE proposal SET status='applying', updated=? WHERE id=? AND status IN "
                               "('open', 'approved')", (time.time(), row["id"])).rowcount
    if not claimed:
        return {"id": row["id"], "applied": False, "reason": "not_open"}
    if row["kind"] == "skill_lock" and _held_by_owner(home, row["subject"], usage if usage is not None else _usage(home)):
        with experience(home) as conn:
            conn.execute("UPDATE proposal SET status='applied', updated=? WHERE id=? AND status='applying'", (now, row["id"]))
        return {"id": row["id"], "applied": False, "reason": "already"}
    try:
        _hermes(home, found[0], runner=runner)
    except BaseException:
        with experience(home) as conn:
            conn.execute("UPDATE proposal SET status=?, updated=? WHERE id=? AND status='applying'",
                         (previous, now, row["id"]))
        raise
    with experience(home) as conn:
        still = conn.execute("UPDATE proposal SET status='applied', updated=? WHERE id=? AND status='applying'",
                             (now, row["id"])).rowcount
        if not still:   # the claim was taken back meanwhile (a process thought this one dead): the other one records it
            return {"id": row["id"], "applied": False, "reason": "not_open"}
        decision = decisions.record_applied(conn, row["id"], code=row["code"], before=previous, apply=found[0],
                                            undo=found[1], now=now, actor=actor)
    if row["kind"] == "self_missing":   # from now on that profile has had the plugin: turning it off there is kept
        from .environment import remember, root_of

        remember(root_of(home), seen=[str(row["subject"]).split("/", 1)[0]])
    return {"id": row["id"], "applied": True, "decision": decision, "undo": explain.command_text(found[1]),
            "undo_args": found[1]}


def apply_many(home: Path, ids: list, *, now: float, actor: str = "terminal", runner=None) -> dict:
    """Apply several proposals one after the other (the panel's and the menu's "apply all")."""
    import sqlite3

    applied, skipped = [], []
    usage = _usage(home)
    from .experience import experience
    from .explain import NOT_BULK

    for raw in ids[:50]:
        try:
            with experience(home) as conn:
                row = conn.execute("SELECT kind FROM proposal WHERE id=?", (int(raw),)).fetchone()
            if row is not None and row["kind"] in NOT_BULK:
                skipped.append({"id": raw, "reason": "one_by_one"})   # which plugin to keep is decided one by one
                continue
            result = apply_proposal(home, int(raw), now=now, actor=actor, runner=runner, usage=usage)
        except (TypeError, ValueError, OverflowError) as error:   # Stale included: checked again on disk, nothing ran
            skipped.append({"id": raw, "reason": str(error)[:160]})
            continue
        except (OSError, sqlite3.Error):
            skipped.append({"id": raw, "reason": "busy"})
            break
        (applied.append(result["id"]) if result.get("applied") else skipped.append({"id": result["id"],
                                                                                   "reason": result.get("reason")}))
    return {"applied": applied, "skipped": skipped}


def undo_applied(home: Path, result: dict, *, now: float, runner=None) -> dict:
    """Undo an applied proposal (decisions.undo derived the undo from what was applied). The owner's "no" is written
    first: the skip markers (here, and in the shared state for a change in another profile) and the proposal marked as
    being undone. Only then Hermes runs, and the proposal is closed as the owner's refusal. A failure in between (a
    busy database, the shared state's lock) never leaves an undone change the Mode auto would apply again; a failure
    before Hermes runs changes nothing in Hermes."""
    import time

    from . import decisions
    from .experience import experience

    code, proposal = str(result.get("code") or ""), int(result["proposal"])
    with experience(home) as conn:
        if code:
            conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES (?, ?)", (f"auto_skip:{code}", str(now)))
        conn.execute("UPDATE proposal SET note='undoing', updated=? WHERE id=? AND status='applied'", (now, proposal))
    if code.startswith("env:"):
        from .environment import remember, root_of

        remember(root_of(home), skip=[code])   # kept for whichever profile looks after the Hermes next
    try:
        _hermes(home, list(result["undo"]), runner=runner)
    except BaseException:
        try:   # nothing was undone: the proposal stays applied (the owner's "no" stays too: never re-applied by itself)
            with experience(home) as conn:
                conn.execute("UPDATE proposal SET note=NULL WHERE id=? AND note='undoing'", (proposal,))
        except (sqlite3.Error, OSError):
            pass
        raise
    for attempt in range(3):   # Hermes undid it: the record must not be lost to a busy moment
        try:
            with experience(home) as conn:
                conn.execute("UPDATE proposal SET status='rejected', note='undone', updated=? WHERE id=?", (now, proposal))
                decisions.mark_undone(conn, result["id"], now=now)
            break
        except (sqlite3.Error, OSError):
            if attempt == 2:
                raise
            time.sleep(0.5 * (attempt + 1))
    return {**result, "undone": True}


def memory_skip(conn, name: str, mark: str, now: float) -> None:
    """Never take this entry out by itself again (the owner put it back)."""
    conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES (?, ?)", (f"auto_skip:memory:{name}:{mark}", str(now)))


def _memory(home: Path, runtime_config, *, now: float) -> list[dict]:
    """Take out an entry said word for word in both MEMORY.md and USER.md while Hermes loads both: the copy in
    MEMORY.md, each time only while the copy in USER.md is still there (memory_care.cross_file_copies, remove). A
    repeat inside one file is left alone: Hermes drops it itself when it loads or writes the file, so taking it out
    saves nothing and could take the only copy left. An entry that differs in anything, case and spacing included (a
    path, a password, an indented block), is the owner's to decide; nothing goes when the configuration was not read."""
    from . import memory_care
    from .experience import experience

    with experience(home) as conn:
        skipped = {row[0] for row in conn.execute("SELECT key FROM cursor WHERE key LIKE 'auto_skip:memory:%'")}
    removed = []
    for _ in range(MAX_MEMORY_PER_PASS):   # read again after each removal: positions and repeats change
        pairs = [pair for pair in memory_care.cross_file_copies(home, runtime_config)
                 if f"auto_skip:memory:{pair['drop']['file']}:{pair['drop']['fingerprint']}" not in skipped]
        if not pairs:
            break
        drop, keep = pairs[0]["drop"], pairs[0]["keep"]
        result = memory_care.remove(home, drop["file"], drop["fingerprint"], keep=keep, now=now, actor="automatic",
                                    runtime_config=runtime_config)
        if not result.get("removed"):
            break
        removed.append({"file": drop["file"], "decision": result["decision"]})
    return removed


def automatic(home: Path, *, now: float, mode: str, runtime_config=None, runner=None, usage=None,
              deadline: float | None = None, remote=None) -> dict:
    """The automatic actions of one pass. Failures are reported, never raised: the pass itself must not fail.
    `deadline` (time.monotonic) leaves Hermes commands out when one might not finish before it. `remote` replaces
    fleet_update.remote_head (the commit the repository's default branch is on), read once per pass."""
    import time

    from .experience import experience
    from .fleet_update import remote_head

    head: dict = {}

    def latest() -> str | None:
        if "sha" not in head:
            head["sha"] = (remote or remote_head)()
        return head["sha"]

    done: dict = {"mode": mode if mode in MODES else "off", "memory": [], "applied": [], "failed": []}
    try:   # whatever the mode: the trash keeps what it got for 30 days, and no longer
        done["trash_emptied"] = empty_trash(home, now=now)
    except OSError:
        done["trash_emptied"] = 0
    if done["mode"] == "off":
        return done
    try:
        done["memory"] = _memory(home, runtime_config, now=now)
    except (OSError, ValueError, sqlite3.Error) as error:
        done["failed"].append({"what": "memory", "error": type(error).__name__})
    if done["mode"] != "full":
        return done
    from . import explain

    usage = usage if isinstance(usage, dict) else _usage(home)
    from .environment import root_of, state

    shared_skips = set(state(root_of(home))["skip"])
    with experience(home) as conn:
        held = {}
        for key, value in conn.execute("SELECT key, value FROM cursor WHERE key LIKE 'auto_skip:%' OR key LIKE 'auto_fail:%'"):
            held[key] = value
        rows = conn.execute(f"""SELECT id, code, kind, subject FROM proposal WHERE status='open'
            AND kind IN ({','.join('?' * len(explain.AUTOMATIC))}) ORDER BY id""", explain.AUTOMATIC).fetchall()
    for row in rows:
        if len(done["applied"]) >= MAX_CHANGES_PER_PASS:
            break
        if row["kind"] in explain.FLEET and not _target_allows(home, row["subject"]):
            continue   # that profile has the plugin, set to another mode: its owner decides there
        needed = NEEDED_SECONDS.get(row["kind"], COMMAND_MIN_SECONDS)
        if deadline is not None and deadline - time.monotonic() < needed:
            continue   # not enough time left in this pass for this one: the next pass
        expected = (f"env:{row['kind']}:{str(row['subject']).replace('/', ':', 1)}" if row["kind"] in explain.FLEET
                    else f"{row['kind']}:{row['subject']}")
        if row["code"] != expected or f"auto_skip:{row['code']}" in held or row["code"] in shared_skips \
                or explain.plan(dict(row)) is None:
            continue   # only the proposals the loop writes itself, with a plain name, and never one the owner undid
        try:
            failed_at = float(held.get(f"auto_fail:{row['code']}") or 0)
        except ValueError:
            failed_at = 0
        if now - failed_at < FAILURE_BACKOFF_SECONDS:
            continue
        if row["kind"] == "self_missing":
            # Hermes installs the repository's default branch as it is now (never `--ref`: a pin there breaks every
            # later update), so the plugin installs itself only while that is the very commit the lead runs, as
            # fleet_update.auto_sync spreads only code the owner already runs; else it waits for the next pass.
            from .environment import self_sha

            if "lead" not in head:
                head["lead"] = self_sha(home)
            if not head["lead"] or latest() != head["lead"]:
                done.setdefault("waiting", []).append({"proposal": row["id"], "kind": row["kind"],
                                                       "subject": row["subject"], "reason": "lead_commit"})
                continue
        try:
            result = apply_proposal(home, row["id"], now=now, actor="automatic", runner=runner, usage=usage)
        except Stale:
            continue   # checked again on disk and not so any more: expired, nothing ran
        except Exception as error:   # never fails the pass
            done["failed"].append({"proposal": row["id"], "error": str(error)[:120]})
            with experience(home) as conn:
                conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES (?, ?)", (f"auto_fail:{row['code']}", str(now)))
            if len(done["failed"]) >= 2:
                break   # two refusals in one pass: something is wrong with Hermes itself; the next pass tries again
            continue
        if result.get("applied"):
            done["applied"].append({"proposal": row["id"], "kind": row["kind"], "subject": row["subject"],
                                    "decision": result["decision"]})
    try:   # this plugin in every other profile on the lead's code: a pinned, older or half-installed copy
        from .fleet_update import auto_sync

        synced = auto_sync(home, now=now, runner=runner, deadline=deadline, remote=latest)
        if synced is not None:
            done["plugin_sync"] = synced
    except Exception as error:   # never fails the pass
        done["failed"].append({"what": "plugin_sync", "error": type(error).__name__})
    try:   # the gateway that cannot start because its service names a Python Hermes removed (203/EXEC)
        repaired = repair_gateway(home, now=now, deadline=deadline)
        if repaired is not None:
            done["gateway_repair"] = repaired
    except Exception as error:   # never fails the pass
        done["failed"].append({"what": "gateway_repair", "error": type(error).__name__})
    if deadline is None or deadline - time.monotonic() >= NEEDED_SECONDS["pm_gc"]:
        # never past the pass's own deadline: Hermes' command gets what is left of it (at most PM_GC_SECONDS)
        limit = None if deadline is None else max(COMMAND_MIN_SECONDS, min(
            PM_GC_SECONDS, int(deadline - time.monotonic()) - COMMAND_MIN_SECONDS))
        try:
            done["maintenance"] = reclaim(home, now=now, runner=runner, timeout=limit)
        except Exception as error:   # never fails the pass
            done["failed"].append({"what": "pm_gc", "error": str(error)[:120]})
    return done


def _target_allows(home: Path, subject) -> bool:
    """A change by itself in another profile: allowed where the plugin is not on (the lead's mode decides), and where
    it is on only if that profile's own mode is auto too; never in a profile that opted out of the fleet
    (environment.fleet_managed: `fleet_managed: false`), nor in one whose configuration cannot be read now (invalid
    while it is edited, too large): its owner's choices are unknown there, so nothing runs by itself this pass. The
    configuration is read once, and every answer comes from that one reading."""
    from .environment import _read_config, fleet_managed, profile_mode

    profile = str(subject or "").split("/", 1)[0]
    try:
        target = _profile_home(home, profile)
    except ValueError:
        return False
    config = _read_config(target)
    if not isinstance(config, dict):
        return False
    reader = lambda _home: config   # noqa: E731
    if not fleet_managed(target, read_config=reader):
        return False
    if Path(target).resolve() == Path(home).resolve():
        return True
    mode = profile_mode(target, read_config=reader)
    return mode is None or mode == "auto"


GATEWAY_REPAIR_EVERY_SECONDS = 86_400   # an automatic repair that did not bring the gateway up waits a day


def repair_gateway(home: Path, *, now: float, deadline: float | None = None, host=None) -> dict | None:
    """Self-repair of the Mode auto: the gateway's own systemd unit (user scope) names a Python that no longer exists
    (Hermes replaced its bundled Python; systemd fails with status=203/EXEC and loops), so it is pointed at the Python
    Hermes uses now, exactly as `hermes evolution care fix-gateway` does: a copy of the file is kept, the journal of
    repairs records it, and `hermes evolution care undo-gateway` puts it back (from any profile). Only by the profile
    that looks after the whole Hermes, only for the unit Hermes names for this home (or for the root whose gateway
    serves it, never a profile's legacy unit then), only when it runs this Hermes (its HERMES_HOME), never while any
    gateway of this Hermes runs (its process, heartbeat, pid file, lock, host record) or any gateway unit of either
    scope runs, starts or stops (hermes_care.start_blockers), never for a unit that is off and not failing, never in
    system scope (root's), never again for a situation the owner undid, at most once a day. Recorded in the history.
    None when nothing was due."""
    import os
    import time

    if host is None and os.name != "posix":
        return None   # systemd units: Linux only (`host` replaces the machine's commands and files)
    from . import decisions, hermes_care, system_update
    from .admin import profile_name
    from .environment import lead, root_of
    from .experience import experience

    if lead(root_of(Path(home)), now=now) not in (None, profile_name(home)):
        return None
    with experience(home) as conn:
        row = conn.execute("SELECT value FROM cursor WHERE key='care_check'").fetchone()
        last = conn.execute("SELECT value FROM cursor WHERE key='auto_gateway_repair'").fetchone()
    try:
        care = json.loads(row[0]) if row else {}
        tried = float(last[0]) if last else 0.0
    except (TypeError, ValueError):
        care, tried = {}, 0.0
    if not isinstance(care, dict) or "gateway_exec_missing" not in (care.get("findings") or ()):
        return None
    if 0 <= now - tried < GATEWAY_REPAIR_EVERY_SECONDS:
        return None
    if deadline is not None and deadline - time.monotonic() < 120:
        return None
    if hermes_care.live_gateways(home, now=now):
        return None   # a gateway of this Hermes runs (degraded and starting ones too): nothing is down
    served = bool(hermes_care.gateway(home, now=now).get("served_by_root"))
    host = host or system_update.Host()
    probe = system_update.probe_gateway_units(host, timeout=10)
    units = probe.get("units") or []
    names = hermes_care.expected_gateway_units(home, served=served, host=host)
    broken = [item for item in units if item.get("scope") == "user" and item.get("unit") in names
              and item.get("exec_exists") is False and item.get("replacement")]
    unit = hermes_care.pick_gateway_unit(broken, home=home, served=served, host=host) if broken else None
    if unit is None or unit.get("enabled") is False and not (unit.get("restart_loop") or unit.get("exec_missing")
                                                            or unit.get("active_state") == "failed"):
        return None   # a leftover that is off says nothing about the gateway
    if hermes_care.repair_skipped(home, unit):
        return None   # the owner undid this very repair: never again by itself
    if hermes_care.start_blockers(home, unit, units, now=now, unverified=probe.get("unverified") or (),
                                  automatic=True, served=served, host=host):
        return None   # a second gateway could start, or the unit is not this Hermes' gateway
    with experience(home) as conn:   # marked before it runs: another process never repairs the same unit meanwhile
        conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('auto_gateway_repair', ?)", (str(now),))
    root = root_of(Path(home))
    service = root if unit.get("unit") == hermes_care.gateway_unit_name(root, host=host) else Path(home)
    wait = system_update.START_WAIT_SECONDS if deadline is None else \
        max(10.0, min(system_update.START_WAIT_SECONDS, deadline - time.monotonic() - 60))
    try:
        result = system_update.fix_unit_exec(host, unit, now=now, home=service, wait=wait)
    except ValueError as error:
        result = {"ok": False, "unit": unit.get("unit"), "error": str(error)[:160]}
    if result.get("undo"):   # what `care undo-gateway` reads, from any profile of this Hermes
        try:
            hermes_care.record_repair_event(home, {"event": "repair", "at": now, "actor": "automatic",
                                                   "profile": profile_name(home), **result["undo"]})
        except OSError:
            pass
    hermes_care.refresh_after_repair(home, now, host=host)
    with experience(home) as conn:
        decision = decisions.record_gateway_repair(conn, str(result.get("unit") or unit.get("unit")),
                                                   before=str(result.get("from") or unit.get("exec_path") or ""),
                                                   after=str(result.get("to") or ""), ok=bool(result.get("ok")),
                                                   undo=bool(result.get("undo")), now=now)
    return {"unit": result.get("unit") or unit.get("unit"), "ok": bool(result.get("ok")), "from": result.get("from"),
            "to": result.get("to"), "error": result.get("error"), "state": result.get("state"), "decision": decision}


RECLAIM_EVERY_SECONDS = 7 * 86_400


def reclaim(home: Path, *, now: float, runner=None, timeout: int | None = None, host=None) -> dict | None:
    """Free old tool versions nothing references with Hermes' own `hermes pm gc`, when the health check found 300 MB
    or more of them, at most once a week. Right before, the health check is taken again (the stored one may be hours
    old): gc runs only if nothing it would delete is in use (a service's command, a running program) and every store
    it sweeps can be read (hermes_care.gc_blocked). `host`: the machine whose units are read. Recorded in the
    history (nothing to undo: Hermes fetches a version again if something asks for it)."""
    from . import decisions, hermes_care
    from .experience import experience

    with experience(home) as conn:
        row = conn.execute("SELECT value FROM cursor WHERE key='care_check'").fetchone()
        last = conn.execute("SELECT value FROM cursor WHERE key='auto_gc'").fetchone()
    try:
        care = json.loads(row[0]) if row else {}
        ran = float(last[0]) if last else 0.0
    except (ValueError, TypeError):
        care, ran = {}, 0.0
    if not isinstance(care, dict) or "unused_tools" not in (care.get("findings") or ()) \
            or now - ran < RECLAIM_EVERY_SECONDS:
        return None
    if hermes_care.gc_blocked(care):
        return None   # a tools folder gc would delete is one a service still starts from (or the check is old)
    from .admin import profile_name
    from .environment import lead, root_of

    if lead(root_of(Path(home)), now=now) not in (None, profile_name(home)):
        return None   # one tool store for the whole Hermes: the profile that looks after all of it frees it
    try:   # what is in use now, not hours (nor the minute a unit list is cached) ago
        with experience(home) as conn:
            care = hermes_care.cached(conn, home, now=now, force=True, host=host, fresh=True)
    except Exception:
        return None
    if not isinstance(care, dict) or "unused_tools" not in (care.get("findings") or ()) or hermes_care.gc_blocked(care):
        return None
    with experience(home) as conn:   # marked before it runs: a second process never starts another one meanwhile
        conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('auto_gc', ?)", (str(now),))
    result = hermes_care.run_action(home, "pm_gc", runner=runner, **({} if timeout is None else {"timeout": timeout}))
    with experience(home) as conn:
        decision = decisions.record_maintenance(conn, "pm_gc", reclaimable=care.get("reclaimable"),
                                                ok=bool(result.get("ok")), now=now, actor="automatic")
    return {"action": "pm_gc", "ok": bool(result.get("ok")), "decision": decision}


def schedule_digest(home: Path, *, deliver: str = "local", lang: str = "en", now: float, actor: str = "terminal",
                    runner=None, weekday: int = 1, hour: int = 8) -> dict:
    """Schedule the weekly digest as a Hermes cron job that runs no agent and spends no token (`--no-agent`, a
    script under the profile's scripts/), delivered to `deliver` (local, or a channel such as telegram). Once only:
    an existing job of the plugin is kept. Recorded with its undo (`hermes cron remove <job>`)."""
    import os

    from . import decisions
    from .admin import hermes_output
    from .evolve import digest_schedule_spec
    from .experience import experience
    from .status import _digest_scheduled

    import secrets
    import time

    # Claimed first (one row, taken atomically): two clicks, or the panel and a terminal at once, never create two
    # jobs; a claim left by a process that died expires after DIGEST_CLAIM_SECONDS.
    real = time.time()
    with experience(home) as conn:
        conn.execute("DELETE FROM cursor WHERE key='digest_schedule_claim' AND CAST(value AS REAL) < ?",
                     (real - DIGEST_CLAIM_SECONDS,))
        claimed = conn.execute("INSERT OR IGNORE INTO cursor(key, value) VALUES ('digest_schedule_claim', ?)",
                               (str(real),)).rowcount
    if not claimed:
        return {"scheduled": False, "reason": "busy"}
    temporary = None
    try:
        if _digest_scheduled(home):
            return {"scheduled": False, "reason": "already"}
        spec = digest_schedule_spec(home, weekday=weekday, hour=hour, lang=lang if lang in ("en", "pt") else "en",
                                    deliver=deliver)
        script = Path(spec["script_path"])
        script.parent.mkdir(parents=True, exist_ok=True)
        temporary = script.with_name(f".{script.name}.{secrets.token_hex(4)}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(spec["script_content"])
        os.replace(temporary, script)
        temporary = None
        args = spec["create_args"]
        args = args[args.index("cron"):]   # run_hermes names the profile itself
        code, out = hermes_output(home, args, runner=runner, timeout=180)
        found = re.search(r"Created job:?\s*([A-Za-z0-9_-]{1,64})", out)
        if code != 0 or not found:
            raise ValueError(" ".join(out.split())[-200:] or "Hermes refused the command")
        with experience(home) as conn:
            decision = decisions.record_cron(conn, found.group(1), name="hermes-atlas-evolution-digest",
                                             schedule=args[2], deliver=deliver, now=now, actor=actor)
        return {"scheduled": True, "job": found.group(1), "schedule": args[2], "deliver": deliver, "decision": decision}
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        try:
            with experience(home) as conn:
                conn.execute("DELETE FROM cursor WHERE key='digest_schedule_claim' AND value=?", (str(real),))
        except Exception:   # the claim expires by itself
            pass


def recent(conn, *, now: float, days: int = 7) -> list[dict]:
    """The automatic actions of the last `days`, newest first, as the history shows them."""
    rows = conn.execute("SELECT * FROM decision_log WHERE actor='automatic' AND at >= ? ORDER BY id DESC LIMIT 50",
                        (now - days * 86_400,)).fetchall()
    return [{"id": row["id"], "at": row["at"], "kind": row["kind"], "target": row["target"], "action": row["action"],
             "before": json.loads(row["before"]), "after": json.loads(row["after"]), "undone": row["undone"]}
            for row in rows]

