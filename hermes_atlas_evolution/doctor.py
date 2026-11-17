"""Hermes' own doctor, read for the owner.

`hermes doctor` is Hermes' full check of an installation. Run as a command it also calls every configured
provider's API, audits npm packages against the registry, starts the dashboard's imports in a subprocess and asks
GitHub for its login, so it takes minutes and spends network. The plugin runs the same checks without those four and
without the terminal backend check (which may open an SSH connection or start a container): in a short-lived Python
process of Hermes' own interpreter, with the profile's home and a clean environment, each check called as Hermes
calls it without `--fix`. Nothing is changed; like `hermes doctor`, it loads the profile's plugins to list their
tools and leaves its logs and lock files. What the checks print is read line by line (✓ ⚠ ✗ and the → notes), and
what they list as problems is kept as is.

`findings` turns that into situations of the playbook, each with Hermes' words, plain words in the owner's
language, what to do and the command:
- a folder of the profile's cache that Hermes' cleaner never touches and holds more than 1 GB (doctor_cache). A
  package cache that rebuilds itself (uv, pnpm, npm, pip...) goes to the plugin's trash on approval, one at a time,
  put back with undo, deleted after 30 days; any other folder (the browser's logins, conversation attachments,
  generated images...) is the owner's, with what it holds and the command to look inside;
- the rollback snapshots above Hermes' own size cap (doctor_checkpoints): the owner's, with Hermes' three ways out;
- a security advisory or a suspicious MCP server (doctor_security): the owner's, first in the list;
- anything else the doctor lists as a problem (doctor_issue), in Hermes' words with an explanation and the command
  where the plugin knows it, and the command Hermes names otherwise.
Warnings that only say an optional piece is not set up (an optional package, a provider without a login) are not
problems: they are counted, not proposed. A problem of the whole machine (a security advisory, linger, the PATH,
the gateway's supervision) is said once, by the profile that looks after the whole Hermes.

When Hermes has no `hermes_cli.doctor.DOCTOR_CHECKS` (another major version), the run is recorded as not done and
the menu, the panel and `status` say so; nothing is proposed from it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

EVERY_SECONDS = 24 * 3600
KEEP_SECONDS = 3 * EVERY_SECONDS   # an older result is not shown: the profile went idle or the runs were skipped
TIMEOUT_SECONDS = 120
OUTPUT_MAX = 2_000_000
MARKER = "@@ATLAS-EVOLUTION-DOCTOR@@"
# Network, minutes-long or outward checks: the provider probes, npm audit against the registry, the dashboard import in
# a subprocess (up to 120 s), the Skills Hub (asks GitHub for its login) and the terminal backend (may open SSH or
# start a container).
SKIP = ("_check_api_connectivity", "_check_npm_audit", "_check_web_dashboard_import", "_check_skills_hub",
        "_check_terminal_backend")
ENV_KEEP = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT",
            "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "SHELL")
_PROFILE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")   # Hermes' rule for a profile name
_CACHE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,88}")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_ROW = re.compile(r"^ {2}([✓⚠✗]) (.+?)\s*$")
_NOTE = re.compile(r"^ {4,}(?:→ )?(.+?)\s*$")
_QUOTED_COMMAND = re.compile(r"[`'\"](hermes [a-z][^`'\"\n]{1,160})[`'\"]")
_HERMES_COMMAND = re.compile(r"\b(hermes [a-z][a-z0-9 _./=<>-]{1,120}?)(?=$|[,;)]|\.\s|\.$)")

# Package caches that rebuild themselves when something needs them again: the only cache folders moved with one key.
REGENERABLE = ("uv", "pnpm", "npm", "pip", "yarn", "huggingface", "piper-voices")
# What the other folders Hermes keeps under cache/ hold (en, pt).
CACHE_ROLES = {
    "browser": ("the browser tool's profile, with its logins and cookies", "o perfil do navegador do agente, com logins e cookies"),
    "chrome-debug": ("the browser tool's debug profile", "o perfil de depuração do navegador do agente"),
    "documents": ("files attached to conversations, which old sessions point to",
                  "arquivos anexados às conversas, que sessões antigas usam"),
    "images": ("images of conversations", "imagens das conversas"),
    "generated": ("images and files the agent generated", "imagens e arquivos que o agente gerou"),
    "audio": ("audio of conversations", "áudios das conversas"),
    "delegation": ("the work of subagents", "o trabalho dos subagentes"),
    "spillover": ("large tool outputs that sessions point to", "saídas grandes de ferramentas que as sessões usam"),
    "web": ("pages the agent read", "páginas que o agente leu"),
}

# Run in Hermes' own interpreter: sys.path comes from the process that runs the plugin (Hermes' launcher sets it),
# the profile from HERMES_HOME. Each check is called as `hermes doctor` calls it without --fix. Hermes raises
# SystemExit subclasses for some config problems: caught too, so one check never hides the others.
BOOT = r"""
import contextlib, io, json, os, sys
sys.path[:0] = [p for p in json.loads(os.environ.pop("ATLAS_EVOLUTION_PATH", "[]")) if isinstance(p, str)]
skip = set(json.loads(os.environ.pop("ATLAS_EVOLUTION_SKIP", "[]")))
only = set(json.loads(os.environ.pop("ATLAS_EVOLUTION_ONLY", "[]")))
marker = os.environ.pop("ATLAS_EVOLUTION_MARKER")
os.environ.setdefault("HERMES_INTERACTIVE", "1")
real = sys.stdout
def done(payload):
    real.write("\n" + marker + json.dumps(payload) + "\n")
    real.flush()
