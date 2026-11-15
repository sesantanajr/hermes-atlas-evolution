"""Select comparable signals from profile-local observations for adaptive review."""


def current_metrics(host: dict, usage: dict, workloads: dict, baseline: dict) -> dict:
    memory = host.get("memory") or {}
    total, available = memory.get("total_bytes"), memory.get("available_bytes")
    metrics = {"prompt_tokens": baseline.get("average_prompt_tokens"), "research_ms": baseline.get("research_p95_ms")}
    if type(total) is int and total > 0 and type(available) is int and 0 <= available <= total:
        metrics["memory_available_pct"] = 100 * available / total
    pressure = host.get("pressure") or {}
    for kind in ("memory", "io", "cpu"):
        average = ((pressure.get(kind) or {}).get("some") or {}).get("avg60")
        if average is not None:
            metrics[f"{kind}_psi_pct"] = average
    for lane, metric in (("main", "api_error_pct"), ("tool", "tool_error_pct")):
        considered = {"ok", "error", "auth", "timeout", "rate_limit", "upstream"} if lane == "main" else {"ok", "error"}
        rows = [row for row in usage["groups"] if row["lane"] == lane and row["outcome"] in considered]
        count = sum(row["requests"] for row in rows)
        if count >= 5:
            metrics[metric] = 100 * sum(row["requests"] for row in rows if row["outcome"] != "ok") / count
    kanban = workloads["kanban"]
    if kanban["status"] == "ok":
        metrics["queue_depth"] = kanban["counts"].get("ready", 0)
    return metrics


def workload_correlations(workloads: dict, native_findings: list[dict]) -> list[dict]:
    cron, kanban = workloads["cron"], workloads["kanban"]
    if cron["status"] != "ok" or kanban["status"] != "ok":
        return []
    worker_limits = next((item["evidence"][0]["value"] for item in native_findings
                          if item["code"] == "kanban_worker_rate_limits"), 0)
    burst = (cron.get("schedule") or {}).get("peak_scheduled_agent_starts", 0)
    ready = kanban["counts"].get("ready", 0)
    if worker_limits < 3 or burst < 2 or ready < 2:
        return []
    return [{
        "code": "cross_workload_capacity_review", "severity": "warning", "confidence": "medium",
        "hypothesis": "Há fila pronta, saídas por limite de taxa e um pico previsto de rotinas com modelo.",
        "evidence": [{"signal": "kanban.ready", "value": ready},
                     {"signal": "kanban.observed_worker_rate_limits", "value": worker_limits},
                     {"signal": "cron.forecast_peak_agent_starts", "value": burst}],
        "alternatives": ["A projeção do Cron e as falhas do Kanban cobrem janelas distintas. A cota compartilhada precisa ser confirmada."],
        "action_ids": ["test_cron_staggering_before_adding_workers"],
        "recommendation": "Compare cotas e horários por provedor. Faça um ensaio escalonando os disparos antes de abrir mais workers.",
    }]
