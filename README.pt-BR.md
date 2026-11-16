![Atlas carregando uma esfera celeste sobre uma grade técnica preta. Atlas Evolution.](docs/banner.jpg)

# Atlas Evolution

Um ciclo de auto-melhoria medido para o Hermes Agent. Ele aprende com o histórico do próprio perfil já na primeira passada e depois roda sozinho: falhas de ferramenta que voltam a acontecer viram lições de uma frase para o agente, cada lição, reescrita de skill e mudança de configuração é julgada pelo que aconteceu antes e depois dela, e uma guarda de runtime impede que o agente derrube o Hermes em que ele mesmo roda. As lições escritas a partir de modelos fixos entram em vigor na hora e são aposentadas quando não ajudam; textos escritos por um modelo de IA ou pelo agente esperam a sua aprovação, e mudanças no próprio Hermes ficam com você. O Atlas Evolution é um plugin independente da comunidade: usa a API pública de plugins do Hermes e a biblioteca padrão do Python, e não abre conexão de rede própria.

[![Testes](https://github.com/sesantanajr/hermes-atlas-evolution/actions/workflows/tests.yml/badge.svg)](https://github.com/sesantanajr/hermes-atlas-evolution/actions/workflows/tests.yml)
[![Licença: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Hermes Agent 0.23.1 ou superior](https://img.shields.io/badge/Hermes%20Agent-%E2%89%A5%200.23.1-6f42c1.svg)](https://github.com/NousResearch/hermes-agent/releases/tag/v0.23.1)
[![Python 3.11 ou superior](https://img.shields.io/badge/python-%E2%89%A5%203.11-3776ab.svg)](https://www.python.org/downloads/)

[English](README.md) | **Português (Brasil)**

## Instalação

Exige o Hermes Agent 0.23.1 ou superior. O Hermes carrega os plugins por perfil, mesmo quando um único gateway atende todos, então instale o plugin em cada perfil que você usa.

```sh
# O perfil padrão
hermes plugins install sesantanajr/hermes-atlas-evolution --enable

# Um perfil com nome
hermes -p <perfil> plugins install sesantanajr/hermes-atlas-evolution --enable

# Todos os perfis com nome (o perfil padrão não fica nesta pasta: rode também o primeiro comando)
for p in $(ls ~/.hermes/profiles); do hermes -p "$p" plugins install sesantanajr/hermes-atlas-evolution --enable; done

# Confira
hermes evolution status --lang pt
```

A instalação carrega o plugin no gateway em execução; não é preciso reiniciar. O primeiro `status` num perfil já faz a primeira passada, que lê até 14 dias do histórico daquele perfil, então ele não começa do zero (`--no-learn` pula essa passada). O `status` também lista todos os perfis do mesmo Hermes e dá o comando de instalação para cada perfil usado nos últimos 7 dias que não tem o plugin. O `--lang pt` mostra a saída em português quando o perfil não está configurado em português.

Na lista de plugins do Hermes, o plugin aparece por enquanto com um cartão genérico: o Hermes só mostra capa para os itens do catálogo curado de plugins dele, revisado pela Nous Research ([detalhes](docs/pt-BR/hermes-integration.md#capa-e-o-catálogo-de-plugins)).

## Atualização

```sh
hermes plugins update hermes-atlas-evolution                # o perfil padrão
hermes -p <perfil> plugins update hermes-atlas-evolution    # cada um dos outros perfis
hermes gateway restart
hermes evolution status --lang pt
```

O `plugins update` baixa o código novo, mas não recarrega um gateway que já está rodando (o Hermes recarrega plugins na instalação, ao ligar e ao desligar um plugin), então reinicie o gateway uma vez no final. O reinício deixa os turnos em andamento terminarem antes; quando há sessões rodando, ele diz quais são e pergunta antes de seguir. O `status` aponta um gateway que ainda roda código antigo.

## Migração da 0.5.0

Até a 0.5.0, o plugin era instalado como `hermes-evolution`. O nome mudou, então o `hermes plugins update` não consegue substituir essa cópia. Em cada perfil que a tem, remova primeiro a cópia antiga e depois instale a nova (para um perfil com nome, acrescente `-p <perfil>` aos dois comandos):

```sh
hermes plugins remove hermes-evolution
hermes plugins install sesantanajr/hermes-atlas-evolution --enable
```

- O que foi aprendido fica: o comando `hermes evolution`, os dados em `<HERMES_HOME>/evolution/` e o `settings.json` mantêm os nomes.
- Enquanto a cópia antiga está ligada, a nova não registra nada, então nenhum turno é contado duas vezes. Se você instalou a cópia nova primeiro, rode `hermes gateway restart` depois de remover a antiga.
- O `plugins remove` também apaga o bloco `plugins.entries.hermes-evolution` do `config.yaml` do perfil. Se você mudou opções no formulário de configurações do Hermes, renomeie esse bloco para `hermes-atlas-evolution` antes.
- Dois padrões mudaram: `lessons` agora é `auto` (`lessons: suggest` mantém o comportamento da 0.5.0; as lições que já aguardavam continuam aguardando), e `tool_hints` agora é `false`.
- As ferramentas do agente e o toolset delas agora são `hermes_atlas_evolution_plan`, `hermes_atlas_evolution_lessons` e `hermes-atlas-evolution`. Atualize uma lista explícita de toolsets ou o `auxiliary.background_review.extra_tools` que cite os nomes antigos.

O `status` num perfil que já roda a 0.6.0 lista os perfis que ainda estão com o nome antigo, com esses dois comandos.

## O que ele faz

- **Aprende com o histórico que o Hermes já guarda.** As primeiras passadas reprocessam os últimos 14 dias de sessões do perfil, então uma falha que se repetiu na semana passada pode virar lição já no primeiro dia.
- **Transforma falhas de ferramenta repetidas em lições.** Uma falha vista pelo menos 3 vezes, em 2 sessões ou em 2 dias, vira uma frase como "Ao chamar `example_search`, informe sempre o argumento obrigatório `query`." O agente lê as lições ativas no início de cada sessão.
- **Mede antes e depois.** Cada lição contra a própria falha, cada reescrita de skill pelos turnos que carregaram a skill, cada mudança de configuração pela semana anterior e pela seguinte. No modo `auto`, uma lição que não ajuda é aposentada.
- **Consulta um modelo de IA quando nenhum modelo fixo serve.** O refletor rascunha uma lição por meio do Hermes, com no máximo 6 chamadas por dia no padrão, e o rascunho espera por você.
- **Acompanha a saúde do Hermes.** Sessões que terminam como órfãs, contexto pesado por chamada, compactações que liberam pouco espaço, plugins que falham ao carregar ou disputam o mesmo hook e falhas de ferramenta causadas por outro plugin, que nunca viram lição.
- **Protege o runtime.** Barra os comandos do agente que parariam, reiniciariam ou derrubariam o gateway, ou que apagariam o Python, o código ou o perfil em que o Hermes roda. O agente é orientado a deixar o reinício com você.
- **Roda sozinho e pergunta antes de mudar o Hermes.** Uma passada em segundo plano roda no máximo uma vez por hora em cada perfil. Mudanças no Hermes, como travar uma skill, viram propostas com o comando exato para você executar.

## Comandos

Subcomandos de `hermes evolution`. O `--lang en|pt` funciona com qualquer um deles.

| Comando | O que faz |
|---|---|
| `status` | Se o plugin está observando, aprendendo e protegendo este perfil, e a situação de cada perfil deste Hermes. |
| `report [--days N]` | O resumo dos últimos N dias (7 no padrão), comparado com o período anterior. |
| `lessons list` | As lições, com origem, evidência e efeito medido. |
| `lessons review` | Passa por todas as lições que aguardam aprovação, com o texto exato: `a` aprova, `r` rejeita, Enter pula. |
| `lessons approve <id>` | Ativa uma lição em espera depois de mostrar o texto exato. |
| `proposals list`, `proposals approve <id>`, `proposals reject <id>` | A fila de propostas e a sua decisão sobre uma delas. |
| `learn` | Roda uma passada agora, sem esperar a de segundo plano. |

Todos os comandos e opções: [Comandos](docs/pt-BR/evolution-loop.md#comandos).

## Configuração

Defina as opções no Hermes Desktop, em Settings → Plugins, ou no `config.yaml` do perfil, em `plugins.entries.hermes-atlas-evolution.settings`. Reinicie o gateway depois de uma mudança.

| Opção | Padrão | O que faz |
|---|---|---|
| `lessons` | `auto` | `auto`: as lições de modelo fixo entram em vigor na hora, são medidas e são aposentadas quando não ajudam; rascunhos do refletor e propostas do agente aguardam aprovação. `suggest`: toda lição aguarda aprovação. `off`: só mede. |
| `language` | `auto` | `en` ou `pt`. `auto` segue o idioma do perfil. |
| `reflector` | `true` | Rascunha lições com um modelo de IA, por meio do Hermes, para falhas que não têm modelo fixo. |
| `autopilot_minutes` | `60` | Roda a passada em segundo plano no máximo uma vez por intervalo, de 15 a 1440 minutos. Com `0`, ela só roda quando você chama `learn`. |
| `runtime_guard` | `true` | Barra os comandos do agente que parariam o gateway ou apagariam aquilo de que o Hermes precisa para rodar. |
| `agent_tools` | `false` | Oferece ao agente as ferramentas `hermes_atlas_evolution_plan` e `hermes_atlas_evolution_lessons`. |
| `tool_hints` | `false` | Também acrescenta a lição correspondente ao resultado de ferramenta que falhou. Vem desligado porque o Hermes só mantém a alteração do primeiro plugin num resultado de ferramenta. |
| `feedback_signals` | `true` | Conta as suas correções, perguntas repetidas e agradecimentos, só como números. |

```yaml
plugins:
  entries:
    hermes-atlas-evolution:
      settings:
        lessons: suggest
        language: pt
```

Todas as opções, o `settings.json` e o que acontece com um valor inválido: [Configuração e idioma](docs/pt-BR/evolution-loop.md#configuração-e-idioma).

## Privacidade e segurança

- **O que ele guarda.** Contadores, classes de falha, hashes dos IDs de sessão, frases de lição e propostas, em `<HERMES_HOME>/evolution/`. Não guarda texto de conversa, prompt, argumento ou saída de ferramenta, nem conteúdo de memória. Com o refletor ligado, guarda um trecho de erro limpo, de até 300 caracteres, por falha sem modelo fixo, até a falha ganhar uma lição.
- **O que ele lê fora dos próprios dados**, sempre só para leitura: o `state.db` do Hermes (o histórico das primeiras passadas e do radar de saúde), o `logs/agent.log` e as rotações dele, do `agent.log.1` ao `agent.log.5`, o ledger do Curator e o arquivo de uso das skills e, para o `status`, a contagem de sessões dos outros perfis do mesmo Hermes e o estado do próprio plugin neles. O supervisor de operações também lê contagens do Cron, do Kanban, dos arquivos de memória e do Linux ([lista completa](docs/pt-BR/hermes-integration.md#o-que-o-plugin-lê)).
- **Threads em segundo plano.** Em cada processo do Hermes, por perfil: um relógio que acorda a cada 10 minutos e a thread que roda a passada. Nenhuma transação fica aberta durante uma chamada ao modelo.
- **Chamadas a modelos.** Só o refletor chama um modelo, por meio do Hermes e das configurações de provedor dele (tarefa auxiliar `evolution_reflector`), no máximo 3 vezes por passada e 6 vezes por dia no padrão. Com `reflector: false`, ele para.
- **Rede.** Nenhuma conexão própria e nenhuma telemetria.
- **Chats do gateway.** O plugin não envia nada a um chat. Os avisos dele aparecem só nas suas superfícies locais (CLI, TUI, Desktop). O que o outro lado de um chat do gateway vê é o que o agente escreve.
- **Decisões.** Aprovar, rejeitar e ativar se recusam a rodar no terminal do agente. O plugin só altera os próprios dados; mudanças no Hermes são impressas como comandos para você.

Detalhes no [SECURITY.md](SECURITY.md) (em inglês).

## Documentação

- [O ciclo de evolução](docs/pt-BR/evolution-loop.md): como as lições são criadas, entregues e medidas, o refletor, o radar de saúde, o resumo com um exemplo, o `status` e todos os comandos e opções.
- [Integração com o Hermes](docs/pt-BR/hermes-integration.md): hooks, a guarda de runtime, o que o plugin lê, permissões, notas de instalação, compatibilidade e limites.
- [Operações](docs/pt-BR/operations.md): o supervisor de operações (pressão no Linux, uso e custo, Cron e Kanban).
- [CONTRIBUTING.md](CONTRIBUTING.md) (em inglês): desenvolvimento, testes e as regras para mudanças.
- [SECURITY.md](SECURITY.md) (em inglês): modelo de ameaças, o que o plugin guarda e lê, e como relatar uma vulnerabilidade.
- [CHANGELOG.md](CHANGELOG.md) (em inglês): o que mudou em cada versão.

## Licença

O Atlas Evolution é software livre e de código aberto, publicado sob a [licença MIT](LICENSE) (identificador SPDX `MIT`). Você pode usar, copiar, modificar, mesclar, publicar, distribuir, sublicenciar e vender cópias, inclusive para uso comercial, desde que o aviso de copyright e o texto da licença acompanhem todas as cópias. O software é fornecido como está, sem garantia de qualquer tipo.

Contribuições são bem-vindas sob a mesma licença; veja o [CONTRIBUTING.md](CONTRIBUTING.md).

## Autor

Criado por **Sérgio Sant'Ana Júnior**. [LinkedIn](https://www.linkedin.com/in/sergiosantanacloud/)
