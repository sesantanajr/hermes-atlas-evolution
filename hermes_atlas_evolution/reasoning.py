"""Correlate locally measured signals and explain the limits of attribution."""

from __future__ import annotations

import math
import re
from collections import Counter
from statistics import median


def _number(value):
    if type(value) is int and 0 <= value <= 10**24:
        return value
    if type(value) is float and math.isfinite(value) and 0 <= value <= 10**24:
        return value
    return None


def _ratio(numerator, denominator):
    numerator, denominator = _number(numerator), _number(denominator)
    if numerator is not None and denominator:
        return round(numerator / denominator, 4)
    return None


def _pressure(host, resource, stall="some"):
    resource_data = (host.get("pressure") or {}).get(resource) or {}
    return _number((resource_data.get(stall) or {}).get("avg60"))


def _finding(code, severity, confidence, hypothesis, evidence, alternatives, actions, recommendation):
    return {
        "code": code,
        "severity": severity,
        "confidence": confidence,
        "hypothesis": hypothesis,
        "evidence": [{"signal": signal, "value": value} for signal, value in evidence.items() if value is not None],
        "alternatives": list(alternatives),
        "action_ids": list(actions),
        "recommendation": recommendation,
    }


def _bounded_samples(history, metric):
    values = history.get(metric)
    if isinstance(values, (tuple, list)):
        return [number for value in values[-35:] if (number := _number(value)) is not None]
    return []


def _above_baseline(history, metric, current):
    """Use a robust historical reference only when enough comparable samples exist."""
    samples = _bounded_samples(history, metric)
    if len(samples) < 5 or current is None:
        return None
    typical = median(samples)
    mad = median(abs(sample - typical) for sample in samples)
    if current >= max(typical * 1.5, typical + max(3 * mad, 1)):
        return typical
    return None


