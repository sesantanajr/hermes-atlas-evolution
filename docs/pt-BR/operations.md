# Supervisor de operações

[English](../operations.md) | **Português (Brasil)**

Além do [ciclo de evolução](evolution-loop.md), o Atlas Evolution supervisiona o host e a carga de um perfil do Hermes. Ele examina o que o Hermes grava, mede a carga do agente e do Linux, e cruza evidências para sugerir testes e conduzir remediações reversíveis. Por padrão, o supervisor só observa: um ensaio no Linux exige configuração explícita. Ele usa a API pública de plugins do Hermes e preserva o seu provedor de memória, os seus plugins e o seu roteador. A Proteção do Hermes, que impede o agente de reiniciar o gateway ou de apagar aquilo de que o Hermes precisa para rodar, está descrita em [Integração com o Hermes](hermes-integration.md#a-proteção-do-hermes).

Todos os comandos abaixo são subcomandos de `hermes evolution`. Nomes de campos, códigos e identificadores da saída em JSON estão sempre em inglês. O texto para leitura (hipóteses, alternativas, recomendações, planos, cartões Kanban e mensagens de erro) sai em inglês ou português do Brasil: `--lang en|pt` antes ou depois de qualquer subcomando; senão, a configuração `language` do plugin, depois `HERMES_LANGUAGE`, `display.language` e o locale do processo e, por fim, inglês. A ajuda dos comandos segue o `HERMES_LANGUAGE`, o `display.language` e o locale.

## Conteúdo

1. [O que o supervisor mede](#o-que-o-supervisor-mede)
2. [Comandos](#comandos)
3. [Diagnósticos e alertas adaptativos](#diagnósticos-e-alertas-adaptativos)
4. [Uso e custo](#uso-e-custo)
5. [Pesquisa, delegação e agendamento](#pesquisa-delegação-e-agendamento)
6. [Remediação no Linux](#remediação-no-linux)
7. [Chaves de configuração](#chaves-de-configuração)
8. [Dados e compatibilidade](#dados-e-compatibilidade)
9. [Comandos antigos](#comandos-antigos)
10. [Como retirar o plugin](#como-retirar-o-plugin)

## O que o supervisor mede

| Área | Evidência coletada | Interpretação |
|---|---|---|
| Linux | RAM disponível, cache recuperável, swap, PSI, limites de cgroup, CPU, I/O, volume, inodes e áreas conhecidas do perfil | Separa cache útil de pressão de memória. Contadores acumulados descrevem histórico. PSI mede tempo com tarefas esperando por recursos. |
| Hermes | Estado do gateway, legibilidade e estrutura da memória nativa, integridade SQLite e categorias de erros em logs | Cruza falhas de gravação com espaço, inodes e bloqueios. Indica quando uma falha pede reprodução no Hermes, no provedor ou no plugin. A checagem de integridade (`PRAGMA quick_check`) cobre um `state.db` com WAL de até 64 MiB, com prazo cooperativo de 300 ms; acima disso, o status é `integrity_skipped_large`. |
| Plugins e skills | Tamanho, estado configurado, evidência recente de uso quando atribuível, duplicatas e variantes de skills | O uso recente aparece como dado e não gera diagnóstico. Skills duplicadas geram achados de revisão, e plugins do perfil desabilitados explicitamente com 32 MiB ou mais geram `large_disabled_plugins`. |
| Pesquisa e tokens | Duração de turnos concluídos com ferramentas web conhecidas, erros de ferramenta, tentativas, tokens por classe e cache | Distingue pesquisa longa, falha de ferramenta, limite de provedor e pouco reaproveitamento de contexto. O p95 de pesquisa é aproximado por faixas. |
| Delegação e manutenção | Eventos de subagentes, compactação, execuções do Curator e saídas de workers Kanban | Mostra falhas recorrentes e a cobertura de tokens antes e depois da compactação, quando o Hermes informa. |
| Fila e agendamento | Picos de disparos do Cron, falhas de execução e de entrega, tarefas agendadas que não puderam ser agendadas ou disparadas, itens prontos e bloqueados, cartões parados em `scheduled` e sinais de lease vencida no Kanban | Ajuda a identificar congestionamento e a escolher entre tarefas curtas, trabalho durável e scripts sem modelo. |

O coletor guarda agregados. Não registra texto de conversas, conteúdo da memória nativa, resultados de busca nem linhas brutas de log. Um incidente guarda o texto da hipótese do próprio plugin.

Campos da saída que vale conhecer:

| Campo | Onde | Significado |
|---|---|---|
| `dropped_events` | `observer_usage` no `usage` e no `tick` legado; `usage` no `ops inspect` e no `ops tick` | Eventos de hook descartados porque a fila de gravação estava cheia. Nesse caso, os totais são um limite inferior. |
| `tool_result_error` | `logs.events` no `health`; `health.logs.events` no `ops inspect` e no `ops tick` | Linhas de log que apenas repetem a saída de uma ferramenta que falhou. Elas não contam mais como falha de armazenamento, de memória ou de plugin. Aparece quando é maior que zero. |
| `schedule_errors`, `recent_fire_errors` | `workloads.cron.counts` | Tarefas agendadas habilitadas que o Hermes não conseguiu agendar, e disparos vencidos nas últimas 24 horas que nunca chegaram ao executor. |
| `scheduled` | `workloads.kanban.counts` | Cartões aguardando na coluna `scheduled`. |
| `served_by_host_gateway` | `gateway` no `health`; `health.gateway` no `ops inspect` e no `ops tick` | O perfil é um perfil com nome atendido pelo gateway raiz, cujo registro é usado nas checagens. |

## Comandos

```sh
hermes evolution ops inspect --upstream-tag v0.23.1
hermes evolution usage --days 7
hermes evolution ops tick --upstream-tag v0.23.1
hermes evolution ops history
hermes evolution ops review --days 14
hermes evolution ops plan research --effort deep
hermes evolution ops plan delegation --durable
hermes evolution ops similar "disco lento e pesquisas demoradas"
```

| Comando | O que faz |
|---|---|
| `ops inspect` | Coleta e explica os sinais do momento. Não grava observações e não altera nada no Hermes. |
| `ops tick [--notify-only]` | Coleta, compara com o histórico, registra hipóteses e linhas de base e executa somente as ações habilitadas pela configuração. Também faz uma rodada do ciclo de evolução, no máximo uma vez por hora, e a devolve no campo `evolution`; a rodada é reservada como a do autopilot, então as duas nunca rodam ao mesmo tempo, e tem um prazo de 200 segundos, dentro dos 300 segundos que o script do `schedule-spec` dá ao tick. Se o tick falhar, tenta encerrar um ensaio ativo no Linux. |
| `ops history` | Mostra até 100 incidentes e 100 ensaios, com o estado de cada um. |
| `ops review [--days N]` | Cruza medições adaptativas, feedback e resultados dos ensaios. Padrão de 14 dias, de 1 a 35. |
| `ops feedback CÓDIGO helpful\|dismissed\|reset` | Avalia um alerta adaptativo já emitido. |
| `ops similar "<texto>" [--limit 1..10]` | Procura incidentes anteriores parecidos com o texto. Padrão 5. |
| `ops plan research\|delegation\|operations [--effort quick\|standard\|deep] [--durable]` | Devolve um plano somente leitura. Esforço padrão `standard`. |
| `ops handoff TIPO ASSUNTO` | Cria um cartão Kanban de verificação bloqueado. `TIPO` é `resources`, `research`, `delegation`, `curator` ou `gateway`. |
| `ops schedule-spec [--every-minutes N] [--upstream-tag TAG]` | Imprime uma receita de cron sem agente para `ops tick --notify-only`. |
| `ops rollback` | Encerra um ensaio ativo no Linux e restaura o valor anterior. Sem ensaio, devolve `idle`. |
| `usage [--days 1..35]` | Mostra as medições dos hooks e a contabilidade nativa do Hermes em campos separados. |

`ops inspect` e `ops tick` aceitam `--days 1..35`, `--upstream-tag`, `--hermes-source` e `--sensitivity low|balanced|high`. `--upstream-tag` é uma referência informada pelo operador para comparação offline; nada é baixado. Dentro do Hermes, `--hermes-source` usa por padrão a árvore do Hermes em execução.

Com `--notify-only`, o `ops tick` não imprime nada quando não há novidade. Caso contrário, imprime `alerts`, `host_action` (só quando um ensaio vira `active`, `rolled_back`, `released` ou `not_applied`), `maintenance_changes` e `evolution`: as lições novas, os rascunhos do refletor, as regressões cujo veredito ficou final, as lições aposentadas e as propostas de risco alto da rodada, ou `null`.

### Fora do Hermes

Para rodar sem habilitar o plugin, ou quando a instalação usa `plugins.isolation: host` (em que o Hermes ignora os comandos de CLI de plugins), execute a partir da raiz do projeto extraído:

```sh
python -m hermes_atlas_evolution --home /caminho/do/perfil --hermes-version 0.23.1 \
  ops inspect --upstream-tag v0.23.1
```

O `--home` usa `HERMES_HOME` por padrão. O executor independente lê o `config.yaml` do perfil (até 1 MiB) quando o PyYAML está instalado, para obter o provedor de memória, os limites, as configurações de Cron, Kanban e delegação, o `display.language` e as configurações do próprio plugin. Sem o PyYAML, usa os padrões. `--hermes-source` acrescenta ao inventário a árvore de plugins e skills distribuída com o Hermes. O executor nunca cria um `--home` inexistente: os comandos que abrem os bancos do Evolution falham, o `health` informa `profile_present: false`, e `ops history`, `ops similar`, `ops rollback`, `ops plan`, `ops schedule-spec` e `ops handoff` terminam sem erro. O refletor do ciclo de evolução não roda no executor independente, nem os hooks, o autopilot, o relógio dele e a Proteção do Hermes, que dependem do plugin carregado pelo Hermes.

## Diagnósticos e alertas adaptativos

Cada diagnóstico apresenta hipótese, evidências, alternativas, confiança e ação proposta.

O aprendizado adaptativo agrupa as observações em faixas de seis horas UTC e compara a amostra atual com a mesma faixa dos dias anteriores, numa janela de 28 dias, com estatística robusta (mediana ponderada com meia-vida de 7 dias e desvio absoluto mediano), depois de pelo menos 5 amostras. Considera RAM disponível, pressão de CPU, memória e I/O, tokens de prompt, tempo de pesquisa, taxas de erro de ferramenta e de API e profundidade da fila Kanban, conforme a cobertura. Só o `ops tick` grava amostras.

- `--sensitivity low|balanced|high`, ou a chave `sensitivity`, ajusta o limiar. O padrão é `balanced`, e a opção da linha de comando vence a chave.
- `ops feedback CÓDIGO helpful|dismissed|reset` ajusta a recorrência dos alertas. Um alerta dispensado fica em silêncio por 14 dias. Os demais voltam no máximo a cada 12 horas, ou a cada 6 horas quando avaliados como úteis, e um aumento de severidade é entregue na hora. Alertas críticos são sempre entregues, no máximo uma vez por hora.
- Um diagnóstico que não propõe ação, como um cache de página saudável, continua no relatório e não é enviado como aviso.

`ops similar` compara incidentes com termos e trigramas bilíngues. A chave `semantic_incident_model` pode indicar o caminho absoluto do diretório de um modelo multilíngue local do SentenceTransformers, já instalado em formato `safetensors`. Nesse caso o comando usa CPU e arquivos locais, com código remoto e downloads desativados. A similaridade ajuda a consultar incidentes. A decisão de remediação usa medições checáveis e regras independentes.

## Uso e custo

`usage` apresenta as medições dos hooks (`observer_usage`) e a contabilidade nativa (`hermes_native_billing`) em campos distintos, com a cobertura dos dados e a distinção entre custos efetivos e estimados.

- As falhas do provedor são classificadas a partir do veredito que o Hermes dá a cada tentativa que falha (limite de taxa, autenticação, tempo esgotado, falha do provedor), e toda tentativa falha de uma requisição repetida é contada. Uma ferramenta que estoura o prazo conta como tentativa falha.
- `rates_usd_per_million` é uma tabela por provedor e por modelo, com os valores `input`, `output`, `cache_read` e `cache_write`, cada um de 0 a 1.000.000. As chaves são comparadas com o nome do provedor e com o modelo devolvido pelo provedor, em minúsculas, então escreva-as em minúsculas. Os quatro valores são obrigatórios para que a linha seja precificada. Tokens de raciocínio não têm preço próprio.
- `daily_estimated_budget_usd` é um limite opcional, maior que 0 e no máximo 1.000.000, que usa o dia UTC e a parcela observada e precificada. Quando a estimativa chega ao limite, o `tick` informa o achado `evolution_daily_budget`. Ele não interrompe o Hermes nem nenhuma chamada de modelo. Uma estimativa parcial pode divergir da fatura.

```json
{
  "rates_usd_per_million": {
    "example-provider": {
      "example-model": {"input": 1.0, "output": 2.0, "cache_read": 0.1, "cache_write": 1.25}
    }
  },
  "daily_estimated_budget_usd": 5
}
```

O provedor, o modelo e os preços acima são apenas ilustrativos.

## Pesquisa, delegação e agendamento

O `ops plan` devolve um plano somente leitura com um orçamento sugerido: consultas de busca, extrações de página e frentes paralelas para pesquisa; frentes paralelas e rodadas de revisão para delegação; e amostras comparáveis para operações. Planos de pesquisa incluem descoberta de fontes, verificação de evidência e registro de incertezas. Planos de delegação indicam `delegate_task` para respostas que o agente atual precisa aguardar e Kanban para trabalhos que precisam sobreviver a reinícios (`--durable`).

Ligue `agent_tools` nas configurações do plugin no Hermes (ou com `hermes config set plugins.entries.hermes-atlas-evolution.settings.agent_tools true` no seu próprio terminal) e depois rode `hermes gateway restart`: o plugin registra `hermes_atlas_evolution_lessons` para o perfil quando carrega. Desligar vale na hora. Inclua `hermes-atlas-evolution` quando o perfil usa uma lista explícita de toolsets. (A ferramenta de planos `hermes_atlas_evolution_plan` saiu na 0.10.0: devolvia um texto fixo e não lia dado nenhum; `hermes evolution ops plan` continua imprimindo o mesmo plano.)

A leitura dos quadros Kanban compartilhados exige `"inspect_shared_kanban": true`. A criação de cartão exige a sua própria autorização de encaminhamento, independente dessa chave. O exemplo associa as verificações ao perfil `reviewer` no quadro existente `ops`.

```json
{
  "inspect_shared_kanban": true,
  "kanban_handoff": {"enabled": true, "board": "ops", "assignee": "reviewer"}
}
```

```sh
hermes evolution ops handoff resources "cpu-spikes"
hermes evolution ops schedule-spec --every-minutes 5 --upstream-tag v0.23.1
```

O `handoff` cria no Kanban nativo um cartão bloqueado, destinado à verificação, com chave de idempotência por perfil, quadro, responsável, tipo, assunto e dia UTC. O operador libera o cartão depois de revisar o escopo. O assunto é um identificador de até 80 caracteres. O comando não faz nada dentro de um worker Kanban ou de uma tarefa delegada, e precisa do `hermes` no `PATH`.

O `schedule-spec` entrega o script e os argumentos do Cron nativo para agendar `ops tick --notify-only` em modo `no_agent` como a tarefa agendada do Hermes (cron) `hermes-atlas-evolution-watchdog`, com saída local e sem abrir uma sessão de modelo. O intervalo vai de 5 a 1.440 minutos. Nada é gravado. Revise o script e aplique a receita no perfil escolhido. O tick usa a política Linux desse perfil. Use o intervalo de cinco minutos em modo `apply`, para que os ensaios sejam verificados e encerrados.

## Remediação no Linux

Por padrão, o supervisor observa. A configuração abaixo autoriza um ensaio em **uma unidade auxiliar** não crítica, no mesmo `slice` do Hermes, em systemd com cgroup v2. Escolha uma tarefa de segundo plano cujo desempenho possa cair temporariamente sob concorrência. O modo `observe` permite medir os dois serviços primeiro. O modo `apply` autoriza o ajuste já delimitado. Fora do Linux, esse código fica inerte.

Salve a configuração em `<HERMES_HOME>/evolution/settings.json`.

```json
{
  "linux_supervisor": {
    "mode": "observe",
    "scope": "user",
    "hermes_unit": "hermes-gateway.service",
    "background_unit": "my-background-indexer.service",
    "resources": ["cpu", "io", "memory"],
    "memory_high_bytes": 536870912
  }
}
```

| Chave | Valores | Padrão |
|---|---|---|
| `mode` | `observe` ou `apply` | `observe` |
| `scope` | `user` ou `system` | `user` |
| `hermes_unit`, `background_unit` | dois nomes diferentes de unidade `.service` | obrigatórias |
| `resources` | qualquer combinação de `cpu`, `io` e `memory` | `["cpu"]` |
| `memory_high_bytes` | número inteiro de MiB, no mínimo 128 MiB | obrigatória quando `memory` está na lista |

**Evidência exigida.** Um ensaio só começa com o perfil presente, o `state.db` saudável, nenhuma falha crítica ou de gateway informada e um diagnóstico que proponha reequilibrar aquele recurso. O supervisor então confirma duas amostras separadas por 60 segundos a 30 minutos, a identidade dos dois serviços, pressão PSI no cgroup do Hermes e participação de pelo menos 65% da unidade auxiliar na atividade do par medido. Para I/O, exige atividade no mesmo dispositivo. Usa os controladores disponíveis, peso inicial efetivo 100 e identificação inequívoca das unidades.

**O ajuste.** Em CPU e I/O, o ajuste define temporariamente `CPUWeight` ou `IOWeight` da unidade auxiliar como 50. Em memória, o limite `MemoryHigh` precisa ser explícito, e o supervisor exige cache limpo recuperável, margem sobre a memória de trabalho e ausência de sinais de swap e OOM. Escolha `memory_high_bytes` com base na unidade real. O exemplo só demonstra o formato.

**Verificação e reversão.** Cada ajuste usa `systemctl --runtime`. O supervisor registra a intenção antes de aplicar, confere o valor efetivo, mede a resposta e restaura a configuração anterior se o efeito ficar incerto ou a pressão não cair pelo menos 20%. O próximo tick depois de 15 minutos encerra o ensaio. Depois de qualquer ensaio, nenhum outro começa por uma hora. Um ensaio ineficaz ou não verificado bloqueia a mesma unidade e o mesmo recurso por 24 horas, e ensaios ineficazes repetidos em 14 dias adiam aquele recurso por 24, 48 e depois 96 horas. O comando só usa as permissões que o processo já tem. Ajustes externos observados têm precedência.

Programe `hermes evolution ops tick` **a cada cinco minutos** no agendador existente, com o usuário que administra essas unidades. A aplicação exige que os ticks de verificação também estejam rodando. Para encerrar um ensaio antes da hora, rode `hermes evolution ops rollback`. Use `ops history` para confirmar que o peso foi restaurado. O ajuste é temporário no systemd e é desfeito quando o host reinicia. O supervisor recomenda, e não aplica, limpeza de dados, mudanças em plugins, mudanças de memória e correções de código; o que o plugin muda sozinho no Hermes está em [O modo e as ações automáticas](evolution-loop.md#o-modo-e-as-ações-automáticas).

Os cenários de mudança e reversão de peso são testados com um adaptador de systemd simulado. Valide o controlador e a unidade escolhida em homologação antes de autorizar o modo `apply`.

## Chaves de configuração

O `<HERMES_HOME>/evolution/settings.json` é um objeto JSON opcional de até 64 KiB. Uma mudança vale no gateway em execução em até um minuto, sem reiniciar: o plugin compara o horário de modificação dele no máximo a cada 30 segundos. Cada rodada do autopilot e cada comando o leem de novo. As opções do ciclo de evolução estão descritas em [O ciclo de evolução](evolution-loop.md#configuração-e-idioma). As que aparecem no formulário de configurações do Hermes vencem as mesmas chaves deste arquivo. As ferramentas de arquivo do agente conseguem gravar este arquivo, então valores que ampliariam a confiança (`owner_sender_ids`, `owner_platforms`, `agent_advice: true`, `runtime_guard: false`, um `mode` diferente de `manual`, `legacy_memory_commands: true`, `automatic_shared_recall: true`) são ignorados aqui; defina-os no formulário de configurações do Hermes ou com `hermes config set`.

| Chave | Formato | Padrão | Efeito |
|---|---|---|---|
| `evolution` | objeto | veja o ciclo | `lessons`, `language`, `tool_hints`, `feedback_signals`, `slow_turn_seconds`, `reflector`, `reflector_daily_limit`, `autopilot_minutes`, `mode` (só `manual` vale aqui); `owner_sender_ids`, `owner_platforms` e `runtime_guard: false` são ignorados aqui e só valem pelo formulário do Hermes |
| `agent_advice` | booleano | `false` | Ignorado quando `true`: as ferramentas do agente só são ligadas pelo formulário do Hermes (`agent_tools`). |
| `sensitivity` | `low`, `balanced`, `high` | `balanced` | Limiar dos alertas adaptativos. |
| `rates_usd_per_million` | objeto por provedor e modelo | nenhum | Preços para estimar custos (veja [Uso e custo](#uso-e-custo)). |
| `daily_estimated_budget_usd` | número acima de 0, até 1.000.000 | nenhum | Limite diário (UTC) sobre a estimativa precificada. |
| `inspect_shared_kanban` | booleano | `false` | Inclui os quadros Kanban compartilhados no diagnóstico. |
| `kanban_handoff` | objeto: `enabled`, `board`, `assignee` | desligado | Autoriza o `ops handoff`. `board` e `assignee` são identificadores em minúsculas. |
| `linux_supervisor` | objeto | ausente | Política dos ensaios reversíveis no systemd (veja [Remediação no Linux](#remediação-no-linux)). |
| `semantic_incident_model` | caminho absoluto | nenhum | Modelo local de embeddings para o `ops similar`. |
| `legacy_memory_commands` | booleano | `false` | Permite novas notas antigas e o comando de chat `/evolution`. Ignorado aqui quando `true`: defina com `hermes config set plugins.entries.hermes-atlas-evolution.settings.legacy_memory_commands true`. |
| `automatic_shared_recall` | booleano | `false` | Com `legacy_memory_commands`, injeta no turno as notas antigas compartilhadas que casarem. Ignorado aqui quando `true`, como o `legacy_memory_commands`: defina na configuração do Hermes. |

## Dados e compatibilidade

- Os bancos ficam em `<HERMES_HOME>/evolution/`: `evolution.sqlite3`, com as métricas do observador e o ciclo de evolução, e `supervisor.sqlite3`, com incidentes, linhas de base, amostras adaptativas e ensaios. As métricas do observador ficam por 35 dias (os ids de entrega que evitam contar um evento duas vezes, 2 dias); os agregados do ciclo, os incidentes e os ensaios encerrados, por 90 dias. A retenção de 35 dias é aplicada pela primeira gravação de hook de cada dia UTC em cada processo, então vale mesmo sem `tick` agendado. Backups e logs próprios do Hermes seguem a retenção que já têm.
- O `evolution.sqlite3` está no esquema 8. Esquemas mais antigos são migrados automaticamente, e um esquema mais novo também abre, porque o plugin só acrescenta tabelas e colunas. Uma cópia muito antiga do plugin não abre um banco migrado: voltar até ela exige restaurar um backup do arquivo ou apagá-lo. A pasta de dados `<HERMES_HOME>/evolution/` manteve o nome quando o plugin foi renomeado.
- As gravações dos hooks compartilham uma conexão por processo e por perfil, que fica aberta enquanto o Hermes roda. No Windows, isso significa que o `evolution.sqlite3` não pode ser apagado enquanto o Hermes está rodando.
- Os bancos do próprio Hermes (`state.db`, quadros Kanban) são abertos somente para leitura. Para o `status`, isso inclui o `state.db` dos outros perfis da mesma raiz, onde ele só conta sessões, e o banco do plugin nesses perfis, onde ele lê a última rodada e as contagens de lições.
- Os arquivos das notas antigas são preservados. Os comandos que registram novas notas e o comando de chat legado exigem `legacy_memory_commands: true`. A injeção antiga de notas também exige `automatic_shared_recall: true`. Por padrão, a inspeção usa os sinais da memória nativa do Hermes.
- O contrato de integração foi verificado na árvore oficial v0.23.1, commit `3b421578588243f920886e6fe79a6faa87ea026e`.

## Comandos antigos

| Comando | O que faz |
|---|---|
| `health [--upstream-tag TAG]` | Imprime o instantâneo dos arquivos do Hermes e dos sinais do host em que o `ops inspect` se apoia. |
| `tick [--upstream-tag TAG] [--days 1..35] [--changes-only]` | Roda a política de manutenção original e registra as decisões. O `ops tick` já a inclui. |
| `feedback CÓDIGO helpful\|dismissed\|reset` | Registra como um achado de manutenção se saiu. |
| `history` | Mostra as prioridades de manutenção e o feedback. |
| `memory [--shared \| --dm PLATAFORMA USUÁRIO CHAT] ...` | Acesso aos dados das notas antigas: `remember`, `recall`, `evaluate`, `audit`, `forget`. |

## Como retirar o plugin

Execute `hermes evolution ops rollback` antes de desabilitar o plugin com `hermes plugins disable hermes-atlas-evolution` ou de removê-lo com `hermes plugins remove hermes-atlas-evolution`. Desabilitar faz um gateway em execução descarregar o plugin; depois de uma remoção, rode `hermes gateway restart`. O plugin é instalado por perfil, então repita isso em cada perfil que o tiver (`hermes -p <perfil> ...`). Nenhum dos dois comandos apaga os dados em `<HERMES_HOME>/evolution/`; apague essa pasta você mesmo se quiser que eles sumam. No Modo Auto, o perfil que cuida do Hermes inteiro lembra onde o plugin já esteve e nunca o reinstala lá sozinho.

## Referências

[Hermes Agent v0.23.1](https://github.com/NousResearch/hermes-agent/releases/tag/v0.23.1) · [Hooks do Hermes](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/developer-guide/observer-hooks.md) · [Linux PSI](https://docs.kernel.org/accounting/psi.html) · [Linux cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html) · [systemctl](https://www.freedesktop.org/software/systemd/man/latest/systemctl.html) · [SentenceTransformers](https://sbert.net/docs/package_reference/sentence_transformer/model.html)
