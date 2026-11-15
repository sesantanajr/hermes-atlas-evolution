![Atlas carrying a celestial sphere over a black technical grid. Atlas Evolution.](docs/banner.jpg)

# Atlas Evolution

An independent community plugin for Hermes Agent. Atlas Evolution correlates Linux resource pressure with agent activity, detects Cron and Kanban congestion, learns from comparable observations, and coordinates bounded research and operational handoffs. Tested with **Hermes Agent 0.23.1**.

```sh
hermes plugins install sesantanajr/hermes-atlas-evolution --enable
hermes plugins doctor --ci hermes-evolution
hermes evolution ops inspect --upstream-tag v0.23.1
```

The installed plugin ID is `hermes-evolution`. Observation is the default. See [integration and permissions](docs/hermes-integration.md) for the Hermes surfaces, evidence and action boundaries.

## Autor

Criado por **Sérgio Sant'Ana Júnior**. [LinkedIn](https://www.linkedin.com/in/sergiosantanacloud/)

## Guia em português

Supervisor operacional open source para **Hermes Agent 0.23.1**, licenciado sob MIT. Examina o que o Hermes grava, mede a carga do agente e do Linux, e cruza evidências para sugerir testes, melhorar o desempenho e conduzir remediações reversíveis. Usa os ganchos públicos do Hermes e preserva seu provedor de memória, seus plugins e seu roteador. Requer Python 3.11 ou superior. O modo padrão usa somente a biblioteca padrão de Python, com nenhuma chamada adicional a modelos.

## Instalação e comandos

Use `hermes plugins install sesantanajr/hermes-atlas-evolution --enable` no perfil escolhido. Se necessário, use `hermes -p NOME` para selecionar o perfil. Confira o carregamento com `hermes plugins doctor --ci hermes-evolution`.

Para instalar manualmente, copie a pasta inteira do plugin, com `plugin.yaml` na raiz, para `<HERMES_HOME>/plugins/hermes-evolution/` do perfil e execute `hermes plugins enable hermes-evolution`. O padrão em Linux é `~/.hermes`. Instalações Windows geralmente usam `%LOCALAPPDATA%\hermes`. Use o procedimento de manutenção do perfil caso o gateway já esteja em execução.

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

`inspect` coleta e explica sinais no momento. `tick` coleta, compara com o histórico, registra hipóteses e executa somente ações habilitadas pela configuração. `history` mostra incidentes e o estado de cada tentativa. `review` cruza as medições adaptativas, o feedback e os resultados de ensaios. `usage` apresenta as medições dos hooks e a contabilidade nativa em campos distintos, com cobertura de dados e distinção entre custos efetivos e estimados. `--upstream-tag` é uma referência informada pelo operador para comparação offline.

Para rodar sem ativar o plugin, ou quando a instalação usa `plugins.isolation: host`, execute da raiz extraída do projeto. Nesse modo, `--hermes-source` também permite incluir a árvore de plugins e skills distribuídos pelo Hermes.

```sh
python -m hermes_evolution --home /caminho/do/perfil --hermes-version 0.23.1 \
  ops inspect --upstream-tag v0.23.1
```

## O que o supervisor mede

| Área | Evidência coletada | Interpretação |
|---|---|---|
| Linux | RAM disponível, cache recuperável, swap, PSI, limites de cgroup, CPU, I/O, volume, inodes e áreas conhecidas do perfil | Separa cache útil de pressão de memória. Contadores acumulados descrevem histórico. PSI mede tempo com tarefas esperando por recursos. |
| Hermes | Gateway, legibilidade e estrutura da memória nativa, integridade SQLite e categorias de erros em logs | Cruza falhas de gravação com espaço, inodes e bloqueios. Indica quando uma falha pede reprodução no Hermes, no provedor ou no plugin. A checagem de integridade tem limite de 64 MiB e prazo cooperativo de 300 ms. |
| Plugins e skills | Tamanho, estado configurado, evidência recente de uso quando atribuível, duplicatas e variantes de skills | Uma amostra sem uso gera uma hipótese de revisão. Hooks, comandos e provedores podem atuar sem deixar uma chamada de ferramenta atribuível. |
| Pesquisa e tokens | Duração de turnos concluídos com ferramentas web conhecidas, erro de ferramenta, tentativas, tokens por classe e cache | Distingue pesquisa longa, falha de ferramenta, limite de provedor e pouco reaproveitamento de contexto. O p95 de pesquisa é aproximado por intervalos. |
| Delegação e manutenção | Eventos de subagentes, compactação, Curator e saídas de workers Kanban | Mostra falhas recorrentes e cobertura de tokens antes e depois da compactação quando informada pelo Hermes. |
| Fila e agendamento | Picos de disparos do Cron, falhas de execução e entrega, itens prontos, bloqueios e sinais de lease vencida no Kanban | Ajuda a identificar congestionamento e a escolher entre tarefas curtas, trabalho durável e scripts sem modelo. |

Cada diagnóstico apresenta hipótese, evidências, alternativas, confiança e ação proposta. O aprendizado agrupa observações em faixas de seis horas UTC e compara horários equivalentes com estatística robusta após amostragem suficiente. Considera RAM disponível, pressão de CPU, memória e I/O, tokens, pesquisa, falhas e profundidade da fila, conforme a cobertura. Use `--sensitivity low|balanced|high` ou a chave `sensitivity` para ajustar o limiar. `ops feedback CODIGO helpful|dismissed|reset` ajusta a recorrência de alertas, preservando a sinalização crítica. O coletor guarda agregados, sem registrar texto de conversas, conteúdo da memória nativa, resultados de busca ou linhas brutas de log.

