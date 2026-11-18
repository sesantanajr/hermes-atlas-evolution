# Atualização

[English](../en-US/upgrading.md) | **Português (Brasil)**

## Atualizar o plugin

Todo comando diz o perfil em que roda: `default` para o perfil raiz, ou o nome do perfil.

```sh
hermes -p <perfil> evolution update --all          # todos os perfis; um perfil fixado passa para o último commit
hermes -p <perfil> evolution update                # escolha os perfis numa lista
hermes -p <perfil> evolution update --list         # versão, commit e fixação de cada perfil
sh install.sh --all --yes --restart                # o mesmo sem o plugin carregado
hermes gateway restart
```

O `update` só instala onde o plugin já está; acrescente `--install-missing` para instalá-lo nos perfis escolhidos que não o têm, e `--yes` para pular a pergunta.

Manualmente, um perfil por vez:

```sh
hermes plugins update hermes-atlas-evolution                 # o perfil padrão
hermes -p <perfil> plugins update hermes-atlas-evolution     # um perfil com nome

# Todos os perfis com nome
for p in $(ls ~/.hermes/profiles); do hermes -p "$p" plugins update hermes-atlas-evolution; done

hermes gateway restart
systemctl restart hermes-dashboard   # como root, ou do jeito que você roda o dashboard
hermes -p <perfil> evolution status
```

O `plugins update` pela linha de comando só troca os arquivos: não recarrega um gateway que já está rodando, então reinicie o gateway uma vez no fim. O dashboard mostra a aba **Evolution** quando relê os plugins; se ela não aparecer, reinicie o dashboard. (O Hermes recarrega um gateway em execução ao instalar, ligar e desligar, e quando a atualização é aplicada pelo Hermes Desktop, pelo dashboard ou por `/plugins update` num chat.) Se algo ainda estiver rodando (um turno, uma tarefa agendada do Hermes (cron), uma tarefa delegada), o reinício lista e pergunta `Interrupt them and restart the gateway? [y/N]`: responda N e rode de novo em alguns minutos, ou y para interromper. O `status` aponta um gateway que ainda roda código antigo.

**Todos os perfis de uma vez, sozinho.** No Modo Auto, o perfil que cuida do Hermes inteiro mantém a cópia do plugin em cada outro perfil no código dele: uma cópia fixada em outro commit, mais antiga, com outro código na mesma versão, ou deixada pela metade por uma atualização interrompida é levada ao commit do perfil líder com o comando do próprio Hermes (no máximo dois perfis por rodada, uma vez por situação, nova tentativa no dia seguinte se o Hermes recusar). Um perfil em que o plugin está desligado, ou em Assistido ou Manual, fica como está, e um link de desenvolvimento e um perfil com `fleet_managed` desligado também. Nada novo é baixado sozinho: uma cópia sem fixação só é atualizada enquanto o último commit do repositório for o que o líder roda (lido com `git ls-remote`), então atualize o líder (`hermes -p <perfil> evolution update`) e os outros acompanham. Cada sincronização fica no histórico (`hermes -p <perfil> evolution decisions`); reinicie o gateway uma vez para carregar o código novo.

O Hermes mantém a fixação quando um plugin é instalado de novo sem `--ref` (ele reinstala o commit fixado), e o `plugins update` dele recusa uma cópia fixada. Por isso o `hermes evolution update`, o `install.sh` e o Modo Auto movem a fixação com `hermes -p <perfil> plugins install <url> --force --ref <commit>`, o jeito do próprio Hermes; o perfil continua fixado, agora no código atual.

A maior parte das configurações vale em até um minuto. Algumas esperam o `hermes gateway restart` (no terminal: a próxima sessão): `runtime_guard`, `tool_hints`, e `agent_tools` ou `reflector` quando você o liga (desligar qualquer um dos dois vale sem reiniciar).

Se um perfil ainda tem o plugin com o nome antigo `hermes-evolution`, rode `hermes -p <perfil> plugins remove hermes-evolution` e depois `hermes -p <perfil> plugins install sesantanajr/hermes-atlas-evolution --enable`.

O que mudou em cada versão: [Mudanças](../../CHANGELOG.pt-BR.md).

## Atualizar para a 0.12.0

Atualize como acima e reinicie o gateway uma vez. Não há nada para migrar à mão.

- **No Hermes 0.23.2, atualize já.** O lançador novo dele faz a 0.11.0 e as anteriores recusarem os seus comandos de dono no seu próprio terminal ("Só o operador decide sobre lições e propostas…" em `owner`, `care doctor`, aprovar lições). A 0.12.0 o reconhece. Até atualizar, a aba Evolution do dashboard com login continua decidindo.
- **O banco ganha uma tabela.** A `lesson_measure` guarda quando o agente leu cada lição pela primeira vez, porque agora uma lição é medida a partir desse momento. Tabelas só são acrescentadas, então uma cópia mais antiga do plugin continua abrindo o banco. Uma lição que estava ativa mas nunca foi mostrada ao agente perde o veredito antigo e passa a ser medida a partir da primeira vez que aparecer; uma que já apareceu mantém a medição.
- **Dois valores vão para a configuração do Hermes.** `tool_hints: true` e um `reflector_daily_limit` acima de 6 agora só valem pela configuração do Hermes, como os ids do dono e as ferramentas do agente: no `<pasta do perfil>/evolution/settings.json`, que as ferramentas de arquivo do agente podem gravar, eles são ignorados, e o `status` os mostra em Pede atenção. Se você definiu um deles lá, defina de novo pelo seu próprio terminal e reinicie o gateway:

  ```sh
  hermes -p <perfil> config set plugins.entries.hermes-atlas-evolution.settings.tool_hints true
  hermes -p <perfil> config set plugins.entries.hermes-atlas-evolution.settings.reflector_daily_limit 12
  hermes gateway restart
  ```

