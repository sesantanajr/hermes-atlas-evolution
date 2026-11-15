# O ciclo de evolução

[English](../evolution-loop.md) | **Português (Brasil)**

Este documento descreve o ciclo que o Atlas Evolution 0.4.0 acrescenta ao Hermes Agent: o que é registrado, quando uma falha vira lição, como lições, skills e mudanças são medidas, o que faz o refletor opcional e o que vai para a fila de propostas. Cada limiar citado aqui é uma constante do código, e o módulo onde ela está aparece ao lado. Para instalação e visão geral, veja o [README](../../README.pt-BR.md).

## Conteúdo

1. [Uma passada](#uma-passada)
2. [Sinais](#sinais)
3. [Lições](#lições)
4. [O refletor](#o-refletor)
5. [Como uma lição é medida](#como-uma-lição-é-medida)
6. [Skills pelo resultado](#skills-pelo-resultado)
7. [Mudanças e experimentos](#mudanças-e-experimentos)
8. [Saúde das skills](#saúde-das-skills)
9. [Rendimento do aprendizado](#rendimento-do-aprendizado)
10. [Propostas](#propostas)
11. [Resumo](#resumo)
12. [Configuração e idioma](#configuração-e-idioma)
13. [Ferramentas do agente e a skill distribuída](#ferramentas-do-agente-e-a-skill-distribuída)
14. [Armazenamento](#armazenamento)

## Uma passada

Os hooks registram sinais enquanto o agente trabalha. Todo o resto acontece numa passada (`evolve.cycle`), que lê os registros do próprio Hermes, atualiza as tabelas do plugin e nunca altera o Hermes. Uma passada roda quando:

- você executa `hermes evolution learn`;
- o `hermes evolution ops tick` roda, no máximo uma vez por hora (`CYCLE_INTERVAL_SECONDS`; o resultado vem no campo `evolution` da saída);
- a receita do resumo semanal roda, chamando `learn --notify-only` antes do `report`.

Nada agenda a passada por conta própria. Sem um desses três caminhos, os sinais se acumulam, mas nenhuma lição é derivada e nada é medido.

Uma passada faz o seguinte, nesta ordem:

1. Descarta agregados mais antigos que a janela de retenção.
2. Conta as revisões em segundo plano registradas desde a passada anterior.
3. Deriva lições das falhas recorrentes.
4. Pede rascunhos ao refletor quando ele está ligado e o acesso a modelos do Hermes está disponível; caso contrário, apaga os trechos de erro em espera.
5. Mede cada lição ativa ligada a uma falha.
6. Calcula a saúde das skills nos últimos 7 dias.
7. Detecta mudanças de configuração e reescritas de skills.
8. Compara a semana anterior e a semana seguinte de cada mudança acompanhada.
9. Fecha as propostas `skill_lock` aprovadas cuja skill já está travada ou fixada.
10. Compara as skills carregadas nos últimos 14 dias com o perfil como um todo.
11. Reconstrói as propostas abertas a partir da evidência atual.

Com `--notify-only`, o `learn` imprime apenas lições novas, rascunhos novos do refletor, regressões cujo veredito ficou final nesta passada, lições aposentadas nesta passada e propostas de risco alto abertas nesta passada. Quando não há nenhuma, não imprime nada.

## Sinais

### Desfecho das ferramentas

O hook `post_tool_call` conta toda chamada que termina com status `ok`, `error` ou `blocked`, por ferramenta e dia UTC: chamadas, erros, bloqueios e duração total. Outros status, como `timeout` e `cancelled`, são contados pelo observador de operações e não entram no ciclo. O Hermes encapsula ferramentas adiadas em `tool_call`; quando esse invólucro traz uma única chamada, ela é atribuída à ferramenta que de fato rodou, e um invólucro não resolvido recebe o nome da ferramenta tirado do texto de erro do próprio Hermes.

Uma chamada com status `error` ou `blocked` é classificada (`signals.classify`). O classificador lê primeiro o texto que o Hermes ou a ferramenta escreveu sobre o erro: a mensagem de erro (primeiros 2.000 caracteres) e, quando o resultado é um objeto JSON, os campos `error`, `hint`, `approval` e `message`. Só quando esse texto não casa com nenhuma regra é que ele lê a saída do comando: os últimos 1.500 caracteres do campo `output`, ou os primeiros 1.500 caracteres de um resultado que não é JSON. Também lê o `exit_code`. Resultados com mais de 262.144 caracteres não são interpretados como JSON. O texto é classificado em memória e descartado.

O resultado é uma assinatura: os 20 primeiros caracteres hexadecimais do SHA-256 de ferramenta, classe e detalhe. Duas falhas com a mesma ferramenta, a mesma classe e o mesmo detalhe são a mesma falha.

### Classes de falha

As regras são testadas na ordem da tabela, e a primeira que casa dá nome à classe (`signals._RULES`).

| Classe | Casa, por exemplo, com | Detalhe guardado | Modelo de lição |
|---|---|---|---|
| `missing_argument` | `missing required argument(s): X`, `the following arguments are required` | o nome do argumento | sim, quando o nome é conhecido |
| `approval_refused` | `User denied`, `has NOT consented`, `Silence is not consent`, `timed out without user response`, `Failed to send approval request`, `nobody can approve` | nenhum | nunca |
| `locked_resource` | `is locked by the user`, `locked by operator` | nenhum | sim |
| `review_precondition` | `Refusing background curator patch`, `has not been loaded in this review` | nenhum | sim |
| `edit_precondition` | `Refusing to overwrite`, `has not seen its`, "must be read before" | nenhum | sim |
| `edit_mismatch` | `Could not find a match for old_string`, `No edit was applied` | nenhum | sim |
| `blocked_by_policy` | `BLOCKED:`, `flagged as dangerous`, `blocked by policy` e toda chamada `blocked` que não casa com outra regra | `inline_script` para execução de código em linha | sim |
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

Catorze classes têm modelo (`lessons.TEMPLATES`).

- **`approval_refused`** cobre a barreira de aprovação do Hermes: o usuário negou, ninguém respondeu, nenhum aprovador pôde ser alcançado ou o contexto não permite aprovar. O Hermes já diz ao agente para não chegar ao mesmo resultado por outro caminho, então essa classe nunca vira lição de tipo algum: não tem modelo, não recebe rascunho do refletor nem proposta `investigate_failure`. O resumo a marca como "nunca vira lição: quem decide é a aprovação do usuário".
- **`auth`, `code_exception`, `network`, `command_failed` e `other`** não têm modelo porque nenhuma frase fixa é um conselho seguro para elas. Quando uma delas se repete, o refletor pode rascunhar uma lição (se estiver ligado), e a fila ganha uma proposta `investigate_failure`.
- **`blocked_by_policy`** tem um modelo que diz ao agente para não repetir, não procurar outro caminho para o mesmo resultado e dizer ao usuário do que precisava.

### O filtro do detalhe

O detalhe é o único valor livre tirado de uma mensagem de erro, então passa por um filtro antes de ser gravado (`signals._clean_hint`). Ele só é mantido quando tem cara de nome curto:

- casa com `[A-Za-z_][A-Za-z0-9_.+-]*` e tem no máximo 32 caracteres;
- tem no máximo 4 dígitos e pelo menos uma letra;
- quando passa de 8 caracteres, no máximo 60% das letras são maiúsculas;
- não é URL nem começa com um prefixo conhecido de credencial;
- dentro do Hermes, sai inalterado do redator de segredos do próprio Hermes, que roda com `force=True` para valer mesmo quando o perfil desligou a redação.

Um detalhe lido da saída de um comando, que qualquer um pode escrever, também precisa ser um nome de comando em minúsculas (`[a-z_][a-z0-9_+-]*`) ou, para um módulo ausente, um nome de módulo em minúsculas com pontos. Todo o resto é descartado, e a falha é gravada sem detalhe. Nomes de ferramentas e skills passam por uma lista de permissão parecida (`experience.label`); o nome que não passa é gravado como `unknown`.

### Turnos

Um turno é medido do `pre_llm_call` ao `on_session_end`, que o Hermes dispara ao fim de toda execução, inclusive as interrompidas. Cada turno é registrado por dia UTC e superfície.

| Superfície | Quando |
|---|---|
| `subagent` | o payload tem uma sessão pai |
| `local` | plataforma `cli`, `tui`, `desktop`, `acp` ou `local` |
| `cron` | plataforma `cron` |
| `gateway` | qualquer outra plataforma |

O registro guarda só contadores: turnos, concluídos, falhos, interrompidos, lentos, turnos com ferramentas, erros de ferramenta, duração total e máxima, e seis faixas de latência (até 15 s, 30 s, 60 s, 120 s, 300 s e acima). Um turno é lento quando dura pelo menos `slow_turn_seconds` (padrão 60). O p50 e o p95 do resumo são o limite superior da faixa onde o percentil cai, não valores exatos. O rastreador mantém no máximo 512 turnos abertos em memória, esquece um turno aberto há mais de 6 horas e limita a duração a 4 horas.

### Sinais do dono

Com `feedback_signals` ligado, o plugin olha a mensagem do usuário no início de um turno e grava um contador por dia e tipo. A mensagem em si não é gravada.

- **Quem conta.** Sessões locais, sempre. Mensagens do gateway, só quando o ID do remetente está em `owner_sender_ids`. Subagentes, nunca.
- **Correção.** A mensagem começa com um marcador de correção em português ou inglês, como "não," ou "não." (com pontuação), "não era isso", "errado", "wrong" ou "try again".
- **Agradecimento.** A mensagem começa com um marcador de agradecimento, como "obrigado", "thanks" ou "perfect".
- **Pergunta repetida.** A mensagem chega até 180 segundos depois da anterior na mesma sessão, tem pelo menos 4 palavras distintas e compartilha pelo menos 60% delas com a anterior. A mensagem anterior fica em memória como um conjunto de palavras em hash, para até 256 sessões.

Uma correção ou pergunta repetida também é cobrada das skills do turno corrigido (veja [Skills pelo resultado](#skills-pelo-resultado)). São heurísticas sobre as primeiras palavras de uma mensagem. Leia as contagens como indicadores.

### Eventos de skills

O hook `on_skill_lifecycle` conta, por skill e dia, as ações `loaded`, `patched`, `edited`, `created`, `installed`, `archived`, `stale` e `restored`, além de `reused_after_patch` quando o Hermes informa que uma skill alterada voltou a ser carregada. Um evento `loaded` também é ligado ao turno aberto da mesma sessão ou tarefa, para as skills pelo resultado.

## Lições

Uma lição é uma frase no formato "quando X, faça Y". O conjunto das lições é o playbook.

### Quando uma falha vira lição

Uma passada cria uma lição para uma assinatura de falha quando tudo o que segue vale (`lessons.derive`):

- o modo é `suggest` ou `auto`;
- a falha apareceu pelo menos 3 vezes (`MIN_TOTAL`);
- apareceu em pelo menos 2 sessões ou em pelo menos 2 dias UTC, porque uma sessão ruim não é um padrão;
- a classe tem modelo, e o detalhe está presente quando o modelo precisa dele;
- o nome da ferramenta é conhecido;
- ainda não existe lição para aquela assinatura.

As sessões são contadas quando a sessão que falhou é diferente da sessão da falha anterior com a mesma assinatura. O ID da sessão só é gravado como hash truncado.

A frase vem de um modelo fixo em inglês e português (`lessons.TEMPLATES`), preenchido com o nome da ferramenta e o detalhe, e montado no idioma configurado a cada leitura. Nenhum modelo de linguagem é chamado. Exemplo para `missing_argument`: "Ao chamar `example_search`, informe sempre o argumento obrigatório `query`."

### Modos

| `lessons` | Novas lições derivadas | O que o agente vê | Lições ineficazes |
|---|---|---|---|
| `off` | nenhuma é derivada | nada; a seção de prompt e o hook da dica não são registrados | uma proposta `lesson_retire` |
| `suggest` (padrão) | criadas como `candidate`, cada uma com uma proposta de risco baixo | só as lições que você aprovou | uma proposta `lesson_retire` |
| `auto` | criadas como `active` | as lições ativas, a partir da próxima sessão | aposentadas pela passada 14 dias depois da ativação; antes disso, uma proposta `lesson_retire` |

Sinais e medição continuam em todos os modos. Em todos os modos, as lições escritas por você sem `--activate`, propostas pelo agente ou rascunhadas pelo refletor aguardam aprovação.

### Status e origem

| Status | Significado |
|---|---|
| `candidate` | derivada, aguardando aprovação |
| `quarantined` | escrita pelo operador sem `--activate`, proposta pelo agente ou rascunhada pelo refletor; aguardando aprovação |
| `active` | mostrada ao agente |
| `retired` | não é mais mostrada; apagada 90 dias depois da última atualização |

| Origem | Criada por | Ligada a uma falha | Risco da proposta |
|---|---|---|---|
| `derived` | uma passada, a partir de uma assinatura de falha | sim | baixo |
| `operator` | `hermes evolution lessons add` | não | baixo |
| `reflector` | uma passada com o refletor ligado | sim | médio |
| `agent` | a ferramenta `atlas_evolution_lessons`, ação `propose` | não | alto |

### Regras do texto

O texto de lição que não vem de modelo fixo, seja seu, do agente ou do refletor, passa por `lessons.clean_text`:

- é normalizado para Unicode NFKC e tem os espaços colapsados;
- caracteres de controle, de formatação (direção de texto e largura zero), de uso privado e substitutos e separadores de linha ou parágrafo são recusados, não removidos, para que o que você lê seja exatamente o que o agente recebe;
- tem de 12 a 240 caracteres;
- não pode conter valor com cara de credencial (`sk-`, `ghp_`, `gho_`, `github_pat_`, `xox?-`, `AKIA`, `AIza` seguidos de pelo menos 8 caracteres) nem URL com usuário e senha.

Uma lição indica a ferramenta a que se aplica, ou `general`. O playbook guarda no máximo 200 lições que não são derivadas nem aposentadas. O agente pode propor no máximo 5 lições por hora (`agent_tools.PROPOSALS_PER_HOUR`).

### Entrega

- **Seção de prompt** (`atlas-evolution.playbook`). O Hermes a monta no início de cada sessão e a congela no prompt de sistema daquela sessão. Ela lista as lições ativas de qualquer origem, primeiro as medidas como eficazes e depois as de maior evidência, até 8 lições e 1.200 caracteres, sob um cabeçalho de uma linha. A seção é registrada com limite de 1.400 caracteres. Ela só lê o banco e conta cada exibição (`shown`) pela fila de gravação dos hooks, então uma passada rodando em outro processo não deixa uma sessão nova sem as suas lições.
- **Dica no resultado que falhou** (`transform_tool_result`, com `tool_hints` ligado). Quando uma chamada com status `error` ou `blocked` falha com uma assinatura que tem lição ativa, a frase é acrescentada àquele resultado. Um objeto JSON ganha a chave `atlas_evolution_lesson`; qualquer outro texto ganha uma linha final que começa com `[Lição do Atlas Evolution]` (`[Atlas Evolution lesson]` em inglês). Cada assinatura vira dica uma vez por sessão, com no máximo 6 dicas por sessão (`hints.MAX_HINTS_PER_SESSION`), e só quando a chamada tem ID de sessão. Resultados com mais de 262.144 caracteres e chamadas bem-sucedidas ficam intactos. Só lições ligadas a uma falha (derivadas e rascunhos do refletor) podem virar dica. O índice das lições ativas é recarregado no máximo uma vez por minuto, então uma lição aprovada durante uma sessão pode virar dica naquela mesma sessão.

Como a seção fica fixa durante toda a sessão e a dica é acrescentada a um resultado novo de ferramenta, nenhum dos dois caminhos altera um prefixo que o provedor já tenha em cache.

## O refletor

O refletor (`reflector`) rascunha lições para falhas recorrentes que não têm modelo. Vem desligado e é ligado pela opção `reflector`. Enquanto está desligado, nada do que segue acontece.

**O que é guardado.** Quando o plugin carrega com o refletor ligado e `lessons` não está em `off`, o hook de ferramentas guarda, para cada assinatura de falha das classes `code_exception`, `command_failed`, `network`, `auth` e `other`, o trecho de erro mais recente (`failure_sample`):

1. as últimas 4 linhas não vazias do texto de erro e da saída, unidas, com no máximo 1.200 caracteres;
2. passadas pelo redator de segredos do Hermes, quando o plugin roda dentro do Hermes, com `force=True` para valer mesmo quando o perfil desligou a redação;
3. mascaradas pelo plugin: URLs viram `<url>`, e-mails `<email>`, caminhos de dois ou mais segmentos `<path>`, sequências hexadecimais de 16 ou mais caracteres `<hex>`, valores com prefixo de credencial `<secret>`, sequências de 5 ou mais dígitos `<n>` e textos entre aspas com mais de 40 caracteres `'<text>'`;
4. cortadas nos últimos 300 caracteres (`EXCERPT_LIMIT`).

**Quais falhas recebem rascunho.** A mesma regra de recorrência das lições derivadas: pelo menos 3 ocorrências, em pelo menos 2 sessões ou 2 dias, com trecho guardado e sem lição. As mais frequentes vêm primeiro.

**Quantas chamadas.** No máximo 3 por passada (`PER_RUN`) e `reflector_daily_limit` por dia UTC (padrão 6, de 1 a 50). Toda tentativa conta para o limite diário, inclusive as que falham.

**A chamada.** O plugin registra a tarefa auxiliar do Hermes `evolution_reflector` ("Atlas Evolution reflector") com `inherit_from="compression"`, então, por padrão, ela usa a configuração de modelo da tarefa de compressão. Você escolhe outro modelo em `auxiliary.evolution_reflector`, na configuração do perfil. A passada chama `ctx.llm.complete_structured` com essa tarefa, um esquema JSON (`applies`, booleano, e `lesson`, texto), no máximo 300 tokens de saída e prazo de 60 segundos. A entrada traz o nome da ferramenta, a classe da falha, o detalhe, o número de ocorrências, sessões e dias, e o trecho. As instruções, no idioma configurado, dizem que o trecho é dado e nunca instrução, pedem `applies: false` quando a falha parece passageira, específica de um pedido ou fora do controle do agente, e, caso contrário, pedem uma frase de 20 a 200 caracteres no formato "quando X, faça Y", sem caminhos, nomes, credenciais, URLs ou texto copiado do trecho. O plugin não abre conexão: quem faz a chamada é o Hermes, com a própria configuração de provedor.

**O que é aceito.** Um rascunho é descartado quando `applies` não é verdadeiro, quando a frase não passa nas [regras do texto](#regras-do-texto), quando contém `<` ou quando casa com o filtro de injeção (`reflector._INJECTION`), que recusa texto em inglês que manda o modelo deixar de lado as instruções anteriores ou desconsiderar algo, cita um system prompt ou diz ao modelo o que ele é agora, pedidos em português para ignorar ou esquecer as instruções, e marcas `system` ou `assistant`. Um rascunho aceito vira uma lição `quarantined` com origem `reflector`, ligada à assinatura da falha, e ganha uma proposta de risco médio. Depois de aprovado, é mostrado, vira dica e é medido como uma lição derivada.

**Quando o trecho é apagado.** Assim que a falha tem uma lição, quando o modelo recusa ou o rascunho é descartado, 14 dias depois da última atualização (`SAMPLE_DAYS`) e em toda passada que roda com o refletor desligado. Um erro do provedor, ou uma passada sem o acesso a modelos do Hermes, mantém o trecho para uma passada seguinte.

**Onde ele roda.** Só numa passada executada pela CLI do Hermes (`hermes evolution learn`, `ops tick` ou a receita semanal), porque o acesso ao modelo vem do Hermes, e só enquanto `lessons` não está em `off` (senão o bloco informa `lessons_off`, já que um rascunho aprovado nunca seria mostrado). O executor independente (`python -m hermes_evolution`) não rascunha nada; o `learn` dele informa `unavailable_without_hermes_llm` e mantém os trechos em espera para a próxima passada feita pelo Hermes.

A saída do `learn` tem um bloco `reflector` com os rascunhos, o número de recusados e de falhas, as chamadas feitas no dia e o limite diário.

## Como uma lição é medida

Cada passada mede toda lição ativa ligada a uma assinatura de falha (`lessons.evaluate`): as derivadas e os rascunhos do refletor já aprovados.

| Item | Valor |
|---|---|
| Janela "antes" | os 14 dias anteriores ao dia da ativação (`BASELINE_DAYS`), contados na primeira passada depois da ativação e então congelados, para que a retenção nunca os altere |
| Janela "depois" | os últimos 14 dias desde a ativação (`AFTER_WINDOW_DAYS`): a partir do dia seguinte à ativação, ou de 13 dias antes de hoje quando a ativação é mais antiga |
| Dia da ativação | fica fora das duas janelas, porque é em parte anterior à lição |
| Taxa | falhas da assinatura por chamada da ferramenta, quando as duas janelas têm pelo menos 5 chamadas; senão, falhas por dia de calendário de cada janela |
| Primeiro veredito | 5 dias depois da ativação ou 20 chamadas da ferramenta depois dela, o que vier primeiro, e nunca no dia da ativação |

| Veredito | Condição |
|---|---|
| `effective` | a taxa depois é no máximo metade da taxa antes |
| `ineffective` | pelo menos 3 falhas na janela "depois" e taxa depois de pelo menos 90% da taxa antes |
| `inconclusive` | qualquer outro caso, inclusive nenhuma falha nas duas janelas |

O veredito é recalculado a cada passada enquanto a lição está ativa. Reativar uma lição começa uma nova medição, com nova linha de base. No modo `auto`, uma lição `ineffective` 14 dias ou mais depois da ativação é aposentada (`RETIRE_AFTER_DAYS`). Nos outros casos, um veredito `ineffective` abre uma proposta `lesson_retire`.

É uma comparação de dois períodos para uma falha. Não controla mudanças na forma de usar a ferramenta.

## Skills pelo resultado

O Hermes informa cada skill que carrega pelo `on_skill_lifecycle`. O plugin liga uma skill `loaded` ao turno aberto da mesma sessão ou tarefa, até 16 skills por turno (`signals.MAX_SKILLS_PER_TURN`), e cobra de cada uma o desfecho do turno (`skill_outcome_daily`):

- o turno falhou, foi interrompido ou foi lento;
- o turno teve pelo menos um erro de ferramenta;
- o dono o corrigiu: a mensagem seguinte do dono na mesma sessão, até 30 minutos depois do fim do turno (`CORRECTION_WINDOW_SECONDS`), foi uma correção ou uma pergunta repetida. Isso exige `feedback_signals`.

Só turnos interativos são atribuídos, os das superfícies local e gateway, que são também os turnos do perfil com que a skill é comparada. Turnos de cron e de subagentes não são atribuídos.

**Comparadas com o perfil** (`skill_health.skill_outcomes`). A linha de base do perfil são todos os turnos interativos (local e gateway) da janela: a parcela que falhou, foi interrompida ou foi lenta, e correções mais perguntas repetidas por turno. Uma skill com pelo menos 8 turnos (`MIN_SKILL_TURNS`) é marcada `skill_underperforming` quando, na parcela de turnos que falharam, foram interrompidos ou foram corrigidos, a taxa dela passa a do perfil por pelo menos a margem abaixo e é pelo menos 1,5 vez o maior valor entre a taxa do perfil e 1%.

| Taxa | Margem |
|---|---|
| falharam | 5 pontos percentuais |
| interrompidos | 10 pontos percentuais |
| corrigidos | 5 pontos percentuais |

Uma passada compara os últimos 14 dias e abre uma proposta `skill_outcome` de risco baixo para cada skill marcada, com o comando `hermes curator ledger --skill <skill>` como próximo passo. O `hermes evolution skills --days N` devolve a mesma comparação para a sua janela no bloco `outcomes`, e o resumo a mostra como "Skills pelo resultado".

**Reescrita de skill julgada pelos próprios turnos** (`skill_health.compare_skill_windows`). Quando o ledger do Curator mostra uma reescrita, a mudança é julgada pelos turnos que carregaram aquela skill na semana anterior e na seguinte, não pelo perfil inteiro. Cada janela precisa de pelo menos 5 desses turnos. Uma taxa piorou quando subiu pelo menos a sua margem e chegou a pelo menos 1,25 vez o valor antigo, e melhorou quando caiu pelo menos a sua margem e chegou a no máximo 0,8 vez o valor antigo.

| Taxa | Margem |
|---|---|
| falharam | 5 pontos percentuais |
| interrompidos | 10 pontos percentuais |
| com erro de ferramenta | 10 pontos percentuais |
| corrigidos | 5 pontos percentuais |
| lentos | 15 pontos percentuais |

Um veredito `regressed` abre uma proposta `skill_rollback` de risco alto com o comando nativo `hermes curator rollback <entry-id>`, em que a entrada é a última do ledger daquela skill no dia da mudança.

## Mudanças e experimentos

### O que conta como mudança

| Tipo | Origem | Granularidade |
|---|---|---|
| `config` | configuração do Hermes, comparada com o retrato da passada anterior | uma mudança por chave e dia UTC |
| `skill` | o ledger do Curator: `create`, `patch`, `edit`, `write_file`, `remove_file` | uma mudança por skill e dia UTC, com o número de reescritas |
| `lesson` | ativação de uma lição | registrada para contar como mudança simultânea; o efeito é medido como descrito acima |

Só estas chaves de configuração são acompanhadas (`experiments.CONFIG_KEYS`): `compression.enabled`, `compression.threshold_tokens`, `compression.proactive_prune_tokens`, `model.default`, `model.model`, `model.provider`, `memory.provider`, `memory.nudge_interval`, `skills.creation_nudge_interval`, `auxiliary.background_review.enabled`, `auxiliary.background_review.model`, `auxiliary.compression.model`, `tool_budget.mcp_result_size_chars`, `curator.enabled`, `curator.consolidate`, além da lista de plugins habilitados como contagem e um hash curto. Um valor que é um rótulo simples é guardado como está. Qualquer outro valor (um nome `provedor/modelo`, uma URL, um mapa) é guardado como `set:` e uma impressão digital de 10 caracteres, nunca o valor, para que uma mudança entre dois valores assim ainda seja detectada.

A primeira passada só registra o retrato. A configuração está disponível para os comandos `hermes evolution`. O executor independente só a enxerga com o PyYAML instalado e, sem ele, não acompanha nenhuma mudança de configuração.

### Janelas e métricas

Uma mudança é comparada entre os 7 dias anteriores e os 7 dias seguintes, sem contar o dia da mudança (`WINDOW_DAYS`). O primeiro veredito, provisório, sai 3 dias depois da mudança. Ele é recalculado a cada passada e fica final 8 dias depois da mudança.

Uma reescrita de skill usa os turnos que carregaram a skill, como descrito em [Skills pelo resultado](#skills-pelo-resultado). Uma mudança de configuração usa as métricas do perfil inteiro:

| Métrica | Mudança que importa | Amostra mínima em cada janela |
|---|---|---|
| `turn_failed_pct` | 2 pontos percentuais | 20 turnos |
| `turn_slow_pct` | 5 pontos percentuais | 20 turnos |
| `turn_mean_ms` | 3.000 ms | 20 turnos |
| `owner_corrections_per_100_turns` | 2 | 20 turnos |
| `tool_error_pct` | 2 pontos percentuais | 40 chamadas de ferramenta |
| `uncached_input_tokens_per_call` | 300 tokens | 30 chamadas de API |

As métricas de turno usam só as superfícies interativas (`local` e `gateway`). As correções do dono incluem perguntas repetidas. Uma métrica piorou quando subiu pelo menos o valor acima e pelo menos 25% em termos relativos. Melhorou quando caiu pelo menos esse valor e pelo menos 20%. A variação relativa é calculada contra o maior entre o valor antigo e o valor acima, para que bases pequenas não gerem razões grandes.

| Veredito | Condição |
|---|---|
| `insufficient_data` | nenhuma métrica teve a amostra mínima nas duas janelas |
| `regressed` | pelo menos uma métrica pior e nenhuma melhor |
| `improved` | pelo menos uma métrica melhor e nenhuma pior |
| `mixed` | algumas melhores e algumas piores |
| `neutral` | medido, sem mudança relevante |

Cada veredito guarda as amostras que o sustentam, o número de outras mudanças a até 7 dias desta (`concurrent_changes`) e um rótulo de método: `same_metrics_week_before_vs_week_after_correlational` para configuração e `turns_that_loaded_the_skill_week_before_vs_week_after` para skills. Um veredito diz que as métricas se moveram depois da mudança. Não diz que a mudança foi a causa.

O `hermes evolution experiments` lista as mudanças com os vereditos.

## Saúde das skills

A saúde das skills lê dois arquivos do Hermes e nada mais (`skill_health`): `skills/.usage.json`, para os contadores, e `skills/.curator_ledger.jsonl`, para o histórico de alterações. O conteúdo das skills nunca é aberto. Do ledger, o plugin guarda o ID da entrada, o horário, o nome da skill, a ação, o autor e os hashes do conteúdo antes e depois.

| Achado | Condição | Proposta |
|---|---|---|
| `skill_churn` | 8 ou mais reescritas na janela, ou 2 ou mais vai-e-volta | `skill_lock`, risco médio |
| `skill_unexercised_rewrites` | 5 ou mais reescritas desde a última vez que a skill foi reutilizada depois de uma alteração | `skill_review`, risco baixo |
| `skill_never_used` | criada pelo agente, ativa, nunca usada e com pelo menos 21 dias | `skill_archive`, risco baixo |

Um vai-e-volta é uma reescrita que devolve um arquivo a um hash que ele já teve antes na janela (A, depois B, depois A). Skills fixadas e travadas não geram achado. O `hermes evolution skills` imprime o mesmo relatório para 1 a 35 dias.

## Rendimento do aprendizado

O Hermes 0.23.1 não tem hook para o fim de uma revisão em segundo plano, então o rendimento vem da linha de conclusão que o logger `agent.background_review` grava em `logs/agent.log` (`logwatch`). De cada linha, o plugin guarda o dia, o número de chamadas de modelo, os tokens de entrada, saída e cache, e o rótulo do resultado: `none`, `skill`, `memory`, `skill+memory` ou `error`. O resto da linha não é gravado. As linhas são contadas nos dias locais do log, porque o Hermes as marca no horário local.

Um cursor (inode e posição) impede que uma linha seja contada duas vezes. Na primeira execução, ou depois de uma rotação, a leitura começa nos últimos 4 MiB do arquivo, para que histórico antigo não seja atribuído a hoje. Uma passada lê no máximo 32 MiB.

O resumo mostra quantas revisões rodaram, a parcela que não produziu nada e os tokens gastos. Quando o plugin também observou as chamadas ao modelo principal, acrescenta a parcela dos tokens de entrada sem cache e de saída que foi para as revisões. Essa parcela é aproximada.

## Propostas

Tudo o que o ciclo quer mudar passa por uma fila única (`proposals`). Cada proposta tem a evidência, um nível de risco e um comando.

| Tipo | Risco | Abre quando | Comando | Na aprovação |
|---|---|---|---|---|
| `lesson` | pela origem: derivada ou do operador, baixo; do refletor, médio; do agente, alto | uma lição está `candidate` ou `quarantined`; a evidência traz a frase exata | `hermes evolution lessons approve <id>` | o plugin ativa a lição |
| `lesson_retire` | baixo | uma lição ativa é medida como `ineffective` | `hermes evolution lessons retire <id>` | o plugin aposenta a lição |
| `skill_lock` | médio | `skill_churn` | `hermes curator lock <skill>` | você executa |
| `skill_review` | baixo | `skill_unexercised_rewrites` | `hermes curator ledger --skill <skill>` | você revisa o ledger |
| `skill_archive` | baixo | `skill_never_used` | `hermes curator archive <skill>` | você executa |
| `skill_outcome` | baixo | os turnos de uma skill vão pior que o perfil | `hermes curator ledger --skill <skill>` | você revisa a skill |
| `review_interval` | médio | pelo menos 10 revisões em segundo plano nos últimos 7 dias e pelo menos metade sem produzir nada | `hermes config set skills.creation_nudge_interval 20` | você executa |
| `investigate_failure` | baixo | uma falha de `auth`, `code_exception`, `network`, `command_failed` ou `other` vista pelo menos 5 vezes em pelo menos 2 dos últimos 7 dias | `hermes evolution lessons add --tool <ferramenta> "<o que fazer no lugar>"` | você escreve a lição |
| `investigate_latency` | médio | pelo menos 20 turnos interativos em 7 dias e pelo menos 30% deles lentos | `hermes evolution report --days 7` | você investiga |
| `skill_rollback` | alto | uma mudança de skill com veredito `regressed` | `hermes curator rollback <entry-id>` | você executa |
| `config_review` | alto | uma mudança de configuração com veredito `regressed` | `hermes config get <chave>` | você revisa a configuração |

O plugin só aplica os dois tipos de lição. Para todos os outros, o `proposals approve` marca a proposta como aprovada e devolve o comando no campo `run_this`. O plugin não executa nada no seu lugar.

### Quem pode decidir

O `proposals approve`, o `proposals reject` e os subcomandos `approve`, `retire` e `add` de `lessons` se recusam a rodar quando o ambiente traz um marcador de agente (`cli.AGENT_MARKERS`): `HERMES_SESSION_ID`, `HERMES_KANBAN_TASK` ou `HERMES_DELEGATED_CHILD_CONTEXT`, definidos pelo Hermes para trabalho do agente, ou `ATLAS_EVOLUTION_AGENT`, que o plugin acrescenta a todo subprocesso de uma sessão do agente por meio de `register_session_env`. Rode-os no seu próprio terminal.

Ativar algo mostra o texto antes e pergunta `[s/N]`: `lessons approve` e `lessons add --activate` mostram a lição, e `proposals approve` mostra a lição de uma proposta de lição ou o comando de qualquer outra. Sem terminal interativo, o comando exige `--yes`. `lessons retire` e `proposals reject` não perguntam.

### Ciclo de vida

- `open`: aguardando decisão. A evidência é atualizada a cada passada.
- `applied`: uma proposta de lição ou `lesson_retire` aprovada, as mesmas propostas resolvidas direto por `lessons approve` ou `lessons retire`, ou uma proposta `skill_lock` cuja skill uma passada posterior encontrou travada ou fixada.
- `approved`: uma proposta nativa que você aprovou. Só a `skill_lock` é fechada automaticamente depois; as demais ficam aprovadas como registro da decisão.
- `rejected`: recusada. Rejeitar uma proposta `lesson` também aposenta a lição, e o `lessons retire` marca a proposta aberta da lição como rejeitada. Uma proposta rejeitada reabre depois de 30 dias se a evidência continuar lá.
- `expired`: a evidência sumiu numa passada posterior. Reabre se a evidência voltar.

O `proposals list` ordena por risco, do maior para o menor. O `proposals approve` e o `proposals reject` aceitam `--note` com até 200 caracteres.

### Propostas de regressão

`skill_rollback` e `config_review` são montadas a partir dos vereditos gravados das mudanças dos últimos 30 dias (`REGRESSION_DAYS`), provisórios ou finais. A proposta fica aberta enquanto o veredito continua `regressed` e a mudança tem menos de 30 dias; a evidência diz se o veredito é final. O `learn --notify-only` imprime uma proposta de risco alto uma vez, na passada que a abre, e uma regressão uma vez, na passada em que o veredito fica final.

## Resumo

O `hermes evolution report` monta o resumo dos últimos 1 a 35 dias (padrão 7) e o compara com o mesmo intervalo anterior.

| Seção | Conteúdo |
|---|---|
| Resumo | turnos interativos, p50 e p95, chamadas e erros de ferramentas, custo estimado pela contabilidade do Hermes quando disponível, sinais do dono |
| Comparação com o período anterior | as seis métricas do perfil acima, com um veredito |
| Falhas que se repetem | até 8 assinaturas, com contagem, dias e status da lição; `approval_refused` aparece como "nunca vira lição" |
| Ciclo de auto-melhoria | revisões em segundo plano, parcela que não produziu nada, tokens, parcela dos tokens que foi para o aprendizado, reescritas de skills e as skills mais reescritas |
| Skills pelo resultado | o perfil inteiro e até 8 skills com pelo menos 8 turnos, marcando as que vão pior que o perfil |
| Playbook | contagem por status e até 8 lições ativas com o efeito medido |
| Mudanças e o efeito medido | até 8 mudanças que já têm veredito |
| Propostas aguardando decisão | até 12 propostas abertas com o comando |

O `--format json` devolve a estrutura completa, que traz também os turnos de cron e de subagentes, os totais por ferramenta, os eventos de skills, o bloco de skills pelo resultado e o uso observado pelos hooks. Onde um número não pôde ser medido, o resumo diz "não medido".

O `report --schedule-spec` imprime uma receita: um script e os argumentos de um `hermes cron create` nativo no modo sem agente, por padrão no dia da semana 1, às 8 horas, com entrega local (`--weekday`, `--hour`, `--deliver`, `--days` e `--lang` mudam isso; o dia 0 é domingo). O script roda `learn --notify-only` e depois `report`. O comando não grava arquivo nem cria rotina.

## Configuração e idioma

As opções do ciclo podem ser definidas em dois lugares (`cli.effective_settings`):

1. o formulário de configurações do Hermes, declarado pelo `config_schema` do `plugin.yaml` e gravado pelo Hermes em `plugins.entries.hermes-evolution.settings`;
2. o bloco `evolution` do `<HERMES_HOME>/evolution/settings.json` (lá, `agent_tools` corresponde à chave de primeiro nível `agent_advice`).

O Hermes vence chave a chave. O plugin lê as configurações quando carrega; os comandos as leem a cada execução.

A validação muda conforme quem chama (`evolve.options`). Os comandos recusam com erro um `lessons`, `slow_turn_seconds`, `owner_sender_ids` ou `reflector_daily_limit` inválido. Dentro do gateway, cada valor inválido volta ao padrão e um aviso é registrado uma vez; um `lessons` inválido significa `off`, e um `settings.json` ilegível desliga as lições e deixa todas as outras opções no padrão até ser corrigido, para que um erro de digitação nunca ligue o que você desligou. `tool_hints` e `feedback_signals` ficam ligados a menos que sejam `false`; `reflector` só fica ligado quando é `true`.

**Idioma** (`i18n`). Tudo o que uma pessoa ou o agente lê sai em inglês ou português do Brasil: lições, o cabeçalho da seção de prompt, dicas, o resumo, diagnósticos, planos, recomendações, cartões Kanban, as respostas das ferramentas do agente, mensagens de erro e a ajuda dos comandos. O idioma é escolhido nesta ordem: `--lang en|pt` na linha de comando, a opção `language` do plugin, o `HERMES_LANGUAGE` do perfil, o `display.language` da configuração do perfil, o locale (`LC_ALL`, `LC_MESSAGES`, `LANG`) e, por fim, inglês. A ajuda dos comandos é montada antes da leitura das opções, então segue só o `HERMES_LANGUAGE`, o `display.language` e o locale. Códigos como `pt`, `pt-BR` e `pt_br` selecionam português. Nomes de campo, códigos e identificadores da saída JSON continuam em inglês. Um catálogo de pares de frases no estilo gettext (`catalog`) traduz o resto, e o `tests/test_i18n.py` falha quando falta o par de uma frase de diagnóstico, de um texto de ajuda, de um cartão Kanban ou de um plano.

## Ferramentas do agente e a skill distribuída

As duas ferramentas pertencem ao conjunto `atlas-evolution` e só ficam disponíveis com `agent_tools` (`agent_advice` no `settings.json`) ligado.

| Ferramenta | Ações |
|---|---|
| `atlas_evolution_lessons` | `search`: até 5 lições ativas de uma ferramenta, incluindo as `general`. `propose`: guarda em quarentena uma lição para revisão do operador, no máximo 5 por hora. `status`: contagem de lições por status e até 20 propostas abertas. |
| `atlas_evolution_plan` | devolve um plano delimitado de pesquisa, delegação ou diagnóstico operacional. Está descrita em [Operações](operations.md). |

A skill `hermes-evolution:evolution-review` é registrada junto com o plugin. Ela orienta o agente a conduzir a revisão com o dono: ler o resumo, explicar falhas, skills pelo resultado e lições, percorrer as propostas abertas do maior risco para o menor com o texto exato de cada lição, e entregar as decisões ao dono como comandos para rodar no próprio terminal. O agente informa e recomenda; os comandos que ele executa não conseguem aprovar, rejeitar nem ativar nada.

## Armazenamento

As tabelas do ciclo ficam em `<HERMES_HOME>/evolution/evolution.sqlite3`, ao lado das tabelas do observador de operações. O banco está no esquema 7. A versão 0.4.0 migra os esquemas 0 a 6 automaticamente no primeiro uso: o 4 trouxe o ciclo, o 5 as skills pelo resultado, o 6 os trechos do refletor e o 7 as linhas de base congeladas das lições. Qualquer outro esquema é recusado, inclusive um banco já migrado aberto por uma versão anterior do plugin.

| Tabela | Uma linha por | Guarda |
|---|---|---|
| `tool_daily` | dia e ferramenta | chamadas, erros, bloqueios, duração total |
| `failure` | assinatura | ferramenta, classe, detalhe, primeira e última ocorrência, total, sessões, hash da última sessão com falha |
| `failure_daily` | dia e assinatura | contagem |
| `failure_sample` | assinatura | o trecho de erro limpo guardado para o refletor e quando foi atualizado; vazia com o refletor desligado |
| `turn_daily` | dia e superfície | contadores de turnos e faixas de latência |
| `feedback_daily` | dia e tipo | contagem de correções, perguntas repetidas ou agradecimentos |
| `skill_daily` | dia, skill e ação | contagem |
| `skill_outcome_daily` | dia e skill | turnos que carregaram a skill e quantos falharam, foram interrompidos, foram lentos, tiveram erro de ferramenta ou foram corrigidos, mais a duração total |
| `review_daily` | dia e resultado | revisões, chamadas de modelo, tokens de entrada, saída e cache |
| `lesson` | lição | assinatura, ferramenta, classe, detalhe, origem, status, texto próprio, horários, vezes exibida, dicas dadas, veredito, taxa antes e depois, contagens congeladas da linha de base |
| `change` | mudança acompanhada | tipo, assunto, detalhe, horário, veredito, métricas medidas |
| `proposal` | proposta | tipo, risco, assunto, evidência, comando, status, nota |
| `cursor` | chave | posição no `agent.log`, último retrato da configuração, último horário do ledger, última passada agendada, chamadas do refletor no dia |

Agregados diários, mudanças, propostas decididas e lições aposentadas ficam por 90 dias (`experience.RETENTION_DAYS`). Lições não aposentadas e propostas abertas ou aprovadas são mantidas. A retenção é aplicada por cada passada e pela primeira gravação de hook de cada dia UTC em cada processo.

As gravações dos hooks compartilham uma conexão por processo e perfil. Enquanto o banco está ocupado, uma atualização espera numa fila de até 512 entradas; o que estoura é contado e informado como `dropped_events` pelo `hermes evolution usage`.
