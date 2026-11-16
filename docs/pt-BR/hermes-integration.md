# Atlas Evolution e as interfaces do Hermes

[English](../hermes-integration.md) | **Português (Brasil)**

A integração usa como referência a árvore do Hermes Agent 0.23.1 (commit `3b421578588243f920886e6fe79a6faa87ea026e`). Este documento lista o que o plugin registra no Hermes, o que ele lê de um perfil, qual configuração libera cada recurso, o que ele não faz e onde uma validação adicional é mais útil. [Release oficial](https://github.com/NousResearch/hermes-agent/releases/tag/v0.23.1) · [API pública de plugins](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/developer-guide/plugins/public-api.md)

O plugin se apoia na API publicada de plugins: usa os métodos de `ctx` abaixo e só importa os módulos do Hermes `hermes_constants`, `hermes_cli.config`, `agent.redact` (`redact_sensitive_text`) e `agent.secret_scope`, todos listados na API pública de plugins do Hermes. Cada método opcional de `ctx` é consultado antes do uso, então um host que não o tenha ainda carrega o plugin, sem aquele recurso. Duas leituras olham módulos que o Hermes já carregou no processo, sem importá-los: a guarda de runtime pega a localização do pacote `hermes_cli` para saber onde está o código do Hermes, e o radar de saúde lê a lista de plugins do gerenciador de plugins do Hermes (`hermes_cli.plugins`), que não faz parte da API publicada de plugins. Quando esse módulo não está carregado, o radar pula essa leitura.

## O que o plugin registra

| Superfície | API de plugins | Registrada quando |
|---|---|---|
| 17 hooks, listados abaixo | `ctx.register_hook` | sempre, exceto `transform_tool_result` (lições ligadas e `tool_hints` ligado) e `pre_tool_call` (`runtime_guard` ligado) |
| Seção de prompt `hermes-evolution.playbook`, até 1.400 caracteres | `ctx.register_system_prompt_section` | `lessons` é `suggest` ou `auto` |
| Ferramentas `hermes_evolution_plan` e `hermes_evolution_lessons`, conjunto `hermes-evolution` | `ctx.register_tool` com `check_fn` | sempre; as duas ficam indisponíveis para o agente até `agent_tools` ser ligado |
| Skill `hermes-evolution:evolution-review` | `ctx.register_skill` | quando o host oferece o método |
| Variável `ATLAS_EVOLUTION_AGENT=1` em todo subprocesso de uma sessão do agente | `ctx.register_session_env` | quando o host oferece o método |
| Tarefa auxiliar `evolution_reflector`, herdando de `compression` | `ctx.register_auxiliary_task` e, depois, `ctx.llm.complete_structured` | `reflector` está ligado quando o plugin carrega e o host oferece os dois |
| Formulário de configurações com 11 opções | `config_schema` no `plugin.yaml` | sempre; o Hermes grava os valores em `plugins.entries.hermes-evolution.settings` |
| Comando de CLI `hermes evolution` | `ctx.register_cli_command` | só em plugins no mesmo processo; o Hermes o ignora com `plugins.isolation: host` |
| Comando de chat `/evolution` para as notas da 0.1 | `ctx.register_command` | `legacy_memory_commands` é `true` |
| Callback de descarga que grava os contadores na fila e fecha o banco | `ctx.on_unload` | quando o host oferece o método |
| Thread em segundo plano `hermes-evolution-autopilot`, um daemon que roda a passada | nenhuma; iniciada pelos hooks `post_llm_call`, `on_session_end` e `on_kanban_dispatch_tick` | `autopilot_minutes` não é 0 |

Os hooks, a seção de prompt, a guarda de runtime, o intervalo do autopilot e a tarefa do refletor são definidos quando o plugin carrega, então reinicie o gateway depois de mudar essas configurações. Cada passada do autopilot e cada comando leem as configurações de novo.

## Hooks

O manifesto declara os mesmos 17 hooks, e um teste compara as duas listas.

| Hook | O que o plugin faz com ele |
|---|---|
| `pre_tool_call` | A [guarda de runtime](#a-guarda-de-runtime): barra os comandos de terminal do agente que parariam o gateway ou apagariam aquilo de que o Hermes em execução precisa. Só é registrado com `runtime_guard` ligado. |
| `post_api_request` | Conta a chamada, os tokens e a duração por provedor e modelo. |
| `api_request_error` | Conta cada tentativa que falhou, classificada como limite de taxa, autenticação, timeout, upstream ou erro. |
| `post_auxiliary_call` | Conta chamadas auxiliares e falhas por tarefa. |
| `post_tool_call` | Conta o desfecho da ferramenta, classifica uma falha numa assinatura, cronometra turnos que usam ferramentas de pesquisa web e, com o refletor ligado, guarda um trecho limpo de uma falha sem modelo. |
| `transform_tool_result` | Acrescenta a lição correspondente a um resultado que falhou. Só é registrado com as lições ligadas e `tool_hints` em `true`. |
| `pre_llm_call` | Inicia o cronômetro do turno, classifica em memória os sinais do dono e cobra uma correção do dono das skills do turno anterior. Só devolve contexto para a recuperação opcional das notas da 0.1. |
| `post_llm_call` | Fecha a medição de um turno de pesquisa e dá um empurrão no autopilot. |
| `on_session_end` | Registra o desfecho e a duração do turno e o desfecho de cada skill que ele carregou, e dá um empurrão no autopilot. |
| `on_skill_lifecycle` | Conta carregamentos, alterações e outros eventos do ciclo de vida das skills, e liga uma skill carregada ao seu turno. |
| `subagent_start`, `subagent_stop` | Contam delegações, desfechos e duração. |
| `post_compaction` | Registra tokens antes e depois de uma compactação quando o Hermes os informa. |
| `post_curator_run` | Conta execuções do Curator, simuladas ou reais. |
| `on_kanban_worker_exited` | Conta saídas de workers por tipo. |
| `on_session_finalize` | Devolve no máximo um aviso curto por dia UTC sobre decisões pendentes, só para uma sessão das superfícies locais do dono (CLI, TUI, Desktop) que não seja de subagente. O Hermes mostra o aviso fora da conversa. |
| `on_kanban_dispatch_tick` | Dá um empurrão no autopilot. O Hermes o dispara uma vez por tick do despachante do Kanban, a cada 60 segundos no padrão, só onde o despachante roda dentro do gateway (`kanban.dispatch_in_gateway`). |

Um empurrão só compara dois relógios; a passada em si roda na thread do autopilot (veja [O autopilot](evolution-loop.md#o-autopilot)). Todo callback falha de forma aberta: um hook nunca levanta erro dentro de um turno, e uma falha é registrada no log no máximo uma vez por tipo de evento. As gravações dos hooks passam por uma conexão de banco por processo e perfil. Enquanto o banco está ocupado, uma atualização espera numa fila de até 512 entradas e é gravada junto com o próximo evento ou quando o plugin é descarregado. O excedente é contado e informado como `dropped_events`. A seção de prompt só lê o banco e conta a sua exibição pela mesma fila.

Na máquina de desenvolvimento, um evento de hook levou cerca de 0,1 ms com esse desenho, contra cerca de 6 ms antes, quando o desenho anterior também perdia 155 de 800 eventos com 8 threads concorrentes. São medições de desenvolvimento numa única máquina, não uma garantia.

## A guarda de runtime

Com `runtime_guard` ligado (o padrão), o plugin registra um callback de `pre_tool_call` (`guard`). O Hermes o chama antes de cada chamada de ferramenta do agente, e uma diretiva de bloqueio substitui a chamada: a mensagem dela vira o resultado da ferramenta que o agente lê. A guarda só olha o argumento `command` de ferramentas do tipo terminal (`terminal`, `shell`, `bash`, `run_command`, `execute_command`) e barra dois tipos de comando.

| Tipo | Barrado | O que o agente recebe |
|---|---|---|
| `gateway_stop` | `systemctl` com `restart`, `stop`, `kill`, `try-restart`, `reload-or-restart`, `try-reload-or-restart`, `condrestart`, `force-reload` ou `disable --now` numa unidade `hermes-gateway` (inclusive uma instância ou o sufixo `.service`); `service` com `restart` ou `stop` nessa unidade; `pkill` ou `killall` com um argumento que contenha hermes; `hermes gateway stop`, que deixaria o Hermes parado | o comando cortaria os turnos em andamento, inclusive este; use `hermes gateway restart`, que espera os turnos em andamento terminarem; um reinício forçado fica para o dono rodar no próprio terminal |
| `runtime_removal` | `rm`, `rmdir`, `unlink`, `shred`, `mv` (as origens) ou `find` com `-delete` ou com `-exec` rodando `rm`, apontados para um caminho protegido, para uma pasta que contenha um deles ou com um glob que case com um deles | o comando apagaria ou moveria esse caminho, e o Hermes pararia de funcionar até alguém consertar à mão; remover isso fica para o dono, no próprio terminal, com o Hermes parado |

Os caminhos protegidos são aqueles de que o Hermes em execução precisa: os prefixos do interpretador Python em execução e do ambiente dele, o código do Hermes (a pasta que contém o pacote `hermes_cli`), a raiz do Hermes e este perfil. Um caminho com menos de três componentes, como `/usr`, nunca entra na lista, então um interpretador do sistema cujo prefixo é `/usr` não fica coberto. Arquivos dentro de um caminho protegido também não ficam cobertos; só o próprio caminho e as pastas acima dele.

Como um comando é lido:

- Ele é dividido em `;`, `&&`, `||`, `|`, `&`, parênteses e quebras de linha, e em cada parte só conta a palavra do comando, depois de atribuições de variáveis e de invólucros como `sudo`, `env`, `nohup`, `timeout`, `nice`, `ionice`, `stdbuf`, `exec`, `command`, `builtin`, `time`, `chrt` e `doas`. Um comando citado nos argumentos de outro, como num `echo`, não é um comando.
- Um comando passado a um shell com `-c` (`bash`, `sh`, `zsh`, `dash`, `ksh`) é lido um nível adentro. Um comando executado por outro programa, como `ssh`, ou a partir de um arquivo de script, não é examinado.
- O corpo de um here-document é dado gravado em algum lugar, não um comando que roda agora, então é ignorado.
- Um caminho relativo à pasta pessoal é expandido com a pasta pessoal. Um caminho relativo à pasta de trabalho não é checado.
- Qualquer dúvida deixa o comando passar: uma aspa sem par, um comando com mais de 20.000 caracteres, uma ferramenta que não é do tipo terminal.

A checagem é uma função pura sobre o comando: não lê nem grava nada, e o tamanho da entrada é limitado. O callback nunca levanta erro, porque o Hermes trata como bloqueio um `pre_tool_call` que falha ou estoura o tempo. Cada bloqueio é contado por tipo na `guard_daily` (o comando não é gravado), e o `hermes evolution status` e o resumo mostram as contagens. A mensagem segue o idioma do plugin. O terminal do próprio operador não é afetado.

Essa guarda existe porque, num servidor real, o agente reiniciou o próprio gateway com `systemctl` 13 vezes numa semana, 12 delas a partir de um único chat num único dia, cortando os turnos em andamento, e uma vez apagou o runtime Python que a unidade do gateway usava, o que deixou o Hermes fora do ar por cerca de uma hora. Ela é uma rede de proteção para esses dois casos, não uma sandbox: a política de aprovação do próprio Hermes para comandos de terminal continua valendo.

## O refletor e o acesso a modelos do Hermes

O refletor é a única parte do plugin que envolve um modelo, e vem ligado. Quando `reflector` está ligado no carregamento, o plugin registra a tarefa auxiliar `evolution_reflector` com `inherit_from="compression"`: ela parte da configuração efetiva da tarefa de compressão, e você pode dar a ela provedor e modelo próprios em `auxiliary.evolution_reflector`, na configuração do perfil. Uma passada feita dentro do Hermes (pelo autopilot, pelo `hermes evolution learn` ou pelo `ops tick`) então chama `ctx.llm.complete_structured` com essa tarefa, um esquema JSON, no máximo 300 tokens de saída e prazo de 60 segundos. O Hermes resolve o provedor, envia a requisição e a contabiliza; o plugin não abre conexão, e nenhuma transação do plugin fica aberta durante a chamada. Quando o host não oferece acesso a modelos para plugins, ou enquanto `lessons` está em `off`, o ciclo roda sem rascunhos. Três chamadas reais pelo Hermes em produção (provedor DeepSeek, cerca de 600 tokens e cerca de 2 segundos cada) geraram três rascunhos em português. O que é enviado e o que é aceito de volta está descrito em [O ciclo de evolução](evolution-loop.md#o-refletor).

## O que o plugin lê

Tudo em modo somente leitura e com limite de tamanho e de tempo.

| Origem | Uso |
|---|---|
| `skills/.usage.json`, `skills/.curator_ledger.jsonl` | Saúde das skills e mudanças acompanhadas. Só metadados e hashes; o conteúdo das skills nunca é aberto. |
| `logs/agent.log` e, na primeira execução, do `agent.log.1` ao `agent.log.5` | A linha de conclusão de cada revisão em segundo plano. O final dos logs do Hermes também é varrido em busca de categorias de erro. Links são ignorados. |
| `state.db` | Checagem de integridade e a contabilidade de uso do próprio Hermes. A leitura do histórico: sessões e mensagens dos 14 dias anteriores ao início da observação, de 25 em 25 sessões, classificadas em memória e descartadas. O radar: como terminaram as sessões dos últimos 7 dias e os tokens de prompt por chamada do agente, da `session_model_usage`. |
| `state.db` dos outros perfis da mesma raiz do Hermes | Para o `status`: o número de sessões nos últimos 7 dias de cada perfil que não tem a pasta `plugins/hermes-evolution`. Nada mais é lido. |
| O gerenciador de plugins do Hermes, quando está carregado no processo | Para o radar: os plugins habilitados, o número de hooks que cada um registrou e o nome de um erro de carregamento. |
| `plugin.yaml` dos plugins distribuídos com o Hermes e de `<HERMES_HOME>/plugins/` | Para o radar: os hooks que cada plugin declara (`provides_hooks`). No máximo 500 pastas e 64 KiB por manifesto. |
| `cron/jobs.json`, quadros Kanban | Diagnóstico de carga de trabalho, em contagens. |
| `gateway_state.json`, `gateway.pid` | Estado do gateway, inclusive de um perfil nomeado servido pelo gateway da raiz. |
| `memories/MEMORY.md`, `memories/USER.md` | Presença, tamanho, leitura em UTF-8 e contagens de estrutura, como entradas, duplicatas e entradas grandes demais. O conteúdo não é gravado nem relatado. |
| Configuração do Hermes | A entrada do próprio plugin em `plugins.entries.hermes-evolution.settings`, `display.language`, uma lista de permissão de configurações para acompanhar mudanças, e os limites de Cron, Kanban, delegação e memória. |
| `HERMES_LANGUAGE` | O idioma do perfil, lido por `agent.secret_scope` dentro do Hermes ou do ambiente fora dele. |
| `/proc`, `/sys/fs/cgroup`, propriedades de unidades systemd | Medições de pressão do Linux e de serviços. |

## Como se encaixa nos recursos do Hermes

| Área | Superfície do Hermes | Uso pelo Atlas Evolution |
|---|---|---|
| Auto-melhoria | Revisão em segundo plano, skills e ledger do Curator | Mede o que o ciclo nativo deixa em aberto: se uma reescrita foi seguida de turnos melhores ou piores para aquela skill, quais skills são reescritas em círculos e quanto as revisões custam. Regressões são propostas como o rollback do próprio Curator. [Curator](https://hermes-agent.nousresearch.com/docs/user-guide/features/curator) |
| Falhas de ferramenta | `post_tool_call`, `transform_tool_result`, seções do prompt de sistema | Transforma falhas recorrentes em lições e as entrega sem tocar no prompt em cache. [Observer hooks](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/developer-guide/observer-hooks.md) |
| Aprovações | A barreira de aprovação do Hermes | Uma aprovação recusada é registrada como `approval_refused` e nunca vira lição, então o plugin nunca ensina o agente a contornar uma decisão do usuário. |
| Acesso a modelos | Tarefas auxiliares e `ctx.llm` | O refletor usa a própria tarefa auxiliar, então o modelo e o custo dele ficam visíveis e configuráveis como os de qualquer outra tarefa auxiliar. |
| Configurações | `config_schema` e as configurações de plugins do Desktop | As opções principais são editadas no mesmo lugar dos outros plugins. |
| Ciclo de vida do gateway | `pre_tool_call`, `hermes gateway restart` | A guarda de runtime barra os comandos do agente que reiniciariam, parariam ou derrubariam o gateway de dentro de um turno e indica ao agente o reinício do próprio Hermes, que espera os turnos em andamento. |
| Histórico de sessões | `state.db`, `on_session_finalize` | A leitura do histórico aprende com os 14 dias anteriores à instalação. O radar aponta sessões que terminam como órfãs, contexto pesado por chamada e compactações fracas. Um aviso curto ao dono aparece no fim de uma sessão local. |
| Plugins | Gerenciador de plugins e manifestos | O radar aponta plugins que falham ao carregar, registram hooks que não declaram ou disputam um hook de transformação em que só o primeiro resultado vale. |
| Pesquisa | `web_search`, `web_extract` e a skill de citações fundamentadas | Acompanha turnos com ferramentas web reconhecidas, propõe orçamento e define o que verifica uma alegação. [Web search](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-search) · [Grounded citations](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/user-guide/skills/bundled/research/research-grounded-citations.md) |
| Delegação | `delegate_task` e Kanban | Mede desfechos de subagentes. Propõe delegação síncrona para trabalho ligado ao turno e um cartão bloqueado para verificação durável. [Kanban](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/user-guide/features/kanban.md) |
| Rotinas | Cron, entrega local e execução `no_agent` | Detecta disparos concentrados, falhas de entrega e rotinas que não podem ser agendadas. Gera receitas para o tick do supervisor e para o resumo semanal. [Cron sem agente](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/guides/cron-script-only.md) |
| Memória | Provedor nativo e compactação | Observa o comportamento da compactação e inventaria a estrutura dos arquivos de memória nativa. Nunca escreve na memória e nunca guarda o conteúdo dela. |
| Custos | Hooks do provedor e contabilidade de uso do Hermes | Distingue tokens de entrada, de cache e de saída, custos medidos e estimativas. Compara o comportamento com uma linha de base do próprio perfil. |
| Recursos | PSI, cgroup v2 e systemd | Junta pressão e comportamento do serviço com a saúde do perfil. Um ensaio em CPU, I/O ou `MemoryHigh` exige configuração explícita, verificação e restauração. [Linux PSI](https://docs.kernel.org/accounting/psi.html) · [cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html) · [systemd](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html) |

## Permissões

| Configuração | Efeito |
|---|---|
| Instalação padrão | Observadores com dados agregados. O autopilot roda uma passada no máximo uma vez por hora, e as primeiras passadas aprendem com 14 dias do histórico do perfil. Falhas recorrentes viram lições candidatas, que aguardam aprovação. O refletor rascunha lições que também aguardam aprovação. A guarda de runtime barra os comandos descritos acima. Os comandos de operador podem inspecionar os sinais do perfil. |
| `lessons: auto` | As lições derivadas ficam ativas sem aprovação e chegam ao agente nas sessões novas. Os rascunhos do refletor continuam esperando. |
| `lessons: off` | Nenhuma lição é derivada e nenhuma superfície de lição é registrada. |
| `agent_tools` (`agent_advice` no `settings.json`) | Libera `hermes_evolution_plan` e `hermes_evolution_lessons`. A primeira devolve etapas, orçamento e critérios de verificação. A segunda consulta lições, informa o estado da fila e guarda lições propostas em quarentena. Numa lista explícita de toolsets, inclua `hermes-evolution`. |
| `reflector` (ligado por padrão) | Guarda trechos limpos de falhas sem modelo e deixa uma passada pedir a um modelo, pelo Hermes, rascunhos em quarentena, dentro de `reflector_daily_limit` chamadas por dia. `false` o desliga. |
| `autopilot_minutes` (60 por padrão) | Intervalo da passada em segundo plano. `0` deixa as passadas para o `learn`, o `ops tick` e a receita semanal. |
| `runtime_guard` (ligado por padrão) | Registra o bloqueio no `pre_tool_call`. `false` o remove. |
| `inspect_shared_kanban` | Inclui os quadros da raiz compartilhada que o Hermes informa ao processo. Os relatórios exibem contagens. |
| `kanban_handoff.enabled` | Autoriza `ops handoff` com quadro e perfil escolhidos de antemão. O cartão começa bloqueado e aguarda revisão do operador. |
| `linux_supervisor.mode: apply` | Autoriza ensaios temporários na unidade auxiliar configurada, com as permissões que o processo já tem no systemd. |

## O que o plugin não faz no Hermes

- Não trava, arquiva, reverte, edita nem cria skills. Isso aparece como comandos `hermes curator` para você.
- Não altera a configuração do Hermes. Uma proposta que alteraria aparece como comando `hermes config`.
- Não escreve na memória nem lê o conteúdo dela além das contagens de estrutura acima.
- Não reescreve um prompt já em uso, não injeta contexto a cada turno (exceto a recuperação opcional das notas da 0.1) e não muda argumentos de ferramentas. As únicas alterações em conteúdo visível ao agente são a frase acrescentada ao resultado de uma ferramenta que falhou e, quando a guarda de runtime barra um comando, a mensagem fixa de recusa no lugar do resultado.
- Não para, não reinicia e não derruba nada. A guarda de runtime só recusa um comando; ela nunca executa um.
- Não grava no histórico do Hermes: o `state.db` e os logs são abertos somente para leitura.
- Não muda nada em outros perfis. O `status` só conta as sessões deles e imprime o comando de instalação.
- Não aprova, nega nem contorna aprovações do Hermes, e nunca transforma uma aprovação recusada em conselho.
- Não registra middleware, provedor, provedor de memória, padrão de redação nem pacote de idioma, e não substitui nada do núcleo do Hermes.
- Não cria rotinas de Cron. As receitas são impressas para você criar. O único cartão Kanban que ele cria é o cartão bloqueado do `ops handoff`, que exige `kanban_handoff`.
- Não abre conexão de rede nem envia telemetria. Um modelo só é chamado pelo refletor, por meio do Hermes.
- Não deixa o agente decidir: as decisões do playbook se recusam a rodar num terminal do agente.

## Notas de instalação

- **Pelo GitHub.** `hermes plugins install sesantanajr/hermes-atlas-evolution --enable` no perfil escolhido; `hermes -p NOME` seleciona o perfil. Confira com `hermes plugins doctor --ci hermes-evolution`.
- **Em todo perfil que você usa.** O Hermes carrega os plugins por perfil, mesmo quando um único gateway atende vários perfis, então instale o plugin em cada um: `hermes -p <perfil> plugins install sesantanajr/hermes-atlas-evolution --enable`. O `hermes evolution status` lista os perfis movimentados da mesma raiz do Hermes que não o têm.
- **Manualmente.** Copie a pasta inteira do plugin, com o `plugin.yaml` na raiz, para `<HERMES_HOME>/plugins/hermes-evolution/` e execute `hermes plugins enable hermes-evolution`. O padrão no Linux é `~/.hermes`. Instalações Windows geralmente usam `%LOCALAPPDATA%\hermes`. Use o procedimento de manutenção do perfil caso o gateway já esteja em execução.
- **Isolamento em host.** Com `plugins.isolation: host`, o Hermes ignora `register_cli_command`; use `python -m hermes_evolution --home <perfil> ...` a partir da pasta do plugin. O refletor não roda nesse executor independente; os trechos em espera ficam para a próxima passada feita pelo Hermes. O autopilot e o refletor não foram verificados com isolamento em host. Veja [Operações](operations.md#fora-do-hermes).

## Avançado: deixar a revisão nativa propor lições

O Hermes pode admitir uma ferramenta nomeada na revisão em segundo plano por meio de `auxiliary.background_review.extra_tools`. Incluir `hermes_evolution_lessons` permite que a revisão registre o que aprendeu como proposta de lição no playbook, ao lado das gravações habituais em skills e memória.

```yaml
# config.yaml do perfil
auxiliary:
  background_review:
    extra_tools:
      - hermes_evolution_lessons
```

- Essa é uma configuração do operador no Hermes. O plugin nunca a define.
- A ferramenta precisa estar disponível para o agente principal, então isso também exige `agent_tools` ligado.
- Uma lição proposta por esse caminho tem origem `agent`: fica em quarentena, a proposta tem risco alto e ela só chega ao agente depois que você aprova. A ferramenta aceita no máximo 5 propostas por hora.
- Uma proposta aprovada aparece na seção de prompt como qualquer outra lição. Ela não está ligada a uma assinatura de falha, por isso não recebe veredito individual de antes e depois. A ativação dela ainda conta como mudança simultânea nos demais vereditos.
- Essa combinação não foi exercitada num gateway de produção.

## Onde a próxima validação tem mais valor

1. Rode o ciclo num gateway de produção por algumas semanas e compare os vereditos das lições e as skills marcadas pelo resultado com uma revisão manual dos mesmos turnos.
2. Acompanhe o autopilot num gateway de longa duração por vários dias: as passadas que o `status` mostra, a duração delas e a latência dos hooks enquanto uma passada roda.
3. Revise os rascunhos do refletor contra as falhas de origem, além das três chamadas reais feitas até agora, e escolha um modelo de baixo custo para `auxiliary.evolution_reflector`.
4. Confira os bloqueios da guarda de runtime e as propostas do radar contra o que de fato aconteceu no host, para achar falsos positivos.
5. Compare a duração e a qualidade de pesquisas reais antes e depois dos planos. Use o mesmo conjunto de perguntas, valide as fontes e registre onde o orçamento deixou uma lacuna material.
6. Cruze falhas dos workers, sobreposição de horários e volume de fila. Faça uma mudança por vez e acompanhe latência, taxa de erro e acúmulo de trabalho.
7. Monte um conjunto controlado de cenários Linux com pressão de CPU, I/O e memória. Verifique o valor efetivo do controlador, o impacto no Hermes e o rollback com o agente e a unidade auxiliar em execução.