- **Uma configuração nova, `fleet_managed`** (ligada por padrão). Desligue-a num perfil que o perfil que cuida de todos deve deixar em paz: ele então nunca instala, atualiza nem move nada para a lixeira ali sozinho, e continua mostrando o que encontra. `hermes -p <perfil> config set plugins.entries.hermes-atlas-evolution.settings.fleet_managed false`, nesse perfil. O `settings.json` só pode desligá-la.
- **Um `config.yaml` não lido vale Manual.** Quando o plugin não consegue ler o `config.yaml` (sem PyYAML no executor avulso, um arquivo acima de 1 MiB, um erro de YAML), ele age como no modo Manual e o `status` diz isso primeiro. Instale o PyYAML, ou rode o executor avulso com o Python do próprio Hermes.
- **Reparos do gateway.** Todo reparo do serviço do gateway, e todo desfazer, agora vai para um registro do Hermes inteiro (`<raiz>/evolution/gateway-repair.json`, os últimos 40). O registro único que a 0.9 a 0.11 guardavam continua sendo lido, então um reparo feito antes da atualização ainda pode ser desfeito com `hermes -p <perfil> evolution care undo-gateway`, de qualquer perfil.
- **Os comandos dizem o perfil.** Tudo o que o plugin mostra, no terminal, no menu, no painel e no resumo, aparece como `hermes -p <perfil> evolution …`. A saída de `--format json` não muda.
- **Quem contribui.** A suíte de testes fica no ramo `tests`; o [CONTRIBUTING.md](../../CONTRIBUTING.md#the-test-suite) explica como obtê-la.

## Atualizar para a 0.11.0

A 0.11.0 remove o que a 0.10.0 marcou como obsoleto. Atualize como acima; depois confira os dois pontos abaixo que podem afetar você: uma tarefa agendada antiga e as notas da versão 0.1.

| Removido | Use no lugar |
|---|---|
| `hermes -p <perfil> evolution ops ...`, o supervisor de operações de Linux da 0.3 (`inspect`, `tick`, `history`, `rollback`, `similar`, `review`, `feedback`, `plan`, `handoff`, `schedule-spec`, os ensaios de pesos de cgroup no Linux, o encaminhamento ao Kanban) | `status`, `care` e a aba **Saúde** do dashboard para a saúde do Hermes ([Manutenção](maintenance.md)); `usage` para as chamadas ao modelo e o custo delas ([Uso e custo](evolution-loop.md#uso-e-custo)) |
| `health` | `care check` |
| `tick` | Nada para rodar: a rodada de aprendizado roda sozinha, e o `learn` roda uma agora |
| `feedback` | Nada: ele avaliava os achados do antigo `tick` |
| `history` | `decisions` |
| `memory remember`, `recall`, `audit` e `forget`, o comando de chat `/evolution` e a recuperação automática das notas compartilhadas nos turnos (as notas da 0.1) | A memória do próprio Hermes; as notas que ainda estão no banco estão abaixo |

- **Configurações antigas são ignoradas.** `sensitivity`, `inspect_shared_kanban`, `kanban_handoff`, `linux_supervisor`, `semantic_incident_model`, `daily_estimated_budget_usd`, `legacy_memory_commands` e `automatic_shared_recall` não são mais lidas, nem no `settings.json` nem na configuração do Hermes. Um valor que ficou lá é ignorado, sem erro nem aviso; apague quando quiser. O `rates_usd_per_million` continua: o `usage` precifica as chamadas com ele.
- **As notas da 0.1 ficam no banco.** Nada mais as lê nem grava, e o agente não as recebe mais, mas elas não são apagadas. Enquanto houver alguma, o `status` mostra em Pede atenção "N notas da versão 0.1 continuam no banco do plugin", com um comando só de leitura que as mostra, para você copiar o que precisar (para a memória do Hermes, por exemplo). Para salvá-las em `evolution/notes-0.1.json`, ao lado do banco, e tirá-las dele, o `status` dá um segundo comando; depois de rodá-lo, o item some.
- **Uma tarefa agendada que rodava o tick antigo precisa sair.** Uma tarefa que roda `hermes -p <perfil> evolution ops tick` ou `hermes -p <perfil> evolution tick` agora falha em toda execução. A receita do `ops schedule-spec` a chamava de `hermes-atlas-evolution-watchdog`. O `status` encontra essa tarefa no agendador do Hermes e a mostra em Pede atenção, com o comando `cron remove` exato. Encontre-a com `hermes cron list` e remova-a com `hermes cron remove <tarefa>`, em cada perfil em que você a criou (`hermes -p <perfil> cron list`). Se você a agendou em outro agendador (um crontab, um timer do systemd), remova-a lá. A tarefa do resumo semanal (`hermes-atlas-evolution-digest`) continua.
- **O banco do supervisor.** O `evolution/supervisor.sqlite3` não é mais gravado nem lido. Apague-o se quiser o espaço de volta.
- **O executor independente** ainda aceita `--hermes-version` e o ignora, então um script que o passa continua rodando; você pode tirá-lo.
