"""Atlas Evolution panel for the Hermes dashboard, mounted at /api/plugins/hermes-atlas-evolution/.

It shows what `hermes evolution status`, `report`, `lessons list` and `proposals list` show, for the profile
the dashboard is looking at (Hermes scopes every plugin request to that profile and refuses it where the
plugin is disabled). It decides nothing: approving, rejecting, retiring and importing stay commands for the
owner's own terminal, and the panel prints them with the profile's `-p` already filled in.

The plugin's code is loaded from the folder next to this one under a private module name, so the panel never
shares state with a copy of the plugin the dashboard process may have loaded for its hooks.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import threading
import time
from pathlib import Path

PACKAGE = "hermes_atlas_evolution_panel"
_LOCK = threading.Lock()


def _module(name: str):
    """A module of the plugin's package, imported once under PACKAGE."""
    with _LOCK:
        if PACKAGE not in sys.modules:
            folder = Path(__file__).resolve().parent.parent / "hermes_atlas_evolution"
            spec = importlib.util.spec_from_file_location(PACKAGE, folder / "__init__.py",
                                                          submodule_search_locations=[str(folder)])
            package = importlib.util.module_from_spec(spec)
            sys.modules[PACKAGE] = package
            try:
                spec.loader.exec_module(package)
            except Exception:
                sys.modules.pop(PACKAGE, None)
                raise
    return importlib.import_module(f"{PACKAGE}.{name}")


def _prefix(profile: str) -> str:
    return "hermes" if profile == "default" else f"hermes -p {profile}"


def snapshot(home: Path, *, now: float, runtime_config: dict | None = None, lang: str | None = None,
             days: int = 7) -> dict:
    """Everything the panel shows, for one profile. Raises OSError or sqlite3.Error when the store is busy."""
    cli, evolve, status, report = _module("cli"), _module("evolve"), _module("status"), _module("report")
    lessons, proposals, experience = _module("lessons"), _module("proposals"), _module("experience")
    runtime_config = runtime_config if isinstance(runtime_config, dict) else {}
    days = days if isinstance(days, int) and 1 <= days <= 35 else 7
    config = cli.effective_settings(home, runtime_config)
    opts = evolve.options(config, runtime_config, strict=False)
    lang = lang if lang in ("en", "pt") else opts["language"]
    state = status.build(home, now=now, config=config, runtime_config=runtime_config)
    digest = report.build(home, now=now, days=days, lang=lang, rates=config.get("rates_usd_per_million", {}),
                          own_transform=opts["tool_hints"] and opts["lessons"] != "off")
    with experience.experience(home) as conn:
        playbook = lessons.listing(conn, lang=lang)
        queue = proposals.listing(conn, status="open")
    hermes = _prefix(state["profile"])
    return {
        "lang": lang, "days": days, "generated": now, "profile": state["profile"], "version": state["plugin_version"],
        "mode": opts["lessons"], "status": state, "report": digest, "lessons": playbook, "proposals": queue,
        "status_md": status.markdown(state, lang), "report_md": report.markdown(digest, lang),
        "commands": {"review": f"{hermes} evolution lessons review", "learn": f"{hermes} evolution learn",
                     "approve_lesson": f"{hermes} evolution lessons approve {{id}}",
                     "retire_lesson": f"{hermes} evolution lessons retire {{id}}",
                     "approve_proposal": f"{hermes} evolution proposals approve {{id}}",
                     "reject_proposal": f"{hermes} evolution proposals reject {{id}}",
                     "export": f"{hermes} evolution lessons export --out lessons.json",
                     "import": f"{hermes} evolution lessons import --from <profile> --dry-run"},
    }


def _current() -> tuple[Path, dict]:
    """The home and configuration of the profile this request is scoped to."""
    from hermes_constants import get_hermes_home

    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
    except Exception:   # the panel still shows the plugin's own data
        config = {}
    return Path(get_hermes_home()), config


def _router():
    try:
        from fastapi import APIRouter, Query
        from fastapi.responses import JSONResponse
    except ImportError:   # not the dashboard: nothing to mount
        return None
    router = APIRouter()

    @router.get("/snapshot")
    def get_snapshot(days: int = Query(7, ge=1, le=35), lang: str = Query("", max_length=2)):
        # A plain def: FastAPI runs it in a worker thread, so reading the store never blocks the event loop.
        import sqlite3

        try:
            home, config = _current()
            return snapshot(home, now=time.time(), runtime_config=config, lang=lang or None, days=days)
        except (OSError, sqlite3.Error):
            return JSONResponse({"error": "busy"}, status_code=503)

    return router


router = _router()
