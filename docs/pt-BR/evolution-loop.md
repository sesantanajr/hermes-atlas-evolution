# O ciclo de evolução

[English](../evolution-loop.md) | **Português (Brasil)**

Este documento descreve o ciclo que o Atlas Evolution 0.4.0 acrescenta ao Hermes Agent: o que é registrado, quando uma falha vira lição, como os efeitos são medidos e o que vai para a fila de propostas. Cada limiar citado aqui é uma constante do código, e o módulo onde ela está aparece ao lado. Para instalação e visão geral, veja o [README](../../README.pt-BR.md).

## Conteúdo

1. [Uma passada](#uma-passada)
2. [Sinais](#sinais)
3. [Lições](#lições)
4. [Como uma lição é medida](#como-uma-lição-é-medida)
5. [Mudanças e experimentos](#mudanças-e-experimentos)
6. [Saúde das skills](#saúde-das-skills)
7. [Rendimento do aprendizado](#rendimento-do-aprendizado)
8. [Propostas](#propostas)
9. [Resumo](#resumo)
10. [Ferramentas do agente e a skill distribuída](#ferramentas-do-agente-e-a-skill-distribuída)
11. [Armazenamento](#armazenamento)

## Uma passada

Os hooks registram sinais enquanto o agente trabalha. Todo o resto acontece em uma passada (`evolve.cycle`), que lê os registros do próprio Hermes, atualiza as tabelas do plugin e nunca altera o Hermes. Uma passada roda quando:

- você executa `hermes evolution learn`;
- o `hermes evolution ops tick` roda, no máximo uma vez por hora (o resultado vem no campo `evolution` da saída);
- a receita do resumo semanal roda, chamando `learn --notify-only` antes do `report`.

Nada agenda a passada por conta própria. Sem um desses três caminhos, os sinais se acumulam, mas nenhuma lição é derivada e nada é medido.

Uma passada faz o seguinte, nesta ordem:

1. Descarta agregados mais antigos que a janela de retenção.
2. Conta as revisões em segundo plano registradas desde a passada anterior.
3. Deriva lições das falhas recorrentes.
4. Mede cada lição derivada que está ativa.
5. Calcula a saúde das skills nos últimos 7 dias.
6. Detecta mudanças de configuração e reescritas de skills.
7. Compara a semana anterior e a semana seguinte de cada mudança acompanhada.
8. Fecha as propostas `skill_lock` aprovadas cuja skill já está travada ou fixada.
9. Reconstrói as propostas abertas a partir da evidência atual.

Com `--notify-only`, o `learn` imprime apenas lições novas, regressões, lições aposentadas nesta passada e propostas abertas de risco alto. Quando não há nenhuma, não imprime nada.

## Sinais

### Desfecho das ferramentas

O hook `post_tool_call` conta toda chamada que termina com status `ok`, `error` ou `blocked`, por ferramenta e dia UTC: chamadas, erros, bloqueios e duração total. Outros status, como `timeout` e `cancelled`, são contados pelo observador de operações e não entram no ciclo. O Hermes encapsula ferramentas adiadas em `tool_call`; quando esse invólucro traz uma única chamada, ela é atribuída à ferramenta que de fato rodou.

Uma chamada com status `error` ou `blocked` é classificada. O classificador olha a mensagem de erro (primeiros 2.000 caracteres) e, quando o resultado é JSON, os campos `error`, `hint`, `approval` e `message`, os últimos 1.500 caracteres de `output` e o `exit_code`. Para um resultado que não é JSON, olha os primeiros 1.500 caracteres. Resultados com mais de 262.144 caracteres não são interpretados. O texto é classificado em memória e descartado.

O resultado é uma assinatura: os 20 primeiros caracteres hexadecimais do SHA-256 de ferramenta, classe e detalhe. Duas falhas com a mesma ferramenta, a mesma classe e o mesmo detalhe são a mesma falha.

### Classes de falha

As regras são testadas na ordem da tabela, e a primeira que casa dá nome à classe (`signals._RULES`).

| Classe | Casa, por exemplo, com | Detalhe guardado | Modelo de lição |
|---|---|---|---|
| `missing_argument` | `missing required argument(s): X`, `the following arguments are required` | o nome do argumento | sim, quando o nome é conhecido |
| `locked_resource` | `is locked by the user` | nenhum | sim |
| `review_precondition` | `Refusing background curator patch`, `has not been loaded in this review` | nenhum | sim |
| `edit_precondition` | `Refusing to overwrite`, "must be read before" | nenhum | sim |
| `edit_mismatch` | `Could not find a match for old_string`, `No edit was applied` | nenhum | sim |
| `approval_timeout` | `timed out without user response` | nenhum | sim |
| `blocked_by_policy` | `BLOCKED:`, `flagged as dangerous` e toda chamada `blocked` que não casa com outra regra | `inline_script` para execução de código em linha | sim |
| `rate_limited` | `rate limit`, `too many requests`, `429`, `quota exceeded` | nenhum | sim |
| `timeout` | `timed out`, `timeout`, `deadline exceeded` | nenhum | sim |
| `missing_dependency` | `X: command not found`, `No module named X`, `Unable to locate package X` | o comando, módulo ou pacote | sim, quando o nome é conhecido |
| `permission_denied` | `permission denied`, `EACCES`, `operation not permitted` | nenhum | sim |
| `auth` | `401`, `403`, `unauthorized`, `invalid api key` | nenhum | não |
| `backend_unavailable` | `not initialized`, `connection refused`, `is not running` | nenhum | sim |
| `not_found` | `not found`, `no such file`, `does not exist`, `unknown tool` | `skill` ou `file` | sim |
| `limit_exceeded` | `exceeds`, `too long`, `too large`, `limit of N` | nenhum | sim |
| `invalid_argument` | `usage:`, `unrecognized arguments`, `invalid choice`, `must be one of` | nenhum | sim |
| `code_exception` | um traceback do Python, `SyntaxError` | a classe da exceção | não |
| `network` | `connection reset`, `network is unreachable`, erro de SSL | nenhum | não |
| `command_failed` | nenhuma regra casou e o código de saída não é zero | `exit_<código>` ou `exit_signal` | não |
| `other` | nada casou | nenhum | não |

Quinze classes têm modelo. `auth`, `code_exception`, `network`, `command_failed` e `other` nunca viram lição derivada, porque nenhuma frase fixa serve como conselho seguro para elas. Quando uma dessas se repete, a fila recebe uma proposta `investigate_failure`.

### O filtro do detalhe

O detalhe é o único valor livre tirado de uma mensagem de erro, por isso passa por um filtro antes de ser gravado (`signals._clean_hint`). Ele só é mantido quando tem cara de nome curto:

- casa com `[A-Za-z_][A-Za-z0-9_.+-]*` e tem no máximo 32 caracteres;
- tem no máximo 4 dígitos e pelo menos uma letra;
- quando passa de 8 caracteres, no máximo 60% das letras são maiúsculas;
- não é URL e não começa com prefixo conhecido de credencial;
- dentro do Hermes, sai inalterado do filtro de segredos do próprio Hermes.

Qualquer outra coisa é descartada, e a falha é gravada sem detalhe. Nomes de ferramenta e de skill passam por uma lista de permissão parecida (`experience.label`); o nome que não passa é gravado como `unknown`.

### Turnos

Um turno é medido de `pre_llm_call` até `on_session_end`, que o Hermes dispara ao fim de toda execução, inclusive as interrompidas. Cada turno é registrado por dia UTC e por superfície.

| Superfície | Quando |
|---|---|
| `subagent` | o payload traz uma sessão pai |
| `local` | plataforma `cli`, `tui`, `desktop`, `acp` ou `local` |
| `cron` | plataforma `cron` |
| `gateway` | qualquer outra plataforma |

O registro tem apenas contadores: turnos, concluídos, falhos, interrompidos, lentos, turnos com ferramentas, erros de ferramenta, duração total e máxima, e seis faixas de latência (até 15 s, 30 s, 60 s, 120 s, 300 s e acima). Um turno é lento quando dura pelo menos `slow_turn_seconds` (padrão 60). O p50 e o p95 do resumo são o limite superior da faixa em que o percentil cai, não valores exatos. O rastreador mantém no máximo 512 turnos abertos em memória, e a duração é limitada a 4 horas.

### Feedback do dono

Com `feedback_signals` ligado, o plugin olha a mensagem do usuário no início do turno e guarda um contador por dia e por tipo. A mensagem em si não é gravada.

- **De quem são as mensagens que contam.** Sessões locais, sempre. Mensagens do gateway, só quando o ID do remetente está em `owner_sender_ids`. Subagentes, nunca.
- **Correção.** A mensagem começa com um marcador de correção em português ou inglês, como "não", "errado", "wrong" ou "try again".
- **Agradecimento.** A mensagem começa com um marcador de agradecimento, como "obrigado", "thanks" ou "perfect".
- **Pergunta repetida.** A mensagem chega até 180 segundos depois da anterior na mesma sessão, tem pelo menos 4 palavras distintas e compartilha pelo menos 60% delas com a anterior. A mensagem anterior fica em memória como um conjunto de palavras em hash, para até 256 sessões.

São heurísticas sobre as primeiras palavras da mensagem. Leia as contagens como indicadores.

### Eventos de skills

O hook `on_skill_lifecycle` conta, por skill e por dia, as ações `loaded`, `patched`, `edited`, `created`, `installed`, `archived`, `stale` e `restored`, além de `reused_after_patch` quando o Hermes informa que uma skill alterada foi carregada de novo.

## Lições

Uma lição é uma frase no formato "quando X, faça Y". O conjunto das lições é o playbook.

### Quando uma falha vira lição

A passada cria uma lição para uma assinatura de falha quando tudo isto é verdade (`lessons.derive`):

- o modo é `suggest` ou `auto`;
- a falha foi vista pelo menos 3 vezes (`MIN_TOTAL`);
- ela apareceu em pelo menos 2 sessões ou em pelo menos 2 dias UTC, porque uma sessão ruim ainda não é um padrão;
- a classe tem modelo, e o detalhe existe quando o modelo precisa dele;
- o nome da ferramenta é conhecido;
- ainda não existe lição para essa assinatura.

A contagem de sessões sobe quando a sessão da falha é diferente da sessão da falha anterior da mesma assinatura. O ID da sessão é guardado apenas como hash truncado.

A frase vem de um modelo fixo em inglês e português (`lessons.TEMPLATES`), preenchido com o nome da ferramenta e o detalhe. Nenhum modelo de linguagem é chamado. Exemplo para `missing_argument`: "Ao chamar `example_search`, informe sempre o argumento obrigatório `query`."

### Modos

| `evolution.lessons` | Lições novas | O que o agente vê | Lições ineficazes |
|---|---|---|---|
| `off` | nenhuma é derivada | nada; a seção de prompt e o hook da dica não são registrados | proposta `lesson_retire` |
| `suggest` (padrão) | criadas como `candidate`, cada uma com uma proposta de risco baixo | só as lições que você aprovou | proposta `lesson_retire` |
| `auto` | criadas como `active` | as lições ativas, a partir da sessão seguinte | aposentadas pela passada depois de 14 dias ativas; antes disso, proposta `lesson_retire` |

Os sinais e a medição continuam em todos os modos.

### Status e origem

| Status | Significado |
|---|---|
| `candidate` | derivada, aguardando aprovação |
| `quarantined` | escrita pelo agente, aguardando aprovação |
| `active` | mostrada ao agente |
| `retired` | não é mais mostrada; fica guardada por 90 dias |

| Origem | Criada por |
|---|---|
| `derived` | uma passada, a partir de uma assinatura de falha |
| `operator` | `hermes evolution lessons add`; já nasce ativa |
| `agent` | a ferramenta `atlas_evolution_lessons`, ação `propose`; nasce em quarentena |
| `reflector` | reservada para um item do roteiro; nada a cria na 0.4.0 |

### Lições personalizadas

Uma lição do operador ou do agente é uma frase de 12 a 240 caracteres, sem caracteres de controle e sem nada que pareça segredo (`lessons.valid_text`). Ela indica a ferramenta a que se aplica, ou `general`. O playbook aceita no máximo 200 lições personalizadas não aposentadas. O agente pode propor no máximo 5 lições por hora.

Lições personalizadas não estão ligadas a uma assinatura de falha. Elas aparecem na seção de prompt e na ação `search` da ferramenta, mas nunca são anexadas como dica e não recebem efeito medido.

### Entrega

- **Seção de prompt** (`atlas-evolution.playbook`). O Hermes a monta no início de cada sessão e a congela no prompt de sistema daquela sessão. Ela lista as lições ativas, primeiro as medidas como eficazes e depois as de maior evidência, até 8 lições e 1.200 caracteres. A seção é registrada com limite de 1.400 caracteres. Cada montagem incrementa o contador `shown` da lição.
- **Dica no resultado que falhou** (`transform_tool_result`, com `tool_hints` ligado). Quando uma chamada falha com uma assinatura que tem lição ativa, a frase é acrescentada àquele resultado. Um objeto JSON ganha a chave `atlas_evolution_lesson`; qualquer outro texto ganha uma linha final que começa com `[Lição do Atlas Evolution]` (ou `[Atlas Evolution lesson]`, em inglês). Cada assinatura recebe dica uma vez por sessão, com no máximo 6 dicas por sessão. Resultados com mais de 262.144 caracteres e chamadas bem-sucedidas ficam intactos. O índice de lições ativas é recarregado no máximo uma vez por minuto.

Como a seção fica fixa durante toda a sessão e a dica vai junto de um resultado novo de ferramenta, nenhum dos dois caminhos altera um prefixo que o provedor já colocou em cache.

## Como uma lição é medida

Cada passada mede toda lição ativa que tem assinatura (`lessons.evaluate`).

| Item | Valor |
|---|---|
| Janela anterior | os 14 dias antes do dia da ativação (`BASELINE_DAYS`) |
| Janela posterior | do dia seguinte à ativação até hoje |
| Dia da ativação | fica fora das duas janelas, porque parte dele é anterior à lição |
| Taxa | falhas da assinatura por chamada da ferramenta, quando as duas janelas têm pelo menos 5 chamadas; falhas por dia nos demais casos |
| Primeiro veredito | 5 dias depois da ativação ou 20 chamadas da ferramenta depois dela, o que vier primeiro, e nunca no dia da ativação |

| Veredito | Condição |
|---|---|
| `effective` | a taxa posterior é no máximo metade da anterior |
| `ineffective` | pelo menos 3 falhas depois da ativação e taxa posterior de pelo menos 90% da anterior |
| `inconclusive` | qualquer outro caso, inclusive nenhuma falha nas duas janelas |

O veredito é recalculado a cada passada enquanto a lição está ativa. No modo `auto`, a lição que está `ineffective` 14 dias depois da ativação é aposentada (`RETIRE_AFTER_DAYS`). Nos outros casos, o veredito `ineffective` abre uma proposta `lesson_retire`.

É uma comparação de dois períodos para uma única falha. Ela não controla mudanças na forma como a ferramenta foi usada.

## Mudanças e experimentos

### O que conta como mudança

| Tipo | Fonte | Granularidade |
|---|---|---|
| `config` | configuração do Hermes, comparada com o instantâneo da passada anterior | uma mudança por chave e dia UTC |
| `skill` | ledger do Curator: `create`, `patch`, `edit`, `write_file`, `remove_file` | uma mudança por skill e dia UTC, com o número de reescritas |
| `lesson` | ativação de uma lição | registrada para contar como mudança concorrente; o efeito é medido como descrito acima |

Só estas chaves de configuração são acompanhadas (`experiments.CONFIG_KEYS`): `compression.enabled`, `compression.threshold_tokens`, `compression.proactive_prune_tokens`, `model.default`, `model.provider`, `memory.provider`, `memory.nudge_interval`, `skills.creation_nudge_interval`, `auxiliary.background_review.enabled`, `auxiliary.background_review.model`, `auxiliary.compression.model`, `tool_budget.mcp_result_size_chars`, `curator.enabled`, `curator.consolidate`, e a lista de plugins habilitados, como contagem mais um hash curto. Os valores são guardados como rótulos curtos; o valor que não é um rótulo simples é guardado como `set`.

A primeira passada só registra o instantâneo. A configuração está disponível para os comandos `hermes evolution`. O executor independente só a enxerga quando o PyYAML está instalado; sem ele, nenhuma mudança de configuração é acompanhada.

### Janelas e métricas

Uma mudança é comparada nos 7 dias anteriores e nos 7 dias seguintes, deixando de fora o dia da mudança (`WINDOW_DAYS`). O primeiro veredito, provisório, sai 3 dias depois da mudança. Ele é recalculado a cada passada e se torna final 8 dias depois da mudança.

| Métrica | Variação que importa | Amostra mínima em cada janela |
|---|---|---|
| `turn_failed_pct` | 2 pontos percentuais | 20 turnos |
| `turn_slow_pct` | 5 pontos percentuais | 20 turnos |
| `turn_mean_ms` | 3.000 ms | 20 turnos |
| `owner_corrections_per_100_turns` | 2 | 20 turnos |
| `tool_error_pct` | 2 pontos percentuais | 40 chamadas de ferramenta |
| `uncached_input_tokens_per_call` | 300 tokens | 30 chamadas de API |

As métricas de turno usam só as superfícies interativas (`local` e `gateway`). As correções do dono incluem as perguntas repetidas. Uma métrica piorou quando subiu pelo menos o valor acima e pelo menos 25% em termos relativos. Melhorou quando caiu pelo menos esse valor e pelo menos 20%. A variação relativa é calculada sobre o maior entre o valor antigo e o valor acima, para que bases pequenas não gerem proporções enormes.

| Veredito | Condição |
|---|---|
| `insufficient_data` | nenhuma métrica teve a amostra mínima nas duas janelas |
| `regressed` | pelo menos uma métrica pior, nenhuma melhor |
| `improved` | pelo menos uma métrica melhor, nenhuma pior |
| `mixed` | algumas melhores e outras piores |
| `neutral` | medido, sem variação relevante |

Cada veredito guarda as amostras que o sustentam, o número de outras mudanças a até 7 dias desta (`concurrent_changes`) e o rótulo do método, `same_metrics_week_before_vs_week_after_correlational`. O veredito diz que as métricas se moveram depois da mudança. Não diz que a mudança foi a causa.

`hermes evolution experiments` lista as mudanças com os vereditos.

## Saúde das skills

A saúde das skills lê dois arquivos do Hermes e mais nada (`skill_health`): `skills/.usage.json`, para os contadores, e `skills/.curator_ledger.jsonl`, para o histórico de alterações. O corpo das skills nunca é aberto. Do ledger ficam o ID da entrada, o horário, o nome da skill, a ação, o autor e os hashes de conteúdo de antes e de depois.

| Achado | Condição | Proposta |
|---|---|---|
| `skill_churn` | 8 ou mais reescritas na janela, ou 2 ou mais ocorrências de vai-e-volta | `skill_lock`, risco médio |
| `skill_unexercised_rewrites` | 5 ou mais reescritas desde a última vez em que a skill foi reutilizada após uma alteração | `skill_review`, risco baixo |
| `skill_never_used` | criada pelo agente, ativa, nunca usada e com pelo menos 21 dias | `skill_archive`, risco baixo |

Um vai-e-volta é uma reescrita que devolve um arquivo a um hash que ele já teve antes na janela (A, depois B, depois A). Skills fixadas ou travadas não geram achado. `hermes evolution skills` imprime o mesmo relatório para 1 a 35 dias.

## Rendimento do aprendizado

O Hermes 0.23.1 não tem hook para o fim de uma revisão em segundo plano, então o rendimento vem da linha de conclusão que ele grava em `logs/agent.log` (`logwatch`). De cada linha o plugin guarda o dia, o número de chamadas de modelo, os tokens de entrada, de saída e de cache, e o rótulo do resultado: `none`, `skill`, `memory`, `skill+memory` ou `error`. O resto da linha não é gravado.

Um cursor (inode e posição) impede que a mesma linha seja contada duas vezes. Na primeira execução, ou depois de uma rotação, a leitura começa dentro dos últimos 4 MiB do arquivo, para que o histórico antigo não seja atribuído a hoje. Uma passada lê no máximo 32 MiB.

O resumo mostra quantas revisões rodaram, a parcela que não produziu nada e os tokens gastos. Quando o plugin também observou as chamadas do modelo principal, ele acrescenta a parcela dos tokens de entrada sem cache e de saída que foi para as revisões. Essa parcela é aproximada.

## Propostas

Tudo o que o ciclo quer mudar passa por uma fila única (`proposals`). Cada proposta traz a evidência, um nível de risco e um comando.

| Tipo | Risco | Aberta quando | Comando | Ao aprovar |
|---|---|---|---|---|
| `lesson` | baixo | uma lição está `candidate` ou `quarantined` | `hermes evolution lessons approve <id>` | o plugin ativa a lição |
| `lesson_retire` | baixo | uma lição ativa foi medida como `ineffective` | `hermes evolution lessons retire <id>` | o plugin aposenta a lição |
| `skill_lock` | médio | `skill_churn` | `hermes curator lock <skill>` | você executa |
| `skill_review` | baixo | `skill_unexercised_rewrites` | `hermes curator ledger --skill <skill>` | você executa |
| `skill_archive` | baixo | `skill_never_used` | `hermes curator archive <skill>` | você executa |
| `review_interval` | médio | pelo menos 10 revisões em segundo plano nos últimos 7 dias e pelo menos metade sem produzir nada | `hermes config set skills.creation_nudge_interval 20` | você executa |
| `investigate_failure` | baixo | uma falha sem modelo vista pelo menos 5 vezes em pelo menos 2 dos últimos 7 dias | `hermes evolution lessons add --tool <ferramenta> "<o que fazer no lugar>"` | você escreve a lição |
| `investigate_latency` | médio | pelo menos 20 turnos interativos em 7 dias e pelo menos 30% deles lentos | `hermes evolution report --days 7` | você investiga |
| `skill_rollback` | alto | uma mudança de skill com veredito `regressed` | `hermes curator rollback <id-da-entrada>` | você executa |
| `config_review` | alto | uma mudança de configuração com veredito `regressed` | `hermes config get <chave>` | você revisa a configuração |

O plugin só aplica os dois tipos de lição. Para todos os outros, `proposals approve` marca a proposta como aprovada e devolve o comando no campo `run_this`. Nada é executado por você.

### Ciclo de vida

- `open`: aguardando decisão. A evidência é atualizada a cada passada.
- `applied`: proposta de lição aprovada, ou proposta `skill_lock` cuja skill uma passada posterior encontrou travada ou fixada.
- `approved`: proposta nativa que você aprovou. Só a `skill_lock` é fechada automaticamente depois; as demais ficam aprovadas, como registro da decisão.
- `rejected`: recusada. Rejeitar uma proposta `lesson` também aposenta a lição. Uma proposta rejeitada pode reabrir depois de 30 dias se a evidência continuar lá.
- `expired`: a evidência tinha sumido em uma passada posterior. Reabre se a evidência voltar.

`proposals list` ordena por risco, do maior para o menor. `proposals approve` e `proposals reject` aceitam `--note` com até 200 caracteres.

### Comportamento conhecido das propostas de regressão na 0.4.0

Uma proposta `skill_rollback` ou `config_review` é reconstruída a partir dos vereditos calculados na passada atual. Uma mudança é avaliada do dia 3 até o veredito se tornar final, no dia 8, e não é avaliada de novo depois disso. Por isso, uma proposta de regressão aberta expira na primeira passada depois que o veredito se torna final. Decida enquanto a mudança está em avaliação. O veredito em si continua visível em `hermes evolution experiments` e no resumo.

## Resumo

`hermes evolution report` monta o resumo dos últimos 1 a 35 dias (padrão 7) e compara com o mesmo período anterior.

| Seção | Conteúdo |
|---|---|
| Resumo | turnos interativos, p50 e p95, chamadas de ferramenta e erros, custo estimado pela contabilidade do Hermes quando disponível, sinais do dono |
| Comparado ao período anterior | as seis métricas acima, com um veredito |
| Falhas que se repetem | até 8 assinaturas, com contagem, dias e status da lição |
| Ciclo de auto-melhoria | revisões em segundo plano, parcela que não produziu nada, tokens, reescritas de skills e as skills mais reescritas |
| Playbook | contagem por status e até 8 lições ativas com o efeito medido |
| Mudanças e o efeito medido | até 8 mudanças que já têm veredito |
| Propostas aguardando decisão | até 12 propostas abertas, com o comando |

`--format json` devolve a estrutura completa, que também traz os turnos de cron e de subagentes, os totais por ferramenta e os eventos de skills. `--lang en` ou `--lang pt` se sobrepõe ao idioma configurado. Onde um número não pôde ser medido, o resumo diz "não medido".

`report --schedule-spec` imprime uma receita: um script e os argumentos de um `hermes cron create` nativo em modo sem agente, por padrão no dia da semana 1, às 8 horas, com entrega local (`--weekday`, `--hour`, `--deliver`, `--days` e `--lang` mudam isso; o dia 0 é domingo). O script roda `learn --notify-only` e depois `report`. O comando não grava arquivo e não cria rotina.

## Ferramentas do agente e a skill distribuída

As duas ferramentas pertencem ao toolset `atlas-evolution` e só ficam disponíveis quando `agent_advice` é `true` na configuração.

| Ferramenta | Ações |
|---|---|
| `atlas_evolution_lessons` | `search`: até 5 lições ativas de uma ferramenta, incluídas as `general`. `propose`: guarda uma lição em quarentena para revisão do operador. `status`: contagem de lições por status e até 20 propostas abertas. |
| `atlas_evolution_plan` | devolve um plano delimitado de pesquisa, delegação ou diagnóstico operacional. Está descrita em [Operações](operations.md). |

A skill `hermes-evolution:evolution-review` é registrada junto com o plugin. Ela orienta o agente a conduzir a revisão com o dono: ler o resumo, explicar falhas e lições, percorrer as propostas abertas do maior risco para o menor e aplicar apenas o que o dono aprovou. As regras da skill proíbem aprovar em nome do dono.

## Armazenamento

As tabelas do ciclo ficam em `<HERMES_HOME>/evolution/evolution.sqlite3`, ao lado das tabelas do observador de operações. A versão 0.4.0 migra esse banco para o esquema 4 automaticamente. A versão 0.3.0 não abre um banco migrado.

| Tabela | Uma linha por | Guarda |
|---|---|---|
| `tool_daily` | dia e ferramenta | chamadas, erros, bloqueios, duração total |
| `failure` | assinatura | ferramenta, classe, detalhe, primeira e última ocorrência, total, sessões, hash da última sessão com a falha |
| `failure_daily` | dia e assinatura | contagem |
| `turn_daily` | dia e superfície | contadores de turno e faixas de latência |
| `feedback_daily` | dia e tipo | contagem de correções, perguntas repetidas ou agradecimentos |
| `skill_daily` | dia, skill e ação | contagem |
| `review_daily` | dia e resultado | revisões, chamadas de modelo, tokens de entrada, de saída e de cache |
| `lesson` | lição | assinatura, ferramenta, classe, detalhe, origem, status, texto personalizado, horários, vezes mostrada, dicas dadas, veredito, taxa anterior e posterior |
| `change` | mudança acompanhada | tipo, assunto, detalhe, horário, veredito, métricas medidas |
| `proposal` | proposta | tipo, risco, assunto, evidência, comando, status, nota |
| `cursor` | chave | posição no `agent.log`, último instantâneo da configuração, último horário do ledger, última passada agendada |

Agregados diários, mudanças, propostas decididas e lições aposentadas ficam por 90 dias (`experience.RETENTION_DAYS`). Lições não aposentadas e propostas abertas ou aprovadas são mantidas. A retenção é aplicada por cada passada e pela primeira gravação de hook de cada dia UTC.

As gravações dos hooks compartilham uma conexão por processo e por perfil. Enquanto o banco está ocupado, a atualização espera em uma fila de até 512 entradas; o que estoura a fila é contado e aparece como `dropped_events` em `hermes evolution usage`.
