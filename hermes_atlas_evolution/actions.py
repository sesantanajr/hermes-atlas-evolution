"""Actions the plugin takes for the owner (0.8.3): a proposal applied with one click, and automatic actions.

`auto_actions` (a setting the agent cannot widen, see cli.TRUSTED_ONLY) says what runs by itself at the end of each
learning pass:
- "off": nothing; everything waits for the owner.
- "safe" (default): copies of a memory entry that are the very same text, character for character, are taken
  out of Hermes' memory (a copy of the file is kept); nothing else. An entry the owner put back stays.
- "full": also the reversible changes to Hermes of explain.AUTOMATIC (today: locking a skill the curator keeps
  rewriting), at most MAX_CHANGES_PER_PASS per pass, with Hermes' own command; one Hermes refuses waits a day.

Every action is recorded in the decision history with "automatic" as the actor and can be undone from the menu,
the panel or `hermes evolution decisions undo <id>`. An automatic change the owner undoes is never applied
automatically again (`auto_skip:<code>`).
"""

from __future__ import annotations

import json
import re
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


def _hermes(home: Path, args: list, *, runner=None) -> None:
    """Run a plan's arguments. ["@<profile>", ...] runs in that profile of the same Hermes (`hermes --profile`), and
    ["@<profile>", "__trash__" | "__restore__", <folder>] moves a failed install's folder to the plugin's trash or back."""
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


def _trash(home: Path, target: Path, profile: str, folder: str, *, back: bool) -> None:
    """Move a failed install's folder (`plugins/.install-*` of profile `profile`) to this profile's own
    `evolution/trash/<profile>/`, or back. Only such folders; never through a link (any part of either path); never
    over something that is already there. Entries kept longer than 30 days are deleted from the trash."""
    import os
    import re as _re
    import shutil
    import stat
    import time

    if not _re.fullmatch(r"\.install-[A-Za-z0-9_-]{1,64}", folder) or not _re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", profile):
        raise ValueError("Only a failed install's folder is moved")
    own, target = Path(home).resolve(), Path(target).resolve()
    plugins = _real_folder_chain(target, ["plugins"], create=False)
    trash = _real_folder_chain(own, ["evolution", "trash", profile], create=not back)
    live, kept = plugins / folder, trash / folder
    source, destination = (kept, live) if back else (live, kept)
    try:
        if not stat.S_ISDIR(os.lstat(source).st_mode):
            raise ValueError("Nothing to move")
    except FileNotFoundError:
        raise ValueError("Nothing to move") from None
    if os.path.lexists(destination):
        raise ValueError("Something is already there")
    os.rename(source, destination)   # same file system: never a copy that could follow a link
    now = time.time()
    try:
        entries = list(os.scandir(trash))
    except OSError:
        entries = []
    for entry in entries:
        try:
            if _re.fullmatch(r"\.install-[A-Za-z0-9_-]{1,64}", entry.name) and entry.is_dir(follow_symlinks=False) \
                    and now - entry.stat(follow_symlinks=False).st_mtime > TRASH_KEEP_SECONDS:
                shutil.rmtree(entry.path, ignore_errors=True)
        except OSError:
            continue


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
    names = {subject}
    root = Path(home) / "skills"
    try:
        for found, skill_file in enumerate(root.rglob("SKILL.md")):
            if found >= 2000 or len(skill_file.relative_to(root).parts) > 5:
                break
            try:
                head = skill_file.read_text(encoding="utf-8", errors="replace")[:4096]
            except OSError:
                continue
            if any(line.split(":", 1)[1].strip().strip("'\"") == subject for line in head.splitlines()[:30]
                   if line.startswith("name:")):
                names.add(skill_file.parent.name)
    except OSError:
        pass
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
    return {"id": row["id"], "applied": True, "decision": decision, "undo": explain.command_text(found[1])}


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
        except (TypeError, ValueError) as error:   # Stale included: checked again on disk, nothing ran
            skipped.append({"id": raw, "reason": str(error)[:160]})
            continue
        except (OSError, sqlite3.Error):
            skipped.append({"id": raw, "reason": "busy"})
            break
        (applied.append(result["id"]) if result.get("applied") else skipped.append({"id": result["id"],
                                                                                   "reason": result.get("reason")}))
    return {"applied": applied, "skipped": skipped}


def undo_applied(home: Path, result: dict, *, now: float, runner=None) -> dict:
    """Undo an applied proposal (decisions.undo derived the undo from what was applied): run it, close the proposal as
    the owner's refusal, and keep the autopilot from applying it again."""
    from . import decisions
    from .experience import experience

    _hermes(home, list(result["undo"]), runner=runner)
    with experience(home) as conn:
        conn.execute("UPDATE proposal SET status='rejected', note='undone', updated=? WHERE id=?",
                     (now, int(result["proposal"])))
        if result.get("code"):
            conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES (?, ?)", (f"auto_skip:{result['code']}", str(now)))
        decisions.mark_undone(conn, result["id"], now=now)
    if str(result.get("code") or "").startswith("env:"):
        from .environment import remember, root_of

        remember(root_of(home), skip=[result["code"]])   # kept for whichever profile looks after the Hermes next
    return {**result, "undone": True}