try:
    with contextlib.redirect_stdout(io.StringIO()):
        from hermes_cli import doctor
        from hermes_cli.left_core_migration import installs_held
    checks = list(doctor.DOCTOR_CHECKS)
except BaseException as error:
    done({"error": "unavailable", "detail": type(error).__name__})
    os._exit(0)
result = {"checks": []}
with installs_held():
    for title, check in checks:
        name = getattr(check, "__name__", "")
        if name in skip or (only and name not in only):
            continue
        buffer = io.StringIO()
        error = None
        try:
            with contextlib.redirect_stdout(buffer):
                found = check(False)
        except BaseException as caught:
            found, error = None, type(caught).__name__
        result["checks"].append({"name": name, "section": title, "text": buffer.getvalue()[-20000:],
                                 "issues": [str(x) for x in getattr(found, "issues", [])][:40],
                                 "manual": [str(x) for x in getattr(found, "manual_issues", [])][:40],
                                 "error": error})
done(result)
os._exit(0)
"""


def _environment(home: Path) -> dict:
    env = {key: os.environ[key] for key in ENV_KEEP if key in os.environ}
    env.update({"HERMES_HOME": str(home), "NO_COLOR": "1", "TERM": "dumb", "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1", "ATLAS_EVOLUTION_PATH": json.dumps([item for item in sys.path if item]),
                "ATLAS_EVOLUTION_SKIP": json.dumps(SKIP), "ATLAS_EVOLUTION_MARKER": MARKER})
    return env


def rows(text: str) -> list[dict]:
    """The lines a check printed: {mark: ok|warn|fail, text, notes}. A note (→ or a deeper line) belongs to the line
    above it."""
    found: list[dict] = []
    for raw in _ANSI.sub("", text or "").splitlines():
        line = raw.rstrip()
        row = _ROW.match(line)
        if row:
            found.append({"mark": {"✓": "ok", "⚠": "warn", "✗": "fail"}[row.group(1)], "text": row.group(2),
                          "notes": []})
            continue
        note = _NOTE.match(line)
        if note and found and len(found[-1]["notes"]) < 6:
            found[-1]["notes"].append(note.group(1))
    return found


def _spawn(argv: list, *, env: dict, cwd: str, timeout: int) -> str:
    """Run the checks in a process group of their own: on a timeout the whole group is stopped, so a process a check
    started (ssh, docker) can never hold the pipes open past it. Raises subprocess.TimeoutExpired."""
    posix = os.name == "posix"
    process = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, encoding="utf-8", errors="replace",
                               start_new_session=posix)
    try:
        out, _err = process.communicate(timeout=timeout)
        return out or ""
    except subprocess.TimeoutExpired:
        try:
            if posix:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except OSError:
            pass
        try:
            process.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        raise


def run(home: Path, *, only=(), runner=None, timeout: int = TIMEOUT_SECONDS) -> dict:
    """Hermes' doctor checks for one profile, without network and without --fix: {ok, checks|error, seconds}."""
    home = Path(home)
    if runner is None and os.environ.get("ATLAS_EVOLUTION_NO_HERMES") == "1":
        return {"ok": False, "error": "disabled"}
    if runner is None and not (home / "config.yaml").is_file():
        return {"ok": False, "error": "not_a_profile"}
    env = _environment(home)
    if only:
        env["ATLAS_EVOLUTION_ONLY"] = json.dumps(list(only))
    started = time.monotonic()
    argv = [sys.executable, "-I", "-c", BOOT]
    try:
        if runner is None:
            out = _spawn(argv, env=env, cwd=str(home), timeout=timeout)
        else:
            out = runner(argv, env=env, cwd=str(home), stdin=subprocess.DEVNULL, capture_output=True,
                         encoding="utf-8", errors="replace", timeout=timeout, check=False).stdout or ""
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "seconds": round(time.monotonic() - started, 1)}
    except OSError:
        return {"ok": False, "error": "unavailable"}
    seconds = round(time.monotonic() - started, 1)
    out = out[-OUTPUT_MAX:]
    payload = None
    for line in reversed(out.splitlines()):
        if line.startswith(MARKER):
            try:
                payload = json.loads(line[len(MARKER):])
            except ValueError:
                payload = None
            break
    if not isinstance(payload, dict):
        return {"ok": False, "error": "no_answer", "seconds": seconds}
    if payload.get("error"):
        return {"ok": False, "error": str(payload["error"])[:40], "seconds": seconds}
    checks = []
    for item in payload.get("checks") or ():
        if not isinstance(item, dict):
            continue
        checks.append({"name": str(item.get("name") or "")[:80], "section": str(item.get("section") or "")[:80],
                       "rows": rows(str(item.get("text") or "")),
                       "issues": [str(x)[:600] for x in item.get("issues") or ()][:40],
                       "manual": [str(x)[:600] for x in item.get("manual") or ()][:40],
                       "error": item.get("error")})
    return {"ok": True, "checks": checks, "seconds": seconds}


