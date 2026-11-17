# Atualização

[English](../en-US/upgrading.md) | **Português (Brasil)**

## Atualizar o plugin

```sh
hermes evolution update --all          # todos os perfis; um perfil fixado passa para o último commit
hermes evolution update                # escolha os perfis numa lista
hermes evolution update --list         # versão, commit e fixação de cada perfil
sh install.sh --all --yes --restart    # o mesmo sem o plugin carregado
hermes gateway restart
```

Manualmente, um perfil por vez:

```sh
hermes plugins update hermes-atlas-evolution                 # o perfil padrão
hermes -p <perfil> plugins update hermes-atlas-evolution     # um perfil com nome

# Todos os perfis com nome
for p in $(ls ~/.hermes/profiles); do hermes -p "$p" plugins update hermes-atlas-evolution; done

hermes gateway restart
sudo systemctl restart hermes-dashboard   # ou do jeito que você roda o dashboard
hermes evolution status
```

O `plugins update` pela linha de comando só troca os arquivos: não recarrega um gateway que já está rodando, então reinicie o gateway uma vez no fim. O dashboard mostra a aba **Evolution** quando relê os plugins; se ela não aparecer, reinicie o dashboard. (O Hermes recarrega um gateway em execução ao instalar, ligar e desligar, e quando a atualização é aplicada pelo Hermes Desktop, pelo dashboard ou por `/plugins update` num chat.) Se algo ainda estiver rodando (um turno, uma tarefa agendada do Hermes (cron), uma tarefa delegada), o reinício lista e pergunta `Interrupt them and restart the gateway? [y/N]`: responda N e rode de novo em alguns minutos, ou y para interromper. O `status` aponta um gateway que ainda roda código antigo.

**Todos os perfis de uma vez, sozinho.** No Modo Auto, o perfil que cuida do Hermes inteiro mantém a cópia do plugin em cada outro perfil no código dele: uma cópia fixada em outro commit, mais antiga, com outro código na mesma versão, ou deixada pela metade por uma atualização interrompida é levada ao commit do perfil líder com o comando do próprio Hermes (no máximo dois perfis por rodada, uma vez por situação, nova tentativa no dia seguinte se o Hermes recusar). Um perfil em que o plugin está desligado, ou em Assistido ou Manual, fica como está, e um link de desenvolvimento também. Nada novo é baixado sozinho: uma cópia sem fixação só é atualizada enquanto o último commit do repositório for o que o líder roda, então atualize o líder (`hermes evolution update`) e os outros acompanham. Cada sincronização fica no histórico (`hermes evolution decisions`); reinicie o gateway uma vez para carregar o código novo.

O Hermes mantém a fixação quando um plugin é instalado de novo sem `--ref` (ele reinstala o commit fixado), e o `plugins update` dele recusa uma cópia fixada. Por isso o `hermes evolution update`, o `install.sh` e o Modo Auto movem a fixação com `hermes -p <perfil> plugins install <url> --force --ref <commit>`, o jeito do próprio Hermes; o perfil continua fixado, agora no código atual.

A maior parte das configurações vale em até um minuto. Três esperam o `hermes gateway restart` (no terminal: a próxima sessão): `runtime_guard`, `tool_hints` e `agent_tools` quando você o liga (desligar vale na hora).

Se um perfil ainda tem o plugin com o nome antigo `hermes-evolution`, rode `hermes -p <perfil> plugins remove hermes-evolution` e depois `hermes -p <perfil> plugins install sesantanajr/hermes-atlas-evolution --enable`.

O que mudou em cada versão: [Mudanças](../../CHANGELOG.pt-BR.md).

## Atualizar para a 0.11.0

A 0.11.0 remove o que a 0.10.0 marcou como obsoleto. Atualize como acima; depois confira os dois pontos abaixo que podem afetar você: uma tarefa agendada antiga e as notas da versão 0.1.

| Removido | Use no lugar |
|---|---|
| `hermes evolution ops ...`, o supervisor de operações de Linux da 0.3 (`inspect`, `tick`, `history`, `rollback`, `similar`, `review`, `feedback`, `plan`, `handoff`, `schedule-spec`, os ensaios de pesos de cgroup no Linux, o encaminhamento ao Kanban) | `status`, `care` e a aba **Saúde** do dashboard para a saúde do Hermes ([Manutenção](maintenance.md)); `usage` para as chamadas ao modelo e o custo delas ([Uso e custo](evolution-loop.md#uso-e-custo)) |
| `health` | `care check` |
| `tick` | Nada para rodar: a rodada de aprendizado roda sozinha, e o `learn` roda uma agora |
| `feedback` | Nada: ele avaliava os achados do antigo `tick` |
| `history` | `decisions` |
| `memory remember`, `recall`, `audit` e `forget`, o comando de chat `/evolution` e a recuperação automática das notas compartilhadas nos turnos (as notas da 0.1) | A memória do próprio Hermes; as notas que ainda estão no banco estão abaixo |

- **Configurações antigas são ignoradas.** `sensitivity`, `inspect_shared_kanban`, `kanban_handoff`, `linux_supervisor`, `semantic_incident_model`, `daily_estimated_budget_usd`, `legacy_memory_commands` e `automatic_shared_recall` não são mais lidas, nem no `settings.json` nem na configuração do Hermes. Um valor que ficou lá é ignorado, sem erro nem aviso; apague quando quiser. O `rates_usd_per_million` continua: o `usage` precifica as chamadas com ele.
- **As notas da 0.1 ficam no banco.** Nada mais as lê nem grava, e o agente não as recebe mais, mas elas não são apagadas. Enquanto houver alguma, o `status` mostra no que precisa de atenção "N notas da versão 0.1 continuam no banco do plugin", com um comando só de leitura que as mostra, para você copiar o que precisar (para a memória do Hermes, por exemplo). Para salvá-las em `evolution/notes-0.1.json`, ao lado do banco, e tirá-las dele, o `status` dá um segundo comando; depois de rodá-lo, o item some.
- **Uma tarefa agendada que rodava o tick antigo precisa sair.** Uma tarefa que roda `hermes evolution ops tick` ou `hermes evolution tick` agora falha em toda execução. A receita do `ops schedule-spec` a chamava de `hermes-atlas-evolution-watchdog`. O `status` encontra essa tarefa no agendador do Hermes e a mostra no que precisa de atenção, com o comando `cron remove` exato. Encontre-a com `hermes cron list` e remova-a com `hermes cron remove <tarefa>`, em cada perfil em que você a criou (`hermes -p <perfil> cron list`). Se você a agendou em outro agendador (um crontab, um timer do systemd), remova-a lá. A tarefa do resumo semanal (`hermes-atlas-evolution-digest`) continua.
- **O banco do supervisor.** O `evolution/supervisor.sqlite3` não é mais gravado nem lido. Apague-o se quiser o espaço de volta.
- **O executor independente** ainda aceita `--hermes-version` e o ignora, então um script que o passa continua rodando; você pode tirá-lo.
