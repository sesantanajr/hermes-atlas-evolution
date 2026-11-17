"""Keep this plugin current in every profile of a Hermes (0.9.1, rewritten in 0.9.2).

Hermes installs plugins per profile, so an update used to mean one `hermes -p <profile> plugins update ...` per
profile, a security-scan prompt in each, and a dead end in any profile pinned to a commit ("is pinned to <sha>").

Hermes keeps its install records outside the plugin's folder, in `<home>/plugins/.install-metadata.json`, one record
per plugin: {"pinned": true|false, "revision": "<sha>", "source": ...}. A pinned install stays pinned when it is
installed again without `--ref` (Hermes reinstalls the pinned commit), and `plugins update` refuses it; the only public
way to move it is `plugins install <source> --force --ref <40-character commit>`. So, with Hermes' own commands only:

* installed, not pinned  -> `plugins update hermes-atlas-evolution --force`
                            (a refusal falls back to a forced reinstall; a pin Hermes reports moves as below);
* pinned to a commit     -> `plugins install <url> --force --ref <commit>`: the pin moves to the commit this plugin
                            should run (the latest one of the repository for `hermes evolution update`, the lead
                            profile's own commit for the automatic sync);
* folder without a manifest (an interrupted install or update) -> `plugins install <url> --force`;
* missing (only on request) -> `plugins install <url> --enable`;
* a linked development checkout is never touched.

`--enable` is added only where the plugin is on already (a no-op there): updating a profile never switches the plugin
on where its owner switched it off. `--force` only skips Hermes' interactive scan prompt for this plugin, the
operator's own repository. Nothing here restarts the gateway: `status` tells when a running gateway still has the old
code, and the operator restarts it once.

`auto_sync` is the Mode auto's part: the lead profile brings the other profiles' copies to its own commit, at most
SYNC_MAX_PER_PASS per pass, once per situation, never where that profile's own mode is not auto. A pinned copy moves
with `--ref <lead's commit>`; an unpinned one is updated only while the repository's default branch is on the lead's
commit, so nothing the owner has not already run on the lead is ever installed by itself.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

PLUGIN = "hermes-atlas-evolution"
URL = "https://github.com/sesantanajr/hermes-atlas-evolution.git"
UPDATE_TIMEOUT = 300
REMOTE_TIMEOUT = 20
SYNC_MAX_PER_PASS = 2
SYNC_TIMEOUT = 180                 # one profile's update inside a learning pass
SYNC_NEEDED_SECONDS = SYNC_TIMEOUT + 30
SYNC_RETRY_SECONDS = 86_400        # a sync Hermes refused is tried again a day later
_SHA = re.compile(r"[0-9a-f]{40}")
_VERSION = re.compile(r"""^version:\s*["']?([0-9A-Za-z.+-]{1,32})["']?\s*$""", re.M)
_PINNED = re.compile(r"\bis pinned\b|\bpinned to\b", re.I)


def _read(path: Path, limit: int = 64 * 1024) -> str | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return None


def _git_sha(folder: Path) -> str | None:
    """The commit a plugin folder is on, read from .git without running git."""
    from .environment import git_origin

    _repo, sha = git_origin(folder)
    return sha or None


def _pin(home: Path) -> str | None:
    """The commit Hermes pinned this plugin to in that profile ("pinned" when the record names none), or None."""
    from .environment import install_record

    pinned = install_record(Path(home), PLUGIN, PLUGIN).get("pinned") or ""
    return str(pinned) or None


def _enabled(home: Path) -> bool | None:
    """Whether the profile's config.yaml lists the plugin as on; None when the file cannot be read."""
    from .environment import _plugin_lists, _read_config

    config = _read_config(Path(home))
    if config is None:
        return None
    enabled, disabled = _plugin_lists(config)
    return bool(enabled) and PLUGIN in enabled and PLUGIN not in disabled


def inspect(name: str, home: Path) -> dict:
    folder = Path(home) / "plugins" / PLUGIN
    linked = folder.is_symlink()
    present = folder.is_dir()
    manifest = _read(folder / "plugin.yaml") if present else None
    installed = manifest is not None
    version = None
    if manifest:
        found = _VERSION.search(manifest)
        version = found.group(1) if found else None
    return {"profile": name, "home": str(home), "installed": installed, "version": version,
            "sha": _git_sha(folder) if installed else None, "pinned": _pin(home),
            "broken": present and not installed and not linked, "linked": linked,
            "enabled": _enabled(home) if present else None, "is_profile": (Path(home) / "config.yaml").is_file()}


def plan(root: Path) -> list[dict]:
    """Every profile of this Hermes (the default first) with what is installed there."""
    from .environment import profiles

    return [inspect(name, home) for name, home in profiles(Path(root))]


def select(rows: list[dict], wanted) -> list[dict]:
    """The rows the operator picked: "all", or a list of profile names. An unknown name is refused, not ignored."""
    if wanted == "all":
        return [row for row in rows if row["is_profile"]]
    names = [item.strip() for item in wanted if isinstance(item, str) and item.strip()]
    known = {row["profile"]: row for row in rows}
    unknown = [name for name in names if name not in known]
    if unknown:
        raise ValueError(f"Unknown profile(s): {', '.join(unknown)}. Known: {', '.join(known)}")
    seen, picked = set(), []
    for name in names:
        if name not in seen:
            seen.add(name)
            picked.append(known[name])
    return picked


def remote_head(*, timeout: int = REMOTE_TIMEOUT, runner=None) -> str | None:
    """The commit the repository's default branch is on now (`git ls-remote`), None when it cannot be read."""
    import shutil

    git = shutil.which("git")
    if git is None and runner is None:
        return None
    run = subprocess.run if runner is None else runner
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="", SSH_ASKPASS="")
    try:
        result = run([git or "git", "ls-remote", URL, "HEAD"], env=env, stdin=subprocess.DEVNULL, capture_output=True,
                     encoding="utf-8", errors="replace", timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in (result.stdout or "").splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == "HEAD" and _SHA.fullmatch(sha.strip()):
            return sha.strip()
    return None


def _enable(row: dict) -> list:
    return ["--enable"] if row.get("enabled") is True else []


def step(row: dict, *, install_missing: bool, target: str | None = None) -> list | None:
    """The first Hermes command for one profile, or None when there is nothing to do (or nothing safe to do)."""
    if row.get("linked"):
        return None   # a development checkout: its owner updates it
    if not row["installed"] and not row.get("broken"):
        if not install_missing:
            return None
        # Hermes keeps a pin even after the folder is gone, and installs the pinned commit again without --ref
        return ["plugins", "install", URL, "--enable", *(["--ref", target] if row.get("pinned") and target else [])]
    if row.get("pinned"):
        if not target or (row["pinned"] == target and row.get("installed")):
            return None   # no commit to move the pin to, or it is there already
        return ["plugins", "install", URL, "--force", *_enable(row), "--ref", target]
    if row.get("broken"):
        return ["plugins", "install", URL, "--force", *_enable(row)]
    return ["plugins", "update", PLUGIN, "--force"]


def _skip_reason(row: dict, target: str | None) -> str:
    if row.get("linked"):
        return "linked"
    if not row["installed"] and not row.get("broken"):
        return "missing"
    if row.get("pinned") and not target:
        return "pinned_no_target"
    return "current"


def run(rows: list[dict], *, install_missing: bool = False, target: str | None = None, runner=None, executable=None,
        timeout: int = UPDATE_TIMEOUT, progress=None, remote=None) -> list[dict]:
    """Apply step() to each row, one profile at a time; a failure in one profile never stops the others.

    `target` is the commit a pinned profile moves to; by default the repository's latest one (`remote()`, read once,
    only when a profile needs it)."""
    from .admin import hermes_output

    resolved = {"done": target is not None, "sha": target}

    def wanted() -> str | None:
        if not resolved["done"]:
            resolved["done"] = True
            resolved["sha"] = (remote or remote_head)()
        return resolved["sha"]

    done = []
    for row in rows:
        aim = wanted() if row.get("pinned") and not row.get("linked") else resolved["sha"]
        command = step(row, install_missing=install_missing, target=aim)
        outcome = {"profile": row["profile"], "before": row.get("sha"), "commands": []}
        if command is None:
            outcome["status"] = "skipped"
            outcome["reason"] = _skip_reason(row, aim)
            done.append(outcome)
            continue
        if progress is not None:
            progress(row["profile"])
        code, text = hermes_output(Path(row["home"]), command, runner=runner, executable=executable,
                                   timeout=timeout)
        outcome["commands"].append({"args": command, "code": code})
        if code not in (0, 124, 126, 127) and command[1] == "update":
            # A pin the record did not show, an older Hermes without `update --force`, or a checkout that is not a
            # git one: a pin moves with --ref; anything else is installed again over the copy.
            pinned = bool(_PINNED.search(text))
            aim = wanted() if pinned else None
            if pinned and not aim:
                outcome["commands"][-1]["after"] = "pinned"
            else:
                fallback = ["plugins", "install", URL, "--force", *_enable(row), *(["--ref", aim] if pinned else [])]
                code, text = hermes_output(Path(row["home"]), fallback, runner=runner, executable=executable,
                                           timeout=timeout)
                outcome["commands"].append({"args": fallback, "code": code, "after": "pinned" if pinned
                                            else "update_failed"})
        after = inspect(row["profile"], Path(row["home"]))
        outcome.update(status="done" if code == 0 else "failed", after=after.get("sha"),
                       version=after.get("version"), pinned=after.get("pinned"))
        if code != 0:
            outcome["detail"] = " ".join(text.split())[-300:]
        done.append(outcome)
    return done


# ── the Mode auto: the lead brings every other profile to its own commit ──────────────────────────────────────────
def _newer(candidate: str | None, current: str | None) -> bool:
    if not candidate or not current:
        return False
    from .environment import _newer as newer

    return newer(str(candidate), str(current))


def _signature(row: dict) -> str:
    return "|".join(str(row.get(key) or "") for key in ("sha", "pinned", "version", "broken"))


def due(rows: list[dict], lead: dict, lead_sha: str, memo: dict, *, now: float, mode_of=None) -> list[tuple[dict, str]]:
    """(row, reason) for each other profile whose copy should be brought to the lead's commit by itself: broken,
    pinned elsewhere, older, or the same version on other code. Never a linked copy, a profile where the plugin is
    off, one whose own mode is not auto, or a newer copy; once per situation (a refusal is tried again a day later)."""
    from .environment import profile_mode

    mode_of = mode_of or profile_mode
    found = []
    for row in rows:
        if row["profile"] == lead["profile"] or not row.get("is_profile") or row.get("linked"):
            continue
        if not (row.get("installed") or row.get("broken")) or row.get("enabled") is not True:
            continue   # missing: the self_missing proposal decides; off: its owner turned it off there
        if row.get("broken"):
            reason = "broken"
        elif _newer(row.get("version"), lead.get("version")):
            continue   # newer than the lead: never taken back
        elif row.get("pinned"):
            if row["pinned"] == lead_sha:
                continue   # pinned to the lead's very commit: already the same code
            reason = "pinned"
        elif _newer(lead.get("version"), row.get("version")):
            reason = "outdated"
        elif row.get("sha") and row["sha"] != lead_sha:
            reason = "skew"
        else:
            continue
        try:
            mode = mode_of(Path(row["home"]))
        except Exception:
            mode = "manual"
        if mode not in (None, "auto"):
            continue   # that profile's owner chose assist or manual there
        last = memo.get(row["profile"]) if isinstance(memo.get(row["profile"]), dict) else None
        if last and last.get("lead") == lead_sha and last.get("sig") == _signature(row) and (
                last.get("ok") or 0 <= now - float(last.get("at") or 0) < SYNC_RETRY_SECONDS):
            continue   # tried already for this very situation
        found.append((row, reason))
    return found


def auto_sync(home: Path, *, now: float, runner=None, deadline: float | None = None, remote=None) -> dict | None:
    """The lead profile's automatic sync of the other profiles' copies (Mode auto). None when this profile is not
    the lead, or its own copy is not a checkout of the published repository (a fork's commit cannot be installed)."""
    import time

    from . import decisions
    from .admin import profile_name
    from .environment import lead, profiles, root_of, self_sha
    from .experience import experience

    root = root_of(Path(home))
    own = profile_name(home)
    if lead(root, now=now) != own:
        return None
    lead_sha = self_sha(Path(home))
    if not lead_sha:
        return None
    rows = [inspect(name, folder) for name, folder in profiles(root)]
    me = next((row for row in rows if row["profile"] == own), None)
    if me is None or not me["installed"] or me["linked"]:
        return None   # a development link may run a commit GitHub does not have: never a reference
    with experience(home) as conn:
        found = conn.execute("SELECT value FROM cursor WHERE key='plugin_sync'").fetchone()
    try:
        memo = json.loads(found[0]) if found else {}
    except (TypeError, ValueError):
        memo = {}
    memo = memo if isinstance(memo, dict) else {}
    result = {"lead": own, "commit": lead_sha[:12], "synced": [], "failed": [], "waiting": []}
    head = {"read": False, "sha": None}

    def latest() -> str | None:
        if not head["read"]:
            head["read"] = True
            head["sha"] = (remote or remote_head)()
        return head["sha"]

    def keep(profile: str, sig: str, ok: bool) -> None:
        memo[profile] = {"lead": lead_sha, "sig": sig, "at": now, "ok": ok}
        with experience(home) as conn:
            conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('plugin_sync', ?)",
                         (json.dumps(memo, sort_keys=True),))

    for row, reason in due(rows, me, lead_sha, memo, now=now):
        if len(result["synced"]) + len(result["failed"]) >= SYNC_MAX_PER_PASS:
            break   # the others at the next pass (a waiting copy runs nothing and takes no room)
        if deadline is not None and deadline - time.monotonic() < SYNC_NEEDED_SECONDS:
            break   # the next pass: a pass is never cut in the middle of an update
        target_row = row
        if not row.get("pinned") and latest() != lead_sha:
            if not row.get("broken"):
                # Without a pin Hermes installs the default branch as it is now, which is not the lead's commit when
                # the repository moved on (or could not be read): only code the owner already runs on the lead is
                # spread. Tried again a day later, or as soon as the lead moves.
                keep(row["profile"], _signature(row), False)
                result["waiting"].append({"profile": row["profile"], "reason": reason})
                continue
            # A half-installed copy does not load at all: repaired now on the lead's commit (`--ref` pins it there;
            # the next syncs move that pin with the lead, and `hermes evolution update` moves it to the latest).
            target_row = {**row, "pinned": "repair"}
        outcome = run([target_row], target=lead_sha, runner=runner, timeout=SYNC_TIMEOUT)[0]
        after = inspect(row["profile"], Path(row["home"]))
        ok = outcome.get("status") == "done" and after.get("sha") in (lead_sha, None) and after.get("installed")
        memo[row["profile"]] = {"lead": lead_sha, "sig": _signature(after), "at": now, "ok": ok}
        entry = {"profile": row["profile"], "reason": reason, "before": (row.get("sha") or "")[:12],
                 "after": (after.get("sha") or "")[:12], "version": after.get("version")}
        with experience(home) as conn:
            conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('plugin_sync', ?)",
                         (json.dumps(memo, sort_keys=True),))
            entry["decision"] = decisions.record_sync(conn, row["profile"], reason=reason, before=entry["before"],
                                                      after=entry["after"], version=entry["version"], ok=ok, now=now)
        if ok:
            result["synced"].append(entry)
        else:
            detail = outcome.get("detail") or ("" if outcome.get("status") != "done" else "ended on another commit")
            result["failed"].append({**entry, "detail": str(detail)[-160:]})
            if len(result["failed"]) >= 2:
                break   # two refusals in one pass: Hermes itself is in trouble; the next pass tries again
    return result