# ── what the doctor said, in plain words ────────────────────────────────────────────────────────────────────────
_SIZE = r"([\d.,]+ ?[KMGT]?i?B)"


def _known(pattern, key, fact_en, fact_pt, do_en, do_pt, command="", *, named=False, host=False):
    """A sentence of Hermes' doctor the plugin knows: what it means (fact), what to do, the command (arguments after
    `hermes -p <profile>`, or a full command line starting with "!"), whether its first group names the subject
    (a plugin) and whether it is a problem of the whole machine."""
    return {"pattern": re.compile(pattern), "key": key, "fact": (fact_en, fact_pt), "do": (do_en, do_pt),
            "command": command, "named": named, "host": host}


# {profile} is the profile, {1} the sentence's first group, {tools} the tools without a key.
KNOWN = (
    _known(r"missing API keys for full tool access", "tool_keys",
           "Some tools that are on have no key: {tools}.",
           "Algumas ferramentas ligadas não têm chave: {tools}.",
           "If you use them, add the key with `hermes -p {profile} setup`; if not, turn them off with "
           "`hermes -p {profile} tools`, so the agent stops seeing them.",
           "Se você as usa, coloque a chave com `hermes -p {profile} setup`; se não, desligue-as com "
           "`hermes -p {profile} tools`, para o agente parar de vê-las.", "tools"),
    _known(r"Run 'hermes setup' to create \.env", "env_missing",
           "This profile has no .env file, where Hermes keeps its keys.",
           "Este perfil não tem o arquivo .env, onde o Hermes guarda as chaves.",
           "Run `hermes -p {profile} setup` to create it.", "Rode `hermes -p {profile} setup` para criá-lo.", "setup"),
    _known(r"Run 'hermes setup' to configure API keys", "keys_missing",
           "No model provider key is set in this profile.", "Nenhuma chave de provedor de modelo está configurada neste perfil.",
           "Run `hermes -p {profile} setup` and choose a provider.", "Rode `hermes -p {profile} setup` e escolha um provedor.",
           "setup"),
    _known(r"state\.db is large", "sessions_prune",
           "The session database is large and keeps growing.", "O banco de sessões está grande e só cresce.",
           "Turn on Hermes' own cleanup of old sessions.", "Ligue a limpeza de sessões antigas do próprio Hermes.",
           "config set sessions.auto_prune true"),
    _known(r"state\.db (FTS|schema)", "sessions_repair",
           "The session database needs a repair (its search index or its structure).",
           "O banco de sessões precisa de reparo (o índice de busca ou a estrutura).",
           "Run the command: Hermes makes a backup first, then repairs.",
           "Rode o comando: o Hermes faz um backup antes e depois repara.", "sessions repair"),
    _known(r"structural corruption", "sessions_recover",
           "The session database is damaged in its tables, beyond what a repair fixes.",
           "O banco de sessões está danificado nas tabelas, além do que um reparo resolve.",
           "Stop the gateway (`hermes gateway stop`), then follow Hermes' recovery in its own words below: inspect first, "
           "then recover to a new file.",
           "Pare o gateway (`hermes gateway stop`) e siga a recuperação do Hermes, no texto dele abaixo: primeiro "
           "inspecionar, depois recuperar para um arquivo novo."),
    _known(r"Enable linger", "linger",
           "The gateway runs as a user service that stops when you log out and does not start after a reboot.",
           "O gateway roda como serviço do usuário: para quando você sai da sessão e não volta depois de reiniciar.",
           "Run `sudo loginctl enable-linger $USER` once.", "Rode uma vez `sudo loginctl enable-linger $USER`.",
           "!sudo loginctl enable-linger $USER", host=True),
    _known(r"Stale root-level provider/base_url", "config_root_keys",
           "provider/base_url sit at the top of config.yaml instead of under `model:`.",
           "provider/base_url estão no topo do config.yaml em vez de dentro de `model:`.",
           "Run `hermes -p {profile} doctor --fix`: it moves them (after `hermes -p {profile} backup --quick`).",
           "Rode `hermes -p {profile} doctor --fix`: ele os move (depois de `hermes -p {profile} backup --quick`).",
           "doctor --fix"),
    _known(r"migrate config", "config_migrate",
           "config.yaml is from an older Hermes.", "O config.yaml é de um Hermes mais antigo.",
           "Run `hermes -p {profile} doctor --fix`: it migrates the file and keeps a backup.",
           "Rode `hermes -p {profile} doctor --fix`: ele migra o arquivo e guarda um backup.", "doctor --fix"),
    _known(r"HERMES_MAX_ITERATIONS", "max_iterations",
           "An old HERMES_MAX_ITERATIONS in .env overrides agent.max_turns in config.yaml.",
           "Um HERMES_MAX_ITERATIONS antigo no .env passa por cima do agent.max_turns do config.yaml.",
           "Run `hermes -p {profile} doctor --fix`: it takes the line out of .env.",
           "Rode `hermes -p {profile} doctor --fix`: ele tira a linha do .env.", "doctor --fix"),
    _known(r"Memory provider '([^']+)' is turned off", "memory_off",
           "The memory provider {1} is turned off, so Hermes uses only its built-in memory.",
           "O provedor de memória {1} está desligado, então o Hermes usa só a memória embutida.",
           "Turn it back on with `hermes -p {profile} plugins enable {1}`.",
           "Ligue de novo com `hermes -p {profile} plugins enable {1}`.", "plugins enable {1}", named=True),
    _known(r"Plugin '([^']+)' was turned off by an update", "plugin_off_update",
           "An update turned the plugin {1} off.", "Uma atualização desligou o plugin {1}.",
           "If you want it, turn it back on with `hermes -p {profile} plugins enable {1}`; if not, remove it with "
           "`hermes -p {profile} plugins remove {1}`.",
           "Se você o quer, ligue de novo com `hermes -p {profile} plugins enable {1}`; se não, remova com "
           "`hermes -p {profile} plugins remove {1}`.", "plugins enable {1}", named=True),
    _known(r"Plugin '([^']+)' is in both plugins\.enabled and plugins\.disabled", "plugin_both_lists",
           "The plugin {1} is in both lists of config.yaml, so it stays off.",
           "O plugin {1} está nas duas listas do config.yaml, então fica desligado.",
           "Choose one: `hermes -p {profile} plugins enable {1}` or `hermes -p {profile} plugins disable {1}`.",
           "Escolha um: `hermes -p {profile} plugins enable {1}` ou `hermes -p {profile} plugins disable {1}`.",
           "plugins enable {1}", named=True),
    _known(r"Cron store .* is not writable", "cron_store",
           "Hermes cannot write its scheduled tasks, so they are being skipped.",
           "O Hermes não consegue gravar as tarefas agendadas, então elas estão sendo puladas.",
           "Follow the fix in Hermes' words below (usually the folder's owner or permissions).",
           "Siga a correção no texto do Hermes abaixo (em geral, o dono ou as permissões da pasta)."),
    _known(r"Free disk space", "cron_disk",
           "The disk that holds the scheduled tasks is almost full; they stop when it fills.",
           "O disco das tarefas agendadas está quase cheio; elas param quando ele encher.",
           "See what takes the space with `du -sh ~/.hermes/* | sort -h | tail` and free it.",
           "Veja o que ocupa espaço com `du -sh ~/.hermes/* | sort -h | tail` e libere.",
           "!du -sh ~/.hermes/* | sort -h | tail", host=True),
    _known(r"legacy per-profile gateways|host multiplexer|No host gateway owns", "gateway_multiplex",
           "Each profile still runs its own gateway; Hermes now runs one gateway for every profile.",
           "Cada perfil ainda roda o próprio gateway; o Hermes agora roda um gateway só para todos os perfis.",
           "Run `hermes gateway migrate --multiplex` once.", "Rode uma vez `hermes gateway migrate --multiplex`.",
           "!hermes gateway migrate --multiplex", host=True),
    _known(r"Duplicate platform credential|same .*token.* in profiles", "duplicate_credential",
           "Two profiles use the same chat bot credential, so both answer the same chat.",
           "Dois perfis usam a mesma credencial de bot de chat, então os dois respondem ao mesmo chat.",
           "Keep that credential in one profile only: take it out of the other profile's .env.",
           "Deixe essa credencial num perfil só: tire-a do .env do outro perfil.", host=True),
    _known(r"Add .* to your PATH", "path",
           "The `hermes` command is not on your PATH.", "O comando `hermes` não está no seu PATH.",
           "Add `export PATH=\"$HOME/.local/bin:$PATH\"` to your shell's startup file (~/.bashrc or ~/.zshrc).",
           "Acrescente `export PATH=\"$HOME/.local/bin:$PATH\"` ao arquivo de início do seu shell (~/.bashrc ou ~/.zshrc).",
           host=True),
)
_ADVISORY = re.compile(r"security advisory (\S+?):", re.I)
_ACK = re.compile(r"hermes doctor --ack (\S+?)[`'\"]?(?:$|[\s,.;])")
_MCP = re.compile(r"mcp_servers\.([A-Za-z0-9._-]{1,80})")
_SECURITY = re.compile(r"security advisory|mcp_servers\.|rotate (any )?credentials", re.I)
# Warnings that only say an optional piece is not set up: counted, never proposed.
_OPTIONAL = re.compile(r"\((optional|not logged in|not configured)|optional, not installed|"
                       r"\(missing [A-Z0-9_, ]+\)$|system dependency not met|configured but not available", re.I)
