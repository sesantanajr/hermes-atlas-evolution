"""English and Brazilian Portuguese for everything a person or the agent reads.

The language follows Hermes: the plugin's own `language` setting when it is `en` or `pt`, otherwise the
profile's `HERMES_LANGUAGE`, then `display.language` from the profile configuration, then the process
locale, then English. Hermes codes such as `pt`, `pt-BR` and `pt_br` all select Portuguese.

Diagnostic modules write their sentences in one language and `localize` maps them to the other through
the pairs in `catalog`, gettext style: the source sentence is the key. Codes, numbers and identifiers
are never translated. A test keeps the catalog complete.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache

from .catalog import PAIRS, PATTERNS

LANGUAGES = ("en", "pt")
_TO = {"en": {pt: en for en, pt in PAIRS}, "pt": {en: pt for en, pt in PAIRS}}
_PATTERNS = {
    "en": [(re.compile(pt_regex), en_format) for _en_regex, en_format, pt_regex, _pt_format in PATTERNS],
    "pt": [(re.compile(en_regex), pt_format) for en_regex, _en_format, _pt_regex, pt_format in PATTERNS],
}


def normalize(value) -> str | None:
    """Map a Hermes or POSIX language tag to `en` or `pt`; None for anything else or `auto`."""
    if not isinstance(value, str):
        return None
    tag = value.strip().lower().replace("_", "-").split(".")[0]
    if tag.startswith("pt"):
        return "pt"
    if tag.startswith("en"):
        return "en"
    return None


def _hermes_env_language() -> str | None:
    try:
        from agent.secret_scope import get_secret
    except ImportError:
        return os.environ.get("HERMES_LANGUAGE")
    try:
        value = get_secret("HERMES_LANGUAGE")
    except Exception:   # an unscoped read in a multiplexed process must not break the caller
        value = None
    return value if isinstance(value, str) else None


def _display_language(runtime_config) -> str | None:
    config = runtime_config
    if not isinstance(config, dict):
        try:
            from hermes_cli.config import load_config_readonly
        except ImportError:
            return None
        try:
            config = load_config_readonly() or {}
        except Exception:
            return None
    display = config.get("display") if isinstance(config, dict) else None
    value = display.get("language") if isinstance(display, dict) else None
    return value if isinstance(value, str) else None


def resolve(preferred=None, runtime_config=None) -> str:
    """The language for this profile: explicit setting, then Hermes, then the process locale, then English."""
    explicit = normalize(preferred)
    if explicit:
        return explicit
    for candidate in (_hermes_env_language(), _display_language(runtime_config),
                      os.environ.get("LC_ALL"), os.environ.get("LC_MESSAGES"), os.environ.get("LANG")):
        found = normalize(candidate)
        if found:
            return found
    return "en"


@lru_cache(maxsize=2048)
def text(value: str, lang: str) -> str:
    """Translate one sentence to `lang`. Unknown text is returned unchanged."""
    target = _TO.get(lang)
    if target is None:
        return value
    found = target.get(value)
    if found is not None:
        return found
    for pattern, replacement in _PATTERNS[lang]:
        match = pattern.fullmatch(value)
        if match:
            parts = [target.get(group, group) for group in match.groups()]
            return replacement.format(*parts)
    return value


def localize(value, lang: str):
    """Translate every catalogued sentence inside a result. Keys, codes and numbers stay as they are."""
    if lang not in LANGUAGES:
        return value
    if isinstance(value, str):
        return text(value, lang) if " " in value else value
    if isinstance(value, dict):
        return {key: localize(item, lang) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [localize(item, lang) for item in value]
    return value
