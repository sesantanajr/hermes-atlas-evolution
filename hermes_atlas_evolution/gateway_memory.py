"""The gateway's memory, against the budget Hermes keeps for its agent cache (0.14.0).

A gateway keeps one agent per warm conversation, each with its whole transcript in memory. Hermes sheds the least
recently used ones once the process's anonymous memory passes a budget (`agent.agent_cache.memory_high_mb`, "auto"
by default: 65% of the service's MemoryHigh, else of MemoryMax, else of the machine's memory). Every time it does, the
next message of each conversation it shed rebuilds it from disk (slower), and when nothing can be shed (every agent
in a turn) memory keeps climbing towards the service's limit, where the kernel first throttles and then kills it.

This module reads, and never changes anything: the gateway's process (/proc), its service's limits (`systemctl show`),
the machine's memory, Hermes' setting, and how often the log said the budget was passed (logsignals). From that it
says whether the budget fits what the gateway really uses, and what to change if it does not: a higher MemoryHigh for
the service when the machine has room, or a smaller agent cache when it has not.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

AUTO_FRACTION = 0.65           # gateway/agent_cache_pressure.py _AUTO_BUDGET_FRACTION
AUTO_FLOOR_MB = 512            # below it Hermes turns the pressure pass off
HEADROOM = 1.2                 # a budget this far above the peak the log saw fits
MACHINE_SHARE = 0.6            # a service's MemoryMax should stay within this share of the machine's memory
UNIT = "hermes-gateway.service"
_MB = 1024 * 1024
_KB_LINE = re.compile(r"^(\w+):\s+(\d+)\s*kB", re.M)


def _q(count, one: str, many: str) -> str:
    """A count with its word in the right number: 1 vez, 2 vezes."""
    return f"{count} {one if count == 1 else many}"


def _number(value) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in ("infinity", "[not set]", "max") or not text.isdigit():
        return None
    number = int(text)
    return number if 0 < number < (1 << 62) else None


def _proc_kb(host, path: str) -> dict:
    text = host.read(path, 65_536) if host is not None else None
    return {key: int(value) for key, value in _KB_LINE.findall(text or "")}


def gateway_pid(root: Path) -> int | None:
    """The gateway's pid from Hermes' own record of the root (gateway.pid), when it names a live process."""
    import os

    try:
        with open(Path(root) / "gateway.pid", "rb") as stream:
            record = json.loads(stream.read(65_536) or b"{}")
    except (OSError, ValueError):
        return None
    pid = record.get("pid") if isinstance(record, dict) else None
    if type(pid) is not int or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except (PermissionError, OSError):
        pass
    return pid


def unit_limits(host, unit: str = UNIT) -> dict:
    """MemoryHigh, MemoryMax, MemoryCurrent and MemoryPeak of the gateway's service (user scope first), in bytes;
    absent where systemd does not say."""
    for prefix in (["--user"], []):
        code, out, _err = host.run(["systemctl", *prefix, "show", unit, "-p",
                                    "MemoryHigh,MemoryMax,MemoryCurrent,MemoryPeak,LoadState"], timeout=10)
        if code != 0:
            continue
        values = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        if values.get("LoadState") not in (None, "loaded"):
            continue
        return {"scope": "user" if prefix else "system",
                **{key: _number(values.get(key)) for key in ("MemoryHigh", "MemoryMax", "MemoryCurrent", "MemoryPeak")}}
    return {}


def configured_budget(config) -> tuple[str, int | None]:
    """(how the budget is set, its MB when it is a number): ("auto", None), ("off", None) or ("fixed", MB)."""
    section = ((config or {}).get("agent") or {}).get("agent_cache") if isinstance(config, dict) else None
    value = section.get("memory_high_mb", "auto") if isinstance(section, dict) else "auto"
    if value is False or isinstance(value, str) and value.strip().lower() in ("", "off", "none", "false", "disabled"):
        return "off", None
    if isinstance(value, str) and value.strip().lower() == "auto" or value is True:
        return "auto", None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "auto", None
    return ("fixed", number) if number > 0 else ("off", None)


def reading(home: Path, *, host=None, signals: dict | None = None, config=None) -> dict:
    """What the gateway uses, what it may use, what the budget is, and what the log saw (logsignals' summary)."""
    from .environment import _read_config, root_of

    if host is None:
        from .system_update import Host

        host = Host()
    root = root_of(Path(home))
    config = _read_config(root) if config is None else config
    pid = gateway_pid(root)
    process = _proc_kb(host, f"/proc/{pid}/status") if pid else {}
    machine = _proc_kb(host, "/proc/meminfo")
    limits = unit_limits(host)
    how, fixed = configured_budget(config)
    base = limits.get("MemoryHigh") or limits.get("MemoryMax") or (machine.get("MemTotal", 0) * 1024 or None)
    if how == "fixed":
        budget = fixed
    elif how == "auto" and base:
        budget = int(base * AUTO_FRACTION / _MB)
        budget = budget if budget >= AUTO_FLOOR_MB else None
    else:
        budget = None
    pressure = (signals or {}).get("memory_pressure") or {}
    found = {"pid": pid, "rss_mb": round(process.get("VmRSS", 0) / 1024) if process else None,
             "anon_mb": round(process.get("RssAnon", 0) / 1024) if process.get("RssAnon") else None,
             "budget_mb": budget, "budget_setting": how,
             "memory_high_mb": round(limits["MemoryHigh"] / _MB) if limits.get("MemoryHigh") else None,
             "memory_max_mb": round(limits["MemoryMax"] / _MB) if limits.get("MemoryMax") else None,
             "current_mb": round(limits["MemoryCurrent"] / _MB) if limits.get("MemoryCurrent") else None,
             "peak_unit_mb": round(limits["MemoryPeak"] / _MB) if limits.get("MemoryPeak") else None,
             "scope": limits.get("scope"),
             "machine_mb": round(machine.get("MemTotal", 0) / 1024) or None,
             "available_mb": round(machine.get("MemAvailable", 0) / 1024) or None,
             "events": int(pressure.get("count") or 0), "event_days": int(pressure.get("days") or 0),
             "peak_mb": round(float(pressure.get("peak") or 0)) or None,
             "stuck": int((pressure.get("detail") or {}).get("stuck") or 0)}
    found["suggestion"] = suggest(found)
    return found


def suggest(found: dict) -> dict | None:
    """What to change when the budget does not fit the peak the log saw; None when it fits or nothing was seen."""
    peak, budget = found.get("peak_mb"), found.get("budget_mb")
    if not found.get("events") or not peak or not budget:
        return None
    need = peak * HEADROOM
    if budget >= need:
        return None
    if found.get("budget_setting") == "fixed":
        return {"kind": "budget", "memory_high_mb": None, "budget_mb": int(math.ceil(need / 64) * 64)}
    high = int(math.ceil(need / AUTO_FRACTION / 256) * 256)
    ceiling = max(high + 1024, int(high * 1.5))
    machine = found.get("machine_mb") or 0
    if machine and ceiling <= machine * MACHINE_SHARE:
        return {"kind": "unit", "memory_high_mb": high, "memory_max_mb": ceiling,
                "budget_after_mb": int(high * AUTO_FRACTION)}
    return {"kind": "cache", "idle_ttl_secs": 900}


def _gb(mb, lang: str) -> str:
    if mb is None:
        return "?"
    text = f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"
    return text.replace(".", ",") if lang == "pt" else text


def lines(found: dict | None, lang: str = "en") -> list[str]:
    """The reading in Markdown bullets (empty without a gateway or a budget to compare)."""
    if not isinstance(found, dict) or not (found.get("pid") or found.get("events")):
        return []
    pt = lang == "pt"
    out = []
    now_part = (f"agora {_gb(found.get('rss_mb'), lang)}" + (f" (anônima {_gb(found.get('anon_mb'), lang)})"
                                                            if found.get("anon_mb") else "")) if pt else \
        (f"now {_gb(found.get('rss_mb'), lang)}" + (f" (anonymous {_gb(found.get('anon_mb'), lang)})"
                                                   if found.get("anon_mb") else ""))
    budget = found.get("budget_mb")
    if found.get("budget_setting") == "auto" and budget:
        source = (f"{int(AUTO_FRACTION * 100)}% do MemoryHigh de {_gb(found.get('memory_high_mb'), lang)}" if pt else
                  f"{int(AUTO_FRACTION * 100)}% of the {_gb(found.get('memory_high_mb'), lang)} MemoryHigh") \
            if found.get("memory_high_mb") else ("automático" if pt else "automatic")
        budget_part = (f"orçamento do cache de agentes {_gb(budget, lang)} ({source})" if pt else
                       f"agent cache budget {_gb(budget, lang)} ({source})")
    elif budget:
        budget_part = (f"orçamento do cache de agentes {_gb(budget, lang)} (fixo)" if pt else
                       f"agent cache budget {_gb(budget, lang)} (fixed)")
    else:
        budget_part = "sem orçamento do cache de agentes" if pt else "no agent cache budget"
    limit = found.get("memory_max_mb")
    parts = [now_part, budget_part]
    if limit:
        parts.append(f"limite do serviço {_gb(limit, lang)}" if pt else f"service limit {_gb(limit, lang)}")
    if found.get("events"):
        parts.append((f"{_q(found['events'], 'estouro', 'estouros')} em {_q(found['event_days'], 'dia', 'dias')}, pico {_gb(found.get('peak_mb'), lang)}"
                      if pt else f"{_q(found['events'], 'overrun', 'overruns')} on {_q(found['event_days'], 'day', 'days')}, peak "
                                 f"{_gb(found.get('peak_mb'), lang)}"))
    out.append(("- **Memória do gateway:** " if pt else "- **Gateway memory:** ") + " · ".join(parts) + ".")
    suggestion = found.get("suggestion")
    if suggestion and suggestion.get("kind") == "unit":
        out.append(
            f"  - O orçamento fica abaixo do que o gateway usa nos picos, então o Hermes tira conversas da memória "
            f"e a próxima mensagem delas demora mais. Esta máquina tem {_gb(found.get('machine_mb'), lang)}: dá para "
            f"subir o MemoryHigh do serviço para {_gb(suggestion['memory_high_mb'], lang)} e o MemoryMax para "
            f"{_gb(suggestion['memory_max_mb'], lang)} (`systemctl --user edit hermes-gateway.service`, com "
            f"`[Service]` `MemoryHigh={suggestion['memory_high_mb']}M` e `MemoryMax={suggestion['memory_max_mb']}M`, "
            f"depois reinicie o gateway); o orçamento passa a {_gb(suggestion['budget_after_mb'], lang)}. Ou deixe "
            "como está, se preferir menos memória a respostas mais rápidas nessas conversas." if pt else
            f"  - The budget is below what the gateway uses at its peaks, so Hermes drops conversations from memory and "
            f"their next message takes longer. This machine has {_gb(found.get('machine_mb'), lang)}: the service's "
            f"MemoryHigh can go to {_gb(suggestion['memory_high_mb'], lang)} and MemoryMax to "
            f"{_gb(suggestion['memory_max_mb'], lang)} (`systemctl --user edit hermes-gateway.service`, with "
            f"`[Service]` `MemoryHigh={suggestion['memory_high_mb']}M` and `MemoryMax={suggestion['memory_max_mb']}M`, "
            f"then restart the gateway); the budget becomes {_gb(suggestion['budget_after_mb'], lang)}. Or leave it, "
            "if you prefer less memory to faster answers in those conversations.")
    elif suggestion and suggestion.get("kind") == "budget":
        out.append(f"  - O orçamento fixo fica abaixo do pico: `hermes config set agent.agent_cache.memory_high_mb "
                   f"{suggestion['budget_mb']}` (sem passar de ~65% do MemoryHigh do serviço)." if pt else
                   f"  - The fixed budget is below the peak: `hermes config set agent.agent_cache.memory_high_mb "
                   f"{suggestion['budget_mb']}` (keeping it under ~65% of the service's MemoryHigh).")
    elif suggestion and suggestion.get("kind") == "cache":
        out.append("  - A máquina não tem folga para um limite maior: guarde conversas por menos tempo, "
                   "`hermes config set agent.agent_cache.idle_ttl_secs 900`." if pt else
                   "  - The machine has no room for a higher limit: keep conversations for less time, "
                   "`hermes config set agent.agent_cache.idle_ttl_secs 900`.")
    if found.get("stuck"):
        out.append(f"  - Em {found['stuck']} dos estouros nenhuma conversa podia sair (todas no meio de um turno): a "
                   "memória só cai quando esses turnos terminam." if pt else
                   f"  - In {found['stuck']} of the overruns no conversation could go (all in the middle of a turn): "
                   "memory only drops once those turns end.")
    return out