_CACHE_HOG = re.compile(r"/cache/([^/\s]+)/ is " + _SIZE + r" and outside every pruner")
_CHECKPOINTS = re.compile(r"checkpoints .*?are on: " + _SIZE + r" across (\d+) project.*?above the " + _SIZE + r" cap",
                          re.I)
_MISSING_KEY = re.compile(r"^(\S+) \(missing ([A-Z0-9_, ]+)\)$")

TITLES = {
    "tool_keys": ("Tools that are on have no key", "Ferramentas ligadas sem chave"),
    "env_missing": ("The profile has no .env file", "O perfil não tem o arquivo .env"),
    "keys_missing": ("No model provider key in this profile", "Nenhuma chave de provedor neste perfil"),
    "sessions_prune": ("The session database keeps growing", "O banco de sessões só cresce"),
    "sessions_repair": ("The session database needs a repair", "O banco de sessões precisa de reparo"),
    "sessions_recover": ("The session database is damaged", "O banco de sessões está danificado"),
    "linger": ("The gateway does not come back after a reboot", "O gateway não volta depois de reiniciar"),
    "config_root_keys": ("config.yaml has settings in the wrong place", "O config.yaml tem ajustes no lugar errado"),
    "config_migrate": ("config.yaml is from an older Hermes", "O config.yaml é de um Hermes mais antigo"),
    "max_iterations": ("An old setting in .env overrides config.yaml", "Um ajuste antigo no .env passa por cima do config.yaml"),
    "memory_off": ("The memory provider is off", "O provedor de memória está desligado"),
    "plugin_off_update": ("An update turned a plugin off", "Uma atualização desligou um plugin"),
    "plugin_both_lists": ("A plugin is both on and off", "Um plugin está ligado e desligado ao mesmo tempo"),
    "cron_store": ("Scheduled tasks are being skipped", "As tarefas agendadas estão sendo puladas"),
    "cron_disk": ("The disk of the scheduled tasks is almost full", "O disco das tarefas agendadas está quase cheio"),
    "gateway_multiplex": ("One gateway per profile is still running", "Ainda roda um gateway por perfil"),
    "duplicate_credential": ("Two profiles answer the same chat", "Dois perfis respondem ao mesmo chat"),
    "path": ("The hermes command is not on the PATH", "O comando hermes não está no PATH"),
}


