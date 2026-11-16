# Atlas Evolution e as interfaces do Hermes

[English](../hermes-integration.md) | **Português (Brasil)**

A integração usa como referência a árvore do Hermes Agent 0.23.1 (commit `3b421578588243f920886e6fe79a6faa87ea026e`). Este documento lista o que o plugin registra no Hermes, o que ele lê de um perfil, qual configuração libera cada recurso, o que ele não faz e onde uma validação adicional é mais útil. [Release oficial](https://github.com/NousResearch/hermes-agent/releases/tag/v0.23.1) · [API pública de plugins](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/developer-guide/plugins/public-api.md)

O plugin se apoia na API publicada de plugins: usa os métodos de `ctx` abaixo e só importa os módulos do Hermes `hermes_constants`, `hermes_cli.config`, `agent.redact` (`redact_sensitive_text`) e `agent.secret_scope`, todos listados na API pública de plugins do Hermes. Cada método opcional de `ctx` é consultado antes do uso, então um host que não o tenha ainda carrega o plugin, sem aquele recurso. Cinco leituras olham módulos que o Hermes já carregou no processo, sem importá-los, e nenhuma delas faz parte da API publicada de plugins: a guarda de runtime pega a localização do pacote `hermes_cli` para saber onde está o código do Hermes; o radar de saúde lê a lista de plugins do gerenciador de plugins do Hermes (`hermes_cli.plugins`); o comando `hermes evolution` lê a versão do pacote `hermes_cli` já carregado para a checagem de atualização; os hooks de turno perguntam ao `agent.session_source` a origem real da sessão em andamento (um worker do Kanban roda com plataforma `cli`); e o `status` pergunta ao `hermes_cli.plugin_isolation` se os plugins rodam num host de plugins. Quando um módulo não está carregado, aquela leitura é pulada e o plugin usa o que o payload do hook informa.

## O que o plugin registra

| Superfície | API de plugins | Registrada quando |
|---|---|---|
| 17 hooks, listados abaixo | `ctx.register_hook` | sempre, exceto `transform_tool_result`, que é registrado enquanto as lições estão ligadas e fica em silêncio a menos que `tool_hints` esteja ligado |
| Seção de prompt `hermes-atlas-evolution.playbook`, até 1.400 caracteres | `ctx.register_system_prompt_section` | `lessons` é `suggest` ou `auto` |
| Ferramentas `hermes_atlas_evolution_plan` e `hermes_atlas_evolution_lessons`, toolset `hermes-atlas-evolution` | `ctx.register_tool` com `check_fn` | sempre; as duas ficam indisponíveis para o agente até `agent_tools` ser ligado |
| Skill `hermes-atlas-evolution:evolution-review` | `ctx.register_skill` | quando o host oferece o método |
| Variável `ATLAS_EVOLUTION_AGENT=1` em todo subprocesso de uma sessão do agente | `ctx.register_session_env` | quando o host oferece o método |
| Tarefa auxiliar `evolution_reflector`, herdando de `compression` | `ctx.register_auxiliary_task` e, depois, `ctx.llm.complete_structured` | `reflector` está ligado quando o plugin carrega e o host oferece os dois |
| Formulário de configurações com 11 opções | `config_schema` no `plugin.yaml` | sempre; o Hermes grava os valores em `plugins.entries.hermes-atlas-evolution.settings` |
| Comando de CLI `hermes evolution` | `ctx.register_cli_command` | só em plugins no mesmo processo; o Hermes o ignora com `plugins.isolation: host` |
| Comando de chat `/evolution` para as notas da 0.1 | `ctx.register_command` | `legacy_memory_commands` é `true` |
| Callback de descarregamento, que grava os contadores que estavam na fila e fecha o banco | `ctx.on_unload` | quando o host oferece o método |
| Thread em segundo plano `hermes-atlas-evolution-autopilot`, um daemon que roda a passada | nenhuma; iniciada pelos hooks `post_llm_call`, `on_session_end` e `on_kanban_dispatch_tick`, e pelo relógio | `autopilot_minutes` não é 0 |
| Thread em segundo plano `hermes-atlas-evolution-clock`, um daemon que empurra a passada 2 minutos depois de o plugin carregar e depois a cada 10 minutos | nenhuma; parada por meio do `ctx.on_unload` | `autopilot_minutes` não é 0 e o host oferece `on_unload` |

Os hooks, a seção de prompt, a guarda de runtime, o intervalo do autopilot e a tarefa do refletor são definidos quando o plugin carrega, então reinicie o gateway depois de mudar essas configurações. Cada passada do autopilot e cada comando leem as configurações de novo.

**Duas cópias num perfil.** Quando o perfil ainda tem a cópia instalada com o nome usado até a 0.5.0, `hermes-evolution`, e ela está ligada, o plugin não registra nada e avisa no log com o comando que remove a cópia antiga (`hermes plugins remove hermes-evolution`): as duas cópias observariam os mesmos turnos e gravariam no mesmo banco.

**A versão que um processo carregou.** Um processo de gateway ou de dashboard que carrega o plugin registra, no banco do plugin, a versão do plugin, o tipo do processo e quando ele começou, identificados pelo ID do processo, para que o `status` aponte um que ainda roda código antigo ([Status e aviso ao dono](evolution-loop.md#status-e-aviso-ao-dono)).

## Hooks

O manifesto declara os mesmos 17 hooks, e um teste compara as duas listas.

| Hook | O que o plugin faz com ele |
|---|---|
| `pre_tool_call` | Guarda, só em memória, um hash do comando de cada chamada do tipo terminal, para saber depois se outro plugin o reescreveu. Com `runtime_guard` ligado, roda a [guarda de runtime](#a-guarda-de-runtime): barra os comandos de terminal do agente que parariam o gateway ou apagariam aquilo de que o Hermes em execução precisa. Não é registrado onde o Hermes roda os plugins num host de plugins (veja a guarda). |
| `post_api_request` | Conta a chamada, os tokens e a duração por provedor e modelo. |
| `api_request_error` | Conta cada tentativa que falhou, classificada como limite de taxa, autenticação, timeout, upstream ou erro. |
| `post_auxiliary_call` | Conta chamadas auxiliares e falhas por tarefa. |
| `post_tool_call` | Conta o desfecho da ferramenta e a duração dela, classifica uma falha numa assinatura (como `rewritten_command` quando o `middleware_trace` do Hermes aponta uma alteração ou o comando que rodou difere do que o `pre_tool_call` viu), conta uma aprovação recusada por origem da sessão, conta a falha de novo quando o turno atendia o dono (veja [Quem é atendido](evolution-loop.md#quem-é-atendido)), cronometra turnos que usam ferramentas de pesquisa web e, com o refletor ligado, guarda um trecho mascarado de uma falha sem modelo fixo dos turnos do próprio dono. |
| `transform_tool_result` | Acrescenta a lição correspondente a um resultado que falhou quando `tool_hints` está em `true`, o que vem desligado: o Hermes só mantém a alteração do primeiro plugin num resultado de ferramenta. Numa sessão que não atende o dono, só entram lições de modelo fixo. Registrado enquanto as lições estão ligadas, e em silêncio fora disso. |
| `pre_llm_call` | Descobre a superfície do turno e se ele atende o dono, inicia o cronômetro do turno, classifica em memória os sinais do dono e atribui uma correção do dono às skills do turno anterior. Só devolve contexto para a recuperação opcional das notas da 0.1. |
| `post_llm_call` | Fecha a medição de um turno de pesquisa e dá um empurrão no autopilot. |
| `on_session_end` | Registra o desfecho e a duração do turno e o desfecho de cada skill que ele carregou, e dá um empurrão no autopilot. |
| `on_skill_lifecycle` | Conta carregamentos, alterações e outros eventos do ciclo de vida das skills, e liga uma skill carregada ao seu turno. |
| `subagent_start`, `subagent_stop` | Contam delegações, desfechos e duração. |
| `post_compaction` | Registra tokens antes e depois de uma compactação quando o Hermes os informa. |
| `post_curator_run` | Conta execuções do Curator, simuladas ou reais. |
| `on_kanban_worker_exited` | Conta saídas de workers por tipo. |
| `on_session_finalize` | Devolve no máximo um aviso curto por dia UTC sobre decisões pendentes, só para uma sessão das superfícies locais do dono (CLI, TUI, Desktop) que não seja de subagente. O Hermes 0.23.1 descarta esse texto nas sessões locais que ele hospeda quando o visualizador fecha ou a sessão é recolhida (`cli_close`, `tui_close`, `new_session`, os recolhimentos), então, nesses casos, o plugin não devolve nada e o aviso do dia não é gasto; as decisões pendentes aparecem no `status` e no resumo. |
| `on_kanban_dispatch_tick` | Dá um empurrão no autopilot. O Hermes o dispara uma vez por tick do despachante do Kanban, a cada 60 segundos, por padrão, para o perfil do próprio gateway, e só onde o despachante roda dentro do gateway (`kanban.dispatch_in_gateway`). A thread do relógio cobre os outros perfis que um gateway atende. |

Um empurrão só compara dois relógios; a passada em si roda na thread do autopilot (veja [O autopilot](evolution-loop.md#o-autopilot)). Nenhum callback bloqueia o turno quando falha: um hook nunca lança erro dentro de um turno, e uma falha é registrada no log no máximo uma vez por tipo de evento. As gravações dos hooks passam por uma conexão de banco por processo e perfil. Enquanto o banco está ocupado, uma atualização espera numa fila de até 512 entradas e é gravada junto com o próximo evento ou quando o plugin é descarregado. O excedente é contado e informado como `dropped_events`. A seção de prompt só lê o banco e conta a sua exibição pela mesma fila.

Na máquina de desenvolvimento, um evento de hook levou cerca de 0,1 ms com esse desenho, contra cerca de 6 ms antes, quando o desenho anterior também perdia 155 de 800 eventos com 8 threads concorrentes. São medições de desenvolvimento numa única máquina, não uma garantia.

## A guarda de runtime

Com `runtime_guard` ligado (o padrão), o callback de `pre_tool_call` do plugin roda a guarda (`guard`). O Hermes o chama antes de cada chamada de ferramenta do agente, e uma diretiva de bloqueio substitui a chamada: a mensagem dela vira o resultado da ferramenta que o agente lê. A guarda só olha o argumento `command` de ferramentas do tipo terminal (`terminal`, `shell`, `bash`, `run_command`, `execute_command`) e barra dois tipos de comando.

| Tipo | Barrado | O que o agente recebe |
|---|---|---|
| `gateway_stop` | Linux: `systemctl` com `restart`, `stop`, `kill`, `try-restart`, `reload-or-restart`, `try-reload-or-restart`, `condrestart`, `force-reload` ou `disable --now` numa unidade `hermes-gateway`, inclusive a `hermes-gateway-<perfil>` de um perfil, a `hermes-gateway-<hash>` de uma pasta própria, uma instância e o sufixo `.service`; `service` com `restart` ou `stop` nessa unidade. macOS: `launchctl` com `kickstart -k`, `stop`, `bootout`, `unload`, `remove` ou `kill` no LaunchAgent `ai.hermes.gateway` ou `ai.hermes.gateway-<sufixo>`, citado sozinho, como `gui/<uid>/<rótulo>` ou pelo plist. Windows: `sc` ou `net` com `stop`, os cmdlets do PowerShell `Stop-Service`, `Restart-Service`, `Suspend-Service` e `Stop-ScheduledTask`, e `schtasks /End`, no serviço ou tarefa `Hermes_Gateway` ou `Hermes_Gateway_<sufixo>`. Em qualquer sistema: `pkill`, `killall`, `taskkill` ou `Stop-Process` com um argumento que contenha hermes; `hermes gateway stop` e `hermes gateway restart` | o comando cortaria os turnos em andamento, inclusive este; não tente de outra forma; diga ao dono que é preciso reiniciar e por quê; o dono roda `hermes gateway restart` no próprio terminal, e esse comando mostra os turnos ainda em andamento e pergunta antes de interrompê-los |
| `runtime_removal` | `rm`, `rmdir`, `unlink`, `shred`, `mv` (as origens) e, no Windows, `del`, `erase`, `rd`, `Remove-Item` e `Move-Item`, ou `find` com `-delete` ou com `-exec` rodando `rm`, apontados para um caminho protegido, para uma pasta que contenha um deles ou com um glob que case com um deles | o comando apagaria ou moveria esse caminho, e o Hermes pararia de funcionar até alguém consertar à mão; remover isso fica para o dono, no próprio terminal, com o Hermes parado |

Os caminhos protegidos são aqueles de que o Hermes em execução precisa: os prefixos do interpretador Python em execução e do ambiente dele, o interpretador base para o qual um ambiente virtual aponta (a linha `home` do `pyvenv.cfg` e o `sys._base_executable`), o código do Hermes (a pasta que contém o pacote `hermes_cli`), a raiz do Hermes e este perfil. Cada um é protegido tanto como está escrito quanto resolvido, então um link conta pelos dois nomes: o link `cpython-3.11-...` do uv para `cpython-3.11.16-...`, ou um `~/.hermes` que aponta para outro disco. Um caminho com menos de três componentes, como `/usr`, nunca entra na lista, então um interpretador do sistema cujo prefixo é `/usr` não fica coberto. Arquivos dentro de um caminho protegido também não ficam cobertos; só o próprio caminho e as pastas acima dele.

Como um comando é lido:

- Ele é dividido em `;`, `&&`, `||`, `|`, `&`, parênteses e quebras de linha, e em cada parte só conta a palavra do comando, depois de atribuições de variáveis e de invólucros como `sudo`, `env`, `nohup`, `timeout`, `nice`, `ionice`, `stdbuf`, `exec`, `command`, `builtin`, `time`, `chrt`, `doas`, `setsid`, `systemd-run` com as opções dele, `runuser`, `watch`, `flock`, `xargs` e `rtk run` (ou `rtk err` e os outros subcomandos do `rtk` que rodam um comando). Um comando citado nos argumentos de outro, como num `echo`, não é um comando.
- Um comando passado como um único texto é lido um nível para dentro quando vai para um shell (`bash`, `sh`, `zsh`, `dash`, `ksh`) com `-c` ou um grupo de opções que o contenha, como `-lc` ou `-ec`, para `su`, `runuser`, `script` ou `flock` com `-c` ou `--command` (também `script -qc`), para o PowerShell (`powershell`, `pwsh`) com `-Command` ou `-c` e para o `cmd` com `/c`. Um comando executado por outro programa, como `ssh`, ou a partir de um arquivo de script, não é examinado.
- O corpo de um here-document é texto que vai ser gravado em algum lugar, não um comando que roda agora, então é ignorado.
- `~`, `$HOME` e `$HERMES_HOME` (este perfil) são expandidos no começo de um caminho, assim como `%USERPROFILE%`, `%LOCALAPPDATA%`, `%APPDATA%` e as formas `$NOME` e `$env:NOME` deles, quando estão definidos. No Windows, `C:\x`, `C:/x` e o `/c/x` do Git Bash são o mesmo caminho, comparado sem diferenciar maiúsculas. Um caminho relativo à pasta de trabalho, ou que começa com outra variável, não é checado.
- Qualquer dúvida deixa o comando passar: uma aspa sem par, um comando com mais de 20.000 caracteres, uma ferramenta que não é do tipo terminal.

A checagem é uma função pura sobre o comando: não lê nem grava nada, e o tamanho da entrada é limitado. O callback nunca lança erro, porque o Hermes trata como bloqueio um `pre_tool_call` que falha ou estoura o tempo. Cada bloqueio é contado por tipo na `guard_daily` (o comando não é gravado), e o `hermes evolution status` e o resumo mostram as contagens. A mensagem segue o idioma do plugin. O terminal do próprio operador não é afetado.

Essa guarda existe porque, num servidor real, o agente reiniciou o próprio gateway com `systemctl` 13 vezes numa semana, 12 delas a partir de um único chat num único dia, cortando os turnos em andamento, e uma vez apagou o runtime Python que a unidade do gateway usava, o que deixou o Hermes fora do ar por cerca de uma hora. O mesmo histórico também tinha um reinício agendado por meio do `systemd-run`, e por isso a guarda olha o comando que está por trás dos invólucros (wrappers) acima. Desde a 0.6.0, o `hermes gateway restart` do agente também é barrado: sem terminal, o Hermes 0.23.1 se recusa a reiniciar enquanto há sessões rodando, e o `--yes` dele as interromperia, então o reinício fica com o dono. A guarda é uma rede de proteção para esses casos, não uma sandbox: a política de aprovação do próprio Hermes para comandos de terminal continua valendo.

**Host de plugins.** Onde o Hermes roda os plugins num processo separado (`plugins.isolation: host`), o plugin não registra nenhum `pre_tool_call`, então nem a guarda nem a checagem de comando reescrito rodam ali. Cada `pre_tool_call` vira uma chamada ao host, e o Hermes bloqueia uma ferramenta cujo `pre_tool_call` falha: um host que caísse bloquearia todas as ferramentas do perfil. O `status` diz que a guarda está desligada por esse motivo.

## O refletor e o acesso a modelos do Hermes

O refletor é a única parte do plugin que envolve um modelo de IA, e vem ligado. Quando `reflector` está ligado no carregamento, o plugin registra a tarefa auxiliar `evolution_reflector` com `inherit_from="compression"`: ela parte da configuração efetiva da tarefa de compressão, e você pode dar a ela provedor e modelo próprios em `auxiliary.evolution_reflector`, na configuração do perfil. Uma passada feita dentro do Hermes (pelo autopilot, pelo `hermes evolution learn` ou pelo `ops tick`) então chama `ctx.llm.complete_structured` com essa tarefa, um esquema JSON, no máximo 300 tokens de saída e prazo de 60 segundos. O Hermes resolve o provedor, envia a requisição e a contabiliza; o plugin não abre conexão, e nenhuma transação do plugin fica aberta durante a chamada. Quando o host não oferece acesso a modelos para plugins, ou enquanto `lessons` está em `off`, o ciclo roda sem rascunhos. Três chamadas reais pelo Hermes em produção (provedor DeepSeek, cerca de 600 tokens e cerca de 2 segundos cada) geraram três rascunhos em português. O que é enviado e o que é aceito de volta está descrito em [O ciclo de evolução](evolution-loop.md#o-refletor).

## O que o plugin lê

Tudo em modo somente leitura e com limite de tamanho e de tempo.

| Origem | Uso |
|---|---|
| `skills/.usage.json`, `skills/.curator_ledger.jsonl` | Saúde das skills e mudanças acompanhadas. Só metadados e hashes; o ciclo de evolução nunca abre o conteúdo de uma skill. |
| Arquivos `SKILL.md` das pastas de skills e os primeiros 8 KiB do `__init__.py` de outros plugins | Só para o supervisor de operações (`ops inspect`, `ops tick`): os arquivos de skill são lidos, dentro de um limite total de bytes, para achar skills duplicadas, e só o hash e o título ficam; o começo do `__init__.py` de um plugin diz se ele é um plugin de provedor. Nada disso é guardado como texto. |
| `logs/agent.log` e, na primeira execução, do `agent.log.1` ao `agent.log.5` | A linha de conclusão de cada revisão em segundo plano. O final dos logs do Hermes também é varrido em busca de categorias de erro. Links são ignorados. |
| `state.db` | Checagem de integridade e a contabilidade de uso do próprio Hermes. A leitura do histórico: sessões e mensagens dos 14 dias anteriores ao início da observação, de 25 em 25 sessões, classificadas em memória e descartadas. O radar: como terminaram as sessões dos últimos 7 dias e os tokens de prompt por chamada do agente, da `session_model_usage`. |
| `pyvenv.cfg` do ambiente Python em execução | A guarda de runtime: o interpretador base para o qual o ambiente aponta. |
| Os outros perfis da mesma raiz do Hermes | Para o `status`, até 200 perfis: o número de sessões nos últimos 7 dias, da tabela `sessions` de cada `state.db`, se a pasta `plugins/` tem o `hermes-atlas-evolution`, o antigo `hermes-evolution` ou os dois (a linha `name` de um `plugin.yaml` ali identifica um clone desta versão guardado com o nome antigo da pasta) e, onde o plugin tem dados, o fim da última passada e as contagens de lições, do `evolution/evolution.sqlite3`. Nada mais é lido, e nada é gravado lá. |
| `/proc/<pid>/stat` e `/proc/<pid>/cmdline` dos processos ancestrais do comando, `/proc/self/cgroup` | No Linux, só quando um comando de decisão (`lessons approve`, `retire`, `add`, `review`, `proposals approve`, `reject`) roda: se um processo do Hermes é ancestral ou se o comando roda no cgroup de um gateway do Hermes. Nada é gravado. |
| O gerenciador de plugins do Hermes, quando está carregado no processo | Para o radar: os plugins habilitados, o número de hooks que cada um registrou e o nome de um erro de carregamento. |
| `plugin.yaml` dos plugins distribuídos com o Hermes e de `<HERMES_HOME>/plugins/` | Para o radar: os hooks que cada plugin declara (`provides_hooks`). No máximo 500 pastas e 64 KiB por manifesto. |
| `cron/jobs.json`, quadros Kanban | Diagnóstico de carga de trabalho, em contagens; o `status` também confere se a rotina do resumo semanal existe. |
| A tabela `sessions` do `state.db` deste perfil: origem e remetente de cada sessão de chat | Para o `status`, as sessões e o número de remetentes de cada canal de chat em 7 dias (só contagens). Para o `hermes evolution owner`, rodado pelo operador no próprio terminal, os ids de remetente de cada canal em 30 dias com as contagens de sessão. Nada é gravado. |
| `gateway_state.json`, `gateway.pid` | Estado do gateway, inclusive de um perfil com nome servido pelo gateway da raiz. |
| `memories/MEMORY.md`, `memories/USER.md` | Presença, tamanho, leitura em UTF-8 e contagens de estrutura, como entradas, duplicatas e entradas grandes demais. O conteúdo não é gravado nem relatado. |
| Configuração do Hermes | A entrada do próprio plugin em `plugins.entries.hermes-atlas-evolution.settings`, `display.language`, uma lista de permissão de configurações para acompanhar mudanças, e os limites de Cron, Kanban, delegação e memória. |
| `HERMES_LANGUAGE` | O idioma do perfil, lido por `agent.secret_scope` dentro do Hermes ou do ambiente fora dele. |
| `/proc`, `/sys/fs/cgroup`, propriedades de unidades systemd | Medições de pressão do Linux e de serviços. |

## Como se encaixa nos recursos do Hermes

| Área | Superfície do Hermes | Uso pelo Atlas Evolution |
|---|---|---|
| Automelhoria | Revisão em segundo plano, skills e ledger do Curator | Mede o que o ciclo nativo deixa em aberto: se uma reescrita foi seguida de turnos melhores ou piores para aquela skill, quais skills são reescritas em círculos e quanto as revisões custam. Regressões são propostas como o rollback do próprio Curator. [Curator](https://hermes-agent.nousresearch.com/docs/user-guide/features/curator) |
| Falhas de ferramenta | `post_tool_call`, `transform_tool_result`, seções do prompt de sistema | Transforma falhas recorrentes em lições e as entrega sem tocar no prompt em cache. [Observer hooks](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/developer-guide/observer-hooks.md) |
| Aprovações | A barreira de aprovação do Hermes | Uma aprovação recusada é registrada como `approval_refused` e nunca vira lição, então o plugin nunca ensina o agente a contornar uma decisão do usuário. |
| Acesso a modelos | Tarefas auxiliares e `ctx.llm` | O refletor usa a própria tarefa auxiliar, então o modelo e o custo dele ficam visíveis e configuráveis como os de qualquer outra tarefa auxiliar. |
| Configurações | `config_schema` e as configurações de plugins do Desktop | As opções principais são editadas no mesmo lugar dos outros plugins. |
| Ciclo de vida do gateway | `pre_tool_call`, `hermes gateway restart` | A guarda de runtime barra os comandos do agente que reiniciariam, parariam ou derrubariam o gateway de dentro de um turno e orienta o agente a deixar o reinício com o dono, no Linux, no macOS e no Windows. O `hermes gateway restart` do dono mostra os turnos ainda em andamento e pergunta antes de interrompê-los. O `status` aponta um gateway que ainda roda código antigo do plugin depois de uma atualização. |
| Histórico de sessões | `state.db`, `on_session_finalize` | A leitura do histórico aprende com os 14 dias anteriores à instalação, contando cada mensagem uma vez, embora a compactação do Hermes guarde cópias dela. O radar aponta sessões que terminam como órfãs, contexto pesado por chamada e compactações fracas. Um aviso curto ao dono é devolvido no fim de uma sessão local, onde o Hermes o mostra. |
| Quem é atendido | `pre_llm_call` (plataforma, remetente), a origem da sessão, `owner_sender_ids` | Um turno atende o dono nas superfícies dele, nas automações e nos subagentes dele, ou num chat com um remetente listado em `owner_sender_ids`. Só esses turnos deixam uma lição derivada entrar em vigor sozinha, alimentam o refletor e chegam às ferramentas do agente; um chat com outra pessoa recebe só lições de modelo fixo. |
| Plugins | Gerenciador de plugins, manifestos e `pre_tool_call` | O radar aponta plugins que falham ao carregar, registram hooks que não declaram ou disputam um hook de transformação em que só o primeiro resultado vale, e falhas de ferramenta causadas por outro plugin: chamadas que o Hermes bloqueou depois que o `pre_tool_call` de um plugin travou ou deu erro, e comandos que falharam depois que um plugin os reescreveu. Essas nunca viram lição. |
| Perfis | Um gateway, vários perfis | Cada perfil carrega os próprios plugins. O `status` mostra todos os perfis da raiz do Hermes e o comando para cada um que não tem o plugin, e o relógio dá a cada perfil carregado as suas passadas, não só ao perfil do gateway. |
| Pesquisa | `web_search`, `web_extract` e a skill de citações fundamentadas | Acompanha turnos com ferramentas web reconhecidas, propõe orçamento e define o que verifica uma alegação. [Web search](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-search) · [Grounded citations](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/user-guide/skills/bundled/research/research-grounded-citations.md) |
| Delegação | `delegate_task` e Kanban | Mede desfechos de subagentes. Propõe delegação síncrona para trabalho ligado ao turno e um cartão bloqueado para verificação durável. [Kanban](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/user-guide/features/kanban.md) |
| Rotinas | Cron, entrega local e execução `no_agent` | Detecta disparos concentrados, falhas de entrega e rotinas que não podem ser agendadas. Gera receitas para o tick do supervisor e para o resumo semanal. [Cron sem agente](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/guides/cron-script-only.md) |
| Memória | Provedor nativo e compactação | Observa o comportamento da compactação e inventaria a estrutura dos arquivos de memória nativa. Nunca escreve na memória e nunca guarda o conteúdo dela. |
| Custos | Hooks do provedor e contabilidade de uso do Hermes | Distingue tokens de entrada, de cache e de saída, custos medidos e estimativas. Compara o comportamento com uma linha de base do próprio perfil. |
| Recursos | PSI, cgroup v2 e systemd | Junta pressão e comportamento do serviço com a saúde do perfil. Um ensaio em CPU, I/O ou `MemoryHigh` exige configuração explícita, verificação e restauração. [Linux PSI](https://docs.kernel.org/accounting/psi.html) · [cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html) · [systemd](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html) |

## Permissões

| Configuração | Efeito |
|---|---|
| Instalação padrão | Observadores com dados agregados. O autopilot roda uma passada no máximo uma vez por hora, e as primeiras passadas aprendem com 14 dias do histórico do perfil. Falhas recorrentes com modelo fixo viram lições ativas (`lessons: auto`) assim que os turnos do próprio dono as encontram duas vezes; elas chegam ao agente nas sessões novas, são medidas e são aposentadas quando não ajudam. Falhas que só outras pessoas causaram esperam aprovação. O refletor rascunha lições que aguardam aprovação. A guarda de runtime barra os comandos descritos acima. Os comandos de operador podem inspecionar os sinais do perfil. |
| `owner_sender_ids` | Os ids de remetente, em texto ou número, cujos turnos de chat contam como do dono, como numa sessão local. |
| `owner_platforms` | As plataformas em que todo chat conta como do dono, desde a primeira mensagem: só uma plataforma em que ninguém mais escreve, como um bot privado ou a própria interface do dono pelo servidor de API. |
| `lessons: suggest` | As lições derivadas viram candidatas e aguardam aprovação, como todas as outras. |
| `lessons: off` | Nenhuma lição é derivada e nenhuma superfície de lição é registrada. |
| `tool_hints` (desligado por padrão) | A lição correspondente também é acrescentada ao resultado de ferramenta que falhou, o que descarta a alteração que o `transform_tool_result` de outro plugin faria nesse resultado. |
| `agent_tools` (`agent_advice` no `settings.json`) | Libera `hermes_atlas_evolution_plan` e `hermes_atlas_evolution_lessons`, que só respondem numa sessão que atende o dono. A primeira devolve etapas, orçamento e critérios de verificação. A segunda consulta lições, informa o estado da fila e guarda lições propostas em quarentena. Numa lista explícita de toolsets, inclua `hermes-atlas-evolution`. |
| `reflector` (ligado por padrão) | Guarda trechos mascarados de falhas sem modelo fixo dos turnos do próprio dono e deixa uma passada pedir a um modelo de IA, pelo Hermes, rascunhos em quarentena, dentro de `reflector_daily_limit` chamadas por dia. `false` o desliga. |
| `autopilot_minutes` (60 por padrão) | Intervalo da passada em segundo plano. `0` deixa as passadas para o `learn`, o `ops tick`, a receita semanal e o primeiro `status`. |
| `runtime_guard` (ligado por padrão) | Registra o bloqueio no `pre_tool_call`, exceto num host de plugins. `false` o remove. |
| `inspect_shared_kanban` | Inclui os quadros da raiz compartilhada que o Hermes informa ao processo. Os relatórios exibem contagens. |
| `kanban_handoff.enabled` | Autoriza `ops handoff` com quadro e perfil escolhidos de antemão. O cartão começa bloqueado e aguarda revisão do operador. |
| `linux_supervisor.mode: apply` | Autoriza ensaios temporários na unidade auxiliar configurada, com as permissões que o processo já tem no systemd. |

## O que o plugin não faz no Hermes

- Não trava, arquiva, reverte, edita nem cria skills. Isso aparece como comandos `hermes curator` para você.
- Não altera a configuração do Hermes. Uma proposta que alteraria aparece como comando `hermes config`.
- Não escreve na memória nem lê o conteúdo dela além das contagens de estrutura acima.
- Não reescreve um prompt já em uso, não injeta contexto a cada turno (exceto a recuperação opcional das notas da 0.1) e não muda argumentos de ferramentas. As únicas alterações em conteúdo visível ao agente são a frase acrescentada ao resultado de uma ferramenta que falhou, com `tool_hints` ligado, e, quando a guarda de runtime barra um comando, a mensagem fixa de recusa no lugar do resultado.
- Não para, não reinicia e não derruba nada. A guarda de runtime só recusa um comando; ela nunca executa um.
- Não grava no histórico do Hermes: o `state.db` e os logs são abertos somente para leitura.
- Não muda nada em outros perfis. O `status` só lê as sessões deles e o estado do próprio plugin neles, e imprime o comando de instalação ou de migração para você rodar.
- Não aprova, nega nem contorna aprovações do Hermes, e nunca transforma uma aprovação recusada em conselho.
- Não registra middleware, provedor, provedor de memória, padrão de mascaramento de dados sensíveis nem pacote de idioma, e não substitui nada do núcleo do Hermes.
- Não cria rotinas de Cron. As receitas são impressas para você criar. O único cartão Kanban que ele cria é o cartão bloqueado do `ops handoff`, que exige `kanban_handoff`.
- Não abre conexão de rede nem envia telemetria. Um modelo só é chamado pelo refletor, por meio do Hermes.
- Não deixa o agente decidir: os comandos de decisão do playbook não rodam num terminal do agente.

## Notas de instalação

- **Pelo GitHub.** `hermes plugins install sesantanajr/hermes-atlas-evolution --enable` no perfil padrão, `hermes -p <perfil> plugins install sesantanajr/hermes-atlas-evolution --enable` num perfil com nome. Instalar com `--enable` faz um gateway em execução carregar o plugin na hora; a seção de prompt vale a partir da próxima sessão. Confira com `hermes plugins doctor --ci hermes-atlas-evolution` e `hermes evolution status`.
- **Em todo perfil que você usa.** O Hermes carrega os plugins por perfil, mesmo quando um único gateway atende vários perfis. Para todos os perfis com nome: `for p in $(ls ~/.hermes/profiles); do hermes -p "$p" plugins install sesantanajr/hermes-atlas-evolution --enable; done`, mais o comando do perfil padrão. O `hermes evolution status` lista todos os perfis da mesma raiz do Hermes, com o comando de instalação para cada perfil movimentado que não tem o plugin.
- **Atualização.** O `hermes plugins update hermes-atlas-evolution`, com `-p <perfil>` para cada perfil com nome, baixa o código novo, mas um gateway em execução continua com o código que carregou: o Hermes recarrega um gateway em execução na instalação, ao ligar e ao desligar um plugin, e numa atualização feita pelo Hermes Desktop, pelo dashboard ou pelo `/plugins update` num chat, não nessa atualização pela linha de comando. Termine com `hermes gateway restart`; se ainda houver algo rodando, ele lista o que é e pergunta, em inglês, `Interrupt them and restart the gateway? [y/N]`. O `status` aponta um processo de gateway ou de dashboard que ainda roda uma versão mais antiga, em sistemas POSIX.
- **Migração da 0.5.0.** Até a 0.5.0, o nome do plugin era `hermes-evolution`. Em cada perfil que o tem, rode `hermes plugins remove hermes-evolution` e depois `hermes plugins install sesantanajr/hermes-atlas-evolution --enable`, e não o `hermes plugins update`: o Hermes dá à pasta de um plugin o nome dele, então uma atualização não substituiria a cópia antiga. Remova primeiro: a instalação faz um gateway em execução procurar os plugins de novo, o que descarrega a cópia removida e carrega a nova; se você instalou primeiro, rode `hermes gateway restart` depois da remoção. Enquanto a cópia antiga está instalada e ligada, a nova não registra nada, nem o próprio comando, então o `hermes evolution` desse perfil continua sendo o da 0.5.0 (veja [O que o plugin registra](#o-que-o-plugin-registra)); o `status` rodado num perfil que já está na 0.6.0 marca esse perfil como tendo as duas cópias e imprime a correção. O `plugins remove` do Hermes também apaga o `plugins.entries.hermes-evolution` do `config.yaml` do perfil, então renomeie esse bloco para `hermes-atlas-evolution` antes, se você mudou opções no formulário de configurações. Os dados em `<HERMES_HOME>/evolution/` ficam. Onde uma lista explícita de toolsets ou o `auxiliary.background_review.extra_tools` citar `atlas-evolution` ou `atlas_evolution_lessons`, use `hermes-atlas-evolution` e `hermes_atlas_evolution_lessons`.
- **Manualmente.** Copie a pasta inteira do plugin, com o `plugin.yaml` na raiz, para `<HERMES_HOME>/plugins/hermes-atlas-evolution/` e execute `hermes plugins enable hermes-atlas-evolution`. A pasta padrão do Hermes no Linux é `~/.hermes`. Instalações Windows geralmente usam `%LOCALAPPDATA%\hermes`. Ao habilitar, um gateway em execução carrega o plugin; se o Hermes disser que nenhum gateway respondeu, rode `hermes gateway restart`.
- **Isolamento em host.** Com `plugins.isolation: host`, o Hermes ignora `register_cli_command`; use `python -m hermes_atlas_evolution --home <perfil> ...` a partir da pasta do plugin. A guarda de runtime fica desligada ali (veja [A guarda de runtime](#a-guarda-de-runtime)). O refletor não roda nesse executor independente; os trechos em espera ficam para a próxima passada feita pelo Hermes. O autopilot e o refletor não foram verificados com isolamento em host. Veja [Operações](operations.md#fora-do-hermes).

### Capa e o catálogo de plugins

O Hermes só mostra a capa de um plugin para os itens do catálogo curado de plugins dele, a pasta `plugin-catalog/` do repositório hermes-agent, revisada pela Nous Research ([envio ao catálogo](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission)). A `image` de um item precisa ser uma imagem https num host do GitHub, na proporção 2:1, fixada no commit do item. Instalado pelo GitHub, o Atlas Evolution aparece com um cartão genérico até o item dele no catálogo ser aceito. A capa dele é o [banner.jpg](../banner.jpg), de 1280×640.

## Compatibilidade e limites

Exige Hermes Agent 0.23.1 ou superior e Python 3.11 ou superior.

**Verificado na máquina de desenvolvimento** (medições, não garantias):

- Os testes unitários e de ponta a ponta contra um Hermes simulado, também com o Python do Hermes e a função nativa de mascaramento de segredos dele. A integração contínua roda os testes no Linux com Python 3.11 a 3.13 e em máquinas reais de macOS e Windows; os testes que são POSIX por desenho (bits de permissão, a checagem de processo vivo, runtimes com link simbólico) são pulados no Windows.
- `hermes plugins validate` do Hermes 0.23.1: sem avisos (2 ferramentas, 17 hooks), varredura de segurança `safe`, pronto para `plugins.isolation: host`.
- A leitura do histórico, numa cópia de um perfil real com 276 sessões: a 0.6.0 reprocessa 373 turnos e 3.566 chamadas de ferramentas, exatamente uma por mensagem lógica da janela (a 0.5.0 contava 6.731, cada cópia que a compactação do Hermes guarda, e 86 turnos interrompidos em vez de 23), em menos de um segundo. Das 152 falhas, 129 vieram dos turnos do próprio dono.
- O autopilot (uma reserva por perfil, reserva que expira, nova tentativa, fixação do perfil, um `learn` manual esperando a passada em andamento): verificado nos testes e na CLI. O relógio, a liberação de uma passada na saída do processo, a checagem de código antigo e a proteção contra duas cópias num perfil: verificados nos testes.
- Medido na 0.4.0 e não repetido desde então: cerca de 0,1 ms por evento de hook (antes, cerca de 6 ms, com perda de eventos sob concorrência); com um `state.db` de cerca de 170 MB, `learn` cerca de 0,3 s e 25 MB, `report` cerca de 0,35 s, `ops tick` cerca de 0,5 s.

**Verificado em produção:** três chamadas reais do refletor pelo Hermes (provedor DeepSeek, cerca de 600 tokens e cerca de 2 segundos cada) geraram três rascunhos em português.

**Não verificado:** vereditos de lições e skills pelo resultado ao longo de semanas de uso real (ainda não há resultados de campo), o relógio e a checagem de código antigo num gateway de longa duração que atende vários perfis, a migração da 0.5.0 num gateway em execução, o empurrão do Kanban no autopilot (que depende de `kanban.dispatch_in_gateway`), o autopilot e o refletor com `plugins.isolation: host`, ensaios no Linux com systemd real, outras versões do Hermes, e um gateway do Hermes rodando no macOS ou no Windows (os testes rodam lá, o gateway não).

**Limites**

- Os vereditos comparam dois períodos; são correlações, não experimentos controlados.
- Só as lições ligadas a uma falha (derivadas e do refletor) têm efeito medido.
- Um banco no esquema 8 (0.5.0 em diante) não abre na 0.4.0 ou anterior. Para voltar até lá, restaure um backup ou apague o `evolution.sqlite3`. A 0.6.0 mantém o esquema 8 e só acrescenta uma tabela, então a 0.5.0 ainda abre o banco dela; da 0.6.0 em diante, um banco num esquema mais novo também abre, porque as versões seguintes só acrescentam tabelas e colunas.
- O autopilot precisa de um empurrão: um turno, o relógio ou o tick do Kanban. Um host sem `on_unload` não tem relógio, então ali um perfil parado espera o próximo turno.
- A leitura do histórico só pega o que o Hermes registrou antes de o plugin começar a observar o perfil, até 14 dias para trás. Ela não reprocessa chamadas ao modelo nem tokens, mede as chamadas de ferramenta só de forma aproximada (do pedido ao resultado) e não reconhece um comando que outro plugin reescreveu; um binário que falhou ao vivo depois de uma reescrita fica fora das lições a partir daí.
- Com `plugins.isolation: host` não existe `hermes evolution`: use `python -m hermes_atlas_evolution --home <perfil> ...` (que só lê o `config.yaml` com o PyYAML). As receitas de cron dependem da CLI do Hermes; uma passada pelo executor independente não rascunha nada e mantém os trechos em espera para uma passada feita dentro do Hermes.
- A contagem de revisões depende de uma linha que o Hermes 0.23.1 grava no `agent.log`. Os sinais do dono são heurísticas sobre as primeiras palavras da mensagem.
- A guarda de runtime só olha a palavra do comando em cada parte de um comando de terminal, depois dos invólucros que ela conhece, e um nível dentro de um comando passado como um único texto. Um comando aninhado mais fundo, um executado via `ssh` ou a partir de um script, um comando que ela não consegue interpretar e um caminho relativo à pasta de trabalho passam. Ela é uma rede de proteção, não uma sandbox.
- A checagem de código antigo compara números de versão: um processo que carregou outro código com o mesmo número de versão não é apontado, e no Windows nenhum processo é apontado.
- A checagem de operador (que só deixa o dono decidir) lê a ancestralidade dos processos e os cgroups em `/proc`, então fora do Linux ela depende só dos marcadores do ambiente.
- Quem um turno de chat atende vem do `owner_sender_ids` e do `owner_platforms`. Com os dois vazios, todo chat de mensagens conta como de outra pessoa: as falhas dele continuam contadas, mas as lições que vêm delas esperam aprovação e o chat recebe só lições de modelo fixo. Uma plataforma listada em `owner_platforms` confia em todos que escrevem nela.

## Avançado: deixar a revisão nativa propor lições

O Hermes pode admitir uma ferramenta nomeada na revisão em segundo plano por meio de `auxiliary.background_review.extra_tools`. Incluir `hermes_atlas_evolution_lessons` permite que a revisão registre o que aprendeu como proposta de lição no playbook, ao lado das gravações habituais em skills e memória.

```yaml
# config.yaml do perfil
auxiliary:
  background_review:
    extra_tools:
      - hermes_atlas_evolution_lessons
```

- Essa é uma configuração do operador no Hermes. O plugin nunca a define.
- A ferramenta precisa estar disponível para o agente principal, então isso também exige `agent_tools` ligado.
- Uma lição proposta por esse caminho tem origem `agent`: fica em quarentena, a proposta tem risco alto e ela só chega ao agente depois que você aprova. A ferramenta aceita no máximo 5 propostas por hora.
- Uma proposta aprovada aparece na seção de prompt como qualquer outra lição. Ela não está ligada a uma assinatura de falha, por isso não recebe veredito individual de antes e depois. A ativação dela ainda conta como mudança simultânea nos demais vereditos.
- Essa combinação não foi testada num gateway de produção.

## Onde a próxima validação tem mais valor

1. Rode o ciclo num gateway de produção por algumas semanas e compare os vereditos das lições e as skills marcadas pelo resultado com uma revisão manual dos mesmos turnos.
2. Acompanhe o autopilot num gateway de longa duração que atende vários perfis, por vários dias: as passadas que o `status` mostra para cada perfil, a duração delas e a latência dos hooks enquanto uma passada roda.
3. Migre alguns perfis reais da 0.5.0 e confira com o `status` que cada um conta os seus turnos uma vez só e mantém as suas lições.
4. Revise os rascunhos do refletor contra as falhas de origem, além das três chamadas reais feitas até agora, e escolha um modelo de baixo custo para `auxiliary.evolution_reflector`.
5. Confira os bloqueios da guarda de runtime e as propostas do radar contra o que de fato aconteceu no host, para achar falsos positivos.
6. Compare a duração e a qualidade de pesquisas reais antes e depois dos planos. Use o mesmo conjunto de perguntas, valide as fontes e registre onde o orçamento deixou uma lacuna material.
7. Cruze falhas dos workers, sobreposição de horários e volume de fila. Faça uma mudança por vez e acompanhe latência, taxa de erro e acúmulo de trabalho.
8. Monte um conjunto controlado de cenários Linux com pressão de CPU, I/O e memória. Verifique o valor efetivo do controlador, o impacto no Hermes e o rollback com o agente e a unidade auxiliar em execução.
