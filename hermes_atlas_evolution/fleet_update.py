"""Update (or install) this plugin in every profile of a Hermes, or in the profiles the operator picks (0.9.1).

Hermes installs plugins per profile, so an update used to mean one `hermes -p <profile> plugins update ...` per
profile, a security-scan prompt in each, and a dead end in any profile pinned to a commit ("is pinned to <sha>").
This module plans and runs that with Hermes' own public commands only, one profile at a time:

* installed and not pinned -> `plugins update hermes-atlas-evolution --force`
  (falls back to the install below when Hermes refuses the update);
* pinned to a commit       -> `plugins install <url> --force --enable` (no --ref: the profile follows the default
  branch again, so the next plain update works);
* missing (only on request) -> `plugins install <url> --enable`.

`--force` only skips Hermes' interactive scan prompt for this plugin, the operator's own repository; it is never
run from an agent's shell (cli._operator_only) and the runtime guard vetoes it there too.
Nothing here restarts the gateway: the caller prints `hermes gateway restart` once at the end.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

PLUGIN = "hermes-atlas-evolution"
URL = "https://github.com/sesantanajr/hermes-atlas-evolution.git"
UPDATE_TIMEOUT = 300
_SHA = re.compile(r"[0-9a-f]{40}")
_VERSION = re.compile(r"""^version:\s*["']?([0-9A-Za-z.+-]{1,32})["']?\s*$""", re.M)
_PINNED = re.compile(r"\bpinned to\b", re.I)


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
    git = folder / ".git"
    head = _read(git / "HEAD", 4096)
    if head is None:
        return None
    head = head.strip()
    if _SHA.fullmatch(head):
        return head
    if not head.startswith("ref: "):
        return None
    ref = head[5:].strip()
    if ".." in ref or ref.startswith("/"):
        return None
    loose = _read(git / ref, 4096)
    if loose and _SHA.fullmatch(loose.strip()):
        return loose.strip()
    for line in (_read(git / "packed-refs", 1024 * 1024) or "").splitlines():
        sha, _, name = line.partition(" ")
        if name.strip() == ref and _SHA.fullmatch(sha):
            return sha
    return None


def _pin(folder: Path) -> str | None:
    """The commit Hermes pinned this install to, from its .install-metadata.json (tolerant of the key's name)."""
    text = _read(folder / ".install-metadata.json")
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("pinned") is False:
        return None
    for key in ("pinned", "pin", "ref", "pinned_ref", "commit"):
        value = data.get(key)
        if isinstance(value, str) and _SHA.fullmatch(value.strip().lower()):
            return value.strip().lower()
    return None


def inspect(name: str, home: Path) -> dict:
    folder = Path(home) / "plugins" / PLUGIN
    manifest = _read(folder / "plugin.yaml")
    installed = manifest is not None
    version = None
    if manifest:
        found = _VERSION.search(manifest)
        version = found.group(1) if found else None
    return {"profile": name, "home": str(home), "installed": installed, "version": version,
            "sha": _git_sha(folder) if installed else None, "pinned": _pin(folder) if installed else None,
            "is_profile": (Path(home) / "config.yaml").is_file()}


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


def step(row: dict, *, install_missing: bool) -> list | None:
    """The first Hermes command for one profile, or None when there is nothing to do."""
    if not row["installed"]:
        return ["plugins", "install", URL, "--enable"] if install_missing else None
    if row["pinned"]:
        return ["plugins", "install", URL, "--force", "--enable"]
    return ["plugins", "update", PLUGIN, "--force"]


def run(rows: list[dict], *, install_missing: bool = False, runner=None, executable=None,
        timeout: int = UPDATE_TIMEOUT, progress=None) -> list[dict]:
    """Apply step() to each row, one profile at a time; a failure in one profile never stops the others."""
    from .admin import hermes_output

    done = []
    for row in rows:
        command = step(row, install_missing=install_missing)
        outcome = {"profile": row["profile"], "before": row.get("sha"), "commands": []}
        if command is None:
            outcome["status"] = "skipped"
            done.append(outcome)
            continue
        if progress is not None:
            progress(row["profile"])
        code, text = hermes_output(Path(row["home"]), command, runner=runner, executable=executable,
                                   timeout=timeout)
        outcome["commands"].append({"args": command, "code": code})
        if code != 0 and command[1] == "update":
            # A pin Hermes knows and the metadata did not say, an older Hermes without `update --force`, or a
            # broken checkout: a forced reinstall from the repository fixes all three.
            fallback = ["plugins", "install", URL, "--force", "--enable"]
            code, more = hermes_output(Path(row["home"]), fallback, runner=runner, executable=executable,
                                       timeout=timeout)
            outcome["commands"].append({"args": fallback, "code": code, "after": "pinned" if _PINNED.search(text)
                                        else "update_failed"})
            text = more
        after = inspect(row["profile"], Path(row["home"]))
        outcome.update(status="done" if code == 0 else "failed", after=after.get("sha"),
                       version=after.get("version"), pinned=after.get("pinned"))
        if code != 0:
            outcome["detail"] = " ".join(text.split())[-300:]
        done.append(outcome)
    return done


def markdown(result: dict, lang: str) -> str:
    pt = lang == "pt"
    lines = ["# " + ("Atualização do plugin nos perfis" if pt else "Plugin update across profiles"), ""]
    if result.get("listed"):
        lines.append("| " + (" | ".join(("Perfil", "Instalado", "Versão", "Commit", "Fixado")) if pt else
                                " | ".join(("Profile", "Installed", "Version", "Commit", "Pinned"))) + " |")
        lines.append("|---|---|---|---|---|")
        for row in result["listed"]:
            yes, no = ("sim", "não") if pt else ("yes", "no")
            lines.append(f"| {row['profile']} | {yes if row['installed'] else no} | {row['version'] or '-'} | "
                         f"{(row['sha'] or '-')[:8]} | {(row['pinned'] or '')[:8] or no} |")
        return "\n".join(lines) + "\n"
    words = {"done": "atualizado" if pt else "updated", "failed": "falhou" if pt else "failed",
             "skipped": "sem o plugin (pulado)" if pt else "plugin not installed (skipped)"}
    for item in result.get("results") or ():
        moved = ""
        if item.get("status") == "done":
            before, after = (item.get("before") or "")[:8], (item.get("after") or "")[:8]
            moved = f" {before or '?'} → {after or '?'}" + (f" (v{item['version']})" if item.get("version") else "")
        lines.append(f"- **{item['profile']}**: {words.get(item['status'], item['status'])}{moved}")
        if item.get("detail"):
            lines.append(f"  - {item['detail']}")
    if any(item.get("status") == "done" for item in result.get("results") or ()):
        lines += ["", ("Reinicie o gateway uma vez para carregar o código novo: `hermes gateway restart`." if pt
                       else "Restart the gateway once to load the new code: `hermes gateway restart`.")]
    return "\n".join(lines) + "\n"