def title(key: str, lang: str) -> str | None:
    pair = TITLES.get(key or "")
    return None if pair is None else pair[1 if lang == "pt" else 0]


def _bytes(text: str) -> int:
    match = re.match(r"([\d.,]+) ?([KMGT]?)i?B", text.strip())
    if not match:
        return 0
    try:
        number = float(match.group(1).replace(",", ""))
    except ValueError:
        return 0
    return int(number * {"": 1, "K": 2**10, "M": 2**20, "G": 2**30, "T": 2**40}[match.group(2)])


def _fill(text: str, *, profile: str, groups: tuple, tools: list[str]) -> str:
    text = text.replace("{profile}", profile).replace("{tools}", ", ".join(f"`{item}`" for item in tools[:8]) or "—")
    for index, group in enumerate(groups, start=1):
        text = text.replace("{%d}" % index, f"`{group}`" if "`" not in text else str(group))
    return text


def explain(text: str, lang: str, *, profile: str = "<profile>", tools: list[str] | None = None) -> dict:
    """What one problem the doctor listed means: {key, fact, do, command, subject, host}. The key is "" when the plugin
    does not know that sentence; the command is the full command line, or ""."""
    tools = tools or []
    pt = 1 if lang == "pt" else 0
    for entry in KNOWN:
        match = entry["pattern"].search(text)
        if not match:
            continue
        groups = tuple(group for group in match.groups() if group)
        safe = all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", group) for group in groups)
        if entry["named"] and not safe:
            break   # a name the plugin will not put in a command: said in Hermes' own words
        fact = _fill(entry["fact"][pt], profile=profile, groups=groups, tools=tools)
        # the fact names a group as code; the action already has it inside a command
        fact = fact.replace("``", "`")
        do = entry["do"][pt]
        for index, group in enumerate(groups, start=1):
            do = do.replace("{%d}" % index, group)
        do = do.replace("{profile}", profile).replace("{tools}", ", ".join(tools[:8]))
        command = entry["command"]
        for index, group in enumerate(groups, start=1):
            command = command.replace("{%d}" % index, group)
        command = command[1:] if command.startswith("!") else (f"hermes -p {profile} {command}" if command else "")
        subject = entry["key"] + (f".{groups[0]}" if entry["named"] and groups else "")
        return {"key": entry["key"], "fact": fact, "do": do, "command": command, "subject": subject,
                "host": entry["host"]}
    named = _QUOTED_COMMAND.search(text) or _HERMES_COMMAND.search(text)
    args = named.group(1)[len("hermes "):].strip() if named else ""
    return {"key": "", "fact": "", "do": "", "command": f"hermes -p {profile} {args}" if args else "",
            "subject": f"issue.{_digest(text)}", "host": False}