def memory_skip(conn, name: str, mark: str, now: float) -> None:
    """Never take this entry out by itself again (the owner put it back)."""
    conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES (?, ?)", (f"auto_skip:memory:{name}:{mark}", str(now)))


def _memory(home: Path, runtime_config, *, now: float) -> list[dict]:
    """Take out copies of an entry that are the very same text, character for character. An entry that differs in
    anything, case and spacing included (a path, a password, an indented block), is the owner's to decide."""
    from . import memory_care
    from .experience import experience

    with experience(home) as conn:
        skipped = {row[0] for row in conn.execute("SELECT key FROM cursor WHERE key LIKE 'auto_skip:memory:%'")}
    removed = []
    for _ in range(MAX_MEMORY_PER_PASS):   # read again after each removal: positions and repeats change
        issues = [item for item in memory_care.review(home, runtime_config)["issues"]
                  if item["kind"] == "exact" and item.get("identical")
                  and f"auto_skip:memory:{item['suggested_drop']['file']}:{item['suggested_drop']['fingerprint']}" not in skipped]
        if not issues:
            break
        drop = issues[0]["suggested_drop"]
        result = memory_care.remove(home, drop["file"], drop["fingerprint"], now=now, actor="automatic",
                                    runtime_config=runtime_config)
        if not result.get("removed"):
            break
        removed.append({"file": drop["file"], "decision": result["decision"]})
    return removed


def automatic(home: Path, *, now: float, mode: str, runtime_config=None, runner=None, usage=None,
              deadline: float | None = None) -> dict:
    """The automatic actions of one pass. Failures are reported, never raised: the pass itself must not fail.
    `deadline` (time.monotonic) leaves Hermes commands out when one might not finish before it."""
    import time

    from .experience import experience

    done: dict = {"mode": mode if mode in MODES else "off", "memory": [], "applied": [], "failed": []}
    if done["mode"] == "off":
        return done
    try:
        done["memory"] = _memory(home, runtime_config, now=now)
    except (OSError, ValueError) as error:
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
    if deadline is None or deadline - time.monotonic() >= NEEDED_SECONDS["pm_gc"]:
        try:
            done["maintenance"] = reclaim(home, now=now, runner=runner)
        except Exception as error:   # never fails the pass
            done["failed"].append({"what": "pm_gc", "error": str(error)[:120]})
    return done


def _target_allows(home: Path, subject) -> bool:
    """A change by itself in another profile: allowed where the plugin is not on (the lead's mode decides), and where
    it is on only if that profile's own mode is auto too."""
    from .environment import profile_mode

    profile = str(subject or "").split("/", 1)[0]
    try:
        target = _profile_home(home, profile)
    except ValueError:
        return False
    if Path(target).resolve() == Path(home).resolve():
        return True
    mode = profile_mode(target)
    return mode is None or mode == "auto"


RECLAIM_EVERY_SECONDS = 7 * 86_400


def reclaim(home: Path, *, now: float, runner=None) -> dict | None:
    """Free old tool versions nothing references with Hermes' own `hermes pm gc`, when the health check found 300 MB
    or more of them, at most once a week. Recorded in the history (nothing to undo: Hermes fetches a version again if
    something asks for it)."""
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
    if "unused_tools" not in (care.get("findings") or ()) or now - ran < RECLAIM_EVERY_SECONDS:
        return None
    result = hermes_care.run_action(home, "pm_gc", runner=runner)
    with experience(home) as conn:
        conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('auto_gc', ?)", (str(now),))
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

    if _digest_scheduled(home):
        return {"scheduled": False, "reason": "already"}
    spec = digest_schedule_spec(home, weekday=weekday, hour=hour, lang=lang if lang in ("en", "pt") else "en",
                                deliver=deliver)
    script = Path(spec["script_path"])
    script.parent.mkdir(parents=True, exist_ok=True)
    temporary = script.with_name(script.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(spec["script_content"])
    os.replace(temporary, script)
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


def recent(conn, *, now: float, days: int = 7) -> list[dict]:
    """The automatic actions of the last `days`, newest first, as the history shows them."""
    rows = conn.execute("SELECT * FROM decision_log WHERE actor='automatic' AND at >= ? ORDER BY id DESC LIMIT 50",
                        (now - days * 86_400,)).fetchall()
    return [{"id": row["id"], "at": row["at"], "kind": row["kind"], "target": row["target"], "action": row["action"],
             "after": json.loads(row["after"]), "undone": row["undone"]} for row in rows]