def markdown(result: dict, lang: str) -> str:
    pt = lang == "pt"
    lines = ["# " + ("Atualização do plugin nos perfis" if pt else "Plugin update across profiles"), ""]
    if result.get("listed") is not None:
        lines.append("| " + (" | ".join(("Perfil", "Instalado", "Versão", "Commit", "Fixado")) if pt else
                                " | ".join(("Profile", "Installed", "Version", "Commit", "Pinned"))) + " |")
        lines.append("|---|---|---|---|---|")
        yes, no = ("sim", "não") if pt else ("yes", "no")
        broken = "incompleto" if pt else "incomplete"
        linked = "link de desenvolvimento" if pt else "development link"
        for row in result["listed"]:
            state = linked if row.get("linked") else broken if row.get("broken") else yes if row["installed"] else no
            lines.append(f"| {row['profile']} | {state} | {row['version'] or '-'} | "
                         f"{(row['sha'] or '-')[:8]} | {(row['pinned'] or '')[:8] or no} |")
        return "\n".join(lines) + "\n"
    words = {"done": "atualizado" if pt else "updated", "failed": "falhou" if pt else "failed"}
    skipped = {"missing": "sem o plugin (pulado; use --install-missing)" if pt else
               "plugin not installed (skipped; use --install-missing)",
               "linked": "link de desenvolvimento (não mexido)" if pt else "development link (left as it is)",
               "pinned_no_target": "fixado; o último commit do repositório não pôde ser lido (rede ou git)" if pt else
               "pinned; the repository's latest commit could not be read (network or git)",
               "current": "já atualizado" if pt else "already current"}
    for item in result.get("results") or ():
        moved = ""
        if item.get("status") == "done":
            before, after = (item.get("before") or "")[:8], (item.get("after") or "")[:8]
            moved = f" {before or '?'} → {after or '?'}" + (f" (v{item['version']})" if item.get("version") else "")
        state = skipped.get(item.get("reason"), item.get("reason")) if item.get("status") == "skipped" \
            else words.get(item["status"], item["status"])
        lines.append(f"- **{item['profile']}**: {state}{moved}")
        if item.get("detail"):
            lines.append(f"  - {item['detail']}")
    if any(item.get("status") == "done" for item in result.get("results") or ()):
        lines += ["", ("Reinicie o gateway uma vez para carregar o código novo: `hermes gateway restart`." if pt
                       else "Restart the gateway once to load the new code: `hermes gateway restart`.")]
    return "\n".join(lines) + "\n"