def security(text: str, profile: str) -> dict:
    """A security problem the doctor listed: its stable subject (the advisory's id or the MCP server's name) and the
    command that closes it once handled."""
    advisory = _ADVISORY.search(text)
    ack = _ACK.search(text)
    server = _MCP.search(text)
    if advisory or ack:
        ident = (advisory or ack).group(1).strip("`'\".,")
        safe = re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,80}", ident)
        return {"subject": f"security.{ident if safe else _digest(text)}", "host": True,
                "command": f"hermes -p {profile} doctor --ack {ident}" if safe else f"hermes -p {profile} doctor"}
    if server and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}", server.group(1)):
        return {"subject": f"mcp.{server.group(1)}", "host": False,
                "command": f"hermes -p {profile} mcp remove {server.group(1)}"}
    return {"subject": f"security.{_digest(text)}", "host": True, "command": f"hermes -p {profile} doctor"}


def _digest(text: str) -> str:
    return hashlib.sha1(re.sub(r"\d+", "#", text).encode("utf-8")).hexdigest()[:10]


def findings(profile: str, home: Path, result: dict, *, lang: str = "en") -> list[dict]:
    """The doctor's problems for one profile as findings (environment._finding's shape). A finding of the whole
    machine carries evidence["host"]: environment._doctor says it once."""
    from .environment import _finding

    if not result.get("ok"):
        return []
    found: list[dict] = []
    tools: list[str] = []
    for check in result.get("checks") or ():
        for row in check["rows"]:
            if row["mark"] == "ok":
                continue
            key = _MISSING_KEY.match(row["text"])
            if key:
                tools.append(key.group(1))
            hog = _CACHE_HOG.search(row["text"])
            if hog and _CACHE.fullmatch(hog.group(1)) and hog.group(1) not in ("scratch", "terminal"):
                name = hog.group(1)
                regenerable = name in REGENERABLE
                path = f"{home}/cache/{name}"
                found.append(_finding("doctor_cache", profile, f"cache.{name}", risk="medium" if regenerable else "low",
                                      evidence={"folder": f"cache/{name}", "bytes": _bytes(hog.group(2)),
                                                "regenerable": regenerable, "role": name if name in CACHE_ROLES else "",
                                                "hermes": row["text"][:400]},
                                      command="" if regenerable else f"du -sh {path}/* | sort -h | tail -20"))
                continue
            points = _CHECKPOINTS.search(row["text"])
            if points:
                found.append(_finding("doctor_checkpoints", profile, "checkpoints", risk="low",
                                      evidence={"bytes": _bytes(points.group(1)), "projects": int(points.group(2)),
                                                "cap": _bytes(points.group(3)), "hermes": row["text"][:400]},
                                      command=f"hermes -p {profile} checkpoints status"))
    seen: set[str] = set()
    for check in result.get("checks") or ():
        for text in check["issues"] + check["manual"]:
            if text in seen:
                continue
            seen.add(text)
            if _SECURITY.search(text):
                said = security(text, profile)
                found.append(_finding("doctor_security", profile, said["subject"], risk="high",
                                      evidence={"hermes": text[:600], "section": check["section"], "host": said["host"]},
                                      command=said["command"]))
                continue
            told = explain(text, "en", profile=profile, tools=tools)
            found.append(_finding("doctor_issue", profile, told["subject"], risk="medium" if check["name"] in (
                "_check_state_db", "_check_cron_store", "_check_gateway_supervision", "_check_config_file",
                "_check_env_file") else "low",
                evidence={"hermes": text[:600], "key": told["key"], "tools": tools[:12], "section": check["section"],
                          "manual": text in check["manual"], "host": told["host"]},
                command=told["command"]))
    return found


