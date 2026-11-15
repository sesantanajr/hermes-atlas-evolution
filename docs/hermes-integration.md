# Atlas Evolution e as interfaces do Hermes

A integração usa a árvore do Hermes Agent 0.23.1 como referência. A tabela mapeia o que o Hermes já oferece, o tipo de evidência que o Atlas acrescenta e a permissão usada na versão 0.3.0. [Versão oficial](https://github.com/NousResearch/hermes-agent/releases/tag/v0.23.1) · [API pública de plugins](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/developer-guide/plugins/public-api.md)

| Área | Superfície do Hermes | Uso pelo Atlas |
|---|---|---|
| Pesquisa | `web_search`, `web_extract` e a skill de citações fundamentadas | Acompanha turnos com ferramentas web reconhecidas, propõe orçamento e define o que verifica uma alegação. [Web search](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-search) · [Grounded citations](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/user-guide/skills/bundled/research/research-grounded-citations.md) |
| Delegação | `delegate_task` e Kanban | Mede desfechos de subagentes. Propõe delegação síncrona para trabalho ligado ao turno e um cartão bloqueado para verificação durável. [Kanban](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/user-guide/features/kanban.md) |
| Rotinas | Cron, entrega local e execução `no_agent` | Detecta disparos concentrados e falhas de entrega. Gera uma receita para executar o supervisor com saída apenas quando houver alerta ou transição. [Cron sem agente](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/guides/cron-script-only.md) |
| Memória e skills | Provedor nativo, compactação e Curator | Observa o comportamento de compactação, inventaria metadados e evidencia redundâncias para revisão. O Curator continua responsável pelo ciclo de vida de skills gerenciadas. [Curator](https://hermes-agent.nousresearch.com/docs/user-guide/features/curator) · [Observer hooks](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/developer-guide/observer-hooks.md) |
| Custos | Hooks do provedor e contabilidade de uso do Hermes | Distingue tokens de entrada, cache, saída, custos medidos e estimativas. Compara o comportamento com uma linha de base do próprio perfil. [Observer hooks](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/developer-guide/observer-hooks.md) |
| Recursos | PSI, cgroup v2 e systemd | Junta pressão e comportamento do serviço com a saúde do perfil. Um ensaio em CPU, I/O ou `MemoryHigh` exige configuração explícita, verificação e restauração. [Linux PSI](https://docs.kernel.org/accounting/psi.html) · [cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html) · [systemd](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html) |

## Permissões

| Configuração | Efeito |
|---|---|
| Instalação padrão | Observadores com dados agregados. Os comandos de operador podem inspecionar os sinais do perfil. |
| `agent_advice` | Libera `atlas_evolution_plan` como ferramenta de consulta. Cada chamada retorna etapas, orçamento e critérios de verificação. Em uma lista explícita de toolsets, inclua `atlas-evolution`. |
| `inspect_shared_kanban` | Inclui os quadros da raiz compartilhada que o Hermes informa ao processo. Os relatórios exibem contagens. |
| `kanban_handoff.enabled` | Autoriza `ops handoff` com quadro e perfil previamente escolhidos. O cartão começa bloqueado e aguarda revisão do operador. |
| `linux_supervisor.mode: apply` | Autoriza ensaios temporários na unidade auxiliar configurada, com as permissões que o processo já possui no systemd. |

## Onde a próxima validação terá maior valor

1. Compare a duração e a qualidade de pesquisas reais antes e depois dos planos. Use o mesmo conjunto de perguntas, valide as fontes e registre onde o orçamento deixou uma lacuna material.
2. Cruze falhas dos workers, sobreposição dos horários e volume de fila. Faça uma mudança por vez e acompanhe latência, taxa de erro e acúmulo de trabalho.
3. Monte um conjunto controlado de cenários Linux com pressão de CPU, I/O e memória. Verifique o valor efetivo do controlador, o impacto no Hermes e o rollback com o agente e a unidade auxiliar em execução.
4. Reaproveite o resultado de verificações e do Curator como lições operacionais revisadas. Compare o efeito de recomendações em perfis distintos antes de compartilhar políticas.
