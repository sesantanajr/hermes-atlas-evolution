"""Keep this plugin on the same code in every profile of a Hermes, with Hermes' own commands only (0.9.1; since
0.13.0 the plugin never picks a commit and never reads the network itself).

Hermes installs plugins per profile and keeps its install records outside the plugin's folder, in
`<home>/plugins/.install-metadata.json`. What code a profile gets is always Hermes' decision, from that record:

* a catalog install (the record has a "catalog" block): `plugins update hermes-atlas-evolution` moves it to the
  catalog's reviewed pin, and a profile without the plugin gets `plugins install hermes-atlas-evolution`, the same
  reviewed pin (catalog rule 3: the pin is the only way code reaches a catalog install);
* a git install: `plugins update hermes-atlas-evolution` pulls the source its owner installed from. A copy Hermes
  pinned with `--ref` refuses that update ("is pinned to <sha>"): it is moved only to the commit the profile running
  the command runs (`plugins install <repository> --force --ref <that commit>`), and only when both copies come from
  this repository; a profile without the plugin gets that very commit the same way;
* a linked development checkout, a fork's pinned or half-installed copy, and a profile whose own copy is neither of
  the above are never touched.

Nothing here runs by itself: `hermes -p <profile> evolution update` is the owner's command. Unattended updates are
Hermes' own `plugins.auto_apply`. `--enable` is added only where the plugin is on already (a no-op there): updating a
profile never switches the plugin on where its owner switched it off. `--force` is used only with `install`, where it
means "replace the copy that is there"; an update never skips Hermes' scan prompt. Nothing restarts the gateway:
`status` tells when a running gateway still has the old code, and the owner restarts it once.
"""

from __future__ import annotations

import re
from pathlib import Path

PLUGIN = "hermes-atlas-evolution"
UPDATE_TIMEOUT = 300
_SHA = re.compile(r"[0-9a-f]{40}")
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")   # a catalog key
_VERSION = re.compile(r"""^version:\s*["']?([0-9A-Za-z.+-]{1,32})["']?\s*$""", re.M)
_PINNED = re.compile(r"\bis pinned\b|\bpinned to\b", re.I)


def _read(path: Path, limit: int = 64 * 1024) -> str | None:
    """The first `limit` bytes of a regular file, with the plugin's one safe reader (environment.read_text): never
    through a link, never a named pipe or a device."""
    from .environment import read_text

    return read_text(path, limit, part="head")


def _record(home: Path) -> dict:
    from .environment import install_record

    return install_record(Path(home), PLUGIN, PLUGIN)


def catalog_name(record: dict) -> str:
    """The catalog key a Hermes install record says this copy came from ("" for any other install)."""
    block = record.get("catalog") if isinstance(record, dict) else None
    name = block.get("name") if isinstance(block, dict) else None
    return name if isinstance(name, str) and _NAME.fullmatch(name) else ""


def _origin(home: Path) -> str:
    """Where this plugin's copy in a profile comes from: "self" (a checkout of this repository, or Hermes' install
    record naming it), "other" (a fork or another repository), "" when nothing says."""
    from .ecosystem import normalize_repo
    from .environment import REPOSITORY, git_origin

    ours = normalize_repo(f"https://github.com/{REPOSITORY}")
    repo, _sha = git_origin(Path(home) / "plugins" / PLUGIN)
    if not repo:
        record = _record(home)
        repo = next((str(record[key]) for key in ("source", "url", "repo", "origin")
                     if isinstance(record.get(key), str) and record[key]), "")
    if not repo:
        return ""
    found = normalize_repo(repo)
    return "self" if found in (ours, normalize_repo(REPOSITORY)) else "other"


def _git_sha(folder: Path) -> str | None:
    """The commit a plugin folder is on, read from .git without running git."""
    from .environment import git_origin

    _repo, sha = git_origin(folder)
    return sha or None


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
    record = _record(home)
    catalog = catalog_name(record) if present and not linked else ""
    return {"profile": name, "home": str(home), "installed": installed, "version": version,
            "sha": _git_sha(folder) if installed else None, "pinned": "" if catalog else (record.get("pinned") or ""),
            "catalog": catalog, "broken": present and not installed and not linked, "linked": linked,
            "enabled": _enabled(home) if present else None, "is_profile": (Path(home) / "config.yaml").is_file(),
            "origin": _origin(home) if present and not linked else ""}


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


def source(home: Path) -> list[str] | None:
    """What `plugins install` needs to give another profile the code this profile runs: the catalog name for a
    catalog install (Hermes installs the reviewed pin), this repository and `--ref <commit>` for a checkout of it.
    None for a linked development copy, a fork, or a copy nothing says where it came from."""
    from .environment import REPOSITORY, self_sha

    folder = Path(home) / "plugins" / PLUGIN
    if folder.is_symlink() or not folder.is_dir():
        return None
    catalog = catalog_name(_record(home))
    if catalog:
        return [catalog]
    sha = self_sha(Path(home))
    return [REPOSITORY, "--ref", sha] if _SHA.fullmatch(sha or "") else None