def summary(result: dict) -> dict:
    """Counts of one profile's run: problems, warnings and optional pieces not set up."""
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error")}
    counts = {"ok": True, "checks": len(result.get("checks") or ()), "fail": 0, "warn": 0, "optional": 0,
              "issues": 0, "seconds": result.get("seconds")}
    for check in result.get("checks") or ():
        counts["issues"] += len(check["issues"]) + len(check["manual"])
        for row in check["rows"]:
            if row["mark"] == "fail":
                counts["fail"] += 1
            elif row["mark"] == "warn":
                counts["optional" if _OPTIONAL.search(row["text"]) else "warn"] += 1
    return counts


# ── once a day, by the profile that looks after the whole Hermes ────────────────────────────────────────────────
MEMORY_FLOOR = 600 * 2**20   # each run takes about 210 MB for a few seconds: never on a machine short of memory


def _memory_available() -> int | None:
    try:
        with open("/proc/meminfo", encoding="ascii") as stream:
            for line in stream:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def stored(conn) -> dict:
    row = conn.execute("SELECT value FROM cursor WHERE key='doctor_runs'").fetchone()
    try:
        value = json.loads(row[0]) if row else {}
    except ValueError:
        value = {}
    return value if isinstance(value, dict) else {}


def current(runs: dict, *, now: float) -> dict:
    """The runs recent enough to speak for their profile now."""
    kept = {}
    for name, item in runs.items():
        try:
            at = float(item.get("at") or 0) if isinstance(item, dict) else 0.0
        except (TypeError, ValueError):
            continue
        if 0 <= now - at < KEEP_SECONDS:
            kept[name] = item
    return kept


