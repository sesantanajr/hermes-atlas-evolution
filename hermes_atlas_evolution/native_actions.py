"""Operator actions that reuse supported Hermes Kanban and cron interfaces."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

PLAYBOOKS = {
    "resources": "Compare recent PSI, available RAM, cgroup limits and CPU or I/O contention. Report measured deltas.",
    "research": "Propose distinct research lanes and primary sources. Verify evidence with the Hermes citation skill when available.",
    "delegation": "Review failed versus successful delegation, runtime and concurrency. Propose the smallest useful split and a verification step.",
    "curator": "Review aggregate skill-use and Curator results. Identify a candidate and the native rollback procedure.",
    "gateway": "Review gateway state and sanitized incident totals. Identify the most useful independent health check.",
}
_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_SUBJECT = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.@-]{0,79}\Z")
_TASK_ID = re.compile(r"t_[0-9a-f]{8}\Z")
_TAG = re.compile(r"v?[0-9]+\.[0-9]+\.[0-9]+(?:[-.][0-9A-Za-z.-]+)?\Z")
_TASK_STATUSES = {"blocked", "triage", "todo", "ready", "running", "review", "done", "archived", "scheduled"}
HANDOFF_RULES = ("Use verification and observation only. Report evidence, uncertainty and a proposed next step. "
                 "Keep raw logs and secrets out of the handoff. Configuration changes and service operations require "
                 "separate approval.")
_RUNNER_FLAGS = ("HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID")
_JOB_NAME = "hermes-atlas-evolution-watchdog"
_SCRIPT_NAME = _JOB_NAME + ".py"


def _profile_args(profile: Path) -> list[str]:
    # A root HERMES_HOME alone can still follow Hermes's sticky active_profile.
    name = profile.name if profile.parent.name == "profiles" else "default"
    if not _SLUG.fullmatch(name):
        raise ValueError("Invalid Hermes profile directory name")
    return ["--profile", name]


def create_handoff(home: Path, config: dict, *, kind: str, subject: str, now: float | None = None,
                   runner=None, lang: str = "en") -> dict:
    """Request one bounded, blocked card using a fixed verification playbook."""
    policy = config.get("kanban_handoff")
    if policy is None or isinstance(policy, dict) and policy.get("enabled") is not True:
        return {"status": "disabled", "reason": "kanban_handoff_not_enabled"}
    if not isinstance(policy, dict):
        raise ValueError("kanban_handoff must be a settings object")
    board, assignee = policy.get("board"), policy.get("assignee")
    if not all(isinstance(item, str) and _SLUG.fullmatch(item) for item in (board, assignee)):
        raise ValueError("kanban_handoff requires a board slug and an assignee profile")
    if not isinstance(kind, str) or kind not in PLAYBOOKS:
        raise ValueError("Unsupported handoff playbook")
    if not isinstance(subject, str) or not _SUBJECT.fullmatch(subject):
        raise ValueError("Use an identifier as the handoff subject, with up to 80 characters")
    if any(os.environ.get(key, "").strip() for key in _RUNNER_FLAGS):
        return {"status": "skipped", "reason": "native_worker_context"}
    profile = Path(home).expanduser().resolve()
    if not profile.is_dir():
        return {"status": "unavailable", "reason": "profile_not_found"}
    executable = shutil.which("hermes")
    if not executable or Path(executable).suffix.lower() in (".bat", ".cmd"):
        return {"status": "unavailable", "reason": "hermes_launcher_not_found"}
    stamp = datetime.now(timezone.utc) if now is None else datetime.fromtimestamp(now, timezone.utc)
    identity = json.dumps([os.path.normcase(str(profile)), board, assignee, kind, subject, str(stamp.date())])
    key = "hermes-atlas-evolution:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    from .i18n import text

    portuguese = lang == "pt"
    title = f"Atlas Evolution: {'verificar' if portuguese else 'verify'} {kind} / {subject}"
    body = (
        f"{'Assunto' if portuguese else 'Subject'}: {subject}\n\n{text(PLAYBOOKS[kind], lang)}\n\n"
        + text(HANDOFF_RULES, lang)
    )
    command = [
        str(Path(executable).resolve()), *_profile_args(profile), "kanban", "--board", board, "create", title,
        "--body", body, "--assignee", assignee, "--initial-status", "blocked",
        "--max-retries", "1", "--max-runtime", "5m", "--created-by", "hermes-atlas-evolution",
        "--idempotency-key", key, "--json",
    ]
    environment = dict(os.environ, HERMES_HOME=str(profile))
    run = subprocess.run if runner is None else runner
    try:
        result = run(command, env=environment, cwd=profile, stdin=subprocess.DEVNULL,
                     capture_output=True, encoding="utf-8", errors="replace", timeout=20, check=False)
    except OSError:
        return {"status": "unavailable", "reason": "hermes_launcher_failed"}
    except subprocess.TimeoutExpired:
        return {"status": "unverified", "reason": "native_handoff_timeout", "idempotency_key": key}
    if result.returncode != 0:
        return {"status": "unverified", "reason": "native_handoff_failed", "idempotency_key": key}
    try:
        task = json.loads(result.stdout) if len(result.stdout) <= 65_536 else None
    except (ValueError, TypeError):
        task = None
    task_id = task.get("id") if isinstance(task, dict) else None
    native_status = task.get("status") if isinstance(task, dict) else None
    if not (isinstance(task_id, str) and _TASK_ID.fullmatch(task_id)
            and isinstance(native_status, str) and native_status in _TASK_STATUSES):
        return {"status": "unverified", "reason": "invalid_native_handoff_result", "idempotency_key": key}
    return {
        "status": native_status, "task_id": task_id, "board": board,
        "idempotency_key": key, "requires_operator_release": native_status == "blocked",
    }


def schedule_spec(home: Path, *, every_minutes: int = 5, upstream_tag: str | None = None, lang: str = "en") -> dict:
    """Produce a profile-local, no-agent cron recipe without writing files or jobs. Its messages reach the owner
    through the job's delivery target, so they are written in `lang`."""
    from .i18n import text

    if type(every_minutes) is not int or not 5 <= every_minutes <= 1440:
        raise ValueError("Cron interval must be between 5 and 1440 minutes")
    if upstream_tag is not None and (
        not isinstance(upstream_tag, str) or len(upstream_tag) > 48 or not _TAG.fullmatch(upstream_tag)
    ):
        raise ValueError("Use a released version tag such as v0.23.1")
    profile = Path(home).expanduser().resolve()
    profile_args = _profile_args(profile)
    arguments = [*profile_args, "evolution", "ops", "tick", "--notify-only"]
    if upstream_tag:
        arguments += ["--upstream-tag", upstream_tag]
    script = f'''"""Run the configured Atlas Evolution policy without an agent turn."""
import os
import shutil
import subprocess

launcher = shutil.which("hermes")
if not launcher:
    raise SystemExit({text("Hermes CLI is not on the scheduler PATH.", lang)!r})
try:
    result = subprocess.run(
        [launcher, *{arguments!r}], capture_output=True, text=True,
        env=dict(os.environ, HERMES_HOME={str(profile)!r}),
        encoding="utf-8", errors="replace", timeout=300, check=False
    )
    if result.returncode or len(result.stdout) > 65_536:
        raise SystemExit({text("Atlas Evolution tick failed. Run ops tick in this profile to inspect.", lang)!r})
except (OSError, subprocess.TimeoutExpired):
    raise SystemExit({text("Atlas Evolution tick could not be verified.", lang)!r}) from None

if result.stdout.strip():
    print(result.stdout.rstrip())
'''
    create_args = [
        "hermes", *profile_args, "cron", "create", f"every {every_minutes}m", "--name", _JOB_NAME,
        "--no-agent", "--script", _SCRIPT_NAME, "--deliver", "local",
    ]
    return {
        "status": "planned", "profile_home": str(profile),
        "script_name": _SCRIPT_NAME, "script_path": str(profile / "scripts" / _SCRIPT_NAME),
        "script_content": script, "create_args": create_args,
        "posix_command": f"HERMES_HOME={shlex.quote(str(profile))} {shlex.join(create_args)}",
    }
