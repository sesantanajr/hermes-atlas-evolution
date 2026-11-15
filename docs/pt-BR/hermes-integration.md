# Atlas Evolution e as interfaces do Hermes

[English](../hermes-integration.md) | **Português (Brasil)**

A integração usa a árvore do Hermes Agent 0.23.1 como referência (commit `3b421578588243f920886e6fe79a6faa87ea026e`). Este documento lista o que o plugin registra no Hermes, o que ele lê de um perfil, qual configuração libera cada capacidade e onde a próxima validação tem mais valor. [Versão oficial](https://github.com/NousResearch/hermes-agent/releases/tag/v0.23.1) · [API pública de plugins](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/developer-guide/plugins/public-api.md)

O plugin usa apenas a API de plugins publicada: os métodos de `ctx` listados abaixo e os módulos `hermes_constants`, `hermes_cli.config` e `agent.redact`. O import privado de versão que existia nas versões anteriores foi removido na 0.4.0.

## O que o plugin registra

| Superfície | API de plugins | Registrada quando |
|---|---|---|
| 14 hooks, listados abaixo | `ctx.register_hook` | sempre, exceto `transform_tool_result` |
| Seção de prompt `atlas-evolution.playbook`, com até 1.400 caracteres | `ctx.register_system_prompt_section` | `evolution.lessons` é `suggest` ou `auto` |
| Ferramentas `atlas_evolution_plan` e `atlas_evolution_lessons`, toolset `atlas-evolution` | `ctx.register_tool` | sempre; as duas ficam indisponíveis para o agente até `agent_advice` ser `true` |
| Skill `hermes-evolution:evolution-review` | `ctx.register_skill` | quando o host oferece o método |
| Comando de CLI `hermes evolution` | `ctx.register_cli_command` | só para plugins em processo; o Hermes o ignora com `plugins.isolation: host` |
| Comando de barra `/evolution`, para as notas da 0.1 | `ctx.register_command` | `legacy_memory_commands` é `true` |
| Rotina de encerramento, que grava os contadores em fila e fecha o banco | `ctx.on_unload` | quando o host oferece o método |

## Hooks

O manifesto declara os mesmos 14 hooks, e um teste compara as duas listas.

| Hook | O que o plugin faz com ele |
|---|---|
| `post_api_request` | Conta a chamada, os tokens e a duração, por provedor e modelo. |
| `api_request_error` | Conta cada tentativa que falhou, classificada como limite de taxa, autenticação, tempo esgotado, falha do provedor (`upstream`) ou erro. |
| `post_auxiliary_call` | Conta chamadas auxiliares e falhas, por tarefa. |
| `post_tool_call` | Conta o desfecho da ferramenta, classifica a falha em uma assinatura e cronometra os turnos que usam ferramentas de pesquisa web. |
| `transform_tool_result` | Acrescenta a lição correspondente ao resultado que falhou. Só é registrado com as lições ligadas e `tool_hints` em `true`. |
| `pre_llm_call` | Inicia o cronômetro do turno e classifica em memória o feedback do dono. Só devolve contexto para a recuperação de notas da 0.1, que é opcional. |
| `post_llm_call` | Fecha a cronometragem de um turno de pesquisa. |
| `on_session_end` | Registra o desfecho e a duração do turno. |
| `on_skill_lifecycle` | Conta carregamentos, alterações e outros eventos do ciclo de vida das skills. |
| `subagent_start`, `subagent_stop` | Contam as delegações, os desfechos e a duração. |
| `post_compaction` | Registra os tokens antes e depois de uma compactação, quando o Hermes informa. |
| `post_curator_run` | Conta as execuções do Curator, simuladas ou reais. |
| `on_kanban_worker_exited` | Conta as saídas de workers, por tipo. |

Todo callback falha de forma aberta: um hook nunca levanta erro dentro de um turno, e uma falha é registrada em log no máximo uma vez por tipo de evento. As gravações dos hooks passam por uma conexão de banco por processo e por perfil. Enquanto o banco está ocupado, a atualização espera em uma fila de até 512 entradas e é gravada junto com o evento seguinte. O que estoura a fila é contado e aparece como `dropped_events`.

Na máquina de desenvolvimento, um evento de hook levou cerca de 0,1 ms depois dessa mudança, contra cerca de 6 ms antes dela, quando o desenho anterior ainda perdia 155 de 800 eventos com 8 threads concorrentes. São medições de desenvolvimento em uma única máquina, não uma garantia.

## O que o plugin lê

Tudo é lido sem escrita, com limite de tamanho e de tempo.

| Fonte | Para quê |
|---|---|
| `skills/.usage.json`, `skills/.curator_ledger.jsonl` | Saúde das skills e mudanças de skill acompanhadas. Só metadados e hashes; o corpo das skills nunca é aberto. |
| `logs/agent.log` | A linha de conclusão de cada revisão em segundo plano. O final dos logs do Hermes também é varrido em busca de categorias de erro. |
| `state.db` | Checagem de integridade e contabilidade de uso do próprio Hermes. |
| `cron/jobs.json`, quadros Kanban | Diagnóstico de carga, como contagens. |
| `gateway_state.json`, `gateway.pid` | Estado do gateway, inclusive de um perfil nomeado atendido pelo gateway raiz. |
| `memories/MEMORY.md`, `memories/USER.md` | Presença, tamanho, legibilidade em UTF-8 e contagens de estrutura, como entradas, duplicatas e entradas acima do limite. O conteúdo não é gravado nem relatado. |
| Configuração do Hermes | Uma lista de permissão de chaves para acompanhar mudanças, e os limites de Cron, Kanban e delegação. |
| `/proc`, `/sys/fs/cgroup`, propriedades de unidades systemd | Medições de pressão e de serviços no Linux. |

## Como se encaixa nos recursos do Hermes

| Área | Superfície do Hermes | Uso pelo Atlas Evolution |
|---|---|---|
| Auto-melhoria | Revisão em segundo plano, skills e ledger do Curator | Mede o que o ciclo nativo deixa em aberto: se uma reescrita foi seguida de métricas melhores ou piores, quais skills são reescritas em círculos e quanto as revisões custam. O Curator continua responsável pelo ciclo de vida das skills gerenciadas. [Curator](https://hermes-agent.nousresearch.com/docs/user-guide/features/curator) |
| Falhas de ferramenta | `post_tool_call`, `transform_tool_result`, seções do prompt de sistema | Transforma falhas recorrentes em lições e as entrega sem tocar no prompt em cache. [Observer hooks](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/developer-guide/observer-hooks.md) |
| Pesquisa | `web_search`, `web_extract` e a skill de citações fundamentadas | Acompanha turnos com ferramentas web reconhecidas, propõe orçamento e define o que verifica uma alegação. [Web search](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-search) · [Grounded citations](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/user-guide/skills/bundled/research/research-grounded-citations.md) |
| Delegação | `delegate_task` e Kanban | Mede desfechos de subagentes. Propõe delegação síncrona para trabalho ligado ao turno e um cartão bloqueado para verificação durável. [Kanban](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/user-guide/features/kanban.md) |
| Rotinas | Cron, entrega local e execução `no_agent` | Detecta disparos concentrados, falhas de entrega e rotinas que não podem ser agendadas. Gera receitas para o tick do supervisor e para o resumo semanal. [Cron sem agente](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/guides/cron-script-only.md) |
| Memória | Provedor nativo e compactação | Observa o comportamento da compactação e inventaria a estrutura dos arquivos de memória nativa. Nunca escreve na memória e nunca guarda o conteúdo dela. |
| Custos | Hooks do provedor e contabilidade de uso do Hermes | Distingue tokens de entrada, de cache e de saída, custos medidos e estimativas. Compara o comportamento com uma linha de base do próprio perfil. |
| Recursos | PSI, cgroup v2 e systemd | Junta pressão e comportamento do serviço com a saúde do perfil. Um ensaio em CPU, I/O ou `MemoryHigh` exige configuração explícita, verificação e restauração. [Linux PSI](https://docs.kernel.org/accounting/psi.html) · [cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html) · [systemd](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html) |

## Permissões

| Configuração | Efeito |
|---|---|
| Instalação padrão | Observadores com dados agregados. Falhas recorrentes viram lições candidatas, que aguardam aprovação. Os comandos de operador podem inspecionar os sinais do perfil. |
| `evolution.lessons: "auto"` | As lições derivadas ficam ativas sem aprovação e chegam ao agente nas sessões novas. |
| `evolution.lessons: "off"` | Nenhuma lição é derivada e nenhuma superfície de lição é registrada. |
| `agent_advice` | Libera `atlas_evolution_plan` e `atlas_evolution_lessons`. A primeira devolve etapas, orçamento e critérios de verificação. A segunda consulta lições, informa o estado da fila e guarda lições propostas em quarentena. Em uma lista explícita de toolsets, inclua `atlas-evolution`. |
| `inspect_shared_kanban` | Inclui os quadros da raiz compartilhada que o Hermes informa ao processo. Os relatórios exibem contagens. |
| `kanban_handoff.enabled` | Autoriza `ops handoff` com quadro e perfil previamente escolhidos. O cartão começa bloqueado e aguarda revisão do operador. |
| `linux_supervisor.mode: apply` | Autoriza ensaios temporários na unidade auxiliar configurada, com as permissões que o processo já possui no systemd. |

Nenhuma configuração permite que o plugin trave, arquive ou reverta uma skill, nem que altere a configuração do Hermes. Isso continua sendo comando do operador.

## Notas de instalação

- **Pelo catálogo.** `hermes plugins install sesantanajr/hermes-atlas-evolution --enable` no perfil escolhido; `hermes -p NOME` seleciona o perfil. Confira com `hermes plugins doctor --ci hermes-evolution`.
- **Manualmente.** Copie a pasta inteira do plugin, com `plugin.yaml` na raiz, para `<HERMES_HOME>/plugins/hermes-evolution/` e execute `hermes plugins enable hermes-evolution`. O padrão em Linux é `~/.hermes`. Instalações Windows geralmente usam `%LOCALAPPDATA%\hermes`. Use o procedimento de manutenção do perfil caso o gateway já esteja em execução.
- **Isolamento em host.** Com `plugins.isolation: host`, o Hermes ignora `register_cli_command`; use `python -m hermes_evolution --home <perfil> ...` a partir da pasta do plugin. Veja [Operações](operations.md#fora-do-hermes).

## Avançado: deixar a revisão nativa propor lições

O Hermes pode admitir uma ferramenta nomeada na revisão em segundo plano por meio de `auxiliary.background_review.extra_tools`. Incluir `atlas_evolution_lessons` permite que a revisão registre o que aprendeu como proposta de lição no playbook, ao lado das gravações habituais em skills e memória.

```yaml
# config.yaml do perfil
auxiliary:
  background_review:
    extra_tools:
      - atlas_evolution_lessons
```

- Essa é uma configuração do operador no Hermes. O plugin nunca a define.
- A ferramenta precisa estar disponível para o agente principal, então isso também exige `"agent_advice": true` em `<HERMES_HOME>/evolution/settings.json`.
- A lição proposta por esse caminho fica em quarentena e só chega ao agente depois que você aprova. A ferramenta aceita no máximo 5 propostas por hora.
- Uma proposta aprovada aparece na seção de prompt como qualquer outra lição. Ela não está ligada a uma assinatura de falha, por isso não recebe veredito individual de antes e depois. A ativação dela ainda conta como mudança concorrente nos demais vereditos.
- Essa combinação não foi exercitada em um gateway de produção.

## Onde a próxima validação terá maior valor

1. Rode o ciclo em um gateway de produção por algumas semanas e compare os vereditos das lições com uma revisão manual das mesmas falhas.
2. Compare a duração e a qualidade de pesquisas reais antes e depois dos planos. Use o mesmo conjunto de perguntas, valide as fontes e registre onde o orçamento deixou uma lacuna material.
3. Cruze falhas dos workers, sobreposição dos horários e volume de fila. Faça uma mudança por vez e acompanhe latência, taxa de erro e acúmulo de trabalho.
4. Monte um conjunto controlado de cenários Linux com pressão de CPU, I/O e memória. Verifique o valor efetivo do controlador, o impacto no Hermes e o rollback com o agente e a unidade auxiliar em execução.
5. Reaproveite o resultado de verificações e do Curator como lições operacionais revisadas. Compare o efeito de recomendações em perfis distintos antes de compartilhar políticas.