def _host_diagnoses(health, host, contention):
    native_host = health.get("host") or {}
    logs = (health.get("logs") or {}).get("events") or {}
    memory = host.get("memory") or native_host.get("memory") or {}
    cgroup = host.get("cgroup") or {}
    disk = host.get("disk") or native_host.get("disk") or {}
    load = host.get("load") or native_host.get("load") or {}
    pressure_memory = _pressure(host, "memory")
    cgroup_pressure = _pressure(cgroup, "memory")
    pressure_io = _pressure(host, "io")
    pressure_cpu = _pressure(host, "cpu")
    mem_available = _ratio(memory.get("available_bytes"), memory.get("total_bytes"))
    mem_cache = _ratio(memory.get("page_cache_bytes"), memory.get("total_bytes"))
    cgroup_used = _ratio(cgroup.get("memory_current_bytes"), cgroup.get("memory_max_bytes"))
    cgroup_anon = _ratio(cgroup.get("memory_anon_bytes"), cgroup.get("memory_current_bytes"))
    cpu_load = _ratio(load.get("one_minute"), load.get("logical_cpus"))
    disk_free = _ratio(disk.get("free_bytes"), disk.get("total_bytes"))
    inode_free = _ratio(disk.get("inodes_available"), disk.get("inodes_total"))
    storage_failures = _number(logs.get("storage_io")) or 0
    locks = _number(logs.get("database_locked")) or 0
    oom = _number(logs.get("resource_exhaustion")) or 0

    low_memory = mem_available is not None and mem_available < 0.10
    tight_cgroup = cgroup_used is not None and cgroup_used >= 0.90
    memory_stalls = (pressure_memory or 0) >= 2 or (cgroup_pressure or 0) >= 2
    if low_memory or tight_cgroup or memory_stalls:
        convergent = memory_stalls and (low_memory or tight_cgroup or bool(oom))
        yield _finding(
            "memory_reclaim_contention", "warning", "high" if convergent else "medium",
            "Pressão de memória com espera de tarefas." if memory_stalls else "Folga de memória reduzida no host ou cgroup.",
            {"host.mem_available_ratio": mem_available, "host.page_cache_ratio": mem_cache,
             "host.psi_memory_some_avg60_pct": pressure_memory, "cgroup.psi_memory_some_avg60_pct": cgroup_pressure,
             "cgroup.memory_limit_used_ratio": cgroup_used, "cgroup.anon_memory_share": cgroup_anon,
             "logs.resource_exhaustion": oom},
            ("Picos curtos de outros serviços podem explicar a amostra.",
             "A ocupação do cgroup pode incluir cache de arquivos recuperável."),
            ("pause_optional_context", "review_memory_working_set") if convergent else ("review_memory_working_set",),
            "Compare a pressão em ciclos consecutivos e meça a memória dos serviços residentes e das rotinas em lote.",
        )
    elif oom:
        yield _finding(
            "recent_memory_exhaustion", "warning", "medium", "Os logs registraram falta de memória na janela analisada.",
            {"logs.resource_exhaustion": oom, "host.mem_available_ratio": mem_available,
             "host.psi_memory_some_avg60_pct": pressure_memory, "cgroup.memory_limit_used_ratio": cgroup_used},
            ("A falha pode pertencer a outro processo, ao cgroup ou a um pico que já passou.",),
            ("review_memory_working_set",), "Compare a série do host e o horário do job com os registros do supervisor.",
        )
    elif mem_cache is not None and mem_cache >= 0.30 and mem_available is not None and mem_available >= 0.25 and pressure_memory is not None:
        yield _finding(
            "page_cache_headroom", "info", "high", "O cache de arquivos está alto e o host ainda tem RAM disponível.",
            {"host.mem_available_ratio": mem_available, "host.page_cache_ratio": mem_cache,
             "host.psi_memory_some_avg60_pct": pressure_memory}, (), (),
            "Preserve o cache e acompanhe a memória disponível e os stalls de memória.",
        )

    capacity_low = disk_free is not None and disk_free < 0.10
    inodes_low = inode_free is not None and inode_free < 0.05
    if capacity_low or inodes_low or storage_failures:
        yield _finding(
            "native_write_capacity_risk", "critical" if storage_failures and (capacity_low or inodes_low) else "warning",
            "high" if (capacity_low or inodes_low) and storage_failures else "medium",
            "A escassez de espaço ou inodes no volume do perfil é compatível com falhas de gravação."
            if capacity_low or inodes_low else "Os logs mostram esgotamento de espaço em algum caminho usado pelo Hermes.",
            {"profile_disk.free_ratio": disk_free, "profile_disk.free_inode_ratio": inode_free,
             "logs.storage_capacity_errors": storage_failures},
            ("Um diretório de cache, montagem remota ou quota pode usar outro volume.",),
            ("pause_optional_context", "review_storage_hotspots") if capacity_low or inodes_low else ("review_storage_hotspots",),
            "Localize o volume e a quota da gravação que falhou. Verifique cache e retenção com os serviços proprietários.",
        )

    for kind, pressure, corroboration in (("cpu", pressure_cpu, cpu_load), ("io", pressure_io, locks)):
        threshold = 10 if kind == "cpu" else 5
        candidate = contention.get(kind, {})
        measured = _number(candidate.get("pressure_avg10"))
        busy_background = _number(candidate.get("background_units_per_second"))
        busy_hermes = _number(candidate.get("hermes_units_per_second"))
        background_share = _number(candidate.get("share"))
        # The paired sampler requires consistent pressure in both snapshots and real activity in both cgroups.
        attributed = bool(candidate.get("attributed") is True and (measured or 0) >= threshold
                          and busy_background and busy_hermes and background_share is not None
                          and 0.65 <= background_share <= 1)
        if not attributed and (pressure or 0) < threshold:
            continue
        linked = (corroboration or 0) >= (1.25 if kind == "cpu" else 2)
        actions = (f"rebalance_background_{kind}", f"review_{kind}_contention") if attributed else (f"review_{kind}_contention",)
        yield _finding(
            f"{kind}_contention", "warning", "high" if attributed or linked else "medium",
            "A carga de segundo plano monitorada coincide com pressão nas duas amostras pareadas."
            if attributed else "Há espera sustentada pelo recurso no host.",
            {f"host.psi_{kind}_some_avg60_pct": pressure,
             "host.load_per_cpu" if kind == "cpu" else "logs.database_locked": corroboration,
             "sample.psi_some_avg10_pct": measured if attributed else None,
             "sample.background_units_per_second": busy_background if attributed else None,
             "sample.hermes_units_per_second": busy_hermes if attributed else None,
             "sample.background_share": background_share if attributed else None},
            ("Outros processos podem contribuir para a pressão no mesmo intervalo.",
             "A latência do provedor e gargalos de outros volumes exigem confirmação própria."),
            actions,
            "Meça de novo depois de reduzir a prioridade da tarefa de segundo plano autorizada."
            if attributed else "Identifique o processo ou volume responsável antes de alterar prioridades.",
        )

    memory_candidate = contention.get("memory", {})
    bounded_high = _number(memory_candidate.get("memory_high_bytes"))
    working_set = _number(memory_candidate.get("background_memory_current_bytes"))
    cache_headroom = _number(memory_candidate.get("background_inactive_clean_file_bytes"))
    if (memory_candidate.get("attributed") is True and (_number(memory_candidate.get("pressure_avg10")) or 0) >= 5
            and bounded_high and working_set and cache_headroom and 0 < working_set - bounded_high <= cache_headroom):
        yield _finding(
            "background_memory_reclaim_trial", "warning", "medium",
            "A pressão de memória coincidiu com cache recuperável na unidade auxiliar nas duas amostras.",
            {"sample.psi_memory_some_avg10_pct": memory_candidate["pressure_avg10"],
             "sample.background_current_bytes": working_set, "sample.background_clean_file_bytes": cache_headroom,
             "operator.memory_high_bytes": bounded_high},
            ("Falhas de página e outras unidades podem contribuir para a espera no Hermes.",),
            ("rebalance_background_memory",),
            "Verifique se o limite temporário reduz pressão sem aumentar swap, OOM ou latência na unidade auxiliar.",
        )

    if locks >= 2 and (pressure_io or 0) < 5:
        yield _finding(
            "sqlite_writer_contention", "warning", "medium", "Gravações concorrentes são uma hipótese para os bloqueios de SQLite.",
            {"logs.database_locked": locks, "host.psi_io_some_avg60_pct": pressure_io},
            ("Um escritor prolongado, checkpoints ou bloqueios de um volume remoto também podem produzir esse padrão.",),
            ("review_sqlite_writer_schedule",), "Correlacione os horários de jobs, backups e checkpoints com os bloqueios.",
        )