def collect(home: Path, rows_: list[dict], *, now: float, deadline: float | None = None, runner=None,
            force: bool = False) -> dict:
    """{profile: {at, result}} for the profiles in use, each run at most once a day (or now with `force`), within the
    pass's time. Kept in the lead's database; a result older than KEEP_SECONDS is dropped."""
    from .experience import experience

    with experience(home) as conn:
        runs = stored(conn)
    names = {row["profile"] for row in rows_}
    runs = current({name: item for name, item in runs.items() if name in names}, now=now)
    order = sorted(rows_, key=lambda row: (-(row.get("sessions_7d") or 0), row["profile"]))
    for row in order:
        name = row["profile"]
        if not _PROFILE.fullmatch(name):
            continue
        last = runs.get(name) or {}
        fresh = 0 <= now - float(last.get("at") or 0) < EVERY_SECONDS
        if fresh and not force:
            continue
        if not (row.get("sessions_7d") or row.get("self") or name == "default"):
            continue   # a profile nobody uses waits for the owner to ask
        if deadline is not None and deadline - time.monotonic() < TIMEOUT_SECONDS + 15:
            break
        free = _memory_available() if runner is None else None
        if free is not None and free < MEMORY_FLOOR:
            break   # tried again at the next pass
        runs[name] = {"at": now, "result": run(Path(row["home"]), runner=runner)}
    with experience(home) as conn:
        conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('doctor_runs', ?)", (json.dumps(runs),))
    return runs


def forget(conn, profile: str) -> None:
    """Drop a profile's stored run (a change found stale): the next look runs the checks again there."""
    runs = stored(conn)
    if runs.pop(profile, None) is not None:
        conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('doctor_runs', ?)", (json.dumps(runs),))


def verify(home: Path, proposal: dict, *, runner=None) -> dict | None:
    """The doctor finding behind a proposal, checked again now: the same code in a fresh run of the checks that
    produce it, or None."""
    from .environment import profiles, root_of

    subject = str(proposal.get("subject") or "")
    profile = subject.split("/", 1)[0]
    if not _PROFILE.fullmatch(profile):
        return None
    homes = dict(profiles(root_of(Path(home))))
    target = homes.get(profile)
    if target is None:
        return None
    kind = proposal.get("kind") or ""
    only = {"doctor_cache": ("_check_directory_structure",), "doctor_checkpoints": ("_check_checkpoint_store",)}.get(kind, ())
    result = run(target, only=only, runner=runner)
    return next((item for item in findings(profile, target, result) if item["code"] == proposal.get("code")), None)
