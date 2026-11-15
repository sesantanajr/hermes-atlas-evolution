# Supervisor de operações

[English](../operations.md) | **Português (Brasil)**

Além do [ciclo de evolução](evolution-loop.md), o Atlas Evolution supervisiona o host e a carga de um perfil do Hermes. Ele examina o que o Hermes grava, mede a carga do agente e do Linux, e cruza evidências para sugerir testes e conduzir remediações reversíveis. Por padrão, só observa. Usa apenas os hooks públicos do Hermes e preserva o seu provedor de memória, os seus plugins e o seu roteador.

Todos os comandos abaixo são subcomandos de `hermes evolution`. Os nomes de campos e os códigos da saída em JSON estão em inglês. O texto das hipóteses e recomendações sai quase todo em português nesta versão.

## Conteúdo

1. [O que o supervisor mede](#o-que-o-supervisor-mede)
2. [Comandos](#comandos)
3. [Diagnósticos e alertas adaptativos](#diagnósticos-e-alertas-adaptativos)
4. [Uso e custo](#uso-e-custo)
5. [Pesquisa, delegação e agendamento](#pesquisa-delegação-e-agendamento)
6. [Remediação no Linux](#remediação-no-linux)
7. [Dados e compatibilidade](#dados-e-compatibilidade)
8. [Comandos mantidos de versões anteriores](#comandos-mantidos-de-versões-anteriores)
9. [Como retirar o plugin](#como-retirar-o-plugin)

## O que o supervisor mede

| Área | Evidência coletada | Interpretação |
|---|---|---|
| Linux | RAM disponível, cache recuperável, swap, PSI, limites de cgroup, CPU, I/O, volume, inodes e áreas conhecidas do perfil | Separa cache útil de pressão de memória. Contadores acumulados descrevem histórico. PSI mede tempo com tarefas esperando por recursos. |
| Hermes | Estado do gateway, legibilidade e estrutura da memória nativa, integridade SQLite e categorias de erros em logs | Cruza falhas de gravação com espaço, inodes e bloqueios. Indica quando uma falha pede reprodução no Hermes, no provedor ou no plugin. A checagem de integridade tem limite de 64 MiB e prazo cooperativo de 300 ms. |
| Plugins e skills | Tamanho, estado configurado, evidência recente de uso quando atribuível, duplicatas e variantes de skills | Uma amostra sem uso gera uma hipótese de revisão. Hooks, comandos e provedores podem atuar sem deixar uma chamada de ferramenta atribuível. |
| Pesquisa e tokens | Duração de turnos concluídos com ferramentas web conhecidas, erros de ferramenta, tentativas, tokens por classe e cache | Distingue pesquisa longa, falha de ferramenta, limite de provedor e pouco reaproveitamento de contexto. O p95 de pesquisa é aproximado por faixas. |
| Delegação e manutenção | Eventos de subagentes, compactação, execuções do Curator e saídas de workers Kanban | Mostra falhas recorrentes e a cobertura de tokens antes e depois da compactação, quando o Hermes informa. |
| Fila e agendamento | Picos de disparos do Cron, falhas de execução e de entrega, rotinas que não puderam ser agendadas ou disparadas, itens prontos e bloqueados, cartões parados em `scheduled` e sinais de lease vencida no Kanban | Ajuda a identificar congestionamento e a escolher entre tarefas curtas, trabalho durável e scripts sem modelo. |

O coletor guarda agregados. Não registra texto de conversas, conteúdo da memória nativa, resultados de busca nem linhas brutas de log.

A versão 0.4.0 acrescentou estes campos à saída:

| Campo | Onde | Significado |
|---|---|---|
| `dropped_events` | `usage` e o bloco `usage` de `ops inspect` e `ops tick` | Eventos de hook descartados porque a fila de gravação estava cheia. Nesse caso, os totais são um limite inferior. |
| `tool_result_error` | `health`, em `logs.events` | Linhas de log que apenas repetem a saída de uma ferramenta que falhou. Elas não contam mais como falha de armazenamento, de memória ou de plugin. |
| `schedule_errors`, `recent_fire_errors` | `workloads.cron.counts` | Rotinas que o Hermes não conseguiu agendar, e disparos vencidos nas últimas 24 horas que nunca chegaram ao executor. |
| `scheduled` | `workloads.kanban.counts` | Cartões aguardando na coluna `scheduled`. |
| `served_by_host_gateway` | `health.gateway` | O perfil é um perfil nomeado atendido pelo gateway raiz, cujo registro é usado nas checagens. |

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
| `ops inspect` | Coleta e explica os sinais do momento. Não altera nada. |
| `ops tick [--notify-only]` | Coleta, compara com o histórico, registra hipóteses e executa somente as ações habilitadas pela configuração. Também roda uma passada do ciclo de evolução, no máximo uma vez por hora, e a devolve no campo `evolution`. Com `--notify-only`, imprime só alertas novos, transições de ação no host, mudanças de manutenção e, da passada, lições novas, regressões, lições aposentadas e propostas de risco alto. |
| `ops history` | Mostra os incidentes e o estado de cada tentativa. |
| `ops review [--days N]` | Cruza as medições adaptativas, o feedback e os resultados dos ensaios. Padrão de 14 dias, até 90. |
| `ops feedback CODIGO helpful\|dismissed\|reset` | Avalia um alerta adaptativo. |
| `ops similar "<texto>" [--limit 1..10]` | Procura incidentes anteriores parecidos com o texto. |
| `ops plan research\|delegation\|operations [--effort quick\|standard\|deep] [--durable]` | Devolve um plano, somente leitura. |
| `ops handoff TIPO ASSUNTO` | Cria um cartão Kanban de verificação, bloqueado. `TIPO` é `resources`, `research`, `delegation`, `curator` ou `gateway`. |
| `ops schedule-spec [--every-minutes N] [--upstream-tag TAG]` | Imprime uma receita de cron sem agente para o `ops tick --notify-only`. |
| `ops rollback` | Encerra um ensaio ativo no Linux e restaura o valor anterior. |
| `usage [--days 1..35]` | Apresenta as medições dos hooks e a contabilidade nativa do Hermes em campos distintos. |

`ops inspect` e `ops tick` aceitam `--days 1..35`, `--upstream-tag`, `--hermes-source` e `--sensitivity low|balanced|high`. `--upstream-tag` é uma referência informada pelo operador para comparação offline; nada é baixado.

### Fora do Hermes

Para rodar sem ativar o plugin, ou quando a instalação usa `plugins.isolation: host` (em que o Hermes ignora os comandos de CLI dos plugins), execute a partir da raiz extraída do projeto:

```sh
python -m hermes_evolution --home /caminho/do/perfil --hermes-version 0.23.1 \
  ops inspect --upstream-tag v0.23.1
```

O executor independente lê o `config.yaml` do perfil quando o PyYAML está instalado, para o provedor de memória, os limites e as configurações de Cron, Kanban e delegação. Sem o PyYAML, usa os padrões. `--hermes-source` inclui no inventário a árvore de plugins e skills distribuídos com o Hermes. Um `--home` que não existe é erro; o executor não cria a pasta.

## Diagnósticos e alertas adaptativos

Cada diagnóstico apresenta hipótese, evidências, alternativas, confiança e ação proposta.

O aprendizado adaptativo agrupa as observações em faixas de seis horas UTC e compara a amostra atual com a mesma faixa dos dias anteriores, em uma janela de 28 dias, com estatística robusta (mediana ponderada e desvio absoluto mediano), depois de pelo menos 5 amostras. Considera RAM disponível, pressão de CPU, memória e I/O, tokens de prompt, tempo de pesquisa, taxas de erro de ferramenta e de API e profundidade da fila, conforme a cobertura.

- `--sensitivity low|balanced|high`, ou a chave `sensitivity`, ajusta o limiar. O padrão é `balanced`.
- `ops feedback CODIGO helpful|dismissed|reset` ajusta a recorrência dos alertas. Um alerta dispensado fica em silêncio por 14 dias. Os demais voltam no máximo a cada 12 horas, ou a cada 6 horas quando avaliados como úteis. Alertas críticos são sempre entregues, no máximo uma vez por hora.
- Um diagnóstico que não propõe ação, como um cache de página saudável, continua no relatório e não é enviado como aviso.

`ops similar` compara incidentes com termos e trigramas bilíngues. A chave `semantic_incident_model` pode indicar o caminho absoluto de um modelo multilíngue local do SentenceTransformers, já instalado e provisionado em formato `safetensors`. Nesse caso o comando usa CPU e arquivos locais, com código remoto e downloads desativados. A similaridade ajuda a consultar incidentes. A decisão de remediação usa medições checáveis e regras independentes.

## Uso e custo

`usage` apresenta as medições dos hooks e a contabilidade nativa em campos distintos, com a cobertura dos dados e a distinção entre custos efetivos e estimados.

- As falhas do provedor são classificadas a partir do veredito que o Hermes dá a cada tentativa que falha (limite de taxa, autenticação, tempo esgotado, falha do provedor), e toda tentativa falha de uma requisição repetida é contada. Uma ferramenta que estoura o prazo conta como tentativa falha.
- `rates_usd_per_million` é uma tabela por provedor e por modelo, com os valores `input`, `output`, `cache_read` e `cache_write`. As chaves são comparadas com o nome do provedor e com o modelo devolvido pelo provedor, em minúsculas. Os quatro valores são obrigatórios para que a linha seja precificada.
- `daily_estimated_budget_usd` é um limite opcional que usa o dia UTC e a parcela observada e precificada. Quando a estimativa chega ao limite, o `tick` informa o achado `evolution_daily_budget`. Ele não interrompe o Hermes nem nenhuma chamada de modelo. Uma estimativa parcial pode divergir da fatura.

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

`ops plan` propõe um orçamento de consultas, páginas e tarefas paralelas. Planos de pesquisa incluem descoberta de fontes, verificação de evidência e registro de incertezas. Planos de delegação indicam `delegate_task` para respostas que o agente atual precisa aguardar e Kanban para trabalhos que precisam sobreviver a reinícios.

Ative `"agent_advice": true` em `<HERMES_HOME>/evolution/settings.json` e recarregue o plugin para liberar `atlas_evolution_plan` ao perfil. Inclua `atlas-evolution` quando o perfil usa uma lista explícita de toolsets. A ferramenta retorna apenas o plano.

A leitura compartilhada dos quadros Kanban exige `"inspect_shared_kanban": true`. A criação de cartão exige também a sua própria autorização de encaminhamento. O exemplo associa as verificações ao perfil `reviewer` no quadro existente `ops`.

```json
{
  "agent_advice": true,
  "inspect_shared_kanban": true,
  "kanban_handoff": {"enabled": true, "board": "ops", "assignee": "reviewer"}
}
```

```sh
hermes evolution ops handoff resources "cpu-spikes"
hermes evolution ops schedule-spec --every-minutes 5 --upstream-tag v0.23.1
```

`handoff` cria um cartão bloqueado, destinado à verificação, com chave de idempotência, no Kanban nativo. O operador libera o cartão depois de revisar o escopo. O assunto é um identificador de até 80 caracteres.

`schedule-spec` entrega o script e os argumentos do Cron nativo para agendar `ops tick --notify-only` em modo `no_agent`, com saída local e sem abrir uma sessão de modelo. O intervalo vai de 5 a 1.440 minutos. Revise o script e aplique a receita no perfil escolhido. O ciclo usa a política Linux desse perfil. Use o intervalo de cinco minutos em modo `apply`, para que os ensaios sejam verificados e encerrados.

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

**Evidência exigida.** O supervisor confirma duas amostras separadas por pelo menos 60 segundos, a identidade dos serviços, pressão PSI no cgroup do Hermes e participação de pelo menos 65% da unidade auxiliar na atividade do par medido. Para I/O, exige atividade no mesmo dispositivo. Usa os controladores disponíveis, peso inicial efetivo 100 e identificação inequívoca das unidades.

**O ajuste.** Em CPU e I/O, o ajuste define temporariamente `CPUWeight` ou `IOWeight` da unidade auxiliar como 50. Em memória, o limite `MemoryHigh` precisa ser explícito, e o supervisor exige cache limpo recuperável, margem sobre a memória de trabalho e ausência de sinais de swap e OOM. Escolha `memory_high_bytes` com base na unidade real. O exemplo só demonstra o formato.

**Verificação e reversão.** Cada ajuste usa `systemctl --runtime`. O supervisor registra a intenção antes de aplicar, confere o valor efetivo, mede a resposta e restaura a configuração anterior se o efeito ficar incerto ou a pressão não cair pelo menos 20%. O próximo ciclo depois de 15 minutos encerra a tentativa. Há cooldown após ensaios ineficazes, e o comando só usa as permissões que o processo já tem. Ajustes externos observados têm precedência.

Programe `hermes evolution ops tick` **a cada cinco minutos** no agendador existente, com o usuário que administra essas unidades. A aplicação exige que os ciclos de verificação também estejam operacionais. Para encerrar uma tentativa antes da hora, rode `hermes evolution ops rollback`. Use `ops history` para confirmar que o peso foi restaurado. O ajuste é temporário no systemd e sai com o reboot do host. A limpeza de dados, o desligamento de plugins, as mudanças de memória e as correções de código são recomendações de revisão nesta versão.

Os cenários de mudança e reversão de peso são testados com um adaptador de systemd simulado. Valide o controlador e a unidade escolhida em homologação antes de autorizar o modo `apply`.

## Dados e compatibilidade

- Os bancos ficam em `<HERMES_HOME>/evolution/`: `evolution.sqlite3`, com as métricas do observador e o ciclo de evolução, e `supervisor.sqlite3`, com incidentes, linhas de base e ensaios. As métricas ficam por 35 dias, e os incidentes e ações encerradas por 90 dias. A retenção de 35 dias é aplicada pela primeira gravação de hook de cada dia UTC, então vale mesmo sem `tick` agendado. Backups e logs próprios do Hermes seguem a retenção que já têm.
- A versão 0.4.0 migra o `evolution.sqlite3` para o esquema 4 automaticamente. A versão 0.3.0 não abre um banco migrado: voltar de versão exige restaurar um backup do arquivo ou apagá-lo.
- As gravações dos hooks compartilham uma conexão por processo e por perfil. No Windows, isso significa que o `evolution.sqlite3` não pode ser apagado enquanto o Hermes está rodando.
- Os bancos do próprio Hermes (`state.db`, quadros Kanban) são abertos somente para leitura.
- Arquivos de notas da versão 0.1 são preservados. Os comandos que registram novas notas e o comando de chat legado exigem `legacy_memory_commands: true`. A injeção antiga de notas também exige `automatic_shared_recall: true`. Por padrão, a inspeção usa os sinais da memória nativa do Hermes.
- O contrato de integração foi verificado na árvore oficial v0.23.1, commit `3b421578588243f920886e6fe79a6faa87ea026e`.

## Comandos mantidos de versões anteriores

| Comando | O que faz |
|---|---|
| `health [--upstream-tag TAG]` | Imprime o instantâneo dos arquivos do Hermes e dos sinais do host em que o `ops inspect` se apoia. |
| `tick [--days 1..35] [--changes-only]` | Roda a política de manutenção da versão 0.2 e registra as decisões. O `ops tick` já a inclui. |
| `feedback CODIGO helpful\|dismissed\|reset` | Registra como um achado de manutenção se saiu. |
| `history` | Mostra as prioridades de manutenção e o feedback. |
| `memory ...` | Acesso aos dados de notas da 0.1: `remember`, `recall`, `evaluate`, `audit`, `forget`. |

## Como retirar o plugin

Execute `hermes evolution ops rollback` antes de desabilitar o plugin com `hermes plugins disable hermes-evolution`, e recarregue o gateway do perfil.

## Referências

[Hermes Agent v0.23.1](https://github.com/NousResearch/hermes-agent/releases/tag/v0.23.1) · [Hooks do Hermes](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/developer-guide/observer-hooks.md) · [Linux PSI](https://docs.kernel.org/accounting/psi.html) · [Linux cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html) · [systemctl](https://www.freedesktop.org/software/systemd/man/latest/systemctl.html) · [SentenceTransformers](https://sbert.net/docs/package_reference/sentence_transformer/model.html)