def _request_diagnoses(usage, research, history, daily):
    rows = usage.get("groups") or ()
    main = [row for row in rows if row.get("lane") == "main"]
    failures = Counter()
    total = Counter()
    for row in main:
        requests = _number(row.get("requests")) or 0
        samples = _number(row.get("usage_samples")) or 0
        outcome = row.get("outcome")
        if outcome in ("rate_limit", "upstream", "auth", "timeout", "error"):
            failures[outcome] += requests
        elif outcome == "ok":
            total["requests"] += requests
            total["samples"] += samples
            for bucket in ("input_tokens", "cache_read_tokens", "cache_write_tokens"):
                total[bucket] += _number(row.get(bucket)) or 0

    counted = total["requests"] + sum(failures.values())
    if failures["rate_limit"] >= 3:
        yield _finding(
            "provider_rate_limit", "warning", "high", "O provedor recusou repetidamente tentativas por limite de taxa.",
            {"main.rate_limit_attempts": failures["rate_limit"], "main.known_attempts": counted,
             "main.rate_limit_ratio": _ratio(failures["rate_limit"], counted)},
            ("Pode ser cota compartilhada, sobreposição de rotinas ou limite aplicado pelo relay.",),
            ("review_concurrency_and_backoff",), "Compare a concorrência por perfil e teste espaçar rotinas antes de mudar a rota.",
        )
    if failures["auth"] >= 2:
        yield _finding(
            "provider_authorization_failures", "warning", "high", "Há recusas HTTP de autenticação ou autorização.",
            {"main.authorization_attempts": failures["auth"], "main.known_attempts": counted},
            ("Uma permissão insuficiente ou relay intermediário também pode emitir 401 ou 403.",),
            ("verify_provider_authorization",), "Verifique conta, validade e permissão usando o diagnóstico nativo do provedor.",
        )
    if failures["upstream"] + failures["timeout"] >= 3:
        yield _finding(
            "provider_attempt_failures", "warning", "medium", "Timeouts ou erros do provedor contribuem para as tentativas com falha.",
            {"main.upstream_attempts": failures["upstream"], "main.timeout_attempts": failures["timeout"],
             "main.known_attempts": counted},
            ("A rota de rede, o relay e a carga no host também podem explicar a latência.",),
            ("compare_provider_routes",), "Correlacione erros e tempo de resposta por provedor e compare com o fallback existente.",
        )

    prompt = total["input_tokens"] + total["cache_read_tokens"] + total["cache_write_tokens"]
    avg_prompt = _ratio(prompt, total["samples"])
    cache_share = _ratio(total["cache_read_tokens"], prompt)
    usage_coverage = _ratio(total["samples"], total["requests"])
    baseline = _above_baseline(history, "average_prompt_tokens", daily.get("average_prompt_tokens"))
    if total["samples"] >= 5 and avg_prompt is not None and avg_prompt >= 5000 and cache_share is not None and cache_share < 0.15:
        yield _finding(
            "large_prompt_low_reuse", "info", "high" if (usage_coverage or 0) >= 0.7 else "medium",
            "Prompts grandes com pouco reaproveitamento dominam as amostras medidas.",
            {"main.average_prompt_tokens": avg_prompt, "main.cache_read_share": cache_share,
             "main.usage_samples": total["samples"], "main.usage_coverage": usage_coverage,
             "history.average_prompt_tokens_median": baseline},
            ("Alguns modelos ou provedores não informam cache.",
             "O histórico acumulado, schemas ou instruções podem aumentar a entrada."),
            ("benchmark_prompt_components",), "Compare tamanho dos schemas, instruções e memória em um conjunto estável de tarefas.",
        )

    turns = _number(research.get("turns")) or 0
    slow_turns = _number(research.get("slow_turns")) or 0
    limit = _number(research.get("limit_ms"))
    p95 = _number(research.get("p95_ms"))
    max_ms = _number(research.get("max_ms"))
    tool_calls = _number(research.get("tool_calls")) or 0
    tool_errors = _number(research.get("tool_errors")) or 0
    if limit and turns and 0 < slow_turns <= turns and max(p95 or 0, max_ms or 0) >= limit:
        error_ratio = _ratio(tool_errors, tool_calls)
        hypothesis = ("A janela reuniu turnos de pesquisa lentos e falhas em ferramentas de pesquisa."
                      if tool_errors >= 2 and (error_ratio or 0) >= 0.20
                      else "Parte dos turnos de pesquisa excede o orçamento de duração configurado.")
        yield _finding(
            "research_slow_turns", "warning", "high" if turns >= 10 and slow_turns >= 3
            else "medium" if turns >= 3 and slow_turns >= 2 else "low", hypothesis,
            {"research.sampled_turns": turns, "research.slow_turns": slow_turns,
             "research.p95_ms": p95, "research.max_ms": max_ms, "research.limit_ms": limit,
             "research.tool_calls": tool_calls, "research.tool_errors": tool_errors,
             "history.research_p95_ms_median": _above_baseline(history, "research_p95_ms", daily.get("research_p95_ms"))},
            ("A pesquisa pode ter exigido mais fontes ou trabalho de síntese no provedor.",
             "O agregado de turnos não separa toda a espera do provedor da espera de ferramentas."),
            ("review_research_budget",), "Meça chamadas por turno e tempo por ferramenta. Teste limites por etapa com qualidade observada.",
        )

    for metric, label in (("average_prompt_tokens", "tokens de entrada"), ("research_p95_ms", "tempo de pesquisa")):
        current = daily.get(metric)
        historical = _above_baseline(history, metric, current)
        if historical is not None:
            yield _finding(
                f"daily_baseline_{metric}", "info", "medium", f"A medição de {label} do dia está acima da faixa recente.",
                {f"today.{metric}": current, f"history.{metric}_median": historical},
                ("Mudanças de modelo, categoria de tarefa e horário podem explicar parte do desvio.",),
                ("compare_daily_workload",), "Compare rotinas, modelos e tipos de tarefas com dias representativos.",
            )

    tool_counts = Counter()
    for row in rows:
        if row.get("lane") == "tool" and row.get("outcome") in ("ok", "error", "timeout"):
            tool_counts["ok" if row["outcome"] == "ok" else "error"] += _number(row.get("requests")) or 0
    tool_attempts = sum(tool_counts.values())
    tool_error_share = _ratio(tool_counts["error"], tool_attempts)
    if tool_attempts >= 10 and (tool_error_share or 0) >= 0.25:
        yield _finding(
            "tool_failure_pattern", "warning", "high", "Uma parcela significativa das ferramentas executadas falhou.",
            {"tools.executed_attempts": tool_attempts, "tools.failed_attempts": tool_counts["error"],
             "tools.failure_ratio": tool_error_share},
            ("Um conector indisponível ou entrada inadequada pode concentrar falhas.",),
            ("trace_failing_tool_families",), "Use os rastros existentes para isolar a ferramenta e ensaiar uma reprodução no perfil de teste.",
        )