`ops similar` compara incidentes com termos e trigramas bilíngues. A opção `semantic_incident_model` pode indicar um modelo multilíngue local do SentenceTransformers já instalado e provisionado em formato `safetensors`. O comando usa CPU e arquivos locais, com código remoto e downloads desativados. A similaridade ajuda a consultar incidentes. A decisão de remediação usa medições checáveis e regras independentes.

## Pesquisa, delegação e agendamento

`ops plan` propõe um orçamento de consultas, páginas e tarefas paralelas. Planos de pesquisa incluem descoberta de fontes, verificação de evidência e registro de incertezas. Planos de delegação indicam `delegate_task` para respostas que o agente atual precisa aguardar e Kanban para trabalhos que precisam sobreviver a reinícios. Ative `"agent_advice": true` em `<HERMES_HOME>/evolution/settings.json` e recarregue o plugin para liberar `atlas_evolution_plan` ao perfil. Inclua `atlas-evolution` quando o perfil usa uma lista explícita de toolsets. A ferramenta retorna apenas o plano.

A leitura compartilhada dos quadros Kanban exige `"inspect_shared_kanban": true`. A criação de cartão exige também configurar a sua própria autorização de encaminhamento. O exemplo associa as verificações ao perfil `reviewer` no quadro existente `ops`.

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

`handoff` cria um cartão bloqueado, destinado à verificação, com chave de idempotência no Kanban nativo. O operador libera o cartão após revisar o escopo. `schedule-spec` entrega o script e os argumentos do Cron nativo para agendar `ops tick --notify-only` em modo `no_agent`, com saída local e sem abrir uma sessão de modelo. Revise o script e aplique a receita no perfil escolhido. O ciclo usa a política Linux desse perfil. Use o intervalo de cinco minutos em modo `apply` para verificar e encerrar os ensaios.

## Remediação no Linux

Por padrão, o supervisor observa. A configuração abaixo autoriza um ensaio em **uma unidade auxiliar** não crítica, no mesmo `slice` do Hermes, em systemd com cgroup v2. Escolha uma tarefa de segundo plano cujo desempenho possa cair temporariamente sob concorrência. O modo `observe` permite medir os dois serviços primeiro. O modo `apply` autoriza o ajuste já delimitado.

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

O supervisor confirma duas amostras separadas por pelo menos 60 segundos, identidade dos serviços, pressão PSI no cgroup do Hermes e participação de pelo menos 65% da unidade auxiliar na atividade do par medido. Para I/O, exige atividade no mesmo dispositivo. Usa os controladores disponíveis, peso inicial efetivo 100 e identificação inequívoca das unidades.

Em CPU e I/O, o ajuste define temporariamente `CPUWeight` ou `IOWeight` da unidade auxiliar como 50. Em memória, o limite `MemoryHigh` precisa ser explícito e o supervisor exige cache limpo recuperável, margem sobre a memória de trabalho e ausência de sinais de swap e OOM. Escolha `memory_high_bytes` com base na unidade real. O exemplo só demonstra o formato. Cada ajuste usa `systemctl --runtime`. O supervisor registra a intenção antes de aplicar, confere o valor efetivo, mede a resposta e restaura a configuração anterior se o efeito ficar incerto ou a pressão não cair pelo menos 20%. O próximo ciclo depois de 15 minutos encerra a tentativa. Há cooldown após ensaios ineficazes e o comando só usa as permissões do processo. Ajustes externos observados têm precedência.

Programe `hermes evolution ops tick` **a cada cinco minutos** no agendador existente, com o usuário que administra essas unidades. A aplicação exige que os ciclos de verificação também estejam operacionais. Para encerrar uma tentativa antecipadamente, rode `hermes evolution ops rollback`. Use `ops history` para confirmar que o peso foi restaurado. O ajuste é temporário no systemd e sai com o reboot do host. A limpeza de dados, desligamento de plugins, mudanças de memória e correções de código são recomendações de revisão nesta versão.

## Dados e compatibilidade

- O banco do supervisor fica em `<HERMES_HOME>/evolution/`, com janela de 35 dias para métricas e de 90 dias para incidentes e ações encerradas. Backups e logs próprios do Hermes seguem sua retenção existente.
- Arquivos de notas da versão 0.1 são preservados. Os comandos que registram novas notas e o comando de chat legado exigem `legacy_memory_commands: true`. A injeção antiga de notas também exige `automatic_shared_recall: true`. Por padrão, a inspeção usa os sinais da memória nativa do Hermes.
- A tabela de tarifas em `rates_usd_per_million` aceita chaves por provedor e por modelo com valores `input`, `output`, `cache_read` e `cache_write`. O limite opcional `daily_estimated_budget_usd` usa o dia UTC e a parcela observada e precificada. Uma estimativa parcial pode divergir da fatura.
- Para retirar a integração, execute `ops rollback` antes de desabilitar o plugin com `hermes plugins disable hermes-evolution`, e recarregue o gateway do perfil.

```sh
python -m unittest discover -s tests -v
```

O contrato de integração foi verificado na árvore oficial v0.23.1, commit `3b421578588243f920886e6fe79a6faa87ea026e`. Os cenários de mudança e reversão de peso usam um adaptador de systemd simulado. Valide o controlador e a unidade escolhida em homologação antes de autorizar o modo `apply`.

[Hermes Agent v0.23.1](https://github.com/NousResearch/hermes-agent/releases/tag/v0.23.1) · [Hooks do Hermes](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/developer-guide/observer-hooks.md) · [Linux PSI](https://docs.kernel.org/accounting/psi.html) · [Linux cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html) · [systemctl](https://www.freedesktop.org/software/systemd/man/latest/systemctl.html) · [SentenceTransformers](https://sbert.net/docs/package_reference/sentence_transformer/model.html)