def install_args(found: list[str], *, enable: bool, force: bool = False) -> list[str]:
    """`plugins install` arguments from source(): `--enable` right after the source, the ref last."""
    return ["plugins", "install", found[0], *(["--force"] if force else []), *(["--enable"] if enable else []),
            *found[1:]]


def _enable(row: dict) -> bool:
    return row.get("enabled") is True


def step(row: dict, *, install_missing: bool, lead: list[str] | None = None) -> list | None:
    """The Hermes command for one profile, or None when there is nothing to do (or nothing safe to do). `lead` is
    source() of the profile the command runs in."""
    if row.get("linked"):
        return None   # a development checkout: its owner updates it
    if row.get("catalog"):
        return ["plugins", "update", PLUGIN]   # Hermes moves it to the catalog's reviewed pin
    if row.get("origin") == "other" and (row.get("pinned") or row.get("broken")):
        return None   # a fork: installing this repository over it would replace the owner's code
    if not row["installed"] and not row.get("broken"):
        return install_args(lead, enable=True) if install_missing and lead else None
    git_lead = lead if lead and len(lead) == 3 else None
    if row.get("pinned"):
        if not git_lead or (row["pinned"] == git_lead[2] and row.get("installed")):
            return None   # nothing to move the pin to, or it is there already
        return install_args(git_lead, enable=_enable(row), force=True)
    if row.get("broken"):
        return install_args(lead, enable=_enable(row), force=True) if lead else None
    return ["plugins", "update", PLUGIN]


def _skip_reason(row: dict, lead: list[str] | None) -> str:
    if row.get("linked"):
        return "linked"
    if row.get("origin") == "other" and (row.get("pinned") or row.get("broken")):
        return "fork"
    if not row["installed"] and not row.get("broken"):
        return "no_source" if lead is None else "missing"
    if row.get("pinned") and not (lead and len(lead) == 3):
        return "pinned_no_target"
    if row.get("broken"):
        return "no_source"
    return "current"


def run(rows: list[dict], *, install_missing: bool = False, here: Path | None = None, runner=None, executable=None,
        timeout: int = UPDATE_TIMEOUT, progress=None) -> list[dict]:
    """Apply step() to each row, one profile at a time; a failure in one profile never stops the others.

    `here` is the home of the profile the command runs in: its row goes first, and its code (source(), read again
    after its own update) is what a pinned, half-installed or missing copy elsewhere is brought to."""
    from .admin import hermes_output

    here_name = None
    if here is not None:
        here_name = next((row["profile"] for row in rows if Path(row["home"]) == Path(here)), None)
    ordered = sorted(rows, key=lambda row: row["profile"] != here_name)
    lead = {"source": source(Path(here)) if here is not None else None}
    done = []
    for row in ordered:
        command = step(row, install_missing=install_missing, lead=lead["source"])
        outcome = {"profile": row["profile"], "before": row.get("sha"), "commands": []}
        if command is None:
            outcome["status"] = "skipped"
            outcome["reason"] = _skip_reason(row, lead["source"])
            done.append(outcome)
            continue
        if progress is not None:
            progress(row["profile"])
        code, text = hermes_output(Path(row["home"]), command, runner=runner, executable=executable,
                                   timeout=timeout)
        outcome["commands"].append({"args": command, "code": code})
        if code not in (0, 124, 126, 127) and command[1] == "update" and _PINNED.search(text) \
                and lead["source"] and len(lead["source"]) == 3 and row["profile"] != here_name \
                and (row.get("origin") or _origin(Path(row["home"]))) == "self":
            # a pin the record did not show: moved to the code of the profile the command runs in, never further
            fallback = install_args(lead["source"], enable=_enable(row), force=True)
            code, text = hermes_output(Path(row["home"]), fallback, runner=runner, executable=executable,
                                       timeout=timeout)
            outcome["commands"].append({"args": fallback, "code": code, "after": "pinned"})
        after = inspect(row["profile"], Path(row["home"]))
        outcome.update(status="done" if code == 0 else "failed", after=after.get("sha"),
                       version=after.get("version"), pinned=after.get("pinned"))
        if code != 0:
            outcome["detail"] = " ".join(text.split())[-300:]
        if row["profile"] == here_name and here is not None:
            lead["source"] = source(Path(here))   # its own update may have moved it: the others follow it
        done.append(outcome)
    return done


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
               "pinned_no_target": "fixado em outro commit; este perfil não roda uma cópia do repositório para onde"
               " movê-lo (mova com `hermes -p <perfil> plugins install <origem> --force --ref <commit>`)" if pt else
               "pinned to another commit; this profile does not run a copy of the repository to move it to (move it"
               " with `hermes -p <profile> plugins install <source> --force --ref <commit>`)",
               "no_source": "a cópia deste perfil não veio do catálogo nem do repositório: nada de onde instalar"
               " (instale com `hermes -p <perfil> plugins install hermes-atlas-evolution`)" if pt else
               "this profile's own copy came neither from the catalog nor from the repository: nothing to install"
               " from (install it with `hermes -p <profile> plugins install hermes-atlas-evolution`)",
               "fork": "cópia de um fork ou de outro repositório (não mexida)" if pt else
               "a copy from a fork or another repository (left as it is)",
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