def _native_diagnoses(health, inventory):
    native = {row.get("id") for row in health.get("findings", ())}
    logs = (health.get("logs") or {}).get("events") or {}
    dependency_errors = _number(logs.get("plugin_dependency_error")) or 0
    contract_errors = _number(logs.get("plugin_contract_error")) or 0
    if dependency_errors or contract_errors:
        yield _finding(
            "plugin_load_or_hook_contract", "warning", "medium",
            "O log indica incompatibilidade de importação ou do contrato de hooks em um plugin.",
            {"logs.plugin_dependency_error": dependency_errors, "logs.plugin_contract_error": contract_errors},
            ("A causa pode ser a versão do plugin ou o ambiente Python usado pelo serviço.",),
            ("reproduce_plugin_in_disposable_profile",),
            "Execute o doctor nesse build do Hermes e reproduza a importação e o hook em perfil descartável.",
        )
    access_errors = _number(logs.get("storage_access")) or 0
    if access_errors >= 2:
        yield _finding(
            "filesystem_access_errors", "warning", "medium", "Permissões ou montagem somente leitura podem bloquear gravações.",
            {"logs.storage_access_errors": access_errors},
            ("A mensagem também pode originar de uma ferramenta acessando outro volume ou serviço.",),
            ("review_service_user_and_mount",), "Compare o usuário e a montagem efetiva com o destino da operação que falhou.",
        )
    logs = (health.get("logs") or {}).get("events") or {}
    state = (health.get("database") or {}).get("status")
    if state == "integrity_error":
        yield _finding(
            "native_state_integrity", "critical", "high", "A verificação do SQLite retornou falha de integridade.",
            {"state.integrity_error": True}, ("A leitura pode ter sido feita de um sistema de arquivos defeituoso.",),
            ("pause_optional_context", "verify_state_offline"), "Preserve uma cópia consistente e verifique a integridade fora da carga do gateway.",
        )
    if "gateway_version_mismatch" in native:
        yield _finding(
            "gateway_runtime_revision_drift", "warning", "high", "O processo confirmado do gateway reporta versão diferente da CLI.",
            {"gateway.version_mismatch": True}, ("Atualização em andamento ou launcher de outro ambiente.",),
            ("review_gateway_runtime_revision",), "Compare o serviço supervisionado e o interpretador do gateway com a instalação usada pela CLI.",
        )
    if state == "unexpected_schema":
        mismatch = "gateway_version_mismatch" in native
        yield _finding(
            "native_state_schema_issue", "warning", "medium",
            "Um descompasso de versão é compatível com o esquema inesperado." if mismatch
            else "O esquema observado difere da tabela de estado esperada para o Hermes.",
            {"state.unexpected_schema": True, "gateway.version_mismatch": True if mismatch else None},
            ("O perfil pode apontar para um banco diferente, incompleto ou em migração.",),
            ("verify_profile_schema_and_runtime",), "Confira perfil, instalação e migrações nativas antes de restaurar ou gravar estado.",
        )
    plugin_faults = _number(logs.get("plugin_failure")) or 0
    if plugin_faults >= 2:
        yield _finding(
            "plugin_fault_pattern", "warning", "medium", "Falhas recentes de plugins ou MCP merecem reprodução isolada.",
            {"logs.plugin_or_mcp_errors": plugin_faults,
             "gateway.version_mismatch": True if "gateway_version_mismatch" in native else None},
            ("Dependência, configuração, conector remoto ou incompatibilidade podem produzir o mesmo sinal.",),
            ("validate_plugins_in_test_profile",), "Reproduza a falha no perfil de teste e valide a versão e as dependências do plugin.",
        )
    memory_faults = _number(logs.get("memory_failure")) or 0
    if memory_faults >= 2:
        yield _finding(
            "native_memory_pipeline_fault", "warning", "medium", "Há erros recentes na operação da memória ou na recuperação.",
            {"logs.memory_failure": memory_faults, "logs.plugin_or_mcp_errors": plugin_faults,
             "state.integrity_error": True if state == "integrity_error" else None},
            ("Um provedor externo indisponível, permissões ou dados incompatíveis podem produzir sinais parecidos.",),
            ("test_native_memory_provider",), "Use uma nota sintética no perfil de teste para conferir gravação e recuperação no provedor ativo.",
        )

    descriptions = {
        "duplicate_skills_exact": ("Skills com conteúdo idêntico foram encontradas em mais de uma origem.", "review_duplicate_skills",
                                   "Determine qual origem é efetivamente selecionada, preservando cópias de referência.", "high"),
        "duplicate_skill_names": ("Skills com o mesmo nome podem ter sobreposição funcional ou precedência ambígua.", "compare_skill_variants",
                                  "Compare conteúdo e precedência no perfil, usando exemplos que exercitam cada variante.", "medium"),
        "large_disabled_plugins": ("Plugins desativados ocupam espaço no inventário amostrado.", "review_disabled_plugin_footprint",
                                   "Confirme dependências, retenção e escopo dos consumidores antes de arquivar pelo processo do operador.", "high"),
        "native_memory_redundancy": ("A estrutura observada da memória nativa contém repetições.", "review_saved_memory_structure",
                                     "Confirme se a repetição atravessa arquivos ou categorias e compare o recall antes de editar.", "high"),
        "native_memory_review": ("A estrutura dos arquivos de memória nativa apresenta um sinal que merece revisão.", "review_saved_memory_structure",
                                 "Revise formato, tamanho e retenção no provedor ativo e teste exemplos de recuperação.", "medium"),
    }
    for observation in inventory.get("findings", ()):
        entry = descriptions.get(observation.get("id"))
        if entry is None:
            continue
        hypothesis, action, recommendation, confidence = entry
        facts = observation.get("evidence") or {}
        facts = {f"inventory.{key}": number for key, value in facts.items()
                 if isinstance(key, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,47}", key)
                 and (number := _number(value)) is not None} if isinstance(facts, dict) else {}
        facts["inventory.observed"] = True
        alternatives = ("Uma cópia pode ser intencional ou atender outro perfil mesmo sem tráfego no inventário.",)
        if action == "review_saved_memory_structure":
            alternatives = ("Entradas parecidas podem ter origem, escopo ou propósito diferentes.",)
        yield _finding(observation["id"], "info", confidence, hypothesis, facts, alternatives, (action,), recommendation)


def diagnose(health: dict, usage: dict, *, host: dict | None = None, inventory: dict | None = None,
             telemetry: dict | None = None, history: dict | None = None) -> list[dict]:
    """Return explainable candidates with graded evidence. Confidence is ordinal, not causal probability."""
    telemetry = telemetry or {}
    possible_contention = [telemetry.get("contention"), *(telemetry.get("contention_candidates") or ())]
    contention = {candidate["kind"]: candidate for candidate in possible_contention
                  if isinstance(candidate, dict) and candidate.get("kind") in ("cpu", "io", "memory")
                  and candidate.get("attributed") is True}
    findings = list(_host_diagnoses(health, host or {}, contention))
    findings += _request_diagnoses(usage, telemetry.get("research") or {}, history or {},
                                   telemetry.get("current_daily_baseline") or {})
    findings += _native_diagnoses(health, inventory or {})
    severity = {"critical": 0, "warning": 1, "info": 2}
    confidence = {"high": 0, "medium": 1, "low": 2}
    return sorted(findings, key=lambda item: (severity[item["severity"]], confidence[item["confidence"]], item["code"]))
